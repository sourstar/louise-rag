"""tests/test_plugins_drag.py — the drag as a corpus (step 9f; 07 §7.8
`source = "drag"`): a peer holding a collection's record shards reads its
documents from them — discover, fetch, parse — as the producer parsed
them, so the chunks and their ids are the producer's; superseded shards
are not read; the scan cursor skips a shard whose rows are committed;
the collection file's key makes the switch; `records build` refuses."""
import pickle
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import collections as collections_mod
from app.pack import records as pack_records
from app.plugins import drag
from app.plugins.base import DiscoverState, DocRef

RECORDS = [
    {"uid": "pubmed:1", "native_id": "1", "lang": "en", "title": "A title", "creators": ["Doe, J"], "date": "2020",
     "subjects": ["Neoplasms"], "keys": [], "sections": [["Title", "A title"], ["Abstract", "Body one."]],
     "extra": {"journal": "J"}, "origin": "pubmed25n0001.xml#0", "content_hash": "aa" * 16, "origin_hash": "11" * 16},
    {"uid": "pubmed:2", "native_id": "2", "lang": "de", "title": "Zwei", "creators": [], "date": None,
     "subjects": [], "keys": ["x:1"], "sections": [["Title", "Zwei"]], "extra": {},
     "origin": "pubmed25n0001.xml#1", "content_hash": "bb" * 16, "origin_hash": "22" * 16},
]


def a_drag(tmp_path):
    d = tmp_path / "drag"
    (d / "records" / "year=2020").mkdir(parents=True)
    pack_records.write_shard(d / "records/year=2020/00001.jsonl.zst", RECORDS)
    pack_records.write_shard(d / "records/year=2020/00001.2026-07.jsonl.zst", RECORDS[:1])   # superseded
    (d / "records" / "year=2021").mkdir()
    pack_records.write_shard(d / "records/year=2021/00001.jsonl.zst", [dict(RECORDS[1], uid="pubmed:3",
                                                                             native_id="3", title="Drei")])
    return d


def test_the_shards_are_the_corpus_and_a_record_parses_as_the_producer_wrote_it(tmp_path):
    d = a_drag(tmp_path)
    assert [p.relative_to(d).as_posix() for p in drag.shard_files(d)] == \
        ["records/year=2020/00001.jsonl.zst", "records/year=2021/00001.jsonl.zst"], "a superseded version is not read"
    inner = collections_mod.PLUGIN_REGISTRY["pubmed"]
    src = drag.DragSource(inner, d)
    assert src.source_id == "pubmed" and src.license_class is inner.license_class, "the plugin's attributes"
    (tmp_path / "state").mkdir()
    state = DiscoverState(corpus_root=d, state_dir=tmp_path / "state")
    refs = list(src.discover(state))
    assert [r.native_id for r in refs] == ["1", "2", "3"]
    assert refs[0].locator == f"{d / 'records/year=2020/00001.jsonl.zst'}#pubmed:1" and refs[0].extra["lang"] == "en"
    raw = src.fetch(refs[0])
    assert raw.raw_hash == "11" * 16, "the record's own origin_hash: the unit the producer parsed"
    doc = src.parse(raw)
    assert (doc.uid, doc.title, doc.creators, doc.language, doc.date, doc.subjects, doc.extra) == \
        ("pubmed:1", "A title", ["Doe, J"], "en", "2020", ["Neoplasms"], {"journal": "J"})
    assert doc.sections == [("Title", "A title"), ("Abstract", "Body one.")]
    assert pack_records.content_hash(doc.title, [list(s) for s in doc.sections]) == \
        pack_records.content_hash("A title", RECORDS[0]["sections"]), "same text, same chunks, same ids"
    assert src.parse(src.fetch(refs[2])).title == "Drei"
    with pytest.raises(KeyError):
        src.fetch(DocRef("pubmed", "9", f"{d / 'records/year=2020/00001.jsonl.zst'}#pubmed:9"))
    with pytest.raises(ValueError):
        drag.split_locator("no-separator")
    again = pickle.loads(pickle.dumps(src))
    assert isinstance(again, drag.DragSource) and again.drag_dir == d, "travels to the parse pool"


def test_the_cursor_skips_a_shard_whose_rows_are_committed(tmp_path):
    d = a_drag(tmp_path)
    src = drag.DragSource(collections_mod.PLUGIN_REGISTRY["pubmed"], d)
    (tmp_path / "state").mkdir()
    state = DiscoverState(corpus_root=d, state_dir=tmp_path / "state")
    taken = []
    for ref in src.discover(state):
        state.received += 1
        taken.append(ref.native_id)
        if ref.native_id == "2":
            state.committed = state.received          # the first shard's rows durable
    assert taken == ["1", "2", "3"]
    state2 = DiscoverState(corpus_root=d, state_dir=tmp_path / "state")
    assert [r.native_id for r in src.discover(state2)] == ["3"], "the committed shard is skipped; the other not"


