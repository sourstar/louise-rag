"""tests/hold/test_annotations_update.py — an annotations collection is
UPDATED, not replaced (operator, 2026-10-23; docs/notes/annotation-collections.md
"Updating"): the next snapshot of the same collection, its claims re-checked
against the target's newer snapshot, a claim's uid kept across updates, a
claim left out tombstoned, the re-issued parts' old versions kept under their
superseded names (09 §9.5) and the claims file it replaced beside the new."""
import json
from datetime import datetime, timezone

import pytest

from app.hold import annotations as ann, check, codex as cx, collection as col, shard as shard_mod, \
    records as rec, snapshot as sn
from app.pack import records as pack_records
from tests.hold.conftest import KEY, NOW
from tests.hold.test_annotations import TARGET_SNAPSHOT, a_claim, a_target, claims_file, make

NAME = "pubmed-same-work"
OCTOBER = datetime(2026, 10, 23, 12, tzinfo=timezone.utc)


def target_next(root, target, texts: dict[str, str], *, sid="2026-09"):
    """The target's next snapshot: `texts` uid -> new abstract, None for a
    record the origin withdrew; its part re-issued, the old one kept."""
    d = col.shard_dir_of(root, "pubmed")
    prev = sn.load(d / shard_mod.snapshot_path(TARGET_SNAPSHOT))
    part = d / prev.shards[0].path
    records = []
    for r in pack_records.iter_records(part):
        t = texts.get(r["uid"], r["sections"][0][1])
        if t is None:
            continue
        sections = [["Abstract", t]]
        records.append(dict(r, sections=sections, content_hash=rec.content_hash(r["title"], [tuple(s) for s in sections])))
    staged = d / "staged.jsonl.zst"
    pack_records.write_shard(staged, records)
    raw = staged.read_bytes()
    shard_mod.promote(staged, part, shard_dir=d, new_root=shard_mod.pieces_root(raw))
    shard = sn.Shard(slice="year=2023", n=1, root=shard_mod.pieces_root(raw),
                     records_root=rec.records_root(r["content_hash"] for r in records),
                     first_id=records[0]["native_id"], last_id=records[-1]["native_id"], records=len(records),
                     chunks=len(records), chunks_root="sha256:" + "22" * 32, tokens=512 * len(records), bytes=len(raw))
    before = {r["uid"]: r["content_hash"] for r in pack_records.iter_records(
        shard_mod.superseded_name(part, TARGET_SNAPSHOT))}
    snap = sn.Snapshot(collection_id=target.collection_id, snapshot_id=sid, previous=prev.hash, shards=(shard,),
                       delta=sn.compute_delta(before, {r["uid"]: r["content_hash"] for r in records}), files={})
    (d / shard_mod.snapshot_path(sid)).write_bytes(snap.canonical())
    return {r["uid"]: r["content_hash"] for r in records}


def update(root, **kw):
    kw.setdefault("clock", OCTOBER)
    return ann.update(root, NAME, key=KEY, now=NOW, log=lambda m: None, **kw)


def claims_by_uid(root, snap):
    d = col.shard_dir_of(root, NAME)
    return {r["uid"]: ann.claim_of(r) for s in snap.shards for r in pack_records.iter_records(d / s.path)}


