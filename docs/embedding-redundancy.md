# Not embedding the same thing twice

*Proposal, 2026-08-28. For review — nothing here is built.*

The point of exchanging vectors is that the community pays for an embedding
**once**. Gutenberg is the case to reason about, because the numbers are
now measured rather than guessed:

| | |
|---|---|
| chunks in `gutenberg-2026-06` | **8,661,545** |
| this node's encoder | ~0.47 chunks/s (Qwen3-Embedding-8B on one Arc) |
| **one node embedding the whole pack** | **≈ 213 GPU-days** |
| ten nodes, perfectly partitioned | ≈ 21 days each |
| ten nodes, uncoordinated | up to 213 days each, for the same output |

The gap between the last two rows is the whole subject.

## 0. Two things measured first, one of which killed an idea

**Duplicate text inside the corpus is not a lever.** The obvious saving —
Gutenberg ships a standard licence header in every book, so embed identical
text once — is worth **0.1%**. Across 400 sampled books, 44 of 43,902 chunks
had text that appeared more than once. Fixed-size chunking merges the
boilerplate into book-specific text, so it never repeats exactly. Content
addressing the vector store would buy nothing here; it is not proposed.

**Chunks are almost all full size**: 36,761 of 43,902 sampled chunks are at
the 4,000-char maximum. So chunk *count* tracks corpus bytes almost exactly,
and there is no "lots of tiny cheap chunks" effect to exploit.

## 1. Why lazy-embed-as-contribution is the wrong mechanism

The operator's own doubt is correct, and the reason is worth stating
sharply: **demand is correlated across peers.**

Everybody searches the popular books. If contribution is a side effect of
answering local queries, then every peer independently embeds *the same
small popular set*, and nobody embeds the long tail. That is the worst
possible allocation: it concentrates duplication exactly where duplication
is already highest, and leaves the 90% that needs coverage untouched.

Lazy embedding is still worth doing — it serves a waiting user, which is a
latency problem, not a coverage problem. But it must not be the organising
principle, and it needs one change (§3) to stop it *causing* redundancy.

## 2. Where the redundancy actually comes from

Ranked by how much community GPU time each costs:

| source | cost | fixable by |
|---|---|---|
| **A re-pin of the space** (encoder, chunker, or cleaning version) | the ENTIRE corpus, ×every peer — 213 GPU-days per node, at once | discipline + layers/derived collections (§6) |
| **N peers each embedding the same corpus** | ×N | the coverage map (§3) + partitioning (§4) |
| **Recomputing instead of importing**, because verifying someone else's vectors feels riskier than doing it yourself | ×(peers who distrust) | cheap verification, already designed |
| **Work lost when a peer leaves mid-shard** | up to one shard per departure | publish partial work (§5) |
| Duplicate text within the corpus | **0.1% — measured, not a lever** | — |

The ordering matters: **a single careless version bump costs more than every
coordination failure combined.** That is the headline, and it is why §6 is
in this document at all.

## 3. The mechanism: a coverage map small enough to gossip

Everything else follows from one primitive — *can a peer cheaply answer "has
someone already embedded this chunk?"*

For a space, publish a **coverage bitmap**: one bit per chunk, in the
manifest's chunk order.

```
8,661,545 chunks / 8 = 1.08 MB raw, and it is mostly runs — a few hundred
KB compressed for the whole of Gutenberg.
```

That is small enough to gossip with the catalogue digests (§13), and
updates are bit-deltas measured in kilobytes. Per-shard fill fractions
(1,591 numbers) ride along for cheap coarse decisions.

With that one artefact, every other decision becomes **local and
coordination-free**:

- **Lazy embed asks before computing.** On a query miss, check the bitmap:
  if the chunk is covered, fetch the vector; only compute if it is not. This
  is the change that turns lazy embedding from a redundancy *source* into a
  redundancy *sink* — the popular chunks everybody wants are precisely the
  ones already covered, so the correlated demand costs one download instead
  of N embeddings.
