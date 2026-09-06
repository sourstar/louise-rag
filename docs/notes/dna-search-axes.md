# DNA search axes — multi-space retrieval for the genome collections

**Status: PARKED, deliberately.** The design is settled to the point below;
the evaluation is waiting on the Fable model. Do not start building the schema
change — start with the evaluation (§7), and only amend the architecture doc
if it shows the gap is real.

---

## 1. What triggered this

The A/B that moved DNABERT-2 onto the Arc also measured, as a control, the
cosine between **unrelated random sequences: 0.98**. Related sequences score
0.999997. So the useful signal sits in the third decimal place.

That is not a bug in the server. It is the textbook signature of an
**anisotropic** embedding space, and it follows directly from the training
objective.

## 2. Why the space is anisotropic

DNABERT-2 is a **masked language model**. MLM only requires the hidden states
to carry enough information to reconstruct masked tokens. Nothing in that
objective asks unrelated sequences to sit far apart, so mean-pooled vectors
collapse into a narrow cone. This is the same "representation degeneration"
that makes vanilla BERT bad at semantic search, and the reason SimCSE and
sentence-transformers exist in NLP.

## 3. DNABERT-S is NOT the fix

DNABERT-S is DNABERT-2 plus contrastive training. Architecturally identical —
117M params, dim 768, same server, same container, only `MODEL_ID` and the
pinned revision change. Operationally a drop-in; in our terms a new
`space_id`, so all three genome collections re-embed from zero.

**But it contrasts along the wrong axis.** Its training treats *two
non-overlapping chunks of the same genome* as a positive pair. Two random
stretches of hg38 are not homologous — the model is simply told to make them
alike. So what it learns is genome-of-origin identity: composition, codon
bias, k-mer signature. Ideal for metagenomic binning; wrong for us on both
counts §9 names:

- The dense leg is **queried by sequence** for read placement. That needs
  chunks of hg38 to be distinguishable *from each other*. DNABERT-S is
  explicitly optimized to make them all look like "hg38".
- §9 wants **one shared space so cross-species search works natively** (human
  gene → mouse ortholog → knockout phenotype). A model trained to push species
  apart in embedding space works against that.

This is reasoning from the training objective, not a measurement. It is the
first thing §7 should try to falsify.

## 4. Three similarity axes, and they do not rank alike

"Similar" means at least three different things for DNA:

| axis | "these two are close" means | good for | needs a model? |
|---|---|---|---|
| **homology** | the letters descend from a common ancestor — near-identical, or diverged with indels | read placement, ortholog finding | **no** — k-mer / minimizer sketch, Jaccard or containment |
| **function** | they do the same job — both promoters, same protein domain — even with different letters | annotating an unknown sequence | **yes** — the only one that does |
| **composition** | same GC content, codon usage, k-mer spectrum | telling species apart | **no** — k-mer spectrum |

**Two of the three need no neural model at all.** That is what BLAST,
minimap2 and Mash do. A sketch is deterministic, has no model-file hash, no
dtype drift and no anisotropy, and costs no GPU — it lands cleanly on
invariant 3. Its `space_id` pins `k`, sketch size, canonicalization and hash
function: a *cleaner* space definition than a neural one, not a messier one.

DNABERT-2 does not give literal-sequence similarity either. An MLM's pooled
vector is not edit distance; it is an unshaped mixture of all three axes,
which is exactly why the cone is so narrow.

And abstraction is not purely a liability. For exact read placement, vectors
compete with BWA-MEM and minimap2, which are decades-optimized and exact.
Where vector search earns its place is where exact alignment fails — the
diverged ortholog at ~85% identity with indels, the functionally equivalent
element with different letters.

## 5. The real architectural finding: invariant 4 has no third category

Invariant 4 is a binary. Facts-by-key go into context as pinned blocks; ranked
lists **always** go through RRF. Multi-axis search creates a third thing it
cannot express: **two ranked lists that are both rankable and must NOT be
fused.**