def test_the_collection_file_s_source_key_makes_the_switch(tmp_path, monkeypatch):
    from tests.hold.conftest import a_root, pubmed
    from app.hold import collection as col
    root = tmp_path / "louise" / "collections"
    monkeypatch.setattr(collections_mod, "louise_root", lambda: tmp_path / "louise")
    monkeypatch.setattr(collections_mod, "_config_dir", lambda: tmp_path / "louise")
    codex = pubmed()
    a_root(root, codex)
    name = col.name_of(root, codex.collection_id)
    toml = tmp_path / "louise" / "collections" / f"{name}.toml"
    toml.write_text(f'codex = "{codex.collection_id}"\nslices = []\nkinds = ["records"]\nembed = "off"\n'
                    f'share = false\nsource = "drag"\n[encoder]\nurl = "http://e"\nembed_model = "m"\nquery_model = "m"\n', encoding="utf-8")
    c = collections_mod._load_one(toml)
    assert c.source == "drag" and c.corpus_dir == root / name / "drag"
    assert isinstance(c.plugin_instance(), drag.DragSource) and c.plugin_instance().source_id == "pubmed"
    toml.write_text(toml.read_text(encoding="utf-8").replace('source = "drag"', 'source = "tape"'), encoding="utf-8")
    with pytest.raises(ValueError, match="source is"):
        collections_mod._load_one(toml)
    toml.write_text(toml.read_text(encoding="utf-8").replace('source = "tape"\n', ""), encoding="utf-8")
    c = collections_mod._load_one(toml)
    assert c.source == "origin" and not isinstance(c.plugin_instance(), drag.DragSource)


def test_records_build_refuses_a_fetched_collection(tmp_path, monkeypatch):
    from app.hold import collection as col, records as hold_records
    monkeypatch.setattr(collections_mod, "louise_root", lambda: tmp_path / "louise")
    monkeypatch.setattr(collections_mod, "by_name",
                        lambda name: SimpleNamespace(name=name, source="drag"))
    monkeypatch.setattr(col, "resolve", lambda root, ref: ("pubmed", SimpleNamespace()))
    monkeypatch.setattr(hold_records, "_paths", lambda root, name: (tmp_path / "d", tmp_path / "s", tmp_path / "g"))
    with pytest.raises(hold_records.RecordsError, match="HELD, not built"):
        hold_records.build(tmp_path / "louise" / "collections", "pubmed")


def test_a_fetched_collection_s_file_says_its_documents_come_from_the_drag(tmp_path, monkeypatch):
    from app import louise
    from tests.hold.conftest import a_root, pubmed
    root = tmp_path / "elsewhere" / "collections"
    monkeypatch.setattr(collections_mod, "louise_root", lambda: tmp_path / "louise")
    codex = pubmed()
    a_root(root, codex)
    p = louise._write_held_file(root, codex, "http://e", source="drag")
    text = p.read_text(encoding="utf-8")
    assert 'source = "drag"' in text and 'embed = "off"' in text
    p2 = louise._write_held_file(root, pubmed(), "http://e", source="drag")
    assert p2 == p, "a name holds one collection for its life: writing it again is nothing to do"


def test_a_fetched_collection_is_scanned_and_cut_from_its_shards(tmp_path, monkeypatch):
    """End to end as the worker runs it: the discovery scan over a
    drag-sourced collection fills the manifest from the shards, the
    metadata and the sections come back through the same plugin calls the
    stages make, and the chunks are cut by the Codex's layer — the same
    cut the producer made, so a bundle's vectors land on them."""
    from app import db, discovery, layers, plugin_io
    from app.hold import collection as col
    from tests.hold.conftest import a_root, pubmed
    root = tmp_path / "louise" / "collections"
    monkeypatch.setattr(collections_mod, "louise_root", lambda: tmp_path / "louise")
    monkeypatch.setattr(collections_mod, "_config_dir", lambda: tmp_path / "louise")
    codex = pubmed()
    a_root(root, codex)
    name = col.name_of(root, codex.collection_id)
    d = col.drag_of(root, name)
    (d / "records" / "year=2020").mkdir(parents=True, exist_ok=True)
    pack_records.write_shard(d / "records/year=2020/00001.jsonl.zst", RECORDS)
    toml = tmp_path / "louise" / "collections" / f"{name}.toml"
    toml.write_text(f'codex = "{codex.collection_id}"\nslices = "all"\nkinds = ["records"]\nembed = "off"\n'
                    f'share = false\nsource = "drag"\n[encoder]\nurl = "http://e"\nembed_model = "m"\nquery_model = "m"\n', encoding="utf-8")
    c = collections_mod._load_one(toml)
    con = db.connect(c)
    try:
        out = discovery.sync_collection(con, c)
        assert out.get("error") is None and out["added"] == 2, out
        rows = con.execute("SELECT * FROM documents ORDER BY uid").fetchall()
        assert [r["uid"] for r in rows] == ["pubmed:1", "pubmed:2"]
        assert rows[0]["path"].endswith("#pubmed:1") and rows[0]["lang"] == "en"
        meta = plugin_io.metadata(c.plugin_instance(), rows[0])
        assert (meta["title"], meta["authors"], meta["lang"], meta["subjects"]) == ("A title", "Doe, J", "en",
                                                                                      "Neoplasms")
        sections = plugin_io.sections(c.plugin_instance(), rows[0])
        assert sections == [("Title", "A title"), ("Abstract", "Body one.")]
        chunks = layers.chunks_for_document(c, rows[0], sections)
        assert chunks and all(ch["id"].startswith("pubmed:1:") for ch in chunks)
        assert chunks == layers.chunks_for_document(c, rows[0], sections), "a pure function of the record"
        assert discovery.sync_collection(con, c)["added"] == 0, "the cursor skips what is committed"
    finally:
        con.close()


