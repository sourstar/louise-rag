# Step 6 pre-flight — what becomes permanent the moment it runs

*Written 2026-09-08, on the `louise` branch, before step 6.*

**Read this before running `hold collection create` on real data.**

Step 6 is a point of no return and the migration plan does not treat it as
one. The moment `create` runs against the real corpus it mints a
`collection_id` and a `space_id` that are permanent: a collection is never
edited (02 §2.7), so anything wrong at that instant is wrong for the life
of the collection and for every peer that ever adopts it. A defect found
the day after costs a corrective sister and a re-embed; a defect found by a
peer costs our reputation for the collection.

The plan's structure is one-step-one-PR forward and it has no recorded
mechanism for reopening a step marked done — so these have nowhere to be
fixed and no gate that stops them reaching the identity. This file is that
gate. **Every line must read DONE or DELIBERATE before step 6 runs.**

| # | what freezes | state |
|---|---|---|
| 1 | shard numbering | **DONE** 2026-09-08 |
| 2 | the chunker's id | **DONE** 2026-09-09 |
| 3 | the chunker's parameters | **DONE** 2026-09-09 |
| 4 | `space_id`'s function | **DONE** 2026-09-09 — needed no change |
| 5 | the calibration reference | **DONE** 2026-09-09 — set rebuilt, spec amended; two guards left |
| 6 | T0's envelope gate | **DONE** 2026-09-09 — 3 runs; the SHAPE was the defect, not the constants |
| 7 | layers: one or two | **DONE** 2026-09-09 — one collection, two layers |
| 8 | credits | **DONE** 2026-09-09 — and it opened item 9 |
| 9 | the corpus and community credits | **DONE** 2026-09-09 — `credits` carries a `kind` |

---

## 1. Shard numbering — DONE

`app/pack/build.py` numbered shards from 0; 09 §9.4 types `n` as "the
shard number within the slice, FROM 1" and 02 §2.2 says "shards are
numbered 00001… per slice". Fixed 2026-09-08, with a test.

Not a filename detail: the shard path is in the snapshot's shard entry, in
the member name (09 §9.16) and in a bundle header's `shard` field (§9.7),
so two peers that number differently disagree about which shard a bundle
covers — and `space_id` would not catch it, because numbering is not in it.

## 2. The chunker's id — DONE

`app/plugins/gutenberg/source.py` declares `chunking_profile = "book"`.
06 §6.3's registry has four chunkers and `book` is not one of them:
`fixed`, `cdc`, `abstract`, `verbatim`. What the book chunker actually
does — fixed windows with an overlap, paragraph-aligned — IS `fixed`.

The id is inside `space_id` (invariant 1), so this freezes at `create`.

**It also disables T1 forever.** 03 §3.5 gates the overlap-band check on
`fixed` layers, so a Gutenberg collection whose layer says `book` is never
T1-checked by anyone, for the life of the collection.

RESOLVED by ADDING `fixed` beside `book` rather than renaming anything
(`4e1e559`). `book` is untouched and stays grandfathered; a new Codex
recipe declares `fixed`. The two share `_fixed_windows`, so
`fixed{tokens: 1000, overlap: 120}` cuts byte-identically to `book` at
the config defaults AND produces the same chunk ids, since
FIXED_CHUNK_VERSION is 1 exactly as config.CHUNK_VERSION is. A Codex
collection therefore inherits a legacy pack's chunk ids and its vectors
carry over. Nothing in the legacy path moved.

## 3. The chunker's parameters — DONE

06 §6.3, immediately above its registry table: **"a chunker reads no node
configuration."**

`app/chunking.py` reads `config.CHUNK_TOKENS` (default 1000) and
`config.CHUNK_OVERLAP` (default 120) **from the environment**.

So two nodes agree on a `space_id` and disagree on chunks. That is
invariant 1's failure mode written out: the space id promises the vectors
are comparable and the chunks are not the same chunks. It is worse than a
wrong id, because nothing detects it — both peers compute the id the
document prescribes and both are honest.

There are TWO `Layer` types and only one is right:

