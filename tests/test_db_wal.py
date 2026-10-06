"""tests/test_db_wal.py — the WAL conversion of a fresh database waits out a
lock (invariant 6: SQLite WAL everywhere). Two connections opening one new
file at once — the web and the deposit thread on node.sqlite, two threads on
a deposit manifest — made `PRAGMA journal_mode=WAL` fail with "database is
locked" on the loser, which has no busy handler for that step. Seen in the
full suite on 2026-10-07 (test_deposit_localagi), as in the deposit manifest
race of 2026-10-05."""
import sqlite3

import pytest

from app import db


class Flaky:
    def __init__(self, fail_times, error="database is locked"):
        self.fail_times, self.error, self.calls = fail_times, error, []

    def execute(self, sql):
        self.calls.append(sql)
        if len(self.calls) <= self.fail_times:
            raise sqlite3.OperationalError(self.error)

        class Row:
            def fetchone(self_inner):
                return ("wal",)
        return Row()


def test_a_locked_conversion_is_retried_then_succeeds():
    con = Flaky(2)
    assert db.wal(con, pause_s=0.0) == "wal"
    assert con.calls == ["PRAGMA journal_mode=WAL"] * 3


def test_a_lock_that_never_lifts_raises_after_the_budget():
    con = Flaky(1000)
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        db.wal(con, tries=5, pause_s=0.0)
    assert len(con.calls) == 5


def test_another_error_is_raised_at_once():
    con = Flaky(3, error="disk I/O error")
    with pytest.raises(sqlite3.OperationalError, match="disk I/O"):
        db.wal(con, pause_s=0.0)
    assert len(con.calls) == 1


def test_a_real_connection_converts(tmp_path):
    con = sqlite3.connect(tmp_path / "x.sqlite", isolation_level=None)
    assert db.wal(con) == "wal"
    assert con.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    con.close()
