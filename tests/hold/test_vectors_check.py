"""tests/hold/test_vectors_check.py — 03 §3.2's `vectors-sample`, locally.

Migration plan step 4h, the last of step 4. The plan's own sentence is
the one thing this file exists to hold in place: `vectors-sample` draws
SIXTEEN chunks per sampled shard always, and the plan sets how many
shards and never how many chunks.
"""
from dataclasses import replace

import numpy as np
import pytest

from app import embeddings, louise
from app.hold import collection as col, land, vectors_check as vck
from app.hold.vectors_check import VectorsCheckError
from tests.hold.test_admit import (chunk_texts, made,  # noqa: F401
                                   reference_and_envelope, space_embed)
from tests.hold.test_vectors_export import build_drag, first_shard
from tests.hold.test_vectors_import import _member, _stored, importable  # noqa: F401

SEED = b"\x01" * 32



# Written for a node with no encoder (EMBED_URL unset): with no default
# encoder now, that is a collection that names none (operator, 2026-09-18)
pytestmark = pytest.mark.strict_encoder

@pytest.fixture
def stocked(importable):
    """A collection whose Lance table holds the bundle's vectors."""
    root, snapshot, codex, dest, ids = importable
    assert louise.main(["hold", "vectors", "import", "gutenberg",
                        "--root", str(root), "--bundle", str(dest)]) == 0
    return root, snapshot, codex, ids


def _check(root, *extra):
    return louise.main(["hold", "vectors", "check", "gutenberg",
                        "--root", str(root), "--seed", SEED.hex(), *extra])


def _run(codex, snapshot, root, *, embed, drop=None, fraction=None):
    def ids_for(entry):
        return sorted(land.cut(codex, land.records_for(
            root, "gutenberg", snapshot, f"{entry.slice}/{entry.n:05d}", None)))

    cut = land.cut(codex, land.records_for(
        root, "gutenberg", snapshot, first_shard(snapshot), None))
    from app.hold import records as rec
    local = rec.local_collection(root, "gutenberg", codex)
    return vck.check(
        codex, snapshot, seed=SEED, chunk_ids_for=ids_for,
        text_of=lambda cid: (cut.get(cid) or {}).get("text"),
        vectors_for=lambda i: embeddings.vectors_by_chunk_id(i, collection=local),
        embed=embed, fraction=fraction, drop=drop)


# ---- the plan sets shards and never chunks -------------------------------

def test_sixteen_chunks_per_shard_comes_from_the_SPACE_not_the_plan(stocked):
    """The migration plan's own sentence. Sixteen is
    `space.calibration.n`, so it travels with the space and two peers
    checking one collection draw the same size of sample whatever their
    local plans say — a plan that could set it would let a peer publish an
    attestation that looked like everyone else's and rested on four
    chunks."""
    root, snapshot, codex, _ = stocked
    r = _run(codex, snapshot, root, embed=lambda t: space_embed(t, dim=codex.space.dim))
    assert r.n == codex.space.calibration.n == 16

    narrow = replace(codex, validation=replace(
        codex.validation, sample={"chunks": {"shards": "0.05"}}))
    wide = replace(codex, validation=replace(
        codex.validation, sample={"chunks": {"shards": "1"}}))
    assert vck.plan_fraction(narrow) == "0.05"
    assert vck.plan_fraction(wide) == "1"
    # …and the chunk count is untouched by either
    assert narrow.space.calibration.n == wide.space.calibration.n == 16


def test_a_plan_that_declined_to_sample_gets_every_shard(stocked):
    """A plan with no entry for this check did not ask for LESS."""
    root, snapshot, codex, _ = stocked
    bare = replace(codex, validation=replace(codex.validation, sample={}))
    assert vck.plan_fraction(bare) == "1"
    assert len(vck.shards_for(bare, snapshot, seed=SEED)) == len(snapshot.shards)


def test_the_run_flag_raises_the_plans_sample_and_cannot_lower_it(stocked):
    """07 §7.3 in as many words."""
    root, snapshot, codex, _ = stocked
    tight = replace(codex, validation=replace(
        codex.validation, sample={"chunks": {"shards": "1"}}))
    assert len(vck.shards_for(tight, snapshot, seed=SEED, fraction="0.01")) \
        == len(snapshot.shards), "--sample lowered the plan's own fraction"


def test_the_draw_is_seeded_and_reproducible(stocked):
    root, snapshot, codex, _ = stocked
    a = vck.shards_for(codex, snapshot, seed=SEED)
    b = vck.shards_for(codex, snapshot, seed=SEED)
    assert [s.n for s in a] == [s.n for s in b]


def test_a_snapshot_with_no_shard_is_a_setup_error_not_a_verdict(stocked):
    root, snapshot, codex, _ = stocked
    with pytest.raises(VectorsCheckError, match="no shard to sample"):
        vck.shards_for(codex, replace(snapshot, shards=()), seed=SEED)


