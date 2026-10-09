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
from tests.hold.conftest import fake_query_embed  # noqa: E402,F811
from tests.hold.conftest import KEY, NOW, PLATFORM, fake_embed
from tests.hold.test_template import ORIGIN, SPACE, VALIDATION, recipe


def _collection(tmp_path: Path, **over):
    """A gutenberg collection under a LOUISE_ROOT-shaped tree."""
    root = tmp_path / "collections"
    col.create_from_recipe(root, "gutenberg", recipe(**over),
                           platform_image=PLATFORM, embed=fake_embed, embed_query=fake_query_embed,
                           name="gutenberg", key=KEY, now=NOW)
    return root


def _build(root, corpus_dir, **kw):
    out = rec.build(root, "gutenberg", corpus_dir=corpus_dir,
                    log=lambda *_: None, **kw)
    # What the operator's next command does (`hold records validation`, 07
    # §7.3). A snapshot cannot be frozen without it: 09 §9.4 lists
    # validation.json in `files` with no "or null", and a drag holding a
    # file no snapshot names is refused by `share` (07 §7.8).
    rec.write_validation(root, "gutenberg")
    return out


# ---- the build -----------------------------------------------------------

def test_a_corpus_on_disk_becomes_record_shards_in_the_drag(
        isolated_config, tmp_path, corpus_dir):
    root = _collection(tmp_path)
    result = _build(root, corpus_dir, records_per_shard=2)

    assert result.records == 3
    shards = sorted((root / "gutenberg" / "drag" / "records").rglob("*.jsonl.zst"))
    assert [p.relative_to(root / "gutenberg" / "drag").as_posix() for p in shards] == [
        "records/lang=en/00001.jsonl.zst", "records/lang=fr/00001.jsonl.zst"]


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
    # The calibration member is written by `create`, before any build, and
    # is a member of every snapshot (09 §9.4, §9.12) — so it belongs here.
    cal_member, = (d / "calibration").glob("*.safetensors")
    stem = cal_member.name.removesuffix(".safetensors")
    assert present == [f"calibration/{stem}.safetensors",
                       f"calibration/{stem}.sig.json",
                       "codex.json",
                       "records/lang=en/00001.jsonl.zst",
                       "records/lang=fr/00001.jsonl.zst",
                       "validation.json"]
    assert not (d / "manifest.json").exists()
    assert (root / "gutenberg" / "state" / "build" / "manifest.json").is_file()


def test_a_build_needs_a_corpus_to_parse(isolated_config, tmp_path):
    root = _collection(tmp_path)
    with pytest.raises(RecordsError, match="no corpus directory"):
        rec.build(root, "gutenberg", log=lambda *_: None)


def test_a_yellow_collection_is_not_built_without_its_acknowledgement(
        isolated_config, tmp_path, corpus_dir):
    """Invariant 9, 04 §4.4: however the data arrived.

    The collection TIGHTENS gutenberg's green to yellow, which 04 §4.4
    allows — and a tightened collection's plugin ships no terms text, so
    the recipe supplies the one it is tightening to. Without that,
    `create` refuses: a yellow collection whose terms text exists nowhere
    is one no peer could ever acknowledge (09 §9.13).
    """
    import hashlib
    body = b"Terms this collection tightens to.\n"
    p = tmp_path / "TERMS.txt"
    p.write_bytes(body)
    root = _collection(tmp_path, license={
        "class": "yellow",
        "terms_sha256": "sha256:" + hashlib.sha256(body).hexdigest(),
        "terms_path": str(p)})
    assert (root / "gutenberg" / "drag" / "LICENSE-TERMS").read_bytes() == body
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


def test_a_snapshot_names_the_calibration_member_and_its_signature(
        isolated_config, tmp_path, corpus_dir):
    """09 §9.4's `files` lists five entries and gives "or null" only to
    LICENSE-TERMS and to seals.

    This is the test that was missing. `create` began writing
    `calibration/<set>.safetensors` and `_files` still named only
    validation.json and LICENSE-TERMS, so the member was in the drag and in
    no snapshot — and 07 §7.8 does not merely skip such a file, it makes
    `share` refuse the WHOLE DRAG: "share refuses a drag directory holding a
    file that neither the Codex names, nor a snapshot of the chain lists".
    Nothing failed, because nothing looked.
    """
    from app.hold import drag as drag_mod
    root = _collection(tmp_path)
    _build(root, corpus_dir, records_per_shard=2)
    snap = sn.create(root, "gutenberg", snapshot_id="2026-08", log=lambda *_: None)

    codex = col.read(root, "gutenberg")
    member, sig = drag_mod.calibration_paths(codex.space.calibration.set)
    for name in (member, sig):
        assert snap.files.get(name), f"{name} is listed with a root and bytes"
        assert set(snap.files[name]) == {"root", "bytes"}

    d = root / "gutenberg" / "drag"
    assert snap.files[member]["bytes"] == (d / member).stat().st_size
    assert snap.files[member]["root"] == drag_mod.pieces_root((d / member).read_bytes())


