"""The verification ladder (architecture-v2.md §6), as a library.

Callable from the bundle importer today and from the coordinator later, which
is why nothing here touches SQLite, the manifest, or the trust store: it takes
vectors and an artifact, and returns a Verdict.

    T0  algebraic   dim/dtype/norm/NaN/dupes, per-dim moment envelope,
                    stratified pairwise-cosine distribution
    T1  geometric   Gram realizability gate, probe-coordinate profile,
                    measured-overlap band
    T2  challenge   beacon-selected golden triples in the signed header
    T3  recompute   OS-entropy sample, re-embedded, truncated SPRT

THREE RULES THE CALLER MUST NOT LOSE:

  SKIPPED IS NOT PASSED. A tier with no artifact, no endpoint or too few
  vectors reports SKIPPED and says why. Verdict.passed is False when NOTHING
  ran, so an all-offline ladder can never be mistaken for approval.

  TIER REACHED IS THE DEGRADATION SIGNAL. A bundle admitted with the embed
  endpoint down records tier_reached=2, and a later sweep can re-run T3 when
  the endpoint returns. That is expressed as a tier, never as a pass/fail flip.

  ALPHA BELONGS TO THE NODE, NOT THE ARTIFACT. It is a false-reject budget the
  operator is willing to pay. An artifact that could set it could turn
  verification off by asking for alpha=1.
"""
import numpy as np

from . import t0, t1, t2, t3
from .artifact import ArtifactError, SpaceArtifact, find, load
from .verdict import FAILED, PASSED, SKIPPED, Tier, TierResult, Verdict

# Family-wise false-reject budget, split across the statistical sub-tests by the
# union bound so the total per-bundle rate stays under it. A node-level risk
# appetite, deliberately not a per-space number.
DEFAULT_ALPHA_FAMILY = 1e-3
_ALPHA_SHARE = {
    "t0_moment": 0.2,
    "t0_pairs": 0.2,
    "t1_probe": 0.3,
    "t1_overlap": 0.1,
    "t3_sprt": 0.2,
}

__all__ = ["run_ladder", "Verdict", "TierResult", "Tier", "SpaceArtifact",
           "ArtifactError", "load", "find", "PASSED", "FAILED", "SKIPPED",
           "DEFAULT_ALPHA_FAMILY"]


def run_ladder(chunk_ids, vectors, *, dim: int, dtype: str, artifact=None,
               header=None, texts=None, embed_fn=None, uid_of=None, seq_of=None,
               corpus_tag: str | None = None, chunking_profile: str | None = None,
               alpha_family: float = DEFAULT_ALPHA_FAMILY,
               max_tier: Tier = Tier.T3, rng=None, now=None) -> Verdict:
    """Run the ladder, cheapest tier first, stopping at the first FAILURE.

    Each tier gates the next (§6: "Five tiers, cheapest first, each gating the
    next"), so a bundle that fails T0 never costs a GPU call.
    """
    generator = rng if rng is not None else np.random.default_rng()
    verdict = Verdict(space_id=getattr(artifact, "space_id", None),
                      artifact_version=getattr(artifact, "version", None))
    n_docs = len({uid_of(c) for c in chunk_ids}) if uid_of else 1

    def share(name: str) -> float:
        return alpha_family * _ALPHA_SHARE[name]

    # ── T0 ──────────────────────────────────────────────────────────────
    structural = t0.check_structural(chunk_ids, vectors, dim, dtype)
    verdict.add(structural)
    if structural.failed:
        return verdict

    dead = t0.check_dead_dimensions(vectors, artifact)
    if dead is not None:
        verdict.add(dead)
        if dead.failed:
            return verdict

    verdict.add(t0.check_moment_envelope(vectors, artifact, n_docs=n_docs,
                                          alpha=share("t0_moment")))
    if uid_of is not None:
        verdict.add(t0.check_pair_cosines(chunk_ids, vectors, artifact, uid_of,
                                           n_docs=n_docs, alpha=share("t0_pairs"),
                                           rng=generator))
    if verdict.failed or max_tier < Tier.T1:
        return verdict

    # ── T1 ──────────────────────────────────────────────────────────────
    verdict.add(t1.check_gram_gate(chunk_ids, vectors, artifact, dtype, generator))
    verdict.add(t1.check_probe_profile(vectors, artifact, corpus_tag=corpus_tag,
                                        chunking_profile=chunking_profile,
                                        n_docs=n_docs, alpha=share("t1_probe")))
    if uid_of is not None and seq_of is not None:
        verdict.add(t1.check_overlap_band(chunk_ids, vectors, artifact, texts=texts,
                                           uid_of=uid_of, seq_of=seq_of,
                                           n_docs=n_docs, alpha=share("t1_overlap")))
    if verdict.failed or max_tier < Tier.T2:
        return verdict

    # ── T2 ──────────────────────────────────────────────────────────────
    if header is not None:
        verdict.add(t2.check_challenges(header, artifact, now=now))
    if verdict.failed or max_tier < Tier.T3:
        return verdict

    # ── T3 ──────────────────────────────────────────────────────────────
    verdict.add(t3.check_spot_recompute(
        chunk_ids, vectors, artifact, texts=texts, embed_fn=embed_fn,
        backend_fingerprint=getattr(header, "backend_fingerprint", ""),
        dtype=dtype, alpha=share("t3_sprt"), rng=rng))
    return verdict
