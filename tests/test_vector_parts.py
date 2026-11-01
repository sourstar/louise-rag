"""One vector table per value of a search key (app/vector_parts.py;
operator, 2026-11-01: "Different tables, make it a standard practice when
separated searches might be done"; docs/notes/vector-tables-split.md).

A collection whose plugin declares a table key keeps one table per value:
every write lands in its document's part, every read and delete by document
reaches that part only, and a search reads the parts its filter selects and
merges them — giving the order one exact search over one table gives."""
from __future__ import annotations

import json
import threading

import numpy as np
import pytest

pytest.importorskip("lancedb")

from app import collections as collections_mod, embeddings, vector_index, vector_parts  # noqa: E402
from app.pack import slicing  # noqa: E402
from app.plugins.wikipedia.source import native_id_of  # noqa: E402

DIM = 8


def _collection(tmp_path, name: str):
    return collections_mod.Collection(
        name=name, plugin="wikipedia", profile=None, embed_mode_default=None,
        corpus_dir=tmp_path / "corpus", manifest_path=tmp_path / name / "m.db",
        fts_dir=tmp_path / name / "fts", lance_dir=tmp_path / name / "lance",
        embed=collections_mod.EmbedRecipe(url="http://enc:1", api="openai", model="m",
                                          dtype="f32", ctx=512, dim=DIM))


@pytest.fixture
def declared(monkeypatch):
    monkeypatch.setitem(slicing.TABLE_KEYS, "wikipedia", "lang")


@pytest.fixture
def split(tmp_path, isolated_config, declared):
    return _collection(tmp_path, "wiki")


@pytest.fixture
def single(tmp_path, isolated_config):
    return _collection(tmp_path, "wiki-one")


def _vec(seed: int) -> np.ndarray:
    v = np.random.default_rng(seed).standard_normal(DIM).astype(np.float32)
    return v / np.linalg.norm(v)


def _rows(lang: str, page: int, n: int = 3, *, lang_column: str | None = None) -> list[dict]:
    nid = native_id_of(lang, page)
    return [{"id": f"wikipedia:{nid}:{i}:1", "book_id": nid, "lang": lang_column or lang,
             "seq": i, "chapter": "", "text": f"{lang} {page} {i}", "vector": _vec(nid * 10 + i)}
            for i in range(n)]


DOCS = [("en", p) for p in range(1, 9)] + [("it", p) for p in range(1, 6)] + [("de", p) for p in range(1, 4)]


def _fill(collection) -> None:
    for lang, page in DOCS:
        embeddings.upsert_chunks(_rows(lang, page), collection)


def _part_ids(collection, value) -> set[str]:
    part = vector_parts.view(collection, value)
    return {r["id"] for r in embeddings._conn(part).open_table(embeddings.TABLE).to_arrow().to_pylist()}


def _version(collection, value) -> int:
    return embeddings._conn(vector_parts.view(collection, value)).open_table(embeddings.TABLE).version


@pytest.fixture
def query(monkeypatch):
    """The query vector a search embeds, set by the test."""
    box = {"v": _vec(1)}
    monkeypatch.setattr(embeddings, "embed_query", lambda c, q, **kw: list(box["v"]))
    return box


# ---- the layout -------------------------------------------------------------------

def test_a_declared_collection_is_created_split_and_each_document_lands_in_its_part(split):
    _fill(split)
    lay = json.loads((split.lance_dir / vector_parts.LAYOUT).read_text())
    assert lay == {"format": 1, "table_key": "lang"}
    assert not (split.lance_dir / "chunks.lance").exists(), "no single table"
    assert vector_parts.values(split) == ["de", "en", "it"]
    assert all(i.startswith("wikipedia:" + str(native_id_of("it", 0))[:3]) for i in _part_ids(split, "it"))
    assert len(_part_ids(split, "en")) == 8 * 3 and len(_part_ids(split, "de")) == 3 * 3
    assert embeddings.stats(split) == {"vectors": len(DOCS) * 3}


def test_a_collection_with_a_single_table_keeps_it_until_moved(single, monkeypatch):
    embeddings.upsert_chunks(_rows("en", 1), single)            # undeclared then: one table
    assert (single.lance_dir / "chunks.lance").exists()
    monkeypatch.setitem(slicing.TABLE_KEYS, "wikipedia", "lang")
    embeddings.upsert_chunks(_rows("it", 1), single)            # declared now, a table exists
    assert not (single.lance_dir / vector_parts.LAYOUT).exists()
    assert not vector_parts.is_split(single) and embeddings.stats(single) == {"vectors": 6}


