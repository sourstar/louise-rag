"""tests/hold/test_carry.py — a new collection with the same records takes the
old one's parts (G3), local state (G1) and, by provenance, its vectors (G2).

docs/notes/v5-migration.md; amendment A1 (02 §2.4/§2.7, 03 §3.5). The fixture
is the migration in small: two gutenberg collections from one recipe, the old
at Codex version 4 (its layers carry no `input`) and the new at version 5 —
identical records, different spaces.
"""
import json
import os
import sqlite3
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from app import db, embeddings, spaces
from app.hold import carry, collection as col, records as rec, seed, snapshot as sn
from app.hold.codex import CodexError, Parser
from app.pack import build as pack_build
from tests.hold.conftest import KEY, NOW, PLATFORM, fake_embed, fake_query_embed
from tests.hold.test_template import SPACE, recipe

ARCHIVE = "genesis:medcpt-pubmed"
quiet = lambda *_a, **_k: None                       # noqa: E731


class Stopped(Exception):
    """A stop between two steps of a window."""


def _create(root, name, **kw):
    return col.create_from_recipe(root, "gutenberg", recipe(**kw.pop("over", {})),
                                  platform_image=PLATFORM, embed=fake_embed,
                                  embed_query=fake_query_embed, name=name, key=KEY, now=NOW,
                                  **kw)


def _old(tmp_path, corpus_dir):
    """`old`, built and frozen: version 4, the live collections' shape."""
    root = tmp_path / "collections"
    old = _create(root, "old", space_version=4)
    rec.build(root, "old", corpus_dir=corpus_dir, records_per_shard=2, log=quiet)
    rec.write_validation(root, "old")
    sn.create(root, "old", snapshot_id="2026-08", log=quiet)
    return root, old


@pytest.fixture
def pair(isolated_config, tmp_path, corpus_dir):
    root, old = _old(tmp_path, corpus_dir)
    new = _create(root, "new")
    rec.write_validation(root, "new")
    assert old.version == 4 and new.version == 5
    assert old.space_id != new.space_id and old.collection_id != new.collection_id
    return root, old, new


def _seeded(root):
    out = seed.seed_shards(root, "new", "old", log=quiet)
    snap = sn.create(root, "new", snapshot_id=out["snapshot_id"], log=quiet)
    return out, snap


# ---- G3: the SHARD seeded from identical parts ------------------------------

def test_the_parts_are_the_sources_linked_and_the_snapshot_names_the_same_parts(pair):
    root, old, new = pair
    out, snap = _seeded(root)
    src = sn.load(root / "old" / "shard" / "snapshots" / "2026-08.json")
    assert out["snapshot_id"] == snap.snapshot_id == "2026-08"
    assert [s.to_dict() for s in snap.shards] == [s.to_dict() for s in src.shards]
    assert snap.collection_id == new.collection_id and snap.previous is None
    assert out["parts"] == {"linked": len(src.shards), "copied": 0, "present": 0}
    for s in snap.shards:
        a, b = root / "old" / "shard" / s.path, root / "new" / "shard" / s.path
        assert os.stat(a).st_ino == os.stat(b).st_ino, "a hard link: one inode on tank"
    claim = json.loads((root / "new" / "state" / "build" / rec.STAGING_CLAIM).read_text())
    assert claim["collection_id"] == new.collection_id


def test_a_seeded_snapshot_is_byte_for_byte_a_built_one(pair, tmp_path, corpus_dir):
    """RULE 1's test: indistinguishable from the by-the-book artefact. The
    same Codex built from the origin in another root gives the same file."""
    root, _old_codex, _new = pair
    _out, seeded = _seeded(root)
    other = tmp_path / "by-the-book" / "collections"
    _create(other, "new")
    rec.build(other, "new", corpus_dir=corpus_dir, records_per_shard=2, log=quiet)
    rec.write_validation(other, "new")
    built = sn.create(other, "new", snapshot_id="2026-08", log=quiet)
    assert seeded.canonical() == built.canonical() and seeded.hash == built.hash

    def rows(r):
        con = sqlite3.connect(r / "new" / "state" / "build" / pack_build.BUILD_DIR
                              / pack_build.BUILD_DB)
        try:
            return con.execute("SELECT slice, n, path, records, chunks, bytes, root, "
                               "records_root, chunks_root, first_native_id, last_native_id, "
                               "first_uid, last_uid, last_ord FROM shards ORDER BY slice, n"
                               ).fetchall()
        finally:
            con.close()
    assert rows(root) == rows(other), "the build database a build of it would leave"


def test_seeding_again_is_nothing_to_do(pair):
    root, _old_codex, _new = pair
    first, a = _seeded(root)
    again, b = _seeded(root)
    assert again["parts"]["present"] == sum(first["parts"].values())
    assert again["build_db"] == "present" and a.hash == b.hash


def test_a_part_already_in_the_target_with_other_bytes_is_never_overwritten(pair):
    root, _old_codex, _new = pair
    src = sn.load(root / "old" / "shard" / "snapshots" / "2026-08.json")
    stray = root / "new" / "shard" / src.shards[0].path
    stray.parent.mkdir(parents=True)
    stray.write_bytes(b"not the part")
    with pytest.raises(CodexError, match="never overwritten"):
        seed.seed_shards(root, "new", "old", log=quiet)
    assert stray.read_bytes() == b"not the part"


def test_a_source_part_re_issued_since_its_snapshot_is_refused(pair):
    root, _old_codex, _new = pair
    src = sn.load(root / "old" / "shard" / "snapshots" / "2026-08.json")
    p = root / "old" / "shard" / src.shards[0].path
    p.write_bytes(p.read_bytes() + b"\0")
    with pytest.raises(CodexError, match="no longer holds"):
        seed.seed_shards(root, "new", "old", log=quiet)
    assert not (root / "new" / "shard" / src.shards[0].path).exists()


def test_a_record_deciding_difference_is_refused_before_anything_moves(pair):
    root, old, new = pair
    other = col.create(root, replace(new, parser=Parser("gutenberg", 99)), name="other")
    assert seed.record_differences(old, other) == ["parser"]
    assert seed.record_differences(old, new) == [], "a new space decides no record"
    with pytest.raises(CodexError, match="parser"):
        seed.seed_shards(root, "other", "old", log=quiet)
    assert not (root / "other" / "state").exists()


def test_the_source_and_its_snapshot_split_from_the_reference():
    cid = "sha256:" + "ab" * 32
    assert seed.split_ref("old") == ("old", None)
    assert seed.split_ref("old:2026-08") == ("old", "2026-08")
    assert seed.split_ref(f"{cid}:2026-W36") == (cid, "2026-W36")
    assert seed.split_ref(cid) == (cid, None)


def test_the_command_seeds_then_freezes(pair, monkeypatch):
    from app import louise
    root, _old_codex, new = pair
    monkeypatch.setenv("LOUISE_PLATFORM_IMAGE", PLATFORM)
    assert louise.main(["hold", "snapshot", "create", "new", "--root", str(root),
                        "--shards-from", "old:2026-08"]) == 0
    snap = sn.load(root / "new" / "shard" / "snapshots" / "2026-08.json")
    assert snap.collection_id == new.collection_id and snap.records == 3


