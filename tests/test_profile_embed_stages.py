"""`louise dev embedder-profile --stages` (tools/profile_embed.py): each step
of one request timed through the app's own path, VeGa's and a raw model
server's, and the arms' vectors compared — the instrument that splits
VeGa's turnaround (2026-11-02: 125 ms a text through VeGa, 81.5 raw)."""
from __future__ import annotations

import base64
import hashlib
import json
import struct
from types import SimpleNamespace

import pytest

from app.vega import client as vclient
from app.vega import tag as vtag
from tools import profile_embed as pe

RUN = "sha256:" + "ab" * 32
VEGA = "http://vega"


def _vector(text: str) -> bytes:
    h = hashlib.sha256(text.encode("utf-8")).digest()
    return struct.pack("<8f", *[(b - 128) / 64 for b in h[:8]])


class FakeLocalAI:
    """LocalAI's /v1/embeddings for two models: `vega-*` takes tagged inputs
    and leaves a receipt per answer, as the wrapper does; any other takes
    them plain. `tamper` changes a vector after its receipt is written."""

    def __init__(self, tamper: bool = False) -> None:
        self.receipts: dict[str, dict[str, str]] = {}
        self.bodies: list[dict] = []
        self.tamper = tamper

    def post(self, url, json=None, timeout=None):
        assert url == "http://localai/v1/embeddings" and json["encoding_format"] == "base64"
        self.bodies.append(json)
        data = []
        for i, inp in enumerate(json["input"]):
            if json["model"].startswith("vega-"):
                tid, form, content, window = vtag.parse_window(inp)
                vec = _vector(content)
                self.receipts[tid] = {"id": tid, "run_id": RUN,
                                      "input_digest": vtag.input_digest(form, content, window),
                                      "vector_digest": vtag.vector_digest(vec)}
                if self.tamper:
                    vec = bytes([vec[0] ^ 1]) + vec[1:]
            else:
                assert vtag.MARK not in inp, "a raw model server is sent the text untagged"
                vec = _vector(inp)
            data.append({"index": i, "embedding": base64.b64encode(vec).decode()})
        body = {"data": data, "usage": {"prompt_tokens": 7 * len(data)}}
        return SimpleNamespace(content=_json(body), raise_for_status=lambda: None)

    def get(self, url, params=None, timeout=None):
        assert url == f"{VEGA}/vega/receipts"
        ids = params["ids"].split(",")
        body = {"receipts": [self.receipts[i] for i in ids if i in self.receipts]}
        return SimpleNamespace(status_code=200, json=lambda: body)


def _json(doc) -> bytes:
    return json.dumps(doc).encode()


def _args(**kw):
    base = {"url": "http://localai", "model": ["vega-m", "m-raw"], "texts": None,
            "chars": 300, "api": None, "vega_url": VEGA, "window": None, "pool": None,
            "max_chars": None, "texts_file": None, "warmup": 2}
    return SimpleNamespace(**(base | kw))


def test_each_path_times_its_own_steps_and_the_arms_are_compared():
    fake = FakeLocalAI()
    texts = ["first text", "second text", "third text"]
    vega = pe.run_arm(texts, url="http://localai", model="vega-m", api="vega", vega_url=VEGA,
                      window=None, warmup=0, post=fake.post, get=fake.get)
    raw = pe.run_arm(texts, url="http://localai", model="m-raw", api="openai", vega_url=None,
                     window=None, warmup=0, post=fake.post, get=fake.get)
    assert set(vega.ms) == {"tag", "post", "parse", "receipts", "verify"}
    assert set(raw.ms) == {"post", "parse"}
    assert all(len(v) == 3 for v in vega.ms.values()) and vega.tokens == 21
    assert pe.compare(vega, raw) == (pytest.approx(1.0), 3)
    lines = pe.report([vega, raw])
    assert any(line.startswith("  receipts") for line in lines)
    assert lines[-1].startswith("m-raw vs vega-m: worst cosine 1.00000000, 3/3 bit-identical")


def test_a_vector_its_receipt_does_not_vouch_for_is_refused_as_the_app_refuses_it():
    fake = FakeLocalAI(tamper=True)
    with pytest.raises(vclient.Refusal, match="vector digest"):
        pe.staged_request("a text", url="http://localai", model="vega-m", api="vega",
                          vega_url=VEGA, window=None, post=fake.post, get=fake.get)


def test_a_window_is_carried_in_the_tag():
    fake = FakeLocalAI()
    pe.staged_request("a text", url="http://localai", model="vega-m", api="vega",
                      vega_url=VEGA, window=512, post=fake.post, get=fake.get)
    assert vtag.parse_window(fake.bodies[-1]["input"][0])[3] == 512


def test_a_vega_model_without_a_receipts_url_is_refused_before_a_number_is_given():
    fake = FakeLocalAI()
    with pytest.raises(SystemExit, match="receipts cannot be read"):
        pe.staged_request("a text", url="http://localai", model="vega-m", api="vega",
                          vega_url=None, window=None, post=fake.post, get=fake.get)


def test_the_whole_command_leases_each_model_and_warms_it_on_other_texts(tmp_path):
    fake = FakeLocalAI()
    f = tmp_path / "texts.jsonl"
    f.write_text('"one"\n{"text": "two"}\n\n"three"\n', encoding="utf-8")
    leased, lines = [], []

    class Lease:
        def __init__(self, model, api):
            leased.append((model, api))

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    rc = pe.stages_main(_args(texts_file=str(f), texts=2), post=fake.post, get=fake.get,
                        lease=Lease, out=lines.append)
    assert rc == 0
    assert leased == [("vega-m", "vega"), ("m-raw", "openai")], "api follows the vega- prefix"
    sent = [inp for b in fake.bodies if b["model"] == "m-raw" for inp in b["input"]]
    assert sent[-2:] == ["one", "two"] and len(sent) == 4, "two warm-up texts, then the two"
    assert all(t not in ("one", "two") for t in sent[:2])
    assert lines[0].startswith("2 texts")
    assert "2/2 bit-identical" in lines[-1]


def test_texts_from_a_pool_or_a_file_never_both(tmp_path):
    with pytest.raises(SystemExit, match="not both"):
        pe.read_texts(_args(pool=str(tmp_path), texts_file=str(tmp_path / "x")))
    assert len(pe.read_texts(_args(texts=5))) == 5, "synthetic, with a nonce"
