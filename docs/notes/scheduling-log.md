# The scheduling measurement log

**Append-only. One row per deployed scheduling change, written BEFORE the
next one starts.** `scheduling.md` says how the scheduler works and why;
this file says what each change actually did on the node, under what
conditions, so the next person can tell a win from a coincidence.

It exists because of 2026-09-23. Four fixes had moved throughput
19.9 → 64 docs/s over one night, recorded as four numbers in a commit
message with no statement of what the node was doing at the time. The next
change was then measured as "54.2 docs/s before, 24.3 after" — a 2.2x
regression — and *nothing in the record could rule out "different
articles"*, because wikipedia's documents differ by two orders of
magnitude and the two windows covered different parts of the corpus. The
regression happened to be real, and was only confirmed because the
per-document unit cost moved too. That was luck.

Then it happened again with the sign reversed: the next row, the same
change's good half on its own, read as "+3.5 % throughput for 4.1x the
CPU" — a second regression — until `chunks/doc` turned out to have gone
from 0.65 to 3.59. It was a 5.7x win. **A wikipedia document is not a unit
of work**, and a throughput number without its scenario is not evidence in
either direction.

## The rules

1. **No scheduling change is deployed without a row here** — filled in
   before the deploy (the `before` half) and completed after it.
2. **Record the scenario, not just the number.** Which collections had
   work and of what kind, the node mode, where in the corpus, what else was
   running. Two rows are comparable only if their scenarios are.
3. **Normalise by CHUNKS, not documents.** This is the one that matters and
   it was learned the hard way, twice in one night, in both directions. A
   wikipedia document is not a unit of work: a redirect-dense stretch of the
   corpus yields **~0.6 chunks per document**, a stretch of real articles
   **~3.6** — so `docs/s` between two windows can compare workloads that
   differ five-fold. Always record `chunks/doc`; it is what says whether two
   rows are comparable at all. Compare `chunks/s`, and `CPU per chunk` for
   efficiency. `ms/doc` inherits the same flaw and is only usable between
   rows with the same `chunks/doc`.
4. **A throughput number that moves without CPU moving, or CPU that moves
   without throughput, is a question, not a result.** Both times that
   happened here the answer was in `chunks/doc`.
5. **One change per deploy.** 2026-09-23 shipped two and the loss buried
   the win; the good half had to be re-measured on its own.
6. **Record reverts as rows.** A change that was backed out is the most
   valuable row in the file.

## How a row is measured

Same commands every time, so the numbers mean the same thing
(`scheduling.md` §7 for the reasoning):

```bash
# worker-tree CPU over 60 s, as a share of 1200 % (12 cores)
#   — the whole process tree, so the pool's children are counted
# throughput and the share of wall time actually inside batches
bash tools/sched_measure.sh <window-minutes>   # run ON THE NODE

# per-document unit cost, per stage — the scenario-independent one
docker logs --since <N>m ix-louise-rag-worker-1 | grep 'unit costs'

# how many pool children had work (since 601b3c0)
docker logs --since <N>m ix-louise-rag-worker-1 | grep 'parse pool'

# which thread is blocked on what — `dump`, NOT `record`:
#   record drops threads parked in wait() unless --idle
docker run --rm --pid=host --cap-add SYS_PTRACE pyspy:local py-spy dump --pid <pid>
```

---

## Rows

### 2026-08-26 — the reference snapshot

Not a change: the baseline costs in `scheduling.md` §4. Kept as the origin
of the series.

---

### 2026-09-23 (night) — four fixes, measured cumulatively

Commits `5b98358` (pool window kept full), `a671818` (affine sizer),
`f45994f` (lazy page bytes), `7d7512b` (damped step). Recorded in `9710e24`.

**Scenario — NOT RECORDED AT THE TIME.** Reconstructed from the commit and
the logs: wikipedia rdf/fts on its pipeline thread, gutenberg embedding,
pubmed caught up. Corpus position, node mode and concurrent GPU load were
not written down, and the four steps are cumulative rather than isolated —
so no single line below is attributable to its own change with confidence.

| step | docs/s |
|---|---|
| before | 19.9 (batches of 285, 75 s units) |
| + pool window kept full | 29.2 |
| + affine sizer, lazy page bytes | 72.5 (over 25 min, 66,000 documents) |
| + damped step (final) | 64.3 (longest unit 57 s → 32 s) |

