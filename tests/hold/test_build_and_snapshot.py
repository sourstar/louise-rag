"""tests/hold/test_build_and_snapshot.py — a corpus on disk becomes a drag.

`hold records build` parses, slices, shards and hashes into the drag;
`hold snapshot create` freezes what it produced as 09 §9.4's snapshot.
This is the first test in the migration that drives a REAL corpus through
the new commands rather than exercising pure functions.
"""
import json
from pathlib import Path

import pytest

from app.hold import collection as col, drag, records as rec, snapshot as sn
from app.hold.codex import CodexError
from app.hold.records import RecordsError
from tests.hold.conftest import PLATFORM
from tests.hold.test_template import ORIGIN, SPACE, VALIDATION, recipe


def _collection(tmp_path: Path, **over):
    """A gutenberg collection under a LOUISE_ROOT-shaped tree."""
    root = tmp_path / "collections"
    col.create_from_recipe(root, "gutenberg", recipe(**over),
                           platform_image=PLATFORM, name="gutenberg")
    return root


def _build(root, corpus_dir, **kw):
    return rec.build(root, "gutenberg", corpus_dir=corpus_dir,
                     log=lambda *_: None, **kw)


# ---- the build -----------------------------------------------------------

def test_a_corpus_on_disk_becomes_record_shards_in_the_drag(
        isolated_config, tmp_path, corpus_dir):
    root = _collection(tmp_path)
    result = _build(root, corpus_dir, records_per_shard=2)

    assert result.records == 3
    shards = sorted((root / "gutenberg" / "drag" / "records").rglob("*.jsonl.zst"))
    assert [p.relative_to(root / "gutenberg" / "drag").as_posix() for p in shards] == [
        "records/lang=en/00000.jsonl.zst", "records/lang=fr/00000.jsonl.zst"]


def test_the_drag_holds_only_records_after_a_build(
        isolated_config, tmp_path, corpus_dir):
    """07 §7.8: the drag holds only what the Codex or a snapshot names. The
    legacy builder also writes a manifest.json, a TERMS.txt and a
    validation.json of its own shape; none of them are drag members under a
    Codex, so the build runs in staging and only records are promoted."""
    root = _collection(tmp_path)
    _build(root, corpus_dir, records_per_shard=2)
    d = root / "gutenberg" / "drag"
    present = sorted(p.relative_to(d).as_posix() for p in d.rglob("*") if p.is_file())
    assert present == ["codex.json",
                       "records/lang=en/00000.jsonl.zst",
                       "records/lang=fr/00000.jsonl.zst"]
    assert not (d / "manifest.json").exists()
    assert (root / "gutenberg" / "state" / "build" / "manifest.json").is_file()


def test_a_build_needs_a_corpus_to_parse(isolated_config, tmp_path):
    root = _collection(tmp_path)
    with pytest.raises(RecordsError, match="no corpus directory"):
        rec.build(root, "gutenberg", log=lambda *_: None)


def test_a_yellow_collection_is_not_built_without_its_acknowledgement(
        isolated_config, tmp_path, corpus_dir):
    """Invariant 9, 04 §4.4: however the data arrived."""
    root = _collection(tmp_path, license={"class": "yellow",
                                          "terms_sha256": "sha256:" + "b9" * 32})
    with pytest.raises(RecordsError, match="no acknowledgement recorded"):
        _build(root, corpus_dir, records_per_shard=2)


# ---- the snapshot --------------------------------------------------------

def test_a_snapshot_freezes_the_build_with_9_4s_shard_entries(
        isolated_config, tmp_path, corpus_dir):
    root = _collection(tmp_path)
    _build(root, corpus_dir, records_per_shard=2)
    rec.write_validation(root, "gutenberg")
    snap = sn.create(root, "gutenberg", snapshot_id="2026-08", log=lambda *_: None)

    assert snap.snapshot_id == "2026-08"
    assert snap.previous is None
    assert snap.records == 3
    assert len(snap.shards) == 2
    for s in snap.shards:
        assert s.root.startswith("sha256:")
        assert s.records_root.startswith("sha256:")
        assert s.chunks_root.startswith("sha256:")
        assert s.tokens == s.chunks * 512, "tokens is chunks x space.ctx"
        assert s.bytes > 0
        assert s.toolchain == "own"
        assert s.first_id and s.last_id

    written = root / "gutenberg" / "drag" / "snapshots" / "2026-08.json"
    assert written.read_bytes() == snap.canonical()
    assert json.loads(written.read_bytes())["kind"] == "louise-snapshot"


