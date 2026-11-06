"""tests/share/test_holdings.py — holdings (04 §4.3; 09 §9.3): what a
collection's file wants, what the SHARD holds, the mark after the rename,
promotion that verifies the root first and sets a wrong file aside."""
import sqlite3

import pytest

from app.hold import shard as shard_mod, snapshot as sn
from app.hold.shard import MemberFile
from app.share import holdings, members as mem

CID = "sha256:" + "0" * 64


def a_snapshot(tmp_path, *, slices=("year=2023", "year=2024"), terms=True):
    d = tmp_path / "shard"
    files = {}
    shards = []
    data = {}
    for i, sl in enumerate(slices, 1):
        for n in (1, 2):
            b = bytes([i * 10 + n]) * (20_000 * n)
            path = f"records/{sl}/{n:05d}.jsonl.zst"
            data[path] = b
            shards.append(sn.Shard(slice=sl, n=n, root=shard_mod.pieces_root(b), records_root="sha256:" + "11" * 32,
                                   first_id="a", last_id="z", records=2, chunks=4,
                                   chunks_root="sha256:" + "22" * 32, tokens=2048, bytes=len(b)))
    val = b'{"checks": []}'
    data["validation.json"] = val
    files["validation.json"] = {"root": shard_mod.pieces_root(val), "bytes": len(val)}
    if terms:
        t = b"the terms\n"
        data["LICENSE-TERMS"] = t
        files["LICENSE-TERMS"] = {"root": shard_mod.pieces_root(t), "bytes": len(t)}
    else:
        files["LICENSE-TERMS"] = None
    cal = b"c" * 100
    data["calibration/abc.safetensors"] = cal
    files["calibration/abc.safetensors"] = {"root": shard_mod.pieces_root(cal), "bytes": len(cal)}
    snap = sn.Snapshot(collection_id=CID, snapshot_id="2026-08", previous=None, shards=tuple(shards),
                       delta=sn.Delta((), (), ()), files=files)
    return d, snap, data


def test_member_names_say_their_kind_and_slice():
    assert holdings.kind_of("records-year=1975-1979") == "records"
    assert holdings.slice_of("records-year=1975-1979") == "year=1975-1979"
    assert holdings.slice_of("relations-lang=de/pages=3M") == "lang=de/pages=3M"
    assert (holdings.kind_of("terms"), holdings.slice_of("terms")) == ("terms", None)
    # the family map (09 §9.16): `hold collection show wikidata-bge` raised on it (2026-11-01)
    assert (holdings.kind_of("slicing"), holdings.slice_of("slicing")) == ("slicing", None)
    assert "slicing" in holdings.WHOLE_KINDS, "a fetcher needs the map before any slice is of use"
    assert holdings.kind_of("calibration-abc") == "calibration" and holdings.slice_of("seals-2026-08") is None
    assert holdings.slice_of("vectors-" + "a" * 16 + "-year=2023") == "year=2023"
    assert holdings.kind_of("index-" + "a" * 16) == "index" and holdings.slice_of("index-" + "a" * 16) is None
    assert holdings.slice_of("index-year=2023") is None, "no text-index member since 2026-10-08: index- is the vector index"
    with pytest.raises(mem.MemberError):
        holdings.kind_of("plates-year=2023")


def test_wanted_is_the_file_s_slices_and_kinds_with_the_whole_collection_members_terms_first(tmp_path):
    d, snap, data = a_snapshot(tmp_path)
    names = [m.name for m in holdings.wanted(snap, slices="all", kinds=("records", "vectors"))]
    assert names == ["terms", "calibration-abc", "validation", "records-year=2023", "records-year=2024"], \
        "terms first, then the whole-collection members, then the slices; vectors come as bundles, not here"
    names = [m.name for m in holdings.wanted(snap, slices=("year=2024",), kinds=("records",))]
    assert names == ["terms", "calibration-abc", "validation", "records-year=2024"]
    names = [m.name for m in holdings.wanted(snap, slices=(), kinds=("records",))]
    assert names == ["terms", "calibration-abc", "validation"], "no slice: the whole-collection members alone"
    assert [m.name for m in holdings.wanted(snap, slices="all", kinds=("relations",))] == \
        ["terms", "calibration-abc", "validation"], "no relations shards in this snapshot"
    d2, snap2, _ = a_snapshot(tmp_path / "green", terms=False)
    assert "terms" not in [m.name for m in holdings.wanted(snap2, slices="all", kinds=("records",))]