* `app/layers.py`'s — `{chunker: str, version: int, rule: str}`, **no
  params field at all**, which is why the parameters have nowhere to live
  and end up in the environment.
* `app/hold/codex.py`'s — `Chunker(id, version, params)`, which is 09
  §9.3's shape and carries them.

RESOLVED (`4e1e559`). `app/layers.py`'s Layer gained `params`, the
dispatch passes them, and `fixed` refuses a layer that does not say.
`space_payload` was checked and does NOT carry params, so the legacy
space id every existing vector is keyed to did not move; `as_manifest`
emits them only when non-empty, so existing manifests are byte-identical.
Both are pinned by tests.

## 4. `space_id` is computed by two different functions — DONE, and needed no change

They are not variants. They hash different things:

* `app/spaces.py:compute_space_id` — BLAKE2b over
  `{embed_model_hash, dtype, normalization, pooling, chunk_version,
  cleaning_version, truncation_policy, …}`
* `app/hold/codex.py:space_id` — SHA-256 over
  `{cleaning_version, encoder:{image,weights}, layers,
  space:{ctx,dim,dtype,epoch,normalise,pooling,query}}`

09 §9.3 fixes the second — and the right answer turned out to be to STOP
TRYING TO MAKE THEM AGREE. They name different things: the legacy id
governs legacy packs, `codex.space_id` governs Codex collections, and a
collection is one or the other until `dev adopt` moves it. The alarming
version of this finding was about the LEGACY EXPORTER, which step 4
deletes. `app/hold/bundle.py` already refuses a bare-hex `space_id`, so
a conformant bundle cannot carry the legacy value — now asserted by a
test (`4e1e559`) so it cannot quietly stop being true. No code changed.

## 5. The calibration reference — DONE 2026-09-09 (the set), OPEN (the guards)

`create` embeds the 512-text calibration set and writes the reference
vectors' hash into the Codex. That hash is what every peer's gate is
decided against, forever.

**Measured 2026-09-08, and the set could not do its job.** Longest text
118 words ≈ 174 tokens, median 26 words, against a space `ctx` of 512 —
nothing in it came within a third of the context window.
`approved-encoders.md` on the b10630 incident: *"Short tests do not catch
this, which is precisely why the approval trial (16 texts) and the backend
gate both pass while the corpus takes damage."* The gate as specified would
have passed the exact build that damaged this node's corpus.

**Both halves are now done.** It was a SPEC defect as well as an artefact
one — 09 §9.12 said "512 SHORT texts", in four documents. §9.12 now
requires a set to span the encoder's whole input range up to the largest
`ctx` it serves, and says why one set serves every space (`7bda70d`, on
`main`). The set itself was rebuilt (`d99ca7f`): 5 → 8192 tokens, 58
texts filling an 8192 window, id `sha256:7d06a2f6…`, the 32
near-duplicate pairs carried over verbatim and still last,
`tools/build_calibration_set.py` reproducing the bytes and
`calibration/text.sources.json` recording where each drawn text came
from. A gate run costs about a minute instead of seven seconds, and zero
bandwidth: the member is `512 × dim × f32` whatever the texts weigh.

**Still open, and they are the guards rather than the set:** the
reference is captured from whatever the encoder returned at `create` time
**with no independent check**, and §9.12 already defines the `.sig.json`
provenance to record it. Produce it against a digest-pinned build and
cross-check on a second endpoint before `create` writes it.

## 6. T0's envelope gate — MEASURED 2026-09-09, and it was refusing honest work

03 §3.5's T0 compares a bundle's vectors against the calibration member's
`envelope` tensor. Its literals had never been checked against real data,
and the envelope's hash is a Codex field — so an envelope that refuses
honest bundles freezes at `create` too.

**Run**: 512 calibration texts and 300 chunks of real Gutenberg prose, cut
by the collection's own `chunk_book` and embedded by the encoder that
collection pins — `embed-gpu-qwen3-8b`, Qwen3-Embedding-8B-Q4_K_M, dim
4096, ctx 8192, ~2,600 tok/s. About nine minutes end to end.

