"""T3 — spot recompute. The only tier that calls the real model.

architecture-v2.md §6: "n~16 chunks, private per-verifier randomness (never a
bundle-digest seed — that is grindable ...), sequential test, tau from the
space's cross-backend calibration table".

SAMPLING MUST COME FROM OS ENTROPY. §6 spells out the attack on a content-derived
seed: corrupt a fraction f, then re-roll the nonce until the derived sample
misses the corrupted chunks, at an expected cost of (1-f)^-n tries. So the
sample is drawn from secrets.SystemRandom by default; the seeded path exists
only so tests can be deterministic, and a test asserts the default is not.

THE SEQUENTIAL TEST, AND AN HONEST STATEMENT OF WHAT IT DELIVERS.
Truncated Wald SPRT on miss indicators x_i = 1[cos_i < tau]:

    Lambda_m = S_m*ln(f1/f0) + (m - S_m)*ln((1-f1)/(1-f0))
    A = ln((1-beta)/alpha)   -> reject (bundle FAILS)
    B = ln(beta/(1-alpha))   -> accept (bundle PASSES)
    at n_max: FAIL iff Lambda > 0

Wald's A and B IGNORE OVERSHOOT AND TRUNCATION, so the designed alpha/beta are
not the delivered ones. `delivered_error_rates` computes the true values by
exact enumeration over the (m, S_m) lattice, and the verdict records those
rather than the design targets.

In particular, at §6's own n~16 against f=5%, the achievable beta is
0.95^16 = 0.44 — n is a BUDGET, not an acceptance boundary. §6's safety
argument depends on that number: 0.44^10 = 2.7e-4 is exactly its "10 independent
importers at f=5% -> 3x10^-4". The strength comes from independent importers,
not from one importer's beta, and this module must not claim otherwise.
"""
import math
import secrets

import numpy as np

from .verdict import FAILED, PASSED, SKIPPED, Tier, TierResult

DEFAULT_N = 16
DEFAULT_N_MAX = 64
DEFAULT_ALPHA = 1e-2
DEFAULT_BETA = 0.05
COARSE_TAU = 0.95        # fallback only; see tau_source in the verdict


def choose_sample(chunk_ids: list[str], size: int, rng=None) -> list[str]:
    """Uniform without replacement, from OS entropy by default.

    Uniform over CHUNKS (not stratified by document) is what makes §6's
    (1-f)^n arithmetic true. Targeted single-document poisoning is explicitly
    T4's job, not this one's.
    """
    source = rng if rng is not None else secrets.SystemRandom()
    if len(chunk_ids) <= size:
        return list(chunk_ids)
    return source.sample(list(chunk_ids), size)


def detection_probability(total: int, corrupt_fraction: float, sample: int,
                          *, per_chunk_power: float = 1.0,
                          honest_miss_rate: float = 0.0) -> float:
    """P(at least one miss) under reject-on-first-miss.

        p_miss    = f*q + (1-f)*f0
        P(reject) = 1 - (1 - p_miss)^n

    §6's (1-f)^n is the special case q=1, f0=0.
    """
    if total <= 0 or sample <= 0:
        return 0.0
    n = min(sample, total)
    p_miss = corrupt_fraction * per_chunk_power + (1 - corrupt_fraction) * honest_miss_rate
    p_miss = min(max(p_miss, 0.0), 1.0)
    return 1.0 - (1.0 - p_miss) ** n


def hypergeometric_detection(total: int, corrupt: int, sample: int) -> float:
    """Exact, for sampling without replacement — what the sampler actually does."""
    if total <= 0 or corrupt <= 0 or sample <= 0:
        return 0.0
    n = min(sample, total)
    miss = 1.0
    for i in range(n):
        remaining = total - i
        if remaining <= 0:
            break
        clean = total - corrupt - i
        if clean <= 0:
            return 1.0
        miss *= clean / remaining
    return 1.0 - miss


