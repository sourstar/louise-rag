"""A check re-embeds a pair layer's chunk as the pair its record gives back.

docs/notes/input-forms.md (operator, 2026-10-08); 03 §3.5 as amended. The
checks that re-cut a shard's records — T3 on a bundle's import, `hold check
vectors-sample`, `hold vectors check` — keep each chunk's `fields` beside its
text (`land.cut`), and hand the encoder the chunk (`land.input_of`), so a pair
layer is checked in the form it was written in. The legacy ladder's T3 and the
admission re-embed take the same as `inputs`. Text layers hand over their
text exactly as before. And the pair calibration set is drawn from a real
SHARD's records (`calibration.pair_set_from_shard`).
"""
from pathlib import Path

import numpy as np
import pytest

from app import collections as collections_mod, embeddings, layers as layers_mod
from app.bundles import checks
from app.hold import admit, calibration as cal, check as chk, collection as col, \
    land as landing, records as rec, snapshot as snap
from app.hold.codex import Chunker, Layer
from app.verify import t3
from tests.hold.conftest import KEY, NOW, PLATFORM, fake_embed, fake_query_embed, pubmed
from tests.hold.test_pair_calibration import PAIR_BYTES, pair_embed, pair_recipe
from tests.hold.test_template import recipe

RECORDS = [{"uid": f"gutenberg:{n}", "native_id": n, "lang": "en",
            "sections": [["Title", f"Title {n}"], ["Abstract", f"Abstract {n}."],
                         ["Methods", f"Methods {n}."]]} for n in range(1, 4)]


class Pairs:
    """Anything `layers.layers_for` reads: one abstract layer feeding pairs."""
    name = "pairs"
    layers = (layers_mod.Layer("abstract", 1, "default", input=dict(layers_mod.INPUT_PAIR)),)


class Texts:
    name = "texts"
    layers = (layers_mod.Layer("abstract", 1, "default"),)


def test_the_cut_keeps_a_pair_layer_s_fields_and_only_its():
    pair = landing.cut(Pairs, RECORDS)
    text = landing.cut(Texts, RECORDS)
    assert set(pair) == set(text), "the form moves no chunk id"
    cid = next(iter(pair))
    assert pair[cid]["fields"] == ("Title 1", "Abstract 1.\n\nMethods 1.")
    assert all("fields" not in e for e in text.values()), "a text layer's entry as it was"
    assert landing.input_of(pair, cid) is pair[cid]
    assert landing.input_of(text, cid) == text[cid]["text"]
    assert landing.input_of(text, "absent:0:0:1") is None


def _vectors_of(cut_):
    """The vectors a pair-fed encoder wrote for these chunks."""
    return {cid: pair_embed([tuple(e["fields"])])[0] for cid, e in cut_.items()}


def _pair_fed(items):
    assert all(isinstance(x, dict) and x.get("fields") for x in items), items[:1]
    return pair_embed([tuple(x["fields"]) for x in items])


def _flattened(items):
    return pair_embed([x["text"] for x in items])


def test_t3_on_import_re_embeds_the_pairs():
    cut = landing.cut(Pairs, RECORDS * 1)
    stored = _vectors_of(cut)
    codex = pubmed()
    # sixteen chunks are what T3 draws; pad the bundle with clones of the three
    accepted = [(cid, stored[cid]) for cid in cut] * 6
    accepted = [(f"{cid}#{i}" if i else cid, v) for i, (cid, v) in enumerate(accepted)]
    cut_x = {cid: cut[cid.split("#")[0]] for cid, _ in accepted}
    tier = admit.tier3(codex, accepted, text_of=lambda c: landing.input_of(cut_x, c),
                       embed=_pair_fed, seed=b"\x07" * 32)
    assert tier.verdict == admit.PASS, tier.why
    tier = admit.tier3(codex, accepted, text_of=lambda c: landing.input_of(cut_x, c),
                       embed=_flattened, seed=b"\x07" * 32)
    assert tier.verdict == admit.FAIL, "a flattened re-embed is another function"


def test_the_legacy_ladder_and_the_admission_re_embed_take_the_pairs_as_inputs():
    cut = landing.cut(Pairs, RECORDS)
    ids = list(cut)
    stored = _vectors_of(cut)
    vectors = [stored[c] for c in ids]
    texts = {c: e["text"] for c, e in cut.items()}
    inputs = {c: layers_mod.chunk_input(e) for c, e in cut.items()}
    ok = checks.check_sample_reembed(ids, vectors, texts, _pair_fed, inputs=inputs)
    assert ok.passed, ok.detail
    seen = []
    checks.check_sample_reembed(ids, vectors, texts, lambda b: seen.extend(b) or
                                [stored[c] for c in ids][:len(b)])
    assert all(isinstance(x, str) for x in seen), "without inputs, the texts, as ever"
    fed = []
    t3.check_spot_recompute(ids, vectors, None, texts=texts, inputs=inputs,
                            embed_fn=lambda b: fed.extend(b) or _pair_fed(b))
    assert fed and all(isinstance(x, dict) for x in fed)


def test_embed_chunks_feeds_a_re_cut_entry_as_a_pair(monkeypatch):
    seen = []
    monkeypatch.setattr(embeddings, "_embed_texts",
                        lambda items, **kw: seen.extend(items) or [[1.0]] * len(items))
    cut = landing.cut(Pairs, RECORDS)
    recipe_ = collections_mod.EmbedRecipe(url="http://x", api="openai", model="",
                                          dtype="f32", ctx=512)
    embeddings.embed_chunks(Pairs, [landing.input_of(cut, c) for c in cut], recipe=recipe_)
    assert seen == [("Title 1", "Abstract 1.\n\nMethods 1."),
                    ("Title 2", "Abstract 2.\n\nMethods 2."),
                    ("Title 3", "Abstract 3.\n\nMethods 3.")]


