"""T2 — challenge triples. Zero embed calls, zero GPU, checkable by anyone.

architecture-v2.md §6: "golden (text, vector, per-backend tolerance) triples;
producer embeds beacon-derived challenge texts into the bundle header ...
misconfiguration — 'proof of correct oven, not each loaf'."

WHAT THIS CATCHES THAT T0 AND T1 CANNOT. T0 and T1 examine the bundle's INTERNAL
GEOMETRY against the space's aggregate statistics, and both are invariant to a
systematic, geometry-preserving displacement of the whole bundle: the right
model at a different quantization, a different pooling mode, normalization
applied at the wrong point, a different truncation ctx. Those produce real
embeddings of real texts, with correct anisotropy, a correct overlap band and a
plausible probe profile — sitting in a systematically wrong place. T0/T1 are
blind to all of it. T2 tests the bundle's ABSOLUTE REGISTRATION to the space, by
comparing against known vectors for known texts.

THE BEACON, AND ITS HONEST LIMIT. The challenge indices are derived from the
producer's declared creation time:

    epoch   = created_at // epoch_seconds
    beacon  = HMAC-SHA256(root, b"louise-rag/beacon/v1" || u64be(epoch))
    index_t = HMAC-SHA256(beacon, b"sel" || u32be(t)) mod bank_size

`root` is published inside the SIGNED artifact, so verification is arithmetic:
no network, no state, works offline, and a coordinator with no GPU can run it on
upload.

Because `root` is public, challenges are predictable forever, and a determined
producer can pre-compute the whole bank once and pass T2 while embedding the
actual bundle with garbage. THIS IS THEREFORE NOT A SECURITY CONTROL — §6 says
as much ("proof of correct oven, not each loaf") and a T2 pass must never
substitute for T3.

Do not "harden" this by GENERATING challenge texts from the beacon. That makes
them un-cacheable, but then the author cannot pre-publish golden vectors, the
verifier has to embed the challenges itself, and T2's zero cost becomes a
T3-shaped GPU call. Hardening T2 turns it into T3.
"""
import hashlib
import hmac
import struct
import time

import numpy as np

from .verdict import FAILED, PASSED, SKIPPED, Tier, TierResult

_BEACON_DOMAIN = b"louise-rag/beacon/v1"


def challenge_indices(root: bytes, epoch: int, count: int, bank_size: int) -> list[int]:
    beacon = hmac.new(root, _BEACON_DOMAIN + struct.pack(">Q", epoch),
                      hashlib.sha256).digest()
    out, t = [], 0
    while len(out) < count and t < count * 64:
        draw = hmac.new(beacon, b"sel" + struct.pack(">I", t), hashlib.sha256).digest()
        index = int.from_bytes(draw[:8], "big") % bank_size
        if index not in out:
            out.append(index)
        t += 1
    return out


