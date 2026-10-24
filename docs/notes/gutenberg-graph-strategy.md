# gutenberg-graph — a strategy by category (proposal, 2026-10-24)

*Operator: "Review, cleanup and organize the architectural proposal for
wiki gutenberg: different approaches depending on the book category …
blind tags for biographies, interviews, diaries, memoirs, something
closer to the street. More polished publications like encyclopedias are
less likely to reveal a hidden gem, but can still add details to a known
fact. … a reasonable computational approach (even if computation will
ideally be shared among peers) and model size (ideally fitting 16 GB
VRAM and in any case not exceeding 32 GB)."*

*This note reorganises three earlier texts into one proposal: the
operator's design in [wikidata-plugins.md](wikidata-plugins.md)
("gutenberg-graph"), the byte-level amendments in
[gutenberg-graph-amendments.md](gutenberg-graph-amendments.md) (A-K1..K4,
decided 2026-10-20, steps 1–2 written), and §5 and §9 of
[graphrag-compliance.md](graphrag-compliance.md) (the LazyGraphRAG
split; unlinked mentions and linking by relational context). Nothing
here is decided or built. Numbers are **measured**, **modelled** or
**published** as measuring.md §5 asks; a cell that says "measure" is a
number nobody has yet.*

## 0. In one paragraph

A text is worth graph work in proportion to how close its writer stood
to the events, and costs in proportion to its tokens times the method
run over it. Gutenberg is mostly fiction and polished print; the hidden
gems — a pseudonym unmasked, a story rebuilt from diaries that never
met — are in the first-person and eyewitness genres, which are a small
share of the tokens. So: a cheap, deterministic, bit-exact layer over
EVERYTHING (mentions linked to Wikidata, co-occurrence, communities,
no LLM); the expensive, tolerance-checked layer (an LLM reading for
relations, cross-document linking of the unnamed) only where the gems
are, chosen by a rule the record itself answers; and the model that
reads fits a 16 GB card, with the 32 GB model kept as the stated run of
a sibling for peers who have the card. Every tier is a packet type with
its own method, so a peer takes a tier's part the way it takes an
embedding part today.

## 1. Categories, computable from the record

The rule must be computable from the record alone (06 §6.3's spirit, as
a layer rule is), so a verifier assigns the same tier. Gutenberg's RDF
gives three fields the parser already keeps (app/plugins/gutenberg/rdf.py):
`subjects_by_scheme.LCC` (class letters), `subjects_by_scheme.LCSH`
(headings with their **form subdivisions** — "Diaries", "Correspondence",
"Personal narratives", "Biography", "Autobiography", "Interviews",
"Description and travel", "Encyclopedias and dictionaries",
"Periodicals"; the Library of Congress *Subject Headings Manual* H 1095
fixes the free-floating list, a standard, not ours), `dcmi_type`
(`Text` / `Sound` / …) and `bookshelves`. A heading's form subdivision
beats the class letter: a diary of a painter is CT or ND by class and
"Diaries" by form, and the form is what says how close to the street
it is.

