"""The ladder's result object (architecture-v2.md §6: "Verdict object: tier
reached, pass/fail, evidence payload for the ledger").

Three things this has to get right:

  TIER REACHED IS NOT THE SAME AS PASSED. A ladder that stops at T1 because the
  embed endpoint is down is NOT the same as one that ran T3 and passed, and the
  ledger must be able to tell them apart later — otherwise a node cannot re-sweep
  the bundles it admitted at a weaker rung when the endpoint comes back.

  SKIPPED IS NOT PASSED. A tier with no artifact to check against reports
  SKIPPED and says why. Recording that as a pass would quietly overstate how
  much verification a bundle actually received.

  EVIDENCE OUTLIVES THE DECISION. §6's T4 promises "proof of forgery (text,
  claimed, recomputed)". A verdict that records only a boolean makes that an
  accusation rather than something a third party can check, so every tier
  carries its statistics forward.
"""
import json
from dataclasses import asdict, dataclass, field
from enum import IntEnum


class Tier(IntEnum):
    T0 = 0      # algebraic: dim/dtype/norm/NaN/dupes + distribution envelope
    T1 = 1      # geometric: probe profile, overlap band, semantic triangles
    T2 = 2      # challenge: golden triples in the bundle header
    T3 = 3      # spot recompute: sample and re-embed against the real model


class Outcome(str):
    pass


PASSED = "passed"
FAILED = "failed"
SKIPPED = "skipped"      # could not run — no artifact, no endpoint, too few vectors


@dataclass(frozen=True)
class TierResult:
    tier: Tier
    outcome: str
    detail: str
    statistics: dict = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.outcome == PASSED

    @property
    def failed(self) -> bool:
        return self.outcome == FAILED


@dataclass
class Verdict:
    """What the ladder concluded, and on what evidence."""
    results: list[TierResult] = field(default_factory=list)
    space_id: str | None = None
    artifact_version: int | None = None

    def add(self, result: TierResult) -> "Verdict":
        self.results.append(result)
        return self

    @property
    def failed(self) -> bool:
        return any(r.failed for r in self.results)

    @property
    def passed(self) -> bool:
        """No tier failed AND at least one actually ran. An all-skipped ladder
        is not a pass — it is an absence of evidence, and callers that treat it
        as approval are exactly the bug this property exists to prevent."""
        return not self.failed and any(r.passed for r in self.results)

    @property
    def tier_reached(self) -> Tier | None:
        """The highest tier that actually RAN and passed. None if none did."""
        passed = [r.tier for r in self.results if r.passed]
        return max(passed) if passed else None

    @property
    def failures(self) -> list[TierResult]:
        return [r for r in self.results if r.failed]

    def summary(self) -> str:
        return "; ".join(f"{r.tier.name}={r.outcome}" for r in self.results)

    def evidence(self) -> str:
        """Serialized for the bundles ledger. Compact, but complete enough that
        a later reader can re-derive why a bundle was accepted or refused."""
        return json.dumps({
            "space_id": self.space_id,
            "artifact_version": self.artifact_version,
            "tier_reached": int(self.tier_reached) if self.tier_reached is not None else None,
            "passed": self.passed,
            "tiers": [
                {"tier": int(r.tier), "outcome": r.outcome, "detail": r.detail[:400],
                 "statistics": r.statistics}
                for r in self.results
            ],
        }, sort_keys=True, separators=(",", ":"), default=float)[:8000]

    def raise_if_failed(self, exc_type) -> None:
        if self.failed:
            detail = "; ".join(f"{r.tier.name}: {r.detail}" for r in self.failures)
            raise exc_type(f"verification ladder failed — {detail}")
