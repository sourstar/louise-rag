# Choosing an embedder, publishing the score, and superseding a collection

**Status: PROPOSAL for evaluation. Nothing built, nothing agreed.**
Written 2026-08-26 from an operator directive that resolves a question
`gazzetta-plugin.md` §7 could not: *who* chooses a collection's embedder, and
what happens when they choose badly.

## 0. The directive, restated

> Model benchmarking is a separate activity. The choice of the embedder
> belongs to the **creator of a collection**. Pinning a collection **freezes**
> the embedder, because from that moment the community starts contributing.
> An **embedder score** is therefore added to the collection, so that every
> peer can double-check the result and, if warranted, contact the author for
> a review. A review implies a **derived collection**, eventually superseding
> the old one — ideally in the early stages of the common embedding. In this
> way every peer reuses most of the data it has already downloaded, by
> construction.

This is a better answer than "measure and pick the winner", which is what
this repo's documents said until now, because it names the thing those
documents left out: **authority**. Measurement does not choose; someone does.

## 1. Why the shape is right

The directive separates three concerns the current design conflates:

| concern | who holds it | mechanism |
|---|---|---|
| **authority** — which encoder this collection uses | the collection's creator | the `[embed]` recipe, pinned into `space_id` at creation |
| **accountability** — was that a good choice? | every peer | the published score, re-runnable |
| **remedy** — it was not a good choice | the community | a *derived* collection, never a mutation |

The third row is the one that makes the other two safe. A pinned space
**cannot** be revised in place — invariant 1 makes a mismatch a
refuse-to-boot error and forbids auto-migration, and `space repin` drops
every vector, every lexical entry and the verification ledger. So "the author
was wrong" has no in-place remedy and never will. Supersession by a derived
collection is not a workaround for that limitation; it is the only
correctly-shaped remedy, and the format already half-implements it.

## 2. What the pack format already provides

Three pieces exist:

- **`derived_from`** is already a manifest field (`pack/manifest.py`,
  currently always `null`). It is exactly the supersession edge.
- **`spaces` is a list.** One collection can carry several embedding spaces
  over the same records — so a derived collection is not a fork of the data,
  it is a new space over shared shards.
- **Record shards are content-addressed** — a v2 merkle `root` per shard, and
  torrent members keyed by `info_hash`. A peer already holding the records
  of collection A holds, bit for bit, the records of any collection derived
  from A. Nothing is re-fetched to follow a supersession except the vectors.

That last point is the "reuse most of the downloaded data, by construction"
in the directive, and it is real. It is also **quantitatively weaker at high
dimension than it sounds**, which matters for §5:

| what a peer holds | bytes per chunk (order of magnitude) |
|---|---|
| record (legal article, ~3 kB text, zstd) | ~1 kB |
| text index share | a few hundred bytes |
| vector, dim 1024 f16 | 2 kB |
| vector, dim 3840 f16 | **7.7 kB** |

So at dim 1024 a supersession costs a peer roughly half its bytes again; at
dim 3840 it costs several times what the records cost. **"Ideally in the
early stages" is not a preference, it is the economics** — the re-fetch bill
scales with `coverage × dim`, and both only grow.

## 3. The missing piece: an evaluation block per space

Proposed addition to each entry of `spaces[]`:

```json
"evaluation": {
  "suite": {"name": "JuriFindIT", "split": "expert", "version": "1.0",
            "sha256": "…", "public": true},
  "metric": "ndcg@10", "value": 0.71,
  "also": [{"metric": "recall@20", "value": 0.88}],
  "conditions": {"k": 10, "chunker": "akn_article", "chunker_version": 1,
                 "dim": 2048, "dim_full": 3840, "rerank": null},
  "coverage_check": {"targets": 23104, "resolved_in_pack": 23104},
  "run_by": "<pubkey>", "run_at": "2026-…", "tier": "reproducible"
}
```

Design points, each earning its place:

**`conditions` is part of the claim.** A score without the chunker, the
pinned dimension and whether a reranker was in the loop is not comparable to
anything. This is the same lesson as the reranker hint in
[`reranker-scope.md`](reranker-scope.md) §8.

