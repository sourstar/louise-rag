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

from app import chunking, cli, collections as collections_mod, config, db, discovery, spaces, worker
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


def test_space_repin_resets_everything_the_old_chunks_touched(isolated_config, tmp_path, capsys):
    """The operator step behind every "wipe and re-embed" refusal: drops the
    pin, resets embed AND fts marks (the lexical index holds chunk ids cut
    by the old chunker — same id, different text otherwise), clears the
    verification ledger, keeps fetched archive shards (no re-download),
    drops finished ones, removes the vector table. Refuses without --yes."""
    c = collections_mod.default_collection()
    con = db.connect(c)
    for i in range(1, 6):
        con.execute("INSERT INTO documents(uid,source,native_id,path,in_scope,rdf_done,"
                    "fts_done,embed_done,embed_version) VALUES(?,'g',?,?,1,1,1,1,1)",
                    (f"g:{i}", str(i), f"/x{i}"))
    con.execute("INSERT INTO space(id,space_id,dim,components,pinned_at) VALUES(1,'old',8,'{}',0)")
    con.execute("INSERT INTO genesis_imports(archive,shard,state,docs,bytes,updated_at) "
                "VALUES('arc','0','final',9,0,0)")
    con.execute("INSERT INTO genesis_imports(archive,shard,state,docs,bytes,pending,next_pass_at,updated_at) "
                "VALUES('arc','1','applied',3,100,7,99,0)")
    con.execute("INSERT INTO genesis_imports(archive,shard,state,updated_at) VALUES('arc','2','fetching',0)")
    con.commit()
    c.lance_dir.mkdir(parents=True, exist_ok=True)
    (c.lance_dir / "table.lance").write_bytes(b"vectors")

    assert cli.main(["space", "repin", c.name]) == 2, "no --yes: plan only"
    assert con.execute("SELECT COUNT(*) FROM space").fetchone()[0] == 1
    assert (c.lance_dir / "table.lance").exists()

    assert cli.main(["space", "repin", c.name, "--yes"]) == 0
    out = capsys.readouterr().out
    assert "5 documents to re-embed" in out and "5 to re-index" in out
    assert con.execute("SELECT COUNT(*) FROM space").fetchone()[0] == 0
    assert con.execute("SELECT SUM(embed_done)+SUM(fts_done)+SUM(embed_version) FROM documents").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM chunk_verification").fetchone()[0] == 0
    rows = {r["shard"]: dict(r) for r in con.execute("SELECT * FROM genesis_imports")}
    assert "0" not in rows, "a final shard's files are gone: the row goes with them"
    assert rows["1"]["state"] == "fetched" and rows["1"]["docs"] == 0 \
        and rows["1"]["pending"] is None and rows["1"]["next_pass_at"] == 0
    assert rows["2"]["state"] == "fetching", "an in-flight download resumes"
    assert not c.lance_dir.exists()
