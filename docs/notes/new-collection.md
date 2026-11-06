# Making a collection: one workflow

*Operator, 2026-11-06: "A mess up in a collection embedding is the most
severe flaw we can have, this is not acceptable. I expected that a clear
workflow for the creation of a new collection had been implemented." And:
"Any new case not covered must be added to this framework. The base idea is
at some point to also make a wizard."*

**The rule.** Every collection is made by one workflow:
`louise hold collection new` on the command line, its routes (Library,
LocalAGI, OpenAI vector stores), and later its wizard. It is never made by
commands chained by hand.

- **What the operator gives:** only what only the operator knows. That is
  the local name, plus the plugin and recipe for a new space.
- **Everything else is derived and checked:** the endpoint, the models, the
  CPU twin, the window, the cut, the instruction and the tolerance.
- **Embedding stays off** until the gate has passed and the operator turns it
  on.

## The steps

Every case runs a subset of these, always in this order. Each step is
checked against the one before it, and each says what it found.

| # | Step | What it does | Refuses when |
|---|---|---|---|
| 1 | resolve | Reads the recipe or the source collection. Finds the endpoint, the models and the CPU twin on the node. | A twin does not state a CPU run of the same weights. |
| 2 | check | Checks the target before anything is measured. A collection of that name already made is held to the recipe by `space_id`. | The platform image is unknown, or an existing collection is another space. |
| 3 | calibrate | The card against the CPU twin, for each role (document; also query when the space has a query encoder). Uses the space's own ctx, cut, instruction, dtype, and set or pairs. The CPU lane is kept in the node's store. | An endpoint does not answer (exit 3). |
| 4 | tolerance | Takes the measured tolerance. A recipe that states none takes it. | The recipe states another tolerance: a tolerance is measured, never typed. |
| 5 | make | Does one of: create, copy a held space, reuse a held reference, deposit, annotations, peer fetch. | The plugin or recipe is refused (07 §7.3). |
| 6 | carry | Re-creation only: takes from the predecessor what is provably identical (RULE 1). | — |
| 7 | cpu-reference | Writes the collection's copy of the CPU reference from the store. Nothing is computed twice. | The twin is not reproducible. |
| 8 | gate | `hold embedder check`, for each role. | The gate fails (exit 1). |
| 9 | swap | Re-creation only: forgets the predecessor and takes its name. | — |

The workflow is resumable. Run it again after a stop: a step already done is
checked, not redone, and the CPU lane resumes from the store's journal.

## The cases

The operator gives the first column; the workflow finds everything else.

