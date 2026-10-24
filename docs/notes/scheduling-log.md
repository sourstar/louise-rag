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

**STEP 2 (a833c1e, 20:41–21:42 CEST):** (to fill) utilisation; then the A/B.