Worker CPU: flat ~190 % of 1200 % → bursts of 1007 %, samples 108, 1007,
116, 238, 107, 109, 350, 209. Noted at the time: the parse phase uses ten
cores and the commit phase one, alternately.

**Verdict:** kept. **Caveat:** cumulative, scenario unrecorded — treat the
individual deltas as indicative only.

---

### 2026-09-23 02:xx — baseline before the admission change

No code change; the measurement the next row is compared against.

**Scenario.** Node running everything. wikipedia: the only collection with
parse work (rdf + fts on its one pipeline thread). gutenberg: embedding
only, its pipeline thread logging "rdf/fts caught up"; its chunking is
in-process (`worker._load_chunks`) and never enters the pool. pubmed:
caught up, idle. RAGE serving bge-m3 and qwen3-embedding-4b; a 7 GiB
stranger had been on the card earlier in the day. `PARSE_PROCESSES=11`.

| metric | value |
|---|---|
| worker tree CPU | 77 % of 1200 % (60 s) |
| throughput | 54.2 docs/s (83 batches, 51,215 docs, 945 s in batches, 30 min window) |
| wall time inside batches | 945 s of 1800 s (53 %) |
| wikipedia rdf | 6.3 ms/doc |
| wikipedia fts | 10.2 ms/doc |
| **chunks/doc** | **~0.65** (fts lines: 1215/2025, 669/1024, 437/888, 1882/2700, 736/1000) |
| **chunks/s** | **~35** |
| **CPU per chunk** | **~22 ms** |
| pool children busy | 1 running, 10 in `futex_do_wait`; 132 % of 1200 % between them |
| parent thread | 5 % of one core |

**Corpus position: a redirect-dense stretch.** 0.65 chunks per document
means most of these documents produced no text at all — wikipedia's
redirects are kept as records. This is the number that makes the rows below
comparable, and it was very nearly not recorded.

`py-spy dump`: `pipeline-wikipedia` parked in `future.result()` inside
`parallel.compute`. The node was blocked end to end on pool admission
(`11 // 3 = 3` children, 6-task window, 22 slots available).

---

### 2026-09-23 00:53 — `601b3c0`, TWO changes at once — REGRESSION, reverted in part

Shipped together: (a) admission counts what is HELD, not who has been seen;
(b) parts cut at 8 per child instead of 2.

**Scenario.** As the baseline row, EXCEPT: the containers were recreated, so
the sizer restarted from its configured defaults (it is in-process and not
persisted) and the startup discovery scan ran. Measurement window 12 min
starting ~9 min after the restart, against a 30 min window before — **the
windows are not equal and cover different parts of the corpus.** This is
the comparability problem this file exists to stop.

| metric | before | after | |
|---|---|---|---|
| worker tree CPU | 77 % | **193 %** | ↑ |
| throughput | 54.2 docs/s | **24.3** | ↓ 2.2x |
| wall inside batches | 53 % | 40 % | ↓ |
| wikipedia rdf | 6.3 ms/doc | **28.8** | ↑ 4.6x |
| wikipedia fts | 10.2 ms/doc | **27.2** | ↑ 2.7x |
| **chunks/doc** | ~0.65 | **0.13 – 0.69** | comparable, if anything LIGHTER |
| manifest deferrals | — | 0 | |
| errors | — | none | |

**Verdict: (b) REVERTED, (a) not attributable from this row.** More CPU for
less work is redundant computation. The `chunks/doc` row is what makes this
a finding rather than a window artefact: the documents in the "after"
window were no heavier than the baseline's — several batches were markedly
lighter (1000 documents for 129 chunks) — and the per-document cost still
rose 4.6x. Which redundancy was not isolated; the likeliest is locality
(parts are cut by equal COUNT while wikipedia's documents come out of
shared multistream blocks, so every child holding one of a block's
documents decompresses the whole block).

*(`chunks/doc` added to this row retrospectively, from the captured log
lines, once the next row showed why it was the number that mattered.)*

**What this row cost, and the process lesson:** two changes in one deploy,
the loss hid the win, and (a) — which the `py-spy dump` above shows was a
real end-to-end block — has to be re-measured alone. Rule 5 above comes
from here.