def test_every_drag_file_is_named_by_the_codex_or_a_snapshot(
        isolated_config, tmp_path, corpus_dir):
    """07 §7.8's rule, asserted over the whole tree rather than over a list
    someone remembered to update: every file in the drag is `codex.json`, a
    shard a snapshot names, or an entry in its `files`. A member that is in
    neither makes the drag unshareable, so this is the check that catches
    the next one added without a `files` entry."""
    root = _collection(tmp_path)
    _build(root, corpus_dir, records_per_shard=2)
    snap = sn.create(root, "gutenberg", snapshot_id="2026-08", log=lambda *_: None)

    d = root / "gutenberg" / "drag"
    named = {"codex.json"} | set(snap.files) | {s.path for s in snap.shards} \
        | {r.path for r in snap.relations} \
        | {f"snapshots/{snap.snapshot_id}.json",
           f"snapshots/{snap.snapshot_id}.sig.json"}
    on_disk = {p.relative_to(d).as_posix() for p in d.rglob("*") if p.is_file()}
    assert on_disk - named == set(), \
        "a drag file that no snapshot names makes `share` refuse the drag"


def test_shards_are_numbered_from_one(isolated_config, tmp_path, corpus_dir):
    """09 §9.4: "the shard number within the slice, FROM 1"; 02 §2.2:
    "shards are numbered 00001… per slice in the order they are created".

    The builder numbered from 0 until 2026-09-08. It is not a filename
    detail: the shard path is in the snapshot's shard entry, in the member
    name (09 §9.16) and in a bundle header's `shard` field (§9.7), so two
    peers that number differently disagree about which shard a bundle
    covers — and `space_id` would not catch it, because numbering is not in
    it. Fixed before any Codex existed, which was the last moment it cost
    nothing.
    """
    root = _collection(tmp_path)
    _build(root, corpus_dir, records_per_shard=2)
    snap = sn.create(root, "gutenberg", snapshot_id="2026-08", log=lambda *_: None)

    assert {s.n for s in snap.shards} == {1}, "one shard per slice, numbered 1"
    for s in snap.shards:
        assert s.path.endswith("/00001.jsonl.zst")
        assert (root / "gutenberg" / "drag" / s.path).is_file()
    assert not list((root / "gutenberg" / "drag" / "records").rglob("00000.*"))


def test_the_build_stores_each_records_content_hash(
        isolated_config, tmp_path, corpus_dir):
    """09 §9.5's `content_hash`, on the manifest, written as each shard
    closes.

    Why stored rather than derived: `possession.json` (09 §9.7) binds a
    bundle's uids to this value, so verifying an arriving bundle is one
    indexed lookup over its uids. Deriving it instead means re-reading and
    re-hashing shards on every import — a per-bundle cost that scales with
    the CORPUS, which is the rule this project keeps paying for.

    NOT `txt_hash`: that is the ORIGIN bytes' hash, which §9.5 calls
    `origin_hash`. Both are 32 hex characters, so a peer that sent one
    where the other was meant would refuse every record in silence.
    """
    import sqlite3
    from app.pack import records as pack_records

    root = _collection(tmp_path)
    _build(root, corpus_dir, records_per_shard=2)

    d = root / "gutenberg" / "drag"
    on_disk = {}
    for shard in sorted(d.rglob("*.jsonl.zst")):
        for rec in pack_records.iter_records(shard):
            on_disk[rec["uid"]] = rec["content_hash"]
    assert on_disk, "the fixture must produce records"

    # The collection's OWN manifest, the one `hold records build`
    # writes (app/hold/records.py builds a Collection whose
    # manifest_path is under the collection's state directory) — not
    # whatever the global config would resolve.
    con = sqlite3.connect(root / "gutenberg" / "state" / "manifest.sqlite")
    try:
        rows = dict(con.execute(
            "SELECT uid, content_hash FROM documents WHERE content_hash IS NOT NULL"))
    finally:
        con.close()

    assert rows == on_disk, "every record's hash is on the manifest, and matches"
    for uid, h in rows.items():
        assert len(h) == 32 and all(c in "0123456789abcdef" for c in h), \
            f"{uid}: a content_hash is BLAKE2b-128 as 32 lowercase hex (09 §9.5)"


# ---- a stopped build, a foreign build, and the image that may build -------