def check_challenges(header, artifact, *, now: float | None = None) -> TierResult:
    """Validate the challenge block a producer embedded in the signed header."""
    if artifact is None or artifact.goldens is None:
        return TierResult(Tier.T2, SKIPPED, "no space artifact: no golden bank")
    block = artifact.goldens_block()
    beacon_cfg = block.get("beacon") or {}
    root_hex = beacon_cfg.get("root")
    if not root_hex:
        return TierResult(Tier.T2, SKIPPED, "artifact publishes no beacon root")

    claimed = getattr(header, "challenge", None)
    if not claimed:
        # A v1 bundle carries no challenge payload. Skipped, never passed:
        # recording an absent check as a pass would overstate verification.
        return TierResult(Tier.T2, SKIPPED,
                          "bundle carries no challenge block (format v1)")

    now = time.time() if now is None else now
    created_at = int(getattr(header, "created_at", 0))
    max_age = int(beacon_cfg.get("max_bundle_age_seconds", 30 * 24 * 3600))
    max_skew = int(beacon_cfg.get("max_clock_skew_seconds", 900))
    # Checked BEFORE the epoch is derived: created_at is producer-controlled, so
    # without this "beacon-derived" degrades to "producer-derived".
    if created_at > now + max_skew:
        return TierResult(Tier.T2, FAILED,
                          f"bundle claims a creation time {int(created_at - now)}s in "
                          f"the future", {"created_at": created_at})
    if now - created_at > max_age:
        return TierResult(Tier.T2, FAILED,
                          f"bundle is {int((now - created_at) / 86400)} days old, past "
                          f"the challenge freshness window", {"created_at": created_at})

    epoch_seconds = max(1, int(beacon_cfg.get("epoch_seconds", 3600)))
    count = int(beacon_cfg.get("challenge_count", 8))
    bank_size = int(block.get("bank_size", artifact.goldens.shape[0]))
    expected = challenge_indices(bytes.fromhex(root_hex), created_at // epoch_seconds,
                                 count, bank_size)

    indices = claimed.get("indices") if isinstance(claimed, dict) else None
    vectors = claimed.get("vectors") if isinstance(claimed, dict) else None
    if not isinstance(indices, list) or not isinstance(vectors, list):
        return TierResult(Tier.T2, FAILED, "challenge block is malformed")
    if list(indices) != expected:
        return TierResult(
            Tier.T2, FAILED,
            f"challenge indices do not match the beacon for this epoch — the "
            f"producer answered a different question than it was asked",
            {"expected": expected[:8], "claimed": list(indices)[:8]})
    if len(vectors) != len(expected):
        return TierResult(Tier.T2, FAILED, "challenge vector count does not match")

    tau, source = _tau_for(artifact, getattr(header, "backend_fingerprint", ""),
                           getattr(header, "dtype", "f32"))
    goldens = np.asarray(artifact.goldens, dtype=np.float64)
    evidence, worst = [], 1.0
    for index, claimed_vec in zip(expected, vectors):
        golden = goldens[index]
        candidate = np.asarray(claimed_vec, dtype=np.float64)
        if candidate.shape != golden.shape:
            return TierResult(Tier.T2, FAILED, "challenge vector has the wrong width")
        denominator = float(np.linalg.norm(candidate) * np.linalg.norm(golden))
        cosine = 0.0 if denominator == 0 else float(np.dot(candidate, golden) / denominator)
        worst = min(worst, cosine)
        evidence.append({"index": int(index), "cos": cosine,
                         "exact": bool(cosine >= 1.0 - 1e-12)})

    stats = {"tau": tau, "tau_source": source, "worst_cos": worst,
             "challenges": evidence}
    # An exact match means the producer COPIED the vector out of the public
    # artifact rather than computing it. A careful cheater adds noise, so this
    # is a reputation signal recorded in the verdict, not a gate.
    stats["exact_matches"] = sum(1 for e in evidence if e["exact"])

    if worst < tau:
        return TierResult(
            Tier.T2, FAILED,
            f"challenge vectors do not match the space's goldens "
            f"(worst cos {worst:.6f} < tau {tau:.6f}) — the producer's embedding "
            f"setup is displaced from this space: wrong quantization, pooling, "
            f"normalization point or truncation", stats)
    return TierResult(Tier.T2, PASSED,
                      f"{len(evidence)} beacon-selected challenges match the space's "
                      f"goldens (worst cos {worst:.6f})", stats)


def _tau_for(artifact, backend_fingerprint: str, dtype: str) -> tuple[float, str]:
    """Tolerance for this producer/verifier backend pair, widened by the
    storage-dtype penalty. f16 storage perturbs cosine by ~4e-4, which is the
    same order as tau's distance from 1.0 — without the penalty every honest
    f16 bundle false-rejects."""
    calibration = artifact.calibration()
    default = float(calibration.get("tau_default", 0.995))
    tau, source = None, "tau_default"
    for pair in calibration.get("pairs") or []:
        globs = pair.get("producer_class", "")
        if globs and globs in (backend_fingerprint or ""):
            penalty = float((pair.get("storage_dtype_penalty") or {}).get(dtype, 0.0))
            tau, source = float(pair.get("tau", default)) - penalty, "calibration_pair"
            break
    if tau is None:
        penalty = float((calibration.get("storage_dtype_penalty") or {}).get(dtype, 0.0))
        tau = default - penalty
    # Tighten-only for untrusted artifacts (same rule as t3._tau_for): an
    # advisory artifact publishing a permissive tolerance must not be able to
    # wave a wrong-oven producer through the challenge check.
    if not getattr(artifact, "trusted", False) and tau < 0.95:
        tau, source = 0.95, source + "+advisory-floor"
    return tau, source