---

### 2026-09-23 01:24 — `8b1aced`, admission alone (`PARTS_PER_CHILD` back to 2)

The win from `601b3c0`, with the loss removed. Stated in advance: pool
occupancy should rise from ~1.3 children, and if `ms/doc` did not return to
≈6.3 / ≈10.2 then admission was not the win the thread dump implied.

**Scenario.** As the baseline row — wikipedia the only collection parsing,
gutenberg embedding, pubmed idle, `PARSE_PROCESSES=11` — **except that the
corpus had moved into a stretch of real articles.** 11 min window starting
~1 min after the recreate.

Two windows, 11 min and 25 min, agreeing — the second is the one to quote.

| metric | baseline | 11 min | **25 min** | |
|---|---|---|---|---|
| **chunks/doc** | ~0.65 | 3.59 | **3.51** | the workload is ~5.4x heavier |
| pool **slots in flight** | ~1.3 of 11 (12 %) | 8.5 (77 %) | 8.1 / 8.9 / 8.5 (73–81 %) | **NOT running children — see the correction below** |
| worker tree CPU | 77 % of 1200 % | 316 % | **408 %** | |
| chunks/s of wall | *see caveat* | — | **73.3** | |
| docs/s inside batches | 54.2 | 56.1 | 69.3 | *do not compare across chunks/doc* |
| wall inside batches | 53 % | 51 % | **54 %** | **unchanged — untouched by this** |
| wikipedia rdf | 6.3 ms/doc | 11.8 | 10.9 – 14.4 | on ~5.4x the text |
| wikipedia fts | 10.2 ms/doc | 13.0 | 8.1 – 13.6 | on ~5.4x the text |
| manifest deferrals | — | 0 | **0** | |
| errors | — | none | **none** | |

**Verdict: KEPT**, but on the `py-spy dump` evidence, not on the number
this row first quoted.

### CORRECTION, same night — the occupancy metric overstated this

`parallel.occupancy()` counts **tasks in flight** (submitted and not yet
collected), capped at the pool size. `compute` submits a whole batch at
once — parts ≈ `PARTS_PER_CHILD x n` = the cap — so the slots stay in
flight through the entire straggler tail while the children that finished
early sit idle. It therefore answers *"was the pool allowed work?"*, which
is the admission question and a real one, and NOT *"were the children
working?"*, which is what the metric's name, this row, and its commit
message all claimed.

Measured directly, sampling children in state `R` at 1 Hz for 90 s:

```
1110bbbbb6222221110bb7543332221111111111111113342152103b2032205bb6411111bb0502bb1bbbb41000
mean 3.7 running of 11;  at 0 for 10 s;  >= 8 for 18 s
```

**73–81 % in flight, 34 % actually running.** Both numbers are true and
they answer different questions. Renamed to `slots_in_flight`; the log line
now reads "slots in flight".

The admission fix is still right — `py-spy dump` showed
`pipeline-wikipedia` blocked in `future.result()` and the pool provably
under-admitted — but the size of the win is not established by this row.

The throughput multiple is an **estimate, not a measurement**. Baseline
`chunks/s of wall` cannot be recovered: the container has been recreated
twice and `docker logs` only holds the current one, so it rests on 5
sampled fts lines (chunks/doc ≈ 0.65) and an assumption that about half of
the 83 batches were fts — giving ~9 chunks/s of wall against 73.3 now,
call it ~8x with wide error bars. **A future row must not be in this
position:** capture the full `chunks` totals into the row *before*
recreating anything.

**The stated falsifier fired, and was wrong.** `ms/doc` did NOT return to
6.3/10.2; by the rule written in advance this row should have read "not the
win the dump implied". It reads the opposite because `chunks/doc` — which
that falsifier did not control for — had gone from 0.65 to 3.59. Read on
raw `docs/s` alone (+3.5 % for 4.1x the CPU) this looked like a second
regression, and was within one step of being recorded as one. **A criterion
stated in advance is only as good as the normaliser it is stated in.**
Rules 3 and 4 above are the correction, and this is the row that bought
them.

**Distance to the operator's target.** The ask is the CPU constantly above
95 % — above ~1140 % of 1200 %. This row is at **408 %**, so roughly a
third of the way, and the three remaining terms are now all measured
rather than guessed:

