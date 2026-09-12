"""A final shard reopens when fts crosses its range.

THE BUG. `final` is decided by `pending_in_range_sql`, which counts
documents still UPSTREAM of fts. A document that crosses fts during the
shard's pass is therefore counted as "not waiting" — it is past fts —
while never having been offered the shard's vector, because the pass
already walked its id. A final shard is never walked again, so that
vector can never arrive: the document falls to the local encoder, which
is the exact trade genesis exists to avoid, and under `embed = off` it
gets nothing at all.

Measured on pubmed 2026-09-13: 37 of 37 shards final, 38,324 documents
inside the archive's own id range unembedded, concentrated in the shards
fts was working through as the archive drained.
"""
import sqlite3

import pytest

from app.bundles import genesis


class FakeArchive:
    archive_id = "medcpt-pubmed"
    local = False
    ids_per_shard = 1_000_000
    shards = ["0", "1", "2"]

    def id_range_of(self, shard):
        n = int(shard)
        return (n * self.ids_per_shard, (n + 1) * self.ids_per_shard - 1)


class FakeCollection:
    genesis_archive = "medcpt-pubmed"
    name = "pubmed"
    manifest_path = "/tmp/pubmed.db"


@pytest.fixture
def con():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE genesis_imports (archive TEXT, shard TEXT, state TEXT, "
              "pending INTEGER, next_pass_at INTEGER, updated_at INTEGER, "
              "docs INTEGER, PRIMARY KEY (archive, shard))")
    c.execute("CREATE TABLE documents (uid TEXT PRIMARY KEY, native_id TEXT, "
              "in_scope INTEGER DEFAULT 1, error TEXT, fts_done INTEGER DEFAULT 0, "
              "embed_done INTEGER DEFAULT 0)")
    for shard in ("0", "1", "2"):
        c.execute("INSERT INTO genesis_imports(archive, shard, state, pending, "
                  "next_pass_at, updated_at, docs) VALUES (?,?,'final',0,0,1,900000)",
                  ("medcpt-pubmed", shard))
    return c


@pytest.fixture(autouse=True)
def archive(monkeypatch):
    a = FakeArchive()
    monkeypatch.setitem(genesis.ARCHIVES, "medcpt-pubmed", a)
    return a


def test_fts_inside_a_final_shard_reopens_it(con):
    """The whole point: a document that clears fts at id 1,500,000 means
    shard 1 has a vector for it that it can no longer deliver."""
    got = genesis.reopen_finals_for_range(con, FakeCollection(), ["1500000"])
    assert got == ["1"]
    row = con.execute("SELECT state, pending, next_pass_at FROM genesis_imports "
                      "WHERE shard='1'").fetchone()
    assert row["state"] == "applied", "the drain only walks applied/applying"
    assert row["pending"] is None, "unknown pending is what makes it DUE"
    assert row["next_pass_at"] == 0, "and due now, not after the old backoff"


def test_the_other_shards_are_untouched(con):
    genesis.reopen_finals_for_range(con, FakeCollection(), ["1500000"])
    others = con.execute("SELECT shard FROM genesis_imports WHERE state='final' "
                         "ORDER BY shard").fetchall()
    assert [r["shard"] for r in others] == ["0", "2"]


def test_a_batch_spanning_two_shards_reopens_both(con):
    got = genesis.reopen_finals_for_range(con, FakeCollection(), ["999999", "1000001"])
    assert sorted(got) == ["0", "1"]


def test_a_batch_that_straddles_a_shard_without_entering_it(con):
    """min/max bracket shard 1 but no document is IN it — the range test
    alone would reopen it and pay a re-fetch for nothing."""
    got = genesis.reopen_finals_for_range(con, FakeCollection(), ["5", "2000005"])
    assert sorted(got) == ["0", "2"]


def test_an_already_open_shard_is_not_rewritten(con):
    con.execute("UPDATE genesis_imports SET state='applied', pending=7, "
                "next_pass_at=9999 WHERE shard='1'")
    assert genesis.reopen_finals_for_range(con, FakeCollection(), ["1500000"]) == []
    row = con.execute("SELECT pending, next_pass_at FROM genesis_imports "
                      "WHERE shard='1'").fetchone()
    assert (row["pending"], row["next_pass_at"]) == (7, 9999), \
        "its own backoff and pending count survive"


def test_non_numeric_ids_belong_to_no_range(con):
    assert genesis.reopen_finals_for_range(con, FakeCollection(), ["PMC123", None]) == []


