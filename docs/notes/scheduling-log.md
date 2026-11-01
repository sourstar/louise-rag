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
caught up, idle. The model pool serving bge-m3 and qwen3-embedding-4b; a 7 GiB
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
pubmed and wikipedia embed off, the model pool serving).** Per collection, the whole
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

**The node too** (München, 2026-10-19 22:00): the node's gutenberg is
`embed = "on-demand"`, not "all" — the model its nightly contribute uses
is named by no collection that embeds everything, so the same sweep
unloads it there: contribute-gutenberg.log counts **12 cold starts
today**, and the worker log shows sweeps of vega-bge-m3 and
vega-dar-legal-it at 21:56. The commit's note that the change is inert on
the node was wrong; the lease matters on both peers.

**Deployed 2026-10-19 22:28 CEST on both peers (0624b18, München's gate:
4,756 passed).** The node at 22:32: one row, `http://192.168.178.10:30286 |
vega-qwen3-embedding-4b | share contribute pid 379 | until 22:41:02`,
renewed per batch; no new worker line kinds. One cold start remains that
is not the sweep's: the start-up run check unloads the 4B ("embed is
'lazy', not 'all'") before the contribute has its lease, and the
contribute then waits for the writer lock behind the worker's on-demand
embed unit (1 document, 337 chunks, 22:30:34) — the pause-wrapper cost
the publisher retires. Counts over the night follow.

**Perugia, after (0624b18, pass 20:31–20:41Z, stopped by SIGTERM for the
23:00 shutdown, exit 130 in 14 s).** Worker "unloaded: idle…" lines during
the pass: **0** (seven in the 3.4 h before). LocalAI "Backend process
stopped … 4b": **1**, at 20:29:34Z — the worker's start-up run check
("embed is 'off'"), before the pass; during the pass **0**. The lease row
(`host.docker.internal:30286 | vega-qwen3-embedding-4b | share contribute
pid 216`) appeared with the first batch, renewed (until 20:46 → 20:50),
and was gone 2 s after exit — released by the command. Rate over 7 min of
embedding: no slowdown seen; too short against the 143/min to be the
figure (the bundles mixed vectors of the earlier stop). The row stays
open for the next long pass and for the node's night.

**Perugia, 21:05Z: the other direction.** 24 min after the contribute
exited and released its lease, vmmemwsl at 35 % CPU and 74 % RAM with
nothing running: the LocalAI container at 200 % CPU, 12.7 GB; inside it
the idle `llama-cpp-fallback` of vega-qwen3-embedding-4b at 283 % CPU
(the HIP runtime busy-polls under dxg); `/system` for the same model:
cpu_percent 0.078, rss 62,914,560 — the VeGa wrapper's numbers. Never
exactly 0.0, so the sweep never unloaded it. Perugia shut it down by
hand (`POST /backend/shutdown`): 0.03 % CPU, 5.7 GB page cache. Fix the
same night: the sweep's idle test is the last request any process made
(`encoder_use`, written by the embeddings client), not CPU.

**The node, after — the night 2026-10-19/20 on 0624b18 (the lease; the
CLI contribute under the pause, 22:31:03 → 06:00:28, 449 min).** Cold
starts in contribute-gutenberg.log: **0** (12 the day before). The lease
row present and renewed during the pass, none after; the worker unloaded
the 4B at 06:01:46, 78 s after the pass — the intended idle unload — and
not once during the night; overnight the sweep unloaded only
vega-dar-legal-it (every ~30 min) and MedCPT after pubmed's 06:00 flip.
The pass: probed 832, mine 643, claimed 827, held 1, left_to_fetch 0,
built **88,165 chunks in 449 min = 196 chunks/min** (90,732 "to embed"
over 144 parts, the last interrupted), 150 bundles accepted and
announced (974,949 chunks in the built ones), 4 refused — all by T0's
envelope test on the node's own validated run (open issue §16 of the
review note). The row for the LEASE is complete on both peers.

**Expected after.** No "unloaded" line for a model under lease while a
contribute runs; `/backend/shutdown` only for a model no process is
embedding with; a lease a killed process left gone within 600 s; an
on-demand encoder nobody asks unloaded within two beats of its last
answer, whatever its idle CPU. Cost
of the fix: one `INSERT OR REPLACE` per batch (seconds apart), one
SELECT per settle beat over a table of a handful of rows (plan-checked:
tests/test_embedder_release.py). To be measured on Perugia over one
contribute pass: shutdown count (expect 0 while embedding), chunks/min
(expect the same 142 or slightly better). On the node: cold starts in
contribute-gutenberg.log over one night's pass (expect 0, against 12).

### 2026-10-19 — the publisher: contribute's pass as a worker stage under its lock (07 §7.8 "all p2p") — PREPARED, not deployed

`worker._publisher` / `_publisher_beat` / `_publish_pass`;
`contribute.once(build=False)`; knobs `PUBLISH_BEAT_S` 300, `PUBLISH_BUDGET_S`
600. Operator's go, 2026-10-19 night ("tell me when I can run everything on
Perugia"). Review: the architecture session; the Perugia session measures.

**Why.** The worker holds a collection's writer lock for as long as it runs
it and `share contribute` needs the same lock, so a node published only
with the collection paused (the node: a cron CLI under `node pause
gutenberg`, 23:00–07:00; Perugia: the CLI loop with the worker in
acquire-only). "Run everything" on Perugia would have taken the lock, ended
the CLI loop and embedded led parts into local vectors nothing bundled.

**Scenario / before (Perugia, the CLI loop, 2026-10-19).** gutenberg
`embed = "off"`, worker acquire-only, `share contribute` (pid 524) on
vega-qwen3-embedding-4b over DXG: the a894a3f pass, 6,725 s, built 15,669
chunks in ~109 min of embedding ≈ **143 chunks/min**, 28 bundles (28
accepted, 0 refused, 28 announced), held 0, claimed 237, left_to_fetch 0.

**Expected after (Perugia under "run everything", gutenberg `embed =
"p2p"`, `share = true`, the CLI loop stopped).** The embed stage embeds
the led parts' documents by the worker's own batching (chunks/min to be
compared with the 143 of the CLI build); the publisher bundles each led
part as its vectors complete, within one `PUBLISH_BEAT_S` of the last
document, so coverage on the node's map rises in steps of a part rather
than at the end of a CLI pass. The publisher's own cost per beat: one
`once` pass with no build — `claimed_uids`/coverage reads, one records
read per probed part, sixteen challenge rows embedded per bundle — the
held path's measured costs (the row of the same day above). To be
measured on Perugia: chunks/min of the embed stage; minutes from a part's
last document to its bundle's announcement; the publisher's pass duration
and the budget hits ("interrupted at the budget" lines); duplicate bundles
(expect 0: the held path leaves out claimed rows).

**DEPLOYED on the node 2026-10-20 06:19:30 CEST (033efb7, München's
gate: 4,793 passed; staged tests 14 + 5 + 127).** The switch: cron 5
`set-embed.sh gutenberg p2p on-demand` at 06:00 and cron 6 the reverse at
22:30, both enabled; cron 9/10 (contribute + pause) disabled; gutenberg
not paused; no CLI contribute; the loader's effective mode under
`p2p`/`share = true` is `p2p`; today's one-off 08:00–17:00 window flips
gutenberg to p2p (and pubmed-v8 to all) in place of a contribute. BEFORE
for this row: the night above (196 chunks/min, 150 bundles). AFTER: the
first p2p night, with LocalAI's WatchDog lines.

**The node too** (operator, the same night): gutenberg goes from
`on-demand` + the 23:00–07:00 cron contribute under `node pause` to
`embed = "p2p"` under the worker, the cron retired. München's VRAM
check (22:4x CEST): the card holds 31.9 GiB; the 4B embedder resident
9,450 MiB; one RAG answer beside it peaks at 18.9 GiB (chat 7,441,
translator 6,424, reranker 1,664); LocalAI's watchdog threshold is 68 %
(21.7 GiB) and at 20:26Z it evicted the translator and an OpenVINO
embedder at 85 %. Embedder + chat + reranker ≈ 58 % fit; a non-English
Ask adds the translator and the 2026-10-14 pattern returns, softened.
MÜNCHEN'S CHOICE: the knob is WHEN gutenberg embeds — `embed = "p2p"`
22:30–06:00 and `on-demand` by day through the existing set-embed.sh
flip (cron 5/6 re-pointed; cron 9/10, the contribute start/stop and the
pause, retire); the mode stays run-everything, so acquisition, the
catalogue and the other collections run all day, and the quiet hours
hold. The publisher is therefore gated on `share`, not on `p2p`: the
parts the night finished leave in the morning. BEFORE from tonight's
contribute window on 0624b18 (chunks, minutes, chunks/min, bundles,
cold starts); AFTER from the first night on the publisher, and no part
bundled by both peers.

### 2026-10-20 — the bundle intake's hard split cuts by index (pitfall 18) — DEPLOYED 62a098d, measured

**Change.** `chunking._fixed_windows`: a paragraph longer than the window
is cut by index, not by re-slicing the remainder once per window; the
output is byte-identical (the old loop is the oracle in
tests/test_chunker_fixed.py). `land(cut_=…)`: the intake's one cut of
the part serves the ladder's T3 and the landing; it was made twice.

**Scenario.** Perugia (AMD RX 9060 XT, Docker Desktop over WSL2, the
leecher): gutenberg `embed = "p2p"`, `share = true`, run everything;
taking the node's bundles in while embedding its own led parts; the
ivf_pq index build (1,745 s) inside the window too.

**BEFORE (Perugia, 05:24–07:24Z on 033efb7/4e63103).** embed 58
documents, 4,406 chunks in 72 min = **61 chunks/min** (the CLI
contribute on the same card: 143); GPU GFX_ACTIVITY 0 % most of the
time; worker 98 % CPU, the busy thread `share-fetch` at ~20 user + 80
sys ticks/s, in `_fixed_windows` line 102 in both py-spy dumps; one
50-book part (38,273 chunks) from T0 warning to accepted **18 min 28 s**
(06:38:56 → 06:57:24); its twin, nothing to land, 30 s; gate waited
7,412 s / served 3,249 s; embed producer idle in `skip_test →
coverage.rows`. Here, on a Linux host: a 3.4 MB one-paragraph book
0.136 s and 1,737 minor faults before, 0.005 s and 449 after; identical
chunks.

**AFTER (Perugia, 08:55–09:55Z on 62a098d, worker up since 08:55:24Z, no
index build inside, heavy intake running the whole hour).** embed 8,305
chunks embedded + 15,446 reused = **138 chunks/min** (BEFORE 61; the CLI
143 — the stage is at CLI throughput while taking bundles in). Intake:
199 bundles accepted, 1,243,357 chunks landed 09:13:53–09:57:47Z, one
bundle every 10–15 s; 9 with a T0 WARNING (the envelope tie-breaker),
e.g. 64f89a08… judged → accepted **14 s** (warning 09:13:39, accepted
09:13:53, "0 landed, 18,518 compared: pass") against 18 min 28 s before.
Threads at ~10:00Z over 30 s while 7 bundles landed: the top four were
LanceDB's native `lance_backgroun` (1,736/1,633/1,229/987 user ticks,
14–30 sys — native writes that release the GIL); `share-fetch` not in the
top four, py-spy idle in `fetch_file` (network); sys time gone — no WSL2
page-fault storm. Gate 09:36 → 09:56Z: admitted 6,812 → 9,420, waited
+3,466 s, served +1,202 s — 1.7 s waited per 0.46 s served, the
concurrency-2 submission queue on a `parallel:1` encoder, not contention
(the stage is at CLI throughput). Publisher: 8 passes, 26 parts bundled
from vectors held here, all accepted, 0 refused; "left to the fetch" 251
→ 174. **Decision (architecture session):** the cut stays in the intake's
thread; moving it to the parse pool was the fallback if the fix left the
embed starved, and it did not. München's node, taking Perugia's bundles,
still owes its own AFTER line.

### 2026-10-20 — the node's gutenberg embeds p2p all day (operator) — asked of München