def test_an_undeclared_plugin_never_splits(single):
    embeddings.upsert_chunks(_rows("en", 1) + _rows("it", 1), single)
    assert vector_parts.views(single) == [single]


# ---- search -----------------------------------------------------------------------

def _ids(hits) -> list[tuple[str, float]]:
    return [(h["chunk_id"], round(h["score"], 6)) for h in hits]


def test_an_unfiltered_search_merges_every_part_in_one_tables_order(split, single, query):
    _fill(split)
    _fill(single)
    for seed in (1, 2, 3):
        query["v"] = _vec(seed)
        got = embeddings.search("q", k=10, collection=split)
        want = embeddings.search("q", k=10, collection=single)
        assert _ids(got) == _ids(want)
        assert len({h["lang"] for h in embeddings.search("q", k=48, collection=split)}) == 3


def test_a_language_search_reads_its_part_alone_with_no_row_filter(split, single, query, monkeypatch):
    _fill(split)
    _fill(single)
    asked = []
    real = embeddings._search_table
    monkeypatch.setattr(embeddings, "_search_table",
                        lambda c, v, k, langs, b, val: asked.append((c.vector_part, langs)) or real(c, v, k, langs, b, val))
    got = embeddings.search("q", k=5, langs=["it"], collection=split)
    assert asked == [("it", None)], "the it part, no `lang IN` mask"
    assert _ids(got) == _ids(embeddings.search("q", k=5, langs=["it"], collection=single))
    asked.clear()
    two = embeddings.search("q", k=6, langs=["it", "de"], collection=split)
    assert sorted(a[0] for a in asked) == ["de", "it"]
    assert _ids(two) == _ids(embeddings.search("q", k=6, langs=["it", "de"], collection=single))


def test_ties_merge_by_chunk_id(split, query, monkeypatch):
    _fill(split)
    a = {"_distance": 0.5, "id": "wikipedia:2:0:1", "book_id": 2, "chapter": "", "lang": "it", "text": ""}
    b = dict(a, id="wikipedia:1:0:1", book_id=1, lang="en")
    monkeypatch.setattr(embeddings, "_search_table",
                        lambda c, *x: [a] if c.vector_part == "it" else [b] if c.vector_part == "en" else [])
    assert [h["chunk_id"] for h in embeddings.search("q", k=5, collection=split)] == [b["id"], a["id"]]


def test_a_book_id_search_reads_only_the_books_parts(split, query, monkeypatch):
    _fill(split)
    asked = []
    real = embeddings._search_table
    monkeypatch.setattr(embeddings, "_search_table",
                        lambda c, v, k, langs, b, val: asked.append(c.vector_part) or real(c, v, k, langs, b, val))
    hits = embeddings.search("q", k=5, collection=split, book_ids=[native_id_of("de", 2)])
    assert asked == ["de"] and {h["book_id"] for h in hits} == {native_id_of("de", 2)}


def test_parts_names_values_directly(split, query):
    _fill(split)
    hits = embeddings.search("q", k=50, collection=split, parts=["de"])
    assert {h["lang"] for h in hits} == {"de"} and len(hits) == 9


# ---- writes and deletes by document -------------------------------------------------

def test_delete_then_insert_touches_its_part_alone(split, monkeypatch):
    _fill(split)
    other = {v: _version(split, v) for v in ("en", "de")}
    monkeypatch.setattr(embeddings, "_embed_texts",
                        lambda items, recipe=None, **kw: [list(_vec(len(str(t)))) for t in items])
    nid = native_id_of("it", 3)
    chunks = [{"id": f"wikipedia:{nid}:{i}:1", "seq": i, "chapter": "", "text": f"new {i}"} for i in range(2)]
    embeddings.upsert_book(nid, "it", chunks, split)
    assert {i for i in _part_ids(split, "it") if f":{nid}:" in i} == {c["id"] for c in chunks}
    assert {v: _version(split, v) for v in ("en", "de")} == other, "no commit in another part"


def test_a_batch_across_parts_is_marked_once_after_every_commit(split):
    _fill(split)
    seen = []
    docs = [embeddings.PendingDoc(book_id=int(r[0]["book_id"]), rows=r, purge=True, seen=0)
            for r in (_rows("en", 20), _rows("de", 20))]

    def marked():
        seen.append({v: _version(split, v) for v in ("en", "de")})
    before = {v: _version(split, v) for v in ("en", "de")}
    embeddings.write_pending(docs, split, on_written=marked)
    assert len(seen) == 1 and all(seen[0][v] > before[v] for v in before), \
        "the mark runs once, after both parts committed"
    assert sum(f":{native_id_of('de', 20)}:" in i for i in _part_ids(split, "de")) == 3