def test_the_snapshots_roots_are_the_ones_the_build_computed(
        isolated_config, tmp_path, corpus_dir):
    """Two of the three cannot be recomputed from the drag alone without
    re-chunking, which is why the build computes them."""
    from app.pack import records as pack_records
    root = _collection(tmp_path)
    _build(root, corpus_dir, records_per_shard=2)
    snap = sn.create(root, "gutenberg", snapshot_id="2026-08", log=lambda *_: None)
    d = root / "gutenberg" / "drag"
    for s in snap.shards:
        path = d / s.path
        assert s.root == drag.pieces_root_of(path)
        assert s.bytes == path.stat().st_size
        hashes = [r["content_hash"] for r in pack_records.iter_records(path)]
        assert s.records_root == rec.records_root(hashes)
        assert s.records == len(hashes)


def test_the_first_snapshot_marks_every_record_added(
        isolated_config, tmp_path, corpus_dir):
    root = _collection(tmp_path)
    _build(root, corpus_dir, records_per_shard=2)
    snap = sn.create(root, "gutenberg", snapshot_id="2026-08", log=lambda *_: None)
    assert len(snap.delta.added) == 3
    assert snap.delta.changed == () and snap.delta.tombstoned == ()


def test_files_lists_validation_and_nulls_a_green_collections_terms(
        isolated_config, tmp_path, corpus_dir):
    """09 §9.4's `files`; 02 §2.6: green collections have no LICENSE-TERMS."""
    root = _collection(tmp_path)
    _build(root, corpus_dir, records_per_shard=2)
    rec.write_validation(root, "gutenberg")
    snap = sn.create(root, "gutenberg", snapshot_id="2026-08", log=lambda *_: None)
    assert snap.files["validation.json"]["bytes"] > 0
    assert snap.files["validation.json"]["root"].startswith("sha256:")
    assert snap.files["LICENSE-TERMS"] is None


def test_validation_json_is_the_codexs_own_block(isolated_config, tmp_path):
    from app.hold.codex import canonical_json
    root = _collection(tmp_path)
    rec.write_validation(root, "gutenberg")
    raw = (root / "gutenberg" / "drag" / "validation.json").read_bytes()
    assert raw == canonical_json(col.read(root, "gutenberg").validation.to_dict())
    assert json.loads(raw)["checks"] == VALIDATION["checks"]


def test_re_freezing_the_same_id_is_nothing_to_do(
        isolated_config, tmp_path, corpus_dir):
    root = _collection(tmp_path)
    _build(root, corpus_dir, records_per_shard=2)
    a = sn.create(root, "gutenberg", snapshot_id="2026-08", log=lambda *_: None)
    b = sn.create(root, "gutenberg", snapshot_id="2026-08", log=lambda *_: None)
    assert a.hash == b.hash


def test_a_different_snapshot_under_one_id_is_refused(
        isolated_config, tmp_path, corpus_dir):
    """04 §4.3's dispute is between peers; it is not something to create
    locally by overwriting."""
    root = _collection(tmp_path)
    _build(root, corpus_dir, records_per_shard=2)
    sn.create(root, "gutenberg", snapshot_id="2026-08", log=lambda *_: None)
    p = root / "gutenberg" / "drag" / "snapshots" / "2026-08.json"
    d = json.loads(p.read_bytes())
    d["shards"][0]["records"] = 99
    p.write_bytes(json.dumps(d, sort_keys=True, separators=(",", ":")).encode())
    with pytest.raises(CodexError, match="different bytes"):
        sn.create(root, "gutenberg", snapshot_id="2026-08", log=lambda *_: None)


def test_a_snapshot_needs_a_build(isolated_config, tmp_path):
    root = _collection(tmp_path)
    with pytest.raises(CodexError, match="no build to freeze"):
        sn.create(root, "gutenberg", snapshot_id="2026-08", log=lambda *_: None)


def test_a_snapshot_round_trips_through_its_own_bytes(
        isolated_config, tmp_path, corpus_dir):
    root = _collection(tmp_path)
    _build(root, corpus_dir, records_per_shard=2)
    snap = sn.create(root, "gutenberg", snapshot_id="2026-08", log=lambda *_: None)
    again = sn.load(root / "gutenberg" / "drag" / "snapshots" / "2026-08.json")
    assert again == snap
    assert again.hash == snap.hash


def test_the_snapshot_id_defaults_to_the_period_that_closed(
        isolated_config, tmp_path, corpus_dir):
    """From the Codex's own clock and cadence — no argument needed."""
    root = _collection(tmp_path)
    _build(root, corpus_dir, records_per_shard=2)
    snap = sn.create(root, "gutenberg", log=lambda *_: None)
    assert len(snap.snapshot_id) == 7 and snap.snapshot_id[4] == "-"