def test_a_holder_links_the_parts_it_has_and_keeps_the_snapshot_it_holds(pair, monkeypatch, capsys):
    """Perugia, 2026-10-21: adopting the node's twin, `snapshot create
    --shards-from` refused at the first figures container its gutenberg
    never held (101 GB; its file takes records and vectors). A peer that
    already holds the snapshot (fetched, signed by its creator) is a
    HOLDER: the parts its source has are linked, a part neither holds is
    absent, and nothing is computed or signed; the creator still needs every
    part."""
    from app import louise
    from app.hold import seed as seed_mod
    root, _old_codex, new = pair
    monkeypatch.setenv("LOUISE_PLATFORM_IMAGE", PLATFORM)
    _out, snap = _seeded(root)                      # the creator's run: new holds the snapshot
    raw = (root / "new" / "shard" / "snapshots" / f"{snap.snapshot_id}.json").read_bytes()
    paths = [p for p, _b, _r in seed_mod.parts_of(snap)]
    assert len(paths) >= 2
    gone_everywhere, relinked = paths[0], paths[1]
    for name in ("old", "new"):
        (root / name / "shard" / gone_everywhere).unlink()
    (root / "new" / "shard" / relinked).unlink()
    assert seed_mod.holds_the_snapshot(root, "new", seed_mod.latest_snapshot(root, "old", snap.snapshot_id))
    assert louise.main(["hold", "snapshot", "create", "new", "--root", str(root),
                        "--shards-from", f"old:{snap.snapshot_id}"]) == 0
    err = capsys.readouterr().err
    assert "holds 2026-08 already" in err and "1 not held by old" in err
    assert (root / "new" / "shard" / relinked).is_file() and not (root / "new" / "shard" / gone_everywhere).exists()
    assert (root / "new" / "shard" / "snapshots" / f"{snap.snapshot_id}.json").read_bytes() == raw, "kept as fetched"
    # the creator's rule is unchanged: a target without the snapshot needs every part
    with pytest.raises(CodexError, match="the source SHARD does not hold it"):
        seed_mod._link(root / "old" / "shard" / gone_everywhere, root / "elsewhere" / "x", snap.shards[0].root)


# ---- G1: carry ----------------------------------------------------------------

def _local(root, name):
    return rec._local_collection(root, name, col.read(root, name))


def _vectors(root, old):
    """Three vectors per document in `old`'s table, its space pinned, one
    coverage row of its own."""
    from app.share import coverage as cov
    coll = _local(root, "old")
    con = db.connect(coll)
    try:
        docs = con.execute("SELECT uid, native_id, lang FROM documents ORDER BY rowid").fetchall()
        con.execute("INSERT INTO space(id, space_id, dim, pinned_at) VALUES(1, ?, ?, 0)",
                    (old.space_id, old.space.dim))
        cov.record(con, bundle_id="b-old", shard=cov.shard_id(old, "lang=en/00001"),
                   producer="k", uids=[docs[0]["uid"]], chunks=1, tiers={}, source="local",
                   accepted_at=None)
    finally:
        con.close()
    rng = np.random.default_rng(7)
    rows = [{"id": f"{d['uid']}:{seq}:1", "book_id": int(d["native_id"]), "lang": d["lang"] or "en",
             "seq": seq, "chapter": "", "text": f"t{seq}",
             "vector": rng.standard_normal(old.space.dim).astype("float32")}
            for d in docs for seq in range(3)]
    embeddings.upsert_chunks(rows, collection=coll, dtype=old.space.dtype)
    return [d["uid"] for d in docs], [r["id"] for r in rows]


def test_carry_moves_the_local_state_and_re_pins_the_space(pair):
    root, old, new = pair
    _seeded(root)
    uids, ids = _vectors(root, old)

    plan = carry.carry(root, "old", "new", log=quiet)
    assert plan["apply"] is False and (root / "old" / "state" / "manifest.sqlite").is_file()
    assert not (root / "new" / "state" / carry.MARKER).exists(), "a dry run writes nothing"

    out = carry.carry(root, "old", "new", apply=True, log=quiet)
    assert out["done"] == {"lance": "moved", "index": "moved", "manifest": "moved"}
    assert out["coverage_rows_cleared"] == 1 and out["documents"] == len(uids)
    assert not (root / "old" / "state" / "manifest.sqlite").exists()
    assert not (root / "old" / "lance").exists()

    coll = _local(root, "new")
    assert sorted(embeddings.chunk_ids_of_books([int(u.split(":")[1]) for u in uids],
                                                collection=coll)) == sorted(ids)
    con = db.connect(coll)
    try:
        assert con.execute("SELECT space_id, dim FROM space").fetchone()[:] == \
            (new.space_id, new.space.dim)
        spaces._validate_codex(con, coll)            # the boot check passes: not refused
        assert con.execute("SELECT COUNT(*) FROM coverage").fetchone()[0] == 0
    finally:
        con.close()

    again = carry.carry(root, "old", "new", apply=True, log=quiet)
    assert again["done"] == {"lance": "present", "index": "present", "manifest": "present"}


def test_carry_with_copy_leaves_the_source_whole(pair):
    root, old, new = pair
    _seeded(root)
    _uids, ids = _vectors(root, old)
    out = carry.carry(root, "old", "new", copy=True, apply=True, log=quiet)
    assert out["done"]["manifest"] == out["done"]["lance"] == "copied"
    old_coll, new_coll = _local(root, "old"), _local(root, "new")
    assert embeddings.stats(old_coll)["vectors"] == embeddings.stats(new_coll)["vectors"] == len(ids)
    con = db.connect(old_coll)
    try:
        assert con.execute("SELECT space_id FROM space").fetchone()[0] == old.space_id
        assert con.execute("SELECT COUNT(*) FROM coverage").fetchone()[0] == 1
    finally:
        con.close()


def test_carry_refuses_a_target_whose_parts_are_not_the_sources(pair):
    root, old, _new = pair
    _vectors(root, old)
    with pytest.raises(CodexError, match="no snapshot"):
        carry.carry(root, "old", "new", apply=True, log=quiet)
    assert (root / "old" / "state" / "manifest.sqlite").is_file()


def test_carry_refuses_a_non_empty_target(pair):
    root, old, _new = pair
    _seeded(root)
    _vectors(root, old)
    (root / "new" / "lance").mkdir(parents=True, exist_ok=True)
    (root / "new" / "lance" / "something").write_text("x")
    with pytest.raises(CodexError, match="not empty"):
        carry.carry(root, "old", "new", apply=True, log=quiet)
    assert (root / "old" / "lance").is_dir()


def test_carry_refuses_another_function(pair):
    """A1: the engine and the input form may differ; the weights may not."""
    root, old, new = pair
    other = _create(root, "other", over={"space": {**SPACE, "encoder": {
        **SPACE["encoder"], "weights": "sha256:" + "5e" * 32}}})
    assert carry.space_differences(old, new) == []
    assert carry.space_differences(old, other) == ["encoder.weights"]
    with pytest.raises(CodexError, match="encoder.weights"):
        carry.carry(root, "old", "other", apply=True, log=quiet)


def test_carry_refuses_a_table_pinned_to_another_space(pair):
    root, old, _new = pair
    _seeded(root)
    _vectors(root, old)
    con = sqlite3.connect(root / "old" / "state" / "manifest.sqlite")
    con.execute("UPDATE space SET space_id='sha256:' || hex(randomblob(32))")
    con.commit()
    con.close()
    with pytest.raises(CodexError, match="pinned to"):
        carry.carry(root, "old", "new", apply=True, log=quiet)


