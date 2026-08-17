"""PubMed plugin — CorpusSource conformance and generic-orchestrator sync
against the pubmed-mini fixture: 3 containers (1 baseline + 2 updatefiles),
19 XML elements total (17 <PubmedArticle>, 2 <DeleteCitation>) spanning 16
distinct PMIDs, one of which (30000001) is revised by the first updatefile.

Deliverable 4's required cases: normal record (30000006), revised record
(30000001, baseline vs. updatefile), DeleteCitation (30000002, 30000003),
missing abstract (30000004), non-ASCII text (30000005).
"""
import shutil
from pathlib import Path

import pytest

from app import chunking, collections as collections_mod, config, db, discovery
from app.plugins.base import DiscoverState
from app.plugins.pubmed import SOURCE
from app.plugins.pubmed.source import DELETE_MARKER, split_locator

FIXTURE_CORPUS = Path(__file__).parent / "fixtures" / "pubmed-mini"


def test_worker_stages_run_via_the_protocol_fallback(tmp_path, isolated_config):
    """Regression for the live 30k-article failure: worker.stage_rdf and
    _load_chunks assumed gutenberg's read_metadata/read_chapters helpers.
    A protocol-only plugin (pubmed) must flow through fetch→parse from a
    manifest row — including the record index round-tripping through the
    persisted locator."""
    from app import worker
    collection = _collection(tmp_path)
    con = db.connect(collection)
    discovery.sync_collection(con, collection)

    row = con.execute("SELECT * FROM documents WHERE uid='pubmed:30000006'").fetchone()
    assert row is not None and "::" in row["path"], "locator must carry the record index"
    worker.stage_rdf(con, row, collection)
    refreshed = con.execute(
        "SELECT * FROM documents WHERE uid='pubmed:30000006'").fetchone()
    assert refreshed["rdf_done"] == 1 and refreshed["title"]

    chunks = worker._load_chunks(refreshed, collection)
    assert chunks and chunks[0]["text"]
    assert chunks[0]["id"].startswith("pubmed:30000006:")


def test_scan_cursor_skips_unchanged_containers(tmp_path):
    """Incremental discovery: with a state_dir, an unchanged container is
    never re-opened — at full-PubMed scale that is the difference between an
    hour of XML parsing per scan and a stat pass. A touched container
    re-enumerates; without a state_dir behavior is the old full scan."""
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    shutil.copy(FIXTURE_CORPUS / "pubmed25n0001.xml", corpus / "pubmed25n0001.xml")
    state_dir = tmp_path / "data"
    state_dir.mkdir()

    state = DiscoverState(corpus_root=corpus, state_dir=state_dir)
    first = list(SOURCE.discover(state))
    assert first, "first scan enumerates"
    assert (state_dir / "pubmed_scan_cursor.json").is_file()

    assert list(SOURCE.discover(state)) == [], "unchanged container skipped"

    # a content change (new mtime/size) re-enumerates
    import os
    path = corpus / "pubmed25n0001.xml"
    os.utime(path, (path.stat().st_atime, path.stat().st_mtime + 5))
    assert list(SOURCE.discover(state)), "touched container re-enumerates"

    # stateless calls keep full-scan behavior
    assert list(SOURCE.discover(DiscoverState(corpus_root=corpus)))


def test_corrupt_container_costs_one_error_ref_not_the_scan(tmp_path):
    """With acquisition in the background, discovery can meet a truncated or
    checksum-failed container on disk — the scan must survive it (observed
    live: a zlib error killed the whole pubmed discovery)."""
    import gzip as gzip_mod
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    shutil.copy(FIXTURE_CORPUS / "pubmed25n0001.xml", corpus / "pubmed25n0001.xml")
    (corpus / "pubmed25n9999.xml.gz").write_bytes(
        gzip_mod.compress(b"<PubmedArticleSet>")[:20])   # truncated mid-stream

    refs = list(SOURCE.discover(DiscoverState(corpus_root=corpus)))
    errors = [r for r in refs if r.error and "unparseable" in r.error]
    good = [r for r in refs if r.error is None]
    assert len(errors) == 1 and errors[0].native_id == "pubmed25n9999.xml.gz"
    assert good, "the healthy container must still be discovered"


def _collection(tmp_path) -> collections_mod.Collection:
    return collections_mod.Collection(
        name="pubmed", plugin="pubmed", profile=None, embed_mode_default=None,
        corpus_dir=FIXTURE_CORPUS,
        manifest_path=tmp_path / "manifest.db",
        fts_dir=tmp_path / "fts",
        lance_dir=tmp_path / "lancedb",
    )


