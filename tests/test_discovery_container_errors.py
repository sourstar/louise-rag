"""A container that cannot be enumerated is a scan REPORT, not a document.

Observed live on PubMed: container-level failures (a scan racing an
in-flight download) were stored as documents keyed on the FILENAME, and
nothing ever removed them — 264 orphan rows, 116 of them for containers
that had since enumerated perfectly. An operator's "clear errors & retry"
then made every one eligible for the rdf stage, where fetch() can only
refuse: the locator carries no record index. They re-errored every round,
forever.
"""
import pytest

from app import collections as collections_mod, db, discovery
from app.plugins.base import CanonicalDoc, DocRef, RawDoc


class ScanPlugin:
    """Yields one healthy document, one BROKEN document, and one container
    failure — the three cases discovery must tell apart."""
    source_id = "scan"
    chunking_profile = "book"

    def __init__(self, container_fails=True):
        self.container_fails = container_fails

    def discover(self, state):
        yield DocRef(source_id="scan", native_id="1", locator="/c/x::0",
                     mtime=1, size=10)
        yield DocRef(source_id="scan", native_id="2", locator="/c/x::1",
                     mtime=1, size=10, error="no txt file")
        if self.container_fails:
            yield DocRef(source_id="scan", native_id="broken.xml.gz",
                         locator="/c/broken.xml.gz", scope="container",
                         error="unparseable container broken.xml.gz: EOFError")

    def fetch(self, ref):
        return RawDoc(ref=ref, raw_hash="h", data=b"x")

    def parse(self, raw):
        return CanonicalDoc(uid=f"scan:{raw.ref.native_id}", title="t",
                            creators=[], language="en", date=None, subjects=[],
                            keys=[], extra={}, sections=[("A", "body")],
                            raw_hash="h")


@pytest.fixture
def collection(isolated_config, tmp_path, monkeypatch):
    monkeypatch.setitem(collections_mod.PLUGIN_REGISTRY, "scan", ScanPlugin())
    tmp_path.mkdir(parents=True, exist_ok=True)
    return collections_mod.Collection(
        name="scan", plugin="scan", profile=None, embed_mode_default=None,
        corpus_dir=tmp_path, manifest_path=tmp_path / "manifest.db",
        fts_dir=tmp_path / "fts", lance_dir=tmp_path / "lancedb")


def test_container_failure_creates_no_document(collection):
    con = db.connect(collection)
    stats = discovery.sync_collection(con, collection)

    uids = {r["uid"] for r in con.execute("SELECT uid FROM documents")}
    assert uids == {"scan:1", "scan:2"}, \
        "a container failure must never become a document row"
    assert "scan:broken.xml.gz" not in uids
    assert stats["container_errors"] == 1
    assert stats["missing_txt"] == 1        # the broken DOCUMENT still counts

    # it is reported, not swallowed — the System page merges every
    # collection's events, so this is where an operator sees it
    messages = [r["message"] for r in con.execute(
        "SELECT message FROM events WHERE stage='discover'")]
    assert any("broken.xml.gz" in m for m in messages)


def test_broken_documents_still_become_documents(collection):
    """The distinction must not swallow genuine per-document failures: a
    book whose .txt is missing stays a real, countable, retryable row."""
    con = db.connect(collection)
    discovery.sync_collection(con, collection)
    row = con.execute("SELECT error FROM documents WHERE uid='scan:2'").fetchone()
    assert row["error"] == "no txt file"


def test_a_clean_rescan_heals_by_not_reporting(collection, monkeypatch):
    """No orphan can survive a fixed container: the next scan simply does
    not report it, and there was never a row to clean up."""
    con = db.connect(collection)
    discovery.sync_collection(con, collection)
    monkeypatch.setitem(collections_mod.PLUGIN_REGISTRY, "scan",
                        ScanPlugin(container_fails=False))
    stats = discovery.sync_collection(con, collection)

    assert "container_errors" not in stats
    uids = {r["uid"] for r in con.execute("SELECT uid FROM documents")}
    assert uids == {"scan:1", "scan:2"}


def test_every_plugin_marks_container_failures(tmp_path):
    """The container-based plugins must all use scope='container' — each
    was minting filename-keyed phantoms the same way. (genome, the fourth,
    is boxed in attic/.)"""
    from app.plugins.base import DiscoverState
    from app.plugins.embedded.source import EmbeddedSource
    from app.plugins.pubmed.source import PubmedSource
    from app.plugins.wikipedia.source import WikipediaSource

    root = tmp_path / "corpus"
    (root / "data" / "en").mkdir(parents=True)
    # one unreadable file per plugin's expected shape
    (root / "bad.xml.gz").write_bytes(b"not gzip")
    (root / "data" / "en" / "000.parquet").write_bytes(b"not parquet")
    (root / "xxwiki-latest-pages-articles-multistream.xml.bz2").write_bytes(b"x")

    state = DiscoverState(corpus_root=root, state_dir=None)
    for source in (PubmedSource(), EmbeddedSource(), WikipediaSource()):
        errors = [r for r in source.discover(state) if r.error]
        assert errors, f"{source.source_id}: expected a container failure"
        for ref in errors:
            assert ref.scope == "container", \
                f"{source.source_id} still mints a document for a container failure"