# ---- the three outcomes --------------------------------------------------

def test_this_peers_own_vectors_reproduce(stocked):
    root, snapshot, codex, ids = stocked
    r = _run(codex, snapshot, root, embed=lambda t: space_embed(t, dim=codex.space.dim))
    assert r.verdict == vck.PASS, r.why
    assert r.failing == () and r.dropped == ()
    assert sum(s.checked for s in r.shards) >= 1


def test_no_encoder_is_INCONCLUSIVE_and_never_a_pass(stocked):
    """03 §3.1's third outcome. This is the one that matters: a check
    that passed for want of an encoder would certify a table nobody
    looked at."""
    root, snapshot, codex, _ = stocked
    r = _run(codex, snapshot, root, embed=None)
    assert r.verdict == vck.INCONCLUSIVE
    assert "no compliant encoder" in r.why


def test_an_empty_table_is_INCONCLUSIVE_and_never_a_pass(importable):
    """The other half of the same trap: nothing to reproduce is not
    proof that everything reproduces."""
    root, snapshot, codex, dest, _ = importable
    r = _run(codex, snapshot, root,
             embed=lambda t: space_embed(t, dim=codex.space.dim))
    assert r.verdict == vck.INCONCLUSIVE
    assert "not one chunk" in r.why


def test_a_wrong_encoder_fails_the_stored_rows(stocked):
    """What a failure means here is NOT what T3's means: this judges what
    is already stored, whoever put it there."""
    root, snapshot, codex, ids = stocked
    r = _run(codex, snapshot, root,
             embed=lambda t: space_embed(t, dim=codex.space.dim, tag=b"other"))
    assert r.verdict == vck.FAIL, r.why
    assert set(r.failing) <= set(ids) and r.failing


# ---- --drop --------------------------------------------------------------

def test_drop_empties_a_failing_row_even_when_its_shard_passes(stocked):
    """The two verdicts are at different granularities on purpose.

    §3.2's `vectors-sample` is STATISTICAL — a shard passes on the
    calibration quorum, so one bad chunk in three does not condemn it,
    and the attestation says the collection reproduces. 02 §2.4's remedy
    is per ROW: a chunk that did not reproduce is a chunk nothing should
    claim, whatever its neighbours did. So the row goes and the verdict
    stays `pass`, and neither is a mistake.
    """
    root, snapshot, codex, ids = stocked
    before = _stored(root, codex)
    assert set(before) == set(ids)

    victim = sorted(ids)[0]

    def half_wrong(texts):
        # the victim's text answers from another space; the rest from this
        want = (land.cut(codex, land.records_for(
            root, "gutenberg", snapshot, first_shard(snapshot), None))
            [victim]["text"])
        return [space_embed([t], dim=codex.space.dim,
                            tag=b"other" if t == want else b"")[0]
                for t in texts]

    dropped = []
    from app.hold import records as rec
    local = rec.local_collection(root, "gutenberg", codex)

    def drop(cids):
        dropped.extend(cids)
        return embeddings.delete_chunks(cids, collection=local)

    r = _run(codex, snapshot, root, embed=half_wrong, drop=drop)
    assert r.verdict == vck.PASS, r.why
    assert r.shards[0].passed == 2 and r.shards[0].checked == 3
    assert dropped == [victim] and r.dropped == (victim,)
    assert "emptied" in r.why
    after = _stored(root, codex)
    assert set(after) == set(ids) - {victim}


def test_without_drop_nothing_is_deleted(stocked):
    """Deleting a stranger's vectors on a cosine is a decision an operator
    makes, so the flag is off by default."""
    root, snapshot, codex, ids = stocked
    r = _run(codex, snapshot, root,
             embed=lambda t: space_embed(t, dim=codex.space.dim, tag=b"other"))
    assert r.verdict == vck.FAIL and r.failing
    assert r.dropped == ()
    assert set(_stored(root, codex)) == set(ids)


def test_delete_chunks_refuses_an_id_that_is_not_9_5s_shape(stocked):
    root, _, codex, _ = stocked
    from app.hold import records as rec
    local = rec.local_collection(root, "gutenberg", codex)
    with pytest.raises(ValueError, match="09 §9.5"):
        embeddings.delete_chunks(["g:1:0:1' OR '1'='1"], collection=local)


# ---- the verb ------------------------------------------------------------

def test_the_verb_is_in_the_tree():
    a = louise.build_parser().parse_args(["hold", "vectors", "check", "g"])
    assert a.fn is not None and a.drop is False


def test_the_command_exits_3_without_an_encoder(stocked, capsys):
    """07 §7.7 reserves 3 for inconclusive."""
    root, _, _, _ = stocked
    assert _check(root) == 3
    assert "INCONCLUSIVE" in capsys.readouterr().out
