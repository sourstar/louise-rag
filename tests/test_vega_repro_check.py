"""tools/vega_repro_check.py: invariant 12 measured — each text alone, over
fresh loads and shuffled orders, compared as float32 bytes."""
from __future__ import annotations

import base64
import json

import pytest

from app.vega import tag as vtag
from tools import vega_repro_check


class Answer:
    def __init__(self, status, body):
        self.status_code, self.content = status, json.dumps(body).encode()
        self.text = self.content.decode()


def backend(*, drift_on_load=None):
    """A fake LocalAI: the vector of a text is its bytes, unless the text is
    `drift_on_load[0]` on load `drift_on_load[1]`."""
    log = {"shutdowns": 0, "sent": []}

    def post(url, json, timeout):
        if url.endswith("/backend/shutdown"):
            log["shutdowns"] += 1
            return Answer(200, {})
        assert url == "http://localai:30286/v1/embeddings" and json["encoding_format"] == "base64"
        (tagged,) = json["input"]
        _id, _form, content, window = vtag.parse_window(tagged)
        log["sent"].append((content, window))
        raw = content.encode().ljust(8, b"\0")[:8]
        if drift_on_load and content == drift_on_load[0] and log["shutdowns"] == drift_on_load[1]:
            raw = raw[:-1] + b"\1"
        return Answer(200, {"data": [{"index": 0, "embedding": base64.b64encode(raw).decode()}]})
    return post, log


TEXTS = ["Cold ash in the grate", "Refer', inquis, ad senatum", "Fra den Tid"]


def test_every_text_alone_over_fresh_loads_and_orders_is_one_byte_string():
    post, log = backend()
    out = vega_repro_check.measure(TEXTS, url="http://localai:30286/", model="vega-bge-m3",
                                   loads=2, repeats=2, window=1024, post=post, sleep=lambda s: None)
    assert out == {"texts": 3, "vectors": 12, "identical": 3, "differ": [], "max_abs": {},
                   "non_finite": []}
    assert log["shutdowns"] == 2, "each load begins afresh"
    assert sorted(log["sent"]) == sorted([(t, 1024) for t in TEXTS] * 4), "alone, with the window"
    orders = [tuple(c for c, _ in log["sent"][i:i + 3]) for i in range(0, 12, 3)]
    assert len(set(orders)) > 1, "the passes are in different orders"


def test_a_text_that_changes_on_a_fresh_load_is_named():
    post, _log = backend(drift_on_load=("Fra den Tid", 2))
    out = vega_repro_check.measure(TEXTS, url="http://localai:30286", model="vega-bge-m3",
                                   loads=2, repeats=1, window=None, post=post, sleep=lambda s: None)
    assert out["differ"] == [2] and out["identical"] == 2
    assert out["max_abs"][2] > 0


def test_a_refused_text_stops_the_measurement():
    def post(url, json, timeout):
        return Answer(200, {}) if url.endswith("/shutdown") else Answer(500, {"error": "no"})
    with pytest.raises(RuntimeError, match="HTTP 500"):
        vega_repro_check.measure(TEXTS, url="http://localai:30286", model="m", loads=1,
                                 repeats=1, window=None, post=post, sleep=lambda s: None)


def test_the_texts_are_spread_over_the_file():
    assert vega_repro_check.spread(list("abcdefgh"), 4) == list("aceg")
    assert vega_repro_check.spread(list("ab"), 4) == list("ab")


def test_a_line_is_a_text_or_a_pair():
    """MedCPT's article encoder is fed (title, abstract) pairs: measured as pairs."""
    assert vega_repro_check.item({"text": "a"}) == "a"
    assert vega_repro_check.item({"pair": ["title", "abstract"]}) == ("title", "abstract")


def test_the_sweep_passes_through_every_length_band_up_to_the_window():
    """A kernel not reproducible at some shapes is found only by an input of
    that shape: MedCPT's 32 measured pairs had none of 65–73 tokens, the band
    where the GPU plugin moved (2026-10-30)."""
    long = " ".join(f"w{i}" for i in range(1000))
    texts = ["short", long, "mid " * 20]
    got = vega_repro_check.sweep(texts, 512, 512)
    assert len(got) == 512 and [len(t.split()) for t in got] == list(range(1, 513))
    coarse = vega_repro_check.sweep(texts, 100, None)
    sizes = [len(t.split()) for t in coarse]
    assert sizes[0] == 1 and sizes[-1] == 1000 and len(sizes) == 100
    pairs = [("T", long), ("U", "a b")]
    got = vega_repro_check.sweep(pairs, 3, 10)
    assert got == [("T", "w0"), ("T", " ".join(f"w{i}" for i in range(5))),
                   ("T", " ".join(f"w{i}" for i in range(10)))]
    assert vega_repro_check.sweep(texts, 0, 512) == []