| statistic | measured | the old literal | |
|---|---|---|---|
| mean-vector cosine | **0.7522** | ≥ 0.9 | **FAIL** |
| per-dim SD ratio | p0 0.375, p50 0.797, p100 1.379 | all within [0.5, 2.0] | **FAIL**, 16 of 4096 |

Both refused a bundle whose vectors were entirely correct. The reason is
plain once seen: the calibration set is deliberately diverse — every
script, five tokens to a full window — and a bundle is one collection's
prose in one language at one chunk size. Their mean directions were never
going to coincide, and a real bundle is LESS varied than the set, so the
ratios sit under 1 rather than straddling it.

**First fix (SUPERSEDED by runs B and C below), cosine ≥ 0.5 and an
every-dimension band of [0.25, 4.0]**, chosen against
what T0 is FOR rather than by loosening until green. Same vectors, no
re-embedding — the negative controls are computed from the two cached
matrices:

| bundle | cosine | dims outside [0.25, 4] | |
|---|---|---|---|
| real Gutenberg chunks | 0.752 | 0 | **PASS**, +0.25 margin |
| random unit vectors | −0.023 | 3 | FAIL |
| dimension-permuted (a foreign model) | −0.018 | 26 | FAIL |
| un-normalised (rows scaled) | 0.752 | 44 | FAIL — the cosine misses it entirely |
| half real, half random | 0.766 | 2 | FAIL — likewise |
| degenerate (one vector repeated) | 0.476 | 4096 | FAIL |

The two statistics are not redundant: the cosine catches random, foreign
and degenerate vectors; the deviation band catches the un-normalised and
the half-substituted, which the cosine does not notice at all.

**Two things the run also settled.** No dimension has zero SD (min
2.3e-06), so the ratio test is not a division by zero. And the margin on
the last two rows is thin — a half-random bundle is caught by two
dimensions out of 4096 — which is consistent with T0 being sanity, as its
own table says, with bulk substitution left to T3 and T4.

**The open risk is the SHAPE, not the constants.** `app/verify/t0.py`
already argues that a per-dimension gate over thousands of dimensions is a
multiple-comparison problem — *"at dim=1024 ... it rejects 94% of HONEST
bundles"* — and implements one aggregate decision instead (a sum of
squared z-scores against a bootstrapped null published per bundle size).
That is the better test, and §9.12's envelope of two rows cannot feed it.
So the band stays with measured constants, and 03 §3.5 now records that
the first honest bundle it refuses is evidence the shape is wrong rather
than the band narrow.


**Runs B and C, later the same day, overturned the first fix.** The band
had been widened and the rule left as "every dimension". Wrong lever.

| run | encoder | dim | corpus | cosine | SD min | outside [0.5, 2] |
|---|---|---|---|---|---|---|
| A | Qwen3-Embedding-8B | 4096 | Gutenberg English | 0.752 | 0.375 | 16 |
| B | Qwen3-Embedding-8B | 4096 | Gutenberg **Chinese** | 0.658 | 0.355 | 33 |
| C | Qwen3-Embedding-**4B** | **2560** | Gutenberg English | 0.747 | 0.532 | **0** |

Changing the ENCODER moved the cosine 0.005. Changing the CORPUS moved it
0.095 — nineteen times as much. And the deviation test passed at 2560
dimensions and failed at 4096 on the same honest vectors, which nothing
about the encoder or the corpus explains: `min` over 4096 samples is
smaller than `min` over 2560 for no reason but the count.

So the rule was a multiple-comparison problem and widening only postpones
it. Stated as a PERCENTILE the dimension dependence disappears. A grid
over quantiles and bands against three honest bundles and seven broken
ones picked **p2/p98 within [0.4, 2.0]** — honest margin +0.157 at the low
end against +0.014 for p1/p99 at the original band, with the same five of
seven controls caught. The constants barely moved; the shape was the
defect.