class Sprt:
    """Truncated Wald SPRT over miss indicators."""

    def __init__(self, f0: float, f1: float, alpha: float, beta: float, n_max: int):
        self.f0 = min(max(f0, 1e-9), 0.5)
        self.f1 = min(max(f1, self.f0 * 1.0001), 0.999)
        self.alpha, self.beta, self.n_max = alpha, beta, n_max
        self.upper = math.log((1 - beta) / alpha)
        self.lower = math.log(beta / (1 - alpha))
        self._miss_weight = math.log(self.f1 / self.f0)
        self._hit_weight = math.log((1 - self.f1) / (1 - self.f0))

    def statistic(self, misses: int, observed: int) -> float:
        return misses * self._miss_weight + (observed - misses) * self._hit_weight

    def decide(self, misses: int, observed: int) -> str | None:
        """'fail' | 'pass' | None (continue)."""
        value = self.statistic(misses, observed)
        if value >= self.upper:
            return "fail"
        if value <= self.lower:
            return "pass"
        if observed >= self.n_max:
            return "fail" if value > 0 else "pass"
        return None

    def delivered_error_rates(self, p_miss: float) -> float:
        """P(this test rejects) at a true miss rate p_miss, by exact enumeration
        over the (m, S_m) lattice with absorbing boundaries.

        This is the only honest way to state the achieved rates: Wald's A and B
        ignore overshoot and truncation, so the delivered alpha/beta differ from
        the designed ones — sometimes substantially at small n_max.
        """
        states = {0: 1.0}          # misses -> probability of being here, still running
        reject = 0.0
        for observed in range(1, self.n_max + 1):
            nxt: dict[int, float] = {}
            for misses, probability in states.items():
                for outcome, weight in ((1, p_miss), (0, 1 - p_miss)):
                    if weight <= 0:
                        continue
                    total_misses = misses + outcome
                    verdict = self.decide(total_misses, observed)
                    if verdict == "fail":
                        reject += probability * weight
                    elif verdict is None:
                        nxt[total_misses] = nxt.get(total_misses, 0.0) + probability * weight
            states = nxt
            if not states:
                break
        return reject


def _tau_for(artifact, backend_fingerprint: str, dtype: str) -> tuple[float, float, float, str]:
    """(tau, f0, f1, source). Falls back to a coarse floor when no artifact is
    available, and LABELS it so a degraded pass is never mistaken for a clean
    one.

    TIGHTEN-ONLY FOR UNTRUSTED ARTIFACTS. An artifact defines what "honest"
    means, so an unsigned or unrecognised one is advisory: it may make this
    check STRICTER than the built-in coarse floor, never looser. Concretely a
    hostile artifact publishing tau=0.1, f0=0.5 or f1=1e-9 would otherwise
    wave anything through — so for untrusted artifacts tau is floored at the
    coarse gate, f0 capped at the coarse honest-miss rate, and f1 floored at
    the coarse detection target. A TRUSTED author (platform.toml
    [trust] space_authors, signature verified) may set whatever its
    measurements support, including tolerances below the coarse floor for a
    genuinely noisy backend pair.
    """
    if artifact is None:
        return COARSE_TAU, 1e-4, 0.05, "coarse-fallback"
    calibration = artifact.calibration()
    f1 = float((calibration.get("detect_target") or {}).get("f1", 0.05))
    tau, f0, source = None, 1e-4, "tau_default"
    for pair in calibration.get("pairs") or []:
        if pair.get("producer_class") and pair["producer_class"] in (backend_fingerprint or ""):
            penalty = float((pair.get("storage_dtype_penalty") or {}).get(dtype, 0.0))
            tau = float(pair.get("tau", 0.995)) - penalty
            f0 = float(pair.get("p_miss_honest", 1e-4))
            source = "calibration_pair"
            break
    if tau is None:
        tau = float(calibration.get("tau_default", 0.995))
    if not getattr(artifact, "trusted", False):
        clamped_tau = max(tau, COARSE_TAU)
        clamped_f0 = min(f0, 1e-4)
        clamped_f1 = max(f1, 0.05)
        if (clamped_tau, clamped_f0, clamped_f1) != (tau, f0, f1):
            source += "+advisory-floor"
        tau, f0, f1 = clamped_tau, clamped_f0, clamped_f1
    return tau, f0, f1, source