Fusing a homology ranking with a functional ranking yields a list that answers
neither question. "Where did this read come from" and "what does this sequence
do" have different correct answers for the same input. The §9 phrase — "merged
only where merging is meaningful" — already covers this in spirit, but the
invariant's letter says fuse everything rankable.

Options:

1. **Route by intent** — the query declares its axis, one leg runs. Cheapest,
   but the same sequence genuinely carries both questions.
2. **Fuse within axis, separate across** — RRF the legs that answer the same
   question, return distinct result sets otherwise. Needs a real amendment to
   invariant 4 and a UI that shows two answers. **Recommended**: §9 puts both
   the ortholog case (homology) and the annotation case (function) in scope.
3. Fuse everything and hope. No.

This decision is bigger than the storage question and should be settled first.

## 6. What it costs, and what it does not

**Storage: more tables, not more engines.** Invariant 6 pins SQLite + LanceDB.
Multiple spaces are multiple Lance tables in the same LanceDB keyed by
`space_id`. Per-table metrics are native — cosine for the learned leg,
Hamming or Jaccard for binary sketches — so Lance covers all three axes. The
only thing that would justify a new engine is true alignment (FM-index / BWT),
and that is a different project.

**The code is one-space-per-collection today**, so this is a schema change
beyond the doc (CLAUDE.md: ask first):

- `recipe_for()` (`app/collections.py:120`) returns exactly one `EmbedRecipe`
  → must return named legs.
- `app/embeddings.py` writes to a single hardcoded `TABLE` → per-space table.
- the dim check in `app/spaces.py:227` reads `chunks` → per table.
- **`embed_done` in the documents table becomes per-leg.** A document can be
  sketched but not embedded. This is the part that touches idempotence and
  resumability (invariant 2), and the only part needing real design care — the
  rest is plumbing.

## 7. Sequencing — do NOT change the schema to find out whether the idea works

1. **Measure the existing dense leg on both axes.** Near-zero cost, no schema
   change, tells us what we actually have:
   - *homology*: take a region, mutate it or take an overlapping window, and
     check the true locus outranks unrelated chunks.
   - *cross-species*: a known human/mouse ortholog pair against background.
     Does the ortholog rank above noise?

   The 0.98 cosine may be cosmetic. Invariant 4 sends corpus legs through RRF,
   which is **rank-based**, so absolute values never reach the fusion — a
   compressed range can still rank correctly. If it does, the only real cost
   is ANN index quality, since a narrow cone partitions poorly and hurts
   recall.

   **If homology passes but cross-species fails, the dense leg is a slow BLAST
   and the interesting half of §9 does not work.** That is the result to watch
   for.

2. **Compute k-mer sketches for the same test set offline, in a script.** No
   schema change and no new dependency — a k-mer sketch is ~30 lines of numpy.
   This closes the gap that matters most right now: **there is no baseline.**
   We cannot currently say whether DNABERT-2 beats a plain k-mer sketch at
   anything on our corpus, which makes every model decision — DNABERT-S,
   Evo 2, whitening — an argument rather than a measurement.

3. **Only if step 2 shows a real gap**, amend the architecture doc —
   invariant 4's third category and the multi-space schema together — and then
   build.

## 8. Other options on the table

- **Whitening.** Subtract the corpus mean and apply a whitening transform. The
  standard cheap fix for anisotropy; it spreads the cone without retraining.
  It changes the vectors, so it is a space change and must apply identically
  to the query encoder — but hours, not weeks.
- **Evo 2** — the model §9 actually named, chosen because it is trained across
  all domains of life so cross-species works natively. DNABERT-2 was the
  pragmatic stand-in. The catch: StripedHyena with custom CUDA kernels, so
  porting to XPU is a real project, not a container swap.
- The paper §9 cites for read placement (Embed-Search-Align) reached parity
  with BWA-MEM using contrastive fine-tuning on a *positional* objective. So
  the general shape — MLM backbone plus contrastive training — is right;
  DNABERT-S just contrasts along the wrong dimension.
