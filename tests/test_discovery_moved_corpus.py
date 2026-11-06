"""A moved corpus forgets EVERY scan cursor its plugin keeps.

Discovery's locators are absolute paths and its cursors are keyed on
corpus-relative ones, so a corpus mounted somewhere else looks unchanged to
the cursor: every container is skipped and no ref is emitted to carry the
new path. `discovery._clear_cursor_if_relocated` exists for exactly that —
and it cleared only `<plugin>_scan_cursor.json`.

Observed live 2026-09-15: wikipedia's origin moved to
<LOUISE_ROOT>/collections/wikipedia/origin. The dump walk's cursor was
cleared; the archive walk's (`wikipedia-archive_scan_cursor.json`) was not,
so every archive shard was skipped as unchanged, no archive locator was
repaired, and the archive import — which groups its candidates into shards
by those locators, relative to the new root — stood still for 40 minutes
with 884,176 documents waiting.
"""
import time

from app import collections as collections_mod, db, discovery
from app.plugins import cursor


def _collection(tmp_path, root):
    return collections_mod.Collection(
        name="wikipedia", plugin="wikipedia", profile=None, embed_mode_default=None,
        corpus_dir=root, manifest_path=tmp_path / "state" / "manifest.sqlite",
        fts_dir=tmp_path / "index", lance_dir=tmp_path / "lance")


def _cursors(state):
    cursor.save(state, "wikipedia", {"enwiki/x.xml.bz2": [1, 2]})
    cursor.save(state, "wikipedia-archive", {"data/en/000.parquet": [3, 4]})


def test_a_moved_corpus_clears_the_archive_walks_cursor_too(tmp_path):
    new_root = tmp_path / "origin"
    new_root.mkdir()
    c = _collection(tmp_path, new_root)
    con = db.connect(c)
    con.execute(
        "INSERT INTO documents(uid,source,native_id,path,updated_at) VALUES(?,?,?,?,?)",
        ("wikipedia:10000000001", "wikipedia", "10000000001",
         "/corpora/wikipedia/data/en/000.parquet::0::15", int(time.time())))
    con.commit()
    state = c.manifest_path.parent
    _cursors(state)

    assert discovery._clear_cursor_if_relocated(con, c, new_root) is True
    assert cursor.load(state, "wikipedia") == {}
    assert cursor.load(state, "wikipedia-archive") == {}, \
        "the archive walk must re-enumerate, or its locators are never repaired"
    con.close()


def test_a_corpus_that_has_not_moved_keeps_both(tmp_path):
    root = tmp_path / "origin"
    root.mkdir()
    c = _collection(tmp_path, root)
    con = db.connect(c)
    con.execute(
        "INSERT INTO documents(uid,source,native_id,path,updated_at) VALUES(?,?,?,?,?)",
        ("wikipedia:10000000001", "wikipedia", "10000000001",
         f"{root.as_posix()}/data/en/000.parquet::0::15", int(time.time())))
    con.commit()
    state = c.manifest_path.parent
    _cursors(state)

    assert discovery._clear_cursor_if_relocated(con, c, root) is False
    assert cursor.load(state, "wikipedia") and cursor.load(state, "wikipedia-archive")
    con.close()


def test_clear_takes_a_plugins_sub_cursors_and_no_other_plugins(tmp_path):
    """The Library's "rescan" is the same call, and "forget everything"
    means every route's cursor — but not a different plugin's whose name
    merely starts the same way."""
    _cursors(tmp_path)
    cursor.save(tmp_path, "wikipediaX", {"a": [1, 1]})
    assert cursor.clear(tmp_path, "wikipedia") is True
    assert cursor.load(tmp_path, "wikipedia") == {}
    assert cursor.load(tmp_path, "wikipedia-archive") == {}
    assert cursor.load(tmp_path, "wikipediaX") == {"a": [1, 1]}
    assert cursor.clear(tmp_path, "wikipedia") is False, "nothing left to forget"


def test_the_same_bytes_restamped_somewhere_else_take_the_new_path_in_one_scan(tmp_path):
    """wikipedia-v8 (2026-10-29): 21.9 M carried rows named the old
    collection's mount, and a corpus reads as moved — a whole re-enumeration
    at every start — until every locator is under the root. A row whose
    (mtime, size) moved and whose hash did not kept its old path for one more
    scan; it takes the new one now, its stage marks untouched."""
    from types import SimpleNamespace as NS
    from app.plugins.base import DocRef
    root = tmp_path / "origin"
    root.mkdir()
    c = _collection(tmp_path, root)
    con = db.connect(c)
    con.execute(
        "INSERT INTO documents(uid,source,native_id,path,mtime,size,txt_hash,rdf_done,"
        "embed_done,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        ("wikipedia:1", "wikipedia", "1", "/old/mount/data/de/000.parquet::0::15",
         100, 7, "h", 1, 1, int(time.time())))
    con.commit()
    plugin = NS(fetch=lambda ref: NS(raw_hash="h"))
    ref = DocRef(source_id="wikipedia", native_id="1",
                 locator=f"{root.as_posix()}/data/de/000.parquet::0::15", mtime=200, size=7)
    counts = {k: 0 for k in ("seen", "added", "changed", "missing", "container_errors",
                             "relocated", "resigned")}
    assert discovery._apply_batch(con, plugin, [ref], counts, None, wait=True)
    row = con.execute("SELECT path,mtime,rdf_done,embed_done FROM documents").fetchone()
    assert row["path"] == ref.locator and row["mtime"] == 200
    assert (row["rdf_done"], row["embed_done"]) == (1, 1), "the same bytes: nothing redone"
    assert counts["relocated"] == 1 and counts["changed"] == 0
    assert discovery._clear_cursor_if_relocated(con, c, root) is False, "the corpus no longer reads as moved"
    con.close()
