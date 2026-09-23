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
| **pool children busy** | **~1.3 of 11 (12 %)** | 8.5 (77 %) | **8.1 / 8.9 / 8.5 (73–81 %)** | **sustained over three 10-min intervals** |
| worker tree CPU | 77 % of 1200 % | 316 % | **408 %** | |
| chunks/s of wall | *see caveat* | — | **73.3** | |
| docs/s inside batches | 54.2 | 56.1 | 69.3 | *do not compare across chunks/doc* |
| wall inside batches | 53 % | 51 % | **54 %** | **unchanged — untouched by this** |
| wikipedia rdf | 6.3 ms/doc | 11.8 | 10.9 – 14.4 | on ~5.4x the text |
| wikipedia fts | 10.2 ms/doc | 13.0 | 8.1 – 13.6 | on ~5.4x the text |
| manifest deferrals | — | 0 | **0** | |
| errors | — | none | **none** | |

**Verdict: KEPT**, on the occupancy evidence.

**What is rigorous here and what is not — say it plainly.** The rigorous
number is **pool occupancy: 12 % → 73–81 %, sustained**. It is
scenario-independent by construction (it measures whether children had
work, not how much work each document was) and it was the defect the
`py-spy dump` identified, so it is the claim and the evidence matching.

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

1. **46 % of wall time falls outside any batch** (54 % inside, unchanged
   from the baseline's 53 %). Nothing here touched it and it is not yet
   attributed — `py-spy record --idle` over a full window is the next
   instrument, since the thread in question is blocked, not burning.
2. **Parse and commit never overlap.** One pipeline thread per collection
   alternates a pooled parse (many cores) with a commit (one core). This
   is the audit's first open item and the only one that raises the
   AVERAGE rather than the peak.
3. **Occupancy is 77 %, not 100 %** — the tail of each batch, which is the
   straggler problem the reverted change tried and failed to fix by
   granularity. The real fix is to cut parts along the origin blocks.

Beyond those the ceiling is policy, not scheduling: wikipedia's embedding
is off and gutenberg is GPU-bound.
