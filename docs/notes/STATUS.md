# Project status

**Last updated: 2026-09-12.** What exists, what is designed but unbuilt,
and what is deliberately not being built. Read this before planning work.

**The specification moved.** `docs/README.md` and the nine numbered
documents it lists are now the design; `architecture-v2.md` and its §17
amendments are the history that produced them and are kept for that.
[`migration-plan.md`](migration-plan.md) says in what order the code
reaches the documents, and the `louise` branch is where it does.

Node-specific facts — addresses, paths, ports, the live collection inventory —
are deliberately **not** here: they live in `deploy/reference/DEPLOYMENT.md`,
which is gitignored, because this file is public to anyone who clones.
(Working from a machine without that file: a current copy is kept on the
node itself, beside the deployment, readable over the same SSH access used
to deploy.)

## Bootstrapping a new development machine

Everything except two items travels through this repository.

1. `git clone` — code, tests, docs, CLAUDE.md. Read this file first.
2. Fetch the node reference from the node into the local clone (the app
   root is the directory the deploy procedure tars into; the file sits one
   level above `src/`):
   `scp <node>:<app-root>/DEPLOYMENT.md deploy/reference/DEPLOYMENT.md`
   After editing it locally, copy it back the same way — the node-side copy
   is what the *next* machine bootstraps from.
3. **The SSH key does not travel** — not through git, chat, or any log
   (invariant 8). Move it between machines yourself, or authorize a fresh
   key on the node.
4. Reaching the node from outside its LAN: substitute your remote
   host/tunnel for the LAN address in every DEPLOYMENT.md command; nothing
   else changes.
5. No local Python is assumed anywhere: `docker build -t louise-rag:dev .`
   recreates the test image, and tests run as in Conventions below.

---

## Where the roadmap stands

| phase | state |
|---|---|
| **Phase 0** — generalize, no network | **done.** CorpusSource + collections + `space_id`, five plugins, ReferenceSource/EntityResolver/`doc_keys`, query federation, acquisition layer with profiles, transfer manager. |
| **Phase 1** — bundles + community | **built, never exercised.** Signed bundle export/import, verification ladder T0–T4, coordinator with leases and demand-sets, `contribute` mode, genesis import. Integration-tested two-node. The *deliverable* — publishing a revived PubMed corpus — has never been done, and §12 argues that artifact is the entire go-to-market. |
| **Phase 2** — swarm | **transport done, discovery designed.** BitTorrent v2 via libtorrent works and is tested against a real two-session swarm; peer/collection discovery is specified in [`p2p-discovery.md`](p2p-discovery.md) and not built. |
| **Phase 3** — decentralize | not started, correctly. §12: "don't build the DHT until someone begs." |

### v1.0 roots — the development collections are not the product (2026-09-12)

Operator decision: the collections this development phase produces become
**version 1.0 of the production phase**, built as ROOTS on a corpus or a
pre-embedded corpus — not daughters, sisters or corrective sisters of the
non-compliant development ones. The standing rule for getting there is
"take the fastest route, keeping the final result compliant by the books;
the final result compliancy is a non negotiable condition."

Auditing the development collections against 09 §9.5 and §9.3 found, and
the code now fixes:

| where | gap | fixed in |
|---|---|---|
| record line | `layer` and `chunks` published, in neither §9.5's table nor 03 §3.2 | `app/pack/records.py` |
| pubmed | `creators` always `[]` (origin has them on 99.0%) | parser 5 |
| pubmed | `lang` hardcoded `"en"` — **9.6% of the corpus is not English** | parser 5 |
| pubmed | `date` always `null` (origin has it on 100%) | parser 5 |
| gutenberg | MARC subfield delimiters in **5.86%** of titles; §9.5 says "cleaned" | parser 6 |
| **every collection** | `derivation` — a required §9.3 key the module never had | `app/hold/codex.py` |

That last one is the big one: every `collection_id` this platform ever
computed was taken over 34 of the spec's 35 keys. Adding it is a **flag
day** — `_only` requires every field, so on deploy every existing
`codex.json` becomes unloadable and each collection is refused until
re-minted. It therefore waits for the gutenberg fetch, which the refusal
would stop.

[`v1-rebuild.md`](v1-rebuild.md) has the order, the two routes for
pubmed's 35.3M vectors and the measurement that chooses between them
(20.5 chunks/s on the CPU encoder = 23 days to re-embed, versus a bundle
re-import that costs no download and re-derives the corrected `lang`),
and the questions still open for the operator.

### The migration to the documented architecture (branch `louise`)