Now measured rather than assumed: a 10 %- or 25 %-random bundle passes
both statistics (cosine 0.752, indistinguishable from clean). The extremum
rule caught those with the very statistic that refuses honest work, so no
version of this test sees partial contamination AND admits real bundles.
T3 fails a 10 % bundle 1.70 % of the time (computed, not estimated — the
first draft of this note said "four times in five"); T4 re-embeds what a
search cites.

**And it answers the operator's design question** — should the rule be
tailored per embedder, estimated at `create`? Mostly not, once the
statistic is expressed correctly. The apparent per-embedder variation was
the badly-shaped statistic, and the percentile removes it: 2560 and 4096
now agree to within 0.04 at p2. What genuinely varies is the CORPUS, and
that is the one term a create-time estimate cannot capture, because 06
§6.6 fixes that a Codex is complete before a single record exists. The
sample is still two corpora and two encoders, one plugin, one chunker;
PubMed under the `abstract` chunker is the untested case most unlike
Gutenberg, and is the next measurement worth making.

Specs `1c4c1f3` on main supersedes `7848f59` and `31b99b3`.

**Run D, dim 768 — the third dimension, and a correction.** MedCPT
article encoder (BERT-class, ctx 512, TEI) on the SAME Gutenberg slice as
run A.

| run | encoder | dim | class | corpus | cosine | ratio min | outside [0.5, 2] |
|---|---|---|---|---|---|---|---|
| A | Qwen3-Embedding-8B | 4096 | decoder | English | 0.752 | 0.375 | 16 |
| B | Qwen3-Embedding-8B | 4096 | decoder | Chinese | 0.658 | 0.355 | 33 |
| C | Qwen3-Embedding-4B | 2560 | decoder | English | 0.747 | 0.532 | 0 |
| D | MedCPT article | 768 | BERT | English | 0.933 | 0.453 | 0 |

The last column is now a clean monotone in the dimension and in nothing
else — 0 at 768, 0 at 2560, 16 at 4096, 33 at 4096 on a harder corpus —
which is the multiple-comparison signature and not a property of any
encoder or corpus. The percentile form holds on all four at p2 = 0.601,
0.557, 0.645, 0.682.

It also corrects what runs A-C implied. "Changing the encoder moved the
cosine 0.005" was a SIZE change inside one family. Across a model CLASS
it moves 0.18. The three terms, separated:

| what changes | Δ cosine |
|---|---|
| model size (8B → 4B) | 0.005 |
| corpus (English → Chinese) | 0.095 |
| model class (decoder → BERT) | 0.181 |

So the operator's per-embedder instinct was better founded than run C
alone suggested — the encoder does matter, just not its size. What has
not changed is the conclusion: all four clear the 0.5 floor, the
percentile rule needs no per-space tuning, and the deviation half is the
steadier of the two (p2 spans 0.557-0.682 across everything measured).

Specs `4db9657` on main.

**Run E — PubMed, and the band turns out to be two-sided.** The shape the
note itself called untested: abstracts under the `abstract` chunker, one
chunk per article, 302–3,034 characters against Gutenberg's 3,892–4,000.
Same encoder and the cached envelope from run D, so only the bundle was
embedded.

| run | encoder | dim | corpus | cosine | p2 | p98 |
|---|---|---|---|---|---|---|
| A | Qwen3-8B | 4096 | gutenberg/en | 0.752 | 0.601 | ~1.05 |
| B | Qwen3-8B | 4096 | gutenberg/zh | 0.658 | 0.557 | ~1.12 |
| C | Qwen3-4B | 2560 | gutenberg/en | 0.747 | 0.645 | ~1.04 |
| D | MedCPT | 768 | gutenberg/en | 0.933 | 0.682 | 1.274 |
| E | MedCPT | 768 | **pubmed** | 0.882 | **0.953** | **1.666** |

Every Gutenberg bundle sat UNDER 1 — less varied than the calibration
set — and this note and 03 §3.5 both drew the obvious conclusion twice:
that the floor was the side that mattered and that every perturbation
pushed toward it. Both were properties of one corpus shape. PubMed
straddles 1.

| | worst honest | bound | margin |
|---|---|---|---|
| low side | p2 0.557 (qwen3/4096, chinese) | 0.4 | 0.157 |
| high side | p98 1.666 (medcpt/768, pubmed) | 2.0 | 0.334 |

