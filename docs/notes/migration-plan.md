# Migration plan — from the current code to the documented architecture

*Working document for the `louise` branch. The specification is
`docs/README.md` and the nine documents it lists; this file says in what
order the code gets there, what each step reuses, and how each step is
known to be done. Update it as steps land; delete it when the last one
does.*

*Rewritten 2026-09-08 against the specification as it stands after the
read-through (`spec-review-2026-09-07.md`) and the seven decisions of
commit `0df266b`. The previous version was written against seven
documents and a Codex that has since gained twelve fields, lost one and
changed how it is hashed.*

## 0. Ground rules for every step

- **One step, one pull request, tests included.** A step is done when
  its tests pass *and every pre-existing test still passes*. The tests
  under `tests/` are the safety net for the whole migration; nothing is
  deleted from it, tests are moved and renamed as the code they cover
  moves.
- **Old names stay as aliases** until §7.6's migration is complete: a
  new command lands beside the old one, the old one prints a deprecation
  line naming the new one, and both call the same function.
- **Nothing is re-downloaded and nothing is re-embedded.** The corpora on
  disk (Gutenberg, PubMed baseline and update files, Wikipedia dumps),
  the plugins that parse them, and the vectors already computed or
  imported (MedCPT, the BGE-M3 Wikipedia archive, this node's own) are
  the starting point. The migration gives them a Codex and admits them
  through the same gates a stranger's copy would pass.
- **The invariants in CLAUDE.md are unchanged**: idempotent writes,
  deterministic chunking, the GPU boundary (HTTP only), secrets by path,
  licence gates before use, `origin_hash` on every record.
- **09 is the arbiter of bytes.** Where a step touches something that is
  hashed, signed or moved, 09's field table is the specification and the
  narrative documents are the reason. A disagreement between them is a
  bug in the documents: stop and ask, do not choose.
- New dependencies, schema changes and wire-format changes: ask first.
  Step 10 has one.

## 1. The branch

```
git checkout -b louise            # specs stay on main; code lands here
```

Specs are edited on `main` and merged forward into `louise`; code never
lands on `main`. On a machine with the real environment:

```
docker build -t louise-rag:dev .
docker run --rm -v "${PWD}:/srv" -w /srv -e PYTHONPATH=/srv louise-rag:dev \
  timeout -s KILL 900 python -m pytest tests/ -q
```

The baseline at the time of writing is **1362 passed, 1 failed**. The
failure is `tests/test_plugins_pubmed_pack.py::
test_pack_is_rebuilt_when_the_container_or_the_format_changes`, which
asserts on `st_mtime_ns` as a proxy for "was rebuilt"; the rebuild
demonstrably runs. It is pre-existing, it is a test defect and not a
code defect, and step 2 replaces the code it covers — fix it there
rather than before.

## 2. What is already on the branch, and what is wrong with it

`app/hold/` and `app/share/` are the first increment, written *before*
the read-through. Two of the three modules are sound; the third is not.

| module | state |
|---|---|
| `app/hold/snapshot.py` (125 lines) | sound in shape, short of the current snapshot: no `chunks_root`, no `relations`, no `files`, `ids: [first, last]` where 09 §9.4 now has `first_id`/`last_id`, `tombstoned` as bare uids where it is now `{uid, reason}` |
| `app/hold/collection.py` (149 lines) | sound; `derive` needs the new flags (step 1) |
| `app/hold/codex.py` (371 lines) | **wrong, and everything hashes through it** — see step 0 |
| `app/share/rendezvous.py` (131 lines) | sound; its tests are the partitioning tests step 9 builds on |

---

## 3. Steps, in order

Each step names the documents it implements, the existing code it wraps,
the tests that prove it, and what the operator has to do by hand.

### Step 0 — Bring the Codex to 09 §9.3
*09 §9.3, 02 §2.1, 02 §2.4, 02 §2.7. Rewrites `app/hold/codex.py`.
Nothing else can be right until this is.*

`app/hold/codex.py` implements a Codex that no longer exists. It is
missing twelve fields, has one field the specification struck, and
computes the identity over the wrong bytes. It is the one place in the
migration where *rewrite* beats *wrap*, because every hash in the
system is downstream of it.

What changes:

- **Two hashes, not one.** `collection_id = H(canonical JSON of the
  Codex with exactly the key `descriptive` removed)`; `codex_hash = H(
  the file's own bytes)`. Today there is one hash over everything.
- **The `descriptive` block** — `{title, summary}` — exists, is the one
  key outside the identity, and is never used to sort, merge or resolve.
- **`credits`**, derived at creation from the archive ids the recipe
  names, expanded from the platform's registry (02 §2.7, 06 §6.6). It is
  hashed, so it cannot be edited afterwards. A sister's follow the space:
  her mother's when `space_id` matches, the `--space-from` Codex's when
  that supplied the space, `--archives`' when she names an encoder,
  `[]` when she names nothing.
- **`facets`** — the declared filterable structure (`flat` / `tree` +
  separator / `range`), with `authority: {name}` and no version. Values
  live in records, never here.
- **`work`**, **`modality`**, **`erasure`**, **`privacy`**,
  **`validation`**, **`protocol_version`**, **`profile`**.
- **`update` is an object** — `{cadence: "monthly" · "weekly", day,
  grace_days}` — not three flat fields. `day` reads as day-of-month
  (1–28) when monthly and ISO weekday (1–7) when weekly.
- **`epoch` is `"0"`**, not `""`, for a root; a daughter *and a sister*
  keep their mother's; only a corrective sister mints the salted value.
- **`from_snapshot` → `continues_through`**, and it names the *mother's*
  last snapshot.
- **`space_from`** records the Codex a sister copied her space from.
- **`toolchain.encoder_image` is deleted.** The encoder image is named
  in `space.encoder.image` alone; a third `toolchain` key would be
  refused by 09 §9.18 and would move every `collection_id`.
- **Unknown keys are refused** on any hashed or signed object (09 §9.18).

**Tests** (`tests/hold/test_codex.py`, extended): 09 §9.3's worked
example round-trips to its published `collection_id` and `codex_hash`
byte for byte — this is the single most valuable test in the migration
and it is written first; two Codexes differing only in `descriptive`
have the same `collection_id` and different `codex_hash`; a float
anywhere is refused; an unknown key is refused; a sister's `space_id`
equals her mother's when only `update` changed; a corrective sister's
`epoch` is the salted hash and her sister's is that same salted hash,
not `"0"`.

**Operator**: nothing.

### Step 1 — The drag on disk, and the collection template
*02 §2.6, 09 §9.16, 09 §9.3, 07 §7.3, 07 §7.8. Wraps
`app/pack/manifest.py`, `app/collections.py`, `app/plugins/base.py`.*

This is the step that makes "a collection" a thing on disk that a
stranger could receive, and it is where the **generic collection
template** lands.

- `app/hold/collection.py` gains the drag layout — 07 §7.4 fixes the
  module list and there is no `drag.py` in it: the drag IS the
  collection on disk. The member table of 09 §9.16 and the tree of 02
  §2.6 — `codex.json`, `snapshots/`, `records/<slice>/NNNNN.jsonl.zst`,
  `relations/`, `vectors/<space>/`, `buckets/`, `validation.json`,
  `LICENSE-TERMS`, `calibration/`, `seals/`. A file that is neither in
  the snapshot's `files` nor named by a `shards` entry is not a member
  and is not served.
- `hold collection create <plugin> --recipe FILE [--space-from CODEX]`:
  the recipe is a TOML carrying every Codex field the plugin does not
  default. **The template is what the plugin defaults**: origin, parser,
  licence class, privacy, modality, erasure, the chunker family,
  `default_layers`, `default_slicing`, `default_facets`. A new
  collection is therefore a plugin plus a `space` block plus a `work`
  block, and `create` refuses (exit 2) a recipe without those two unless
  `--space-from` supplies the space.
- `hold collection derive` gains `--space FILE`, `--work FILE`,
  `--archives ID …`; `--space-from` copies `space` and `layers` and
  never `cleaning_version`, and refuses a `cleaning_version` mismatch
  naming both values. `create --space-from` refuses the same mismatch
  against the plugin's.
- `hold collection show` names, for a collection that cannot start, the
  reason it cannot (a `space_id`/`dim` mismatch is a refusal for that
  collection and not for the node).
- The pre-acknowledgement member set is exactly `codex.json`,
  `snapshots/*` and `LICENSE-TERMS`.

**Tests**: a recipe with only `space` and `work` builds a Codex for each
of the five plugins; the same recipe twice prints the same id and exits
0; `--space-from` with a differing `cleaning_version` exits 2 naming
both; a yellow collection with no ack refuses fetch, build, embed,
index, share and search, each naming `hold collection ack`; the drag
tree round-trips through `app/hold/collection.py` with no file
unaccounted
for.

**Operator**: nothing.

### Step 2 — `hold records build` and `hold snapshot create`
*02 §2.2, 02 §2.3, 06 §6.6, 09 §9.4, 09 §9.5, 09 §9.14. Wraps
`app/pack/build.py`, `records.py`, `slicing.py`, `cutoff.py`,
`tools/pack_convert.py`.*

- `app/hold/records.py`: `build(collection, snapshot_id)` runs the
  existing pack builder over a corpus directory with the collection's
  plugin and returns shard entries instead of writing a manifest. Every
  record's `origin_hash` is required — the existing check stays.
  **New**: it REFUSES (exit 2) a record whose `native_id` sorts before
  the open shard's `first_id`, naming the record and the shard, so shard
  ranges stay a partition.
  **New**: it writes `relations/<slice>/NNNNN.jsonl.zst` from the
  plugin's optional `relations(doc) -> Iterator[Edge]`, and nothing at
  all when the plugin does not implement it.
- `app/hold/snapshot.py`: `create(collection, shards, previous)` writes
  `snapshots/<id>.json` as canonical bytes with the current shape —
  `first_id`/`last_id`, `bytes`, `chunks`, **`chunks_root`**, `tokens`,
  `relations`, `files`, and `delta.tombstoned` as `{uid, reason}`.
  `chunks_root` is SHA-256 over, in chunk order, each chunk's id as
  UTF-8 followed by the raw 16-byte BLAKE2b-128 of its text; it costs
  the chunking pass the `chunks` count already costs.
- The snapshot id follows the cadence: `YYYY-MM` monthly, `YYYY-Www`
  weekly, naming the period that CLOSED at the cutoff.
- CLI: `hold records build|show|check`, `hold snapshot create|delta|list`;
  aliases `pack build`, `snapshot record|delta|list`.

**Tests**: the golden Gutenberg mini fixture builds to a pinned snapshot
hash; building twice gives identical bytes; a record without
`origin_hash` is refused; an out-of-order `native_id` is refused naming
the record and the shard; `chunks_root` is stable across two builds and
changes when a chunker version does; a weekly collection's ids are ISO
weeks and `verify_chain` accepts consecutive ones; a plugin with
`relations()` writes the member and one without writes nothing. And the
`st_mtime_ns` test of §1 is rewritten to assert on the rebuild's output.

**Operator**: nothing.

### Step 3 — `hold embedder check|benchmark|calibrate`
*03 §3.3, 02 §2.4. Wraps `backend check/rebaseline`,
`tools/profile_embed.py`, `app/bundles/keys.py`.*

- `check(collection)` embeds **the whole calibration set** with this
  peer's encoder and passes when, over `quorum.n` texts drawn from it by
  the seeded rule of 03 §3.2, at least `quorum.min_pass` reach cosine ≥
  `tolerance` and the median of those n does. The whole set is embedded
  because the statement reports over all of it.
- `benchmark(collection)` measures tokens/s on the same chunks. The
  digest's `benchmarks` entry is **keyed by `space_id`**, because that
  is how 04 §4.5 reads it.
- `hold embedder calibrate --encoder-image D --encoder-weights D
  [--against …]` measures the tolerance a space can bear, **before**
  `collection create`, and its output is the recipe's
  `space.tolerance`.
- `--accept` replaces `rebaseline`: it records a passing run as this
  node's baseline for drift detection, not as the collection's
  reference.

**Tests**: with the fixture endpoint mock, `check` refuses a mismatched
encoder and accepts the reference; the pass decision is over the drawn
n and the embedding is over the whole set (assert the call count);
`benchmark` is stable within 10 % across runs on the mock; the statement
verifies with the node's public key; a digest carries one `benchmarks`
entry per space.

**Operator**: nothing.

### Step 4 — `hold vectors build|import|export|check`
*02 §2.4, 03 §3.5, 09 §9.7. Wraps the embed stage, `app/bundles`,
`genesis import`, `tools/vector_compliance.py`, `tools/vector_sweep.py`.*

- `build(collection, shard)`: the existing embed stage over one shard,
  emitting a signed bundle every few hundred chunks. Labels against the
  current centres when the index exists.
- `import(bundle | --archive ID)`: one path for a peer's bundle and a
  public pre-embedded archive, through T0–T3. Admissibility is
  `space_id` plus per-record `content_hash`; `collection` and
  `snapshot_id` on a bundle are provenance and never gates.
  **`--archive` refuses an archive not already in the Codex's `credits`**
  (06 §6.6) — which is why `credits` must be declared at creation.
  `--strict` refuses a producer not on this peer's allow list and never
  admits below T3; with an empty allow list it admits nothing, which is
  why it is off by default.
- `check([--drop])`: `vector_compliance` and `vector_sweep` as one
  command. `vectors-sample` draws **sixteen** chunks per sampled shard,
  always; the plan sets how many shards and never how many chunks.

**Tests**: the existing `tests/bundles`, `tests/verify` and genesis
tests pass under the new entry points; a bundle with foreign vectors is
refused at T0; a bundle whose `collection` differs but whose `space_id`
and `content_hash`es match is ADMITTED; an uncredited archive is
refused; `--strict` on an empty allow list admits nothing; a corrupted
stored vector is found and dropped by `check --drop`.

**Operator**: nothing.

### Step 5 — `hold check`, `validation.json`, attestations
*03 §3.1–3.6, 09 §9.8. Wraps `app/verify`, `pack verify`.*

- `app/hold/check.py`: reads the collection's plan, runs the named
  checks at the named sample sizes, prints the report, and emits one
  attestation per completed check (not yet shared). A check that could
  not run is *inconclusive* and produces no attestation.
- Checks in this step: `roots`, `records`, `chunks` (now against
  `chunks_root`, deriving each document's layer from the Codex's rules
  rather than reading it), `records-vs-origin` (comparing every record
  line of the shard byte for byte, not merely the resulting
  `records_root`), `license`, `signature`, `calibration` (step 3),
  `centres`, `labels`, `vectors-sample` (step 4). `anchor` and
  `origin-seal` come in step 10.
- `hold records check` is `hold check --checks roots,records[,
  records-vs-origin][,origin-seal]` — `records` included, which is the
  only local check that proves what is inside a shard.
- The trust list filters attestations and lifts burns. Its allow side
  imposes nothing on a bundle except through `vectors import --strict`.

**Tests**: `tests/pack/test_*verify*` moved; a plan with an unknown check
name is refused; the attestation for a failing deterministic check names
the shard; a `chunks` failure on a chunker that moves boundaries without
changing the count is CAUGHT (this is the test that justifies
`chunks_root`); an inconclusive check writes no attestation.

**Operator**: nothing.

### Step 6 — Proof-of-concept import — ONE collection, end to end
*Everything above, on real data, at a scale that fits in an afternoon.
**This is the milestone the operator asked for, and the stop before the
full import.***

Pick the smallest real collection this node holds — Gutenberg is the
candidate: a corpus already on disk, green licence, no acknowledgement
to record, and a `native_id` that sorts numerically, so step 2's
ordering refusal will not fire spuriously.

1. `hold embedder calibrate` the encoder actually running; record the
   tolerance it can bear, which becomes the recipe's `space.tolerance`.
2. `hold collection create gutenberg --recipe …` with the platform and
   encoder digests of the images actually running
   (`docker inspect --format '{{index .RepoDigests 0}}' <image>`). If
   the encoder digest is not the one the approved-encoder list names for
   this device class, **stop**: that is the drift the migration exists
   to make impossible.
3. `hold records build` over the corpus directory already on disk. No
   fetch, no re-download.
4. `hold snapshot create`.
5. `hold vectors import` this node's already-computed vectors with
   producer `legacy-local`. Vectors that fail the ladder are not
   migrated; the report says how many and where. (Gutenberg's known
   corrupt fraction is expected to fall out here.)
6. `hold check` the whole plan and read the report.
7. `ask search` one question against it and read the result object.

**Done when** the report is clean, the search answers, and the numbers —
records, shards, chunks, admitted vectors, rejected vectors, wall clock
per stage — are written into `docs/notes/STATUS.md`. **A step whose
commit message has no numbers in it is a guess.**

**Not in this step**: PubMed, Wikipedia, the genome collections, sharing,
and any collection whose documents need figures. Those wait for step 8.

**Operator**: runs it on the NAS. This is the first acceptance on real
data rather than fixtures.

### Step 7 — GATE: what may be in a cleaned corpus besides text
*Blocks step 8 and nothing before it. See
[`figures-in-the-corpus.md`](figures-in-the-corpus.md).*

Gutenberg has books that are not the book without their plates
(architecture, interior design, technology); PubMed has structures,
schemes, gels and plots; a patents collection — on the roadmap — is one
where the drawings are part of the claim.

The specification has no answer today: a record is `sections: [[heading,
text], …]`, `content_hash` covers title and sections only, every chunker
cuts text, and 01's first promise is that only parsed text, vectors and
derived indexes travel. A figure does not go unindexed — it does not
exist.

The note frames five sub-questions (does a figure travel; does it enter
`content_hash`; what is its unit; is it embedded and by what; whose
licence) and four candidate shapes (caption-only text; an optional
member; figure-as-record; a multimodal space). **The operator decides;
this plan does not.** Whatever is decided becomes an amendment to 01,
02 §2.3, 06 §6.3 and 09 §9.5 before step 8 begins.

Until then, step 8 imports only what is whole as text.

### Step 8 — Migrate the collections this node holds
*02 §2.1, 02 §2.7, 07 §7.6. New: `louise dev migrate pack --recipe FILE`
and `louise dev adopt <pack-dir> <collection>`.*

Step 6 for each remaining collection, plus the pre-Codex packs already
on disk:

- `dev migrate pack <name> --recipe FILE [--platform-image D]` gives a
  pre-Codex pack its Codexes and first snapshot. The recipe carries, per
  space in the pack, what the pack cannot supply — the encoder image and
  weights, tolerance, work, validation, descriptive — and the
  calibration set is embedded per space as `create` does; the first
  space named is the root and the others `derived_from` it.
- `dev adopt <pack-dir> <collection>` is the one-time move of a
  pre-Codex pack's vectors and index into the collection's drag.
- The old id is aliased in the local catalogue for one cadence, so URLs
  and TOML references keep resolving.

**Tests**: a fixture collection with an old-style manifest migrates to a
Codex whose `collection_id` is stable across two runs; record counts
match the old manifest; the admitted-vector count is reported; running
the migration twice is a no-op.

**Operator**: per collection, on the NAS, with the digests. Record each
outcome in `docs/notes/STATUS.md`.

### Step 9 — `share`: serve, fetch, contribute, attest
*04. Wraps `app/acquire/transport_torrent.py`, `torrent_publish.py`,
`app/transfer`, `app/discovery.py`, `app/contribute.py`,
`app/share/rendezvous.py`. Makes the coordinator optional.*

- `share <collection>`: publish + seed as one motion; announce under
  `H(collection_id ‖ snapshot_id)` and `H(lineage_id ‖ snapshot_id)`
  (libtorrent's DHT is already linked); digests and events over the node
  API as today. A searcher probes the current id, then each earlier id
  back to twelve cadences.
- `share fetch`: choose members, verify pieces, stage, promote. Before
  an acknowledgement only `codex.json`, `snapshots/*` and
  `LICENSE-TERMS` are fetchable.
- `share contribute`: `rendezvous.mine` over the coverage map's
  uncovered shards → `hold.vectors.build` → `share`. The coordinator's
  `/work` is consulted only if configured, and its answer is advisory.
- `share attest`: `hold.check` → publish the attestations as events.
- The digest carries `codex_hash` per collection, and a `codex.json`
  pulled from a peer whose bytes hash otherwise is dropped.
- Coverage map: per-document bitmap per space, assembled from bundles
  and attestations, exchanged in digests.

**Tests**: `tests/integration` gets a two-peer test on localhost: peer B
fetches from A, contributes a shard, A imports it; then the
**single-survivor test** — B is stopped, A produces the next snapshot,
embeds it, and a fresh peer C finds A by computing the DHT key. A
`codex.json` with a substituted `descriptive` block is dropped on the
pull path. The rendezvous tests already cover the partitioning.

**Operator**: two nodes on the LAN for the first real run.

### Step 10 — Certified collections and the Gazzetta plugin
*02 §2.8, 03 §3.7, 03 §3.8, 06 §6.4, 09 §9.15. New code; one dependency
decision.*

- `hold key anchor`: fetch the well-known URL, compare the fingerprint,
  record rotation and revocation.
- Manifest-only gate at promotion for `publication: certified`; the
  space-halt on a violation found later; the exclusion notice in search.
- `seals/`: capture validation evidence at snapshot time. Each entry now
  carries **`seal`** (the signature envelope, DER as base64) and
  **`content_sha256`** (the digest the signature commits to), so
  archival mode verifies the signature offline without the sealed bytes
  — `file_hash` is BLAKE2b-128 of the file and is what *live* mode
  re-fetches against.
  **Dependency**: validating a CAdES envelope and a PAdES seal against
  the EU trusted lists needs a signature-validation library (candidates:
  `pyhanko` for PAdES, CMS via `asn1crypto`/`cryptography` for CAdES;
  the Commission's DSS is Java). **Decide before writing a line.**
- The Gazzetta Ufficiale plugin: acquisition of the certified `.pdf.p7m`
  issues, the sealed PDF as the origin form with the reason recorded
  beside the plugin, parsing pinned by the platform image.

**Tests**: a foreign shard is refused at promotion; a fixture `.p7m`
with a self-signed chain validates in archival mode against captured
evidence *with the network unplugged*, and fails live against an empty
trusted list; the third attribution row of 03 §3.8 flags the certifier,
not the sharer.

**Operator**: the certifier key and anchor for the first certified
collection — this node's own key and a well-known URL on a domain you
control, marked `is_origin: false`.

### Step 11 — `ask serve`, facets, relations
*05. Wraps `app/query_service.py`, `app/retrieval.py`, `app/refs`.*

- MCP server exposing `louise_search`, `louise_lookup`, `louise_traverse`.
  Results carry snapshot, trust level, certification.
- `louise_search(query, collections?, space?, filters?, limit?, as_of?,
  freshness?, translate?)`. `freshness` and `translate` are per-request
  booleans defaulting to false and are **not** node settings.
  `louise_lookup(id)` takes no `as_of`.
- `ask facets <collection> [<field>] [--under VALUE]`: the fields a
  collection can be filtered on, or one field's values with this peer's
  counts.
- `question` is the literal `"relevance"` on every leg; there is one
  list. The rule that legs answering different questions are never fused
  is implemented and costs nothing until the search-axes amendment
  declares a second.
- The `relations` member is followed by the traversal tool, which
  reports unresolved cross-collection ids rather than hiding them.
- `--as-of DATE` is `YYYY-MM-DD`, read at 00:00:00 in the collection's
  `origin.clock`; a shorter form exits 2.

**Tests**: tool schemas validate; a search result carries the fields of
05 §5.3; a `filters` clause restricts the legs and never ranks; a
`ReferenceSource` lookup enters the context as a pinned block and never
RRF (this is CLAUDE.md invariant 4 and it gets a test here); an
unresolved reference names the shard it needs.

### Step 12 — Retire the old names
*07 §7.6.*

Remove the aliases, `app/cli.py`'s old tree and the `tools/` scripts now
under `louise dev`. Update `README.md`'s quickstart to the new commands.
Delete this file.

## 4. Dependencies between steps

```
0 codex ── 1 drag/template ──┬── 2 records/snapshot ──┬── 5 check ──┬── 6 POC ── 7 GATE ── 8 migrate ── 9 share ── 12 retire
                             │                        │             │
                             └── 3 embedder ── 4 vectors ───────────┘
                                                      │
                                                      └── 10 certified ── 11 ask
```

- **0 blocks everything.** Every hash is downstream of the Codex.
- 3 is independent of 2; 4 needs 3; 5 needs 2 and 4.
- **6 is the operator's stop**: real data, one collection, numbers in
  STATUS.md.
- **7 is a decision, not code**, and it blocks 8 alone.
- 9 needs 8 to have something real to share.
- 10 and 11 need 5 and can proceed in parallel with 8 and 9.

## 5. Kick-off prompt for Claude Code

> Read CLAUDE.md, then docs/README.md and the nine documents it lists,
> in order — 09 last and in full, since it is the reference the others
> point into. Then read docs/notes/migration-plan.md and
> docs/notes/STATUS.md. We are on the `louise` branch. Implement
> **step N** of the migration plan exactly as scoped there: wrap the
> existing code it names rather than rewriting it (step 0 is the one
> exception and says so), add the new command beside the old one with a
> deprecation alias, and ship the tests the step lists. Every
> pre-existing test must stay green. If the step conflicts with a
> document, stop and ask; if it needs a dependency, ask before adding
> it. When the step is done, update docs/notes/STATUS.md with what is
> built and what it cost — with numbers — and mark the step done in
> this plan.
