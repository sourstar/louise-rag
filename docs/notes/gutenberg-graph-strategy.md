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
| **wikidata's QUERY encoder** (jina-embeddings-v3, `retrieval.query` adapter — `vega-jina-v3-query`, docs/notes/wikidata-recipe.toml; torch on louise-python behind VeGa) | linking a mention to Wikidata: the mention embedded as a QUERY of wikidata's space, searched against wikidata's vectors, top-10 Q-ids. **Invariant 1**: wikidata's space is jina-v3 at dim 512; a vector from gutenberg's encoder (Qwen3-Embedding-4B) searched against it is meaningless — the first draft of this note had that error (architecture session's review, 2026-10-25) | ~1.1 GB f16 (572M); resident on the node for the wikidata collection, an ADDITIONAL model on a 16 GB peer | bit-exact with `LOUISE_TORCH_DETERMINISTIC=1` — MEASURED for the document model (512/512, approved-encoders.md torch row); the query model's measurement is listed as following and is a precondition here |
| the **masked relation embedding** (`[PERSON] met [PERSON] in [PLACE] in [DATE]`, graphrag-compliance §9.3) | templates compared with TEMPLATES — never against gutenberg's or wikidata's vectors — so it is a space of the METHOD, named in it: one pinned run embeds both sides. Proposed: jina-v3's `retrieval.passage` run (already required above), so one torch model serves linking and templates | no extra model | as the row above |
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
| **D — fiction, poetry, drama** | the base layer with one change: unnamed spans are **anchored to the book** (§9.2 row 4, `gutenberg:<id>#<surface>`), never linked across books; a per-book character co-occurrence network (chapters as windows) as a `community` claim with `level` 0 of that book; no relations, no reader, no summaries. **Limitation of the anchor by surface** (review, 2026-10-25): two characters with one surface ("the doctor" in two households) collapse, one character with two surfaces ("Mr. Starbuck", "Starbuck") splits. The alternative is an anchor by a WITHIN-BOOK coreference cluster — deterministic string clustering of surfaces (exact; cheap; catches the second case, not the first) or a neural coreference model (a `model` method with its floor). Stated as a limitation of the first version; the cluster anchor is §7's fifth measurement | real places and people in novels still link to Wikidata (Florence in a novel IS Florence); a who-is-who per novel for nothing; the largest tier at the smallest unit cost | characters stay per book, so Sherlock Holmes in two volumes is two anchors unless a work claim (same-work / translation-of) unites the volumes — it can, through gutenberg-book-editions; no gems expected and none sought | GLiNER + linking + CPU; the bulk of the base layer's tokens, nothing else |

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
  GLiNER (< 1 GB), jina-v3 for linking and templates (~1.1 GB), the 12B
  reader at Q6_K (~10 GB) — not at once with the peer's own encoders;
  the reader runs when nothing embeds, as the plan already says for the
  26B. A peer with 32 GB adds nothing to THIS sibling; it may produce the
  26B sibling's claims. **A VRAM check comes first** (review, 2026-10-25):
  the node ran at 85–86.5 % of its 32 GB the night before, with its
  resident models; a 10 GB reader does not fit BESIDE them, only in their
  place, so the pass is scheduled when the encoders are unloaded, and the
  check is the budget's, not a guess.
- **Claims production is corpus work.** It runs at the CORPUS level of
  scheduling.md and stands aside on the WHOLE card while a query holds
  the claim (scheduling.md §12, amended 2026-10-25: embedding has the
  lower priority) — so a pass never slows a search, and its throughput is
  whatever the idle windows leave.
- **The exact layer runs on torch, and torch is not yet checked by the
  wrapper** (invariant 12, "Open"). GLiNER 2.5 is a transformers model,
  so it runs on louise-python with `LOUISE_TORCH_DETERMINISTIC=1`
  (`torch.use_deterministic_algorithms(True)` + oneDNN deterministic
  primitives; on NVIDIA also `CUBLAS_WORKSPACE_CONFIG=:4096:8`,
  approved-encoders.md torch row), and its bit-exactness is MEASURED
  before the first claim as louise-python-rebuild.md §4 says — several
  fresh loads, shuffled orders, every span and score byte-identical.
  Until that measurement passes, "the exact layer" is a design intent,
  not a property; a model that does not reproduce under the switch is
  not used.
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
5. **New dependencies, asked first** (CLAUDE.md): a Leiden
   implementation (`igraph` + `leidenalg`, or `graspologic` — the one
   GraphRAG pins; version pinned, seed fixed, both inside the method);
   the `gliner2` package in louise-python (a new tree, so a new stated
   run for every model on it — louise-python-rebuild.md); Fellegi–Sunter
   either as `splink` or as our own ~200-line module (the standard is the
   method, not the package; the package would pay only if its EM fit and
   blocking are used — write down which).