### What the 300-second profile then showed — the three items are ONE

`py-spy record --idle --threads`, 300 s, 5,960 samples per thread. The
operator had described it from outside first: *"peaks in which all cores
are at 99 %, then the workload goes down, and up again"*, and asked whether
the single-threaded part was the GPU embedding feeder.

| thread | where it was |
|---|---|
| **pipeline-wikipedia** | **5,756 / 5,960 (96.6 %) in `compute` — blocked on the pool** |
| pipeline-wikipedia | 77 catalog `_commit`, 47 `fts.index_books` (the commit phase: **2 %**) |
| pipeline-wikipedia | selection, archive, bundle drain: **< 1 % combined** |
| embed-gutenberg | 5,954 / 5,960 blocked in `_embed_parallel` — waiting on the GPU, burning nothing |
| embed-wikipedia, embed-pubmed | 100 % idle sleep |
| MainThread, index-maintainer, stats-publisher | 100 % idle |

**No: the embed feeder is not the single-threaded phase**, and neither is
the commit, at 2 %. The only CPU consumers are the pool's children, and the
single-threaded stretches are **stragglers** — one child still on its part
while ten have nothing.

**And the three open items above are one phenomenon: the batch is a
barrier.** With `PARTS_PER_CHILD = 2` a batch cuts into ~22 parts and the
cap is 22, so `compute` submits the ENTIRE batch at once and then drains
it. There is no refill — the operator's phrase, "the problem seems to be
the refill of the work queues", is exactly right and more literal than it
sounds. One sawtooth per batch: burst at submission, decay as parts finish,
trough of straggler tail + commit + selection, then the next burst. The
"46 % outside batches" and the "77 % occupancy" are two views of the same
trough, and "parse and commit never overlap" is its cause.

So the next change is not any of the three separately. It is to stop the
pool draining between batches — overlap batch N's tail with batch N+1's
parse — and it must go out ALONE, with running-children sampling as its
measure, since slots-in-flight cannot see this at all.

Beyond that the ceiling is policy, not scheduling: wikipedia's embedding
is off and gutenberg is GPU-bound.

---

### 2026-09-23 02:55 — `8592c33`, cut the batch along its work units

One change: `parallel._partition` cuts contiguous parts on multistream
CHUNK boundaries and balances them by chunk bytes, via a new
`plugin_io.work_span` the wikipedia source implements. gutenberg and pubmed
return None and keep the equal-count cut. (Carried with it, behaviourally
inert: the `slots_in_flight` rename of `59a6b55`.)

**Scenario.** As the rows above, except the corpus stretch is lighter:
**chunks/doc 1.69** against 3.51. 9 min window, 10 min after the recreate.

| metric | previous row | this row | |
|---|---|---|---|
| chunks/doc | 3.51 | 1.69 | lighter stretch |
| **chunks/s of wall** | 73.3 | **103.7** | **+41 %** |
| worker tree CPU | 408 % | 372 % | −9 % |
| **CPU per chunk** | 5.57 | **3.59** | **−36 %** |
| wikipedia rdf | 10.9–14.4 ms/doc | **2.0** | |
| wikipedia fts | 8.1–13.6 ms/doc | **0.4** | |
| **running children (mean of 11)** | **3.7** | **1.7** | **FELL** |
| running children at 0 | 10 s of 90 | **33 s of 90** | worse |
| slots in flight | 73–81 % | 61 % | |
| wall inside batches | 54 % | 48 % | worse |
| deferrals / errors | 0 / none | **0 / none** | |

**Verdict: KEPT — and the stated falsifier FIRED.** It said mean running
children must rise well above 3.7. It fell to 1.7. Both halves are true and
they do not conflict:

* The **work** got cheaper, as simulated: 36 % less CPU per chunk, 41 %
  more chunks/s at 9 % less CPU. That is the locality half — chunks are no
  longer decompressed by two children — and it matches the simulation's
  0.72–0.89x total work.
* The **pool is idler**, because the work it was given shrank while nothing
  else changed. The bottleneck MOVED OUT OF THE POOL. A falsifier written
  in the right normaliser can still be the wrong QUESTION: "are the
  children busy" stops being the goal the moment the children stop being
  the constraint.

