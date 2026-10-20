# Rebuilding `louise-python`: the runbook

*Written 2026-10-20 for the first rebuild after the consolidation: the
image on the node (2026-10-17, `sha256:9eb8807c…`) has no `jina-v3` task,
and `wikidata` needs it (wikidata-build-plan.md §2). München asked for an
installer and a runbook; this is the runbook, deploy/jina-embeddings-v3/
fetch.py the installer. Steps 1–3 are the general procedure for any rebuild
of this backend; §4 is jina-v3's own.*

## 0. Why a rebuild is a flag day for every model on the backend

A backend engine's `image` is the digest of its INSTALLED FILES, hashed at
every load (vega.md §4, 09 §9.3). A new tree is therefore a new engine and
a new run for every model LocalAI serves from it — on the node today:
vega-dar-legal-it (normattiva's document encoder), vega-medcpt-article-ov
(pubmed-v8's document encoder) and its `-cpu` twin, louise-translate-madlad
and louise-rerank-medcpt; München also names vega-medcpt-query. Each
pinned collection keeps the statement of the run it embeds with; the new
run matches none of them, so each collection's encoder goes through the
calibration gate once more (03 §3.3, `hold embedder check`), and a gate
that fails stops that collection's embedding — not its searches.

RULE 1 says what to expect. The four live tasks' code is unchanged — the
rebuild adds `task_jina_v3` beside them and nothing else — so their
vectors are the same bytes and the gate passes once with nothing
re-embedded, as the consolidation itself did (backend-consolidation.md:
normattiva 16/16 worst 1.000000, pubmed 16/16 worst 1.000000). That is
the expectation to CHECK, not to assume: the pins in requirements-*.txt
are the same, but a wheel index can serve a rebuilt wheel under the same
version, and `pip freeze` in the build (the Dockerfile prints it) is the
first thing to diff against the 2026-10-17 build's.

The cost is GPU time in a window, the risk is a live collection paused on
a gate failure, and the mitigation is that the old tree stays installed
and the switch is one YAML line per model with a backup beside it
(deploy/vega/switch/backends-20261017.sh is the pattern).

## 1. Before the window (any day; nothing on the card)

1. **Build and save** (Dockerfile header; the context is the repository
   root at the gated commit — name it in the report):
   ```
   docker build -f deploy/localai/backends/louise-python/Dockerfile --build-arg VARIANT=intel \
     -t louise-python:intel-20261020 .
   docker save louise-python:intel-20261020 -o /mnt/tank/apps/localai/backends/.louise-python-intel-20261020.tar
   ```
   Keep the build log: `pip freeze` and the `torch … transformers …
   openvino …` line are the record to diff against 2026-10-17's.
2. **The weights folder**, on the read-only weights mount, by the
   installer (standard library only; runs on the host):
   ```
   python3 -I deploy/jina-embeddings-v3/fetch.py /mnt/ssd5/apps/models/jina-embeddings-v3
   ```
   It writes the five weight files and the ten remote-code modules as
   regular files (huggingface_hub's cache writes symbolic links, which
   `weights_digest` refuses), checks every file against the commit's tree
   and tokenizer.json against the `wikidata-text` layer's sha256, and
   prints the **weights digest** — the recipe's `space.encoder.weights`.
   **Done on the node 2026-10-20 (München)**: code commit
   `bd55a5ec8e6c0fb1d6c26efb4b6a4a74ce8a88d3`, digest
   `sha256:f0f752aea9e660f8120e2cb7ae32377794c45e53bfbeee3f1f6ecf36d7c0c572`
   (16 files, 0 symlinks, 1.1 GB, equal to `app.vega.runs.weights_digest`
   in the deployed image); both are pinned in fetch.py now, and a folder
   with another digest is refused. The target already existed from the
   2026-10-18 cosine measurement, with the files the layout excludes
   (1_Pooling/, README.md, modules.json, custom_st.py,
   config_sentence_transformers.json, the code's README and converter);
   the installer refused it and München moved it aside as
   `jina-embeddings-v3.pre-installer-20261018` — nothing deleted, the
   operator's to remove.
3. **The model YAMLs**: deploy/vega/stage/models/vega-jina-v3.yaml and
   vega-jina-v3-query.yaml, placed through the API as the others were
   (vega.md §12 — the call drops comments; the file goes back afterwards).
   `context_size` is the model's 8,192, not the space's ctx (the YAML says
   why). Both NOT pinned.
4. **The names**: the new tree `louise-python-20261020`, its wrapper
   `vega-louise-python-20261020`. The wrapper package is rebuilt for the
   new name (deploy/vega/stage/build_vega_package.sh, `BACKEND=
   louise-python-20261020`, the rest as 2026-10-17) — a wrapper names its
   real backend in vega.json and nothing else changes in it.
5. **The BEFORE record**: `louise hold embedder run --encoder <VEGA URL>
   --model <m>` for each of the live models (the run id, the engine's
   `image`), and the receipts service's tree for that image
   (`GET /vega/trees/<image>`, saved to a file) — `hold embedder tree-diff
   BEFORE AFTER` names what moved if a gate fails.

## 2. In the window

Order matters; each step waits for the one before.

1. `louise node pause` — the worker finishes its units (07 §7.7); note the
   mode it had, `node show` prints it.
2. Unload the five models (`POST /backend/shutdown` per model, as
   rollout-p2.sh step 4), so none is mid-load while its backend changes.
3. Install the new tree BESIDE the old one, as LocalAI installs any
   (backend-consolidation.md; rollout-p2.sh step 3):
   ```
   curl -sf -X POST "$LOCALAI/api/backends/install-external" -H "Content-Type: application/json" \
     -d '{"uri":"ocifile:///backends/.louise-python-intel-20261020.tar","name":"louise-python-20261020"}'
   ```
   wait for `run.sh` under the backends path; then the wrapper package
   the same way under its name.
4. Switch each model's `backend:` line to `vega-louise-python-20261020`
   (or `louise-python-20261020` for the two non-VeGa models), with a
   `.bak-20261020` copy beside each file and the line-count check
   backends-20261017.sh does. Nothing else in the YAMLs moves.
5. **The gate, per collection**: `louise hold embedder check normattiva`,
   `louise hold embedder check pubmed-v8` — each loads its model on the new
   tree, states the run, and must print PASS 16/16 with worst 1.000000 as
   on 2026-10-17 (a worst below 1 on the same code is itself a finding:
   the tree-diff from step 1.5 says what moved). The statement is written
   with the check (`--accept` has nothing left to do on the runs path).
6. **MADLAD and the reranker** have no calibration gate: run the
   consolidation's own A/B (the same 42 segments through
   `/v1/completions`, the same rerank request) against the saved outputs
   of the old tree and record identical / differing counts; the old tree
   flipped two segments between its own runs, so "41/42" is the bar, not
   "42/42".
7. `louise node resume`. The node is back on the new tree; the old one
   stays installed and unused until the operator says to remove it
   (`POST /backends/delete/<name>`, as 2026-10-17).

## 3. Fallback — on ANY failure in step 5 or 6

Restore the `.bak-20261020` YAMLs (every one, not the failing model's
alone: one tree serves them all), unload, `node resume`, and report the
failing gate's lines and the tree-diff. The new trees stay installed and
unused; nothing was embedded on them, since the node was paused. The
collection that failed stays as it was before the window. "Fix it after"
is not an option (CLAUDE.md): the window ends there, and the next one
opens when the cause is known.

## 4. jina-v3: the measurement before the collection (invariant 12)

Only after §2 is green. The task is new on the card, so nothing about it
is assumed:

1. Load vega-jina-v3 (one call; the task warms its kernels). Its receipt
   is the stated run — `louise hold embedder run --encoder <VEGA URL>
   --model vega-jina-v3` prints `run`, `engine`, `weights`; the same for
   vega-jina-v3-query (`--query`). These are the recipe's `<<run>>` values,
   and `weights` must equal the installer's digest.
2. **Bitwise reproducibility, env UNSET** (approved-encoders.md's method
   on the B70: `capture.py` + a byte compare of f32): the 512 calibration
   texts, two fresh loads × two shuffled orders, through VeGa so each
   answer carries a receipt. Pass: 512/512 bit-identical across all four.
3. If any vector differs: add `"LOUISE_TORCH_DETERMINISTIC": "1"` to the
   YAML's `env` (deploy/jina-embeddings-v3/server.py switches
   `torch.use_deterministic_algorithms(True)` and oneDNN's deterministic
   primitives on from it; an op with no deterministic kernel then fails
   the text, which the measurement will see) and measure again. The
   stated run carries the env, so the recipe's run is the one measured.
4. Record both results in approved-encoders.md ("Bitwise reproducibility,
   by vendor", the torch row) with the numbers. A model that is not
   bit-identical under either setting is NOT the collection's encoder:
   stop and report; the collection is not created (invariant 12 is a
   pillar, not a tolerance).
5. **The function is the measured one**: the 2026-10-18 reproduction of
   Wikimedia's archive vectors (cosine 0.999999 on CPU, gpu-silent-wrong-
   vectors.md) ran from a huggingface_hub cache. Repeat it on a handful of
   entity texts from the installed folder — the same vectors to 1e-4 — so
   the folder's layout is shown to compute what the cache did.
6. **`work.tokens`**: the throughput of step 2 (tokens/s on the card, one
   text per call) × the collection's embedding window per month, as the
   recipe template asks.

## 5. What to report

| item | value |
|---|---|
| commit built, image tag, `pip freeze` diff against 2026-10-17 | |
| installer: code commit resolved, weights digest | |
| BEFORE/AFTER tree digests (`image`) | |
| normattiva gate: run, 16/16, worst | |
| pubmed-v8 gate: run, 16/16, worst | |
| MADLAD, reranker A/B | |
| jina-v3: run ids (document, query); 512-text measurement env unset / set | |
| jina-v3 vs Wikimedia's vectors, n texts, min cosine | |
| `work.tokens` and the throughput it came from | |
| wall clock per step; the mode restored | |

## 6. Follow-ups this leaves

- The wrapper's `DETERMINISM` table (deploy/vega/vega_wrapper.py) has no
  torch row: once §4 says which setting reproduces, the row is added and
  the wrapper refuses a torch card run whose model env lacks it — the
  same enforcement the SYCL and ROCm rows have. DAR-legal-it on the same
  backend was measured 1/4 bit-identical against ITSELF on 2026-10-17
  (max abs 8.2e-8): invariant 12 is open for every torch model, not only
  the new one, and the same measurement is owed there.
- `/mnt/ssd5/apps/models/jina-embeddings-v3.pre-installer-20261018` (1.1 GB,
  the 2026-10-18 measurement's folder) is moved aside, not deleted: the
  operator's to remove.
