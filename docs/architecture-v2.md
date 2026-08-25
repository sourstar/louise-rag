# A General-Purpose, Peer-to-Peer RAG Platform — v2

**Consolidated architecture.** Supersedes v1 (`p2p-rag-architecture-sketch.md`). New in this revision: the verification ladder (§6), the reference/entity layer (§4), the acquisition layer, transfer manager, and preservation failsafe (§5.1–5.3), the community workload protocol (§7), concrete genesis corpora and their licensing (§8), the genomics collection including multi-species (§9), hardware placement (§10), a revised roadmap (§11), the configuration/control plane and web interface (§14–15), and packaging/deployment (§16).

**Goal.** Generalize the single-source `gutenberg-rag` app into a platform where (1) any data source is a plugin, (2) sources live in separate collections federated at query time, (3) both raw corpora and vector artifacts are shareable peer-to-peer, and (4) the embedding workload is split across contributors — with verification strong enough that vectors from strangers are safe to import.

**Framing that emerged and now drives the project:** every public embedding corpus today is a one-shot dump — NCBI's MedCPT PubMed vectors (2023), Cohere's Wikipedia (2023), Upstash's BGE-M3 Wikipedia (June 2024). All frozen, all decaying, none maintained, because no mechanism exists for anyone but the original sponsor to extend them. **This platform is the missing maintenance layer.**

---

## 1. Inherited invariants (do not break these)

Five properties of the existing skeleton are what make the distributed design possible:

- **Manifest-driven resumable ingestion** — per-document stage flags with versions; every write delete-then-insert keyed on document id. Importing a peer's work is therefore just another way to complete a stage, and an import racing a local computation is harmless.
- **Deterministic versioned chunk ids** — `{uid}:{seq}:{CHUNK_VERSION}`, a pure function of cleaned text. The keystone: **vector bundles carry no text**; the importer re-derives it locally. Small bundles, and vectors are useless to anyone lacking the raw data.
- **Content hashing in discovery** — blake2 per source file behind an mtime/size gate; the bridge to content-addressed distribution.
- **Rank-based fusion (RRF)** — ranks only, never scores, so collections with different indexes and different embedding models merge without calibration.
- **Demand-driven embedding** — the lazy queue is a demand signal, and becomes the network's work scheduler.

**Hard rule:** the app container never touches the GPU. All model work stays behind HTTP (Ollama / OpenAI-compatible / TEI / remote peer). This is what lets peers with incompatible hardware run the same image — and it is what makes §10 work.

---

## 2. Layer 1 — Plugin capabilities

The plugin is not one interface but three composable roles. A source may implement more than one.

```python
class CorpusSource(Protocol):        # documents → chunks → vectors
    source_id: str
    license_class: LicenseClass      # gates P2P sharing per plane (§8)
    privacy_class: PrivacyClass      # public | restricted | never-share (§9)
    chunking_profile: str            # book | abstract | article | genomic-window
    def discover(self, state) -> Iterator[DocRef]
    def fetch(self, ref) -> RawDoc           # local → swarm → origin
    def parse(self, raw) -> CanonicalDoc

class ReferenceSource(Protocol):     # lookup tables — no chunking, no vectors
    source_id: str                   # clinvar, gnomad, hgnc, ensembl-compara
    ref_version: int
    def schema(self) -> RefSchema    # tables, primary keys, declared entity links
    def sync(self, state) -> Iterator[RecordBatch]   # baseline + delta, idempotent

class EntityResolver(Protocol):      # query text → keys
    def resolve(self, query, refs) -> list[Entity]   # regex + dictionaries
    def expand(self, entity, refs) -> list[Entity]   # traverse declared links
```

**Canonical document model:**

```
CanonicalDoc:
  uid        = f"{source_id}:{native_id}"      # gutenberg:1342, pubmed:38112345
  title, creators[], language, date
  subjects[]   # controlled English vocabulary — LoC headings, MeSH, GO terms.
               # Keeping this contract preserves free cross-lingual catalog search.
  keys[]       # EntityIds this doc concerns (§4)
  extra{}      # source-specific JSON
  sections[]   # (heading, text)
  raw_hash
```

Everything Gutenberg-specific in the current tree lives in three files (`discovery.py`, `pg_text.py`, `rdf.py`); everything else is already generic. Porting Gutenberg onto `CorpusSource` is a refactor, not a rewrite.

---

## 3. Layer 2 — Collections, spaces, topology

A **collection** = (plugin, corpus snapshot, pipeline versions, embedding space) → its own manifest + FTS index + vector table.

```
space_id = hash(embed_model_file_hash,   # weights digest, not the name
                dtype, normalization, pooling,
                CHUNK_VERSION, text_cleaning_version,
                truncation/ctx policy,
                query_encoder_hash)      # bi-encoder pairs (MedCPT) differ here
```

*(Amended after Phase-1 integration.)* Two things the hash deliberately does
**not** cover, with the compensating checks import must therefore make:
`dim` is only observable once vectors exist, so it is pinned and compared
**separately** — bundle header vs parquet width vs the pinned space row vs the
live table, all four ways. And chunk *geometry* (`CHUNK_TOKENS`,
`CHUNK_OVERLAP`) is not part of the recipe — only `CHUNK_VERSION` is — so two
honest nodes at the same version can chunk differently; import compensates by
requiring the bundle's chunk-id set to exactly equal the locally re-derived
one, which also catches cleaning drift for free. Any implementation that skips
either compensating check has silently reintroduced mixed-space corruption.

Collections in the same space *may* share a physical vector table; different spaces *must* stay separate — cosine distance across models is meaningless. **Default: separated storage, federated querying.** The `space_id` is also the namespace for network artifacts: a bundle only imports into a matching space, which mechanically prevents mixed-model corruption.

**Rule learned from Cohere:** canonical spaces must pin **open-weights** models only. A space whose encoder is a paid API can be held but not extended or verified, and pooled community GPU-hours would accumulate behind someone else's tollbooth.

Manifest generalization: `books` → `documents(uid, source, native_id, …stage flags…)`, plus `doc_keys(uid, entity_id)`, `bundles`, `providers`, and the reference tables owned by each `ReferenceSource`.

---

## 4. Layer 3 — Entities, references, and the query plan

Not everything should be embedded. ClinVar significance, gnomAD frequencies, HGNC symbols are **lookup tables**: the right answer to "frequency of rs80359351" is an exact key match, not a semantic neighbour. Vectorizing them makes them worse.

**Canonical entity ids**, species-qualified from the start:

```
hgnc:11998        mgi:98834         # TP53 human / Trp53 mouse — never bare "TP53"
rs:80359351       pmid:38112034     mesh:D001943
region:grch38:13:32316461-32316527  # assembly-qualified always
```

**Query plan** — one new stage before the existing pipeline:

```
resolve → expand → plan → execute → fuse
```

- No entities found → plan degenerates to today's pipeline exactly. Full backward compatibility, zero cost on generic queries.
- Entities found → two distinct execution paths:
  - **Structured lookups** (ReferenceSource, exact by key) → enter the LLM context as authoritative blocks, **never ranked, never in RRF**.
  - **Corpus collections** → receive the free-text query *plus* entity filters/boosts; their ranked lists go through RRF as usual.

The category distinction matters: RRF fuses what is *rankable by relevance*; lookups are *facts by key*. Mixing them is a type error.

Entity extraction is cheap (regex + dictionaries). For queries with no explicit identifier ("the Ashkenazi founder variant of BRCA2"), fall back to the LLM already used for translation fan-out — ten lines where `llm.translate_query` lives.

`doc_keys` translates directly into filters the code already has: LanceDB `where entity IN (...)` with prefilter, Tantivy field filter.

---

## 5. Layer 4 — P2P exchange

Two planes, deliberately separate.

**Raw plane** — signed snapshot manifests (`snapshot_id, source_id, [(file, size, blake2)]`) plus content-addressed transfer with origin fallback. BitTorrent v2 recommended (per-file merkle matches the existing one-hash-per-file model; webseeds give origin fallback natively). PubMed maps perfectly: NLM already publishes an immutable yearly baseline plus daily deltas. Every completed download makes the peer a seeder — the entire origin-offload story.

**Derived plane — vector bundles:**

```
bundle/
  header.json      space_id, collection, uid list, chunk count, dim/dtype,
                   producer pubkey, backend fingerprint, content digest,
                   challenge embeddings slot (§6 T2)
  vectors.parquet  chunk_id, vector[dim]             ← no text, ever
  possession.json  uid → producer's raw_hash          ← added in Phase 1
  MANIFEST.sig     exactly 64 raw bytes: Ed25519 over a domain-separated,
                   length-framed payload of the header's wire bytes + the
                   sha256 of each sidecar
```

*(Amended after Phase-1 integration.)* `possession.json` was added so vectors
bind to a specific text **revision** — Gutenberg re-issues books and PubMed
revises records, and without per-uid hashes, revision-A vectors attach
silently to revision-B text. It is a sidecar rather than header fields so it
is parsed only *after* the signature verifies. The signature domain prefix
exists because the same node key also signs snapshot manifests and space
artifacts; nothing else binds a signature to "bundle".

Import is a new implementation of the `embed` stage: verify → space match → confirm local `raw_hash` (possession binding) → verification ladder → bulk add → set `embed_done`. Parquet keeps the wire format engine-neutral.

**Delivery channel** *(previously unspecified — the upload half was defined in
§7 but no retrieval path existed)*: a consumer receives bundles into a
per-collection **inbox directory** its worker drains — filled by hand, by a
sync of the coordinator's artifact store, or by the HF-repo/torrent transports
below. There is deliberately no `GET /bundles/{id}` on the coordinator: it
"holds no bulk data", and a download endpoint would quietly make it a CDN.

**Reference tables** need only the raw plane — they are copies of public data, not derived work, so content-addressing suffices and no trust ladder applies.

### 5.1 Acquisition layer — declarative specs, generic machinery

The plugin declares *where the bytes live*; the engine implements *how to move them*, once, for everyone.

```python
class AcquisitionSpec:
    transport: Literal["rsync","https","ftp","torrent","hf_hub","s3"]
    endpoints: list[Endpoint]      # ordered: mirrors first, origin last; regions
    layout: str                    # one-dir-per-doc | container-files | single-file
    filters: list[str]             # include/exclude, e.g. "*.txt", "!audio/"
    checksums: ChecksumSource      # origin sidecar (.md5) | manifest | none→blake2
    delta: DeltaSpec               # updatefiles-dir | dated-dumps | full-resync
    etiquette: RateLimit           # per-origin max_conns, bandwidth cap, windows
    profiles: dict[str, Filter]    # demo | starter | recent | full  ← the UX lever
    estimated_bytes: dict[str,int]
```

**Profiles** are the biggest ease-of-use win: default install fetches `demo` (minutes), growing later is one command because acquisition is a first-class manifest stage (`fetch_done`, `fetch_version`) — interrupted downloads resume like everything else, and delta sync is the *same code path* as initial acquisition, driven by the existing mtime/size→hash gate. Engine-side, once: resumable transfers, checksum verification, disk-space preflight against `estimated_bytes`, a licence-acknowledgement gate for yellow sources, and **origin etiquette as enforced config rather than folklore** (PG asks for mirrors and rsync; NLM asks for limited concurrency).