**Where it moved.** `py-spy record --idle`, 180 s: the pipeline thread is
still 80 % blocked in `compute` (2,873 of 3,600 samples), 19 % running. But
the children are 77 % asleep on `futex_do_wait` / `anon_pipe_read` —
waiting for work, not for disk (zero `D` state, 1.8 MB/s read). So the
parent waits on ONE straggler while ten children have nothing left to take.
`slots_in_flight` read 6.7 while 1.7 ran, which is exactly its documented
flaw: a task finished but not yet COLLECTED still counts, because results
are consumed in order.

The batch log names the shape: batches are now **20,000 documents** (the
sizer grew them as the work got cheap, to its `hi` clamp) taking
**0.9–2.6 s**, with occasional **52.5 s** and **53.7 s** outliers. Most
batches no longer fill the pool at all, and the outliers are not the large
ones.

**The balance half did NOT transfer, and the cost model is why.** Parts are
balanced by COMPRESSED CHUNK BYTES, which is a proxy for decompression
only. A 26 kB chunk can hold 55 stubs or one enormous article, and the
parse cost — wikitext, not bz2 — does not follow it. Left unfinished rather
than guessed at: the next step is to MEASURE per-part durations, which
needs instrumenting `compute`, not to invent a second proxy. The first
proxy is what this row is about.

**Distance to the target.** CPU 372 % of 1200 % against the ask of
>1140 %. The efficiency gain is real and idleness is now the whole problem:
52 % of wall outside any batch, and inside a batch one part holds the rest.
Neither is an admission or a partitioning question any more.

### 2026-10-08 — scheduled checks: the plan in the background (03 §3.2)

**The change.** A new worker thread, `scheduled-checks`
(`app/scheduled_checks.py`), runs `louise hold check <name> --origin
<corpus> --json` as a CHILD PROCESS at nice 10, one at a time on the node,
for each Codex collection whose snapshot has no scheduled check yet or whose
last one is a cadence old; only in a mode that parses. Nothing in the round
loop, a stage's selection or the pools changes.

**Before (measured on the node the same day, `hold check` by hand inside the
running worker; node `running`, gutenberg scanning its new snapshot,
pubmed and wikipedia embed off, RAGE serving).** Per collection, the whole
plan with its origin:

| collection | wall | peak RSS | result |
|---|---|---|---|
| gutenberg-mini-open, -certified | 1–2 s | 0.14–0.18 GB | pass |
| pubmed, before the fixes | 1,532 s | **18.7 GB** | pass |
| pubmed, after the fixes (6928c2c) | **816 s** | **2.0 GB** | pass |
| gutenberg (before the fixes) | 1,534 s | 2.6 GB | fail: 1 record changed at the origin since the build |
| wikipedia, louise-stop, deposit-granite | — | — | no snapshot: not checked |

The 18.7 GB was two defects, fixed first (6928c2c): loading pubmed's first
snapshot materialised its 39,994,988-uid delta (4.3 GB → 16 MB), and
`records-vs-origin` held every sampled line before the origin bound
(~13 GB → streamed). Worker RSS: 4.2 GB at 55 min uptime before; 2.2 GB at
15 min after (to be read again at a comparable uptime).

**Expected after.** At deploy, every Codex collection with a snapshot is due
once (none has a scheduled check yet): pubmed (~14 min, 2 GB), gutenberg
(~25 min, ≤2.6 GB), the minis (seconds), back to back, then nothing until a
new snapshot or a month. The pipeline threads keep their CPU (nice 10, a
separate process: no GIL taken).

**After (6b6e597, deployed 11:43Z).** The first round ran back to back, one
child at a time, nice 10: gutenberg pass in 1,616 s; gutenberg-744ba8430fed
and the three minis pass in 2 s each; pubmed pass in 739 s. Peak child RSS
sampled every 30 s: **1.68 GB** (the 2.0 GB measured by hand is the true
peak between samples). Worker RSS **2.26 GB at 40 min** uptime, against 4.2 GB
at 55 min before the snapshot fix. Nothing is due again until a new snapshot
or a month. Not measured: the pipeline's docs/s during the runs (only
gutenberg's rdf scan had work, too little to read a difference from).

---

