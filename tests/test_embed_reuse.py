"""RULE 1: a document whose file changed and whose chunks did not keeps its
vectors (2026-10-09: Project Gutenberg regenerated its files and discovery
reset 23,718 books whose chunks were byte-identical to what was stored).

Reuse needs all of: the chunk's id AND text equal to a stored row's (or, on
a recut, its text's hash — 06 §6.3), a TEXT-form chunk (a pair layer's
vector is its fields', which no stored row keeps), and the document's
`embed_version` current (`embeddings.may_reuse`). What reached the encoder
and what was kept is counted (`EmbedCounts`) and logged per call.
"""
import numpy as np
import pytest

from app import collections as collections_mod, config, db, embeddings, worker
from app import layers as layers_mod
from app.hold.codex import Chunker, Layer
from tests.hold.conftest import pubmed


@pytest.fixture
def collection(tmp_path, isolated_config):
    return collections_mod.Collection(
        name="g", plugin="gutenberg", profile=None, embed_mode_default=None,
        corpus_dir=tmp_path / "corpus", manifest_path=tmp_path / "g" / "m.db",
        fts_dir=tmp_path / "g" / "fts", lance_dir=tmp_path / "g" / "lance",
        embed=collections_mod.EmbedRecipe(url="http://enc:1", api="openai", model="m",
                                          dtype="f32", ctx=512, dim=4))


def _chunks(texts, uid="gutenberg:1"):
    return [{"id": f"{uid}:{i}:1", "seq": i, "chapter": "", "text": t}
            for i, t in enumerate(texts)]


@pytest.fixture
def encoder(monkeypatch):
    """What reaches the encoder, item by item; each text a distinct vector."""
    calls: list = []
    answered = iter(range(1, 10**6))     # every answer distinct, cleared or not

    def fake(items, recipe=None, **kw):
        calls.extend(items)
        return [[float(len(str(t))), float(next(answered)), 2.0, 3.0] for t in items]
    monkeypatch.setattr(embeddings, "_embed_texts", fake)
    return calls


def _rows(collection) -> dict:
    tbl = embeddings._conn(collection).open_table(embeddings.TABLE)
    return {r["id"]: (r["text"], np.asarray(r["vector"]).tobytes())
            for r in tbl.to_arrow().to_pylist()}


def _version(collection) -> int:
    return embeddings._conn(collection).open_table(embeddings.TABLE).version


# ---- the write ------------------------------------------------------------------

def test_an_identical_recut_calls_no_encoder_and_writes_nothing(collection, encoder):
    embeddings.upsert_book(1, "en", _chunks(["a", "b", "c"]), collection)
    before, version = _rows(collection), _version(collection)
    encoder.clear()

    counts = embeddings.EmbedCounts()
    n = embeddings.upsert_book(1, "en", _chunks(["a", "b", "c"]), collection,
                               counts=counts)
    assert encoder == [], "every chunk is stored with its id and text"
    assert n == 3 and (counts.embedded, counts.reused, counts.chars) == (0, 3, 0)
    assert _rows(collection) == before
    assert _version(collection) == version, "nothing to write, so no commit"


def test_one_changed_text_embeds_that_chunk_alone(collection, encoder):
    embeddings.upsert_book(1, "en", _chunks(["alpha", "beta", "gamma"]), collection)
    before = _rows(collection)
    encoder.clear()

    counts = embeddings.EmbedCounts()
    embeddings.upsert_book(1, "en", _chunks(["alpha", "BETA!", "gamma"]), collection,
                           counts=counts)
    assert encoder == ["BETA!"]
    assert (counts.embedded, counts.reused, counts.chars) == (1, 2, 5)
    after = _rows(collection)
    assert set(after) == set(before)
    for cid in ("gutenberg:1:0:1", "gutenberg:1:2:1"):
        assert after[cid] == before[cid], "kept: the stored bytes, not a recomputation"
    assert after["gutenberg:1:1:1"][0] == "BETA!"
    assert after["gutenberg:1:1:1"][1] != before["gutenberg:1:1:1"][1]


@pytest.mark.parametrize("dtype", ["f16", "f32"])
def test_a_carried_vector_keeps_its_stored_bytes(collection, encoder, dtype):
    """Read as numpy rows of the table's own type (`_held_columns`), carried
    to a new id by text hash: the bytes written are the bytes stored."""
    from dataclasses import replace
    collection = replace(collection, embed=replace(collection.embed, dtype=dtype))
    embeddings.upsert_book(1, "en", _chunks(["alpha", "beta"]), collection)
    before = _rows(collection)
    embeddings.upsert_book(1, "en", _chunks(["NEW", "alpha", "beta"]), collection)
    after = _rows(collection)
    assert after["gutenberg:1:1:1"][1] == before["gutenberg:1:0:1"][1]
    assert after["gutenberg:1:2:1"][1] == before["gutenberg:1:1:1"][1]
    held = embeddings.held_chunks(collection, [1]).of(1)
    assert held["gutenberg:1:1:1"]["vector"].dtype == (
        np.float16 if dtype == "f16" else np.float32)


