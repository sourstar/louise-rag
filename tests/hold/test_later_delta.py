"""A LATER snapshot's delta (2026-10-28). It read the previous snapshot's
parts at their paths after the build had promoted the new versions over
them: both sides were the new bytes, and every later snapshot would have
said nothing changed. The previous version is now read where it is held —
its superseded name, kept until the snapshot is frozen where the origin
replaces in place — verified by its `records_root`, and only the parts whose
bytes changed are read, compared by an Arrow hash join."""
from __future__ import annotations

import os
import random

import pyarrow as pa
import pytest

from app.hold import collection as col, records as rec, shard as shard_mod, snapshot as sn
from app.hold.codex import CodexError
from tests.hold.test_build_and_snapshot import _build, _collection


def _revise(root, corpus_dir, shard_dir, part) -> str:
    """One book of `part` revised upstream and synced; its uid."""
    from app import db, discovery
    from app.pack import records as pack_records
    uid = next(iter(pack_records.iter_records(shard_dir / part.path)))["uid"]
    book = corpus_dir / uid.split(":")[1]
    txt = next(book.glob("*.txt*"))
    data = txt.read_bytes()
    i = data.index(b"*** END OF")
    txt.write_bytes(data[:i] + b"A paragraph revised upstream.\n\n" + data[i:])
    os.utime(txt, (2_000_000_000, 2_000_000_000))
    os.utime(book, (2_000_000_000, 2_000_000_000))
    coll = rec._local_collection(root, "gutenberg", col.read(root, "gutenberg"), corpus_dir=corpus_dir)
    con = db.connect(coll)
    try:
        assert discovery.sync_collection(con, coll)["changed"] == 1
    finally:
        con.close()
    return uid


def test_an_origin_that_replaces_in_place_keeps_the_old_version_until_the_snapshot(
        isolated_config, tmp_path, corpus_dir, monkeypatch):
    """Gutenberg replaces a book in place. The build keeps the version it
    replaces until the next snapshot is frozen — the delta is computed
    against it — and the snapshot drops it then (01 promise 7)."""
    monkeypatch.setattr(shard_mod, "_origin_versions", {})
    root = _collection(tmp_path)
    _build(root, corpus_dir, records_per_shard=2)
    snap = sn.create(root, "gutenberg", snapshot_id="2026-08", log=lambda *_: None)
    shard_dir = col.shard_dir_of(root, "gutenberg")
    assert not shard_mod.origin_keeps_history(shard_dir)
    first = snap.shards[0]
    old = (shard_dir / first.path).read_bytes()
    uid = _revise(root, corpus_dir, shard_dir, first)
    _build(root, corpus_dir, records_per_shard=2)
    kept = shard_dir / shard_mod.superseded_name(shard_dir / first.path, "2026-08")
    assert kept.read_bytes() == old, "kept until the snapshot: it is still 2026-08's part"
    said: list[str] = []
    snap2 = sn.create(root, "gutenberg", snapshot_id="2026-09", log=said.append)
    assert snap2.delta.to_dict() == {"added": [], "changed": [uid], "tombstoned": []}
    assert not kept.exists(), "dropped once 2026-09 is frozen"
    assert any("1 replaced version(s) of 2026-08 dropped" in s for s in said)
    # the re-run of an existing snapshot: identical bytes, nothing to drop
    assert sn.create(root, "gutenberg", snapshot_id="2026-09", log=lambda *_: None).hash == snap2.hash


def test_a_previous_version_not_held_is_refused_before_anything_is_read(
        isolated_config, tmp_path, corpus_dir, monkeypatch):
    """A delta that guessed passes every check while naming the wrong
    records. A previous version held nowhere is a refusal, and it comes
    from a stat, not after reading the SHARD."""
    from app.pack import records as pack_records
    monkeypatch.setattr(shard_mod, "_origin_versions", {})
    root = _collection(tmp_path)
    _build(root, corpus_dir, records_per_shard=2)
    snap = sn.create(root, "gutenberg", snapshot_id="2026-08", log=lambda *_: None)
    shard_dir = col.shard_dir_of(root, "gutenberg")
    first = snap.shards[0]
    _revise(root, corpus_dir, shard_dir, first)
    _build(root, corpus_dir, records_per_shard=2)
    shard_mod.superseded_name(shard_dir / first.path, "2026-08").unlink()
    read: list = []
    real = pack_records.iter_records
    monkeypatch.setattr(pack_records, "iter_records", lambda p, *a, **k: read.append(p) or real(p, *a, **k))
    with pytest.raises(CodexError, match="not held"):
        sn.create(root, "gutenberg", snapshot_id="2026-09", log=lambda *_: None)
    assert read == [], "refused before any part is read"
    assert not (shard_dir / shard_mod.snapshot_path("2026-09")).exists()