def test_a_fetched_collection_never_acquires_from_the_origin(tmp_path, monkeypatch):
    """Found live 2026-10-07: the second peer fetched three certified books
    and its worker started an rsync of the whole Gutenberg mirror — the file
    holds every slice and the records kind, which derived acquire_enabled
    true, and the corpus of a fetched collection is its drag."""
    from tests.hold.conftest import a_root, pubmed
    from app.hold import collection as col
    root = tmp_path / "louise" / "collections"
    monkeypatch.setattr(collections_mod, "louise_root", lambda: tmp_path / "louise")
    monkeypatch.setattr(collections_mod, "_config_dir", lambda: tmp_path / "louise")
    codex = pubmed()
    a_root(root, codex)
    name = col.name_of(root, codex.collection_id)
    toml = root / f"{name}.toml"
    base = (f'codex = "{codex.collection_id}"\nslices = "all"\nkinds = ["records", "vectors"]\nembed = "off"\n'
            f'share = false\n%s[encoder]\nurl = "http://e"\nembed_model = "m"\nquery_model = "m"\n')
    toml.write_text(base % "", encoding="utf-8")
    assert collections_mod._load_one(toml).acquire_enabled, "an origin-sourced file holding everything acquires"
    toml.write_text(base % 'source = "drag"\n', encoding="utf-8")
    c = collections_mod._load_one(toml)
    assert c.acquire_enabled is False and c.corpus_dir == root / name / "drag"
    d = collections_mod.codex_defaults(codex, name, "all", ("records", "vectors"), "drag")
    assert d["acquire_enabled"] is False and d["corpus_dir"] == c.corpus_dir, \
        "one derivation for the loader and the writer: a rewrite adds no acquire_enabled key"
    toml.write_text(base % 'source = "drag"\nacquire_enabled = true\n', encoding="utf-8")
    with pytest.raises(ValueError, match="the drag IS this collection's corpus"):
        collections_mod._load_one(toml)


def test_the_inner_plugin_s_corpus_readers_are_never_reached(tmp_path):
    """Found live 2026-10-07: gutenberg's lazy readers (`read_text`,
    `read_chapters`, `read_metadata`) were forwarded by the wrapper, looked
    for an epub in a book directory a fetching peer does not have, and
    wrote "not indexable" onto every fetched document — the metadata read
    "succeeded" with empty titles. The stages must read the records."""
    from app import plugin_io

    class GutenbergShaped:
        source_id = "gutenberg"
        license_class = "green"

        def _never(self, *a, **k):
            raise AssertionError("a corpus reader reached on a fetched collection")
        read_text = read_chapters = read_metadata = read_figures = prepare = prepare_key = current = _never

    d = tmp_path / "drag"
    (d / "records" / "year=2020").mkdir(parents=True)
    pack_records.write_shard(d / "records/year=2020/00001.jsonl.zst", RECORDS)
    src = drag.DragSource(GutenbergShaped(), d)
    for name in drag.DragSource.CORPUS_READERS:
        assert not hasattr(src, name), name
    assert src.license_class == "green", "everything else is still the plugin's"
    ref = next(iter(src.discover(DiscoverState(corpus_root=d, state_dir=tmp_path / "state"))))
    row = {"path": ref.locator, "native_id": ref.native_id, "uid": "pubmed:1"}
    assert plugin_io.metadata(src, row)["title"] == "A title"
    assert plugin_io.sections(src, row) == [("Title", "A title"), ("Abstract", "Body one.")]
    assert plugin_io.full_text(src, row) == "A title\n\nBody one."
    assert plugin_io.prepare(src, row) is None