def _fetch_parse(native_id: str, container_name: str):
    ref = next(r for r in SOURCE.discover(DiscoverState(corpus_root=FIXTURE_CORPUS))
               if r.native_id == native_id and split_locator(r.locator)[0].name == container_name
               and r.extra.get("kind") == "article")
    raw = SOURCE.fetch(ref)
    return SOURCE.parse(raw), raw


# ---------------- discover() ----------------

def test_discover_yields_articles_and_deletes():
    refs = list(SOURCE.discover(DiscoverState(corpus_root=FIXTURE_CORPUS)))
    articles = [r for r in refs if r.extra.get("kind") == "article"]
    deletes = [r for r in refs if r.extra.get("kind") == "delete"]

    assert len(articles) == 17  # 16 distinct PMIDs + one revision of 30000001
    assert len(deletes) == 2
    assert {r.native_id for r in deletes} == {"30000002", "30000003"}
    for r in deletes:
        assert r.error == DELETE_MARKER
    for r in articles:
        assert r.error is None
        assert r.source_id == "pubmed"

    revised = [r for r in articles if r.native_id == "30000001"]
    assert len(revised) == 2
    assert {split_locator(r.locator)[0].name for r in revised} == {
        "pubmed25n0001.xml", "pubmed25n0002.xml"}


# ---------------- fetch()/parse() ----------------

def test_parse_normal_record():
    doc, raw = _fetch_parse("30000006", "pubmed25n0001.xml")
    assert doc.uid == "pubmed:30000006"
    assert doc.title == "A filler record about widget durability."
    assert doc.sections[0] == ("Title", doc.title)
    assert doc.sections[1][0] == "Abstract"
    assert "wear" in doc.sections[1][1]
    assert doc.raw_hash == raw.raw_hash


def test_parse_missing_abstract():
    doc, _ = _fetch_parse("30000004", "pubmed25n0001.xml")
    assert doc.title == "A record with no abstract on file."
    assert [s[0] for s in doc.sections] == ["Title"]  # no Abstract section at all
    assert "Widgets" in doc.subjects


def test_parse_non_ascii():
    doc, _ = _fetch_parse("30000005", "pubmed25n0001.xml")
    assert "Übertragungsraten" in doc.title
    assert "эффективность" in doc.title
    assert "результаты" in doc.sections[1][1]


def test_parse_mesh_and_journal_date():
    doc, _ = _fetch_parse("30000001", "pubmed25n0001.xml")
    assert doc.subjects == ["Widgets", "Calibration"]
    assert doc.extra["journal"] == "J Widget Res"
    assert doc.extra["date"] == "2023-Jan"


def test_revision_replaces_content():
    baseline, _ = _fetch_parse("30000001", "pubmed25n0001.xml")
    revised, _ = _fetch_parse("30000001", "pubmed25n0002.xml")
    assert baseline.title != revised.title
    assert "(corrected)" in revised.title
    assert revised.subjects == ["Widgets", "Calibration", "Reproducibility of Results"]
    assert baseline.raw_hash != revised.raw_hash


def test_fetch_rejects_delete_refs():
    ref = next(r for r in SOURCE.discover(DiscoverState(corpus_root=FIXTURE_CORPUS))
               if r.extra.get("kind") == "delete")
    with pytest.raises(ValueError):
        SOURCE.fetch(ref)


def test_fetch_detects_md5_tampering(tmp_path):
    tampered = tmp_path / "pubmed25n0001.xml"
    tampered.write_bytes(
        (FIXTURE_CORPUS / "pubmed25n0001.xml").read_bytes() + b"\n<!-- tampered -->")
    (tmp_path / "pubmed25n0001.xml.md5").write_text(
        (FIXTURE_CORPUS / "pubmed25n0001.xml.md5").read_text())
    ref = next(r for r in SOURCE.discover(DiscoverState(corpus_root=tmp_path))
               if r.native_id == "30000001" and r.extra.get("kind") == "article")
    with pytest.raises(RuntimeError, match="md5"):
        SOURCE.fetch(ref)


# ---------------- chunking profile "abstract" ----------------

def test_chunk_abstract_one_chunk_per_record():
    doc, _ = _fetch_parse("30000006", "pubmed25n0001.xml")
    chunks = list(chunking.chunk_abstract(doc.uid, "30000006", doc.sections))
    assert len(chunks) == 1
    assert chunks[0]["id"] == f"{doc.uid}:0:{config.CHUNK_VERSION}"
    assert chunks[0]["seq"] == 0
    assert doc.title in chunks[0]["text"]