def test_a_stopped_build_promotes_nothing_and_the_next_run_finishes_it(
        isolated_config, tmp_path, corpus_dir):
    """Invariant 2. A build stopped before its end promoted what it had
    cut, putting part of a record set into the drag (found 2026-09-21)."""
    import threading
    root = _collection(tmp_path)
    stop = threading.Event()
    stop.set()
    first = rec.build(root, "gutenberg", corpus_dir=corpus_dir,
                      log=lambda *_: None, records_per_shard=2, stop=stop)
    assert first.interrupted
    assert not list((root / "gutenberg" / "drag").rglob("*.jsonl.zst"))
    second = _build(root, corpus_dir, records_per_shard=2)
    assert not second.interrupted and second.records == 3
    assert len(list((root / "gutenberg" / "drag" / "records").rglob("*.jsonl.zst"))) == 2


def test_a_build_of_another_codex_or_parser_is_never_resumed(
        isolated_config, tmp_path, corpus_dir):
    """`pack build` resumes whatever its database holds. On 2026-09-21 a
    parser-4 build sat in pubmed's staging under a parser-8 Codex."""
    root = _collection(tmp_path)
    _build(root, corpus_dir, records_per_shard=2)
    claim = root / "gutenberg" / "state" / "build" / rec.STAGING_CLAIM
    held = json.loads(claim.read_text(encoding="utf-8"))
    claim.write_text(json.dumps({**held, "parser": {"id": "gutenberg", "version": 1}}),
                     encoding="utf-8")
    with pytest.raises(RecordsError, match="Move it aside"):
        rec.build(root, "gutenberg", corpus_dir=corpus_dir, log=lambda *_: None)
    claim.unlink()
    with pytest.raises(RecordsError, match="no record of which Codex"):
        rec.build(root, "gutenberg", corpus_dir=corpus_dir, log=lambda *_: None)


def test_the_commands_build_and_freeze_only_under_the_reference_image(
        isolated_config, tmp_path, corpus_dir, monkeypatch, capsys):
    """03 §3.4, through the command line, where the peer's image is known."""
    from app import louise
    from app.hold import equivalence
    root = _collection(tmp_path)
    args = ["hold", "records", "build", "gutenberg", "--root", str(root),
            "--corpus", str(corpus_dir), "--shard-records", "2"]
    # an image whose versions differ from the Codex's (one with the Codex's
    # versions is equivalent by invariant 3: 03 §3.4 amended 2026-10-09)
    monkeypatch.setattr(equivalence, "equivalent_by_versions", lambda c: False)
    monkeypatch.setenv("LOUISE_PLATFORM_IMAGE", "sha256:" + "2e" * 32)
    assert louise.main(args) == 2
    assert "03 §3.4" in capsys.readouterr().err
    assert not (root / "gutenberg" / "state" / "build").exists()
    assert louise.main(["hold", "snapshot", "create", "gutenberg", "--root", str(root)]) == 2

    monkeypatch.setenv("LOUISE_PLATFORM_IMAGE", PLATFORM)
    assert louise.main(args) == 0
    assert len(list((root / "gutenberg" / "drag" / "records").rglob("*.jsonl.zst"))) == 2


def test_the_build_command_exits_130_when_stopped(
        isolated_config, tmp_path, corpus_dir, monkeypatch):
    """07 §7.7: SIGTERM or SIGINT finishes the unit and exits 130."""
    from app import louise
    from app.pack.build import BuildResult
    root = _collection(tmp_path)
    monkeypatch.setenv("LOUISE_PLATFORM_IMAGE", PLATFORM)
    seen = {}

    def stopped(*_a, stop=None, **_k):
        seen["stop"] = stop
        return BuildResult(tmp_path, 0, 1, 2, 2, 0, True, None)
    monkeypatch.setattr(rec, "build", stopped)
    assert louise.main(["hold", "records", "build", "gutenberg", "--root", str(root),
                        "--corpus", str(corpus_dir)]) == 130
    assert seen["stop"] is not None, "the command hands the build a stop to honour"


def test_the_build_says_why_its_staging_manifest_is_unsigned(isolated_config, tmp_path, corpus_dir):
    """`hold records build` asks the pack builder for no signature — its
    manifest is staging, and the snapshot is what is signed. The line said
    "UNSIGNED (no NODE_KEY_PATH)" on a worker that had one (2026-10-07)."""
    root = _collection(tmp_path)
    said = []
    rec.build(root, "gutenberg", corpus_dir=corpus_dir, log=said.append, records_per_shard=2)
    line = next(s for s in said if s.startswith("[pack build] manifest:"))
    assert line.endswith("unsigned (the snapshot carries the signature)") and "NODE_KEY_PATH" not in line