def test_held_chunks_judges_each_document_by_its_parts_version(split):
    _fill(split)
    en, de = native_id_of("en", 1), native_id_of("de", 1)
    held = embeddings.held_chunks(split, [en, de])
    assert held.seen_of(en) == _version(split, "en") and held.seen_of(de) == _version(split, "de")
    assert len(held.of(en)) == 3 and len(held.of(de)) == 3


def test_reads_and_deletes_by_id_route_to_the_documents_part(split):
    _fill(split)
    it = _rows("it", 2)
    ids = [r["id"] for r in it]
    got = embeddings.vectors_by_chunk_id(ids + [_rows("en", 4)[0]["id"]], split)
    assert set(got) == set(ids) | {_rows("en", 4)[0]["id"]}
    assert np.allclose(got[ids[0]], it[0]["vector"])
    assert embeddings.books_with_vectors([native_id_of("it", 2), native_id_of("en", 99)], split) \
        == {native_id_of("it", 2)}
    assert sorted(embeddings.chunk_ids_of_books([native_id_of("it", 2)], split)) == sorted(ids)
    others = {v: _version(split, v) for v in ("en", "de")}
    assert embeddings.delete_chunks(ids[:2], split) == 2
    embeddings.delete_books([native_id_of("it", 2)], split)
    assert not any(f":{native_id_of('it', 2)}:" in i for i in _part_ids(split, "it"))
    assert {v: _version(split, v) for v in ("en", "de")} == others


def test_a_document_the_router_cannot_place_is_found_by_probe_and_deleted_only_there(split, monkeypatch):
    _fill(split)
    nid = native_id_of("de", 3)
    real = vector_parts.values_of_books
    monkeypatch.setattr(vector_parts, "values_of_books",
                        lambda c, books: {b: (None if int(b) == nid else v) for b, v in real(c, books).items()})
    others = {v: _version(split, v) for v in ("en", "it")}
    embeddings.delete_books([nid], split)
    assert not any(f":{nid}:" in i for i in _part_ids(split, "de"))
    assert {v: _version(split, v) for v in ("en", "it")} == others, \
        "a Lance delete that matches nothing still commits: never sent blind"
    with pytest.raises(vector_parts.Unrouted):
        embeddings.upsert_chunks(_rows("de", 3), split)          # a write is never guessed


def test_a_row_whose_language_disagrees_with_its_part_is_refused(split):
    with pytest.raises(vector_parts.Unrouted, match="disagree"):
        embeddings.upsert_chunks(_rows("it", 1, lang_column="en"), split)
    assert not vector_parts.values(split)


def test_scans_cover_every_part(split):
    _fill(split)
    n = sum(b.num_rows for b in embeddings.scan_columns(split, ["id"]))
    assert n == len(DOCS) * 3


def test_a_split_write_registers_each_part_once_before_the_manifest_lock(split, monkeypatch):
    _fill(split)
    entered = []
    real = vector_index.writing

    class counting(real):
        def __enter__(self):
            entered.append(self.key)
            return super().__enter__()
    monkeypatch.setattr(vector_index, "writing", counting)
    docs = [embeddings.PendingDoc(book_id=int(r[0]["book_id"]), rows=r, purge=True, seen=0)
            for r in (_rows("en", 30), _rows("it", 30))]
    embeddings.write_pending(docs, split)
    assert sorted(entered) == sorted(str(vector_parts.part_dir(split, v)) for v in ("en", "it")), \
        "once per part: a nested registration would wait on a compaction that waits on it"


def test_two_writers_creating_the_layout_agree(tmp_path, isolated_config, declared):
    c = _collection(tmp_path, "race")
    errors = []

    def write(lang):
        try:
            embeddings.upsert_chunks(_rows(lang, 1), c)
        except Exception as e:      # noqa: BLE001
            errors.append(e)
    ts = [threading.Thread(target=write, args=(lang,)) for lang in ("en", "it", "de", "fr")]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert not errors and vector_parts.values(c) == ["de", "en", "fr", "it"]