6. 09 §9.4/§9.5: the `rumors` member and states (rumors.md, §10 below).

## 7. Measurements before any of it is kept

1. Documents and chunks per tier under §1's rule (one manifest query);
   records with neither LCC nor LCSH.
2. GLiNER 2.5 on 1,000 chunks of A and B: mentions found, linked by the
   deterministic pick, unlinked; **the PRECISION of the pick on a judged
   sample** (a linked count without its precision is not a number — the
   same rule §9.4 of graphrag-compliance.md sets for `same-entity`);
   joint relations emitted against the schema; bit-exactness of GLiNER
   and of jina-v3's query run over several fresh loads and shuffled
   orders under `LOUISE_TORCH_DETERMINISTIC=1` (invariant 12).
3. The 12B reader against the 26B on the same 200 chunks of A: relations
   agreed, JSON failures, tokens/s on a 16 GB and a 32 GB card — this
   decides whether the first sibling's reader is the 12B.
4. Fellegi–Sunter on A's unnamed ends: candidate links above threshold,
   precision on a judged sample from history and biography
   (graphrag-compliance §9.4).
5. Tier D's per-book network on ten novels: does it read as a who-is-who
   to a person who knows the book; how many characters the surface anchor
   collapses or splits against a string-clustered anchor (§3, tier D).

If the operator says nothing, the plan of wikidata-plugins.md stands as
written (one method for C, D, E, F, N with the 26B; unlinked mentions
dropped); this note is then the record of the alternative and its costs.

## 8. What happens to what the analysis finds

*Operator, 2026-10-24: "What happens to the new entities discovered,
conflicting relationships and new relationships discovered during the
analysis?"* The rule under all three: Louise promotes nothing to a fact.
A discovery is a claim with its evidence, placed beside the hits and
traversable; the road to "fact" runs through Wikidata, with a person in
the loop, and the result flows back as the next snapshot.

**A new entity** — a thing no Q-id names.
- Exact layer: a kept span (`mention`, null object, §9.2 row 2 of
  graphrag-compliance.md), in every tier; anchored to its book in D.
