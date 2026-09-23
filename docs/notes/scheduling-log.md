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
per-document unit cost moved too. That was luck. A throughput number
without its scenario is not evidence.

## The rules

1. **No scheduling change is deployed without a row here** — filled in
   before the deploy (the `before` half) and completed after it.
2. **Record the scenario, not just the number.** Which collections had
   work and of what kind, the node mode, where in the corpus, what else was
   running. Two rows are comparable only if their scenarios are.
3. **Prefer scenario-independent metrics.** `docs/s` depends on which
   documents; `ms/doc` per stage, pool occupancy and CPU do so much less.
   When they disagree, the per-document cost is the one to believe.
4. **One change per deploy.** 2026-09-23 shipped two and the loss buried
   the win; the good half had to be re-measured on its own.
5. **Record reverts as rows.** A change that was backed out is the most
   valuable row in the file.

## How a row is measured

Same commands every time, so the numbers mean the same thing
(`scheduling.md` §7 for the reasoning):

```bash
# worker-tree CPU over 60 s, as a share of 1200 % (12 cores)
#   — the whole process tree, so the pool's children are counted
# throughput and the share of wall time actually inside batches
bash /tmp/measure-after.sh <window-minutes>

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
| pool children busy | 1 running, 10 in `futex_do_wait`; 132 % of 1200 % between them |
| parent thread | 5 % of one core |

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
| manifest deferrals | — | 0 | |
| errors | — | none | |

**Verdict: (b) REVERTED, (a) not yet attributable.** More CPU for less work
is redundant computation, and the per-document cost — the metric that does
not depend on which articles — moved with it, which is what makes the
regression real rather than a window artefact. Which redundancy was not
isolated; the likeliest is locality (parts are cut by equal COUNT while
wikipedia's documents come out of shared multistream blocks, so every child
holding one of a block's documents decompresses the whole block).

**What this row cost, and the process lesson:** two changes in one deploy,
the loss hid the win, and (a) — which the `py-spy dump` above shows was a
real end-to-end block — has to be re-measured alone. Rule 4 above comes
from here.

---

### 2026-09-23 (pending) — admission alone, `PARTS_PER_CHILD` back to 2

**Scenario to hold constant:** same as the baseline row; measure over a
window of the same length (30 min), starting at least 10 min after the
restart so the sizer has re-converged.

**Watch:** `parse pool — N of 11 children busy` (the line added with this
change) should move from ~1.3 to near 11 while wikipedia has parse work.
`ms/doc` must return to ≈6.3 / ≈10.2 or better; if it does not, admission
is not the win the dump suggested and this row says so.

*(to be completed)*
