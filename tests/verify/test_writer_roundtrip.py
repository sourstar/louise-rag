"""The artifact writer must produce something the loader accepts and the tiers
can actually use. A writer whose output the verifier rejects is worse than no
writer at all, so this is a genuine round trip rather than a unit test of
either half.
"""
import numpy as np
import pytest
from nacl import signing

from app import db, spaces
from app.verify import Tier, run_ladder
from app.verify import artifact as artifact_mod
from tests.bundles.helpers import DIM, seed_document
from tools import build_space_artifact

KEY = signing.SigningKey(bytes(range(32)))


def _uid_of(chunk_id):
    return chunk_id.rsplit(":", 2)[0]


def _seq_of(chunk_id):
    return int(chunk_id.rsplit(":", 2)[1])


@pytest.fixture
def calibrated(con, collection, tmp_path):
    """A reference corpus large enough for a document-block bootstrap to have
    something to resample."""
    rows = []
    for native_id in range(1, 13):
        rows += seed_document(con, collection, f"gutenberg:{native_id}", native_id,
                               n_chunks=4, seed=native_id)
    out = tmp_path / "artifact-out"
    # 6000 replicates, not a token number: the ladder refuses to evaluate an
    # alpha finer than 1/B, and T0's share of the default budget is 2e-4, so
    # anything under 5000 makes the envelope check SKIP — correctly, but then
    # this fixture would not exercise it.
    build_space_artifact.build(collection, out, replicates=6000, signing_key=KEY)
    return out, rows


def test_writer_output_loads(calibrated):
    out, _ = calibrated
    loaded = artifact_mod.load(out)
    assert loaded.dim == DIM
    assert loaded.envelope is not None and loaded.envelope.shape == (2, DIM)
    assert loaded.probes is not None
    assert loaded.goldens is not None


def test_writer_declares_a_document_bootstrap(calibrated):
    """The loader refuses anything else, so the writer must genuinely resample
    documents rather than chunks."""
    out, _ = calibrated
    assert artifact_mod.load(out).bootstrap_unit == "document"


def test_writer_signature_verifies_for_its_author(calibrated):
    out, _ = calibrated
    author = bytes(KEY.verify_key).hex()
    assert artifact_mod.load(out, trusted_authors=[author]).trusted is True


def test_writer_publishes_size_bucketed_nulls(calibrated):
    out, _ = calibrated
    loaded = artifact_mod.load(out)
    buckets = (loaded.envelope_block().get("moment_statistic") or {}) \
        .get("null_quantiles_by_bucket") or []
    assert buckets, "no bucketed null was published"
    assert all("q" in b and b["q"] for b in buckets)


def test_ladder_accepts_the_corpus_it_was_calibrated_on(calibrated, collection):
    """The load-bearing round trip: vectors from the reference corpus must not
    be rejected by a null built from that same corpus. A too-tight null (the
    chunk-bootstrap mistake) shows up here immediately."""
    out, rows = calibrated
    loaded = artifact_mod.load(out)
    chunk_ids = [r["id"] for r in rows[:16]]
    vectors = [np.asarray(r["vector"], dtype=np.float64) for r in rows[:16]]
    verdict = run_ladder(chunk_ids, vectors, dim=DIM, dtype="f32", artifact=loaded,
                         uid_of=_uid_of, seq_of=_seq_of, max_tier=Tier.T1)
    assert not verdict.failed, verdict.summary()


def test_ladder_rejects_foreign_vectors_against_a_real_artifact(calibrated, collection):
    """Vectors from somewhere else entirely, judged against a real calibrated
    envelope rather than a hand-written one."""
    out, rows = calibrated
    loaded = artifact_mod.load(out)
    chunk_ids = [r["id"] for r in rows[:16]]
    rng = np.random.default_rng(4242)
    foreign = []
    for _ in chunk_ids:
        v = rng.normal(size=DIM) - 3.0            # displaced from the corpus mean
        foreign.append(v / np.linalg.norm(v))
    verdict = run_ladder(chunk_ids, foreign, dim=DIM, dtype="f32", artifact=loaded,
                         uid_of=_uid_of, seq_of=_seq_of, max_tier=Tier.T1)
    assert verdict.failed, f"foreign vectors passed: {verdict.summary()}"


def test_unsigned_artifact_still_loads_as_advisory(con, collection, tmp_path):
    for native_id in range(1, 9):
        seed_document(con, collection, f"gutenberg:{native_id}", native_id,
                       n_chunks=4, seed=native_id)
    out = tmp_path / "unsigned"
    build_space_artifact.build(collection, out, replicates=100, signing_key=None)
    loaded = artifact_mod.load(out)
    assert loaded.trusted is False
    assert loaded.envelope is not None
