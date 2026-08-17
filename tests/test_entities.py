"""app/entities.py: tag_document() — the generic doc_keys population hook."""
from pathlib import Path

from app import collections as collections_mod, db, entities, refs
from app.refs import store as refs_store

FIXTURE_REFS = Path(__file__).parent / "fixtures" / "refs-mini"


def _store(tmp_path):
    con = refs.connect(tmp_path / "refs.db")
    refs_store.sync_all(con, FIXTURE_REFS)
    return refs.RefStore(con, [s.schema() for s in refs.REFS_REGISTRY.values()])


def _con(tmp_path):
    collection = collections_mod.Collection(
        name="pubmed", plugin="pubmed", profile=None, embed_mode_default=None,
        corpus_dir=tmp_path, manifest_path=tmp_path / "manifest.db",
        fts_dir=tmp_path / "fts", lance_dir=tmp_path / "lancedb")
    con = db.connect(collection)
    con.execute(
        "INSERT INTO documents(uid,source,native_id,path,updated_at) VALUES(?,?,?,?,0)",
        ("pubmed:1", "pubmed", "1", "/x"))
    return con


def test_tag_document_writes_doc_keys(tmp_path):
    store = _store(tmp_path)
    con = _con(tmp_path)

    ids = entities.tag_document(con, "pubmed:1", "A study of BRCA2 variants",
                                 ["no gene mentioned in this chunk"], refs.RESOLVER, store)
    assert ids == ["hgnc:1101"]
    rows = [r["entity_id"] for r in con.execute(
        "SELECT entity_id FROM doc_keys WHERE uid=?", ("pubmed:1",))]
    assert rows == ["hgnc:1101"]


def test_tag_document_is_idempotent(tmp_path):
    store = _store(tmp_path)
    con = _con(tmp_path)

    entities.tag_document(con, "pubmed:1", "BRCA2 study", [], refs.RESOLVER, store)
    entities.tag_document(con, "pubmed:1", "BRCA2 study", [], refs.RESOLVER, store)
    count = con.execute(
        "SELECT COUNT(*) c FROM doc_keys WHERE uid=?", ("pubmed:1",)).fetchone()["c"]
    assert count == 1


def test_tag_document_ordinary_prose_writes_nothing(tmp_path):
    store = _store(tmp_path)
    con = _con(tmp_path)

    ids = entities.tag_document(con, "pubmed:1", "A record about widget durability",
                                 ["nothing gene-related here"], refs.RESOLVER, store)
    assert ids == []
    count = con.execute(
        "SELECT COUNT(*) c FROM doc_keys WHERE uid=?", ("pubmed:1",)).fetchone()["c"]
    assert count == 0


def test_tag_document_updates_when_content_changes(tmp_path):
    store = _store(tmp_path)
    con = _con(tmp_path)

    entities.tag_document(con, "pubmed:1", "BRCA2 study", [], refs.RESOLVER, store)
    entities.tag_document(con, "pubmed:1", "TP53 study", [], refs.RESOLVER, store)
    rows = [r["entity_id"] for r in con.execute(
        "SELECT entity_id FROM doc_keys WHERE uid=?", ("pubmed:1",))]
    assert rows == ["hgnc:11998"]
