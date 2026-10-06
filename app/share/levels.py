"""Trust levels (03 §3.6; 09 §9.3 `trust_level`; 05 §5.3) — what strangers
found, computed LOCALLY from the attestations this peer holds and the
bundles it has accepted. Migration step 9e.

- An attestation counts toward a level only for the snapshot it names
  and for at most two cadences from its `time`. Independent attestations
  come from distinct keys and, for a sampled check, carry distinct seeds;
  a null seed (an exhaustive or deterministic check) is not compared. A
  key is counted once. The trust list is a filter over whose attestations
  are counted — a denied or burned key's are not — never a weight; an
  empty list counts everyone.
- `unverified`: only the publisher's word. `checked`: at least three
  independent keys passed `roots`, `chunks` and `calibration`; at least
  one passed `records-vs-origin`; and for every producer whose bundles
  the collection has here, a key other than that producer passed
  `vectors-sample` on its vectors. `verified`: at least eight independent
  keys, and the union of their samples covers a quarter of shards for
  `records-vs-origin` and a tenth for `vectors-sample`. `flagged`: a
  deterministic check failed under an image whose verdicts count (the
  Codex's, or one proven equivalent, 03 §3.4) — one reproduction is
  enough; or a statistical check failed twice, from independent verifiers,
  the second on the chunk ids the first named.
- Thresholds are advisory (keys are free, 08 §8.1): they say how many
  strangers said the same thing. What protects a peer is its own checks —
  quarantine (app/copy_state.py) comes before any of this.
"""
from __future__ import annotations

import time
from typing import Any, Iterable

from app.hold import snapshot as sn
from app.share import coverage as cov, messages

UNVERIFIED, CHECKED, VERIFIED, FLAGGED = "unverified", "checked", "verified", "flagged"
CHECKED_KEYS = 3
VERIFIED_KEYS = 8
ORIGIN_SHARE, SAMPLE_SHARE = 0.25, 0.10


def cadence_seconds(codex) -> float:
    return cov.period_seconds({"monthly": "P1M", "weekly": "P1W"}.get(codex.update.cadence, "P1M"))


def counting(codex, snapshot: sn.Snapshot | None, attestations: Iterable[dict[str, Any]], *,
             now: float | None = None, trust: dict[str, set[str]] | None = None) -> list[dict[str, Any]]:
    """The attestations that count for this snapshot now: its own, within
    two cadences, passed or failed, from keys the trust list does not shut
    out."""
    now = time.time() if now is None else now
    if snapshot is None:
        return []
    window = 2 * cadence_seconds(codex)
    shut = set((trust or {}).get("deny", ())) | set((trust or {}).get("burn", ()))
    out = []
    for b in attestations:
        if b.get("collection_id") != codex.collection_id or b.get("snapshot_hash") != snapshot.hash:
            continue
        if b.get("result") not in ("pass", "fail") or b.get("key") in shut:
            continue
        try:
            t = messages.parse_time(b.get("time"))
        except Exception:   # noqa: BLE001 — a time that is not one counts for nothing
            continue
        if t < now - window or t > now + 3600:
            continue
        out.append(b)
    return out


def independent(bodies: Iterable[dict[str, Any]], check: str, result: str = "pass") -> list[dict[str, Any]]:
    """One attestation per key for `check` with `result`; for a sampled
    check, one per (key, seed) too — the same key with the same seed said
    the same thing twice."""
    seen_keys: set[str] = set()
    seen_seeds: set[str] = set()
    out = []
    for b in sorted((b for b in bodies if b.get("check") == check and b.get("result") == result),
                    key=lambda b: str(b.get("time") or "")):
        key, seed = str(b.get("key")), b.get("seed")
        if key in seen_keys or (seed is not None and seed in seen_seeds):
            continue
        seen_keys.add(key)
        if seed is not None:
            seen_seeds.add(str(seed))
        out.append(b)
    return out


def claims(codex, bodies: list[dict[str, Any]], *, reference_images: set[str],
           own_key: str | None = None) -> list[dict[str, Any]]:
    """Another key's deterministic failures under an image whose verdicts
    count — each a CLAIM (03 §3.6: acted on after local reproduction, shown
    as a claim meanwhile) — one per (check, shard, key)."""
    out: dict[tuple, dict[str, Any]] = {}
    for b in bodies:
        if b.get("check") not in cov.DETERMINISTIC or b.get("result") != "fail":
            continue
        if str(b.get("platform_image") or "") not in reference_images or b.get("key") == own_key:
            continue
        for shard in (b.get("shards") or [None]):
            out.setdefault((b["check"], shard, b.get("key")), {
                "check": b["check"], "shard": shard, "key": b.get("key"), "time": b.get("time")})
    return [out[k] for k in sorted(out, key=lambda k: (k[0], str(k[1]), str(k[2])))]