**Change.** Operator, 2026-10-20 ("queries are for testing only so far, no
production environment"): gutenberg on the node goes from `p2p` 22:30–06:00
and `on-demand` by day (cron 5/6, München's VRAM choice of 2026-10-19) to
`embed = "p2p"` all day; cron 5 and 6 disabled; cron 7/8 (pubmed-v8's
nights) untouched. The known cost is accepted: the 4B embedder (9,450 MiB)
resident all day beside chat (7,441) and the reranker (1,664) is 58 % of
the card; a non-English Ask adds the translator (6,424) and crosses
LocalAI's 68 % watchdog, which evicts models mid-answer (2026-10-14).

**Set 2026-10-20 10:18:57 CEST (08:18:57Z, München):** the file already
said `embed = "p2p"` since the 08:00 window; cron 5 and 6 disabled; the
one-off heavy-window2.sh stopped, so its 17:00 flips (gutenberg back to
on-demand, pubmed-v8 all → on-demand) will not run — pubmed-v8 stays
`all` until cron 8, both collections on the card this afternoon; cron 7/8
untouched.

**BEFORE.** The night rows above (contribute: 196 chunks/min, 150
bundles; the lease night) and today's one-off 08:00–17:00 p2p window.

**MEASURED, and ENDED by the operator.** p2p all day ran 08:18:57 →
13:09:54 CEST. The last clean hour (11:57–12:56): 11,914 chunks in 58.4
min = **204 chunks/min** on the node; Perugia's intake took the node's
bundles as fast as they came (its own 175 chunks/min beside, 93 % of the
vectors held by 12:10Z, "0 left to the fetch"). At 13:09:54 the operator
switched gutenberg to `on-demand` from the Library (one batch in the
following hour: 2,919 chunks for 5 documents a query touched), and at
14:xx confirmed it: **"München should stay silent now, on-demand only."**
The node's gutenberg stays `on-demand`; 751,795 chunks in 6,022 documents
(7 %) remain unembedded on both peers until queries touch them or the
operator says otherwise. Perugia's "slow fetch" of the afternoon was this:
nothing left to take, and nothing new made.
**AFTER.** The first full p2p day: chunks/min by hour, bundles published,
cold starts (expected 0: the embedder is held), WatchDog lines, and Ask
latency when one is asked.

### 2026-10-20 — the wikidata scan runs in the heavy windows (operator) — set, measuring

**Change.** The first streaming pass over the 20261012 dump (the pack and
the label table, resumable per 100,000 entities) runs only 22:30–06:00,
and today also until 17:00: TrueNAS cron 11 pauses the collection at 06:00
(`node pause wikidata`), cron 12 resumes it at 22:30, a one-off pause at
17:00 today. The architecture session had decided "all day" under the
operator's "no production environment"; the operator, asked by München,
kept the window: WINDOWS ONLY.

**Scenario.** The node, gutenberg `embed = "p2p"` all day beside it; the
dump already on disk (fetched and md5-checked 04:49Z, adopted by the fetch
at 11:00:30–11:04:50Z, nothing downloaded); the scan started by the
worker's discover at create, 12:57:05 CEST.

**BEFORE.** gutenberg 204 chunks/min (11,914 chunks, 11:57–12:56 CEST),
no scan.

**The scan.** 1,600,000 entities in 12 min = ~133,000 entities/min, one
core (~92 % CPU), a part every 40–60 s, the first parts the largest
(00000 357 MB, 00015 79 MB); 1.7 GB of pack after 16 parts. At ~120 M
entities: ~15 h — today's window plus two nights. gutenberg's rate
during the scan is confounded by the DAR measurement on the card; a clean
figure follows.

**Windows lifted (operator, 2026-10-20, later: "München can do the
wikidata cpu heavy lifting").** The scan runs continuously on the node
from now to its end — ~4 h of it done in today's window, ~11 h left at
133,000 entities/min — and the CPU steps after it (records build, first
snapshot, the gate's checks) run on the node too; crons 11 and 12 and the
17:00 one-off go. gutenberg stays on-demand.

**MEASURED (München, 15:06:48 CEST).** Crons 11/12 disabled (not deleted:
a flip back is one command), the 17:00 one-off cancelled; wikidata was
never paused (only pubmed is). Position: 167 parts × 100,000 =
16,700,000 entities, 5,753,591 pruned (34 %), pack 15 GB on tank
(labels.sqlite 1.10 GB + 0.32 GB WAL). Rate 16.7 M in 129.7 min since
12:57:05 = **~128,800 entities/min**, steady against the first 12 min's
133,000. ETA at ~120 M entities (the May dump's count; this dump's total
is known only at the end): ~103 M left ≈ 13.4 h → **about 04:30 CEST,
21 Oct**. The pack grows ~7 GB/h, so ~110 GB at the end; tank/louise-
corpora 790 G used, 7.32 T free. Still owed: df at the end, the build's
and the snapshot's wall clock. The clean gutenberg p2p figure does not
exist: the DAR measurement sat on the card 12:57–13:2x and gutenberg went
on-demand at 13:09:54; the only clean number is the BEFORE, 204
chunks/min (11:57–12:56).

**The pass on every core (München, 3781e8f + eb74d95, operator: "squeeze
everything from the cpu").** `pack.build(workers=)`: this process
decompresses and cuts lines (lbzip2 on the PATH, else bz2), children at
nice 10 parse and write whole chunks, this process commits in chunk
order; files, labels and the resume point byte for byte the serial
pass's (tests at four chunk sizes); a failed or hung child stops the pass
at the last commit and the process goes serial. Measured on the node,
first 120,000 entities (3,517 MB of JSON) beside the live scan: serial
43.5 MB/s; 11 processes with bz2 68.5 MB/s; 11 processes + lbzip2
**195.3 MB/s (6,663 entities/s)**; the live serial scan does ~29 MB/s.
Plan: gate eb74d95 on a gate image rebuilt from the new Dockerfile,
deploy on the node at once (supersedes "at the scan's end"); the pass
resumes at the last committed chunk (≈ entity 22–25 M). Owed: the rate
after the deploy and the new ETA.

**DEPLOYED on the node 17:17:38 CEST (678450f, image sha256:1416795f…,
gate PASSED 4,876 / 1 skipped).** The pack resumed at chunk 344 (34.4 M
entities); `lbzip2 -n 11` at ~860 % CPU through the skip phase, the box
~99 % busy, mostly nice'd. Rate and ETA follow once new chunks commit.
Same deploy, the publisher: the sweep set 27 refused own copies aside
(27, not 26: one more refused row elsewhere); served dirs now en/00219 2,
en/00043 4. The seeder's sync then deleted the set-aside tree
(state/share is the seeder's) — cf430b5 moves it to state/refused-bundles.

**Measured after the deploy (München).** 530,000 entities/min at 17:43
CEST (39.9 M of ~120 M), ~730,000/min over 17:43–17:51 (45.7 M): 4.1–5.7×
the serial 128,800. End of the scan expected ~19:30 CEST. Then the deploy
of 8710eed (PASSED, 4,882 / 1 skipped: the wikidata pack on
`derived.place`, LOUISE_CACHE_DIR=/data/cache on the node's NVMe,
refused-bundles, the union cache) and phase 2 with the label table on the
fast drive — owed: the adoption's wall clock for the ~60 GB pack and phase
2's entities/s a core against 133–179 on tank.

### 2026-10-20 — a peer leads only where it embeds and shares; a switch releases its parts (operator) — PREPARED

**Change.** The digest carries a space's benchmark only for collections
with `embed = p2p | all` and `share = true` (gossip.leads); a neighbour
whose digest offers no member of the collection is not scored (coverage
.live_peers); a mode or sharing switch in the Library asks the gossip
beat for a digest at once (node_state.request_digest) instead of at the
hour. 04 §4.5, 07 §7.8, 09 §9.9 amended. Operator's rule: "switching
embed mode from p2p to something else shall release the p2p embedding
partial job of a peer".

**MEASURED BEFORE (München, `hold vectors show gutenberg`, ~14:25 CEST;
Perugia 12:25Z).** 1,259 open parts by union on the node. Leaders: the
node 485 parts / 247,720 open chunks — on-demand since 13:09:54, so they
stall; Perugia 419 / 229,840 — embedding at 283 chunks/min (20 min);
9f7c969c… (louise-peer2 on the dev PC, embed off, share off, older code)
355 / 224,603 — it has produced no bundle ever and never will; its digest
carries a 1,477 tokens/s benchmark, which is why it leads. Without the
change these 840 parts wait `grace_days` (7) for the cascade.

**EXPECTED AFTER.** Once deployed on the node and Perugia: peer2 is
dropped from both live sets at their next recomputation (it offers no
member); the node's digest carries no gutenberg benchmark (on-demand), so
Perugia leads every open part — about 700,000 chunks, ~40 h at its 283
chunks/min — and the node takes Perugia's bundles in. AFTER numbers owed:
Perugia's led parts and chunks/min the hour after the deploy; the node's
intake of Perugia's bundles.

### 2026-10-20 — the bounty list has no leader: the embed stage walks the open parts by rank (operator) — PREPARED

**Change.** 04 §4.5 amended at the operator's word ("for the bounty list
THERE IS NO LEADER"): any peer may embed any open part; rendezvous gives
each peer the ORDER (rank 1 parts, then rank 2, …); the stage widens to
the next rank when a round finds nothing at the current one
(`leading.widen`, called by `_backfill_rows` under p2p), and starts again
at rank 1 when the live set or the coverage changes; a part is left aside
only while another producer is visibly on it (a bundle of theirs within
`3 × tokens / w`, `contribute.visibly_on`). The `grace_days` cascade on
embedding is gone (`_ranking_leader` is the first of the ordering,
information for `hold vectors show` and the publisher).

**MEASURED BEFORE (Perugia 12:25Z).** 1,062 open parts in its view: 424
its own at rank 1 (250,908 chunks pending), 638 the node's — the node on
on-demand since 13:09:54 CEST, so they wait `grace_days` = 7; the stage
skipped 3,244 of 5,994 unembedded documents as "led elsewhere"; Perugia
embedding 283 chunks/min at full GPU with ~2,750 documents of its own
left, i.e. hours from idling beside 600,000 open chunks.

**EXPECTED AFTER.** Perugia exhausts rank 1, widens, and continues on the
node's parts without a pause; the node (on-demand) and peer2 (no
benchmark, no member offered) are not in the ordering at all after the
same deploy. Owed: Perugia's chunks/min and "led parts await" over the
hour after the deploy, and the count of parts where two producers'
bundles meet (the duplicates-as-checks budget, 03 §3.6: under a tenth).

### 2026-10-20 — two jobs at a time: the embed stage works the first two parts of its order (operator) — DEPLOYED on Perugia 13:18Z (cfab45e), measuring

**Change.** Operator, 2026-10-20: "You can take up to 2 jobs at a time, it
means that upon completion of one you start the second having the time to
get another job (no idle between jobs)". 04 §4.5 amended. The rank walk of
the row above becomes a JOB WINDOW: `contribute.job_order` lists the open
parts this peer may take in the order it takes them (rank 1 first, within
a rank by its own score, highest first; parts another producer is visibly
on are not in it), `leading.build(order=…, start=…)` admits
`order[start:start + leading.JOBS]` (JOBS = 2) and leaves every other open
part for now; `leading.widen` — called by `_backfill_rows` when a round
finds nothing under p2p — moves the window down the order by JOBS; a
bundle accepted (a job done), a digest or the hour rebuilds the view and
the window starts again at the head. So while part A embeds, part B is
admitted beside it; when A's last document is done the walk continues
into B in the same round, and the next build (A's bundle accepted, A
closed) admits C beside B — the card never idles between jobs, and the
other peers see this one on at most two parts, instead of on every part
of its rank at once (the row above: 424 rank-1 parts admitted together,
walked in id order, so a second peer could only ever meet Perugia on the
part its walk was in).

**MEASURED BEFORE (Perugia 12:25Z, the row above).** 1,062 open parts,
424 at rank 1, 283 chunks/min at full GPU; the stage admitted all 424 at
once. Not yet measured: the rank walk of 99241f9 is gated, not deployed.

**EXPECTED AFTER.** The same chunks/min (the GPU is the bound, not the
selection — the window changes WHICH documents the backfill walk returns,
not how many); zero idle rounds between parts; "led parts await" counts
the parts beyond the window; two producers' bundles meet on at most the
parts a peer entered after the other's last digest. Owed: Perugia's
chunks/min, idle rounds and the duplicates count over the hour after the
deploy, and the node's `hold vectors show` leader split.

**DEPLOYED (Perugia, 13:18Z / 15:18 CEST, cfab45e — PASSED by München,
4,862 tests, 18 min 30 s; it carries 99241f9, 8cf5ead, cf6365e, 6578544
too).** Smoke: 0 failures, bit-identical live. First round 13:19:08:
"embedding 20 document(s), 2 submitted and 1 prepared at a time", rows
returned, no stall. `hold vectors show gutenberg`: 1,249 open parts,
covered 9,783,255 of 10,477,214 chunks; leader field node 721 / Perugia
528 — the node still in the ordering until its own deploy (at the scan's
end, ~04:30 CEST), which under the walk only orders Perugia's parts. The
hour's numbers (chunks/min against 283, rounds and idle rounds, parts
where two producers' bundles meet, "led parts await") due ~14:20Z.

**AFTER (Perugia, 13:18–14:18Z, read 14:18:59Z).** 8,496 chunks
embedded, 0 reused, **141.6 chunks/min** — the clean BEFORE is 138
(62a098d) and 143 (the CLI), not 283, which counted the reused-heavy
rounds right after `dev mark-embedded`: the rule costs nothing. 18
rounds, selections 1–50 documents, **0 idle rounds**, 9 "led elsewhere"
lines, 0 errors. Publisher every ~6.7 min: 2 parts bundled per pass, all
accepted and announced, "3 led part(s) await the embed stage" steady.
Duplicates: 23 bundles on 22 parts, every one a completion of a part the
node has bundles on; counting the node's copy once, 21 of 22 parts are
disjoint (en/00267: node 6,013 + Perugia 153 + 328 = union 6,494). Open
parts 1,237 (1,249 at 13:19Z), covered 9,785,118 of 10,477,214; leader
field node 721 / Perugia 516. **en/01192, resolved the same afternoon**:
a node bundle of 6,924 chunks accepted on Perugia on 2026-10-19's compare
path had no files; the union read 963 of 7,676 (Perugia's own re-signing
of the node's rows) and the walk would have re-embedded 6,713 chunks the
node holds. c993199 takes such a bundle again (Perugia 15:16Z: 60705a18
brought 5,570 chunks it did not hold), e688b89 lets the union cache see
the files arrive. **Second hour (Perugia, 15:15–16:15Z, 678450f):** 8,015
chunks, **133.6 chunks/min**, 2 bundles taken again, 0 errors; publisher
2 and 4 parts a pass, "6 led part(s) await the embed stage", 0 left to
the fetch. Union after the recompute (16:23Z): en/01192 6,931 (963 before;
the 956 shared chunks are Perugia's OWN embedding of chunks the node also
embedded — 0 of 972 rows bit-identical, cosine min 0.997850 — not
re-signed rows: the fileless row's cost in GPU time, a check),
en/00689 7,094 (7,057), en/00043 29,097 and en/00219 41,924 unchanged;
no part with an unreadable bundle left — the two "present but
unreadable" were stale entries. 8710eed deployed on Perugia 16:15Z, no
cache folder (all its drives are NVMe, operator), nothing in
state/refused-bundles.

### 2026-10-22 — gutenberg's scan signature covers the record's files (9ed5ed2)

**Change.** A book directory's cursor entry now also records the files that
decide its record — pg<id>.rdf and the form file `pick_form` chose — with
their (mtime_ns, size), so a file rewritten IN PLACE (the directory's mtime
unmoved) is seen by the next scan. Two stats more per book per scan. An
entry written before is re-examined once.

**Measured on the node** (München, read-only: `GutenbergSource.discover`
over the live origin, 79,732 book directories, against a copy of the
cursor in a scratch state dir; nothing written to a manifest):
- BEFORE (1652337, the directory alone): 3.3 s and 2.7 s a steady-state
  pass, 0 books re-examined (13.1 s cold, 2.2 s warm earlier the same day).
- AFTER, the conversion (once per peer, the first scan after the deploy):
  every book re-examined, its RDF parsed for `lang` — 1,183.5 s for the
  last 17,981 books of a run interrupted by the switch's rename, so ~70 min
  for the whole corpus at this pool's load.
- AFTER, steady state: 3.9, 3.9, 3.9, 3.8 s, 0 books re-examined. One pass
  right after the conversion re-examined 287 (staged at the conversion's
  end, not yet promoted); every later pass 0. **+0.6–1.2 s a pass.**

### 2026-10-23 — a vector index refresh writes a delta; the deltas merge past four — PREPARED

**Why.** normattiva's maintainer logged nothing 01:00–06:00 on 10-22/23; the
one refresh line of 10-22 read "196048 unindexed rows folded in (20838s)",
and the night's prune and compaction never got a turn (35k versions, 22,683
small fragments).

**Before** (München, ZFS clones of normattiva's table at 13:27, normattiva
paused, 4 cpus / 12 GiB per container, the node otherwise busy with the
records build and the worker): 8,178,785 rows, 22,691 fragments (22,683
under 10k rows), 35,172 versions, IVF_PQ 713 partitions, 591,591 unindexed.
- `optimize_indices()` (the deployed call): **404.9 s** (~0.68 ms a row);
  partitions 713 → 719 — the NEWEST index, here the base, rewritten.
- `optimize_indices(num_indices_to_merge=0)`: **93.1 s** (4.3×).
- `compact_files` 30.4 s (22,685 → 5 fragments); `cleanup_old_versions`
  887.3 s (35,174 versions, 78 GB of the table's 82 GB).
- The night itself: load 11.6 on 12 cores at 01:46, the maintainer at
  nice 10 — the 20,838 s was this call starved, not a hang.

**Measured here** (lance 9.0.1, IVF_PQ 512 partitions, dim 768): a query
(nprobes 30, refine 30) 24 ms with 1–5 indices, 36 ms at 9, 41 ms at 13;
merging four deltas 0.4 s over a 300k or a 900k base alike, merging the
base too 0.7 / 1.1 s. A no-argument `optimize_indices()` over 13 indices
merged none of them (0.4 s, 13 left).

**Change.** `refresh` calls `optimize_indices(num_indices_to_merge=0)` —
a delta over the new rows — and, past `VECTOR_INDEX_MAX_INDICES` (4), merges
the deltas (`indices - 1`), never the base. The log line names the index
count and a merge.

**After** (to fill in on the node): the first nightly refresh lines of
normattiva — seconds per refresh and rows folded in, the index count, and
whether prune ("pruned …") and compaction ("compaction started …") get
their turns the same night. Scenario to record with it: which collections
embed (normattiva p2p at night?), the node mode, the load average.

**Compaction first — measured, NOT built** (München, one clone made at
15:02 and destroyed at 15:03:48, the same 591,591-row backlog):
`compact_files` 22.9 s (22,685 → 4 fragments), then the delta 76.6 s —
99.5 s in all against 93.1 s for the delta alone. The delta is 18 % faster
on compacted fragments, the pair is not: the order stays index first,
fragments in the same pass (2026-10-14). Compaction's value is its own —
delete memory, scans, and the versions it lets cleanup drop (78 GB here).

**Still open:** the maintainer's nice under a saturated CPU, if the nights
after this deploy still show the refresh starved.

### 2026-10-24 — the records build streams its pages; the parse pool is sized from the cpu quota — PREPARED, two steps

**The defect, measured (München, py-spy 18:15–18:20 CEST, read-only).**
`hold records build wikipedia-v8` in its own container, 4-cpu quota
(`cpu.max` 400000/100000), nice 19, the deployed image. `os.cpu_count()` 12,
so `PARSE_PROCESSES` defaulted to 11: 11 children in 4 cpus. docker stats
every 8 s over 96 s: mean ~132 % (1.3 of 4 cpus). py-spy record 120 s,
`--subprocesses --idle`, 89,973 samples: 47.8 % children idle waiting for
work; per child, 7–45 % of its time parsing, two children doing most of it;
the parent 100 % in the wait for the page. Cause: `pack_slices` hands
`parallel.compute` ONE page of ≤500 documents and waits for its slowest part
before reading the next, and the batch deadline (`PARSE_TIMEOUT_S` 120 s over
the WHOLE page) threw 15 pages away in 18 h and parsed them again serially.
Rate this run: 8.15 M records in 18.8 h, ~120 rec/s (an en slice of ~300 k:
~185 rec/s; en/0M 5 h).

**Scenario.** wikipedia-v8's records build alone in its container (the
worker's other collections in theirs); slices de, en finished to
en/pages=27M; the rest of en, then es, fa, fr, it, ja, pt, ru, tr, zh.
`chunks/doc` not yet recorded for the window — read it from the build log's
part entries (`chunks` / `records`) before and after, per rule 3.

**The changes, ONE per step (scheduling.md §6, 8):**
1. *The pool size* — `config.available_cpus()`: affinity and the cgroup quota
   (`cpu.max`, v1 `cfs_quota_us`), as the JVM, Go (automaxprocs) and .NET
   read it; `PARSE_PROCESSES` defaults to it − 1 (3 here). On the running
   image the same step is `PARSE_PROCESSES=3` in the build container's env:
   no deploy, measured first.
2. *The page stream* — `parallel.compute_pages`: page k+1's parts are
   submitted while page k is with the caller (window kept full across
   pages, the admission budget unchanged, parts still cut by `work_span`);
   the timeout bounds the wait for the NEXT answer (no part answered for
   `PARSE_TIMEOUT_S`), and on a timeout only the unanswered parts are
   computed in-process. Cost tests: tests/test_parallel.py (the next page
   submitted before the caller takes this one; a stalled part costs only
   itself). Byte-identical parts: `test_pack_records_are_computed_by_the_pool`.

**Expected, unmeasured:** step 1 removes the oversubscription (the straggler
gets a whole cpu, the timeouts go); step 2 keeps the 3 children fed — together
~1.3 → ~3.5–4 cpus, 2.5–3× on the remaining ~61 M records.

**The before numbers were contended** (München, 18:40 CEST): the profile and the
hour before it ran while the wikidata pack's version-3 upgrade held 6–7 of
the 12 cores (17:13–18:35 CEST): 108.5 rec/s, 252 chunks/s, 2.32 chunks/doc.
Right after it ended (18:36 CEST, 208 s, 51 parts): 479 rec/s, 2,361 chunks/s,
4.93 chunks/doc, docker stats 100–418 %, with stretches at exactly ~100 %
(one core: the page barrier). So the BEFORE is a clean hour of en,
18:40–19:40 CEST, nothing else heavy; then step 1 (19:40–20:40, PARSE_PROCESSES=3),
then step 2 (a833c1e, 20:45–21:45), all normalised by chunks.

**BASELINE, clean (18:40–19:40 CEST; deployed image, PARSE_PROCESSES 11,
nothing else heavy).** docker stats ×12: mean 232 % (100–391; three readings
at exactly ~100 %). 382 parts, 759,764 records, 2,888,004 chunks, 3.80
chunks/doc, 230.7 rec/s, 877 chunks/s; no "[parse pool]" line. By slice:
en/28M 1.03 chunks/doc, 161 rec/s; en/29M 1.93, 233 rec/s; en/2M's first
236 k records 8.63, 357 rec/s, 3,080 chunks/s. CPU per chunk: 232 % × 3,600 s
/ 2,888,004 = 2.9 ms.

**STEP 1 (19:41–20:41 CEST; same image, PARSE_PROCESSES=3).** Mean 221 %
(101–309). 127 parts, all en/2M's next 252 k records, 689,593 chunks, 2.74
chunks/doc, 76.6 rec/s, 210 chunks/s; no "[parse pool]" line. CPU per chunk
11.5 ms — FOUR times the baseline's, on the same slice: the cost follows the
wikitext (templates, tables, refs), not the chunks, so rule 3's unit does
not normalise this build and the two hours are not comparable on
throughput. What IS comparable is the utilisation: 3 children held the same
~2.2 cpus as 11, and neither hour timed out — once the node was quiet the
oversubscription was not what capped it (it was under contention, before).
The ~100 % phases (one core: the page barrier, the serial parent) recur in
both: step 2's target.

**The fair A/B (agreed with München):** the same page range built twice in a
scratch build dir, the deployed tree and a833c1e, CPU only — wall time,
CPU-s per record and the utilisation on IDENTICAL records. That, not an hour
of a different stretch, decides step 2.

**STEP 2 (a833c1e tree, 20:41:55–21:41:55 CEST, PARSE_PROCESSES default
= quota − 1 = 3).** docker stats ×12: 300 301 302 300 194 300 308 302 307 163
301 100 → mean 265 % (100–308); readings at ~100 % (one core): 1/12 (baseline
3/12, step 1 2/12). 355 parts in en/30M–32M, 705,733 records, 1,971,340
chunks, 2.79 chunks/doc, 214.1 rec/s, 598 chunks/s; no "[parse pool]" line.
CPU-s per record: baseline 11.0 ms, step 1 31.6 ms (en/2M's heavy stretch),
step 2 13.5 ms — content-bound, not comparable across steps. Utilisation
232 % → 221 % → 265 %, and flat at the pool's ceiling (3 children) in step 2:
the stream keeps the children fed, the barrier pauses are gone, the
parent's own share is small. One cpu of the 4 stays mostly idle.

**Byte identity, fixtures (architecture, 2026-10-24).** The live staging
holds parts cut by c3a7612 (to 20:41) and by a833c1e (from en/30M on). The
same 700-record corpus (gutenberg-mini's three books ×, sizes varied so
children finish out of order), records_per_shard 60, 3 real children,
built under each tree: 12 parts, sha256 identical. Not the proof on
wikipedia's own records — the A/B's byte check is — but the paths the two
trees differ in (pack_slices' page stream, slice_of) cut the same bytes.

**Next, step 3 — the 4th child, in the A/B:** the A/B adds a third arm,
a833c1e with PARSE_PROCESSES=4, on the same 250 k records. If it holds
~400 % with no "[parse pool]" line and a lower wall time than arm 2, the
records build's default becomes the whole quota (the build's parent only
feeds and writes); the node worker keeps quota − 1 (its parent runs the
round loop and the API). That change ships with these numbers in its
commit, not before.

**THE A/B (München, 2026-10-25; origin/main e330ed5,
docs/notes/records-ab-2026-10-25/run2-*.json).** en/2M's first 250,000
records, 2,096,112 chunks, 125 parts, 4-cpu quota, nice 19, the live build
stopped:

| arm | wall s | cpu s | cpu s/record | mean util | one-core readings | min–max % |
|---|---|---|---|---|---|---|
| c3a7612 (11 children) | 761.2 | 2015.0 | 0.00806 | 265 % | 6 of 95 | 100–355 |
| a833c1e (3 children) | 629.5 | 1775.5 | 0.00710 | 282 % | 0 of 78 | 149–307 |
| a833c1e, PARSE_PROCESSES=4 | 543.8 | 1824.7 | 0.00730 | 336 % | 0 of 67 | 132–400 |
| c3dcb4c (3 children) | 622.9 | 1767.3 | 0.00707 | 284 % | 0 of 77 | 176–308 |

The parts (n, sha256, records_root, chunks_root) were identical in all
seven runs (these four and the first run's three), and no arm logged a
"[parse pool]" line.
- Step 2 is 1.21× faster than the old pool on wall time, at 12 % less CPU
  per record.
- c3dcb4c costs nothing against it.
- The 4th child is 1.16× faster on wall time at +3 % CPU per record. It
  meets two of the three rules (lower wall, no fallback). The third,
  ~400 %, it misses: it averaged 336 %, peaking at 400.

DECIDED (architecture): the 4th child is taken.
`config.build_parse_processes()` gives `hold records build` the whole
quota unless PARSE_PROCESSES is set; the worker keeps quota − 1.

The first A/B run (11:17–12:50) is VOID for speed. Its harness reached the
container on stdin, so the spawned children found no main file and
disabled themselves, and every arm ran on one core. Its parts still count
for the byte check.


### 2026-10-25 — query processing highest, corpus embedding lowest, a query's later step first (operator) — PREPARED

**Operator:** "Embedding shall have the lower priority and query processing
the highest; the subsequent steps of a query in process shall be processed
first to avoid wasting the effort spent for the previous steps."

**Change (app/rage/dispatch.py, app/embeddings.py):**
- The levels are a query's steps in order: CORPUS < TRANSLATE < QUERY
  (embedding) < RERANK < CHAT (the answer). The 2026-09-19 order put query
  embedding on top and chat below the rerank. Where a query's steps meet at
  one gate, the one further along goes first.
- Corpus work stands aside ON THE WHOLE CARD while any step of a person's
  query holds the node's claim. Its next request, and the warm that may load
  a corpus encoder, wait for the claim to clear. A request already sent
  finishes. The wait is bounded by the request's own timeout (then a
  deferral, nothing on the document) and wakes on stop.
- The 2026-09-20 removal of this yield was for chat's clock (87–92 s at
  990 MHz alone). The GPU governor now holds the clock for the claim's
  lifetime; pinned, standing aside measured 64.2 s against 67.5 s.

**BEFORE (the night of 2026-10-24, München).** Scenario, corrected
2026-10-25 08:45: normattiva embedded NOTHING (embed off, nothing left). The
corpus work on vega-qwen3-embedding-4b was GUTENBERG's on-demand embedding,
of the books the campaign's own searches touched: 65 documents, 6,556 chunks
embedded and 6,330 reused, ~10.0 M tokens, EMBED_BATCH 1 at concurrency 2,
7,028 requests served in 3,035 s (0.43 s each). Also running: the
campaign's run 5 tail and the heading full search (3 arms, reranker on) from
23:04, and the records build on CPU. The GPU was at 85–86.5 % VRAM at the
reclaimer's 4 fires.
- /v1/rerank (louise-rerank-bge, parallel:1): 5,662 × 200, **130 × 500
  (2.2 %)**, all between 23:00 and 03:00 CEST. None by day (50 calls, one
  test beside 16 embedding batches).
- Queries losing their rerank: **19 % of first searches** (95/496), 3 % of
  repeats.
- Live searches: t_rerank **30.4 s** (20:05) and **42.6 s** (00:06; that
  minute normattiva sent 86 embedding calls, and 2 of the 5 reranks were
  refused).

**AFTER (to fill), same scenario:**
- the per-call 500 rate (the harness logs every refused call with LocalAI's
  body, d6ddc85);
- rerank ms per call;
- per-query misses;
- live t_rerank;
- the on-demand corpus work's documents/hour while the campaign runs: what
  corpus pays, since its requests now wait whenever a query step is live.

**AFTER (München, the night of 2026-10-27/28, 22:30–04:14; pass 3,
`/root/heading-full-results-3.jsonl`).** NOT the same scenario. The GPU was
at 230 W (the operator's order of 21:31), not 85 W. Gutenberg embedded
"all" at concurrency 2, not on demand. Normattiva embedded (0.90 chunks/s at
23:35), and wikidata's parse and the wikipedia-v8 records build shared the
CPU.
- /v1/rerank: 6,623 × 200, **14 × 500 (0.21 %)**, against 2.2 % (LocalAI
  log, 21:30Z–03:15Z).
- Queries losing their rerank on the first attempt: **9 of 496 at arm 0
  (1.8 %)**, against 95 (19 %). Counting any arm, 13 (2.6 %) against 117
  (23.6 %). All 13 are the harness's 14.4 s timeout, not a 500. After the
  retry pass, 1 is left (WQ276015/zh/284).
- Search time, arm 0, whole search: median **9.9 s**, p90 12.8 s, against
  6.9 s and 13.5 s. Rerank ms per call: not logged by the harness or by
  LocalAI's request line.
- Live t_rerank: none, because nobody searched that night.
- What corpus pays: gutenberg embedded 131 documents (0.56 chunks/s,
  **22.8 docs/h**) during the pass, and 139 (2.43 chunks/s, **79.4
  docs/h**) in the 1 h 45 min after it, with the same profile and level.
  While a campaign runs, corpus keeps about 30 % of its rate.

**Claim per step, not per query (operator, 2026-10-25: "subsequent steps
first does not mean keep the gpu unloaded for a long time").** Measured
the same morning, the deployed path with every HTTP call timed, 3 gutenberg
searches and 1 Ask:
- translation: chat 0.3–1.3 s, MADLAD 0.5–1.3 s;
- gap (a), the text legs: 1.0–2.3 s;
- query embedding: 0.08–0.1 s warm (4.4 s once, cold);
- gap (b), dense search, fusion, attach, tokenize: 0.7–1.1 s;
- 3–6 reranks of 0.3–4.4 s, 9–37 ms apart;
- in Ask, gap (c) of 2.2 s before a 5.2 s answer.
The gaps total 3.4–4.1 s per query of 11.6–16.7 s. A corpus request (one
text, 0.43 s) that starts in a gap shares the card with the next step for
at most ~0.43 s, or ~0.9 s for the two in flight. A whole-query claim
(04b4f50) would have idled the card for the gaps and deferred corpus work
for the whole query. Reverted (1b8d8cc): corpus stands aside while a step
runs and fills the gaps between steps.

Deployed together with the one-retry on a 5xx (b573be4). The per-call 500
rate is the priority's alone, because the harness counts every POST,
retried or not.

### 2026-10-25 — the parse pool keeps its children, and a stopped build leaves — PREPARED

**The defect (München, 2026-10-25 10:58–11:15 CET, read-only py-spy).**
wikipedia-v8's live build, on a833c1e's tree, did not exit on SIGTERM. Its
part was committed (en/60M part 103, 10:58:45) and main had returned. The
main thread was joining two pool manager threads in `_python_exit`, the two
queue feeders were blocked in `_send`, and no parse child was alive. It was
SIGKILLed at 11:15, with nothing lost. The causes are scheduling.md §5,
pitfall 19: CPython gh-115634 (a 3.12 pool loses the children that retire at
`max_tasks_per_child`), and a pool thrown away with `shutdown(wait=False)`
that the exit then joins with no bound.

**Reproduced here (3.12.3 against 3.13.16, which carries the upstream
fix):**
- 8 parts on 2 children retiring after 2 tasks: 3.12 answered 4 and had 0
  children left; 3.13 answered all 8;
- the watchdog's `shutdown(wait=False, cancel_futures=True)` and then a
  normal exit: 3.12 was still there after 30 s, 3.13 left in 0.1 s;
- a stream whose children retire after every part, stopped: with the
  pool shut down before its children were terminated (3.14's order), 2
  exits in 8 hung on 3.12; terminated first, 0 in 12 on 3.12 and 0 in 12
  on 3.13.

**The change (one):** `parallel._Executor` (the gh-115634 backport),
`parallel.discard` (stop a pool thrown away), and the exit stopping the
pool first. A hung wikidata pack pool is discarded the same way, and the
build's log lines are flushed.

**BEFORE (München, from build-night.log, which has no per-line timestamps,
so the counts are per run).** Every one of the 34 lines is "[parse pool]
TimeoutError: computing this batch in-process":
- the c3a7612 image, `PARSE_PROCESSES` 11 (2026-10-24 23:25 → 10-25 07:00):
  15 lines in 1,409 part lines;
- step 1 (`PARSE_PROCESSES` 3, one hour): 0 lines in 74 part lines;
- the a833c1e run (~14.3 h, up to the hung stop): 4 lines in 1,271 part
  lines, and it ENDED with no child in the container;
- earlier runs: 9, 3 and 3 lines.
A/B arm 1 (the c3a7612 image) did 250,000 records, 2,096,112 chunks and 125
parts in 1,865.5 s wall for 1,859.7 cpu-s: one core of four, which is the
serial in-process path a pool without children falls back to.

**Check before it goes live (München):** arm 4, c3dcb4c's tree on the same
250 k records. Its parts (n, sha256, records_root, chunks_root) must be
identical to arms 1–3; until they are, the build stays held.

**Scenario for AFTER:** the live wikipedia-v8 build in its container, 4-cpu
quota, `PARSE_PROCESSES` 3, `PARSE_TASKS_PER_CHILD` 500, after the A/B arms.

**AFTER (to fill):**
- "[parse pool]" fallback lines per hour (BEFORE: count them in the build
  log since a833c1e);
- live parse children every 60 s (expected: 3 throughout);
- the time from `wikipedia-build.sh stop` to exit (BEFORE: never; SIGKILL
  after 15 min);
- rec/s and chunks/s against step 1's hour, on comparable slices (rule 3).

**AFTER so far (München):** the live build restarted on c3dcb4c at 13:37:36
CET. The sampler reads 3 children at ~300 % (13:40, 13:41). The A/B arm
on c3dcb4c: no "[parse pool]" line, 622.9 s, parts byte-identical.

### 2026-10-25 — the build uses every core; the page stream refills on any answer (operator) — DEPLOYED on the build 15:20 CET

**The operator:** "Use all the cores", then "Fix it, we are wasting CPU time."

**BEFORE (München).** The build container ran at `--cpus 12`, `PARSE_PROCESSES` 12, on c3dcb4c: 223 rec/s. The cpu sat at one core (99–100 %) in many 60 s samples between 400 and 1,180 %. py-spy `--gil` on the parent showed the GIL held ~8 % of the time, so the parent's Python was not the limit. `compute_pages` was:
- a part that had answered held its slot until its page was consumed in order;
- the loop waited on the OLDEST part alone, refilling nothing meanwhile;
- it read at most three pages ahead.
So one long article in the oldest page left 11 children idle.

**The change (21da0e3, München; reviewed and merged by architecture).** The window counts only RUNNING parts. Any answer returns the loop to the top, which refills. The watchdog deadline is reset by any answer and after each yield. `PAGES_AHEAD` is 16, the memory bound. Results stay in page and part order. The cost test `test_one_slow_part_does_not_hold_back_the_pages_after_it` fails on the old code and passes now.

**A/B, 12 cpus, the same 250 k records** (parts identical to the earlier five runs):

| arm | wall | cpu s | cpu s/record | mean util | one-core readings |
|---|---|---|---|---|---|
| c3dcb4c, 12 children | 408.5 s | 2830 | 0.0113 | 693 % | 1 of 51 |
| 21da0e3, 12 children | 334.2 s | 3324 | 0.0133 | 995 % | 1 of 41 |

On 4 cpus the arms were 544–630 s. Live since 15:20: 572 rec/s and 1,750 chunks/s over the first 5 minutes, against the morning's 223 rec/s and 322 chunks/s on a different stretch. CPU per record is higher at 12 children (SMT siblings, memory bandwidth). The operator asked for throughput.

**Note (review).** A part that answered and waits for its page's turn still counts in `_held` and in the occupancy meter until consumed. That is harmless for the build, which is alone in its container, and `compute_pages` is not used by the worker. Revisit if it ever is.

### 2026-10-28 — an idle parse pool gives its memory back — PREPARED

**BEFORE (München, 17:26–17:42 CET).** The worker at 15.6–15.7 GiB and
0.65 % CPU: 12 children of the parse pool (11 workers and the resource
tracker), 10.8 GiB RSS together, ~1.0 GiB each, idle since the quiet profile
stopped parsing at 17:00 (cpu_cores 0, at the end of its unit) and through a
node pause at 17:41. They would have stayed until 22:30: the pool lives for
the process's life (`app/parallel.py` `pool()`, created on first use). The
node (61 GB, ARC ~16 GB) had 18 GB available, and wikipedia-v8's first
snapshot could not start; a worker restart in paused mode gave 0.5 GiB.

**SCENARIO.** Node running then paused; wikidata parsing and gutenberg
embedding before 17:00; quiet profile from 17:00.

**The change.** `parallel.release_idle(PARSE_IDLE_RELEASE_S)` on the beat
thread, beside the shares line: when nobody has been inside the pool for
600 s (no batch in `compute`/`compute_pages`, no slot held, no `pool()`
call), it is stopped (`discard`) and the beat logs `parse pool released: N
children idle 600 s`. The next parse starts a fresh pool. loky's reusable
executor does the same with its `timeout` (300 s by default); the pool stays
the standard library's, with the gh-115634 fix. Nothing in the round loop, a
stage's selection or the admission changes. Tests: the children end and the
next pool computes the same chunks; a caller inside or a held slot is never
released; the beat carries the call.

**To verify AFTER (deploy alone, after wikipedia-v8's check):** the log line
10 min after the last parse of the quiet profile (17:00) or after a pause;
`docker stats` for the worker before and after it; no `[parse pool]` failure
line; the first parse after a release starts its children (a spawn and its
plugin caches, seconds) and the batch's ms/doc against the same stage's
before it.

### 2026-10-30 — the page stream's look-ahead in rows, not pages — DEPLOYED 17:44 CET, MEASURED

**BEFORE (München, 17:21–17:30 CET; wikidata's records build on 11959e78,
the regrouped pack, 12 children, `--cpus 12`).**
- 2,000–2,400 records/s on the `event` family: slices of 5–17 parts, parts of ~14–18 k records.
- cgroup cpu 630 % of 1,200 %.
- A child (py-spy, 60 s): 46.8 % in the pool's `get`, waiting for work.
- The parent's main thread (py-spy `--threads --idle`, 60 s): 51.3 % waiting in `compute_pages`; the rest serial (pages 9.2 %, commits 5.6 %, `close_shard` 6.1 %).
- The pool's manager thread: 66 % in select. The part writers: idle.

Both sides waiting at once is the stall: the reading had stopped at
`PAGES_AHEAD` = 16 pages of 500 rows (8,000 rows, ~4 s of this family's
work) behind one large entity in the oldest page.

**SCENARIO.** The build alone in its container. The regroup was done
(17:18). The worker was running beside it (wikipedia embedding and
compaction), in its own cgroup.

**The change.** The look-ahead is bounded in rows, `ROWS_AHEAD` = 64,000:
the 16 pages of 4,000 the held build already had, and ~30 s of wikidata's
parsing. It is the same lesson as the line cache (lessons-learned.md): a
cap in units whose size the data decides. The cost test
`test_one_slow_part_does_not_stall_the_look_ahead_past_sixteen_pages` fails
on the old code (the watchdog gave the slow part up) and passes now.

**To verify AFTER (deploy alone, the build restarted on it; it resumes from
its last part):**
- cgroup cpu % over 360 s, and a child's py-spy share in the pool's `get`, against 630 % and 46.8 %;
- records/s on a family stretch comparable to the BEFORE's;
- the parent's RSS: up to 64,000 waiting records more, a few KB each.

**AFTER (München, 17:44–17:52 CET, b53c4164, the same build restarted on it).**

| | BEFORE | AFTER |
|---|---|---|
| a child idle in the pool's `get` | 46.8 % | 16.9 % |
| cgroup cpu over 360 s | 630 % | 740 % |
| records/s | ~2,340 (`event`) | ~3,500 (`heritage`) |
| memory.peak | 13,326 MiB | 12,732 MiB |
| parent RSS | — | 804 MB |
| children RSS | — | 870–900 MB, flat |

The two records/s windows cover different families, so that row is
indicative only. The children's time is now their own work:
`canonical_doc` 53.9 % (`read_row_group` 16 %), the tokenizer 16.8 %, and
the files asked per record ~3 %. That last cost is fixed next, by
`backend.rechecked`.

### 2026-10-30 — the backfill sweep restarts on a change of coverage, not of the epoch — VERIFIED

**BEFORE (München, 18:22–18:32 CET, 6081ca8).** wikipedia's dump layer
(native ≥ 10^12):

| in_scope | rdf | fts | embed | documents |
|---|---|---|---|---|
| 1 | 1 | 1 | 0 | 18,812,062 |
| 1 | 1 | 1 | 1 | 12,127,944 |
| 1 | 0 | – | – | 19 |

So 18.8 M documents were ready for the encoder. In 10 minutes the dump
layer committed 0 of them. Three thread dumps 30 s apart found the
embed-wikipedia producer asleep in `time.sleep(WORKER_IDLE_SLEEP)` each
time, with no work() threads. The encoder gate showed ~13 % busy.

**Cause.** `_cursor_page` restarted the sweep at rowid 0 whenever the
process-wide `genesis.coverage_epoch` moved. That epoch moves when any
archive's shard goes final or applied, about once a minute while
wikipedia's layer 1 imports. Each sweep therefore began again among the
22 M rows of layer 1. Its local archive covers those rows, so they are
filtered out, and at 1 s of walk per call and 5 s of sleep between empty
calls, the sweep never reached the dump layer behind them.

**SCENARIO.** wikipedia running: genesis importing de/03x, rdf and fts
caught up. The wikidata build ran beside it in its own container.

**The change.** The cursor is keyed on the coverage it walks against (the
archive's covered ranges), not on the epoch. A shard going final changes
the ranges and still resets the sweep, since its released documents sit
below the cursor. A local archive's declared spans never change, so its
imports no longer reset the sweep. Cost test:
`test_a_local_archive_s_epoch_never_restarts_the_sweep` fails on the old
code and passes now.

**To verify AFTER:** over 10 minutes,
- wikipedia's dump-layer commits (`embed_done=1`, native ≥ 10^12) and their chunks;
- the encoder gate's busy share against ~13 %;
- the producer thread no longer asleep in the dumps.

**AFTER (München, 19:23:07–19:33:07 CET, dad4338, deployed 19:02:39).**

The first sweep after the restart still needed ~17 minutes to cross layer 1
(the next row, #104, takes that away). The dump layer was reached at
19:20:19. In the 10-minute window:

| | BEFORE (18:22–18:32) | AFTER (19:23–19:33) |
|---|---|---|
| dump-layer commits (native ≥ 10^12) | 0 | 1,590 docs / 7,616 chunks |
| layer 1 (genesis) | 38,223 / 339,862 | 47,335 / 354,950 |
| encoder gate, corpus | 478 admitted, served 83 s | 10,028 admitted, waited 466 s, served 610 s |
| producer, three dumps | asleep (`WORKER_IDLE_SLEEP`) | `_embed_parallel`'s wait with work() threads in the encoder POST and at admit; `write_pending` → `_drop_clashing_many`; refill → `held_chunks` |

- Streams: 382 / 388 / 362 / 399 / 91 documents, 1,747 / 1,597 / 1,499 / 1,602 / 1,317 chunks, 0 reused.
- Throughput: about 13 chunks/s at concurrency 2.
- One "8 document(s) deferred" warning.

The encoder is now the bound: the gate served the whole window, and
requests queue at admit. At that rate the dump layer's ~90 M chunks are an
encoder-throughput question, not a scheduling one.

### 2026-10-30 — the light compaction: small fragments merged, the book_id index caught up — VERIFIED with #106 (below)

**BEFORE (München, wikipedia's table, read-only, b53c4164 / 6081ca8).**
- Table: 145,615,272 rows in 2,523 fragments, 2,368 of them small (~500 rows each: one per flush).
- The book_id BTREE leaves 1,959,307 rows unindexed.
- One `held_chunks` for 128 documents: 2.4–3.4 s.
- The pipeline-wikipedia thread (py-spy, 60 s): genesis 81 %; its flush's Lance `delete` ≈ 32 % of the thread; `add` 4.4 %.

A full compaction was due only at 4,000 small fragments, because it
rewrites large fragments with deletions: 40–51 minutes of ~10 cores. The
book_id index followed the VECTOR refresh's 2 % threshold (2.9 M rows on
this table).

**Bench (this container).**

| table: 1 M rows indexed + 2,000 fragments of 50 rows | delete of 128 ids |
|---|---|
| before | 0.36–0.66 s |
| after the light compaction (1.3 s for 100 k rows) | 0.011–0.022 s |
| after the BTREE delta (0.2 s) | 0.009–0.031 s |

**The change.** `light_compact_due` (≥ `VECTOR_INDEX_LIGHT_COMPACT_FRAGMENTS`,
256, and the full one not due) triggers `compact(light=True)`:
- Lance's `compact_files(materialize_deletions=False)` merges the small fragments only, so no large fragment is rewritten;
- `optimize_indices(index_names=[book_id], num_indices_to_merge=0)` folds the new rows into the book_id index as a delta, merged past `VECTOR_INDEX_MAX_INDICES`.

It runs under the same `rewrite` lock as the full compaction, so writers
defer for its seconds. The full compaction is unchanged. Cost test:
`test_the_light_compaction_merges_the_small_fragments_and_catches_the_book_id_index_up`.

**To verify AFTER:**
- the first "light-compacted N small fragment(s)" line and its seconds;
- fragments and book_id unindexed rows after it;
- one `held_chunks` of 128 against 2.4–3.4 s;
- genesis's delete share of the pipeline thread against 32 %;
- genesis documents per 10 minutes.

### 2026-10-30 — the new walk's window reads the candidates, not the rows — VERIFIED

**BEFORE (München, 19:03–19:20 CET, dad4338 = #102).** #102 works, but the
first sweep after the restart needed ~17 minutes:
- 19:03–19:13: 0 dump-layer commits; the producer asleep in all three dumps.
- py-spy --locals at 19:20: the "new" cursor at pos 33,868,000 of top 80,760,101, window width 17,632, found False — ~34 M rowids in ~17 min, ~33 k rowids/s over the 1 s budgets and 5 s sleeps.
- A cold window cost 0.18 s per 20 k rowids at rowid 0, 0.013–0.04 s higher up.
- Candidates (1,1,1,0) by rowid: 88,831 in 0–5 M (all covered); 826 k in 30–35 M; ~3.4 M per 5 M bucket from 35 to 60 M.

`_BACKFILL_WINDOWED` was forced onto the primary key (`+in_scope …`), so
each window read every row it spanned. That was right for the re-index and
version walks, whose prefix is not all-equality (architecture-v2 §49). It
is not right for the new walk: all four scope columns are equalities, so
SQLite seeks the scope index on (1,1,1,0) and on the rowid range (the
index's implicit tail).

**Bench (synthetic 5 M rows, 3.4 M covered and embedded, warm cache).** The
covered region was walked in 0.56 s on the primary key and in 0.11 s on the
index. Cold and with the node's wide rows the gap is the 34 M row reads
against ~88 k candidate reads. Cost test: a 20,000-row embedded window took
220,000 SQLite steps before and fewer than 2,000 now.

**The change.** The `+` goes from the new walk's window only; the version
walk keeps the primary key. Plan-checked: `SEARCH documents USING INDEX
idx_documents_scope (in_scope=? AND rdf_done=? AND fts_done=? AND
embed_done=? AND rowid>? AND rowid<?)`, no TEMP B-TREE.

**To verify AFTER (deploy, worker restart):**
- the time from the worker's start to the first dump-layer commit (BEFORE ~17 min);
- one py-spy --locals of the producer: the new cursor's pos and width within the first minute;
- then #102's 10-minute AFTER.

**Plan check on the node's manifest** (sqlite 3.46.1, wikipedia's covered
predicate): `SEARCH documents USING INDEX idx_documents_scope (in_scope=?
AND rdf_done=? AND fts_done=? AND embed_done=? AND rowid>? AND rowid<?)`.

| window | rows | seconds |
|---|---|---|
| (0, 200 k] | 0 | 0.000 |
| (0, 2 M] | 0 | 0.001 |
| (0, 34 M] | 128 | 0.002 |
| (33 M, 35 M] | 128 | 0.000 |
| (40 M, 40.2 M] | 128 | 0.021 |

**AFTER (München, 47886d3: #104 deployed ALONE from branch
deploy/104-before-103, with #103's three files kept at dad4338).**
- The worker started at 19:42:41 CET.
- Its first wikipedia stream started at 19:42:53: 128 documents / 686 chunks.
- That stream committed at 19:44:58: "129 document(s) … 694 chunks embedded, 0 reused".

So the dump layer was reached 12 s after the start, against ~17 min
BEFORE. py-spy --locals every ~3 s through the first 75 s never caught
`_cursor_page`: the walk finishes between samples.

### 2026-10-30 — a document cut to nothing is marked with its group, never sent — PREPARED

**BEFORE (München, read-only, ~20:00 CET).** Remaining in wikipedia's dump
layer (native ≥ 10^12, in_scope=1, fts_done=1, embed_done=0):

| documents | with chunks | chunks |
|---|---|---|
| 18,809,470 | 3,052,055 | 11,406,075 |

15,757,415 of them have `chunks` = 0 (redirects). From rowid 35 M on they
are ~85 % of every 5 M bucket. Through the stream, each takes a slot and
`_load_chunks` (12 ms; 60 of 60 sampled cut to 0 chunks, 0 tokens) to end
in `mark_done`: ~53 CPU-hours, plus the stream slots and held reads. The
11.66 M already marked were marked by a one-off pass on 2026-10-23.

**The change (a rule in code, not a one-off).** `_embed_parallel`'s
`settle_empty` marks done, in one transaction per group, the candidates
that meet all four conditions:
- `chunks` = 0;
- `tokens` = 0;
- written by the current `FTS_VERSION` (a count from an older chunker is not trusted: the stale counts of task #35);
- holding no vector (the group's held read, already made).

A document that still holds vectors takes the normal path, whose delete
removes them. The backlog and every future dump are covered by the same
code. Test: `test_a_document_cut_to_nothing_is_marked_without_a_slot`, which
fails on the old code.

**To verify AFTER:**
- over 10 minutes, dump-layer documents marked with 0 chunks against those embedded;
- chunks embedded per minute (the encoder should stay the bound, now on documents with text only);
- the producer's streams: documents per stream with chunks.

### 2026-10-30 — every vector write through the registration; the light compaction first — VERIFIED

**BEFORE (München, #103's first run, ee13d64).**
- The light compaction ran where it could: gutenberg merged 1,944 small fragments of 1,958 into 19 in 3 s; pubmed merged 1,500 of 1,542 into 48 in 6 s.
- wikipedia, at 8,246 small fragments of 8,398, was past the full threshold (4,000), so the light pass was not due. The FULL compaction ran from 19:58:27 and at 20:02:57 was "skipped: Retryable commit conflict … Rewrite transaction was preempted by concurrent transaction Delete".
- The embed stood down for it as designed: 40 documents deferred, no stream from 19:58:46 to 20:03:03.

**Causes.**
1. genesis's flush and the bundle importer committed under the manifest lock alone (`vector_write_lock`), never through `vector_index.writing`. So `rewrite` neither waited for them nor held them off. This is normattiva's lesson of 10-23, on two more paths.
2. `light_compact_due` excluded the case where the full one was due.

**The changes.**
- genesis's flush and the importer's write go through `embeddings._write_lock(con, collection)`, which takes both locks. A refusal is a retryable commit conflict: genesis's pass defers (`vector store busy`) and the importer returns retryable.
- A write held off by a rewrite now WAITS up to `REWRITE_WAIT_S` (60 s) before it is refused, so a light compaction of seconds costs no deferrals.
- The light compaction runs first whenever its threshold is met; the full one is then due only if small fragments remain.

Tests:
- the light compaction runs first even where the full one is due;
- a write waits out a short rewrite and defers past a long one, as a contention;
- genesis and the importer write through the registration.

**Left open (not a defect).** Deletions are materialized only by the full pass, which now seldom runs. A trigger on the deleted fraction would make it run on wikipedia (~40–51 minutes of ~10 cores, writers held off). That is to be weighed with the operator, in a quiet window.

**To verify AFTER:**
- wikipedia's first "light-compacted" line and its seconds;
- fragments and unindexed book_id rows after it;
- held_chunks and the clash read of 128 against 0.83–1.28 s and 0.17–0.29 s;
- genesis's delete share against 32 %;
- no "compaction skipped … preempted" line.

**AFTER (München, dea059e: #106 deployed ALONE from branch
deploy/106-before-105, with #105's files kept at ee13d64; worker up 20:19:53
CET).**
- 20:25:17 "light compaction started: 9232 small fragment(s) of 9384, 145649566 rows".
- 20:25:58 "light-compacted 9232 small fragment(s) of 9384 into 203 (33s); the book_id index's new rows indexed".

| wikipedia's table | BEFORE (19:5x, ee13d64) | AFTER (20:26, dea059e) |
|---|---|---|
| fragments / small | 7,751 / 7,599 | 245 / 88 |
| book_id_idx unindexed rows | 1,374,799 | 21,130 (4 deltas) |
| held_chunks, 128 dump-layer docs (213 rows) | 0.95 / 0.83 / 1.28 s | 0.250 / 0.098 / 0.083 s |
| `_drop_clashing_many`-shaped read, same 128 | 0.29 / 0.17 / 0.24 s | 0.019 / 0.011 / 0.047 s |
| pipeline-wikipedia: genesis flush delete / add | ~32 % / 4.4 % | 15.7 % / 8.1 % |
| genesis documents per 10 min | 33,200 (18:22), 47,335 (19:23) | 71,018 (20:26–20:36) |
| "preempted" lines | 1 (20:02:57) | 0 |

vector_idx still leaves 2,189,958 rows unindexed: its own refresh, which
this change does not touch. The embed in the same 10 minutes made
1,004–1,622 chunks per stream (about 11 chunks/s), so the encoder is still
the bound.

### 2026-10-30 — the light compaction before the prune and the refresh — MEASURED (#107)

**BEFORE (München, #105's window, a6b6b71).**
- Fragments: 20:25:58 light-compacted 9,232 → 203. By 20:55:04 there were 2,029 small ones again (→ 195 in 23 s), and 339 by 20:59. They come back at ~70–80 a minute from genesis and embed commits.
- Timing: the maintainer reached the light pass only after a 253 s prune and a 305 s vector refresh (20:49:55 → 20:55:04).
- held_chunks of 128: 0.08–0.25 s just after a light pass, 0.40–0.71 s at 339 small fragments.
- The clash read: 0.01–0.05 s just after a light pass, 0.08–0.11 s at 339 small fragments.

**The change.** In `vector_index.ensure` the light compaction runs
first, before the prune and the index work. It runs once a pass, and the
full compaction is skipped in a pass that ran the light one. Test:
`test_the_light_compaction_is_not_queued_behind_the_long_passes`.

**To verify AFTER:**
- small fragments sampled every 5 minutes over 30 minutes;
- the light lines' seconds;
- held_chunks of 128, three times, mid-interval.

**AFTER (München, 27408bf, worker up 21:10:51, watched until 21:42:39).**
The light pass runs first: at 21:12:07 it merged 1,038 small fragments into
206 in 4 s, and the prune followed (21:15:39, 213 s, 2.9 GiB). Then no
index pass for the rest of the watch, because `ensure` runs once every
VECTOR_INDEX_REFRESH_S (1,800 s). Every 5 minutes, one held_chunks read and
one clash read of 128 dump-layer documents:

| time | fragments / small | held_chunks / clash read |
|---|---|---|
| 21:11:53 | 1,189 / 1,032 | 1.234 / 0.284 s |
| 21:16:59 | 712 / 554 | 0.425 / 0.041 s |
| 21:22:02 | 1,156 / 998 | 1.053 / 0.275 s |
| 21:27:07 | 1,362 / 1,204 | 1.077 / 0.331 s |
| 21:32:13 | 1,676 / 1,519 | 1.172 / 0.255 s |
| 21:37:18 | 2,029 / 1,872 | 1.752 / 0.452 s |
| 21:42:27 | 2,257 / 2,101 | 2.445 / 0.877 s |

Small fragments come back at ~60–70 a minute, and held_chunks follows them
almost linearly. The pass order is right; the cadence is wrong.

### 2026-10-30 — the light compaction on its own cadence — VERIFIED

**BEFORE:** the table above (#107's AFTER). Over its 30 minutes the table
went from 554 to 2,101 small fragments, and held_chunks of 128 from 0.43 s
to 2.45 s (mean ~1.3 s).

**The change.** Between the index's passes, the maintainer asks each table
for its small fragments every VECTOR_INDEX_LIGHT_CHECK_S (60 s;
`worker._light_compact` → `vector_index.light`). It light-compacts at 256 or
more, unless a build, refresh or rewrite holds the table. The check reads
`tbl.stats()`' fragment statistics alone, with no row count and no index
statistics. The pass itself is unchanged and still runs the light pass first.

**Expected:** at ~65 a minute the threshold is crossed every ~4 minutes,
merged in 4–6 s. Writers wait those seconds (REWRITE_WAIT_S), about 2 % of
the time. Small fragments stay under ~300, and held_chunks of 128 stays near
0.1–0.4 s.

**To verify AFTER (30 minutes, after #108's deploy):**
- small fragments every 5 minutes;
- every "light-compacted" line, with its seconds;
- held_chunks and the clash read of 128 every 5 minutes;
- genesis documents per 10 minutes, and "deferred" lines from embed or genesis (the cost: writers waiting out the merge);
- the time of one `vector_index.small_fragments` call on wikipedia.

**AFTER (München, e5e0cc2 = #109 deployed alone, worker up 21:44:42Z;
22:46:57–23:17:18 CET).**

| time | fragments / small | book_id_idx unindexed | held_chunks 128 | clash read 128 |
|---|---|---|---|---|
| 22:46:57 | 345 / 187 | 74,673 | 0.396 s | 0.034 s |
| 22:52:00 | 615 / 457 | 54,928 | 0.195 | 0.030 |
| 22:57:04 | 368 / 211 | 85,570 | 0.331 | 0.030 |
| 23:02:06 | 491 / 334 | 144,822 | 0.376 | 0.100 |
| 23:07:11 | 393 / 233 | 95,564 | 0.332 | 0.037 |
| 23:12:14 | 407 / 247 | 102,792 | 0.424 | 0.061 |
| 23:17:18 | 379 / 221 | 90,157 | 0.177 | 0.021 |

- **Light merges in the 30 minutes:** 10.
  - The first ran at the pass's start: 785 → 196 in 23 s.
  - One ran right after a 292 s vector refresh: 501 → 198 in 79 s.
  - The rest came every 2–3 minutes at 267–359 small fragments, in 1–7 s each.
- **Writers held off:** one deferral, "upstash…de/114.parquet: pass deferred — vector store busy (RewriteInProgress)" at 22:53:47. That was during the 79 s merge, past the 60 s wait.
- **Genesis documents per 10 minutes:** 36,903, then 59,267, then 88,222.
- **One `small_fragments(wikipedia)` call:** 51 ms cold, 11 ms warm.

So against #107's 554 → 2,101 small fragments and held_chunks 0.43 → 2.45 s,
the table stays at 187–457 small fragments and held_chunks at 0.18–0.42 s.

### 2026-10-31 — gutenberg's encoder with flash attention on, oneDNN's SDPA off — MEASURED

**What changed.** vega-qwen3-embedding-4b moved at 13:41 from flash attention off to on with
`GGML_SYCL_FA_ONEDNN=0` (P5 engine, run 0a80405f; approved-encoders.md). The measurement is
`fa_speed2.py`: 150 real gutenberg chunks (~3,240 characters each), one request each, the live
model and the candidate in alternating passes on the same card in the same minutes.

| pass | FA off (run b36c5912) | FA on + FA_ONEDNN=0 (run 0a80405f) |
|---|---|---|
| 1 | 2.33 chunks/s (64.4 s) | 2.23 chunks/s (67.3 s) |
| 2 | 2.32 chunks/s (64.8 s) | 2.48 chunks/s (60.5 s) |

- **BEFORE, the same harness 2026-10-31 morning:** FA off 2.0–2.3 chunks/s; FA on with oneDNN's SDPA 3.9–5.4, which was fast and not reproducible.
- **The gate's benchmark (512 calibration texts):** 1,294 → 1,500 tokens/s (+16 %).
- **The repro harness (prefixes up to 8,191 words):** −23 to −45 % a run.

**Reading.** At gutenberg's chunk length the switch is about neutral. Its gain is on long inputs.
The speed oneDNN's SDPA gave (2–2.4×) does not come back without its non-reproducibility.
gutenberg has nothing pending beyond book 673 today, so the card's share is unchanged until new
books arrive.

### 2026-10-31 — every query's reranker with flash attention on, oneDNN's SDPA off — MEASURED

**What changed.** louise-rerank-bge moved at 14:06 from the strict settings (FA off,
GGML_SYCL_ENABLE_OPT/GRAPH/FUSION=0) to FA on + `GGML_SYCL_FA_ONEDNN=0` (approved-encoders.md).
A search reranks its top 50 (05 §5.6), so a query's wait for the reranker is the row that counts.
Measured on gutenberg's real candidates (rerank_capture.py), warm unless said:

| request | strict (BEFORE) | FA on + FA_ONEDNN=0 (AFTER) |
|---|---|---|
| 40 documents, median of 9 / 45 | 3.87 s | 2.80 s |
| 56 documents | 5.32 s | 3.55 s |
| 72 documents | 7.00 s | 4.46 s |
| 50 documents, median of 10 | 5.22 s | 4.39 s (4.24–4.47 live after the move) |

The 40/56/72 medians include each load's first request, and the 50-document medians are warm.
FA on with oneDNN's SDPA gave 1.8–2.2 s for 50 on 2026-10-21, and NaN with it.

### 2026-10-31 — wikipedia's encoder requests batched by 16 (#129) — MEASURED, no gain: the bound moved

**What changed.** `batch = 16` in wikipedia's `[encoder]` (16:07:32). The node's EMBED_BATCH stays 1.
run_check refused batched writes until its gate ran at 16. The gate ran at 16:11:19 and passed
16 of 16 at 16:12:35, batch-invariant True, statement version 5, exact over 3 loads
(gate-batching.json: `embed_batch 16`). The same 10-minute production profile was run before and
after, both in the day profile with rdf/fts running and gutenberg idle (/root/measure/prod-profile*.log,
prod_profile.sh, spy_split.py):

| | BEFORE 14:34 (1 a request) | AFTER 16:15 (16 a request) |
|---|---|---|
| chunks/min, two 5-min halves | 1,149 / 777 | 739 / 1,241 |
| card busy (bge-m3's process, per minute) | 7–14 % | 1–20 % |
| embed pool threads idle | ~40 % | ~77 % |
| pool: HTTP / admission / slot wait | 15–24 / 14–24 / 13–25 % | 9–12 / 6–7 / 2–5 % |
| producer: waiting on the parse pool's results (chunks to prepare) | 24 % | 52 % |
| producer: `vector_write_lock` | — | 16 % |
| producer: `mark_done` / `settle_empty` / `clear_verification` | ~27 % | ~16 % |
| pipeline thread: the archive import (`drain_held` > `_append_document` > `flush`) | ~38 % | ~41 % |

**Reading.** Batching took the encoder off the path: the pool threads now wait for work three
quarters of the time. Throughput did not move (~960 → ~990 chunks/min, within the halves' spread).
The bound is the producer side:
- preparing the next documents' chunks, which is wikitext parsed in the shared parse pool and
  waited on half the time;
- the vector write lock, contended by the pipeline thread's archive import writing the same table;
- the per-document bookkeeping (B, being built).

The next lever is the chunk preparation. The batch stays: it is gated, exact and costs nothing,
and it is what the encoder will need once preparation keeps up.

### 2026-10-31 — wikipedia's vector table held ~142 GiB of old IVF_PQ generations — MEASURED, cause found, nothing changed

**Measured (München, read-only, 21:00 CET).** wikipedia's Lance table, 146,398,044 rows, 544.5 GB on
disk: `data/` 338,191 MiB, `_indices/` 181,084 MiB in 630 directories, 675 versions (16:56–18:59).
The LIVE indexes are 29.25 GB: `vector_idx` IVF_PQ (L2, 128 sub-vectors × 8 bits, 3 segments,
27.96 GB = 191 B/row) and `book_id_idx` BTree (4 segments, 1.29 GB). The index directories by
mtime and size:

| built | GiB | |
|---|---|---|
| 10-30 15:52, 16:06, 16:13, 16:50, 19:14, 19:38, 19:49, 20:02 | 17.7–17.9 each, 142.4 together | full IVF_PQ generations, unreferenced |
| 10-31 01:27 | 17.8 | live (8bb3fc62) |
| 10-31 14:29 | 7.4 | live delta |
| 610 smaller | 9.1 | |

**Cause.** Eight FULL index trainings in 4 h 10 min on 10-30, the day of the twins repair
(STATUS 2026-10-30: 21.9 M twin rows dropped): ~143 GiB written in that window, ~34 GiB/h.
The worker's log of that day is gone with its container (four deploys since), so which
call trained them is read from the table, not the log. The maintainer's prune leaves them:
`vector_index.prune` calls `cleanup_old_versions(older_than=2 h)` with `delete_unverified`
FALSE on purpose (a concurrent writer's uncommitted files), and Lance treats an unreferenced
file younger than 7 days as unverified. They go by themselves after ~11-06.

**The maintainer's own call, run by hand on wikipedia (paused):** 621 versions removed, 4.2 s,
13.0 GiB freed (data/ 338,191 → 326,115 MiB; _indices/ 181,084 → 180,269 MiB; _versions/
29 → 6 MiB).

**Open.** (1) Which path trained eight full indexes in four hours on a repair; a bulk drop should
train once at its end. (2) Whether to free the 142 GiB now: `cleanup_old_versions(older_than=0,
delete_unverified=True)` under the vector write lock, as `purge` does after an erase, while
wikipedia is paused and nothing writes its table — or let the 7 days pass.

### 2026-10-31 — wikidata-bge's held-line records build: the parent, then the feeding (#148) — MEASURED

**Scenario.** `hold records build wikidata-bge --records-from wikidata` (75,896,183 records, 4,009
parts cut by content), under supervise-pass, `--cpus 12`, night profile (12 cores), nice 19.
wikipedia's Lance rewrite paused after the first 15 min (ssd4 at 11 ms reads starved the build's
`commit_written` manifest UPDATEs: 2,800 rec/s beside the copy against 6,700 alone, range=8).
Rates compared on ONE slice, astronomy/range=9: wikidata's records differ in size by family and
range, so records/s across slices is not a comparison.

| | BEFORE #148 (23:00) | AFTER #148 (23:14–23:17) |
|---|---|---|
| astronomy/range=9 records/s | 4,507 | 4,297 |
| container CPU | 6.4 cores (range=8) | 8.25 cores |
| parent, py-spy 20 s at 50 Hz | busy ~100 %: `_recv` 34.8 %, `_send` 15.6 %, `dumps` 6.7 %, `commit_written` 5.3 % | busy 24 % (242 samples): `_recv` 31 %, `commit_written` 15 % of those |
| a pool child (15 s, idle included) | — | one 100 % waiting on the call queue; another busy: `held_or_parsed` 34 %, `_encode` (tokenizer, the chunk re-cut) 32.6 %, frame reads ~12 % |
| a part writer | — | 100 % idle |

**Reading.** #148 took the parent off the path (busy 100 % → 24 %) and the throughput did not
move: the pool is now under-fed. Children wait on the queue while the parent is idle, so the
bound is the task window or its ordering (`compute_pages` for held pages), not the CPU. Owed to
the architecture session: which of the window cap, the page size of held work, or the in-order
consumption leaves children idle.

**AFTER #149** (a part queued behind each child; held lookups by part; deployed 23:47, the build
resumed from its staged parts). Mid-slice, internal/range=0, 23:53:40–23:56:29: **~4,100
records/s** (692.7 k in 169 s), the container at 3.6 cores in that window. Each child at 42–56 %
over 5 s (BEFORE #149: 45–57 %). The parent's threads together busy 175 % of 20 s: `_send` 35 %,
`_recv` 26 %, `commit_written` 9 %, `dumps` 3 %. Three threads of one interpreter, so one GIL:
the parent's pipes are the shared bound again, now from both sides (feeding parts, taking
results). A slice boundary shows the parent alone (55 %) and every child at 0 % for seconds.
Not changed tonight. 946 of 4,009 parts staged at 23:57 (24 %); at this pace the build ends ~04:15.

### 2026-11-01 — wikipedia's vector table rewritten onto stable row ids (#146), indexed, swapped — MEASURED

**Scenario.** `dev rewrite-vectors wikipedia` under supervise-pass (6 cores), 146,398,044 rows, ssd4.
Then the after-swap chain without an agent (worker and web restarted, stable row ids verified,
latency measured, the old table dropped, the `lang` bitmap index built, latency again).

| step | measured |
|---|---|
| copy, alone on ssd4 (after 02:34) | 325 fragments, ~11 GiB/min; finished 02:39, 2,000 sampled rows verified byte for byte |
| `book_id` / `lang` scalar indexes | 23 s / 42 s |
| vector index | 02:40 → 09:10:51, **6 h 31 min** |
| old table dropped | 355.6 GiB; ssd4 avail 278 G at once, 97.6 G still held by the hourly snapshots until they age out |

| search latency, 36 runs | before the rewrite | swapped, no `lang` index | with the `lang` index |
|---|---|---|---|
| unfiltered, median / p90 | 86 ms / — | 144.1 / 223.0 ms | **57.3 / 91.0 ms** |
| `lang = en`, median / p90 | 2.1 s / — | 198.9 / 270.1 ms | **202.0 / 254.5 ms** |

**Reading.** The rewrite itself is what cut the English-filtered search 10×. The bitmap index
on `lang` moved the unfiltered median (144 → 57 ms; the first run follows a restart, so part
of that is warm-up), and not the filtered one.

**What cost time.**
- **Two waiters fired on the same "built" line.** They started the copy and wikipedia's re-cut
  together. The re-cut fell from 1,500 to 350 records/s beside the copy, through ssd4 read
  latency on its manifest UPDATEs. The re-cut was held behind the swap. One trigger starts one
  I/O-heavy pass; the next waits for it.
- **wikidata-bge's first snapshot OOMed 18 times at 8 GiB.** `_first_delta` holds every uid as
  a str (75.9 M, ~11 GB) and serialises ~1.5 GB of canonical JSON. It passed at 20 GiB.

**The filtered search's remaining cost** (the architecture session, measured on a 3 M-row
stable-row-id table with IVF_PQ and the `lang` bitmap): prefilter `en` 20.7 ms, `tr` 18.1 ms,
against 10.6 ms unfiltered. Postfilter with oversampling is slower (`en` 28.7 ms, `tr` 130.7 ms),
and it would change the results (overlap@20 with the prefilter answer 0.72–0.78). What remains
is the prefilter's row mask, about 58 M rows for `en` on wikipedia, which is Lance's own design.
The only lever is one vector table or index per language, a storage-layout change not worth it
at 202 ms. No action.

### 2026-11-01 — wikidata's bge-m3 embedding, one text a request: where the time goes (M1–M4) — MEASURED

**Scenario.** `wikidata` (formerly wikidata-bge) embeds nine families, 43.3 M chunks of ~120
tokens (~1.4 a document). Run sha256:92775442… on the SYCL F16 llama.cpp backend, `parallel:1`,
`GGML_SYCL_ENABLE_DNN=0`. One text a request: the gate admitted no batching for this run. The
worker keeps 2 documents submitted and the dispatch gate lets 1 request through
(`concurrency = 1`). Requests are counted from LocalAI's log, one per chunk; they match the
vector-count deltas (2,374 against 2,377). Also on the machine: the architecture's gutenberg
CPU-reference job (Qwen3-4B on CPU).

| window | chunks/s |
|---|---|
| first hour, beside the wikipedia re-cut (09:46–10:36, vector count) | 4.0 |
| M2: re-cut running, 5 min | 5.1 |
| M2: re-cut frozen (`docker pause`), 5 min | **7.9** |
| quiet profile, re-cut ended (two 5-min windows) | 7.8 / 7.3 |

**M1, py-spy of the VeGa wrapper, 60 s at 200 Hz (`--idle --threads`).** `embedding()` held
21.8 s of thread time for 283 calls:

| `embedding()` share | % | per call |
|---|---|---|
| the backend's gRPC forward | 98.8 | ~76 ms |
| receipt delivery | 0.9 | ~0.7 ms |
| digests | 0.1 | — |

The backend was busy ~36 % of the window. The rest of each ~212 ms cycle is outside the
wrapper: the worker, LocalAI's HTTP layer, the receipts GET. On 2026-10-30 wikipedia's shorter
chunks took ~42 ms in the same call.

**M4, soundness of wikipedia's `batch = 16` (since 2026-10-31 16:07:32).** 144.1 M of
wikipedia's 146.4 M chunks are the upstash archive's (`chunk_verification`), so the batch-16
writes were found as the table's tail. The archive imports stopped at 16:32 and wikipedia was
paused at 18:58. Each sampled vector was re-embedded alone through `embed_chunks` and converted
as the writer does (`np.asarray(v, dtype=np.float16)`):
- the last 2,000 rows: **0 differ**;
- 2,000 spread over the last 180,000 rows (119,626 of them this encoder's, written 16:32–18:59):
  **0 differ**.

The comparison is at the stored f16, which is what the artifact holds.

**Reading.**
- The bound is not the card. Frozen, the CPU contention of the re-cut cost a third of the
  rate (5.1 → 7.9).
- The quiet profile does not halve this embed, unlike wikipedia's. Wikidata's producer reads
  held records and needs no parse pool.
- With the re-cut finished, ~7.5/s puts the nine families at ~67 days.
- A changed `[encoder] concurrency` is a run new to the peer: the worker re-ran the gate for
  8 min before writing again, which is correct. The first M3 counted that gate as zero
  throughput.

**M3, the encoder's `concurrency` (the dispatch gate's admissions; the server stays `parallel:1`
and runs one sequence at a time).** Quiet profile, re-cut ended. Each count is taken after the
gate the change triggered had passed:

| `concurrency` | requests (chunks) / s |
|---|---|
| 1 | 7.3 / 7.8 (5-min windows), 8.3 (clean, 11:41–11:46 UTC) |
| 2 | 10.7 |
| 4 | **15.9** (worker: "4 submitted"; its gate at 12:31:19, 16/16, median 1.000000, batch-invariant True) |

**What changed.** At 12:46:45 wikidata's `[encoder]` was set to `concurrency = 4`, with
`device = "gpu"` declared because concurrency > 1 would otherwise read as a CPU encoder. The
nine families now take ~31 days instead of ~60. Queued requests hide the overhead outside the
forward that one request at a time leaves bare. At 15.9/s the forward can take at most ~63 ms,
under M1's 76 ms at one in flight, so part of that 76 ms was itself latency, not compute.

**AFTER, measured.**
- No gate ran: the statement at 4 was already held. The worker reads "4 submitted".
- 4,967 requests in 11:51–11:56 UTC, **16.6 chunks/s**.
- **Bytes:** the last 1,500 vectors written at 4 (documents updated 12:53:27–12:57:05) were
  re-embedded one text at a time, as M4 did: **0 differ** at the stored f16.

### 2026-11-01 — wikidata at batch 16, and wikipedia's GPU share (the operator's question) — MEASURED

**Scenario.** bge-m3, one SYCL F16 llama.cpp process serving both collections, `parallel:1`. The
card's busy share is that process's (`/root/measure/gpu_busy.py`: DRM fdinfo cycles). Rates come
from the worker log's batch totals (`embed_rate.py`), the worker's CPU from its cgroup
(`/sys/fs/cgroup/docker/<id>/cpu.stat` on TrueNAS, not `system.slice`). Day profile (8 cores).

| window | card busy / min (corrected from fdinfo ×2, saturation test 18:2x; fdinfo read in brackets) | wikidata chunks/s | wikipedia chunks/s | worker CPU |
|---|---|---|---|---|
| wikidata at batch 16 alone, first 15 min after its gate (vector-count slope) | — | 42.0 | paused | — |
| A: wikipedia alone (14:22–14:32) | 16–42 % (8–21) | paused | 32.0 / 18.7 | — |
| B: both (14:34–14:44) | 50–72 % (25–36) | 32.9 / 19.9 | 11.9 / 7.0 | — |
| C: wikidata alone at batch 16 (14:46–14:51) | 72–80 % (36–40) | **57.0** | paused | 0.96 cores |

**What changed.**
- wikidata's `[encoder] batch = 16`, under `concurrency = 4`. Its gate re-ran (16/16, median
  1.000000, batch-invariant True).
- **Bytes:** the last 2,000 rows written at 16 were re-embedded one text at a time: **0 differ**.
- **The day's path:** 4.0 → 8.3 (c1) → 16.6 (c4) → 57.0 chunks/s (c4 + batch 16).

**Window A's producer, py-spy 60 s at 100 Hz, `--idle`** (`spy_split2.py`, `spy_acquire.py`,
`spy_upsert.py`):
- **The producer thread:** waiting on in-flight results 62.6 %, chunk preparation 16.6 %,
  bookkeeping 9.6 %, vector write 5.5 %.
- **Its pool:** `_worker` idle 23.0 %, the SUBMITTED slot semaphore 22.1 % (`worker.py:1582`:
  the prepared document waiting for a slot, by design), HTTP 19.2 %, `upsert_book` 16.6 %.
  Of `upsert_book`: `_rows_for > check_write > statements_for_space` 87.6 %, which re-lists and
  re-reads the run statements from disk per document; `check` 10.4 %; Lance `add` 0.2 %.
- **Lance's version history:** 4,409 commits in 10 min. 2,224 added ~503 rows each and 2,165
  removed ~503: +1.106 M / −1.092 M rows. The pipeline thread's genesis re-import after the re-cut
  did it: 22 upstash parts, 1,089,033 vectors, 188,644 documents in the window, 0 mismatches.

**Reading.**
- Neither the card (≤40 %) nor the CPU (<1 core) bounds wikidata at batch 16: the bound is
  latency per request.
- wikipedia's write path is not a lock. It is the per-document statement re-read, and the
  genesis churn shares the table's writes.
- Levers, all handed to the architecture session:
  - cache the statements on their directory's mtime;
  - skip a genesis rewrite of identical bytes (RULE 1);
  - then concurrency or batch steps, gated.

### 2026-11-01 — #162 (own statements first; genesis skips byte-identical re-imports) — MEASURED AFTER

**Scenario.** 17:12–17:22, wikipedia and wikidata both embedding. Day-or-night: the full-speed override
(night profile, 12 cores) was in force. gutenberg paused and held. No CPU reference or gate on the card.
The same harness as window A (`prod_profile_tag.sh`, py-spy 60 s at minute 2, `lance_commits.py`).

| | BEFORE (window A / B, 14:22–14:44) | AFTER #162 |
|---|---|---|
| wikipedia table commits | 441 / min (2,224 add ~503 rows, 2,165 remove ~503) | **5.5 / min**, all additions (median 188 rows) |
| rows in 10 min | +1,106,191 / −1,091,552 | +10,650 / 0 |
| genesis in 10 min | 22 parts, 1,089,033 vectors rewritten | 8 parts, 130,885 vectors read, none rewritten |
| wikipedia chunks/s, beside wikidata | 11.9 / 7.0 | **17.4 / 16.7** |
| wikidata chunks/s, beside wikipedia | 32.9 / 19.9 | 13.4 / 26.5 |
| card busy (bge-m3 process) / min, corrected from fdinfo ×2 (saturation test 18:2x) | 50–72 % (fdinfo 25–36) | 30–68 % (fdinfo 15–34) |
| the pool's `upsert_book` share | 16.6 % (`statements_for_space` 87.6 % of it) | 3.8 % (`check_write > statements` 22.6 %, `> check` 71.2 %) |

**Reading.** The rewrite churn is gone: 80 times fewer commits, and the table now grows only by
what is embedded. wikipedia's rate beside wikidata rose 1.5–2.4×. The per-document statement re-read
fell from ~14.5 % of the pool to ~0.9 %. wikidata's first half was low: it was 35 minutes after its
own re-gate and still beside wikipedia's catch-up.

### 2026-11-01 — step 1 BEFORE: wikidata alone at batch 16 / concurrency 4 (5b83a25 deployed) — MEASURED, the new instrument blind

**Scenario.** 17:41–17:56, wikidata alone; wikipedia, pubmed and normattiva paused for the window,
gutenberg paused and held. 5b83a25 (#164 instruments, #165 GPU_PROBE_URL to the worker, #166 the gate fix)
deployed at 17:33. Full-speed override in force (night profile, 12 cores). Nothing else on the card.

| measure | value |
|---|---|
| wikidata chunks/s (worker log) | **62.3** (56,458 chunks, 53,203 documents in 15 min) |
| card busy per min, corrected from fdinfo ×2 (saturation test 18:2x) | 90, 88, 82, 86, 84, 86, 86, 84, 86, 86, 84, 86, 76, 74 % (`gpu_busy.py` read half: 45 … 37 %) |
| worker CPU (cgroup) | 0.77 cores |
| `schedule-report --gpu`: held (gate's served seconds/min) | wikidata ~200/min (2,883 in 15 min) |
| `schedule-report --gpu`: busy, gaps, lost | **invalid**: 0.0 % busy, gaps 60 s/min |

**The instrument is blind.** The 5-minute cross-check read 31–37 % with gpu_busy.py and 0.0 % from the card
record. gpu-probe (`pid: host`, root, `CAP_PERFMON` only) gets "Permission denied" on
`/proc/<pid>/fdinfo` of the backend process. Reading another process's fdinfo needs ptrace-read access
(`CAP_SYS_PTRACE`), so the probe counts no DRM client and reports `"engines": {}, "processes": []`.
The host reads 2 DRM fds on the same process. Nothing was changed. The gaps at the 120 s stream
boundaries cannot be seen until the probe can read fdinfo, so this row is redone once it can.

### 2026-11-01 — the card's busy figure: the PMU, and fdinfo reads half on this card (saturation test 18:2x) — MEASURED

76964d2 (#169) reads the B70's busy from xe's perf PMU (`xe_0000_03_00.0`, engine-active / engine-total
ticks, CAP_PERFMON only). To settle the PMU (~70 %) against fdinfo (~33 %) seen at 18:00, every collection
was paused (gutenberg held) and ccs0 loaded on its own: 4 loops of one ~8 k-token text on vega-bge-m3 through
the vouched path, 63 requests in 128 s, 0.49 requests/s.

| | perf ccs0 | `/busy` (PMU) | fdinfo (`gpu_busy.py`) |
|---|---|---|---|
| saturating load, 2 min | 91.8 %, 92.1 % | mean 90.4 % (131 samples) | 45 %, 46 % |
| idle, 1 min | 0.0 % | — | 0 % |

**Reading.** fdinfo's client cycles are halved on this card and kernel; the PMU is the truth and does not
overcount at idle. Every fdinfo figure this afternoon is ×2: the tables above are corrected, with the fdinfo
reading in brackets. So "neither the card (≤ 40 %)" in the 14:5x reading means ≤ 80 % on the PMU.
**The copy engine:** under load, bcs reads the same as ccs within a few tenths, both in perf (18:29: bcs 78.4 %,
ccs 78.3 %) and in the probe. The Level Zero copy context is resident and counted active, so step 1's figure
is ccs alone.

### 2026-11-01 — step 1 BEFORE: wikidata alone at batch 16 / concurrency 4, on the PMU — MEASURED

**Scenario.** 18:28–18:43, wikidata alone. wikipedia, pubmed and normattiva paused for the window; gutenberg
paused and held. 76964d2 deployed. Full-speed override (night profile, 12 cores). Nothing else on the card.

| minute (UTC) | ccs | bcs | gaps | lost |
|---|---|---|---|---|
| 17:29 | 77.4 % | 77.6 % | 0 s | 13.5 s |
| 17:30–17:42 | 80.5–83.9 % | 80.9–84.3 % | 0–1 s | 9.4–11.5 s |
| all (885 s recorded) | — | 82.1 % (busiest engine) | **1 s (0.1 s/min)** | **158.7 s (10.8 s/min)** |

- **wikidata:** **78.8 chunks/s** (71,316 chunks, 68,322 documents in 15 min); held ~200 s/min.
- **Worker CPU:** 0.76 cores (cgroup).
- **`gpu_busy.py` beside it:** 40–43 %, i.e. 80–86 % ×2, agreeing.

**Reading.** The card is ~82 % busy with wikidata alone and there are no idle gaps of a whole second, even at
the 120 s stream boundaries (gaps 0.1 s/min). The missing ~18 % is lost INSIDE the seconds, between requests.
Lever 1, which removes the drain at the stream boundary, has little visible to recover here. The 18 % is the
request turnaround at 4 in flight × 16 texts.
