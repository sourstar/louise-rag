"""Weighted rendezvous hashing — who embeds which shard, with nobody in charge.
docs/04-sharing.md §4.5.

For every uncovered shard, every peer computes the same ranking of the peers
it believes alive. No messages partition the work; only the peer set does,
and that arrives with the catalogue digests. Properties this module is
tested for, because they are what the design promises:

- deterministic: same inputs, same ranking, on every peer;
- alone, a peer takes everything;
- removing a peer reassigns only that peer's shards, to a deterministic
  successor;
- a peer with twice the weight is top on about twice as many shards;
- a full ordering per shard, so a stalled leader has a defined successor.

Pure functions, no I/O.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

from app.hold.codex import canonical_json, digest

DEFAULT_MARGIN = 3.0          # a peer may have other work; give it 3x its own estimate


@dataclass(frozen=True)
class Peer:
    key: str                  # public key, hex
    benchmark: float          # tokens per second on the reference chunks (§3.3)


@dataclass(frozen=True)
class ShardWork:
    #: 04 §4.5: `"<collection_id>/<space>/<slice>/<n>"` — FOUR segments.
    #: `<space>` is 02 §2.4's path segment, and the same string is what an
    #: attestation uses to name a shard (09 §9.8), so a three-segment id
    #: here would not match one there.
    shard_id: str
    tokens: int               # chunks × space.ctx, as the snapshot lists it


def _unit(peer_key: str, shard_id: str) -> float:
    """04 §4.5: `h = (first 64 bits of H(peer_key ‖ shard_id), as an unsigned
    integer, + 0.5) / 2^64`.

    `H(a ‖ b)` is THE concatenation of 09 §9.1 — SHA-256 over the canonical
    JSON of the array `[a, b]`. Not a separator byte, which §9.1 rules out
    in as many words: "There is no other concatenation: no separator bytes,
    no length prefixes, no raw digests joined end to end." Every peer must
    compute the same score from the same digests or the partition is not a
    partition, so this is interop, not taste.
    """
    raw = digest(canonical_json([peer_key, shard_id]))
    x = int(raw.split(":", 1)[1][:16], 16)
    return (x + 0.5) / 2 ** 64


def score(peer: Peer, shard: ShardWork) -> float:
    """04 §4.5: `score = ln(h) / w`, computed in IEEE-754 double; the highest
    wins, and a tie goes to the lower peer key.

    `ln(h)` is negative and `w` positive, so the score is negative and the
    largest (nearest zero) wins — which is the same ordering as `h^(1/w)`,
    written the way the document writes it so the two cannot drift.
    """
    if peer.benchmark <= 0:
        raise ValueError(
            f"{peer.key}: no benchmark for this space, so it is UNSCORED here "
            f"(04 §4.5) — a rate measured on another encoder says nothing "
            f"about this one. An unscored peer is absent from the ordering, "
            f"which `eligible` decides; it is never scored as slow")
    h = _unit(peer.key, shard.shard_id)
    return math.log(h) / peer.benchmark


def expected_seconds(peer: Peer, shard: ShardWork, margin: float = DEFAULT_MARGIN) -> float:
    """04 §4.5: eligible ⇔ `3 × tokens / w ≤ seconds(work.per)`."""
    if peer.benchmark <= 0:
        return math.inf
    return shard.tokens / peer.benchmark * margin


def eligible(peer: Peer, shard: ShardWork, limit_seconds: float,
             margin: float = DEFAULT_MARGIN) -> bool:
    """A peer may take a shard only if it can finish it, with margin, within
    the collection's limit for that shard size. Small shards admit weak
    peers; large shards admit few.

    A peer with NO benchmark for this space is excluded here too, and for a
    different reason: 04 §4.5 calls it unscored rather than slow, "since a
    rate measured on another encoder says nothing about this one". Both end
    up absent from the ordering, which is what the partition needs; only a
    report would care which."""
    return expected_seconds(peer, shard, margin) <= limit_seconds


def ranking(shard: ShardWork, peers: Iterable[Peer], limit_seconds: float,
            margin: float = DEFAULT_MARGIN) -> list[Peer]:
    """All eligible peers for `shard`, best first. Deterministic: ties on
    score (practically impossible) break on the key."""
    elig = [p for p in peers if eligible(p, shard, limit_seconds, margin)]
    return sorted(elig, key=lambda p: (-score(p, shard), p.key))


def rank_of(me: str, ranking: Sequence[Peer]) -> int | None:
    """`me`'s place in a part's ordering, from 1; None when not eligible for
    it. 04 §4.5 (amended 2026-10-20): the ordering is the ORDER a peer works
    through the open parts — first the parts where it is first, then those
    where it is second — never a fence."""
    for i, p in enumerate(ranking, 1):
        if p.key == me:
            return i
    return None


def leader(shard: ShardWork, peers: Iterable[Peer], limit_seconds: float,
           margin: float = DEFAULT_MARGIN) -> Peer | None:
    r = ranking(shard, peers, limit_seconds, margin)
    return r[0] if r else None


def successor(shard: ShardWork, peers: Iterable[Peer], after: str, limit_seconds: float,
              margin: float = DEFAULT_MARGIN) -> Peer | None:
    """The next peer in line after `after` has stalled (§4.5: no bundle within
    grace_days). None if `after` is not in the ranking or is last."""
    r = ranking(shard, peers, limit_seconds, margin)
    keys = [p.key for p in r]
    if after not in keys:
        return None
    i = keys.index(after)
    return r[i + 1] if i + 1 < len(r) else None


def assign(shards: Sequence[ShardWork], peers: Iterable[Peer], limit_seconds: float,
           margin: float = DEFAULT_MARGIN) -> dict[str, str | None]:
    """shard_id → key of the leader (None if no peer is eligible)."""
    peers = list(peers)
    return {s.shard_id: (lambda p: p.key if p else None)(leader(s, peers, limit_seconds, margin))
            for s in shards}


def mine(me: str, shards: Sequence[ShardWork], peers: Iterable[Peer], limit_seconds: float,
         margin: float = DEFAULT_MARGIN) -> list[ShardWork]:
    """The shards this peer should work on now: the ones it leads. A peer
    that has finished its own picks among the least-covered shards instead
    (the tie-break of §4.5); that is the caller's business, with the
    coverage map."""
    peers = list(peers)
    return [s for s in shards if (l := leader(s, peers, limit_seconds, margin)) and l.key == me]


def fetch_leader(collection_id: str, snapshot_id: str, peers: Iterable[Peer]) -> Peer | None:
    """Who fetches the origin's delta at the cutoff (§4.5, last paragraph):
    the same function over a synthetic key, with no size limit — every
    peer can fetch."""
    key = ShardWork(f"{collection_id}/{snapshot_id}/fetch", tokens=0)
    return leader(key, peers, limit_seconds=math.inf)


def share_of_leadership(peers: Sequence[Peer], shards: Sequence[ShardWork],
                        limit_seconds: float = math.inf) -> Mapping[str, float]:
    """Fraction of shards each peer leads — the instrument behind the
    'twice the weight, twice the shards' property."""
    counts = {p.key: 0 for p in peers}
    for s in shards:
        l = leader(s, peers, limit_seconds)
        if l:
            counts[l.key] += 1
    n = max(1, len(shards))
    return {k: v / n for k, v in counts.items()}