def flag(codex, bodies: list[dict[str, Any]], *, reference_images: set[str],
         own_key: str | None = None) -> str | None:
    """Why the snapshot is flagged, or None (03 §3.6): this peer's own
    deterministic failure under an image whose verdicts count — one
    reproduction is enough; two distinct keys failing the same shard with
    a deterministic check; or two independent verifiers failing a
    statistical check on the same chunks. A stranger's lone deterministic
    failure is a claim (`claims`), not a flag."""
    for b in bodies:
        if b.get("check") in cov.DETERMINISTIC and b.get("result") == "fail" \
                and str(b.get("platform_image") or "") in reference_images \
                and own_key is not None and b.get("key") == own_key:
            shards = b.get("shards") or []
            return (f"this peer's own {b['check']} failed under an image whose verdicts count"
                    + (f" on {shards[0]}" if shards else ""))
    by_shard: dict[tuple, set[str]] = {}
    for c in claims(codex, bodies, reference_images=reference_images, own_key=own_key):
        by_shard.setdefault((c["check"], c["shard"]), set()).add(str(c["key"]))
    for (check, shard), keys in sorted(by_shard.items(), key=lambda kv: (kv[0][0], str(kv[0][1]))):
        if len(keys) >= 2:
            return (f"{check} failed for two keys on the same shard"
                    + (f" ({shard})" if shard else "") + f": {', '.join(k[:23] + '…' for k in sorted(keys))}")
    fails = [b for b in bodies if b.get("check") in cov.STATISTICAL and b.get("result") == "fail"]
    for first in fails:
        named = set((first.get("detail") or {}).get("failed") or [])
        if not named:
            continue
        for second in fails:
            if second is first or second.get("key") == first.get("key"):
                continue
            if second.get("seed") is not None and second.get("seed") == first.get("seed"):
                continue
            again = set((second.get("detail") or {}).get("failed") or [])
            if again and again <= named:
                return (f"{first['check']} failed twice on the same chunks, by "
                        f"{str(first.get('key'))[:23]}… and {str(second.get('key'))[:23]}…")
    return None


def _shard_share(bodies: list[dict[str, Any]], check: str, total: int) -> float:
    named: set[str] = set()
    for b in independent(bodies, check):
        named.update(b.get("shards") or [])
    return len(named) / total if total else 0.0


def level(codex, snapshot: sn.Snapshot | None, attestations: Iterable[dict[str, Any]],
          rows: Iterable[dict[str, Any]] = (), *, now: float | None = None,
          trust: dict[str, set[str]] | None = None,
          reference_images: set[str] | None = None, own_key: str | None = None) -> dict[str, Any]:
    """`{level, why, keys, passed: {check: n}, coverage: {check: share},
    claims: [...]}` — the claims being strangers' deterministic failures
    not yet reproduced here nor seconded."""
    now = time.time() if now is None else now
    bodies = counting(codex, snapshot, attestations, now=now, trust=trust)
    images = set(reference_images) if reference_images is not None else {codex.toolchain.platform_image}
    passed = {c: independent(bodies, c) for c in ("roots", "chunks", "calibration", "records-vs-origin",
                                                   "vectors-sample")}
    keys = {str(b.get("key")) for c in passed for b in passed[c]}
    total = len(snapshot.shards) if snapshot is not None else 0
    coverage = {"records-vs-origin": _shard_share(bodies, "records-vs-origin", total),
                "vectors-sample": _shard_share(bodies, "vectors-sample", total)}
    out: dict[str, Any] = {"keys": len(keys), "passed": {c: len(v) for c, v in passed.items()},
                           "coverage": coverage, "why": [],
                           "claims": claims(codex, bodies, reference_images=images, own_key=own_key)}
    flagged = flag(codex, bodies, reference_images=images, own_key=own_key)
    if flagged:
        out.update(level=FLAGGED, why=[flagged])
        return out
    why = []
    for c in ("roots", "chunks", "calibration"):
        if len(passed[c]) < CHECKED_KEYS:
            why.append(f"{c}: {len(passed[c])} of {CHECKED_KEYS} independent keys")
    if not passed["records-vs-origin"]:
        why.append("records-vs-origin: no key has passed it on a sample")
    producers = {r["producer"] for r in rows if r.get("accepted_at") and r.get("producer")}
    for p in sorted(producers):
        if not any(p in ((b.get("detail") or {}).get("producers") or []) and b.get("key") != p
                   for b in passed["vectors-sample"]):
            why.append(f"vectors-sample: nobody but {p[:23]}… has checked its vectors")
    if why:
        out.update(level=UNVERIFIED, why=why)
        return out
    more = []
    if len(keys) < VERIFIED_KEYS:
        more.append(f"{len(keys)} of {VERIFIED_KEYS} independent keys")
    if coverage["records-vs-origin"] < ORIGIN_SHARE:
        more.append(f"records-vs-origin samples cover {coverage['records-vs-origin']:.0%} of shards, "
                    f"not a quarter")
    if coverage["vectors-sample"] < SAMPLE_SHARE:
        more.append(f"vectors-sample covers {coverage['vectors-sample']:.0%} of shards, not a tenth")
    out.update(level=CHECKED if more else VERIFIED, why=more)
    return out