| step | state |
|---|---|
| **0** — the Codex at 09 §9.3 | **done, 2026-09-08.** `app/hold/codex.py` rewritten: two hashes (`collection_id` over the object with `descriptive` removed, `codex_hash` over the file), the twelve fields it lacked, `toolchain.encoder_image` struck, `epoch` `"0"`, `continues_through`, `space_from`, `update` as an object with a cadence, and §9.18's unknown-key refusal. 45 tests in `tests/hold/test_codex.py`, 18 in `test_collection.py`; the first of them writes 09 §9.3's worked example out by hand and asserts the module produces exactly it. **1396 passed, 1 failed** (the pre-existing `st_mtime_ns` proxy in `test_plugins_pubmed_pack.py`, which step 2 replaces); 81 of the branch's own tests run in 3.7 s. |
| **1** — the drag on disk, the collection template | **done, 2026-09-08.** `app/hold/drag.py`: 09 §9.16's member names, 02 §2.6's tree, the pre-ack gate, and BEP 52's pieces root implemented rather than delegated (`roots` is a check; a peer must compute a root without a torrent session). `app/hold/template.py`: all six plugins declare what 06 §6.1 requires, and `hold collection create <plugin> --recipe FILE` builds a Codex from a TOML recipe end to end. Two spec defects found by implementing and fixed on main: `index-<slice>` could collide with `index-<space>` (94cf2b5), and 07 §7.4's module listing did not admit a module backing no command (d73dcb4). ONE OPEN QUESTION for the operator: an archive's signing key — see [`archive-signing-keys.md`](archive-signing-keys.md). 22 + 27 tests; 119 in `tests/hold`. Whole suite: **1445 passed, 1 failed** (the same pre-existing st_mtime_ns proxy) in 211 s. |
| **2** — records build, snapshot create | **done, 2026-09-08.** A corpus on disk becomes a drag end to end: `app/hold/records.py` (records_root, chunks_root, the out-of-order and origin_hash refusals, the build driver), `app/hold/snapshot.py` at 09 §9.4, the roots computed in `app/pack/build.py` where the shards are cut, and the CLI verbs. The drag holds exactly 02 §2.6's five files because the build runs in staging and only record shards are promoted (04 §4.3's pattern). Caught two errors in earlier steps (codex.json outside the drag; idempotence by name not identity) and one contradiction between documents (09 §9.3 vs 07 on what a collection's directory is called). Still owed: the relations member, which waits for a plugin that implements `relations()`. 201 tests on the branch. |
| **3** — embedder check/benchmark/calibrate | **partly done, 2026-09-08/09.** The calibration set (`app/hold/calibration.py`, `calibration/text.jsonl`), the envelope signer (`app/hold/envelope.py`, 09 §9.2's six domains), the gate (`app/hold/embedder.py`), 09 §9.1's seeded `sample()`, and the calibration MEMBER writer (`app/hold/calibration.py` + `collection.write_calibration`, with `canonical_safetensors` because six identical `save_file` calls produce six different files). **The set was rebuilt 2026-09-09** — the first one's longest text was 174 tokens against `ctx` 512 and 8192, so the gate would have passed the b10630 build that damaged this corpus; it now runs 5 → 8192 tokens with 58 texts filling an 8192 window, id `sha256:7d06a2f6…`, reproducible by `tools/build_calibration_set.py`. Not done: `app/verify/backend_gate.py` still records its own first run as a baseline and passes. |
| **4** — vectors build/import/export/check | **DONE 2026-09-10**, all four verbs. `a`–`d`: 09 §9.7's bytes (`bundle.py`), the four files (`vectors.py`), the gathering and `hold vectors export`; `c` needed no code. **`e`** (`dc8a2b2`, `567574b`): `app/hold/admit.py` is 03 §3.5's ladder, written fresh rather than wrapping `app/verify`, whose tiers disagree with §3.5 in substance and whose `trust.py` implements the TOFU §3.5 and 08 §8.1 forbid; `app/hold/land.py` places the admitted rows against this peer's own cut of its own records, and `embeddings.upsert_chunks` writes them keyed on the CHUNK ID rather than the document. **`f`** closed with it — `licensing.require_ack` on both doors, the archive's and the importer's. **`g`** (`f01c04f`): `app/hold/admission.py`, 08 §8.2's aggregate limit — 64 MB and 1000 objects per hour over material from keys whose work this peer has not seen reproduced, and a bounded queue that drops the NEWEST — plus the `reproduced` ledger it rests on, which is why it lands here and not with step 9: the fact accumulates. **`h`** (`de8d5c4`): `vectors_check.py`, 03 §3.2's `vectors-sample` with `--drop`. **And `build`** (`8fd9cab`), the verb `a`–`h` never named and step 6 runs at point 5 — its gate is a passing calibration statement for THIS `space_id`, read through the verifying envelope reader, which is what b10630 cost. Findings: T0's envelope cannot judge a bundle under 32 rows; `dim`/`dtype` are inside `space_id` so those header checks are only about a header that lies; §3.2's statistical verdict and 02 §2.4's per-row `--drop` are at different granularities on purpose. Two readings flagged in the plan for the operator to disagree with: `--budget` off by default on an operator-named import, and `app/bundles/trust.py` left alive until step 12. |
| **5** — `hold check` | **most of it done, 2026-09-10** (`7440805`, `dca2fa7`). `app/hold/check.py` runs the plan, reports, and writes one attestation per COMPLETED check (09 §9.8's fifteen fields, asserted one by one). Seven checks implemented — `roots`, `records`, `chunks`, `license`, `signature`, `calibration`, `vectors-sample` — and every other row of 03 §3.2's table returns INCONCLUSIVE with its reason, so a plan naming one nobody built says so rather than shrinking silently. `hold records check` is the alias WITH `records` in it. Two things it turned up. **The snapshot sidecar was never written**, so `signature` had nothing to check: §9.4's sidecar is the one signed object that is NOT §9.2's envelope — a set of `{key, time, sig}` merged by union, each over the domain and the 32 raw hash bytes — and `hold snapshot create` now signs it. And **`figures` was missing from `PLAN_CHECKS`**, which made a collection with plates unable to name the check that validates them. Still owed: `centres` and `labels` (need `hold index build --vector`, which no step names), and `records-vs-origin`, which is blocked by the finding below. |
| **6** — proof-of-concept import, one collection | **DONE 2026-09-10 — the round trip closes.** Ran against the node's re-pinned encoder from a local Docker build into two scratch roots. 22 Gutenberg books → 1 shard, **1,637 chunks**; snapshot `2026-08` signed by 1 key; 1,637 vectors built on the Arc; exported as ONE bundle of **8,509,256 bytes** (1,637 chunks over 22 records — under the 15,000 cap, so not split); imported into a SECOND root holding nothing but the drag and its own key. **T0 PASS** (1,637 rows inside the envelope, cosine **0.891**), **T1 PASS** (64 triples), **T2 PASS** (16/16, median 1.0000), **T3 PASS** (16/16, median 1.0000 — the importer's own encoder reproduced the producer's vectors exactly). The producer key was FIRST SEEN and admitted, which is the TOFU regression closed. `hold check` on the importer: all seven planned checks PASS, seven signed attestations written. **Wall clock**: create 4m39s (embedding the 512-text calibration set), records build 3.7s, snapshot <1s, embedder check 4m39s, vectors build **6m13s** (~1.5M tokens, ~4,030 tokens/s), export 5.5s, import **8.5s**, check 4m45s. Refused: 0. Unplaceable: 0. |
| **6b** — the legacy vectors | **mostly dissolved 2026-09-10.** 99.4 % of every vector this node holds came from an ARCHIVE (35.3M MedCPT, 12.7M Upstash) and re-enters by `hold vectors import --archive`, credited and re-proved. What is left is ~253,000 chunks this node embedded itself across pubmed and wikivec — hours of re-embedding, not the GPU-days that made the operator's question hard |
| **7** — what may be in a cleaned corpus besides text | **DECIDED 2026-09-09, and it blocks nothing.** The operator settled all four open calls and the amendment landed across all nine documents (`c8adbc6`): a figure is an optional MEMBER, never in `content_hash`, in a record line declared per collection by the Codex's `record_line`. Step 8's three destinations are all whole as text, and Gutenberg-with-plates is a NEW collection (operator, 2026-09-08), so step 8 runs at `record_line` 1 throughout. What is owed is CODE — `CanonicalDoc.figures`, the figures member kind, the snapshot list, the record key — and it is owed by the first collection that carries plates, not before. `record_line` 2 is refused at create until then |
| **8** — full migration | not started. **Scope fixed by the operator 2026-09-10**: four destinations, not seven — `gutenberg`; `pubmed` + the MedCPT pre-embeddings; `wikipedia` + the wikivec pre-embeddings (wikivec stops being a collection and becomes wikipedia's layer 1); and the three genome collections **stay on the old format**, because no DNA calibration set exists so `create` cannot write them a Codex at all |
| **9**–**12** — share, certified, ask, retire | not started |


### The node's deployment — replaced 2026-09-10

It was **pre-migration**: `src/app` dated 2026-08-28, no `app/hold` at
all, so none of steps 0–6 was on it. Operator: *"the current louise in
the nas is an half cooked deployment, replace it."*

Replaced by the procedure in `deploy/reference/DEPLOYMENT.md`, which was
itself wrong in two ways and is now fixed:

* **`calibration/` was not in the deploy tar.** `app/hold/calibration.py`
  resolves its sets as `<src root>/calibration/<modality>.jsonl`, so the
  documented deploy shipped code that could not find its own calibration
  set.
* **`COPY calibration` was not in the Dockerfile either** (`9b883ed`) —
  the same omission one layer down, and the one that mattered more.
  **Every local test run bind-mounts the repo over `/srv`, which shadows
  the image's contents entirely**, so 1942 passing tests here have never
  once exercised what `COPY` actually put in the image. It took a
  `docker run` with no mount, on the node, to see it.
* The procedure now **stages then promotes** rather than extracting over
  the live tree. `tar -x` overlays: a module deleted on the branch
  survives on the node and stays importable, which is one way a
  deployment becomes half-cooked without anyone doing anything wrong.

Verified rather than assumed: `calibration/text.jsonl` hashes to
`7d06a2f6…` on the node — the very set id the Codex names — and the
suite runs **1942 passed** in the built image (a first run had one flaky
failure, recorded above). The previous tree is kept at `src.old/` until
the operator is satisfied.

**The app itself is still STOPPED.** It was stopped before any of this
and starting it is a separate decision: the worker would begin rounds
over pubmed, wikipedia and genome on code that has never run against
them, and `RECORD_KEYS` gained `origin` today, so any pack rebuild writes
different record bytes than the ones on disk.


### The node's collections — cleaned 2026-09-10

`gutenberg-pack` was dropped from the PLAN on 2026-09-10 (operator: *"an
intermediate product, do not consider it"*) and was still a live
collection on the NODE — an active TOML, a worker thread every boot, and
48 GB of text index. Removed: the TOML, its `.paused` twin, and
`/data/gutenberg-pack/`. It held 77,389 documents and **one** embedded
vector, and its 48 GB was an fts index over
the archive pool’s `louise-packs/gutenberg-2026-06`, which survives — so nothing
irreplaceable went, and no ZFS snapshot on `ssd4/apps` is holding the
blocks.

Eleven stale collection TOMLs (`.bak-*`, `.removed-manual`) were moved to
`config/collections/_removed-2026-09-10/` rather than deleted; they are a
few hundred bytes each and are config history.
`genome-dm6.toml.cpu-backup` and `.gpu-backup` were KEPT — they encode a
choice the operator makes by hand, not clutter.

**Seven active collections**, and the worker starts exactly seven
threads: genome ×3, gutenberg, pubmed, wikipedia, wikivec.

**Deliberately NOT removed**, both of which look like candidates and are
not:

* **`wikivec`** — 296 GB and 12.8M archive vectors. Step 8 turns it into
  `wikipedia`'s layer 1; deleting it destroys what the migration needs.
* **`/data/pubmed-pack/`** — 61 GB, and NOT a collection at all: it is
  the pubmed plugin's derived per-container Parquet cache
  (`app/plugins/pubmed/pack.py`). Rebuilt automatically, at ~17 s per
  container over ~1,200 containers, so deleting it costs hours of CPU the
  next time pubmed is read.

There is still **no `hold collection forget`** — 07 §7.3 specifies it and
neither CLI implements it, so this was done by hand. That is the command
step 12 owes, and until it exists every cleanup is manual and unaudited.


### Step 8 — `gutenberg` — DONE 2026-09-10, REPLACED 2026-09-12

**The collection recorded below is retired.** It reads the plain text and
carries no plates, and the operator replaced it with one that reads the
epub — see "Gutenberg with plates" below. Its TOML moved to
`config/collections/_replaced-2026-09-12/` and its data to
`data/collections/gutenberg.retired-2026-09-12/`; nothing is deleted
until the replacement passes its own `hold check`. What follows stands
because it is what step 8 proved, and because the replacement is measured
against it.

**DONE AND VERIFIED 2026-09-10.** The numbers, and the check that proves
them:

| | |
|---|---|
| `collection_id` | `sha256:dd528265706eddb1f56787fcebd34b5339dc6d428f266a8ad0cc8371236d6670` |
| `space_id` | `sha256:c6f8b7c102e9fb489033183d121e4eef63f838e1f24d725c82fcb79c32bfdb1e` |
| snapshot `2026-08` | `sha256:0cf129d224632c17a6a08edc0a347cf9e08b82de33b5e1b54b5d09fd028b7484`, signed by 1 key |
| records | **77,866** in **1,602 shards** across **56 slices** |
| chunks | **8,705,606** |
| drag | 8.9 GB |
| discovery | 395 s — 79,320 seen, 77,866 added, 1,454 without text, 40 empty stubs |
| records build | ~75 min (pass 1 serial at 19.2 docs/s; pass 2 at PARSE_PROCESSES=10) |
| snapshot | 115 s |
| `hold check` | 500 s |

`hold check` over the plan, sampling 81 of 1,602 shards at the plan's
0.05:

```
PASS  roots              1602 shard(s) held, 0 whose bytes are not the snapshot's root
PASS  records            3937 record(s) over 81 sampled shard(s), 0 failing
PASS  chunks             3937 record(s) re-cut over 81 sampled shard(s), 0 failing
PASS  records-vs-origin  3937 record(s) re-parsed from the origin, 0 differing
PASS  license            green: the Codex records no terms text
PASS  signature          1 signature(s) over this snapshot's hash
PASS  calibration        16/16 reached 0.95, median 1.0000
INCONCLUSIVE vectors-sample  nothing to reproduce, which is not a pass
```

Seven signed attestations; the inconclusive check correctly produced
none. **`records-vs-origin` passing on 3,937 records is the `origin`
locator added the same morning, working at scale** — every sampled record
re-fetched from the corpus and re-parsed to the same line, byte for byte.

`vectors-sample` is inconclusive because gutenberg holds ZERO vectors,
which is the expected state: the move to `qwen3-embedding-4b` emptied its
table and this collection starts from nothing. 8,705,606 chunks at step
6's measured 4,030 tokens/s is roughly 23 GPU-days, which is what
`embed = on-demand` exists for and is nobody's afternoon.

**The legacy `gutenberg` is untouched and still what the running node
serves.** Per the operator's rule — complete, verify, and only then clean
up — it stays until pubmed and wikipedia are done too.

**Created for keeps.** `collection_id
sha256:dd528265706eddb1f56787fcebd34b5339dc6d428f266a8ad0cc8371236d6670`,
`space_id sha256:c6f8b7c102e9fb48…`, version 2 / `record_line` 1, on the
node against the re-pinned b10603 encoder. 284 s, all of it embedding the
512-text calibration set. The recipe is
`config/recipes/gutenberg-real.toml` on the node.

**PREFLIGHT ITEM 2 CAUGHT IT, one `create` too late and one records build
in time.** The first attempt was created with `layers = [('book', 1, {},
'default')]` — the plugin's `chunking_profile`, inherited because the
recipe did not declare `layers`. `book` is NOT one of 06 §6.3's four
chunkers; it is a grandfathered name. Preflight item 2 spells out the
consequence: *"03 §3.5 gates the overlap-band check on `fixed` layers, so
a Gutenberg collection whose layer says `book` is never T1-checked by
anyone, for the life of the collection."* `layers` is inside
`collection_id`, so that is not correctable — only replaceable.

The first collection was discarded (it had no records and no vectors) and
the recipe now declares the layer explicitly:

```toml
[[layers]]
rule = "default"
[layers.chunker]
id = "fixed"
version = 1
params = { tokens = 1000, overlap = 120 }
```

Those numbers are the node's own live settings, read from the worker's
environment the same day (`CHUNK_TOKENS=1000`, `CHUNK_OVERLAP=120`,
`CHUNK_VERSION=1`), and preflight item 2 established that `fixed` at them
cuts byte-identically to `book` and produces the same chunk ids. So
declaring it cost nothing and bought T1 for the life of the collection.

**The lesson for `pubmed` and any later `create`: a recipe that omits
`layers` inherits the plugin's grandfathered profile.** The template is
doing what it is told; the recipe has to say.

Note that `space_id` moved between the two attempts — `673f7a32…` with
`book`, `c6f8b7c1…` with `fixed` — because layers are inside it. The
step-6 proof shares the FIRST one, so the throwaway and the real
collection are NOT in one space. That is correct and costs nothing: the
proof proved the round trip, not the space, and the throwaway holds 1,637
vectors nobody will ever import.


**MEASURED 2026-09-10, running it for real: pass 1 parses the whole
corpus SERIALLY, and pass 2 parses it again.** `pack_build.plan` asks
`slicing.slice_hint(origin, row)` for each document's slice and falls
back to a full `plugin_io.canonical_doc` parse when the hint cannot
answer. Gutenberg slices by `lang` — and `discovery.sync_collection`
inserts `(uid, source, native_id, path, txt_name, mtime, size,
updated_at)` and **no lang**: lang is filled later, by the rdf stage. So
on a FRESH manifest the hint never fires, and pass 1 parses every book
just to learn its language.

Measured on 77,866 books: **19.2 documents/s, single-threaded** —
`plan` does not use the parse pool — for about 68 minutes, before pass 2
starts and parses the same corpus again (that one does use the pool).
Discovery itself was 395 s for 79,320 directories.

It is not a bug and nothing is wrong with the output; it is a per-corpus
cost paid twice, and CLAUDE.md's rule about costs that scale with the
corpus is the one it brushes against. Two cheap fixes when someone wants
them, in order of value:

* have discovery record `lang` when the plugin can supply it cheaply
  (gutenberg's RDF is already opened during the scan), so `slice_hint`
  answers from the manifest and pass 1 costs a table walk;
* failing that, give pass 1 the parse pool, which it has no reason not
  to use — it is the same parse per document that pass 2 parallelises.

Also worth fixing in the deploy doc: `python -m app.louise` in a
container is not a tty, so **stdout is block-buffered** and `pack build`'s
progress lines do not appear in `docker logs` until the process exits.
The discovery lines do appear because they go through the app logger to
stderr. Run detached builds with `-e PYTHONUNBUFFERED=1`; without it a
working build is indistinguishable from a hung one, which cost twenty
minutes of doubt here.

### Gutenberg with plates — created 2026-09-12, corpus fetching

The operator's decision: one Gutenberg collection, reading the epub where
the origin publishes one, and the plain-text one deleted. Not two
collections, not a sister — the plates edition recomputes its records from
the origin with a different parser and a different cleaner, which makes it
a collection created FROM A SOURCE and therefore at the root of its own
lineage (02 §2.7: "a collection created from a source has none").

| | |
|---|---|
| `collection_id` | `sha256:e395a9cd1e21d1da54d4fa3329fdd5cd4e31193117c255dd057793fb49de6954` |
| `cleaning_version` | 2 — `pg_epub.read_clean` reflows paragraphs where `pg_text.read_clean` keeps the .txt's hard wrap |
| `record_line` | 2 — the line carries `figures` (09 §9.5) |
| `parser` | gutenberg version 5 — the three-way reading rule, first form that can be READ |
| layers | one, `fixed{tokens: 1000, overlap: 120}` version 1 — byte-identical to the retired collection's cut |
| validation | `roots, records, chunks, records-vs-origin, figures, license, signature, calibration, vectors-sample` |

**The reading rule, recorded as `origin.form` in the Codex (invariant
11):** the epub where the origin publishes one, because the plates are
only there and nowhere else; the plain text where it does not; the
catalogue record where neither exists. One rule applied in that order, so
a verifier reaching for the same book directory reaches the same form.

THE FIRST FORM THAT CAN BE READ, never the first that exists — parser
version 5, after nine books turned out to carry a ZERO-BYTE
`pg<id>-images.epub` dated 2009 beside a good plain one, and `fts` died
on three of them with `BadZipFile` within the hour of switching the
collection on. `pg_epub.readable` asks the same question `read_clean`
will — the zip opens, the container names a rootfile, the package
document parses into a manifest and a spine — so a book cannot be chosen
as an epub and then fail to be read as one. An empty `.txt` and an empty
`.rdf` are refused for the same reason.
`source.pick_form` is the single implementation, and `read_text` /
`read_chapters` — the worker's lazy path for fts and embed — call it too:
if `parse` and the lazy path chose different forms, the index and the
records would describe different text for one document and every chunk id
would name a chunk whose content the record does not contain.

**What the corpus holds, measured 2026-09-12** over a random sample of
2,000 of the 79,377 book directories on the node:

| form available locally | share | books |
|---|---|---|
| `-images.epub` | 60.5 % | ~48,000 |
| plain `.epub` only | 0.25 % | ~200 |
| `.txt` but no epub | 37.1 % | ~29,400 |
| `.rdf` only | 2.2 % | ~1,700 |
| nothing readable | 0.05 % | ~40 |

The 37 % is why the fetch matters: the local mirror was filled by a spec
whose `extras` filter carried `exclude=("*images*",)` — it excluded
precisely the illustrated editions this collection exists to read. The
origin publishes `pg<id>-images.epub` for the txt-only books sampled, so
those ~29,400 books are reading the wrong form until the fetch lands.
`acquire/specs.py` now puts `*.epub` in `filters` and `extras` is empty.

**The fetch, planned 2026-09-12:** 393,151 files, 303.6 GB upstream
against 241 GB held, so roughly 60 GB genuinely missing. `full-resync`,
mirrors benchmarked at fetch time (ibiblio 2.002 s against aleph 3.028 s
on `1/pg1.rdf`).

**THE CORPUS WAS MOUNTED READ-ONLY, and the fetch reported progress all
day anyway.** This cost most of an afternoon and it is the most useful
thing in this section.

`/corpus` was `:ro` for both services — correct for years, while the
mirror was populated from outside Louise and the app only ever read it.
The operator's instruction of 2026-09-12 makes acquisition a writer, and
nobody changed the mount.

What made it expensive is that the failure is shaped exactly like success.
`is_adoptable` only reads and hashes, so every file already on disk was
adopted normally: 6,270 of them, 1.41 GB, and the card's fetched counter
climbed steadily for hours. Every file that had to be DOWNLOADED failed
at the instant it was promoted out of staging —
`OSError: [Errno 30] Read-only file system` — 8,238 times, onto rows
nothing surfaced. A fetch at 6,000 files and climbing that had delivered
not one new byte.

Three of the day's diagnoses were wrong because of it, and each wrong
answer was reached honestly from a real measurement:

* "the fetch is at 60 files an hour, 273 days" — the rate was real and it
  was the rate of ADOPTION, throttled by hashing 241 GB behind a busy
  pool, not by any network.
* "ibiblio accepted the connection and went silent" — true, and a
  side-show: those transfers were going to fail at promotion regardless.
  The `--timeout` fix is right on its own merits and fixed nothing here.
* "no files landed in 90 minutes" — CORRECT, and I talked myself out of
  it, because rsync preserves origin mtimes so `find -newermt` cannot see
  a landed file. The instrument was wrong and the conclusion it gave was
  right; I trusted the instrument.

The discriminator that settled it is one column: `endpoint_url` is
`(adopted from disk)` for an adoption and the mirror's address for a
download. Grouping completed rows by it took one query and would have
answered the question at any point that afternoon. `bytes_fetched` does
NOT discriminate — an adoption records the file's full size, which is
what sent me down the "it is re-downloading everything we have" path for
ten minutes.

The worker now mounts the corpus read-write; the web service stays
read-only, because acquisition is the worker's job and least privilege on
the operator's own book library is worth the extra line. The 8,238
read-only errors were cleared with their retry counts — an environment
fault should never have written an error to a row at all ("deferral is
not failure"), and a file that burned three attempts against a read-only
mount would otherwise stay out of every future pass.

**On the mirrors: BOTH refuse us intermittently, and it is not aleph.**
The first read of this was wrong — "exit 12 against aleph while ibiblio
serves normally" — because it sampled the error table and the error table
was full of pre-fix rows. Split by timestamp, since the mount fix:

| endpoint | downloaded | failed |
|---|---|---|
| ftp.ibiblio.org | 798 | 412 |
| aleph.gutenberg.org | 289 | 83 |

ibiblio fails MORE in absolute terms and both fail about a third of
attempts. What they say, in full:

```
exit 10: failed to connect to ftp.ibiblio.org (152.19.134.42):
         No route to host (113)
         ...(2606:f640:...): Connection refused (111)
exit 12: [receiver] read error: Connection reset by peer (104)
```

A refusal at connect time on one and a reset mid-transfer on the other —
both consistent with a public mirror throttling a single client, and both
intermittent rather than a block: two thirds of attempts succeed.

`[transfer] per_endpoint` is already 4 and `workers` 8, the conservative
defaults, and nothing on this node overrides them. (`docker top` shows
~14 rsync processes, which reads as a violation and is not: rsync forks a
receiver child, so that is ~7 transfers against a cap of 8.) Lowering
per_endpoint to 2 might trade some parallelism for fewer refusals and
come out ahead — it might equally halve the rate — and that is an A/B
worth one measurement rather than a guess, on a fetch that is currently
healthy at **3.67 GB/h measured over 180 s, ~16 h for the ~60 GB
missing**. Not taken while the pass is running.

**An unreadable origin file is a CORPUS FACT, not a stage failure.**
`BadZipFile` escaped as `fts: …`, and a stage-prefixed error is what the
Library counts as BLOCKED and what burns a retry — the oscillation
`tests/test_unindexable_stability.py` exists to prevent. `parse` and
`read_text` raise `UnindexableDocument` unprefixed now. In practice the
rule self-heals first: a file that stops being an epub stops being the
form, and the next one is chosen.

**Do not run `records build` until the fetch completes.** A book that has
an epub upstream and only `.txt` here parses to a record a verifier
cannot reproduce — `records-vs-origin` re-fetches and reaches for the
epub. Discovery self-corrects when the epub lands (the chosen file's
mtime and size move, so the change gate resets `rdf_done`, `fts_done`,
`embed_done`), but a shard already built does not.

**The id was minted three times, and each mint was a defect caught before
a single record existed.**

| id | why it was replaced |
|---|---|
| `sha256:88075d0a…` | its `validation` plan did not name `figures` — 51 GB of plates and nothing that ever re-hashes one. `validation` is inside `collection_id`, so it was re-minted rather than amended; `template._validation` now refuses a `record_line` 2 recipe that omits the check |
| `sha256:9a857d31…` | parser version 4 took the first form that EXISTED, and Project Gutenberg publishes zero-byte `pg<id>-images.epub` files — nine here. `fts` died on gutenberg:2215, :2202, :2208 with `BadZipFile` within the hour. The rule takes the first form that can be READ, which is parser version 5 |
| `sha256:e395a9cd…` | current |

That the cost of each was one `create` is the whole argument for finding
them before the build: `record_line`, `validation` and `parser` are all
inside `collection_id`, and none of them can be corrected afterwards —
only replaced, by a collection every peer would have to adopt again.

### PubMed — where the archive's coverage ends, measured 2026-09-12

The question "is the genesis import finished" had no answer anyone could
give from the log, because a fetcher that downloads nothing is silent.
Measured:

| | |
|---|---|
| records | 39,994,988 |
| embedded from `medcpt-pubmed` | 35,267,888 |
| unembedded OUTSIDE the archive's range | 4,538,777 |
| unembedded INSIDE it, before the fix below | 188,323 |
| unembedded INSIDE it, after | 38,324 |

The archive declares ids to **36,999,999** over 37 shards of 1M. The
4.5M gap is PMIDs past that — post-snapshot documents the archive was
never going to cover, and the local encoder's work whenever the operator
spends it (`embed = "off"` today). It is not a stall and never was.

The 188,323 inside the range WERE stuck, and that was a defect: five
shards sat `applied` with their files gone, the drain would not walk a
shard that is not on disk (the 2026-09-11 fix) and the fetcher would not
re-fetch anything in state `applied`. Neither half was wrong alone;
together they had no path forward. `next_shard_to_fetch` now takes such a
shard — last, after every shard never fetched — and the re-fetch keeps
the `applied` state so the pass is a revisit rather than a re-import of
a million documents.

**OUTCOME, the same day.** The five shards came back, applied, and went
`final` within ten minutes of the fix landing:

```
medcpt-pubmed/9:  imported  29,999 vectors; FINAL
medcpt-pubmed/8:  imported 120,000 vectors; FINAL
medcpt-pubmed/10: imported       0 vectors; FINAL
medcpt-pubmed/11: imported       0 vectors; FINAL
medcpt-pubmed/12: imported       0 vectors; FINAL
archive fetch (round): landed 5 shard(s), stopped: nothing pending
```

**All 37 shards are `final` and the archive import is COMPLETE** —
35,417,887 embedded, of which 149,999 arrived because of the fix. The
three that imported nothing reported 55, 33 and 19 chunk-shape
mismatches: multi-chunk documents a single-vector-per-document archive
cannot cover, skipped rather than half-embedded, which is the designed
behaviour and has its own test.

Worth recording about the transport. NCBI cut shard 8 at 2,194,943,575
bytes of 3,046,960,256 — "peer closed connection without sending complete
message body". It was DEFERRED, not errored onto a document, and the
retry resumed from the `.part` by Range and finished the remaining
700 MB in 59 seconds. That is invariant 2's byte-granular resume doing
exactly what it exists for, against a server that will not serve a 3 GB
file in one go.

**`hold check` — ALL EIGHT PASS, 2026-09-12. Pubmed is verified.** The
first complete run, against snapshot `2026-08` (`sha256:3bfe1d1d…`), seed
`313fc965…`, with eight signed attestations:

```
PASS  roots              1358 shard(s) held, 0 whose bytes are not the root
PASS  records            2010881 record(s) over 68 sampled shard(s), 0 failing
PASS  chunks             2010881 record(s) re-cut, 0 failing
PASS  records-vs-origin  2010881 record(s) re-parsed from the origin, 0 differing
PASS  license            the terms are the Codex's
PASS  signature          1 signature(s) over this snapshot's hash
PASS  calibration        16/16 reached 0.95, median 1.0000
PASS  vectors-sample     960 chunk(s) over 68 sampled shard(s) reproduce at 0.95
PASS  pubmed  snapshot 2026-08
```

**`vectors-sample` is the one that had never run.** Every earlier attempt
was INCONCLUSIVE for want of a single vector to sample; this one
re-embedded 960 chunks on the MedCPT encoder and reproduced the archive's
own imported vectors at tolerance 0.95. That is the trust question for
pre-embedded data answered on this node's own hardware: the 35.4 M
vectors that arrived from `medcpt-pubmed` are the vectors they claim to
be.

THIS IS THE FIRST COLLECTION IN THE CODEX FORMAT TO PASS ITS WHOLE PLAN.

**`records-vs-origin` at 2,010,881 records with zero differing is
invariant 11 proven at two-million scale**: every sampled record
re-fetched from the PubMed baseline and re-parsed to the same bytes.

AND ALL THREE OF THAT MORNING'S FAILURES WERE THE CHECKS, NOT THE
COLLECTION. `license` compared a bare digest against 09 §9.2's prefixed
id form and so could never pass on a yellow collection. `signature`
failed because the `louise-snapshot` version bump had made the LOADER
re-serialise an existing v1 document as v2, giving it a hash its own
bytes do not produce. `records-vs-origin` called 1,640,709 records
"differing" when the origin was mounted read-only and every OSError
landed on the failed list. Pubmed's records, terms, vectors and signature
were sound throughout; it took three fixes to the INSTRUMENTS before the
node could say so. A day's lesson in one collection: the check that
reports a catastrophe is as likely to be broken as the thing it reports
on, and under promise 8 a false catastrophe is an argument for replacing
a collection that was never wrong.

### What is waiting on the operator

* **The legacy artefacts each migration leaves behind** are tracked in
  [`legacy-to-delete.md`](legacy-to-delete.md), with the conditions that
  must hold before each is removed (operator, 2026-09-10: complete,
  confirm, and only then delete). About 587 GB is recoverable once every
  condition is met, and none of it before.

**Three were settled 2026-09-10** and are recorded here rather than
deleted, because each was a genuine fork:

1. **The record's `origin` locator** — added to both writers
   (`c4cba30`). 09 §9.5 requires it of every record and `RECORD_KEYS` did
   not carry it, so half of invariant 11 was missing and a peer that
   RECEIVED a shard could never re-fetch its source.
   `records-vs-origin` now re-parses byte for byte. Measured blast
   radius: the record LINES and so each shard file's transport root, and
   nothing else — no `records_root`, no `chunks_root`, no chunk id, no
   vector, nothing re-embedded.
   [`records-origin-gap.md`](records-origin-gap.md) keeps the reasoning
   and the one divergence still open in that list (`layer` and `chunks`,
   which retire with the legacy pack verifier at step 12).
2. **The Codex is written at version 2 with `record_line`** (`248dd3b`).
   Version 1 is still read and never rewritten. `record_line` 2 is
   refused at create with its reason — the figures machinery is unbuilt.
   It also turned up that step 0's "single most valuable test in the
   migration" was pinning a version-1 variant rather than the document's
   own worked example.
3. **A bundle is about 15,000 chunks** (`9eb9ba5`, and `8fb195c` on
   main), split on RECORD boundaries. The documents disagreed — 04 §4.5
   said "a few hundred", 09 §9.10's example shows 14,800 and 15,200 — and
   the numbers settled it: ~2,400 coverage rows per fully covered PubMed
   instead of ~120,000, and T3 at 0.16 % of production instead of 5.3 %.
   The crash-granularity argument for small bundles is gone now that
   `vectors build` commits every batch.

**Still open:**

- **`--budget` is off by default** on an operator-named `hold vectors
  import`, and **`app/bundles/trust.py` is left alive** until step 12
  retires the old names. Both are readings of step 4g stated in the plan;
  neither needs a decision unless they are wrong.
- **The `refused` field on 09 §9.10's coverage row** — all three
  documents promise the refusal is recorded and no field table has a
  place for it (step 4's operator note).
- **An archive's signing key** — see
  [`archive-signing-keys.md`](archive-signing-keys.md), open since step 1.
- **A DNA calibration set**, which is why the three genome collections
  stay on the old format: without one `create` cannot write them a Codex
  at all.

## Built and running

- **Five plugins**: gutenberg, pubmed, wikipedia, embedded (pre-embedded
  corpora), genome. Each declares `license_class`, `privacy_class`,
  `chunking_profile` and `mutability`.
- **Per-collection embedding spaces.** `collections.recipe_for()` is THE
  resolution point: collection `[embed]` block → `platform.toml`
  `[embed_defaults.<plugin>]` → Tier-1 env, each a fallback and never an
  override. A node runs several encoders at once.
- **`space_id` immutability.** Pinned per collection; refuse-to-boot on
  mismatch, never auto-migration. Includes the chunker (amendment 28).
- **Acquisition**: declarative `AcquisitionSpec` per source, profiles
  (incremental / exclusive / additive), delta gate, checksum sidecars,
  license gates, disk preflight, resumable at byte granularity, **adoption**
  of files already on disk.
- **Transfer manager**: central token bucket, priority classes, schedule
  windows, per-endpoint etiquette, multi-connection single-file downloads
  (off by default).
- **Verification ladder** T0–T4 with quarantine and verify-on-use; genesis
  import for reusing published vectors.
- **Web UI**: Ask / Library / Stats / Network / System, server-rendered.
- **Snapshots and deltas**: manifests, parent-chained deltas with
  tombstones, vector deltas as ordinary bundles.
- **BitTorrent v2** transport and corpus publishing.

## Pipeline performance (2026-08-21/22 review — measured, not estimated)

A profiling pass over the ingestion pipeline on a 33M-document manifest
removed four independent per-document costs. All numbers were measured live
before and after; the commit messages carry the full evidence.

| defect | cost before | after |
|---|---|---|
| tantivy writer+commit per document (`fts.index_books` now batches) | 136.9 ms/doc | 0.6 ms |
| HGNC symbol dict rebuilt per document (resolver now memoised on `ref_meta` version) | 75.6 ms/doc | 0.03 ms |
| `DELETE FROM catalog_fts WHERE uid=?` scans the FTS5 shadow table (`uid UNINDEXED`) — **extraction was O(N²)** | 14.02 ms/doc at 36k rows, growing | INSERT OR REPLACE by `documents.rowid`, size-independent |
| `ORDER BY CAST(native_id AS INTEGER)` sorting the whole set per browse page | ~10 s/page | 1 ms (expression index, worker-built) |

Net: **rdf 16.0 → 0.36 ms/doc; fts ~130 → ~1 ms/doc warm.** Structural rules
that came with this, worth preserving:

- **rdf and fts interleave a batch at a time** (`RDF_BATCH_DOCS` /
  `FTS_BATCH_DOCS`, default 200): fts lags rdf by at most one batch, never
  by the corpus. Mark done only AFTER the commit, inside one transaction.
- **A locked manifest or unreachable encoder is a deferral, never a
  document error** (`manifest_contended` / `endpoint_unavailable`): recording
  an error removes the document from every selection until a human clears it.
- **Selection queries must be plan-checked, not just behaviour-checked** —
  tests assert no `TEMP B-TREE` / `LIST SUBQUERY`; a merely-slow query
  passes every behavioural test.
- **Scan cursors key on path relative to the corpus root**, with a legacy
  bare-name fallback on exact signature match.
- **Cold single-document probes measure the container, not the pipeline**
  (gzip cannot seek): pubmed rdf is 5.8 s cold and was 0.36 ms warm.
  Benchmark warm, over hundreds of documents.

**2026-08-24 — the queue-eligible join was the pipeline's ~20-40x brake.**
The lazy-queue selection at the top of every `_next_book` let the planner
invert its join: SEARCH over ALL in_scope rdf_done=1 documents (3.6M on
pubmed, growing with progress) probing the usually-empty queue per row,
plus a TEMP B-TREE — 4.9 s per step, measured live, in every collection
with embed_mode lazy/all. CROSS JOIN pins queue→documents; measured
after: pubmed rdf+fts at 894k docs/h (was ~18-54k/h), remaining corpus
~40 h, not weeks. The plan test asserts join order — behaviour alone
returns the same rows either way. Batch sizes bumped to 1000 in the
node's worker env. Diagnosis chain preserved in commit and
DEPLOYMENT.md; the container-cache cold-parse log and batch timings that
found it stay in.

**2026-08-24 — extra formats, and the p2p exchange policy** (amendment 35).
Peers exchange only the data that fulfils Louise's purpose. An origin's
alternative formats (gutenberg's epubs) are now declarable per spec
(`spec.extras`) and opt-in per collection (TOML `extras = ["epub"]`,
chosen at create or in grow) — fetched from the corpus source as a LOCAL
convenience, classified exclusively (`fetch_files.extra`, split
`acquire_listing` totals), shown as a separate `(+ e/E)` pair on the
Library card, and excluded unconditionally from published corpus torrents.
Explanation lives in Help's Acquisition tab; the GUI stays clean.

**2026-08-23 — discovery is batched, resumable and off-thread** (amendment
32). The startup scan used to hold the collection's pipeline thread (wikivec:
1,083 s per restart, no rdf/fts/genesis meanwhile), ran in autocommit (one
transaction per row — that WAS the 1,083 s), and saved its cursor only at
the end, so a restart mid-scan threw the whole pass away. Now:
`sync_collection` commits every `DISCOVER_BATCH_REFS` (2,000) refs and
signals the plugin (`DiscoverState.received/committed/on_commit`);
`cursor.Progress` promotes a container into the saved cursor only once its
last ref is committed, so an interrupted scan resumes exactly where its
rows are durable; the worker runs each collection's scan on its own thread
(`_start_scan`, one at a time, stop-aware on SIGTERM and removal, joined at
shutdown). The embedded plugin's boundary straddler made this subtle and
exposed two latent locator bugs, both fixed: a rescanned shard whose
neighbour is skipped now completes the carry from that neighbour's tail or
head (it used to mint or "relocate" the straddler with one segment), and
an embedded ref's `size` is the article's row count, not its shard's byte
size (a straddler gaining a segment is now a change, never a relocation).
`tests/test_discovery_resumable.py` pins it, including every interruption
point of a straddling fixture. The genome collections are parsing/indexing
only by operator decision — embedding awaits an architectural review
([`dna-search-axes.md`](dna-search-axes.md)).

**2026-08-23 — local genesis archives no longer sweep the shard list.**
Measured live on wikivec (959 shards, CPU encoder, so the pipeline thread
owns rdf + fts + genesis): with 200 embed-eligible documents present,
genesis claimed every round re-reading the next shard oldest-first (~437 MB,
~10 s, "imported 0 vectors across 0 documents") and rdf/fts got one turn
per full sweep — **200 documents per 3.2 hours, 13.5 M pending.** Now the
candidates' own locators name the shards worth reading (an index range over
`idx_documents_scope`, plan-checked), a shard nobody waits for is never
opened, documents a read cannot cover (boundary straddlers) are remembered
as residual per shard, and while rdf/fts still have work a read waits for a
batch (`GENESIS_MIN_CANDIDATES`) or an age cap (`GENESIS_MAX_WAIT_SEC`).
`tests/bundles/test_genesis_starvation.py` pins all of it, through the
worker — keep it passing across any rework of this path. Open follow-on:
the read still loads a whole shard's `embedding` column for the ~200–1000
documents it wants; the locators carry row ranges, so reading only those
row groups would cut it to a fraction (and would make straddlers
importable by reading both segments).

**2026-08-25 — remote genesis archives are fetched like a corpus and
applied as indexing goes (amendment 38).** Live pubmed after weeks:
8.25M of 40M indexed, **0 of 37 MedCPT shards fetched, 44 documents
embedded** — the post-settle gate (apply once, after the whole collection
had passed rdf/fts) held the free vectors back for the months the
indexing takes, and the embed thread would have downloaded each 3 GB
shard inline when it finally ran. Now `app/bundles/genesis_fetch.py`
lands shards on its own thread under the `archive` budget class (below
`backfill`: the corpus wins the link), bounded on disk
(`GENESIS_CACHE_BYTES`, default 24 GiB ≈ the active shard and a few
ahead) with a disk preflight; `drain_next_shard` applies whatever is on
disk for the documents indexed so far, revisits with a per-shard
geometric backoff (`GENESIS_MAX_WAIT_SEC` doubling to
`GENESIS_PASS_BACKOFF_MAX_SEC` after empty passes), and finalises — files
deleted, `coverage_epoch` bumped — once its id range has no document
waiting. MedCPT's coverage is declared (`ids_per_shard=1_000_000`,
verified exact against chunks 0/1/36), so the worker's "all" backfill
excludes covered ranges by predicate and walks the scope index from a
rowid cursor (`_backfill_rows`; plan-checked: no TEMP B-TREE — the old
`ORDER BY (lang='en')` sorted every candidate per round). Per-flush
manifest writes in `import_shard` are now one `BEGIN IMMEDIATE`
transaction (they were one commit per UPDATE). Observed on the NAS the
same evening: shards 0–2 landed at 7.5–10.8 MB/s; the first pass over
shard 0 ran at **~150 documents/s** on pubmed's own pipeline thread —
a container parse per 30k documents (0.6 ms/doc) plus one Lance
`delete` PER DOCUMENT on a growing table (the quadratic trap again) —
i.e. ~1.8 h per shard with rdf/fts stalled meanwhile. Two fixes in the
same change: the flush deletes ONCE per batch (`book_id IN (...)`), and
a pass is **budgeted** (`GENESIS_PASS_BUDGET_S`, 300 s): it commits what
it has, records the row as `applied` with unknown coverage, and resumes
after an equal share for rdf/fts; a stop (SIGTERM) resumes at once on
restart, and a row left `applying` by a crash resumes the same way.
`idx_documents_native_int` is what makes the pending range count cheap
— the worker builds it at startup. Then the restart's first pass went
to shard 1 and **its same-function proof failed at worst-of-8 cos
0.874** — while shard 0 had proved and imported, and a per-document
check of shard 1's single-chunk samples sat at ≥ 0.993: one sampled
document whose text changed since NCBI's 2023 snapshot. A snapshot of a
living corpus always has some. The proof is now a **quorum** (16
samples, median ≥ τ and 75 % ≥ τ; the samples below τ are stale
documents, evicted from the batch and left to the encoder, never
imported), and a rejected shard stops the archive ONLY while nothing has
proven it — under a proven archive the rejected shard alone is out and
its id range is released to the local encoder. (Shard 1 on the NAS,
rejected under the old rule, was reset to `fetched` by hand —
`UPDATE genesis_imports SET state='fetched', detail=NULL WHERE
shard='1'` — and re-proved; the recipe for any future case.)
Also measured: roughly half of the sampled 1970s-era shard-1 documents
chunk into TWO pieces locally, so the shape gate skips them (the
`chunk_abstract` follow-up in *Known gaps* is what would lift that).
Deployed and re-run: shard 1's quorum proof passed with exactly one
stale sample skipped; the cache bound held the fetcher at 8 shards
(22 GiB, "next fetch when one is final"). One more placement fix from
the same evening: a remote archive's pass runs on the collection's
PIPELINE thread (`_drain_remote_archive`), never on the GPU-serial main
loop — a collection whose encoder is the shared GPU would otherwise
block every other collection's embedding for each 300 s slice.
**Resurfaced 2026-08-26 (amendment 42):** shard 1 proved and imported
~950k vectors over thirty passes, each pass skipping the same one stale
document, and was then REJECTED by a pass whose only eligible document
was that one — the proof re-ran from scratch every pass, and
`RE_ENCODE_MIN_SAMPLE = 1` let a sample of one be a verdict. Fixed at
the root: the proof is per archive and persists (`proven_at`; older
rows count by their imports), later passes only screen for staleness, a
verdict needs ≥ 8 failing samples (`RE_ENCODE_REJECT_MIN`), fewer on an
unproven archive are inconclusive (deferred, nothing written; an empty
sample likewise), and `genesis retry <collection> [--shard N]` puts a
rejected shard back in line — used on the NAS for shard 1 after the
deploy.
**Scheduling review (2026-08-27, operator: "is the scheduling of the two
archives optimal?") — measured from the worker log, not optimal, three
fixes each with a regression test (amendment 45):** (1) a MedCPT revisit
pass walked the million-row shard from row 0 with a manifest lookup per
row, was cut by its 300 s budget re-skipping what earlier passes had
imported, and each pass imported less than the last (86k, 101k, 87k,
72k, 68k, 58k, 49k) — half of pubmed's pipeline thread; now one indexed
range query names the eligible documents of the shard's id range and
only their rows are read from the mmap (`tests/bundles/test_genesis.py::
test_a_revisit_pass_costs_the_new_documents_not_the_shard` counts the
per-document lookups). (2) every 1,000-document wikivec fts batch
triggered a read of the whole 437 MB shard to import those documents
(`GENESIS_MIN_CANDIDATES` = the fts batch); a candidate read now opens
only the row groups the candidates' locators name
(`test_genesis_local.py::test_a_candidate_read_touches_only_the_
candidates_row_groups`). (3) the vector-index refresh ran 41 minutes on
wikivec's pipeline thread (`115k rows folded in (2448s)` — compaction
of a 16.8M-row table on the HDD pool) and idled rdf/fts/archive for the
duration; index maintenance now runs on its own thread
(`index-maintainer`), marks the collection busy so the embed step and
archive pass defer for it (rdf/fts continue), and a refresh is due at
50k rows AND 2 % of the table (`VECTOR_INDEX_REFRESH_FRACTION`;
`tests/test_vector_index.py`). Measured before the fixes: pubmed fts
≈ 35 docs/s wall with ~50 % of its thread in archive passes; wikivec
1,000 docs per ~50 s of which ~10 s was the shard read. Measured after
the deploy: the first pubmed pass imported 96,500 documents with zero
rows walked in vain ("0 not held locally"), still cut at 300 s because
that many documents were eligible — the budget now bounds real imports
(~320 docs/s), not re-walks; the pubmed index refresh ran 41 s on the
maintainer thread.
**License gate for packs (2026-08-27, operator: the same license as the
corpus and pre-embeddings; amendment 46):** `app/licensing.py` is the
one resolution (class from the spec or the pack manifest, terms from the
spec's file or the pack's byte-exact `TERMS.txt` — same sha256, one ack
valid for both); the worker withholds the corpus scan and the archive
pass without a valid ack, `pack build` refuses an unacknowledged origin,
the create dialog shows the pack's terms and requires acknowledgement
for a yellow/red pack, the card and `/terms` speak for packs. Tests:
`tests/test_license_gate.py`. Invariant 9's "ingestion gate designed,
not built" is now built at the worker and the UI
(`discovery.sync_collection` itself stays callable ungated).
**The parse pool (2026-08-27, operator: "CPU close to 100 % when there's
not much else to do"; amendment 47):** measured 12 cores, load ~4, the
worker at 232 % with one Python thread at 87 % — the GIL — and the CPU
encoders idle by design (the archives cover the indexed ranges).
`app/parallel.py`: fetch/parse/chunk for an fts batch, metadata for an
rdf batch and records for `pack build` run in `PARSE_PROCESSES` (default
cores−1) spawned children at nice 10; writers stay in the worker; one
(ok, value) per document, in order. The `embedded` plugin's fetch reads
row groups instead of caching a whole shard per process. Tests:
`tests/test_parallel.py` (real children: same chunks/metadata/records as
the serial path, byte-identical packs, a failing document alone), the
embedded row-group test. **Measured after the deploy** (12 minutes of
manifest samples): worker 232 % → ~830 % of 12 cores, load 4 → 16, the
GPU encoder unchanged; pubmed fts 35 → 392 docs/s, wikivec rdf+fts 20 →
515 docs/s, wikipedia rdf+fts 450 docs/s — 11–25× on the parse-bound
collections. Memory grew 11.7 → 15.8 GB in 15 minutes as the children's
caches filled: children are now recycled every `PARSE_TASKS_PER_CHILD`
(500) sub-batches. pubmed's "parsed (rdf) TBD" was not a stall: rdf led
fts by 4.2M documents and the worker gives fts the thread while a full
batch is ready — the card now says "after fts (N ahead)" (test).
**Scheduling monitored again 2026-08-28 (operator: "not yet optimal"),
amendment 48.** Measured live (12 cores): pubmed rdf+fts 510–790 docs/s
through the pool, worker 1115 % / load 19.6 — the CPU side IS saturated
now; wikivec's archive 96–115 docs/s; pubmed's archive 943 docs/s while
a pass runs, 0 during its 300 s yield (≈470 average). Two limiters
found and fixed: (1) 34 pubmed pack builds/hour × ~17 s were paid INLINE
by the batch that first touched a container (~45 % of that pipeline's
non-archive time) — plugins now declare `prepare`/`prepare_key` and the
stages prefetch a few thousand documents ahead into the pool
(`tests/test_parallel.py`); (2) no index on `book_id`, the column every
delete-then-insert write filters on — 0.90 s per 500-document flush on
wikivec (35M rows), 0.59 s on pubmed (16M) — the maintainer now builds a
BTREE on it before the vector index (`tests/test_vector_index.py`).
**→ All of this now lives in [`scheduling.md`](scheduling.md)** (operator,
2026-08-28: "keep all these informations about scheduling together… we
keep reworking the scheduling again and again"). That file is the single
reference — the runner/resource model, the measured cost tables, a
ten-entry pitfall catalogue and a pre-change checklist — and
`tools/sched_report.py` is its instrument (`--queries` plan+time the hot
queries, `--phases` where an archive pass goes, `--rates`, `--pool`).
CLAUDE.md now requires reading it before any change to what runs when.
The entries below are the incident history that produced it.
**Checked twice (operator: "it's not the first time you leave some
inefficiency behind") — amendment 49, and the double-check found more
than the first pass.** Every hot query was then TIMED against the live
manifests, not just plan-checked. My own prefetch probe was the first
casualty (`OFFSET k` over the whole pending set → SQLite restarted at
rowid 1 and re-filtered 28.6M done documents, three times per call:
the 10.5 s rdf batches); anchored on the current rowid it is 0–27 ms,
and constraining the FULL scope-index prefix avoids the TEMP B-TREE the
first anchored version still had. It also now costs NOTHING for the four
plugins with no derived cache and on serial nodes. Then three
pre-existing queries, all reading as indexed: the re-index selection at
**16.7 s (wikivec) / 18.3 s (wikipedia) / 127.9 s (pubmed) per round** on
caught-up collections — now a windowed cursor walk that ends and stays
ended until a version bump; its batch peers (19.3 s / 15.9 s) — now an
index prefix for the fresh case; and both archive range queries (19.1 s
and 9.5 s per pass) — now hinted onto the browse index. Instrument:
scratchpad `probe_plans.py` (plan + timing, per collection) and
`probe_after.py` — which must call the code's OWN sql builders: the first
verification pass measured hand-copied SQL and reported the archive fix
as ineffective when it was not even testing it. Verified on the live
manifests afterwards: archive range queries 19.1 s → 1.9 s and 9.5 s →
1.1 s on pubmed (now `idx_documents_native_int`, and the residue is one
row fetch per candidate in a 1M-id range — proportional to the shard,
not the corpus); fts fresh peers 0.2–1.2 ms everywhere. The windowed
re-index walk needed a SECOND fix — see amendment 49's tail: the window
is now a forced rowid range instead of a 5.4 s re-walk of the whole range
per window. FINAL verified numbers on the live manifests (0f241db):
re-index window 5.4 s → **0.30 s** (pubmed), 0.13 s (wikivec), 0.09 s
(wikipedia), and gutenberg's walk ENDS after one window (0.0 ms
thereafter) — so a caught-up collection costs one bounded window per
round until the walk completes, then nothing until a version bump,
against 16.7–127.9 s per round forever; fts fresh peers 0.2–7.1 ms; the
archive range queries 1.8 s and 1.1 s on pubmed, proportional to a
shard's 1M-id range (one row fetch per candidate), 0.6 % of a 300 s pass
— a covering index would remove that too and is NOT worth a second
40M-row index. The scheduling watcher (`/data/sched_watch.py`, restarted
after each deploy, log `/data/sched_watch.log`) prints per-collection
rdf/fts/embed rates every two minutes; it dies with the container and is
purely diagnostic.
**Then the archive apply loop itself (amendment 50; operator: "cpu load
is below 50 % and wikivec pre-embed ETA is ~2 d").** A 150 s wikivec pass
measured phase by phase (`probe_genesis_cost.py`, read-only — it replays
an already-applied shard so nothing is written): vectors read 6.3 s
(15,800 vec/s), manifest lookups 0.4 s, **chunk texts 51.7 s** (2.87 ms
per document, one core), writes ~91 s. The chunking now goes through the
parse pool a batch at a time. The writes are what is left: ~1,100
vectors/s = 36 Lance delete+add pairs and 100k `chunk_verification`
inserts per pass — the next thing to measure if the ETA still matters.
Note wikivec's rate in DOCUMENTS looks low against pubmed's only because
its documents carry ~5.8 chunks each: ~670 vectors/s vs ~900.
NOT a code problem, an operator decision: the **GPU encoder sits at 0 %**
while gutenberg holds 59,627 indexed, unembedded documents, because
gutenberg's embed_mode is `lazy`; the same for wikipedia's 19.4M
(embed_mode lazy, its dense side is meant to come from wikivec). The
gutenberg pack plan needs those vectors before the hand-over.

## Designed, specified, not built

1. **Peer and collection discovery** — [`p2p-discovery.md`](p2p-discovery.md).
   A signed per-node catalogue whose first field is `space_id`; peers via a
   pinned list, then the coordinator, then the DHT. The load-bearing point:
   a DHT finds peers for an *infohash* and can say nothing about whether that
   infohash holds a corpus in your embedding space under a licence you may
   redistribute.
2. **Applying a delta.** Deltas can be computed and published but not
   consumed — `check_applicable` refuses a bad one and then nothing happens.
   Tombstones must delete documents and their vectors; added/changed must be
   marked for re-embed; the snapshot pin must advance. **The format is
   currently write-only**, and this is the gap that stops the whole
   reference-plus-chain model closing.
3. **Query-triggered freshness** — a query that lands on stale content
   enqueues a revision check and a re-embed, answering from what is held and
   improving on the next query. Zipf makes this dramatically cheaper than
   re-embedding a whole delta. Must never block the query, must respect
   `worker_state`, must be rate-limited, and must show which revision
   answered.
4. **Sharing the demand signal.** "N nodes found this stale" is a few bytes
   and lets one node do the work for everyone; the coordinator already counts
   distinct reporters for exactly this.
5. **LOCKSS-style possession audit** — [`lockss-p2p-evaluation.md`](lockss-p2p-evaluation.md).
   Poll the *content* plane, never the vector plane.
5m. **The pack — one distribution format for corpus, vectors, index and
   proofs (operator brief 2026-08-26; PROPOSAL in
   [`pack-format.md`](pack-format.md), nothing built).** Parsed-record
   Parquet shards (the PubMed pack generalised to every plugin — peers
   never see origin files or parsers), vectors / index / deltas as
   separate members of the torrent collection, a signed manifest with
   collection id, snapshot chain, the space's encoder + reference
   version + calibration tolerance, update cadence and deadline; deltas
   at record (CDC: paragraph) granularity feed the coordinator's leases
   (one shard = one lease); a search-time freshness check against the
   origin (no re-embedding); anti-entropy digests + TTL-bounded events
   for cross-advertising, DHT-published catalogues beyond direct peers;
   zstd now with a manifest codec slot for OpenZL later. Improvements it
   brings: Wikipedia pre-embedded f32→f16 (−295 GB), light peers with
   zero compute, chunk-level provenance through merges. Build order in
   §22; open questions in §21 — decide before building. **Part II**
   (same day, second brief): the initiator role, a DECLARATIVE
   validation plan run by open platform code (never a shipped script —
   that is remote code execution), signed attestations with local trust
   levels (unverified → checked → verified by coverage; deterministic
   checks flag on one reproduction, statistical ones on two — no votes),
   one chunker per collection with DERIVED collections for a new space
   (records + index reused by info hash), and inspectable formats: JSONL
   in seekable zstd for records, safetensors for vectors. **Formats
   MEASURED (2026-08-26, §8)** on 30k real PubMed records and 100k real
   MedCPT vectors: JSONL in 1,000-record zstd frames is 4 % smaller than
   Parquet at the same level and costs the same 1.0 ms per record, so
   the text-editor rule is free for records and stays; a trained zstd
   dictionary buys nothing at that frame size (the 20–30 % guess in the
   first draft was wrong); OpenZL 0.2 (PyPI, manylinux only) equals
   zstd-9 on JSONL text and gains 15 % over raw f16 on vectors (zstd:
   8 %), int8 is −75 % but lossy (cosine 0.9989 to f32: it would pass the
   0.95 same-function gate, but sits below the ≥ 0.999 the same model
   reproduces across hardware — a visible degradation, so a separate
   light space at most, never the reference). Decision: zstd-9 frames for records, vectors as raw f16
   safetensors (readable header, no codec — the text-editor rule is
   relaxed for the array by design: a printed float is neither practical
   nor useful, re-embedding is the only real check), OpenZL deferred
   behind the manifest's `codec` slot until its format stabilises.
   **RESTRUCTURED (2026-08-26, operator: "start from the general
   picture and scope, then go into detail")**: Part 0 (scope, a sketch
   of the components of a collection, roles, lifecycle, the rules in
   one place), then Parts I–IV (artefacts / operation / governance /
   status and plan) and an appendix of measurements. Three additions
   from the vector-index discussion: a SLICING scheme (§2: slice =
   plugin-declared path of stable record fields — PubMed year bins,
   Wikipedia lang + page-id range; shards closed once published,
   append-only per slice, tombstones; member = slice × kind, so a peer
   fetches "PubMed before 1980" alone); the VECTOR INDEX as a pack
   member (§5: k-means centres trained once on a corpus-wide sample and
   published; every embedder labels its vectors with the nearest centre
   — a 2-byte column — so the index reassembles by concatenation;
   optional bucket-ordered vectors for light peers and topical
   downloads; versions ADDITIVE within an epoch, so a per-shard
   `index_min` is computed from labels, not declared, and a peer never
   fetches a version it does not need — a label is correct at every
   version, older is slower never wrong; a from-scratch retrain is an
   announced epoch); and the HASH RULES (§6: `content_hash` of the
   canonical text is the truth, origin version numbers are hints,
   silent changes recorded in the delta). Query-side MEASURED
   (appendix A): both live tables are f16 with NO vector index — flat
   scans cost 0.75 s on pubmed (2.2M rows) and 13 s on wikivec (15.8M);
   on 100k real vectors f16 loses nothing (every top-10 "miss" is within
   0.001 cosine of the 10th), int8 swaps ~2 % at the boundary,
   IVF-FLAT / HNSW-SQ answer in 5–6 ms at 98–99 % within 0.001, IVF-PQ
   without re-ranking drops to 66 % on MedCPT's tight cone. The local
   vector index, not compression, is the performance item (build order
   step 3; known gap below). **Layers (2026-08-26; the operator amended
   invariant 1 — amendment 41):** a collection may declare an ordered
   list of chunker LAYERS with every document in exactly one layer by a
   declared rule — the MedCPT case: the archive's chunking for what it
   covers, ours for what it does not — one encoder, space id over encoder
   + layers; a unifying v2 is a derived collection that inherits the
   surviving layer's vectors. pack-format.md §17.1.
   **BUILT, first cut (2026-08-26, unattended; amendment 43):**
   `app/pack/` — `records.py` (JSONL in independent zstd frames + a
   trailing skippable-frame index; pyarrow's bundled zstd, no new
   dependency; `zstd -d shard.jsonl.zst` prints the records),
   `slicing.py` (PubMed year bins, Wikipedia/wikivec language +
   page-id bins, Gutenberg language; cheap slice hints so the planning
   pass never parses what the locator already says), `build.py` (two
   passes over the collection's MANIFEST — plan, then pack per slice in
   id order; shards closed once written and recorded AFTER the rename;
   a stop discards at most one shard's buffer; re-runs skip recorded
   shards), `manifest.py` (Ed25519-signed with the node key; the terms
   text copied byte-exact), `verify.py` (roots / records / chunks /
   license / signature). The `packed` plugin serves a pack as a corpus
   keeping the record's uid, the origin hash as raw_hash and the origin
   plugin's chunker — TESTED: a collection over the pack has the same
   uids, chunk ids/text and possession hashes as one over the origin,
   for gutenberg, pubmed (tombstones excluded) and a synthesized
   wikipedia dump. CLI: `pack build|verify|status|cat|dump`,
   `tools/pack_convert.py` (discovers a raw corpus into a build-local
   manifest, then builds), `torrent publish --per-slice`, `index vector
   build|status`. LAYERS: `app/layers.py` + `[[layers]]` TOML — every
   stage now cuts through `layers.chunks_for_document`; one layer keeps
   every pinned space id byte-identical (test). VECTOR INDEX:
   `app/vector_index.py` — Lance IVF-PQ (IVF-FLAT by knob), built by
   the worker's pipeline thread once a table has VECTOR_INDEX_MIN_ROWS
   (100k), rows folded in by `optimize` past VECTOR_INDEX_REFRESH_ROWS,
   the search probing (nprobes 30, refine 30) whenever an index exists.
   First live minute: the build held the vector write lock — a RESERVED
   lock on the collection's MANIFEST — and every event, count and
   discovery commit of that collection blocked for the build's duration;
   fixed the same minute: no manifest lock for an index over existing
   fragments (Lance's own commit conflict detection covers a concurrent
   write; a delete-then-insert that loses is a deferred step, never lost
   data). Wikipedia's fetch caches the last decompressed
   stream (one slot) — ~100x fewer decompressions for every stage. NAS:
   datasets `<archive pool>/louise-packs/{gutenberg,pubmed,wikipedia}` (recordsize
   1M, lz4) mounted at `/packs` (PACK_MOUNTS_DIR; the create dialog
   lists them as "(pack)"), conversions started with `pack build` from
   the existing collections' manifests — detached inside the worker
   container, logs `/data/pack-<name>.log`, relaunch after any app
   restart with `docker exec -d ix-louise-rag-worker-1 sh -c "python -u
   -m app.cli pack build <name> --out /packs/<name> >> /data/pack-<name>.log
   2>&1"` (resumable; state in `<pack>/.build/build.db`). A finisher
   (`/data/pack_finish.py <name>`, same container) waits for the
   manifest, runs `pack verify --sample 0.02`, and creates the
   `<name>-pack` collection (plugin `packed`, embed off, acquisition off,
   the origin's [embed] recipe and license ack, paths under
   `/data/<name>-pack/`) — delete that TOML to drop the collection. NOT
   built: deltas/snapshots, the call for contribution over packs, gossip,
   freshness, published centres/labels/bucket-ordered vectors,
   attestations, `records-vs-origin`, pack acquisition from a peer
   through a `packed` collection (no acquisition spec yet — a torrent
   fills a folder, the collection is hand-populated over it).
   **Procedure change (2026-08-26, operator; amendment 44): packs are
   CLOSED snapshots built from a COMPLETED origin.** `gutenberg-pack`
   is paused (`gutenberg-pack.toml.paused` on the NAS) until the
   gutenberg origin finishes fetching/parsing/indexing to 2026-06-30;
   the premature pubmed/wikipedia conversions and all finishers were
   stopped (their partial packs under `/packs/*` are throwaway — the
   real builds use `--until`). Built for it: `pack build --until
   YYYY-MM-DD` (gutenberg by `dcterms:issued`, now parsed into
   `CanonicalDoc.date`; pubmed by DateRevised/DateCompleted, now in
   `extra.revised`; wikipedia by dump date; manifest `closed`,
   `origin.cutoff`; a resume under another cutoff is refused) and `pack
   adopt <origin> <packed> [--fts] --yes` (copies the Lance table, the
   embed marks and chunk provenance — and the tantivy index — into the
   twin; same pinned space required; the twin's table must be empty;
   run with the worker stopped). THE HAND-OVER, when gutenberg is
   complete: `pack build gutenberg --out /packs/gutenberg-2026-06
   --until 2026-06-30` → create `gutenberg-pack` over it (the paused
   TOML, corpus_dir updated) and let it discover → stop the worker →
   `pack adopt gutenberg gutenberg-pack --fts --yes` → retire
   `gutenberg.toml` → start. Updates are a derived, conflict-free
   function of (previous manifest, origin window, cutoff) —
   pack-format.md §6.1; the updater is the next build item.
5l. **Torrent COLLECTION — one magnet for a whole published corpus
   (operator request 2026-08-26; BUILT the same day; amendment 40).**
   `torrent publish <c> --per-language` now also writes an INDEX torrent
   (`<c>.collection.torrent`: a tiny directory with `collection.json` —
   members with slice name, the slice's include patterns, magnet, info
   hash, size — plus a copy of every member `.torrent`) and prints its
   magnet: the only thing a peer needs. On the peer, `effective_spec`
   resolves the index once (`app/acquire/torrent_collection.py`; the
   content lands under `<manifest dir>/peer-index/`, cached in the
   control table keyed on the source), then expands the source into the
   members the PROFILE selects, in fetch order (a combined profile's
   order; "all" smallest first). Growing the profile adds members with
   no TOML edit; the grow dialog offers exactly the slices the publisher
   offers (`web._offered_by_torrent_collection`), and "growth options
   are consequential" — the origin's static list is not shown for a
   peer-sourced collection. The Network page's seeding table groups
   torrents per collection (expand ▸ to see the single torrents) with a
   **copy collection magnet** button; rows carry kind/slice. A re-publish
   is a new index and a new magnet (a static index; BEP 46 mutable
   pointers are the follow-up for peers beyond one LAN).
   `tests/acquire/test_torrent_collection.py` (index round-trip, profile
   → members, an end-to-end resolve over a local swarm, seeding rows).
   **Live on the NAS 2026-08-26:** `torrent publish wikivec
   --collection-only --peer-hint <node>:6881` built the index
   from the eleven recorded members without re-hashing (and upgraded
   those pre-`kind` records in place); the seeding table shows one
   `wikivec-collection` row (2.4 MB, seeding, hint attached — "copy
   collection magnet") above the eleven members labelled by slice. The
   magnet is also in `/config/wikivec-collection-magnet.txt`. A peer
   creates its wikivec collection with that one magnet and profile
   `tr` (8 GB), then grows to `tr,it` from the dialog.
   **First peer, Louise2, 2026-08-26 (profile `demo`):** the index
   resolved (11 members, hint), `demo` selected only `tr` (21 files,
   8.97 GB), Turkish parquet files landed at LAN speed (2.5 GB in the
   first two minutes). Three things it exposed, two fixed the same hour:
   (1) the create dialog's default `[embed]` is the NODE's encoder — for
   a pre-embedded corpus the recipe must be the ARCHIVE's function, or
   the genesis proof rejects every shard; Louise2's wikivec now points
   at the NAS's BGE-M3 endpoint (`<node>:30442`, byte-equal
   recipe ⇒ the same space id `ed34b4b0…` as the NAS's wikivec), and a
   wrong pin was dropped with `space repin`. To do: the create dialog
   should offer the archive's recipe for a plugin with a declared
   archive. (2) The torrent transport never credited its bytes to the
   budget (invariant 5's accounting half): 2.5 GB arrived while Traffic
   read 0 — fixed (`_credit_download`, per handle, tested). (3) A new
   collection is unnoticed for the length of a GPU embed round: the
   main loop sits in `_embed_parallel` until its in-flight documents
   finish (six 500-chunk books at 3.5 chunks/s ≈ 15 min on Louise2)
   and `_sync_collections` runs between rounds — recorded under *Known
   gaps*; a restart of the worker was the workaround.
5k. **PubMed container access — measured 2026-08-25, pack BUILT the same
   night (`app/plugins/pubmed/pack.py`).** One row per direct child of
   the container root, in document order (the record index the locators
   carry): the child's exact serialised XML (tail included — emitted only
   once the next sibling has started, so `raw_hash` is byte-identical to
   the DOM path; discovery compares it to detect changed records) plus
   pmid/title/abstract/journal/date/authors for the reader. Row groups
   of 1,000, zstd-3; record i sits in row group i // 1000. Built lazily
   on first sight (discovery) or first fetch, one builder per container,
   atomic tmp+rename, rebuilt when the container's (size, mtime) or
   `PACK_VERSION` changes; freshness memoised per container (the
   footer read cost 3 ms per record, a stat costs microseconds). Lives in
   a hidden `.pack/` beside the containers (discovery lists files only;
   torrent publishing and `corpus_digest` skip hidden entries — cache,
   never p2p material) or in `PUBMED_PACK_DIR` for faster storage (5i).
   Measured on `pubmed26n0600` (30,000 records): build ≈ one DOM parse
   (10.8 s on the Windows box where the DOM parse takes 10.4 s; ~6 s on
   the NAS), **44.9 MB** with the XML kept (the gz is 35.5 MB — ~60 GB
   for all of PubMed), first record 31–48 ms cold, **0.6 ms** warm,
   discovery's three columns 30 ms, reader fields 8 ms, 0 fidelity
   mismatches on a spread sample; `tests/test_plugins_pubmed_pack.py`
   pins byte-identity on the fixture for plain and gzipped containers.
   The XML column (not just the six fields) is what makes it a cache
   with full fidelity — MeSH, references and grants stay available at
   the same cost when entity work wants them. **Live on the NAS
   (deployed 6926a81, 2026-08-26 00:39):** builds take 14–33 s per
   container under the full pipeline load (12–29 MiB each; older
   containers are smaller), paid once — the genesis pass that first
   touches five new containers spends ~125 s of its 300 s slice building
   them (shard 3: 29,706 documents imported in 309 s including the
   builds), rdf/fts continue at 1.4–15 s per 1,000-document transaction
   with the same one-off build on their container; 1,334 containers ≈
   9 CPU-hours spread over the pipeline, after which every record costs
   ~0.6 ms. Same pass, same lesson as 5g: 70,941 of ~100k eligible
   shard-3 documents were chunk-shape mismatches — the chunker change is
   what unlocks the archive.
   *Original measurements and proposal:*
   The corpus is 1,334 gzipped XML containers, 51 GB; one container
   (`pubmed26n0600`) is 35.5 MB gz → 283 MB XML (8×), 30,000 records, so
   the whole archive is ~380 GB uncompressed — never materialise it.
   Per container: gunzip 0.8 s; our `ET.parse` DOM 8.5 s idle (13–20 s
   live, under the pipeline's load) and hundreds of MB of tree, held in
   a ONE-slot cache; `ET.iterparse` streaming with `clear()` 4.1 s;
   extracting (pmid, title, abstract, authors, journal, year) and
   writing a Parquet pack (zstd-9, 1,000-row groups) 4.8 s → **12.2 MB**
   (34.5 MB of raw text; 25,331 of 30,000 records carry an abstract);
   one record by PMID from the pack, via row-group statistics: **16 ms**
   — against 8–20 s today (the container must be parsed whole). A
   gzip stream is not seekable, so "one record" always costs the
   gunzip of everything before it (0.4 s average) plus the parse.
   PROPOSAL — `plugins/pubmed` keeps the gz XML as the archive (what is
   fetched, checksummed and seeded) and builds, once per container, a
   DERIVED pack beside it (`<name>.pack.parquet`, PMID-sorted, rebuilt
   when the gz's checksum changes; ~16 GB for all of PubMed): rdf/fts/
   embed/genesis read records from the pack (16 ms, no cache thrash),
   the reader panel (5j) opens an article instantly, and year/journal
   become available for the bibliographic line without a manifest
   column. Deterministic from the gz, so invariant 3 is untouched; the
   pack is a cache, never exchanged. Build cost ≈ 1,334 × 5 s ≈ 2 h of
   one core, amortised into the first pass over each container.
5j. **Ask tab: translated results and a document reader (operator
   request 2026-08-25; BUILT 2026-08-26).** As specified below, with
   these concrete choices: `POST /ask/translate` (one `llm.translate_texts`
   call per page — numbered JSON in and out, a dropped passage comes back
   as its original; cache keyed on (chunk id, language), bounded; `failed`
   in the reply makes the page say "translation unavailable" instead of
   pretending), the answer language rides `ChatBody.answer_lang` into
   `build_answer_messages` (the model is told to answer in that ISO code —
   never post-translated, so [n] stays aligned), the drop-down offers
   the pre-embedded corpus's eleven languages plus the common European
   ones (`web.RESULT_LANGUAGES`). Reader: `app/reader.py` +
   `GET /read/<collection>/<book_id>?hl=<chunk ids>[&fragment=1]` —
   text from `fts.book_chunks` (a term query on the `book_id` fast field;
   the lexical index stores every chunk's body) or, for an embedded-but-
   not-indexed document, its Lance rows; never the corpus file; the
   in-page panel (`<aside id="reader">`, Esc closes) is the same
   template as the standalone page; hits carry `data-collection/book/
   cid`; [n] in the answer becomes a link into the reader; the reference
   line is authors — title — (PubMed: journal, date from the pack) —
   (lang) — source:id, plus the origin link per plugin. Help updated.
   Not done: PubMed's `date` column (the pack answers the reader instead).
   Verified on both nodes: Louise2's reader opened gutenberg 10218 with
   the hit highlighted (60 chunks, origin link) and translated a snippet
   ("ciao"); the NAS's PubMed hit 10329736 opened with the reference
   "… — J Biol Chem — 1999-May — (en) — pubmed:10329736" and an Italian
   snippet from the local model — but with NO text: after the re-pin
   every PubMed row has `fts_done=0` while the old entries stay
   searchable until rewritten, and the reader had gated its index read
   on the mark. Fixed the same hour: the reader asks the index
   regardless (`test_reader_serves_a_document_mid_reindex…`).
   *Original spec:*
   (1) *Results in another language, interface untouched.* A "show
   results in" drop-down beside the mode selector (default *original*;
   options = the languages indexed in scope plus the operator's default).
   Only CONTENT is translated — snippets, the drawer text, the RAG
   answer **and the title** — never authors or ids, and never the page
   chrome.

   **AMENDED 2026-09-12: the title IS translated.** The original rule
   said titles never change, for "a bibliographic reference must read as
   published". The operator overruled it, and the argument is decisive:

   > "the title must be translated when I ask to read the results in a
   > language I know, then I can still convert the results back to their
   > original language to copy the original title for reference. how am I
   > supposed to understand a title in Arabic or Hindi?"

   The old rule assumed the displayed title was the only copy. It is
   not: the switch is REVERSIBLE, so the published title is always one
   selection back, and the hit also keeps it as a hover tooltip. Against
   that, an untranslated title defeats the feature's whole purpose —
   "results in a language I know" that opens with a line the reader
   cannot read is not a translation, and the corpus genuinely holds
   Arabic, Hindi, Chinese, Japanese, Russian and Greek titles.

   AUTHORS AND IDS STILL DO NOT CHANGE, and for a reason that survives
   the amendment: a personal name is not translated in any language, and
   an id is not language at all. Transliteration is a different
   operation and is not offered. Snippets: one LLM call
   per result page (`POST /ask/translate {lang, items}` → JSON, batched
   like `llm.translate_query`), a small "translated from de" badge, the
   original kept in the provenance drawer, an in-memory LRU keyed on
   (chunk_id, lang) so toggling is free; failure degrades to the original
   with a note. The answer is not post-translated: `build_answer_messages`
   gets the target language and the model answers in it directly
   (cheaper, and citations [n] stay aligned).
   (2) *Open the document from a hit.* The operator asked for a popup;
   PROPOSAL (preferred): an in-page reader panel (slide-over) opened by
   clicking the snippet or the reference — the whole document, the hit
   chunk scrolled into view and highlighted, prev/next hit within the
   document, a bibliographic header and a link to the origin (PubMed
   `pubmed.ncbi.nlm.nih.gov/<pmid>/`, Gutenberg `gutenberg.org/ebooks/<id>`,
   Wikipedia's article URL); the search stays visible behind it, no
   popup blocker, and the same route (`/read/<collection>/<book_id>#c<seq>`)
   is a standalone page for middle-click / new tab. The text comes from
   the INDEX STORES — Lance chunk rows for embedded documents, the FTS
   store otherwise — assembled by book_id and seq, never from the corpus
   file on a click (a PubMed container costs 13–20 s to open; Gutenberg's
   plain files are the only acceptable fallback). Citations [n] in the
   answer become links into the same reader. Reference line per source
   plugin (authors, title, id + link); PubMed's year/journal are not
   persisted today (`documents` has no date column) — an additive `date`
   column is the one schema change this would want, to be asked for.
5i. **Storage tiers: main archive on HDD, working set on SSD (operator
   request 2026-08-25; evaluated, to implement 2026-08-26).** Today
   EVERYTHING sits on `ssd4` (NVMe, 1.81 TB, 569 GB free): corpora 923 GB
   (wikivec 590, gutenberg 256, pubmed 51, wikipedia 26) plus `/data`
   250 GB (Lance 23 + wikivec vectors 127, tantivy 34, pubmed 36). The
   HDD pool `tank` (4 × 6 TB Toshiba in raidz1, 8.9 TB free; `ssd4` is a
   2 TB Kingston NVMe) reads 304 MB/s sequential (direct), 121 MB/s in
   64 KiB direct random reads (~1,900 reads/s — small-file corpora are
   fine too), and 6.3 GB/s when the block is in ZFS's RAM cache (ARC:
   25 GB of 61 GB RAM, 99.8 % hit ratio). Measured stage costs are CPU/GPU, not
   disk: a 30 MB pubmed container takes 13–20 s to gunzip+parse and 0.1 s
   to read from HDD; wikipedia rdf 33–68 s per 1000 documents; a 438 MB
   wikivec shard ~10 s to decode vs 1.5 s to read from HDD (and the
   revisited shard comes back from ARC). Verdict: **no app-level SSD cache
   for reading** — ZFS ARC already is one, warm and free. Plan:
   (1) new datasets `tank/louise-corpora/{wikivec,pubmed,wikipedia}`
   (recordsize 1M, lz4), moved by rsync (~1 h at the measured rate), and
   the app's host mounts repointed — the in-container paths
   (`/corpora/<name>`) stay identical, so every locator stays valid and
   no discovery repair runs; (2) **gutenberg stays on SSD for now**: 256 GB
   of 2.8 M small files, and discovery stats every one of them each
   rescan — measure a scan from HDD before moving it; (3) `/data` (SQLite
   WAL, tantivy, Lance, the genesis cache's 3 GB mmap'd shards) stays on
   SSD, invariant 6; (4) the one place SSD staging DOES pay is
   ACQUISITION: torrent pieces land as random writes, which raidz1
   handles badly — download into `/data/staging` and move each file to
   its corpus directory on completion (the `.part` discipline already
   isolates the incomplete file; the engine's preflight bounds the
   staging area); (5) no L2ARC — it excludes streaming reads by default
   and the ARC hit ratio leaves nothing for it.
   **Built 2026-08-26 (operator: "datasets, not folders").** Datasets
   `tank/louise-corpora/{wikivec,pubmed,wikipedia}` (recordsize 1M, lz4,
   atime off, root:root 770 like the originals); bulk `rsync -aH` while
   the app kept running (wikipedia 26 GB in 64 s, pubmed 51 GB in
   2.6 min, wikivec 590 GB at 200–570 MB/s), then a stop → delta sync →
   mount switch → start window (`/root/switch_window.sh` +
   `/root/switch_mounts.py` on the NAS, which edits the custom app's
   compose config through `midclt call app.update` — only the three host
   paths change, the in-container paths and therefore every locator stay
   identical). Two knobs went in with the switch: `PUBMED_PACK_DIR=
   /data/pubmed-pack` (the packs on the NVMe; the ones already built were
   copied over) and `ACQUIRE_STAGING_DIR=/data/staging` (ff84884: the
   https/ftp/hf transports stream into staging on the NVMe, the verified
   file is moved into its corpus as one sequential copy; an interrupted
   `.part` resumes from staging; a file the area cannot hold lands in
   place; torrents unchanged until 5h). **Switched 2026-08-25 10:22
   CEST:** delta sync moved 0 bytes, the app came back on the datasets,
   discovery on all three reported 0 changed documents, PubMed's
   re-index went on from the 193 packs copied to `/data/pubmed-pack`
   (0 rebuilds), wikivec's archive reads its shards from tank. Lesson
   that cost a failed `up`: `midclt call app.stop/app.start/app.update`
   return a JOB and go on in the background — the window script's
   `start` raced its own `update` and left two *Created* containers with
   name conflicts; `midclt call -j …` waits, and is what the deploy
   recipe uses now. The ssd4 originals were datasets too
   (`ssd4/data/<name>`, which is why a plain `mv` said "resource busy");
   they were `zfs rename`d to `ssd4/data/<name>.moved-to-tank` (675 GB,
   reversible) — `zfs destroy` is the operator's call after a few days on
   tank; the `.pack/` copied into `tank/louise-corpora/pubmed` (5 GB) is
   unused under `PUBMED_PACK_DIR` and can go with them. Gutenberg
   (`ssd4/data/gutenberg`, 302 GB) stays on the NVMe.
5h. **wikivec per-language torrents (operator approved 2026-08-25; BUILT
   2026-08-26).** `torrent publish <collection> --per-language
   [--languages en,tr]` hashes one torrent per `data/<lang>` directory
   (en 192 GB, de 83, fr 75, ru 57, es 54, it 42, ja 33, pt 25, zh 14,
   fa 11, tr 8.4), ALL rooted at `data` with an include filter
   (`publish_corpus(include_patterns=…)` — libtorrent's add_files asks
   about directories too and a False prunes the subtree, so directories
   pass and files are judged), so a peer's files land in the plugin's
   own layout and the torrents combine. The seeding record is a list
   (`seeding.record_published` merges by info_hash; older single-dict
   records still read), the worker seeds every entry. On the peer,
   `peer_source` is one magnet or several — a TOML array, one per line,
   or comma-separated (`engine.peer_sources`); `effective_spec` makes
   one endpoint per torrent, the listing is their union, and
   `run_acquisition` fetches the torrents ONE AFTER THE OTHER in the
   listed order — the first language is complete, and the pipeline busy
   on it, while the rest arrives (amendment 37). The create dialog
   validates every entry. `tests/acquire/test_per_language_torrents.py`.
   **Published on the NAS 2026-08-26:** eleven torrents, 589 GiB, all
   seeding (the Network page's table shows them); hashing ran at
   ~500 MB/s from `tank` with the ARC still warm from the copy (de 82 GB
   in ~3 min, en 191 GB in ~5 min). Lesson: a `nohup docker exec … &`
   started over ssh dies with the session (the first pass lost its log
   and stopped after two languages); `docker exec -d` runs detached
   inside the container — use it for anything that outlives the ssh
   call. The worker's control manifest (seeding table, worker state) is
   the DEFAULT collection's (`/data/genome-dm6/manifest.db` on the NAS),
   not `/data/manifest.db` — read it through `db.connect()`.
5g. **Pre-embedded archives: fetched like a corpus, applied as indexing
   goes, one knob (amendment 38; operator policy, 2026-08-24).** Live
   pubmed: 8.25M/40M indexed, 0/37 MedCPT shards, 44 embedded — the
   post-settle gate withheld the archive for months. Built: the archive
   is a second acquisition stream (`app/bundles/genesis_fetch.py`, budget
   class `archive` below `backfill`, bounded on-disk cache with
   preflight); fetched shards apply at once for the indexed documents,
   are revisited with per-shard geometric backoff, and go FINAL (files
   deleted) when their id range has no document left waiting for fts;
   MedCPT declares its coverage (`shard_of_id`: exact PMID ranges); the
   single Embed-mode knob governs the local encoder only, and `all`
   selects the documents the archive cannot cover through a rowid
   cursor (no per-round re-walk of the covered prefix, no ORDER BY).
   Card: main `embedded (dense)` = total coverage; sub-box = `fetched
   (vectors)` + `pre-embedded (dense)`, no knob. Local archives
   (wikivec) unchanged.
5f. **Index sharing for LIGHT peers — decide by measurement (2026-08-24).**
   The lexical index stays recomputed by default (≈1 ms/doc, deterministic,
   zero trust surface; a received tantivy segment can only be fully
   verified by rebuilding it, and it is coupled to FTS_VERSION /
   ENTITY_VERSION / chunker / tokenizer). But a light peer "does only the
   work it cannot avoid", and pubmed's ~40 CPU-hours IS avoidable if the
   index travels. Option to build if the numbers favor it: index segments
   per lease (they merge like tantivy segments do) shipped beside the
   bundle, guarded like vectors are — an INDEX-SPACE PIN (the four versions
   above; refuse on mismatch) and SAMPLED verification (re-index a few
   received documents locally, compare postings). Decide once pubmed's
   index is complete: its size on disk vs the measured 40 h rebuild.
5e. **Two peer classes, and benchmark-based eligibility (operator design,
   2026-08-24).** *Light* peers do only the processing that cannot be
   shared (discovery, FTS — local by design) and still share corpus bytes
   and processed data (seed torrents, serve bundles). *Full* peers also
   take shared-workload leases (embedding batches) from the coordinator.
   For now a full peer gets a FIXED, generous lease TTL (the existing
   24 h `LEASE_TTL_SECONDS`); an expired lease is reissued to whoever
   asks next. TO BUILD: a benchmark a peer runs to become ELIGIBLE for
   shared workload (encoder consistency vs the space's baseline + a
   throughput measurement) that also sets a proper per-peer lease TTL
   instead of the fixed one; until then eligibility is the operator's
   choice (`contribute` mode on/off) and the TTL is the constant. See
   also the corpus chunking proposal recorded with amendment 37.
5d. **License gate on USE, not just acquisition (amendment 36; operator
   report, 2026-08-24).** A yellow-class shard copied from a peer into a
   hand-populated collection was indexed and imported with no ack — the
   gate only fired on origin fetches. To build: worker round loop +
   pipeline threads skip a yellow+ collection with no valid
   `license_ack` (discovery, genesis, bundles, onward exchange all
   excluded); Library card shows "license acknowledgement required" with
   the existing acknowledge flow; boot warning names gated collections.
   Until built, this is enforced by review only.
5c. ~~**Acquisition source choice at collection create.**~~ **Built
   2026-08-24:** the create panel's Acquisition drop-down offers *off* /
   *from the source* / *from peers* (magnet or .torrent). `peer_source`
   in the TOML redirects the whole acquisition to the torrent transport
   (`engine.effective_spec`); the worker seeds every torrent recorded by
   `torrent publish` and credits uploads to the UP bucket
   (`app/acquire/seeding.py`). What remains unbuilt is DISCOVERY (item
   1): today the magnet carries a direct-peer hint (`x.pe=host:port`)
   pasted by the operator.
5b. **The enumeration travels with the corpus.** A peer that downloads a
   corpus snapshot still re-derives its document list by scanning (hours
   on a large corpus, even resumable). The snapshot manifest already names
   every uid with its raw hash (§5, parent-chained deltas); extended with
   each document's locator-within-the-snapshot, mtime and size it becomes a
   *signed discovery input*: import it, then verify by stat and hash
   spot-check instead of enumerating. This is the part of "indexing" that
   SHOULD be shared. The FTS index itself should not: it is cheap to
   compute (≈1 ms/doc warm, against hours to download), expensive to verify
   (a binary blob; poisoning is invisible), coupled to local config
   (`FTS_VERSION`, analyzers, entity version, chunker) and to the tantivy
   version — and invariant 6 says SQLite state never travels. The rdf
   output (catalog metadata) is small and derivable and could ride along
   for "browse before you index"; a possible later addition to the same
   manifest.
6. **DNA search axes** — [`dna-search-axes.md`](dna-search-axes.md).
   **Parked pending the Fable model.** DNABERT-2's space is anisotropic —
   unrelated sequences score 0.98 cosine — because MLM never asks them apart.
   DNABERT-S is *not* the fix: it contrasts on species-of-origin, which
   collapses within-genome distinctions and pushes apart exactly the
   cross-species pairs §9 wants together. Homology and composition need no
   model at all (k-mer sketches); only function does. The load-bearing point:
   **invariant 4 has no category for two ranked lists that must NOT be
   fused** — a homology ranking and a functional ranking answer different
   questions, and RRF-ing them yields neither. Measure before amending
   anything; there is currently no baseline to judge any model against.

7. **Italian law plugin (Gazzetta Ufficiale / Normattiva)** —
   [`gazzetta-plugin.md`](gazzetta-plugin.md). **Research done, nothing
   built, three operator decisions open.** Normattiva's data went CC BY 4.0
   on 2026-01-01 with an OpenData portal and API (Akoma Ntoso, XML-NIR,
   JSON, ELI) — that, not the IPZS site, is the origin worth building on:
   structured, identified, versioned, openly licensed. Three findings that
   shape any implementation: `native_id` must be integer-parseable
   (`layers.py:120`, `worker.py:260`, the Lance `book_id` column), so a URN
   cannot be one and a synthetic sortable key is needed; the natural chunk
   is the **articolo**, which neither the fixed-width nor the CDC chunker
   cuts on, so a new chunker enters a new `space_id`; and multivigenza is a
   ReferenceSource question (facts-by-key, invariant 4), never N versions of
   one article competing in RRF. The corpus is rendered into a **canonical
   text form** first — structure-preserving, editor-readable, byte-stable,
   verified by a skeleton round-trip test — derived and local exactly as
   pubmed's pack is, with its version folded into the chunker's so it never
   touches the global `CLEANING_VERSION` (the global-knob lesson, second
   coat). Encoder: chosen on measured Italian legal retrieval (JuriFindIT),
   not on what this node runs — KaLM-Embedding-Gemma3-12B (MMTEB #1) and
   Qwen3-Embedding-8B are the open-weight candidates, both MRL, so the
   pinned dimension is a separate decision from the model; closed API models
   are excluded as space owners because a peer must reproduce vectors at
   cos ≥ 0.999. Italian-Legal-BERT is an MLM and would walk straight into
   item 6's anisotropy trap. Operator decisions 2026-08-26: scope is
   **Normattiva only**; the reranker is **jina-reranker-v3** (listwise, one
   pass) under a stated **p95 ≤ 2 s** budget, because latency is part of
   answer quality and a reranker switched off under load contributes zero;
   and the pinned DIMENSION is not to be truncated to buy latency — MRL
   truncation destroys information permanently in a value pinned into
   `space_id`, where a vector index buys the same latency losslessly, which
   makes the index a prerequisite rather than a later fix (the one costed
   consequence). Who chooses the embedder at all, and what happens when the
   choice was wrong, is generalised in
   [`embedder-choice-and-supersession.md`](embedder-choice-and-supersession.md).
   **ELI** (European Legislation Identifier) is adopted as the general
   template for EU-country legal plugins —
   [`eli-legal-plugins.md`](eli-legal-plugins.md) — with a clash check
   against the pack format that found no incompatibility, one gap (the
   manifest has no `jurisdiction`), one structural friction (`native_id`
   must be an integer while ELI is URI-addressed; `uid`/`keys` absorb it),
   one strong fit (**`snapshot` IS an ELI point-in-time coordinate**, which
   makes a consolidated legal corpus reproducible — two peers fetching a
   week apart get identical bytes because the Expression is named in the
   URL, not implied by the calendar), and one defect it corrected: **repeal
   is metadata, not a tombstone** — `eli:repeals` relates Works and
   `in_force` is an Expression property, so a repealed act stays indexed
   and labelled rather than being excluded like a PubMed DeleteCitation,
   which would have discarded law that still governs facts predating its
   repeal.
   Reading the Italian ELI spec in full also **overturned** the readable
   `native_id` scheme: `/eli/stato/{type}/{date}/{number}` is an alias that
   the spec's own example resolves to TWO acts, the unique permalink is
   `/eli/id/{date}/{codiceRedazionale}`, and `codiceRedazionale` is
   alphanumeric — so `native_id` becomes a date-prefixed hash that discovery
   must collision-check. Whether a collection should declare a **content
   class** at all is answered in
   [`collection-classes.md`](collection-classes.md): not a bundled type but
   two axes — `modality` (absorbing the `rerank_class` idea: one declaration
   governing encoder AND reranker eligibility, refused at create and boot,
   which is the checkable form of the incident behind
   `[embed_defaults.<plugin>]`) and `erasure` (append-only / retractable /
   erasable). Its sharpest result: **erasure and exchange are incompatible**
   — a seeded shard cannot be un-published — so `erasable` implies
   never-share and a pack can only ever carry an append-only or retractable
   collection.
   The reranker is the opposite kind of decision —
   `RERANK_URL` is not in `space_id`, so it is measured after the collection
   exists, not before; three things in the current `_rerank` would bite this
   corpus first (a hard `[:2000]` truncation that cuts a long articolo the
   encoder saw whole, a silent degrade to RRF order that no timing records,
   and node-global scope). Pinned facts already bypass RRF and rerank both,
   which is what the citation resolver needs. The GU's own issue stream
   (concorsi, inserzioni) is deliberately NOT scoped: unread terms plus
   personal data.

## Invariants re-audit (2026-09-09) — the review-only ones, plus #9

CLAUDE.md says "the ones enforced by review alone are the ones that have
drifted", which is a pointer worth following. #5 and #6 are the two, and
#9 was checked with them because its own prose names a violation observed
live.

- **#5 BANDWIDTH — holds.** `app/acquire/transport_hf_hub.py` looked like
  a gap: it moves corpus bytes and appears in no budget-drawing module.
  It is not one — `fetch_one` delegates to `HttpsTransport.fetch_one`,
  which calls `budget.acquire_bytes` per chunk. The only unmetered
  traffic is the Hub's tree-listing JSON, which is metadata and not bulk.
- **#6 STORAGE — holds.** Nothing in `app/` or `tools/` runs a VACUUM;
  the single occurrence of the word is `app/db.py:337`, a comment saying
  what to do if one is ever introduced (clear the alignment marker so
  `catalog_fts` is rebuilt against the new rowids).
- **#9 LICENSE GATES — had a hole, now closed.** `require_ack` was called
  from exactly ONE place in the tree, the archive FETCH. A shard already
  on disk never passes through it, which is the bypass the invariant
  itself names and records as observed live. The check now sits at
  `genesis.import_shard` as well, with the test that was missing. The
  IMPORTER half stays open by design — it belongs in the module step 4e
  rewrites (migration plan, step 4f).

## Invariants review (2026-08-23 — amendments 30, 31)

The ten CLAUDE.md invariants were re-read against what was built. They
hold, with one violation and several stale sentences: **#10 was inverted
for `embed_mode`** (TOML parsed and only displayed; the Library's override
permanent) — fixed, the worker now applies the TOML on restart/add/edit and
the reference node's TOMLs were written with their effective modes first so
the deploy changed nothing. Each invariant now says how it is enforced
(*boot* / *test* / *review*); #7, #8, #9 gained mechanical tests
(`tests/test_invariant_guards.py`); #8 gained `ADMIN_TOKEN_FILE`; the stale
`space_id` component list in #1 is a pointer to §3. A short list of
**engineering rules** joined CLAUDE.md, each a recorded incident. Every
stdout log line now names its collection. The golden test no longer fails
on a fresh clone (fixture mtimes are pinned to the golden's values).

## Known gaps and traps

- **Lance versions used to accumulate without bound — FIXED and cleaned
  2026-09-10.** Lance is a versioned store: every write leaves a
  manifest, and a manifest pins the fragments it referenced, so a table
  grows with its write COUNT and not its row count. Pruning existed but
  sat behind two conditions unrelated to what causes growth — it was
  reachable only through the index `refresh`, which is gated on unindexed
  rows (so a table with no index never pruned AT ALL), and it shared a
  flat 150 GiB free-space floor with compaction, which deadlocks: under
  the floor nothing runs, so nothing is freed, so free space never climbs
  back. `vector_index.prune` now runs hourly per collection from
  `ensure`, independent of every index decision, and the floor is
  proportional to the table (25 %, over a 2 GiB margin).

  The one-time cleanup, app stopped, verified by row count, a text read
  and a kNN query on each table afterwards:

  | table | versions | size | freed |
  |---|---|---|---|
  | pubmed | 29,007 → 2 | 116.4 → 79.3 GiB | **37.1 GiB** |
  | wikivec | 13,656 → 2 | 274.6 → 223.3 GiB | **51.3 GiB** |

  **88.4 GiB** in total, 43 minutes, no row touched. Most of wikivec's was
  not manifests but STALE INDEX GENERATIONS: `_indices` went 63.0 → 23.9
  GiB. Worth knowing, because the manifests alone (2.9 GiB) would not
  have justified looking.

  `louise index vector prune <collection> [--keep-hours H] [--force]`
  does it on demand. Careful with what the numbers mean: wikivec's 208
  GiB of `data` is NOT bloat — 91 M chunks at 1024 f16 dimensions really
  is about 209 GiB, and it is 91 M chunks rather than 12.8 M because the
  `verbatim` layer is one chunk per PARAGRAPH, about seven per article.

- **`deploy/reference/DEPLOYMENT.md` is STALE about encoders (found
  2026-09-10) and only the operator can fix it** — it is gitignored, the
  copy of record is on the node, and the procedure copies it back with
  `scp`, which an assistant session cannot run. Two errors, both of which
  sent a day of measurement at the wrong collection: the encoder table
  says `embed-gpu-qwen3-8b` serves `gutenberg` (it serves
  `gutenberg-pack`), and neither `embed-gpu-qwen3-4b` nor the
  `gutenberg-pack` collection appears anywhere in it. **The authority is
  `config/collections/<name>.toml` on the node** — that is what the app
  reads. The live map:

  | collection | encoder | port | dim | ctx |
  |---|---|---|---|---|
  | `gutenberg` | Qwen3-Embedding-4B | 30437 | 2560 | 8192 |
    | `pubmed` | MedCPT article | 30440 | 768 | 512 |
  | `wikipedia`, `wikivec` | bge-m3 | 30442 | 1024 | 2048 |
  | `genome-sacCer3/dm6/hg38` | DNABERT-2 | 30443 | 768 | 1024 |

- **`tests/test_fts_batching.py::test_index_book_still_works_for_single_document_callers`
  is FLAKY (seen 2026-09-10, on the NODE).** Failed once in the first
  full-suite run there, passed alone, and passed in the next full run
  (1942/1942). The assertion is `fts.search(...)` returning a hit, and
  `search()` wraps everything in `try:` and returns `[]` on any exception
  — so the symptom of ANY failure inside it is "no results", which is the
  same trap `get_index` documents for a schema mismatch. Under ~1,900
  tests the pressure on tantivy handles is the obvious suspect and is not
  demonstrated. NOT caused by this branch: `app/fts.py`, `app/worker.py`
  and this test file are all untouched by `main..louise`. The pair of it
  and the parallel-embed flake below share a shape — a legacy test that
  loses a race under load — and both are worth one proper look rather
  than two dismissals.

- **`tests/test_worker_parallel_embed.py::test_one_bad_document_is_isolated`
  is FLAKY (seen 2026-09-09).** Failed once in a full-suite run, then passed
  alone, passed with its own file, and passed in two further full-suite runs
  — including with `-p no:randomly`, so it is not an ordering effect between
  files. It drives `worker._embed_parallel`, a thread pool, and asserts both
  a retry count and that all seven healthy documents finished; under a loaded
  container either could plausibly lose a race. Not diagnosed, and NOT caused
  by anything on this branch (it also passes with the branch's changes
  stashed). Worth a proper look before it is dismissed as noise a second
  time: a test of parallel isolation that is itself timing-dependent is the
  one that will hide a real scheduling regression.

- **8% of gutenberg's stored vectors are corrupt, and the encoder that
  wrote them was an UNAPPROVED build (2026-08-28).** `tools/vector_compliance
  gutenberg` over 163 sampled chunks: 13 below TAU (8.0%), worst cosine
  **0.338**, median 1.000000; every failure a 4,000-char chunk, clustered by
  book (4643, 4639 at 21%; others at 0%). The STORED side is the corrupt one
  — six fresh draws agree with each other at 1.000000 and disagree with the
  stored vector at an identical cosine each time. The production Arc was
  serving `server-intel-b10630` while the approval table pins b106**03**
  (digits transposed; see docs/approved-encoders.md). `gutenberg.embed_mode`
  is now `off` in its TOML so nothing further is written.
  **The trap for whoever picks this up: the endpoint tests CLEAN.** Afterwards
  it reproduced at 1.000000 at every concurrency (1/4/8), identical and
  distinct long texts, serial and parallel. A short probe cannot clear this
  backend — which is exactly why the 16-text approval trial and the backend
  gate's probes both passed while the corpus took damage. Check the CORPUS
  (`vector_compliance`), not just the endpoint.
- **No vector index — every semantic query is a flat scan of the Lance
  table** (`embeddings.search`: `tbl.search(qvec).limit(k)`, no
  `create_index` anywhere). Measured 2026-08-26 with a random unit
  vector: pubmed 2.2M rows 0.75 s, wikivec 15.8M rows **13 s** per
  query, Louise2's 100k-row wikivec 0.05 s; cost is linear in rows ×
  bytes (f16 already halves it). The fix is design work, not a knob:
  Lance indexes do not cover rows added after the build (new rows stay
  flat-scanned until an optimize step), our delete-then-insert write
  pattern needs an index-maintenance stage in the worker, and HNSW at
  15.8M rows is ~14 GB of RAM — the bucket (IVF) family on disk is the
  realistic choice (pack-format.md §5, appendix A; build order step 3).
- **Locators are absolute paths.** Moving a corpus invalidates every one.
  Discovery now detects and repairs this automatically, but storing locators
  relative to `corpus_dir` would remove the failure mode instead of healing
  it. Touches every plugin.
- **Discovery never prunes unseen documents** (`plugins/cursor.py`), so a
  document deleted upstream lives forever. Snapshot deltas carry tombstones;
  nothing else does.
- **`chunk_abstract` is dead code** — `chunker_for` never returns it, so
  pubmed uses `chunk_book`. Deliberate: wiring it would change pubmed's
  chunking (invariant 3). **Measured 2026-08-25, and it now matters:**
  MedCPT holds ONE vector per article (title + abstract, 512 tokens), the
  genesis shape gate binds vectors to chunks 1:1, and `chunk_book` splits
  every abstract-bearing article into two or more chunks — of shard 0's
  647,490 held documents 274,514 (42 %) are multi-chunk, of shard 1's
  938,691 609,182 (65 %), and the fraction rises with the year as
  abstracts become universal. So the archive can cover only the
  title-only records; every article with an abstract is skipped and
  falls to the local encoder once its shard is final. **Built
  2026-08-26 (operator approved 2026-08-25; amendment 39):**
  `chunker_name("abstract")` now returns the abstract chunker — one
  chunk per article, `title\n\nabstract`, no chapter label — under its
  own `ABSTRACT_CHUNK_VERSION` (PubMed-only, invariant 3), and
  "abstract" is no longer grandfathered in the space payload, so
  PubMed's space id moves and the boot check refuses the old pin;
  `python -m app.cli space repin <collection> --yes` (worker stopped) is
  the operator step: drops the pin, resets `embed_done` AND `fts_done`
  (the lexical index held chunk ids cut by the old chunker — same id,
  different text otherwise), clears the verification ledger, keeps
  fetched archive shards (`applied` → `fetched`, no re-download), drops
  finished rows, removes the Lance table. Measured before choosing the
  text format, 24 abstract-bearing articles against the archive: cosine
  median 0.988 / min 0.960 for `title\n\nabstract` (NCBI's `[SEP]` join
  0.991 / 0.974 — not worth a marker in displayed text; abstract alone
  0.976 / 0.945 — the title matters). The encoder is TEI (MedCPT
  tokenizer 3.8–6.2 chars/token, median 5.0; TEI refuses >512 tokens
  with 413 unless started with --auto-truncate), so the client's char
  budget of ctx·4 = 2,048 chars is 410–530 tokens and its over-length
  fallback now shrinks in 20 % steps instead of halving. Tests:
  `tests/test_pubmed_abstract_chunker.py` (one chunk per fixture
  article through the worker, PubMed's id moves and book's does not,
  the repin's every effect). **Re-pinned on the NAS 2026-08-26 07:39
  (5dd6250):** the old pin `b009849c…` held 1,587,743 archive vectors
  (title-only records) and 12,119,490 lexical entries — all reset; the
  worker pinned `41844ebb…` at boot, re-indexes at 1,000 documents /
  1,000 chunks per batch (one per article, ~8–10 h for the 12.1M from
  the packs), and the first archive pass over the re-indexed documents
  reported **0 chunk-shape mismatches** — every article the archive
  holds is now covered as it becomes indexed; the 8 fetched shards were
  kept (`applied` → `fetched`), nothing re-downloaded.
- **Library/Stats counts are cached ~2 minutes** (stale-while-revalidate).
  `invalidate_counts` is wired only to the reset endpoint, so any change made
  outside the web process — including direct SQL — lingers until the TTL.
- **`test_documents_embed_concurrently_and_queue_drains` is flaky**: it
  asserts two threads genuinely overlap, which the scheduler need not do
  under load. A barrier would make it deterministic.
- **`_sync_pipeline_threads` reconciles by collection NAME only**, so a
  pipeline thread keeps the `Collection` object it started with: a TOML edit
  to `corpus_dir` or `[paths]` never reaches the rdf/fts path until restart.
  Harmless for recipe edits (those stages don't use the recipe).
- **No operator control retries a failed acquisition**: the rescan button
  only rescans the filesystem; a transient fetch failure waits for the
  6-hour discovery interval or a worker restart.
- **`EMBED_BATCH=1` is load-bearing.** llama-server corrupts long sequences
  when one `/v1/embeddings` request carries multiple texts, deterministically
  per batch shape, and its prompt cache can replay the corrupted result for
  later single-text requests. Use concurrency for throughput, never batching.
  This does **not** apply to encoders we host ourselves.
- **The main loop is blind for the length of an embed round.**
  `_sync_collections` (hot-add of a new TOML, config changes) and the
  worker's own telemetry run between rounds, and a round of
  `_embed_parallel` returns only when its in-flight DOCUMENTS finish —
  six 500-chunk books on a 3.5 chunks/s GPU held Louise2's loop for
  15+ minutes while a freshly created collection sat unnoticed
  (2026-08-26). The stream deadline stops refilling, not waiting. Fix
  to design: keep the executor across rounds and collect finished
  documents at the next round's start, so a round returns at the
  deadline; until then a worker restart is the workaround.
- **The create dialog's default `[embed]` is the node's encoder** — right
  for a corpus this node embeds, wrong for a pre-embedded one, whose
  recipe must be the ARCHIVE's function (BGE-M3 for wikivec) or the
  genesis proof rejects every shard. Offer the archive's recipe when the
  plugin declares an archive; until then set `[embed]` by hand (Louise2:
  the NAS's BGE-M3 endpoint) and `space repin` if a wrong pin landed.

## Lessons that cost real time

Recorded because each was invisible until it wasn't, and each would be easy
to reintroduce.

- **A fallback that cannot be declined.** `load_all()` synthesized the
  legacy default collection whenever no TOML existed — so on Louise2,
  removing the last collection made "gutenberg" reappear, with no file to
  retire (2026-08-26: "cannot delete all collections"). The mode switch
  is now the `collections/` DIRECTORY existing: absent = legacy default;
  present = exactly its TOMLs, possibly none. Removing the synthesized
  default from the UI creates the empty directory; the create flow
  captures the synthesized default BEFORE creating the directory (the
  "79k books vanished" trap in a new coat); with zero collections the
  control manifest falls back to the legacy path and the worker idles
  until a TOML appears.
- **A global knob that scopes per-thing.** `CHUNK_VERSION` entered every
  collection's `space_id` while chunking is per-plugin, so changing text
  chunking would have re-pinned collections whose chunker never changed —
  including pre-embedded ones whose vectors *cannot* be recomputed by
  re-chunking. Two chunkers at the same version number also produced
  byte-identical space payloads, so the identity has to name the chunker,
  not just its version.
- **Caching keyed on a name.** `fts.get_index` documents it and the counts
  cache repeated it: two Collection objects can share a name and point at
  different files. Key on the physical path. The scan cursor repeated it a
  third time (`path.name` as the key collided across language directories
  and silently disabled all skipping for the embedded plugin).
- **A linear cost per item is quadratic per corpus, and no test corpus is
  big enough to see it.** `DELETE FROM catalog_fts WHERE uid=?` scanned the
  shadow table (uid is UNINDEXED); at fixture scale that is nothing, at 36k
  rows it was already 98% of the stage, at corpus scale it never finishes.
  When a per-document cost depends on how much has been processed so far,
  treat it as a blocking bug regardless of how fast it currently looks.
- **Telemetry published by the thread doing the work** is invisible exactly
  when it matters — the loop is blocked. Publish from elsewhere.
- **Measure before optimising.** Page slowness was assumed to be connection
  setup; it was 12s of full-table scan. `db.connect()` was 1 ms.
- **An unreachable endpoint is not a broken document.** Recording it as an
  error removes the document from backfill until a human intervenes.
  Distinguish deferral from failure — the acquire path already did.
- **Contact with the real library finds what reasoning does not.** Every
  torrent bug (pad files, asynchronous renames, an always-truthy
  `error_code`) came from running a real swarm, not from reading docs.
- **"Did work" is not "made progress".** A stage that returns True for a
  round in which it imported nothing still wins the round from every stage
  behind it. Genesis's 0-document shard reads were each legitimate
  bookkeeping and together were the whole behaviour of the collection. When
  a stage can succeed at doing nothing, the selection that hands it the
  round must know the difference — and a per-round cost that scales with
  the size of a LIST (shards) rather than with the work available
  (candidates) is the quadratic trap again, in a different coat.
- **A log line per round is a benchmark.** "imported 0 vectors across 0
  documents" every ~10 s, for hours, was the measurement; nobody had to
  profile anything. Read the worker log as a time series before reading
  the code.
- **A rate from one window is not a rate.** The Library ETA divided one
  sample window's progress by its length, so a window that caught a
  commit burst said "~20min" and the next, idle one, said "~9h" — the
  operator saw a number swinging between optimistic and pessimistic and
  trusted neither. Now it is an EMA (alpha 0.3, ~3 windows to settle)
  over the windows that saw progress, it needs two windows before it
  shows anything, and while work remains but the rate is untrustworthy
  or zero it prints **TBD** rather than nothing — an honest "not yet"
  is a value; a blank is indistinguishable from "done". Same rule as
  `stats.eta_seconds`: never a number the page cannot stand behind.
- **A statistic the reader cannot act on is noise, not transparency.**
  The Network page listed the transfer manager's four priority classes
  per direction — "interactive 0 · delta 0 · backfill 0 · seeding 0" —
  which answered a scheduler's question, not an operator's. Replaced by
  two totals and the download split by collection; the classes stay in
  the API payload for tests.

## Conventions

- Python 3.12, full type hints, pytest. **Every task ships tests.**
- Tests run in Docker (`docker run --rm -v "$PWD:/srv" -w /srv louise-rag:dev
  python -m pytest tests/ -q`); there is no local interpreter assumption.
- New dependencies, schema changes beyond the doc, or wire-format changes:
  **ask first.**
- Config tiers: env = identity; TOML files = desired state; DB = runtime
  overrides that do not survive restart. Never write desired state to the DB.
- Secrets by filesystem path only — never in env values, config values, logs
  or error messages.