def test_a_new_claims_file_is_the_next_snapshot_of_the_same_collection(tmp_path):
    a_target(tmp_path)
    codex, first = make(tmp_path)                       # annotations:1 = (1,2), annotations:2 = (3,1)
    d = col.shard_dir_of(tmp_path, NAME)
    first_part = (d / first.shards[0].path).read_bytes()
    # (3,1) left out, (2,4) new, (1,2) kept
    got = update(tmp_path, claims=claims_file(a_claim("pubmed:2", "pubmed:4"), a_claim()))
    snap = got.snapshot
    assert col.read(tmp_path, NAME) == codex, "the same Codex: the same collection, not a new one"
    assert (snap.collection_id, snap.snapshot_id, snap.previous) == (codex.collection_id, "2026-10", first.hash)
    sn.verify_chain([first, snap])
    claims = claims_by_uid(tmp_path, snap)
    assert {u: (c["subject"], c["object"]) for u, c in claims.items()} == {
        "annotations:1": ("pubmed:1", "pubmed:2"), "annotations:3": ("pubmed:2", "pubmed:4")}, \
        "a claim keeps its uid; a new one takes the next number; the one left out is gone"
    assert (snap.delta.added, snap.delta.changed) == (("annotations:3",), ())
    assert [(t.uid, t.reason) for t in snap.delta.tombstoned] == [("annotations:2", "withdrawn")]
    assert (got.added, got.changed, got.tombstoned) == (1, 0, 1)
    # the previous snapshot stays servable: its part and its claims file, kept by name
    assert (d / shard_mod.superseded_name(d / first.shards[0].path, "2026-09")).read_bytes() == first_part
    origin = tmp_path / NAME / "origin"
    assert b"pubmed:3" in (origin / "claims.2026-09.jsonl").read_bytes()
    assert b"pubmed:3" not in (origin / "claims.jsonl").read_bytes()
    assert snap.shards[0].path in got.reissued
    # the plan passes on it, signed by this peer
    report = check.run(tmp_path, NAME, codex, snap, seed=b"s" * 32, checks=list(cx.ANNOTATIONS_CHECKS))
    assert {o.check: o.result for o in report.outcomes} == {
        "roots": "pass", "records": "pass", "license": "pass", "signature": "pass"}
    # and the search reads the new claims
    idx = ann.index_of(d)
    assert idx.snapshot_id == "2026-10" and "pubmed:3" not in idx.by_target and "pubmed:4" in idx.by_target


def test_without_a_file_the_claims_are_re_checked_against_the_targets_newer_text(tmp_path):
    target, _ = a_target(tmp_path)
    make(tmp_path)
    now_hashes = target_next(tmp_path, target, {"pubmed:2": "two, revised", "pubmed:3": None})
    dry = update(tmp_path, dry_run=True)
    assert dry.snapshot is None and not (col.shard_dir_of(tmp_path, NAME) / "snapshots/2026-10.json").exists()
    got = update(tmp_path)
    assert got.against == {"pubmed": "2026-09"}, "the newest snapshot the target holds"
    claims = claims_by_uid(tmp_path, got.snapshot)
    assert list(claims) == ["annotations:1"]
    c = claims["annotations:1"]
    assert (c["object_hash"], c["target_snapshot"]) == (now_hashes["pubmed:2"], "2026-09"), \
        "re-pinned to the text the target holds now"
    assert got.changed == 1 and got.tombstoned == 1
    assert got.dropped == ("line 2: pubmed:3",), "the claim about a withdrawn record is named"
    assert (dry.added, dry.changed, dry.tombstoned) == (got.added, got.changed, got.tombstoned)
    # the claims file is the annotator's: a re-check keeps it as written
    assert not (tmp_path / NAME / "origin" / "claims.2026-09.jsonl").exists()


def test_nothing_changed_writes_nothing_and_one_update_a_period(tmp_path):
    a_target(tmp_path)
    make(tmp_path)
    got = update(tmp_path)
    assert got.snapshot is None and (got.added, got.changed, got.tombstoned) == (0, 0, 0)
    assert not (col.shard_dir_of(tmp_path, NAME) / "snapshots/2026-10.json").exists()
    update(tmp_path, claims=claims_file(a_claim()))
    with pytest.raises(ann.AnnotationsError, match="once its period opens"):
        update(tmp_path, claims=claims_file(a_claim(), a_claim("pubmed:2", "pubmed:4")))
    assert update(tmp_path, claims=claims_file(a_claim(), a_claim("pubmed:2", "pubmed:4")),
                  clock=datetime(2026, 11, 2, tzinfo=timezone.utc)).snapshot.snapshot_id == "2026-11"


def test_what_create_refuses_an_update_refuses(tmp_path):
    a_target(tmp_path)
    make(tmp_path)
    with pytest.raises(ann.AnnotationsError, match="does not hold"):
        update(tmp_path, claims=claims_file(a_claim("pubmed:1", "pubmed:9")))
    with pytest.raises(ann.AnnotationsError, match="before 2026-08"):
        update(tmp_path, against=[("pubmed", "2026-07")])
    with pytest.raises(ann.AnnotationsError, match="does not annotate"):
        update(tmp_path, against=[("gutenberg", "2026-08")])
    with pytest.raises(ann.AnnotationsError, match="not an annotations collection"):
        ann.update(tmp_path, "pubmed")
    assert not (col.shard_dir_of(tmp_path, NAME) / "snapshots/2026-10.json").exists(), "nothing written"


