"""tools/cross_vendor_check.py: the cross-vendor deviation row of an encoder,
measured on a machine that holds no collection (operator, 2026-10-21: the
NVIDIA row, on a rented GPU). The vectors come from the gate itself, and the
twin is sent the very texts the gate sent."""
from __future__ import annotations

import math

from app import embeddings
from tests.hold.conftest import KEY, NOW, PLATFORM, fake_embed, fake_query_embed
from tests.hold.test_template import recipe
from tools import cross_vendor_check as cvc


def _held(tmp_path):
    from app.hold import collection as col
    root = tmp_path / "collections"
    col.create_from_recipe(root, "gutenberg", recipe(), platform_image=PLATFORM, embed=fake_embed,
                           embed_query=fake_query_embed, name="gutenberg", key=KEY, now=NOW)
    return root


def _endpoint(monkeypatch, sent):
    """The models behind the URL: the card answers as calibration's encoder
    did; the CPU twin a little off, as a CPU build is."""
    queried: set = set()

    def batch(texts, timeout, recipe, **kw):
        sent.append((recipe.model, list(texts)))
        if recipe.model == "card":
            return fake_embed(texts)
        if recipe.model == "card-q":
            queried.update(texts)
            return fake_query_embed(texts)
        # the twin: the same function as the card on that side, a little off
        base = fake_query_embed(texts) if texts[0] in queried else fake_embed(texts)
        return [[x + (0.01 if i == 0 else 0.0) for i, x in enumerate(v)] for v in base]
    monkeypatch.setattr(embeddings, "_embed_batch", batch)


def test_export_then_measure_prints_the_row_and_the_twin_gets_the_gates_texts(tmp_path, monkeypatch):
    root = _held(tmp_path)
    space = tmp_path / "space"
    written = cvc.export(root, "gutenberg", space)
    assert [p.name for p in written][0] == "codex.json" and written[1].suffix == ".safetensors"

    sent: list = []
    _endpoint(monkeypatch, sent)
    said: list[str] = []
    out = cvc.measure(space, url="http://card:30286", api="openai", model="card",
                      query_model="card-q", twin="cpu-twin", twin_texts=16, say=said.append)

    for role in ("document", "query"):
        whole = out["roles"][role]["whole_set"]
        assert whole["n"] == 512 and math.isclose(whole["worst"], 1.0, abs_tol=1e-6)
        assert whole["below"] == 0
        twin = out["roles"][role]["twin"]
        assert twin["n"] == 16 and twin["worst"] < 1.0 and twin["below"] == 0
    assert any(line.startswith("query: whole set vs reference:") for line in said)

    # the twin was sent exactly what the gate sent the card, side by side, at
    # the spread places (the query side's form is the gate's: tested in
    # tests/hold/test_query_instruction.py)
    card_d = [t for model, batch in sent if model == "card" for t in batch]
    card_q = [t for model, batch in sent if model == "card-q" for t in batch]
    twin = [t for model, batch in sent if model == "cpu-twin" for t in batch]
    picked = cvc.spread(16, 512)
    assert twin == [card_d[i] for i in picked] + [card_q[i] for i in picked]


def test_a_twin_slower_than_its_limit_reports_what_it_has(tmp_path, monkeypatch):
    root = _held(tmp_path)
    space = tmp_path / "space"
    cvc.export(root, "gutenberg", space)
    _endpoint(monkeypatch, [])
    said: list[str] = []
    out = cvc.measure(space, url="http://card:30286", api="openai", model="card",
                      query_model="card-q", twin="cpu-twin", twin_texts=64, twin_limit_s=0.0,
                      say=said.append)
    assert out["roles"]["document"]["twin"]["n"] == 0
    assert any("stopped at its limit after 0 of 64" in line for line in said)
