"""A snapshot nothing published names is DISCARDED, not superseded, and the
collection's records are built again from its own record lines, cut afresh:
the result is the snapshot a build by the book makes (RULE 1; wikipedia-v8's
2026-09, which listed 21.9 M twins of archive pages, 2026-10-30)."""
from __future__ import annotations

import json

import pytest

from app import db
from app.hold import collection as col, discard as discard_mod, records as rec, snapshot as sn
from app.hold.codex import CodexError
from app.pack import build as pack_build
from app.share import seeder
from tests.hold.test_build_and_snapshot import _build, _collection


def _manifest(root, corpus_dir):
    coll = rec._local_collection(root, "gutenberg", col.read(root, "gutenberg"), corpus_dir=corpus_dir)
    from app import discovery
    con = db.connect(coll)
    if not con.execute("SELECT COUNT(*) FROM documents").fetchone()[0]:
        discovery.sync_collection(con, coll)
    return con


def _parts(root) -> dict[str, bytes]:
    d = col.shard_dir_of(root, "gutenberg") / "records"
    return {p.relative_to(d).as_posix(): p.read_bytes() for p in sorted(d.rglob("*.jsonl.zst"))}


def _by_the_book(tmp_path, corpus_dir, without: str | None) -> tuple[dict, str]:
    root = _collection(tmp_path / "book")
    con = _manifest(root, corpus_dir)
    if without:
        con.execute("DELETE FROM documents WHERE uid=?", (without,))
    con.close()
    _build(root, corpus_dir, records_per_shard=1)
    return _parts(root), sn.create(root, "gutenberg", snapshot_id="2026-08", log=lambda *_: None).hash


def _discard(root, snapshot_id="2026-08", **kw):
    con = _manifest(root, None)
    try:
        return discard_mod.discard(root, "gutenberg", snapshot_id, con=con, **kw)
    finally:
        con.close()


def test_a_document_removed_after_the_snapshot_leaves_no_trace_in_the_one_made_again(
        isolated_config, tmp_path, corpus_dir, monkeypatch):
    root = _collection(tmp_path / "a")
    con = _manifest(root, corpus_dir)
    con.close()
    _build(root, corpus_dir, records_per_shard=1)
    first = sn.create(root, "gutenberg", snapshot_id="2026-08", log=lambda *_: None)
    (root / "gutenberg" / "index" / "facets").mkdir(parents=True)
    con = _manifest(root, corpus_dir)
    gone = con.execute("SELECT uid FROM documents ORDER BY uid LIMIT 1").fetchone()[0]
    con.execute("DELETE FROM documents WHERE uid=?", (gone,))     # a twin, removed
    con.close()

    said = _discard(root, dry_run=True)
    assert said["discarded"] is False and seeder.current_snapshot(col.shard_dir_of(root, "gutenberg"))
    out = _discard(root)
    assert (out["hash"], out["parts"]) == (first.hash, len(first.shards))
    assert seeder.current_snapshot(col.shard_dir_of(root, "gutenberg")) is None
    assert _parts(root) == {}, "its parts moved aside with it"
    assert not (root / "gutenberg" / "index").exists(), "what was derived from it goes"

    def never(*a, **k):
        raise AssertionError("a record the discarded snapshot lists was parsed")
    monkeypatch.setattr(pack_build, "_record_of", never)
    rec.build(root, "gutenberg", corpus_dir=corpus_dir, log=lambda *_: None, records_per_shard=1,
              records_from_discarded="2026-08")
    rec.write_validation(root, "gutenberg")
    monkeypatch.undo()
    again = sn.create(root, "gutenberg", snapshot_id="2026-08", log=lambda *_: None)
    parts, book_hash = _by_the_book(tmp_path, corpus_dir, without=gone)
    assert _parts(root) == parts, "the parts a build by the book cuts, byte for byte"
    assert again.hash == book_hash != first.hash
    assert again.previous is None and gone not in {u for u in again.delta.added}


def test_a_document_the_discarded_snapshot_lacks_is_parsed_not_skipped(
        isolated_config, tmp_path, corpus_dir):
    root = _collection(tmp_path / "a")
    con = _manifest(root, corpus_dir)
    late = dict(con.execute("SELECT * FROM documents ORDER BY uid DESC LIMIT 1").fetchone())
    con.execute("DELETE FROM documents WHERE uid=?", (late["uid"],))
    con.close()
    _build(root, corpus_dir, records_per_shard=1)
    sn.create(root, "gutenberg", snapshot_id="2026-08", log=lambda *_: None)
    con = _manifest(root, corpus_dir)
    con.execute(f"INSERT INTO documents({','.join(late)}) VALUES({','.join('?' * len(late))})",
                list(late.values()))                               # admitted after the snapshot
    con.close()
    _discard(root)
    rec.build(root, "gutenberg", corpus_dir=corpus_dir, log=lambda *_: None, records_per_shard=1,
              records_from_discarded="2026-08")
    rec.write_validation(root, "gutenberg")
    again = sn.create(root, "gutenberg", snapshot_id="2026-08", log=lambda *_: None)
    parts, book_hash = _by_the_book(tmp_path, corpus_dir, without=None)
    assert _parts(root) == parts and again.hash == book_hash


def test_a_served_snapshot_or_one_a_later_names_is_not_discarded(isolated_config, tmp_path, corpus_dir):
    root = _collection(tmp_path / "a")
    _build(root, corpus_dir, records_per_shard=1)
    sn.create(root, "gutenberg", snapshot_id="2026-08", log=lambda *_: None)
    con = _manifest(root, corpus_dir)
    db.set_control(con, seeder.CONTROL_KEY, json.dumps({"snapshot_id": "2026-08"}))
    with pytest.raises(CodexError, match="served it"):
        discard_mod.discard(root, "gutenberg", "2026-08", con=con)
    db.set_control(con, seeder.CONTROL_KEY, "")
    con.close()
    sn.create(root, "gutenberg", snapshot_id="2026-09", log=lambda *_: None)
    with pytest.raises(CodexError, match="not the latest"):
        _discard(root)
    assert seeder.current_snapshot(col.shard_dir_of(root, "gutenberg")).snapshot_id == "2026-09"
    assert _parts(root), "nothing moved"


def test_a_rebuild_from_a_snapshot_never_discarded_is_refused(isolated_config, tmp_path, corpus_dir):
    root = _collection(tmp_path / "a")
    with pytest.raises(CodexError, match="no discarded snapshot"):
        rec.build(root, "gutenberg", corpus_dir=corpus_dir, log=lambda *_: None,
                  records_from_discarded="2026-08")
