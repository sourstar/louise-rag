# Where does a reranker belong? — plugin, pack, or node

**Status: PROPOSAL for evaluation. Nothing built, nothing agreed.**
Written 2026-08-26; restructured the same day around a sharper framing from
the operator. Prompted by `gazzetta-plugin.md` §7.4 and answered in general
terms, because the answer is a platform rule, not an Italian-law one.

## 0. The framing, and why it is the right one

The question is not "may a pack carry a reranker" but a three-part one:

1. For a plugin where a reranker is **applicable**, is its use **compulsory**
   or **optional**?
2. If compulsory — is there exactly **one** compulsory reranker, or a **set**
   from which a peer must pick?
3. If the reranker **does not affect the pack's common content**, a peer may
   use whatever reranker it likes.

Clause 3 is the load-bearing one, and it is a better discriminator than the
"correctness pin vs. advisory metadata" split this document argued in its
first draft. That split classified fields by what they look like. This one
classifies them by a **checkable property of the output**:

> Does anything derived from the reranker end up in bytes that travel
> between peers, or in what a peer contributes to a shared space?

Everything below is that test, applied.

---

## 1. What "the pack's common content" is, precisely

From `pack-format.md` §7 and §10, the bytes that travel are: record shards,
the text index, vectors per space, bucket files (the vector index), and the
manifest that names them. Plus, from §11 and `app/contribute.py`, what a
peer *produces* for others: vectors for leased uids, exported as a signed
bundle.

So "common content" = **shard bytes + contributed vectors**. Two properties
of that set matter here:

- **It is content-determined.** A vector is `f(encoder, chunk text)`; a chunk
  is `f(chunker, cleaned text)`. Both functions are pinned — in `space_id`
  and in `layers` — precisely because two peers must produce identical
  output from identical input.
- **Its *extent* is explicitly allowed to vary.** The manifest carries
  `"coverage": 0.62` per space. Which documents a given peer has embedded is
  a free variable of the format; *what* the vector for a given chunk is, is
  not.

That distinction — **what vs. which** — turns out to decide the whole
question.

## 2. The test applied: where a reranker could sit

| where the reranker runs | does its output reach common content? | binding? |
|---|---|---|
| **query time**, after RRF (today: `retrieval.py:234`, `:333`) | no — the ordering is returned to one user and discarded | **optional; free peer choice** |
| **query time → lazy-embed enqueue** (today, see §3) | *which* documents get embedded, never *what* the vectors are | **optional** — it moves coverage, which the format already lets vary |
| **contribute time**, choosing what to embed for a lease | no — the coordinator's `/work` list orders the lease, not the local reranker (`contribute.py`'s docstring is explicit that re-sorting would fight it) | **optional** |
| **build time**, selecting which chunks enter the pack | **yes** — a different reranker yields different shard bytes | **compulsory, and must be pinned** |
| **build time**, supervising index construction (bucket centres, §5.2) | **yes** — the index files differ | **compulsory** |
| **build time**, mining hard negatives for a fine-tuned encoder | no, indirectly — it changes the *encoder*, which `space_id` already pins | already covered |
| **a pack shipping precomputed rerank scores** | **yes** — the scores are shard bytes | **compulsory** |

Read down the table and the pattern is clean: **a reranker binds exactly
when it runs at build time.** At query time it cannot bind, because nothing
durable is downstream of it.

## 3. The one leak in today's code, and why it does not bind

I want to record this because I missed it in the first draft and it is the
sort of thing that looks like a counter-example.

`_attach_metadata_and_enqueue` is called with `final` — the **reranked**
list — and in `lazy` embed_mode it does `_enqueue_lazy_embed(con, ids[:10],
collection)` (`retrieval.py:209`). So the reranker genuinely does decide
which ten documents this node embeds next. That is a real feedback path from
a query-time component into durable local state, and from there into what
this node could later export.

It still does not bind, and the reason is §1's distinction: it changes
**which** documents have vectors, never **what** a vector is. Two peers with
different rerankers converge on different *coverage* and identical *content*
— and coverage is already a declared, per-space, freely-varying quantity in
the manifest. The leak lands exactly on the axis the format treats as free.

Worth stating as a rule, because it is the tripwire for everything in §2's
lower half:

