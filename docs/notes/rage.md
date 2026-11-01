# RAGE — the RAG Engine

*Named by the operator, 2026-11-01: "the collections common engine shall be
named RAGE: 'RAG Engine'. This name has been used in the past for VeGa, be
sure there will be no misunderstandings in the files. Make its api naming
accordingly."*

## 1. What it is

RAGE is Louise's own engine: the one code path that every collection's
embedding, and every question's encoder call, goes through — the same for
every collection and every model to come (operator, 2026-11-01: "a
testbench for all the collections and models to come"). It:

- **chooses** which documents to embed and when (the feed and its lanes);
- **fills** requests: up to `batch` texts a request, `concurrency` requests
  in flight, the texts of several documents in one request;
- **shares** the card between collections, and lets a person's query go
  first without breaking an embedding or a query in the middle;
- **admits** an answer only from a validated run (03 §3.3);
- **measures** itself: the card's busy share, who held it, the gaps, the
  query-under-load harness.

**The base rule** (operator, 2026-11-01, step 2 of the GPU programme: "these
optimizations are only part of the common collections backend (that is not
the localai backend, but louise's engine…)"): how collections are embedded is
improved in RAGE, for all of them at once, never in one collection's code and
never in the model server. A collection carries at most RECOMMENDED RAGE
settings (09 §9.19), which a node may take and its gate checks.

## 2. What it is not

- **Not a model server.** RAGE runs no model (invariant 7). The models run
  behind VeGa (the receipts) and LocalAI (the runner, which alone manages
  the card's memory).
- **Not an *engine* in 09's sense.** An engine (01; 09 §9.3) is always the
  ENCODER's: build, image, args, env, device, frozen in the Codex and inside
  `collection_id`. RAGE's settings — `batch`, `concurrency` — are how RAGE
  drives that engine; they are in no id, and the gate measures them before a
  batched vector is written (`run_check.contributing`).
- **Not the model pool.** See §5.

## 3. Vocabulary

| say | means | never say instead |
|---|---|---|
| RAGE | the RAG Engine, Louise's own, this note | — |
| engine | the encoder's engine (09 §9.3) | for RAGE |
| RAGE settings | `[encoder] batch` and `concurrency` (07 §7.8), recommended per card type in `rage-settings/` (09 §9.19) | "engine settings" |
| the model pool | the container that ran every model, 2026-09-20 → 2026-10-18 — named RAGE then — VeGa's predecessor ([model-pool.md](model-pool.md)) | RAGE |

## 4. Its parts and its API

`app.rage` is RAGE's namespace. What already lives there is its own; the
rest is named here and moves in when its code is next changed (wrap, do not
rewrite — each still sits beside the code it calls).

| part | what it does | where |
|---|---|---|
| share and priority | a gate per encoder URL, levels CORPUS < TRANSLATE < QUERY < RERANK < CHAT, the interactive claim, standing aside | `app/rage/dispatch.py` |
| settings in effect | the file's `[encoder]`, then the recommendation for this card, then the node's defaults | `collections.recipe_for` → `app/rage/settings.py`; the signed file `app/hold/rage_settings.py`; `louise hold rage-settings show\|record` |
| record | one line a second: the card's busy, each gate's seconds held per collection, backlogs, queries | `app/rage/card_record.py` (reads the GPU probe's `/busy`, `app/gpu_busy.py`, `app/gpu_pmu.py`) |
| feed | the documents, the lanes, the window of documents in flight | `app/worker.py` `_embed_producer`, `_embed_parallel` |
| fill | requests filled across documents | `app/embeddings.py` `Coalescer`, `coalescing`, `batch_size` |
| admission | the gate, and the run check before each write | `app/run_check.py`, `app/hold/runs.py` |
| bench | searches under corpus load; the card per minute | `tools/measure/query_under_load.py`, `louise dev schedule-report --gpu` |

The standards it uses instead of its own inventions: dynamic batching as
Triton Inference Server's dynamic batcher and TEI do it (fill), the Linux perf
PMU for the card's busy share (record), deficit round robin for the card's
budget between collections (share, next step), the scheduling notes'
DRR/AIMD/CFS ([scheduling.md](scheduling.md)).

## 5. The name's history, and what still says "rage"

- **2026-09-20 → 2026-10-18.** RAGE named the model pool (before 2026-09-20
  "the model pool"). VeGa took its run identity, LocalAI its lifecycle; it
  retired on 2026-10-18, when the operator asked for "no RAGE nomenclature
  left" and the node's app `louise-rage` became `louise-vega`.
- **2026-11-01.** The name is RAGE's, the RAG Engine's, alone. Every file was
  rewritten that day: the old RAGE is "the model pool", its four notes are
  `model-pool*.md`, and an operator's quotation keeps its words with
  "[the model pool]" beside "RAGE".

What still says `rage` and means the model pool — names of things outside
this repository, or of its past, kept because renaming them changes nothing
here:

- the node's datasets `/mnt/ssd5/apps/louise-rage/` (VeGa's `state/vega`,
  LocalAI's `state/localai-models`) and `/mnt/ssd5/apps/louise-rage-15c/`.
  Renaming them is a node operation (stop VeGa and LocalAI, `zfs rename`,
  both apps' host paths); written down, not scheduled;
- the registry image `ghcr.io/sourstar/rage`, pinned by digest in
  `deploy/bakeoff-st/Dockerfile.xpu`;
- paths in commits before 2026-10-18 — `app/rage/{front,admission,placement,
  store,model,clock,swapyaml,runs,pairs}.py`, `tests/rage/`, `deploy/rage/`,
  `rage.toml`, `rage.d/`, `tools/rage_smoke_*.py`. Today's `app/rage/` shares
  no file name with them;
- commit messages before 2026-11-01, and the removed route
  `/admin/system/rage-alerts` (a test keeps it removed).

`louise-rag` is not RAGE either: it is Louise's own old name — the image,
the TrueNAS app, the dataset `/mnt/ssd4/apps/louise-rag`, and the signed wire
constants `louise-rag/bundle-sig/v1`, `louise-rag/vectors/v1`,
`louise-rag/beacon/v1`, which never change. The repository is renamed
`project-louise` (operator, 2026-10-25 and 2026-11-01); only its URL changed
in the code.
