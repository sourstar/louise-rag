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
| `scheduling-log.md` | one row per deployed scheduling change: the scenario it ran under and what it measured, before and after. Append-only, and filled in before the deploy — a throughput number without its scenario is not evidence |
| `calibration.md`, `calibration.live.md`, `approved-encoders.md` | how the tolerance was measured, and which encoder builds were found compliant on which hardware |
| `p2p-review-open-issues.md` | the P2P gaps found on 2026-10-19 when the second GPU peer joined (held vectors invisible to the network — 91 % of gutenberg —, missed attestations, self-declared addresses, DHT flood, slice stalls, bundles announced from a peer that serves nothing), and the review that fixed five of them the same day: what was decided, built and amended, what stays open, the deploy order |
| `vector-convergence.md` | proposal (2026-10-19): equivalent versions of the same vectors converge on one, by 04 §4.6's adoption rule and a total order — amends 02 §2.4–2.5, 04 §4.5 once reviewed |
| `measuring.md` | how to measure without producing a number that gets quoted wrong: the existing harnesses, the deployed configuration, the disk, the labels — read before measuring anything |
| `phase1-integration-notes.md`, `upstream-rust-stemmers-greek.md` | postmortems: what went wrong, what it cost, what was changed |
| `lockss-p2p-evaluation.md` | why the peer layer is not LOCKSS |
| `archive-signing-keys.md` | who signs a pre-embedded archive's bundles, and therefore what `credits[].key` can hold — open, and it blocks crediting a public archive |
| `figures-in-the-corpus.md` | whether anything but text may enter a cleaned corpus — open, and it blocks full import of any corpus whose figures matter |
| `spec-review-2026-09-07.md` | the review that produced documents 01–09 as they stand: what fifteen lenses found, every decision taken where the documents were silent, and the questions left for the operator |
| `gazzetta-plugin.md`, `eli-legal-plugins.md` | the Italian law corpus researched to a design, and ELI adopted as the template for EU legal plugins — with the clash check that found the pack format compatible |
| `normattiva-multivigenza.md`, `normattiva-multivigenza-review.md` | multivigenza for Normattiva: the snapshot-chain proposal (2026-10-12) and the review that withdrew it the same day with measurements of the whole multivigente archive — one record per Expression, validity resolved by key, snapshots as transaction time; the operator's decisions are listed in the review's §6 |
| `legal-collection-template.md` | the template a legal collection follows, left by Normattiva (2026-10-13): identity and the id width, versions and validity, origin and fidelity, acquisition from a public API (the four findings of the first fetch), chunks and encoder, relations as facts never signals, the reference source, what the studies say and what was done with it, the process, a checklist; `tools/legal_pilot.py` measures the pending points |
| `normattiva-full-corpus.md` | the acts outside Normattiva's 23 pre-packaged collections, measured 2026-10-13 (10,809 of 74,613 for 1948–2026, ordinary laws above all); what the open-data API can export — the enumeration, an asynchronous export of one version per job, the change feed — and three designs; design A BUILT as code 2026-10-13 (export_job.py, the `archive` profile, the normattiva-archive recipe and create script) for a NEW collection, not yet created: one year of the export is to be timed first |
| `collection-classes.md`, `embedder-choice-and-supersession.md`, `reranker-scope.md` | the three general questions that corpus forced: what a collection must declare about its bytes, who chooses the encoder and what is done when the choice was wrong, and whether a pack may ever require a reranker |
| `deposit-collections.md` | Louise as LocalAGI's knowledge base: collections a client writes over HTTP — `never-share`, `erasable`, and what "no trace" means in bytes — adopted 2026-09-29 into 02 §2.9, 06 §6.7, 07 §7.4; migration step 16 |
| `annotation-collections.md` | links WE make between a collection's records (same work, translation of) kept as a side collection of claims: its Codex names the target and the snapshot the claims hold from, its claims are attached to search results as facts — adopted 2026-10-09 into 02 §2.10, 05 §5.3/§5.5, 09 §9.3/§9.4/§9.5/§9.14 |
| `wikidata-plugins.md`, `wikidata-implementation.md` | the operator's design for `wikidata` (jina-v3, Wikimedia's archive as the first vectors), `gutenberg-graph` (mentions, relations, communities over Gutenberg) and the "Philippe" voice guide; what was measured against Wikimedia's data (512-d vectors; 86 % of the archive reproducible, why the rest is not); the mapping onto the spec with the decisions it needs |
| `wikipedia-qids.md` | Wikipedia parser 5 (2026-10-18): each record holds its page's rows of the dump run's `page_props` — the Wikidata item and every other property — from the source, never another collection; the unit, the hash, the measurements, and the order of the switch on the node (amendment A1d) |
| `char-cut-truncation.md` | a known defect (2026-10-18): the platform cuts long inputs by characters, not to `ctx` tokens, so archive vectors of long inputs do not reproduce (pubmed fails its `vectors-sample`) and long inputs embedded here are not the declared function; the evidence and the fix, a wire change for the operator |
| `backend-consolidation.md` | one LocalAI backend per engine: why the four Python models share `louise-python`, measured equal to the backends they replaced, installed by LocalAI's own "From URL"; the patches the stock backends would need; ROCm |
| `louise-python-rebuild.md` | the runbook for rebuilding the `louise-python` backend: why a new tree re-states every model on it, the window, the gate per collection, the fallback, and the invariant-12 measurement a new torch model needs before it encodes a collection (first run: jina-v3, 2026-10-20) |
| `weights-fetch.md` | fetching a collection's weights from peers by digest — seeding per model digest, the first copy from LocalAI's gallery or the creator's own model, where the origin is recorded — design note 2026-09-28, not scheduled |
| `embedder-pool-spec.md` | the external container that serves embeddings for query and creation — specified, not built |
| `rage.md` | RAGE, the RAG Engine: one app to add, load, unload and remove every model, whatever its runtime or device. A front that owns every decision — admission, the run identity, the clock — over a llama-swap that only runs processes, configured from one `rage.toml` — design accepted 2026-09-19, not built; migration step 13 |
| `rage-prior-art.md` | which serving systems and LocalAI forks already do parts of RAGE's admission and eviction, the template for each part, what extending LocalAI's scheduler would take, and whether anyone preloads the next model by the sequence of use (no one does) — research, 2026-10-01 |
| `rage-over-localai.md` | the split accepted 2026-10-03 (migration step 15): Louise the collection protocol, RAGE the vector compliance only, LocalAI the model lifecycle — what RAGE needs from LocalAI (one blocking gap: the load options), the engine's `options` field it added, and the 03 §3.3 / 09 §9.12 amendment |
| `localai-bugs.md` | every action item for the LocalAI team in one place: the defects found while studying its scheduler (with line references and how sure each is), the patches we carry (`deploy/localai/patches/`) and the requests — for the operator to file upstream |
| `rage-review.md` | why `rage.md` reads as it does: the review of the first proposal (four blocking gaps, seven open decisions), the solution accepted for each, and §18, the reasoning behind quarantine (03 §3.6; migration step 14) |
| `STATUS.md`, `implementation-prompts.md` | where things stood at the last update, and the task prompts used to build them |
| `migration-plan.md` | DELETED 2026-10-08, the last step landed: `git show 7f84d44:docs/notes/migration-plan.md`; what it left open is in STATUS.md |

Nothing here is needed to understand or use Louise. All of it explains
why some detail is the way it is, and most of it was paid for.