- Tiers A and B: `same-entity` candidates tie spans across books
  (§9.3). The connected component of candidates above threshold is a
  **cluster**: a claim with the spans as `members` and an id derived
  from their content (as a community's is), traversable, shown as
  "probably one person, attested in these passages" with its score.
  Computed at build time, deterministic given the pinned weights, so
  bit-exact.
- A cluster is never a Q-id and is never minted as an identity (§9.2
  row 3, rejected). If its evidence is good, a person creates the
  Wikidata item citing the Gutenberg passages — Wikidata's notability
  admits "a clearly identifiable entity described by serious and
  publicly available references" — the next wikidata snapshot carries
  the Q-id, and the next gutenberg-graph update (annotation-collections.md
  "Updating": claims re-checked, kept claims keeping their uids) links the
  spans to it. Louise produces the evidence for an identity; Wikidata
  grants it.

**A conflicting relation** — a book says one thing, Wikidata or another
book another.
- Both claims stay, each with its passage. Nothing overwrites Wikidata
  (wikidata-plugins.md already says so) and nothing resolves the
  conflict by a score: a conflict is often the gem (a pseudonym, a wrong
  date in the record).
- The conflict is found by comparison — the same subject and property,
  a different object, where Wikidata's own single-value constraint
  (P2302 / Q19474404) says there can be only one — and RECORDED as the
  claim's rumor state against the wikidata snapshot it was checked
  against (operator, 2026-10-25; rumors.md, §10 below: this supersedes
  the first draft's "not stored"). It is shown as "the book says …;
  Wikidata says …", or "two sources disagree", with both passages.
- Back to Wikidata the same way as below: a person adds the book's
  statement with its reference, at Wikidata's normal or deprecated rank
  as they judge; Louise never edits.

**A new relation** — two known Q-ids, a property Wikidata lacks.
- A `relation` claim with its evidence chunk, attached at either
  entity's record (05 §5.3), walked by `louise_traverse`
  (`annotations:relation`), checked by byte equality (GLiNER) or the
  agreement floor (the reader), §5.
- Only Wikidata properties: a relation the method cannot express as a
  `wikidata:P…` is not a claim; growing the property set is a new method
  version, and the next snapshot carries what it now expresses. (An
  alternative, kept for the record and not recommended: a `relation`
  with a null property and the stating `surface`, the analogue of the
  null-object mention. It would keep what was read at the cost of a
  second untyped kind nobody can traverse.)
- Where the passage states a time ("in 1850"), the claim carries it in
  its own keys (the analogue of Wikidata's P580/P582 qualifiers), so a
  relation from a diary is a relation AT that time, and the chronology
  of §9.3 can be rebuilt from the claims alone.
- Back to Wikidata: a person adds the statement with the passage as its
  reference. The claim IS the reference package — book, chunk, offsets,
  text hash, the stated run that read it — which is what a Wikidata
  reference needs and what a later verifier re-checks.

The loop: discover → claim with evidence → a person proposes it to
Wikidata → the review's outcome and reason recorded against the claim
(rumors.md) → the wikidata snapshot → the gutenberg-graph update links to
it. Nothing
in it is automatic past the claim, and nothing in it needs a Louise
identity registry: Wikidata is the registry (CLAUDE.md, "do not reinvent
the wheel").

## 9. Prior art — the same work under other names

Nobody calls it forensic analysis of books, but each part of §3's tier A
has an established line of research, and the tools named in §2 are its
standard tools (operator's question, 2026-10-24).

**Who hides behind a name.** Two complementary traditions:
- *Stylometry* — authorship from the writing itself: Mosteller & Wallace
  on the Federalist Papers (1964); Burrows' Delta; the 2013 attribution
  of *The Cuckoo's Calling* to Rowling by Juola and Millican from word
  frequencies, letter 4-grams and word pairs in 1,000-line chunks
  (https://www.techwell.com/2013/07/analysis-software-wrecked-jk-rowling-s-anonymity,
  https://teach.htrc.illinois.edu/?p=216), and a 2024 preprint
  questioning how strong that evidence was
  (https://arxiv.org/abs/2407.10301) — style is probabilistic, hence
  "nearest held authors" as a fact beside a hit, never a merge.
- *Entity resolution in archives* — identity from what is said about a
  person, which is §9.3's method: **Golden Agents** (Amsterdam) links
  persons across notarial acts, portraits and Wikidata by embedding
  similarity over shared record context plus domain rules, reporting
  very high precision
  (https://research-portal.uu.nl/en/publications/adding-domain-knowledge-to-improve-entity-resolution-in-17th-and-/,
  https://zenodo.org/record/7478545); the **HIPE** shared tasks (CLEF
  2020, 2022) benchmark NER and linking on 200 years of noisy historical
  newspapers in English, French and German — our steps 1–2 by name
  (https://ceur-ws.org/Vol-2696/paper_255.pdf,
  https://api-impresso.uni.lu/hipe).

**A story rebuilt from witnesses who never met.** Narrative extraction
and timeline reconstruction: the **Text2Story** workshops at ECIR — event
extraction, temporal and causal ordering, cross-document event
coreference, automatic timelines
(https://text2story23.inesctec.pt/); Bugert et al.'s cross-document
event coreference line is "the same event, different diarists" by name.
Slot alignment of events (§9.3) is how those systems decide two reports
describe one event.

**Networks nobody wrote down.** Co-occurrence graphs over text — the
LazyGraphRAG index — have a record in history:
- **Six Degrees of Francis Bacon** (Warren, Shore et al., DHQ 2016):
  13,000+ early-modern Britons and ~200,000 relations inferred from
  co-mentions in the Oxford DNB, names by NER and semi-automatic
  disambiguation; inferred edges drawn grey, human-confirmed black
  (https://dhq-static.digitalhumanities.org/pdf/000244.pdf). Grey versus
  black is our "candidate, never a merge", in a picture.
- **Tudor Networks of Power** (R. and S. Ahnert, OUP 2023): the Tudor
  intelligence and diplomatic network from 132,747 State Papers letters
  and 20,560 people, centrality and brokerage measures choosing whom to
  read closely (https://academic.oup.com/book/51646).

What Louise adds to that toolkit, and those projects lacked: a shared
identity space everyone else also uses (Wikidata), and claims that are
signed, shareable and re-checkable by a stranger. What the toolkit has
that Louise does not yet: stylometry — a per-record style vector as a
derived index, bit-exact, never a leg; a separate proposal if wanted.

## 10. The purpose, as the operator stated it — and four adjustments

*Operator, 2026-10-24: "wiki gutenberg is the bridge between gutenberg
and wikidata. The processing will log the gap between what is found in
the books and what is mentioned in wikidata (potential new entities,
conflicting relationships, potentially new relationships). This log may
enrich the search with 'hypothesis' and be used to ask wikidata to
evaluate a review. Utopically the gap will close when wikidata processes
all the claims and the hypothesis leg will go down to zero."* It holds,
with four adjustments that make it Louise's:

1. **The gap is RECORDED: the rumors** (operator, 2026-10-25, confirmed
   to this session; the design in [rumors.md](rumors.md), branch
   ccr-d144b7d4-l13jt8 at e7bb63f: "'The gap' shall be recorded, it is an additional pillar that
   can be optionally used for searches, the 'rumors'. … they are part of
   the collection. … It's a unique contribution of louise"). This
   supersedes the first draft's "a view, not a log": a rumor's state
   depends on a snapshot of ANOTHER collection and its history — first
   seen, which review closed it, how — cannot be recomputed once the next
   review has happened. So every claim carries a rumor state against the
   wikidata snapshot it was checked against — `open`, `adopted` (Wikidata
   now states it citing OUR passage: a person agreed with our evidence),
   `confirmed` (Wikidata states it on a reference of its own: independent
   evidence, kept apart so the collection cannot confirm itself),
   `rejected`, `known` — in its own member keyed by claim uid, with what
   was `proposed` where and when and the `review`'s outcome, reason and
   source; re-issued at each `hold annotations update`; closed rumors
   stay in the collection (promise 7). The design, its member bytes and
   the spec changes it needs are rumors.md's.
2. **A rumor is never fused** (invariant 4). Optional in a search and
   off by default (rumors.md): beside a hit as a fact labelled *rumor*
   with its confidence, passages and date first seen; a SEPARATE LIST of
   records reached through open rumors — another question, presented
   apart (05 §5.1); a FILTER ("records with an open rumor about Q2044" —
   restricts, never boosts, 05 §5.2). Counts of open rumors are
   descriptive, never a target (rumors.md).
3. **The review channel is Wikidata's own: the Mismatch Finder.**
   Wikimedia's tool takes an external dataset's mismatches (item,
   property, Wikidata's value, the external value, the external URL,
   source, expiry) by CSV through an API; editors review them in a queue
   and fix Wikidata or mark the source wrong
   (https://github.com/Wikidata/Purdue-Data-Mine-2024/blob/main/upload_mismatches.py,
   https://www.ifla.org/?p=217993). A conflicting-relation claim IS the
   row; the external URL is the Gutenberg passage. New relations and new
   entities have no queue: ordinary edits by a person, the claim as the
   reference package. Louise never edits and runs no bot (Wikidata's bot
   policy; not our place). CLAUDE.md: the standard, not our own queue.
4. **Rumors never reach zero, and convergence is not what is measured**
   (operator, 2026-10-25: "Rumors cannot go to zero, it is understood.
   New rumors will be found over the time because new books are added
   and because future hardware will allow better models in the future
   sister collections. The convergence speed is useless, a statistic
   about the adoption and the rejection rate and reason of our proposals
   instead is fundamental for improvement"). This replaces the first
   draft's "zero is honest for history and biography". What each rumor
   records, from the source that gave it (rumors.md): whether it was
   proposed, where and when; the review's OUTCOME and its REASON — the
   Mismatch Finder reviewer's verdict on our row (our value wrong,
   Wikidata's wrong, both, neither), a deprecated statement's "reason for
   deprecated rank" (P2241), a statement added citing our passage
   (`adopted`), our reference removed. What the collection reports at
   each update, on its card: the adoption rate and the rejection rate,
   the rejection reasons, each broken down by rumor kind, property, tier,
   method and model (each sister collection apart), confidence band and
   language. That is the improvement loop — which properties, genres,
   methods and confidence bands get rejected and why; hence where each
   kind's floor belongs, what the next method version fixes, and whether
   a sister collection's model earns its cost. `confirmed` (independent
   evidence) is reported apart from `adopted`. Tier D's anchored
   characters are not rumors about the world and are excluded by
   construction.

Two cautions. Feedback latency is the wikidata snapshot cadence — Louise
reads Wikidata through its dump, never live — and a rejection seldom
reaches the dump at all (an editor who dismisses a mismatch rarely writes
a deprecated statement), so `rejected` is also read from the Mismatch
Finder's review outcomes for the rows we uploaded (rumors.md). And the
gap is a flow, not a stock: every Gutenberg snapshot, every new tier and
every sister collection's better model adds to it, which is why no count
of open rumors is a target and no speed of closure is measured.

**Order** (rumors.md, consistent with §4's order of value): rumors start
with tier A only — diaries, letters, memoirs, personal narratives — under a
strict floor; the judged PRECISION and the YIELD on a sample come first
(how many rumors are right, and how many of the right ones are worth a
reader's time rather than a mundane fact Wikidata merely lacks); then they
are proposed and the adoption and rejection reasons read; only then the
other tiers.