# ---- against a real SHARD: `hold check vectors-sample`, and drawing the set ------------

@pytest.fixture
def pair_shard(isolated_config, tmp_path, corpus_dir):
    root = tmp_path / "collections"
    codex = col.create_from_recipe(root, "gutenberg", pair_recipe(), platform_image=PLATFORM,
                                   embed=pair_embed, embed_query=fake_query_embed, name="pairs",
                                   key=KEY, now=NOW, pairs=PAIR_BYTES)
    rec.build(root, "pairs", corpus_dir=corpus_dir, log=lambda *_: None, records_per_shard=2)
    rec.write_validation(root, "pairs")
    snapshot = snap.create(root, "pairs", log=lambda *_: None)
    return root, codex, snapshot


def test_vectors_sample_re_embeds_a_pair_layer_s_chunks_as_pairs(pair_shard):
    root, codex, snapshot = pair_shard
    cut = {}
    for s in snapshot.shards:
        cut.update(landing.cut(codex, landing.records_for(
            root, "pairs", snapshot, f"{s.slice}/{s.n:05d}", None)))
    assert cut and all("fields" in e for e in cut.values())
    stored = _vectors_of(cut)
    vectors_for = (lambda ids: {i: stored[i] for i in ids if i in stored})
    for embed, want in ((_pair_fed, chk.PASS), (_flattened, chk.FAIL)):
        ctx = chk.Ctx(root, "pairs", codex, snapshot, b"\x05" * 32, override="1",
                      embed=embed, vectors_for=vectors_for)
        assert chk.check_vectors_sample(ctx).result == want


def test_the_pair_set_is_drawn_from_the_collection_s_own_records(pair_shard, monkeypatch):
    """`tools/build_pair_calibration_set.py`'s rule over a real SHARD: fixed by
    the snapshot, cut through the layers, the same bytes twice."""
    root, codex, snapshot = pair_shard
    shard_dir = col.shard_dir_of(root, "pairs")
    records = [r for s in snapshot.shards
               for r in landing.records_for(root, "pairs", snapshot,
                                            f"{s.slice}/{s.n:05d}", None)]
    candidates = list(cal.pair_candidates(codex, records))
    admissible = [c for c in candidates if not cal._pair_fault(c[1], c[2])]
    assert len(admissible) >= 2, "the fixture corpus holds pairs to draw"
    monkeypatch.setattr(cal, "SIZE", 2)
    data, provenance = cal.pair_set_from_shard(shard_dir, codex, snapshot)
    assert cal.pair_set_from_shard(shard_dir, codex, snapshot)[0] == data
    pairs = cal.pairs_of(data, provenance["set"])
    assert len(pairs) == 2 and set(provenance["uids"]) <= {c[0] for c in admissible}
    assert provenance["snapshot_hash"] == snapshot.hash
    assert all(p in {(a, b) for _, a, b in admissible} for p in pairs)


def test_the_tool_writes_the_set_and_its_provenance(pair_shard, tmp_path, monkeypatch, capsys):
    import json

    from tools import build_pair_calibration_set as tool
    root, codex, snapshot = pair_shard
    monkeypatch.setattr(cal, "SIZE", 2)
    out, prov = tmp_path / "pairs.jsonl", tmp_path / "pairs.sources.json"
    assert tool.main([str(root), "pairs", "--out", str(out), "--provenance", str(prov)]) == 0
    data = out.read_bytes()
    sources = json.loads(prov.read_text(encoding="utf-8"))
    assert sources["set"] == cal.set_id(data) and sources["snapshot_id"] == snapshot.snapshot_id
    assert cal.set_id(data) in capsys.readouterr().out
    assert data == cal.pair_set_from_shard(col.shard_dir_of(root, "pairs"), codex, snapshot)[0]


def test_the_pair_set_may_be_drawn_from_a_text_collection_s_records(isolated_config, tmp_path,
                                                                     corpus_dir, monkeypatch):
    """Before the pair Codex exists — its calibration set is in it — the set is
    drawn from the records of the collection that embeds text today, each cut
    in the pair form of its own layer."""
    root = tmp_path / "collections"
    col.create_from_recipe(root, "gutenberg",
                           recipe(layers=[{"chunker": {"id": "abstract", "version": 1}}]),
                           platform_image=PLATFORM, embed=fake_embed,
                           embed_query=fake_query_embed, name="texts", key=KEY, now=NOW)
    rec.build(root, "texts", corpus_dir=corpus_dir, log=lambda *_: None, records_per_shard=2)
    rec.write_validation(root, "texts")
    snapshot = snap.create(root, "texts", log=lambda *_: None)
    monkeypatch.setattr(cal, "SIZE", 2)
    texts = col.read(root, "texts")
    data, provenance = cal.pair_set_from_shard(col.shard_dir_of(root, "texts"), texts, snapshot)
    assert len(cal.pairs_of(data)) == 2
    assert provenance["collection_id"] == texts.collection_id
    # a collection cut by a chunker no pair is admitted on has no pairs to draw
    with pytest.raises(cal.CalibrationError, match="no layer"):
        cal.pair_set_from_shard(col.shard_dir_of(root, "texts"),
                               pubmed(layers=(Layer(Chunker("book", 1, {})),)), snapshot)