The controls were re-run in the PubMed space, since a separation measured
where the floor binds says nothing about where the ceiling does. The tier
is sharper there — six of seven caught rather than five, a quarter random
now among them — because contamination pushes ratios UP through a ceiling
rather than down toward a floor. A tenth random still passes, at p98
1.930 against 2.0.

Three corpora, two chunkers, three encoders, two model classes, dims 768
to 4096. What is left untested is another MODALITY.

Specs `a1b5ca9` on main.

**Run F — bge-m3 at dim 1024, and the coverage is now complete.** The gap
this note named: `wikipedia` and `wikivec`, the largest collections here
and the only ones on the `cdc` and `verbatim` chunkers.

| | encoder | dim | corpus | what it is | cosine | p2 | p98 |
|---|---|---|---|---|---|---|---|
| C | Qwen3-4B | 2560 | gutenberg | **`gutenberg`** | 0.747 | 0.645 | ~1.04 |
| E | MedCPT | 768 | pubmed | **`pubmed`** | 0.882 | 0.953 | 1.666 |
| F | bge-m3 | 1024 | wikipedia | **`wikivec`** / `wikipedia` | 0.813 | 0.811 | 1.332 |

All three live text spaces pass, on both statistics and both forms of the
deviation rule.

**It cost almost nothing and was the most faithful of the six**, because
the bundle needed no re-embedding at all. wikivec's Lance table already
holds the collection's own vectors AND, in its `text` column, the exact
inputs that produced them — `title\nparagraph`, which is `embed_input`.
So 300 rows came straight out. The trap recorded above — that a run over
bare paragraphs would measure a different function and produce an
envelope describing a space nobody holds — could not fire, because
nothing about the input was reconstructed. Only the envelope needed the
encoder, 512 texts in 866 s on the CPU endpoint.

Run F's ratios straddle 1 (p50 1.037) as PubMed's do. That is now two of
three live spaces, which makes straddling the normal case and the
Gutenberg runs sitting wholly under 1 the exception — the opposite of
what the first four runs implied.

Specs `f5ef0f4` on main.

**Which collection each run actually IS (corrected 2026-09-10).** The
first four runs were attributed to the wrong collection for a day.
`DEPLOYMENT.md` says `embed-gpu-qwen3-8b` "serves gutenberg" and does not
mention the 4B app at all; the live
`config/collections/gutenberg.toml` points at port 30437,
`qwen3-embedding-4b`. The TOML is what runs. The node's real spaces:

| collection | encoder | port | dim | ctx | measured by |
|---|---|---|---|---|---|
| `gutenberg` | Qwen3-Embedding-4B | 30437 | 2560 | 8192 | **run C** |
| `pubmed` | MedCPT article | 30440 | 768 | 512 | **run E** |
| `wikipedia` | bge-m3 | 30442 | 1024 | 2048 | — |
| `wikivec` | bge-m3 | 30442 | 1024 | 2048 | — |
| `genome-*` (3) | DNABERT-2 | 30443 | 768 | 1024 | unreachable |

**What a bge-m3 run must do, so it is not a repeat of the 4B mistake in a
subtler form.** `wikipedia` and `wikivec` are the outstanding gap, and
their space is NOT a choice this node made: wikivec's vectors came
pre-embedded from Upstash, so the space IS the archive's function and
anything embedded into it must reproduce that function exactly. Two
parts, and the second is the one a naive run would miss:

- the ENDPOINT — bge-m3 under TEI, `ctx` 2048, dim 1024, `dtype` f16,
  normalised (`config/collections/wikivec.toml`, port 30442);
- the INPUT FORMAT — Upstash prefixed each paragraph with its article
  title, so a chunk's embedding input is `f"{title}\n{text}"`, not the
  paragraph. That is `app/plugins/embedded/source.py::embed_input`, and
  it was established empirically rather than read anywhere: the genesis
  sample re-encode compares this node's re-embedding against the
  archive's own vectors at cos ≥ 0.95, so a wrong separator refuses every
  shard and imports nothing.

