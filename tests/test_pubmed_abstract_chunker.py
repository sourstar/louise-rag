"""PubMed: one chunk per article (STATUS *Known gaps* → built 2026-08-26).

MedCPT holds ONE vector per article (title + abstract, one 512-token
window). The genesis shape gate binds vectors to chunks 1:1, and the book
chunker cut title and abstract into separate chunks — so the archive
covered only title-only records (42–70 % of shard documents were shape
mismatches, live). The abstract chunker makes the article its one chunk,
under its own version number, which moves PubMed's space id and nothing
else's; `space repin` is the operator step that adopts it.
"""
import json
from dataclasses import replace
from pathlib import Path

import pytest

from app import chunking, collections as collections_mod, config, db, discovery, spaces, worker
from app.plugins.base import DiscoverState
from app.plugins.pubmed import SOURCE

FIXTURE = Path(__file__).parent / "fixtures" / "pubmed-mini"


def _pubmed_collection(tmp_path):
    return collections_mod.Collection(
        name="pubmed", plugin="pubmed", profile=None, embed_mode_default=None,
        corpus_dir=FIXTURE, manifest_path=tmp_path / "pubmed" / "manifest.db",
        fts_dir=tmp_path / "pubmed" / "fts", lance_dir=tmp_path / "pubmed" / "lancedb")


def _doc(pmid: str):
    refs = [r for r in SOURCE.discover(DiscoverState(corpus_root=FIXTURE, state_dir=None))
            if r.native_id == pmid and not r.error]
    return SOURCE.parse(SOURCE.fetch(refs[-1]))


def test_the_article_is_its_one_chunk(isolated_config):
    chunk = chunking.chunker_for(SOURCE.chunking_profile, SOURCE.mutability)
    doc = _doc("30000006")
    chunks = list(chunk(doc.uid, 30000006, doc.sections))
    assert len(chunks) == 1
    c = chunks[0]
    assert c["id"] == f"pubmed:30000006:0:{chunking.ABSTRACT_CHUNK_VERSION}"
    assert c["seq"] == 0 and c["chapter"] == "", "no chapter label: the chunk IS the article"
    assert c["text"] == f"{doc.title}\n\n{doc.sections[1][1]}", "title, blank line, abstract"
    # a record without an abstract is still one chunk: the title
    doc4 = _doc("30000004")
    chunks4 = list(chunk(doc4.uid, 30000004, doc4.sections))
    assert len(chunks4) == 1 and chunks4[0]["text"] == doc4.title


def test_every_fixture_article_yields_exactly_one_chunk_through_the_worker(isolated_config, tmp_path):
    collection = _pubmed_collection(tmp_path)
    con = db.connect(collection)
    discovery.sync_collection(con, collection)
    rows = con.execute("SELECT * FROM documents WHERE error IS NULL").fetchall()
    assert rows
    for row in rows:
        worker.stage_rdf(con, row, collection)
        chunks = worker._load_chunks(con.execute("SELECT * FROM documents WHERE uid=?",
                                                 (row["uid"],)).fetchone(), collection)
        assert len(chunks) == 1, (row["uid"], [c["id"] for c in chunks])
        assert chunks[0]["id"].endswith(f":0:{chunking.ABSTRACT_CHUNK_VERSION}")


def test_only_pubmeds_space_id_moves(isolated_config, tmp_path, monkeypatch):
    """Invariant 3: the change is versioned for the chunker that changed.
    PubMed's identity now names the abstract chunker; a book collection's
    payload is byte-identical to before (grandfathered)."""
    monkeypatch.setattr(spaces, "resolve_embed_model_hash", lambda *a, **k: ("abc", False))
    pubmed = spaces.gather_components(_pubmed_collection(tmp_path))
    assert (pubmed.chunker, pubmed.chunk_version) == ("abstract", chunking.ABSTRACT_CHUNK_VERSION)
    as_book = replace(pubmed, chunker="book", chunk_version=config.CHUNK_VERSION)
    assert spaces.compute_space_id(pubmed) != spaces.compute_space_id(as_book)
    assert spaces._config_id(pubmed) != spaces._config_id(as_book), \
        "boot validation must refuse the old pin even with a weak model hash"
    book = spaces.gather_components(collections_mod.default_collection())
    assert book.chunker == "book"
    # grandfathered: the book payload is unchanged (a grandfathered sibling
    # hashes identically), so no existing space re-pins
    assert spaces.compute_space_id(book) == spaces.compute_space_id(replace(book, chunker="verbatim"))
