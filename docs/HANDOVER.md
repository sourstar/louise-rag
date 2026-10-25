# Handover

*Written 2026-10-25, when the project changed hands. This page gets you
oriented. The detail lives in the documents it links.*

## 1. What you are taking over

Louise is a self-hosted RAG platform. Each node holds collections (books,
medicine, encyclopedias, law), answers questions over them with quoted
passages, and shares the documents and the embeddings with other nodes
peer to peer. Whatever it receives it can verify against the original
source. The [README](../README.md) gives the purpose and the architecture
on one page.

## 2. Read in this order

1. [`CLAUDE.md`](../CLAUDE.md): the rules. Twelve invariants, each with how
   it is enforced; the engineering rules, each paid for by an incident;
   the operator's two standing rules (RULE 1: take the shortcut that
   gives identical bytes; RULE 2: interfaces ask only what the user alone
   knows). It is written for an AI assistant and binds a human just the
   same.
2. [`docs/README.md`](README.md), then 01 to 09: the specification. The
   vocabulary of 01 and 07 is the vocabulary of the code; 09 fixes every
   byte that is hashed, signed or moved. Where code and spec disagree, the
   spec wins until it is amended, and every amendment is dated in place.
3. [`docs/notes/STATUS.md`](notes/STATUS.md): where things stand, newest
   first, and what cost time.
4. [`docs/notes/handover-worklist.md`](notes/handover-worklist.md): every
   known gap between spec and code, its decision (*build* or *amend*), and
   what is struck through as done.
5. Before touching scheduling, encoders or measurements, read
   [`scheduling.md`](notes/scheduling.md),
   [`approved-encoders.md`](notes/approved-encoders.md) and
   [`measuring.md`](notes/measuring.md). CLAUDE.md says why.

The other files in `docs/notes/` are the engineering record. A note
replaced by the specification says so in its first line.

## 3. The code

| where | what |
|---|---|
| `app/louise.py` | the command tree (`hold`, `share`, `ask`, `node`, `dev`, `user`); one function per command |
| `app/hold/`, `app/share/`, `app/ask/` | the three surfaces (07 §7.4 lists the modules per noun) |
| `app/worker.py` | the node's rounds over every held collection: acquire, build, index, embed, share |
| `app/api.py`, `app/web.py`, `app/api_commands.py` | HTTP: the pages, the explicit routes, and one route per command |
| `app/plugins/<source>/` | one plugin per origin (06) |
| `app/vega/`, `deploy/vega/` | the Vector Gate in front of the model runner (03 §3.3) |
| `deploy/` | Docker, TrueNAS, LocalAI, scheduled jobs |
| `tests/` | about 5,400 tests, no live model anywhere |

`python -m app.louise --help` walks the tree. Two tests keep the spec
honest: `tests/test_commands_in_spec.py` (every command and flag is in
07) and `tests/hold/test_check.py` (every check 03 names answers).

## 4. Running it

- Tests: `python -m pytest -q -n 4 tests/`. On a machine with less than
  32 GiB free, set `VECTORS_MIN_FREE_GB=1`. Otherwise the vector store's
  disk floor fails about 70 tests that have nothing to do with your change.
- A node: README, "Run a node on any Docker host". `louise node start`
  runs the worker and the web interface; `louise node stop` asks the
  worker to finish its units and exit 0.
- The reference node (München, TrueNAS, Intel Arc Pro B70):
  [`node-runbook.md`](notes/node-runbook.md). The machine's addresses and
  the ssh key's path are in `deploy/reference/DEPLOYMENT.md` on the
  operator's machine, never in the repository. Secrets are referenced by
  path only (invariant 8).

## 5. How work is done here

- **"Fix it after" is never an option.** A known defect stops everything
  until it is fixed. Nothing published is recalled, so a wrong artefact
  lives on in every peer that adopted it.
- **Measure before changing.** A scheduling change without numbers in its
  commit is a guess (`louise dev schedule-report`). Use the harnesses that
  exist (measuring.md).
- **Every change ships tests, and keeps every existing test green.**
- **Questions to the operator** have one shape: what the thing is, a
  table of choices with pros and cons, the recommendation, and what
  happens if they say nothing.
- **Do not reinvent the wheel.** Use the standard or the library call,
  name it, and write down the alternative when you build your own.

## 6. Open at handover

What the code still lacks, and the decisions that are the operator's, are
in [`handover-worklist.md`](notes/handover-worklist.md) §1, §2 and §6. The
node's own list is in [`node-runbook.md`](notes/node-runbook.md) §14. At
the time of writing:

- **Waiting on the operator:** pubmed's OtherAbstract records; a
  periodic snapshot of the origin mirror.
- **Waiting on the node:** wikidata's re-creation (Monday 2026-10-26);
  gutenberg's re-index of 127 parts, then its parser-11 re-creation
  ("every plate"); the wikipedia-v8 records build; the reranker's HTTP 500s
  under load; the counts behind the dateless-record fix.
- **Parked by the operator, kept as blueprints:** arXiv
  ([`arxiv-plugin.md`](notes/arxiv-plugin.md),
  [`arxiv-collection.md`](notes/arxiv-collection.md)) and the knowledge-base
  pipelines (the Gutenberg × Wikidata graph the README calls "under
  construction"; STATUS, 2026-10-22).