Per-source current best forms: Gutenberg → rsync from a listed mirror with text-only excludes; PubMed → FTP baseline + `updatefiles/` with `.md5` sidecars (terms permit redistribution if the copy is kept current *or* labelled stale — the swarm satisfies the first branch by construction); MedCPT vectors → FTP, f32→f16 on import (~100→55 GB); Wikipedia vectors → HF Hub streaming; assemblies/ClinVar/HGNC → HTTPS dumps; gnomAD → public buckets, exomes-only profile ~2 GB.

### 5.2 Transfer manager — bandwidth budgeting and parallelism

Downloading, delta sync, and seeding all compete for one link — usually an asymmetric residential/NAS line where **the uplink is the scarce resource** and saturating it wrecks the downlink too (ACK starvation / bufferbloat). So bandwidth is a single, centrally scheduled budget, not per-tool caps:

- **One token-bucket pair (up/down) shared by every transport** — rsync, HTTPS workers, and the torrent client all draw from the same budget; libtorrent takes programmatic rate limits, CLI tools get wrapped (`--bwlimit`, or trickle) so nothing escapes the scheduler.
- **Priority classes**, preemptive: interactive queries & `lazy-p2p` fetches → delta sync → backfill acquisition → seeding. Seeding gets *leftover* bandwidth, never reserved bandwidth.
- **Defaults that protect the household:** upload cap ≈ 80% of measured uplink, a reserved interactive headroom (~20%), and seeding over **uTP/LEDBAT**, the delay-based congestion control built for exactly this — background transfers that yield automatically when the link is busy.
- **Time windows:** cron-style schedule (`01:00–07:00 full; else 30%`), composing with per-origin etiquette windows from the AcquisitionSpec.
- **Seeding governance:** global and per-torrent ratio/rate caps; replication-aware throttling (seed harder what has few seeders — ties into §5.3 health metrics).

**Parallelism** attacks the latency of large corpora on three axes, all bounded by the same budget:

