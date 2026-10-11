"""tests/hold/test_shard.py — the SHARD on disk: names, paths and roots.

docs/02-collections.md §2.6 gives the tree, docs/09-formats.md §9.16 fixes
the member names and the roots, §9.5 fixes the pieces root.
"""
import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from app.hold import shard as shard_mod
from app.hold.shard import ShardError, MemberFile
from app.hold.codex import License

SPACE = "03ae64488c899305"


# ---- member names ---------------------------------------------------------

def test_every_kind_has_the_name_the_table_gives_it():
    assert shard_mod.records_member("year=1975-1979") == "records-year=1975-1979"
    assert shard_mod.relations_member("year=1980") == "relations-year=1980"
    assert shard_mod.vectors_member(SPACE, "year=1980") == f"vectors-{SPACE}-year=1980"
    assert shard_mod.buckets_member(SPACE, 2, 7) == f"buckets-{SPACE}-v2-g07"
    assert shard_mod.centres_member(SPACE, 3) == f"centres-{SPACE}-v3"
    assert shard_mod.vector_index_member(SPACE) == f"index-{SPACE}"
    assert shard_mod.bundle_member(SPACE, "sha256:" + "ab" * 32) == \
        f"bundle-{SPACE}-" + "ab" * 32
    # 09 §9.1: a `<set>` in a path is the id's first 16 hex digits, and
    # §9.12's worked example is `calibration/6b1a3f0e9c2d4b7f.safetensors`.
    assert shard_mod.calibration_member("sha256:" + "6b" * 32) == \
        "calibration-" + "6b" * 8
    assert shard_mod.seals_member("2026-08") == "seals-2026-08"
    assert shard_mod.origin_member("year=1980") == "origin-year=1980"


def test_the_kinds_are_closed_and_are_what_kinds_takes():
    """02 §2.6: `--kinds` (07 §7.3) takes their names."""
    assert shard_mod.MEMBER_KINDS == ("records", "vectors", "buckets", "index",
                                 "relations", "bundle", "centres",
                                 "calibration", "terms", "seals", "origin",
                                 "figures")


def test_a_slice_may_not_be_shaped_like_a_space():
    """A slice segment of sixteen lowercase hex reads as a `<space>` in every
    member name; refused (09 §9.16's old collision, kept closed)."""
    with pytest.raises(ShardError, match="collide with a vector index"):
        shard_mod.check_slice(SPACE)
    with pytest.raises(ShardError, match="collide with a vector index"):
        shard_mod.check_slice(f"lang=de/{SPACE}")
    shard_mod.check_slice("a1b2c3d4e5f6071")        # fifteen: fine
    shard_mod.check_slice("a1b2c3d4e5f607189")      # seventeen: fine
    shard_mod.check_slice("year=1975-1979")


def test_a_slice_name_is_path_segments():
    shard_mod.check_slice("all")
    shard_mod.check_slice("lang=de/pages=3M")
    shard_mod.check_slice("=odd-but-legal")     # 09 §9.3 admits a leading "="
    for bad in ("/leading", "trailing/", "two//slashes", ".hidden", "has space"):
        with pytest.raises(ShardError):
            shard_mod.check_slice(bad)


def test_a_space_segment_is_sixteen_lowercase_hex():
    shard_mod.check_space(SPACE)
    for bad in (SPACE.upper(), "03ae64488c89930", "03ae64488c8993055", "zzzz"):
        with pytest.raises(ShardError, match="not a <space> segment"):
            shard_mod.check_space(bad)


# ---- paths ----------------------------------------------------------------

def test_shard_paths_are_five_digits():
    assert shard_mod.shard_path("year=1975-1979", 3) == \
        "records/year=1975-1979/00003.jsonl.zst"
    assert shard_mod.relations_path("year=1975-1979", 3) == \
        "relations/year=1975-1979/00003.jsonl.zst"


def test_a_superseded_shard_keeps_its_old_bytes_beside_the_new():
    """02 §2.2: every snapshot in the chain stays servable and checkable."""
    assert shard_mod.shard_path("year=1980", 1, superseded_by="2026-08") == \
        "records/year=1980/00001.2026-08.jsonl.zst"
    assert shard_mod.shard_path("year=1980", 1) == "records/year=1980/00001.jsonl.zst"