> **A reranker may influence *which* records are processed. The moment it
> influences *what* a record's bytes are, it becomes a pinned function.**

## 4. "One compulsory reranker, or a list?" — neither; a predicate

This is the part of the framing worth pushing on hardest, because the
codebase has already solved it once for encoders and the solution is neither
"one" nor "a list".

A pack does **not** name the encoder a peer must run. It ships a
`calibration` block — sixteen chunk IDs plus a `vectors_root` — and a
`tolerance`. Any encoder reproducing those vectors within tolerance is
admissible. That defines the acceptable set **by a predicate**, and it has
three properties an enumeration cannot have:

- **It admits models that did not exist when the pack was published.** A
  newer TEI, a Vulkan build, a re-quantised GGUF — all fine if they pass.
  A name list would have frozen the set at publication.
- **It excludes a model that shares its name but drifted.** This is not
  hypothetical here: `approved-encoders.md` opens with a moving tag that
  silently corrupted 714 books, which is why every build in that file is
  pinned by digest. A list of names is exactly the artefact that incident
  argues against.
- **It states the bar, so disagreement is resolvable.** "Use one of these
  five" gives a peer no way to answer "is my sixth one equivalent?"

So the recommendation on clause 2 of the framing: **if a reranker is ever
compulsory, define the admissible set by an equivalence test, not by one
name and not by a list of names.** A list may accompany it as a convenience
— "these are known to pass" — the way `approved-encoders.md` is a
procurement list beside the behavioural gate, and explicitly not a
substitute for it.

## 5. What a rank-equivalence test would look like

The encoder's test is cheap because vectors compare pointwise. Rankings do
not: two rerankers agree or disagree over an *ordering*, which needs
(query, candidate set) pairs to even evaluate. That is the real obstacle to
a compulsory reranker, and it is constructible:

```json
"rank_calibration": {
  "cases": [{"query": "…", "candidates": ["pubmed:10329736:0:1", …40],
             "reference_order": [7, 2, 19, …]}, …16],
  "metric": "kendall_tau_b",
  "tolerance": 0.90,
  "conditions": {"candidates": 40, "truncation_chars": 2000}
}
```

Properties: the candidates are chunk IDs **already in the pack**, so the
test costs no extra bytes and cannot be run against the wrong text. The
queries are the *producer's*, synthesised from their own corpus — never a
user's, which keeps this out of privacy territory. And `conditions` is part
of the claim, because a ranking is not a property of the model alone (§8).

Two honest problems with it:

1. **A tolerance on τ has no natural value.** Cosine ≥ 0.999 means "the same
   function"; τ ≥ 0.90 means "usually orders things similarly", which is a
   quality judgement wearing an equivalence test's clothes.
2. **`pack-format.md` §16 prefers reproduction to assertion**, and this is
   reproducible — but only of the producer's own frozen cases, which is a
   weaker claim than reproducing a vector.

Both are arguments for keeping the compulsory arm **specified but unused**
(§9), not for leaving it unspecified.

## 6. The peer that cannot comply

If a reranker is ever compulsory, the format already has the right answer
shape, and it should be reused verbatim. From `pack-format.md` §7: a peer
failing the encoder's calibration proof **may still search with what it
holds; it may not contribute** to that space.

The same ladder for a compulsory reranker:

- **Search** — always permitted, with or without the reranker. A peer with
  no reranker at all gets RRF order, which is what happens today with
  `RERANK_URL` empty. Nothing about a pack may make its data unreadable.
- **Contribute** — permitted only when the peer passes the predicate, and
  only for artefacts the reranker actually determined.

That preserves the property that matters most in a P2P system: **you cannot
force anyone's stack**, and a pack that tried would simply not be adopted.

## 7. Security holds regardless — and compulsoriness raises the stakes

Unchanged from the first draft, and more important under this framing, not
less. `_rerank` POSTs `{"query", "texts"}` to `RERANK_URL`
(`retrieval.py:82`) — the user's query together with the passages retrieved
for it. A manifest field able to set that URL would let a remote publisher
choose where this node sends both.

> **A pack may carry a model *identity* — name plus digest — and a
> predicate. It may never carry a URL, host, or path the node will
> contact.**

A *compulsory* reranker naming a URL would be strictly worse than an
optional one: a mandatory outbound call to a publisher-chosen host. Same
instinct as invariant 8 (secrets by path, never by value).

