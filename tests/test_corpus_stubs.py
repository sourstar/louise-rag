"""Empty book directories are transfer stubs, not documents — and the card
distinguishes what retrying can fix from what it cannot.

Live on the reference node: rsync creates gutenberg's directory tree and
then copies only files matching the spec filter (*.txt*, *.rdf), so a title
published solely as epub/mobi/html left an EMPTY directory behind. Each one
became a document permanently carrying "no txt file": ~34,800 of them,
holding every progress bar short of 100%, and "clear errors & retry" could
never fix a single one because the next scan re-applied the error.
"""
import pytest
from fastapi.testclient import TestClient

from app import collections as collections_mod, config, db, discovery, spaces, web
from app.api import app
from app.plugins.base import DiscoverState
from app.plugins.gutenberg.source import GutenbergSource


def _book(root, book_id, files):
    d = root / str(book_id)
    d.mkdir(parents=True, exist_ok=True)
    for name, body in files.items():
        (d / name).write_text(body, encoding="utf-8")
    return d


def test_empty_directories_are_scan_reports_not_documents(tmp_path):
    # NOT tmp_path/"corpus": the shared corpus_dir fixture owns that path
    root = tmp_path / "stub-corpus"
    _book(root, 1, {"1-0.txt": "real book"})
    _book(root, 2, {"pg2.rdf": "<rdf/>"})       # metadata only: a real entry
    _book(root, 3, {})                            # empty: a transfer stub
    _book(root, 4, {})

    refs = list(GutenbergSource().discover(
        DiscoverState(corpus_root=root, state_dir=None)))
    documents = [r for r in refs if r.scope == "document"]
    reports = [r for r in refs if r.scope == "container"]

    assert {r.native_id for r in documents} == {"1", "2"}, \
        "empty directories must not become documents"
    assert [r.error for r in documents if r.native_id == "2"] == [None], \
        "a metadata-only entry is now a catalogue record, not an error"
    assert len(reports) == 1 and "skipped 2 empty book directories" in reports[0].error


def test_stubs_never_reach_the_manifest(isolated_config, tmp_path, monkeypatch):
    # NOT tmp_path/"corpus": the shared corpus_dir fixture owns that path
    root = tmp_path / "stub-corpus"
    _book(root, 1, {"1-0.txt": "real book"})
    for i in (2, 3, 4):
        _book(root, i, {})
    c = collections_mod.Collection(
        name="g", plugin="gutenberg", profile=None, embed_mode_default=None,
        corpus_dir=root, manifest_path=tmp_path / "m.db",
        fts_dir=tmp_path / "fts", lance_dir=tmp_path / "lance")
    con = db.connect(c)
    stats = discovery.sync_collection(con, c)

    uids = [r["uid"] for r in con.execute("SELECT uid FROM documents")]
    assert uids == ["gutenberg:1"]
    assert stats["seen"] == 1, "a scan report is not a document seen"
    assert stats["container_errors"] == 1
    # and the skip is visible rather than silent
    messages = [r["message"] for r in con.execute(
        "SELECT message FROM events WHERE stage='discover'")]
    assert any("empty book directories" in m for m in messages)


@pytest.fixture
def client(isolated_config, monkeypatch):
    monkeypatch.setattr(spaces, "resolve_embed_model_hash", lambda *a: ("abc123", False))
    monkeypatch.setattr(web, "_ping", lambda url: True)
    monkeypatch.setattr(config, "ADMIN_TOKEN", "sekrit")
    with TestClient(app) as c:
        c.headers["X-Admin-Token"] = "sekrit"
        yield c


def test_card_separates_retryable_failures_from_unindexable(client):
    collection = collections_mod.default_collection()
    con = db.connect(collection)
    rows = [
        ("gutenberg:1", "fts: tantivy exploded", 0),   # retryable
        ("gutenberg:2", "no txt file", 0),             # corpus condition
        ("gutenberg:3", "no txt file", 0),
        ("gutenberg:4", None, config.MAX_RETRIES),     # retries exhausted
    ]
    for uid, error, retries in rows:
        con.execute(
            "INSERT INTO documents(uid,source,native_id,path,lang,in_scope,"
            "error,retries) VALUES(?,?,?,?,?,1,?,?)",
            (uid, "gutenberg", uid.split(":")[1], "/x", "en", error, retries))

    page = client.get("/admin/library").text
    assert "2 blocked" in page          # the fts failure + the exhausted one
    assert "2 not indexable" in page    # the two corpus conditions
    assert "failed a stage" in page      # and the retryable ones, named separately
