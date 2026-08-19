"""db.log_event must never crash its caller — observed live under the
per-collection pipeline threads: a long discovery write stream held a
manifest's write lock past another writer's busy timeout, and the acquire
thread's ERROR HANDLER died inside log_event, turning survivable lock
contention into a dead thread.
"""
import sqlite3

from app import collections as collections_mod, db


def test_log_event_survives_a_locked_database(isolated_config, tmp_path):
    c = collections_mod.Collection(
        name="t", plugin="gutenberg", profile=None, embed_mode_default=None,
        corpus_dir=tmp_path, manifest_path=tmp_path / "manifest.db",
        fts_dir=tmp_path / "fts", lance_dir=tmp_path / "lance")
    db.connect(c).close()          # create schema

    holder = sqlite3.connect(tmp_path / "manifest.db")
    holder.execute("BEGIN IMMEDIATE")           # hold the write lock

    victim = sqlite3.connect(tmp_path / "manifest.db", timeout=0.05,
                              isolation_level=None)
    victim.row_factory = sqlite3.Row
    # must print a fallback line and RETURN, never raise
    db.log_event(victim, "error", "acquire", "contended write during scan")

    holder.rollback()
    holder.close()
    # once the lock clears, logging persists again
    db.log_event(victim, "info", "acquire", "back to normal")
    n = victim.execute("SELECT COUNT(*) FROM events "
                        "WHERE message='back to normal'").fetchone()[0]
    assert n == 1
