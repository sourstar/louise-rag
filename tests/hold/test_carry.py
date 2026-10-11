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