A T0 run that embedded bare paragraphs would therefore be measuring a
DIFFERENT function from the one that produced the collection's vectors,
and its envelope would describe a space nobody holds. The calibration
set, by contrast, is embedded plain — it is corpus-independent and has no
titles — and that asymmetry is correct: the envelope is the encoder's
response to the fixed probe set, and the bundle is the collection's
chunks as it actually produces them.

STATUS.md records the same trap from the other end: "the create dialog's
default `[embed]` is the node's encoder — right for a corpus this node
embeds, wrong for a pre-embedded one, whose recipe must be the ARCHIVE's
function (BGE-M3 for wikivec) or the genesis proof rejects every shard".

Every sentence calling the 8B "the Gutenberg collection's encoder" was
wrong, and run C is the step-6 target's space. `gutenberg-pack` ran the
8B but is an INTERMEDIATE PRODUCT (operator, 2026-09-10) and is not a
collection to reason about — so no live space runs at 4096 at all, and
runs A and B are dimension probes rather than a collection's space.

**And that weakens one half of the finding, which is worth stating.** The
COSINE bound of 0.9 refuses BOTH live text spaces measured (0.747, 0.882),
while the DEVIATION bound refuses only the 4096-dimensional one. Someone
looking only at `gutenberg` — at 2560 — would have seen the old
deviation rule pass and called it sound. It is not sound at 2560; it is
untriggered there, and the peer who would have discovered that is one
sharing the space from a 4096-dimensional encoder.

**`DEPLOYMENT.md` is stale and only the operator can fix it** — it is
gitignored, lives on the node, and the deploy procedure copies it back by
`scp`, which this session cannot run. Two errors: the encoder table says
the 8B serves `gutenberg` (it serves `gutenberg-pack`), and neither the
4B app nor `gutenberg-pack` appears in the collection table at all.

**Still open under item 5, unchanged**: the reference is captured from
whatever the encoder returned at `create` time with no independent check.

## 7 and 8. Layers, and credits — DECIDED 2026-09-09, operator

**7. Wikipedia and wikivec are ONE collection with TWO layers**, as 06
§6.3's own worked example spells out. Layer 1 is `verbatim` over the
documents the Upstash archive covers; layer 2 is the default chunker over
everything past that coverage — the post-snapshot articles this node
embeds itself, which is the entire point of two layers. The rule is read
from the Codex's `credits`, never from the platform's build (`9b98012`,
`app/layers.py`, `tests/test_layers_archive_rule.py`).

