# Handover worklist

*Opened 2026-10-25 (operator: "I want to complete the open developments and
have a consistent set of rules, documentation and code. I want to hand over
the project to someone else"). Parked work stays parked, documented as
blueprints (operator: A): arXiv ([arxiv-plugin.md](arxiv-plugin.md),
[arxiv-collection.md](arxiv-collection.md)) and the knowledge-base
pipelines (STATUS, 2026-10-22).*

Each item has an owner: **CODE** (done in the repository), **NODE**
(needs the live node; München), **OPERATOR** (a decision), **EXTERNAL**
(someone outside the project). The decision for a gap between spec and
code is either *build* (the code catches up with the spec) or *amend*
(the spec is changed to say what the code does, and why). Items are struck
through with the commit that closes them.

## 1. In flight (2026-10-25)

| item | owner | state |
|---|---|---|
| Every plate carried; display admits JPEG/PNG only (operator) | CODE | being built |
| A web source's page kept byte-exact (operator) | CODE | being built |
| The Library's dialogs ask only a name (RULE 2) | CODE | being built |
| A deposit source's clone draws from the budget first (invariant 5) | CODE | next |
| A dateless record is a defect, never a silent exclusion (operator: A) | CODE + NODE | counts asked of München |
| Batched answers equal single-text bytes; the gate measures reproduction (operator: A) | CODE | done (c9ac138) |

## 2. Spec says it, code does not have it

| item | where | decision |
|---|---|---|
| `louise node start` / `node stop` (the worker and web started by `python -m app.worker` / uvicorn) | 07 §7.3, CLAUDE.md invariant 2 | build (thin commands over today's entry points) |
| One route per command, `/api/<surface>/<noun>/<verb>` | 07 §7.4 | **build** (operator, 2026-10-25: A): one generic route over the command tree, `--json` output, exit → status, signing verbs queued as jobs |
| The `centres`, `labels`, `model-agreement` and `evaluation` checks (INCONCLUSIVE stubs; `model-agreement` refused) | 03 §3.2 | build `model-agreement` as INCONCLUSIVE-until-built at least; mark the four "not built" in 03 |
| `index build --vector` trains and ships the centres member | 02 §2.4 (07 marks it) | mark in 02 |
| `derive` / `replace --encoder-weights --engine` | 07 §7.3 | build or amend |
| `hold config apply --set` (and 07 §7.6 telling users to run it) | 07 | build |
| `records fetch --slice/--snapshot` | 07 (marked) | build |
| `range` facet filter | 07 (marked) | build |
| `collection list --all`, `share serve <collection>` long form, `continue --through` (code: `--continues-through`) | 07 §7.3 | build (aliases) |
| A check comparing the manifest's counts and text index with the records | STATUS (open defect) | build |
| The archive-shard rendezvous (decided, archive-signing-keys.md) | notes | spec it or drop it |
| Constitution on-demand query (normattiva) | STATUS | build |
| Origin-coverage guard blind to C readers | STATUS | build or document the limit |

## 3. Code has it, spec does not say it

| item | decision |
|---|---|
| Legacy routes still served: `/api/control`, `/api/search`, `/api/verify` (07 §7.6 says replaced) | amend: list them as kept for clients, or retire them |
| Routes with no spec entry: `/api/status`, `/stats`, `/events`, `/scope`, `/discover`, `/reset`, `/books*`, `/encoders/suggest`, `/admin/library/*`, `/admin/network*`, `/admin/system*` | amend (07 §7.4: the web pages' own routes, named as such) |
| Commands not in the tree: `hold records validation`, `hold collection migrate`, `dev unindexable`, `dev adopt-bundles`, `mark-embedded`, `withdraw-covered`, `deprecate-duplicates`, `louise user …` | amend 07 §7.3 |
| Flags not in the tree: `create --calibration-from`, `continue --reslice`, `dev carry --resliced/--scan`, `vectors import --require-t3/--allow/--budget`, `index build --type/--partitions`, `share fetch --embed`, `ask traverse --collection` | amend 07 §7.3 |
| 07 §7.4's module listing (`app/node/`, `hold/config`, …) does not match `app/` | amend to the real layout |

## 4. Defects in the text or code

- ~~07 §7.3: the `reindex` entry was spliced into `drop-rows`' text.~~ (5da3dd9)
- ~~app/louise.py: 24 mojibake sequences, visible in `--help`.~~ (repaired 2026-10-25)
- ~~CLAUDE.md: "`pack build` until the migration lands".~~ (5da3dd9)
- tools/calibrate_space.py, tools/pack_convert.py: retired by 07 §7.6 — remove or say what still uses them.

## 5. Invariants enforced by review alone

| invariant | the test that could enforce it |
|---|---|
| 5 bandwidth | every module doing network I/O draws from `app/transfer` or is on a named exemption list (model calls) |
| 6 storage | no `VACUUM` on a manifest; manifests open in WAL; archive and state never under `LOUISE_CACHE_DIR` |
| 8 secrets | a sentinel secret never appears in logs or error output over startup and failure paths |
| 11 origin form | each registered plugin has its recorded origin-form note |
| 12 fresh loads | needs a GPU: stays with the measurement harness |

## 6. Decisions for the operator

- ~~The HTTP API~~ — decided: A, build the generic route (2026-10-25).
- Retention: "everything held is verifiable", what is kept following the origin (proposed A, 2026-10-25; draft: [retention-amendment-draft.md](retention-amendment-draft.md)).
- pubmed's OtherAbstract records ([pubmed-other-abstracts.md](pubmed-other-abstracts.md)).
- A periodic snapshot of the origin mirror's dataset (STATUS).
- `normattiva-archive`: create after timing one year of the export.

## 7. Node (München)

The wikidata re-creation (Monday 2026-10-26) and its records build; the
gutenberg reindex and the 127 parts; the rerank AFTER numbers and the
cause of the HTTP 500s; the live wikipedia-v8 build to its end; pubmed's
residual misses; normattiva's `records-vs-origin` re-run and its
`shard_records` measurement; the live `watchdog_busy_timeout`; the ARC
`c_max` report; retiring `backend_gate` once no live Codex is v1/v2; the
node runbook ([node-runbook.md](node-runbook.md), asked 2026-10-25).

## 8. External

LocalAI and upstream bug reports and the Wikidata report (the operator
files them); the Gazzetta's certified records (a certifier body); the
card's fan (firmware or xe driver).

## 9. ~~Notes superseded but not marked at their top~~ (5da3dd9: each now opens with what superseded it)

architecture-v2.md, p2p-rag-architecture-v2.md, p2p-discovery.md,
lockss-p2p-evaluation.md, implementation-prompts.md, pack-format.md,
no-central-node.md, certified-collections.md, lineage.md,
embedding-redundancy.md, pack-live-edge.md, embedder-pool-spec.md,
rage.md, figures-in-the-corpus.md, archive-signing-keys.md,
calibration.md, step4-vectors-study.md, step6-preflight.md — each gets a
first line saying what superseded it.