### 2026-10-19 — the embed stage leaves another peer's parts to it (04 §4.5) — PREPARED, not deployed

`app/share/leading.py`; `worker._backfill_rows`; `contribute.leaders` (the
cascade, read-only, shared with `shards_i_lead`). Under `embed = "all"` a
shared collection's open parts whose rendezvous leader is ANOTHER live peer
are not embed candidates here; the new walk then goes by the cursor so the
skipped head never blocks it. Review: the NAS session.

**Scenario.** Measured read-only on the Perugia peer's gutenberg manifest
(snapshot 2026-09, 1,637 parts, 79,583 documents — the node's snapshot,
byte-identical members) with three live peers at their benchmarks: this
peer 1,856 tok/s (its gate, run 9ab03cb2), the node 3,200 (modelled from
its B70 rows), the laptop 1,500 (assumed). Node mode running, gutenberg
embed off here (the stage was not exercised live).

**Before.** The node embedded every gutenberg part under "all" (its log,
2026-10-19 00:00: batches of ~900–1,500 chunks, beside two contributing
peers whose bundles would only have been checks).

**Measured (the new code's own paths, `/d/louise-work/measure_leading.py`).**
- Index build — `contribute.leaders` + the bisection tables: median
  **434 ms**, max 468 ms, for 1,635 open parts; at most once a `REBUILD_S`
  (60 s) and only when the snapshot, the live set, the coverage rows or the
  hour change. Scales with parts, not documents.
- Lookup: **0.71 µs** a document. Left to the other two: 56,597 (71 %);
  this peer leads 474 of 1,635 parts (29 %, its benchmark share 28 %).
- One backfill page: plain 0.15 ms, filtered ≤ 1.75 ms (20k-row windows),
  ≤ 0.61 ms (200k); plan `SEARCH documents USING INTEGER PRIMARY KEY
  (rowid>? AND rowid<?)`.

**Expected after (when a node runs "all" beside contributing peers).** Each
embeds about its benchmark share of the open parts; a leader that publishes
nothing within `grace_days` loses its parts to the next in line (the cascade,
noticed within an hour). To be measured on the first node that runs it:
chunks embedded per peer per day against its share, and the duplicate rate.

### 2026-10-19 — the embedder sweep honours an encoder lease (pitfall 17) — PREPARED, not deployed

`worker._sweep_unheld_embedders`; `node_state.encoder_leases`;
`louise._leasing` on `share contribute` and `hold vectors build`. Found and
measured by the Perugia session, read-only, during the a894a3f contribute
pass; review: the architecture session.

**Scenario.** Perugia: gutenberg `embed = "off"`, `share contribute`
(CLI) embedding de/00041 and de/00045 with vega-qwen3-embedding-4b on
LocalAI (AMD RX 9060 XT over DXG), 11,480 chunks in 81 min ≈ 142/min,
GPU at 100 %; worker mode running, nothing else embedding.

**Before.** `/system` cpu_percent of the backend, every 10 s for 3 min
(18 samples): 2.05, 0.63, 1.05, 0, 1.04, 2.51, 1.07, 1.57, 0, 0.71,
1.03, 1.91, 0, 1.07, 1.09, 1.45, 0.76, 0 — exactly 0.0 one sample in
four while embedding. Sweep log "unloaded: idle or named by no held
collection" at 18:29, 18:31, 18:43, 18:49, 19:11, 19:33, 19:39Z (and
the 18:18 start-up run check): each a LocalAI "Backend process stopped
… exitCode=0" + `/backend/shutdown 200`, then "BackendLoader starting"
on the contribute's next request and its answer 19 s later. Cost
7 × 19 s ≈ 2.2 min of 3.4 h (~1 %) plus a fresh load each time; no
non-200, no retry, no defer line anywhere.

**Expected after.** No "unloaded" line for a model under lease while a
contribute runs; `/backend/shutdown` only for a model no process is
embedding with; a lease a killed process left gone within 600 s. Cost
of the fix: one `INSERT OR REPLACE` per batch (seconds apart), one
SELECT per settle beat over a table of a handful of rows (plan-checked:
tests/test_embedder_release.py). To be measured on Perugia over one
contribute pass: shutdown count (expect 0 while embedding), chunks/min
(expect the same 142 or slightly better).