- **Background work targets gaps**, not demand.
- **A joining peer computes nothing** it can download.

Coverage is already a first-class idea in the format ("`coverage` makes
partial embedding a first-class state"); this proposes making it a
*published, gossiped artefact* rather than local knowledge.

## 4. Partitioning without a coordinator

Given the map, peers must still avoid choosing the same gap. Two rules, no
central authority:

**Rendezvous (HRW) assignment.** For every uncovered shard *s*, each peer
computes `score = H(peer_pubkey ‖ shard_id)` and works on the shards where
it scores highest among the peers it believes are live. No messages are
needed to partition — only an approximate peer set, which the catalogue
gossip already carries. When a peer disappears, only *its* shards are
reassigned, and they are reassigned to a deterministic successor rather than
to everyone at once.

**Randomised tie-breaking on top.** A peer that has finished its own
assignment picks uniformly among the *k* least-covered shards rather than
the single least-covered one, so idle peers do not herd onto the same gap.

The coordinator (§11) remains useful — for policy, credit and the work list
— but it stops being *required* for partitioning, which is the part that
must keep working when it is offline.

## 5. Losing less when a peer leaves

A lease is per shard (~7,000 chunks ≈ one GPU-hour), but publication should
not be. A peer should emit a signed bundle every few hundred chunks, so an
abandoned lease loses only the unpublished remainder rather than the hour.
The bundle format is already per-chunk; this is a cadence decision, not a
format change.

The same applies to the local write path: this node lost hours of encoder
output more than once by holding a document's vectors in memory until the
end. That was fixed for one document (amendment 55); the network-facing
version of the same lesson is "publish early, publish partial".

## 6. The dominant term: don't re-pin the space

Every row above is worth tens of days. **A space re-pin is worth 213 days,
per peer, at once** — it invalidates every vector the community holds.

So the strongest single measure to reduce community-wide embedding is
procedural, not algorithmic:

1. **A version bump is a community event**, not a local convenience. The
   encoder build, the chunker version and the cleaning version are all in
   `space_id` for exactly this reason.
2. **Layers make it partial** (amendment 41): if only one layer's chunker
   changes, only that layer is recomputed; the rest of the corpus keeps its
   vectors and a derived collection inherits them.
3. **The approved-encoder list is the control**, and it has to be honoured —
   this node was found running an untrialled build (b10630 against an
   approved b10603), which is how 8% of gutenberg's stored vectors came to
   be corrupt. Those now need recomputation: **a self-inflicted re-embed of
   the worst kind, because it is invisible until measured.**

## 7. What I would build first

In order, because each step is useful alone and the earlier ones make the
later ones cheap:

1. **The coverage bitmap** — published per space, gossiped, with per-shard
   fill fractions. Nothing else works without it.
2. **Fetch-before-compute in the lazy path** — the smallest change with the
   largest immediate effect, and it directly answers the operator's doubt.
3. **Partial publication cadence** — bundles every few hundred chunks.
4. **HRW assignment** — only once there are enough peers for partitioning to
   matter; until then the coordinator's leases are sufficient.

## 8. Open questions

1. **Bitmap freshness vs size.** A stale map causes duplicate work; a
   too-frequent one costs gossip. What staleness is acceptable — minutes,
   hours?
2. **Does coverage need to be per chunk, or is per shard enough?** Per
   shard is ~1,591 numbers and would make the lazy-path check useless
   (a shard is partially covered almost always). Per chunk costs ~1 MB.
   Proposal takes per chunk; the cost seems affordable but it is the main
   size decision.
3. **Trust of the coverage map itself.** A peer that lies about coverage
   ("everything is done") suppresses work. Signed per-publisher maps and
   union-of-claims would bound that; worth deciding before it matters.
4. **Should lazy-embedded vectors be published at all?** They are free to
   offer once computed, and §3 makes them rare. Proposal: yes, as
   unsolicited bundles (pack-live-edge.md §3.4) — but as a by-product, never
   as the mechanism.
