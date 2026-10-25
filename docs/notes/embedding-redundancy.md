# Not embedding the same thing twice

> **Superseded** (2026-10-25, handover) by `docs/02-collections.md` §2.4 (carries) and `docs/06-plugins.md` §6.3 (chunk-hash reuse), which absorbed most of it.


*Proposal, 2026-08-28. For review — nothing here is built.*

The point of exchanging vectors is that the community pays for an embedding
**once**. Gutenberg is the case to reason about, because the numbers are
now measured rather than guessed:

| | |
|---|---|
| chunks in `gutenberg-2026-06` | **8,661,545** |
| this node's encoder | ~~~0.47 chunks/s~~ → **3.2 chunks/s** (Qwen3-Embedding-8B-Q4_K_M on one Arc, `-np 1`, client concurrency 1; *measured* 2026-08-28/29 by three harnesses within 2 %. The 0.47 was the PIPELINE's rate, 6.4x slower than the encoder; corrected in August, landed 2026-10-18) |
| **one node embedding the whole pack** | ~~≈ 213 GPU-days~~ → **≈ 31 GPU-days** (*modelled* from the line above; gutenberg's current encoder, Qwen3-Embedding-4B, is faster still) |
| ten nodes, perfectly partitioned | ≈ 3.1 days each |
| ten nodes, uncoordinated | up to 31 days each, for the same output |

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
| **A re-pin of the space** (encoder, chunker, or cleaning version) | the ENTIRE corpus, ×every peer — 31 GPU-days per node (modelled), at once | discipline + layers/derived collections (§6) |
| **N peers each embedding the same corpus** | ×N | the coverage map (§3) + partitioning (§4) |
| **Recomputing instead of importing**, because verifying someone else's vectors feels riskier than doing it yourself | ×(peers who distrust) | cheap verification, already designed |
| **Work lost when a peer leaves mid-shard** | up to one shard per departure | publish partial work (§5) |
| Duplicate text within the corpus | **0.1% — measured, not a lever** | — |

The ordering matters: **a single careless version bump costs more than every
coordination failure combined.** That is the headline, and it is why §6 is
in this document at all.

## 2a. The whole node, measured (2026-08-28)

Gutenberg is not the hard case. Every collection on this node:

| collection | documents | chunks | chunks/doc | embedded | archive |
|---|---|---|---|---|---|
| gutenberg | 77,861 | 8,705,239 | 111.8 | 1,370,181 (16%) | — |
| gutenberg-pack | 77,389 | 8,661,545 | 111.9 | 0 | — |
| **pubmed** | 39,994,988 | 39,958,399 | **1.00** | 32,660,007 (82%) | medcpt-pubmed |
| **wikipedia** | 19,402,137 | 32,536,168 | 1.68 | **121 (0.0004%)** | **none** |
| **wikivec** | 21,983,119 | **144,137,255** | 6.56 | 58,233,661 (40%) | upstash |
| genome-hg38 | 455 | 911,911 | **2004.2** | 0 | — |
| genome-dm6 / sacCer3 | 1,887 | 45,231 | 22 / 203 | 34,846 | — |
| **total** | | **234,955,748** | | | |

~~5,786~~ **≈ 850 GPU-days to embed all of it once** at this node's 3.2
chunks/s (*modelled*: the encoder's measured rate with Qwen3-Embedding-8B;
the 5,786 used the pipeline's 0.47 — corrected in August, landed 2026-10-18).
The ordering below does not change; only the scale shrinks ~6.8x.

Three things follow that gutenberg alone would not have shown:

- **Wikipedia is the real problem, not Gutenberg.** 32.5M chunks, **no
  genesis archive**, and 121 chunks embedded — 0.0004%. That is **801
  GPU-days** of genuinely uncoordinated community work waiting to happen.
  Gutenberg's 213 days is second.
- **PubMed and wikivec are mostly a DISTRIBUTION problem, not a computation
  one.** Both have archives supplying vectors (82% and 40% imported
  already), so the community's job there is moving bytes and verifying them,
  not spending GPU. The redundancy to avoid is re-importing and
  re-verifying, not re-embedding.
- **Chunks per document vary by four orders of magnitude** — 1.00 for a
  PubMed abstract, 111.8 for a Gutenberg book, 2004 for a human chromosome.
  Any design that assumes a typical document is wrong for something here.

## 3. The mechanism: a coverage map small enough to gossip

Everything else follows from one primitive — *can a peer cheaply answer
"has someone already embedded this?"*

**Granularity: per DOCUMENT.** My first draft said per chunk; the numbers
above say that is the wrong unit, for three independent reasons.

1. **It matches the unit of work.** A document is embedded and published
   atomically — `upsert_book` writes a document's chunks together,
   `embed_done` is a per-document flag, and a bundle carries documents.
   There is no such thing as a published half-document.
2. **It matches the question the lazy path asks.** The selector picks a
   DOCUMENT to embed (`_next_embed`), so "is this document covered?" is
   exactly the lookup needed. A chunk bitmap would answer a question nobody
   asks.
3. **It is three times smaller overall, and a hundred times smaller where
   documents are large:**

| space | per-chunk bitmap | **per-document bitmap** |
|---|---|---|
| gutenberg | 1.0 MB | **9.7 KB** |
| pubmed | 4.8 MB | 4.8 MB (identical — 1.00 chunks/doc) |
| wikipedia | 3.9 MB | 2.3 MB |
| wikivec | 17.2 MB | 2.6 MB |
| **all spaces** | **28.0 MB** | **≈ 9.7 MB** |

PubMed is the case that shows the unit is right rather than merely cheap:
at exactly 1.00 chunks per document the two maps are the same object, so
there is nothing to trade off.

**Per shard is still too coarse**, which was the original question: a shard
holds thousands of documents and is almost always partially covered, so a
shard-level map cannot answer the lazy-path lookup at all.

**Work remaining needs a second number.** A document bitmap says *what* is
left but not *how much*, and with 1 to 2,004 chunks per document those are
very different questions. The manifest already carries per-shard record and
chunk counts, so scheduling reads: bitmap for "is it done", shard totals for
"how expensive is the gap". No new artefact.

Updates are bit-deltas measured in kilobytes, gossiped with the catalogue
digests (§13).

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

1. **The coverage bitmap, per DOCUMENT** — published per space, gossiped,
   with the manifest's existing per-shard chunk totals for cost. Nothing
   else works without it. Start with wikipedia, which needs it most and has
   almost nothing to lose by being the pilot.
2. **Fetch-before-compute in the lazy path** — the smallest change with the
   largest immediate effect, and it directly answers the operator's doubt.
3. **Partial publication cadence** — bundles every few hundred chunks.
4. **HRW assignment** — only once there are enough peers for partitioning to
   matter; until then the coordinator's leases are sufficient.

## 8. Open questions

1. **Bitmap freshness vs size.** A stale map causes duplicate work; a
   too-frequent one costs gossip. What staleness is acceptable — minutes,
   hours?
2. **Does a document bitmap need an ordering that survives updates?** A bit
   position must mean the same document to every peer. The manifest's shard
   list plus in-shard record order gives one; a new shard appends, so
   positions are stable — but a RE-ISSUED shard (§6.1, a changed record)
   keeps its number while its contents may shift. Either bits are keyed by
   (shard, index-within-shard) and a re-issue invalidates only that shard's
   range, or the map is a sorted uid list, which is larger. The first looks
   right and needs checking against the re-issue rule.
3. **Wikipedia has no archive and no plan.** 801 GPU-days, 0.0004% done. It
   is the collection this whole design exists for, and it is the one nobody
   has started. Does it get an archive (like pubmed and wikivec), or is it
   the first corpus the community genuinely embeds together?
4. **Trust of the coverage map itself.** A peer that lies about coverage
   ("everything is done") suppresses work. Signed per-publisher maps and
   union-of-claims would bound that; worth deciding before it matters.
5. **Should lazy-embedded vectors be published at all?** They are free to
   offer once computed, and §3 makes them rare. Proposal: yes, as
   unsolicited bundles (pack-live-edge.md §3.4) — but as a by-product, never
   as the mechanism.