def test_a_local_archive_has_no_declared_ranges(con, archive, monkeypatch):
    monkeypatch.setattr(archive, "local", True)
    assert genesis.reopen_finals_for_range(con, FakeCollection(), ["1500000"]) == []


def test_a_collection_without_an_archive_pays_nothing(con):
    class Bare:
        genesis_archive = None
    assert genesis.reopen_finals_for_range(con, Bare(), ["1500000"]) == []


def test_the_coverage_epoch_moves_so_the_encoder_re_reserves(con):
    """`_covered_ranges` releases a FINAL shard's range to the local
    encoder. Reopening takes it back, and the backfill cursors are keyed
    on the epoch — without the bump they keep the stale coverage for a
    full TTL and the encoder spends GPU on documents about to arrive."""
    before = genesis.coverage_epoch()
    genesis.reopen_finals_for_range(con, FakeCollection(), ["1500000"])
    assert genesis.coverage_epoch() > before


class Spy:
    """`sqlite3.Connection.execute` is read-only, so the cost tests wrap
    rather than patch."""

    def __init__(self, con):
        self._con = con
        self.sql: list[str] = []

    def execute(self, sql, *a):
        self.sql.append(" ".join(sql.split()))
        return self._con.execute(sql, *a)

    def __getattr__(self, name):
        return getattr(self._con, name)


def test_the_hot_path_reads_one_small_table_once(con):
    """COST TEST (CLAUDE.md: count the lookups, not the behaviour). This
    runs after every fts batch forever. It must never touch `documents`,
    whose scan on pubmed is 40M rows — the event carries the ids, so no
    count is needed at all."""
    spy = Spy(con)
    genesis.reopen_finals_for_range(spy, FakeCollection(), ["1500000"])
    assert not any("documents" in s for s in spy.sql), spy.sql
    reads = [s for s in spy.sql if s.upper().startswith("SELECT")]
    assert len(reads) == 1, f"one ledger read per fts batch, got {len(reads)}: {reads}"


def test_nothing_to_reopen_writes_nothing(con):
    """The common case, a million times over: fts is far past the
    archive's ranges. One read, zero writes."""
    spy = Spy(con)
    assert genesis.reopen_finals_for_range(spy, FakeCollection(), ["50000000"]) == []
    assert [s for s in spy.sql if s.upper().startswith("UPDATE")] == []


# ---- the one-shot repair -------------------------------------------------

def test_recheck_reopens_shards_with_unserved_documents(con):
    """The 38,324 already stranded when the fts hook shipped: they crossed
    fts before anything watched for it, so no event will ever fire."""
    con.execute("INSERT INTO documents(uid, native_id, fts_done, embed_done) "
                "VALUES ('a', '1500000', 1, 0)")
    con.execute("INSERT INTO documents(uid, native_id, fts_done, embed_done) "
                "VALUES ('b', '1500001', 1, 0)")
    con.execute("INSERT INTO documents(uid, native_id, fts_done, embed_done) "
                "VALUES ('c', '2500000', 1, 1)")     # served: shard 2 stays final
    got = genesis.reopen_finals_with_unserved(con, FakeCollection())
    assert got == {"1": 2}
    states = dict(con.execute("SELECT shard, state FROM genesis_imports").fetchall())
    assert states == {"0": "final", "1": "applied", "2": "final"}


def test_recheck_ignores_documents_that_have_not_cleared_fts(con):
    """Those are `pending`, which is the OTHER count — the shard is not
    final because of them and reopening it early buys nothing."""
    con.execute("INSERT INTO documents(uid, native_id, fts_done, embed_done) "
                "VALUES ('a', '1500000', 0, 0)")
    assert genesis.reopen_finals_with_unserved(con, FakeCollection()) == {}


def test_recheck_ignores_errored_documents(con):
    """An errored document is out of every selection until a human
    intervenes; reopening a shard for it would re-fetch forever."""
    con.execute("INSERT INTO documents(uid, native_id, fts_done, embed_done, error) "
                "VALUES ('a', '1500000', 1, 0, 'boom')")
    assert genesis.reopen_finals_with_unserved(con, FakeCollection()) == {}


def test_recheck_is_idempotent(con):
    con.execute("INSERT INTO documents(uid, native_id, fts_done, embed_done) "
                "VALUES ('a', '1500000', 1, 0)")
    assert genesis.reopen_finals_with_unserved(con, FakeCollection()) == {"1": 1}
    assert genesis.reopen_finals_with_unserved(con, FakeCollection()) == {}, \
        "shard 1 is no longer final, so the repair has nothing left to do"
