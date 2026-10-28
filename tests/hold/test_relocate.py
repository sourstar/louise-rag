"""`hold records relocate` and the locator it repairs (wikipedia-v8,
2026-10-28): the build's locator fell back to a file's bare NAME for a path
outside the origin base, and records-vs-origin re-fetched none of 52,440
sampled records. The locator now reaches the base through a link it holds,
or refuses; relocate writes the origins of records already built again, by
the build's own function and writer — byte for byte the parts a rebuild
writes (RULE 1)."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app import plugin_io
from app.hold import relocate as rl, records as rec
from app.hold.codex import CodexError
from app.pack import build as pack_build, records as pack_records
from tests.hold.test_build_and_snapshot import _build, _collection


def _parts(root):
    shard = root / "gutenberg" / "shard"
    return {p.relative_to(shard).as_posix(): p.read_bytes()
            for p in sorted((shard / "records").rglob("*.jsonl.zst"))}


def _rows(root):
    db = root / "gutenberg" / "state" / "build" / ".build" / "build.db"
    con = sqlite3.connect(db)
    try:
        return con.execute("SELECT slice, n, path, records, chunks, bytes, sha256, root, "
                           "records_root, chunks_root FROM shards ORDER BY slice, n").fetchall()
    finally:
        con.close()


def _break(root):
    """The old build's records: every origin wrong (a file part that locates
    nothing, as the fallback's bare name did); the rows updated as that build
    recorded them."""
    from app.hold import shard as shard_mod
    shard = root / "gutenberg" / "shard"
    con = sqlite3.connect(root / "gutenberg" / "state" / "build" / ".build" / "build.db")
    for path in sorted((shard / "records").rglob("*.jsonl.zst")):
        total = pack_records.read_index(path).chunks
        recs = list(pack_records.iter_records(path))
        for i, r in enumerate(recs):
            # wrong as the old fallback was: a file part that locates nothing
            r["origin"] = "lost/" + r["origin"]
            r["chunks"] = total if i == 0 else 0
        info = pack_records.write_shard(path, recs)
        con.execute("UPDATE shards SET bytes=?, sha256=?, root=? WHERE path=?",
                    (info.bytes, info.sha256, shard_mod.pieces_root_of(path),
                     path.relative_to(shard).as_posix()))
    con.commit()
    con.close()


def test_relocate_writes_the_parts_a_rebuild_writes(isolated_config, tmp_path, corpus_dir):
    root = _collection(tmp_path)
    _build(root, corpus_dir, records_per_shard=2)
    good_parts, good_rows = _parts(root), _rows(root)
    _break(root)
    assert _parts(root) != good_parts, "the broken origins changed the parts"
    dry = rl.relocate(root, "gutenberg", corpus_dir=corpus_dir, workers=1, dry_run=True, log=lambda *_: None)
    assert dry.relocated == dry.records > 0 and dry.rewritten == 0
    assert _parts(root) != good_parts, "a dry run writes nothing"
    got = rl.relocate(root, "gutenberg", corpus_dir=corpus_dir, workers=1, log=lambda *_: None)
    assert got.relocated == got.records and got.rewritten == len(good_parts)
    assert _parts(root) == good_parts, "byte for byte the parts the build wrote"
    assert _rows(root) == good_rows, "and the rows snapshot create reads"
    again = rl.relocate(root, "gutenberg", corpus_dir=corpus_dir, workers=1, log=lambda *_: None)
    assert again.relocated == 0 and again.rewritten == 0, "idempotent"


def test_relocate_is_refused_while_a_snapshot_lists_the_parts(isolated_config, tmp_path, corpus_dir):
    from app.hold import snapshot as sn
    root = _collection(tmp_path)
    _build(root, corpus_dir, records_per_shard=2)
    sn.create(root, "gutenberg", snapshot_id="2026-08", log=lambda *_: None)
    with pytest.raises(CodexError, match="snapshot.*never change"):
        rl.relocate(root, "gutenberg", corpus_dir=corpus_dir, workers=1, log=lambda *_: None)


def test_a_path_reaches_the_base_through_a_link_it_holds_or_is_refused(tmp_path):
    elsewhere = tmp_path / "wikivec" / "data" / "de"
    elsewhere.mkdir(parents=True)
    (elsewhere / "000.parquet").write_bytes(b"x")
    base = tmp_path / "wikipedia"
    base.mkdir()
    (base / "data").symlink_to(tmp_path / "wikivec" / "data")
    via = plugin_io.relative_to_base(elsewhere / "000.parquet", base)
    assert via == Path("data/de/000.parquet"), "through the link the base holds"
    assert plugin_io.relative_to_base(base / "data" / "de" / "000.parquet", base) == Path("data/de/000.parquet")
    assert plugin_io.relative_to_base(Path("enwiki/x.bz2"), base) == Path("enwiki/x.bz2"), \
        "a relative path is relative to the base already"
    outside = tmp_path / "other" / "000.parquet"
    outside.parent.mkdir()
    outside.write_bytes(b"y")
    assert plugin_io.relative_to_base(outside, base) is None

    class Plugin:
        pass
    with pytest.raises(plugin_io.OriginOutsideBase, match="not under the origin base"):
        plugin_io.origin_locator(Plugin(), f"{outside}#1", base)
    assert plugin_io.origin_locator(Plugin(), f"{elsewhere / '000.parquet'}#1", base) == "data/de/000.parquet#1"


def test_the_build_stops_on_a_record_that_cannot_locate_its_origin(
        isolated_config, tmp_path, corpus_dir, monkeypatch):
    """Not skipped: a skipped record is a quietly incomplete SHARD."""
    root = _collection(tmp_path)

    def refuse(plugin, locator, base):
        raise plugin_io.OriginOutsideBase(f"{locator}: not under the origin base {base}")
    monkeypatch.setattr(plugin_io, "origin_locator", refuse)
    with pytest.raises(pack_build.BuildError, match="not under the origin base"):
        rec.build(root, "gutenberg", corpus_dir=corpus_dir, log=lambda *_: None, records_per_shard=2)