@pytest.mark.parametrize("old,new,embedded", [
    (["a", "b"], ["a", "b", "c", "d"], ["c", "d"]),      # grew
    (["a", "b", "c", "d"], ["a", "b"], []),              # shrank
])
def test_a_changed_chunk_count_embeds_the_new_and_drops_the_gone(
        collection, encoder, old, new, embedded):
    embeddings.upsert_book(1, "en", _chunks(old), collection)
    before = _rows(collection)
    encoder.clear()

    counts = embeddings.EmbedCounts()
    embeddings.upsert_book(1, "en", _chunks(new), collection, counts=counts)
    assert encoder == embedded
    assert (counts.embedded, counts.reused) == (len(embedded), len(new) - len(embedded))
    after = _rows(collection)
    assert set(after) == {c["id"] for c in _chunks(new)}, "no stale row survives"
    for cid in set(before) & set(after):
        assert after[cid] == before[cid]


def test_reuse_false_embeds_everything_and_replaces_every_row(collection, encoder):
    """The vectors are another EMBED_VERSION's: none of them is kept."""
    embeddings.upsert_book(1, "en", _chunks(["a", "b", "c"]), collection)
    before = _rows(collection)
    encoder.clear()

    counts = embeddings.EmbedCounts()
    embeddings.upsert_book(1, "en", _chunks(["a", "b"]), collection, reuse=False,
                           counts=counts)
    assert encoder == ["a", "b"]
    assert (counts.embedded, counts.reused) == (2, 0)
    after = _rows(collection)
    assert set(after) == {"gutenberg:1:0:1", "gutenberg:1:1:1"}
    assert all(after[c][1] != before[c][1] for c in after), "recomputed, not kept"


@pytest.mark.parametrize("version,allowed", [(0, True), (None, True), ("current", True),
                                             ("older", False), ("newer", False)])
def test_may_reuse_only_at_the_current_embed_version(version, allowed, monkeypatch):
    monkeypatch.setattr(config, "EMBED_VERSION", 3)
    v = {"current": 3, "older": 2, "newer": 4}.get(version, version)
    assert embeddings.may_reuse({"embed_version": v}) is allowed


def test_a_row_without_its_version_is_not_reused():
    assert embeddings.may_reuse({"uid": "g:1"}) is False


# ---- a pair layer ----------------------------------------------------------------

@pytest.fixture
def pair_layer(monkeypatch):
    """The collection's one layer feeds the encoder pairs (09 §9.3)."""
    layers = pubmed(version=5, layers=(Layer(Chunker("abstract", 1, {}), "default",
                                             {"form": "pair", "split": "first-section"}),
                                       )).layers
    monkeypatch.setattr(layers_mod, "layers_for", lambda collection: layers)


def _pair(first, rest, uid="pubmed:1"):
    return [{"id": f"{uid}:0:1", "seq": 0, "chapter": "",
             "text": "\n\n".join(x for x in (first, rest) if x), "fields": (first, rest)}]


def test_a_pair_chunk_is_never_reused_by_id_and_text(collection, encoder, pair_layer):
    embeddings.upsert_book(1, "en", _pair("T", "A"), collection)
    encoder.clear()
    counts = embeddings.EmbedCounts()
    embeddings.upsert_book(1, "en", _pair("T", "A"), collection, counts=counts)
    assert encoder == [("T", "A")], "the stored row cannot show the fields it was made of"
    assert (counts.embedded, counts.reused) == (1, 0)


def test_one_text_two_pairs_never_shares_a_vector(collection, encoder, pair_layer):
    """`chunk_abstract` joins the sections' bodies: ("A", "B\\n\\nC") and
    ("A\\n\\nB", "C") cut to one text, and are two encoder inputs."""
    embeddings.upsert_book(1, "en", _pair("A", "B\n\nC"), collection)
    first = _rows(collection)["pubmed:1:0:1"]
    encoder.clear()
    embeddings.upsert_book(1, "en", _pair("A\n\nB", "C"), collection)
    assert encoder == [("A\n\nB", "C")], "carried by text hash would be another pair's vector"
    after = _rows(collection)["pubmed:1:0:1"]
    assert after[0] == first[0] and after[1] != first[1]


# ---- the worker ------------------------------------------------------------------

class FakePlugin:
    source_id = "fake"

    def fetch(self, ref):
        from app.plugins.base import RawDoc
        return RawDoc(ref=ref, raw_hash="h", data=ref.native_id.encode())

    def parse(self, raw):
        from app.plugins.base import CanonicalDoc
        nid = raw.data.decode()
        return CanonicalDoc(
            uid=f"fake:{nid}", title=f"doc {nid}", creators=[], language="en",
            date=None, subjects=[], keys=[], extra={},
            sections=[("Abstract", f"abstract of {nid}")], raw_hash="h")