def test_an_interrupted_update_is_resumed_to_the_same_bytes(tmp_path, monkeypatch):
    a_target(tmp_path)
    make(tmp_path)
    data = claims_file(a_claim(), a_claim("pubmed:2", "pubmed:4"))
    real = sn.Snapshot.canonical
    calls = []

    def boom(self):
        calls.append(1)
        if len(calls) == 1:
            raise KeyboardInterrupt("killed before the snapshot file")
        return real(self)
    monkeypatch.setattr(sn.Snapshot, "canonical", boom)
    with pytest.raises(KeyboardInterrupt):
        update(tmp_path, claims=data)
    monkeypatch.setattr(sn.Snapshot, "canonical", real)
    got = update(tmp_path, claims=data)
    assert got.snapshot is not None and got.added == 1
    d = col.shard_dir_of(tmp_path, NAME)
    first = sn.load(d / "snapshots/2026-09.json")
    assert shard_mod.pieces_root_of(d / shard_mod.superseded_name(d / first.shards[0].path, "2026-09")) \
        == first.shards[0].root, "the first snapshot's part is still its own bytes"


def test_the_command_updates_it_and_says_what_changed(tmp_path, monkeypatch, capsys):
    from app import louise
    a_target(tmp_path)
    make(tmp_path)
    f = tmp_path / "new.jsonl"
    f.write_bytes(claims_file(a_claim(), a_claim("pubmed:2", "pubmed:4")))
    monkeypatch.setattr(ann, "datetime", type("D", (), {"now": staticmethod(lambda tz=None: OCTOBER)}))
    base = ["hold", "annotations", "update", NAME, "--root", str(tmp_path), "--claims", str(f)]
    assert louise.main(base + ["--dry-run"]) == 0
    dry = json.loads(capsys.readouterr().out)
    assert dry["written"] is False and dry["added"] == 1 and dry["tombstoned"] == 1
    assert louise.main(base) == 0
    got = json.loads(capsys.readouterr().out)
    assert got["written"] and got["snapshot_id"] == "2026-10" and got["against"] == {"pubmed": TARGET_SNAPSHOT}
    assert louise.main(["hold", "annotations", "update", NAME, "--root", str(tmp_path), "--against", "pubmed"]) != 0
    assert "TARGET=SNAPSHOT" in capsys.readouterr().err


def test_the_claims_are_re_checked_against_the_targets_records_twin_where_it_serves(tmp_path):
    """gutenberg's A1f twin (2026-10-20): a same-records carry of the target
    under a new Codex is what the search reads; an update checks against it."""
    from app.hold import carry
    target, _ = a_target(tmp_path)
    make(tmp_path)
    twin, _ = a_target(tmp_path, name="pubmed-twin", texts=("one", "two, as the twin holds it", "three", "four"),
                       cleaning_version=2)
    assert twin.collection_id != target.collection_id
    d = col.shard_dir_of(tmp_path, "pubmed-twin")
    s = sn.load(d / shard_mod.snapshot_path(TARGET_SNAPSHOT))
    nxt = sn.Snapshot(collection_id=twin.collection_id, snapshot_id="2026-09", previous=s.hash, shards=s.shards,
                      delta=sn.Delta((), (), ()), files={})
    (d / shard_mod.snapshot_path("2026-09")).write_bytes(nxt.canonical())
    (tmp_path / "pubmed-twin" / "state").mkdir(parents=True, exist_ok=True)
    (tmp_path / "pubmed-twin" / "state" / carry.MARKER).write_text(
        json.dumps({"done": True, "from": target.collection_id}))
    got = update(tmp_path)
    assert got.against == {"pubmed-twin": "2026-09"}
    c = claims_by_uid(tmp_path, got.snapshot)["annotations:1"]
    assert c["target_snapshot"] == "2026-09" and got.changed == 2