**`coverage_check` is the cheap integrity test.** It asserts that every
target ID the suite points at actually resolves to a chunk in *this* pack.
A score computed against a corpus that does not contain the answers is not a
low score, it is a meaningless one — a mistake made in an early draft of
`gazzetta-plugin.md` §7.2 and worth making structurally impossible. A peer
can verify this **without having the suite's queries at all**.

**`tier` is honest about reproducibility.** `pack-format.md` §16 prefers
reproduction to assertion, and evaluation suites do not always cooperate:

- `reproducible` — the suite is public and hash-identified; any peer re-runs
  it and confirms or refutes.
- `asserted` — the suite cannot be redistributed (a licence, private data).
  The score is signed but unverified, and **must never be displayed as if it
  were reproduced** — the same ladder `[trust] space_authors` already applies
  to space artefacts.

For the middle case where queries are restricted but the mapping is not, a
suite can ship `sha256(query) → target chunk IDs`. A peer that has the suite
verifies fully; a peer that does not can still run `coverage_check` and
confirm the score was not computed against an absent corpus.

## 4. Review and supersession, as a lifecycle

1. **Creation.** The creator benchmarks (separately, as the directive says —
   benchmarking is not part of building a collection), pins the winner,
   publishes the collection with its `evaluation` block.
2. **Audit.** Any peer re-runs a `reproducible` score. Cheap: the corpus is
   already local, the suite is public, the conditions are stated.
3. **Counter-evaluation.** A peer measuring materially differently publishes
   a signed block of the same shape against the same collection — the
   mechanical form of "contact the author for a review". No new machinery:
   it is the attestation pattern of `pack-format.md` §16 pointed at quality
   instead of at bytes.
4. **Derived collection.** If the review carries, someone publishes a new
   collection: `derived_from: <old collection_id>`, **the same record
   shards**, a new space. Peers follow by fetching vectors only.
5. **Supersession.** The old manifest gains `superseded_by` — a field that
   does not exist yet and would need adding — and demand migrates on its own,
   because the coordinator's `/work` list is already ordered by what
   consumers are asking for.

Nothing in that chain arbitrates. There is no authority that declares one
collection better, and there should not be: the format records signed
claims, and **adoption is the vote**. That matches this project's stated
preference for not building governance before someone begs for it.

## 5. What this costs, stated plainly

- **Supersession is not free for peers**, and gets less free over time (§2).
  The format should make the score visible at *creation*, when the re-fetch
  bill is smallest — which argues for `evaluation` being effectively
  mandatory for a published collection rather than optional.
- **Eval-set gaming is possible.** A creator picks the suite that flatters
  their encoder. Mitigations, none complete: the suite is named and hashed;
  `public: true/false` is visible; counter-evaluations use other suites; and
  a third-party suite should be marked as such and weigh more than a
  creator-authored one.
- **Two collections, two scores, a split community.** Real risk. The
  `derived_from` chain at least keeps them genealogically related rather
  than parallel strangers, and shared record shards keep the split cheap.

## 6. Relation to the reranker question

[`reranker-scope.md`](reranker-scope.md) concluded that a reranker is
advisory because its output never reaches common content. An embedder is the
opposite: its output **is** the common content. So the same block shape
carries very different weight, and that is the useful distinction:

- **embedder `evaluation`** — justifies a *pinned* function; effectively
  mandatory; the basis for supersession.
- **reranker `rerank_hint`** — justifies nothing; advisory; a peer may ignore
  it and hold identical data.

One schema, two tiers of consequence, and the difference is derivable rather
than stipulated: does the artefact this score defends end up in bytes that
travel?

## 7. Open questions

1. **Is `evaluation` mandatory for a published collection?** §5's economics
   say effectively yes; the format has no other mandatory quality field.
2. **`superseded_by` needs adding**, and it is a mutation of an
   already-signed manifest — so it is really a new signed record pointing at
   the old collection, not an edit. Which record, published where?
3. **What makes a review "carry"?** Deliberately unanswered above. A count of
   independent reproductions? The creator's own agreement? Nothing, and let
   adoption decide?
4. **Does a counter-evaluation need the same suite**, or is measuring on a
   *different* suite a legitimate challenge? (Probably yes, and probably it
   should be labelled as a different claim rather than a contradiction.)
5. **Should `evaluation` also cover the lexical leg and the fusion**, not
   just the dense one? An end-to-end score is what a user experiences; a
   dense-only score is what the pinned function is responsible for.
