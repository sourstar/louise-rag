"""tests/share/test_members.py — members as torrents and the DHT keys (09
§9.16; 04 §4.2–4.3; migration step 9a): a file's torrent is a function of
its path, length and pieces root alone, and libtorrent computes the SAME
infohash from the file — two honest publishers land in one swarm; the
keys are 04 §4.2's; a snapshot implies its members."""
import hashlib
import json

import pytest

from app.hold import drag, snapshot as sn
from app.hold.codex import canonical_json
from app.share import members as m

CID = "sha256:" + "9d" * 32
LID = "sha256:" + "3b" * 32
ROOT = "sha256:" + "0b" * 32
ROOT2 = "sha256:" + "e4" * 32


# ---- bencoding ----------------------------------------------------------------

def test_bencode_is_bep3_with_keys_sorted_as_bytes():
    assert m.bencode(0) == b"i0e" and m.bencode(-7) == b"i-7e"
    assert m.bencode("ab") == b"2:ab" and m.bencode(b"\x00\xff") == b"2:\x00\xff"
    assert m.bencode(["a", 1]) == b"l1:ai1ee"
    assert m.bencode({"b": 1, "a": [2], "": "x"}) == b"d0:1:x1:ali2ee1:bi1ee"
    for bad in (True, 1.5, None):
        with pytest.raises(m.MemberError):
            m.bencode({"k": bad})


def test_bencode_agrees_with_libtorrent():
    lt = pytest.importorskip("libtorrent")
    info = m.info_dict("records-all", "records/all/00001.jsonl.zst", 10485760, ROOT)
    assert m.bencode(info) == lt.bencode(info)


# ---- one file, one torrent --------------------------------------------------------

def test_the_info_dictionary_is_exactly_the_worked_example_of_9_16():
    info = m.info_dict("records-year=1975-1979", "records/year=1975-1979/00001.jsonl.zst",
                       10485760, ROOT)
    assert info == {
        "file tree": {"records": {"year=1975-1979": {"00001.jsonl.zst": {
            "": {"length": 10485760, "pieces root": bytes.fromhex("0b" * 32)}}}}},
        "meta version": 2, "name": "records-year=1975-1979", "piece length": 16777216}
    assert m.infohash(info) == "sha256:" + hashlib.sha256(m.bencode(info)).hexdigest()
    assert len(m.dht_id(m.infohash(info))) == 20


def test_a_torrent_is_a_function_of_path_length_and_root_alone():
    a = m.infohash(m.info_dict("records-all", "records/all/00001.jsonl.zst", 10, ROOT))
    assert a == m.infohash(m.info_dict("records-all", "records/all/00001.jsonl.zst", 10, ROOT))
    assert a != m.infohash(m.info_dict("records-all", "records/all/00001.jsonl.zst", 11, ROOT))
    assert a != m.infohash(m.info_dict("records-all", "records/all/00001.jsonl.zst", 10, ROOT2))
    assert a != m.infohash(m.info_dict("records-all", "records/all/00002.jsonl.zst", 10, ROOT))
    for bad in ("", "/records/x", "records/../x", "records//x", "records/./x"):
        with pytest.raises(m.MemberError):
            m.info_dict("records-all", bad, 10, ROOT)
    with pytest.raises(m.MemberError):
        m.info_dict("records-all", "records/all/00001.jsonl.zst", 10, "sha256:short")


def test_libtorrent_computes_the_same_infohash_from_the_file(tmp_path):
    """The test that makes 04 §4.3 true: a peer that computes the torrent
    from the snapshot (path, length, root) and a peer that lets libtorrent
    build it from the file on disk land in one swarm."""
    lt = pytest.importorskip("libtorrent")
    member, rel = "records-year=1975-1979", "records/year=1975-1979/00001.jsonl.zst"
    data = bytes(range(256)) * 700 + b"tail"          # several 16 KiB blocks, odd tail
    f = tmp_path / member / rel
    f.parent.mkdir(parents=True)
    f.write_bytes(data)
    ours = m.info_dict(member, rel, len(data), drag.pieces_root(data))
    fs = lt.file_storage()
    lt.add_files(fs, str(tmp_path / member))
    t = lt.create_torrent(fs, m.PIECE_LENGTH, flags=lt.create_torrent_flags_t.v2_only)
    lt.set_piece_hashes(t, str(tmp_path))
    gen = t.generate()                                 # libtorrent 2.1 keys its entries as bytes
    theirs = gen.get("info") if "info" in gen else gen[b"info"]
    assert lt.bencode(theirs) == m.bencode(ours), (theirs, ours)
    assert str(lt.torrent_info(gen).info_hashes().v2) == m.infohash(ours)[7:]


# ---- the keys (04 §4.2) --------------------------------------------------------------

