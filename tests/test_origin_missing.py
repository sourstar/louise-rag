"""A document whose file is missing: the self-healing error (operator,
2026-10-30). The rename of wikipedia-v8 re-rooted only the first path of a
two-file locator, and 85,943 documents were written `embed:
FileNotFoundError` — each out of every selection, seen only when the
embedding looked slow. The error now names its file, is said once, and is
cleared by itself once the file is back."""
import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace

from app import collections as collections_mod, db, origin_missing, worker


def test_the_missing_file_is_read_from_the_error_wherever_it_comes_from(tmp_path):
    missing = str(tmp_path / "gone" / "enwiki-page_props.sql.gz")
    try:
        open(missing, "rb")
    except FileNotFoundError as e:
        direct = e
    assert origin_missing.path_of(direct) == missing
    try:
        try:
            open(missing, "rb")
        except FileNotFoundError as e:
            raise RuntimeError("the parse failed") from e
    except RuntimeError as wrapped:
        assert origin_missing.path_of(wrapped) == missing, "through the chain"
    # crossed the parse pool as text
    assert origin_missing.path_of(f"FileNotFoundError: [Errno 2] No such file or directory: '{missing}'") == missing
    assert origin_missing.path_of(NotADirectoryError(20, "Not a directory", "/a/file/x")) == "/a/file/x"
    assert origin_missing.path_of(ValueError("bad XML")) is None


def _manifest(tmp_path, rows):
    p = tmp_path / "state" / "manifest.sqlite"
    p.parent.mkdir(parents=True)
    con = sqlite3.connect(p, isolation_level=None)
    con.execute("CREATE TABLE documents (uid TEXT PRIMARY KEY, error TEXT, retries INT, updated_at INT)")
    con.executemany("INSERT INTO documents VALUES (?,?,?,0)", rows)
    return SimpleNamespace(name="wikipedia", manifest_path=p), con


def test_a_file_back_clears_the_errors_naming_it_and_nothing_else(tmp_path, monkeypatch):
    gone, other = tmp_path / "v8" / "page_props.sql.gz", tmp_path / "elsewhere" / "x.gz"
    c, con = _manifest(tmp_path, [
        ("w:1", f"embed: {origin_missing.message(str(gone))}", 1),
        ("w:2", f"embed: {origin_missing.message(str(gone))}", 3),
        ("w:3", f"rdf: {origin_missing.message(str(other))}", 1),
        ("w:4", "embed: ValueError: bad XML", 1),
        ("w:5", None, 0),
    ])
    monkeypatch.setattr(origin_missing, "HEAL_WINDOW", 2)       # several windows
    assert origin_missing.note(c, str(gone)) is True
    assert origin_missing.note(c, str(gone)) is False, "said once"
    assert origin_missing.note(c, str(other)) is True
    assert origin_missing.heal(con, c) == {}, "nothing is back yet"
    gone.parent.mkdir()
    gone.write_bytes(b"x")
    assert origin_missing.heal(con, c) == {str(gone): 2}
    got = dict((u, (e, r)) for u, e, r in con.execute("SELECT uid, error, retries FROM documents"))
    assert got["w:1"] == (None, 0) and got["w:2"] == (None, 2), "cleared, the retry given back"
    assert got["w:3"][0] and got["w:4"][0] and got["w:5"] == (None, 0), "nothing else touched"
    assert origin_missing.noted(c) == [str(other)], "a path back is forgotten"
    assert origin_missing.due(c, now=0.0) is True and origin_missing.due(c, now=1.0) is False


def test_the_stage_writes_the_file_s_name_says_it_once_and_the_maintainer_heals(
        isolated_config, corpus_dir, monkeypatch, tmp_path):
    c = collections_mod.default_collection()
    con = db.connect(c)
    from app import discovery
    discovery.sync_collection(con, c)
    rows = con.execute("SELECT * FROM documents WHERE rdf_done=0 LIMIT 2").fetchall()
    missing = tmp_path / "renamed" / "book.txt"

    def gone(*_a, **_k):
        raise FileNotFoundError(2, "No such file or directory", str(missing))
    monkeypatch.setitem(worker.STAGES, "rdf", gone)
    for row in rows:
        assert worker._execute_step(con, c, "rdf", row) is True
    errors = [e for (e,) in con.execute("SELECT error FROM documents WHERE error IS NOT NULL")]
    assert errors == [f"rdf: origin missing: {missing}"] * len(rows)
    said = [m for (m,) in con.execute("SELECT message FROM events WHERE level='warn'") if "origin missing" in m]
    assert len(said) == 1 and "cleared by themselves once it is back" in said[0]
    missing.parent.mkdir()
    missing.write_text("back")
    monkeypatch.setattr(origin_missing, "_last_heal", {})
    assert worker._heal_origin_missing(con, c) is True
    assert con.execute("SELECT COUNT(*) FROM documents WHERE error IS NOT NULL").fetchone()[0] == 0
    assert any("origin back" in m for (m,) in con.execute("SELECT message FROM events"))
    con.close()