@pytest.fixture
def fake_collection(isolated_config, tmp_path, monkeypatch):
    monkeypatch.setitem(collections_mod.PLUGIN_REGISTRY, "fake", FakePlugin())
    c = collections_mod.Collection(
        name="fake", plugin="fake", profile=None, embed_mode_default=None,
        corpus_dir=tmp_path / "corpus", manifest_path=tmp_path / "manifest.db",
        fts_dir=tmp_path / "fts", lance_dir=tmp_path / "lancedb",
        embed=collections_mod.EmbedRecipe(
            url="http://cpu-encoder:1", api="openai", model="m",
            dtype="f16", ctx=512, dim=8, concurrency=2))
    con = db.connect(c)
    for i in range(1, 5):
        con.execute(
            "INSERT INTO documents(uid,source,native_id,path,lang,in_scope,"
            "rdf_done,fts_done,embed_done) VALUES(?,?,?,?,?,1,1,1,0)",
            (f"fake:{i}", "fake", str(i), f"/x/{i}", "en"))
    scope = db.get_scope(con)
    scope["embed_mode"] = "all"
    db.set_scope(con, scope)
    return c


@pytest.fixture
def texts(monkeypatch):
    seen: list = []

    def fake(items, recipe=None, **kw):
        seen.extend(items)
        return [[1.0] + [0.0] * 7 for _ in items]
    monkeypatch.setattr(embeddings, "_embed_texts", fake)
    return seen


def _reset_by_discovery(con):
    """What discovery writes when a file's bytes changed (discovery.py)."""
    con.execute("UPDATE documents SET rdf_done=1, fts_done=1, embed_done=0")


def _last_embed_event(con) -> str:
    return con.execute("SELECT message FROM events WHERE stage='embed' "
                       "ORDER BY id DESC LIMIT 1").fetchone()[0]


def test_the_parallel_stage_reuses_a_reset_document_s_vectors(fake_collection, texts):
    con = db.connect(fake_collection)
    recipe = collections_mod.recipe_for(fake_collection)
    assert worker._embed_parallel(con, fake_collection, recipe) is True
    assert len(texts) == 4
    assert "4 chunks embedded, 0 reused" in _last_embed_event(con)

    _reset_by_discovery(con)
    texts.clear()
    assert worker._embed_parallel(con, fake_collection, recipe) is True
    assert texts == [], "every chunk identical: no encoder call at all"
    assert con.execute("SELECT COUNT(*) FROM documents WHERE embed_done=1 "
                       "AND embed_version=?", (config.EMBED_VERSION,)).fetchone()[0] == 4
    assert "0 chunks embedded, 4 reused" in _last_embed_event(con)


def test_the_parallel_stage_re_embeds_another_version_s_vectors(
        fake_collection, texts, monkeypatch):
    con = db.connect(fake_collection)
    recipe = collections_mod.recipe_for(fake_collection)
    worker._embed_parallel(con, fake_collection, recipe)
    texts.clear()

    monkeypatch.setattr(config, "EMBED_VERSION", config.EMBED_VERSION + 1)
    _reset_by_discovery(con)
    worker._embed_parallel(con, fake_collection, recipe)
    assert len(texts) == 4, "an older version's vectors are made again, never kept"
    assert "4 chunks embedded, 0 reused" in _last_embed_event(con)


def test_stage_embed_reuses_and_says_so(fake_collection, texts):
    con = db.connect(fake_collection)
    row = con.execute("SELECT * FROM documents WHERE uid='fake:1'").fetchone()
    worker.stage_embed(con, row, fake_collection)
    assert len(texts) == 1
    assert _last_embed_event(con) == "embedded 1 chunks, reused 0"

    _reset_by_discovery(con)
    texts.clear()
    row = con.execute("SELECT * FROM documents WHERE uid='fake:1'").fetchone()
    worker.stage_embed(con, row, fake_collection)
    assert texts == []
    assert _last_embed_event(con) == "embedded 0 chunks, reused 1"
    assert con.execute("SELECT embed_done FROM documents WHERE uid='fake:1'").fetchone()[0] == 1


def test_a_wholly_reused_document_is_not_encoder_throughput(fake_collection, texts,
                                                            monkeypatch):
    """`stats.record_fetch` skips adopted files for the same reason: a
    reused chunk is not the encoder's work, and counted as such it would
    report a speed no encoder has."""
    from app import stats
    recorded: list = []
    monkeypatch.setattr(stats.recorder(), "record_embed",
                        lambda name, **kw: recorded.append(kw))
    con = db.connect(fake_collection)
    row = con.execute("SELECT * FROM documents WHERE uid='fake:1'").fetchone()
    worker.stage_embed(con, row, fake_collection)
    assert [r["chunks"] for r in recorded] == [1]
    _reset_by_discovery(con)
    worker.stage_embed(con, con.execute(
        "SELECT * FROM documents WHERE uid='fake:1'").fetchone(), fake_collection)
    assert len(recorded) == 1, "nothing reached the encoder, nothing is recorded"
