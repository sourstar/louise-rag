"""tools/endpoint_vectors.py and app/hold/fingerprint.py.

The model pool replaces the per-model apps in one cut-over, and they are shut off
BEFORE it loads (operator, 2026-09-19). Its first proof is that its vectors
match today's endpoints — so today's are recorded first, while they still
answer, and compared after.
"""
import hashlib
import json
import math

import numpy as np
import pytest

from app import collections as collections_mod, embeddings
from app.hold import calibration as cal, fingerprint
from app.hold.codex import digest
from tools import endpoint_vectors as tool


# ---- the fingerprint (09 §9.12) ----------------------------------------------

def test_the_sixteen_are_the_shortest_the_longest_and_fourteen_between():
    _set_id, texts = cal.load("text")
    picked = fingerprint.indices(texts)
    assert len(picked) == 16
    order = sorted(range(512), key=lambda i: (len(texts[i].encode("utf-8")), i))
    assert picked == [order[(k * 511) // 15] for k in range(16)], "ranks ⌊k × 511 / 15⌋"
    sizes = [len(texts[i].encode("utf-8")) for i in picked]
    assert sizes == sorted(sizes) and sizes[0] == min(map(len, (t.encode() for t in texts)))
    assert sizes[-1] == max(len(t.encode("utf-8")) for t in texts)


def test_ties_in_length_are_broken_by_position():
    texts = ["bb", "aa", "c", "dddd"]           # "bb" and "aa" tie at two bytes
    order = [2, 0, 1, 3]
    assert fingerprint.indices(texts) == [order[(k * 3) // 15] for k in range(16)]


def test_the_fingerprint_is_the_hash_of_the_vectors_as_little_endian_f32():
    vectors = [[float(i), 0.5, -1.25] for i in range(16)]
    expected = digest(b"".join(np.asarray(v, dtype="<f4").tobytes() for v in vectors))
    assert fingerprint.of(vectors) == expected
    assert fingerprint.of(vectors[::-1]) != expected, "in that order"
    with pytest.raises(ValueError, match="16"):
        fingerprint.of(vectors[:15])


# ---- one text a request, as the endpoint returned it ---------------------------

def _fake_encoder(monkeypatch, *, scale=3.0, wrong_beyond=None):
    """A deterministic 'model': a vector from the text's hash, NOT unit
    length, so normalisation would show. `wrong_beyond`: it answers wrongly for
    inputs longer than that many characters — right on a sentence, wrong on a
    full window, which is what happened here."""
    calls = []

    def request(batch, timeout, recipe):
        calls.append((recipe.url, list(batch)))
        out = []
        for text in batch:
            h = hashlib.sha256(text.encode("utf-8")).digest()
            v = [scale * (b - 127.5) / 127.5 for b in h[:8]]
            if wrong_beyond is not None and len(text) > wrong_beyond:
                v = [-x for x in v]
            out.append(v)
        return out

    monkeypatch.setattr(embeddings, "_request_vectors", request)
    return calls


def test_each_text_goes_alone_and_comes_back_unnormalised(monkeypatch, isolated_config):
    calls = _fake_encoder(monkeypatch)
    recipe = collections_mod.recipe_for(collections_mod.default_collection())
    vectors = embeddings._embed_one_at_a_time(["one", "two", "three"], recipe=recipe)
    assert [batch for _url, batch in calls] == [["one"], ["two"], ["three"]]
    norm = math.sqrt(sum(x * x for x in vectors[0]))
    assert abs(norm - 1.0) > 0.1, "as the endpoint returned it"
    unit = embeddings._embed_texts(["one"], recipe=recipe)[0]
    assert abs(math.sqrt(sum(x * x for x in unit)) - 1.0) < 1e-6, "the worker's path still normalises"


# ---- record, then compare --------------------------------------------------------

def test_what_is_recorded_is_the_sixteen_first_then_an_even_spread():
    _set_id, texts = cal.load("text")
    picked = tool.chosen(texts, 64)
    assert picked[:16] == fingerprint.indices(texts)
    assert len(picked) == len(set(picked)) and 70 <= len(picked) <= 80


def test_the_same_engine_compares_bit_for_bit(monkeypatch, isolated_config, tmp_path, capsys):
    _fake_encoder(monkeypatch)
    out = tmp_path / "rec"
    assert tool.main(["record", "--out", str(out), "--texts", "8"]) == 0
    name = collections_mod.default_collection().name
    meta = json.loads((out / f"{name}.document.json").read_text())
    assert meta["fingerprint"].startswith("sha256:") and len(meta["indices"]) >= 16
    assert (out / f"{name}.document.safetensors").is_file()

    assert tool.main(["compare", "--against", str(out), "--tolerance", "0.999"]) == 0
    said = capsys.readouterr().out
    assert "SAME, bit for bit" in said and "every text within it" in said


def test_a_bi_encoder_pair_is_two_recordings_each_from_its_own_endpoint(
        monkeypatch, isolated_config, tmp_path):
    """MedCPT's article and query encoders are different models. The first
    version named both files alike and the query side overwrote the
    document side's."""
    import dataclasses
    calls = _fake_encoder(monkeypatch)
    pair = dataclasses.replace(
        collections_mod.recipe_for(collections_mod.default_collection()),
        url="http://nas:30443", query_url="http://nas:30444")
    monkeypatch.setattr(collections_mod, "recipe_for", lambda _c: pair)
    out = tmp_path / "rec"
    assert tool.main(["record", "--out", str(out), "--texts", "8"]) == 0
    name = collections_mod.default_collection().name
    sides = {side: json.loads((out / f"{name}.{side}.json").read_text())
             for side in ("document", "query")}
    assert sides["document"]["url"] == "http://nas:30443"
    assert sides["query"]["url"] == "http://nas:30444"
    assert {url for url, _b in calls} == {"http://nas:30443", "http://nas:30444"}
    assert len(list(out.glob("*.safetensors"))) == 2

    calls.clear()
    new = "http://nas:30437/upstream/medcpt-query"
    assert tool.main(["compare", "--against", str(out), "--url", f"{name}.query={new}"]) == 0
    assert {url for url, _b in calls} == {"http://nas:30443", new}


def test_another_engine_is_judged_by_its_cosines(monkeypatch, isolated_config, tmp_path, capsys):
    """Judged by the Codex's own `space.tolerance` (03 §3.1), which wins over
    any `--tolerance`."""
    calls = _fake_encoder(monkeypatch)
    out = tmp_path / "rec"
    tool.main(["record", "--out", str(out), "--texts", "8"])
    # the full window: the longest text as truncated to the Codex's ctx
    window = max(len(text) for _url, batch in calls for text in batch)

    # the same function at another scale: not bit-equal, and within tolerance
    _fake_encoder(monkeypatch, scale=2.0)
    assert tool.main(["compare", "--against", str(out)]) == 0
    said = capsys.readouterr().out
    assert "different" in said and "tolerance    0.95" in said

    # wrong on the full window only — what a short check never sees
    assert window > 1_000
    _fake_encoder(monkeypatch, wrong_beyond=window - 1)
    assert tool.main(["compare", "--against", str(out)]) == 1
    assert "below it" in capsys.readouterr().out


def test_an_endpoint_that_does_not_answer_is_a_deferral(monkeypatch, isolated_config,
                                                        tmp_path, capsys):
    def down(batch, timeout, recipe):
        raise ConnectionRefusedError("nothing listens")

    monkeypatch.setattr(embeddings, "_request_vectors", down)
    assert tool.main(["record", "--out", str(tmp_path / "rec")]) == 3
    assert "deferred" in capsys.readouterr().err


def test_compare_can_be_pointed_at_the_new_endpoint_before_the_files_are_switched(
        monkeypatch, isolated_config, tmp_path):
    """The proof runs with the model pool up and the collection files still naming the
    old ports: `--url` names the model on the model pool."""
    calls = _fake_encoder(monkeypatch)
    out = tmp_path / "rec"
    tool.main(["record", "--out", str(out), "--texts", "8"])
    calls.clear()
    name = collections_mod.default_collection().name
    new = "http://nas:30437/upstream/qwen3-embedding-4b"
    assert tool.main(["compare", "--against", str(out), "--url", f"{name}={new}/"]) == 0
    assert {url for url, _batch in calls} == {new}
    with pytest.raises(SystemExit):
        tool.main(["compare", "--against", str(out), "--url", "gutenberg"])
