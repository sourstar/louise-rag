# The live edge: running ahead of a frozen pack, and contributing the work

*Proposal, 2026-08-28. For review — nothing here is built.*

A pack is **closed**: `gutenberg-2026-06` holds what the origin held on
2026-06-30 and never changes. Updates arrive on a cadence — say monthly,
frozen and published at a deadline. That leaves a real gap for a peer that
has an origin to fetch from:

> Between the freeze and the next publication, the origin keeps producing.
> A peer that only consumes packs is a month stale. A peer that fetches the
> origin itself is current — but its work is invisible to everyone, and when
> the official update lands it looks like a stranger's data.

This proposes the **live edge**: the window between a pack's cutoff and now,
fetched and processed locally, structured so that the official update
*absorbs* it rather than colliding with it — and so the embeddings computed
in it (a lazy embed, a background backfill) become a contribution instead of
being thrown away.

## 1. What already makes this possible

Nothing here needs a new trust model, because §6.1 already says an update is
a **pure function of (previous manifest, origin window, cutoff)**:

> Two honest updaters with the same origin window therefore produce
> BYTE-IDENTICAL shards and the same delta.

The live edge is that same derivation, run early and locally, on a window
whose right-hand end has not been fixed yet. If the derivation is a
function, a peer running ahead is computing a *prefix* of what the official
update will contain — not a private variant of it.

Two existing facts do the heavy lifting:

- **`content_hash` identifies a record's text** independently of who parsed
  it, when, or into which shard.
- **A chunk id is `{uid}:{seq}:{version}`** — derived from the document and
  the chunker, and carrying no shard, no snapshot, and no publisher.

So a vector computed locally on a live-edge document is valid for the
official record **exactly when the two content hashes agree**, and that is
checkable with no coordination at all.

## 2. The one thing a peer must NOT do: guess shard boundaries

The tempting design is to have the peer produce shards immediately, numbered
after the last shard in its manifest, so they slot into the update.

That does not work, and it is worth being precise about why. A shard closes
at ~7,000 chunks. The peer's window ends *now*; the official window ends at
the *freeze*. The peer therefore reaches the boundary at a different record,
so its shard 1592 and the official shard 1592 contain different sets. Two
honest peers with different windows produce different shards — which is
exactly the divergence §6.1 warns about, arriving here by construction
rather than by accident.

**So: the live edge is not sharded.** Local work is keyed on uid and chunk
id, which are shard-independent, and sharding stays what it already is — a
*publication* concern, decided once by whoever freezes the window.

This is the load-bearing choice in the proposal. It is what lets a peer run
arbitrarily far ahead without ever forking the format.

## 3. The design

### 3.1 State: two cutoffs, both declared

A collection over a pack records what it holds:

```toml
pack_snapshot  = "2026-06-30"   # the closed pack it is built on
edge_until     = "2026-08-28"   # how far past it this node has fetched
```

`edge_until` is the honest answer to "how fresh am I?", and it is the field
that makes two peers comparable. A peer that fetched later has a larger
edge; a light peer has none. Neither is wrong, and the number says which.

### 3.2 Documents in the edge are marked, not separated

Live-edge documents live in the same manifest as the pack's, with one
column: `origin = pack | edge`. They are parsed, indexed and (optionally)
embedded exactly like any other document — same plugin, same chunker
version, same cleaning version, therefore the same chunk ids and the same
`content_hash` the official update will compute.

Search uses both. The reader shows both. The only difference is that edge
documents are **unverified** in §16's sense: no signed manifest asserts
them, and a consumer that wants only published content can filter on the
column.

### 3.3 Absorption when the update lands

The official update publishes shards, a delta, and a manifest whose
`previous` is the pack the peer holds. Reconciliation is per uid:

| the peer holds | the update says | outcome |
|---|---|---|
| edge record, same `content_hash` | published | **confirmed** — flip `origin` to `pack`, keep the text index and the vectors as they are |
| edge record, different `content_hash` | published | the origin changed after the peer fetched: take the official record (delete-then-insert on uid), **discard the peer's vectors for its chunks** |
| edge record | not in the update | outside the official cutoff — stays `edge`, absorbed by a later update |
| no record | published | ordinary fetch of the new shard |

Nothing is renumbered and nothing is re-embedded in the common case. The
peer's month of work becomes part of the published collection by changing
one column.

### 3.4 Contribution: work done before the work list existed

§11 leases work: the coordinator hands out a shard, the peer computes, a
signed bundle comes back. A live-edge peer has done the opposite — computed
first, and only later discovers that its chunks are on a work list.

The extension is small: **accept unsolicited bundles**. A bundle whose
chunk ids appear in the update's work list, whose `content_hash` matches,
and whose vectors pass the same calibration check as a leased bundle, is
credited exactly like leased work. The coordinator then removes those chunks
from the list so nobody computes them twice.

This costs one relaxation — a lease stops being a precondition for credit —
and buys the property that makes the whole thing worth doing: **a lazy embed
answered for one user is a contribution to everyone**. The vectors a peer
computed because somebody searched for a book are the same vectors the
network needs.

Two safeguards, both already in the format:

- credit is per verified chunk, so a flood of unsolicited bundles costs the
  coordinator a verification, not correctness;
- the calibration/tolerance block decides acceptance, so a peer whose
  encoder drifted contributes nothing, whether leased or not.

### 3.5 Publishing the update yourself

If no official update arrives, a live-edge peer already holds everything
needed to publish one: it has the previous manifest, the origin window, and
a cutoff it can choose. That is §6.1's "any peer may publish an update",
with the edge as the raw material. The freeze is then a *decision* — pick a
date, shard the edge up to it, sign — not a separate pipeline.

## 4. What this changes, and what it does not

**Unchanged:** the pack format, shard numbering, slicing, the manifest,
chunk ids, the same-function proof, trust levels. A consumer that ignores
the live edge sees exactly today's system.

**New:**

1. `edge_until` in the collection state, and `origin = pack | edge` per
   document (a manifest column, not a wire-format change);
2. the absorption step at update time (§3.3) — a reconciliation pass, all
   of whose branches are delete-then-insert on uid;
3. unsolicited bundles accepted against a work list (§3.4);
4. a role in §0.3: a **live peer** — a peer with origin access that runs
   ahead of the pack. Light peers cannot; they have no origin to fetch.

## 5. Open questions for review

1. **Does the edge get published as a pack member at all?** Proposal says
   no: it is local state until a freeze turns it into shards. The
   alternative — publishing a `provisional/` member — would let peers share
   freshness directly, at the cost of the divergence §2 describes.
2. **Who freezes?** `update.coordinator` exists in the manifest. Until a
   policy lands, "the first valid update whose `previous` matches" is the
   rule, and the edge makes every live peer a candidate publisher. That is
   either healthy redundancy or a race — worth deciding deliberately.
3. **How far may an edge run?** A peer six months ahead of the pack is
   mostly running its own collection. A cap (`edge_max_days`) would make
   that explicit rather than emergent.
4. **Do we credit edge work retroactively at all?** §3.4 says yes. The
   contrary argument is that it rewards computing without coordination and
   could duplicate effort. The counter is that lazy embeds happen anyway,
   and the choice is only whether their output is wasted.
5. **Chunker version drift.** Absorption assumes the peer chunked with the
   same chunker and cleaning version as the publisher. If they differ, the
   chunk ids differ and the vectors are not interchangeable — the same
   `space_id` rule as everywhere else, but here it is the *publisher's*
   version that decides, and a peer that upgraded early is the one that
   loses its work. Worth stating in the manifest as a minimum/maximum
   accepted chunker version.
