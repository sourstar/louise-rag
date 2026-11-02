"""The conversion checks for a candidate encoder (operator, 2026-11-02: "Do the
same exercise of harrier with Nemotron 1b embedder, I want it on llama.cpp
with 16bit precision"): tools/measure/f16_range.py and st_parity.py."""
from __future__ import annotations

import json
import struct

import numpy as np
import pytest

from tools.measure import f16_range, st_parity


def _safetensors(path, tensors: dict[str, tuple[str, bytes, list[int]]]) -> None:
    header, blob, at = {}, b"", 0
    for name, (dtype, raw, shape) in tensors.items():
        header[name] = {"dtype": dtype, "shape": shape, "data_offsets": [at, at + len(raw)]}
        blob += raw
        at += len(raw)
    h = json.dumps(header).encode()
    path.write_bytes(struct.pack("<Q", len(h)) + h + blob)


def _bf16(values) -> bytes:
    f = np.asarray(values, dtype="<f4")
    return (f.view("<u4") >> 16).astype("<u2").tobytes()


def test_a_value_past_f16s_range_is_counted_and_fails(tmp_path):
    p = tmp_path / "m.safetensors"
    _safetensors(p, {"a": ("BF16", _bf16([1.0, -2.5, 70000.0, 1e-9, 2.0 ** -20]), [5]),
                     "b": ("F32", np.asarray([0.5, 3.0], dtype="<f4").tobytes(), [2]),
                     "ids": ("I64", np.asarray([1, 2], dtype="<i8").tobytes(), [2])})
    rows = {r["tensor"]: r for r in f16_range.scan(p)}
    assert set(rows) == {"a", "b"}, "integer tensors are not cast"
    assert rows["a"]["overflow"] == 1 and rows["a"]["to_zero"] == 1 and rows["a"]["subnormal"] == 1
    assert rows["a"]["max_abs"] == pytest.approx(70000.0, rel=1e-2)
    assert rows["b"]["overflow"] == rows["b"]["to_zero"] == 0
    assert f16_range.main([str(p)]) == 1


def test_weights_within_range_pass(tmp_path):
    p = tmp_path / "ok.safetensors"
    _safetensors(p, {"w": ("BF16", _bf16(np.linspace(-3, 3, 64)), [64])})
    assert f16_range.main([str(p)]) == 0
    s = f16_range.summary(f16_range.scan(p))
    assert s["values"] == 64 and s["overflow"] == 0 and s["max_abs"] == pytest.approx(3.0)


def _sample(tmp_path):
    corpus = [{"uid": f"u{i}", "chunks": ["x" * (i + 1), "y" * (40 - i)]} for i in range(20)]
    qs = [{"uid": f"u{i}", "lang": "en" if i % 3 else "it", "query": f"q{i}"} for i in range(30)]
    (tmp_path / "corpus.jsonl").write_text("".join(json.dumps(e) + "\n" for e in corpus))
    (tmp_path / "queries.jsonl").write_text("".join(json.dumps(q) + "\n" for q in qs))


def test_pick_spreads_the_documents_over_their_lengths_and_alternates_languages(tmp_path):
    _sample(tmp_path)
    got = st_parity.pick(tmp_path, docs=8, queries=6)
    docs = [t["text"] for t in got if t["side"] == "document"]
    assert len(docs) == 8 and len(set(docs)) == 8
    assert min(map(len, docs)) == 1 and max(map(len, docs)) == 40, "the shortest and the longest"
    qs = [t for t in got if t["side"] == "query"]
    assert [q["lang"] for q in qs] == ["en", "it", "en", "it", "en", "it"]
    assert got == st_parity.pick(tmp_path, docs=8, queries=6), "deterministic"


def test_compare_passes_only_when_every_text_reaches_the_bar_on_both_sides():
    texts = [{"side": "document", "text": "a"}, {"side": "document", "text": "b"},
             {"side": "query", "text": "q"}]
    ref = np.eye(3, 4, dtype="float32")
    near = ref + np.array([[0, 0.01, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0.001]], dtype="float32")
    res = st_parity.compare(texts, ref, near)
    assert res["verdict"] == "PASS" and res["below_pass"] == []
    assert res["to_read"] == [], "cosines 0.99995 and 0.9999995: above both bars"
    assert res["document"]["n"] == 2 and res["query"]["n"] == 1
    off = ref.copy()
    off[2] = np.array([0, 0, 0.6, 0.8], dtype="float32")      # the query side moved
    bad = st_parity.compare(texts, ref, off)
    assert bad["verdict"] == "FAIL" and bad["below_pass"] == [2]
    assert bad["document"]["worst"] == pytest.approx(1.0)
    with pytest.raises(SystemExit, match="another dimension"):
        st_parity.compare(texts, ref, np.eye(3, 2))


def test_a_text_between_the_bars_is_flagged_to_read():
    texts = [{"side": "document", "text": "a"}]
    ref = np.asarray([[1.0, 0.0]], dtype="float32")
    got = np.asarray([[1.0, 0.1]], dtype="float32")             # cosine 0.995
    res = st_parity.compare(texts, ref, got)
    assert res["verdict"] == "PASS" and res["to_read"] == [0]


def test_the_candidate_is_embedded_as_the_ab_tool_embeds_it(monkeypatch):
    from tools.measure import wikidata_encoder_ab as ab
    seen = []

    class Lease:
        def __enter__(self): return self
        def __exit__(self, *e): return False

    def encode(texts, query):
        seen.append((query, list(texts)))
        return [[float(query), 1.0] for _ in texts]
    monkeypatch.setattr(ab, "encoder_fn", lambda enc: (encode, Lease()))
    texts = [{"side": "query", "text": "q"}, {"side": "document", "text": "d"}]
    got = st_parity.candidate(texts, "nemotron")
    assert got == [[1.0, 1.0], [0.0, 1.0]], "each text in its own place"
    assert seen == [(False, ["d"]), (True, ["q"])]


def test_the_reference_step_imports_nothing_of_the_app():
    """It runs in a throwaway venv with torch (invariant 7: never the app
    image): the module must load with the standard library alone."""
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(st_parity))
    top = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    names = {a.name.split(".")[0] for n in top if isinstance(n, ast.Import) for a in n.names} | \
        {(n.module or "").split(".")[0] for n in top if isinstance(n, ast.ImportFrom)}
    assert names <= {"__future__", "argparse", "json", "sys", "pathlib"}