| tier | what it is | the rule, first match wins | corpus share |
|---|---|---|---|
| **A — the street** | first-person and eyewitness: diaries, letters and correspondence, memoirs and autobiographies, personal narratives (war, slavery, exploration), interviews and oral history, trial and inquest records, travel accounts written by the traveller | any LCSH form subdivision in {Diaries, Correspondence, Personal narratives, Autobiography, Interviews, Sources, Trials, litigation, etc.}; LCC CT (biography written as autobiography is caught by the form) | measure |
| **B — about people and events** | biography written by others, history, local history, art history, collected documents edited by a scholar | LCC C, D, E, F, N; LCSH form {Biography, History, Sources} | measure (the plan's "~15 %" is modelled) |
| **C — polished print** | encyclopedias, dictionaries, gazetteers, handbooks, almanacs, periodicals and annuals, textbooks, science and technology, law, religion as doctrine | LCC A (AE, AG, AP, AY), Q, R, S, T, K, B–BX except biography; LCSH form {Encyclopedias, Dictionaries, Periodicals, Handbooks, manuals, etc., Textbooks} | measure |
| **D — fiction, poetry, drama** | the novels, the plays, the verse, juvenile literature | LCC P (PN–PZ) and everything the other rows did not claim (the default row) | measure — the largest, by common knowledge of Gutenberg; the number comes from the manifest |

The first measurement is one query over the live manifest: documents
and chunks per tier under this rule, and how many records carry no LCC
and no LCSH at all (those fall to D by default, which is the safe
direction: the cheap tier). Nothing below is costed until that table
exists.

## 2. The toolbox, with what each needs

| tool | does | size / device | exact? |
|---|---|---|---|
| **GLiNER 2.5** (`fastino/gliner2.5-multi-v1`, Apache-2.0; checkpoints 74M–287M, published) | mentions (NER with our own labels), and **joint entity–relation decoding** — relations as a schema we declare, without an LLM | < 1 GB f16; GPU or CPU | bit-exact per stated run (invariant 12; measure it as any encoder) |
| the collection's **encoder** (Qwen3-Embedding-4B, already resident for gutenberg) | linking a mention to Wikidata (search its vectors, top-10 Q-ids); the **masked relation embedding** (`[PERSON] met [PERSON] in [PLACE] in [DATE]`, graphrag-compliance §9.3) | ~8 GB f16, already paid for | bit-exact (invariant 12) |
| a **reading LLM, 16 GB class** | relation extraction where GLiNER's schema is not enough; the Q-id pick among candidates "or none"; community summaries | Gemma 3 12B at Q6_K ≈ 10 GB + 8k context, or Qwen3 14B Q4_K_M ≈ 9 GB; the plan's own HHEM table ranks Gemma 3 12B (4.4 %) and Qwen3 8B (4.8 %) ABOVE Gemma 4 26B A4B (5.2 %) on hallucination (published, wikidata-plugins.md) | not bit-exact: `model` method, agreement floor measured (03 §3.2) |
| a **reading LLM, 32 GB class** (the plan's Gemma 4 26B A4B Q8_0 ≈ 27 GB) | the same, better JSON and longer context, on peers that have the card | 32 GB cards only, alone, at night | as above; a DIFFERENT model is a different sibling (wikidata-plugins.md) |
| **Leiden** (igraph or graspologic, pinned version, fixed seed) | communities over the graph | CPU, RAM ∝ edges | bit-exact given library version + seed (measure: two runs, byte compare) |
| **Fellegi–Sunter** scoring (our own small module, or `splink`) | linking an unnamed span to a Q-id or to another span by shared event slots (§9.3) | CPU | deterministic given pinned weights and threshold |
| **stylometry** (Burrows' Delta over function-word frequencies; `stylo`'s method, our code) | a per-record style vector; "nearest held authors" beside a hit — the pseudonym case | CPU, one pass per record | deterministic; a derived index or a claims collection, never a leg |

The 16 GB rule is met by everything in the table but the last LLM row,
and that row is a sibling's stated run, not a requirement. A peer with
a 16 GB card runs every tier; a peer with 32 GB runs the same tiers with
the larger reader where the Codex of THAT sibling says so. The two never
mix in one collection: a claim names its model digest.

## 3. The strategy, tier by tier

What every tier gets, cheap and bit-exact (the LazyGraphRAG index,
graphrag-compliance §5): GLiNER mentions over every chunk; each mention
linked to Wikidata by the encoder's top-10 and a deterministic pick
(the top candidate within a margin, else none — NO LLM in this step, so
the layer stays exact; the LLM pick is a tier A/B refinement); an
unlinked span KEPT as a `mention` with a null object (§9.2 row 2);
co-occurrence edges between the entities and spans of one chunk; Leiden
over the whole graph; no summary. On top of that:

| tier | adds | pros | cons | cost (modelled; the first measurement replaces it) |
|---|---|---|---|---|
| **A — the street** | GLiNER joint relations with a declared schema (met, wrote to, travelled with, lived at, employed, accused, married — the Wikidata properties we care about), then the 16 GB reader over chunks with ≥ 2 mentions for what the schema missed, with the masked template embedded for every relation; **cross-document linking** of unnamed spans by shared event slots (§9.3), including span-to-span across books; stylometry per record; LLM community summaries for A's communities only | the gems live here; the only tier where a link's evidence is a witness; the reader's tokens are spent on the smallest share | the reader is the costly, non-exact step; unnamed-span linking is probabilistic and must be shown as a candidate; A is small, so its communities are sparse until B joins | GLiNER ~free; reader ≈ 1 call per qualifying chunk (plan: 4–8k ctx) — at the plan's rate (~15 % of Gutenberg ≈ one week of a 32 GB card) a 16 GB reader on tier A alone is DAYS, not weeks, if A is a few per cent of chunks (measure) |
| **B — about people and events** | GLiNER joint relations with the same schema; the reader ONLY where a chunk has ≥ 3 linked Q-ids and GLiNER emitted nothing (a bounded fraction, measure); linking of unnamed spans to Q-ids (not span-to-span); LLM summaries of B's communities | most of the historical graph for the price of GLiNER; relations are between known Q-ids, so they are traversable and auditable against Wikidata; conflicts with Wikidata flagged as the plan says | GLiNER's schema is closed — a relation it has no label for is lost until the schema grows (a new method version); the reader's share must be capped or B costs what the plan costs | GLiNER over B's chunks ≈ hours on any GPU, CPU-feasible; the reader bounded to the fraction above |
| **C — polished print** | nothing beyond the base layer; Wikidata facts beside the hit do the "details to a known fact" work at query time (05 §5.1 facts); unnamed spans kept but never linked | near zero cost; exact; an encyclopedia article mentioning Q2044 enriches every Florence query across the library | no relations extracted — "Brunelleschi built the dome" is found by search, not traversed; periodicals' news items may hide a gem the rule puts here (an LCSH "Personal narratives" subdivision rescues the obvious cases) | GLiNER + linking only: the base layer's ~8B tokens (plan's estimate for ALL of Gutenberg) are mostly C and D; 2–5 days on the B70 for mentions is the plan's number for everything (modelled) |
| **D — fiction, poetry, drama** | the base layer with one change: unnamed spans are **anchored to the book** (§9.2 row 4, `gutenberg:<id>#<surface>`), never linked across books; a per-book character co-occurrence network (chapters as windows) as a `community` claim with `level` 0 of that book; no relations, no reader, no summaries | real places and people in novels still link to Wikidata (Florence in a novel IS Florence); a who-is-who per novel for nothing; the largest tier at the smallest unit cost | characters stay per book, so Sherlock Holmes in two volumes is two anchors unless a work claim (same-work / translation-of) unites the volumes — it can, through gutenberg-book-editions; no gems expected and none sought | GLiNER + linking + CPU; the bulk of the base layer's tokens, nothing else |

Two cross-cutting rules:

- **A relation needs two ends.** In A and B a relation with one unnamed
  end is written (its end is the kept span); in C and D it is not
  extracted at all. §9.3's linking then runs ONLY over A's and B's
  unnamed ends, which bounds the Fellegi–Sunter work to the tiers where
  an identity is worth finding.
- **Promotion is a rule change, not a re-run.** A book the rule put in D
  that a reader finds to be a memoir ("Robinson Crusoe" is PR; a real
  castaway's narrative may be too) moves by an LCSH form the origin
  carries or not at all: we do not reclassify by hand, we fix the rule
  (a new method version) and the next snapshot moves every such book.

## 4. Compute and sharing

- **Units.** A tier's work is cut by gutenberg's record parts, as
  embedding work is: a peer takes a part of tier B's mentions the way it
  takes a part to embed (04 §4.5), produces the claims with the stated
  run, signs them; the producer's own re-run of a sample (GLiNER,
  linking: bit-exact; the reader: the agreement floor) is the check
  before adoption (03). The Codex's `scope` per packet type
  (wikidata-plugins.md) becomes the tier rule per packet type.
- **Order of value per GPU-hour.** Base layer over A and B first (small,
  highest value per token), then the readers over A, then the base layer
  over C and D (large, cheap, parallel across peers), then the readers'
  bounded share of B, then summaries. Communities are recomputed as one
  block when a tier's claims change (the plan's rule), on the CPU, off
  the GPU loop.
- **The 16 GB floor.** Every stated run in the first sibling fits 16 GB:
  GLiNER (< 1 GB), the encoder (8 GB, resident anyway), the 12B reader
  at Q6_K (~10 GB) — not at once; the reader runs when the encoder is
  not embedding, as the plan already says for the 26B. A peer with 32 GB
  adds nothing to THIS sibling; it may produce the 26B sibling's claims.
- **Nights and pins.** The reader is pinned for the pass (app/model_pins.py)
  and runs in the embedder's idle windows; GLiNER and the linking are
  small enough to run beside embedding on the same card, which is to be
  measured (invariant 12's `-np 1` rule holds for the encoder; a second
  model on the card is not a second stream in the encoder).

## 5. Verification, per method kind

| method | kind | check |
|---|---|---|
| GLiNER mentions and joint relations, deterministic linking, co-occurrence, Leiden, stylometry, Fellegi–Sunter | deterministic (a new `method.kind`, 09 §9.5: pinned library/model digest, seed, weights, threshold) | re-run a sample, byte compare — like a chunker; no floor |
| the reader's relations, Q-id picks and summaries | `model` (09 §9.5 as decided 2026-10-20) | `model-agreement` (03 §3.2): re-run a sample, count agreement against the floor measured on the first 1,000 (gutenberg-graph-amendments.md "Decided") |
| a `same-entity` candidate (§9.3) | deterministic given its inputs | recompute the score; the claim carries evidence and confidence, never merges records |

A tier's claims collection may therefore be TWO collections: the exact
layer (never needs a floor, adopted on byte equality) and the reader's
layer (adopted on agreement). Splitting them means a 16 GB peer can
verify and serve the exact layer of every tier while trusting the
reader's layer at its stated trust level (03 §3.6). Recommended.

**Both annotate gutenberg and wikidata; neither annotates the other**
(operator's question, 2026-10-24: "an annotation of an annotation?" —
no). Every claim is identified by its content (09 §9.5), so the reader's
layer needs no pointer into the exact layer: a relation names two
wikidata records and a gutenberg chunk; a Q-id pick for a kept span is a
`mention` on the same chunk, `start` and `end`, with the object filled
where the exact layer's is null — matched by (chunk, offsets); a
summary is a `community` claim with the SAME sorted `members` and the
summary filled where the exact layer's is empty — the same community by
content, the member list duplicated (megabytes). 02 §2.10's rule that an
annotations collection is never a target stands, and `_targets` keeps
refusing one: a claim about a claim would go stale at every re-issue of
the claim beneath it, and §2.10's time rule is defined on records.

## 6. What this changes in the specification (asked, not written)

1. 09 §9.5: a `mention` with a null object carrying `surface`, `start`,
   `end`, `label`; an `anchor` end form `gutenberg:<id>#<surface>` for
   tier D; the `same-entity` claim type; a deterministic `method.kind`
   beside `model`. All inside hashed bytes — ask first (CLAUDE.md).
2. The gutenberg-graph Codex: `tiers = [{name, rule, packets}]` with the
   rule vocabulary of §1 (LCC letters, LCSH form subdivisions,
   `dcmi_type`), replacing the plan's `scope` by LoC class; inside
   `collection_id`.
3. 05 §5.3: a candidate `same-entity` shown beside the hit with its
   evidence; a tier-D character network as a fact beside a novel's hits.
4. 03 §3.2: the exact-layer check (byte equality on a re-run sample)
   beside `model-agreement`.

## 7. Measurements before any of it is kept

1. Documents and chunks per tier under §1's rule (one manifest query);
   records with neither LCC nor LCSH.
2. GLiNER 2.5 on 1,000 chunks of A and B: mentions found, linked by the
   deterministic pick, unlinked; joint relations emitted against the
   schema; bit-exactness over two fresh loads (invariant 12).
3. The 12B reader against the 26B on the same 200 chunks of A: relations
   agreed, JSON failures, tokens/s on a 16 GB and a 32 GB card — this
   decides whether the first sibling's reader is the 12B.
4. Fellegi–Sunter on A's unnamed ends: candidate links above threshold,
   precision on a judged sample from history and biography
   (graphrag-compliance §9.4).
5. Tier D's per-book network on ten novels: does it read as a who-is-who
   to a person who knows the book.

If the operator says nothing, the plan of wikidata-plugins.md stands as
written (one method for C, D, E, F, N with the 26B; unlinked mentions
dropped); this note is then the record of the alternative and its costs.