def test_chunk_abstract_missing_abstract_still_yields_title_chunk():
    doc, _ = _fetch_parse("30000004", "pubmed25n0001.xml")
    chunks = list(chunking.chunk_abstract(doc.uid, "30000004", doc.sections))
    assert len(chunks) == 1
    assert chunks[0]["text"] == doc.title


# ---------------- generic orchestrator (app/discovery.py, reused from P0.1) ----------------

def test_ingest_via_generic_orchestrator(tmp_path):
    """The orchestrator de-duplicates by uid within one discover() pass,
    keeping the LAST ref seen (see app/discovery.py) — so a fresh ingest
    converges straight to current truth: 30000001 is inserted once holding
    the updatefile's (revised) content, not inserted-then-updated; 30000002/
    30000003 never gain a live article row at all because their
    DeleteCitation ref (later in file order) wins before any write happens.
    """
    collection = _collection(tmp_path)
    con = db.connect(collection)

    stats = discovery.sync_collection(con, collection)
    assert stats["seen"] == 19          # 17 article refs + 2 delete refs, raw scan count
    assert stats["added"] == 14         # 16 PMIDs minus the 2 that are delete-only
    assert stats["changed"] == 0        # nothing pre-existed to change on a fresh ingest
    assert stats["missing_txt"] == 2    # the 2 DeleteCitation refs processed

    rows = {r["uid"]: r for r in con.execute("SELECT uid, error, txt_hash FROM documents")}
    assert len(rows) == 16  # one row per distinct PMID; deletes update, not duplicate

    live = {uid for uid, r in rows.items() if r["error"] is None}
    tombstoned = {uid for uid, r in rows.items() if r["error"] == DELETE_MARKER}
    assert tombstoned == {"pubmed:30000002", "pubmed:30000003"}
    assert live == {f"pubmed:{n}" for n in range(30000001, 30000017)} - tombstoned

    # A freshly-`added` row never gets its hash computed (fetch() only runs
    # on the change-detection path) — same pre-existing quirk verified for
    # Gutenberg's golden fixture. See test_revision_overwrites_across_
    # separate_sync_runs below for the genuine changed-row hash proof.
    assert rows["pubmed:30000001"]["txt_hash"] is None

    # idempotence: re-running sync against the same, unchanged fixture is a no-op.
    stats2 = discovery.sync_collection(con, collection)
    assert stats2["added"] == 0
    assert stats2["changed"] == 0
    rows2 = con.execute("SELECT COUNT(*) c FROM documents").fetchone()["c"]
    assert rows2 == 16


def test_revision_overwrites_across_separate_sync_runs(tmp_path):
    """The more realistic delta scenario: baseline synced today, updatefile
    arrives and is synced tomorrow as a SEPARATE sync_collection() call —
    this exercises the genuine `changed` path (the single-pass ingest test
    above absorbs same-pass revisions into a single `added`, per its own
    docstring)."""
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    shutil.copy2(FIXTURE_CORPUS / "pubmed25n0001.xml", corpus / "pubmed25n0001.xml")
    shutil.copy2(FIXTURE_CORPUS / "pubmed25n0001.xml.md5", corpus / "pubmed25n0001.xml.md5")

    collection = collections_mod.Collection(
        name="pubmed", plugin="pubmed", profile=None, embed_mode_default=None,
        corpus_dir=corpus, manifest_path=tmp_path / "manifest.db",
        fts_dir=tmp_path / "fts", lance_dir=tmp_path / "lancedb")
    con = db.connect(collection)

    stats1 = discovery.sync_collection(con, collection)
    assert stats1["added"] == 11  # file1's 11 baseline records

    baseline_hash = con.execute(
        "SELECT txt_hash FROM documents WHERE uid='pubmed:30000001'").fetchone()["txt_hash"]

    # the updatefile arrives later
    shutil.copy2(FIXTURE_CORPUS / "pubmed25n0002.xml", corpus / "pubmed25n0002.xml")
    shutil.copy2(FIXTURE_CORPUS / "pubmed25n0002.xml.md5", corpus / "pubmed25n0002.xml.md5")

    stats2 = discovery.sync_collection(con, collection)
    assert stats2["added"] == 3        # 30000012/13/14 are new
    assert stats2["changed"] == 1      # 30000001 is genuinely revised
    assert stats2["missing_txt"] == 1  # the DeleteCitation for 30000002

    revised_hash = con.execute(
        "SELECT txt_hash FROM documents WHERE uid='pubmed:30000001'").fetchone()["txt_hash"]
    assert revised_hash != baseline_hash

    # idempotent re-run against the now-stable two-file corpus
    stats3 = discovery.sync_collection(con, collection)
    assert stats3["added"] == 0
    assert stats3["changed"] == 0