## 8. Why the node measures anyway, even under a predicate

A reranker's behaviour is not a property of the model alone. At least three
conditions belong to the consumer:

1. **Candidate budget** — `max(k * 4, 40)` (`retrieval.py:234`, `:333`).
2. **Truncation** — `h["text"][:2000]` (`retrieval.py:83`), a hard constant,
   ~400 tokens at this repo's measured chars/token. A 32k-context model
   judging a 2000-character prefix is not the model anyone benchmarked.
3. **Query mix** — the producer's cases are not this node's users.

Hence `conditions` inside the calibration block (§5): a predicate that does
not state its conditions is not reproducible.

## 9. The proposal, in three tiers

Mirroring the config tiers rather than inventing a new axis:

| tier | declares | value | binding? |
|---|---|---|---|
| **plugin** (code) | **applicability** — `rerank_class = "text" \| "dna" \| null` | static, per source | a *wrongness* check only: warn when the node's reranker is of the wrong class. A text cross-encoder scoring DNA is a category error, the same argument that gave `genome` its own encoder default — not a quality knob |
| **pack** (manifest) | **bindingness** — `optional` (default) or `required` + predicate | per published artefact | `required` is legitimate **only** when the pack ships something the reranker determined (§2's lower half). Identity + digest + predicate; never a URL |
| **node** (env / TOML) | **which model actually runs** | `RERANK_URL`, operator-set | always the operator's, subject to the predicate when one exists |

And the recommendation for **today**: every plugin is `optional`, because
nothing in the current pipeline consumes rerank output durably. `RERANK_URL`
stays node-global (it is Tier-1 env, `config.py:32` — not in `EmbedRecipe`,
not on `Collection`), a pack may carry an advisory hint with a measured
delta, and peers choose freely — which is clause 3 of the framing, and it
holds for every plugin that exists.

The value of specifying the compulsory arm now is the tripwire it creates:

> **Any future feature that makes rerank output durable — build-time chunk
> selection, reranker-supervised index construction, precomputed scores in a
> shard — must declare the reranker in the manifest with a predicate, or it
> is publishing bytes no peer can reproduce.**

That rule costs nothing today and is the one thing that would be expensive
to retrofit, because it would be discovered as a reproducibility failure
across an already-distributed pack.

## 10. What this deliberately does not propose

- No `rerank_url` in a pack, ever (§7).
- No automatic application of a hint or predicate on import — a pack is data
  received from a producer, and invariant 10's tiers give received data no
  route to desired state.
- ~~No per-collection reranker (`RERANK_URL` is node-global) until a
  measured case shows one node-wide choice failing some collection.~~
  Superseded 2026-09-15 by the operator's decision and by 05 §5.6 itself,
  which reranks each list on its own: a collection file's `[reranker]` now
  names its reranker, each collection's list is reranked by it, and the
  reranked lists are fused BY RANK — the "N incomparable score scales"
  this bullet feared never meet on one scale. See §12.
- No change to pinned facts bypassing RRF and rerank both
  (`retrieval.py:227`, `:367`) — invariant 4 at the query layer.

## 11. Open questions

1. **Is the compulsory arm worth specifying before a use for it exists?**
   The tripwire in §9 is the argument for; YAGNI is the argument against.
2. **Is a τ tolerance an equivalence test or a quality threshold in
   disguise?** (§5.1.) If the latter, "compulsory" may be unachievable for
   rerankers in principle, and the honest format would say so.
3. **Does a hint/predicate justify a wire-format field at all**, or does the
   producer's measurement belong in the pack's README, leaving the manifest
   a pure description of data?
4. **Is `rerank_class` worth having** while `genome` is the only non-text
   plugin it would guard?
5. **Should the lazy-embed enqueue path (§3) read the *fused* order rather
   than the *reranked* one?** It would remove the leak entirely and make the
   "query time cannot bind" claim unconditional. Against: the reranked order
   is the better answer to "what is this node's user actually interested
   in", which is the whole point of lazy embedding.

## 12. MedCPT's cross-encoder — measured, parked, then served (2026-09-15)

**Decision (operator).** A reranker per collection, on the GPU where the
model runs there correctly. PubMed's natural reranker, NCBI's
MedCPT-Cross-Encoder (trained with the MedCPT article and query encoders
PubMed runs), fit neither place at first, so PubMed used the shared GPU
reranker gutenberg and wikipedia use (bge-reranker-v2-m3) until the
OpenVINO route below landed the same afternoon.

**Served: OpenVINO on the Intel GPU, the pair tokenizer outside it.**
`deploy/medcpt-rerank/`: `convert.py` turns NCBI's weights into an
OpenVINO IR (fp16 weights); `server.py` builds `input_ids`,
`attention_mask` and `token_type_ids` with the model's own
`tokenizer.json`, runs the IR on the GPU and answers TEI's `/rerank`.
It runs as the third service of the embed-gpu-medcpt app (:30445), and
pubmed's `[reranker]` names it (`api = "tei"`). Held to transformers fp32
on the original weights, with 3 real pubmed queries × 50 candidates
(8.8–13.1 k tokens a list, ~95 % of them segment 1):

| | max \|logit diff\| | Kendall τ | top-10 | 50 candidates |
|---|---|---|---|---|
| encodings (tokenizers vs transformers) | identical | | | |
| IR on CPU, f32 | < 0.01 | 0.974–1.000 | same | 2.6–11.8 s (loaded node) |
| IR on the GPU, f16 | ≤ 0.04 | 0.974–1.000 | same | 72–95 ms warm |

Four lists at once: 251 ms. Through the platform's search: 300–392 ms
rerank. τ 0.974 is near-ties swapping, not disagreement: the top 10 are
identical on every query. One trap on the way: this CPU (Zen 4) runs
OpenVINO in bf16 by default, and the first CPU check showed drift of up to
0.25 logits and τ 0.93. That was the precision, not the conversion.

**On the CPU** (TEI 1.8, NCBI's weights, 435-token candidates): 0.32 s per
candidate with the node paused — 3.0 s for 10, 6.4 s for 20, 15.9 s for 50
— and ~1.7× that under the worker's load (10.8 s for 20, 26 s for 50).
Against the spec's former 400 ms budget that was 16× short at 20
candidates; against the 8 s the operator set the same day, 50 candidates
— the spec's number — are still twice over even on an idle node, and 20
fit only while nothing else runs.

**As GGUF** (llama.cpp b10630), two faults, one fixable here:
1. The converter drops BERT's pooler (`pooler.dense`, "we are only using
   BERT for embeddings") and writes no pooling type. Patched in a
   throwaway converter (keep it as `classifier.dense` → `cls`; write RANK):
   the right document then ranks first.
2. llama.cpp gives EVERY token segment 0 (`src/models/bert.cpp` adds
   `type_embd` row 0; `llama_batch` carries no segment ids), and a BERT
   cross-encoder was trained with the document as segment 1. The scores
   stay 8–15 logits off NCBI's and the rest of the order differs. Not
   fixable in the file alone.

**Routes, as they stood before the OpenVINO sidecar:**
- OpenVINO Model Server on the Intel GPU through its Cohere-style
  `/v3/rerank`. This turned out to be wrong: its rerank calculator
  (`src/rerank/rerank_calculator_ov.cc`) fills `token_type_ids` with 0,
  the same fault as llama.cpp. The model runs fine under OpenVINO; the
  pairing has to be done outside the server, which is what the sidecar
  does.
- A llama.cpp that passes segment ids for rerank pairs (a patched build,
  or upstream if it ever lands).
- Fold segment 1 into a second copy of the vocabulary (token embedding +
  segment-1 row − segment-0 row) and send the document half as those ids:
  exact, no C++ change, but the caller must tokenize — the platform holds
  no tokenizer (invariant 7), so it would need the server's /tokenize.

The deciding measurement for any of them is the same two numbers: NCBI's
scores reproduced (Kendall tau against TEI or transformers on the same
pairs), and 50 candidates inside the budget.

## 13. Choosing reranker WEIGHTS — the August inventory (2026-08-29)

Written then in a local working copy, never committed; landed 2026-10-18
because none of it was recorded anywhere else. The node has served
`bge-reranker-v2-m3` (`louise-rerank-bge`) since; this is what to know
before adding any other.

| model | license | GGUF from a trusted source | finding |
|---|---|---|---|
| **Qwen3-Reranker-0.6B** | Apache-2.0 | **yes** — `ggml-org/Qwen3-Reranker-0.6B-Q8_0-GGUF` | *measured* on the Arc: scores separate (0.482 vs 8e-6), the relevant passage first in English *and* for an Italian query against an English passage, 1.86 s for 40 passages x 2,000 chars |
| Qwen3-Reranker-4B | Apache-2.0 | no | must be converted with `convert_hf_to_gguf.py`; ~5.8 s/query (*modelled* from measured embedder ratios) |
| Qwen3-Reranker-8B | Apache-2.0 | no | as above; ~8.7 s/query (*modelled*) |
| `BAAI/bge-reranker-v2-m3` | Apache-2.0 | yes (the node's f16 GGUF) | 100+ languages, one forward pass — the one chosen |
| `jina-reranker-v2-base-multilingual` | **CC-BY-NC-4.0** | — | **ruled out**: non-commercial weights must not feed a published pack |

- **Qwen publishes no GGUF for any reranker size** (unlike its embedders).
  Of 45 community Qwen3-Reranker GGUF repos, only the 0.6B comes from
  `ggml-org`.
- **Community conversions often drop `cls.output.weight`** and then return
  ~4.5e-23 for every document while answering 200 OK (llama.cpp #16407,
  #17743). Any reranker GGUF must pass the two checks a broken one cannot
  fake: **scores that separate, and the relevant passage first.**
- The April 2026 timestamps on Qwen's reranker repos are README and
  Sentence-Transformers commits, not new weights.

## 14. Trained rerankers for the cross-language bias (2026-10-23)

**Why now.** On gutenberg, bge-reranker-v2-m3 scores an Italian query
against an English passage far below a same-language pair: Moby-Dick −5.298
against "la balena bianca e l'ossessione del capitano", +1.204 against its
English translation (München, the node). 834baf5 judges each candidate
against the question in its own language, and then English pairs outscore
Italian ones (an Italian query 9–10 of 10 non-Italian); 8bee186 orders each
language in its fused slots. Both work around a model whose scores are not
on one scale across languages. The operator asked for trained models.

**Candidates** (Hugging Face, 2026-10-23; licences read from the repos):

| model | licence | size | cross-lingual | GGUF / runner | note |
|---|---|---|---|---|---|
| **Qwen/Qwen3-Reranker-0.6B** | Apache-2.0 | 0.6B | trained multilingual + cross-lingual (arXiv 2506.05176) | **`ggml-org/Qwen3-Reranker-0.6B-Q8_0-GGUF`** | §13 *measured* on the Arc: Italian query vs English passage, the relevant one first; 1.86 s / 40 × 2,000 chars |
| Qwen/Qwen3-Reranker-4B | Apache-2.0 | 4B | as above, stronger | no trusted GGUF: convert, then §13's two checks | ~5.8 s/query modelled |
| nvidia/llama-nemotron-rerank-1b-v2 | OpenMDW-1.1 + Llama 3.2 licence | 1.2B | **"fine-tuned for multilingual, cross-lingual QA retrieval"**, 26 languages incl. it | transformers (bidirectional Llama, custom code): sidecar, not llama.cpp | licence to read for a published pack |
| mixedbread-ai/mxbai-rerank-base-v2 / large-v2 | Apache-2.0 | 0.5B / 1.5B | 100+ languages (arXiv 2506.03487) | transformers (Qwen2 causal); TEI-tagged | |
| Alibaba-NLP/gte-multilingual-reranker-base | Apache-2.0 | 0.3B | 70+ languages | TEI | fast; weaker ceiling |
| Horizon-Labs/multilingual-reranker-base | Apache-2.0 | 0.3B | ~50 languages, MIRACL-distilled | TEI (ModernBERT) | published 2026-10, unproven |
| **nlpai-lab/LAMAR-600m** (arXiv 2607.22042, 2026-07) | **MIT** | 0.6B | **trained for exactly this**: stage 1 "English-anchored relevance distillation" from Qwen3-Reranker-4B gives one relevance scale across languages; stage 2 prefers the query's language only among semantically equivalent documents (ko 5.79 vs en 4.99 for the same answer, both far above negatives at −13) | **`XLMRobertaForSequenceClassification`, `type_vocab_size` 1 — the architecture of bge-reranker-v2-m3**, so the runner already serving `louise-rerank-bge` serves it; no segment-id issue (§12). Convert ourselves with `convert_hf_to_gguf.py` (a community GGUF exists, `mradermacher/LAMAR-600m-GGUF`; §13's two checks either way) | 51 training languages, 8,192 tokens; evaluated on MIRACL, XGLUE, MLDR |
| Querit/Querit-4B (arXiv 2606.19037) | Apache-2.0 | 4B | best MTEB multilingual-v2 reranking average (71.09, 2026-08-29) | Qwen3 base; convert | its card lists zh en es fr de ru ja ko — **not Italian** |
| nvidia/Nemotron-3.5-Rerank-8B (2026-10-20) | OpenMDW-1.1 | 8.4B | 34 languages incl. it; multimodal | vLLM / transformers | an 8B pointwise model: seconds per query |
| zeroentropy/zerank-2-reranker | Apache-2.0 | 4B | card lists English only | | not for this |
| albertobarnabo/bge-reranker-v2-m3-italian | Apache-2.0 | 0.6B | Italian-only fine-tune (+0.010 nDCG@10 on Italian) | same architecture | monolingual: does not address the cross-language scale |
| jinaai/jina-reranker-v3, v3.5, v2-multilingual, m0 | **CC-BY-NC-4.0** | — | — | — | **ruled out** (§13: non-commercial weights) |

**What to measure, and the deciding number.** Campaign run 5's harness
(`tools/measure/rerank_lang_variants.py`, the product's own path) with the
reranker swapped, on the 39 titles, 451 descriptive and the it/en pairs,
plus one number no aggregate shows: the **cross-language gap** — the mean
score of a relevant pair whose query and passage differ in language minus
that of a same-language one, on the pairs. A model whose gap is near zero
needs neither 834baf5 nor the slots: the typed question against every
candidate, one call, one scale. Each candidate is bit-exact first
(invariant 12 applies to a reranker's scores as to an encoder's vectors:
the same pair, the same score). Order: **LAMAR-600m first** (trained for
the cross-language scale, MIT, the same architecture and runner as the
reranker it would replace — a drop-in), then Qwen3-Reranker-0.6B (a trusted
GGUF; its scale is LAMAR's teacher's), then Qwen3-Reranker-4B (converted).
LAMAR's same-language preference among EQUIVALENT documents agrees with
one hit per work (05 §5.3: a work shown in the reader's language) — it must
not demote a work that exists only in another language, and the it/en
pairs (Moby-Dick, Don Quixote) are exactly that test.

### 14a. LAMAR-600m measured (München, 2026-10-24 05:55)

| check | result |
|---|---|
| model card's example (LocalAI vs reference) | 5.8176 / 5.0364 / −13.3933 / −13.8386 vs 5.7904 / 4.9918 / −13.3835 / −13.8347 (max diff 0.045) |
| invariant 12 | bit-identical over 3 fresh loads × 2 orders, 0 pairs differing |
| 42 real pairs, LocalAI vs transformers | max diff 3.29, mean 0.22; same order on 4 of 6 queries. Every large diff is ONE passage (Don Quixote en, "in writers like Feliciano de Silva…"), query-dependent (3.29, 3.07, 1.36, 1.08, 0.11); the other six agree within 0.04. Tokenisation identical (479 tokens, same ids): f16 precision inside the model suspected; an f32 GGUF would test it |
| cross-language gap on the pairs | LAMAR −2.002 (reference) / −1.849 (LocalAI) vs bge −1.678 |

**LAMAR does not narrow the gap on our pairs** — the reason it was first
(§14) — and its f16 GGUF disagrees with its reference on one passage. Not
adopted. Run 6 (LAMAR, base variant) finishes in Saturday's window for the
record; Qwen3-Reranker-0.6B is next on §14's list only if the operator wants
the gap pursued.

Run 5, partial (127 queries with all three reranks; München): titles MRR
base / score / slots 0.799 / 0.825 / 0.791, R@10 0.973 / 0.946 / 0.946;
descriptive MRR 0.422 / 0.443 / 0.413, R@10 0.689 / 0.800 / 0.744. By the
deploy rule (no loss on titles or descriptive) no variant passes yet: `score`
gains descriptive and loses titles R@10. The complete run (486/496) decides.