def test_a_plugin_whose_slice_needs_the_manifest_routes_by_one_read(tmp_path, isolated_config, monkeypatch):
    """Wikidata's family needs the manifest row (the dump locator); gutenberg's
    `lang` slice reads the manifest row the same way, without parsing."""
    from app import db
    monkeypatch.setitem(slicing.TABLE_KEYS, "gutenberg", "lang")
    c = collections_mod.Collection(
        name="g", plugin="gutenberg", profile=None, embed_mode_default=None,
        corpus_dir=tmp_path / "corpus", manifest_path=tmp_path / "g" / "m.db",
        fts_dir=tmp_path / "g" / "fts", lance_dir=tmp_path / "g" / "lance",
        embed=collections_mod.EmbedRecipe(url="http://enc:1", api="openai", model="m",
                                          dtype="f32", ctx=512, dim=DIM))
    con = db.connect(c)
    for nid, lang in ((1, "en"), (2, "fr"), (3, "en")):
        con.execute("INSERT INTO documents (uid, source, native_id, path, lang) VALUES (?, ?, ?, ?, ?)",
                    (f"gutenberg:{nid}", "gutenberg", str(nid), f"{nid}.txt", lang))
    con.commit()
    con.close()
    assert vector_parts.values_of_books(c, [1, 2, 3, 4]) == {1: "en", 2: "fr", 3: "en", 4: None}
    rows = [{"id": f"gutenberg:{n}:0:1", "book_id": n, "lang": lang, "seq": 0, "chapter": "",
             "text": str(n), "vector": _vec(n)} for n, lang in ((1, "en"), (2, "fr"), (3, "en"))]
    embeddings.upsert_chunks(rows, c)
    assert vector_parts.values(c) == ["en", "fr"] and len(_part_ids(c, "en")) == 2


# ---- the index, the checks, the bundles, per part ----------------------------------

def test_index_maintenance_runs_part_by_part(split, monkeypatch):
    from app import config
    _fill(split)
    monkeypatch.setattr(config, "VECTOR_INDEX_MIN_ROWS", 4)
    out = vector_index.ensure(split)
    assert set(out["parts"]) == {"de", "en", "it"}, "each part its own book_id index"
    st = vector_index.stats(split)
    assert st["rows"] == len(DOCS) * 3 and st["book_id_indexed"] and set(st["parts"]) == {"de", "en", "it"}
    assert st["lang_indexed"] is None, "a language part needs no lang BITMAP"


def test_parts_of_two_widths_refuse_the_collection(split):
    from app import spaces
    _fill(split)
    assert spaces.known_lance_dims(split.lance_dir) == {DIM}
    assert spaces.known_lance_rows(split.lance_dir) == len(DOCS) * 3
    odd = vector_parts.view(split, "fr")
    rows = [dict(r, vector=np.ones(DIM * 2, np.float32), lang="fr") for r in _rows("fr", 1)]
    embeddings.upsert_chunks(rows, odd)
    assert spaces.known_lance_dims(split.lance_dir) == {DIM, DIM * 2}
    with pytest.raises(spaces.BootValidationError, match="one collection is one space"):
        spaces._refuse_split_dims(split)


def test_export_and_verify_read_each_document_from_its_part(split):
    from app.bundles import export, verify_on_use
    _fill(split)
    books = [native_id_of("en", 1), native_id_of("de", 2)]
    rows, dim, dtype = export._read_vectors(split, books)
    assert dim == DIM and dtype == "f32" and len(rows) == 6
    ids = [r["id"] for r in _rows("it", 3)] + [_rows("en", 2)[0]["id"]]
    got = verify_on_use._texts_and_vectors(split, ids)
    assert set(got) == set(ids) and got[ids[0]][0] == "it 3 0"


def test_an_imported_bundle_lands_in_its_documents_part(split):
    from types import SimpleNamespace
    from app.bundles import importer
    nid = native_id_of("it", 40)
    rows = [{"id": f"wikipedia:{nid}:{i}:1", "book_id": nid, "lang": "it", "seq": i, "chapter": "",
             "text": f"t{i}", "vector": _vec(i).tolist()} for i in range(2)]
    importer._write_rows(split, nid, rows, SimpleNamespace(dtype="f32", dim=DIM))
    assert vector_parts.values(split) == ["it"] and len(_part_ids(split, "it")) == 2


def test_the_worker_sees_a_split_store_as_holding_vectors(split):
    from app import worker
    assert not worker.holds_vectors(split)
    _fill(split)
    assert worker.holds_vectors(split)