def test_a_copy_of_other_bytes_is_not_the_previous_version(isolated_config, tmp_path, corpus_dir,
                                                           monkeypatch):
    """The size says where to look; the records_root says whether it is that
    version. A held file whose records are not the listed ones is refused."""
    monkeypatch.setattr(shard_mod, "_origin_versions", {})
    root = _collection(tmp_path)
    _build(root, corpus_dir, records_per_shard=2)
    snap = sn.create(root, "gutenberg", snapshot_id="2026-08", log=lambda *_: None)
    shard_dir = col.shard_dir_of(root, "gutenberg")
    first, other = snap.shards[0], snap.shards[1]
    assert sn._pairs(shard_dir / first.path, first.records_root).num_rows == first.records
    assert sn._pairs(shard_dir / first.path, other.records_root) is None, "another part's root"
    _revise(root, corpus_dir, shard_dir, first)
    _build(root, corpus_dir, records_per_shard=2)
    real = sn._pairs
    monkeypatch.setattr(sn, "_pairs", lambda p, r: None if ".2026-08." in p.name else real(p, r))
    with pytest.raises(CodexError, match="no held copy has the records_root"):
        sn.create(root, "gutenberg", snapshot_id="2026-09", log=lambda *_: None)


def test_unchanged_parts_are_not_read(isolated_config, tmp_path, corpus_dir, monkeypatch):
    """A part listed by both snapshots under one root holds the same bytes:
    it adds nothing to the delta and is not read — a Parquet collection
    too, whose previous snapshot is loaded with its container."""
    from app.hold import template
    from app.pack import records as pack_records
    sl = dict(template.default_slicing(template.source_for("gutenberg")))
    root = _collection(tmp_path, slicing={**sl, "container": "parquet"})
    _build(root, corpus_dir, records_per_shard=2)
    sn.create(root, "gutenberg", snapshot_id="2026-08", log=lambda *_: None)
    _build(root, corpus_dir, records_per_shard=2)
    read: list = []
    real = pack_records.iter_records
    monkeypatch.setattr(pack_records, "iter_records", lambda p, *a, **k: read.append(p) or real(p, *a, **k))
    snap2 = sn.create(root, "gutenberg", snapshot_id="2026-09", log=lambda *_: None)
    assert snap2.delta.to_dict() == {"added": [], "changed": [], "tombstoned": []}
    assert read == []
    prev = sn._previous(col.shard_dir_of(root, "gutenberg"), "2026-09")
    assert all(s.path.endswith(".parquet") for s in prev.shards)


def _side(d: dict[str, str], parts: int) -> list:
    items = list(d.items())
    random.shuffle(items)
    out = []
    for k in range(parts):
        chunk = items[k::parts]
        out.append(pa.table({"uid": pa.array([u for u, _ in chunk], pa.string()),
                             "h": pa.array([bytes.fromhex(h) for _, h in chunk], pa.binary(16))}))
    return out


def test_the_join_is_compute_delta():
    """The Arrow join gives exactly what `compute_delta` gives over the two
    whole maps — added, changed, tombstoned, each sorted."""
    rnd = random.Random(7)
    random.seed(7)
    for _ in range(20):
        uids = [f"c:{rnd.randrange(10**6)}" for _ in range(rnd.randrange(0, 400))]
        prev = {u: os.urandom(16).hex() for u in uids if rnd.random() < 0.8}
        cur = {}
        for u in uids:
            r = rnd.random()
            if u in prev and r < 0.6:
                cur[u] = prev[u]
            elif r < 0.85:
                cur[u] = os.urandom(16).hex()
        assert sn._join_delta(_side(prev, 3), _side(cur, 4), name="c") == sn.compute_delta(prev, cur)
    assert sn._join_delta([], [], name="c") == sn.Delta((), (), ())


def test_a_uid_in_two_parts_is_refused():
    h = os.urandom(16)
    t = pa.table({"uid": pa.array(["c:1"], pa.string()), "h": pa.array([h], pa.binary(16))})
    with pytest.raises(CodexError, match="two parts"):
        sn._join_delta([], [t, t], name="c")


def test_a_first_delta_is_read_from_the_parts_indexes(isolated_config, tmp_path, corpus_dir, monkeypatch):
    """A first snapshot's delta from each part's own index, by threads: the
    same uids as decoding every record, which was the whole 43-57 min of
    wikipedia-v8's first snapshot (2026-10-30), and no record decoded."""
    from app.pack import records as pack_records
    for container in ("jsonl", "parquet"):
        from app.hold import template
        sl = dict(template.default_slicing(template.source_for("gutenberg")))
        root = _collection(tmp_path / container, slicing={**sl, "container": container})
        _build(root, corpus_dir, records_per_shard=2)
        shard_dir = col.shard_dir_of(root, "gutenberg")
        snap = sn.create(root, "gutenberg", snapshot_id="2026-08", log=lambda *_: None)
        by_records = sorted({r["uid"] for s in snap.shards
                             for r in pack_records.iter_records(shard_dir / s.path)})
        read: list = []
        real = pack_records.iter_records
        monkeypatch.setattr(pack_records, "iter_records", lambda p, *a, **k: read.append(p) or real(p, *a, **k))
        assert list(sn._first_delta(shard_dir, snap.shards).added) == by_records
        assert read == [], "no record decoded"
        monkeypatch.undo()
