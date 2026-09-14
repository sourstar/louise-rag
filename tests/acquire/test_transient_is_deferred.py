"""A dropped mirror connection is a deferral, not a file's error.

CLAUDE.md: "Deferral is not failure: an unreachable endpoint ... never
writes an error to a document."

Measured on gutenberg 2026-09-13, mid-fetch: 9,480 of 91,863 fetch_files
rows carried an error and the count climbed all evening. Every one of
them was the same thing —

    rsync aleph.gutenberg.org::gutenberg-epub/10/pg10-images.epub:
    exit 12: read error: Connection reset by peer (104)

— a mirror dropping the connection, from a host that rotates under us
(`ftp.ibiblio.org` is what the spec records). Nothing was lost, because
`_is_pending` has never consulted `error` or `retries`. But the
operator's only summary of fetch health counted mirror flakiness as
failure, which is how it read from outside and what it cost to dismiss.

The retry histogram at the time: 9,228 at retries=1, 245 at 2, 7 at 3.
"""
import sqlite3

import pytest

from app.acquire import engine

RESET = ("TransportError: rsync aleph.gutenberg.org::gutenberg-epub/10/"
         "pg10-images.epub: exit 12: rsync: [receiver] read error: "
         "Connection reset by peer (104)\nrsync error: error in socket IO "
         "(code 10) at io.c(807) [receiver=3.4.1]")
MISSING = ("TransportError: rsync ftp.ibiblio.org::gutenberg-epub/99/pg99.epub: "
           "exit 23: some files could not be transferred")


class Remote:
    def __init__(self, rel_path="10/pg10-images.epub"):
        self.rel_path = rel_path
        self.size = 102239
        self.mtime = 1788557058
        self.extra = False


@pytest.fixture
def con():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE fetch_files (source_id TEXT, rel_path TEXT, "
              "endpoint_url TEXT, remote_size INTEGER, remote_mtime INTEGER, "
              "local_path TEXT, bytes_fetched INTEGER, checksum_declared TEXT, "
              "checksum_algo TEXT, blake2 TEXT, fetch_done INTEGER DEFAULT 0, "
              "fetch_version INTEGER DEFAULT 0, profile TEXT, error TEXT, "
              "retries INTEGER DEFAULT 0, extra INTEGER DEFAULT 0, "
              "updated_at INTEGER, PRIMARY KEY (source_id, rel_path))")
    return c


def _fail(con, error, times=1, rel="10/pg10-images.epub"):
    for _ in range(times):
        engine._upsert_failed(con, "gutenberg", Remote(rel),
                              "ftp.ibiblio.org::gutenberg-epub", 0, "full", error)
    return con.execute("SELECT error, retries FROM fetch_files "
                       "WHERE rel_path=?", (rel,)).fetchone()


def test_a_connection_reset_writes_no_error(con):
    row = _fail(con, RESET)
    assert row["error"] is None
    assert row["retries"] == 1, "the attempt is still counted"


def test_a_real_failure_is_reported_at_once(con):
    """`exit 23` is the file, not the mirror. Nothing about this change
    may make a genuine failure quieter."""
    assert _fail(con, MISSING)["error"] is not None


def test_the_quiet_is_bounded(con):
    """A file that cannot cross after DEFER_ATTEMPTS mirror drops is no
    longer "the mirror is flaky" — it is something a human must see. This
    is the whole reason the deferral is safe."""
    for attempt in range(1, engine.DEFER_ATTEMPTS):
        assert _fail(con, RESET)["error"] is None, f"attempt {attempt}"
    escalated = _fail(con, RESET)
    assert escalated["retries"] == engine.DEFER_ATTEMPTS
    assert escalated["error"] is not None, "it must not retry in silence forever"
    assert "Connection reset" in escalated["error"]


def test_retries_counts_every_attempt_either_way(con):
    """`retries` is what bounds the quiet, so a deferral may never skip
    it — that was the bug this fix could have introduced."""
    assert _fail(con, RESET, times=3)["retries"] == 3


def test_each_file_is_counted_on_its_own(con):
    _fail(con, RESET, times=3, rel="10/pg10-images.epub")
    other = _fail(con, RESET, times=1, rel="11/pg11-images.epub")
    assert other["retries"] == 1 and other["error"] is None


@pytest.mark.parametrize("message", [
    "rsync: connection unexpectedly closed (55 bytes received so far)",
    "rsync error: error in socket IO (code 10) at io.c(807)",
    "rsync error: error in rsync protocol data stream (code 12)",
    "ssh: connect to host: Connection timed out",
    "ssh: connect to host: Connection refused",
    "Temporary failure in name resolution",
    "write error: Broken pipe",
    # verbatim from gutenberg's fetch_files, 2026-09-14 — each was written
    # as a file error on the first attempt while the mirror was failing
    "rsync: [Receiver] failed to connect to ftp.ibiblio.org (152.19.134.42): "
    "No route to host (113)",
    "rsync ftp.ibiblio.org::gutenberg-epub/16877/pg16877.txt: exit 35: rsync "
    "error: timeout waiting for daemon connection (code 35) at socket.c(278)",
    "rsync error: timeout in data send/receive (code 30) at io.c(197)",
    "rsync aleph.gutenberg.org::gutenberg-epub/13954/pg13954-images.epub: "
    "timed out after 300s",
    "@ERROR: max connections (20) reached -- try again later",
])
def test_the_shapes_a_dropped_endpoint_takes(message):
    assert engine.transient(message) is True


@pytest.mark.parametrize("message", [
    "sidecar mismatch: declared md5 abc, computed def",
    "exit 23: some files could not be transferred",
    "No space left on device",
    "Permission denied",
    "",
])
def test_what_is_never_the_endpoint_going_away(message):
    """A checksum mismatch, a missing file, a full disk, a permission —
    none of these get quieter, and an empty message is never transient."""
    assert engine.transient(message) is False


def test_retry_behaviour_is_untouched(con):
    """The point of the change, stated as a test: this is REPORTING only.
    `_is_pending` gates on fetch_done/size/mtime and has never consulted
    `error` or `retries`, so a deferred file and an errored one are
    selected identically. If that ever stops being true, the deferral
    becomes a silent infinite retry and this fix is wrong."""
    import inspect
    src = inspect.getsource(engine._is_pending)
    assert "error" not in src and "retries" not in src, (
        "_is_pending now reads error/retries — re-examine _upsert_failed, "
        "because deferring would then change what gets fetched")