def test_the_keys_are_the_first_20_bytes_of_h():
    k = m.collection_key(CID, "2026-08")
    assert k == hashlib.sha256(canonical_json([CID, "2026-08"])).digest()[:20]
    assert canonical_json([CID, "2026-08"]) == json.dumps([CID, "2026-08"],
                                                         separators=(",", ":")).encode()
    assert m.lineage_key(LID, "2026-08") == hashlib.sha256(canonical_json([LID, "2026-08"])).digest()[:20]
    assert m.lineage_key(LID, "2026-08") != k and m.collection_key(CID, "2026-09") != k
    w = "sha256:" + "ab" * 32
    assert m.weights_key(w) == hashlib.sha256(w.encode()).digest()[:20] == m.image_key(w)
    assert all(len(x) == 20 for x in (k, m.weights_key(w)))
    with pytest.raises(m.MemberError):
        m.key()
    with pytest.raises(m.MemberError):
        m.collection_key(CID, "")


# ---- members of a snapshot --------------------------------------------------------------

def shard(sl, n, root, size, ids=("a", "b")):
    return sn.Shard(slice=sl, n=n, root=root, records_root="sha256:" + "11" * 32,
                    first_id=ids[0], last_id=ids[1], records=2, chunks=4,
                    chunks_root="sha256:" + "22" * 32, tokens=2048, bytes=size)


def a_snapshot(**over):
    base = dict(
        collection_id=CID, snapshot_id="2026-08", previous=None,
        shards=(shard("year=1975-1979", 1, ROOT, 10), shard("year=1975-1979", 2, ROOT2, 20),
                shard("year=2023", 1, ROOT, 30)),
        delta=sn.Delta((), (), ()),
        relations=(sn.RelationShard("year=2023", 1, ROOT2, 5, 7),),
        figures=(sn.FiguresShard("year=2023", 1, ROOT, 2, 9),),
        files={"validation.json": {"root": ROOT, "bytes": 100},
               "LICENSE-TERMS": None,
               "calibration/0123456789abcdef.safetensors": {"root": ROOT2, "bytes": 200},
               "calibration/0123456789abcdef.sig.json": {"root": ROOT, "bytes": 300},
               "seals/2026-08.json": {"root": ROOT2, "bytes": 400}},
    )
    base.update(over)
    return sn.Snapshot(**base)


def test_a_snapshot_implies_its_members_with_their_files_and_roots():
    got = {x.name: x for x in m.members_of(a_snapshot())}
    assert sorted(got) == sorted(["records-year=1975-1979", "records-year=2023",
                                  "relations-year=2023", "figures-year=2023", "validation",
                                  "calibration-0123456789abcdef", "seals-2026-08"])
    r = got["records-year=1975-1979"]
    assert [f.path for f in r.files] == ["records/year=1975-1979/00001.jsonl.zst",
                                         "records/year=1975-1979/00002.jsonl.zst"]
    assert r.root == drag.member_root(r.files) and r.bytes == 30
    assert got["records-year=2023"].root == ROOT, "one file: the file's pieces root"
    assert got["relations-year=2023"].files[0].path == "relations/year=2023/00001.jsonl.zst"
    assert got["figures-year=2023"].files[0].path == "figures/year=2023/00001.fig.zst"
    assert [f.path for f in got["calibration-0123456789abcdef"].files] == \
        ["calibration/0123456789abcdef.safetensors", "calibration/0123456789abcdef.sig.json"]
    assert "terms" not in got, "a green collection's null entry is no member"
    assert r.to_dict() == {"name": r.name, "root": r.root, "bytes": 30}
    torrents = r.torrents()
    assert [t.path for t in torrents] == [f.path for f in r.files]
    assert torrents[0].infohash != torrents[1].infohash
    with pytest.raises(m.MemberError):
        m.members_of(a_snapshot(files={"other.json": {"root": ROOT, "bytes": 1}}))


def test_the_terms_member_exists_for_a_gated_class():
    s = a_snapshot(files={"LICENSE-TERMS": {"root": ROOT2, "bytes": 50}})
    got = {x.name: x for x in m.members_of(s)}
    assert got["terms"].files[0].path == "LICENSE-TERMS" and got["terms"].bytes == 50


def test_index_members_come_from_the_drag_on_disk_with_roots_per_file(tmp_path):
    d = tmp_path / "drag"
    seg = d / "index" / "year=2023" / "00001"
    seg.mkdir(parents=True)
    (seg / "meta.json").write_bytes(b'{"fts_version": 3}')
    (seg / "seg.bin").write_bytes(bytes(40000))
    (d / "index" / "year=1975-1979").mkdir()            # empty: no member
    got = {x.name: x for x in m.members_of(a_snapshot(), drag_dir=d)}
    idx = got["index-year=2023"]
    assert [f.path for f in idx.files] == ["index/year=2023/00001/meta.json",
                                           "index/year=2023/00001/seg.bin"]
    assert idx.files[1].root == drag.pieces_root(bytes(40000)) and idx.bytes == 40000 + 18
    assert "index-year=1975-1979" not in got
    assert idx.root == drag.member_root(idx.files)