| Case | When | The operator gives | Steps | Example |
|---|---|---|---|---|
| **A. A new space** | A plugin and a recipe of its own | plugin, recipe, name | 1–5, 7, 8 | wikidata on Nemotron |
| **B. A new space with its own question encoder** | `space.query.encoder` in the recipe | as A | 1–5, 7, 8, for both roles | — |
| **C. A pair space** | The calibration set is query–document pairs | as A, plus the pair file | as B | pubmed (MedCPT) |
| **D. The same document side as a held collection** | A re-creation whose document side is unchanged | as A, plus the held collection | 1, 2, 5 (the member reused), 7, 8 | wikidata, parser 2 |
| **E. A copy of a held space** | A second collection in an existing space | plugin, recipe, the space's collection, name | 1, 2, 5 (copied), 7, 8 | a second collection in wikipedia's space |
| **F. A re-creation** | A collection replaced, nothing official published from it (RULE 1) | as A, D or E, plus the predecessor | that case, then 6, 9 | wikipedia-v8 → wikipedia |
| **G. A private collection** | LocalAGI or an OpenAI vector store; not shared | name only | as E (the served model's held space); the first on an encoder is A | `notes` |
| **H. Annotations** | Claims about another collection; no encoder | claims file, name | 1, 2, 5 | gutenberg-book-editions |
| **I. A peer's collection** | A copy fetched from a peer | collection id, name | 1, 5 (fetched), 7, 8, on this node's encoder | Perugia's gutenberg |
| **J. Sister, daughter, corrective** | Lineage (02 §2.7) | the mother, plus what changes | as A, B or C into a new space; else 5, 7, 8 | none yet ("No official daughters") |

### Simple examples

```bash
# A — a new space (wikidata on Nemotron)
louise hold collection new wikidata --recipe recipes/wikidata-nemo.json --name wikidata \
    --encoder http://node:30286 --api vega --embed-model vega-nemotron-3-embed-1b \
    --cpu-model vega-nemotron-3-embed-1b-cpu

# G — a private collection, from LocalAGI or any OpenAI SDK: only a name
curl -X POST http://node/api/collections -H "Authorization: Bearer $(cat deposit.token)" \
     -d '{"name": "notes"}'
python -c 'from openai import OpenAI
OpenAI(base_url="http://node/v1", api_key=open("deposit.token").read().strip()).vector_stores.create(name="notes")'
```

The other cases take the same command with the case's own inputs, shown in
§"Status" as each one is built.

## Adding a case

A situation that no row above covers is a NEW CASE, and it is added here
before it is used. It is never run as a chain of commands.

1. Declare it in `app/hold/workflow.py`: what the operator gives, what is
   derived, its steps, and one example.
2. Use the existing steps. Add a step only when none does the job, and give
   it its refusals.
3. Add its row above, with a simple example.
4. Tests:
   - its own tests;
   - `tests/test_creation_framework.py` fails when a case is declared but not
     documented here;
   - the same test fails when any code writes a Codex outside the
     framework's steps.

## The wizard

The wizard comes later, on the same declarations.

- It asks only a case's "operator gives". The Library asks only the name
  (RULE 2).
- It shows what the workflow derived (the endpoint, the twin, the window,
  the cut).
- It runs the steps with their progress, and stops at the first refusal,
  saying it in plain words.

## Status

| Case | State |
|---|---|
| A | Built: `new` (#331, #333, #334). Not yet run on the node (wikidata, 2026-11-06). |
| B–J | Planned, phases below. |

**The plan.** Each phase ships with tests and is measured on the node before
the next one starts.

1. **The framework.**
   - `app/hold/workflow.py`: cases, steps, and a state file per collection (`state/new.json`).
   - `new` rebuilt on it, without changing its behaviour.
   - An existing collection is held to the recipe by `space_id`, not by
     five fields.
   - Calibrate uses the space's dtype.
   - The guard test, its allow-list naming each path not yet moved.
2. **E, D, G, I.**
   - The space copy, the reused reference, the private collection (the
     LocalAGI and OpenAI routes go through the workflow), the peer copy.
   - Each ends with the CPU reference and the gate.
3. **B, C, F, H.**
   - The query role measured (calibrate measures the document side only
     today), and pairs.
   - Re-creation with the carry and the swap.
   - Annotations.
4. **J; the hand route closed.**
   - Lineage.
   - `create`, `create deposit`, `derive`, `continue`, `replace`, `share
     fetch` and the Library's create become steps of the workflow.
   - The guard test's allow-list is empty.
5. **The wizard.** The command line (`new --wizard`) first, then the Library
   dialog.

## What the review found (2026-11-06)

| Finding | Where | Fixed in |
|---|---|---|
| Calibrate cut by characters a space that cuts by tokens; the store keyed no cut | calibrate, cpu_reference | #327 |
| An existing collection compared by engine id, not weights | `new` | #333 |
| The target checked after the measurement; the name optional | `new` | #334 |
| An existing collection held to five fields, not its `space_id` (engine, dim, dtype, pooling, layers unchecked) | `new` | phase 1 |
| A recipe with any `space.query.encoder` needs the query side; `new` fails at create, after measuring | `new` | phase 1 (refused first), phase 3 (built) |
| Calibrate measures in f32, whatever the space's dtype | calibrate | phase 1 |
| The gate passes with no CPU comparison when no CPU reference is kept | gate | phase 2 (every case makes one) |
| The Library's create sets a hand tolerance, makes no CPU reference, runs no gate, and embeds on demand | Library | phase 4 |
| A private collection's first space (`[deposit].space`) carries a hand tolerance | deposit | phase 2 |
| A space copy re-embeds the reference and only warns when it differs | `create --space-from` | phase 2 (refused) |

**Not covered, on purpose.** These are outdated and are not cases:
- Codex versions below 3 (image-based encoders);
- a whole Codex given as JSON to `create`;
- tolerance "by decision";
- `pack build`;
- the hand chain calibrate → create → cpu-reference → check.
