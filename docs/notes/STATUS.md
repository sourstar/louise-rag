# Project status

**Last updated: 2026-10-25.** What exists, what is designed but unbuilt,
and what is deliberately not being built. Read this before planning work.

**2026-10-26: the node was powered off 02:33–21:22 CET**, cleanly, by a
`systemctl poweroff` from a truenas_admin session (not the UPS), with the CPU
at 90–91 °C for hours under the 12-core records build. Monday's daytime queue
(the deploy, gutenberg's re-index, wikidata's re-creation, the first weekday
stop) did not run and moves to the next day. The rerank AFTER pass stopped
at 07:00 partway. gutenberg embeds fully in the quiet hours (22:30–06:00,
cron 6 and 5), from 2026-10-26. Operator, the same night: "Quiet hours
light gpu usage (quiet) and cpu usage reduced to 8 cores only" — the card's
max clock is capped in 22:30–06:00 (the night embedding runs slower and
quieter) and the records build gets 8 cores.

**Where things stand (2026-10-25).**
- **wikidata** is re-created on Monday 2026-10-26 on family map
  `sha256:78399d60…` (approved, 29398cf). It keeps the version-3 pack and
  brings parser 2, the history section and parts cut by content; its
  records build follows.
- **wikipedia-v8** (parser 5, token cut) is mid records build, in cron
  windows. Its pool now keeps its children and takes the whole cpu quota
  (c3dcb4c, 6694b19).
- **pubmed** is the Codex-8 collection (renamed 2026-10-20), paused.
- **gutenberg** is on-demand on the node. Perugia, the second GPU peer, is
  shut down (2026-10-25 10:23 CET) after handing its vectors over.
- **Search:** query steps take priority over corpus embedding (7cc6975,
  deployed). The entity boost is removed and an identifier restricts only
  as the reader's `keys` filter (05 §5.2 amended; deploys after tonight's
  measurement).
- **Open defects:** the node passes `ADMIN_TOKEN` as an env VALUE to the
  worker and web (invariant 8; deploy/truenas-app.yaml takes
  `ADMIN_TOKEN_FILE`, the node's app is not yet on it); the node key's
  backup is `/mnt/tank/homes/alanari/louise-node.key` (+ `.pub`, 2026-10-25,
  checked byte-identical and its public half derived from it), which is on
  the same machine: an off-machine copy is the operator's to keep;
  the node's private key sits in `/config`, which the LAN-facing web
  mounts read-write — the yaml gives it to the worker alone, read-only
  (both moved to a secrets dataset in the next deploy);
  the reranker's HTTP 500s under load (cause not found);
  the 127-part gap (stale counts and text index): `louise dev index-drift`
  names the documents, `dev reindex` fixes them, on the node 2026-10-26.
- **Parked:** arXiv and the knowledge-base pipelines.
- **Counted on the node (2026-10-25):** how many records have no date is
  NOT known: the build databases hold no skip for it, but `hold records
  build` never passes a cutoff, so that exclusion has never run; the
  `cutoff-undated` flag gives the number at the next gutenberg and pubmed
  build (docs/notes/corpus-inconsistencies.md). A sample of 120,000 pubmed baseline citations
  holds none without DateRevised or DateCompleted. gutenberg's one build
  skip is book 673, whose encoding the origin declares broken. The census
  of the mirror's 79,770 `pg<N>.rdf` files is running (book 10 has none).
  gutenberg holds no superseded part versions. LocalAI's watchdog kills a
  model busy for more than 10 min (`busy_timeout`); its idle timeout is
  disabled. The ZFS ARC is capped at 16 GB.

**The specification moved.** `docs/README.md` and the nine numbered
documents it lists are now the design; `architecture-v2.md` and its §17
amendments are the history that produced them and are kept for that.
The migration that brought the code to them is complete (2026-10-08); its
plan is deleted (`git show 7f84d44:docs/notes/migration-plan.md`), and what
it left open is below.

## Open after the migration

- **"Every plate" — gutenberg parser 11** (operator, 2026-10-25; 08 §8.7,
  09 §9.5, 05 §5.3, 07 §7.4 amended). Invariant 11 wins over 08's
  refusal: every image an epub has is carried in the figures container
  as opaque bytes — SVG, GIF, anything over the caps — `carried: true`
  where parser 10 wrote `false`. The closed list (jpeg, png) and the caps
  (16 MiB, 64 MP declared) bind DISPLAY: the page says "not displayable"
  with type and size and offers the file as `application/octet-stream`,
  `attachment` (app/ask/figures.py, app/ask/api.py). What moves: the
  record LINE and the figures container of the books that had such an
  image (2,407 SVG and 46 GIF images; ten books for the SVGs). What does
  not: `sections`, `content_hash`, `origin_hash`, every chunk — so every
  vector carries. The live gutenberg collection takes it by RULE 1
  re-creation carrying what is identical (02 §2.4 A1c, `louise dev carry
  --rebuilt`), as parser 9 was; nothing has been run against a
  live node. 4,096 figures per record stays in 08 as a cap on what a
  record may name: enforced since 2026-10-25 (the build aborts over it,
  `hold check`'s `figures` row fails it; it never was before).

- **arXiv — PARKED (operator, 2026-10-22).** A blueprint only: docs/notes/arxiv-plugin.md (bulk forms, licences, pre-embeddings) and docs/notes/arxiv-collection.md (the abstracts collection, every version linked, only the latest embedded; the encoder on hold with a 20,000-abstract / 500-question measurement kept). Nothing built, nothing downloaded; no session works on it until the operator resumes it.

- **The P2P review** (2026-10-19,
  [p2p-review-open-issues.md](p2p-review-open-issues.md), "The review"):
  seven gaps found when the second GPU peer joined, five fixed on the
  review branch with amendments to 04 §4.2/§4.3/§4.5, 07 §7.3 and 09 §9.9
  — a holder bundles the vectors it already holds whoever leads the part
  (the node's 91 % of gutenberg becomes coverage); attestations and
  bundles are pulled from digests, not only carried by events; a pinned
  address wins over the declared one; a fetch nobody serves is deferred
  and bounded, never hung; a bundle is announced only where it is served
  and a fetched public collection is shared. Deployed on the node 16:00
  CEST; its first holder day bundled 1,296 parts (7.9 M chunks) with no
  GPU and, after each restart, 418 parts a second time — DEPRECATED by
  the producer's own `deprecate` attestation (03 §3.6, 09 §9.8; `dev
  deprecate-duplicates`; note §11 with the design's open points): kept
  and served a day longer, never offered for a new fetch, the fallback
  when the current copy is gone (operator's design, 2026-10-19). Perugia's first
  pass re-embedded led parts the node already offered (note §12, fixed).
  Still open there: vector convergence (measure first; not decided). A
  leecher's `records-vs-origin` is done (Perugia PASSED, 2026-10-24). The
  PUBLISHER is built (56b8a50); the node's gutenberg stays on-demand
  (operator, 2026-10-20), and Perugia is shut down (2026-10-25).
- **Wikidata, re-created Monday 2026-10-26** on map `78399d60…` (29398cf):
  parser 2, the history section, parts cut by content (RULE 1: the
  version-3 pack kept, `--calibration-from`). Then the records build and
  the text index. Scholarly articles, lexemes, niche languages and the
  map's small misplacements go into ONE later re-creation. The 99 GB
  version-1 pack is deleted after the build. Build plan:
  [wikidata-build-plan.md](wikidata-build-plan.md).
- **Normattiva, what the second part left** (2026-10-12,
  [normattiva-multivigenza-review.md](normattiva-multivigenza-review.md) §3.12,
  §7): the measurement before `shard_records` is fixed (a second archive
  listing a month on) and the Constitution's on-demand query. `normattiva`
  is live (2026-10-14); its DAR-legal-it vectors are re-embedded bit-exact
  in quiet hours (77148be, b2f3dd6); `normattiva-archive` is not created;
  its `records-vs-origin` re-run is still owed.
  Built on 2026-10-12: the vector carry, `relations()`, the
  central-directory delta, the member as origin unit, the `urn`/`eli`/
  `akn` reference source with the citation resolver
  (`app/refs/normattiva.py`; `keys` written at parser 6), the original's
  start at its Expression date with `valid_from` as a third index column,
  and a repealed act's validity closed by another act's typed whole-act
  repeal (`closures`, `validity_closures`; review §3.2 case 2).
- **The Gazzetta plugin**: certified records carrying `extra.seal` — the
  certifier body's to run (step 10).
- **`backend_gate`** stays while any live Codex is version 1 or 2 (today:
  wikipedia's parser-4 Codex, until wikipedia-v8 replaces it; re-count on
  the node). History:
  Moving one to version 4 is a new collection AND a new space (09 §9.3), so
  its vectors would be recomputed — unless RULE 1 carries them. Measured
  2026-10-08 (32 random chunks re-embedded now vs stored, f16): gutenberg
  differs by ≤9e-5, inside the encoder's own run-to-run variation (≤1.2e-4)
  — carryable; wikipedia by 1.5e-3, four times it (its first layer is the
  wikivec archive's engine); pubmed materially (cosine 0.958). So pubmed and
  wikipedia would be re-embedded in full, and `backend_gate` cannot go yet.
- **Operator**: file upstream [`localai-bugs.md`](localai-bugs.md) (step 15),
  [`upstream-bugs.md`](upstream-bugs.md) (V1/V2) and
  [`upstream-reports/wikidata.md`](upstream-reports/wikidata.md); none filed yet.
- **The character cut** (2026-10-18, [char-cut-truncation.md](char-cut-truncation.md)):
  the platform cuts long inputs by characters, not to `ctx` tokens. Pubmed
  fails its own `vectors-sample` on it (74 of 1,088 sampled chunks, all
  NCBI's long abstracts; 73 reproduce once the encoder truncates). The fix,
  approved the same day, BUILT and DEPLOYED (wrappers and Louise, 2026-10-18).
  The cut is a space's: a space that declares `tokens` sends a VeGa encoder
  every input whole with `ctx` as the tag's `window`, cut there exactly; every
  live Codex keeps the character cut its references were made with, until its
  corrected Codex (wikipedia's parser-5 switch first) — the note's §4–5.
  **The exact cut needs our llama.cpp backend** (2026-10-18, operator: "patch
  it"): upstream's ignores token ids ([localai-bugs.md](localai-bugs.md) B14),
  and 3 of 512 texts have no text with exactly their first 1,022 tokens. P2
  embeds them (189/189 cut exactly on the CPU build); the wrapper sends a cut
  as its tokens where `Gate._probe` finds the backend does. LIVE on the node
  2026-10-18 21:19 (`deploy/vega/switch/rollout-p2.sh`): the five SYCL models
  bit-identical over fresh loads, 0 of 512 refused at 1,024, every gate passed,
  nothing re-embedded; the AMD peer runs its `hipblas` build the same way.
  **Pubmed's corrected Codex is built** (2026-10-18, operator: "Yes";
  "Remember rule 1"): Codex version 8 declares `space.cut = "tokens"` (inside
  `space_id`), `dev carry --recut` crosses it (A1e), and
  `tools/recipe_from_codex.py` rebuilds the live v6 Codex byte for byte before
  making its token twin (space `504c1d34…`). **Ran 2026-10-18/19, overnight**
  (operator: "continue unattended"; Louise c4be8d0 live, bc3c00f's tools run
  from source — no internet to build): `pubmed-v8` (`a1d8a9f7…`) holds pubmed's
  state by carry; the recut dropped 844,918 local rows the character cut
  shortened (102 of them CJK: each embedded alone, a pair layer); vectors-sample
  then found NCBI rows of another input — 54,548 POPLINE citations whose only
  abstract is an OtherAbstract, cut to their title
  ([pubmed-other-abstracts.md](pubmed-other-abstracts.md), a decision for the
  operator) — dropped with `dev drop-rows`, and 5 single misses; two fresh
  seeds then PASS (1 and 3 rows under the tolerance, below the quorum). Since
  then all 39,994,988 documents are done, the collection is renamed `pubmed`
  (2026-10-20), v6 is retired, crons 7/8 are disabled, and it is PAUSED. **Open, first:** those residual misses (~0.2 % of
  sampled rows, 2022 PMIDs: records revised after NCBI embedded them?) —
  compare every imported row with the title/abstract NCBI ships beside its
  embeddings (`pubmed_chunk_N.json`) once the internet is back, and drop the
  ones that differ.
- **Wikipedia parser 5** (2026-10-18, [wikipedia-qids.md](wikipedia-qids.md)):
  the QIDs from each run's `page_props`, built and tested with `dev carry
  --resign` (A1d). The switch is under way: wikipedia-v8's records build
  (2026-10-24 entries below).
- **Vector writes stop below the disk floor** (2026-10-18, operator):
  `app/disk_guard.py`; `node show` lists the filesystems. ssd5 (LocalAI's
  models, ComfyUI's, Ollama's) stood at 94 % that day, 26 GB free.
- **A record parsed from bytes its `origin_hash` does not cover** (found
  2026-10-11, operator: "note this issue for the future"). Gutenberg's
  records took creators, dates and subjects from `pg<id>.rdf` while
  `origin_hash` — and discovery's change signature — covered only the form
  read, so a catalogue revision was invisible: records-vs-origin read it as
  a parser fault, and the record was never rebuilt. Fixed for gutenberg
  (parser 9, `dc22c26`); check 03 §3.2 amended (`b7b6ec2`: a record revised
  at the origin is "changed", not failed). **Guarded since 2026-10-11**
  (operator's yes): `tests/origin_coverage.py` records every file `parse()`
  opens (Python's `open` and pyarrow's Parquet readers) and fails unless
  `fetch()` read it too, and unless a change to it moves discovery's
  signature; each plugin's test module runs it over its fixture, and
  `tests/test_origin_coverage.py` fails when a registered plugin is neither
  covered nor exempt with a reason. Audit result: pubmed, wikipedia,
  normattiva and embedded parse only the bytes fetch hashed; deposit reads
  its `.deposit.json` beside the file — exempt (Louise writes it once, and a
  deposit collection publishes no record). Proven not vacuous: parser 8's
  `origin_files` put back, the gutenberg test names `pg<id>.rdf`. Its limit:
  a file opened by C code other than pyarrow (lxml's own parser on a path,
  a native reader) is not seen. Related, by design and worth knowing: a catalogue-only revision
  does not re-issue a part (02 §2.3: "changed" is the cleaned text's hash);
  it shows at the part's next re-issue. And the origin folder is a mirror,
  overwritten in place, with no periodic snapshot of its dataset: a record
  "changed at the origin" can never be re-verified against the bytes it was
  built from.
- **A GPU inference that never returns** (2026-10-10/11,
  [v5-migration.md](v5-migration.md) "PubMed's re-embed, as it ran"). Twice
  an OpenVINO `infer_new_request` in `louise-medcpt-article-ov` never
  returned and PubMed embedded nothing (7 h, then 25 min); gutenberg's
  llama.cpp encoder stalled 20 min after the second. The first two hangs
  began within seconds of the host fan script's `gpu_wake` (debugfs
  `forcewake_all`; 22:27:07 and 06:08:50 UTC), whose check read
  `gtidle/idle_status` once and so caught the card in gt-c6 between requests.
  **A third hang (2026-10-11 ~12:07 UTC) came with NO wake** (none since the
  patched script started at 11:22 local), while three encoders shared the
  card (pubmed's OpenVINO, gutenberg's and wikipedia's llama.cpp). So the
  wake is at most one trigger; the cause is unknown — the xe driver, Level
  Zero under two runtimes on one card, or OpenVINO's GPU plugin. NOT
  concurrency inside the backend: `f880ce0` serialized inference (kept;
  harmless, ~40 docs/s either way) and the second and third hangs had one
  thread in `infer`, three waiting on the lock.
  Mitigated 2026-10-11 (operator's choice; the script is the operator's,
  `/mnt/ssd5/scripts/fancontrol/fancontrol.py`, original kept as
  `fancontrol.py.bak-2026-10-11`): a wake now needs `gtidle/idle_residency_ms`
  to have grown by ≥ 98 % of the 10 s check interval, i.e. a GPU that did no
  work at all since the last check. Measured under Louise's load: 260–474 ms
  idle per 10 s (3–5 %), so a serving card is never woken. **Not robust:**
  - a job that starts during the 1 s hold, or between the residency read and
    the wake, still meets a forcewake (a query encoding, a batch resuming
    after a pause, the worker's 5-minute retry);
  - which overlap hangs the work is not known (a forcewake held over a
    submission, or one taken while the GT leaves c6), so the window cannot be
    argued closed, only made narrow.
  Still open:
  - **the robust layer is a watchdog, whatever the cause — now needed, since
    the cause is not the wake alone — and LocalAI has one**: the busy watchdog (`watchdog_busy_enabled`; ON since 2026-10-12 at 10 min, operator — its first second killed a pubmed encoder hung for 8 h, and LocalAI reloaded it on the next call with no hand;
    `watchdog_busy_timeout`, 5m — two values on record; confirm the live one on the node). It times each backend from its OLDEST
    in-flight request, covers every backend (VeGa or not, pinned or not),
    and force-kills (SIGTERM, SIGKILL after the grace period); the next call
    loads the model again. 5 m matches the worker, which abandons a call at
    300 s anyway. The cost: one global timeout, so a legitimate call longer
    than it (a long chat answer, a big batch) is killed too; measure the
    longest real calls first. Writing our own in VeGa's wrapper
    (`deploy/vega/vega_wrapper.py` owns the inner process) would allow a
    per-model bound but changes the wrapper's files, i.e. a new run for
    every vega model, and leaves the non-VeGa backends uncovered;
  - the stuck fan the wake works around is the card's firmware; a firmware or
    xe update that keeps the temperature fresh in gt-c6 retires the wake;
  - throughput the OpenVINO way: true batching (several items per
    `infer`) or an `AsyncInferQueue` with the plugin's own streams, instead
    of one item per gRPC call;

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
5. No local Python is assumed anywhere: `docker build --target dev -t louise-rag:dev .`
   recreates the test image, and tests run as in Conventions below. The
   dev image (with requirements-dev.txt) is not the deploy gate: the gate
   is the image built from requirements.txt alone.
6. The agent container (a Claude session beside the node) is described in
   `deploy/agent/README.md`.

---

## Where the roadmap stands

| phase | state |
|---|---|
| **Phase 0** — generalize, no network | **done.** CorpusSource + collections + `space_id`, five plugins, ReferenceSource/EntityResolver/`doc_keys`, query federation, acquisition layer with profiles, transfer manager. |
| **Phase 1** — bundles + community | **built and exercised** between two peers on gutenberg (2026-10-19→25: bundles both ways, a SHARD fetched and checked). The *deliverable* — publishing a revived PubMed corpus — is still not done (after the wikidata work). |
| **Phase 2** — swarm | **built** per 04 §4.2: BitTorrent v2 via libtorrent, digests, neighbours, the peer list and the offer (`app/share/gossip.py`, `peers.py`, `offer.py`). [`p2p-discovery.md`](p2p-discovery.md) is the earlier design. |
| **Phase 3** — decentralize | no DHT of our own; libtorrent's mainline DHT finds swarms by the collection's swarm key (04 §4.2). |

### 2026-10-25: the operator's decisions on the spec audit — every plate, keep all, the catalogue exception, CLAUDE.md aligned, a run gives back its own bytes, no silent dateless records

From the audit of docs 01–09 against CLAUDE.md (2c6642e), the operator decided:
- **"Every plate."** Every image the origin has is carried as opaque bytes, SVG and over-cap ones included. Only display decodes, and it admits only JPEG/PNG within the caps; any other plate is offered as a download (being built: a gutenberg parser bump, re-created under RULE 1).
- **"Keep all."** A web page subscribed to a deposit is kept byte-exact, and its record is parsed from it (being built).
- **"The final goal is the quality of the information retrieval."** The catalogue's first record after the reranker is kept, named as invariant 4's one exception (05 §5.6).
- **"Align claude to the code."** Invariants 1, 4 and 10 updated.
- **Batching, choice A.** The gate fails a run whose single-text passes differ (invariant 12, now measured). Batched answers must equal the single-text bytes, or the run is limited to one-at-a-time work (the 2026-10-12 band is gone).
- **Dateless records, choice A.** A record a dated snapshot cannot place is a defect to fix, never a silent exclusion. Gutenberg's cause is a missing or damaged pg<N>.rdf in our copy. PubMed's record parser already falls back to MedlineDate and ArticleDate, so a dateless PubMed record means NCBI gave no date of any kind. Counts are asked of München before the fix.
- **RULE 2.** The Library's dialogs ask only a name; the node resolves the encoder from the Codex's weights (being built).
- ~~**Invariant 5.** A deposit source's git clone draws from the bandwidth budget~~ — as it lands, git paused while each increment is drawn (2026-10-25).

### 2026-10-25: the origin's inconsistencies, logged and flagged (operator; dateless records, choice A)

"Add any inconsistency found in the corpora in a report. Make the parsers such that on a regular basis any inconsistency is logged and flagged." The report: [corpus-inconsistencies.md](corpus-inconsistencies.md). The mechanism (06 §6.1 amended, `app/issues.py`): a parse returns `CanonicalDoc.issues` beside the record, never in it (no record byte, hash or PARSER_VERSION moves; nothing is rebuilt); `hold records build` records them for every record it parses, the worker's rdf stage for the codes its read checks; per document the checked codes are replaced, so a fixed origin clears them; each is logged once with the collection's name; `hold collection show`, `node health` and the Library's card count them, `hold issues list` lists them. Wired: `cutoff-undated` (the platform's, every gutenberg and pubmed record on every build, cutoff or not); gutenberg `rdf-missing`, `rdf-unreadable`, `rdf-no-ebook`, `epub-unreadable`, `epub-image-missing`, `epub-document-missing`; pubmed `no-publication-date`, `no-title`, `other-abstract-only`; normattiva `frbr-expression-stale`, `expression-before-publication`. The manifest gains the table `issues`, created on open (07 §7.3 `dev migrate schema` amended). Found on the way: `hold records build` passes no cutoff to the builder, so the build databases' 0 "no cutoff date" skips (München) count nothing; the first build with the flag gives the real number.

### 2026-10-25: the records build A/B — every part byte-identical; the build takes the whole cpu quota

München ran en/2M's first 250,000 records of wikipedia-v8 (2,096,112 chunks, 125 parts, a 4-cpu quota) under four trees. The 125 parts were byte-identical in all seven runs. Wall time:
- the old pool, 11 children: 761 s;
- a833c1e, 3 children: 630 s;
- 4 children: 544 s, 1.16× faster than 3 at +3 % CPU per record;
- c3dcb4c (the exit fix), 3 children: 623 s.
`hold records build` now takes the whole quota (6694b19); the worker keeps quota − 1. The live build runs on c3dcb4c since 13:37 CET and takes 6694b19 at its next restart. Table and decision: [scheduling-log.md](scheduling-log.md), 2026-10-24 row.

### 2026-10-25: the wikidata family map approved — `78399d60…`

The third revision's report under 41e3112's roots (legislation a work root, cell line a molecular root) was approved for Monday's `create` (29398cf; [wikidata-family-slices.md](wikidata-family-slices.md)). Small misplacements (railway station, tram stop, park, ship, facilities) wait for the one later re-creation, with scholarly articles, lexemes and the niche languages.

### 2026-10-25: a stopped records build never exited — the parse pool fixed (München's find, invariant 2)

wikipedia-v8's build committed its part on SIGTERM, returned from main, then hung in the interpreter's exit for 15 minutes until it was SIGKILLed. Nothing was lost. The causes, both reproduced on Python 3.12.3 (the node's interpreter):
- CPython gh-115634: a pool loses the children that retire at their task limit and is left with none, with parts queued;
- a pool thrown away by the watchdog is joined at exit for as long as its children never answer.

Fixed in `app/parallel.py`:
- the upstream fix is backported;
- a pool that is thrown away has its children terminated until its manager ends;
- the exit stops the pool first.

Also: a hung wikidata pack pool is stopped the same way, and the build's log lines are flushed. Tests: tests/test_parallel_exit.py, under 3.13 and 3.12. Measurement row: [scheduling-log.md](scheduling-log.md); pitfall 19 in [scheduling.md](scheduling.md) §5.

### 2026-10-25: the entity boost removed — 05 §5.2, "nothing boosts", measured; an identifier restricts only as a filter (operator: A)

The search added 1/60 to the fused score of a candidate the query's entity tags. 05 §5.2 forbids it (task #33, found by PR #4). Measured on TREC Precision Medicine 2017–18 (80 topics, NIST's judgements, [query-campaign.md](query-campaign.md)), NDCG@10 was:
- no boost, no restriction: 0.3143;
- the boost: 0.3073 (won 16, lost 27);
- the spec's restriction to records tagged with any named identifier: 0.3002; with all of them: 0.2983. Both are ~2 s slower.

None of the differences is significant. The boost is removed, with its lookup; the tests now check that a tag moves nothing. Deployed after tonight's priority measurement (one change per deploy).

DECIDED (operator, "I agree with A"): 05 §5.2 amended. An identifier the query names pins its facts and restricts nothing; a reader who wants the tagged records alone asks with `--filter keys=<id>` (`filters: {"keys": …}` in the API), bounded and refused when too broad as a facet value is. Built in `app/ask/filters.py` and `query_service.TAGGED_SQL` (driven by the entity index, plan-checked); tests in tests/ask/test_filters.py. Timed on pubmed's manifest (München, c14af77, beside the A/B on 4 cpus): a refusal (TP53, EGFR, HGNC:5172, each over 20,000 tags) reads the entity index only, 9–83 ms cold and 1 ms warm (6.4 s before c14af77). BRCA2's 12,053 records take 0.57 s, of which the uid fetch is ~47 µs a row (12.8 s cold before). A covering index on doc_keys(entity_id, uid) would cut that 0.57 s further; it is not added, because 0.57 s for a filter the reader asks for does not pay for a second copy of doc_keys on a 40 M-document manifest. Revisit if the filter becomes common.

### 2026-10-25: Perugia's hand-over, and the coverage count that lied (Perugia, München)

Perugia shuts down for a long time (operator). Its 1,008 bundles (336,586 chunks) were already on the node. 848 chunks of 9 books (lang=en/00872–00875), held only there, were bundled (4 bundles, 1,168 chunks) for the node to take in. The 13 München-key bundles it held cover parts the node covers fully. Perugia stays up until München's per-part confirmation.

Found on the way:
- **a race** (f386934, Perugia): two writers of one possession cache entry shared a temp file;
- **a silent fallback** (5863f81): `counting.view_kwargs` caught the race's error and counted the rows. For a re-minted collection that is only its own bundles: 114,154 covered for 10,164,506. A failed count is now `CoverageUnknown`, never the undercount. The publish pass defers, leading's "every doubt embeds" stands by name, and the CLI and card show no figure.

**The 127-part gap is stale manifest counts, not data** (München, coverage_explain and a re-cut of the records, 2026-10-25):
- every bundle is credited, and cutting the records gives exactly the snapshot's part totals (7,714 / 8,820 / 7,085);
- the live parse agrees with the records (gutenberg:52478 108/108, manifest 105; 52483 69/69, manifest 82), so invariant 3 holds;
- `documents.chunks`, and the text index it was counted from, were written by an earlier index pass under an earlier parse and never refreshed, because `fts_version` stayed 1. Possession converts covered records to chunks through that column, hence the undercount. Whoever changed the live parse back then did so without a version bump.

Remedy, targeted (RULE 1, not a whole-collection FTS_VERSION bump): `tools/measure/chunks_drift.py --all --ids-out` lists the drifting records, and `louise dev reindex --ids` (new) queues exactly those for the text-index stage (fts_done = 0, embedding untouched). Runs by day after tonight's measurement.

CLOSED by `louise dev index-drift` (07 §7.3, app/hold/drift.py): the manifest's counts, the text index and, on a seeded sample of parts, the index's stored chunks, against the records. `hold check --checks chunks` still re-cuts against the snapshot only.

Its cost, MEASURED at two scales on a synthetic agreeing collection (3 chunks a record, ~500 characters a chunk, parts of 5,000, built straight into parts, manifest and text index; this container, 2026-10-25):

| records | full walk, `--sample 0` | default `--sample 0.05` |
|---|---|---|
| 20,000 | 0.84 s (41.9 µs a record) | 1.26 s (63.0 µs a record) |
| 300,000 | 17.3 s (57.7 µs a record) | 19.5 s (64.9 µs a record) |

NOT MEASURED at 2 million: that fixture's build filled the shared disk at 1.6 M records and was stopped. Extrapolated LINEARLY from the 300,000 rate, 2 M records take ~115 s unsampled and ~130 s at 0.05. Two points do not prove linearity: the unsampled per-record cost rose 38 % from 20 k to 300 k (the sampled cost 3 %), and the cause is not isolated (page cache or the index aggregation's growth with the index). A run on a real node's collection settles it.

### 2026-10-25: the gap between the corpus and Wikidata is recorded — "rumors" (operator)

"'The gap' shall be recorded, it is an additional pillar that can be optionally used for searches, the 'rumors'. Review over review of wikidata the current 'rumors' shrink, but they are part of the collection." Design in [rumors.md](rumors.md):
- a rumor is a corpus-derived claim (a new entity, connection, conflicting value or tag) above a measured confidence floor, which the current wikidata snapshot does not state;
- its state (open, confirmed, rejected, known) is recorded per snapshot in its own member, keyed by claim uid, and closed rumors stay in the collection;
- in a search, rumors are an optional pillar, off by default: facts beside hits, a separate list, a filter, never fused.
This supersedes PR #4's "the gap is a view". The spec changes are listed for approval and built with gutenberg-graph. Review of PR #4 itself:
- one real finding, the entity boost against 05 §5.2 (task #33);
- to fix before merge: wikidata linking with gutenberg's encoder (wikidata's space is jina-v3), torch determinism for GLiNER, new dependencies to ask, per-book surface anchors, linking precision.

### 2026-10-25: the reranker's misses — the night's 117, a fresh check, one retry, and query steps first (operator)

FOUND (2026-10-25, München's heading full search; the cause of the HTTP 500s is still open): 117 of 496 queries lost their rerank inside the budget overnight (95 on arm 0, 12 on arm 2, 10 on arm 3, rising through the night) while normattiva's re-embed ran on the card. The asymmetry between arms points at a first-call cost (a load or a wait behind corpus work). Asked of München, read-only: the arm order, how many LIVE searches since 22:30 came back `reranked: false` and why, and the harness's why/ms per miss in Sunday night's resume (deployed conditions). If live searches were hit, this is a defect fixed before anything else, through scheduling.md §6 with numbers.

FIX, part 1 (operator 2026-10-25: "Fix this defect with priority over all the rest"). app/model_load.py's check against LocalAI's `/system` trusted a 2 s cached answer for every caller. In a search, the query embedding's check refreshes that cache, and the rerank's check a moment later believed it. An eviction between the two, by LocalAI's reclaimer making room for another model's load, sent the rerank to a cold reranker: the 7–8 s load ran INSIDE the 8 s budget, against the operator's rule of 2026-10-09 ("Timeouts shall not include cold start"). Reproduced by test (`test_an_eviction_after_a_cached_check…`, which fails on the old code). Now:
- a single request a person waits on asks `/system` fresh (rerank, chat, translation: `max_age_s=0`, about 11 ms);
- a query's embedding burst shares a 0.25 s answer, and corpus work keeps the 2 s cache;
- each loaded model's backend pid is noted, so a rerank that times out with its model gone or re-spawned says "evicted before the request and loaded again inside its budget", and the event log records that cold start, distinct from a reranker that was merely slow.
**It does not explain the 117** (München, 2026-10-25 07:30 CET, LocalAI's log 22:30 CEST to 07:30 CET, 88,346 lines): louise-rerank-bge was never loaded or evicted all night, and there were no cold-start lines for the rerank. The reclaimer fired 4 times, at 85.1–86.5 % of its 85 %, evicting the 4B encoder twice and chat twice. The misses were **HTTP 500 refusals**: /v1/rerank answered 5,662 × 200 and 130 × 500 (2.2 % of calls), every 500 between 23:00 and 03:00 CEST, with no reason in LocalAI's log. A score-variant query sends 4–6 calls in sequence (one per language group), and any failed one makes the whole list the fused order, so 2.2 % per call becomes 10–20 % of queries. Re-run by day with normattiva idle: 6 of the 117, 32 calls, all 200 (340–5,374 ms). Part 1 stays as a guard for the rule (a stale check is a real window), not as the fix. Also found: two LIVE searches spent 30.4 s (20:05 CEST) and 42.6 s (00:06 CEST) in the rerank, both while the campaign was reranking. Separate processes share no gate, so a person's calls can queue behind the harness's inside LocalAI. Next: the 500's body and the backend's stderr, VRAM and gtt per second, concurrency on the reranker, and the YAML's slots and context.

München's day tests: 6 of the missed queries (32 calls) and 18 reranks of 69/45/9 × 4.4k characters beside the 4B encoder embedding (VRAM peak 83 %) — all 200. bge runs `parallel:1`, context 2560, batch = ubatch 2560, flash attention off. At 00:06 CEST the person's search shared the reranker with the harness and the card with a burst of 86 embedding calls by normattiva in one minute, with 2 × 500 of 5 calls. So the refusals come under load and pass at once.

FIX, part 2: a 5xx is asked again ONCE, after 0.25 s, when at least a second of the budget is left (`retrieval._rerank_call`). The rerank is a pure function of its body, the same policy as urllib3's `Retry` on a status list and gRPC's retry on UNAVAILABLE. One refused call of a query's 4–6 no longer costs the whole list its rerank. The reason now carries the server's own message (`HTTP 500: <LocalAI's error.message>`) and says when the second try failed too, so a live search records the cause the logs lack. Tonight's harness (d6ddc85) logs every refused call with LocalAI's body. The per-call 500 rate measures the reranker; the per-query misses measure the fix. The cause of the 500s is still open until that body is read: the backend's allocation or decode failing under the card's load is the reading.

Operator, the same day: the retry treats the symptom; "Embedding shall have the lower priority and query processing the highest; the subsequent steps of a query in process shall be processed first to avoid wasting the effort spent for the previous steps." **FIX, part 3, the root (app/dispatch.py):**
- The levels are a query's steps in order: CORPUS < TRANSLATE < QUERY < RERANK < CHAT. 09 §9.12 is amended: the same five header values, a new order.
- Corpus work stands aside on the WHOLE CARD while any step of a person's query holds the node's claim. Its next request and its encoder's warm wait, bounded by the request's own timeout (then a deferral), and wake on stop.
- The device-wide yield removed on 2026-09-20 (chat's clock) returns, with the GPU governor holding the clock.
BEFORE numbers and the scenario are in scheduling-log.md (2026-10-25). Correction: the night's corpus work beside the reranker was GUTENBERG's on-demand embedding of the books the campaign's searches touched, at 1 text a request, 0.43 s each, two in flight. normattiva embedded nothing. The claim stays PER STEP: a whole-query claim (04b4f50, reverted in 1b8d8cc) would have idled the card for the 3.4–4.1 s of gaps per query, while a corpus request in a gap shares the card with the next step for at most ~0.43 s. Found before deploying it (München): **the GPU governor was not running on the node.** There was no TrueNAS app and no `app.gpu_governor` process, and the System status reported none. Its YAML records a deploy on 2026-09-20; it is not known since when it has been gone. So chat has run at the idle clock whenever the card was otherwise quiet. It is installed from deploy/truenas-gpu-governor.yaml before 7cc6975 (as 50030dc on c3a7612) goes out, with min_freq checked idle, during a search and after. Also: the test fake in test_rerank_per_collection.py now carries a status code, instead of product code reading one with a default.

### 2026-10-24: the family map revised on the node's first report — `built` split from `heritage`, `internal` a fallback, place's roots narrowed

The pack's version 3 on the node (München, 17:13–18:35 CEST, 82 min: class tree 1,256 s, codes 3,668 s; 5,231,012 P279 links; largest item Q141715864; map `f79007bd…`, 3.4 MB; codes 122 MB): scholarly 114,950 (+46,010,584 pruned), internal 9,677,031, heritage 5,808,872, event 1,472,201, person 13,736,759, organization 3,738,186, place 7,963,064, work 7,704,829, taxon 4,168,948, property 13,972, other 21,497,371 — summing to the manifest's 75,896,183. München's review found `other` unlisted, churches in place, legislation in internal, every built structure in heritage. Revised before `create` (families.py; wikidata-family-slices.md §5): `heritage` = monuments, archaeological and heritage sites and the designated; new `built`; `place` = settlements, administrative areas, landforms, bodies of water, protected areas; `internal` a fallback; the report lists `other`'s top 50 and probes 30 known classes. Next: the codes and report of the revised map on the node (~1 h CPU), reviewed, then `create`. *Done 2026-10-25: third revision; map `78399d60…` approved (29398cf).*

### 2026-10-24: wikipedia-v8's records build ran on ~1.3 of 4 cpus — the pool sized from the quota, the pages streamed

München's report (18:20Z): the build is not done by Monday 07:00 — 13.37 M of ~74.8 M records, ~120 rec/s, 4–6 days continuous; and only ~1 cpu busy. Their profile (py-spy, read-only) found both causes: `PARSE_PROCESSES` from `os.cpu_count()` = the host's 12 inside a 4-cpu quota (11 children), and a barrier per page of ≤500 documents with a timeout over the whole page (47.8 % of children's samples idle; 15 pages re-parsed serially in 18 h). Fixed in two separately measurable steps, the before numbers and scenario in scheduling-log.md: `config.available_cpus()` (affinity and the cgroup quota, as JVM/Go/.NET read it) and `parallel.compute_pages` (the next pages submitted while the caller writes this one; a progress watchdog, only unanswered parts recomputed). Byte-identical parts (the pool-vs-serial build test). Monday's wikidata does not wait for wikipedia-v8. The operator decided day running on weekdays (2026-10-24): the build runs 08:00–12:00 and 13:30–17:00 as well as 22:30–06:00 (node time, CEST/CET), and is stopped outside them (SIGTERM: it finishes the part, commits, exits 130, and resumes at the next window). Weekends are unchanged: the build runs all day. Set by München (TrueNAS cron, the quiet-hours pattern): /root/louise-scheduled/wikipedia-build.sh start|stop (stop = SIGTERM, then docker wait ≤ 15 min, logging the exit code and the last part written); cron 18 stops at 06:00, 12:00 and 17:00 Mon–Fri, 19 starts at 08:00, 20 at 13:30 and 22:30, and 21 is an idempotent start at 00:05 Sat and Sun (Friday 22:30's run continues to Monday 06:00). The first stop/resume (Mon 12:00 → 13:30) is checked: exit 130, and no part cut twice.

### 2026-10-24: the re-slicing daughter, built — a family added reuses every record and vector, equal to a build from scratch

The second half of the operator's requirement ("I agree"). `hold collection continue --reslice` (the slicing a creation gets now: the pack gives the map of the code's current family list over its class tree, built from the `subclass` table in a minute when the list changed); `dev carry --resliced` (amendment A1g: only the slicing differs and the target cuts by content — the manifest, text index and vector table carried, no snapshot compared); `hold records build <daughter> --records-from <mother>` (her parts from her mother's latest snapshot's record lines, indexed per part from each part's own trailing index, none parsed, each record cut into chunks again for `chunks_root`; refused unless only the slicing differs). Test: a mother built from the origin, a daughter with a family added (`city` before `place`) built from her record lines with the parser forbidden, and a collection built from scratch with the new map — the daughter's parts are the scratch build's, paths and bytes. Amendment: 02 §2.7. Full suite 5252 passed, 2 skipped.

### 2026-10-24: parts cut by content — a daughter with a new family map equals a collection built from scratch

Operator: "the structure must be such that if a new family is added a daughter allows you to reuse all the vectors and corpus of the mother. It must be functionally equivalent to a new collection created from scratch", then "I agree" to B: content-defined part boundaries in Monday's creation. A Codex whose slicing declares `parts: {rule: "content", min, max}` (wikidata's family block: 10,000 / 40,000 around 20,000) cuts each slice where a record's uid fingerprint (SHA-256, first 8 bytes) marks a boundary past `min`, or at `max` — content-defined chunking (LBFS; FastCDC's bounds), the record as the unit. The layout is a pure function of each slice's records: from scratch, resumed, updated or a daughter's, the same parts byte for byte; a change re-issues its part and at worst the next few; no closed parts to keep. The `records` check verifies each sampled part's cut. Amendments: 02 §2.2 (a record's slice is the one its fields give at each snapshot, for such a collection), 09 §9.3 `parts`, pack-format §2. Tests: purity and locality over 20,000 uids, the check, a family collection built whole and stopped-and-resumed after every part — the same parts. Full suite: 5249 passed; 33 errors and failures under parallel load in tests/vega, the GPU governor, run_check and quarantine, all 390 passing when run alone. Owed next (*done: af32643*): the daughter step itself — re-slice from the mother's record lines (no parse), carry the vector table across the new parts (no GPU), and a test that the daughter's parts equal a from-scratch build's.

### 2026-10-24: a family added later — every pinned map stays readable (defect found and fixed)

Operator: "What happens if a new family is created?" Answering it found a defect in 0f0c02f: `load_map` read only maps whose family list was the code's current one, so adding a family to the code would have made every pinned map — and every collection sliced by it — unreadable. Fixed before anything was built on it: a map carries its own family list (precedence order) and roots and is read by them; codes are per map; the code's list is only what a NEW map is written with. Map bytes unchanged, so München's upgrade ahead from e0cb788 stays valid. A new family itself: a new map — before publication a re-creation (family codes again, ~40 min CPU, and the records re-cut; vectors the same bytes, their carry across a re-cut owed when it first happens), after publication a new collection whose parts peers fetch again (wikidata-family-slices.md §7). Test: tests/test_plugins_wikidata_families.py (18).

### 2026-10-24: no official daughters so far (operator, RULE 1); the recommendations accepted

Operator: "No official daughters so far, rule 1. I accept your recommendations". While nothing official has been published, a change to what a collection is goes into a RE-CREATION carrying what is identical (the pack, `--calibration-from`, the vectors of unchanged chunks), never a daughter — CLAUDE.md RULE 1's examples, wikidata-family-slices.md §7. Accepted: the 99 GB version-1 pack beside the dump on tank is kept until Monday's records build has finished, then deleted (München does it); the family list is reviewed from the pack's report before `create`; scholarly articles, lexemes, the niche it/de/fr/es sections (after their pilot) and any root fix go into ONE later re-creation; embedding by family starts with heritage, events, places and people (phase 2, `embed_slices`); the reranker: the score stays unless run 5 shows otherwise — run 5 complete (496/496, 2026-10-24 23:04 CEST): no variant dominates (titles MRR score 0.834 / base 0.809 / slots 0.802; descriptive 0.412 / 0.422 / 0.419, R@10 0.714 / 0.687 / 0.701), so `score` stays (reranker-scope.md §14a). München runs the pack's version-3 upgrade ahead of Monday from e0cb788 (create's corpus lookup after the rename to `wikidata-p1`). *Done 2026-10-24.*

### 2026-10-24: wikidata sliced by family — built: pack version 3, the map pinned in the Codex and carried in the SHARD

Operator: "Go with A" (a family map pinned at creation). Built: app/plugins/wikidata/families.py — the rule (twelve families in precedence; an entity takes the first among its non-deprecated P31 values, a heritage designation makes it heritage; a CLASS takes its NEAREST root, a tie to precedence: precedence alone would have filed every municipality under organization, through "local government"), the map as canonical bytes named by their sha256, the walk of classes newer than the map, the slice `family=<f>/range=<n>`. The pack's version 3, upgraded in place from version 2 (RULE 1, no pass over the bz2): `subclass` (every item's P279 links) and each chunk's largest item, then the dump's own map (`families-<h>.json`), then every entity's code under it (`families-<h>.u8`, one byte an entity) and `families-<h>.report.json` (each family's records, pruned entities and the 25 classes that fill it most, with labels). Parsing does not wait for it (a version-2 pack whose names are done is enough); discovery does. `create` takes the slicing block and the map from the plugin (the pinned dump's pack; several dumps and no `snapshot` are refused in plain words) and writes the SHARD member `slicing/<sha256 hex>.json`; every snapshot lists it (09 §9.4), `share` moves it as the member `slicing` (09 §9.16); the records build reads the Codex's map from the SHARD, checks its hash and slices by the pack's codes (the parse is the fallback, through the same map); the `records` check verifies the member and each sampled record's slice (03 §3.2). A Codex sliced by id range — the wikidata of 2026-10-20 — keeps its ranges. A build never runs a second codes pass beside the worker's own (it waits). Amendments: pack-format.md §2, 09 §9.3 / §9.4 / §9.16, 03 §3.2. Tests: tests/test_plugins_wikidata_families.py (16, the end-to-end plan pass included). *(Upgrade and review done; Monday's order is now: deploy → `create` on map 78399d60 → records build.)* Monday's order on the node, as first planned: deploy → the worker's scan upgrades the pack (~1 h: the class tree ~18 min, the codes ~40 min, as the classes run measured its two passes) → READ the report before `create` (the family list is fixed for the collection's life) → `create` on the new target → the records build.

### 2026-10-24: wikidata's history section, in parser 2; scholarly articles and lexemes later; the slicing constraints written down

Operator: "Go with family slices and the history section now", then "Go with 3" (scholarly articles and lexemes join later, not in Monday's re-creation) "also if it makes sense choose which languages to fetch and embed", then "explain the slicing constraints and future proof constraints". The history section: after each language's text, what the best-rank filter drops (a property's normal-rank statements when it has a preferred one; not quantities, not external ids; each with its qualifiers; at most ten a property, the most recent first), headed `en:history`, under "Other statements include" — the English text stays byte for byte (`textify.history_text`; the word index files it with English, `fts.by_section_language`). Parser 2 still: nothing was built with it. The constraints, and the languages answer (fetch: no choice, a record keeps every language; embed: English now, the niche it/de/fr/es sections with parser 3; no `lang=` slice key — announced, then withdrawn: it would be `mul` for every item): [wikidata-family-slices.md](wikidata-family-slices.md) §7. The binding one: the family list is fixed at creation, so its sizes are reviewed before `create`.

### 2026-10-24: Perugia — gutenberg's records-vs-origin PASSED over the network; quarantine cleared

The real origin check the operator asked for (2026-10-23: no `ack --origin-unchecked`), made possible by c3b9d79's network re-fetch for rsync origins. On b3ac1be (full suite 5149 passed; smoke clean; qwen3-4b bit-identical), 12:27–12:50Z: PASS, 87 records re-parsed from the origin, 0 differing, 84 moved (the mirror regenerates book folders — identical but for origin_hash, counted as reproduced, 03 §3.2 as amended 2026-10-07), reading 87 of 4,100 origin units, 241 MiB of the 256 MiB a check may read (operator, 2026-09-20). One run's depth: 0 of 87 bounds the per-record failure rate at ~3.4 % (95 %); seeds differ per run and per peer, and `--origin-mib` raises it for a deeper run. Copy state: in use.

### 2026-10-24: wikidata sliced by family with a map pinned at creation (operator: A); the history section; nothing pruned

Operator: "Propose a way to choose what to embed like it is currently done for Wikipedia language fetching/embedding … If not written explicitly in the guidelines a collection cannot prune any information from the corpus" (CLAUDE.md, invariant 11; and "Always give a pros cons", Style). The Wikimedia-pages pruning proposal is withdrawn: they stay records, and what a node embeds is chosen by slice, as Wikipedia by language. Family slices met the rule that a slice is stable for a record's life (pack-format.md §2): a class tree is not. Option A — the map of class to family pinned at creation, an amendment of the slicing rule — chosen ("Go with A, but write down today's report"): [wikidata-family-slices.md](wikidata-family-slices.md). With the history section, both go into Monday's re-creation; its records build moves by about a day. Wiktionary and Wikidata's lexemes written down as collections to come ([future-collections.md](future-collections.md)).

### 2026-10-24: what the wikidata text dropped or misread — four fixes in parser 2, three things counted

Operator: "check if there is any other information from wikidata that we silently ignore like the original language". Fixed in parser 2, before any record: `novalue` was written "has <property>", the opposite (upstream; now "no value", Wikibase's wording), `somevalue` lost its qualifiers (an unknown date's bounds), a lexeme value was written as a Python dict (upstream), a quantity lost its ± (our formatter). Counted in the next classes run: the statements the best-rank filter drops (former countries, names, populations), references to merged or deleted entities (the JSON dump has no redirects), Wikimedia's own pages kept as records (categories, templates, disambiguations). Upstream items W6, W7 in the report. The table: wikidata-build-plan.md, last section. The node's pack is at version 2 since 12:00 CEST (in-place upgrade, 37 min 38 s: 2,215 language codes, 17,954,384 names; RULE 1 against an hours-long rebuild). wikidata-refs version 3 synced from it: 121,724,461 rows in 817 s (refs.db 11.16 → 12.62 GB); the 182,531 entities with no label at all have no row. `ask lookup`: Q42 "Douglas Adams" (its label is `mul` in this dump), Q3235171 "Les Roses de la vie" (fr), Q3242104 "Liouberetskaya" (fr, the Latin label over the Russian). The operator also approved the niche selection (a language's section only where Wikidata says more in it than in English) and the pilot before the full embedding.

### 2026-10-24: monuments, buildings and events measured — 10.4 M entities, 1.6 G tokens a language; the occurrence tree is polluted

`tools/wikidata_coverage.py classes` on the node (51 min, read-only): 10,437,567 entities selected (built structure 5.64 M, monument 0.67 M, archaeological site 0.36 M, occurrence 3.90 M of which planned event 1.44 M, heritage designation alone 0.51 M). Wikidata names them in Italian (label or mul) for 11.6 %, German 25.3 %, French 21.2 %, Spanish 12.7 %; their texts' other labels exist in Italian for 86 %. One more language for all: 1.61 G tokens, 3.7 months of wikidata's budget; only where Wikidata names the entity in that language: 1.14 G tokens for all four. Barbacane/barbican and merlon are in the built-structure tree; machicolation needs architectural element (Q391414). The occurrence tree brings in clinical trials, theses, statutory instruments, stars and coins (W5 of upstream-reports/wikidata.md; W3 six P279 cycles, W4 the missing-label worklists, written). Next: a second run with the event roots' chains; the operator's languages decision. *Both done (b3ac1be; languages decided 2026-10-24).* And a pack of NEWER code is never started over (München's safety check: the deployed version-1 worker would have unlinked the version-2 pack).

### 2026-10-24: wikidata — no id where a name exists (operator); the pack's `name` table, upgraded in place

Operator: "As a general rule showing an id is meaningless for humans; in case there are multiple non English languages use in order: 1. the original name if it can be determined and if it's in latin characters 2. Any other language in latin characters. 3. The name in the original language if it can be determined." names.py — the original name from the entity's name statements (P1705/P1559/P1448/P1476/P2561), then its label in its original language (P407/P364/P103 through each language item's code, P424: the language lookup); Latin by Unicode script; among labels the entity's own wikis, then French, Spanish, Italian, German, then Wikidata's listing order (operator: "Go with your recommendation and the language lookup"); any label before an id — in parser 2: the title (20.8 % of the records were titled by their id), the entity's own name in its text, every referenced name, and `ask lookup` (wikidata-refs ref_version 3). The pack (PACK_VERSION 2) gains `language` (written with the chunks) and `name` (a pass over its own lines after them); a complete version-1 pack is upgraded in place by two such passes, not rebuilt from the bz2 (RULE 1); parser 2 parses only from a pack whose names are done, every document deferring before that. wikidata-build-plan.md "No id where a name exists".

### 2026-10-24: wikidata parser 2 — a referenced label falls back to `mul`; the collection is created again before its records

The vendored textifier skipped upstream's `mul` step for REFERENCED entities (the entity's own label had it): an entity labelled only in `mul`, Wikidata's default label since 2024, was written as its id in the text of everything naming it. Fixed as parser 2 (textify.py `Normalizer._chain`: language, `mul`, fallback — upstream's `get_lang_val`), with Italian connectives inert for English. The Codex of 2026-10-20 names parser 1 and nothing was published, so wikidata is created again from the same recipe before Monday's records build (RULE 1: calibration document side reused with `--calibration-from`, the pack kept, the text index rebuilt as already planned; wikidata-build-plan.md "Parser 2"). Counted on the node (`tools/wikidata_coverage.py mul`, a clustered sample of 8,823 kept entities): 280 English texts change (3.2 %), ids written 870 → 506 of 155,247 label lookups — "PlayStation 2", taxa named by their scientific `mul` label. Titled by their id (no English, no `mul` label), exactly over the manifest: 15,765,162 of 75,896,183 records, 20.8 % (about one in five; "one in four" was read off 20 entities).

### 2026-10-24: monuments, buildings and events in more languages — designed, measured first; a standing report to the Wikidata team

Operator: "I want that also monuments, buildings and events are full text translated as well / As ancient and uncommon words (barbacane, barbican) if it fits / Any improvement to wikidata goes in a report to be submitted to the wikidata team for integration." A daughter after the English build; per added language, the textifier fed Wikidata's own labels (no machine translation: records reproduce from the dump); the selection is the P279 closure of architectural structure, monument, archaeological site, event and occurrence, their classes (the vocabulary: barbican/barbacane), and heritage designations (wikidata-build-plan.md, last section). `tools/wikidata_coverage.py classes` counts it on the node first. The report: [upstream-reports/wikidata.md](upstream-reports/wikidata.md). Open: the languages (recommended it, de, fr, es). *Decided 2026-10-24.*

### 2026-10-24: wikidata is searched by name in every language (operator: "names only")

"Go with names only, before Monday's records build." Every label and alias of an entity, in every language (`index_names`, from the record's `extra`, which keeps the whole entity), is indexed as whole-name keys on the record's first chunk, with its popularity (sitelinks). `fts.search_names` matches the question's word sequences (up to 8 words) against them — the longer name first, then the most linked: Wikidata's own near match and rescore (WikibaseCirrusSearch), so a famous entity is not outranked by an obscure one of the same name, as BM25 over a bag of every name would rank it. Keys are `|name` and `<lang>|name`: a search limited to languages matches names in those languages. One more list of the text leg, from the reader's own words (05 §5.1 amended). The text stage takes chunks and names from ONE parse (parse pool kind `chunks+names`). Not in records, sections, `content_hash` or vectors, so the records build is unchanged; an erase-rewrite refuses a names index (names are not stored). Owed on the node with Monday's rebuild: the index's size and `search_names`' time on the real index (CLAUDE.md: time every new hot-path query). Not done: the catalogue (title list) keeps the English title; a title in another language for the 1 in 4 entities with no English label would change the records (title is in `content_hash`).

### 2026-10-24: a multilingual record is indexed by section language (wikidata)

The node's wikidata (75,896,183 documents, all `lang = "mul"` by design: an entity has no one language, wikidata-build-plan.md) sat whole in the `other` text group: its English text unstemmed, and every language-limited search ("in English") skipped it, since `other` is searched only for a language that maps there. `fts.by_section_language`: a `mul` record's sections are each indexed in the group of the language their heading names (wikidata's chunker puts the section's language in `chapter`), with that language stored — ONLY a language the plugin declares (`fts.section_languages`: wikidata `("en",)`; no other plugin declares any). The first form took any two or three letters for a code and would have filed pubmed's "AIM" sections and gutenberg's "II" chapters under fake languages; caught the same day, before any node ran it. Any other heading keeps `mul` in `other`, as before; a delete removes every part. The records keep `mul` (correct, and unchanged): only the local text index moves. Owed on the node: wikidata's text index rebuilt (its `other` group dropped, fts re-run), with the records build, Monday. The catalogue and vector legs, which filter by the RECORD's language, keep a `mul` record when its plugin writes one of the asked languages (`retrieval.limited_langs`; only wikidata declares `languages`, `("en",)`), operator 2026-10-24: "Fix the language defect".

### 2026-10-23: annotations collections are updated, not replaced (operator)

"Implement docs/notes/annotation-collections.md immediately … Annotations must be updated not replaced." `hold annotations update` / the API route / the Library card's *re-check claims* and *update with a claims file*: the next snapshot of the same collection, its claims re-checked against the targets' newest snapshots (records twin included), kept claims keeping their uids, left-out ones tombstoned, superseded parts and the replaced claims file kept (annotation-collections.md "Updating"; 02 §2.10, 07 §7.3, 09 §9.4 amended). Found on the way, and fixed: a peer never followed ANY collection's next snapshot — the pull acted on `snapshot` events that no code emits; it now follows what its neighbours' digests list (`accept.newer_from_digests`), annotations collections included. One update a period (monthly for gutenberg-book-editions: its next is 2026-10, written now; the one after from November).

### 2026-10-23: a copy fetched as a SHARD checks its origin over the network (operator)

Perugia's gutenberg (fetched from the node, no corpus) sat in report-only quarantine with `records-vs-origin` inconclusive for want of an origin. Operator: no `ack --origin-unchecked`; "it will be cleared when the origin is checked". `app/acquire/file_origin.py`: with no `--origin`, a Codex whose spec is one directory per document over rsync or FTP (gutenberg) has its sampled documents' directories listed on the spec's mirrors (first that lists, mirrors first) and their admitted files fetched by acquisition's own transport and budget into a temporary base, removed at exit; a SIGTERM (the scheduled check's stop) ends rsync's group rather than stranding it. The check's 256 MiB bound decides how much: units are weighed by listing only when the bound reaches them, and 8 unlisted in a row end the asking (the origin is away: inconclusive, as before). The scheduled check already omits `--origin` for a shard-sourced copy, so Perugia's next scheduled check runs it — nothing to do there. pubmed (container files) and wikipedia (dumps) are not served: their units are files, and a file's listing is the transport's to add.

### 2026-10-23: processing pins its encoder, and restores it after (operator)

"Model eviction during processing shall be forbidden … reading the pinning status, in case pinning a model before processing, then restoring the previous pinning status after processing." LocalAI's reclaimer evicted vega-qwen3-embedding-4b 129 times in 72 h between batches (localai-bugs.md B3/B4/B7/B11). `app/model_pins.py`: the first holder reads `pinned` (`GET /api/models/config-json/<m>`), pins if unpinned (`PUT /models/toggle-pinned/<m>/pin`), the last holder restores; a model pinned before is never touched. Shared by every process through `state/model-pins.json` under a lock. Holders: the embed stage from its first document to PIN_IDLE_S (120 s) idle, never released mid-document; the publisher's pass; the calibration gate and the re-check; every chunk or calibration embed of any process (`embeddings._pin`, released at exit or idle). Queries (`embed_query`) are never pinned. Any model LocalAI serves, by USE not by kind (München: the pin API is every model's): a reranker or the chat model too when a job makes many calls — a book's summary holds the chat model from its first chapter to its reduce; a single call (a query's rerank, translation or answer) needs no pin, since LocalAI never evicts a busy model. A `holding` block beats every 60 s, so an hour-long job is never taken for dead. tools/measure/classical_guard.py reads pins LIVE before it shuts down an idle model of Louise's (its static list would have shut down an encoder mid-embed). Dead holders: dropped after HOLDER_TTL_S (900 s), a killed worker's at the next boot (reported). "Failed pin calls: monitor such event, it shouldn't happen unless there is some bug": every failed read/pin/unpin/restore is an error event (stage `models`) naming model, call, status and holder; counted in the record, shown on the System page ("Model pins"), in `hold collection show` (`model_pin_failures`) and in the worker's telemetry (`worker_model_pins`); processing goes on unpinned, asked again in 60 s. A failed restore stays listed and is retried every settle beat. With this, the staged YAMLs are the UNPINNED baseline (a819c6b, deployed 11:08). A static pin handed over to the mechanism is UNPINNED BEFORE the worker starts: the worker read prior=true for the two still pinned and would have left them pinned (München repaired the record by hand). Found live the same hour: the worker's per-process holder was stamped at its call's START, so a call over 120 s could be dropped under itself, and its idle releases were logged as "silent for over 900 s"; a call in flight is now alive whatever its age, the idle clock starts at its end, and an idle release says so.

### 2026-10-23: bge-m3's context trimmed to its spaces' 1024 (operator)

"Trim bge-m3 context to the needed one when it fits the other ongoing activities, remember rule 1." vega-bge-m3 runs context_size 8192 for wikipedia's ctx-1024 spaces (8x the KV it needs); every other model's context already matched its space (München, from the deployed YAMLs, 2026-10-23; the chat model's 16384 is its answer window's worst case and stays). The trim is a new engine RUN, not a new space: nothing is re-embedded or re-minted (RULE 1); it is validated once — bit-exact over fresh loads and orders (invariant 12), then the calibration gate (03 §3.3) — before it writes. Scheduled by München around the records build.

### 2026-10-23: a restart is explained before anything resumes (operator)

"To stay on the safe side, in the future track why system restarts." Perugia's PC restarted twice in the night (Windows Update), killing the gutenberg adoption and a gate mid-run; nothing was lost (staging empty, every step resumable). Standing practice for every session driving a machine: on finding it restarted (short uptime, tasks ended together, containers just up), read WHY before resuming and report when, why and what was interrupted. Windows: `Get-WinEvent -FilterHashtable @{LogName='System'; Id=1074,1076,6005,6006,6008,41}` (1074 names the initiator — TrustedInstaller = Windows Update; 6008/41 = unexpected); Linux/TrueNAS: `last -x reboot shutdown`, `journalctl --list-boots`, `journalctl -b -1 -n 50`. Read-only; Windows Update settings are the operator's decision. München's PC restarted three times for Windows Update too (03:44–03:47, 1074 MoUsoCoreWorker/TrustedInstaller) and lost its session's in-memory wake-up, which is why the 06:03 deploy never fired: anything that must happen AT A TIME runs node-side (a sleep-then-exec script, a TrueNAS cron), never in a session timer alone. Perugia's two interruptions had two causes: a Windows Update reboot at 07:32 local while logged out (operator-confirmed; updates downloaded just before, no 1074 recorded — a reboot while logged out may leave none). After such a reboot Docker Desktop starts only at login, so a Windows peer is DOWN — no seeding, no fetch — from the reboot until someone logs in and, at 08:56 local, the operator updating Docker Desktop by hand, which restarted every container without a reboot (its updater log reads the same as an automatic update: ask before concluding which) — so also check `%LOCALAPPDATA%\Docker\log\host\` when containers restart together and uptime did not reset.

### 2026-10-22: knowledge-base pipelines — specified, WAITING (operator)

Proposal `notes/kb-pipelines.md` (D1 fusing spaces by rank, D2 pipelines in Louise TOML, D3 upstream after); specs `notes/kb-pipelines-spec.md` and `notes/kb-protocol-extensions.md`, both DRAFT. Operator: wait for the end of the current developments, then summarize what is planned and decide. Nothing built.

### 2026-10-22: users, tokens and Connect (operator)

"A search-only gui (aka user gui). A user token giving only access to the
ask tab", then "connect Louise to OpenWebUI, or agents". Built: `louise user
add | list | role | expire | reissue | revoke | remove` and the admin API
(07 §7.4 amended), `users.toml` holding token hashes only (§7.8), Ask
closed once the first active user exists, the token as the bearer key of
`/v1`, `/api/ask` and MCP, `/v1/models` naming `louise` and
`louise/<collection>`, MCP's Streamable HTTP answers (202, batch, GET 405),
and `/connect` filled in per viewer. Open: a live check from Open WebUI
(OpenAI connection and MCP tool) and from one MCP agent — neither was run
against a real client yet. Then the Open WebUI pipe (`deploy/openwebui/louise_pipe.py`): one model per collection, each person's collections, annotations, answer language and result count as Valves, one search per question (`sources: true`), the hits as Open WebUI sources linked to the reader. Then OpenAI's vector stores and files API (`app/openai_stores.py`, 07 §7.4): curated collections read-only, deposit collections writable; checked with OpenAI's own SDK in a scratch install (not a dependency).

### 2026-10-21: normattiva's vectors re-embedded bit-exact (DAR-legal-it)

`normattiva`'s vectors are re-embedded with its DAR-legal-it encoder in the bit-exact configuration, in the node's quiet hours (77148be, b2f3dd6). Its `records-vs-origin` re-run is still owed.

### 2026-10-21: NVIDIA support, before any peer has a card (operator)

[nvidia-support.md](nvidia-support.md). The defect it closes: VeGa read a
run's card from DRM fdinfo, which a CUDA process does not have, so a run on an
NVIDIA card would have been STATED AS A CPU RUN. Now the card is read from the
/dev/nvidia<N> the backend holds plus nvidia-smi's used memory around the
load (WSL2: /dev/dxg, one named card), the driver from the bound module (WSL2:
`dxg nvidia <ver>`), the runtime as the hashed CUDA libraries the backend
mapped; MIG mode states no run. Contract: llama.cpp `parallel:1`; torch adds
`CUBLAS_WORKSPACE_CONFIG=:4096:8` and cuDNN deterministic. Deploy:
deploy/localai-nvidia (LocalAI CUDA 13 by digest, YAMLs tested against the
wrapper), louise-python `VARIANT=cuda`. Floor: driver R580, Turing. Not
measured on a card: the first one runs README step 6 before it embeds.

### 2026-10-20: T2 is T0's envelope tie-breaker

Four of the node's own `gutenberg` parts (lang=en/00043 twice, 00219,
00220) were refused by T0's envelope in the night of 2026-10-19/20 —
homogeneous parts, 2nd percentile 0.289–0.392, one cosine 0.4981 — with
T2 passing on every one
([p2p-review-open-issues.md](p2p-review-open-issues.md) §16). Operator
decision: an envelope failure on a bundle whose T2 passes is a WARNING
recorded in the report, not a refusal (03 §3.5 amended;
`app/hold/admit.py`). The literals are not widened; T2 SKIPPED and a
structural T0 failure still refuse. The four parts re-ladder at the
publisher's next pass after the deploy.

### 2026-10-20: the bounty list has no leader (operator); a peer leads only where it embeds and shares

Two corrections to how open parts are shared, both from the first full p2p
day (gutenberg, node + Perugia + the dev PC's idle peer). (1) The digest
carries a space's benchmark only for collections with `embed = p2p | all`
and `share = true`; a neighbour offering no member of a collection is not
scored for it; a mode or sharing switch sends a digest at once — the idle
peer led 355 open parts by a benchmark alone, the on-demand node 485 more,
all waiting a seven-day grace window. (2) The operator's correction of the
model itself: "for the bounty list THERE IS NO LEADER" — any peer may embed
any open part; rendezvous is the ORDER a peer walks the list, widened one
rank when a round finds nothing (`leading.widen`), and the only part left
aside is one another producer is visibly on (`contribute.visibly_on`, a
bundle within `3 × tokens / w`). Then the operator's rule of the same
afternoon: a peer works at most TWO parts at a time — the first two of its
order (`contribute.job_order`, `leading.JOBS`), the next admitted the
moment one yields nothing, so the card never idles between jobs and the
others see it on at most two parts. The `grace_days` cascade on embedding is
gone; `leader` in `hold vectors show` is the first of the ordering,
information only. 04 §4.5, 07 §7.8, 09 §9.9, §9.10 amended;
[scheduling-log.md](scheduling-log.md) has the measured BEFORE and the
numbers owed AFTER. Also today: a landed bundle marks its part's documents
embedded (Perugia's card read 57 % with 93 % of the vectors held), and the
offer says when a collection cannot be taken yet (no snapshot served).

### 2026-10-20, night: what is open for the morning

Perugia is off for the night (operator); the node runs. Gutenberg's open
parts get no new vectors meanwhile (the node's gutenberg is on-demand).
1. *(Done: Perugia's copy in use, 2026-10-24.)* Perugia (on 274d461, gutenberg p2p, adoption NOT run) deploys 0a2c6ef —
   PASSED 4,898 and live on the node since 22:41:58 CEST — or a later
   PASSED tip, then adopts the node's twin d913cceb
   (deploy/16f/adopt-gutenberg-twin.sh, seed-only across the run, no
   scheduled check in progress); `quarantine_in_use_before` only if its own
   checks have not run.
2. *(Done: run 4, 902ceb4; run 5 kept `score`.)* The reranker regression on titles (MRR 0.738 before it, 0.579 after):
   München measures (a) a catalogue rank-1 kept on top, (b) RRF of the
   reranker's order with the fused one (`retrieval.RERANK_FUSE`, dc36f1a,
   off), (c) the head line weighted — titles, then descriptive; the switch
   moves only on those numbers.
3. The ARC cap at 16 GiB: c_max and MemAvailable after, from München. *(Still not reported.)*
4. *(Superseded by the pack's re-creation.)* Wikidata's indexing rate from the NVMe pack, from 22:42.
5. This session's messages to the peers are refused (no trusted-device
   proof); the operator relays them.

### 2026-10-20: the node's ZFS ARC capped at 16 GiB (operator)

During the 99 GB copy of wikidata's pack to the fast drive the ARC, uncapped
(`zfs_arc_max = 0` on a 61 GB host), held 34 GiB and left 0.4 GiB available;
Qwen3's and bge-m3's loads failed at 22:01–22:02 CEST (backend exit −1, a
cooldown) and gutenberg's vector leg was withheld for minutes (München).
Operator, 22:5x: "Cap at 16GB" — `zfs_arc_max = 17179869184`, set at
runtime and as a TrueNAS post-init command so it survives a reboot. Owed
from München: c_max and MemAvailable after, and the time.

### 2026-10-20: a fast drive for derived data — LOUISE_CACHE_DIR (operator)

"A cache folder on a fast drive, like nvme or ssd, to be used for such
operations." München measured wikidata's parse at ~95 % label lookups in
labels.sqlite beside the dump on tank (HDD): 133–179 entities/s a core
cold while the scan writes, 430 warmed, 1,763 from RAM; phase 2 is ~78 M
documents. `LOUISE_CACHE_DIR` (config.CACHE_DIR, identity tier, in the
parse pool's knobs) names the drive; `app/derived.py` places every
plugin's derived cache under `<kind>/<tag of the origin's resolved path>/`
and carries an existing cache in once (`adopt`: whole or not at all,
`*.tmp` left out, the original left). pubmed's pack and wikipedia's
page-props pack follow it here; the wikidata pack is München's (pack_dir
through `derived.place`), with the node's env `LOUISE_CACHE_DIR=/data/cache`
(NVMe, 571 GB free) at the scan's end. Perugia leaves it unset (operator:
an all-NVMe PC gains nothing from a second place for the same cache). 07
§7.8 and CLAUDE.md invariant 6 amended.

### 2026-10-20: a refused own export is set aside and not retried for a day (München's facts on the node)

The node published en/00219 seven times and en/00043 eight times in ten
hours: its own ladder refused each export at T0 (the homogeneous-envelope
clause, a warning since 4e63103), the refusal left the part open and the
row at 0 chunks, every publisher pass exported it again under a new id,
and every refused copy sat in `state/bundles/`, which the seeder serves
and the digest offers whole — "not announced" in the log, fetched and
compared by Perugia all the same (26 copies). Now a refused export goes to
`state/share/refused/…` (`contribute.refused_dir`), refused copies of
earlier passes are swept there at the next pass (`sweep_refused`), and a
part whose own export was refused is not exported again for
`REFUSED_RETRY_S` (24 h) — the vectors stay for `hold vectors check`. A
part genuinely failing T0 now costs one copy a day, not one a pass. 04
§4.5 amended. Deployed on the node 17:17:38 CEST (678450f): the sweep set
27 aside — and the seeder's next sync DELETED them: `seeder.sync` removes
every directory under `state/share/` but `torrents` and the current
snapshot's, and `share --stop` removes the tree whole (München). The 27
were refused duplicates (vectors in the table, rows keep the refusal);
München moved the directory to `state/refused-bundles/` (cf430b5), under
neither seeder-owned tree, test extended. Known limit: a refused row whose
directory is gone carries no time, so the day's hold does not apply to it
— the two parts concerned are closed, so nothing re-exports today.

### 2026-10-20: an accepted bundle without its files is taken again, never re-signed (Perugia's find)

en/01192 on Perugia: a node bundle of 6,924 chunks accepted 2026-10-19 on
the compare path (every chunk held already) had no files; the union read
963 of 7,676 — Perugia's own re-signing of the node's rows — and the walk
would have re-embedded 6,713 chunks the node holds; 4 such parts, 45,197
chunks. Now `accept.files_here` decides: the fetch beat lists and takes
such a bundle again (`listed_bundles`, `take`), and the publisher leaves a
part with another producer's fileless row to that fetch
(`contribute.unreadable_bundles`), re-signing nothing; this peer's own
fileless rows claim nothing, as before. 04 §4.5 amended. Perugia's deploy
(15:14Z) showed the re-take working — 60705a18 brought 5,570 chunks this
peer did not hold — and a defect in the fix: the union cache, keyed on the
part's bundle ids, never saw the files arrive under the same id, so
en/01192 stayed at 963 and open; the entry now records which bundles were
unreadable and whether their file existed, and is recomputed when that
changes (`counting._entry_stands`). After the recompute en/01192 reads
6,931 — the node's 6,924 and 7 more of Perugia's 963. The 956 shared
chunks are NOT re-signed rows: Perugia's walk embedded them itself while
the fileless row made the part look open (its 9b66fe5b against the node's
60705a18: 0 of 972 rows bit-identical, cosine min 0.997850, median
0.999051) — an independent duplicate, a check (04 §4.5), the GPU time the
defect cost. `dev deprecate-duplicates` now also finds a SUBSET copy
(every record and chunk row in an earlier bundle of another producer,
vector for vector, 274d461) — and rightly left this one alone.
The node's seven copies of en/00219: the entry below.

### 2026-10-20: a taken copy tests itself as its parts land (Perugia's find)

gutenberg-book-editions, taken at 11:59Z, read "quarantine (reported only)"
for 2.5 h: "roots not yet run on this peer — cleared by `louise hold check
…`", and nothing was going to run it. Now the fetch beat runs the copy's
own `roots`, `records` and (with a space) `chunks` over each part it
promotes and writes the attestations (`check.attest_landed`,
`transport._attest_landed`); the cost is the parts landed, never the
collection. 03 §3.6 amended. `hold check` by hand stays the same check.

### 2026-10-20: the Library's remove — forget a copy, retire a build (operator's bug on Perugia)

A collection taken from the offer, removed in the Library and taken again
under another name was refused: "already held here as 'annotations'". The
Library's remove only RETIRED the file and kept `collections/<name>/shard/`
with its Codex, which `write` reads as held. Now the Library asks, in plain
words, and defaults by `source`: a copy taken from peers (`source =
"shard"`) is FORGOTTEN — `hold collection forget` through the worker, the
copy and its state gone, takeable again under any name; a collection built
here is retired as before, restorable by renaming the file back, its id
bound to the name meanwhile. The refusal names the way out. The take dialog
of the same day asks who computes the vectors (off | on-demand | all | p2p)
and requires the encoder, which a question needs as much as an embedding.

### 2026-10-20: the wikidata collection EXISTS on the node

Created 10:39:28–10:57:00Z by München from recipe 1de5d1c
([wikidata-recipe.toml](wikidata-recipe.toml)), after five refused or
pre-checked attempts of the day (no calibration set; no `--query-encoder`;
a label on the query encoder; no origin block; `sample = 512`). The create
now pre-checks the recipe before any embedding (2184639).
`collection_id sha256:ed34502a2a6bf131131019e0a4415b53ea219a7cf36338a95c3ee2c8fb8d176d`,
`space_id sha256:bb514f97156a252c14d853e86f520e477cff7bcdfa697420f705544616382724`,
platform image `sha256:297f2fb5…` (ef2720a). Held file
`collections/wikidata.toml`: kinds records + vectors, embed off, share off,
`[encoder]` VeGa at 192.168.178.10:30286, vega-jina-v3 / vega-jina-v3-query;
`snapshot = "20261012"` added by hand (the Codex's origin.spec pins no
files, so the file names the dump). Quarantine until the first snapshot and
the gate. Next (*done*): `hold records fetch wikidata --profile all` (103.4 GB, the
pack and the label table in one streaming pass), then the first snapshot,
then gutenberg-graph's steps 4–5 against it.

### 2026-10-20: pitfall 18 measured on Perugia — the embed stage back at CLI throughput under heavy intake

62a098d, 08:55–09:55Z: 138 chunks/min (61 before, CLI 143) while 199
bundles landed, judged → accepted 14 s against 18 min 28 s; the busy
threads are LanceDB's native writers, not `share-fetch`
([scheduling-log.md](scheduling-log.md), the pitfall-18 row). The cut
stays in the intake thread.

### 2026-10-20: gutenberg-graph, step 2a — Codex version 9, the three claim types, several targets

Built on step 1's amendments: `app/hold/codex.py` reads and writes version 9
(`slicing.container`, `annotates` as a list — `targets_of` is the one reader),
`app/hold/annotations.py` checks and pins `mention`, `relation` and
`community` claims (a chunk end re-cut with the target's chunker and
compared to its `text_hash`; an entity's canonical id mapped to its record
through the plugin's id rule), writes 09 §9.14's three edges and takes
several targets at create (CLI, API, job, Library card). Parquet parts
(step 2b) are written and read: `app/pack/records.py` behind the same API,
the part named from the Codex's `slicing.container` by every reader (the
snapshot entry learns it from the `codex.json` beside the snapshot, no wire
key), a recipe may declare it for any collection. Nothing already written moves: a one-target JSONL collection of the two
2026-10-09 types is still version 7, byte for byte
([gutenberg-graph-amendments.md](gutenberg-graph-amendments.md), "Decided").
Tests: tests/hold/test_annotations.py (+6), test_codex.py and
test_backend_engine.py (version 10 is the unknown one now).

### 2026-10-20: gutenberg-graph, step 1 — the specification amendments written

The operator's decisions of the day (two targets, Parquet parts for every
collection, a measured agreement floor) are in the documents, dated: 09
§9.3 Codex version 9 (`annotates` as a list, `slicing.container`), §9.5
the `mention`, `relation` and `community` claims with their keys and the
`model` method, "The file (B): Parquet" beside the zstd frames, §9.14 the
three edges, §9.18; 02 §2.10; 05 §5.3 and §5.5; 03 §3.2 `model-agreement`
([gutenberg-graph-amendments.md](gutenberg-graph-amendments.md), "The
order"). Nothing is built yet: step 2 is app/hold/annotations.py and the
Parquet parts, step 3 the Ask side, steps 4–5 the methods once the wikidata
collection exists.

### 2026-10-20: jina-v3 reproduces its own bytes with torch's deterministic switch; the wikidata recipe is filled

München, on the third louise-python tree (-20261020c): with torch's
defaults 204 of 512 calibration texts came back bit-identical over two
fresh loads and two shuffled orders — the short texts differed; with
`LOUISE_TORCH_DETERMINISTIC=1` (the task's switch, in the model's env, so the
stated run carries it) the document model is 512/512. Throughput 2,016
tokens/s warm. docs/notes/wikidata-recipe.toml is filled from the two
stated runs byte for byte (document `bec9c3a0…`, query `dd90f8d4…`, image
`1807f280…`) and `work.tokens` set to 1.7 G a month from the measured rate
at eight hours a day — the one chosen number, inside collection_id, for
the operator to confirm. Create follows the query model's 512/512. *Decided: 430 M tokens a month (2 h a day).*

### 2026-10-20: pubmed-v8 is `pubmed` (operator; München, 08:24–08:35Z)

Its 39,994,988 documents all done (rdf, fts, embed), as the operator's
condition asked. `hold collection rename pubmed-v8 pubmed`, exit 0, 11 min:
the old v6 `pubmed` set aside first — `/data/collections/pubmed` →
`pubmed-v6-retired`, `/shards/pubmed` (40 GB) → `/shards/pubmed-v6-retired`,
its shard link re-pointed, nothing deleted — then directory, shard, bundles
and held file moved, 39,994,988 `documents.path` and 2,668
`fetch_files.local_path` rewritten in one transaction (the manifest's WAL
is 24.8 GB after it; the next writer's checkpoints fold it back, a TRUNCATE
checkpoint while paused gives the space back; ssd4 has ~570 GB free). The
pause moved with it: `pubmed` is embed "all", PAUSED, for the operator to
resume; cron 7/8 name `pubmed` and are disabled until the operator
re-enables them. Its file keeps `share = false` until the 40 M-chunk
publish is scheduled.

### 2026-10-20: the offer — a peer chooses what it fetches (operator)

`app/share/offer.py`: the catalogue minus what is held, each entry with
the Codex's own words and the live neighbours serving it; `louise share
offer`, `GET /api/share/offer/show`, and the Library's "Offered by peers"
with a *take…* dialog that queues `share fetch` for the worker
(`app/jobs.py` SHARE_FETCH, every mode; `POST /api/share/fetch`) with the
slices, kinds and encoder chosen. 04 §4.2 and 07 §7.3/§7.4 amended.
"Make every collection shareable" (operator, the same day) is the control,
not the default: every Codex collection's card already carries the
*shared* toggle, and the default stays OFF — a default of on was built
and taken back the same hour on the operator's word ("share off by
default is fine"); nothing deployed. The decisions of the day beside it: the old jina folder was deleted by the operator on the node
(München's auto mode refuses irreversible deletes outright, no prompt); gutenberg-graph
gets two targets (Codex version 9), Parquet parts for EVERY collection, and
a measured agreement floor (gutenberg-graph-amendments.md); pubmed-v8's
40 M unbundled chunks publish after the wikidata work.

### 2026-10-20: the bundle intake's quadratic hard split (pitfall 18)

Perugia's py-spy dump on its first morning as a full peer: the worker's
`share-fetch` thread at 100 % CPU, 80 % system, in `_fixed_windows`'s
hard split — the remainder of a long paragraph re-sliced once per window,
1.7 GB of fresh strings for one 3.4 MB book, 18.5 min per 50-book part on
Docker Desktop over WSL2 — the GIL held, the embed producer starved, the
GPU idle (61 chunks/min against the CLI's 143). Fixed by cutting by index
(byte-identical, the old loop kept as the test's oracle; 0.136 → 0.005 s
here) and by landing from the intake's one cut instead of a second
([scheduling.md](scheduling.md) pitfall 18; the log row's AFTER, owed
by Perugia, was measured: 62a098d). The rate gap is the measurement that decides whether the cut
must also leave the intake thread.

### 2026-10-20: wikidata-refs syncs from the plugin's pack

`app/refs/wikidata.py` rewritten (ref_version 2): one row per entity
keyed by its id (`wikidata_entity(id, kind, lang, label)`), synced from
the newest complete pack's `labels.sqlite` under `<ref_root>/wikidata`,
through `louise dev refs-sync --src <collections root>` as normattiva-refs
is; `ask lookup wikidata:Q42` returns the fact with its label. The store
(`app/refs/store.py`) commits per batch and asks a source that knows its
input for its mtime (`input_mtime`), and re-creates an EMPTY table whose
declared shape moved — `get_store()` had created the old shape on every
node at the first open — while refusing a non-empty one by name. No
resolver over labels and the table's size on the node are the two open
points ([wikidata-build-plan.md](wikidata-build-plan.md) §2). *Both closed:
12.62 GB, 121,724,461 rows; lookup by name, 3247c39.*

### 2026-10-20: jina-v3's folder, its YAMLs and the rebuild runbook

München reported the tokenizer's sha256 (`f59925fc…`) and that the other
three wikidata values wait on a `louise-python` rebuild (the live tree has
no `jina-v3` task) and on a defined weights folder. Written:
deploy/jina-embeddings-v3/fetch.py (the folder — weights at ab036b0, remote
code at bd55a5e as an offline Hub cache of REGULAR files, every file
checked against the commit's tree; prints the weights digest),
deploy/vega/stage/models/vega-jina-v3{,-query}.yaml (context_size the
model's 8,192: the task prepends the adapter's instruction before
truncating), the torch determinism hook `LOUISE_TORCH_DETERMINISTIC` in
the task's server (invariant 12: measured first, unset then set), and
[louise-python-rebuild.md](louise-python-rebuild.md), the runbook for the
heavy window: build, install beside, gate per collection, fall back by
YAML, then jina-v3's bit-exactness before any collection uses it.
Installed on the node the same morning: code commit `bd55a5ec…`, weights
digest `sha256:f0f752ae…` (16 files, 0 symlinks, equal to the platform's
digest in the deployed image), both pinned in fetch.py and filled into the
recipe template with the tokenizer's sha256; the 2026-10-18 measurement's
folder, which held files the layout excludes, was refused and moved aside
as `jina-embeddings-v3.pre-installer-20261018` (the operator's to remove).

### 2026-10-19: the deploy gate is München's full suite on the real image

Operator decision (2026-10-19, night), after a tip passed the
architecture session's tests and failed at import on the node's image
(python-multipart present in one container, absent from the other):

- **One gate.** A commit deploys on any peer only after München has run
  the FULL suite on it in an image built from `requirements.txt` alone —
  the image the node runs. That run tests what is deployed; nothing
  else does.
- **The architecture session** runs the test files its change touches
  before every push, and says in the push message which it ran. It does
  not run the full suite: its container carries packages the platform
  does not declare, so its pass is not the gate.
- **Perugia** deploys a commit München has passed without rerunning the
  suite: same code, same image build; what differs is the machine. It
  runs its own smoke checks (the web answers, the worker's first round,
  `louise node health`) and reports those.
- A difference between the two containers that lets a test pass in one
  and fail in the other is itself a defect to fix (tonight's:
  tests/test_invariant_guards.py now refuses a multipart route unless
  the package is declared).

### 2026-10-13: JuriFindIT acknowledged — the operator, for the legal pilot

The operator acknowledged the JuriFindIT question set's licence,
CC-BY-NC-SA-4.0, on 2026-10-13 ("I acknowledge it"), for the Normattiva
pilot's measurement (`tools/legal_pilot.py`, read-only, under
`/data/pilot/jurifindit` on the node). The acknowledgement is of these
bytes, as `legal_pilot fetch` printed them:
`corpus.parquet` 18,544,036 B sha256:33632b14e48cd75b106112c9771f30e1c471f364c57fe95a76bbb641f0f3c524;
`questions-validation.parquet` 33,219 B sha256:4c64549f76143411e555ee30bb787cf7ac2899ee4f6479cd6b855441e4adf8c6.

### 2026-10-12: Normattiva — one record per Expression, validity by key

The snapshot-chain multivigenza proposal of the morning
([`normattiva-multivigenza.md`](normattiva-multivigenza.md)) was reviewed
against the live archive and withdrawn the same day
([`normattiva-multivigenza-review.md`](normattiva-multivigenza-review.md)):
`as_of` withholds exactly the amended acts, a monthly chain never holds the
6.7 % of version transitions shorter than a month (37 % in the forty
codes), and it conflates valid time with transaction time. The operator
took the review's six decisions as recommended (§6.1) and said go.

**Built the same day, two parts.** (1) The plugin at parser 3: every
member of every multivigente zip is a record (`eli.expression_native_id`,
sorted by the start of validity so arrival stays monotonic), the floor on
validity at discovery (`VALIDITY_FLOOR = 1948-01-01`), one copy per
Expression (M before V before O — which removes the blocking defect D1:
the old rank put the text-less repeal state of 79,922 royal decrees ahead
of their text), the repeal state detected, `in_force` in place of
`eli_in_force`; the `normattiva` slicing scheme (type and year, 50 a part);
the spec fetching the 22 M zips and the repealed acts' O zip (7.8 GB).
(2) `app/validity.py`: the manifest's `validity` table filled at the rdf
stage, the index columns written by the fts and embed stages and
refreshed through a moved neighbour's own stages, the legs filtering by
column range, `--as-of` by validity for a versioned collection, results
carrying `validity`; 05 §5.1, 07 §7.3 and 09 §9.3 amended; the ELI note's
rule 2 reversed (the record pins the Expression). The embedder stays
DAR-legal-it — the operator states its licence is the training data's,
CC-BY-NC-SA-4.0 — and the reranker jina-reranker-v3 (review §10).

**Built later the same day:** the cross-record vector carry (§4.3, 06 §6.3
amended), `relations()` with ELI-named edges (§3.12), the central-directory
delta (`zip-directory`, 09 §9.17 amended; `app/acquire/zipdelta.py`) and the
member as origin unit for `records-vs-origin` (`member_origin.py`; 02 §2.3
amended); in the evening the reference source `normattiva-refs`
(`app/refs/normattiva.py`: `atto` and `versione` from the members' heads,
owning `urn` and `eli`; the citation resolver beside the gene resolver in
a `CompositeResolver`; an act's canonical id is its URN:NIR verbatim, 05
§5.2 amended) and the plugin at parser 4 writing `keys`. The Codici
measured: 41,289 distinct article texts across 2.9 M occurrences in 3,321
versions, 2.12× the latest versions' — the carry leaves 1.4 % for the
encoder. Later that evening the ORIGINALE's start was measured on 220
originals (ten per collection, review §7): the Expression date a member
states is its entry into force (equal to the preface note on 151 of 154,
on the vacatio pattern otherwise), so `original_start` sets
`vigenza_from` from it (parser 5) and `valid_from` became the index's
third column — the id keeps its publication prefix, discovery reads no
member, and a query in the fortnight before an act's entry into force
returns nothing. Then §3.2's case 2: the plugin's `closures()` (06 §6.1's
second optional hook) names the acts a version repeals in full — typed
`repeal`, whole-act destination — from the day the destination's `ita@`
Expression states (the Codici: 152 typed repeals, every one an AKN path
with its date), recorded per stating uid in `validity_closures` and
applied by key to the end of the repealed Work's chain in either order
of arrival; records carry the AKN work path as a third key (`akn:`,
parser 6). **Still open:** the measurement owed before `shard_records` is
fixed, the coverage of the 33,750 repealed-only acts by such closures
(on the live collection), and the Constitution's on-demand query. The
one measurement the operator asked for first — a second listing of the
archive a month from now — is dated.

**Step 3 — the collection on the node (2026-10-12, night): STAGED AND
PROVED, the swap and the creation left to the operator.** In the evening
the other session served DAR-legal-it on the node with the operator's
go-ahead (`vega-dar-legal-it`, torch XPU behind VeGa; vega.md §12), which
removed the encoder blocker. `hold embedder run` read the run it states
(sha256:85b9fa1a…, engine `localai/louise-dar-legal-it`, weights
sha256:92f4c9e0…) into the recipe
([`normattiva-recipe.toml`](normattiva-recipe.toml)); the articles' token
lengths were measured for `ctx` (8192, the engine's window; review §7);
pooling is what the backend does, mean. The runbook for 4493c15 is
`deploy/16f/deploy-louise-normattiva.sh`: its non-live phases ran — the
tree staged beside the live one, `louise-rag:staged` built, the live
config loads every collection under it, and tests/test_validity,
test_normattiva, test_refs_normattiva, tests/acquire and tests/hold pass in
it (87, 255, 949). The swap (phases 4–6: promote, build louise-rag:local,
stop, LOUISE_PLATFORM_IMAGE, start) is refused to the agent as a
production deploy, as before; the operator runs the script, then
`deploy/16f/create-normattiva.sh` — the recipe copied into
/config/recipes, `hold collection create` in the web container (the
stated run held to the recipe's engine and weights; slices = "all", so the
worker acquires the full profile itself), the licence terms printed and
acknowledged, the GPU badge — and the worker takes it from there: fetch
(7.8 GB, two connections), scan, parse, fts, embed; pin the model through
LocalAI's API once it embeds. The collection is the quarantine's guinea
pig: never listed, every missing tool fixed.

**CREATED, 2026-10-12 21:24 UTC.** The other session ran both scripts on
the operator's go-ahead after a clean suite (4,529): d07ee60 is deployed
and the collection is `sha256:28562add0b128a229cf3f050532deeef38a95e6022aa0be05abb586776851dd6`,
`slices = "all"`, `embed = "off"` (the operator decides embedding), in
quarantine (gate, roots/chunks, records-vs-origin pending); the worker
began the corpus fetch at once (222 MB of the 7.8 GB an hour later). Three
things the scripts had wrong, fixed in 1f234a1, 5027330 and 8f51502:
`create` runs in the WORKER container (the web mounts /corpora read-only);
it writes `slices = []` by design, so the script sets "all" after the
ack; and the served engine's image had moved (`355a0208…` → `7b5d557e…`,
run 651c4ec0…) with no file under the backend changed — open in vega.md
§12, safe by construction (an unvalidated run's answers are refused
before the write), the recipe names the engine served now. The wrapper
build left its stdlib uncompiled, fixed in 24b9faf. `vega-dar-legal-it`
is not pinned yet.

**VeGa keeps the tree behind a run (the same night, operator's "do it").**
The drift could not be diagnosed after the fact because VeGa held a run's
tree digest and not the list it hashed. Now the wrapper's `tree_digest` is H
of `tree_manifest`, and after it publishes a run it tells VeGa the list once
per tree (`publish_tree`; vega.md §2, §12); VeGa takes it only under its own
digest, keeps it in its file (`trees`) and serves `GET /vega/trees/<image>`;
`hold embedder tree-diff BEFORE AFTER` names the paths added, removed and
changed (07 §7.3). Tests: tests/vega (service, wrapper logic, trees).
NOT ON THE NODE YET: the receipts service (the `vega` service's image, a
Louise image tag) goes first, then the wrappers are rebuilt and reinstalled
with `deploy/vega/stage/build_vega_package.sh` (`vega-llama-cpp`,
`vega-louise-medcpt-article-ov`, `vega-louise-dar-legal-it`), and each
backend's next load tells its tree — the operator's, as every deploy is.

**The fetch's first finding (2026-10-13 00:27 CEST), fixed.** The corpus
fetch died at 5.1 GB: `corpus fetch (startup) failed: ReadTimeout`, and
the next discovery would retry it four hours later. One read timeout on one
zip's Range request ended the whole pass, twice over: the zip delta ran
BEFORE the per-file boundary of `_fetch_one_file`, and the Range reader
raised httpx's own class, which that boundary does not catch. Now the whole
of a file's work is inside its boundary (nothing escapes into the executor;
a file that fails is one failed file, the pass goes on), the Range reader
asks again three times (a second apart, doubling) before it is the
transport's error, and a stream dropped mid-file resumes from the bytes on
disk within the pass (`STREAM_ATTEMPTS`) before the next pass would. Tests
in tests/acquire (the mock origin gained `fail_next`). Ships with the next
deploy; until then the 03:25 UTC retry runs the old code.

**The fetch's second finding (2026-10-13 00:22 UTC), fixed.** Under
67f5e92 the pass survived, but `akn/M/regi-decreti.zip` (1.7 GB) failed
its three in-pass retries at 1,086,390,272 bytes — the same byte as the
night before — and the row carries `retries 3` and an error (reported
after DEFER_ATTEMPTS, as designed; `_is_pending` reads `fetch_done` alone,
so the file is still retried every pass — it is not taken out of
selection, as feared). Probed from here and from the node: the origin cuts
every whole stream at that byte, an open-ended resume `bytes=N-` from there
stalls past the read timeout, while a CLOSED range at the same offset
answers within a second at the full rate. So `transport_https.fetch_ranged`
fetches a file in closed ranges of `RANGE_STEP` (64 MiB), each request with
its own timeout and three retries, a short answer continued from where it
ended; the engine uses it where the spec declares `zip-directory` (an
origin whose delta already rests on Range), and falls back to the stream
when the origin answers a Range with 200. The mock origin gained
`cut_after_bytes` and `refuse_open_ranges`, the two traits seen. Ships with
the next deploy; the build waits for the file.

**The fetch's third finding (2026-10-13 01:26 UTC), fixed.** Under 0699408
the ranged fetch moved regi-decreti forward (1.09 → 1.24 GB), then the
origin answered 500 to every Range request for eight seconds and three files
failed: regi-decreti mid-step, dl-e-leggi-di-conversione at its first step,
and codici — complete on disk — at its directory probe, which declined the
delta and sent a complete file to a fetch that the same 500s then failed.
Two rules were missing. (1) A 5xx is the origin's episode, not the file's:
the Range reader, the ranged step and the stream retry it, five attempts
spaced 1, 2, 4, 8 s (15 s in all, more than the episode), and `transient`
reads a 5xx, a read timeout and a dropped connection as deferrals, so the
row's error stays quiet until DEFER_ATTEMPTS. (2) A directory that could
not be read says nothing about the file: the delta now returns a
**deferral** — the copy on disk stands, the row is left exactly as it was,
the pass counts it skipped, the next pass asks again; only a directory READ
that says the file differs too much, or a member that does not check out,
declines the delta and sends the file to the whole fetch (D4); an origin
that answers a Range with 200 falls to the pre-delta rules. The mock origin
gained `answer_next`. Ships with the next deploy.

**The fourth finding (2026-10-13, after the fetch completed): the id's
width.** The whole archive discovered — 318,345 seen, 329,429 documents —
with `container_errors 4`: four pairs of versions whose native ids
collided, three on 2010-12-16 and one on 2011-02-10. Not four bad acts:
the id was `YYYYMMDD * 10**9 + blake2b mod 10**9`, nine decimal digits of
hash per day, and 2010-12-16 is the taglia-leggi day on which about 77,000
versions start — n²/2·10⁹ ≈ 3, the birthday arithmetic the ELI note itself
warned of. What the guard dropped was the colliding member alone (a scan
report, not the container), but a uid minted from a colliding id would have
been permanent, so the other session did not start the build. Fixed in
eli.py: 18 bits of days from 1500-01-01 (to the year 2217) and 45 bits of
hash, exactly 63 — one pair in 3.5·10¹³, 8·10⁻⁵ expected on that day;
`eli.id_day` reads the day back for the validity columns and the slicing,
nothing else does arithmetic on the id; parser 7. Every id changes, and
the Codex pins the parser that produced the records (02 §2.3), so the
collection is RE-CREATED: remove the collection file and the collection's
directory under data/collections (manifest, index, lance, state; nothing
was built, the corpus on /corpora stays and is adopted by its directories),
then `create-normattiva.sh` again. The node's action, as every creation
is.

**The pilot's harness and the template (2026-10-13).** `tools/legal_pilot.py`
measures a built legal collection deterministically: `coverage` resolves
JuriFindIT's gold articles by key (its `file_name` is the act, its `path` the
article, both through `validity_works` and the shown version's chunks) and
excludes what the collection does not hold — EU regulations are; `run`
scores recall, mrr and ndcg over the gold node ids and the REACH of the
misses, how many sit one declared edge from a hit, which decides the review's
open point on widening by the edges; `temporal-make` mines dated question
stubs from the version table for a person to review, and `temporal-run` holds
`--as-of` to the version's uid and a numeric nugget to its text, with the
undated control (R2's 0 % provenance, measured here). It runs once the
collection embeds. `legal-collection-template.md` collects what Normattiva
left for the next legal collection: identity and the id width, versions and
validity, origin and fidelity, the four acquisition findings as a checklist,
chunks and encoder, relations as facts, the reference source, the studies
adopted and not, the process, and a numbered checklist.

**The fifth and sixth findings (2026-10-13, the re-created collection's
first pass), fixed.** (5) The 21 complete zips were not adopted: the delta
read each directory, found nothing changed, and REBUILT the file anyway —
7.8 GB copied through tank and re-hashed every pass, mtimes moved, nothing
moved over the network ("fetched 21, adopted 0"). Now a directory that
says "the same members, in the same order" is the adoption: nothing
written, nothing hashed (the directory's CRC-32 per member is the
verification), the row keeps the digest the last fetch wrote, the pass
counts it adopted. (6) Two files — regi-decreti and the repealed acts' O
zip, the two with the most members — had no row and were "skipped": their
directory reads had timed out five times over and 2c4194f's deferral
counted them skipped, silently, because the reasons lived in a list nobody
printed. Probed from here: this origin caps a Range response at 50 MiB
(52,428,800 bytes, then a stall), and the directory reader's third step
asked 64 MB. The tail is now read in pieces of at most 32 MiB, the ranged
fetch's step is 32 MiB, and every delta's decline or deferral is an event.
Both files are in the build: discovery and the stages read the corpus and
the documents table; `fetch_files` is consulted by the Library page alone.
The next pass adopts both and writes their rows.

**LIVE (2026-10-13, morning UTC).** Built by the other session under
4a2fbbe: 318,349 records in 6,979 parts over 972 slices, 6,878,986 chunks,
about twenty minutes, nothing refused; snapshot 2026-09
`sha256:52700e550cd2…`, signed; `hold check normattiva` PASS for roots,
records and chunks (15,832 records over 349 parts, none failing), licence,
signature, calibration 16/16, vectors-sample 45; out of quarantine, embedding
at about 9 documents a second on `vega-dar-legal-it`, now pinned. The fetch
under 4a2fbbe read "fetched 23, adopted 23", every zip left untouched. Two
findings with it: `records-vs-origin` came back INCONCLUSIVE once — one
sampled record of 15,832 could not be re-fetched from the origin (a
TransportError on the Regi decreti URL; the check's re-fetch shares the
delta's reader, so if it ran before 4a2fbbe's pieces it met the 64 MB tail;
to re-run under 4a2fbbe and, if it repeats, read the whole error) — the
worker's own bounded check had passed at 09:43 UTC, which cleared
quarantine; and the pack build's per-slice line said one shard more than it
wrote (the next index, fixed). The pilot's harness runs next, once the
embedding is complete (legal-collection-template.md §9).

**DENSE SEARCH WITHHELD — the vector table without its columns (2026-10-13,
afternoon UTC).** The node logged `normattiva [dense] dense search withheld:
the vector table carries no validity columns`. Root cause: the batched embed
path (`worker._embed_parallel`, every encoder's since the producers) called
`upsert_book` without `columns=` while `stage_embed` passed them; the first
write created `chunks` without the three columns and `_conform` stripped
every later row to fit the table, silently: 40,112 rows (table created 13:09
local) that no date can filter. `contribute` passed none either. Fixed in
three places: the batched path and `contribute` pass the columns and mark
the rows synced; `_conform` REFUSES a row that carries the columns for a
table that lacks them (`ValidityColumnsMissing`, a deferral — the table's
state, never the document's, so nothing is written to the document and
nothing more is stripped); `hold vectors validity-columns <collection>`
rebuilds the table with the columns read from the version table, every
vector kept (RULE 1, nothing re-embedded; the vector index is rebuilt
after). A guard test asserts every `upsert_book` call under app/ names
`columns=`; `hold index show` prints `validity_columns` for a versioned
collection. Node remedy, for the other session: deploy; stop the worker (it
holds the collection lock); in the worker container `hold vectors
validity-columns normattiva`, then `hold index build normattiva --vector`;
start. Until then the embed stage defers every Normattiva document. The
template's §11–12 carry the finding as the test pool a new collection runs
through.

**"THE ANSWER WAS CUT" (2026-10-13, afternoon UTC).** The operator asked
"Diritto all'oblio" over normattiva four times between 13:44 and 13:57 UTC
and saw the answer cut. Measured on the node: LocalAI caps nothing (a
13,772-token generation ended with `finish_reason: stop`, 61 tok/s); the
operator's exact request, rebuilt in the web container and streamed five
times, ended whole every time (1,044–1,661 characters, full sentences,
`stop`, `[DONE]`); end to end through Louise's own port the same. What the
measurements DID show: the chat model ignores
`chat_template_kwargs.enable_thinking` and thinks before it writes — about
1,400 characters of hidden reasoning per answer, so the page received NOTHING
for 19–34 s after the request (cold start 6–10 s, the prompt, the reasoning),
and the reasoning, in English, once pulled the Italian answer into English;
and two of the four runs have no `query_log` row while LocalAI finished them
(13:50:27 and 13:57:11): their page connection was gone before the model's
end — the generator abandoned by Starlette on the disconnect, nothing logged.
No proxy sits in that path (Funnel fronts port 30014, not the web's 30042),
so what closed those connections is not known from the node; the operator's
browser and route are the missing datum.

Three changes, all measured or tested: Qwen3's own switch ` /no_think` on
the answer prompt (`retrieval.THINK_OFF`, as `llm._RESTRICT_SYSTEM` carries
it): no reasoning, the first word after 0.4 s warm / 9.8 s cold, the answer
in the question's language, whole — the reasoning was never shown, nothing
is lost. The stream's END is said: `llm.chat_stream` RETURNS a `StreamEnd`
(finish_reason, the end marker, an error payload), the API relays the
endpoint from a thread with an SSE comment every 5 s while nothing has
arrived (`KEEPALIVE_S`; the page skips non-data lines), appends
`[answer cut: <why>]` to an answer the endpoint capped, failed or closed
early, and logs one line per answer (`answer: N chars in S s, first word at
F s` / `answer cut after N chars: why`) — the next report has a measurement
where this one had none. Observed and NOT changed: LocalAI's memory
reclaimer evicts the chat model right after every answer ("Memory reclaimer
evicting LRU model … lastUsed" one second after each completion) and nags
"Skipping … busy model" twice a second during it, so every ask pays a 6–10 s
cold start; that is LocalAI's watchdog under the card's squeeze (the embedder
runs beside it), a configuration matter for the operator.

**"COMPLETE, BUT WITHOUT THE LINKS" (2026-10-13, 16:2x UTC, after
765c1e1 went live).** The Italian answer now arrives whole, and without a
single [n]: measured through the web port, 0 markers with the answer
language set and 1 without, where the thinking runs had cited four
sources — the hidden reasoning was where the model planned its citations,
and the switch took them with it; the page links `[digits]` only
(ask.html `linkCitations`). Fixed by asking LAST and BY EXAMPLE: the user
turn ends, after the question, with "cite the source … like [2] or
[2][5]: the numbers of the sources above, nothing else inside the
brackets" (`retrieval.CITE_REMINDER`). A reminder written as "[n]" was
copied literally ("[n4]", 0 markers in four runs); the example gave 4–6
markers in each of three runs, all on the one source that is about the
topic — the thinking runs' extra citations had been of sources that were
not. Test: the user turn ends with the reminder; the system prompt still
ends with the switch.

**NORMATTIVA PARSER 8: EVERY VERSION SINCE 1861; CITATIONS BY DATE AND
NUMBER (2026-10-13, night; operator: "I want everything", "remember rule
1").** The operator's query "art. 12 della legge sull'emigrazione testo
unico 13 novembre 1919, n. 2205" failed three ways, measured on the node:
the act is held (R.D.L. 2205/1919, Testi Unici) but only its 4 versions
from 1940 — the 1948 floor dropped the 16 before, and article 12 was
repealed in 1930, so no held version has its words; the citation parser
found nothing (a title stood between "legge" and the date, and the origin
files the act as a regio decreto-legge), so the act ranked seventh behind
the decrees that cite it; and its current version was not embedded yet.
Parser 8 removes the floor: 341,959 versions against 318,349 (23,610 more,
most from the 1910s–1930s, every one parsing), measured from the zips'
directories. The citation parser admits up to eight words of title between
the form and the date, reads "testo unico"/"t.u." as a form the origin
decides, and a dated citation whose form is not the origin's resolves by
date and number (an undated one still needs its form; tests). RULE 1 for
the collection: a new Codex (the Codex pins the parser, and the old
versions would enter published, closed parts, which the build refuses —
02 §2.3), made by A1c as gutenberg's parser 9 was:
deploy/16f/recreate-normattiva-parser8.sh pauses `normattiva` FIRST (with
parser 8 live it would discover the old versions itself), creates
`normattiva-1861` from the same recipe, points its origin at the same
zips (no fetch; discovery reads the new path as a relocation), carries
manifest, index and vectors (`dev carry --rebuilt --copy`), builds and
snapshots, then `--reconcile` keeps every unchanged document's vectors —
only the 23,610 are embedded, most of their article texts carried by
chunk hash. `normattiva` stays paused; removing it is the operator's call.
Article-level resolution (pinning the cited article's text by key) stays
open, as before.

**`normattiva` IS THE COMPLETE COLLECTION (2026-10-14, night).** The
parser-8 re-creation (`normattiva-1861`) lost its carried manifest to a race
— the worker opened the new collection two seconds after `dev carry` had
removed its empty manifest, db.connect made a fresh one, and the carry took
it for its own (d6575d2 fixes both sides). The vectors were carried; the
manifest was restored from the parser-7 one (c8b0bf3: 318,349 documents,
183,287 embedded, 0 re-embedded) and reconciled. Lesson for any restore of
a manifest: the copy brings the source's runtime embed mode (it was "off",
set to keep the old one asleep); the target's file must be applied after.
Then, operator's go under RULE 1: the parser-7 `normattiva` forgotten
(~18 GB of index, vectors, state and parts; /corpora/normattiva kept, the
new one links it) and `hold collection rename normattiva-1861 normattiva`
(341,959 stored paths rewritten in one transaction; discovery after: added
0). Live: 1,934,944 vectors with validity columns, ~155k documents still to
embed (~5 h). The origin (api.normattiva.it) answers nothing to this
address since the evening's probes; acquisition defers, the zips stand.

**THE ASK PAGE DRAWS THE PLATES; ab4a584 DEPLOYED (2026-10-14, 17:26Z;
image `47a9e739…`).** The API has placed `figures` on results since
ae944b9; the page drew none. `web._attach_figures` places them through
`results._place_figures` (record_line 2 collections, never a red one) and
`_ask_results.html` draws a held plate as a thumbnail on
`GET /api/ask/figure`, names one not held or not carried. A chat answer's
[n] link to these hits, so its sources show them too. Suite 4,621; staged
202. Live: "illustration of the cathedral facade" in gutenberg, 10 hits,
25 plates drawn, one fetched (200, image/jpeg, 600×684); a warm search
8.77 s against the legs' 8.66 s — placing costs ~0.1 s. Shipped with it:
ddb64c0 (dar-legal-it backend source under deploy/, not rebuilt here).


**NORMATTIVA: REMEDY DONE, THE PILOT'S COVERAGE, THE ARCHIVE'S SCOPE
(2026-10-13, afternoon UTC).** The other session deployed 03bc036 and ran
the remedy: `hold vectors validity-columns normattiva` — 156,727 rows,
35,470 documents, 1,359 Lance versions behind — then `hold index build
--vector` (ivf_pq, 196 s); verified from here afterwards: `validity_columns:
true`, 445,980 rows, embedding on with the columns, no refusal in the worker
log. 765c1e1 goes out next. The pilot's `coverage` (run 20261013T111846Z):
179 questions, 175 covered; 590 gold articles, 245 held; 148 not Italian
acts, 162 not articles (annexes, preambles, communications), 34 acts not
held, 1 article not in the shown version. Of the 34, 29 are authorities'
provisions Normattiva does not publish, 1 is held under its ministry's URN
(a harness fix owed), 2 are not state acts, and ONE is: L. 185/1990, absent
from all 23 origin zips. Measured from it (review §10.1): the pre-packaged
collections are thematic and name no collection of ordinary laws; per year
the collection holds 52–71 % of the state's numbered acts since 1990
(85–91 % for 1950–1980, by the DPR and Regi decreti collections). The
records are right and verified; the scope was over-read. DECISION OWED by
the operator: state the scope on the card, or complement the origin by the
API's search routes — new acquisition machinery. The harness also wrote 200
temporal stubs; the operator judged them useless and they were deleted
(2026-10-14);
the `records-vs-origin` re-run is still owed (cut by the deploy).

**NORMATTIVA STAYS AS IT IS; THE FULL CORPUS MEASURED FOR A DAUGHTER
(2026-10-13, evening UTC; operator's decision).** The operator keeps the
collection at the 23 pre-packaged collections and asked how the full body of
state law can be had for a new or daughter collection.
docs/notes/normattiva-full-corpus.md is the answer, measured on the node
against the live collection: the archive holds 205,100 acts (one advanced
search's facets); 1948–2026 has 74,613, the collection 63,804 — 10,809
missing, 85 % held (87/85/79/78 % by period); the gap is ordinary laws
(1990: 223 in the archive, 98 held). The documented API gives the
enumeration (`ricerca/avanzata`, 500 a page, facets by year and type), an
asynchronous export measured end to end (search → token → confirm → ~50 s →
303 → zip, the pre-packaged zips' member naming, at most 7,000 acts a job)
that yields ONE version per job — the original, or the one in force at
`dataVigenza` — and `ricerca/aggiornati` as the change feed. The portal's
own per-act export answers a browser session only and was not pursued. The
archive serves one AKN per Expression whatever the route (a browser
session's export of a held version was byte-identical to the zip member),
so the plugin and parser apply unchanged. Three designs with costs: A, a
daughter with original + current for every act (about 340 jobs, a few GB,
intermediate versions declared missing); B, the full history by one job per
(act, version) — about 7,000 more jobs, four days of the service's time; C,
ask IPZS for a multivigente custom export or a "Leggi" collection. The note
recommends C now and A as the daughter's first step; the machinery A needs
(an `api-export` spec mode, the daughter's Codex per 02 §2.7) is asked
before built, so the next step is the operator's word on C and A.

**A NEW COLLECTION FOR THE FULL CORPUS: THE EXPORT MODE BUILT (2026-10-13,
evening UTC; operator: "a new collection").** The origin's asynchronous
export is an acquisition FILE KIND now: a `RemoteFile` whose `remote` is
`export:<partition json>` (09 §9.17 amended; app/acquire/export_job.py) —
the job started, confirmed, polled and downloaded, every wait bounded, the
stop honoured between polls; `engine._export_fetch` adopts the copy when
the origin's update feed (`ricerca/aggiornati`, read since the copy's
mtime) names no act of its partition, re-exports when it does, leaves it
standing when the feed is unread; a job not done in JOB_BOUND_S is a quiet
deferral (engine.transient). NORMATTIVA_SPEC lists `akn/E/O|V/<year>.zip`
for 1861 to this year under a new `archive` profile; `full` names the 23
zips explicitly, and the spec DECLARES it as what `slices = "all"` spells
(`AcquisitionSpec.all_profile`; 09 §9.17) — the first full suite caught
`archive`, the profile whose filter includes everything, taking its
place, which would have started the live collection exporting — so
`normattiva`'s passes do not change (tested). The mock origin gained the
job routes. The new collection is
`normattiva-archive`: docs/notes/normattiva-archive-recipe.toml — the same
space as `normattiva` byte for byte, so every shared chunk carries its
vectors (RULE 1) — and deploy/16f/create-normattiva-archive.sh, which sets
`profile = "archive"`. NOT CREATED YET: the measurement of one year's job
in both forms (time, size, the VIGENTE member's naming against its
FRBRthis) is owed first — the origin answered the one-act job in 50 s at
15:3x UTC and dropped every job request at 135 s from 17:xx UTC after a
run of probes (throttling or an outage; measure on a clean day, from a
single job, and tell the operator the numbers before the first pass).


### 2026-10-05: the side branches merged into main

Three branches existed beside `main`. `ccr-b147bbfc-9ktipo` was already
in main (step 15c). `claude/rage-backends-louise-mnih8a` (2026-09-27/29)
and `ccr-d5be5f2c-fdn6fl` (2026-09-29/30, a superset of it) were merged
on 2026-10-05, taking main's later decisions wherever the two sides had
decided the same question:
- **Dropped: the load amendment** (branch, 2026-09-28: an engine's
  `load` field, Codex 4 "exactly when" an engine carries it, statement 3
  likewise). Main decided it again on 2026-10-03 as `options` (09 §9.3,
  step 15a) and deployed it on 2026-10-05, so the branch's code, spec
  rows and tests went; `rage.md` §13 records it as superseded.
- **Kept as history**: `rage.md` §22–§23 (LocalAI's rerankers backend
  checked; RAGE spawning LocalAI's backend binaries) — superseded by
  [`rage-over-localai.md`](rage-over-localai.md), kept with a preface.
- **Carried**: a validation counts only when THIS node signed it and is
  re-checked once per process (`app/hold/runs.py`, `app/run_check.py`;
  `rage.md` §13); the web process holds no private key — a public key
  file, signing work queued for the worker (`app/identity.py`,
  `app/jobs.py`; `deploy/truenas-app.yaml` mounts the node key read-only
  in its own dataset, NOT yet applied on the node);
  [`weights-fetch.md`](weights-fetch.md), a design note.
- **Carried and renumbered: deposit collections, step 16** (was 15 on
  the branch): the spec (01, 02 §2.9, 03 §3.2, 04 §4.4, 06 §6.7, 07,
  09 §9.13, §9.17), [`deposit-collections.md`](deposit-collections.md),
  `app/hold/deposit.py`, `app/hold/sources.py`, `app/localagi.py`,
  `app/plugins/deposit/`, `tests/deposit/`; `pypdfium2` in
  `requirements.txt`; **git in the image** (`Dockerfile`, base stage): the
  `.git` source clone needs it, and the merged suite found it missing on
  2026-10-05 (`tests/deposit/test_deposit_sources.py`); **the manifest
  created at `create`** (`deposit._init_manifest`): a fresh manifest opened
  by two threads at once failed its first connect with "database is
  locked" — the rollback-to-WAL conversion of a new file takes a lock
  SQLite does not wait for — and a deposit collection has three writers
  from birth. 16a–16e were built and run against LocalAGI's own
  client on the branch; 16f, the operator's stop, is open. **Not
  deployed**: the node runs 736fc52 + 5429c2b's notes.

Whole suite in Docker on the merged tree, after both fixes: 3514 passed
(tests/deposit three times over, 100 each — the race showed in 1–3 of
99 before). The first run, before them: 3512 passed, 1 failed.

**2026-10-06, the deposit encoder decided**: granite-embedding-107m-multilingual
(the spec's default, LocalAGI's own; operator), over bge-m3 and
qwen3-embedding-4b. In the repository: its LocalAI YAML, pinned like the
eight; its weights mount; the `[deposit]` example in `platform.toml.example`.
On the node: nothing yet — the merge of 2026-10-05 is not deployed either,
because the node lost its internet egress during the image build (DNS and
HTTPS by IP both fail; the gateway answers at 0.03 ms). Both wait for the
operator: the deploy runbook, then the deposit runbook (step 16f).

**2026-10-06, ~09:45Z: 3edbabb DEPLOYED** by `deploy/16f/deploy-louise-3edbabb.sh`,
run from the agent's session after the operator's "try again": staged, the
live config loaded all three collections (the proof had to borrow the web
container's mounts — the drag is on tank), tests/deposit 100 and tests/hold
704 in the staged image, promoted, restarted; web and worker on
`beafb0603389…`. The worker came up clean (no validation set aside, no
error). `src.old/` and `louise-rag:pre-cb419e7` are the way back. The
node's egress had returned after the operator had the TrueNAS Container
`claude-code` (libvirt-lxc, its own LAN address) force-stopped, deleted,
and the node rebooted — it was the cause, not the router. Still waiting:
the granite weights on the node (the agent's scp is refused), then
`deploy/16f/deposit-16f.sh`.

**2026-10-06, ~10:05Z: the deposit runbook ran** (the weights copy went
through on the operator's order). Granite is served by RAGE beside the
eight; its run `5b6dea5d…` was stated on the first answer and the same
run after a reload; the space file was written from `GET /louise/runs`
(engine `localai/llama-cpp 58830f7+patches…` on the Arc, 384 wide, CLS,
512 tokens; tolerance 0.995 by decision); `[deposit]` and the token are
in place; `deposit-granite` was created (space `1900d348…`, the same id
the dry run computed) and its run passed the gate: 16 of 16, median
1.000000, batch-invariant. **The guinea pig found the tooling missing:**
the copy stayed in quarantine for roots/chunks and records-vs-origin,
which a never-share collection never runs — fixed in `copy_state`
(tests 1 and 5 follow the plan; `origin_check: not-planned`), and in
the runbook (gzip body; `show` without `--json`, which the CLI refused
and now accepts). Deployed next.

**2026-10-06, ~10:40Z: 28dfff7 DEPLOYED** (the copy-state fix; suite 3516 on
the workstation, tests/deposit 102 and tests/hold 704 in the staged image).
`deposit-granite` is **in use**, `origin_check: not-planned`. Then
`deploy/16f/localagi-smoke.sh` ran LocalAGI's own request sequence against
the node with the deposit token: no token 401; create 201; list; store
(multipart, the client's temp path, the key its base name); entries; read;
search found the memory; the open `/api/search` never named the
collection; delete; store again and reset erased it with no trace of the
text under `data/collections/`; the worker embedded the deposit through
granite within the second. **Gap found:** no `hold collection forget`
exists (07 §7.3 and 03 §3.6 name it), so the throwaway `smoke-agent`
keeps an empty `data/collections/smoke-agent/` (its held file removed).
**16f's stop proper remains the operator's:** one LocalAGI agent at
`http://192.168.178.10:30042` with the token file's content as
`local_rag_api_key`, memory stored, found, compacted; a file from its UI;
one URL source after `hold collection ack --web-sources`.

**2026-10-06, ~11:00Z: LocalAGI on the node** (operator: "do not touch
localai; create an app for LocalAGI via custom app yaml, a localagi
dataset in tank, wire it to Louise"). Dataset `tank/apps/localagi/pool`;
TrueNAS custom app `localagi` from
[`deploy/truenas-localagi.yaml`](../../deploy/truenas-localagi.yaml):
quay.io/mudler/localagi pinned by digest at sha-7e0947d — the commit the
deposit routes were proven against (quay has no semver tags) — UI on
:30050, LLM the node's own `localai` app (:30286, `qwen3.8-2b-q4`,
light because the card is RAGE's), knowledge base Louise
(`LOCALAGI_LOCALRAG_URL` :30042) with the deposit token as
`LOCALAGI_LLM_API_KEY` — LocalAGI's pool-level RAG client reuses the
LLM key (`cmd/agent_run.go`), so the token also travels to the LLM
endpoint as a bearer; per agent `local_rag_api_key` can replace it.
Proven with a throwaway agent (kb and long-term memory on): on its
first message LocalAGI created `wire-test` on Louise (201) and searched
it (200); the agent was deleted, the collection reset, its held file
removed. The operator's 16f stop can now run from LocalAGI's UI.

**2026-10-06, ~11:05Z: 16f's STOP RUN** (operator: "do it"), by
[`deploy/16f/localagi-stop.sh`](../../deploy/16f/localagi-stop.sh) through
LocalAGI alone (:30050), which proxies its collection routes to Louise:
- memory STORED: the agent `louise-stop` (kb, long-term memory, daily
  compaction with summary; `qwen3.8-2b-q4` on the `localai` app) took one
  fact and LocalAGI wrote it to Louise within seconds (one entry);
- memory FOUND: the next turn's knowledge-base lookup against Louise
  returned it ("Found similar strings in KB") and the reply used it;
- a FILE through LocalAGI's page route landed as an entry and was searchable;
- the web-sources disclaimer accepted for the node
  (`hold collection ack --web-sources`, sha256:7b7c3a22… at 11:05:17Z), one
  URL source (example.com, daily) registered through LocalAGI, fetched by
  Louise's worker within the second, its entry under LocalRecall's key;
- COMPACTION ran at the agent's start and found "no groups to compact":
  it closes past days, and today is open — it will compact tomorrow;
  the list/read/store/delete it uses are 16d's contract, proven twice.
Louise: `louise-stop` in use, `not-planned`, three documents embedded
through granite. The agent stays for the operator. **Step 16 is done**
but for `hold collection forget` (07 §7.3), which the smoke test found
missing.

**2026-10-06, ~11:30Z: `hold collection forget` built** (operator: "go").
`app/hold/forget.py`: the held file goes first (the node stops serving it
at its next round), then — on a running node, in the worker, the one
writer — the collection's threads are retired (`worker.retire_collection`:
pipeline, embed producer, scan and fetch told to stop and waited for,
bounded), then a never-share collection's deposits are erased leaving no
trace (`deposit.erase_all`, 02 §2.9), then the directory whole. `--kinds
vectors` takes the vector-side members and the vector table only;
`--dry-run`; `--here` for a node whose worker is not running; `--yes` off a
terminal. Queued through the DEPOSIT job kind (op "forget") like every
other write of a collection from outside the worker. 07 §7.3 amended: it
said "exits 2 while the node holds the collection", a lock no command
takes yet (§7.7's one-writer lock is still unbuilt). Tests:
`tests/deposit/test_deposit_forget.py` (9).
**Deployed as 122c674 (~11:45Z)** — suite 3525 on the workstation, 704 and
111 in the staged image — and used for real through the worker's job
thread: `smoke-agent` and `wire-test`, the two directories today's
throwaway collections left, forgotten (dry run first; a second forget
says "gone", nothing to do). Held now: deposit-granite, louise-stop and
the three of September.

**2026-10-06, ~12:30Z: LocalAGI closed with a key, compaction seen for real,
housekeeping.** `LOCALAGI_API_KEYS` set (its file
`/mnt/tank/apps/localagi/localagi.apikey`): every route answers 401 without
`Authorization: Bearer <key>` (the `x-api-key` form did not pass at
sha-7e0947d). The health check had to change: static assets are behind the
key too, and TrueNAS interpolated `$code` out of the compose test — `$$`
is the escape; the app sat "unhealthy" until then. The agent's restart
ran COMPACTION on today's group: Louise now holds `summary-2026-10-06.txt`
in place of the two dated memories, beside the file and the source — the
last LocalAGI path exercised against Louise. Removed: the two older
backup images (`pre-122c674` and `src.old` stay), `/tmp/louise-src.tgz`.
The node-side DEPLOYMENT.md paragraph is drafted in the local copy for
the operator to drop in.

**2026-10-06, ~13:00Z: the one writer per collection (07 §7.7) built.**
`app/hold/lock.py`: an advisory flock on `<collection>/state/lock` with the
holder's words inside; a crash leaves no stale lock. The worker takes the
lock of every collection it runs, each round, and runs only those it
holds: one paused by `node pause <collection>` (new CLI surface `node`;
the set is runtime state that survives a restart, like the mode) or no
longer held is retired first — its threads finish their unit — then
released; one another writer holds is left alone and the round says so
once. The writing commands take it or exit 3 naming the holder; `forget
--here`, `vectors check --drop` and `collection migrate --apply` exit 2;
`show`, `list`, `check`, `ask` take nothing. `dev migrate schema` and
`dev adopt` of §7.7 do not exist as commands. Tests:
`tests/hold/test_write_lock.py` (11).
**Deployed as 865ccbe (~13:07Z; suite 3536)** and seen live: the worker holds
`state/lock` for all five collections ("the node (worker)", its pid, since);
`hold records build deposit-granite` from the worker container exits 3
naming it; `node pause deposit-granite` had the lock released within a
round and the command ran; `node resume` had it taken again within a
round. No worker error. Backup images: `pre-865ccbe` alone now.

**2026-10-06, 19:07Z (UTC): 9e DEPLOYED whole as d596355.** Web and worker
on image `a1bdc868…`; `src/.deployed-commit` = d596355; the staged image
passed tests/deposit (111), tests/hold (715), tests/share (105). First
minutes quiet: every thread up, no share, error or warning line, only
Docker's DNS listening. Backup image `pre-d596355` alone. Step 9 is
complete on the node; it waits for a second peer to be exercised live.

**2026-10-08, ~12:25Z (UTC): scheduled checks LIVE (6b6e597); the plan's
memory 18.7 GB → 2 GB (6928c2c); the loose ends fixed (ab40999).** The
worker runs each Codex collection's plan once per snapshot or cadence, as a
child at nice 10, one at a time (app/scheduled_checks.py; scheduling-log.md
2026-10-08): the first round passed everywhere — gutenberg 1,616 s, pubmed
739 s, the minis 2 s — peaking at ~2 GB. Measuring first found pubmed's
plan at 18.7 GB: loading its first snapshot materialised a 39,994,988-uid
delta (now read only when asked: 4.3 GB → 16 MB, and the worker went from
4.2 to 2.3 GB), and the origin check held every sampled line (now
streamed). Loose ends: one trust list (the bundle inbox obeys `hold key
trust`; `hold vectors import` refused nothing a key's denial or burn said —
fixed), `hold embedder rebaseline`, dead code gone; the migration plan
deleted, its open items above.

**2026-10-08, ~10:00Z (UTC): step 12 DONE — the old names retired; 71d8df9
DEPLOYED.** `app/cli.py` and the deprecated aliases are gone; what was still
useful has a `louise` home (cf54afa: `node health`, `dev genesis
recheck|recount`, `hold records show`, `hold snapshot delta`, `hold index
build|show`, `vectors import --retry`, `hold config show|apply`; 71d8df9:
`hold records fetch`), the README, help page and messages name them, and a
defect found on the way is fixed: `share contribute` never asked the licence
gate. Full suite 3,774 passed; on the node no error, nine collections in
use. `louise node health` exits 1 for the five collections whose embed is
off by the operator's choice — what the old `health` said too. Left for the
operator: the legacy bundle inbox's own trusted-key list has no command;
`backend_gate` (Codex v1/v2) has no `rebaseline` (remove the baseline file);
`app/snapshots.py` and `verify_on_use.resweep` look dead;
`migration-plan.md`'s own deletion (done the same day).

**2026-10-08, ~07:20Z (UTC): the genome collections DELETED; step 13f
found done.** At the operator's yes on the named list: genome-hg38, dm6 and
sacCer3's data (2.2 GB, 190 MB, 19 MB) and origins (860 MB, 40 MB, 3.4 MB)
deleted, after `deploy/16f/drop_mounts.py` took their three bind mounts out
of the app (a missing bind source stops a container starting): app stopped,
compose updated, started; nine collections in use after. Their files are
kept as the record in `config/collections-future/` with a README. No app of
step 13f remains on the node (the per-model apps, the governor and Ollama
were deleted by the operator), so 13f is done.

**2026-10-08, ~00:40Z (UTC): the pre-Codex shape REMOVED; ee86a8e DEPLOYED.**
On the operator's four yeses of the day: trusted lists travel in the seals
member, no text-index member, no `dev adopt`/`migrate pack` (08a4c23,
726b10e), and the legacy shape gone (ee86a8e): a collection file without
`codex` is refused, no `collections/` directory means zero collections,
the synthesized gutenberg and `codex_only` are gone, the old create route
answers 410. Tests: every test's default collection is now a Codex
gutenberg (`isolated_config`, `codex_collection()`); about 50 files ported,
legacy-only tests deleted; 3,787 passed. On the node nothing changed: all
nine served collections already named a Codex (`codex_only` was on);
wikivec and the three genome collections stay parked and can be served
only once rebuilt with a Codex. Between the deploys the operator
restarted the NAS (booted 23:51Z on 10-07); it came back clean. Left of the legacy work:
`backend_gate` keeps its probe for Codex v1/v2 (five live collections are
v2), the codex-is-None guards for objects tests build by hand, the
Library's preview route and ack button.

**2026-10-07, ~22:00Z (UTC): the node back; gutenberg's snapshot 2026-09;
408ac93 DEPLOYED (`41e3c9c2…`).** The node's journal stops at 19:47Z (an
LXC machine terminated, then nothing) and resumes only at the operator's
power cycle at 21:02Z: a hang, no OOM line. The rebuild had finished
`records build` (79,562 records, 1,637 shards promoted) and validation; the
power-off killed `snapshot create`, which was re-run alone in the same
image: snapshot `2026-09`, 10,474,568 chunks, `sha256:8a06dec6…`, signed.
gutenberg resumed; the two containers and the `build-gutenberg` tag gone.
Full suite 3,801 passed, then 408ac93 deployed: no errors, every
collection in use, none integrity violated, `node show` live. The next
records build needs the two equivalence runs under `41e3c9c2…` first.

**2026-10-07, ~20:50Z (UTC): steps 10, 11, 14 and the additive half of 12
built and committed; NOT deployed — the node is unreachable.** Since at
least 19:51Z (found when the rebuild was next checked) the node answers
nothing on any path (LAN ping, SSH, the TrueNAS UI,
Tailscale); the router does. The gutenberg rebuild container
(`louise-gutenberg-rebuild`, gutenberg paused in the worker) was running
when it went; its outcome, the node's state and the cause are unknown until
someone is at the machine. Built meanwhile, each with tests, full suite
run before any deploy:
- step 10: the *integrity violated* halt (c731cce); XAdES, the trusted
  lists' own signatures checked against the EU list of trusted lists,
  revocation captured (embedded, given, fetched on the budget), live
  `origin-seal` (b6fefa2) — the XML verifier proven against the published
  EU LOTL and the IS, IT, MT, LI lists;
- step 11: figures on results and `GET /api/ask/figure`; `resolve_in` for
  edges into other collections (ae944b9); and a defect fixed first:
  `Snapshot.shard_of` compared ids as text and ignored slices;
- step 14: `node show` (ecfd959);
- step 12: `louise dev` (b2f3d29); the retirement blocked, reasons in the
  plan.
Decisions waiting for the operator: the trusted lists travelling in the
seals member (a wire-format change); dropping `index-<slice>` (9f) and
`dev migrate pack|adopt`; what becomes of the legacy shape that still
loads the frozen genome collections. Owed on the node: re-run the two
equivalence checks after the next deploy; resume gutenberg; remove the
rebuild container and its `louise-rag:build-gutenberg` tag; scheduled
checks (step 14) measured there first.

**2026-10-07, ~18:50Z (UTC): `image-equivalence`; gutenberg and pubmed
both buildable under the running image; 62eb017 DEPLOYED (`1156f11f…`).**
On the operator's rule — "the source compliancy is after the parsing of
the useless information, that only matters" — an image is now also
equivalent when it parses the same origin bytes into the same records as
the Codex's reference image (03 §3.2 row, 03 §3.4, 09 §9.8): a seeded
sample re-parsed by `tools/reparse_lines.py` under the reference image,
then `hold check --checks image-equivalence,chunks --against FILE` under
the image to be proven. Never planned, never counted toward a trust level.
gutenberg: its own kept image `b8ea620f…` re-parsed 3,972 records over 82
shards, the running image re-parsed all 3,972 byte for byte the same,
chunks passed — equivalent, though its records are out of date against
the mirror (that is records-vs-origin's finding, and it stands). pubmed:
records-vs-origin and chunks pass again under the new image (19,723
re-parsed exactly, 1,983,529 re-cut). Both may be built now; every later
deploy needs the two runs again (about 25 minutes for gutenberg, 11 for
pubmed).


**2026-10-07, ~14:55Z (UTC): the deposit collections re-made; the moved
outcome live; 3d9036b DEPLOYED.** The node rebooted at 14:00Z on its own —
a TrueNAS update (kernel 6.18.42 → 6.18.52) — and was down ~30 minutes;
the new GPU driver gave RAGE new run ids, which the worker gated by itself
(16/16). LocalAGI's two test collections were re-made under their names on
the operator's word: `deposit-granite` (`sha256:163321a3…`, empty) and
`louise-stop` (`sha256:6e12e447…`). Their carry step failed silently — a
`docker exec` without `-i` takes no input — and the forget ran anyway;
`louise-stop`'s three entries were restored byte for byte from the hourly
ZFS snapshot, its `https://example.com/` subscription registered again;
LocalAGI's own routes list, search and show the source. Re-making them
found a defect, fixed in 3d9036b: the worker kept a forgotten collection's
catalog writer, so the one re-made under its name could not write. 03 §3.2
amended on the operator's yes: a record identical but for `origin_hash` is
MOVED, not failed. Equivalence under `4c47498a…` (the image 3d9036b runs):
**pubmed PASS** again (44,613 re-parsed byte for byte, 1,948,582 re-cut,
11 min); **gutenberg FAIL, correctly**: 2,091 moved and 30 genuinely
different — 19 with corrected texts, the rest with changed catalogue
entries (authors' dates, aliases, web pages) — every one a change Project
Gutenberg made after the build, which the current parser renders
faithfully. Gutenberg's records are out of date against the mirror;
equivalence cannot be shown against a source that moved in content.


**2026-10-07, ~13:20Z (UTC): the operator's three decisions, done;
c332c93 DEPLOYED.** (1) The runbook now tells web and worker the image
they run (`deploy/16f/set_platform_image.py`, checked in phase 6): first
run `85fd59f1…` → `bcdeaa7d…`, kept on tank. The certified and open test
collections were re-made from the REAL books 10001–10003 (the old ones
were fixture texts under a mirror's origin spec): `gutenberg-mini-certified`
(`sha256:5d0bc528…`, certified, records-vs-origin in its plan) and
`gutenberg-mini-open-2` (`sha256:90cfbccd…`); the old two are kept and no
longer served. Both left quarantine on the second peer by the book. The
deposit collections still name the stale image — left for the operator.
Equivalence of `bcdeaa7d…` under 03 §3.4: **pubmed PASS** (24,241 records
re-parsed byte for byte, 2,014,540 re-cut, 12 min) — it may be built under
the current image; **gutenberg FAIL** on records-vs-origin (2,138 of 4,074
differing, chunks pass) — but every difference sampled (25 of 25) is
`origin_hash` alone, and every one of those book folders was rewritten on
2026-10-05, after the snapshot: the mirror moved, the parser did not. Its
own image would fail the same check, so gutenberg's builds stay with
`codex-gutenberg` until the check can tell a moved origin from a different
parser (a proposal, not a change). wikipedia has no snapshot yet; its first
records build needs its own image or this one's equivalence. (2) 09 §9.9
amended, version-2 benchmarks live: the node advertises five spaces; the
second peer led the certified collection's shard and the node took its
239-chunk bundle in. (3) Done with (1). Since every deploy now changes the
declared image, HEAD (a log-message fix and notes) was NOT deployed today,
so the equivalence just proven holds for the image running.


**2026-10-07, ~10:15Z (UTC): step 9 LIVE with a second peer; Louise on
any Docker host; deployed through b741bfb.** Backups removed on the node
first (operator: the `pre-*` images and `src.old/`). `deploy/docker/`
runs a node on any Docker host (compose, an env example, `up.sh`;
first boot by `app/node_init.py`), and the second peer runs from it on
the development PC's Docker Desktop (`louise-peer2`, its env file in the
gitignored `deploy/reference/`). Every sub-step of step 9 ran between it
and the node over the LAN — digests both ways, `share fetch` of the
certified collection and of `gutenberg-mini-open` (open, Codex version
4, made for the test), members by torrent in seconds, the fetched
collection scanned from its shards and searched on the second peer with
its certification on each hit, quarantine cleared there by the tooling
(records-vs-origin run in the Codex's own image, loaded from tank),
attestations both ways, `share contribute` on the second peer, and the
node taking its two bundles in (T0–T2 pass, 5 chunks landed). Thirteen
defects found and fixed, each with a test, among them: a fetched
collection acquiring from the origin (an rsync listing of the whole
Gutenberg mirror was stopped before a byte was written), the pointer
never put into the DHT, the attestation step marking bundle, snapshot,
codex and flag events acted before their handlers ran, and the vector
leg withheld for every collection that holds vectors without embedding.
The whole account and the three decisions it leaves for the operator —
the node's stale `LOUISE_PLATFORM_IMAGE` (`85fd59f1…`, so every Codex
created since 2026-09-21 names an image that did not build it), no
benchmark for version-2 collections (a wire-format question), and the
certified plan without records-vs-origin — are in
[`second-peer-2026-10-07.md`](second-peer-2026-10-07.md).


**2026-10-07, ~08:00Z (UTC): the first certified collection LIVE; c019243
then bb39573 DEPLOYED.** `gutenberg-744ba8430fed` (id `sha256:744ba8430fed…`)
is held on the node: created in gutenberg's space (`--space-from`,
`cleaning_version = 2`), three fixture books, records built, validation
written, snapshot `2026-09` signed and certified by this node's key,
pointer seq 1. The anchor document at the Tailscale domain lists it (over
the Funnel too); `hold key anchor` → anchored; `hold check` roots,
records, chunks, license, signature, anchor all PASS; `collection show`
carries `certification` (anchored, certified_by this node's key); the
pointer is served at `/.well-known/louise-pointer/<hex>.json`. As the
quarantine guinea pig it cleared its own quarantine through the tooling
alone: the calibration gate (`hold embedder check --accept`, 16/16 at
median 1.0000, after `node pause <it>` — the worker's lock) and
records-vs-origin against the kept origin (2 of 2, 0 differing); state
in-use; `ask search village` returns its two hits. What it found, each
fixed before proceeding: (1) `hold collection create --name` was parsed
and never passed, so the collection keeps 07 §7.8's default name for its
life — the recipe is `config/recipes/gutenberg-mini-certified.toml`, the
origin folder `data/collections/gutenberg-744ba8430fed/origin`; fixed on
the command line, the job and the route (`HoldCreateBody.name`) in
c019243; (2) the two well-known routes iterated
`load_all(skip_unreadable=True)`'s pair, so the anchor listed no
collection — c019243; (3) `ask search` naming a withheld collection ended
in a traceback: the service's HTTP refusals are exit 2 under the CLI —
bb39573. Staged proofs on the node per deploy: tests/deposit 111,
tests/hold 740, tests/share 110, tests/ask 48 + 18; the whole suite on
the workstation at c019243, 3724. Seen live on bb39573: `ask search
--collections nope` answers `louise: unknown collection(s)` and exit 2;
the certified hits carry `certification` (anchored, certified_by this
node's key); the worker's first minutes carry no error or warning line.
Backups `pre-c019243`, `pre-bb39573`; the seven older `pre-*` images of
the last two days removed (the runbook tags one per deploy).
Noted, not changed: `records-vs-origin` is not in the collection's own
plan (`validation.checks`) — the peer gate asked for it, with `--origin`.
The genome collections stay frozen. Another model continues from here:
[`spec-review-2026-10-07.md`](spec-review-2026-10-07.md) first.


**2026-10-07, ~06:55Z (UTC): the certifier decided; the specs reviewed.**
On the operator's "do it yourself": the certifier key is this node's
own, the anchor the node's Tailscale domain
(`https://mynass-de.tail559dc2.ts.net/.well-known/louise-certifier.json`,
TLS from Tailscale, the Funnel carrying the `.well-known` paths), the
node serving the anchor document from `[certifier]` and the Codexes it
holds; a recipe's `certifier` now reaches the Codex (it was hard-wired
None); the first certified collection goes up on the node after the deploy
that carries the route — as `gutenberg-744ba8430fed`, the `--name` given
having been dropped (the next entry reports it). [`spec-review-2026-10-07.md`](spec-review-2026-10-07.md)
indexes every amendment made today and every place the code still
stands apart from a document. The genome collections stay frozen.

**2026-10-07, ~06:00Z (UTC): the seals DEPLOYED as 55d0970.** The
node's image rebuilt with the four libraries; `tests/hold` (736, the
seal tests among them) passed in the staged image; the restart waited
on its stop job (SUCCESS). Web healthy, the libraries import in the
running container, no error line in the worker's first minutes. There
is no certified collection to seal or check live: the first live
`records seal` waits on a certifier (the operator's key and anchor).
PAdES (next entry's addendum) is committed after it, not deployed.

**2026-10-07, ~05:55Z (UTC): step 10, third brick — the seals, on the
operator's yes to asn1crypto, cryptography, pyhanko and
pyhanko-certvalidator.** A CAdES `.p7m` read, captured detached into
09 §9.15's member (`hold records seal`, each seal validated first), the
trusted list kept by its hash, and `origin-seal` in ARCHIVAL mode
through `hold check` / `hold records check --seal`: the signature over
the captured digest, the certificate path at the captured moment
against the captured chain and the list's anchors, nothing fetched —
proven in tests against a CA and a signer made in the test, the
network unplugged; a foreign list, a tampered digest, a swapped chain,
a lapsed certificate each refused. PAdES as well: the signature inside
a PDF read with pyhanko's reader and validated the same way (a PDF
signed in the test). Owed: live mode, XAdES, captured revocation, the
list's own signature, the Gazzetta plugin (plan). Deployed as the next
entry says; the image rebuilt with the new libraries.

**2026-10-07, 05:00Z (UTC): tree facets to depth 1 DEPLOYED as
7ecac42.** The restart waited on its stop job again (SUCCESS). The
maintainer's pubmed pass ran 05:03–05:11 (two streamed passes over 33 M
rows): `subjects` now lists its 30 768 MeSH descriptors with this peer's
counts (Humans 23 222 703, Female 10 510 179, Male 10 193 344, …) and is
browsable through `ask facets pubmed subjects`; its postings were not
built — 91 683 072 rows for the 27 319 descriptors narrow enough to
filter by, past the 20 M bound — and the listing says so
(`filterable: false`); `lang` has 55 values and is filterable;
`creators` stays uncounted (over 200 000 distinct after 155 185
records). Worker RSS peaked at 4.3 GB through the pass, beside the
embed stream. Backup image `pre-7ecac42`.

**2026-10-07, 04:30Z (UTC): the bounded facets pass and step 9f's
first brick DEPLOYED as c5678a2.** Web and worker on image `f0ad42b9…`;
the runbook's restart phase waited on the stop job (SUCCESS, STOPPED,
then RUNNING) — the first restart through the fixed phase. The node was
then unpaused at 04:19Z and the worker watched for 20 minutes: the
maintainer's pass over pubmed ran 04:19:44–04:24:43 and the worker's
RSS peaked at 5.2 GB (the embed stream running beside it), settling at
1.8 GB; `creators` stopped at 200 000 distinct values after 155 185
records and `subjects` after 1 572 163 (the MeSH descriptor/qualifier
paths), both "not counted on this peer" as designed; `lang` counted and
its narrow values indexed. So on pubmed no facet but `lang` is listed
or filterable here. Done right after (not yet deployed): a tree facet
whose full paths explode is counted to depth 1 — the descriptors — and
posted at that depth, a deeper filter refused by name; pubmed's
`subjects` will list its ~30 000 MeSH descriptors on the next pass.
Backup image `pre-c5678a2`.

**2026-10-07, 03:01Z (UTC): INCIDENT — the worker OOM-killed by the
facets pass; the node rebooted at 03:37Z; the pass bounded.** What
happened: 684931d's index maintainer counted pubmed's facets for the
first time (the first brick of step 11, deployed at 02:45Z). The pass
kept every (value, id) posting of a field in a Python list and every
distinct value in a dict — unbounded — and on 33 million records the
worker reached 34.6 GB anon RSS; the kernel killed it at 05:01:09 local
(03:01Z). The killed process left one thread spinning in kernel space
with SIGKILL pending and undelivered, so Docker could not stop the
container ("did not receive an exit event"); the b4fface deploy's
`app.stop` therefore FAILED after removing the web and gpu-probe
containers, and the runbook — polling the app state for 120 s instead
of waiting for the stop job — called `app.start` on top of it: the web
was down from 03:31Z. Nothing else was stuck (no D-state processes,
pools healthy; an earlier "tank hung" reading was a wrong path). The
node mode was set to `paused` in node.sqlite from the host and the node
rebooted through the middleware (job 2427) so the worker comes back
idle. FIXED in the tree: `app/ask/facets.py` streams postings in
batches, stops counting a field past MAX_DISTINCT (200 000) distinct
values and says so, writes postings only for values of at most
`filters.MAX_IDS` records (a broader value is refused by its count
before any posting is read), and writes none past POSTINGS_ROWS_BOUND
(20 M) per field, clearing an older snapshot's; the maintainer passes
the node's stop and logs each field's outcome. The runbook now waits
for the stop JOB and refuses to start on a failed stop. 03:57Z: the node back (14 min
to boot); the app was CRASHED with the old worker container left over
as exited and no web — `app.stop` (now able to remove it) then
`app.start`, both waited on as jobs: web, worker and gpu-probe up on
b4fface's image, the web healthy, the worker idle at 140 MB in paused
mode. Web down 03:31Z–03:57Z. Pending: the node unpaused once this fix
is deployed, and the first maintainer pass over pubmed watched (RSS)
after it.

**2026-10-07, ~03:25Z (UTC): step 10, second brick — the pointer on
the wire; 9e's leftover; a T4 defect fixed.** The seeder puts this
peer's own certified pointer into the DHT (salt = the collection's raw
id, once per announcement interval); `share fetch` of a certified
collection follows the pointer — DHT, then the well-known path — and
refuses a snapshot whose hash differs. Another key's forgery proof is
reproduced through T4's queue when the chunk is held imported from the
named producer. FIXED before anything else: the T4 brick deployed as
684931d burned the producer in the ledger's bare hex, not the
`ed25519:` form the trust level compares against — a burn would never
have counted; `verify_on_use.key_text` is the one form now. tests:
share/test_pointer 4, share/test_reproduce 1, verify-on-use +1. Not
deployed (node: 684931d) — to deploy next, for the fix. Full-suite
housekeeping: two once-only setup/timing errors today (test_accept's
torrent exchange, test_worker_rotation's fixture setup) passed alone
and in the runs before and after; from now the full suite's whole log
is kept, so a third has its traceback.

**2026-10-07, ~03:05Z (UTC): step 11, sixth brick — `freshness`.** The
top five of each list asked of their origins now (a plugin `current()`
per origin: Gutenberg's cache, E-utilities, the export API; the archive
layer refused as `unavailable`, a fourth state 05 §5.3 now names), the
content hash compared, the current text beside the lists as a fact
where the licence allows. tests/ask +5 (49). Not deployed (node:
684931d). Not tried against the live origins yet: the first live
`--freshness` goes here after the deploy. With this, step 11's remaining
items wait on content that does not exist yet (plates, relations).

**2026-10-07, 02:45Z (UTC): steps 11 (five bricks) and 10 (first brick)
DEPLOYED as 684931d.** Web and worker on image `497e16a9…`;
`src/.deployed-commit` = 684931d; the staged image passed tests/deposit,
tests/hold, tests/share, tests/ask and the verify-on-use tests on the
node. First minutes: the maintainer counted gutenberg's facets for
2026-08 (lang, subjects) within a minute of the start, no error or
warning line. Live, through `/api/ask/*` on the node: `ask chat` answered
a question about Gutenberg in two rounds (one `louise_search` call, a
cited answer) in 2 min 0 s — the chat model's time, through RAGE; a
`lang=en` filter on gutenberg was refused as 63 124 records against the
20 000 bound, a `subjects=Whaling -- Fiction` filter admitted 13 and put
Moby-Dick first; `as_of 2026-09-02` answered from 2026-08 and `as_of
2026-07-02` withheld gutenberg whole, naming the first held snapshot.
Backup image `pre-684931d` beside `pre-d596355`; `src.old/` is 684931d's
predecessor. Nothing cited an imported chunk yet (T4 unmeasured live).

**2026-10-07, ~02:35Z (UTC): step 10 started — certified collections, the
dependency-free half.** The anchor (03 §3.7; 09 §9.11) fetched, kept,
refreshed daily with jitter by the worker, judged by `check_anchor` and
`hold key anchor`; `certification` beside the trust level on results
and in `hold collection show`; the manifest-only gate at promotion (03
§3.8) — a root no certifier-signed snapshot lists is set aside; `hold
collection certify` signing the sidecar and writing the pointer, served
at `/.well-known/louise-pointer/<hex>.json`; `hold key create | trust |
show`. tests: hold/test_anchor 4, test_certify 4, test_node_key 3,
test_check +1. Owed: BEP 44 publication of the pointer, the
integrity-violated halt for a shard found already in the copy, the
seals (blocked on the dependency decision proposed in the plan: asn1crypto,
cryptography, pyhanko), the Gazzetta plugin. Not deployed (node:
d596355). Flake seen once, not reproduced: the full suite before 2ae8662
failed `tests/share/test_accept.py::test_the_survivor_takes_the_
announced_bundle_in_and_serves_it` (real libtorrent sessions on
localhost, a 30 s metadata wait); it passed alone and in the two suites
before. A second occurrence is to be investigated, not retried.

**2026-10-07, ~02:15Z (UTC): step 11, fourth and fifth bricks — `as_of` and
`ask chat`.** `--as-of DATE` answers from the newest held snapshot whose
cutoff is at or before the date (origin clock, `update.day`): records
changed since are dropped and their shards named, every result says
which snapshot answered, a gap in the chain or a date before the first
snapshot withholds the collection and says so. `ask chat` /
`/api/ask/chat`: the chat model with the three tools as functions in
both of CHAT_URL's dialects, bounded rounds, every call and its result
returned with the answer. tests/ask: 44 (+11), test_louise_level
unchanged. Not deployed (node: d596355). Not tried against the live
chat model yet: the first live `ask chat` goes into STATUS after the
deploy.

**2026-10-06, ~20:00Z (UTC): step 11, third brick — T4 on the chunks a search
cites.** Every search queues the imported chunks it returns
(`verify_queue`); the worker re-embeds them in passes of 32 from the
index-maintainer turn, asynchronously, only in a mode that embeds; a
miss must reproduce on a second recompute to be a failure; a reproduced
miss writes and publishes a `forgery` attestation (09 §9.8 amended: the
proof names the encoder) and burns the key in `state/trust.json`, which
the trust level now reads. tests: bundles/test_verify_on_use +7,
hold/test_forgery 3. Not deployed (node: d596355). Unmeasured live: no
imported chunk has been cited on the node yet (its bundles are its own).

**2026-10-06, ~19:30Z (UTC): step 11, second brick — facet filters.** 05 §5.2
as a prefilter on both legs: the maintainer's facet pass also writes
postings (`index/facets/postings.sqlite`, per snapshot);
`app/ask/filters.py` resolves a filter to the records it admits, refusing
what it cannot answer (undeclared, range, not indexed yet, over 20 000
records); `query_service.run_search(filters=)` → `retrieval` `book_ids`
on catalog, FTS and dense. The response carries `filters` and
`filtered` (records admitted per collection). tests/ask: 29. Not deployed
(node: d596355).

**2026-10-06, ~19:15Z (UTC): step 11 started — `ask`'s first brick.** `app/ask/`:
`louise_search`'s shape over the existing retrieval (lists per space,
§5.3's fields, legs' ranks, trust level), `louise_lookup`, `louise_traverse`
over an edge index, facets counted per snapshot by the index maintainer,
the three tools over MCP (stdio and `/api/ask/mcp`, no library), the
`/api/ask/*` routes and the `ask` CLI surface. tests/ask: 23. Owed:
filters, as_of, freshness, figures, T4 on use, `ask chat`.

**2026-10-06, ~19:00Z (UTC): 9e, fifth brick — codex and flag events, duplicates
as checks.** The catalogue of announced Codexes (node.sqlite, `share peers
show`); a flag's attestations pulled and kept, a stranger's lone failure a
claim until reproduced or seconded; a second bundle over held chunks
compared and attested as `vectors-sample` naming both producers, not
stored. tests/share: 106. Not deployed (node: 6ae33fe). 9e's remaining
item, local reproduction of a received flag, waits on a targeted `hold
check`; T4 goes with step 11.

**2026-10-06, ~18:35Z (UTC): the trust level, and `snapshot` events.**
`app/share/levels.py` computes 03 §3.6's level from the attestations held
and the bundles accepted — independent keys and seeds, the thresholds, the
flag rules under the images that count; `hold collection show` prints
`trust_level` and `trust_detail`. The worker pulls a newer snapshot a peer
announces (`accept.newer_snapshots`). tests/share: 102. Not deployed
(node: 6ae33fe).

**2026-10-06, 18:12Z (UTC): 9e DEPLOYED as 6ae33fe.** Web and worker on
image `cb0667a9…`; `src/.deployed-commit` = 6ae33fe. The runbook ran whole
this time (the tarball was the restricted path list; the staged image
passed tests/deposit, tests/hold and tests/share — 96). First minutes:
every collection's threads up, no share, error or warning line, the only
listening socket Docker's DNS (nothing shares, nothing is announced, no
peer is known). Backup image `pre-6ae33fe` alone. Also on this deploy, in
the same tree but committed after it: `snapshot` events acted on by the
worker (a newer snapshot announced is pulled from its announcer).

**2026-10-06, ~18:05Z (UTC): 9e, fourth brick — the import side, and 04 §4.1's
survivor on localhost.** `app/share/accept.py`: a `bundle` event's four
files fetched by their torrents, judged by this peer's ladder, landed,
covered, served from here. The `bundle` event and the digest's `members`
entry now carry `files` for a member no snapshot lists (09 §9.9
amendment — the operator's to veto). Two faults of 9b's seeder found on
the way, invisible with record shards alone: a one-file tree at its root
is a single-file torrent to libtorrent (served from `<save path>/<file>`),
and a received piece is not yet a written one (flush and wait before
promoting). tests/share: 96. Not deployed: the node runs b1c8a29 (9c +
9d), so the validation/terms members it would serve are unaffected until
`share = true` somewhere. Next: deploy 9e; then `snapshot`/`codex`/`flag`
events, duplicates as checks, T4 on use; step 10 (`certified`).

**2026-10-06, ~17:30Z (UTC): a race fixed — the WAL conversion.** The full suite
failed once in 3615 (`test_deposit_localagi`, "database is locked" at
`PRAGMA journal_mode=WAL` in `node_state.connect`): two connections opening
a FRESH node.sqlite at once, the loser with no busy handler for that step —
the same race the deposit manifest had on 2026-10-05. `db.wal(con)` now
waits out a lock for about five seconds, and the manifests, node.sqlite
and admission's store convert through it. tests/test_db_wal.py: 4.

**2026-10-06, ~16:55Z (UTC): 9e, third brick — `share contribute`.** The
preconditions of 04 §4.5, rendezvous over the open shards with the
cascade's windows, build + export + own ladder + `bundle` event per shard
led; bundles served as members and offered in the digest, whose
`coverage` lists the accepted ids per shard; neighbours' claims folded
into the map. `share contribute [<collection>] [--once] [--interval S]`.
tests/share: 88. Not deployed. Next: acting on `bundle` events (the
import side), duplicates as checks, the single-survivor test; then step
10 (`certified`).

**2026-10-06, ~16:40Z (UTC): 9e, second brick — `share attest`.** `hold check`
then every attestation written published as an `attestation` event, once
each (the ids published are recorded); the worker's beat keeps a received
attestation beside the catalogue when it verifies and the collection is
held here, before forwarding. `app/share/attest.py`; tests/share: 84. Not
deployed.

**2026-10-06, ~16:25Z (UTC): 9e, first brick — the coverage map.**
`app/share/coverage.py`: 09 §9.10's rows in the manifest, written by
`vectors import` (accepted, fetched) and `vectors export` (local, not yet
accepted), claims from digests beside them counting for nothing; the
per-shard view with the leader and the grace window among the live peers;
producers; 04 §4.7's vitality from signed work. `hold vectors show`
prints it. tests/share: 80. Not deployed.

**2026-10-06, 16:03Z (UTC): 9c + 9d DEPLOYED as b1c8a29.** Web and
worker on image `5866b43a…`; `src/.deployed-commit` = b1c8a29. The runbook
stopped once between the promote and the build — the tarball was a
whole-tree `git archive`, its extras (CLAUDE.md, .gitignore, …) stayed in
`src.new` and `rmdir` failed under `set -e` — and phases 4b–6 were run
alone (scratchpad `finish-b1c8a29.sh`); the runbook now removes the staging
dir whole. Seen live in the first six minutes: every collection's threads
started, no share, error or warning line; the worker's only listening
socket is Docker's DNS — the new `share-fetch` thread found nothing
missing for the five collections (all `slices = "all"`, every member in
its drag) and opened no session; nothing shares (`share = false`
everywhere), so no digest goes out. Backup images: `pre-b1c8a29` alone.
`[node] address` / `peer_port` are not set in the node's platform.toml:
irrelevant until a peer exists — set them with the first `share peers
add`.

**2026-10-06, ~16:00Z (UTC): 9d done — the members move.** `app/share/holdings.py`
and `transport.py`: what a collection's file asks for (`slices` may now be
a list: part of a collection), what the drag holds, the mark after the
rename; each missing file by the torrent the snapshot's infohash names,
into `state/staging/`, root computed, promoted; the ack gate (terms first,
nothing else before `hold collection ack`); the free-space check; a
15-minute backoff when nobody serves. The worker's new `share-fetch`
thread does it every 15 s when the mode fetches; `share fetch --here` does
it in-process. Digests name `peer_port` (09 §9.9, optional). Two peers on
localhost proven in `tests/share/test_transport.py`. tests/share: 73. Not
deployed (node: 6c06b4a). Next: deploy 9c+9d, then 9e (contribute, attest,
the coverage map).

**2026-10-06, ~14:50Z (UTC): 9d, first brick.** `share fetch <collection>
[--from ADDRESS] [--dht] [--encoder URL] [--name N] [--snapshot ID]`
(`app/share/fetch.py`): finds who holds a collection — by hand, by the
neighbours' digests, by the `[peers]` list, by the DHT — pulls its Codex
chain and its snapshot chain, verifies every link and every advertised
hash, bans a liar and asks the next peer, and holds the collection with
its signed snapshots (an unsigned one staged). The members are the second
brick. tests/share: 61. Not deployed (node: 6c06b4a).

**2026-10-06, ~14:35Z (UTC): 9c done.** `share peers add <address> [--key]`
writes platform.toml's `[peers]` list and `[peers.pins]` — desired state
in the file, the rest of it kept byte for byte; `share peers show`; a
digest claiming a pinned address under another key is refused and logged;
every byte the worker sends (digests, events) draws from the UP bucket
first, DELTA class; the DHT announcement names `[node] address`'s port
(the env var is gone). 07 §7.3's "neighbour for three intervals on faith"
amended: a neighbour is a key at an address. tests/share: 50. Not yet
deployed: the node runs 6c06b4a (9b).

**2026-10-06, ~14:10Z (UTC): 9c, first brick.** The peer messages of 09 §9.9 in
code: `/api/share/{digest,event,codex,snapshot,attestations}/…` in the web,
verifying with the sender's key; peers seen, neighbours, bans and event
ids in the node's state database; the worker sends this node's signed
digest hourly to neighbours and the `[peers]` list and forwards events
once. `[node] address` added to 07 §7.8: where this node answers pulls as
peers reach it. tests/share: 42. 9b deployed earlier as 6c06b4a: the
worker quiet, nothing shares yet (every file says `share = false`).

**2026-10-06, ~13:45Z (UTC): 9b, the seeder, done.** `app/share/seeder.py` in
the worker's seeding beat: for every public collection in use whose file
says `share = true`, one torrent per member file of the current snapshot
— hard links under `state/share/<snapshot>/<member>/`, built by
libtorrent, refused when the infohash is not the snapshot's — published
to the existing seeder (which gained a per-torrent `drop`), the collection
and lineage keys announced through the DHT every fifteen minutes while
the mode seeds. `share <collection> [--stop]`, in the spec's bare form,
writes the desired state. PROVEN on localhost: a leecher that knew only
the infohash it computed from the snapshot fetched the member from our
seeder, metadata and piece layers over the wire. tests/share: 30. Owed:
`--slices`/`--kinds` (9d); the reachable announce port (9c).

**2026-10-06, ~13:25Z (UTC): step 9 started — 9a done.** `app/share/members.py`:
bencoding, the exact info dictionary of 09 §9.16 and its infohash, the
four DHT keys, the members a snapshot implies. The test that matters:
libtorrent 2.1, building the torrent from the file, produces the same
bencoded info and the same v2 infohash as our pure computation from
(path, length, root) — 04 §4.3's "two honest publishers land in one
swarm" holds. Next: 9b, the seeder in the worker.

**2026-10-06, ~13:20Z: aligned** (operator: "delete backup images, align the
specs and the deployed image"). No backup image is left (`local` and
`staged` are one image); `src.old` (13 MB) stays as the tree before
865ccbe. 07 §7.3's `forget` line carries `--dry-run` and `--here`; §7.7's
exit-2 list names `collection migrate --apply` beside the two `dev`
commands that are not built; §7.8's `[deposit] space` may be a JSON file.
The node's DEPLOYMENT.md got today's section appended (its copy had
diverged from the local one before today: 686 vs 713 lines; neither was
overwritten). The node runs 865ccbe; main is the same code plus notes.

### 2026-10-03: LocalAI takes the lifecycle; RAGE keeps the run identity

The operator accepted [`rage-over-localai.md`](rage-over-localai.md):
LocalAI will launch, load, preload, cache and evict the models, and RAGE
will state the run of every answer. That is migration step 15. Done the
same day:
- **The spec.** 03 §3.3 says who launches. 09 §9.3 gives an engine
  `options`, the load options a runner sends over its own protocol. The
  field is absent for an engine started by `args`, so no `engine_id`
  changes and nothing in use is re-gated. Codex 4 and calibration
  statement 3 in §9.18.
- **15b.** The one LocalAI patch RAGE cannot work without: `GET /system`
  lists each model's load options and their hash. It is in
  `deploy/localai/patches/` and tested in LocalAI's own suite.
  [`localai-bugs.md`](localai-bugs.md) is now the one list of LocalAI
  action items: 7 defects, 1 patch, 4 requests, none filed yet.

Nothing changed on the node; RAGE as deployed keeps running until 15f.

**15a, the same day**: Codex 4 and calibration statement 3 in the code
(`app/hold/codex.py`, `embedder.py`, `collection.py`;
`tests/hold/test_engine_options.py`, 17). Once deployed, new collections
are written at version 4. Every existing Codex, engine id, run and
statement stays as it is. Whole suite 3439 passed.

**15d, the same day**: RAGE's front over LocalAI, beside today's
(`RAGE_RUNNER=localai`). Runs are read from LocalAI's `/system` and the
kernel: options, argv, environment, the card from the process's DRM
clients, and weights hashed per process. Also built: the admin door, the
panel, the restart, and `PUT`/`DELETE` of LocalAI's YAML. 37 tests against
a fake LocalAI. Not run against a real one: that is 15c on the node.

**15e in part, the same day**: `deploy/rage/Dockerfile.localai` builds the
patched LocalAI (version `58830f7+patches.17b54353635e`) with its SYCL
backend pinned by digest. It ships the six llama.cpp models' YAML and an
app file. Its `smoke` target ran the whole chain on a CPU with a real
LocalAI: first answer 2.17 s with the load, then 0.027 s, a run on every
answer, one run across two restarts. It found a random seed per load (now
pinned) and LocalAI bug B8. Left: the Python models as LocalAI backends,
and the translator client.

**A defect in it, fixed the same day**: LocalAI copies a model's `env:`
into the load call, and the options' path rule refused it, so the six
YAMLs' `NEO_CACHE_DIR` would have stated no run on the node. 09 §9.3 now
states the environment once, as the engine's `env` (operator). A test
states a run for every shipped YAML.

**15e-2, the same day**: the three Python models are LocalAI backends
(`deploy/localai/backends/`), and the translator's batch travels in one
completion prompt (`TRANSLATE_API=completion`, operator). The MedCPT
backend ran on a real LocalAI on a CPU: ranked right, a run on every
answer, the same run after a restart. It found `run.sh` giving relative
`args`, now fixed. DAR-legal-it and MADLAD need torch, which this sandbox
cannot reach: they first run on the node.

**The image, 2026-10-04**: built on the node and published,
`ghcr.io/sourstar/rage@sha256:b203a361a09bd515f0ca1e9293cf30a7a516384fd3785ceecc1941d7bc1a092d`
(`localai-58830f7-1`). Every backend's libraries imported at build; nothing
run on the card yet. Next: 15c, the operator's go/no-go.

**15c, the same day, unattended**: bge-m3 on the B70 under LocalAI passes
wikipedia's own gate three times out of three, 12 % faster than today's
engine. The MedCPT reranker on the card matches today's to 3e-8. It found
LocalAI bug B9: an intermittent NaN fails the whole request. Louise now
retries it as she retries llama-server's null vectors. Not run: the spill
test, DAR-legal-it and MADLAD on the card, because each needs room the live
models hold. The go is the operator's (rage-over-localai §12).

**15f ready, not run (2026-10-04)**: the operator gave the go ("suspend
dar-legal for now, go on with the rest"; DAR-legal-it serves no
collection). Merged to main, rebuilt and published
(`ghcr.io/sourstar/rage@sha256:e64893cf…4e26`). Every model was tried under
LocalAI beside the live one, which found and fixed chat's template. The
swap itself was refused to the agent as a production deploy, so it is two
scripts on the node for the operator, with a backout (migration-plan 15f).

**15f DONE, 2026-10-05: RAGE runs over LocalAI on the node.** The operator
ran both phases. Every model states a run, and every collection's
calibration answers match the old engine's (lowest cosine 0.999833).
Louise runs the merged main with `TRANSLATE_API=completion`, and 1609aa9
since 06:12 UTC: LocalAI's "exceeds the available context size" shrinks
the cut. The operator deployed it, and web and worker run one image. The
old engine's backups stay in `/mnt/ssd5/apps/louise-rage-15c/cutover/backup`
until 15g.

**15g, the same day: the llama-swap RAGE retired from the repository**
(legacy-to-delete §8; suite 3378 on the node). It is commit 8390155, which
went in under the previous commit's message by mistake. Its description is
in the commit after it. The node keeps `rage:pre-15f`, `louise-rag:pre-15f`
and the backups until the operator removes them.
- **Louise deployed at 736fc52** (06:47 UTC). The translator now speaks only
  the completion shape: 499 languages, a sentence translated, a null for an
  unknown target. The first attempt stopped itself on a failing test. The
  node's `src/deploy` is overlaid, never replaced, so the Dockerfiles 15g
  deleted were still there; they were removed by hand, with copies in
  `src.old/deploy`.
- **RAGE not redeployed on purpose.** 15g changed its code only by
  deletion, and a new image is a new run for every model and a re-gate of
  every space. `e64893cf…` stays, reproducible from 6246334, and the next
  real RAGE change carries 15g.
- **Leftovers removed, the same day** (operator: "remove every leftover"),
  so there is NO WAY BACK to the llama-swap engine on the node any more:
  - its images (`b10630-1`…`-8`; they stay published on ghcr.io) and the
    first LocalAI build;
  - the rollback tags `louise-rag:pre-15f` and `pre-736fc52`, and the old
    MADLAD app images;
  - `/mnt/ssd5/apps/louise-rage-15c/` (backups, `backout.sh`, the before and
    after vectors), `src.old`, and the scratch trees in `/tmp`;
  - the llama-swap-era copies in RAGE's `config/` and `state/`;
  - dangling images and the build cache, about 85 GB;
  - `TRANSLATE_API` in Louise's app config.

  Kept: the running images, every `louise-rag:codex-*`, the llama.cpp base
  images, and other apps' images, including an `ix-localai` app that is not
  RAGE's.

### 2026-09-22: one branch

The operator: "I want to keep only the main branch." Done, and nothing was
lost, because the split had already collapsed on its own: `main` held
**no** commit the code branch lacked — the specs had been merged forward
all along — so `main` fast-forwarded 463 commits to `louise`'s tip. The one
commit that lived only on `spec/2026-09-21` (a version-2 space copied with
`--space-from` stays version 2, 09 §9.3 and 02 §2.7; `drag/` may be mounted
from other storage, 07 §7.8) was replayed onto `main`; its CLAUDE.md hunk
was already there, byte for byte. A third branch,
`collection/gazzetta-ufficiale-italiana`, was an ancestor of `main` and
carried nothing of its own.

Checked before each deletion (`git rev-list --count main..<branch>`, and a
file-level diff for the replayed one), then `louise`, `spec/2026-09-21` and
`collection/gazzetta-ufficiale-italiana` were deleted here and on the
remote, and the scratch worktree that existed to hold `main` was removed.
`git ls-remote --heads` now lists one ref.

What changes for the work: **specs and code are edited together.** A spec
amendment lands in the same commit as the code it governs, or beside it —
no forward-merging, and no "the spec says X but the branch has not caught
up". `migration-plan.md` (deleted 2026-10-08) §1 keeps the old rule as
history because its steps were written under it; `CLAUDE.md` and
[`README.md`](README.md) no longer name a branch; the fossil
`BRANCHING-INSTRUCTIONS.md` at the repository root — a zip-import recipe
for a branch that never reached the remote — is deleted.


**2026-09-23, through the night — what an eight-lens audit found, and the
three fixes that came out of it.** The operator's demand ("I want the CPU
constantly above 95 %") sent a Workflow over the scheduling path: eight
finder agents on distinct lenses, three adversarial verifiers per finding
(evidence / invariants / impact, majority kills), 117 agents, 9.8M tokens.
Forty-one distinct defects were reported; the cross-lens agreement is the
signal worth recording — four lenses independently reached the sizer, three
reached the pool's partitioning, two each reached the commit/pool serialism,
the double parse, the lock held across the pool round trip, and `lo=1`.

**FIXED, measured on the node each time.**

1. **The pool drained at every wave boundary** (5b98358). `compute` submitted
   `wave` tasks, waited for ALL of them, then submitted the next wave — so
   the children went idle while the wave's slowest task finished alone, and
   wikipedia's per-document cost spans two orders of magnitude, so that task
   is reliably far behind. A batch is cut into ~2 parts per child; the window
   is `share x 2`, and `share` is the children divided by the collections
   active in the last 30 s — so with gutenberg's embed chunking also touching
   the pool, 22 parts went through a window of 10, three barriers per batch.
   Alone, a collection's window already covers every part it cuts, which is
   why no single-collection test ever saw it. A slot is now refilled the
   moment its result is taken.

   | measured | before | after |
   |---|---|---|
   | a document, computed alone | 59 ms of CPU | — |
   | what the pipeline thread saw | 40.8 ms (rdf), 34.8 (fts) | — |
   | documents/s within batches | **19.9** | **29.2 sustained** (and 72.5 once the sizer landed — the table below) |

2. **The sizer fitted a proportional model to an affine cost** (a671818).
   Measured by regressing duration on batch size over 64 batches: a FIXED
   2.7 s (rdf) and 4.5 s (fts) per batch — a tantivy commit, a writer, a
   transaction, a reference store — against 41 and 35 ms per document, with
   a 3 s target. Charging that to a per-document rate reports
   `marginal + fixed/n`, so every batch that ran long made the next one
   smaller, which made the rate worse: a divergent loop that had settled at
   36-234 documents, paying the fixed cost as often as it could. Both terms
   are fitted now, and when the fixed cost alone eats the target the budget
   widens to `BATCH_CEILING_S` (12 s) — because then paying it less often is
   the only way to spend less of the thread on it. A pin keeps the old
   model's collapse in the test suite so nobody restores it.

3. **Wikipedia serialised a hundred pages to return one** (f45994f).
   `_stream_pages` parsed a ~100-page multistream chunk and `ET.tostring`d
   every page; `fetch` uses one. `ET.tostring` is the only step there with no
   C accelerator behind it. The cache holds elements now and the bytes are
   produced for the page asked for, by the identical call — pinned byte for
   byte, as pubmed's reader already was (invariant 11: `origin_hash` is over
   exactly those bytes).

**Measured after each deploy, on the node, wikipedia's rdf and fts (the only
CPU work this node has):**

| | documents/s within batches | mean batch | longest unit | worker CPU |
|---|---|---|---|---|
| before tonight | **19.9** | 285 | 75 s | ~190 %, flat |
| + the pool's window kept full | 29.2 | 285 | — | bursts to 617 % |
| + the affine sizer, + lazy page bytes | **72.5** over 25 min | 1,268 | 57 s | bursts to 926 % |
| + the step damped (final) | **64.3** | 941 | 32 s | bursts to **1007 %** |

66,000 documents went through in the 25 minutes of the third row, against
about 9,000 in the same time before. The last row trades ~11 % of that for
the unit bound, which is what a pause, a mode change and SIGTERM wait on:
57 s units were not something to keep.

**The shape of what is left is visible in those CPU samples**: 108 %, 1007 %,
116 %, 238 %, 107 %, 109 %, 350 %, 209 %. The parse phase uses ten cores and
the commit phase uses one, alternately, on one thread per collection — which
is the audit's first open item and the one that would raise the AVERAGE
rather than the peak. Overlapping batch N's commit with batch N+1's parse is
the shape; it is an optimisation, not a defect, and it was not started at
02:00 with a 3.2x improvement already live.
**Refuted by the verifiers, and worth keeping refuted**: a multi-slot stream
cache. A child's parts are disjoint ranges far apart, so the stream missing
at a part boundary was loaded by a DIFFERENT process and no per-process LRU
can hit it — it would buy memory and nothing else.

**Still open, in the audit's own order of value** (none of it started):
- the pool phase and the commit phase never overlap: one thread per
  collection alternates eleven cores and one;
- the parse pool cuts by equal COUNT, so one multistream block is
  decompressed in 9-22 children instead of once — cutting on the locator's
  byte range would remove the decompress and the parse too;
- every wikipedia document goes through mwparserfromhell about four times
  where once would do (rdf runs the full article parser for five metadata
  columns, then fts parses it again), and `wikitext.parse_all` walks the
  tree ~16 times, 11 of them a full `filter_templates()`;
- `stage_rdf` holds the manifest's BEGIN IMMEDIATE across the whole pool
  round trip and the tantivy commit — the fix `stage_fts` already made;
- `fts.index_books` builds a fresh 64 MB tantivy writer per batch per
  language and blocks the thread in `wait_merging_threads()` — the fix
  `catalog.py` already made for itself;
- `catalog.backfill` holds the catalog write lock across a 200,000-rowid
  window while `stage_rdf` waits on it holding BEGIN IMMEDIATE;
- `refs.get_store()` opens a connection and runs every reference source's
  DDL once per fts batch, inside the write transaction;
- the embed producer opens a fresh SQLite connection, with the full schema
  script, once per document; `_request_vectors` decodes the JSON response
  inside the endpoint's gate, holding the lane during the decode;
- admission halves a collection's children for a neighbour that has not
  submitted for 29 seconds;
- `_record_residual` REPLACES a shard's residual set where `_add_residual`
  unions it, so each pass forgets what earlier passes proved.

### 2026-09-22: a core burned on a query that could not find anything

The operator: "check why the scheduling is not saturating the CPU; I see
louise-rag to be constantly below 35%." It was not idling — nearly half of
what it used was waste.

| measured on the node | value |
|---|---|
| pubmed documents, all embedded at the current version | 39,994,988 |
| left to embed, or at an older version | 0 / 0 |
| one call of the unwindowed version selection | **57,819 ms, 0 rows** |
| py-spy, 25 s, that one line | 2,498 of 5,378 samples (**46 %**) |
| worker CPU | 2.1 of 12 cores |

`_backfill_rows` takes an unwindowed branch when a collection declares no
archive, and pubmed's declaration is gone. There the version selection
(`embed_done=1 AND embed_version<N`) has an index prefix that matches
EVERY embedded document and tests `embed_version` row by row — so it read
40M index entries to find nothing, and the embed producer called it again
at once. It started when the collection was set to embed "all" at 14:36;
that is the only mode that reaches the path.

**The same defect had been fixed once.** f5c4f29 (2026-09-19) bounded this
very walk into rowid windows after measuring it at 105–153 s on the same
collection — but only for collections WITH an archive, the no-archive case
being assumed small. It is the pitfall [`CLAUDE.md`](../../CLAUDE.md) names
twice: a plan that reads `SEARCH ... USING INDEX` while SQLite walks the
corpus behind it, and a cost that scales with the corpus instead of with
the work available. Both branches now page the version walk; the NEW walk
stays unbounded on purpose, because its prefix ends on its own candidates
and a document that clears fts must be embeddable in the same round.

**And the sweep itself.** Windowing alone still read the corpus every two
minutes — ~58 s of CPU per sweep, about a tenth of a core for ever, for a
candidate that cannot exist. A re-embed appears only from a version bump
(a code change, so a restart, which clears these in-memory cursors) or an
import of older vectors (which moves the coverage epoch, same). So a
VERSION sweep that finds nothing now waits an hour; the new walk keeps its
two minutes, and every re-embed an operator can ask for sets
`embed_done=0`, which is the new walk's business.

**After the deploy** (3f28835), measured the same evening:

| | before | after |
|---|---|---|
| that line, py-spy over 25 s | 2,498 samples | **0** |
| wikipedia rdf / fts | 51.7 / 52.0 documents per s | **85.1 / 81.6** |
| wikipedia unit cost, fts / rdf | 4.0–12.8 / 5.6–11.0 ms per doc | **0.4 / 0.3** |
| gutenberg embed, and what is left | 0.0/s — 597 h | **0.1/s — 149 h** |

The first sweep after the restart ran its windows for about ten minutes
and then went silent, as designed. The worker settles at ~1 core between
bursts of 4–6 while the pool is fed.

**What still limits saturation**, none of it a defect:
- only wikipedia has CPU-shaped work left (52 documents/s through both
  extraction and indexing); gutenberg and pubmed have nothing to parse;
- wikipedia's pipeline alternates parallel parsing in 11 children with a
  serial commit into one SQLite writer — the commit is the ceiling;
- gutenberg's remaining 71,641 documents are GPU work: its encoder waited
  705 s against 243 s served in the window before the fix;
- **corrected the next day**: the scheduler's line
  `gutenberg: 33% in 582 turns (4s, 0 units)` is 4 s ACROSS those turns —
  7 ms each — not 4 s per turn as first read here. The index maintainer is
  not a cost. What the profile showed under `_next_reindex` is the fts
  reindex sweep restarting after a deploy: pubmed's 40M in ~32,778-row
  windows, ~155 s of CPU spread over ~1,200 rounds, once, and then the
  cursor's `done` flag makes it free. Left alone.
- two real ones, both fixed the same night (see the entry below): the
  archive drain's candidate walk and a TLS context built per embedding
  request.


**Later the same night — the two remaining wastes, and where the ceiling
actually is.** The operator: "fix the scheduling, I want the CPU above
95 %." Two more wastes found by profile and fixed (13389dd):

| py-spy, worker, 30 s | before | after |
|---|---|---|
| `_candidate_shards` — the archive drain's walk | 338 samples (30 %) | **15** |
| `ssl.create_default_context` under the embed client | 234 (16 %) | **0** |

Then the ceiling was measured rather than argued, and it is not the
scheduler:

- **The pool is not the limit.** Over 30 s all ELEVEN children appear in
  the samples and share the work; 2,210 of 3,047 samples are theirs. The
  total is simply ~1 core: wikipedia's extraction and indexing of the
  articles now in flight is all the CPU work the node has.
- **The children are not starved and not blocked.** The pipeline thread
  waits on the pool in 12 of 12 dumps, which is what a thread that has
  handed out its work looks like; the children are asleep between bursts.
- **Nothing is capped.** `cpu.max` is `max` on both containers; the disks
  are at 12 % utilisation; the host is 79 % idle with 2 % iowait.
- **Per document, the work is small**: 59 ms of CPU on average for
  wikipedia's metadata, 0.1 s for 32 documents' chunking — with a long
  tail (one article in the sample cost 138 ms, and a batch's wall time is
  its dearest document).

**So the machine is idle because the big jobs are not runnable:**

| collection | left | why it is not using the machine |
|---|---|---|
| pubmed | 0 | finished |
| gutenberg | 71,359 documents | GPU-bound: ~297 h at the measured rate, one lane, `-np 1` at the server |
| wikipedia | **30,941,198 documents** | **`embed_mode=off`** |

Wikipedia's embedding is the one switch that would fill this machine:
every document would be chunked (CPU, across the eleven children) and then
embedded (GPU). It is off by an operator decision this file already
records — "its dense side is meant to come from wikivec" — and the archive
has nearly finished delivering: of the first 401,408 candidates only 1,192
(0.3 %) still carry an archive locator. Turning it on commits the node to
30.9M documents of GPU work, which is why it is not a thing to flip
unasked.

### 2026-09-22: a reboot renumbered the cards and RAGE would not start

The operator, after a reboot: `louise-rage` start fails with

    error while creating mount source path
    '/sys/class/drm/card0/device/tile0/gt0/freq0':
    mkdir /sys/class/drm/card0/device/tile0: operation not permitted

**DRM card numbers are handed out in load order and swap across boots.**
The Arc had been `card0` and came back `card1`; `card0` is now the
processor's Radeon graphics, which has no `tile0`, so Docker tried to
CREATE the missing source under `/sys` and the container never started.
Not degraded — dead: a bind that cannot be made stops the app, and the
clock is otherwise optional.

[`rage.md`](rage.md) §18 had already said this of render nodes, when the
driver in a run identity moved to the PCI address for the same reason:
*a number is not a name, an address moves only with the hardware.* The
clock bind was the last piece still holding a number.

The address cannot go in the bind either: TrueNAS parses `source:target:
mode` and an address has colons — the rule `tests/rage/test_deploy_files.py`
already carried. So the bind names a colon-free symlink on the pool, made
once, and the test now refuses a card-numbered source as well (f1b9dfc):

    ln -sfn /sys/bus/pci/devices/0000:03:00.0/tile0/gt0/freq0 \
            /mnt/ssd5/apps/louise-rage/gpufreq

Checked in a throwaway container before the app config was touched: Docker
resolves the symlink and the container both reads and WRITES through it
(`min_freq` written back to itself). After the change the app came up,
seven models ready, `/louise/gpu` reading the clock again (400–2800 MHz,
not pinned). Way back: `config/app-config.pre-gpufreq-symlink.json`
(mode 600). Louise's own containers had stayed up throughout — nine hours,
the worker deferring its embeddings, which is what a deferral is for.

### 2026-09-21: the v1.0 collections made compliant, nothing re-fetched

The operator: "make my collections compliant again without re-fetching a
single bit". Detail is in [`v1-rebuild.md`](v1-rebuild.md) and
[`storage-tiering.md`](storage-tiering.md) §7.

- **The image a Codex names had been pruned** for gutenberg and wikipedia.
  They were recovered from a docker-dataset snapshot on the day it
  expired. Every Codex image is now kept on the archive pool
  (`louise-platform-images/`) and tagged `louise-rag:codex-<name>`.
- **A verdict belongs to the image that ran it** (a7cf99c, 1bf3058,
  03 §3.4/§3.6):
  - `hold check` wrote the Codex's image into every attestation, which
    quarantined pubmed on another image's equivalence failure;
  - builds now refuse an image that is neither the reference nor
    equivalent;
  - `records build` honours SIGTERM, promotes nothing when stopped, and
    refuses to resume another Codex's staging.
- **gutenberg:** records built by its own image (79,445 records, 1,635
  shards, 64,736 plates), snapshot `2026-08`, and every deterministic
  check passes under its image. Its drag is on tank.
- **wikipedia:** the same route, running (52.9M records).
- **pubmed:** the v1.0 root `sha256:e1711334…`, created with `--space-from`
  the development Codex, so it is **the same space** and its ~40M vectors
  stay (2c0d984; spec on branch `spec/2026-09-21`). It has parser 8 and
  the four facets. Its records resumed a build already cut (Rule 1:
  byte-identical record code), and the switch is scripted. The archive
  shards still on disk were rightly refused, not a bug.
- **Storage:**
  - the drag and the exports live on tank (0eca48c, `EXPORT_DIR`);
  - the legacy wikivec/wikipedia and the retired gutenberg trees are
    deleted.
- **Rule 1** (`CLAUDE.md`): a development system takes the shortcut that
  recomputes least, the artifact indistinguishable from one made by the
  book.

### 2026-09-21: a PubMed search ran 75 minutes; search and RAGE fixes

Committed on `louise`, not deployed; the other session tests them together.

- **What cost the time: one PubMed search was still running after 75
  minutes, its caller long gone** (py-spy on the live web, through a
  pid-namespace sidecar). The entity boost fetched every document tagged
  with the query's entities, joining `doc_keys` to `documents`. A manifest
  has **no ANALYZE statistics**, so SQLite took `source = ?` as selective
  and walked all 40.0 M documents. And "BRCA2" expanded to 21,968 ClinVar
  variants, each one a boost key and a pinned fact. 98ae00d fixed it:
  - the boost only asks whether a candidate is tagged: 3.1–3.9 ms on the
    real manifest;
  - the boost takes only the entities the query names;
  - facts keep docs/05's one hop, at most 25 per link (668 ms → 24.6 ms),
    and `facts_withheld` counts what was left out.

  The fixture manifest reproduces the node's query plan, so plan checks on
  it are trustworthy (`tests/test_entity_boost.py`).
- **A search whose caller has gone now stops** (89361b3, `app/cancel.py`).
  `Connection.interrupt()` alone misses statements that start after it, so
  a progress handler checks too. `Cancelled` is a `BaseException`, so it is
  never logged as a failed collection.
- **The query instruction comes from the Codex** (30c7276, which reverted
  the node-side `query_prefix` of 25b30f3). See
  [`gazzetta-plugin.md`](gazzetta-plugin.md) §7.4b.
- **Ask tab:**
  - rerank depth up to 300, with a budget scaled from 8 s at 50 to 48 s
    at 300 (9df8190; spec 336a5ee);
  - the answer's opening stays in view while it streams (576224b);
  - a fact reached through a link names that link (da098b4, 05 §5.4).
- **RAGE b10630-8 is live.** When a model does not fit on the card, RAGE
  frees space by evicting lower-priority models first (93ccefa), and it
  reads a request's body before waiting for its model to load (9f77a99).
  See [`rage.md`](rage.md) (amended 2026-09-21).

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

**Amended 2026-09-13: the 38,324 were stuck too, for a second reason,
and "COMPLETE" below was wrong.** See *A final shard that fts overtakes*
further down. The rest of this section stands.

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

### A final shard that fts overtakes — found and fixed 2026-09-13

The 38,324 documents left "unembedded INSIDE the archive's range" above
were not residue. They were a second stall with the same shape as the
first: two correct halves with no path between them.

`final` is decided by `pending_in_range_sql`, which counts documents
still **upstream** of fts, and it is evaluated at the END of a pass that
walked the shard's ids **before** it. A document that crosses fts in
between is counted as "not waiting" — it is past fts — while never
having been offered the shard's vector. A final shard is never walked
again, so that vector could never arrive.

The distribution is the proof. The stranded documents sit exactly where
fts was working as the archive drained:

| shard | stranded | | shard | stranded |
|---|---|---|---|---|
| 36 | 5,784 | | 27 | 1,263 |
| 28 | 5,240 | | 23 | 404 |
| 33 | 4,423 | | 26 | 327 |
| 32 | 4,162 | | 25 | 249 |
| 29 | 3,495 | | 21 | 176 |
| 34 | 3,145 | | 20 | 160 |

Shard 20, which fts had long left behind, lost 160. Shard 36, which fts
was inside, lost 5,784.

**Why an event and not a poll.** Recounting each final shard per round
costs a range count per shard and cannot converge: a document the archive
genuinely does not contain is unserved forever, so the shard would reopen
forever while `_covered_ranges` keeps the local encoder out of its range
— a livelock in place of a gap. `genesis.reopen_finals_for_range` fires
from `stage_fts` only when fts actually crosses the range, which is the
only way new demand can appear. The re-pass imports what it can and
finalises again; a document the archive lacks generates no further event.
Cost on the fts path is one read of a 37-row table with a primary key,
pinned by a cost test.

`louise genesis recheck <collection>` is the one-shot repair for shards
finalised before the hook existed — no event will ever fire for those.

**The general lesson, and it is the third time.** Both pubmed stalls were
a state machine whose terminal state was decided from a condition that
keeps changing after the decision. `applied` + files-gone had no path
forward; `final` + fts-still-arriving had no path forward. Neither showed
as an error, because in both cases every component was doing exactly what
it was told. **When a stage can declare itself done, ask what can still
become true afterwards — and make the thing that becomes true say so.**

### The re-fetch never came — found and fixed 2026-09-19

The reopen fixed nothing on the node: from the recheck of 2026-09-13
onwards, pubmed's 38,324 were still unembedded six days later ("pubmed
embedding never ends"). All 37 shards sat `applied` with `pending`
unknown and due. Every shard's files had been deleted when it finalised,
and the cache directory held 1 KB. Yet the fetcher logged "105 GiB of
shards on disk waiting to finalise" against its 24 GiB bound, and fetched
nothing.

`resident_bytes` counted every row in a resident state **from the ledger
alone**, at the 2.9 GiB size hint: 37 × 2.9 GiB came to 105 GiB for files
that did not exist. So the three parts locked each other:
- the fetcher would not fetch until a shard finalised;
- the drain walks only shards on disk, so it had nothing to finalise;
- `_covered_ranges` still gave the ranges to the archive, so the local
  encoder skipped all 38,324.

This is the `applied` + files-gone family a fourth time. `_on_disk` was
written for the drain on 2026-09-11, and the cache bound was the one
consumer of the pair that still believed the ledger.

The fix: a landed shard counts against the cache only if its files are
on disk. A shard still `fetching` counts at the hint, because its download
is coming. The regression test goes from three shards final, deleted and
reopened to every vector imported. On the old code it fails with exactly
the live symptom, `{'fetched': [], 'reason': 'cache full'}`.

Once this is deployed, the fetcher re-fetches the shards, up to eight on
disk at a time, then applies, finalises and deletes each one. That is
about 105 GiB from NCBI: about 3–4 h at the 7.5–10.8 MB/s measured on
2026-09-11. The alternative was to let the local encoder embed the 38,324,
also about 3 h at pubmed's last measured 3.3–4 documents/s. It was not
taken, because the reopen design already chose the published vectors and
the fix completes it.

### `embed = "off"` is not "stuck", and the log now says which

Three "pubmed is stuck" reports in two days were the same line:

```
[pubmed info:worker] idle — pipeline is caught up
```

4,577,101 documents indexed and waiting for a vector, and the worker
called itself caught up — because a disabled stage and a finished one
look identical from the selection query. Both return no row. The
difference is the entire question the operator is asking.
`_waiting_because_off` now names the work and where the switch is. One
indexed count, and only on the five-minute idle line.

The genesis import is **not** gated on `embed_mode` and never was —
`_serve_one` drains the archive before it looks at embed at all, so
published vectors are imported however the encoder is configured. That
was worth confirming rather than assuming: it is the operator's own
principle and the code already held it.

### The Codex flag day, found by testing the deploy instead of trusting it

The `derivation` migration was on the "waiting" list as a known flag day.
It is worse than that list said: it is not a thing to schedule, it is a
thing that **breaks the node the moment any newer code runs**.

Tested 2026-09-13 with the staged image against the live config, before
promoting anything:

```
LOADED: []
UNREADABLE: gutenberg — CodexError: codex: missing field(s) derivation
            pubmed    — CodexError: codex: missing field(s) derivation
```

Zero collections, and `default_collection()` raises on top of it, which
takes the control manifest with it. `derivation` is a required field of
the version-2 Codex (§9.3) and both `drag/codex.json` files predate it,
so they have been non-compliant since the field was specified — the
running code simply does not know to ask.

**The loader is right and the files are wrong.** `_only` refuses a
missing field as firmly as an unknown one, because a peer that ignored
one would hash the same collection differently (§9.18). So the fix is
`hold collection migrate`, which adds the nullable §9.3 fields a file
predates and nothing else.

| | before | after |
|---|---|---|
| gutenberg `collection_id` | `sha256:e395a9cd…` | `sha256:4a825805…` |
| pubmed `collection_id` | `sha256:02da1b55…` | `sha256:c4e3bd74…` |
| gutenberg `space_id` | `sha256:a02a9052…` | **unchanged** |
| pubmed `space_id` | `sha256:2b9d9416…` | **unchanged** |

The identity moves because `collection_id` is the digest of the object
and the object gained a field. The space does not, because §2.4 excludes
`derivation` — so all 35,417,887 pubmed vectors stay admissible.

**What else names the old id.** Two things, found by reading the drag
rather than by reasoning about the Codex alone, and `migrate` now prints
both:

  - `snapshots/*.json` carry `collection_id`, and
    `snapshot.verify_chain` refuses a collection that changes mid-chain.
    Pubmed holds ONE snapshot, so nothing breaks today — the next one,
    written with the new id, is where it would surface.
  - `hold check` names its witnesses `"<collection_id>/<slice>/<n>"`
    (§9.8), so the eight signed attestations describe the old identity
    and a fresh run produces different witness names.

Neither blocks anything and neither loses anything: `2026-08` stays
valid as the old collection's snapshot, a chain that begins again is
what a new identity means, and the attestations are superseded rather
than wrong. Worth saying out loud because "collection_id moves" sounds
like one fact and is three.

**The second landmine, found by following the first.** `_held()` called
`cx.loads` on every `<name>/drag/codex.json` under the root and let the
exception out. The node holds FIVE Codexes — the two above plus the
retired `gutenberg.parser4-2026-09-12`, `gutenberg.retired-2026-09-12`
and `gutenberg.unchecked-figures-2026-09-12` — and none of the five
carries `derivation`. Migrating only the two the worker serves would have left
`hold collection list`, `show`, `resolve` and `create` all still raising
on a retired directory nothing reads and nobody would think to look at.

Fixed by the codebase's own doctrine (invariant 1, "the node keeps
running its others"): one unreadable Codex refuses itself, not every
command. What is skipped is not hidden — `unreadable()` returns it and
`list` prints it with the command to run. The ordering is pinned by a
test: `migrate` reads the file directly rather than through `_held`, so
the thing that repairs an unreadable Codex is not itself blocked by one.

**The measurement that nearly fooled me.** My first check compared
`from_dict(dict(raw, derivation=None)).space_id` with
`from_dict(migrated).space_id` — two names for the same object, so of
course they matched. The real check computes the old id with the OLD
code on the running container and the new one with the new code, and
only that is evidence. *A comparison whose two sides are constructed the
same way proves the construction, not the claim.*

**And the lesson about deploying.** I had written the runbook and was
one refused `mkdir` away from running it. What caught this was testing
the staged image against the real config rather than against the test
fixtures — the fixtures all carry `derivation`, because the code that
writes them is current. **A green suite says the code is consistent with
itself; only the live config says it is consistent with what is already
on disk.**

### Gutenberg's fetch "errors" are mirror resets, and they are retried

Measured 2026-09-13, mid-fetch: 9,480 of 91,863 `fetch_files` rows carry
an error, and the count climbs as the fetch advances. Every one of them
is the same shape —

```
TransportError: rsync aleph.gutenberg.org::gutenberg-epub/10/pg10-images.epub:
exit 12: rsync: [receiver] read error: Connection reset by peer (104)
```

— a mirror dropping the connection, not a missing file. Note the host:
the endpoint recorded is `ftp.ibiblio.org::gutenberg-epub` and the reset
comes from `aleph.gutenberg.org`, so the name rotates across mirrors
under us.

**No data is lost.** `_is_pending` gates on `fetch_done`, `remote_size`
and `remote_mtime` and does NOT look at `error` or `retries`, so an
errored file is simply picked up again on the next pass. The retry
histogram says the same thing: 9,228 at `retries=1`, 245 at 2, 7 at 3 —
the overwhelming majority have failed once and have not come round yet,
because the first pass is still running.

**What IS wrong is the recording, and it is fixed.** CLAUDE.md:
"Deferral is not failure: an unreachable endpoint ... never writes an
error." A connection reset is the definition of an unreachable endpoint,
and writing it to `error` means the operator's only summary of fetch
health counts transient mirror flakiness alongside real failures — which
is exactly how it read from outside ("errors=9469 and climbing") and why
it cost an investigation to dismiss.

`_upsert_failed` now classifies: a dropped transport (`transient()` —
connection reset, unexpectedly closed, socket IO, protocol stream,
timeout, refused, DNS, broken pipe) writes no `error` while it is only
the mirror. `exit 23`, a sidecar mismatch, a full disk and a permission
are untouched and still reported at once.

**What makes the quiet safe is that it is bounded.** `retries` still
counts every attempt, and after `DEFER_ATTEMPTS` (4) the deferral IS
reported — a file that cannot cross after four mirror drops is no longer
flakiness, it is something a human must see. A deferral that skipped
`retries` would have been an infinite silent retry, which is the bug
this fix could have introduced and the reason a test pins it.

**And why it was safe to make at all**, having first argued for deferring
it: retry behaviour is identical either way. `_is_pending` gates on
`fetch_done`/size/mtime and has never consulted `error` or `retries`, so
a deferred file and an errored one are selected the same. The livelock I
was worried about was already the status quo. A test asserts
`_is_pending` still reads neither column, so the day that changes, this
fix gets re-examined instead of silently becoming wrong.

### The node has been running three different code trees at once

Found 2026-09-13 while checking how far behind the node was. It is not
one tree behind; it is three trees, disagreeing.

```
louise-rag:local (tag)   sha256:fbfdaeb…   built 2026-09-12 21:29
web   container runs     sha256:fbfdaeb…   started 21:30
worker container runs    sha256:60705ee…   started 15:13  ← older image
src/app/plugins/pubmed/source.py           modified 22:26  ← after both
src/app/plugins/gutenberg/rdf.py           modified 23:38  ← after both
```

**web and worker run different code**, which DEPLOYMENT.md forbids in as
many words — "If any Python changed, recreate both, or web and worker
run different code against one manifest." Five files differ: `llm.py`,
`web.py`, `plugins/gutenberg/rdf.py`, `plugins/pubmed/source.py`,
`plugins/pubmed/pack.py`. And `src/` is ahead of both, so a third
version sits on disk that nothing runs.

What is actually live, of two days of parser work:

| | web | worker |
|---|---|---|
| gutenberg credits by role (`_ROLE_LABEL`) | ✗ | ✗ |
| gutenberg MARC title strip (`clean_title`) | ✓ | **✗** |
| gutenberg `subjects_by_scheme` | ✗ | ✗ |
| pubmed `authors()` / parser 5, 6 | ✗ | ✗ |
| translation fix (`flatten_whitespace`) | ✓ | n/a |

**The worker is the one that parses.** So every book adopted since is
rdf'd by the old gutenberg parser. The predicted effect is real but
small: 8 gutenberg titles now contain a `$`, and most are money (`The
$30,000 Bequest`, `$1,000 a Plate`) which `clean_title` correctly leaves
alone — only `Dress design $b: an account of costume…` is a genuine
uncleaned subfield. Single digits, not the thousands the tool cleaned.

**Why this matters more than the eight titles.** It explains the shape
of the last two days: fixes verified in the manifest by a backfill tool
(4,653 titles, 39.2M authors) while the PARSER that keeps them right was
never deployed — so the data was corrected and the thing that produced
it was not. A fix reported as shipped, whose code no process is running,
is indistinguishable from a regression from the outside, and it was
reported as one.

The deploy recreates both containers from one image, which is the fix.
**Check `docker inspect <container> --format "{{.Image}}"` on both after
every deploy** — the assertion that they match is cheap and nobody was
making it.

### `louise health` — the check the "stuck" reports needed

Operator, 2026-09-13: "I TOLD YOU TO ADD THIS CHECK TO YOUR FUCKING TEST
POOL". A unit test cannot see the live node; what was missing is a
command that answers "is it stuck" in one line and exits non-zero when
something is blocked, so it works in a cron or a monitor.

The distinction it exists to draw, and the one the worker never drew: a
stage with no work is IDLE and fine *even when switched off* — "off and
finished" is not a problem, and reporting it would make the check cry
wolf on every idle collection, which is exactly how "idle — pipeline is
caught up" came to be ignored. Only work that CANNOT START is blocked.

It carries an `archive` row because that is the one that hid twice:
those documents are waiting for embed, so they look like ordinary
encoder backlog from the stage counts.

**Corrected 2026-09-13 by an independent re-check**, because the wording
here and in `health.py` both said their vectors "already exist and are
free", and NEITHER half of that was measured. A shard finalises when
nothing in its range is still UPSTREAM of fts — not when everything in
its range received a vector — so range membership does not prove the
archive holds one; and finalising DELETES the shard files, so reopening
costs a re-fetch rather than nothing.

What the numbers support: the archive served 35,417,887 of 35,456,211
in-range documents (99.89%), and the residue is timing-shaped rather
than a coverage hole — 35,858 of the 38,324 fall in shards 27-36, the
ones fts was crossing as the archive drained, against 2,466 across
shards 0-26. A genuine hole would spread evenly. So: likely, and worth
the re-fetch to find out. *An inference with good evidence is still an
inference, and writing it as a measurement is how a report stops being
trustworthy.*

**Timing, on the live pubmed manifest: 95.66 s → 6.75 s**, same output.
Three measurements, and one of them refuted a prediction I had already
written into a comment:

| | |
|---|---|
| stage counts with `AND error IS NULL` | 54.09 s |
| ...as a covering-index prefix, same answers | **0.48 s** |
| error count (`error IS NOT NULL`, to return 0) | 43.56 s → opt-in |
| archive gap over the 37M-id range | 40.60 s |
| ...with the 37 shard ranges merged into one | 41.23 s — **no gain** |
| ...walking the 4.58M embed-waiting set instead | **5.47 s** |
| ...existence probe first, LIMIT 1 | 0.00 s |

**The merge bought nothing, and the reason is worth keeping.** I assumed
per-query overhead dominated and wrote "41 s against 1" into the code
before measuring. The cost is per ROW: one 37M-id range costs exactly
what 37 one-million ranges cost. The win came from asking the question
from the other end — the embed-waiting set is 4.58M rows against the
archive's 37M id span, so walking the SMALLER SIDE is 7.4x less work for
the same answer. *When a count is slow, the question is not how many
queries it takes but which of the two sets it walks.*

`error IS NULL` was the exact false positive CLAUDE.md documents: the
plan reads `SEARCH documents USING INDEX idx_documents_scope
(in_scope=?)` while SQLite does a table lookup per counted row, 4.58M of
them. Leaving the clause out counts errored documents as waiting — a
handful against millions, reported beside the count, and feeding no
scheduling decision, unlike the genesis backlog query where the same
trade would be wrong.

Its own tests found two defects in it: `db.get_scope` sat outside the
guard, so a manifest it could not read crashed the check that exists to
report unreadable manifests; and a stage whose count RAISED was silently
dropped from the report, which would have read as an all-clear — the
"caught up" failure again, in a new place.

### What else on disk the new code reads — checked, and it is fine

Having been bitten twice by "new code, old artefact", the rest of
pubmed's drag was checked rather than assumed. All of it reads:

| member | result |
|---|---|
| `codex.json` | **REFUSED** — missing `derivation`, fixed by `hold collection migrate` |
| `validation.json` | parses |
| `snapshots/2026-08.json` | parses; the chain restarts after migration (above) |
| `records/**/*.jsonl.zst` | 30,000 records, 30 frames, every key parses |

**The one that looked like a third landmine and is not.** The stored
record lines carry 15 keys — `chunks` and `layer` among them — and the
current `RECORD_KEYS` is 13, because those two were dropped to match
§9.5 exactly. A stored line is 725 bytes where a re-canonicalisation is
705, so the bytes genuinely differ.

Nothing compares them. `records` recomputes `content_hash` over title
and sections alone; `roots` hashes the shard FILE, which nobody
rewrites; and `records-vs-origin` puts BOTH sides through
`canonical_line`, so the two dropped keys cancel. That last one is not
luck — it is the fix for the `figures` shape mismatch, which failed 6 of
6 records on a plates trial while the re-parse was byte-identical all
along, and the comment explaining it is still there.

*A key-set change is only a break where something compares the SHAPE
rather than the CONTENT. Finding out which is a five-minute check and
worth it every time.*

### Seed-only kept downloading for ten hours — found and fixed 2026-09-13

Operator: "Is Louise fetching even if the status is seeding only?" It
was, the whole time.

```
seed-only window      2026-09-12 22:45:40  ->  2026-09-13 08:50:42   (10.1 h)
gutenberg fetched     204,486 files, 160.3 GB, INSIDE that window
per hour              7617 7965 13819 81899 54706 7385 7804 7361 7736 7520 674
```

Every hour of the window, not a burst at an edge.

**The contract is not in doubt.** docs/07 defines `node pause` as "no
work"; the worker's own comment defines seed-only as "downloads and
processing stand down exactly like pause". The main loop honoured both —
it sets `_paused` for `paused` and `seed-only`, and the pipeline threads
idle on it. **The fetch never looked.** Its stop was
`_AnyStop(_stop, _gone_event(name))`, SIGTERM or collection-removed, so a
fetch already in flight ran to completion; a gutenberg fetch is one rsync
over the whole mirror, hours long. The genesis archive fetcher had the
same hole. So `paused` could not stop an in-flight download either — by
construction, not observed live.

**The fix took three adversarial reviews, and each found my corrections
wrong.** That is the useful part of this entry.

*Review 1*, of "put `_paused` in the stop":

1. **Blocking, and mine: nothing restarted a fetch the pause stopped.**
   The post-fetch rescan is queued `acquire=False`, `_request_discovery`
   is first-reason-wins so no later `acquire=True` could replace it, and
   the scan stamps the discovery clock — so the fetch did not return for
   `DISCOVER_INTERVAL_MIN`, 360 minutes, after every resume. A new gap:
   before, a pause never stopped a fetch. My test for it called
   `_acquire` by hand and passed while the real wiring never made that
   call.
2. The bandwidth bucket's unlimited branch returned before checking the
   stop, so HTTPS, range and archive downloads ignored a pause — and
   SIGTERM — whenever no `down_max` was set. Pre-existing.
3. A torrent left its file at priority 4, so libtorrent kept pulling it
   after the abort, outside any lease (invariant 5).
4. A pause reached `_paused` once per main-loop round, which can run
   minutes (a 120 s embed stream, an unbounded bundle-inbox import).

*Review 2*, of those corrections:

5. **Blocking — a race reopened (1).** The abort was marked in the try
   body while the thread still had a closing log write to make
   (busy_timeout 60 s). A resume inside that window consumed the re-arm,
   `_acquire` saw the old thread alive and returned silently, and the
   `finally` then reported done with nothing pending. Reproduced
   deterministically by the reviewer.
6. **Blocking — the stats beat read the node state from `load_all()[0]`,
   the main loop from its filtered `all_collections[0]`.** A collection
   with no `worker_state` reads 'paused', so when the two differed — add
   one whose name sorts first — the beat set `_paused` every 10 s and the
   main loop re-armed every round: a fetch stopped and restarted per
   beat, the loop `acquire=False` exists to prevent. The same divergence
   failed five pipeline tests in the full suite: `test_stats.py` runs a
   real publisher with no main loop against a fresh config, whose state
   defaults to 'paused'.
7. With no pipeline threads and `DISCOVER_INTERVAL_MIN=0`, the re-arm
   cleared a clock nobody read, and the fetch was lost until restart.

*Review 3*, of THOSE corrections:

8. The race fix holds. A minor split remained: `_finish` and `_rearm`
   read the mark at different moments of a round, so a fetch whose
   `finally` landed between them was re-armed AND treated as a normal
   completion — a redundant scan, never a lost fetch.
9. **A regression of mine:** without pipeline threads the re-arm cleared
   every pending name, then ran `_discover` for each unguarded — so one
   collection's scan raising lost every restart after it.
10. The control split below — pre-existing, and not mine to decide.

The corrections from review 3 were verified by tests that reproduce the
reviewers' own scenarios and by the full suite — not by a fourth review.

**What ships:**

- `_paused` in the corpus and archive stops. `_AnyStop.fired` records
  WHICH event stopped the work, read after the fact.
- The pause-abort is marked in the thread's `finally`, in the same locked
  section that reports it done. `_rearm_paused_aborts` runs every
  non-paused round, re-arms with `acquire=True`, waits for the old thread
  to be gone, forgets "done" for what it re-arms, isolates each name so
  one failure keeps only itself for the next round, and returns before
  walking any list when nothing is pending.
- A start refused by the pause is remembered.
- The bucket checks the stop before its unlimited branch.
- A torrent stop zeroes the file's priority and credits the bytes once.
- The stats beat sets `_paused` within 10 s, reading the MAIN LOOP's
  control collection, and does nothing without a main loop.
- Without pipeline threads, the re-arm runs `_discover` itself.

**Two known gaps, recorded rather than claimed — both closed the same
day** (next entry): a pause arriving during a *listing* (`rsync
--list-only`, 12 minutes on gutenberg; mirror benchmarks; torrent
metadata) was not seen until the transfer pool started; and the *control
split* — `/api/control` wrote the pause to the unfiltered `load_all()[0]`
while the main loop read its filtered `all_collections[0]`, so with the
first-sorting collection refused at boot the UI said seed-only while the
node downloaded. The operator chose one resolution function for web and
worker; it became `state/node.sqlite`.

**The shape worth keeping.** Every defect here was the same one wearing
different clothes: a decision taken from one source while the thing it
governs reads another. The fetch's stop vs the state that should stop it;
"marked" vs "done" published at different moments; the beat's control
collection vs the main loop's; the API's vs the main loop's. And the test
that should have caught the first was itself built the same way — calling
the function by hand instead of driving the path the worker runs.

**A spec gap found on the way, not a further bug.** docs/07 specifies a
whole `louise node` surface — `start [--acquire-only]`, `stop`, `show`,
`pause [<collection>] [--acquire-only]`, `resume [<collection>]` — and
nothing in `app/` implements any of it, and `migration-plan.md` does not
schedule it. Pause today is node-wide only, through the Library's mode
buttons writing `worker_state`. So "pause one collection" is unbuilt
rather than broken; recorded here because the fix above cites docs/07's
definition of pause, and the next reader deserves to know the command
that definition belongs to does not exist yet.

### The control split and the listing gap — closed 2026-09-13

The two gaps the entry above recorded "rather than claimed". The operator
chose "one function for web and workers" for the first and asked how to
close the second.

**The control split: `state/node.sqlite`.** A read-only map of every site
that decides where node control lives found not two resolutions but FOUR:

| who | read from |
|---|---|
| `/api/control`, Library, System, bandwidth POST | `load_all()[0]`, unfiltered |
| main loop — the state gate, the bandwidth apply | its filtered `all_collections[0]`, boot refusals removed |
| stats beat — seeding gate, seeding table, telemetry | `load_all()[0]` (its pause detection followed the main loop since review 2) |
| index maintainer | `load_all()[0]` |

With the first-sorting collection refused at boot, the index maintainer
and the seeder obeyed the Library while the main loop did not, and the
bandwidth boost never reached the worker at all. Adoption ran only in the
worker, only on an ownership change, check-then-write with no
`BEGIN IMMEDIATE` while the web could write concurrently.

The one function turned out not to need a collection. docs/07 §7.8
already puts runtime overrides in `<LOUISE_ROOT>/state/node.sqlite` ("the
catalogue, coverage maps, peers seen, runtime overrides"), and the file
already existed — hold's admission ledger opens it. `app/node_state.py`
is now the one store for `db.NODE_CONTROL_KEYS`; the API, the Library and
System pages, the bandwidth POST, the main loop, the stats beat (pause
detection AND seeding gate), and the index maintainer all read it. Gone:
`adopt_node_control`, `_orphaned_node_control` and the removal-time
rescue, `_control_name`. The beat acts only while `_main_loop_live` is
set — the reason it needed a main loop behind it is unchanged.

*Migration.* At boot the worker copies each key the node file lacks from
the manifests, in the order the web wrote to (unfiltered first collection
first, so it is the operator's last click that moves), under `BEGIN
IMMEDIATE`, then deletes the keys from every manifest: one home. It fails
CLOSED: a manifest ahead of every copy that cannot be opened or read
blocks that key, nothing is deleted anywhere, and the node reads 'paused'
until a boot sees every manifest or the operator clicks a mode, which
writes the node file and wins. Rollback to an older image reads no mode
and starts paused — the safe direction, and the runbook says so.

*Kept where it was, on purpose:* telemetry (worker rates and stats, the
seeding table) stays on the first collection's manifest. It is not an
override; the web reads the beat's copy, which already follows the web's
own resolution.

*Decided by the operator the same evening: the mode survives a restart.*
§7.8 said runtime overrides "expire at restart" and invariant 10 that
they do not survive one; `worker_state` never did expire. Making it expire
would turn `acquire-only` into `running` on every deploy and start the
encoder. The operator: "don't reset node mode at restart". docs/07 and
CLAUDE.md invariant 10 now name it as the one exception (`c26af9a`), and
a test pins it (`test_the_mode_survives_a_restart`).

**The listing gap.** Every phase before the transfer pool ran with no
stop. Now:

| phase | worst case before | now |
|---|---|---|
| rsync `--list-only` | ~12 min measured on gutenberg; 30 min cap per mirror, then the next mirror | a Popen poll loop: terminated within `_POLL_SECONDS` of the stop; a partial listing is never returned |
| torrent metadata | 120 s per magnet | the bound stop reaches `_await_metadata` |
| torrent-collection index resolve | 120 s + an unbounded download | bound, and a 300 s deadline; `_index_for` re-raises the stop instead of calling it "plain torrent for now" |
| mirror benchmark | 20 s per mirror; a stopped probe recorded as FAILED for 6 h | checked per mirror; the probe thread gets the context; a stopped benchmark records nothing |
| ftp / hf listings | unbounded by pages | checked per entry / per page; ftp `close()`s, never `quit()`s, on an abort |
| planning passes | unmeasured, scale with the listing | checked after listing and after planning, before anything is recorded |

How an abort leaves matters as much as that it leaves. `TransferAborted`
IS a `TransportError`, so the mirrored listing used to mark the mirror
down for 15 minutes and list the next one in full; it now re-raises.
`run_acquisition` turns a pre-pool abort into a result with `"stopped"`
and every counter at zero — what a stop inside the pool already returned
— so the worker logs the pass it was, never an ERROR, the pause re-arm
(`stop.fired`) works unchanged, and `corpus add` says it was interrupted
and exits 130.

*SIGTERM.* `_join_pipeline_threads` never joined the acquire or archive
threads: a shutdown abandoned them as daemons, mid-listing, with the claim
row left for the next boot to wait out. They are joined now, inside the
same 90 s deadline — possible only because every phase honours the stop.

*Found on the way, and fixed — each pre-existing, each with a test:*

- **A collection refused at boot crashed the worker.** `run_forever`'s
  refusal branch prints to `sys.stderr`, and `app/worker.py` never
  imported `sys`: the first space refusal raised NameError at boot and
  crash-looped the worker under `restart: unless-stopped` — the exact
  opposite of invariant 1's "refuse THAT collection, not the node". Latent
  on the node only because both collections load today. The test boots the
  real loop with the first collection refused, and also proves the state
  the web wrote into that refused manifest still moves — the control
  split's own scenario. A mutation run with `sys` removed makes it fail.
- **A signal exited 0**, from the worker and from `corpus add`. docs/07
  §7.7: "exits 130 on a signal"; invariant 2 says the same. Both return
  130 now.
- **The torrent-collection index download had no deadline.** fetch_one's
  loop has none of its own, and the Library's grow plan resolves the index
  inside a WEB REQUEST with no stop, so a swarm announcing the index but
  serving none of it held the request forever. Now `INDEX_FETCH_TIMEOUT_S`
  (300 s): out of time is "unreachable", a TransportError; the caller's
  stop stays a stop.
- `_stats_publisher` passed `all_collections`, a name not bound there, to
  `_log_shares` — a NameError every beat, swallowed, so the fair-share
  lines never reached the log.

**The review** — four lenses, every finding put to a skeptic; eight
confirmed, two refuted — found my work wrong again, in smaller ways:

- *The migration could hand the move to a stale sibling.* A manifest
  that failed to open once at boot was skipped and the next sibling's old
  copy moved instead — the old adoption copied values into every new
  owner and never deleted them — and on the next boot the operator's value
  was deleted. Reproduced. Hence failing closed, above.
- *A stopped multi-torrent pass returned no counters*, and `corpus add`
  died on `result['fetched']`: a regression the stop-aware metadata wait
  opened. Every result now has one shape (`_no_counts`).
- *The worker threads' reads were untested.* Reverting the seeding gate,
  the beat's pause read, the `_log_shares` fix and the index maintainer's
  read — all four — left the suite green, because every test called the
  helper by hand. The entry above records exactly that lesson, and I
  repeated it. The real threads are driven now
  (tests/test_worker_threads_node_state.py).
- *Two tests wrote `worker_state=running` into the real
  `/data/state/node.sqlite`* — on a node's volume, the live mode. They are
  isolated, and a conftest guard fails any test that touches the real
  store.
- *`corpus add` said nothing when stopped in the listing* — `total=0`
  read as an empty origin.
- *The listing tests fired their stop on a 0.1 s timer*, so on a loaded
  host the stop could land before the listing and the test pass without
  exercising the bind. The fakes set the stop themselves now, from inside.
- *This entry did not exist* while the code and codex-only-node.md
  pointed at it.

Refuted, and worth keeping: that copy-if-absent is backwards after the
move. A manifest found at boot is not always newer than the node file — a
collection retired before the move and restored after carries an OLDER
value — so the node file winning is right.

Full suite: 2,433 passed.

### Deployed — 2026-09-13, evening

The operator said "deploy". The runbook (deploy/reference/DEPLOY-NOW.md)
ran as written, with one change: its promote step ended in `rmdir
src.new`, which the leftover top-level files would have failed, so the
listed directories were moved and the rest left behind. Every step was
checked before the next:

| step | result |
|---|---|
| promote + build | `src/` is `d5e6b51`; `louise-rag:local` = `d70e28d` |
| Codex migration + TOMLs | gutenberg `sha256:4a825805…`, pubmed `sha256:c4e3bd74…` — the runbook's predicted ids; TOMLs and codex.json backed up first (codex backups in `state/`, never `drag/`, which a stray file would make share refuse) |
| prove it loads | `LOADED: ['gutenberg', 'pubmed']`, `UNREADABLE: []` |
| recreate both | web and worker on the SAME image for the first time in two days |
| the mode moved | `node control moved to state/node.sqlite (07 §7.8): worker_state='acquire-only'`; node.sqlite holds it; the gutenberg and pubmed copies are gone |
| seed-only, live | rsync 2 -> 0 in **5.0 s** (gutenberg was mid-pass), still 0 five seconds on; mode restored; rsync back in 3.0 s |
| genesis recheck | 30 shards reopened, 38,324 documents |

Held back on purpose: the rdf re-derive (runbook step 7). The node is
acquire-only, where rdf stands down, so queueing it would have shown
gutenberg as "parsed 0" until the mode changes. And the 38,324 documents'
vectors are re-fetched in acquire-only but IMPORTED only when the node
runs — no encoder needed, embed stays off.

**`louise health` then told the truth wrong, twice.** `rdf 24,012
running` on a node where rdf stands down — it never read the node's mode
— and "run `louise genesis recheck pubmed`" for 38,324 documents whose
shards that command had reopened a minute earlier: it counted every
shard's range and blamed them all on FINAL. Both are the "idle — caught
up" failure's family: a report that reads as an instruction and is
wrong. Fixed:

- HELD, a fourth state: work the node's mode holds, named with the mode,
  never exit 1 — resuming is the answer, nothing is wrong. A block that
  resuming would not clear (embed off in the TOML) stays BLOCKED.
- The archive line splits by shard state: documents under FINAL shards
  are blocked and told to recheck; under REJECTED shards, blocked and
  pointed at `genesis status`; under open shards, being served — or held.
- The mode is read with `node_state.peek_worker_state()`, read-only: a
  check creates nothing.

**Its review** (two lenses, every finding put to a skeptic; six
confirmed, five distinct, all fixed) found the rule this module had to
learn: every verdict must be the WORKER's own, or it is a second answer
to the same question, and four times the second answer was "running"
where the worker's was "nothing runs":

- a STOPPED archive — a shard rejected before any shard proved it — was
  "being served"; the worker fetches and imports nothing for it. Now
  decided by `genesis.archive_stopped`, pointing at `genesis retry`;
- a collection without its license acknowledgement, and one refused at
  boot, read as running. Now `licensing.state_for` and
  `spaces.report_boot_status` (read-only) decide;
- the read-only peek made `-wal`/`-shm` beside a closed store (`mode=ro`
  alone does that), cut its URI at `#` or `?` and created a stray
  database, and read an admission-only `node.sqlite` as 'unknown'.
  `db.read_only_connect` had the same URI fault.

**And an invariant-9 hole in the worker itself, found on the way.**
`_next_book` gates its round on the license acknowledgement, but the main
loop embeds through `_next_embed` and `_embed_candidates` whenever
pipeline threads own rdf/fts — the deployed configuration — and neither
asked. A yellow or red collection without an acknowledgement still had
its documents embedded once past fts: use of the corpus, which invariant
9 forbids however the data arrived. Not reachable on this node (gutenberg
is green, pubmed acknowledged — checked live). Gated, with a test whose
control proves the row is otherwise selected
(tests/test_license_gate_embed.py).

**The verification pass of those fixes found the next layer** — and one
defect I had introduced:

- *`louise health` wrote to the node.* It borrowed the System page's boot
  report, which promised READ-ONLY and still filled in a NULL pinned
  width and logged an event for an unconfirmable hash — every System page
  view, and now every check. Both writes are boot-only now
  (`pin_if_missing`), and one manifest that cannot be opened no longer
  makes the report raise and hide a real refusal beside it.
- *Three more invariant-9 paths never asked the gate*: the worker's
  bundle-inbox drain (a bundle landed in an unacknowledged collection,
  reproduced), `louise contribute` (embeds the corpus and uploads the
  vectors) and `bundle resweep` (re-embeds its text). All gated, each test
  with a control proving the call is otherwise made
  (tests/test_license_gate_exchange.py).
- The health report: a declared shard with no ledger row yet is the
  archive's debt, not the encoder's; open shards beside a rejected one
  follow the node's mode; the stopped-archive test now uses the ledger's
  real proof columns (it passed with the old `SELECT` too); a symlinked
  store's sidecars are looked for beside its target.

Known and accepted: for a LEGACY (non-Codex) collection the boot check
probes the encoder, so `louise health` takes up to 10 s per such
collection when the encoder is unreachable. This node's collections are
both Codex collections, which never probe.

### Invariant 9 at every door — the audit, 2026-09-13

The verification pass kept finding one more door that never asked the
license gate, so the question was put whole: an audit mapped every path
that uses, imports or exchanges a collection's data — 129 of them, from
three independent angles (entry points, the library functions that move
data, egress), deduplicated and checked by a critic. Every one found was
pre-existing, and none was reachable on this node (gutenberg green,
pubmed acknowledged; checked live). Closed:

| kind | doors |
|---|---|
| serve | `/read/{collection}/{id}` — the whole text, reachable from Ask with no admin token; `GET /api/books`, `/api/books/{id}`, `/api/books/{id}/summary` |
| import | CLI `bundle import`; the worker's bundle inbox; `importer.import_bundle` as the backstop |
| export | CLI `bundle export`, `snapshot vectors`, `snapshot delta`, the Library's export button, `hold vectors export`, `tools/build_space_artifact`; `export.export_bundle` as the backstop |
| discover | the Library's Rescan, `POST /api/discover`, the worker's inline rescan after a fetch, `hold records build`'s pre-scan, `tools/pack_convert`; a dry-run `plan_acquisition` LISTED the origin before any check and now lists nothing |
| embed / llm | the worker's embed selectors, `hold vectors check`, `hold check`, `bundle resweep`, `louise contribute`, the summary endpoint, the calibration and sweep tools |
| copy | `pack adopt`, both sides |

Three structural fixes rather than doors:

- **`hold records.local_collection` is the one gate for `hold`.** Every
  stage that uses a Codex collection's data builds its local side there,
  and the only check it had asked whether an acknowledgement FILE
  existed — a file whose hash no longer matched the terms passed.
- **THE resolution reads the Codex.** `licensing` read only the plugin's
  registry class, so a Codex that tightens green gutenberg to yellow
  (04 §4.4 allows it) was judged green by every gate but that presence
  check. The Codex's class decides now — 02 §2.1 makes it authoritative —
  against the terms the Codex pins. ("The stricter wins", tried first,
  left a green Codex over a yellow plugin withheld for good, since
  nothing records an ack for a green Codex; the verification pass caught
  it.) A Codex collection's refusal names `louise hold collection ack`,
  the one command that records its acknowledgement, and the Library's
  button no longer answers "ok" for one while writing a TOML line its
  loader never reads. Checked live before shipping: both collections
  resolve exactly as before.
- **One answer to one fault.** When licensing itself fails, search,
  health and the worker all withhold — search failed closed, health
  said fine, and the worker raised into its round.
- **One check, not two.** The acquisition engine kept a private copy of
  the license check, which invariant 9 forbids; it delegates now. And
  search's gate failed OPEN when licensing itself raised — every search,
  chat and verify — and fails closed.

Every gate has a test whose control proves the same call is otherwise
made, so a refusal is the gate's and not an ineligible input.

**Decided by the operator, 2026-09-13: reading a pack is not a "use".**
`pack verify`, `pack cat` and `pack dump` take a pack PATH, not a
collection, and print its records; they stay ungated. They encode
nothing and write nothing to the node, the data enters it only through
discovery, import or adopt (all gated), and `verify` is the check a pack
gets before anyone acknowledges it. The comment above `cmd_pack_verify`
says so, so a later audit does not gate them.

The test that pins it (tests/pack/test_pack_read_ungated.py) was the
first ever to run `pack cat`, which crashed: cli.py used `json` without
importing it. An undefined-name sweep then found `index vector status`
raising NameError on `config` for every collection with no vector index
yet. Both fixed and tested; the sweep of app/ and tools/ is clean.

### Zombie rsync under a Python PID 1 — found 2026-09-13, night

The monitor's "STALLED" on gutenberg was the fetch's tail, not a stall:
10 files never tried, 14,274 that failed before (12,607 once, almost all
ibiblio rsync exit 10) returning at their backoff's pace. Looking for
it, the host showed 25 `[rsync] <defunct>`, all children of the worker,
one about every eight minutes since the deploy.

rsync forks a receiver; when rsync exits first (our SIGKILL at a
deadline, and error exits whose cleanup waits with WNOHANG) the receiver
is re-parented to PID 1 — here `python -m app.worker`, no init — and
never reaped. No pids limit is set, so the cost is a table slot each.
Fix: `init: true` on both services (deploy/truenas-app.yaml); the worker
warns at start when it is PID 1. Reaping in the worker was rejected: it
races subprocess for its own children's status, and a stolen status is
exit 0 — a failed rsync's staging file moved into place as complete.
The node's app config gets `init` through `app.update` (operator's OK).

**Deployed 2026-09-14, 07:04 CEST** (05:04 UTC — the container logs are
UTC, the node's shell is CEST) — `149e40e` (this, the invariant-9
audit, the health fixes, the two CLI NameErrors), by the operator
running deploy-louise.sh: promote, build, prove the image is the new
code and loads both collections, then `app.update` adding `init: true`,
which is the restart. Checked live: web and worker on one image, PID 1
`/sbin/docker-init` in both, mode still acquire-only, licensing
unchanged (gutenberg green, pubmed acknowledged), `louise health` as
staged, no zombie rsync. gutenberg's rdf backlog rose to 29,255 as the
fetch replaced corpus files that had been parsed — changed files are
re-derived, which is what they should be.

### rsync's forked receiver, and pubmed's card — 2026-09-14

**The zombies init did not reap.** `init: true` reaped rsync's orphans,
but two worker children stayed `Z` for 700+ s, each with a live orphaned
receiver under docker-init holding its stdout/stderr pipes. `communicate()`
waits for EOF before it reaps, so the leader stayed unreaped; the deadline
path's `kill(); communicate()` had no timeout at all (a stop could hang a
fetch thread for as long as the orphan lived). Fixed in
`transport_rsync._supervise`: rsync leads its own process group, the
leader is reaped by `poll()` the moment it exits, the pipes get a bounded
grace, then the group is signalled and the pipes abandoned. A stop still
SIGTERMs the LEADER first — sent to the group, real rsync's receiver
ignores it and every stop took 10.5 s instead of 0.5 s (review, measured
against a real `rsync --daemon`; `tests/acquire/test_rsync_real_daemon.py`).
SIGHUP now stops the CLI and the worker like SIGTERM, since rsync no longer
shares the terminal's session. An exit 0 whose fork still held the pipes
is not a success.

**Mirror failures were file errors.** `engine._TRANSIENT` lacked exactly
the shapes measured that morning — "No route to host", exit 35 "timeout
waiting for daemon connection", exit 30, the transport's own "timed out
after", "max connections" — so each was written as a file error on the
first attempt. Reporting only: selection never read either column.

**Pubmed's card was wrong twice, and the ledger once.** The row read
`SUM(genesis_imports.docs)` = 35,337,233 over `in_scope` = 39,994,988. It
now reads vectors HELD inside the archive's declared range — 35,417,887 of
35,456,211 — names the 38,324 in range without a vector (held by
acquire-only) and says the 4,538,777 past PMID 36,999,999 are the local
encoder's. The ledger was 80,654 short in shards 8 (+26,154), 9 (+1,500)
and 13 (+53,000): it counted a pass's documents once, at the pass's end,
outside the flush transaction that sets `embed_done` — any pass that ended
another way lost its flushes from the count (53,000 = 106 flushes of 500).
Counts now land in the flush's own transaction. `louise genesis recount`
recomputes a ledger from chunk provenance; a dry run by default, it never
lowers a row that proves the archive without persisting `proven_at` first.
Repairing the node's three rows is the operator's call: dry run, then
`--yes` with the worker stopped.

**Deployed 2026-09-14, ~13:05 CEST — `59c400b`**, by `deploy-louise.sh`
(the promote was not refused this time). Checked live: one image for
web and worker under docker-init, the worker log clean, `louise health`
unchanged. The card's scan on the real manifest (64 s) returns exactly
the predicted 35,417,887 / 35,456,211, 38,324 waiting, 4,538,777 past
the range. `genesis recount pubmed` dry run (36.7 s): shards 13, 8 and 9
differ by +53,000, +26,154 and +1,500 — present in chunk provenance, so
they are archive imports the ledger lost, not local encodes. Not written.
23 minutes after the restart: 0 zombie rsync, 0 orphans under
docker-init, 14 live transfers — the leak is gone.

### Fidelity audits: gutenberg parser 7, pubmed parser 8 — 2026-09-14

The operator asked before switching embedding on whether gutenberg keeps
everything PG says about a book, and then for the same check on pubmed
and wikipedia (samples). The answer was no for both built plugins.

**Gutenberg — the text that becomes vectors was losing structure.** The
chapter regex read verse and body paragraphs as titles (8 of 32 books);
line breaks, exponents, scene breaks, table cell boundaries, illustrated
initials and non-UTF-8 members were lost or altered; a START marker
missing its `***` could cut a book. Parser 7 cuts an epub at its own
headings and keeps all of these (commit message has the numbers). The
record also gains the epub's header fields, `extra.form`, roles given
by reference, every title and the title's own form.

**THE LIVE COLLECTION CANNOT ADOPT PARSER 7 IN PLACE.** Its Codex pins
parser 5 (the code was already at 6), and the epub cleaning moved, so a
parser-7 gutenberg is a new collection: `cleaning_version = 3` in its
recipe, created by the operator. Nothing is lost by it — gutenberg holds
no vectors (embed off), and its 50,167 indexed documents were cut by
older parsers with no per-document record of which, so they would have
to be re-cut anyway. **Do not switch gutenberg's embedding on before the
replacement exists.**

**Pubmed — records only.** Parser 8 keeps ~20 fields parser 7 dropped
(OtherAbstract was read under the wrong parent and never written), all in
`extra`; `sections` is untouched so the archive's vectors stay bound. Its
Codex pins parser 4, so the same holds: the v1.0 pubmed records are
built with the current parser in a new collection.

Still open from the gutenberg audit, recorded here so it is not lost:
print page numbers and transcriber's original readings (`title=`
attributes, 11/32 and several books), full-size plates reached only
through `linear="no"` pages, a spine member missing from the zip, and an
epub/txt book with no RDF (its OPF metadata unused). Decisions with the
operator: `date` from marc260 $c, and emphasis (italics).

**Wikipedia parser 3** (no collection exists, so nothing forks). The
operator's decisions: `date` is the revision's date and `extra.revised`
its full instant; the last editor is kept when disclosed (`{username,
id}`) and is "not available" when anonymous or suppressed, and an IP is
discarded wherever the revision writes it — a revert's edit summary names
the IP it reverted to, so summaries are redacted too. Everything else the
revision carries is kept, and `extra.wikitext` holds the page as written:
that covers what no structured field names (tables, sfn/efn, maintenance
tags, HTML comments, `{{Use dmy dates}}`, ref names). Three defects fixed:
`lang` now comes from the dump's name (`canonical_doc` drops `ref.extra`,
so an itwiki page read "en"); the dump's last stream parses despite the
footer stream; and `origin` is `<dump>#<start>::<end>`, so
records-vs-origin can reparse a wikipedia record. The prose loses image
options, category and interlanguage links, magic words and apparatus
subsections, and gains captions, quotations, math, `<pre>`, the values of
inline templates, and the cells of tables mwparserfromhell could not read.

Sample: 2,914 real pages (1,549 enwiki, 1,365 simplewiki) through
discover → fetch → `canonical_doc`, in a dump with header and footer
streams. 0 errors, every page dated, 0 IPs outside the stored wikitext,
sort keys on 533/540 pages (the audit counted 536/541). Still in the
prose: TeX inside math (correct), an unclosed `<ref>` on 4 simplewiki
pages, table markup on 11 pages, one mis-nested image link. About 0.2 s
per enwiki page, single-threaded. Open for the operator: `keys` of the form
`wikipedia:<lang>:<title>` (06 §6.5: no reference source registers that
namespace), and `_SKIP_TITLE` still lets `MOS:`, `Event:` and some talk
namespaces through (left alone as asked).

**Deployed 2026-09-14 12:17 UTC** (1cf8b29, image `9b79dc5…`): web and
worker on the same image, `init` true on both, gutenberg/pubmed/wikipedia
parsers 7/8/3 in the running worker, no error in its first minutes. The
node suite against the staged tree: 2,598 passed. The live gutenberg and
pubmed Codices still pin parsers 5 and 4 — as above, the new parsers reach
records through new collections only.

### Normattiva on a generic ELI layer — 2026-09-14

Built on the operator's instruction ("implement also eli plugin") within
the 2026-08-26 scope (Normattiva only; the certified Gazzetta is not ours,
but must stay possible). `app/plugins/eli.py` holds the rules of
[`eli-legal-plugins.md`](eli-legal-plugins.md) §6 that any member state's
plugin reuses — native id from the unique work key, collision guard, the
Expression read from what the origin returned, the fixed `eli_*` extra
keys. `app/plugins/normattiva/` is the Italian plugin (akn.py, rendition.py,
source.py, TERMS.txt), `akn_article` v1 its chunker, `NORMATTIVA_SPEC` its
acquisition: 23 precompiled AKN collections, vigente where offered and
originale for the four published in no other form (the 124,043 repealed
acts among them), ~1.35 GB in all; `demo` is the Codici (40 acts, 10.5 MB).

Written against the live data, not the 2021 profile. A census of the 40
codes: partitions are flat `<chapter>`s whatever their level; notes sit in
an article's `<section>`; the Codice civile's 3,280 articles arrive as
annexes; two members are base64 of the XML. All 40 parse (≤1.3 s the
largest), the rendition reads back to the AKN's skeleton on all 40, and no
`((` survives (the `))` left are a lettera closing a parenthesis).

Decisions taken in the build, open to the operator: `keys` are EMPTY
(parser 2, the same evening — parser 1 wrote unregistered `eli:`/`urn:`
ids, and 05 §5.2 says a plugin writes only registered ones; the
identities are in `extra`); preamble, conclusions and notes
are kept in the record and not embedded; the rendition is versioned by the
PARSER_VERSION (the parser owns `sections` here, not the chunker as §4.3
had it); `max_chars` is 8,000 until the pilot measures it; the spec
declares no file sizes (the origin rebuilds zips in place, and a stale size
could truncate a segmented download), so a rebuilt zip is not re-fetched
by itself; `eli_in_force` is false only on the origin's word (the repealed
acts' collection). Still the operator's: the encoder (the §7 pilot) and
creating the collection; confirming the CC BY 4.0 statement on the live
portal before acknowledging (TERMS.txt says so). Not built: the delta feed
(`ricerca/aggiornati`), the SKOS type tables, the ReferenceSource of §6,
the manifest's `eli` block (§5.3 — a wire-format change, so ask first).
Certified-compatible: the GU identities — the by-id permalink, codice
redazionale, publication date and number — are in every record.

**Deployed 2026-09-14 12:46 UTC** (1c3178e, image `b6fdb73…`): the node
suite against the staged tree passed (2,627); web healthy, both
containers list `normattiva` in the plugin registry, no error in the
worker's first minutes, the node still acquire-only. The code only — no
Normattiva collection exists until the operator creates one.

### The operator's answers, 2026-09-14 evening

* **Normattiva's encoder is the operator's side project** — a dedicated
  embedder trained for it. No off-the-shelf encoder is to be proposed and
  no collection created meanwhile.
* **The CC BY 4.0 statement is confirmed**; TERMS.txt says so instead of
  asking for it (no acknowledgement existed to invalidate).
* **Gutenberg parser 8.** `date` is the year of the physical edition PG
  transcribed — marc906, else marc260 `$c`, else the header's "Original
  publication" (they agree where more than one is present) — and
  `extra.issued` keeps the day it entered PG; `extra.date_source` says
  which. PG records no first-edition date. The census: marc906 on 12,633
  books and marc260 on 12,994 of 79,382, so most books stay null (9 of the
  32 sampled are dated). Italics are kept as `_…_`, the plain text's own
  convention: `<i>`, `<em>`, `<cite>`, inline italic styles and the
  classes the book's stylesheet sets italic (stage directions, verse) —
  76,715 runs over the 32 sample books. Also fixed on the way: a
  self-closing `<script/>` or `<style/>` silenced the rest of its
  document. The text moves, so parser 8 reaches records through the same
  new collection parser 7 needed (cleaning_version 3).
* **`genesis recount pubmed --yes`, done** with the worker stopped:
  medcpt-pubmed shards 13, 8 and 9 set from chunk provenance (+53,000,
  +26,154, +1,500); a recheck finds all 37 ledger rows matching. The worker
  restarted in acquire-only.

**Deployed 2026-09-14 13:40 UTC** (8978ee5, image `cab89ee…`): node suite
on the staged tree 2,643 passed; web healthy, the worker on gutenberg
parser 8, no error in its first minutes, still acquire-only.

**Wikipedia: one collection, wikivec's languages, two layers** (the
operator's plan, restated 2026-09-14 after I had narrowed it to "English,
two sister collections" — wrong). Steps 1–3 built: language-bearing ids in
wikivec's numbering (parser 4), the plugin reading the archive's own text
for the articles it covers and the dumps for the rest, and genesis
importing layer 1's vectors under the collection's ids while the local
encoder keeps off what the archive supplies. The plan, the
mapping onto the documents and what is next are in
[`wikipedia-two-layers.md`](wikipedia-two-layers.md). The legacy
wikipedia/wikivec stay parked in `config/collections-legacy/`, data
untouched, until the collection exists; the `embedded` plugin stays until
then. Boxing `genome` and `packed` (operator: "old plugins … in a box")
is queued behind this.

**Deployed 2026-09-14 15:43 UTC** (15845b3, image `3627030…`): wikipedia
steps 1–3. Node suite on the staged tree 2,670 passed; web healthy, one
image for web and worker, the worker on wikipedia parser 4 with the
Upstash archive declaring its eleven language blocks, no error in its
first minutes, still acquire-only. No collection runs the plugin yet.

**Deployed 2026-09-14 17:29 UTC** (2d49997, image `e7fe4db…`): archive
records located by URL at Upstash revision `ce4e0ea`, the build's refusal
of an uncredited archive, and `cdc` sizes read from the layer. Node suite
on the staged tree 2,681 passed; one image for web and worker, web
healthy, no worker error, still acquire-only. The archive's `data/` is
mounted read-only at `/corpora/wikipedia/data` in both (operator's go,
earlier the same day); the bge-m3 endpoint serves 8,192 tokens, which 03 §3.3
allows for the space's `ctx` of 1,024.

**Before wikipedia's `create`, 2026-09-14 — four defects found and fixed,
one left for the operator.**
- *Discovery ignored the snapshot pin.* It reached acquisition only, and
  dated dumps coexist in one corpus by design, so the pinned `20260801/`
  and the legacy `latest/` would have minted the same English pages
  twice and flipped them on every scan. `DiscoverState.snapshot` now
  carries the pin; the plugin reads `<wiki>/<snapshot>/` alone, and an
  unpinned corpus refuses a second dump of one wiki (06 §6.1).
- *`origin.spec.files` entries were copied as written.* TOML has no null,
  so an entry without a checksum reached the Codex without the key. The
  template now writes 09 §9.17's three keys always and refuses others.
- *`create` defaulted `--platform-image` to `digest(b"")`* — the SHA-256
  of nothing. The recipe path now refuses a missing or placeholder
  digest before reading anything. **The live gutenberg Codex carries that
  placeholder** (`toolchain.platform_image` = `sha256:e3b0c442…`), so its
  `collection_id` names no build; pubmed's names a real one. Replacing
  it is a new collection, and the operator's decision.
- *A reset could be hidden by a refresh already in flight.* It blocked
  the fresh count and could write its pre-reset result after the
  invalidation — seen once as an intermittent
  `test_a_reset_invalidates_the_cache`. An invalidation now bumps a
  generation; `test_counts_invalidation_race.py` forces the interleaving
  and failed before the fix.

Also done: all 1,447 archive shards equal the Hub's LFS sha256 at the
pinned revision; the English 20260801 dump hard-linked into its dated
path; the recipe drafted (ctx 1,024, two layers, `work` 1.6B tokens a
month) and not yet on the node — `create` mints the permanent id and
waits for the operator's go.

**Deployed 2026-09-14 18:44 UTC** (6d80c30, image `5fbf103…`): the four
fixes above. Node suite on the staged tree 2,692 passed; one image for
web and worker, web healthy, no worker error, the archive mount still
read-only in both, still acquire-only.

**Wikipedia created, 2026-09-14** (operator's go): `collection_id`
`sha256:b8ff5fab73452a2796e2ab0912fed2cfeee6cf554b79772d69e6cba01345bc47`,
from `config/recipes/wikipedia-real.toml` in the worker, with bge-m3
embedding the calibration set and `toolchain.platform_image` the digest
of the image the command ran in (07 §7.3). The Codex checked field by
field against the recipe. Its TOML pins snapshot 20260801; the node loads
gutenberg, pubmed and wikipedia, and the worker withheld the new one's
scan and fetch for want of an acknowledgement.

**Acknowledgement by terms hash (04 §4.4).** "An acknowledgement is of a
text, identified by its hash … A collection whose Codex names a
`terms_sha256` this peer has already acknowledged … is acknowledged by
that record." The gate compared a collection's OWN record only; it now
also reads every record on this peer — Codex collections'
`state/ack.json` and collection TOMLs' `license_ack`, the parked legacy
ones included — so the operator's 2026-08-17 acknowledgement of the
CC BY-SA text covers the new collection.

**Read wrong, corrected (operator: "read the manual").** Gutenberg's
Codex value: 07 §7.3 — `toolchain.platform_image` is "the digest of the
platform the command runs in", here the image its `create` ran in, which
is recorded nowhere. 03 §3.4 makes that digest the reference build every
other image must prove equivalent to; with the placeholder there is no
reference, so no peer can build gutenberg's records, and the Codex also
pins parser 5 against the node's 8. 02 §2.7's relations do not cover a
Codex with no snapshot and no defect in records or vectors: the
operator's decision. And `create` should, per 07 §7.3, default to its
own image's digest rather than refuse — pending the operator's go on the
deploy passing that digest into the containers.

**Deployed 2026-09-14 19:28 UTC** (aed89c3, image `e18234a…`): the
acknowledgement by terms hash. Node suite on the staged tree 2,696
passed; three collections loaded, one image for web and worker, web
healthy, no error. Wikipedia reads `acknowledged` (the 2026-08-17 record)
and the worker started its fetch and first discovery at once: the
20260801 index files were landing within the minute (dewiki, frwiki).
Still acquire-only — nothing is embedded.

**`create` pins the platform it runs in (07 §7.3), operator's go
2026-09-14.** `toolchain.platform_image` is "the digest of the platform
the command runs in unless the recipe states it": the recipe's own
value, else `--platform-image`, else `LOUISE_PLATFORM_IMAGE`, which the
deploy now sets in web's and worker's environment to the image they start
in (a container cannot see its own image id) and checks against the
running image. With none of the three, or with the empty-bytes
placeholder, `create` refuses. This replaces the refusal-without-the-flag
of earlier the same day, which contradicted 07 §7.3's command shape.
Deployed 2026-09-14 20:17 UTC (1a01753, image `b8ea620…`): in web and
worker `LOUISE_PLATFORM_IMAGE` equals the running image.

**Gutenberg's Codex, corrected by the book (operator: this is the
refinement of v1.0; fix what is needed, restart nothing, the result
indistinguishable from one made by the book).** The live Codex is the
PLATES collection — its recipe, `gutenberg-plates-recipe.toml`, is now
kept in these notes; `config/recipes/gutenberg-real.toml` is the older
plain-text one. `create` from it, in a scratch root, under the deployed
image: `sha256:7de4793add991892c9104e079608700a469840e67d25748b86d2233c315e27ee`,
differing from the live Codex in exactly `parser.version` (5 → 8) and
`toolchain.platform_image` (placeholder → the image it ran in); all 35
fields otherwise identical, calibration included. The swap into the
drag, with the TOML repointed, was blocked for me by the permission
classifier and is the operator's to run (`gutenberg-swap.sh`, which
checks the starting state and moves nothing unless it holds); the
manifest, cursor and text index stay where they are.

**Wikipedia's card shows its pre-embeddings like PubMed's** (operator):
the same sub-box, rows and badge. The Library now keys the sub-box on a
`standalone` flag — only wikivec, whose card IS the archive — instead
of on the archive's kind, and a collection whose archive files are all
on disk reads them all as fetched.

**Deployed 2026-09-14 20:55 UTC** (8408115, image `d7e2c35…`): node suite
on the staged tree 2,704 passed; one image for web and worker, each with
`LOUISE_PLATFORM_IMAGE` = its running image; no error in either. The
live web builds wikipedia's pre-embeddings as the sub-box: not
stand-alone, 1,447 / 1,447 archive files fetched, badge done.
Gutenberg's TOML still pins the placeholder Codex until the operator
runs the swap.

**`keys`, settled by the spec rather than open.** 05 §5.2: a record's
`keys` restrict a leg to the records holding a RESOLVED identifier, and a
plugin writes only ids whose namespace a reference source has registered —
any other form never matches the resolver's, silently. Nothing registers
`eli`, `urn`, `wikipedia` or `wikidata`, so normattiva (parser 2) and
wikipedia write none. The one real choice left: whether a Wikidata
reference source is wanted BEFORE the wikipedia collection is created —
adding keys afterwards is a new collection. (Live retrieval tags documents
from their text into `doc_keys` and does not read record `keys` yet.)
Deployed 2026-09-14 14:01 UTC (74173fc, image `fa25111…`): node suite
2,643 passed, web healthy, normattiva parser 2 in the worker, no errors.

**Gutenberg's swap: run by the operator, verified 2026-09-15.** The TOML
pins `sha256:7de4793a…`; the placeholder Codex's files are kept beside
the drag in the staging directory, not deleted.

**`genome`, `packed` and `pack adopt` boxed in `attic/`** (operator,
2026-09-14: "remove them from the active code keeping them in a box for
reference until they are either converted to the final format or
permanently dropped"). [`attic/README.md`](../../attic/README.md) lists
what moved and why. No registry, spec, CLI verb, create-dialog path or
help text reaches them now, and the image does not carry them. Tests
that used them as the worked example now use a live plugin or an inline
spec and keep their principle. `embedded` stays until wikipedia's
records are built from the archive ("remove wikipedia only once it is
migrated"). The three genome collections stay parked in
`config/collections-legacy/`, their data untouched.
Deployed 2026-09-14 23:03 UTC (f5269f6, image `13cbc58…`): node suite on
the staged tree 2,683 passed; the image asserts no genome/packed in
either registry and no boxed module shipped; one image for web and
worker, each with `LOUISE_PLATFORM_IMAGE` = its running image; web
healthy, mode still acquire-only, no error in either log. The restart
cut wikipedia's post-fetch discovery scan (running since 22:07), which
restarted at startup.

**The collection file is 07 §7.8's (2026-09-15).** `codex`, `title`,
`slices`, `kinds`, `embed`, `share`, and an `[encoder]` that names the
endpoint only. The archive, the dump, the space's window and the origin
were a second answer to what the Codex or the tree already says; they are
derived now (`collections.codex_defaults`), the old keys still read, and a
file in the old shape keeps the old defaults for what it leaves out.
`hold collection create` writes the file. **Origins** live at
`<LOUISE_ROOT>/collections/<name>/origin/` (operator; docs/07 on main,
43703e3) — on the reference node a mount of the archive-pool datasets
that already hold them, so no byte moves. Origins on the SSD pools go to
the archive pool (operator): only the boxed genome FASTAs were there,
copied to the archive pool's origins dataset and verified by checksum;
the SSD copies wait for the operator in
[`legacy-to-delete.md`](legacy-to-delete.md).

**Wikipedia's "3.04M changed" is not a content change.** All 12,640,809
"changed" pages are enwiki's: acquisition re-fetched the hard-linked
dump (fetch record: 26,668,484,995 bytes, done 22:07:33 UTC) because the
pinned spec declares no size for a dated dump, so the copy already in
place could not be adopted — 27 GB moved again, the same bytes. The dump's
mtime moved, so discovery re-read every enwiki page; none had been
parsed, so each got its first hash and nothing was reset. The fix is in
the engine, not the spec (`origin.spec.files` is inside `collection_id`):
a file whose spec declares no size is adopted at the origin's
Content-Length rather than fetched again (af66b87).

**The Library's new collection is `hold collection create --recipe`**
(07 §7.6): `POST /api/hold/collection/create` runs the same function as
the command (`louise.create_from`) and writes the Codex and 07 §7.8's
file; the page's form is plugin, space-from and recipe, and shows the
command line. The old form wrote a file naming no Codex; its routes stay
one release as deprecated aliases. Help's "Adding a collection" follows.

**Deployed 2026-09-15 02:15 UTC (8261747, image `b9abcab…`)** at the
operator's instruction. Before stopping anything the script loaded the
new files under the new image with the new mounts: all three, with
profiles full/full/all, pubmed's archive `medcpt-pubmed`, wikipedia's
`upstash-wikipedia-2024-06` and dump 20260801 derived from their Codexes.
Web and worker on one image, `LOUISE_PLATFORM_IMAGE` = it; each origin
visible at `<LOUISE_ROOT>/collections/<name>/origin`; the old files kept
as `*.bak-20260915`; no error in either log. Discovery logged "corpus
moved" for all three at 02:15:05 and re-enumerates to rewrite their
stored locators; the old container paths stay mounted until it has, then
leave the compose file ([`legacy-to-delete.md`](legacy-to-delete.md) §6).
The node runs in mode `running` (the operator's switch). The host has
little memory to spare: at 02:08 UTC the kernel's OOM killer took the
CPU TEI encoder four times (~12 G each) and a `llama-server` once, the
TEI kills during a 512-text calibration run against it.

**A GPU bge-m3 endpoint, checked against wikipedia's space (operator,
2026-09-15).** llama.cpp (Intel build b10630) serving `bge-m3-FP16.gguf`.
With `--pooling last` it failed wikipedia's calibration gate outright —
median cosine 0.42: bge-m3 is CLS-pooled and the Codex fixes
`pooling: "cls"`. With `--pooling cls` it passes: worst 0.9990 over all
512 calibration texts, ~38,700 tokens/s. Against Upstash's own vectors
(160 rows, five languages, short and long) it agrees with the CPU TEI
encoder to 0.9994 or better on every row, and is deterministic, batched
or single. A row re-embedded from its `text` alone can fall to 0.78
against the archive; with its title prefixed — what Upstash embedded,
and what `wikipedia.archive.embed_input` sends (amendment 11) — the same
rows are at median 1.0000, worst 0.9607. It is not wikipedia's
configured endpoint: that is one line in the collection file, the
operator's to change.

**Wikipedia's archive import stood still after the origin move, and why
(found and fixed 2026-09-15).** Discovery's moved-corpus repair cleared
`wikipedia_scan_cursor.json` and left the archive walk's own cursor,
`wikipedia-archive_scan_cursor.json`; cursor keys are corpus-relative, so
every archive shard read as unchanged, no archive locator was rewritten
(0 of 60.1M rows in 40 minutes), and the archive import — which groups
its candidates into shards by locator, relative to the new root — had
nothing to do from 02:09 to 03:16 UTC with 884,176 documents waiting.
`cursor.clear` now takes a plugin's sub-cursors too (d06efe7, deployed
03:12 UTC, image `25a2bc0…`; the Library's rescan had the same gap). After
the restart the archive walk re-enumerated, 1.45M of the first 2M rows
were relocated within five minutes, and imports resumed: 35 shards
applied, 254,431 documents with the archive's vectors.

**A PubMed click took 134 s (found 2026-09-15).** Profiled live: the
reader's row lookup, `CAST(native_id AS INTEGER)=? OR native_id=?`,
reaches no index — the unique one is (source, native_id) — so every
click scanned the 40M-row manifest; the text index, the pack and the
reference line together took 0.07 s. The Ask panel's abstract never
arrived. 7f2fbe7 probes the unique index, plan-checked; deployed 03:33
UTC (image `b2aef0e…`, node suite 2,725 passed): the same page now
answers in 0.58 s, the Ask panel's fragment in 0.015 s. (Six archive
shards were skipped as retryable at 03:32–03:33, during the deploy's
image build, when the CPU encoder their sample proof used did not answer
in time; they are retried after the ten-minute backoff.)

**Wikipedia's encoder is the GPU bge-m3 endpoint (operator,
2026-09-15)** — the llama.cpp one that passed wikipedia's gate, in place
of the CPU TEI encoder; one line in its collection file, the old one kept
beside it. Embedding is still off, so for now it serves the archive
import's per-shard sample proofs.

**MedCPT as GGUF (operator, 2026-09-15).** Both encoders converted to
f16 with llama.cpp b10630 — its converter refuses a tokenizer hash it
does not know, and MedCPT's WordPiece vocabulary is not in its list; for
a WordPiece vocabulary that label is not what llama.cpp tokenizes by, so
the refusal was patched to "default" inside the throwaway converter — and
served by a new GPU app, Article and Query. Against the CPU TEI pair on
all 512 calibration texts cut to 500 tokens: article min 0.9896, query
min 0.9910, medians 1.0000, deterministic, batched equal to single. NOT
yet pubmed's endpoints: llama.cpp refuses an input past its batch (512)
where TEI truncates, and the platform cuts to `ctx` by a character
estimate that overshoots on PubMed text (774 tokens seen; 199 of the 512
calibration texts past 500). By the book (09 §9.3: a chunk is truncated
to `ctx`) the platform cuts by the endpoint's own tokenizer — the
operator's go pending.

**Rollback mark: three embedding servers on one GPU (operator,
2026-09-15).** From **03:14:41 UTC** the Intel GPU serves three
llama.cpp embedders at once: Qwen3-4B (gutenberg, since 02:12:46),
bge-m3 (since 02:18:11) and the MedCPT pair (since 03:14:41); wikipedia's
encoder moved onto it at **03:34:26 UTC**. Should vectors computed on it
turn out corrupt, the rollback is per collection: the documents with
`embed_done=1 AND updated_at >= 03:14:41 UTC` — the embed stage stamps
`updated_at` when it writes a vector; other stages move it too, so this
over-selects, which is the safe side — are reset and re-embedded. Only
COMPUTED vectors are at risk: an archive's imported vectors are not
computed on the GPU (it runs their 16-sample proofs). The data dataset
has no ZFS snapshot to roll back to instead, and the SSD pool's backup
replication was in error when this was written.

**Pubmed on the GPU MedCPT pair — what it took (2026-09-15).** Not the
tokenizer cut proposed above: llama.cpp's refusal (HTTP 500 "too large
to process") now shrinks the character cut exactly as TEI's 413 does
(b1e56ab), so both pairs embed the same inputs. With the staged image,
pubmed's gate on the GGUF article encoder passed 16/16 (whole set worst
0.9896, median 1.0000), and all 512 calibration texts UNCUT through the
platform's client, GGUF against TEI, gave article min 0.9896 (the 193
past the first cut: min 1.0000) and query min 0.9910 — none below 0.95.
The node suite then caught two vector-writer races, both older than this
work, fixed before the deploy:
- 1ef447c — on a fresh collection the parallel embed stage's first
  writers raced; one found `chunks` listed but not yet committed, and a
  healthy document recorded "Table 'chunks' was not found". 6 of 150
  runs under load before, 0 of 150 after.
- ac54d6c — a document another writer stored between upsert_book's
  pre-lock read and its lock was appended on top of (24 rows for 12
  ids). A check under the lock costs 0.55–0.65 s on pubmed's table in
  every shape measured (35.4M rows, 7,967 fragments; the book_id BTREE
  covers all but 7,601 rows and the plan uses it), so it runs only when
  the table moved in a way this process's own log of versions cannot
  account for. 150 of 150 runs of both races under load after.

Measured on the way and not changed: upsert_book's per-document
`_written_chunks` read costs ~1 s on pubmed's table. Four threads at ~1 s
each bound the embed rate near 4 documents/s, and pubmed embedded ~3.3/s
on the CPU pair — so the GPU pair may not raise the rate until that read
is cheaper; the fragment count is the likely lever (compaction).

**Deployed and switched.** ac54d6c deployed at 04:59 UTC (image
3f4ea11, web and worker alike). Pubmed's `[encoder]` moved to the GPU
pair at **05:00:42 UTC** — its two URL lines only, edited in place: the
live file carries `embed = "all"`, which a copy prepared elsewhere said
"off" and would have undone (the swap refused it; the TEI file is kept
beside it). The GGUF article encoder took 132k prompt tokens between
05:01:14 and 05:04:47, and pubmed embedded 307 documents in its first
full window on it (05:01:42–05:03:41) with no error — about the rate it
had on TEI (333–440 per window), as the ~1 s read predicts. TEI's
article encoder served the embed call already streaming when the file
changed, and nothing after 05:04:14. These vectors fall under the
rollback mark above.

**Version prune against a table that never stops writing.** Pubmed's
index maintainer skipped its version prune at 04:40 and 05:04 UTC ("Too
many concurrent writers", after 30 s of retries). The first skip is
older than the deploy: prunes had succeeded through 03:38, and the skips
began once pubmed embedded without pause. `optimize` (compact + prune +
index, the only entry point this lancedb has) loses its commit to the
embed stage's adds. The table stays intact and gains versions until a
prune wins. The earlier successes logged "pruned -312" and "-229":
`before - after` also counts the versions concurrent writers added
during the call, so the figure is wrong, not the prune.

**Compaction, measured on a copy (operator's choice, 2026-09-15).** The
table is on the SSD pool already, so moving it would buy nothing. A block-cloned
copy (16 s, almost no space) was run through the app's own `optimize`
(2 h retention) with nothing else writing: 3.9 min, peak 1.2 GiB, rows
unchanged (35,461,914), versions 15,012 → 11,935. upsert_book's
per-document read went from a median of 437–491 ms to 8 ms, the id read
from 304–351 ms to 1.8 ms. So the live table's cost is the missed
maintenance, not the disk — and the maintainer loses its commit to the
continuous embed (above), so on the live table it needs the embedding
paused for its few minutes, or a way to win. Not done on the live table.

**Library badges.** The license ✓ followed `ack_recorded`, so only
pubmed showed one — gutenberg is green, and wikipedia (yellow) is
acknowledged through a record of its terms elsewhere on this peer, which
that flag does not see. It now means nothing gates use (green, or a
valid acknowledgement), with a tooltip saying which; read back from the
new image, all three carry it (3d0b0fd, deployed 07:02
UTC). The GPU/CPU badge reads
the [encoder]'s `device` and infers CPU from concurrency > 1 without it:
pubmed and wikipedia run on the GPU at concurrency 4 and do not declare
it. The one-line `device = "gpu"` edits were refused to me by the
permission classifier and are the operator's to run.

**GPU/CPU labels, fixed at the source (09:01 UTC).** On the operator's
explicit instruction the in-place edit went through: pubmed's and
wikipedia's files declare `device = "gpu"` (backed up beside them), and
all three collections resolve to GPU in the live image.

**A pause that did not stop the CPU (349f639, deployed 08:58 UTC).**
Paused, the worker held ~9 cores — twelve lancedb threads. The index
maintainer read the pause once per 60 s cycle, before its loop over
collections, so a cycle under way ran every collection's Lance optimize:
pubmed's for 485 s during the pause, then the next. It is asked before
each collection and between `ensure`'s steps now. A Lance call already
running cannot be interrupted and finishes — an IVF build can take up to
an hour — and nothing after it starts. Not yet seen on a paused node: the
operator resumed before the deploy. (The 485 s run was pubmed's live
prune finally winning its commit, with nothing writing.)

**Blocked documents that were not broken (687a2c0, deployed 09:23 UTC).**
46 pubmed documents blocked this morning, every one by a moment: 41 ×
HTTP 503 "Loading model" (llama.cpp while its weights load — the GPU
MedCPT app was restarted twice, for the CPU reranker's service and its
removal), 4 × vector-lock timeouts at a deploy's restart, 1 connection
dropped mid-request. Both embed paths now defer 429/502/503/504 and lock
contention; the parallel one — pubmed's — asked only whether the encoder
was unreachable. "Clear errors & retry" read as doing nothing because it
rewrote EVERY in-scope document (`error IS NULL OR …`): 40M rows in one
transaction on pubmed, minutes under the manifest's lock, starving the
worker and outliving the request. It now reads the matching rows first
(~75 s, no write lock) and writes only those, in the background. The 46
need one press of the button, which the dropped connection's document
also does.

**A reranker per collection, on the GPU (6dcb987, deployed 09:23 UTC;
node configuration 09:25–09:38).** Operator's decision: each collection
reranked by its own reranker, on the GPU where the model runs correctly.
PubMed shares gutenberg's and wikipedia's until MedCPT's cross-encoder can
run correctly — 0.32 s per candidate on the CPU even with the node paused,
and as GGUF llama.cpp gives every token segment 0 (reranker-scope.md §12).
The reranker is BAAI/bge-reranker-v2-m3 as f16 GGUF, which reproduces
BAAI's own weights (transformers on the same 8 pairs: identical ranking,
largest difference 0.04), served as a `bge-rerank` service beside bge-m3
in its app, :30447, llama.cpp `--reranking` with four slots. Each
collection file names it in `[reranker]`; `platform.toml`'s `[ask]
latency_budget_ms` is 8000 — and, confirmed by the operator, the spec's
default too (07 §7.8, was 400). The code now does what 05 §5.6 says and it did
not: each collection's list reranked on its own and fused by rank, the top
50 reordered, candidates sent whole, a fallback reported as `reranked:
false`. The CPU MedCPT reranker services added to both MedCPT apps this
morning were removed, with the model they had downloaded.

Measured live through /api/search: pubmed 335–543 ms and wikipedia
361–671 ms every time; gutenberg 2.8–6.4 s (its candidates are ~1,500-token
chunks); all three together 2.4–4.7 s. Of the last two runs' 16 lists 15
were reranked; one (wikipedia's, in a federated search) fell back at the
budget while the shared GPU was busy. At the spec's 400 ms default every
list fell back. With one slot, a request abandoned at the budget kept the
server computing and every search behind it ran out of time too — seen at
5 s, gone with four slots.

**The pause, seen live; pubmed compacted (11:56–12:02 UTC).** The
operator paused the node and turned pubmed's embedding off. Nothing
started after 11:56:38 — no stage, no table written — and the only work
left was wikipedia's index maintenance, one Lance call begun before the
pause, running on to its end as the fix intends (~9 cores meanwhile).
With no writer on pubmed's table, `app.cli index vector prune pubmed` ran
the app's own optimize in 72 s: 1,830 superseded versions dropped (14,668
kept — the 2 h window, and every embed writes one), data files 21,114 →
19,594, the book_id index covering every row. upsert_book's per-document
read went from a median of 383 ms to 7.7–8.3 ms — the copy's 8 ms,
reproduced. The maintainer's own optimize keeps losing its commit to a
continuous embed; while that is so, compaction wants a pause.

**Memory.** With the node out of acquire-only `free` showed 2 GB of 61 GB
available; the operator stopped a llama.cpp probe container (8.5 GB).
Most of what it freed went back to the ZFS ARC (9.2 → 13.9 GB), which is
reclaimable, so the committed load is ~45 GB — the worker 16.9 GB and
the idle CPU bge-m3 encoder 12.1 GB the largest. The OOM kills of 02:08
UTC came during a 512-text burst to that encoder; whether the ARC was
slow to shrink then is not known.

**How the deploy ran.** Local and node suites 2,719 passed on 8261747.
The script (stop the app, swap the three collection files, add the
origin mounts, restart) checks the new files load under the new image
and mounts before it stops anything. The permission classifier refused
its first run; it ran on the operator's explicit instruction. Leaving
acquire-only was refused to me the same way and was the operator's
switch in the Library.

**Gutenberg search, 24 s → 7.5–9 s (58a21f2, deployed 12:52 UTC).**
Measured live: 19.2 s of a 24.4 s search was the lexical leg with
translation, 0.6 s without. Ollama unloads a model after 5 idle minutes
and searches come less often, so nearly every search reloaded qwen3:8b
(6.4 GB, on the GPU) before translating; and the 12-language cap took the
alphabetically first groups. Now `keep_alive` is 30 min (`LLM_KEEP_ALIVE`)
and the query goes to the 6 largest groups by chunks indexed
(`TRANSLATE_MAX_LANGS`). Warm, after the deploy: translation 2.6–3.3 s,
BM25 over 18 groups 0.6 s, reranking 3.3–5.4 s (its ~1,500-token chunks),
total 7.5–9 s; the first search after an idle half hour still pays the
~13 s load.

**The old origin mounts are gone (same restart).** `fetch_files.local_path`
was the last store of the old paths (386,921 gutenberg, 2,668 pubmed, 22
wikipedia rows), relocated to `/data/collections/<name>/origin/` first;
every other table of the three manifests, node.sqlite and refs.db was
scanned and names none. `/corpus`, `/corpora/pubmed`, `/corpora/wikipedia`
and `/corpora/wikipedia/data` left both services, `CORPUS_DIR` points at
gutenberg's `origin/`, all three collections load, and the startup fetch
found every file where the ledger says (2,668 and 22 skipped as held).

**The SSD pool's backup.** Its replication had failed since 2026-08-28:
excluding datasets it had already sent took away its incremental base.
The old target is kept as `tank/backup/ssd4-until-2026-08-28` (1.87 T,
legacy-to-delete §6) and a fresh replication ran at the old name. It
excludes `apps/louise-rag`, so the live collections' 1.1 T has no copy
newer than August — a manual snapshot
(`ssd4/apps/louise-rag@manual-20260915-before-cleanup`) sits on the same
pool. Whether to replicate it is the operator's call.

**Pubmed's write path, measured (12:40 UTC, embedding on).** ~2 documents
a second (1.3–3.0 per 20-minute window since 10:40, no downward trend),
and the MedCPT article encoder idle ~90% of the time (11 ms a request,
~150 ms between). The hourly prune is `optimize` and compacts too; it won
at 10:39 and 11:43. Between prunes every embedded document adds one small
fragment (6,529 of 6,566 fragments at 12:49) and one version, so each
per-document read and commit costs more as the hour goes on. That is the
engineering rules' "per-item cost that depends on how much has been
processed", bounded only by the prune interval. The fix is in the write
path: several documents per Lance commit (one fragment, one version) —
proposed, not built.

**MedCPT reranks pubmed, on the GPU (a93ccfe; live ~13:40 UTC).** An
OpenVINO sidecar (`deploy/medcpt-rerank/`) builds the segment ids with
the model's own pair tokenizer, which neither llama.cpp nor OpenVINO
Model Server's `/v3/rerank` does. Against transformers fp32 on 3 real
queries × 50 candidates: τ 0.974–1.000, top 10 identical, 72–95 ms per
list, against ~16 s on the CPU. Through the search: 300–392 ms.
reranker-scope.md §12 has the numbers.

**A Library change deleted a collection's reranker (cae114a).** Pubmed's
file had lost its `[reranker]` when its embedding was switched back on
(12:16 UTC). Both collection-file writers rebuild the file from the
fields they know, and `[reranker]` was not among them. Pubmed's searches
were unreranked from 12:16 until the block was restored, and nothing
said so. The round-trip guard compared None with None because its
fixtures declared no reranker. A new test now fails on any field left at
its default in both fixtures; it found `title`, `slices`, `kinds` and
`share` covered the same empty way.

**The live collections are backed up; the old backup is gone (operator,
14:30 CEST).** `ssd4/apps/louise-rag` was excluded from the SSD pool's
replication and had no copy newer than August. Its churn under snapshots
is steep: this morning's manual snapshot held 49.6 GB 72 minutes after it
was taken (Lance manifests and compaction, wikipedia's imports, SQLite
pages rewritten in place). So it did NOT join the pool's weekly snapshot
task, which would have pinned days of that on an SSD with 615 G free. It
has its own: snapshot task 4 (hourly, kept 2 h on the SSD) and
replication task 6 to `tank/backup/louise-rag` (kept 1 day there), the
first full send ~450 MB/s. `tank/backup/ssd4-until-2026-08-28` (1.87 T)
was destroyed after a dry run showed no holds or clones. So was the
manual snapshot `@manual-20260915-before-cleanup` (operator, ~15:05 UTC;
49.6 GB), once tank held the full post-cleanup copy: it pinned everything
rewritten since 12:19 UTC, and ssd4's free space had fallen from 618 to
585 G in 40 minutes. What the SSD holds for snapshots is now bounded by
task 4's two hours.

**Pubmed embeds ~25× faster: one read and one commit per batch (370512c,
deployed 13:58 UTC).** Measured per abstract before: reading what it
already held 503 ms, the commit 37 ms, the encoder 32 ms. On a clone, the
read cost 697–918 ms as one-document commits added fragments (+0.5 ms
each), and a commit of 64 or 256 documents cost 9–34 ms in all
(scheduling.md pitfall 13). Now 128 candidates are read at once and the
embedded ones committed together, marked done inside the same manifest
transaction. Live, the four rounds after the deploy embedded 10,113,
9,088, 10,052 and 7,085 documents in ~120 s each (59–84 docs/s), 63 docs/s
wall-clock including the other stages' turns, against 2.50 docs/s before
(4,180 over the 28 minutes up to 13:52). No errors. The table held 333
fragments at 14:10 (296 small), where one-document commits had left
3,560 within 15 minutes of a compaction. At this rate pubmed's ~4.4 M
remaining abstracts take about 19 hours rather than about 20 days.

**Wikipedia: 7.2 M pages that were not articles, removed (84379e1,
1db6186).** The card said 38,138,536 documents lay "past id
109,999,999,999: newer than the archive". Those are the dump layer. 440 of
them, sampled at random and parsed through the plugin, were 279 redirects,
108 pages of other namespaces and 53 articles. Discovery skipped
non-article pages by an English list of namespace names, so on ten of the
eleven wikis `Kategorie:`, `Catégorie:`, `Категория:`, `رده:`, `Şablon:`…
came in as articles. It now reads each dump's own names from its
`<siteinfo>` header (keeping eswiki's `Anexo`, namespace 104: encyclopedic
lists, operator's decision). `tools/wikipedia_drop_namespace_pages`
removed the rows the old list had let in — 7,198,530 in 10 minutes, none
of them touched by rdf, fts or embed, in 1,000-row transactions the
worker never noticed: ru 1.39 M, fr 1.36 M, fa 1.12 M, de 0.80 M, pt
0.75 M, es 0.62 M, tr 0.61 M, it 0.53 M, ja, zh, en 23 k. The card now
says what lies past the range for each archive: for wikipedia, "pages
read from the dumps, which the archive does not hold" (pubmed's stays
"newer than the archive"). Redirects stay records with no text, their
target in `extra` (operator): 92 % of 330 sampled point to an article,
7 % to a section of one, none outside the wiki. Search reads a record's
text and its catalog fields (title, authors, subjects — for a dump-read
article, its categories); nothing else in `extra` is searched.

**A record with no text is not the encoder's work (2026-09-17).** The
same card called all 30,940,006 pages past wikipedia's range the local
encoder's, and most of them are redirects — records of their target and
no more. The counts scan gains one SUM (`arch_outside_empty`: in scope,
outside the range, indexed, no chunk), and the line now reads
"…; N with nothing to embed; only the local encoder can embed the rest".
`chunks` is the fts stage's, so a document still to be indexed is not
counted — unknown, never zero. Live: pubmed 8,576 of its 4,538,777;
wikipedia 0 of 30,940,006, because none of its dump layer has reached fts
yet (its rdf/fts are still inside layer 1), so that number grows as
indexing arrives.

**Wikipedia's three stages, measured (2026-09-17 06:00 UTC).** Position:
52,923,125 in scope, rdf 9.45 M, fts 9.45 M, vectors held 9.45 M, nothing
blocked — all of it inside layer 1 (the archive's 21,983,119 articles);
layer 2's 30,940,006 dump pages are untouched. Rates, from stage counters
over a 10-minute window: rdf 54.7, fts 77.1, import 77.7 docs/s (a
2.3-minute window read 121.5 / 49.5 / 62.7 — the three take turns on one
pipeline thread, so short windows favour whichever is running). The
page's own `stage_rate` agrees: 273 k/h ≈ 76/s for fts and import. The
import's unthrottled events give 45.3 docs/s over 41.4 h — the honest
long-run figure, which includes a 64-minute outage (the NAS rebooted at
05:08 UTC) and the index passes below. rdf and fts log lines go through
`_batch_log` (one per stage per 30 s), so event-derived rdf/fts totals are
lower bounds — count the counters, not the lines.

The import is healthy and paced by indexing as designed: 2,750 passes over
422 shards, 6.5 revisits each, 2.9 % of the documents it met not yet
indexed, 54 empty passes, 6.6 vectors a document.

WHAT COSTS THE NODE — CORRECTED an hour later, having first credited it to
the import: the worker holds ~10 of 12 cores (load 17.3), and 9.92 of them
are in `lancedb-tokio` threads (120 of 137 cumulative CPU minutes); tantivy
≈ 0, and the parse pool's eleven children idle at 0.7 % each — parsing is
NOT the bottleneck. Most of it is the HOURLY OPTIMIZE, not the import: the
pass that began after the 05:39 restart wrote a 2.45 GB fragment (with
196 and 173 MB siblings) at 05:43, touched 647 files and 773 MB in ten
minutes, and the process read 57 MB/s while writing 0.1 MB/s — a
compaction's profile, not a writer's. The import's own work is small
beside it: its `book_id IN (…)` lookup ~1 core (0.36–0.58 s for 100–500
ids), its delete-shaped `book_id IN (…) AND id IN (…)` 1.0–1.3 cores
(0.97–3.69 s for 600–6,252 ids), and its writes ~1.9 commits/s, ~1 new
fragment/s, 486 vectors/s into a 66.8 M-row, 155 GiB table. WHAT MISLED
ME: unindexed rows climbed at the full write rate throughout, which rules
out only the INDEX phase of `optimize()` — never the compaction that runs
before it. A pass logs nothing until it ends, so "no line since the
restart" is not "nothing running". The IVF-PQ index falls behind at exactly the write rate (4.7 M
unindexed rows and climbing, +486/s), and reclaiming that costs refreshes
of 1,672–2,525 s and prunes of 2,371–3,061 s — two of which lost their
commit to the writer ("Retryable commit conflict … Rewrite", 2026-09-16).
Those unindexed rows are also scanned by every wikipedia search.

At these rates layer 1 needs ~45 h more at 77 docs/s (~77 h at the
long-run 45 docs/s), and layer 2's 30.9 M pages ~110 h of indexing after
it — a week of pipeline time, and probably less for layer 2, whose
redirects carry no text.

THE FAULT IS THE CADENCE, not the import. `prune()` calls
`tbl.optimize(cleanup_older_than=…)`, which in this lancedb welds three
jobs into one: version cleanup, compaction, index update. They pay off on
different triggers — cleanup on the 2 h retention window (nothing younger
can be freed, so an hourly run is mostly wasted), compaction on fragment
count, the index refresh on unindexed rows (2 % of the table). Run hourly
as one call, the expensive half always goes first and the valuable half
starves: 4.7 M unindexed against a 1.34 M threshold, while every wikipedia
search scans those rows. Two passes also lost their commit to the importer
("Retryable commit conflict … Rewrite") and threw away ~40 minutes each,
because a prune — unlike a refresh — does not mark the collection busy, so
writers keep committing under it. Operator, 2026-09-17: add `pylance`
(`to_lance()` raises without it, which is what made `cleanup_old_versions`
and `compact_files` unreachable) and split the three.

**Prune, compact and refresh split — deployed 2026-09-18 06:28 UTC
(e4437a9, image `f21ddd6…`).** The operator authorised `pylance`, which is
what made the three reachable apart: `to_lance()` raises without it, and
`cleanup_old_versions` and `compact_files` both route through it. Verified
first on a ZFS clone of the live table — lancedb 0.38 and lance 9.0.1 read
it identically before and after a lance-side write, and the cleanup alone
took 18.5 s to drop 2,176 versions, 479 MB and 124 data files.

What it replaced, measured over two mornings on wikipedia's 155 GiB /
70.7M-row table: the hourly welded `optimize()` held ~10 of 12 cores (631
CPU-minutes in `lancedb-tokio` in one hour; 1,099 % on the container),
read 48–57 MB/s while writing almost nothing, and finished nothing —
three passes in two days died as "Retryable commit conflict … Rewrite" or
"Too many concurrent writers", because a prune marked nothing busy and the
archive import kept committing underneath a rewrite. Behind it the index
starved: 4.7M unindexed rows on 2026-09-17 and 8,626,627 by the next
morning, against a 1.41M threshold, every one of them scanned by every
wikipedia search.

Now: `prune` cleans versions only (no compaction, no headroom test — it
only deletes, and on a tight pool it is the work that relieves it);
`refresh` calls `optimize_indices` and nothing else; `compact` is its own
job on a fragment trigger (`VECTOR_INDEX_COMPACT_FRAGMENTS`, 4,000 small)
and holds the table through `maintenance()` so writers stand down instead
of taking its commit; and `ensure` runs the index before the fragments.
The welded call remains as the fallback where lance is absent, which is
the only path that still needs headroom — a test pins each half.
`list_versions()` is gone from the cleanup path too: 10.3 s on wikipedia's
7,891 manifests, which would have doubled an 18.5 s cleanup to fill in a
log line (`CleanupStats` already says what went).

Straight after the deploy the worker fell from 1,099 % to 190 % CPU, with
1.7 CPU-minutes of `lancedb-tokio` in its first two minutes against 631 in
the hour before, and the disk work became the useful kind: 143 MB/s read,
54 MB/s written, wikipedia importing es/037 and indexing 1,247–2,340
documents a commit.

**What it did, live (2026-09-18).** The NAS was off overnight and came
back at 05:07 UTC; by 06:16 the old code had wikipedia at 8,626,627
unindexed rows, 3,269 fragments and one maintenance pass running 67
minutes with nothing to show — the third such pass in two days, the
previous one having ended at 06:39 the day before as "Too many concurrent
writers … retry_timeout of 30 s". The split deployed at 06:28. Within
roughly twelve minutes wikipedia's unindexed rows fell to 2,084,583 — the
6.5M the old cadence had never got to — and pubmed's fragments fell from
1,547 to 207. Neither cleanup logged anything, correctly: no version on
either table was older than the 2 h window (oldest 1.4 h, the node having
just rebooted), so there was nothing to drop. Compaction stayed out of the
way, as intended, until the fragment trigger.

TWO THINGS STILL OPEN, recorded rather than guessed at. First, no
`info:index` line has appeared at all since the deploy, so the fold was
either an `optimize_indices` still in flight (it commits as it goes and
logs only when it returns) or Lance's own background maintenance — pubmed
compacted without any trigger of ours firing, which our code cannot
explain. Second, `lance_backgroun` threads hold ~6–8 cores continuously
(118.9 CPU-minutes in 19 minutes) while no index file is written and the
worker holds no open handle on either table; the plausible reading is
per-commit BTREE maintenance on Lance's runtime under an import committing
about twice a second, but that is a hypothesis, not a measurement. Both
want an hour with `optimize_indices` instrumented before anything is
concluded.

A THIRD DEFECT, fixed the same morning: `maintenance()` never actually
stood writers down — `worker._vector_writes_allowed` returned True for
every kind of maintenance (amendment 52, deliberately, because a
41-minute refresh reached every few minutes left a collection idle most of
its life). So the compaction's exclusivity, which is the whole reason the
old passes lost their commits, did not exist. `vector_index.rewrite` now
marks a FRAGMENT REWRITE specifically, and the writer gate defers for that
and nothing else: builds and refreshes keep their concurrency, a
compaction — rare by construction — gets the table to itself.

The two open questions above are answered by the same day's log. The fold
was `optimize_indices` in flight: it returned at 07:41 UTC (2,252,059 rows,
2,707 s) and again at 08:57 (2,115,347 rows, 2,576 s). The `lance_backgroun`
cores were that call, running on pylance's own runtime — a fixed 13–14
threads per process (30 `to_lance()` calls in a row add none; the 377 seen
once were Tokio's blocking-I/O threads, which exit after 10 s idle).

### Worker crashes: pubmed's pack shared one reader — fixed 2026-09-18

**Symptom.** pubmed embedded 38–41 docs/s on 09-17 and 23–35 on 09-18,
and the worker restarted three times in three hours (06:21, 07:42 and
09:00 UTC), each time refilling the fts and rdf backlogs before embed got
its share back.

**Evidence.**
- 06:21: kernel SIGSEGV in `libarrow.so.2500` at +0xabbcab.
- 06:57: `ArrowInvalid: ReadRangeCache did not find matching cache entry`
  recorded as an embed error on pubmed:40963103. The document has since
  been embedded; nothing was left erred.
- 07:42: glibc `double free or corruption (fasttop)`.
- 09:00: a general protection fault at the same libarrow offset.

**Cause.** `pack._open` cached one `pq.ParquetFile` per pack and handed it to
every thread. pyarrow 25 enables `pre_buffer` by default, so every read
replaces the reader's ReadRangeCache while another thread's read is still
in it. pubmed's fts and rdf stages and its four embed workers read the same
packs at once.

**Fix (fd190d9, deployed 10:06 UTC, image `1e8575b…`).** Every read opens its
own reader and closes it: one footer parse per 1,000-record row group. The
new test runs six threads over one pack. Against the old module it fails
with the exact error seen live, and it passes with the fix.

**What it was not.**
- *Not the shutdown.* The 09-17 poweroff was clean: systemd synced the
  filesystems, and every pool reports no errors. The worker logged `worker
  stopped (signal)` at 07:03:36 UTC after committing its last batches
  (1,534 pubmed documents and a wikipedia import batch), inside its 120 s
  grace.
- *Not pylance.* pyarrow was already pinned to 25.0.1, the crash is inside
  pyarrow's C++ reader, and the test reproduces it with no Lance at all.
- *Not louise's worker being killed.* The one container Docker SIGKILLed at
  every poweroff was the MedCPT reranker: a Python PID 1 with no SIGTERM
  handler. It now stops on the signal (165c778; exits 0 at once).

**Also found, upstream and bounded: lancedb 0.38 crashes at interpreter
exit.** A `lancedb-tokio-w` thread calls into libpython during finalization
(`_lancedb.abi3.so`+0x8dc90ab → `libpython3.12.so`+0x273130). This explains
the kernel lines at 06:48:38 and 07:07:56 UTC: a staging check and a probe.
- Measured on short-lived processes that ran a search: 9 of 80 exits
  segfaulted, 5 of 40 with pylance installed and 4 of 40 without.
- Effect: exit code 139 *after* the work is committed. It hits CLI runs,
  deploy checks and test runs; a long-running worker meets it once per stop.
- No data is touched, and no newer lancedb exists: `lancedb>=0.15`
  resolves to 0.38.0.
- Worked around the same day in `app/process_exit.py`. `python -m
  app.worker`, `app.cli` and `app.louise` run the parts of a normal exit
  that anyone can observe (threads joined, atexit callbacks, output
  flushed), then leave with `os._exit`, so the teardown never happens.
- Tests pin the behaviour. A child with a `__del__` shows the teardown is
  skipped; the control run through `sys.exit` shows the probe works.
  Twelve processes that searched a Lance table all exit 0.

**Every process we ship now has its SIGTERM behaviour in the suite**
(`tests/test_process_signals.py`):
- The real worker, mid-round, exits 130 with "worker stopped (signal)".
- The real web drains, then reports the signal.
- Every image under `deploy/` either runs uvicorn or runs a script that
  handles SIGTERM itself.
- `web` and `worker` run under an init.

The audit of the other collections is in the suite too
(`tests/test_reader_thread_safety.py`):
- wikipedia's archive opens a new reader for every read.
- normattiva reads its cached zips under a lock.
- gutenberg keeps nothing open.

`tests/test_invariant_guards.py` confines Lance to embeddings,
vector_index and spaces: one layer and one maintenance regime for every
collection.

**pubmed's rate after the fixes (11:15–11:35 UTC).**
- `sched_report --rates` over 300 s: embed 17.5 docs/s and fts 1,507
  docs/s.
- Embedding runs at ~31 docs/s inside its 120 s windows; fts holds the
  thread in between.
- 17,164,250 documents still wait for fts, 13.8M of them untouched since
  the 09-15 re-pin; nothing reset them today. Another 243,026 are past fts
  and wait for embedding.
- At 1,500–2,000 docs/s the fts backlog clears in about 3 hours. Then
  embedding has pubmed's thread to itself.
- The encoder answers in 5–36 ms, so it is not the limit.
- The index maintainer now runs at nice 10 (a27f780). A refresh had been
  taking every core at the pipeline's priority. That change did not move
  pubmed's rate: its threads were not starved. Scheduling pitfall 15 has
  the measurement.

**pubmed's past-archive card line, verified (2026-09-18).** The card
read "4.538.777 past id 36.999.999: newer than the archive; 53 with nothing
to embed; only the local encoder can embed the rest (4.314.026 done)".
Recounted from the manifest:
- 4,538,777 is exact, and no in-scope document lies outside the range
  without being past it.
- The 53 are empty at the origin, all of them in-process records with an
  empty `<ArticleTitle />`, no Abstract, no VernacularTitle and no
  OtherAbstract. The parser reads them right, and the records keep every
  other field.
- "done" was wrong. It was derived as embed_done minus the range's
  embedded, and the embed stage marks an empty document done without
  writing a vector, so 37 of the 53 were also counted as done.
  - The card now sums `arch_outside_embedded` in its one scan: embedded
    and not empty.
  - Exact at the time of the check: 4,538,724 with text, 4,359,996 done,
    178,728 left, none unindexed and none erred.

After the deploy (099f704), pubmed's card computes 4,538,777 past the
archive, 53 with nothing to embed, 4,416,978 done and 121,746 left.

**wikipedia's card, verified the same way (2026-09-18).** The card's own
code agrees with independent SQL, in both directions.
- Past the archive: 30,940,006 past id 109,999,999,999, all of them not
  yet indexed and none erred, so "0 with nothing to embed" is correct: an
  unindexed page cannot be known to be empty. Embedding is off.
- Inside the range: 21,983,119 documents, 10.98M holding vectors, and
  11.0M still being indexed.
- "Which the archive does not hold" holds: of 3,000 random dump-layer
  pages across all 11 languages, none has an archive-layer twin
  (`archive.covers` at discovery).
- Of 150 random dump pages, 132 are redirects (88%) and 18 are articles
  (12%). Once indexed, about 27M will read as "nothing to embed", and the
  local encoder's real work is about 3.7M articles (2.2–5.6M at 95%).

### ssd4 is louise-rag's pool; one hourly restore point (2026-09-18)

The operator wants a restore point for an SSD failure, not a time machine.

**What changed.** ssd4 now holds only `ssd4/apps/louise-rag` (plus the
system dataset).
- The model caches (`bge-m3-models`, `medcpt-models`, `dnabert2-models`),
  `comfyui` with its three children, and the `ita-legal-embed` directory
  moved to `ssd5/apps/`.
  - Each copy was a `zfs send -p` of one snapshot, compared file by file
    (path, size, mode, owner) before the original was destroyed.
- Every consumer was repointed:
  - `embed-gpu-medcpt` and `embed-gpu-bge-m3` were redeployed and are
    healthy.
  - The stopped `embed-cpu-*` and `comfyui` apps were updated in place and
    are still stopped.
  - The stopped standalone container `dnabert2-xpu` was recreated with the
    same settings.
  - The SMB share `comfyui` now points at ssd5.
  - The reference compose files in `deploy/` were updated too.

**Snapshots and replication.** Task 4 and replication 6 (louise-rag's own)
are gone. Task 3 now covers all of ssd4: hourly at :00, kept 2 h, no
excludes.
- Replication 5 holds pending snapshots, so a snapshot is not dropped
  before it is sent, and its retention follows the source.
- `tank/backup/louise-rag` was renamed to
  `tank/backup/ssd4/apps/louise-rag`. The common snapshots came with it, so
  the first run was incremental (6 s), not a 1.3 T send.
- `louise-rag@auto-2026-09-15_15-33` was destroyed. No task owned it (off
  schedule, so no retention would ever have removed it), the backup did not
  hold it, and it pinned 124 G.

**Left as they are.**
- `tank/backup/ssd4/apps/{bge-m3-models,medcpt-models,dnabert2-models,
  comfyui}` are the only backups of those datasets until ssd5's daily
  replication sends them from their new home (01:00). They are stale
  after that.
- ssd5 keeps its daily snapshots for a week.
- `ix-apps/docker` there holds 123 G in snapshots, 35 G of it pinned by
  the off-schedule `@auto-2026-07-29_00-40` on 47 datasets.

### Ask: every language, entries in full, how many (operator, 2026-09-18)

The operator's rules for the Ask tab, for every collection a search
touches: (1) a search covers all languages unless the query limits it;
(2) the drop-down is the language of the answer AND the entries; (3)
entries are translated in full; (4) changing it translates everything at
once. And: the number of results before and after reranking is the
reader's to set, defaulting to the documents' own numbers.

**What was wrong, live.** "la caccia alla balena bianca" was never
translated into English: the fan-out excluded `en`, capped at the six
largest groups, and skipped any collection holding one group. So it ran
untranslated against gutenberg's English index and all of PubMed, which
answered "Libro della caccia" and "Balena's method". The page showed and
translated only the first 420 characters of an entry.

**What changed.**
- `app/language_plan.py`: one plan per search, shared by every
  collection. The query goes into every language the text indexes hold
  (cjk as zh/ja/ko, ranked by each record's best variant; `other` as
  typed). `TRANSLATE_MAX_LANGS` is gone. The plan is cached, so the
  answer's search makes no second model call.
- A restriction comes from `lang:de` (no model call) or from
  `llm.language_restriction`, and is a prefilter on every leg and the
  catalog. The page shows `only: de`, and shows when the model could not
  be asked.
- Entries are on the page whole (clamped, "more" opens them). Titles,
  headings, entries, catalog titles and the answer are translated through
  `POST /ask/translate/stream` (NDJSON), in this order: cached items,
  short items batched, then each long one streamed, the answer first. A
  new switch or search aborts the stream. The translation cache is keyed
  on (lang, text); keyed on (id, lang) it served a second answer the
  first answer's translation.
- `results` (after the reranker, default 10) and `reranked from` (its
  depth, default 50), per request, 1–100. Legs go at least as deep, and
  the federation budget no longer cuts below the depth: 40 under 50 made
  "50" a claim about 40. The answer gets at most 24,000 characters of
  passages, because Ollama drops the START of a prompt that overflows.

**Measured on the node (qwen3:8b, `OLLAMA_NUM_PARALLEL=1`).**
- The restriction, asked for codes alone, got "Russian novels about war",
  "lingua latina grammatica" and a plain Italian query wrong. The model now
  quotes the instruction, and one that is absent from the query, or is the
  whole query, is refused. Live, six phrasings were all right, at about
  1 s each.
- Query translation into 19 languages: 9.2 s idle, 17–23 s with the
  encoders busy, against 2.7 s / 7.6 s for the old six. 59 languages
  (`other`'s tail too) would be 34.3 s idle, which is why `other` is
  searched as typed. **A search pays about +7 s idle for "every
  language"**; the Ask flow's second search now pays nothing.
- One whole 3.8 kB entry: first words after 1.6–2.0 s, done after
  58–84 s. A page of twelve takes minutes of model time whatever the
  order. Hence the streaming, and the answer before the entries.

**Deployed 2026-09-18 ~15:20 UTC (732e22d, image `b1674ab…`).** The node
suite passed on the staged tree (2,841). Web and worker run one image.
The node's `OLLAMA_URL` is now `CHAT_URL`, with the value kept. Live:
- The morning's query now went out in 19 languages, English included.
  Total 37.6 s with the encoders busy, of which the language plan was
  28.6 s (the morning: 27.7 s in total).
- "…, solo testi in tedesco" returned `only: de` and ten German hits, in
  13.3 s.

**The `[sched]` lines are the node's.** "Why is gutenberg logging about
the other plugins?" The fair-share lines cover every collection. The
stats thread writes them through `collections[0]`'s manifest, and the node
has no event log of its own. So they were printed and listed as
gutenberg's, the first collection since genome left. `db.NODE_STAGES` now
labels them `node` on stdout and in the System log. Where they are
stored is unchanged; `tools/sched_report.py` already read them that way.

### A translation model; main languages by default; no default encoder (2026-09-18, evening)

**The translation model.** MADLAD-400-3B-MT runs on the Arc in its own
container (`deploy/madlad-translate`, TrueNAS app `translate-gpu-madlad`,
:30448), and `TRANSLATE_URL` points the app at it (`app/translate.py`). It
does every translation: the query fan-out, and the Ask page's titles,
headings, whole entries and the answer. The chat model is the fallback.
Measured on the node:

| | MADLAD on the Arc | qwen3:8b (chat) |
|---|---|---|
| question into the 19 main languages | 0.76–1.75 s | 56–192 s, GPU busy |
| question into 51 languages | 1.3–1.5 s | 34.3 s idle (59) |
| one whole 3.8 kB entry | 5.9 s | 58–84 s |
| twelve entries in one call | 33.6 s | minutes |

- VRAM at batch 32: 8.0 GB at peak and 8.9 GB idle; about 14 GB is free
  with every other model loaded. Batch 256 reached 26 GB, and was stopped.
- **RAM: 2.96 GiB, from 7.1 GB** ("why the translator taked 12.26GB of
  RAM???"). The weights were loaded into host memory, then moved to the
  GPU, and glibc kept the 4.6 GB it freed. The 12.26 GB also held the file
  cache of the 11.8 GB float32 file.
  - The weights now load straight onto the device (`device_map`), and the
    host heap is trimmed after warm-up.
  - A bfloat16 copy (5.5 GB, `deploy/madlad-translate/to_bf16.py`) is what
    runs. Its translations are identical.
  - **TrueNAS then showed 5.6 GB** ("still using more than 5GB RAM"). It
    counts the cgroup's `memory_stats.usage`, page cache included, and
    2.5 GB of it was the bf16 file, read through mmap at load. The server
    now evicts the model's files after warm-up (`POSIX_FADV_DONTNEED`,
    which works on ZFS: 2,048 MB → 0 in a test): 3.03 GiB, 0.11 GB of
    it file.
  - What remains is the GPU runtime's, not the model's: 1.9 GB of torch's
    compiled GPU kernels (Level Zero binaries, their `zeInfo` metadata
    read in the process's memory) and 0.8 GB of heap. Quantized weights
    would not lower it, because the weights are on the card; only another
    runtime would.
- Its quality is good in the main languages. In the tail it is weak: Welsh
  gave "the red dragon", Latin nonsense. The chat model made the white
  whale a white shark.
- **The GGUF routes failed.** Ollama loads the model and then hangs.
  llama-server runs no encoder (`<2cs>…`). llama.cpp's completion tool is
  correct, but it has no server and no batching.
- **transformers must be 4.46.3.** Version 5.17 leaves the decoder's
  embedding untied, and every output came back `ě ě ě …`.

**Main languages by default** (operator). A search goes out in the
languages that have an index of their own, plus CJK's three. "Every
language held" happens only on request: the Ask page's *all languages*,
`all_languages` on the API, or `lang:all` in the question.

**No default encoder** (operator: "A default embedder is wrong, remove
EMBED_URL. A collection that does not specify an embedder is a non
compliant collection").
- EMBED_URL, EMBED_MODEL, EMBED_API, EMBED_DTYPE, EMBED_CTX and
  `[encoder_defaults]` are gone. `platform.toml` still parses the table, and
  the System page says it is IGNORED.
- A file that names no `[encoder]` is refused at load. The System page now
  shows refusals, which it never did.
- The boot refuses such a collection instead of dying, as the worker did.
- `hold collection create` needs `--encoder URL` and writes it into the
  file. `embed materialize` has nothing left to write.
- The live collections all name their own encoder, so nothing live changed.
- Tests: tests/conftest.py gives a test collection that names no encoder
  the TEST encoder, explicitly. The rule's own tests are marked
  `strict_encoder` (tests/test_no_default_encoder.py).

**The two-node harness passes again, twice in a row.** It had failed since
the code moved under it, and the product was never the cause. Three harness
defects, one behind the other:
1. The node's mode was set in the manifest, which the worker stopped reading
   on 2026-09-13, so the worker stayed paused. It is now set in
   `node_state`.
2. The stage functions were called bare. `stage_fts` opens `BEGIN
   IMMEDIATE` and leaves the commit to `_execute_step`, so the consumer
   held the manifest's write lock for good (byte 120 of `-shm`, in its own
   PID). Its worker then waited 60 s on every write and never imported
   anything. Both nodes now run each stage through `_execute_step`.
3. `embed_mode=off` was set in the manifest. That is Tier 3, which the
   worker resets to the file at its start (invariant 10). The consumer
   embedded the documents itself, and its hits were rightly `local`.
   `config-consumer/` now sets it in the TOML.

**The reader is translated as it is read** (operator, 2026-09-18). Opened
from Ask with a language selected, it translates in this order:
- what is on screen, with the panel's title;
- then as many characters again below and above the view;
- then, while scrolling, a screenful ahead in the direction of the scroll.

Only one request is in flight at a time. If a request busy with text off
screen is running when untranslated text comes into view, it is aborted
and the visible text is requested instead. Text above the view that
changes length would move the passage being read, so the page holds the
view on its first visible unit, and the browser's own anchoring is off
for the panel.

Checked in headless Chromium, on the real page and a real 150-paragraph
fragment, with the endpoint faked and every request recorded:
- the first request was exactly the title plus the visible units;
- the second was the window below and then above, and did not move the view;
- scrolling down sent only text below, and scrolling up only text above;
- a jump was asked for within 130 ms, and the busy request's text was
  never painted;
- changing the language started over from the screen, and "original"
  restored every unit byte for byte.

Over a long read, 40 of the 150 paragraphs were sent.

That check is now part of the suite (operator, 2026-09-18: "do it").
- `tests/test_reader_translation_browser.py` renders the pages through the
  app, then runs `tests/browser/reader_translation.js` in headless
  Chromium. It takes 29 s. Breaking "below and above at first" in the
  page made it fail, as it should.
- The Dockerfile's `dev` stage carries the browser: Debian's
  `chromium-headless-shell` (101 packages rather than 168 for full
  Chromium) and `nodejs`. puppeteer-core 23.11.1 is installed in a
  `node:20-slim` stage, because Debian's npm alone is 363 more packages.
  This adds about 0.7 GB to the dev image.
- The runtime, the default and last stage, is unchanged, and BuildKit
  (Docker 29 on the node) never builds the dev stages for it.
- **Build the test image with `docker build --target dev -t
  louise-rag:dev .`** An image without a browser skips this one test and
  says why. That includes the runtime image the node's staging suite
  runs in.

### The GPU panel on the System page (2026-09-18, night)

The operator asked for the GPU's model, its VRAM and what is used and
free. They also wanted "the memory used by each model" if it was not too
hard. It was not. The System page now shows each card's name, a bar, and
total, used and free. Under that is one row per process holding memory:
the model it serves, its container, its VRAM, its share of the card, and
its GPU buffers in host RAM.

**Where the figures come from.** Measured on the node (kernel 6.18, `xe`):
- The card's used memory comes from the driver's own query
  (`DRM_IOCTL_XE_DEVICE_QUERY`). It equals the debugfs `vram0_mm` usage to
  the byte: 25,746,759,680. The driver reports it only to `CAP_PERFMON`.
  Without that capability the panel uses the processes' sum and says so.
- Each process's figures come from the kernel's DRM fdinfo, summed per
  client. The eight model servers add up to the driver's count less
  24.6 MB. The panel shows that remainder as its own row.
- The model name comes from the command line. For llama-server that is
  its `-m`; for a Python server, its directory. Ollama's runner names only
  a blob, which its manifest resolves (`qwen3:8b (ollama)`).
- A snapshot takes 98 ms, 81 ms of it scanning `/proc`. The page asks
  every 5 s, and only while the System tab is open.
- The card's name comes from pci.ids, pinned by commit and checksum in the
  Dockerfile. The node's own copy calls the Arc Pro B70 "Battlemage G21".

**Why a third container.** A per-process figure needs the host's PID
namespace. Any container that has it can also read every other
container's files and environment through `/proc`. The LAN-facing web and
the worker, which parses untrusted origin data, must not have that.
- `app/gpu_probe.py` runs from the same image as the `gpu-probe` service
  in `deploy/truenas-app.yaml`: `pid: host`, `/dev/dri`, `PERFMON`, and no
  volumes, environment or port.
- The web asks it at `GPU_PROBE_URL` with a 1.5 s bound (`app/gpu.py`).
- A test fails if any other module imports the probe
  (`tests/test_gpu_panel.py`).
- Docker's default capabilities were enough to read the other
  containers' fdinfo; `SYS_PTRACE` was not needed.

**Deployed 2026-09-18 21:30 UTC** (image `f144fb38`; the one before is
tagged `louise-rag:pre-gpu-probe`). Web, worker and probe run the same
image, and the panel loaded both cards through the web. The live compose
gained the `gpu-probe` service and web's `GPU_PROBE_URL`; the config it
replaced is `/root/louise-rag-compose-before-gpu-probe.json` on the node.

**Seen on the first reading.** Pubmed's MedCPT *Article* encoder has
568 KiB in VRAM and 418 MiB of GPU buffers in host RAM. Its twin, the
Query encoder, has 269 MiB in VRAM. The two run the same build with the
same flags and started in the same second, so the Article encoder's
weights were moved out after loading. That was most likely MADLAD's
batch-256 test reaching 26 GB.
- It cost nothing measurable. On a 1.3 kB abstract, 20 requests each, the
  evicted Article encoder took a median 6.0 ms and the Query encoder
  7.9 ms, at the same moment. The model is small (110M parameters).
- Restarted anyway (operator, 21:50 UTC). The weights came back:
  294 MiB in VRAM, 44 MiB in host RAM. The vector for the same input is
  bit-identical before and after.
- The latency after the restart (both encoders around 62 ms) measured the
  shared GPU's other work, not the placement.
- Its 2,300 "input too large" 500s since 10:40 UTC are b1e56ab's protocol
  (the client shortens its cut on refusal), not a fault.

The container column shows Docker's 12-character id. Setting `hostname:`
in a model app's YAML makes it show that name instead.

### The catalog leg: an index, not a scan (2026-09-19)

**What was wrong.** A search's catalog leg (titles, authors and subjects)
was an SQLite FTS5 query ranked by `bm25`. FTS5 scores every match before
keeping the top k, and the query joined every match to `documents`
before its LIMIT. On pubmed's 40M records, measured on the node:

| query | matches | leg |
|---|---|---|
| aspirin platelet aggregation | 15,078 | 4.0 s (1.45 s scoring, 2.6 s the join) |
| cancer treatment | 183,608 | 9.1 s |
| cancer | 1,476,347 | 57.3 s |

A hybrid search spent 18.9 of its 21.6 s here. That is a per-search cost
that grew with the corpus (CLAUDE.md), and the architecture had already
given the catalog to tantivy (architecture-v2.md, "catalog FTS").

**What changed (operator: option A).** `app/catalog.py` keeps the catalog
in tantivy at `index/_catalog`, one index per collection.
- **Fields and matching.** It holds title, authors and subjects (analysed
  as FTS5's `unicode61` did: split, lowercase, accents folded, no
  stemming), plus lang for the restriction and the document's rowid. Every
  word must match, as in FTS5.
- **Search.** `catalog_search` takes the top k and reads only those k rows
  from the manifest.
- **Measured before building it:** 1M and 4M pubmed records, 66,000
  records/s, 96 bytes a record, and a top 10 in 1.2–2.9 ms at 4M.
- **Writers.**
  - The rdf stage commits each batch's entries before the marks, through
    one long-lived writer per collection, so merges never happen inside
    the manifest transaction. A failure raises `CatalogUnavailable`, which
    is a deferral.
  - The index maintainer runs the BACKFILL in rowid windows of 200,000,
    60 s per turn, reading each window under the writer lock. Its state
    and a generation live in `index/_catalog/louise-catalog.json`.
- **Fallback.** Until a collection's backfill is complete, the SQLite
  catalog answers, now joined after its LIMIT when no language restriction
  needs the join. The SQLite catalog is still written.
- **Retiring the SQLite catalog is a separate step.** Its 16.8 GiB (pubmed
  15.7, wikipedia 1.06) is only freed inside the manifests, which are
  never vacuumed (invariant 6). Dropping it takes the manifest's write
  lock for as long as the drop lasts.

**Deployed 2026-09-18 22:58 UTC (d615cf4); live since the backfills
completed.**
- The backfills took about 15 minutes for wikipedia and about 1 hour for
  pubmed, in 60 s turns.
- The indexes: gutenberg 6.5 MB, wikipedia 291 MB (60.1M rowids, only the
  extracted rows are written), pubmed 3.8 GB (40.0M, estimated 3.6 GiB).
- rdf batches were unaffected: 20,000 documents in 1.3 s, catalog commit
  included. No catalog deferral or error was logged.
- Measured through `catalog_search`, best of 3, on 2026-09-19:

| pubmed query | before | after |
|---|---|---|
| aspirin platelet aggregation | 4.0 s | 17.6 ms |
| cancer treatment | 9.1 s | 55.4 ms |
| cancer | 57.3 s | 33.1 ms |

- The hybrid search for "aspirin platelet aggregation" went from 21.6 s to
  4.3 s. The catalog is 18 ms of that; the rest is fts (1.2 s), dense
  (2.4 s) and the reranker (0.5 s).
- It survived the worker's restructuring the same night, with one fix by
  that work: 1f25e6b, `close_all` waits for a commit in flight.

**The SQLite catalog is retired (operator: "do it", 2026-09-19).**
- Nothing writes or reads `catalog_fts` any more. The rdf stage writes
  only the index, and `catalog_search` has no SQLite fallback: an index
  that is not complete yet gives an empty catalog leg.
- New manifests are created without the table. An old manifest's is
  dropped once at the worker's startup (`db.drop_catalog_fts`, replacing
  the rowid realignment it no longer needs).
- `tools/clean_titles.py` now writes the index. The index has one writer,
  the worker's, so the tool asks for the worker to be stopped and changes
  nothing while it runs. `tools/migrate_manifest.py` drops a pre-refactor
  catalog instead of converting it.
- **Measured on a ZFS clone of pubmed's manifest first.**
  - This SQLite is compiled with `SECURE_DELETE`, so a plain `DROP TABLE`
    zeroes every freed page through the WAL. That was a 14.6 GB WAL, still
    growing after 12 minutes. It was stopped.
  - With `PRAGMA secure_delete = OFF` for the drop alone, it took 335 s,
    committed in 2.7 s, with a 16 MiB WAL, and freed 15.7 GiB to the
    manifest's free list. The catalog was public metadata that `documents`
    also holds.
  - So the first boot after the deploy starts the pipeline about six
    minutes later, once. Reads, and so search, go on meanwhile.
- **Deployed 09:28 UTC (1f2f219).** The first boot dropped gutenberg's
  catalog in 0 s, pubmed's in 102 s (the live pages were warmer than the
  clone's) and wikipedia's in 6 s. The pipeline threads started at
  09:29:51, with no restart.
  - Free inside the manifests afterwards: pubmed 15.7 GiB, wikipedia
    1.3 GiB.
  - pubmed's catalog leg, warm: 16–53 ms, as before. The first query of
    each kind after the restart read the index cold (85 ms to 2.9 s).
- A clone on ssd4 is swept into the hourly recursive snapshot. The first
  test clone was caught at 11:00 and its full send to tank (1.1 TiB) was
  aborted at 196 GiB. The partial copy, the clone and its snapshot were
  destroyed, and replication 5 re-ran clean. The second test ran between
  two snapshots.

### Model work by priority; the encoders fed (2026-09-19/20)

**What was wrong, measured live.** gutenberg's encoder (Qwen3-4B, on the
Arc) was busy 20.7% of a 5-minute trace, idle in 233 of 300 seconds, with
78,178 documents waiting. While a document ran it was at 95%: the card
was not slow, it was not fed.
- The main loop fed every encoder endpoint in turn.
- pubmed's turns cost 250 s in ten minutes for zero documents. Its
  backfill walk took 105–153 s per call over 40M rows, because the
  cursor's age was stamped before a walk longer than its TTL.
- py-spy showed the loop in that walk, and in an archive drain inside an
  encoder turn.

**What changed** (operator's design; scheduling.md §12):
- **The walk is bounded** (f5c4f29): rowid windows from a cursor, at
  most 1 s per call. On live pubmed: worst call 1.46 s; the whole sweep
  is 158 s spread over 141 calls.
- **Levels:** corpus < translation < chat < rerank < query.
- **One gate per model server** (`app/dispatch.py`). It admits the
  server's capacity, by level, fair by service time.
- **Corpus stands aside on the GPU per chunk** while anything
  interactive runs, through a claim in `state/node.sqlite`.
- **One embed producer per collection**, with two documents submitted
  and a third prepared (bd6baa8).
- **Nice.** Feeders at 0; pipeline, acquisition and discovery at 5;
  parse pool and index maintainer at 10. The levels are targets over the
  worker's base (00a9fa5); the first deploy's relative steps put
  acquisition at 15.

**After, live (1f25e6b, image d4975f1a):**
- gutenberg's encoder over 5 minutes: mean 82.3%, median 87.9%, never at
  0%, 18 of 298 seconds under 50%.
- About 2.5 documents a minute embedded, against 0.8 before.
- Query embedding and rerank were never the problem: 3–10 ms and 19 ms
  against a 95%-busy card.

**Two instrument lessons.** Measure a model server's GPU share per DRM
client, each client's cycles over its own `drm-total-cycles` (a server
can hold two; borrowing an idle client's time base printed "2225%"). And
drop impossible samples (a counter reset read −821,239%) rather than let
them average in.

**Found on the way.** `catalog.close_all` closed tantivy writers without
their write locks, and the node's suite hit "Already borrowed" at
teardown (test-only; fixed in 1f25e6b, ten repeats clean).

**Then the ladder was measured, and one piece reversed (2026-09-20).**
- **Corpus standing aside from the whole card slowed chat.** Alone on
  the card, the Arc drops its clock: 87–92 s at a mean of 990 MHz,
  against 65–72 s beside corpus work. With the clock held, standing aside
  bought chat only 3 s. It was removed in b4eba5f, and levels now order
  only at a shared gate.
- **The clock is what chat needs.** The **GPU governor** holds `min_freq`
  at 2,800 MHz while the interactive claim is live. Live check: 400 before
  a claim, 2,800 during, 400 after.
- **It runs as a TrueNAS app of its own, `louise-gpu-governor`.** The
  first deploy, as a service inside louise-rag, took Louise down for a few
  minutes: TrueNAS rewrites binds into short syntax, the card's PCI path
  has colons, and one failed service fails the whole app's `up` (013507c).
- **The System tab's GPU panel** shows it running, unable to set the
  clock, or not running. Not running costs only speed.
- **The fan is watched, not controlled (a89b40d, live 2026-09-19).** The
  xe driver's fan stays high after a workload until a GPU process
  restarts, and xe exposes it read-only. The panel shows the live fan,
  temperatures, power and clock (gt0's `act_freq` of `rp0_freq`), and no
  history. The governor samples them every 10 s into `gpu_samples`, kept
  7 days for a widget to come (operator, 2026-09-19). A week at 10 s is
  3.6 MB and stays flat, because pruning on write reuses the freed pages.
  Any workaround belongs to RAGE only; other apps are never restarted for
  it (operator). First samples: 1,873–1,934 RPM, pkg 72–78 °C, vram
  74–80 °C, 192–204 W.

### RAGE: reviewed, redesigned, the front built — nothing on the node yet (2026-09-19)

**What happened, in order.**
1. The first proposal for RAGE (b0f5dad) was reviewed before any code
   ([`rage-review.md`](rage-review.md)) and did not survive as written:
   llama-swap has no load endpoint and evicts by default; the run
   identity could not be computed as specified (a process cannot see its
   image's digest; the raw argv and environment would have made every
   restart another run and re-gated every space); a locally built image
   would have frozen an unobtainable engine into every new Codex; nobody
   admitted a load. The operator accepted every recommendation.
2. **Quarantine** came out of the same review (operator's idea): a copy
   is not used before this peer's own five tests are done; an origin that
   cannot be checked waits for the operator's recorded decision.
3. The amendments are on `main`: **8bbc66a** (the run identity made
   computable, a weights digest for directories, `Louise-Level`, the
   model's URL, `RAGE_URL`) and **98ee4dc** (quarantine).
4. [`rage.md`](rage.md) rewritten to the accepted design;
   `migration-plan.md` (deleted 2026-10-08) gained **step 13** (RAGE,
   parts a–f) and **step 14** (quarantine). Step 12 stays last.
5. The code as it stood before RAGE is tagged **v0.1.0** (`louise`
   01a4d00; the first tag of the repository).
6. **Steps 13b and 13c are built**: `app/rage/`, the launcher of our
   three servers, and 13d's files (image, example config, TrueNAS app).
   [`rage.md`](rage.md) §17 lists each piece and what proves it.

**Numbers.** `tests/rage/`: 96 tests in 7 s, against a directory that
stands in for `/proc` and a fake llama-swap. The whole suite: 3,050
passed, none failed, 7 min 34 s. No number from the node exists yet: no
image has been built.

**What cost time, so it does not again.**
- *The design said "no groups" and would have swapped every model out
  for the next.* llama-swap's default for a model in no group is one
  model at a time across the instance; its own example config says so.
  Policy-free is ONE group with `swap: false`, `exclusive: false`.
  Found by reading the example before writing the generator, not after.
- *Three pre-existing guards said no, and each was right.* Nothing
  imports the GPU probe (so a card's name and capacity are declared in
  `rage.toml`, which is also the only way to get the driver's own words
  09 §9.3 asks for). No GPU stack under `app/`. Every image of ours
  answers SIGTERM as PID 1 — the front's boot had no handler before
  uvicorn is up; it has one now, and that guard asks for a handler, not
  a mention of the signal (`os.kill(pid, SIGTERM)` used to satisfy it).
- *A burned key is not a denied key.* The review first proposed renaming
  a key's "quarantine" to "denied", misreading 07's table. The specs'
  own word is *burned*; the trust file's field and the code's 86 uses of
  the word for keys keep their names.
- *`held` already means possession*, so the third state is `in use`.

**Measured on the node the same day, and what it changed.** Each
per-model container's cgroup `memory.peak`: the eight GPU servers and
the governor hold **6.1 GiB of host RAM at rest and 22.0 GiB at their
load-time peaks** (MADLAD 8.5, the chat model 5.6, Qwen3-4B 3.0). A model
on the GPU is not free on the host, and the first `admission.py` counted
host RAM for CPU placements only: it would have let CPU models fill what
the GPU models already held. Every load is now checked against the
host's budget too (`host.<card>` per entry, the peak). The app file's
first `mem_limit` was 40g, which with Louise's worker at its 20.6 GiB
peak is more than the machine's 61.9; it is **28g**, leaving ZFS at
least 9 GiB of cache in the worst case. Also found: gutenberg's encoder
runs llama.cpp b10603 and the others b10630, so under RAGE's one build
gutenberg's space meets another engine; and Louise's admin token is an
environment VALUE today (`ADMIN_TOKEN_FILE` unset).

**The operator's values (2026-09-19):** folder
`/mnt/ssd5/apps/louise-rage`; port 30437; the chat model is the file
Ollama serves as `qwen3:8b`, whose blob name is its weights digest and
which carries its own chat template; the per-model apps are shut off
before RAGE loads, which makes 13f a cut-over and not a gradual switch
([`rage.md`](rage.md) §16). Still to give: the registry.

**The same day, after the front:**
- **A live bug fixed (8bec7fc):** every streamed answer on the Ask page
  ended with `[error: <Token … was created in a different Context]`.
  `llm.chat_stream` holds `dispatch.call` across its `yield`s, and
  Starlette steps a sync generator in its threadpool with a FRESH COPY
  of the request's context per step: the level was set in the first
  copy and `reset` in the last, which `contextvars` refuses. The gate
  and the claim were released correctly; only the message was wrong.
  NOT DEPLOYED — it needs the web container rebuilt.
- **`tools/endpoint_vectors.py` (3275ccb):** records what each encoder
  answers today — the sixteen fingerprint texts and a spread of the
  calibration set, one text a request, unnormalised — and compares
  RAGE's answers with it afterwards. The cut-over needs it FIRST: once
  the per-model apps are off there is nothing left to compare with.
  `app/hold/fingerprint.py` is 09 §9.12's rule. A test caught
  `Path.with_suffix` replacing ".document": a bi-encoder's query side
  would have overwritten its document side's recording.
- **13e, first part (3dd4194):** every model request carries
  `Louise-Level`; `CHAT_API = ollama | openai` so the chat model can
  leave Ollama for RAGE by switching one variable, and come back;
  `RAGE_URL`. Nothing changes on today's node: the header is ignored by
  today's servers and the protocol defaults to Ollama's.
- **13a, Codex v3:** the engine in the Codex and out of the space, and
  every artefact that named an image moved one version while still
  reading the old one (`migration-plan.md` (deleted 2026-10-08) 13a has
  the list). Every collection on the node is version 2 or legacy, so
  nothing live changes: their Codices load as before, their `space_id`s
  do not move, and what is written for them keeps its old shape. New
  collections — the v1.0 roots — are version 3 and name an engine; the
  two gutenberg recipe notes cannot create one and say so.

- **13e, second part — a vector is written only from an answer that names
  a validated run** (03 §3.3). `app/hold/runs.py` keeps one signed
  statement per run and role under `state/calibration/`, pass or fail,
  and refuses a write whose answers name any other; `app/run_check.py`
  is where the node's embedding client meets it. The worker CHECKS at
  the write (`embeddings._rows_for`, the one place its rows are made, a
  dictionary lookup) and never gates there: an answer from an untested
  run, or from an endpoint that states none, defers the document
  untouched — never an error on it. The gate runs at once from
  `backend_gate.embed_allowed`, once per (space, endpoint): a pass
  resumes the work, a failure or a run-less endpoint PAUSES the
  collection's embedding with its reason. An asymmetric space's query
  encoder is gated the same way, by itself. `hold vectors build` checks
  every batch before its write and gates a new run inline; `hold
  embedder check` validates the run that answers and needs no
  `--accept`. For a version-3 collection this REPLACES the behavioural
  baseline, and `BACKEND_GATE=off` does not switch it off. Version 2
  and legacy collections — every collection on the node — are untouched.
  The gate's second pass contributes the way the peer really does,
  `recipe.concurrency` requests at once included: that is the setting
  the 714-book fault lived in. Found by the suite: a missing calibration
  member was reported as an unreachable encoder (exit 3) because the
  network was asked first; it is a refusal (exit 2) again.
- **Step 14 — quarantine, REPORTING.** `app/copy_state.py` derives a
  copy's state — refused, quarantine, in use — from what the peer holds
  and writes nothing: its own attestations (another key's are not its
  tests), its run statements, the licence resolution, the origin
  acknowledgement. One function for the worker, the search, the export
  door, `louise health`, the Library card and `hold collection show`
  (whose JSON gained `state`, `reasons`, `origin_check` and lost
  `refused`, 09 §9.3). `hold collection ack --origin-unchecked` is
  refused with a test open, with the origin never tried, and with the
  check run; accepted with the origin away, recording time and key. A
  run not yet tested re-quarantines every collection that reaches that
  encoder and the gate clears it at once, with or without embed work; a
  FAILED run withholds the vector leg only, and the search says so
  (`legs_withheld`). **Nothing is withheld yet**: `platform.toml`'s
  `[node] quarantine` defaults to `"report"`, and the worker log, the
  health check and the Library say what enforcing WOULD withhold.
  `"enforce"` is the operator's word, after `hold collection show` reads
  `in-use` for every collection meant to be.

**DEPLOYED 2026-09-19 (node time 21:45), louise 026985b; pushed, with
`main` 98ee4dc and the tag v0.1.0.** The live tree had been exactly
v0.1.0. Staged, built, the suite run on the node (3,154 passed), then the
staged code run READ-ONLY against the node's real configuration beside
the live code: byte-identical `collection_id`, `space_id` and encoder for
gutenberg, pubmed and wikipedia (all Codex v2), none refused. The way
back is kept: `src.old/` and the image tag `louise-rag:v0.1.0-live`.

- **The streamed-answer fault is gone on the node**: a streamed `/v1/chat/
  completions` over gutenberg ran to `[DONE]` — 94 chunks, no error chunk.
- **Quarantine reports, withholds nothing** (`quarantine = report`). One
  evaluation costs 0.13–0.25 ms warm on the node's own state directories.
- **The gate passed for all three encoders**, statements kept
  (`state/calibration.json`): pubmed 16/16, whole set worst 0.9896,
  34,784 tokens/s; wikipedia 16/16, worst 0.9990, 14,662 tokens/s;
  gutenberg 16/16, worst 0.9998, 2,208 tokens/s.
- **Today's encoder answers are recorded** for the RAGE cut-over, five
  sides, 79 texts each: `/data/state/endpoint-vectors-2026-09-19`.
- **What the deploy found, each fixed before going on.** (1) The node's
  test run: version-3 `embedder check` looked for the encoder before the
  calibration member, so a drag without its member read "deferred" on a
  node naming no encoder; the test had passed on a checkout whose own
  collection file names one (c4fa5e6). (2) Reading the first output: `hold
  collection show wikipedia` said the licence was not acknowledged while
  the worker said it was — the command judged from the Codex's class
  where the worker asks `licensing.state_for` (026985b). (3) The promote
  loop does not run under the node's zsh, and a read-only mount fails
  thirteen tests: both now in DEPLOYMENT.md.
- **QUARANTINE CANNOT BE ENFORCED YET, and the reporting deploy is what
  showed it.** Only pubmed has record shards and snapshots in its drag.
  gutenberg and wikipedia hold a Codex and a calibration member and
  nothing else, so `roots`, `chunks` and `records-vs-origin` have nothing
  to run on: they need `hold records build` and `snapshot create` first
  (migration step 8's work, hours each). And `records-vs-origin` as built
  does not fetch: it takes `--origin DIR`. Pointed at the copy this peer
  kept it proves the parser is deterministic, not that the origin still
  serves those bytes — the weaker form its own docstring names — so it was
  NOT run and nothing was attested: pubmed waits on a bounded,
  budget-drawing fetch of the plan's sample, which does not exist yet.
  That fetch and the scheduled run are one piece of work.

**RAGE'S IMAGE IS BUILT ON THE NODE AND PUBLISHED (2026-09-19, 13d's
first half).** `ghcr.io/sourstar/rage@sha256:8ee29dfab3d7f8720cb48cb6b5367c1b17679475472cd615a69a77ce48e40489`
(tag `b10630-1`, 18.2 GB; the base's layers were mounted from
ggml-org/llama.cpp, not uploaded). The login is a Docker config kept by
path, `/mnt/ssd5/apps/louise-rage/secrets/docker` — root's own config
was not touched. **The package is PUBLIC** (operator, 2026-09-19; GitHub
offers no API for it): an anonymous pull by digest answers 200, the
manifest's bytes hash to the digest, and a layer downloads — 32 blobs,
6.32 GB compressed. So the image is "obtainable" (02 §2.4) and a Codex
may freeze it.
- **Settled on the node, with the card, no model loaded:** llama-server
  starts from `/srv` and lists `SYCL0: Intel(R) Arc(TM) Pro B70 Graphics
  (32656 MiB)`; torch 2.14.0+xpu, under the environment a torch model
  declares, reads `xpu available: True` and names the card; OpenVINO lists
  `['CPU', 'GPU']`. These were the unknowns the trial build left.
- **RAGE's folder is ready for the cut-over**: `config/rage.toml` (eight
  models, five preloaded; every weights path checked present on the
  node), `config/truenas-rage.yaml` with the digest filled in,
  `secrets/admin.token` generated there and never shown, `state/` empty.
- **DNA is frozen and inactive** (operator, 2026-09-19: "not ferried to
  the new architecture … until further development"). DNABERT-2 is no
  longer served: commented out of `rage.example.toml`, its bind out of
  the app file, a test holds both. Its weights on the node sit in a
  Hugging Face cache layout whose symbolic links the weights digest
  refuses; placing them as a real directory is part of bringing it back.
- A file first offered as the registry token was Louise's own admin token
  (found by comparing hashes on the node, printing neither). It was not
  sent anywhere; both token files are now mode 600.
- **Left:** 13d's proof with real models and 13f are ONE evening — the
  per-model apps go off before RAGE loads. Today's vectors are recorded.

**THE CUT-OVER IS DONE (2026-09-19 night; 13d's proof and 13f's first
half, one evening as planned).** The operator paused the node, stopped
the five apps (`embed-gpu-qwen3-4b`, `embed-gpu-bge-m3`,
`embed-gpu-medcpt`, `translate-gpu-madlad`, `louise-gpu-governor`) and
installed `louise-rage` from the app file, in that order; the rest was
driven from here. RAGE serves every model Louise uses except chat, on
port 30437; chat followed later the same night (below).

| model | device | ready in | host RSS | card |
|---|---|---|---|---|
| qwen3-embedding-4b | xpu | 35.4 s | 1,011 MiB | 8,628 MiB |
| madlad (torch) | xpu | 20.3 s | 3,152 MiB | 6,209 MiB |
| bge-m3 | xpu | 14.4 s | 1,112 MiB | 1,017 MiB |
| bge-reranker | xpu | 8.3 s | 1,143 MiB | 1,019 MiB |
| medcpt-article | xpu | 6.3 s, on demand | 549 MiB | 292 MiB |
| medcpt-query | xpu | 6.3 s | 543 MiB | 292 MiB |
| medcpt-rerank (OpenVINO) | xpu | 4.3 s | 507 MiB | 288 MiB |

Seven models at once: 8.0 GiB of host RSS by process, 5.6 GiB by the
container's cgroup against its 28 GiB limit, 17.7 GiB of the card's
32.6. MADLAD's 20 s is against the 300 s its own app took.
- **RAGE's vectors against the recording of the old endpoints**, 79
  texts a side, worst cosine: gutenberg 0.999999, pubmed documents
  1.000000, pubmed queries 0.999981, wikipedia 0.999998; median 1.000000
  everywhere. No fingerprint is bit-equal — expected for gutenberg
  (b10603 → b10630), and for the others the engine's settings differ —
  so the cosines judge, and the gate tests.
- **The gate through RAGE passed for all three**, the same whole-set
  figures as through the old apps (worst 0.9896 / 0.9990 / 0.9998).
  Throughput read 142,707 / 72,900 / 3,373 tokens/s against 34,784 /
  14,662 / 2,208 — but the node was PAUSED for these and running for
  those, so this is not yet a comparison of RAGE with the apps.
- **The query path, live**: a search on each collection reranked by its
  own reranker; an Italian question over gutenberg translated by MADLAD
  into every held language; a streamed answer over pubmed ran to
  `[DONE]`, 74 chunks, no error chunk.
- **The worker resumed and works through RAGE**, no error in its log: in
  its first two minutes RAGE served 428 requests to qwen3, 125 to bge-m3
  and 4 to medcpt-article. The GPU clock is RAGE's now (`/louise/gpu`:
  running, rest 400 MHz, max 2,800, not pinned).
- **What was switched**: the `[encoder]` and `[reranker]` URLs of the
  three collection files, to `…:30437/upstream/<model>`; `TRANSLATE_URL`
  and a new `RAGE_URL` on web and worker, through the TrueNAS client
  library so the app's configuration — which carries the admin token —
  never reached a command line. **The way back**:
  `config/backup-pre-rage-2026-09-19/` holds the three files and the
  app's configuration (mode 600); the five apps are stopped, not deleted.
- **Differs from the app file's own note**: RAGE's `secrets/admin.token`
  is a token generated on the node, not Louise's. RAGE reads it once at
  start, and nothing in Louise calls RAGE's operator routes yet, so it
  was left; making them the same is a file copy and a restart of RAGE.
- **Not done**: the retirements, which are
  [`legacy-to-delete.md`](legacy-to-delete.md) §7, by its rule.

**THE NODE'S FOUR MODES, REDEFINED (operator, 2026-09-19; louise 8db5af5,
spec 83fe4b1 on main).** A mode is a statement of what the node does:
*pause* — nothing, no fetching, parsing, seeding or indexing; *seeding
only*; *download/seeding only* — download and seeding only; *run
everything*. Embedding is no mode's business: it runs only where
everything does, and there each collection's own drop-down sets it. One
table says it (`app/node_state.py`: `MODE_RUNS`) and every gate reads it
— the main loop, the scans, the index maintainer, the seeder, `louise
health`, the Library. What changed on a running node is all in
"download/seeding only": it no longer SCANS what it downloads (a scan is
parsing; it is owed and runs when everything does again), and the
vector-index maintainer stands down there. And a scan already running
now ends at its next ref when the mode stops parsing, where a pause used
to wait for it (1,083 s on wikivec).
- **WORK IN FLIGHT STOPS TOO — found by watching the node (412a5c7).**
  After the first deploy, "download/seeding only" took no new document
  and the two in flight went on: RAGE served 470 embedding requests in the
  next 100 s, and the largest book is 5.3 h. Embedding now ends at its next
  slice and an archive pass at its next row — the units SIGTERM already
  had — deferred, never marked, resumed from what is written. Measured
  on the node after the second deploy, embedding requests per 30 s:
  153 running; 31 in the 10–40 s after the switch (the slice in flight);
  **0** after that, with nothing at all in the worker's log; 108 within a
  minute of "run everything" again, no error. Deployed 2026-09-19 night
  (node time 22:00), the node's own test pass 3,179 green; way back
  `src.old/` and the image tags `louise-rag:pre-modes` and
  `louise-rag:v0.1.0-live`.
- **The System tab's clock panel** read "GPU governor not running" after
  the cut-over, because it reads the retired app's heartbeat; with
  `RAGE_URL` set it asks RAGE (f8a6266). `GPU_PROBE_URL` is still what
  feeds the panel's per-process rows — it shows RAGE's seven processes —
  so it was left alone; the operator's remark that it "is not used
  anymore" is OPEN: if it means the probe should go, its rows need
  another source first (rage.md §12 has RAGE's pids joined to the
  probe's).

**2026-09-20, night — ONE ORIGIN CHECK READS AT MOST 256 MiB OF THE ORIGIN.**
Operator: "10.5 GiB are too much, limit the test to some hundred MB."
(spec main fef062f — 03 §3.2, 07 §7.3, 09 §9.8; louise c688567, 2a1e89e.)
- **Why the shard sample never bounded it**: a record shard is cut by the
  collection's slices and an origin file by the origin's. Measured on the
  node, read-only, against pubmed's real snapshot (2026-08: 1,358 shards,
  39,994,988 records), two seeds: a twentieth of the shards = 68 shards ≈
  2.0 M records, read in 34 s, reaching into **651 files / 28.9 GiB** and
  **474 files / 18.6 GiB**. My "10.5 GiB" had been an estimate, and low.
- **The rule**: within the sampled shards the records are grouped by origin
  unit (the file a locator points into); the units are ranked by 09 §9.1's
  one sampling rule under the same seed and read up to the last that keeps
  their sizes within 256 MiB; the first is always read. Every sampled
  record of a unit read is compared byte for byte. Same two seeds, after:
  **5 files / 214 MiB, 14,288 records** and **5 files / 226 MiB, 1,125
  records**. The spread is real — it is how many of a file's records fall
  in the sampled years — and is why a level counts many peers' checks.
- **The platform's bound, not the plan's** (09 §9.3's reason), raised for a
  run with `--origin-mib`, never lowered. The outcome and the attestation
  say what was read (`compared`, `origin_units`, `origin_bytes`, …).
- **A cost the measurement caught**: choosing the files took 51 s, a `stat`
  for every one of two million records; per file it is 0.9 s. A cost test
  holds it.
- NOT CHANGED: there is still no step that FETCHES the origin — `--origin
  DIR` is what a peer gives — so the bound is what that step will have to
  respect when it is built, and what `--origin` reads today.

**2026-09-20, evening — QUARANTINE IS ENFORCED, AND RAGE READS THE CARD.**
- **Quarantine: ENFORCED on the node** (`platform.toml`: `[node] quarantine
  = "enforce"`, the three live collections listed by collection id under
  `quarantine_in_use_before`). The list went in first, in report mode,
  where the old code ignored it; then the deploy; then each collection was
  read as `in-use` with its open tests named; THEN the switch. After it:
  nothing withheld, search answered on gutenberg, pubmed and wikipedia,
  the node still paused as the operator left it. Way back:
  `config/platform.toml.pre-quarantine-enforce`.
- **RAGE reads the card's real memory, gives up idle models when a
  stranger squeezes it, and tells Louise** ([`rage.md`](rage.md) §19;
  louise 789bd0e, 028897a). Fifth image,
  `ghcr.io/sourstar/rage@sha256:c6e8c32be177248e4827aa0c4c9dab934b47b4eb4d6c20f9080e0c3a4b7c3ab9`
  (tag `b10630-5`; `b10630-3` and `-4` were published on the way and are
  superseded — nothing published is deleted). The app gained `cap_add:
  PERFMON`: `xe` tells what is USED only to that capability. On the node:
  32,656 MiB total, 24,691 used, 24,664 RAGE's own, 27 outside, nothing
  pushed out. Every run id is new again — a new image is a new run (09
  §9.12) — and no collection here is owed a gate (all Codex v2). Way
  back: `config/app-config.pre-memory-feature.json` (mode 600).
- **Proved live, not only in tests**: a 7 GiB stranger of my own, twice
  (rage.md §19). The first run showed the rule was not "wise" — 9,937 MiB
  given up for a 1,435 MiB deficit — and the second, after the fix, gave
  up 1,596.
- **Correction**: a model pushed off the card DOES move back when it is
  used and the card has room (chat did, at its next request). An idle one
  stays where it was pushed.
- **NOT MINE, and said**: I asked to stop the dead bake-off container and
  the permission system refused — another session's workload. It was
  gone minutes later by other hands, and that session ran four more
  bake-offs on the card during the evening, all small.

**2026-09-20, afternoon — THE CARD WAS OVERCOMMITTED FROM OUTSIDE RAGE, THE
HOST RAN OUT OF MEMORY, AND THE EARLIER ANSWER ABOUT HOST RAM WAS THE
SMALLER HALF.** Found while building what the operator asked for next:
"separate real RAM occupancy from memory mapped RAM".
- **What happened.** At 14:50 a bake-off container (`bakeoff-st:xpu`,
  another session's; Qwen3-Embedding-0.6B under sentence-transformers,
  `--attn sdpa --batch 32`) took 21.4 GiB of the 32 GiB card and 22.8 GiB
  more of GPU buffers in host RAM. RAGE had 25 GiB resident. The `xe`
  driver moved EVERY resident model's buffers into host RAM with no error
  to anyone (DRM fdinfo, `drm-resident-vram0` against `drm-resident-gtt`):
  chat 476 KiB on the card and 7,403 MiB in host RAM, bge-reranker 9 MiB /
  6,129 MiB, bge-m3 0 / 1,290 MiB. GPU buffers in host RAM, all processes:
  32.3 GiB; host memory used 56 of 63 GiB with the ZFS ARC squeezed to 2.2
  GiB. **The kernel's out-of-memory killer (global) took three of RAGE's
  children**: MADLAD and Qwen3-Embedding at 14:51:43–44, bge-reranker at
  15:03:15 — with an `xe` engine reset in the same second. They go first
  by design (`oom_score_adj` 800). The third kill came as the staged test
  run for this deploy started on the node; that run was stopped at once
  and the suite moved to the workstation.
- **Not touched**: the bake-off is another session's work and the node was
  paused, so nothing of Louise's was lost. At 15:03:15 the bake-off itself
  died of the engine reset (`UR_RESULT_ERROR_DEVICE_LOST`, at 10,752 of
  23,617 documents) and its process has spun one core since, still holding
  the card and ~27 GiB of host RAM: stopping that container is the
  operator's. RAGE restarts a dead model on
  its next request, as another launch. The models pushed out do NOT move
  back by themselves (2026-09-18 showed the same): a reload does it, once
  the card has room.
- **Why RAGE could not prevent it**: admission counts what RAGE's OWN
  models declare. A process that reaches the card without RAGE is
  invisible to it. Anything that needs the card beside RAGE has to go
  through RAGE, or run with RAGE's models unloaded. OPEN, the operator's:
  whether RAGE should read the card's real free memory before a load.
- **The correction.** The host-RAM answer of the night before (the mapped
  model file, plus 270–650 MiB of heap per process) was true and was the
  smaller half. The GPU panel's "in host RAM" column was never RSS: it is
  the driver's buffers in host memory, and on 2026-09-18 it already read
  3.9 GiB for Qwen3. That memory is REAL. Three figures, now shown apart
  (louise d41784d): GPU buffers in host RAM (real), process RAM (real,
  `RssAnon + RssShmem`), mapped file (`RssFile`: page cache, reclaimed at
  will, never occupancy). A row with most of its GPU buffers in host RAM
  reads "pushed off the card". Twenty minutes after a load the models read
  264–397 MiB owned and 0 mapped.
- **A defect of RAGE's found by it, fixed in code (8de3a60), NOT yet on the
  node**: `/louise/models` read "ready" for the three dead models, with
  dead pids. The request path was right (`_verified` starts another
  launch, hashed and checked again), so no run was stated for a dead
  process; the list was wrong. It now reads `exited`. A new front is a new
  image and a reload of every model — the wrong thing to do to an
  overcommitted card, so it waits for the card to be free.

**2026-09-20, afternoon — QUARANTINE: ENFORCEMENT BUILT, WITH THE THREE LIVE
COPIES LISTED AS IN USE BEFORE (switched on the same evening, above). The
switch must never precede the deploy: code that does not know the list
would withhold all three.** Operator: "enforce quarantine … Assume that the
current collections have already passed it. The next collection to be
created will be our guinea pig." (spec main 8b025a2, 03 §3.6; louise
cdaff93.) Under the rules as they stood all three would have left search
the moment the switch flipped — gutenberg and wikipedia have no record
shards to take roots of, and no origin check has a fetch step — so
`platform.toml` gains `[node] quarantine_in_use_before`, a list of
COLLECTION IDS (a name is refused; a refused file enforces nothing). For a
listed copy the decision stands in for roots-and-chunks and the origin
while they have not run or cannot run, and for nothing else: not the gate,
not the licence, not a refusal, not a FAILED test. Nothing is pretended —
tests read `in-use-before`, no attestation is written, `origin_check`
stays `pending`, and the Library says "tests open — in use by decision".
- **What the guinea pig will meet**, known going in: there is no scheduled
  `hold check`; `records-vs-origin` needs `--origin DIR`; `hold records
  build` and the snapshot are commands a person runs. So a new collection
  embeds NOTHING until its records are built, snapshotted and checked.
  That is the rule working — and the list of what to build next. Never
  list the guinea pig to unblock it.

**2026-09-20, midday — THE DRIVER A RUN STATES IS THE CARD'S, AND THE NODE
RUNS THE IMAGE THAT READS IT SO.** Operator: "driver fix: yes"
([`rage.md`](rage.md) §18, item 1; louise bfe5377). Second image,
`ghcr.io/sourstar/rage@sha256:d7686483861ae7dc5f9730b1efefc72f2d6d1e323654d560b4016702fcd37aa4`
(tag `b10630-2`, same base and same llama-swap: only the front changed;
public, anonymous pull checked, the manifest's bytes hash to the digest).
The node's `rage.toml` declares `pci = { xpu = "0000:03:00.0" }` — REQUIRED
here: `xe` and `amdgpu` are both loaded, and without an address the new
front states no run and says what to declare (checked in a throwaway
container before the switch, with and without the address, and against
the AMD graphics' address, which reads `amdgpu`).
- **The driver string is byte for byte what it was**: `kernel
  6.18.23-production+truenas; xe; intel-opencl-icd 26.18.38308.1-0;
  libze-intel-gpu1 26.18.38308.1-0; libze1 1.27.0-1~24.04~ppa2`.
- **All seven run ids are new, and that is the rule, not a side effect**:
  the image is part of every run (09 §9.12), so a new RAGE build is a new
  run and is re-gated. Before and after are kept in
  `state/runs-before-driver-fix.json` beside RAGE's state. Nothing on the
  node is owed a gate today — every collection here is Codex v2, and the
  run check is v3's (step 13e) — but the first v3 collection would have
  been gated again at its next write, by itself.
- Preload after the switch: 7 models, the slowest Qwen3 at 25.3 s. Search
  answered on gutenberg, pubmed and wikipedia (no leg withheld), a
  streamed Ask gave its first token in 6.8 s and finished in 12.9 s, the
  clock reads. No error in RAGE's, the web's or the worker's log. The
  node was left PAUSED, as the operator set it.
- The way back: `config/rage.toml.pre-driver-fix`,
  `config/truenas-rage.yaml.pre-driver-fix`,
  `config/app-config.pre-driver-fix.json` (mode 600). The first image
  stays in the registry — nothing published is deleted — and
  `deploy/bakeoff-st/Dockerfile.xpu` (the bake-off's, not RAGE's) still
  builds on it, which is right: it wants the runtime, not the front.

**2026-09-20, morning — RAGE NO LONGER READS ANYTHING OF OLLAMA'S.**
Operator: "If the chat model runs now in RAGE why do we need Ollama?" —
we did not, except for ONE FILE: the chat model was read from Ollama's
blob store. It now has a copy of RAGE's own,
`/mnt/ssd5/apps/louise-rage/models/chat/qwen3-8b.gguf`, checked against
the digest in the name it came from (`a3de86cd…686f`); the app's bind and
`rage.toml` point at it, and a test forbids a bind or a weights path that
names another app's folder. **The path is not part of a run**: chat's run
id is `sha256:ce6d7fb39087…` before and after, and the six other resident
models came back from the restart with the run ids they had — the first
time "stable across two launches" was seen on the node and not in a test.
Preload after the restart: 7 models in 56 s (Qwen3 31.3 s, MADLAD 19.3,
chat 11.3, bge-m3 9.3, bge-reranker 7.3, MedCPT query 5.3, cross-encoder
3.3). A streamed Ask answered in 8.2 s. The GPU panel's last cryptic row
reads `qwen3-8b`. Ollama has served nothing since chat moved (its only
requests were the comparison's two chats and the unload). The way back
for the move: `config/rage.toml.pre-chat-move`,
`config/app-config.pre-chat-move.json` (mode 600).
- **THE OPERATOR DELETED THE OLD APPS** (seen 2026-09-20: none of
  `embed-gpu-*`, `embed-cpu-*`, `translate-gpu-madlad`,
  `louise-gpu-governor` is on the node any more). So the way back through
  them that [`legacy-to-delete.md`](legacy-to-delete.md) §7 describes is
  gone by the operator's own decision; their model folders stay, because
  RAGE reads them. `ollama` still exists and runs, idle: stopping and
  deleting it, and the 89 GB of its store, is the operator's.
- **The node was rebooted at 10:34** (kernel: a software reset), and came
  back by itself: `louise-rag`, `louise-rage`, the probe, the mode it was
  in. RAGE preloaded on its own. The operator then PAUSED the node from
  the Library (10:38, from 192.168.178.76) and it was left paused — the
  two documents in flight were deferred at their next slice, as the mode
  table now has it.

**LATER THE SAME NIGHT (2026-09-19/20), unattended.**
- **Chat runs in RAGE** (operator: "the same model in Ollama, just run in
  RAGE"). `CHAT_API=openai`, `CHAT_URL=…:30437/upstream/chat`, the way
  back in `config/backup-pre-rage-2026-09-19/app-config.pre-chat.json`
  (mode 600). Loaded in 15 s, 7,250 MiB of the card, no `<think>` in an
  answer. Same prompt, the worker embedding beside both: RAGE 12.2–13.8
  tokens/s generated, Ollama 8.3–8.9. A streamed Ask over gutenberg took
  25 s through RAGE against 47 s through Ollama. Ollama is idle now, its
  model unloaded through its own API; it goes by
  [`legacy-to-delete.md`](legacy-to-delete.md) §7. `chat` and `madlad`
  joined RAGE's `preload`. **Declared card memory corrected**: Qwen3 was
  declared 5,200 MiB and holds 8,628 — admission counts what is declared,
  so that is how a card gets over-committed; now 9,000, and chat 7,600.
  Eight models resident: 25.0 GiB of the card's 32.6, and every model at
  once is 29,800 MiB declared of 30,552 usable.
- **WHY THE llama-server MODELS HOLD SO MUCH HOST RAM** (operator's
  question; measured on the node). **CORRECTED 2026-09-20, afternoon
  (above): this is the smaller half — the panel's "in host RAM" is the
  driver's buffers, which are real, and become the whole model when the
  card is overcommitted.** Two parts. (1) Right after a load, the
  model FILE, memory-mapped: bge-m3 read 1,067 MiB resident, of which 657
  file-backed, for a 1,104 MiB file. The kernel takes those pages back by
  itself — an hour later the same processes read 22–81 MiB file-backed —
  so it was never memory the process owned, though the container's
  `memory.peak` counts it. `--no-mmap` removes it altogether (438 MiB
  right after load, at no anonymous cost, since the weights go straight
  to the card). (2) What stays is the process's own HEAP, about 270–650
  MiB each: bge-m3 412, bge-reranker 396, Qwen3 657, MedCPT 268, chat 474.
  It is NOT the batch size — `-ub 8192` and `-ub 512` read 410 and 400.
  373 of bge-m3's 412 MiB is `[heap]`: the Intel SYCL/Level Zero runtime
  with its compiled kernels, a floor of ~270 MiB that even the 209 MiB
  MedCPT model pays, plus the tokenizer — XLM-R's 250,000-entry
  vocabulary for the two bge models, Qwen's 152,000 with its merges. One
  process per model pays the floor once per model: eight models, ~3.8
  GiB. NOT CHANGED: `--no-mmap` is worth adding to the llama-server
  entries — it lowers the peak the 28 GiB limit is judged against — but
  it is a change to each engine's arguments, so it is the operator's.
- **"servers.launch" on the GPU panel** were MADLAD (6.2 GB of the card)
  and MedCPT's cross-encoder (432 MB): RAGE starts both through one
  Python launcher, and the probe labelled a python process by its
  module. It now labels one that names `--weights` by them, as it does
  llama-server.
  Deployed (b9e1456; the node's own pass 3,190 green) and seen live:
  `madlad400-3b-mt-bf16` and `medcpt-cross-ov`. ONE ROW IS STILL
  CRYPTIC: `sha256-a3de86cd…` is the CHAT model — Qwen3 8B read from
  Ollama's blob store, whose file name is its digest. The probe names
  such a blob from Ollama's `manifests` beside it, and RAGE binds only
  `blobs/`. Two ways, neither taken unattended: bind Ollama's whole
  `models/` read-only and point `weights` at `…/blobs/sha256-…` (the
  run identity does not move: the template keeps `${WEIGHTS}` and the
  digest is of the bytes) — a restart of RAGE; or the design's own join of
  the probe's pids with RAGE's (rage.md §12), which needs RAGE to say
  which container it is, so a new image.
- **What re-fetching the origin would cost, measured** (the open design
  question of step 14): pubmed's plan samples 0.05 of its shards — 68 of
  1,358 — and their records come from about 249 origin files, **10.5 GiB
  to re-fetch from NCBI per check** (12 shards read: 360,000 records, 44
  files, 1.85 GiB; every record carries its origin). That is not the
  operator's "reasonable amount of data". So before a fetch step is
  built the plan needs a decision: a smaller sample for this one check,
  or a check bounded by ORIGIN FILES rather than shards — draw K files,
  re-fetch those, and compare every record of the sampled shards that
  came from them. Nothing was built and nothing attested.
- Spec, on main: 07's four modes (83fe4b1); 05's `legs_withheld`
  (34dd3d3), whose entries now carry `collection_id` in the code too.

**Not done, as of the cut-over.** The retirements of the five apps, the
governor's code and Ollama (above). Of step 14:
NOTHING RUNS `roots`, `chunks` or `records-vs-origin` BY ITSELF — the
gate does, those three are still `louise hold check <name>`, by hand or
by cron — and `records-vs-origin` has no fetch step, and gutenberg and
wikipedia have no record shards to check. A scheduled, budget-drawing
run is a change to what runs when ([`scheduling.md`](scheduling.md) §6:
measure on the node first; invariant 5), so it was not guessed at here.
Until it exists quarantine stays on `report`. `node show` (07 §7.8) is
not built; `louise health` carries the state meanwhile. What the local
trial build of the image found and fixed before any of this reached the
node is in [`rage.md`](rage.md) §17.

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
   [`gazzetta-plugin.md`](gazzetta-plugin.md). **BUILT and deployed
   2026-09-14** (1c3178e, parser 2 in 74173fc) — `app/plugins/eli.py` and
   `app/plugins/normattiva/`, `akn_article` v1, `NORMATTIVA_SPEC`; see
   "Normattiva on a generic ELI layer" above for what the build decided and
   what it left out (the delta feed, the SKOS tables, the §6
   ReferenceSource, the manifest `eli` block). The 2026-08-26 decisions
   below were all taken. **No Normattiva collection exists**, and the
   encoder is what it waits on — now MEASURED rather than argued
   (2026-09-20, `gazzetta-plugin.md` §7.1b). **One decision is open and it
   is the operator's:** 2026-09-14 the encoder was declared the operator's
   own side project, a dedicated embedder, with no off-the-shelf model to
   be proposed meanwhile; the 2026-09-20 bake-off then measured the
   off-the-shelf field and recommends one (§7.1b). Nothing records whether
   the measurement supersedes that decision or merely gives the trained
   model its target and its baselines. Do not create a collection on the
   bake-off's recommendation until that is answered — the choice is
   `space_id`-pinned and irreversible. Normattiva's data went CC BY 4.0
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
   coat). Encoder: the 2026-08-26 shortlist below
   (KaLM-Embedding-Gemma3-12B, Qwen3-Embedding-8B) was reasoning, and
   **2026-09-20 replaced it with measurement** — `tools/encoder_bakeoff.py`
   over JuriFindIT's 179 expert questions, through RAGE on the B70,
   `gazzetta-plugin.md` §7.1b. Neither shortlisted model is the answer. The
   two findings that outrank the ranking: running the published
   **DAR-legal-it** ourselves landed within 0.0014 of its authors' recalls,
   which **validates our metric code** and makes every row comparable to
   anything published against that set; and a **query instruction is worth
   more than most model changes** — the same qwen3-embedding-4b weights
   gain +4.2 MRR@100 from `Instruct: …\nQuery: ` on the query side alone.
   The platform had no mechanism for it on the serving side; since
   30c7276 (2026-09-21) a query is prefixed with the Codex's
   `space.query.instruction` (02 §2.4) — set in the recipe at `hold
   collection create`, inside `space_id` (`gazzetta-plugin.md` §7.4b).
   The recommendation — qwen3-embedding-4b, instructed — **rests on the
   licence and only on the licence**: DAR-legal-it is the better model
   measured identically and 3.3x cheaper per vector, but declares no
   licence, and `approved-encoders.md` bars an unlicensed model from owning
   a pinned space, because a peer could never reproduce it on stated terms.
   bge-m3 is not a floor but 16 MRR points below one. The constraints that
   survive from the original reasoning, and that apply to a trained encoder
   too: a closed API model cannot own a space (a peer must reproduce
   vectors at cos ≥ 0.999), and an MLM — Italian-Legal-BERT was the
   tempting one — walks straight into item 6's anisotropy trap.
   Operator decisions 2026-08-26: scope is
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

- **A fair share handed to collections that had no work** (2026-09-23).
  The parse pool's admission divided the children by PRESENCE —
  collections seen in a 30 s window — so wikipedia, the only one parsing,
  got `11 // 3 = 3` children and a 6-task window out of 22. gutenberg only
  embeds (its chunking is in-process, `worker._load_chunks`) and pubmed
  was caught up; both registered anyway, through `genesis.drain_held`.
  Measured: 11 children alive, **1 running**, 10 in `futex_do_wait`, the
  worker tree at 77 % of 1200 %, and `pipeline-wikipedia` parked in
  `future.result()` inside `parallel.compute`. Two lessons. **Presence is
  not demand** — every share must count what is HELD, the way the encoder
  rotation counts real work. And **nothing logged it**: the batch log
  shows duration and size, so the batches read as merely slow, and the
  sizer then faithfully fitted a cost model to a machine running on one
  core and shrank the batches, which read as the sizer working. Four
  scheduling fixes the night before had moved throughput 19.9 → 64 docs/s
  without moving the CPU — *that* was the signal, and it was there for a
  day before anyone read it as one. Instrument occupancy, not just
  duration: `parallel.occupancy()` now integrates how many children had
  work and `_log_shares` prints it (`parse pool — 1.3 of 11 children busy
  (12%)`). (docs/notes/scheduling.md §11; use `py-spy dump`, not `record` —
  `record` drops threads blocked in `wait` unless `--idle`, and the first
  profile therefore reported zero samples in the pool, which is the
  opposite of the truth.)
- **"Finer parts balance the load" — measured, and it was 4.6x WORSE**
  (2026-09-23, same night). Shipped alongside the admission fix above: cut
  a batch into 8 tasks per child instead of 2, so one dear document cannot
  idle a child. It looks free — a part still carries `MIN_PART` rows, so
  the bytes pickled are the same bytes. Live it took wikipedia's rdf from
  6.3 to 28.8 ms/doc and fts from 10.2 to 27.2, with the CPU UP (77 % →
  193 % of 1200 %) and throughput DOWN (54.2 → 24.3 docs/s). **More CPU for
  less work is redundant computation**; the likeliest redundancy is
  locality — the cut is by equal COUNT while wikipedia's documents come out
  of shared multistream blocks, so every child holding one of a block's
  documents decompresses the whole block. Reverted; the ceiling is now a
  test. The lesson is the assumption, not the constant: **a part's cost is
  not independent of the other parts**, so balance cannot be bought with
  granularity until the cut follows the blocks. And: deploying two changes
  together nearly buried a win under a loss — the admission fix had to be
  re-measured alone.
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

### The legal encoder becomes measurable — 2026-09-20

`gazzetta-plugin.md` §7 held that the encoder for a legal collection is
chosen on Italian-legal retrieval quality, and explicitly that **what this
node happens to run is not an input to that decision**. §12 item 6 then
parked the whole thing on an unanswered question: does JuriFindIT, the set
that would decide it, actually exist to download? It does —
`jurifindit/JuriFindIT`, parquet, CC-BY-NC-SA-4.0 (gated: invariant 9 wants
an ack, and `fetch` writes the digests to record one against).

`tools/encoder_bakeoff.py` runs §7.2's procedure against RAGE: the 179
expert validation questions over the 23,617-node corpus, through
`app.embeddings` so the request shape is production's, sharded and
resumable because it is ~47 minutes per model on the B70.

Three things the data forced, each of which would have produced a
confident wrong answer:

- **`relevant_doc_ids` is a list** — 4.49 per question, up to 128. Recall@k
  and "did we find one" are different numbers and only the first is
  comparable to anything published.
- **The corpus is an AKN node tree**, not a flat article list: 159
  `<< file root node >>` placeholders, one node of 1.13 M characters, and
  llama.cpp refuses an over-context input rather than truncating it. The
  cut is ours to make and must be identical across candidates.
- **No query prefix exists in this codebase** (`embeddings._side` switches
  URL for a bi-encoder pair and nothing else). Right for bge-m3, which is
  symmetric; wrong for Qwen3-Embedding, which documents
  `Instruct: …\nQuery: ` on the query side. So the prefix is a recorded run
  parameter keyed to the query shards only — a variant re-embeds 179
  vectors and reuses all 23,617 document ones.

The competitor is `jurifindit/DAR-legal-it`: 305 M, dim 768, GTE on
`snowflake-arctic-embed-m-v2.0`, recall@10 0.8037 and mrr@100 0.7780 on
that validation split. It **declares no licence**, which bars it from
owning a pinned space under `approved-encoders.md` — a reference row, not
a candidate, until that changes. What circulates as "77.80 for DAR+GIR" is
this plain dense model's own mrr@100, not the graph pipeline's.

**Run 2026-09-20**, and it answered §7 (full table in
[`gazzetta-plugin.md`](gazzetta-plugin.md) §7.1b):

| model | recall@10 | mrr@100 |
|---|---|---|
| DAR-legal-it, **run here** | **0.8031** | 0.7689 |
| DAR-legal-it — *their published table* | 0.8037 | 0.7780 |
| **qwen3-embedding-4b, instructed queries** | 0.7748 | **0.7744** |
| qwen3-embedding-4b, bare queries | 0.7539 | 0.7324 |
| bge-m3 | 0.6834 | 0.6142 |

Corpus cost: bge-m3 18m16s, qwen3-embedding-4b 59m29s, DAR-legal-it ~4m.

DAR-legal-it was run on the card through a **side-car** — `FROM` the
deployed rage digest plus one venv, run once and discarded
(`deploy/bakeoff-st/Dockerfile.xpu`). RAGE itself, its `rage.toml` and the
pushed image were not touched. **Its recalls land within 0.0014 of the
authors' published figures**, which validates our metric code against the
real `InformationRetrievalEvaluator` and makes every row here comparable
to anything published against this set.

Two findings worth carrying out of it. **bge-m3 is not a candidate for
Italian legal** — 16.4 MRR points behind the domain specialist, and behind
a generalist this node already serves. And **the query instruction is
worth +4.2 MRR@100 on identical weights** — bare, qwen3 trails the
specialist by 4.6 points and loses; instructed, it is 0.36 behind and
ties. Closed 2026-09-21: the prefix is the Codex's
`space.query.instruction`, and search applies it since 30c7276
(`gazzetta-plugin.md` §7.4b).

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