def test_the_command_takes_both_locks_and_carries(pair, capsys):
    from app import louise
    root, old, new = pair
    _seeded(root)
    _vectors(root, old)
    assert louise.main(["dev", "carry", "old", "new", "--root", str(root)]) == 0
    assert (root / "old" / "state" / "manifest.sqlite").is_file(), "no --yes: a dry run"
    capsys.readouterr()
    assert louise.main(["dev", "carry", "old", "new", "--root", str(root), "--yes"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["carry"]["space_id"] == new.space_id
    assert (root / "new" / "state" / "manifest.sqlite").is_file()
    for name in ("old", "new"):
        assert (root / name / "state" / "lock").is_file()


# ---- G2: keep by provenance ---------------------------------------------------

def _provenance(root, uids, ids):
    """doc 0: every chunk the archive's (imported); doc 1: one verified, one
    failed, one local; doc 2: another producer's."""
    coll = _local(root, "new")
    con = db.connect(coll)
    try:
        con.execute("UPDATE documents SET embed_done=1")
        rows = [(ids[0], uids[0], "imported", ARCHIVE), (ids[1], uids[0], "imported", ARCHIVE),
                (ids[2], uids[0], "verified", ARCHIVE),
                (ids[3], uids[1], "verified", ARCHIVE), (ids[4], uids[1], "failed", ARCHIVE),
                (ids[6], uids[2], "imported", "ed25519:" + "cd" * 32)]
        con.executemany("INSERT INTO chunk_verification(chunk_id, uid, state, producer) "
                        "VALUES(?,?,?,?)", rows)
    finally:
        con.close()
    return coll, set(ids[:4])


def _carried(pair):
    root, old, _new = pair
    _seeded(root)
    uids, ids = _vectors(root, old)
    carry.carry(root, "old", "new", apply=True, log=quiet)
    return root, uids, ids


def test_keep_drops_every_vector_the_archive_did_not_produce(pair):
    root, uids, ids = _carried(pair)
    coll, kept = _provenance(root, uids, ids)
    out = carry.keep_producer(root, "new", ARCHIVE, window=1, log=quiet)
    assert out == {"collection": "new", "producer": ARCHIVE, "kept": 4, "dropped": 5,
                   "documents_reset": 2, "vectors": 4, "verified": True}
    books = [int(u.split(":")[1]) for u in uids]
    assert set(embeddings.chunk_ids_of_books(books, collection=coll)) == kept
    con = db.connect(coll)
    try:
        done = dict(con.execute("SELECT uid, embed_done FROM documents").fetchall())
        assert done == {uids[0]: 1, uids[1]: 0, uids[2]: 0}
        left = {r[0] for r in con.execute("SELECT chunk_id FROM chunk_verification")}
        assert left == kept, "a dropped chunk's provenance goes with it"
    finally:
        con.close()
    assert carry.keep_producer(root, "new", ARCHIVE, log=quiet) == out, "done: nothing to do"


def test_keep_by_one_scan_drops_what_the_walk_drops(pair):
    """keep_producer_scan (wikipedia's 146 M rows, 2026-10-21): the same rows
    kept and dropped, the same documents reset and provenance cleared, from
    one pass over the table — a book of no kept row goes whole, a mixed one by
    id."""
    root, uids, ids = _carried(pair)
    coll, kept = _provenance(root, uids, ids)
    out = carry.keep_producer_scan(root, "new", ARCHIVE, log=quiet)
    assert out == {"collection": "new", "producer": ARCHIVE, "kept": 4, "dropped": 5,
                   "documents_reset": 2, "vectors": 4, "verified": True}
    books = [int(u.split(":")[1]) for u in uids]
    assert set(embeddings.chunk_ids_of_books(books, collection=coll)) == kept
    con = db.connect(coll)
    try:
        assert dict(con.execute("SELECT uid, embed_done FROM documents").fetchall()) ==             {uids[0]: 1, uids[1]: 0, uids[2]: 0}
        assert {r[0] for r in con.execute("SELECT chunk_id FROM chunk_verification")} == kept
    finally:
        con.close()
    again = carry.keep_producer_scan(root, "new", ARCHIVE, log=quiet)
    assert (again["dropped"], again["kept"], again["verified"]) == (0, 4, True), "run again: nothing left"


def test_keep_by_scan_merges_many_sorted_runs_and_leaves_none(pair):
    """Runs of two ids force several run files per side and a real merge
    (the table's order is not its ids' order); the run files go at the end."""
    root, uids, ids = _carried(pair)
    coll, kept = _provenance(root, uids, ids)
    out = carry.keep_producer_scan(root, "new", ARCHIVE, run=2, log=quiet)
    assert (out["kept"], out["dropped"], out["verified"]) == (4, 5, True)
    books = [int(u.split(":")[1]) for u in uids]
    assert set(embeddings.chunk_ids_of_books(books, collection=coll)) == kept
    assert not (Path(coll.manifest_path).parent / "carry-keep-runs").exists()


def test_a_delete_batch_is_sized_from_the_fragment_count():
    """2026-10-22: the keep's first 50,000-value delete on wikipedia-v8 (3,247
    fragments) was OOM-killed at 33.8 GB; a filter costs ~0.22 kB a value a
    fragment, so the batch is sized to DELETE_MEM_KB from the fragments."""
    assert carry._delete_batch(3247) == 1231, "wikipedia-v8: ~1 GB a delete"
    assert carry._delete_batch(1) == carry.DELETE_BATCH, "a small table: the old cap"
    assert carry._delete_batch(10 ** 6) == 100, "never fewer than 100 a delete"


def test_keep_by_scan_in_one_value_deletes_drops_the_same(pair, monkeypatch):
    """Many small deletes leave exactly what one large one did."""
    monkeypatch.setattr(carry, "_delete_batch", lambda fragments: 1)
    root, uids, ids = _carried(pair)
    coll, kept = _provenance(root, uids, ids)
    assert embeddings.fragment_count(coll) >= 1, "the count the batch is sized from"
    out = carry.keep_producer_scan(root, "new", ARCHIVE, log=quiet)
    assert (out["kept"], out["dropped"], out["verified"]) == (4, 5, True)
    books = [int(u.split(":")[1]) for u in uids]
    assert set(embeddings.chunk_ids_of_books(books, collection=coll)) == kept


def test_keep_by_scan_refuses_a_producer_with_no_marks(pair):
    """A producer named wrong matches nothing: keeping "its" rows would drop
    the whole table."""
    root, uids, ids = _carried(pair)
    coll, _kept = _provenance(root, uids, ids)
    books = [int(u.split(":")[1]) for u in uids]
    before = set(embeddings.chunk_ids_of_books(books, collection=coll))
    with pytest.raises(carry.CarryMismatch, match="no chunk of genesis:nobody is marked"):
        carry.keep_producer_scan(root, "new", "genesis:nobody", log=quiet)
    assert set(embeddings.chunk_ids_of_books(books, collection=coll)) == before, "nothing dropped"


def test_the_command_keeps_by_scan(pair, capsys):
    from app import louise
    root, uids, ids = _carried(pair)
    _provenance(root, uids, ids)
    assert louise.main(["dev", "carry", "old", "new", "--root", str(root), "--yes",
                        "--keep", "producer:medcpt-pubmed", "--scan"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["keep"]["kept"] == 4 and printed["keep"]["verified"] is True


def test_keep_resumes_after_a_stop_with_exact_counts(pair, monkeypatch):
    root, uids, ids = _carried(pair)
    coll, kept = _provenance(root, uids, ids)
    real, calls = embeddings.delete_chunks, []

    def dies_on_the_second(chunk_ids, **kw):
        calls.append(list(chunk_ids))
        if len(calls) == 2:
            raise Stopped
        return real(chunk_ids, **kw)
    monkeypatch.setattr(embeddings, "delete_chunks", dies_on_the_second)
    with pytest.raises(Stopped):
        carry.keep_producer(root, "new", ARCHIVE, window=1, log=quiet)
    con = db.connect(coll)
    try:
        # the second document was marked BEFORE its rows were to go: never lost
        assert con.execute("SELECT embed_done FROM documents WHERE uid=?",
                           (uids[1],)).fetchone()[0] == 0
    finally:
        con.close()
    monkeypatch.setattr(embeddings, "delete_chunks", real)
    out = carry.keep_producer(root, "new", ARCHIVE, window=1, log=quiet)
    assert (out["kept"], out["dropped"], out["documents_reset"]) == (4, 5, 2)
    assert set(embeddings.chunk_ids_of_books([int(u.split(":")[1]) for u in uids],
                                             collection=coll)) == kept


def test_keep_says_so_when_the_table_holds_rows_no_document_names(pair):
    root, uids, ids = _carried(pair)
    coll, _kept = _provenance(root, uids, ids)
    embeddings.upsert_chunks([{"id": "gutenberg:99999:0:1", "book_id": 99999, "lang": "en",
                               "seq": 0, "chapter": "", "text": "x",
                               "vector": np.zeros(768, dtype="float32")}],
                             collection=coll, dtype="f16")
    with pytest.raises(carry.CarryMismatch) as e:
        carry.keep_producer(root, "new", ARCHIVE, log=quiet)
    assert e.value.exit_code == 1


def test_the_keep_names_an_archive():
    assert carry.producer_of("producer:medcpt-pubmed") == "genesis:medcpt-pubmed"
    assert carry.producer_of("producer:ed25519:" + "ab" * 32) == "ed25519:" + "ab" * 32
    with pytest.raises(CodexError, match="producer:<archive>"):
        carry.producer_of("medcpt-pubmed")


def test_the_command_carries_then_keeps(pair, capsys):
    from app import louise
    root, old, _new = pair
    _seeded(root)
    uids, ids = _vectors(root, old)
    con = sqlite3.connect(root / "old" / "state" / "manifest.sqlite")
    con.executemany("INSERT INTO chunk_verification(chunk_id, uid, state, producer) "
                    "VALUES(?,?,'imported',?)", [(ids[0], uids[0], ARCHIVE)])
    con.commit()
    con.close()
    assert louise.main(["dev", "carry", "old", "new", "--root", str(root), "--yes",
                        "--keep", "producer:medcpt-pubmed"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["keep"]["kept"] == 1 and printed["keep"]["verified"] is True


# ---- 03 §3.4 amended 2026-10-09: seeding builds no record --------------------

def test_seeding_needs_no_equivalence_proof_of_this_image(pair, monkeypatch):
    """Live, gutenberg6: the equivalence proof needs a snapshot, and the
    snapshot needed the proof. A seeded snapshot is the reference's parts,
    so an image that is neither the reference nor proven equivalent may
    freeze it — and what it computes must equal the source's parts."""
    from app import louise
    root, _old_codex, new = pair
    monkeypatch.setenv("LOUISE_PLATFORM_IMAGE", "sha256:" + "e" * 64)   # another image
    assert louise.main(["hold", "snapshot", "create", "new", "--root", str(root),
                        "--shards-from", "old:2026-08"]) == 0
    snap = sn.load(root / "new" / "shard" / "snapshots" / "2026-08.json")
    old_snap = sn.load(root / "old" / "shard" / "snapshots" / "2026-08.json")
    assert [(s.slice, s.n, s.root, s.chunks_root) for s in snap.shards] == \
        [(s.slice, s.n, s.root, s.chunks_root) for s in old_snap.shards]


def test_without_seeding_the_image_must_still_be_the_reference(pair, monkeypatch):
    from app import louise
    from app.hold import equivalence
    root, _old_codex, _new = pair
    # an image whose versions differ (one with the Codex's versions is equivalent)
    monkeypatch.setattr(equivalence, "equivalent_by_versions", lambda c: False)
    monkeypatch.setenv("LOUISE_PLATFORM_IMAGE", "sha256:" + "e" * 64)
    assert louise.main(["hold", "snapshot", "create", "old", "--root", str(root),
                        "--snapshot", "2026-09"]) != 0
    assert not (root / "old" / "shard" / "snapshots" / "2026-09.json").exists()


def test_a_seeded_snapshot_whose_chunks_differ_is_withdrawn(pair, monkeypatch, capsys):
    """An image that cuts the chunks otherwise computes another chunks_root:
    the snapshot it froze is removed and the command fails."""
    from dataclasses import replace
    from app import louise
    root, _old_codex, _new = pair
    monkeypatch.setenv("LOUISE_PLATFORM_IMAGE", "sha256:" + "e" * 64)
    real = sn.create

    def other_cut(*a, **k):
        s = real(*a, **k)
        return replace(s, shards=tuple(replace(x, chunks_root="sha256:" + "0" * 64) for x in s.shards))
    monkeypatch.setattr(sn, "create", other_cut)
    assert louise.main(["hold", "snapshot", "create", "new", "--root", str(root),
                        "--shards-from", "old:2026-08"]) == 1
    assert "withdrawn" in capsys.readouterr().err
    assert not (root / "new" / "shard" / "snapshots" / "2026-08.json").exists()


# ---- image-equivalence across record twins (RULE 1, 2026-10-09) --------------

def test_a_reference_made_for_a_record_twin_is_this_collections(pair):
    """The reference image cannot read a newer Codex (gutenberg6, version 6),
    but reads its twin's; the twin's lines are this collection's when the
    record fields agree and the snapshots hold the same parts."""
    from types import SimpleNamespace
    from app.hold import check
    root, old, new = pair
    _out, new_snap = _seeded(root)
    old_snap = sn.load(root / "old" / "shard" / "snapshots" / "2026-08.json")
    ctx = SimpleNamespace(root=root, codex=new, snapshot=new_snap)
    assert check._record_twin(ctx, {"collection_id": old.collection_id,
                                    "snapshot_hash": old_snap.hash}) is None
    # another snapshot hash than the twin holds: not accepted
    why = check._record_twin(ctx, {"collection_id": old.collection_id, "snapshot_hash": "sha256:" + "0" * 64})
    assert why and "holds no snapshot" in why
    # an unknown collection: not accepted
    assert check._record_twin(ctx, {"collection_id": "sha256:" + "1" * 64, "snapshot_hash": old_snap.hash})


def test_a_collection_that_is_not_a_record_twin_is_refused(pair):
    from types import SimpleNamespace
    from app.hold import check
    root, old, _new = pair
    old_snap = sn.load(root / "old" / "shard" / "snapshots" / "2026-08.json")
    # a codex differing in a record field (record_line) is not a twin
    from dataclasses import replace
    ctx = SimpleNamespace(root=root, codex=replace(old, record_line=(old.record_line or 1) + 1),
                          snapshot=old_snap)
    why = check._record_twin(ctx, {"collection_id": old.collection_id, "snapshot_hash": old_snap.hash})
    assert why and "not a record twin" in why and "record_line" in why


def test_a_query_instruction_alone_is_crossed_and_every_row_kept(pair):
    """A1f (2026-10-20, gutenberg's Qwen3 queries): the instruction is prepended
    to queries only, so the table's rows are the target's by the book; any
    other difference beside it is still refused."""
    root, old, _new = pair
    instructed = _create(root, "instructed", over={"space": {**SPACE, "query": {
        **SPACE["query"], "instruction": "Instruct: find the book\nQuery:"}}})
    assert carry.space_differences(old, instructed) == ["query.instruction"]
    assert old.space_id != instructed.space_id
    rec.write_validation(root, "instructed")
    out = seed.seed_shards(root, "instructed", "old", log=quiet)
    sn.create(root, "instructed", snapshot_id=out["snapshot_id"], log=quiet)
    _uids, ids = _vectors(root, old)
    said: list[str] = []
    out = carry.carry(root, "old", "instructed", apply=True, log=said.append)
    assert any("A1f" in s for s in said) and out["done"]["lance"] == "moved"
    assert embeddings.stats(_local(root, "instructed"))["vectors"] == len(ids)
    both = _create(root, "both", over={"space": {**SPACE, "encoder": {
        **SPACE["encoder"], "weights": "sha256:" + "5e" * 32}, "query": {
        **SPACE["query"], "instruction": "Instruct: find another book\nQuery:"}}})
    with pytest.raises(CodexError, match="encoder.weights") as e:
        carry.carry(root, "instructed", "both", log=quiet)
    assert "query.instruction" not in str(e.value), "named as crossed, not as refused"


def test_create_reuses_a_held_twins_document_reference_and_embeds_only_the_query_side(pair):
    """RULE 1 (A1f's twin, 2026-10-20): `calibration_from` reuses the document
    side's reference vectors and envelope of a held Codex whose space is the
    recipe's but for its query side; the Codex written is byte for byte the
    one a by-the-book create computes, and another document side is refused."""
    from tools import recipe_from_codex as rfc
    root, _old, new = pair
    twin_recipe = rfc.recipe_of(new)
    twin_recipe["space"]["query"]["instruction"] = "Instruct: find the book\nQuery:"

    def no_document_embedding(texts):
        raise AssertionError("the document side is reused, never embedded")
    asked: list[str] = []

    def query_embed(texts):
        asked.extend(texts)
        return fake_query_embed(texts)
    twin = col.create_from_recipe(root, "gutenberg", twin_recipe, platform_image=PLATFORM,
                                  embed=no_document_embedding, embed_query=query_embed,
                                  name="twin", key=KEY, now=NOW,
                                  calibration_from=col.shard_dir_of(root, "new"))
    assert asked and all(t.startswith("Instruct: find the book\nQuery:") for t in asked)
    assert twin.space.calibration.vectors == new.space.calibration.vectors
    assert twin.space.envelope == new.space.envelope
    assert twin.space.calibration.query_vectors != new.space.calibration.query_vectors
    book = col.create_from_recipe(root.parent / "by-the-book", "gutenberg", json.loads(json.dumps(twin_recipe)),
                                  platform_image=PLATFORM, embed=fake_embed,
                                  embed_query=fake_query_embed, name="twin", key=KEY, now=NOW)
    assert book.canonical() == twin.canonical(), "indistinguishable from a by-the-book create"

    other = rfc.recipe_of(new)
    other["space"]["encoder"]["weights"] = "sha256:" + "5e" * 32
    with pytest.raises(CodexError, match="document side differs in encoder"):
        col.create_from_recipe(root, "gutenberg", other, platform_image=PLATFORM,
                               embed=no_document_embedding, embed_query=fake_query_embed,
                               name="other", key=KEY, now=NOW,
                               calibration_from=col.shard_dir_of(root, "new"))


def test_keeping_only_an_archives_vectors_lets_the_weights_differ(pair):
    """A1b (2026-10-10, pubmed): the kept rows are the archive producer's, made
    by neither Codex's encoder file, so the weights check does not apply when
    `--keep producer:<archive>` is given; the dim/dtype/... checks still do,
    and vectors-sample at the target's tolerance proves the rows before use."""
    root, old, _new = pair
    other = _create(root, "other", over={"space": {**SPACE, "encoder": {
        **SPACE["encoder"], "weights": "sha256:" + "5e" * 32}}})
    with pytest.raises(CodexError) as e:            # past the weights, stopped by the parts
        carry.carry(root, "old", "other", apply=False, keep="genesis:medcpt-pubmed", log=quiet)
    assert "encoder.weights" not in str(e.value) and "snapshot" in str(e.value)
    with pytest.raises(CodexError, match="encoder.weights"):
        carry.carry(root, "old", "other", apply=False, log=quiet)


def test_skip_marked_reads_only_short_documents_and_its_net_holds(pair):
    """skip_marked (2026-10-10, pubmed): a document whose every chunk carries
    the archive's mark is not read from the vector table. Where the table holds
    more rows than the manifest's chunk count says (this fixture: 3 rows a
    document), the end's count check refuses — the net the shortcut relies on."""
    root, old, _new = pair
    _seeded(root)
    _vectors(root, old)
    carry.carry(root, "old", "new", apply=True, log=quiet)
    with pytest.raises(carry.CarryMismatch):
        carry.keep_producer(root, "new", "genesis:medcpt-pubmed", skip_marked=True, log=quiet)


# ---- A1c: a parser that moved without moving chunks -------------------------

def _rebuilt_pair(pair):
    """`re`: `old`'s Codex with another parser (gutenberg's 9, 2026-10-11), and
    `old`'s vectors in place."""
    root, old, _new = pair
    import shutil
    re_ = col.create(root, replace(old, parser=Parser("gutenberg", 99)), name="re")
    # the same space, so the same calibration member a snapshot names
    shutil.copytree(root / "old" / "shard" / "calibration", root / "re" / "shard" / "calibration",
                    dirs_exist_ok=True)
    rec.write_validation(root, "re")
    uids, ids = _vectors(root, old)
    return root, old, re_, uids, ids


def _build_and_freeze(root, name, corpus_dir):
    rec.build(root, name, corpus_dir=corpus_dir, records_per_shard=2, log=quiet)
    sn.create(root, name, snapshot_id="2026-09", log=quiet)


def test_rebuilt_carries_across_the_parser_and_refuses_anything_else(pair):
    root, old, re_, _uids, _ids = _rebuilt_pair(pair)
    with pytest.raises(CodexError, match="parser"):
        carry.carry(root, "old", "re", apply=True, log=quiet)
    other = col.create(root, replace(old, parser=Parser("gutenberg", 99), cleaning_version=9),
                       name="other")
    with pytest.raises(CodexError, match="cleaning_version"):
        carry.carry(root, "old", "other", rebuilt=True, apply=True, log=quiet)
    out = carry.carry(root, "old", "re", rebuilt=True, copy=True, apply=True, log=quiet)
    assert out["done"]["manifest"] == "copied" and out["rebuilt"] is True
    assert json.loads((root / "re" / "state" / carry.MARKER).read_text())["rebuilt"] is True


def test_reconcile_keeps_the_vectors_of_every_unchanged_document_and_resets_the_rest(
        pair, corpus_dir):
    """A book revised at the origin between the two builds: its content_hash
    moved, its vectors go and it is embedded again; every other book keeps
    its vectors, and every txt_hash becomes its record's origin_hash."""
    root, old, re_, uids, ids = _rebuilt_pair(pair)
    with pytest.raises(CodexError, match="carry --rebuilt"):
        carry.reconcile(root, "old", "re", log=quiet)
    carry.carry(root, "old", "re", rebuilt=True, copy=True, apply=True, log=quiet)
    victim = uids[0]
    book = corpus_dir / victim.split(":")[1]
    txt = next(book.glob("*.txt*"))
    data = txt.read_bytes()
    i = data.index(b"*** END OF")
    txt.write_bytes(data[:i] + b"A paragraph revised upstream.\n\n" + data[i:])
    coll = _local(root, "re")
    con = db.connect(coll)
    try:      # the scan that would have seen it: the build reads the manifest
        con.execute("UPDATE documents SET mtime=mtime+1 WHERE uid=?", (victim,))
        con.commit()
    finally:
        con.close()
    _build_and_freeze(root, "re", corpus_dir)

    out = carry.reconcile(root, "old", "re", window=1, log=quiet)
    assert out["reset"] == 1 and out["kept"] == len(uids) - 1, out
    left = set(embeddings.chunk_ids_of_books([int(u.split(":")[1]) for u in uids], collection=coll))
    assert left == {c for c in ids if not c.startswith(victim + ":")}
    con = db.connect(coll)
    try:
        assert con.execute("SELECT embed_done FROM documents WHERE uid=?", (victim,)).fetchone()[0] == 0
        hashes = dict(con.execute("SELECT uid, txt_hash FROM documents").fetchall())
    finally:
        con.close()
    after = carry._content_of(root, "re")
    assert all(hashes[u] == after[u][1] for u in after), "txt_hash is the record's origin_hash"
    again = carry.reconcile(root, "old", "re", log=quiet)
    assert (again["kept"], again["reset"]) == (out["kept"], out["reset"]), "resumed, not redone"


def test_reconcile_drops_the_vectors_of_a_book_the_rebuild_refused(pair, corpus_dir):
    """pg673 on gutenberg's parser-10 re-mint: refused at the rebuild (its
    encoding declared broken), so the target holds no record of it, and the
    vectors the carry brought must go with it — an unchanged book beside it
    keeps its own."""
    root, old, re_, uids, ids = _rebuilt_pair(pair)
    carry.carry(root, "old", "re", rebuilt=True, copy=True, apply=True, log=quiet)
    refused = uids[1]
    coll = _local(root, "re")
    con = db.connect(coll)
    try:      # what the scan writes for an UnindexableDocument
        con.execute("UPDATE documents SET error=? WHERE uid=?", ("UnindexableDocument: broken", refused))
        con.commit()
    finally:
        con.close()
    _build_and_freeze(root, "re", corpus_dir)
    assert refused not in carry._content_of(root, "re"), "the fixture: no record of it"

    out = carry.reconcile(root, "old", "re", log=quiet)
    assert out["reset"] == 1 and out["kept"] == len(uids) - 1, out
    left = set(embeddings.chunk_ids_of_books([int(u.split(":")[1]) for u in uids], collection=coll))
    assert left == {c for c in ids if not c.startswith(refused + ":")}


def test_a_finished_cursor_the_copy_brought_along_is_not_this_pairs(pair, corpus_dir):
    """2026-10-22, gutenberg's parser-10 reconcile: the manifest copied by
    the carry held the parser-9 reconcile's finished cursor, and the new
    reconcile read it, did nothing and reported the old counts. A cursor
    written for another pair starts over; one written for this pair resumes."""
    root, old, re_, uids, ids = _rebuilt_pair(pair)
    carry.carry(root, "old", "re", rebuilt=True, copy=True, apply=True, log=quiet)
    coll = _local(root, "re")
    con = db.connect(coll)
    try:      # what the copy brought: another pair's pass, done, with its counts
        con.execute("BEGIN IMMEDIATE")
        db.set_control(con, carry.RECONCILE_KEY, json.dumps(
            {"done": True, "kept": 79326, "rehashed": 79387, "reset": 290, "rowid": 79616}))
        con.execute("COMMIT")
    finally:
        con.close()
    _build_and_freeze(root, "re", corpus_dir)
    out = carry.reconcile(root, "old", "re", window=1, log=quiet)
    assert out["kept"] == len(uids) and out["reset"] == 0, out
    assert carry.reconcile(root, "old", "re", log=quiet)["kept"] == len(uids), "this pair's cursor resumes"


def test_a_keep_cursor_the_copy_brought_along_is_not_trusted(pair):
    """The windowed keep's cursor is keyed by producer alone; one copied
    from another collection's manifest must not end the walk before it
    starts."""
    root, uids, ids = _carried(pair)
    coll, kept = _provenance(root, uids, ids)
    con = db.connect(coll)
    try:
        con.execute("BEGIN IMMEDIATE")
        db.set_control(con, carry.KEEP_KEY + ARCHIVE, json.dumps(
            {"done": True, "kept": 1, "dropped": 0, "documents_reset": 0, "rowid": 10**9}))
        con.execute("COMMIT")
    finally:
        con.close()
    out = carry.keep_producer(root, "new", ARCHIVE, log=quiet)
    assert (out["kept"], out["dropped"], out["verified"]) == (4, 5, True), out


@pytest.mark.parametrize("copy", [True, False])
def test_an_empty_manifest_another_writer_made_meanwhile_is_replaced_not_taken(pair, monkeypatch, copy):
    """2026-10-13, normattiva-1861: the worker opened the target two seconds
    after the carry removed its empty manifest, `db.connect` made a fresh
    one, and the carry took it for its own earlier copy — the copy never
    happened (and, moving, the source's manifest would have been deleted as
    an interrupted move). A target manifest with fewer documents than the
    source's is replaced; the source's documents arrive."""
    root, old, new = pair
    _seeded(root)
    uids, _ids = _vectors(root, old)
    real = carry._transfer

    def racing(src, dst, *, copy):
        if dst.name == "lance":                       # the first transfer: the worker strikes
            db.connect(_local(root, "new")).close()   # an empty manifest at the target
        return real(src, dst, copy=copy)
    monkeypatch.setattr(carry, "_transfer", racing)
    out = carry.carry(root, "old", "new", copy=copy, apply=True, log=quiet)
    assert out["done"]["manifest"] == ("copied" if copy else "moved")
    assert out["documents"] == len(uids) and len(uids) > 0
    con = db.connect(_local(root, "new"))
    try:
        assert {r[0] for r in con.execute("SELECT uid FROM documents")} == set(uids)
    finally:
        con.close()



def test_a_moved_sources_empty_manifest_does_not_undo_the_move(pair):
    """2026-10-19, pubmed → pubmed-v8: after the move a restart opened the
    source, whose file still names it, and `db.connect` made it an empty
    manifest; the resumed carry compared 0 with the target's 39,994,988 and
    refused. Resumed, a move whose target holds the documents stands: the
    stray is removed and the carry finishes."""
    root, old, new = pair
    _seeded(root)
    uids, _ids = _vectors(root, old)
    carry.carry(root, "old", "new", apply=True, log=quiet)
    db.connect(_local(root, "old")).close()              # the stray, after the move
    assert (root / "old" / "state" / "manifest.sqlite").is_file()
    again = carry.carry(root, "old", "new", apply=True, log=quiet)
    assert again["done"]["manifest"] == "present" and again["documents"] == len(uids)
    assert not (root / "old" / "state" / "manifest.sqlite").exists(), "the stray goes"



def test_drop_rows_drops_and_marks_the_documents_to_embed_again(pair):
    """02 §2.4's remedy as `dev drop-rows`: unlike `vectors check --drop`, which
    leaves an empty row, the documents are embedded again; an id the table
    does not hold is no work."""
    root, uids, ids = _carried(pair)
    coll, _kept = _provenance(root, uids, ids)
    out = carry.drop_rows(root, "new", [ids[0], ids[3], "gutenberg:99999:0:1"], window=1,
                          log=quiet)
    assert (out["asked"], out["dropped"], out["documents_reset"], out["verified"]) ==         (3, 2, 2, True)
    books = [int(u.split(":")[1]) for u in uids]
    left = set(embeddings.chunk_ids_of_books(books, collection=coll))
    assert ids[0] not in left and ids[3] not in left and len(left) == len(ids) - 2
    con = db.connect(coll)
    try:
        done = dict(con.execute("SELECT uid, embed_done FROM documents").fetchall())
        assert (done[uids[0]], done[uids[1]], done[uids[2]]) == (0, 0, 1)
        prov = {r[0] for r in con.execute("SELECT chunk_id FROM chunk_verification")}
        assert ids[0] not in prov and ids[3] not in prov, "their provenance goes with them"
    finally:
        con.close()


# ---- A1d: an origin that only adds files; a source with no records ----------

def _with_file(c, rel="added/page_props.sql.gz"):
    spec = dict(c.origin.spec)
    spec["files"] = [*(spec.get("files") or []), {"rel_path": rel, "size": None, "checksum": None}]
    spec["filters"] = [*(spec.get("filters") or []), "*.sql.gz"]
    return replace(c, parser=Parser("gutenberg", 99),
                   origin=replace(c.origin, spec=spec, form=c.origin.form + "; and a table beside it"))


def test_an_origin_that_only_adds_files_is_the_one_difference_a1d_admits(pair):
    _root, old, _new = pair
    added = _with_file(old)
    assert carry.origin_only_adds(old, added)
    assert not carry.origin_only_adds(added, old), "a file the records were read from removed"
    moved = replace(added, origin=replace(added.origin, spec={**added.origin.spec, "url": "https://x"}))
    assert not carry.origin_only_adds(old, moved)
    assert not carry.origin_only_adds(old, replace(old, origin=replace(old.origin, form="other")))


def _unrecorded_source(pair, corpus_dir):
    """`src`: discovered and embedded with its real chunks, never built — no
    record, no snapshot (wikipedia's state, 2026-10-18)."""
    from app import discovery, layers, plugin_io
    root, old, _new = pair
    src = col.create(root, replace(old, parser=Parser("gutenberg", 98)), name="src")
    coll = rec._local_collection(root, "src", src, corpus_dir=corpus_dir)
    con = db.connect(coll)
    try:
        discovery.sync_collection(con, coll)
        con.execute("UPDATE documents SET rdf_done=1, fts_done=1, embed_done=1")
        con.execute("INSERT INTO space(id, space_id, dim, pinned_at) VALUES(1, ?, ?, 0)",
                    (src.space_id, src.space.dim))
        con.commit()
        docs = [dict(r) for r in con.execute("SELECT uid, native_id, path, lang FROM documents")]
    finally:
        con.close()
    plugin = coll.plugin_instance()
    rng = np.random.default_rng(3)
    rows = [{"id": c["id"], "book_id": int(d["native_id"]), "lang": d["lang"] or "en",
             "seq": c["seq"], "chapter": c.get("chapter") or "", "text": c["text"],
             "vector": rng.standard_normal(src.space.dim).astype("float32")}
            for d in docs for c in layers.chunks_for_document(coll, d, plugin_io.sections(plugin, d))]
    embeddings.upsert_chunks(rows, collection=coll, dtype=src.space.dtype)
    target = col.create(root, _with_file(src), name="tgt")
    return root, src, target, docs, rows


def test_resign_keeps_every_mark_and_vector_and_takes_the_new_signatures(pair, corpus_dir):
    root, src, tgt, docs, rows = _unrecorded_source(pair, corpus_dir)
    with pytest.raises(CodexError, match="carry --rebuilt"):
        carry.resign(root, "src", "tgt", log=quiet)
    carry.carry(root, "src", "tgt", rebuilt=True, copy=True, apply=True, log=quiet)
    coll = rec._local_collection(root, "tgt", tgt, corpus_dir=corpus_dir)
    con = db.connect(coll)
    try:   # the signatures and hashes the OLD parser left, which the new one does not give
        before = {r["uid"]: (r["mtime"], r["size"]) for r in con.execute("SELECT uid, mtime, size FROM documents")}
        con.execute("UPDATE documents SET mtime=mtime-7, txt_hash='parser-98'")
        con.commit()
    finally:
        con.close()
    out = carry.resign(root, "src", "tgt", sample=50, corpus_dir=corpus_dir, log=quiet)
    assert out["scan"]["resigned"] == len(docs) and not out["scan"].get("changed")
    assert out["compared"] == len(docs) and out["mismatched"] == []
    con = db.connect(coll)
    try:
        got = con.execute("SELECT uid, mtime, size, txt_hash, rdf_done, fts_done, embed_done "
                          "FROM documents").fetchall()
    finally:
        con.close()
    assert {r["uid"]: (r["mtime"], r["size"]) for r in got} == before
    assert all(r["txt_hash"] is None and (r["rdf_done"], r["fts_done"], r["embed_done"]) == (1, 1, 1)
               for r in got), "as a fresh scan writes it; every mark kept"
    left = embeddings.chunk_ids_of_books([int(d["native_id"]) for d in docs], collection=coll)
    assert sorted(left) == sorted(r["id"] for r in rows), "every vector kept"
    again = carry.resign(root, "src", "tgt", sample=50, corpus_dir=corpus_dir, log=quiet)
    assert not again["scan"].get("resigned"), "a second run rewrites nothing"


def test_resign_refuses_when_a_sampled_document_cuts_to_other_chunks(pair, corpus_dir):
    root, src, tgt, docs, rows = _unrecorded_source(pair, corpus_dir)
    carry.carry(root, "src", "tgt", rebuilt=True, copy=True, apply=True, log=quiet)
    coll = rec._local_collection(root, "tgt", tgt, corpus_dir=corpus_dir)
    victim = rows[0]
    embeddings.upsert_chunks([{**victim, "text": victim["text"] + " (another text)"}],
                             collection=coll, dtype=tgt.space.dtype)
    with pytest.raises(carry.CarryMismatch, match="the text moved"):
        carry.resign(root, "src", "tgt", sample=50, corpus_dir=corpus_dir, log=quiet)


def test_resign_is_for_a_source_without_records(pair):
    """A source with a snapshot has every document's content_hash to compare:
    `--reconcile` is the stronger proof and the one to use."""
    root, old, re_, _uids, _ids = _rebuilt_pair(pair)
    carry.carry(root, "old", "re", rebuilt=True, copy=True, apply=True, log=quiet)
    with pytest.raises(CodexError, match="--reconcile"):
        carry.resign(root, "old", "re", log=quiet)


def test_an_instruction_twin_admits_its_predecessors_bundles_and_carries_the_share(pair):
    """A1f extended to the share (RULE 1; München, 2026-10-20): gutenberg's
    43 GB of bundles are for the space without the instruction, and by the
    book the twin would re-export, re-seed and re-fetch every one for the
    same bytes. The twin ADMITS the predecessor's space (T0, the statements);
    its bundles stay filed under their own segment, found by `bundle_dir`,
    served and listed under every admitted segment; the carry moves
    state/bundles and state/share and re-keys the coverage rows and the
    union cache instead of clearing them."""
    from app.hold import admit as adm
    from app.share import contribute, counting, coverage as cov, members as mem
    root, old, _new = pair
    plain = old                                      # the live Codex, version 4 as the node's
    instructed = _create(root, "instructed", space_version=4, over={"space": {**SPACE, "query": {
        **SPACE["query"], "instruction": "Instruct: find the book\nQuery:"}}})
    assert instructed.admitted_spaces == (instructed.space_id, plain.space_id)
    assert instructed.admitted_segments == (instructed.space_segment, plain.space_segment)
    assert plain.admitted_spaces == (plain.space_id,), "no instruction: its own space only"
    # T0 admits the predecessor's bundle, and nothing else
    header = {"space_id": plain.space_id, "dim": instructed.space.dim, "dtype": instructed.space.dtype,
              "producer_pubkey": "ed25519:" + "aa" * 32}
    adm.check_admissible(instructed, header)
    with pytest.raises(adm.AdmitError, match="another space"):
        adm.check_admissible(plain, {**header, "space_id": instructed.space_id})
    with pytest.raises(adm.AdmitError):
        adm.check_admissible(instructed, {**header, "space_id": "sha256:" + "5e" * 32})
    rec.write_validation(root, "instructed")
    out = seed.seed_shards(root, "instructed", "old", log=quiet)
    sn.create(root, "instructed", snapshot_id=out["snapshot_id"], log=quiet)
    _vectors(root, old)                              # old's table and one coverage row of its own
    # one bundle of old's space, its coverage row and the seeder's union cache
    bid = "sha256:" + "b0" * 32
    held = contribute.bundle_dir(root, "old", plain, "lang=en/00001", bid)
    held.mkdir(parents=True)
    for f in mem.BUNDLE_FILES:
        (held / f).write_bytes(f.encode())
    con = db.connect(_local(root, "old"))
    cov.record(con, bundle_id=bid, shard=cov.shard_id(plain, "lang=en/00001"), producer="k", uids=[], chunks=3,
               tiers={"T0": "pass"}, source="fetched", accepted_at="2026-10-20T10:00:00Z")
    con.close()
    share = root / "old" / "state" / "share"
    share.mkdir(parents=True, exist_ok=True)
    (share / counting.CACHE).write_text(json.dumps({cov.shard_id(plain, "lang=en/00001"): {"bundles": [bid],
                                                                                           "covered": 3}}))
    said: list[str] = []
    dry = carry.carry(root, "old", "instructed", apply=False, log=said.append)
    assert any("state/bundles and state/share" in s for s in said) and dry["apply"] is False
    out = carry.carry(root, "old", "instructed", apply=True, log=said.append)
    assert out["done"]["bundles"] == "moved" and out["done"]["share"] == "moved"
    assert not (root / "old" / "state" / "bundles").exists()
    found = contribute.bundle_dir(root, "instructed", instructed, "lang=en/00001", bid)
    assert found.is_dir() and found.parent.parent.parent.name == plain.space_segment, "filed under its own segment"
    state = root / "instructed" / "state"
    assert [d for s in contribute.segments_of(instructed) for d in mem.bundle_dirs(state, s)] == [found]
    assert contribute.bundle_dir(root, "instructed", instructed, "lang=en/00002", "sha256:" + "c1" * 32).parent \
        .parent.parent.name == instructed.space_segment, "a new export goes under the twin's own segment"
    con = sqlite3.connect(state / "manifest.sqlite")
    shards = {r[0] for r in con.execute("SELECT shard FROM coverage")}
    con.close()
    assert shards == {cov.shard_id(instructed, "lang=en/00001")}, "re-keyed, not cleared"
    assert out["coverage_rows_cleared"] == 2 and any("re-keyed to it (A1f)" in s for s in said)
    union = json.loads((state / "share" / counting.CACHE).read_text())
    assert list(union) == [cov.shard_id(instructed, "lang=en/00001")]


def _foreign_row_and_bundle(root, codex, name="old"):
    """Another producer's accepted bundle in `name`: its row and its files."""
    from app.share import contribute, coverage as cov
    coll = _local(root, name)
    con = db.connect(coll)
    try:
        cov.record(con, bundle_id="sha256:" + "ab" * 32, shard=cov.shard_id(codex, "lang=en/00003"),
                   producer="ed25519:" + "9a" * 32, uids=["gutenberg:10001"], chunks=3, tiers={},
                   source="fetched", accepted_at="2026-10-20T00:00:00Z")
    finally:
        con.close()
    d = contribute.bundle_dir(root, name, codex, "lang=en/00003", "sha256:" + "ab" * 32)
    d.mkdir(parents=True, exist_ok=True)
    (d / "possession.json").write_text('{"gutenberg:10001": "h"}', encoding="utf-8")
    return d


def _coverage(root, name):
    con = db.connect(_local(root, name))
    try:
        return {r[0]: r[1] for r in con.execute("SELECT bundle_id, shard FROM coverage")}
    finally:
        con.close()


def test_a_same_space_rebuilt_carry_keeps_the_source_s_rows_unkeyed_and_links_its_bundles(pair):
    """2026-10-22, gutenberg p9 → p10 (the same space, records rebuilt): the
    carry cleared the source's coverage rows — the only record of what had
    been landed from another producer — and the held path then signed it all.
    The rows now stay under the SOURCE's shard ids (provenance; the parts
    were re-cut, so they are never the target's coverage) and the bundle
    tree is hard-linked, so their possession files are read in the target."""
    root, old, re_, _uids, _ids = _rebuilt_pair(pair)
    src_dir = _foreign_row_and_bundle(root, old)
    out = carry.carry(root, "old", "re", rebuilt=True, copy=True, apply=True, log=quiet)
    rows = _coverage(root, "re")
    assert rows["sha256:" + "ab" * 32].startswith(old.collection_id), "unkeyed: still the source's shard id"
    assert out["coverage_rows_cleared"] >= 1 and all(not s.startswith(re_.collection_id) for s in rows.values())
    from app.share import contribute
    linked = contribute.bundle_dir(root, "re", re_, "lang=en/00003", "sha256:" + "ab" * 32) / "possession.json"
    assert linked.read_text() == '{"gutenberg:10001": "h"}'
    assert os.stat(linked).st_ino == os.stat(src_dir / "possession.json").st_ino, "hard-linked, not copied"
    marker = json.loads((root / "re" / "state" / carry.MARKER).read_text())
    assert marker["same_space"] is True and marker["predecessors"] == [old.collection_id]
    assert contribute.predecessors(root, "re") == [old.collection_id]


def test_predecessor_rows_restores_a_carry_that_cleared_them(pair):
    """The remedy for a same-space carry made before provenance was kept:
    the source's rows copied unkeyed, its bundle tree linked, the marker
    naming the predecessor. Run again: nothing more."""
    root, old, re_, _uids, _ids = _rebuilt_pair(pair)
    _foreign_row_and_bundle(root, old)
    carry.carry(root, "old", "re", rebuilt=True, copy=True, apply=True, log=quiet)
    # as the carry of 2026-10-22 left gutenberg: the rows gone, the tree not linked, an old marker
    con = db.connect(_local(root, "re"))
    try:
        con.execute("DELETE FROM coverage")
        con.commit()
    finally:
        con.close()
    import shutil
    shutil.rmtree(root / "re" / "state" / "bundles", ignore_errors=True)
    m = root / "re" / "state" / carry.MARKER
    m.write_text(json.dumps({k: v for k, v in json.loads(m.read_text()).items()
                             if k not in ("same_space", "predecessors")}))
    from app.share import contribute
    assert contribute.predecessors(root, "re") == []
    dry = carry.keep_predecessor_rows(root, "old", "re", log=quiet)
    assert dry["apply"] is False and _coverage(root, "re") == {}
    out = carry.keep_predecessor_rows(root, "old", "re", apply=True, log=quiet)
    assert out["rows_written"] == out["rows"] >= 2 and out["bundles"]["linked"] >= 1
    assert "sha256:" + "ab" * 32 in _coverage(root, "re")
    assert contribute.predecessors(root, "re") == [old.collection_id]
    again = carry.keep_predecessor_rows(root, "old", "re", apply=True, log=quiet)
    assert again["rows_written"] == 0 and again["bundles"]["linked"] == 0


# ---- A1h: a collection holding no vector, into ANY space --------------------

def _bge_space(root, name="bge"):
    """The same records in another FUNCTION: other weights, other width, another
    query encoder (wikidata from jina-v3 to bge-m3, 2026-10-31)."""
    from functools import partial
    space = {**SPACE, "encoder": {**SPACE["encoder"], "weights": "sha256:" + "be" * 32},
             "dim": 1024, "query": {"encoder": {"weights": "sha256:" + "bf" * 32}, "instruction": None}}
    return col.create_from_recipe(root, "gutenberg", recipe(space=space), platform_image=PLATFORM,
                                  embed=partial(fake_embed, dim=1024),
                                  embed_query=partial(fake_query_embed, dim=1024),
                                  name=name, key=KEY, now=NOW)


def test_unembedded_carries_the_manifest_and_index_into_another_space(pair):
    root, old, _new = pair
    bge = _bge_space(root)
    assert {"encoder.weights", "dim", "query.encoder.weights"} <= set(carry.space_differences(old, bge))
    with pytest.raises(CodexError, match="spaces differ"):
        carry.carry(root, "old", "bge", apply=True, log=quiet)
    (root / "old" / "index").mkdir(parents=True, exist_ok=True)
    (root / "old" / "index" / "words").write_text("the text index")
    con = sqlite3.connect(root / "old" / "state" / "manifest.sqlite")
    declined = con.execute("SELECT uid FROM documents ORDER BY rowid LIMIT 1").fetchone()[0]
    con.execute("UPDATE documents SET embed_done=-1 WHERE uid=?", (declined,))
    con.commit()
    n = con.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    con.close()

    plan = carry.carry(root, "old", "bge", unembedded=True, log=quiet)
    assert plan["apply"] is False and (root / "old" / "state" / "manifest.sqlite").is_file()
    out = carry.carry(root, "old", "bge", unembedded=True, apply=True, log=quiet)
    assert out["done"] == {"index": "moved", "manifest": "moved"}, "no vector table crosses"
    assert out["documents"] == n
    assert (root / "bge" / "index" / "words").read_text() == "the text index"
    assert json.loads((root / "bge" / "state" / carry.MARKER).read_text())["unembedded"] is True
    coll = _local(root, "bge")
    con = db.connect(coll)
    try:
        assert con.execute("SELECT space_id, dim FROM space").fetchone()[:] == (bge.space_id, 1024)
        spaces._validate_codex(con, coll)            # the boot check passes: not refused
        assert con.execute("SELECT embed_done FROM documents WHERE uid=?", (declined,)).fetchone()[0] == -1
    finally:
        con.close()
    assert carry.records_twin_of(root / "bge" / "state") == old.collection_id
    again = carry.carry(root, "old", "bge", unembedded=True, apply=True, log=quiet)
    assert again["done"] == {"index": "present", "manifest": "present"}


def test_unembedded_refuses_a_source_that_holds_a_vector(pair):
    root, old, _new = pair
    _bge_space(root)
    _vectors(root, old)
    with pytest.raises(CodexError, match="holds vectors .*row"):
        carry.carry(root, "old", "bge", unembedded=True, apply=True, log=quiet)
    assert (root / "old" / "state" / "manifest.sqlite").is_file()


def test_unembedded_refuses_a_document_marked_embedded(pair):
    root, _old, _new = pair
    _bge_space(root)
    con = sqlite3.connect(root / "old" / "state" / "manifest.sqlite")
    con.execute("UPDATE documents SET embed_done=1 WHERE rowid=(SELECT MIN(rowid) FROM documents)")
    con.commit()
    con.close()
    with pytest.raises(CodexError, match="marked embedded"):
        carry.carry(root, "old", "bge", unembedded=True, apply=True, log=quiet)


def test_unembedded_refuses_other_records_and_any_other_mode(pair):
    root, _old, new = pair
    col.create(root, replace(new, parser=Parser("gutenberg", 99)), name="other")
    with pytest.raises(CodexError, match="parser"):
        carry.carry(root, "old", "other", unembedded=True, apply=True, log=quiet)
    _bge_space(root)
    for mode in ({"rebuilt": True}, {"resliced": True}, {"recut": True}, {"keep": ARCHIVE}):
        with pytest.raises(CodexError, match="carries no vector"):
            carry.carry(root, "old", "bge", unembedded=True, apply=True, log=quiet, **mode)
    assert (root / "old" / "state" / "manifest.sqlite").is_file()


def test_the_command_carries_unembedded(pair, capsys):
    from app import louise
    root, _old, _new = pair
    bge = _bge_space(root)
    assert louise.main(["dev", "carry", "old", "bge", "--root", str(root), "--unembedded", "--yes"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["carry"]["space_id"] == bge.space_id and printed["carry"]["unembedded"] is True
