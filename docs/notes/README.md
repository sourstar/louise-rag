# Engineering notes

Everything in this folder is a **record**, not the specification. The
specification is the numbered documents one level up; where a
note and a document disagree, the document wins.

What is here, and why it is kept:

| note | what it records |
|---|---|
| `architecture-v2.md`, `pack-format.md` | the design as it was written and corrected in flight, with its amendment history; the source the numbered documents were condensed from |
| `p2p-rag-architecture-v2.md` | the same architecture as first consolidated, before any amendment — superseded by `architecture-v2.md`, kept for the record |
| `no-central-node.md`, `certified-collections.md`, `lineage.md` | the three proposals that became §2.7–2.8, §3.7–3.8 and §4 — with the reasoning and the alternatives that were rejected |
| `embedding-redundancy.md`, `pack-live-edge.md`, `p2p-discovery.md`, `storage-tiering.md`, `dna-search-axes.md` | proposals with their measurements; partly absorbed, partly parked |
| `scheduling.md` | the worker's scheduling model, the measured costs, and every pitfall that has cost a rework — read before touching what runs when |
| `calibration.md`, `calibration.live.md`, `approved-encoders.md` | how the tolerance was measured, and which encoder builds were found compliant on which hardware |
| `phase1-integration-notes.md`, `upstream-rust-stemmers-greek.md` | postmortems: what went wrong, what it cost, what was changed |
| `lockss-p2p-evaluation.md` | why the peer layer is not LOCKSS |
| `archive-signing-keys.md` | who signs a pre-embedded archive's bundles, and therefore what `credits[].key` can hold — open, and it blocks crediting a public archive |
| `figures-in-the-corpus.md` | whether anything but text may enter a cleaned corpus — open, and it blocks full import of any corpus whose figures matter |
| `spec-review-2026-09-07.md` | the review that produced documents 01–09 as they stand: what fifteen lenses found, every decision taken where the documents were silent, and the questions left for the operator |
| `gazzetta-plugin.md`, `eli-legal-plugins.md` | the Italian law corpus researched to a design, and ELI adopted as the template for EU legal plugins — with the clash check that found the pack format compatible |
| `collection-classes.md`, `embedder-choice-and-supersession.md`, `reranker-scope.md` | the three general questions that corpus forced: what a collection must declare about its bytes, who chooses the encoder and what is done when the choice was wrong, and whether a pack may ever require a reranker |
| `embedder-pool-spec.md` | the external container that serves embeddings for query and creation — specified, not built |
| `rage.md` | RAGE, the RAG Engine: one app to add, load, unload and remove every model, whatever its runtime or device. A front that owns every decision — admission, the run identity, the clock — over a llama-swap that only runs processes, configured from one `rage.toml` — design accepted 2026-09-19, not built; migration step 13 |
| `rage-review.md` | why `rage.md` reads as it does: the review of the first proposal (four blocking gaps, seven open decisions), the solution accepted for each, and §18, the reasoning behind quarantine (03 §3.6; migration step 14) |
| `STATUS.md`, `implementation-prompts.md` | where things stood at the last update, and the task prompts used to build them |
| `migration-plan.md` | the ordered steps from the current code to the documented architecture; deleted when the last step lands |

Nothing here is needed to understand or use Louise. All of it explains
why some detail is the way it is, and most of it was paid for.