def test_a_model_still_loading_is_waited_for_and_one_that_never_loads_is_not_measured():
    """München, 2026-10-30: after /backend/shutdown LocalAI answered qwen3-4b
    "Unavailable … connection refused" for seconds, every text failed, and a
    harness reading "no differ printed" as clean would have resumed gutenberg
    on eight failed runs."""
    good, _log = backend()
    refusals = {"left": 3}

    def post(url, json, timeout):
        if url.endswith("/v1/embeddings") and refusals["left"]:
            refusals["left"] -= 1
            return Answer(500, {"error": "rpc error: code = Unavailable desc = connection refused"})
        return good(url, json, timeout)
    slept = []
    out = vega_repro_check.measure(TEXTS, url="http://localai:30286", model="m", loads=1,
                                   repeats=1, window=None, post=post, sleep=slept.append)
    assert out["identical"] == 3 and refusals["left"] == 0
    assert slept[0] == vega_repro_check.SETTLE_S and slept.count(1.0) == 3
    now = [0.0]

    def never(url, json, timeout):
        return Answer(200, {}) if url.endswith("/shutdown") else Answer(
            500, {"error": "rpc error: code = Unavailable"})

    def tick(s):
        now[0] += s
    with pytest.raises(RuntimeError, match="HTTP 500"):
        vega_repro_check.measure(TEXTS, url="http://localai:30286", model="m", loads=1,
                                 repeats=1, window=None, post=never, sleep=tick,
                                 clock=lambda: now[0])
    assert now[0] >= vega_repro_check.LOAD_WAIT_S, "bounded, and only then failed"


def test_the_exit_code_says_reproducible_differ_or_not_measured(tmp_path, monkeypatch, capsys):
    import sys
    import types
    f = tmp_path / "t.jsonl"
    f.write_text("\n".join(json.dumps({"text": t}) for t in TEXTS), encoding="utf-8")
    monkeypatch.setattr(vega_repro_check.time, "sleep", lambda s: None)
    args = ["--url", "http://localai:30286", "--model", "m", "--texts", str(f), "--sweep", "0",
            "--loads", "1", "--repeats", "1"]
    post, _ = backend()
    monkeypatch.setitem(sys.modules, "httpx", types.SimpleNamespace(post=post))
    assert vega_repro_check.main(args) == 0 and "REPRODUCIBLE" in capsys.readouterr().out

    def refused(url, json, timeout):
        return Answer(200, {}) if url.endswith("/shutdown") else Answer(400, {"error": "bad"})
    monkeypatch.setitem(sys.modules, "httpx", types.SimpleNamespace(post=refused))
    assert vega_repro_check.main(args) == 2 and "NOT MEASURED" in capsys.readouterr().out


def test_a_nan_that_comes_back_the_same_on_every_load_is_not_reproducible(tmp_path, monkeypatch, capsys):
    """München, 2026-10-31: a NaN identical on every load is one byte string,
    and read as "identical" — a wrong answer passing as clean."""
    import struct
    import sys
    import types
    good, _ = backend()

    def nan_for_one(url, json, timeout):
        r = good(url, json, timeout)
        if url.endswith("/v1/embeddings"):
            (tagged,) = json["input"]
            if vtag.parse_window(tagged)[2] == "Fra den Tid":
                nan = struct.pack("<2f", float("nan"), 0.0)
                return Answer(200, {"data": [{"index": 0, "embedding": base64.b64encode(nan).decode()}]})
        return r
    out = vega_repro_check.measure(TEXTS, url="http://localai:30286", model="m", loads=2,
                                   repeats=1, window=None, post=nan_for_one, sleep=lambda s: None)
    assert out["differ"] == [] and out["non_finite"] == [2]
    f = tmp_path / "t.jsonl"
    f.write_text("\n".join(json.dumps({"text": t}) for t in TEXTS), encoding="utf-8")
    monkeypatch.setattr(vega_repro_check.time, "sleep", lambda s: None)
    monkeypatch.setitem(sys.modules, "httpx", types.SimpleNamespace(post=nan_for_one))
    args = ["--url", "http://localai:30286", "--model", "m", "--texts", str(f), "--sweep", "0",
            "--loads", "1", "--repeats", "1"]
    assert vega_repro_check.main(args) == 1
    assert "NON-FINITE" in capsys.readouterr().out