**8. Credits: the recipe names the archive whose vectors the collection
carries, and the archive's entry names the corpus its vectors were
computed over.** The operator's rule, in their words: "the pre-embeddings
credits the provider of the pre-embeddings and wikipedia (like 'wikipedia
embedded by...'), the part embedded by the community credits wikipedia".

| recipe | `archives =` | why |
|---|---|---|
| wikipedia | `["upstash-wikipedia-2024-06"]` | one entry covers layer 1; its `attribution` names Wikipedia's authors AND Upstash |
| pubmed | `["medcpt-pubmed"]` | `DEFAULT_ARCHIVE_BY_PLUGIN` already says so |
| gutenberg, genome, gazzetta | `[]` | no pre-embedded archive; every vector is this node's or a peer's |

The registry's two attribution strings were rewritten to carry both
parties, because both are owed and the licence clause turns on the corpus
author rather than on whoever we obtained the file from:

    Wikipedia contributors (CC BY-SA 4.0), embedded by Upstash
    as wikipedia-2024-06-bge-m3

    MedCPT article embeddings over PubMed, NCBI/NLM (public domain)

`create` copies these into `credits` whole and hashes them into
`collection_id`, so no later build can alter or drop either name.

**The second half of the operator's rule has nowhere to go, and this is
now the open item.** "The part embedded by the community credits
wikipedia" is about the CORPUS, not about an archive — layer 2 draws on
no archive at all, and its peers belong in the coverage map (02 §2.5)
rather than in `credits`. But the Codex has no field that names whom the
corpus licence requires attributing: `license` is `{class,
terms_sha256}` (09 §9.3), `terms_sha256` hashes the licence TEXT, which
for CC BY-SA states that credit must be given without naming who is owed
it, and `origin` is a locator.

`credits` cannot hold it. `credits` is SPACE-scoped by construction —
02 §2.7, "a sister's `credits` are her mother's when `space_id` matches"
and are her own recipe's when it does not, and 09 §9.3, "a corrective
sister's are `[]`". A corrective sister of Wikipedia therefore carries
every word of Wikipedia's text with an empty `credits`. A corpus credit
is CORPUS-scoped: it must survive every sister, every daughter and every
correction, exactly as `license`, `origin` and `cleaning_version` do.

So the proposal is a third key in the block that already has the right
lifetime: `license: {class, terms_sha256, attribution}` — the credit
line the corpus licence requires, byte-exact, declared by the plugin
(06 §6.1) and copied by `create` like every other licence field, `null`
on green and only on green, by the same rule `terms_sha256` already
follows. It costs one field, it is hashed, and a sister keeps it because
a sister already keeps `license` byte for byte.

It is a CODEX WIRE-FORMAT CHANGE — CLAUDE.md says ask first, and it
changes `collection_id` for every collection — so it is not made here.
The moment to make it is before the first `create` of step 6, and it is
free right now only because nothing has been published beyond this
operator's own machines.

---

## 9. The corpus and community credits — DONE 2026-09-09, operator

Item 8's decision opened this one and the same day closed it. The
operator's rule, verbatim: *"I want to credit the Project Louise community
and the corpus provider, for the part that has been embedded by the
community."*

Neither had anywhere to go. `credits` held pre-embedded archives only;
`license` is `{class, terms_sha256}` and `terms_sha256` hashes the licence
TEXT, which for CC BY-SA says credit must be given without naming who is
owed it; `origin` is a locator. The sharpest form of the gap: a CORRECTIVE
SISTER of a Wikipedia collection would have carried every word of
Wikipedia's text with `credits: []`.

`credits` now carries a `kind` — `corpus`, `archive`, `community` — in one
closed six-key shape, and the kind decides both which keys mean anything
and by which rule the entry descends. Three lifetimes:

| kind | where it comes from | what a relative gets |
|---|---|---|
| `corpus` | the plugin (06 §6.1's `corpus_provider` / `corpus_attribution`) | her mother's, on every relation without exception |
| `archive` | the recipe's `archives`, through the registry | today's rule: the mother's when `space_id` matches, the recipe's when not, none on a corrective sister |
| `community` | `embedding` — present when `open` or `endorsed` | follows `embedding`, which a sister keeps |

Exactly one corpus entry on every collection, GREEN ONES INCLUDED: a
provider that asks for nothing is still the provider, and a rule with an
exception is a rule someone lands on the wrong side of. `credits` is
therefore never empty.

**The trap this could have set, and the reason it did not.** A corpus
entry carries `coverage: "all"`, and 06 §6.3's `archive:<id>` layer rule
reads coverage. Had it read the corpus entry, EVERY document would fall in
the archive layer, be cut by the wrong chunker, and produce different
vectors under a `space_id` two peers agree on — invariant 1's silent
failure mode, introduced by a compliance field. The rule now reads
`kind: "archive"` and nothing else, and a test asserts a corpus-only
collection puts nothing in the archive layer.

Specs `f33b62e` + `2e58d44` on main; code `c5778d2` on `louise`; 19 tests
in `tests/hold/test_credit_kinds.py`.

---

## The rule this file exists to enforce

Nothing here is exotic. Every item is a place where a document says one
thing and the code says another, and where the disagreement becomes
permanent at one instant. The cheap moment is before that instant and there
is no second one.

Related: [`step4-vectors-study.md`](step4-vectors-study.md) found items 2,
3 and 4; the calibration measurement is in this session's commits; the
migration plan's step 6 is being rewritten around decision 7 (the PoC
re-embeds a clean slice rather than importing the legacy vectors).