def test_held_and_missing_are_by_presence_at_the_snapshot_s_size(tmp_path):
    d, snap, data = a_snapshot(tmp_path)
    want = holdings.wanted(snap, slices="all", kinds=("records",))
    assert len(holdings.missing(d, want)) == 7, "nothing on disk: every file of every wanted member"
    for path in ("records/year=2023/00001.jsonl.zst", "records/year=2023/00002.jsonl.zst", "LICENSE-TERMS"):
        (d / path).parent.mkdir(parents=True, exist_ok=True)
        (d / path).write_bytes(data[path])
    (d / "validation.json").write_bytes(b"short")                  # the wrong size: not held
    gone = holdings.missing(d, want)
    assert [f.path for _, f in gone] == ["calibration/abc.safetensors", "validation.json",
                                         "records/year=2024/00001.jsonl.zst", "records/year=2024/00002.jsonl.zst"]
    by_name = {m.name: m for m in want}
    assert holdings.held(d, by_name["records-year=2023"]) and holdings.held(d, by_name["terms"])
    assert not holdings.held(d, by_name["validation"])
    assert holdings.summary(d, snap) == {"slices": ["year=2023"], "kinds": ["records", "terms"],
                                         "members": 2}
    assert holdings.summary(d, None) == {"slices": [], "kinds": [], "members": 0}


def test_promotion_verifies_the_root_renames_then_marks_and_sets_a_wrong_file_aside(tmp_path):
    d, snap, data = a_snapshot(tmp_path)
    d.mkdir()
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    staging = tmp_path / "state" / holdings.STAGING
    member = f"records-year=2023"
    path = "records/year=2023/00001.jsonl.zst"
    f = MemberFile(path, snap.shards[0].root, snap.shards[0].bytes)
    staged = staging / member / path
    staged.parent.mkdir(parents=True)
    staged.write_bytes(data[path])
    target = holdings.promote(con, staged, d, snapshot_id="2026-08", member=member, f=f, now=5.0)
    assert target == d / path and target.read_bytes() == data[path] and not staged.exists()
    assert holdings.marks(con) == {path: {"path": path, "member": member, "snapshot_id": "2026-08",
                                          "root": f.root, "bytes": f.bytes, "promoted": 5.0}}
    # a file that is not what the snapshot says: set aside, nothing in the SHARD, no mark
    path2 = "records/year=2023/00002.jsonl.zst"
    f2 = MemberFile(path2, snap.shards[1].root, snap.shards[1].bytes)
    bad = staging / member / path2
    bad.write_bytes(data[path2][:-1] + b"!")
    with pytest.raises(holdings.Refused, match="pieces root"):
        holdings.promote(con, bad, d, snapshot_id="2026-08", member=member, f=f2)
    assert not bad.exists() and (staging / holdings.REFUSED / member / path2).is_file()
    assert not (d / path2).exists() and set(holdings.marks(con)) == {path}
    short = staging / member / path2
    short.write_bytes(b"x")
    with pytest.raises(holdings.Refused, match="bytes staged"):
        holdings.promote(con, short, d, snapshot_id="2026-08", member=member, f=f2)
    # a mark whose file is gone is cleared
    (d / path).unlink()
    assert holdings.sweep_marks(con, d) == [path] and holdings.marks(con) == {}