1. **Across files** — a pool of M fetch workers pulls from a prioritized queue (PubMed's ~1,200 baseline files are embarrassingly parallel; Gutenberg shards across top-level directories as parallel rsync invocations). Concurrency per endpoint = `min(global_workers, endpoint.max_conns from etiquette)` — parallelism never overrides politeness.
2. **Across mirrors** — different files from different endpoints simultaneously; per-endpoint throughput tracked as an EWMA so the scheduler learns which mirrors are fast *for this peer* and shifts load accordingly. Torrent transport gets this for free (that is its native mode) — one more argument for the swarm as transport.
3. **Across pipeline stages** — download ∥ checksum ∥ decompress ∥ parse as overlapping stages, so CPU work hides behind IO instead of following it.

Segmented multi-connection downloads of a *single* file (aria2-style HTTP ranges) are supported but default-off against origins, enabled only where etiquette allows or against swarm/HF endpoints. Config surface:

```
[bandwidth]  down_max=400Mbit  up_max=40Mbit  reserve_interactive=20%
             schedule="01:00-07:00 full; else 30%"
[transfer]   workers=8  per_endpoint=4  segments_per_file=1
```

Dashboard: live up/down per priority class, per-job ETA, per-endpoint throughput — bandwidth becomes an observable, governed resource instead of whatever the last tool grabbed.

### 5.3 Preservation failsafe — the archive plane

Efficiency was the original motive for P2P; **availability is the stronger one.** PubMed, ClinVar, RefSeq, and the reference assemblies are US federal data — precisely the category that has recently faced politically motivated purges. If the origin disappears, the swarm *is* the corpus. Consequences for the design:

- **Priority inverts:** vectors are reproducible from raw + model; raw is irreplaceable. The failsafe budget goes to the raw plane first — the opposite of the bandwidth argument.
- **Mirror the model weights.** `space_id` pins a weights hash; if those weights vanish, the space becomes unextendable *and unverifiable* (T1/T3 need the model). Weights are archived beside the corpus — a few GB, cheap insurance.
- **Snapshots, not just current state.** Delta sync erases history; content-addressing makes retaining old snapshots nearly free (unchanged files shared across snapshots).
- **Archive origin bytes and schemas,** not just the parse: DTDs, RDF catalogs, GFF annotations. A FASTA without its annotation is a far poorer artifact.
- **Key continuity:** snapshot manifests signed by multiple keys or a threshold scheme — reconstruction must not depend on one person's key surviving.
- **Replication health as a first-class metric:** seeders per snapshot on the dashboard, alert below N; seeding scheduler biases toward under-replicated data.
- **Join, don't duplicate:** SciOp (Safeguarding Research & Culture) already runs a catalog+tracker for at-risk scientific data (~300 TiB, ~9,600 seeders) with RSS torrent feeds the acquisition layer can consume as endpoints, and is extending BitTorrent for evolving multi-origin data — the baseline+deltas problem exactly. Publish snapshots there; consume from there.

The reframing this buys: SciOp preserves bytes, but preserved bytes are inert. This platform is **a preserved corpus that stays queryable** — archive + retrieval + a maintained semantic index.

---

## 6. Verification ladder

Watermarking in the strict sense cannot work here: anyone with the model produces valid embeddings, so a model-derived mark separates nothing; a producer-added mark only duplicates the signature and degrades vectors; zkML costs orders of magnitude more than the embedding. **The embedder's own geometry is the watermark.** Five tiers, cheapest first, each gating the next. Costs below for a reference bundle of 10k chunks × 1k tokens on an 8B embedder (production ≈ 27 GPU-min = 1 unit).

| Tier | Check | Cost | Catches |
|---|---|---|---|
| **T0 algebraic** | dim/dtype match space; unit-norm within f16 tolerance; no NaN/dupes; per-dim moments and pairwise-cosine distribution inside the space envelope (embedding spaces are strongly anisotropic — foreign vectors betray themselves statistically) | ~10⁸ FLOPs, tens of ms CPU | missing normalization (the most common incompetence bug), random/foreign vectors |
| **T1 geometric** | cosine profile against a signed probe set (~512 reference texts embedded once by the space author); **overlap invariant** — consecutive chunks share ~120 tokens, so cos(vₖ,vₖ₊₁) must sit in a predictable band, keyed on the *measured* overlap of the local text (config-independent, survives a lying producer); probe-triangle Gram check *(corrected — see amendment)* | ~10¹⁰ FLOPs, ~0.1–1 s CPU | wrong model, perturbation. Faking the overlap band requires actually modelling the text — at which point honest embedding is cheaper |
| **T2 challenge** | golden (text, vector, per-backend tolerance) triples; producer embeds beacon-derived challenge texts into the bundle header. The beacon is HMAC-epoch over a root published *inside the signed space artifact* — offline-verifiable, no network service | +1.3 s producer, ~0 verifier | misconfiguration — "proof of correct oven, not each loaf". **Not a security control**: the root is public, so the golden bank is precomputable by a determined producer. Resist "hardening" it into beacon-*generated* challenge texts — the author then cannot pre-publish golden vectors, the verifier must embed them itself, and the free check becomes a GPU call (it becomes T3) |
| **T3 spot recompute** | n≈16 chunks, **private per-verifier randomness** (never a bundle-digest seed — that is grindable: corrupt fraction f, re-roll the nonce until the derived sample misses, expected (1−f)⁻ⁿ tries), sequential test, τ from the space's cross-backend calibration table (~0.999; the gap to a different model is 0.3–0.7, so the test is sharp) | ~3–5 s GPU = **0.16%** of production | bulk poisoning. Per importer (1−f)ⁿ survives; 10 independent importers at f=5% → 3×10⁻⁴ |
| **T4 verify-on-use** | a poisoned vector is inert until retrieval surfaces it — so recompute exactly the cited chunks, async via the existing lazy queue. Pass → mark verified forever. Fail → proof of forgery (text, claimed, recomputed), quarantine the key, re-embed locally | ~2–3 s GPU per answer, once per chunk ever cited; ~2% lifetime (query distributions are Zipfian — the hot set verifies in days, cold chunks never cost) | **targeted poisoning**, which random sampling cannot find |

**Complements:** ~5% deliberate overlap in work allocation so independent producers cover common chunks (disagreement localizes the liar without trusting either); trust-proportional sampling with new keys in probation; blocking mode for high-stakes deployments (verify before answering, ~2–3 s first-touch latency).

**Honest limits.** Exact-hash verification is impossible — the same weights on different GPUs/drivers/batch sizes yield different floats, hence the backend fingerprint and tolerance τ. The tolerance window is also the adversary's perturbation budget, so keep τ as tight as the fingerprint allows and treat "always passing just above τ" as a reputation flag. T4 is async: the first answer touching poison may already be wrong. But detection at first use is ~1, so the attacker trades one degraded answer for a permanently burned key — expected value negative.

*(Phase-1 amendments to this section.)*
- **"Semantic triangles" cannot prove fabrication for this wire format.** The
  Gram/PSD realizability condition (det G ≥ 0 over vector-probe-probe triples)
  holds *automatically* for any real vectors — the Gram matrix is XᵀX,
  positive semi-definite by construction; measured minimum over 200k random
  real triples was +0.12. It would discriminate only if bundles transmitted
  per-probe *cosines*, which a marginal-matching forger cannot make jointly
  consistent. Since bundles carry vectors, the check is kept as a cheap
  **integrity canary** (wrong-dtype/endianness decodes, arithmetic bugs) and
  must not be counted as anti-forgery coverage.
- **T3's n≈16 is a sampling *budget*, not an acceptance boundary.** At n=16
  against f=5%, the achievable per-importer miss probability is 0.95¹⁶ ≈ 0.44
  — and the row above *depends* on that (0.44¹⁰ ≈ 3×10⁻⁴ is exactly its
  ten-importer figure). The network argument carries the safety, not one
  importer's β. Implementations must report *delivered* error rates (exact
  enumeration over the truncated sequential test's lattice), never Wald's
  design targets, which ignore overshoot and truncation.
- **The overlap invariant is untestable against synthetic embeddings.** A
  deterministic text→vector mock cannot reproduce adjacent-chunk similarity,
  so end-to-end suites exercise every tier *except* the overlap band; only
  calibration against recorded vectors from a real model covers it. A known
  gap, not a discovered one.

**Net economics.** Honest-path import ≈ 1 s CPU + 5 s GPU ≈ **0.2% of producing the bundle** — verification is ~1/500th of production. Network total for M=50 consumers: everyone-embeds-locally = 50 units; shared = 1 + 0.05 overlap + 50×0.002 ≈ 1.15 (≈2.2 worst-case including lifetime T4) → **20–40× network saving**. That ratio is the justification for the whole design.

---

## 7. Community workload sharing

No standing "BOINC for embeddings" exists; every real instance (LAION, BOINC, Prime Intellect) is the same four verbs: **partition, claim, compute-and-upload, validate-and-aggregate**. Your manifest already *is* the work tracker — the community layer is that state machine exposed over HTTP.

**Coordinator** (per collection+space, ~200 lines FastAPI, holds no bulk data):

```
GET  /work?space=…&n=1000   → lease of a uid range, TTL 24h (advisory, not a lock)
POST /bundles               → signed upload, triggers T0–T2 immediately
GET  /have?space=…          → provided uid ranges (bloom/ranges)
GET  /demand?space=…        → aggregated lazy-queues of consumers
```

Leases can be advisory precisely because the work is idempotent: an expired-and-reissued lease wastes one GPU-hour and breaks nothing. That tolerance is what lets you skip BOINC's hardest machinery.

**Embed modes:** `off | lazy | lazy-p2p | all | contribute`.
- `lazy-p2p` — a query first asks the swarm, computes only on miss, publishes the result. Queries *feed* the network.
- `contribute` — `all` pointed at the network's demand-set. The overnight mode for strong GPUs; ranked by (demanded-but-unprovided) > (unprovided) > (low provider count), tie-broken by rendezvous hashing `h(peer_id, uid)` so peers spread without coordination.

*(Amended after Phase-1 integration.)* `embed_mode` governs what the worker
**spends** — lazy enqueueing, backfill — never what retrieval **uses**. Dense
search runs over whatever vectors exist regardless of mode. This sentence
exists because the two were once conflated: a consumer-role node (§14:
import-only, `embed_mode=off` *because* its vectors arrive from the network)
was the one node type that refused to search the vectors it imported.

**Distribution:** push accepted bundles to a versioned Hugging Face dataset repo per collection — free hosting, global CDN, and how NCBI and Upstash already ship vectors. The P2P swarm then becomes an *alternative transport*, not a prerequisite — which is why community sharing can land in Phase 1. *(Amended: the consumer side of every transport is the per-collection inbox directory defined in §5 — the coordinator itself never serves bundle bytes.)*

**Incentives v1:** credit ledger + leaderboard (BOINC's screenshots were half its marketing) and one functional perk — contributors' demand-sets get scheduling priority, so giving GPU-hours makes your own queries faster.

---

## 8. Genesis corpora and licensing

Two of three flagship collections can start from existing public artifacts:

| Corpus | Genesis artifact | State | Space |
|---|---|---|---|
| **PubMed** | NCBI MedCPT embeddings, all PubMed articles, ~37 × 2.7 GB `.npy` + PMID JSON, **public domain** | frozen Oct–Nov 2023, ~3M records stale | MedCPT, 768-dim, **bi-encoder**: article encoder for docs, query encoder for queries, both open. Cross-encoder reranker also free |
| **Wikipedia** | `Upstash/wikipedia-2024-06-bge-m3`, ~144M vectors, 11 languages, 1024-dim, text included, **Apache 2.0** | frozen June 2024 | BGE-M3 (MIT), open weights, revision-pinned |
| **Gutenberg** | none — the existing skeleton | live | operator's choice (qwen3-embedding etc.) |
| **Genome** | **none exists** — no open precomputed index of the human reference genome anywhere | — | Evo 2 (or DNABERT-2/NTv2, §10) |

Catch-up arithmetic: PubMed 2023→2026 ≈ 3M records ≈ 300 work units ≈ 135 GPU-h — a dozen volunteers over a weekend. Then ~20k records/week ≈ **<1 GPU-h/week** to stay current. That asymmetry is the pitch: the backfill was already paid by NCBI; sustaining it costs a coffee break.

*(Amended after Phase-1 integration — MedCPT.)* The genesis import is now
specified, not just named. It is **admission, not trust** — a published
archive gets the same skepticism as a peer bundle, minus the signature it
never had:

1. **Scoped by possession:** import only vectors whose PMIDs the collection
   actually holds (RAG needs the text; §8's pointer-only rule covers the
   rest). The importer keeps a cursor over the archive shards, so a corpus
   grown later re-sweeps only the new PMIDs.
2. **Convert on import:** f32 → f16 (§5.1's ~100→55 GB), L2-normalized
   client-side like every other vector in the store — the archive's raw
   layout never dictates storage.
3. **Admission checks:** T0 structural (dim=768, unit-norm, NaN/dupes,
   moment envelope) plus a **sample re-encode** of n texts through the
   collection's own `[embed]` endpoint at cosine ≥ the calibrated τ —
   proving the local encoder and the archive are the *same function*, which
   is precisely what makes gap-filling coherent. An archive that fails is
   refused whole, not partially imported.
4. **Provenance recorded:** the ledger rows carry the archive URL, shard,
   and content digest — quarantine works on genesis imports exactly as on
   bundles. The catch-up loop (local encoder over post-snapshot records) is
   ordinary embed-stage work in the same space; nothing about it is
   special-cased.

**License gating, per source and per plane** (`license_class` + `privacy_class` enforce it mechanically):

- **Green (public domain / CC0):** GRCh38 & T2T reference, RefSeq, ClinVar, dbSNP, gnomAD (recent releases), MedCPT vectors, HGNC. Model-organism references and MGI/FlyBase/ZFIN annotations.
- **Yellow (open with conditions):** GO, UniProt, Ensembl, TogoVar (attribution); Wikipedia text (CC BY-SA — share-alike, and whether embeddings are derivative works is unsettled); PubMed abstracts (free bulk download, publisher copyright on text → **raw pointer-only by default, vectors shared**); PMC OA (per-record license flag).
- **Red:** OMIM, GeneReviews (no redistribution). **Personal genomes — never, on either plane:** GDPR art. 9 special category, and DNA foundation-model embeddings have been shown invertible toward the underlying sequence, so sharing vectors would leak the genome. This is why `privacy_class` exists separately from `license_class`.
- **Models:** BGE-M3 (MIT), Qwen3-embedding (Apache), MedCPT (public domain) all clear. **Verify Nucleotide Transformer's weights licence before pinning it into a space** — historically non-commercial. A model's licence contaminates the usability of its entire space.
- **Regulatory:** keep the genomics collection explicitly research/education. Interpreting real patients' variants for clinical decisions would engage IVDR in the EU.

---

## 9. The genomics collection

**Why it fits the architecture almost too well:** a reference assembly is immutable and versioned — the most deterministic corpus imaginable; chunking is fixed overlapping windows, so the chunk id *is* a coordinate (`grch38:chr7:117480025-117481025:v1`); the raw plane is public-domain consortium data.

**First genuinely multi-modal collection.** Dense leg = sequence embeddings queried *by sequence* (Embed-Search-Align showed vector search over a DNA store rivals BWA-MEM for read placement). Catalog/FTS legs = annotation layers queried *by text*. Structured legs = ClinVar/gnomAD by key. Two incompatible embedding spaces plus exact lookups, merged only where merging is meaningful — RRF for the rankable, context blocks for the factual.

**Generation stage changes:** chat LLMs cannot read raw nucleotides usefully. The answer reasons over retrieved regions' *annotations and metadata*, or hands sequences to the DNA model itself for likelihood scoring.

**Worked query.** "What is known about BRCA2 c.658_659del?" → resolve identifier → exact ClinVar + gnomAD lookups → yields gene + coordinates → those keys query the PubMed collection (dense+BM25) and the genome collection (by region) → RRF over the texts → cited answer. Four separate databases, one join at query time. Only the resolution stage is new code.

**Multi-species extension** — the architecture holds; three adjustments, all in the entity layer:

1. **Species-qualified keys** (already shown above) — `TP53`/`Trp53` collide otherwise, coordinates mean nothing without an assembly, rsIDs are human by construction.
2. **Orthology is a new link type** — "human gene → mouse ortholog → knockout phenotype" is `expand()` fed by a new `ReferenceSource` (Ensembl Compara, HomoloGene, MGI/ZFIN). No orchestrator change.
3. **Species state in the query** — default human, explicit disambiguation when the resolver finds multiple candidates.

One collection per assembly, same plugin, same space (Evo 2 is trained across all domains of life, so cross-species search works natively — the cross-lingual trick again). Sizing: human 3.1 Gbp is near worst case among common models — mouse 2.7, zebrafish 1.4, **fly 0.14, yeast 0.012**. Fly and yeast cost GPU-minutes and are the right corpora to validate the plugin *before* spending human-scale hours. Licensing improves too: model-organism data is fully open and the privacy class doesn't apply, so non-human collections are *more* shareable. And model-organism communities are small, organized, and compute-poor — exactly who adopts a shared corpus rather than recomputing it.

---

## 10. Hardware placement (heterogeneity is the point)

Reference node: **Intel Arc Pro B70**, 32 GB, 608 GB/s, ~$949, oneAPI/OpenVINO/SYCL.

| Workload | B70? | Notes |
|---|---|---|
| MedCPT, BGE-M3, Qwen3-embedding | ✅ | GGUF via Ollama/llama.cpp SYCL backend |
| Cross-encoder rerank | ✅ | ~100–500 ms for ~40 candidates |
| Chat generation (20–30B quantized) | ✅ | ~20–40 tok/s → 15–40 s per answer; the real latency bottleneck, not retrieval |
| Verification T0–T2 | ✅ | CPU-only anyway |
| Verification T3/T4 on text spaces | ✅ | requires only an embed endpoint |
| DNABERT-2 / NTv2 sequence embedding | ✅ | ONNX Runtime + OpenVINO EP; 100–500M params; quality below Evo 2 but adequate for "find similar regions" |
| **Evo 2** | ❌ | Vortex requires CUDA PyTorch + Transformer Engine + Flash Attention, plus optional Triton kernels; StripedHyena 2's implicit convolutions have no ONNX/OpenVINO equivalents. The 7B "light install" is a long-shot worth an afternoon, not an architectural bet |

ZLUDA (CUDA-on-AMD) does not rescue Evo 2 — PyTorch support is still blocked on compiler speed and cuBLAS/cuDNN coverage, and it is AMD-only and back to hobby status since v6 (June 2026). It does mean **AMD peers can run GGUF text embedders at near-native speed via the translated CUDA backend of llama.cpp** — and because verification is tolerance-based, their "slightly different backend" vectors pass by design.

**This is the thesis in miniature:** the platform never requires that every peer can *compute* every space — only that every peer can *verify and use* what it imports. A B70 node is a contributor for three spaces out of four and a verifying consumer for the fourth.

### Cost summary for the aggregated genomics RAG

**Build (one-time):** Evo 2 7B over human 1kb windows ≈ 120–250 GPU-h (~25 GB f16); 40B ≈ 700–1500 GPU-h. gnomAD ingest hours of CPU/IO (30–80 GB; exomes-only: minutes, ~2 GB). ClinVar/RefSeq/HGNC minutes. RefSeq gene summaries in a text space ≈ 2 GPU-h on the B70. ANN index 1–2 h. **Total collection 60–160 GB**, and ~90% of the compute is the one piece delegable to the network.

**Per query:** resolve+expand <5 ms; structured lookups ~1 ms each even at 800M rows; lexical legs tens of ms; dense over 36M PubMed vectors with ANN ~20–50 ms + ~10 ms query encoding; RRF <1 ms; rerank 100–500 ms. Sequence dense leg only when the query contains DNA: 0.1–0.3 s on a CUDA peer or ~1 s via hosted API — the only runtime external dependency. **Multi-source join adds ~0.3–1 s; generation dominates at 15–40 s.**

**Steady state:** genome never changes (new assembly = new collection), ClinVar weekly = seconds, gnomAD ~annual = CPU hours, PubMed <1 GPU-h/week. **Maintaining the whole aggregate costs under an hour of GPU per week, all of it B70-capable.**

---

## 11. Roadmap

**Phase 0 — Generalize (no network).** Extract `CorpusSource`; add collections + `space_id`; port Gutenberg unchanged; implement PubMed as the proving second plugin (37M small records, container files, restrictive licence — stresses everything Gutenberg doesn't); add `ReferenceSource`, `EntityResolver`, `doc_keys`, and the resolve/expand/plan stage; query federation. Standalone value, and it validates every interface the network later depends on.

**Phase 1 — Bundles + community, no P2P.** Signed bundle export/import over HTTPS/Hugging Face. Verification ladder T0–T3. Coordinator with leases and demand-sets. `contribute` mode. **Deliverable that doubles as marketing: revive the PubMed corpus** (135 GPU-h catch-up, then weekly cron) and publish it as a maintained dataset. Two friends splitting a uid range already realize the core benefit.

**Phase 2 — Swarm.** BitTorrent v2 raw plane with signed snapshots and origin webseeds; bundle index with have/demand; `lazy-p2p`; T4 verify-on-use; archive plane live — snapshots + weights published to SciOp, replication-health metrics and under-replication-biased seeding.

*(The acquisition layer with profiles and the transfer manager land earlier: AcquisitionSpec + profiles + delta-as-fetch in Phase 0, since the fetch stage needs them anyway; bandwidth budget, priority classes, and parallel fetch workers in Phase 1, when seeding and bulk catch-up first compete for the link.)*

**Phase 3 — Decentralize and harden.** DHT/gossip replacing the tracker; reputation ledger; cross-producer comparison; community-signed canonical spaces to fight fragmentation.

---

## 12. Go-to-market (realistic)

1. **Be excellent with zero other users.** The app is already a genuinely useful solo NAS tool — that is the escape from the cold-start trap. Nobody installs it *for* the network. Protect this: 10-minute Docker quickstart, TrueNAS catalog entry, polished Open WebUI integration.
2. **The artifact is the advertisement.** Publish the revived corpus, not the architecture. Upstash's frozen dump pulls ~2,000 downloads/month with no promotion; a *maintained* one wins on the only axis people search. Every dataset page recruits: "kept current by [project] — run one container to help."
3. **Narrative:** "embedding datasets die the day their sponsor stops paying; here is the first one brought back to life, and the system that keeps it alive." Venues: Show HN, r/selfhosted, r/LocalLLaMA, r/DataHoarder, Ollama/Open WebUI Discords, Academic Torrents, a JOSS paper for citability. Each plugin recruits its own niche (bioNLP, digital humanities, model-organism communities).
4. **Expectations.** Base rate for OSS infrastructure launches is silence. **Thirty contributors in year one would be a success** — already enough to keep PubMed and Wikipedia current forever. Measure weekly active workers and bundle downloads, not stars. Assume you are the entire community for six months. Don't build the DHT until someone begs.
5. **Naming:** pick a neutral, searchable project name — "Gutenberg" is a Project Gutenberg trademark; the plugin may carry it, the platform must not.

---

## 13. Open problems

- **Space proliferation** — every new model splits community effort; canonical-space recommendations concentrate it, old spaces age out.
- **Rerank cost at federation scale** — the latency hot spot; per-collection candidate budgets and rerank-on-demand are the levers.
- **Incentives** — tit-for-tat doesn't map to small bursty bundles; goodwill + demand-priority for v1, nothing stronger in scope.
- **ANN indexes are local** — built over imported vectors, never shared; only vectors travel.
- **Subtle poisoning** — T3+T4+overlap bound it, don't eliminate it.
- **Churn/NAT** — mitigated by webseeds and tracker-mediated relay.
- **Legal gray zones** — per-source, per-plane switches with conservative defaults; the underlying questions remain unsettled.
- **Evo 2 portability** — no non-CUDA path today; either community bundles, hosted API, or accept DNABERT-2/NTv2 as the open sequence space.

---

## 14. Configuration & control plane

**Principle: configuration is desired state, control is operations on running state.** Every knob belongs to exactly one of three tiers, sorted by rate of change.

**Tier 1 — identity & infrastructure** (env vars, restart to change): paths, ports, model endpoints (`CHAT_URL`, `EMBED_URL`, `RERANK_URL`), node key *location* (never key material), coordinator/tracker URLs. The existing `config.py` pattern, unchanged.

**Tier 2 — declarative desired state** (versioned files — diffable, backupable; a node restores from `git clone + compose up`):

```toml
# platform.toml
[node]      name="mynas"  role="contributor"   # solo|consumer|contributor|archivist
[bandwidth] down_max="400Mbit" up_max="40Mbit" schedule="01:00-07:00 full; else 30%"
[network]   coordinator="…" hf_repo="…" trackers=["sciop","…"]
[trust]     allowlist=["ncbi.pub","upstash.pub"] probation_t4_rate=1.0

# collections/pubmed.toml
plugin="pubmed"  profile="recent"  space="medcpt@sha256:…"
embed_mode="lazy-p2p"  share_raw=true  share_vectors=true
delta="daily@03:00"  license_ack="2026-08-16T…/sha256-of-terms"

[embed]                              # (Amended after Phase-1: MedCPT integration)
url="http://nas:30440"  api="openai"  model="medcpt-article"
dtype="f16"  ctx="512"  dim=768
query_url="http://nas:30441"         # bi-encoder pairs: queries use the OTHER encoder
concurrency=4                        # documents embedded in parallel — NOT space
                                     # identity (changes no vector); >1 belongs to
                                     # CPU encoders; GPU llama-server stays serial
                                     # (its multi-sequence handling is the defect
                                     # the batch gate guards against)
```

*(Amended after Phase-1 integration — MedCPT.)* The per-collection `[embed]`
block realizes what §3's "collection = (…, embedding space)" already implied
but Tier 1's single global `EMBED_URL` made unconfigurable: a node holding a
Qwen3-space Gutenberg **and** a MedCPT-space PubMed needs one embedding
recipe *per collection*. Semantics:

- **Fallback, not override:** a collection without an `[embed]` block uses
  the Tier-1 env recipe unchanged — existing single-space deployments are
  untouched. With the block present, every field is read from the TOML and
  the env plays no part for that collection.
- **Immutability is still the pin's job** (§3, invariant 1): the block
  states desired identity; the pinned `space_id` + boot validation decide
  whether the vectors on disk agree, and refuse to boot when they don't.
  Editing the block is exactly as consequential as editing `EMBED_MODEL`
  always was — the pin catches it, never silence.
- **`query_url` names the bi-encoder asymmetry** (MedCPT: article encoder
  for documents, query encoder for queries — `query_encoder_hash` was in
  the `space_id` recipe from day one). Symmetric spaces omit it.
- **The behavioral backend gate baselines per collection** against that
  collection's endpoint — a swapped MedCPT container is caught exactly like
  a swapped llama.cpp build, independently per space.

`role` is the UX multiplier — one word presets a dozen knobs (solo = network off; consumer = import-only; contributor = seed + contribute mode; archivist = seed everything, replication-biased) and maps 1:1 onto the go-to-market personas. Onboarding = "pick your role."

**Tier 3 — runtime operations** (DB-backed, ephemeral verbs): pause/resume worker, narrow scope, quarantine a key, boost bandwidth tonight, force-verify a bundle, publish a snapshot. On restart the files win; live overrides display *as* overrides. This kills config drift by construction.

One settings service, three frontends — CLI, dashboard, authenticated `/admin` API (strictly separate from the OpenAI-compatible query endpoint); anything clickable is scriptable. Safety rails from day one: `--dry-run` on every acquisitive/destructive verb; **boot-time validation that refuses to start on inconsistency** (vector-table dim ≠ pinned `space_id` is refuse-to-boot, not a warning — silently mixed spaces are the one unrecoverable corruption); typed confirmation for irreversible drops; licence acknowledgements recorded as timestamp + hash of the terms text.

---

## 15. Web interface design

**One app, two personas, four areas.** The librarian (owns the node) and the reader (asks questions) — often the same person in different moods. The UI splits accordingly: **Ask** for readers; **Library / Network / System** for the librarian. Server-rendered (FastAPI + Jinja + htmx + SSE), no SPA build step — dashboards are state displays, and htmx-over-SSE handles the live parts. Read-only by default on the LAN; librarian areas behind auth (Tier-3 verbs need it, matching the `/admin` API).

**Ask** — chat with receipts. Answer pane with `[n]` citations; a **provenance drawer** per citation (chunk text, uid, collection, producer key, verification badge: ✓ verified / ◌ imported-unverified / ⚠ probation — T4 made trust *visible*, the UI shows it); collection scope chips (`gutenberg ✓ pubmed ✓ genome ✓`); an **entity strip** showing what resolve/expand found (`BRCA2 → hgnc:1101 → 13q13.1`, ClinVar/gnomAD facts as pinned cards above ranked results, visually distinct because lookups are facts, not hits); query-translation fan-out visible on tap. A sequence-paste box switches the genome leg into by-sequence mode.

**Library** — one shelf-card per collection: four stage bars (fetched / parsed / indexed / embedded — acquisition is a first-class stage, so corpus completeness sits beside index completeness), profile and space badges (`recent · medcpt@7f3a…`), delta freshness ("current through yesterday" — the licence disclaimer state, automated), lazy-queue depth. Card actions = Tier-3 verbs with `--dry-run` previews: grow profile, reprocess stage, pause. Add-collection wizard = plugin picker → profile ladder with size/time estimates → licence text with recorded acknowledgement → dry-run summary → go.

**Network** — the trust and traffic room. Bandwidth live-graph by priority class (interactive/delta/backfill/seeding) with the schedule overlaid; seeding table (ratio, peers, replication health, under-replicated highlighted); bundle ledger in/out with per-producer spot-check pass rates and one-click quarantine (typed confirmation); contribution panel (leased ranges, ETA, credit, leaderboard position); demand heatmap of what the network wants that this node could compute.

**System** — config viewer showing *effective* state per tier (file value, env override, live Tier-3 override, each labelled), worker logs, model-endpoint health pings, boot-validation report, backup/restore of the config directory.

**Design rules:** every panel is the state of a manifest table, not a bespoke store — the UI is a *view* over the same state machine the CLI drives (htmx polling/SSE on manifest changes). Every action shows the CLI equivalent in its confirmation dialog, which teaches automation for free and keeps the three frontends honest. Verification state renders as badges everywhere a producer or bundle appears. And the reader's view never requires understanding the librarian's — a guest on the LAN can ask questions and inspect citations without ever seeing a bandwidth graph.

**Build note:** the first version is the existing dashboard plus four pages; nothing here requires a frontend framework, and every widget maps to a table that already exists in the design (documents, bundles, providers, leases, settings).

---

## 16. Packaging & deployment

**The portability contract:** one OCI image (multi-arch amd64+arm64, GHCR), three mounts, env vars. Everything else follows from two prior decisions — the app never touches the GPU (model runtimes are other containers, reached by URL) and all state is embedded files (SQLite, Tantivy, LanceDB, TOML): no database server, no daemon dependencies, no platform hooks.

```yaml
# docker-compose.yml — the canonical deployment unit
services:
  web:      # dashboard + /v1 + /admin
    image: ghcr.io/PROJECT/app:latest
    environment: [ CHAT_URL, EMBED_URL, RERANK_URL, PUID, PGID ]
    volumes:
      - ${CORPUS}:/corpus:ro
      - ${DATA}:/data
      - ${CONFIG}:/config
    ports: ["8080:8080"]
    healthcheck: { test: ["CMD","curl","-f","http://localhost:8080/healthz"] }
  worker:   # ingestion; same image, same mounts, command: worker
  exchange: # torrent client + bundle store; one forwarded port, UPnP optional
```

Rules that keep it portable: no privileged mode, no host networking; PUID/PGID honored (the NAS permission convention); **never bake weights or corpora into images** — data arrives via the acquisition layer; compose is the reference artifact (a Helm chart may exist; Kubernetes is not the primary target). Secondary path: `pipx install` for Docker-allergic users — documented, not reference.

**Hard constraint to surface early: `/data` must be a local filesystem.** SQLite WAL and LanceDB do not tolerate SMB/NFS. `/corpus` read-only over NFS is fine. On a NAS this is naturally satisfied; the warning exists for desktop users binding network shares.

**TrueNAS as the worked example (never a dependency):** SCALE ≥24.10 is Docker-native → day one = Apps → Custom App → paste compose; later, a community-catalog entry whose form maps onto the same env vars. Recommended dataset layout: one ZFS dataset per mount — `corpus` with `compression=zstd`, large recordsize (public-domain text compresses superbly); `data` on a snapshot schedule (versioned backup of every index and manifest for free; ZFS replication = personal offsite failsafe, recursing §5.3); `config` tiny and snapshotted. GPU (e.g. Arc via `/dev/dri`) belongs to the Ollama/llama.cpp container, not the app.

**Acceptance criterion for portability:** a node restores from `git clone` of its config + `docker compose up` + re-run of acquisition — on any host.

### 16.1 Why both SQLite and LanceDB

They are not two databases doing one job; they are three engines (with Tantivy) doing three jobs no single embedded store does well:

| Concern | SQLite (manifest + refs) | LanceDB (vectors) | Tantivy (lexical) |
|---|---|---|---|
| Data shape | rows, keys, transactions | dense float matrices | inverted term index |
| Query | exact/by-key, joins, ACID state machine | ANN / brute-force cosine | BM25 ranking |
| Access pattern | many small transactional writes | bulk append, columnar scan | segment merge, term seek |
| Role here | stage flags, doc_keys, bundles ledger, ClinVar/gnomAD lookups | embedding storage + dense search | per-language BM25, catalog FTS |

The manifest needs what SQLite is: real transactions (delete-then-insert idempotence *is* a transaction pattern), crash-safe WAL, sub-ms key lookups at hundreds of millions of rows — and §4's rule that lookups are facts-by-key makes ClinVar/gnomAD natural SQLite tables. Vectors need what Lance is: columnar storage where a million 1024-d f16 vectors are a compact scan target, ANN indexing, zero-copy Arrow in/out — which is also why bundles are Parquet: import is a columnar append, not row-by-row INSERTs. Storing vectors as SQLite blobs forfeits ANN, vectorized scans, and memory-mapping; storing workflow state in Lance forfeits transactions and fast point updates. The split is also the P2P boundary: **SQLite state is local truth and never travels; Lance content is derived work and is exactly what travels** (as Parquet). One store optimized for both would be optimized for neither — the "one database" that remains is the query planner's illusion, delivered by RRF + the entity layer.

---

## 17. Phase-1 amendments

This document was corrected in place after the Phase-1 integration pass; the
amendments are marked *(Amended after Phase-1 integration)* at the point of
change. Index, with the evidence behind each in
[`phase1-integration-notes.md`](phase1-integration-notes.md):

1. **§5** — bundle format gains `possession.json` (per-uid raw hashes: vectors
   bind to a text *revision*); signature is domain-separated and length-framed;
   the consumer-side **delivery channel** (inbox directory) is now named — the
   coordinator never serves bundle bytes.
2. **§3** — `space_id` does not hash `dim` or chunk geometry; the compensating
   import checks (four-way dim comparison, chunk-id set equality) are now part
   of the spec.
3. **§6 T1** — the "semantic triangles" claim corrected: Gram/PSD holds
   automatically for transmitted *vectors*; kept as an integrity canary, not
   anti-forgery coverage. Overlap band keyed on *measured* local overlap.
4. **§6 T2** — the beacon is defined (HMAC-epoch over a root in the signed
   space artifact) and explicitly labeled not-a-security-control, with the
   warning against "hardening" it into T3.
5. **§6 T3** — n≈16 is a budget, not an acceptance boundary (β≈0.44 at f=5%);
   report delivered error rates, never Wald's design targets.
6. **§7** — `embed_mode` governs worker *spending*, never retrieval *use*.
7. **§6 honest limits** — the overlap invariant is untestable against
   synthetic embeddings; recorded-real-vector calibration is the only cover.
8. **CLAUDE.md invariant 2** — sub-document checkpoint granularity
   acknowledged (mid-file acquisition, mid-bundle import).

Amended again for the MedCPT integration (multi-space nodes), evidence in
the live NAS deployment:

9. **§14 Tier 2** — collections gain a per-collection `[embed]` block
   (endpoint, api, model, dtype, ctx, dim, and `query_url` for bi-encoder
   pairs); Tier-1 env stays the fallback for collections that declare none.
   The single global `EMBED_URL` could not express one node holding a
   Qwen3-space collection and a MedCPT-space collection at once. The space
   pin and boot validation still own immutability; the backend gate
   baselines per collection.
10. **§8** — the genesis import (NCBI MedCPT archive) is specified as
    admission-not-trust: possession-scoped (held PMIDs only, cursor for
    later growth), f32→f16 + L2 on import, T0 + sample re-encode against
    the collection's own encoder (same-function proof), refused whole on
    failure, ledger-recorded with archive provenance. Catch-up embedding of
    post-snapshot records is ordinary embed-stage work in the same space.

Amended again for pre-embedded corpora (Wikipedia/BGE-M3 reuse), evidence
in the live deployment:

11. **§2/§8 — the pre-embedded corpus plugin (`embedded`) and local genesis
    archives.** The MedCPT import worked because NCBI's unit (one abstract,
    one vector) is a unit our own chunker reproduces exactly. A third-party
    dataset chunked by *someone else's* pipeline over *someone else's*
    snapshot can never be re-derived from our own cleaning of a different
    dump — so instead of mirroring a foreign chunker, the archive becomes
    the corpus. **Format v1** (what a publisher ships to be importable):
    Parquet shards, columns `id` (string, `"{doc}_{seq}"`, rows of one
    document consecutive), `url`, `title`, `text` (the chunk text), and
    `embedding` (fixed-width float list). Text travels WITH vectors, which
    is what keeps possession, FTS, and RAG-with-citations intact.
    Consequences, each mechanical:
    - **`verbatim` chunking profile:** one section = one chunk, no
      windowing, no merge, no split. Chunk shape equals the producer's *by
      construction*, so the chunk-shape gate reduces to a row count check.
      Determinism (invariant 3) holds trivially: chunking is the identity
      function over an immutable corpus.
    - **The producer's embedding input IS the chunk text.** Producers that
      title-prefix their paragraphs (Upstash does) are mirrored by baking
      the same prefix into the chunk text at parse time — so the sample
      re-encode compares like for like, local gap-filling stays coherent,
      and FTS simply also indexes the title per paragraph. Queries are
      never prefixed (symmetric encoder).
    - **Local archives:** the genesis adapter reads vectors from the same
      corpus shards already on disk — no second download, no settle-wait
      (nothing can be "stranded on the local-GPU path" when corpus and
      archive are one artifact), and shards are *revisited* (oldest first)
      instead of applied once, because documents become import-eligible
      gradually as rdf/fts progresses. Idempotent by the same
      delete-then-insert as everything else.
    - **Truncation-aware sampling:** the local serving stack may truncate
      shorter than the producer did (e.g. a CPU TEI capped at 2048 vs
      their 8192); re-encode samples are drawn from chunks inside the
      local budget, where the two functions must agree. Over-budget chunks
      still import — their archived vector is strictly better than what
      the local encoder could produce for them.
    - **License is the CONTENT's license.** A dataset repo's metadata
      license (Upstash says Apache-2.0) does not relicense the Wikipedia
      text inside it; the plugin stays CC BY-SA yellow with the ack gate.
    The admission spine is unchanged and non-negotiable: T0 structural +
    degenerate checks + sample re-encode through the collection's own
    `[embed]` recipe, refused whole, ledger-recorded. This amendment is the
    generalization §8 promised — "any compatible embedded data can join the
    platform the same way" — now with the format that makes it true.

Amended again after a day of running four collections at full corpus
scale. Every item below was forced by something observed live, not by
design taste:

12. **§2 — `DocRef.scope` separates a broken DOCUMENT from an
    unenumerable CONTAINER.** The tolerance rule ("a corrupt container
    costs one error ref, never a dead scan") stored container failures as
    documents keyed on the FILENAME, and nothing ever removed them: 264
    orphan rows on PubMed, 116 of them for containers that had since
    enumerated perfectly, each re-erroring every round after an operator
    pressed "clear errors & retry". A container-scope ref is a scan
    REPORT — logged, never a document — so a later clean scan heals it by
    simply not reporting it again. The same rule retires Gutenberg's
    empty book directories (rsync creates the tree, then copies only what
    the filter matches, so an epub-only title leaves an empty directory):
    ~34k of them were documents that could never be indexed and could
    never be retried away.
13. **§5.1 — `AcquisitionSpec.profile_mode`.** Profiles are not always a
    ladder. *incremental* (demo ⊂ starter ⊂ full) is the original
    meaning and still governs Gutenberg/PubMed/Wikipedia; *exclusive*
    marks profiles that must NEVER be combined — genome, where each
    profile is an assembly and §9's "one collection per assembly" is
    load-bearing, because `native_id` is the record ordinal WITHIN a
    FASTA and a second assembly silently overwrites the first (observed:
    yeast titles on human locators with yeast vectors attached);
    *additive* marks profiles that combine freely — the pre-embedded
    corpus, where each profile is a language and the plugin encodes the
    language into `native_id`, written as `"tr,it,en"` and resolved as
    the union. Growing an exclusive source is refused; growing an
    additive one ADDS a slice rather than replacing it.
14. **§14 — `[embed] device` is descriptive, never identity.** A
    collection may declare `device = "gpu"|"cpu"` so the UI can say which
    resource "turn embedding on" actually spends. It is deliberately NOT
    part of `space_id`: the same model on a different device computes the
    same function, which is exactly what the live cross-backend
    calibration measured (cos 0.9999997 GPU-SYCL vs CPU).
15. **§7 — node-level control state follows the NODE.** `worker_state`
    and the bandwidth override live in the control table of whichever
    collection resolves first, which is an accident of sort order.
    Wiping that collection's derived data (a legitimate repair) emptied
    it, `worker_state` fell back to its default `paused`, and the entire
    node stopped — five collections, no log line. The control collection
    now adopts node keys from a sibling when it has none; per-collection
    keys (`scope`, the gate verdict) are never adopted.
16. **§6 — the backend gate's verdict is published, not just cached.**
    It lived in the worker process, so a paused encoder was
    indistinguishable in the UI from a stalled progress bar. The
    transition is written to the collection's control table (runtime
    state, like `worker_state` — never desired state, invariant 10) and
    the Library card shows the reason.
17. **§8 — genesis-imported vectors carry imported provenance.**
    Verify-on-use reads "no `chunk_verification` row" as *local ✓*, and
    the genesis path wrote none, so every NCBI/Upstash vector claimed
    this node had computed it. They are marked `imported` with the
    archive as producer, which also makes them eligible for
    verify-on-use. Additionally, a LOCAL archive's shards go *barren*
    when a pass imports nothing and yield the round to the local
    encoder — otherwise a residual candidate the archive can never cover
    (a shard-boundary straddler) makes the drain claim every round
    forever and the gap-fill amendment 11 promises never happens.
18. **§5.1 — acquisition ADOPTS files already on disk.** A corpus arrives
    by routes the engine does not control: a dataset copied from another
    machine, a `zfs send` of someone else's mirror, files staged by hand
    before the collection existed. A destination file present at exactly
    the origin's declared size is now verified and recorded rather than
    re-downloaded — re-fetching 238 GB that is already on the disk is a
    week of somebody's uplink for no change in state. The shortcut is
    past the *transfer* only: adoption runs the same
    `checksum.digest_and_verify` a fetch runs, and a file that fails it
    goes back on the download path. `AcquisitionPlan` reports
    `files_adoptable` / `bytes_adoptable`, and those bytes are excluded
    from `bytes_pending`, so the ETA and the disk preflight describe the
    bytes that will actually move.
19. **§14 Tier 2 — per-PLUGIN default embed recipes.** platform.toml
    gains `[embed_defaults.<plugin>]`, resolved as: the collection's own
    `[embed]` block, then the node's per-plugin default, then Tier-1 env
    — each step a fallback for the one before it, never an override
    (invariant 10). Amendment 9 made the recipe per collection; this
    covers the gap it left, which is what a collection gets at BIRTH.
    Env is the node's text encoder, and not every plugin embeds text: two
    genome collections created from the UI pinned a Qwen3 text space over
    DNA, which is not a worse choice but a meaningless one. The endpoint
    URL is deployment-specific, so this is a node file and never repo
    content.
20. **§15 — a collection is created against a chosen corpus folder or not
    at all.** The create flow's "(default — under the shared corpus
    root)" invented a path present in no container; the collection was
    born pointing at nothing and said so only one discovery round later,
    as "corpus dir not found". Selection is now mandatory in both the
    form and the endpoint, and the refusal names the free mounts.
21. **§9 — the assembly catalogue is the full UCSC set.** 25 assemblies
    (yeast through opossum) from one table that feeds `static_files`,
    `profiles` and `estimated_bytes` together, with real probed
    Content-Lengths rather than estimates — the budget planner shows
    those numbers to an operator before a fetch, and a guessed one is a
    lie they plan a night's download around. `profile_mode="exclusive"`
    is unchanged: a profile is an assembly, and a second assembly in one
    corpus collides on `native_id`. `AcquisitionSpec.profile_labels`
    carries the organism, since "monDom5" is a correct and unreadable
    name for a possum.
22. **§5.2 — the third parallelism axis is implemented, still
    default-off.** `transfer.segments_per_file > 1` splits ONE file
    across concurrent Range requests, written straight into their final
    offsets in a preallocated `.part` (concatenating per-segment files
    would need a second full-size copy on disk — 110 GB transient for a
    55 GB dump — and a second pass over every byte). Per-segment resume
    therefore cannot be inferred from file length and lives in a
    `<file>.segments.json` sidecar, written *after* the bytes it
    describes are fsynced so a crash can only leave it lagging;
    re-downloading a lagging segment rewrites identical bytes at
    identical offsets. Segment threads copy the transfer ContextVar
    explicitly — threads do not inherit it, and without that every
    segment would meter itself as an unbound BACKFILL caller and ignore
    SIGTERM. Segment count is capped by the endpoint's existing
    connection allowance, so splitting a file can never smuggle in more
    sockets than politeness already permits, and by a hard ceiling of 8.
23. **§15 — the Network page reports both processes.** A token bucket is
    per-process state and the bytes an operator cares about move in the
    *worker*, so the bandwidth panel showed a flat zero line beside a
    saturated link. The worker publishes its per-class rates to the
    control table each loop (telemetry, not desired state) and the web
    process sums them in; a sample past its max age is dropped rather
    than carried forward, because a frozen number on a live graph reads
    as traffic that is not happening.
24. **§6 / CLAUDE.md style — Greek is stemmed by PyStemmer.** tantivy
    vendors rust-stemmers 1.2.0, dormant since 2019, whose Greek
    algorithm panics on ordinary words; a panic crossing the FFI
    boundary costs the whole document. Greek moved to libstemmer in our
    own process (`config.PYSTEM_LANGS`), which stems the crashing word
    and agrees with rust-stemmers everywhere rust-stemmers works. Adding
    a language to that set changes its index schema, so the mechanism is
    general but the list is short by choice — see
    [`upstream-rust-stemmers-greek.md`](upstream-rust-stemmers-greek.md).
25. **§5 — the torrent transport is implemented (BitTorrent v2).** No
    longer a stub: `list_files` enumerates a torrent's tree with real sizes,
    `fetch_one` pulls exactly one file out of it, and
    `app/acquire/torrent_publish.py` turns a corpus this node holds into a
    `.torrent` plus a magnet, with `app.cli torrent publish|seed`. Three
    things the shape had to get right, each found by a failing test rather
    than by reasoning: libtorrent writes into FINAL paths as pieces arrive,
    so every file is renamed to `<name>.part` inside the torrent and
    promoted with os.replace — and because `rename_file` is asynchronous
    and `torrent_file()` is immutable metadata that never reflects it, the
    rename is awaited via `file_renamed_alert` and the stored name is
    computed, never read back. BitTorrent pads every file up to a piece
    boundary with synthetic `.pad/NNNN` entries (that alignment is exactly
    what lets snapshots share unchanged files), and they are excluded
    everywhere — they are not content and would otherwise become
    fetch_files rows. Rate limiting reuses the rsync lease: libtorrent has
    its own limiter and cannot be metered per byte, so it is handed a slice
    of the central budget rather than exempted from it (invariant 5). The
    session is process-global so completed torrents keep SEEDING; a node
    that only downloads is a leech. libtorrent is an optional, lazily
    imported dependency, so a deployment with no torrent-backed collection
    never loads it.
26. **§5 — peer and collection discovery is designed, not built.** See
    [`p2p-discovery.md`](p2p-discovery.md). The load-bearing distinction:
    a DHT finds peers for an INFOHASH and can say nothing about whether
    that infohash holds a corpus in your embedding space under a licence
    you may redistribute — that is a catalogue question and must be
    answered first. The proposal is a signed per-node catalogue (reusing
    the existing bundle signing key) whose first field is `space_id`, with
    peers found through an explicit pinned list first, the existing
    coordinator as a rendezvous second, and the DHT only once an infohash
    is in hand.
27. **§2 — a source declares whether it REVISES what it publishes.**
    `plugins/base.Mutability` is IMMUTABLE or MUTABLE, and it is not a
    statement about how often a source changes — Project Gutenberg adds
    books constantly — but about whether existing documents are edited in
    place. That is the only case where a chunk boundary moving matters, so
    it is what selects the chunker. Today only wikipedia is MUTABLE; a
    source that is mostly-immutable-but-occasionally-revised belongs in
    IMMUTABLE, because this switch is about the cost of re-chunking rather
    than the fact of change.
28. **§3 — the chunk version is scoped PER CHUNKER, not per node.** It was
    one global `CHUNK_VERSION` entering every collection's `space_id`, so
    changing how TEXT is chunked would have re-pinned collections whose
    chunker never changed — including the pre-embedded ones, whose vectors
    were imported against a *producer's* boundaries and cannot be
    recomputed by re-chunking at all. Fixed-width chunkers still report
    exactly `config.CHUNK_VERSION`, so every existing space id is unchanged
    to the byte, and adopting a new chunker costs only the collections that
    adopt it.
29. **§3 — content-defined chunking for mutable sources.** Fixed-width
    boundaries are counted from the start of the text, so inserting one
    paragraph shifts every boundary after it: a typo fix re-embeds a whole
    document, and a monthly delta costs the corpus rather than the edits.
    `chunk_cdc` cuts on a gear-hash rolling window (FastCDC-style
    normalized chunking, min 1,000 / target 4,000 / max 8,000 characters,
    the maximum bounded by the encoder's context). Measured on synthetic
    prose: inserting 25 characters near the start changed **1 of 71**
    chunks, against **80 of 80** for fixed windows.
    Two deliberate properties. Sections are chunked independently, so an
    edit in one cannot move a boundary in another even in principle. And
    there is NO overlap between CDC chunks — overlap would make each chunk
    depend on its neighbour's tail, which is the coupling the scheme exists
    to remove; a passage straddling a boundary is therefore split, which
    the lexical leg and RRF partly absorb. Determinism (invariant 3) holds:
    the gear table is derived from a fixed constant by blake2b, never from
    `random.*`, so it is identical on every machine and Python version.
    MIGRATION: this changes wikipedia's `space_id`. Its pin must be reset
    and its documents re-chunked (fts + embed) before it will boot. Doing
    it before the enwiki backfill runs is the difference between minutes
    and months, which is why it was done now.

Amended after the invariants review (2026-08-23), evidence in STATUS.md and
the live deployment:

30. **CLAUDE.md — the invariants re-read against what was built.** Each
    invariant now names how it is enforced (*boot* / *test* / *review*),
    because the ones enforced by review alone were the ones that drifted.
    Corrections: (1) the inline list of `space_id` components was stale
    (amendments 2, 14, 28) and is replaced by a pointer to §3; (2) gains the
    mark-after-commit ordering and the bounded-unit note; (3) names the
    chunker, not a global version knob; (4) flags the pending third fusion
    category from the DNA axes work; (6) records that `catalog_fts` is
    rowid-keyed and nothing VACUUMs; (8) extends from key material to
    secrets and adds `ADMIN_TOKEN_FILE` (path wins over value; a
    set-but-unreadable file is a startup error naming the path); (9) records
    that terms text is byte-exact; (10) gains the concrete rule that every
    Tier-3 override expires or is reset when the worker applies the files.
    A short list of **engineering rules** joins CLAUDE.md — each one a
    recorded incident: per-round cost scales with work not lists; "did work"
    is not "made progress"; selection queries are plan-checked; deferral is
    not failure; caches key on paths not names; telemetry from another
    thread; every log line names its collection; measure first.
31. **§7 / §14 — `embed_mode` obeys the tiers.** The TOML's `embed_mode`
    was parsed and only displayed; the Library's override persisted across
    restarts with no expiry — Tier 2 decorative, Tier 3 acting as desired
    state. Now `db.apply_desired_embed_mode` makes the TOML (or the default,
    `lazy`) effective on restart, on add and on a TOML edit, logging the
    replaced override and where to make a choice permanent; the Library
    says the override lasts until then, and labels the control "local
    gap-fill" on a collection with a genesis archive, whose import runs
    regardless of it (amendment 6). The reference deployment's TOMLs were
    written with their effective modes first, so the change was a no-op
    there. Tests: `tests/test_config_tiers_embed_mode.py`,
    `tests/test_invariant_guards.py`.
    CORRECTED one day later: runtime-only was invariant-correct and
    operationally wrong — seven restarts in a day silently reset the
    operator's choice seven times. The Library control now WRITES the TOML
    (a Tier-2 edit through the UI, like create and the license ack) plus
    the scope for immediate effect, so it survives restarts; POST
    /api/scope remains the runtime-only override. The same rewrite-carry
    rule gained `snapshot` (a pin dropped by a rewrite would silently
    unpin the collection back to "latest").
32. **§2 / invariant 2 — discovery is a batched, resumable, off-thread
    stage.** The scan used to be the one stage whose unit was the corpus:
    it held the collection's pipeline thread for its duration, ran in
    autocommit (one transaction per row — wikivec: 1,083 s for 1.5 M rows,
    nearly all SQLite overhead), and saved its cursor once at the end, so a
    restart mid-scan discarded the pass. Now `sync_collection` commits
    every `DISCOVER_BATCH_REFS` refs and reports progress to the plugin
    (`DiscoverState.received` / `committed` / `on_commit`); a plugin STAGES
    a container when it has yielded it and `cursor.Progress` PROMOTES it
    into the saved cursor only once its last ref is committed — the cursor
    can never get ahead of the manifest, so an interrupted scan resumes
    from exactly the containers whose rows are durable; and the worker
    runs each collection's scan on its own thread, one at a time, stopping
    at the next ref on SIGTERM or removal with the batch committed. The
    resumable unit of a scan is one batch of refs (invariant 2 as amended).
    Two embedded-plugin locator defects surfaced and are fixed: a rescanned
    shard whose neighbour is skipped completes the boundary carry from that
    neighbour's tail or head (it used to mint or "relocate" the straddler
    with one segment — reachable at any acquisition boundary), and an
    embedded ref's `size` is the article's row count, not the shard's byte
    size, so a straddler gaining a segment is a change (re-hashed,
    re-processed), never a relocation. `fetch()` for a changed document
    runs outside the write transaction.
33. **§5.1 — the origin's listing is a recorded observation, and every
    denominator reads it.** A fetch pass streams: `fetch_files` gains a
    row per file as the pass processes it, so at every instant mid-pass
    done==total ("1414/1414" on the Library card while the origin held
    1,447 files) and the Stats page's files/bytes-left and fetch ETA read
    zero through every long acquisition. The engine has always walked the
    complete listing at pass start (the delta gate requires it); it now
    records the totals (files, bytes, profile, timestamp) as the
    `acquire_listing` control entry — a cached observation, not desired
    state — and the Library and Stats pages use it as the denominator and
    the ETA's remaining-work figure. RULE FOR NEW PLUGINS (in
    `AcquisitionSpec`'s docstring, where authors look): a transport's
    `list_files()` enumerates the origin's complete listing for the
    profile — paginate to the end, never truncate; a source that cannot be
    listed needs `static_files` or `snapshot_files` instead. Companion
    honesty fixes: the fetched counter shows a `fetching…` badge while the
    acquisition claim is live, and the Stats embed-ETA cell says WHY when
    no number exists ("with indexing (imported)" for genesis collections,
    "on demand (lazy)", "off") instead of a bare dash.
34. **§5.1 — mirrored sources: structure-probed, benchmarked, persisted
    ranking, mid-pass failover (`app/acquire/mirrors.py`).** A spec with
    several endpoints names `probe_file`: a small file at its OFFICIAL
    relative path (gutenberg: `1/pg1.rdf`). Fetching it IS the admission
    rule — a mirror with a different layout cannot serve it and is ranked
    out — and the timed fetch is the benchmark. The outcome persists in
    the control table, so a restart starts with the mirror that won last
    time; the benchmark re-runs when the record ages past 6 h, so a
    better mirror takes over unattended; a mirror failing a real fetch
    mid-pass is marked down 15 min and the file retried immediately on
    the next-ranked one. The gutenberg endpoints were re-probed live:
    rsync.mirrorservice.org (the old FIRST endpoint) no longer carries
    cache/epub and is removed; the pglaf aliases are one host with the
    master; ftp.ibiblio.org is a real second mirror. Found underneath:
    rsync `list_files` was NON-RECURSIVE — for a one-dir-per-doc corpus
    the listing held 79k directories and zero files, so gutenberg's
    in-app acquisition had never enumerated a single book (its corpus
    grew via an external rsync task). `-r` added, listing timeout sized
    for ~800k entries. The create form also gains "(hand-populated)" as
    an explicit profile choice for every plugin: no profile, no
    acquisition, discovery indexes what the operator lands.
35. **§5.1 — extra formats (`spec.extras`), and the exchange policy they
    make explicit: peers exchange ONLY the data that fulfils Louise's
    purpose.** Some origins publish the same document in several formats;
    Louise ingests exactly one (gutenberg: the plain `.txt`). A spec may
    declare the others as named extras (`extras={"epub": Filter(...)}`);
    a collection opts in per format (TOML `extras = ["epub"]`, chosen at
    create or grow). Opted-in extras are fetched from the corpus source
    into the same tree as a LOCAL convenience — and are NEVER p2p
    material: not in published corpus torrents (`torrent_publish`
    excludes their patterns unconditionally, since files from a past
    opt-in outlive the opt-out), not in snapshot manifests, not
    possession material (they never enter `documents`). Classification
    is exclusive — a file the base filters claim is Louise data, never
    an extra — carried as `fetch_files.extra`, split in the
    `acquire_listing` totals (`extra_files_total`/`extra_bytes_total`),
    and shown on the Library card as a separate `(+ e/E)` pair so extras
    never inflate the Louise counters. Details live in Help; the GUI
    stays clean.
36. **§5/§9 — the license gate guards USE, not just acquisition.** The
    original gate (invariant 9) fires "before the first byte moves" —
    which only covers fetching from the origin. Found live during the
    first two-instance exchange (2026-08-24): a yellow-class shard
    copied from a peer into a hand-populated collection was discovered,
    indexed and genesis-imported with no acknowledgement recorded — the
    operator had to notice and acknowledge after the fact. Amended rule:
    a yellow-or-stricter collection with no valid `license_ack` is
    EXCLUDED FROM USE — discovery does not enumerate its corpus,
    genesis/bundles do not import into it, and its data is never
    exchanged onward — regardless of how the bytes arrived
    (acquisition, hand-population, peer copy). The Library card says
    "license acknowledgement required" with the acknowledge button
    instead of silently idling; the ack the create dialog and the CLI
    record stays exactly as today (timestamp + sha256 of byte-exact
    terms text). Enforcement design: the worker's per-round collection
    loop and the pipeline threads skip a gated collection the way
    `acquire_enabled=False` skips acquisition; a boot-time warning names
    it. Not yet built — STATUS.md 5d.
37. **§7 — peer classes and workload chunking (operator design,
    2026-08-24; proposal recorded, not built).** Two kinds of peer:
    *light* (does only unshareable local work — discovery, FTS; still
    seeds corpus bytes and serves processed data) and *full* (also takes
    shared-workload leases). A lease is a fixed, generous TTL for now
    (24 h, the existing `LEASE_TTL_SECONDS`); expired leases are reissued;
    a benchmark-based eligibility + per-peer TTL is a to-do (STATUS 5e).
    Chunking: a lease's unit is a CONTIGUOUS, FETCHABLE corpus range
    sized to ~7,000 chunks (≈1 h on the reference encoders): gutenberg
    50 books by native_id range (~148 chunks/book measured), wikipedia
    2,000 articles by id range within one dump stream (~3.5
    chunks/article measured); pubmed and the pre-embedded corpus carry
    no shared embedding work (archive / pre-embedded); genome awaits its
    encoder review. Lease size therefore becomes per-plugin
    (`target_chunks / chunks_per_doc`), replacing the flat 64-uid default.
    The first lease a joining full peer receives is chosen from the range
    it can fetch first, and acquisition prioritizes that range's files
    (book directories; the torrent pieces covering a dump stream), so the
    peer is embedding its share while the rest of the corpus is still
    arriving. Results travel as signed bundles into peers' inboxes.
38. **§8 — pre-embedded archives: fetched like a corpus, applied as
    indexing goes, one knob (operator policy, 2026-08-24).** The remote
    genesis path applied its shards ONCE, post-settle: a shard downloaded
    only after the whole collection had passed rdf/fts. Observed live on
    pubmed: 8.25M of 40M documents indexed after weeks, 0 of 37 shards
    fetched, 44 documents embedded — the archive that "already paid the
    GPU-hours" contributed nothing for the months the gate would hold.
    Amended rules:
    - **Fetch both, corpus first.** A remote archive is a SECOND
      acquisition stream of the collection: its shards download in the
      background (resumable at byte granularity, same `.part` discipline
      as corpus files, same license gate as the collection — amendment
      36), drawing from the central budget under a new priority class
      `archive` that sits BELOW `backfill` (the corpus, without which the
      vectors are useless) and above `seeding`. Shards on disk are
      bounded (`GENESIS_CACHE_BYTES`) with a disk preflight, so an
      archive never fills the disk ahead of the corpus.
    - **Apply as documents become eligible; revisit; finalise.** A fetched
      shard applies immediately for the documents already indexed, is
      REVISITED as more of its documents clear fts (a pass that imports
      nothing backs off geometrically per shard, so cost follows work,
      not the shard list), and becomes FINAL — files deleted — once no
      in-scope document in its id range is still waiting for fts. A pass
      runs on the collection's own thread beside its rdf/fts and is
      BUDGETED (`GENESIS_PASS_BUDGET_S`): it commits what it has and
      resumes after an equal share for indexing — a million-document
      first pass must not stall the indexer the archive itself depends
      on; a stop resumes at once.
    - **The same-function proof is a quorum.** An archive is a snapshot
      of a living corpus; a sampled document whose text changed since
      reproduces badly under the SAME function (live: shard 1 failed
      worst-of-8 at 0.874 beside a proven shard 0 and samples at
      ≥ 0.993). Sixteen samples; median ≥ τ and 75 % ≥ τ prove the
      function; the samples below τ are stale documents — evicted, never
      imported, left to the encoder. A rejected shard stops the archive
      only while nothing has proven it; under a proven archive the
      rejected shard alone is out, its range released to the encoder. A
      document in a final shard's range that the archive did not cover
      (no abstract, straddler, shape mismatch) is thereby released to the
      local encoder; until then it waits, never a wrong import.
    - **The archive declares its coverage.** MedCPT's 37 shards are exact
      PMID ranges (shard *i* = *i*·10⁶ … *i*·10⁶+999,999, verified from
      the published id lists), so `GenesisArchive.shard_of_id` names a
      document's shard from its native id alone — no download needed to
      know what the archive can cover.
    - **One knob: Embed mode, coverage-aware.** The archive imports
      whenever the worker runs, in every mode — it is free. The knob
      governs the LOCAL encoder only: *off* — nothing; *on-demand* — the
      query queue (any document, covered or not: a question is answered
      now); *all* — the documents the archive CANNOT cover (outside every
      non-final shard's range), selected by a rowid cursor over the scope
      index so a round never re-walks the covered prefix. The former
      "non-English first" ORDER BY on the backfill is dropped: it sorted
      the whole candidate set every round (a TEMP B-TREE, the same trap
      as the rdf ordering) and language priority belongs to the queue.
    - **Card.** Main box: `embedded (dense)` is total coverage (archive +
      local); the sub-box carries `fetched (vectors)` (shards on disk /
      declared, bytes) and `pre-embedded (dense)` (documents the archive
      covered / in scope), and no knob. Local archives (the corpus IS the
      archive) keep their candidate-driven read; only the remote path
      changes.
    Schema: `genesis_imports` gains `id_min`, `id_max`, `bytes`,
    `pending`, `next_pass_at` (additive, migrated on connect); states
    become fetching → fetched → applying → applied ⇄ applying → final |
    rejected | skipped. Built 2026-08-24/25 — STATUS.md 5g.
39. **§3/§8 — PubMed is one chunk per article (2026-08-26; operator
    approved 2026-08-25).** MedCPT holds one vector per article, made
    from title + abstract in one 512-token window; the genesis shape gate
    binds archive vectors to local chunks 1:1; and PubMed's declared
    `abstract` profile had always fallen through to the book chunker,
    which cut title and abstract into separate chunks — so the archive
    could cover only title-only records (42–70 % of shard documents were
    shape mismatches, live). Amended rule: the abstract chunker is wired
    (`title\n\nabstract`, one chunk, no chapter label) under its OWN
    version number, `ABSTRACT_CHUNK_VERSION`, and enters the space
    payload — it is not grandfathered, because no space was ever pinned
    under it — so PubMed's space id moves and nothing else's does. The
    boot check refuses the old pin; `space repin` is the explicit
    operator step (drop the pin, reset embed AND lexical marks — the old
    chunk ids in the index would otherwise name different text — clear
    the verification ledger, keep fetched archive shards, drop the vector
    table). Truncation stays where it was: the client's char budget
    (`ctx·4`) plus the server's own window, with a 20 % shrink on an
    over-length answer. Chosen on measurement: cosine to the archive
    median 0.988 / min 0.960 across 24 abstract-bearing articles.
40. **§5 — a TORRENT COLLECTION is the unit a peer is handed
    (2026-08-26; operator design).** Per-slice torrents (one per
    language of a pre-embedded corpus) made a corpus fetchable a piece
    at a time, but a peer had to be given every magnet and keep its TOML
    in step with the publisher. Amended rule: a publisher that splits a
    corpus also publishes an INDEX torrent — a tiny directory holding
    `collection.json` (members: slice, the slice's include patterns,
    magnet, info hash, size) and a copy of every member `.torrent` —
    and hands out ITS magnet alone. A peer's collection is created with
    that one magnet; which members it pulls is its PROFILE, resolved
    against the index (a combined profile in its own order, "all"
    smallest first, so the peer is busy on a small slice while the big
    ones arrive — amendment 37's principle); growing the profile adds
    members without touching the TOML, and the grow dialog offers only
    what the publisher offers. The index is resolved once per source and
    cached in the collection's control table; its content lives beside
    the manifest, never in the corpus. The Network page groups a
    node's seeded torrents under their collection and offers the
    collection magnet to copy. BitTorrent has no collection object: this
    is a static index, so a re-publish is a new magnet (like a dated
    origin snapshot); BEP 46 mutable pointers keep the magnet stable and
    are the follow-up once peers live beyond one LAN.
41. **§3/§8 — chunker LAYERS: the MedCPT case (2026-08-26; the operator
    amended invariant 1).** A pre-embedded archive fixes the chunking of
    the documents it covers (MedCPT: one vector per article; Upstash: its
    own paragraphs, `verbatim`), and that chunking is not always the one
    we would choose for the documents the archive does NOT cover — new
    and changed articles, for years to come. Under "one collection = one
    chunker" the only options were to imitate the archive forever or to
    re-embed everything (PubMed: ~10,000 GPU-hours). Amended rule: a
    collection declares an ordered list of LAYERS, each with a chunker;
    every document belongs to exactly one layer by a rule computable from
    the record and the manifest alone ("in the archive's id set → layer
    0, else layer 1"); one encoder per collection; `space_id` hashes the
    encoder and the ordered layer chunkers, fixed at creation. Vectors of
    different layers are comparable — same function — and searched as one
    list; upsert stays per document, so idempotence and bundle merging
    are untouched; the archive's shape gate applies per document within
    its layer; the pack's `chunks` check verifies per layer; provenance
    names the layer. A collection with one layer is the common case and
    its layer IS its chunker, so nothing pinned today changes. A v2 that
    unifies the chunkers is a DERIVED collection (pack-format.md §17): it
    inherits every document already embedded with the surviving chunker
    (identical chunk ids) and its work list is the other layer — the
    community pays only for the portion that changes, when it decides
    to. Known cost: short chunks outscore whole abstracts on specific
    queries; a per-document best-chunk step before fusion is to be
    measured before building. Where it pays: not pubmed (abstracts are
    short; one layer, `abstract@1`) but wikivec (`verbatim` for the
    archive's articles, `chunk_cdc` for new and changed ones). Designed,
    not built.
42. **§8 — the same-function proof is per ARCHIVE and persists; a small
    failing sample is never a verdict (2026-08-26).** Amendment 38's
    quorum was re-run from scratch on EVERY revisit pass, on whatever
    sample that pass had. Live: pubmed shard 1 proved and imported ~950k
    vectors over thirty passes, each skipping the same one stale
    document, until a pass whose only eligible document was that one
    re-ran the proof on a sample of one — 0/1, REJECTED, its remaining
    documents released to the encoder. Amended rules: (1) a passed proof
    is recorded on the row (`proven_at`) and proves the ARCHIVE — one
    producer, one function; rows from before the column count by their
    imports, `applying` never does; (2) on a proven archive a pass only
    SCREENS for staleness: every sample below τ is a stale document,
    evicted and left to the encoder, however many; (3) a verdict against
    an archive needs at least `RE_ENCODE_REJECT_MIN` (8) samples failing
    the quorum — fewer, on an unproven archive, are INCONCLUSIVE: nothing
    written, no verdict, the shard retried under its backoff when more
    documents are eligible (an empty sample likewise defers instead of
    refusing); (4) `genesis retry <collection> [--shard]` puts a rejected
    shard back in line. Built 2026-08-26 — STATUS 5g.
43. **§5/§8 — the pack, chunker layers and the local vector index are
    BUILT, first cut (2026-08-26; operator: "do the heavy coding").**
    What exists (pack-format.md §18 maps it): record shards as JSON Lines
    in independent zstd frames with a trailing skippable-frame index
    (`app/pack/records.py`, pyarrow's bundled zstd — no new dependency);
    the slicing scheme per plugin (`slicing.py`: PubMed year bins,
    Wikipedia language + page-id bins, Gutenberg language); a resumable
    `pack build` over a collection's MANIFEST (never a fresh crawl —
    dedup and tombstones come from discovery) with a signed manifest,
    the terms text copied byte-exact, and a validation plan
    (`build.py`, `manifest.py`); `pack verify` with the deterministic
    checks roots / records / chunks / license / signature (`verify.py`);
    the `packed` plugin, which serves a pack as a corpus and keeps three
    identities — the record's uid, the origin hash as `raw_hash`, the
    origin plugin's chunker — so a collection over a pack yields the same
    uids, chunks and possession hashes as one over the origin (tested
    for gutenberg, pubmed, wikipedia); `tools/pack_convert.py` for a
    corpus without a collection; `torrent publish --per-slice` (member =
    slice, rooted at the pack, plus a manifest member, under the torrent
    collection); chunker LAYERS per amendment 41 (`app/layers.py`,
    `[[layers]]` in the TOML, `chunks_for_document` in every stage, the
    ordered layers in the space id only when more than one); and the
    LOCAL VECTOR INDEX (`app/vector_index.py`: Lance IVF-PQ by default —
    IVF-FLAT by knob — built by the worker off the GPU loop once a table
    reaches `VECTOR_INDEX_MIN_ROWS`, new rows folded in by `optimize`
    when `VECTOR_INDEX_REFRESH_ROWS` accumulate, the search probing with
    `nprobes` + `refine_factor` whenever an index exists, `index vector
    build|status`). Also: Wikipedia's fetch caches the last decompressed
    multistream chunk (one slot) — every stage walks pages in stream
    order, and each page paid its stream's decompression before. NOT
    built: snapshots/deltas, the call for contribution over packs,
    gossip, the freshness check, centres/labels/bucket-ordered vectors
    as pack members (the local index is Lance's own for now), attestations
    and trust levels, `records-vs-origin`, and acquiring a pack from a
    peer through a `packed` collection (the plugin has no acquisition
    spec yet: a pack arrives by torrent into a folder and the collection
    is hand-populated over it). Storage: the reference NAS holds packs on
    `tank/louise-packs/<name>` (recordsize 1M, lz4), mounted at `/packs`
    (`PACK_MOUNTS_DIR`), offered by the create dialog as `packed` corpora.
44. **§5 — a pack is a CLOSED snapshot; the origin hands over to it;
    updates are derived, not authored (operator procedure, 2026-08-26).**
    The first packs were built from origins still indexing. Amended
    procedure: (1) the origin collection completes fetching, parsing and
    indexing up to a cutoff date; (2) `pack build --until <date>` builds
    the closed pack — records that existed at the origin on that date
    (gutenberg by PG release date `dcterms:issued`, pubmed by
    `DateRevised`/`DateCompleted`, wikipedia by the dump's date; a record
    with no date under a rule is excluded and counted, never kept), the
    rule recorded in the manifest (`origin.cutoff`, `closed: true`), a
    resume under another cutoff refused; (3) the origin is turned off
    and `pack adopt <origin> <packed>` moves its vector table and embed
    marks (with `--fts` the text index) into the packed twin — no
    re-embedding, because the twin yields the same uids and chunk ids
    in the same pinned space (checked: config id and encoder hash must
    agree; the twin's table must be empty; nothing may write meanwhile).
    The first closed pack is Gutenberg to 2026-06-30; PubMed and
    Wikipedia follow. UPDATES (pack-format.md §6.1): an update is a pure
    function of (previous manifest, origin window, cutoff) — new shards
    appended per slice in id order after the last listed, changed
    records re-issue their shard, deletions are tombstones in the delta
    — so two honest updaters produce byte-identical members and there is
    nothing to reconcile; any peer may publish one (unverified on
    arrival, chained by `previous`), and the maintainer-key policy that
    promotes updates is a later, format-neutral addition
    (`update.coordinator`). The updater itself is not built. Live:
    `gutenberg-pack` paused (its TOML set aside) until the gutenberg
    origin completes; the premature pubmed/wikipedia conversions and
    their finishers stopped.
45. **§8 — an archive pass costs the new documents, never the shard;
    index maintenance never on a pipeline thread (2026-08-27; the
    engineering rule "a per-round cost scales with the work available,
    never with the size of a list").** Measured on the reference node:
    a MedCPT revisit pass re-walked its million rows per pass (half of
    pubmed's pipeline thread), a wikivec candidate read re-read its
    whole 437 MB shard per 1,000-document fts batch, and one vector-index
    refresh idled wikivec's pipeline for 41 minutes. Amended rules: a
    revisit pass over a declared-coverage shard names its eligible
    documents with ONE indexed range query and reads only their rows; a
    local archive read opens only the row groups the candidates' locators
    name; the vector index is maintained by its own low-priority thread,
    one collection at a time, which marks the collection busy — its
    embed step and archive pass defer (rdf/fts and queries continue) —
    and a refresh is due at `VECTOR_INDEX_REFRESH_ROWS` AND
    `VECTOR_INDEX_REFRESH_FRACTION` of the table. Each rule is pinned by
    a regression test that counts the work (lookups, row groups) rather
    than timing it.
46. **§8 / invariant 9 — ONE license gate, packs included (operator rule,
    2026-08-27: "the license that applies to the corpus and its
    pre-embeddings applies to the pack").** The acquisition gate covered
    only plugins with an AcquisitionSpec; a `packed` collection had no
    gate at all, and amendment 36's ingestion gate was designed, not
    built. Amended: `app/licensing.py` is THE resolution — the class from
    the spec or the pack's manifest, the terms text from the spec's file
    or the pack's byte-exact `TERMS.txt` (the origin's, so the SAME
    sha256 and one ack is valid for both), `state_for` = not_required |
    acknowledged | missing. Enforced at every way data arrives: the
    worker withholds the corpus SCAN and the ARCHIVE pass of a
    yellow/red collection whose ack is missing or for other terms (logged
    once an hour: "a hand-populated or packed folder is not a bypass");
    the archive fetcher requires it; `pack build` refuses to pack an
    unacknowledged origin; the create dialog shows the PACK's terms and
    refuses a packed collection over a yellow/red pack without explicit
    acknowledgement (the ack it records is computed from the pack's
    terms); the Library card and `/terms` speak for packs. Green needs
    nothing. `discovery.sync_collection` itself stays ungated (tools and
    tests call it); the gate is the worker's and the UI's.