def test_the_whole_collection_paths():
    assert shard_mod.CODEX_PATH == "codex.json"
    assert shard_mod.TERMS_PATH == "LICENSE-TERMS"
    assert shard_mod.VALIDATION_PATH == "validation.json"
    assert shard_mod.snapshot_path("2026-08") == "snapshots/2026-08.json"
    assert shard_mod.snapshot_sig_path("2026-W36") == "snapshots/2026-W36.sig.json"
    assert shard_mod.seals_path("2026-08") == "seals/2026-08.json"
    assert shard_mod.centres_path(SPACE, 2) == f"vectors/{SPACE}/centres/v2.safetensors"
    assert shard_mod.vector_index_path(SPACE) == f"vectors/{SPACE}/index.json"
    assert shard_mod.calibration_paths("sha256:" + "6b" * 32) == (
        "calibration/" + "6b" * 8 + ".safetensors",
        "calibration/" + "6b" * 8 + ".sig.json")


def test_scaffold_makes_the_tree_of_2_6(tmp_path: Path):
    d = shard_mod.scaffold(shard_mod.shard_dir(tmp_path / "pubmed"))
    assert d == tmp_path / "pubmed" / "shard"
    for name in shard_mod.TREE:
        assert (d / name).is_dir()


# ---- the acknowledgement gate ---------------------------------------------

def test_a_green_collection_gates_nothing(codex):
    green = replace(codex, license=License("green", None))
    assert shard_mod.fetchable_before_ack(green) == ()
    assert not shard_mod.gated_before_ack(green, "records/year=1980/00001.jsonl.zst")


def test_the_pre_ack_set_is_three_and_excludes_validation(codex):
    """02 §2.6: `validation.json` is the Codex's own block written out, so a
    peer holding codex.json already has its bytes."""
    assert shard_mod.fetchable_before_ack(codex) == (
        "codex.json", "snapshots/*", "LICENSE-TERMS")
    for open_ in ("codex.json", "LICENSE-TERMS", "snapshots/2026-08.json",
                  "snapshots/2026-08.sig.json"):
        assert not shard_mod.gated_before_ack(codex, open_)
    for shut in ("validation.json", "records/year=1980/00001.jsonl.zst",
                 f"vectors/{SPACE}/index.json", "seals/2026-08.json",
                 "calibration/6b.safetensors"):
        assert shard_mod.gated_before_ack(codex, shut), shut


# ---- roots ----------------------------------------------------------------

def test_a_file_of_one_block_has_its_single_leaf_as_root():
    data = b"x" * 100
    assert shard_mod.pieces_root(data) == "sha256:" + hashlib.sha256(data).hexdigest()
    exactly = b"y" * shard_mod.BLOCK
    assert shard_mod.pieces_root(exactly) == "sha256:" + hashlib.sha256(exactly).hexdigest()


def test_an_empty_file_hashes_its_empty_leaf():
    assert shard_mod.pieces_root(b"") == "sha256:" + hashlib.sha256(b"").hexdigest()


def test_two_blocks_pair_without_padding():
    data = b"a" * shard_mod.BLOCK + b"b" * 10
    left = hashlib.sha256(data[:shard_mod.BLOCK]).digest()
    right = hashlib.sha256(data[shard_mod.BLOCK:]).digest()
    assert shard_mod.pieces_root(data) == \
        "sha256:" + hashlib.sha256(left + right).hexdigest()


def test_three_blocks_pad_to_four_with_thirty_two_zero_bytes():
    """09 §9.5: the leaf count is padded to the next power of two with
    leaves of 32 zero bytes."""
    data = b"a" * shard_mod.BLOCK + b"b" * shard_mod.BLOCK + b"c" * 5
    leaves = [hashlib.sha256(data[i:i + shard_mod.BLOCK]).digest()
              for i in range(0, len(data), shard_mod.BLOCK)] + [bytes(32)]
    l01 = hashlib.sha256(leaves[0] + leaves[1]).digest()
    l23 = hashlib.sha256(leaves[2] + leaves[3]).digest()
    assert shard_mod.pieces_root(data) == \
        "sha256:" + hashlib.sha256(l01 + l23).hexdigest()