def check_spot_recompute(chunk_ids, vectors, artifact, *, texts, embed_fn,
                         backend_fingerprint: str = "", dtype: str = "f32",
                         n: int = DEFAULT_N, n_max: int = DEFAULT_N_MAX,
                         alpha: float = DEFAULT_ALPHA, beta: float = DEFAULT_BETA,
                         rng=None, batch: int = 8, inputs=None) -> TierResult:
    # `inputs`: what each chunk is fed to the encoder as where that is not its
    # text — a pair layer's chunk with its `fields`, re-cut from the record
    # (09 §9.3). Absent, the text, as ever.
    if embed_fn is None:
        return TierResult(Tier.T3, SKIPPED, "no embed endpoint available")
    if not texts:
        return TierResult(Tier.T3, SKIPPED, "no local text to re-embed")
    fed = inputs if inputs is not None else texts

    available = [c for c in chunk_ids if c in fed]
    if not available:
        return TierResult(Tier.T3, SKIPPED, "no sampled chunk has local text")

    tau, f0, f1, source = _tau_for(artifact, backend_fingerprint, dtype)
    sprt = Sprt(f0, f1, alpha, beta, min(n_max, len(available)))
    by_id = {cid: np.asarray(v, dtype=np.float64)
             for cid, v in zip(chunk_ids, vectors)}

    order = choose_sample(available, min(n_max, len(available)), rng=rng)
    misses, observed, evidence = 0, 0, []
    decision = None
    for start in range(0, len(order), batch):
        window = order[start:start + batch]
        try:
            recomputed = embed_fn([fed[c] for c in window])
        except Exception as e:
            return TierResult(Tier.T3, SKIPPED,
                              f"embed endpoint unavailable ({type(e).__name__}) — "
                              f"could not run, which is not the same as failing")
        for cid, fresh_raw in zip(window, recomputed):
            fresh = np.asarray(fresh_raw, dtype=np.float64)
            claimed = by_id[cid]
            denominator = float(np.linalg.norm(fresh) * np.linalg.norm(claimed))
            cosine = 0.0 if denominator == 0 else float(np.dot(fresh, claimed) / denominator)
            observed += 1
            if cosine < tau:
                misses += 1
                evidence.append({"chunk_id": cid, "cos": cosine, "tau": tau})
            decision = sprt.decide(misses, observed)
            if decision is not None:
                break
        if decision is not None:
            break
    if decision is None:
        decision = "fail" if sprt.statistic(misses, observed) > 0 else "pass"

    p_miss = f1 * 1.0 + (1 - f1) * f0
    stats = {
        "n_used": observed, "n_target": n, "n_max": sprt.n_max,
        "misses": misses, "tau": tau, "tau_source": source,
        "f0": f0, "f1": f1, "alpha_design": alpha, "beta_design": beta,
        # Delivered, not designed — see the module docstring.
        "alpha_delivered": sprt.delivered_error_rates(f0),
        "beta_delivered": 1.0 - sprt.delivered_error_rates(p_miss),
        "detect_at_f1": detection_probability(len(chunk_ids), f1, observed,
                                              honest_miss_rate=f0),
        "failures": evidence[:16],
    }
    if decision == "fail":
        return TierResult(
            Tier.T3, FAILED,
            f"{misses} of {observed} recomputed chunks did not reproduce "
            f"(tau={tau:.6f}, source={source}) — worst "
            f"{min((e['cos'] for e in evidence), default=0.0):.4f}", stats)
    return TierResult(Tier.T3, PASSED,
                      f"{observed} sampled chunks reproduced against the local model "
                      f"(tau={tau:.6f}, source={source})", stats)