def test_the_last_block_is_as_long_as_it_is():
    """Not zero-padded to 16 KiB — a short tail hashes its own bytes."""
    short = b"a" * shard_mod.BLOCK + b"b"
    padded = b"a" * shard_mod.BLOCK + b"b" + bytes(shard_mod.BLOCK - 1)
    assert shard_mod.pieces_root(short) != shard_mod.pieces_root(padded)


def test_a_member_of_one_file_is_that_files_root():
    f = MemberFile("records/year=1980/00001.jsonl.zst", "sha256:" + "1a" * 32, 900)
    assert shard_mod.member_root([f]) == f.root


def test_a_member_of_several_hashes_the_list_in_path_byte_order():
    a = MemberFile("index/year=1980/00001/b.seg", "sha256:" + "2b" * 32, 10)
    b = MemberFile("index/year=1980/00001/a.seg", "sha256:" + "3c" * 32, 20)
    expected = "sha256:" + hashlib.sha256(json.dumps(
        [[b.path, b.root, b.bytes], [a.path, a.root, a.bytes]],
        sort_keys=True, separators=(",", ":"),
        ensure_ascii=False).encode("utf-8")).hexdigest()
    assert shard_mod.member_root([a, b]) == expected
    assert shard_mod.member_root([b, a]) == expected, "order of the argument is not the order"


def test_a_member_has_at_least_one_file():
    with pytest.raises(ShardError, match="at least one file"):
        shard_mod.member_root([])


def test_member_files_reads_roots_and_sizes_off_the_disk(tmp_path: Path):
    d = shard_mod.scaffold(shard_mod.shard_dir(tmp_path / "c"))
    (d / "records" / "year=1980").mkdir(parents=True)
    p = shard_mod.shard_path("year=1980", 1)
    (d / p).write_bytes(b"hello")
    files = shard_mod.member_files(d, [p])
    assert files == (MemberFile(p, shard_mod.pieces_root(b"hello"), 5),)
    with pytest.raises(ShardError, match="not in the SHARD"):
        shard_mod.member_files(d, ["records/year=1980/00002.jsonl.zst"])


# ---- torrents -------------------------------------------------------------

def test_the_info_dict_carries_exactly_the_four_keys():
    """09 §9.16: the torrent is a function of the file's path, size and root
    alone, so two honest publishers produce one infohash."""
    f = MemberFile("records/year=1980/00001.jsonl.zst", "sha256:" + "1a" * 32, 900)
    info = shard_mod.info_dict(shard_mod.records_member("year=1980"), f)
    assert set(info) == {"meta version", "name", "piece length", "file tree"}
    assert info["meta version"] == 2
    assert info["name"] == "records-year=1980"
    assert info["piece length"] == 16777216
    leaf = info["file tree"]["records"]["year=1980"]["00001.jsonl.zst"][""]
    assert leaf == {"length": 900, "pieces root": bytes.fromhex("1a" * 32)}


# ---- what is served -------------------------------------------------------

def test_a_file_in_neither_the_snapshot_nor_a_shard_is_not_a_member():
    """09 §9.4: not a member, and not served."""
    files = ["validation.json", "LICENSE-TERMS"]
    shards = ["records/year=1980/00001.jsonl.zst"]
    assert shard_mod.is_member_file("validation.json", files, shards)
    assert shard_mod.is_member_file("records/year=1980/00001.jsonl.zst", files, shards)
    assert not shard_mod.is_member_file("notes.txt", files, shards)
    assert not shard_mod.is_member_file("records/year=1980/00002.jsonl.zst", files, shards)


def test_a_one_block_file_has_the_same_root_as_its_plain_sha256():
    """Worth pinning because it is a real coincidence, not a bug: BEP 52
    makes a file of at most one block its own single leaf, so for a small
    shard the pieces root and a plain sha256 of the file agree. They part
    company at the second block, and the transport compares the root."""
    small = b"x" * (shard_mod.BLOCK - 1)
    assert shard_mod.pieces_root(small) == "sha256:" + hashlib.sha256(small).hexdigest()
    two = b"x" * (shard_mod.BLOCK + 1)
    assert shard_mod.pieces_root(two) != "sha256:" + hashlib.sha256(two).hexdigest()
