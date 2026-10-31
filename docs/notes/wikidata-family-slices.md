# Wikidata sliced by family — the decision and its background (2026-10-24)

*Operator, 2026-10-24: "Go with A, but write down today's report so that
the day we need to take another decision we have a background." This note
is that background: the question, the rule it met, the three ways out with
their pros and cons, what was chosen and why, the design, the numbers it
rests on, and what would reopen it. The day's wider report is
[wikidata-completeness-report.md](wikidata-completeness-report.md).*

## 1. The question

**Choose what to embed, as Wikipedia does by language** (operator: "Propose
a way to choose what to embed like it is currently done for Wikipedia
language fetching/embedding"). Embedding costs card time; wikidata holds
75.9 M records. The operator also fixed, the same day, that cost is never
answered by leaving entities out of the records: "If not written
explicitly in the guidelines a collection cannot prune any information
from the corpus" (CLAUDE.md, invariant 11). So the 9,680,790 Wikimedia
pages (categories, templates, disambiguations, lists) stay records, and
the choice has to be about what is EMBEDDED.

**How Wikipedia does it.** Each language is a slice (`lang=it/pages=…`,
app/pack/slicing.py). A node's collection file lists the slices it holds
(`slices`); what it holds it embeds (`embed`, 07 §7.8). A profile — "a
language" — names a selection of slices (06 §6.2). The snapshot holds every
language; a node picks.

**Wikidata until now** was sliced by id range only (`range=` id ÷ 10⁷):
nothing a person would choose, so a node held and embedded all of it or
none.

## 2. The rule it met

A slice key must be "known from the record before any embedding, and
stable for the life of the record (so that updates do not move records
between shards and churn the torrents)" (pack-format.md §2, slicing.py).
Publication year, language, the origin's ids qualify; "last modified"
does not.

A family read from Wikidata's class tree is NOT stable as such: one edit
to a subclass link (P279) can move hundreds of thousands of records to
another family at the next dump — the class tree is edited daily, and
measured odd (six P279 cycles; clinical trials, theses, stars and coins
under "occurrence", upstream-reports/wikidata.md W3, W5). An entity that
editors reclassify (its own P31) moves too.

## 3. The ways out, with their pros and cons

| | pros | cons |
|---|---|---|
| **A. A family map pinned at creation.** The class tree of the creation dump decides each class's family; the map is kept with the collection and never changes; later edits to the class tree move nothing; only an entity's own reclassification moves it | per-family holding and embedding, Wikipedia's mechanism; every node computes the same slices from the record and the pinned map, so it is verifiable; a moved wikidata record was going to be re-issued anyway — a changed record is, at every dump | an amendment of the slicing rule; a class created after the creation dump needs a rule (below); ~1.5 days of work and a pass on the node; Monday's records build moves by about a day |
| B. Each record's family frozen at its first appearance | perfectly stable | a record's slice depends on the collection's history, not on the record and a pinned map, so a verifier needs the chain; a misclassification at birth stays for ever; a larger amendment |
| C. Id ranges kept; families chosen inside each part | no slicing change; Monday as planned | parts with partial vectors, which the embed stage, the coverage map, the bundles and the checks do not support — a deep change; peers cannot split the work by family |

**Decided: A** (operator, 2026-10-24: "Go with A"). Recommended for its
first pro: it is the mechanism the platform already has for Wikipedia,
with the one change that makes a class tree behave like a language — it
is frozen for the collection's life.

## 4. The amendment (pack-format.md §2)

*A mutable collection may slice by a value its plugin derives from a map
pinned at the collection's creation — a member of the SHARD, its hash in
the collection's slicing description. The map never changes for the
collection's life, so a record moves between slices only when its own
fields change — the change that re-issues it anyway. First use: Wikidata's
families (2026-10-24).*

A new map (a newer class tree) is a new collection (a daughter, 02 §2.7):
the map is part of what the collection is.

## 5. The design

**Revised 2026-10-24 evening, on the node's first report (München's
review, map `f79007bd…`, 75,896,183 records):** `other` was the largest
family (21.5 M, none of its classes listed); church buildings sat in
`place` (geographical feature nearer than built structure); UK and Irish
statutory instruments, acts of parliament and "events in a year" sat in
`internal`; `heritage` was every built structure (street 726 k, house
242 k, gas station 64 k). So: `heritage` is monuments, archaeological and
heritage sites and the P1435-designated, and a new `built` family holds
the built structures and architectural elements; `place`'s roots are
human settlement, administrative territorial entity, landform, body of
water and protected area (no longer "geographical feature"); `internal`
is a FALLBACK — its roots take only classes no other root reaches; the
precedence is scholarly, heritage, built, event, person, place,
organization, work, taxon, internal, property, lexeme, other (families.py).
The report now lists `other`'s top 50 classes and PROBES — city,
municipality, comunes and communes, church, cathedral, castle, museum,
street, legislation… with the family the map gives each. The table below
is the first version, kept for the record.

**The second report (map `162b7654…`, 8ca3810; München, 2026-10-24 night)
— create stays on hold.** The fallback emptied `internal`: every Wikimedia
category, disambiguation page, template and list article (~8.7 M) reaches
creative work by a longer path and went to `work`. `other` is 22.5 M,
about 7 M of it astronomy and about 3.5 M chemical entities, genes and
proteins. Also in `other`: version/edition/translation (830 k), written work
and document, which belong in `work`; cemetery (277 k); and "constitutional
republic", a P31 of countries. Church, cathedral and castle are `built`, as
designed: `heritage` is what is commemorative or archaeological by its class,
plus the P1435-designated.
Two fixes in a row each traded one error for another, both chosen on
top-25 lists. So no root is chosen any more without the dump's own paths.
`louise dev wikidata-classes` prints, for a class, its shortest P279 path to
each family's root. Given a candidate root set, it estimates the family sizes
from the codes pass already done and lists the classes that would move, in
minutes and without another pass. The first candidate,
`wikidata-coverage-2026-10-24/candidate-roots-1.json`:
- `internal` is no longer a fallback;
- new `astronomy` (astronomical object) and `molecular` (chemical entity,
  gene, protein) families;
- `work` adds version/edition/translation, written work and document;
- `place` adds country and sovereign state;
- `heritage` adds cemetery.
Its paths and preview decide each root. The legislation that once landed in
`internal` is checked by its own path.

**The third revision, adopted 2026-10-25 (the code's roots).** The paths
and preview of candidate 1 (München, 07:10 CET; on main,
`wikidata-coverage-2026-10-24/{families-162b7654fcd2c534,candidate-roots-1}.classes.json`)
confirmed both unverified ids: Q6999 "astronomical object" and Q3024240
"historical country", the latter reaching `place`. Every large move went
where intended:
- Wikimedia categories, disambiguation pages, templates, lists and modules → `internal`;
- stars, galaxies, radio sources, binaries, asteroids and quasars → `astronomy`;
- chemical entities, genes, proteins and stereoisomers → `molecular`;
- versions/editions (830 k), written works (156 k), documents (139 k), issues and volumes → `work`;
- cemeteries (277 k) → `heritage`.

Legislation stayed in `work`, but by one edge. Its path to `internal` runs
legislation (Q49371) → "Wikidata item that needs to be split" (Q91615393) →
Wikidata item → Wikimedia internal item: 6–8 steps, against 8–10 to creative
work. So **legislation is a `work` root**, and the bad edge goes in the
standing report to Wikidata (W8). For the same reason, **cell line is a
`molecular` root**: upstream makes it a direct subclass of taxon (W9), and
its 153,635 items would otherwise count among the taxa.

Estimated entities per family (records and pruned):

| family | entities |
|---|---|
| scholarly | 46,125,519 |
| heritage | 3,206,992 |
| built | 3,169,402 |
| event | 1,473,369 |
| person | 13,736,789 |
| place | 6,773,578 |
| organization | 3,668,794 |
| work | 9,833,914 |
| taxon | 4,168,953 |
| astronomy | 8,380,425 |
| molecular | 3,538,551 |
| internal | 9,247,136 |
| property | 13,972 |
| other | 8,569,598 (was 22.5 M) |

Left in `other`, deliberately: collections and collectables, items of
collection or exhibition, clinical trials, family names, and "constitutional
republic", which countries also carry while their country and
sovereign-state classes are `place`. The creation map and the codes under
these rules (a pass of about an hour), then their report, decide `create`.

**The map create pins: `sha256:78399d609759f3ac…` (41e3112's rules; report
at origin/main 23acfe4, München 2026-10-25 09:28–10:19 CET).**

| family | entities |
|---|---|
| scholarly | 114,947 (+46,010,584 pruned) |
| heritage | 3,208,175 |
| built | 3,168,807 |
| event | 1,473,144 |
| person | 13,736,789 |
| place | 6,773,668 |
| organization | 3,669,054 |
| work | 9,860,689 |
| taxon | 4,015,318 |
| astronomy | 8,394,562 |
| molecular | 3,844,690 |
| internal | 9,250,962 |
| property | 13,972 |
| other | 8,371,406 |

Every probe lands as intended: legislation → work, Wikimedia category →
internal, cemetery → heritage, country and sovereign state → place, cell
line → molecular, edition, written work and document → work.

`other` holds nothing above 700 k; family name, 690 k, is the largest.
**Approved for create.** The small misplacements stay for the ONE later
re-creation (operator, 2026-10-24: later changes go in one re-creation),
with scholarly articles, lexemes and the niche languages:
- railway station (94 k) and tram stop (26 k), likely `built` through
  "facility" (Q13226383), with its paths and a preview first, since
  facility is broad;
- park (86 k) and ship (76 k);
- educational facility (66 k) and facility (52 k).

Fictional humans (48 k) stay out of `person`, which is real people (human,
Q5).

**Families, in precedence order** (an entity in several takes the first):

| family | an entity is in it when | measured (dump 20261012) |
|---|---|---|
| `scholarly` | its P31 is scholarly article (Q13442814) — pruned by D1 until the operator's rule of 2026-10-24 ("everything belonging a collection must be part of the collection"); now an optional slice | 46,010,809 |
| `internal` | its P31 is under Wikimedia internal item (Q17442446) | 9,680,790 |
| `heritage` | under built structure (Q811979), monument (Q4989906), archaeological site (Q839954), architectural element (Q391414), or it has a heritage designation (P1435) | to count (its roots' instances overlap) |
| `event` | under planned event (Q1656682), military operation (Q645883), war (Q198), calamity (Q1689296), massacre (Q3199915), explosion (Q179057), geographically localized event (Q123349687), severe weather (Q741507), demonstration or protest (Q130260549) | to count (the families replace occurrence, W5) |
| `person` | under human (Q5) | to count |
| `organization` | under organization (Q43229) | to count |
| `place` | under human settlement (Q486972), administrative territorial entity (Q56061), geographical feature (Q618123) | to count |
| `work` | under creative work (Q17537576) | to count |
| `taxon` | under taxon (Q16521) | to count |
| `property` | a property (P…) | ~12 k |
| `lexeme` | a lexeme (L…, with its forms and senses), from the lexeme dump | to count (a second origin file) |
| `other` | none of the above | the rest |

The roots' labels are checked against the pinned dump's label table when
the map is built (a wrong id shows as a wrong label in the log), and the
map's size per family is logged; the numbers above marked "to count" come
from that pass.

**A class under roots of several families** (decided while building it,
2026-10-24): the family of the NEAREST root, by P279 steps, and a tie to the
first in precedence — a municipality is one or two steps under
"administrative territorial entity" and further under "organization"
(through "local government"), so it is a place; precedence alone would have
put every municipality in `organization`. Multi-source breadth-first search
from all the roots at once (families.py `nearest_roots`). The ENTITY-level
rule stays precedence: an entity whose P31 values fall in two families takes
the first. `scholarly` is a root like the others (scholarly article and its
subclasses); the pruning rule stays its exact P31, so on Monday a review
article is a record in `family=scholarly` while the scholarly articles
themselves are not yet records (option 3).

**The pinned map** — every class under a root, with its family, from the
creation dump's P279 closure; a SHARD member (`slicing/families.json`, its
sha256 in the slicing description), built once by `create` from the pack.

**An entity's family** — from its own statements, at the dump being read:
its non-deprecated P31 values looked up in the pinned map, its P1435 (a
stated or unknown value; "no value" designates nothing); the first family
in precedence order. A class not listed whose number is at most the map's
`max_item` existed at creation under no root: `other`, for ever. A class
created after the creation dump (a larger number) is walked up the CURRENT
dump's P279 links to the nearest class the map knows (breadth first, a tie
to the first family) — deterministic per dump; such classes are new, so the
walk rarely moves an existing record, and each pass logs how many it
placed. No class found: `other`.

**The slice** — `family=<family>/range=<id ÷ 10⁷>` (`family=property/
range=100` for properties): the family is what a person chooses, the id
range keeps parts of even size under it.

**Without parsing 75.9 M documents** — the plan pass asks `slice_hint` for
every manifest row: the pack keeps each entity's family code (one byte an
id, a 140 MB array beside the label table), computed by the same pass that
builds the map.

**Phase 2** — `embed_slices` in a node's collection file: hold every record
(word search over all of it) while the encoder spends only on some
families; the Library's card offers the families by name.

## 6. Effort and order — built 2026-10-24

1. Parser 2's history section: built (eea21cc).
2. Built the same day: the pack's version 3 (a complete version 2 is
   upgraded in place from its own lines, RULE 1): `subclass` — every item's
   P279 links — and each chunk's largest item, then `families-<h>.json`, the
   map this dump gives, then `families-<h>.u8`, every entity's code under it
   (one byte an entity, in dump order) and `families-<h>.report.json`, each
   family's records and the classes that fill it. `create` takes the block
   and the map from the plugin (`WikidataSource.creation_slicing`: the
   pinned dump's pack) and writes the member; each snapshot lists it;
   `share` moves it as the member `slicing`; the records build reads the
   Codex's map from the SHARD, checks its hash and slices by the pack's
   codes (`slicing.context_for`, `slice_hint`); `hold check`'s `records`
   check verifies the member and every sampled record's slice. A Codex
   sliced by id range keeps its ranges. Tests:
   tests/test_plugins_wikidata_families.py.
3. On the node (the order matters — the family list is fixed at `create`):
   deploy; the worker's scan of wikidata upgrades the pack to version 3
   (the class tree is the 18-minute pass of the classes run, the codes about
   as long as its entity pass, ~40 min — counted then); READ
   `families-<h>.report.json` before `create` (a family badly filled is
   fixed in the roots now, never after); `create` on the new target; the
   records build. Parsing does not wait for the families (a version-2 pack
   whose names are done is enough); discovery does (it finishes the pack
   first).

**Option 3** (operator, 2026-10-24: "Go with 3"): the scholarly articles
and the lexemes join later. Both families are in Monday's map, so they join
as a daughter that adds parts and moves no record (§7).

## 7. The constraints, now and later (operator's question, 2026-10-24)

*"About Family map pinned at creation, explain the slicing constraints and
future proof constraints."*

**What any slice key must satisfy**: pack-format §2, plus the fact that the
slicing block is inside `collection_id` (09 §9.3), so it is fixed for the
collection's life:

| constraint | why | how the pinned map meets it |
|---|---|---|
| read from the record alone, never from a vector | slices exist before anything is embedded | the record's own P31 / P1435, looked up in the map |
| stable for the record's life | a move re-issues two parts (the one it leaves, the one it joins) and churns the torrents | the map never changes; a record moves only when its own P31 / P1435 is edited, and that edit re-issues its part anyway |
| the same on every peer | an update is a pure function of (previous manifest, origin, cutoff); two honest updaters must make byte-identical parts (pack-format §6.1) | the map is a SHARD member, its hash in the slicing block |
| answerable without parsing | the plan pass reads 75.9 M manifest rows | one family byte per entity, kept in the pack |

**What the map freezes, and what each later change costs.** *No official
daughters so far (operator, 2026-10-24: "No official daughters so far, rule
1"): until wikidata has been published, every "daughter" below is a
RE-CREATION — the collection created again with the change, carrying what
is identical (the pack, the calibration with `--calibration-from`, the
vectors of unchanged chunks); later changes are batched into one. The
daughter is the path once something official exists.*

| later | what happens | cost |
|---|---|---|
| a class created after the creation dump | placed by walking the CURRENT dump's P279 links to the nearest class the map has | the one leak: its records move if that class is re-linked upstream. Each update logs how many records the walk placed and moved; that count is the trigger for a new map |
| scholarly articles and lexemes join (operator, 2026-10-24: "Go with 3", later) | a daughter (the parser emits records it did not); their families are already in the map, and the range rule is written over the entity's number in its kind (Q42 → 42, P31 → 31, L7 → 7), so lexemes fit it too | only their new parts; no existing record moves; every part and vector inherited |
| a language added to the text | a SECTION, never a slice: an entity is one record carrying every language (invariant 11). A daughter re-issues the changed parts and keeps every English vector (02 §2.7) | only the new language's chunks are embedded |
| a new family, a family split, a newer class tree | a new map is a new slicing block, so a daughter that re-slices (before publication: a re-creation). *Superseded the same day — operator: "a daughter allows you to reuse all the vectors and corpus of the mother … functionally equivalent to a new collection created from scratch": parts cut by content and the daughter step, below this table.* The code learns the family; every map already pinned keeps reading by its OWN family list (families.py; fixed 2026-10-24, when the operator asked "What happens if a new family is created?": the first version read only maps of the code's current list, so adding a family would have made every pinned map unreadable) | no GPU: the family codes again (~40 min, CPU) and the records re-cut into new parts; before publication that is all; after it, every record and vector file is re-issued and peers fetch the whole collection again. The vectors are the same bytes (same chunk text): carrying them across a re-cut needs `louise dev carry` to accept other parts, owed when the first such change comes |

**A family added, as built (2026-10-24, operator's requirement and "I
agree").** The parts are cut BY CONTENT (02 §2.2, `slicing.parts`), so a
collection's layout is a pure function of its records and its map. Adding
a family: the code learns it (families.py); `hold collection continue
--reslice` writes the daughter's Codex with the map of the code's families
over the pack's class tree (a minute); `dev carry --resliced` moves the
vector table (A1g); `hold records build <daughter> --records-from <mother>`
builds her parts from her mother's record lines — none parsed, each cut
into chunks again (the chunker only); the family codes under the new map
are one CPU pass over the pack (~40 min). No GPU, no record re-derived, and
her parts are byte for byte those of a collection created from scratch with
the new map (tests/test_plugins_wikidata_families.py). Peers fetch only the
parts whose records changed family. So the family list is no longer a
for-ever choice; it is still reviewed before `create` (each re-slice costs
the CPU above).

**Languages to fetch and embed** (operator, the same day: "if it makes sense
choose which languages to fetch and embed"). For fetching there is no choice
to make: the dump is one file, and each record keeps every language
(invariant 11). For embedding, the languages are sections: English for every
entity from Monday; later, the niche it / de / fr / es sections (parser 3)
where Wikidata says more in that language than in English, counted over the
monuments, buildings and events selection (de 945,914 entities, fr 723,772,
it 277,416, es 220,546; 287 M tokens; classes-niche.json). A `lang=` slice
key, announced to the operator and then withdrawn the same day, would have
been constant (`mul`) for every item. The lexemes are the one part with a
language of their own, and they are small next to the items, so a node takes
them whole.

## 8. What would reopen it

- A newer class tree that matters (a family badly mis-assigned by the
  pinned map): a daughter with a new map.
- `other` growing large over the years (new classes the walk cannot place).
- The family list itself (a family a person wants that is not here: add it
  in the next map, never by editing this one).

## 9. What a node holds is what it processes (operator, 2026-10-31)

*"As for other collections the grow profile should drive what's fetched and
processed."* This replaces Phase 2's `embed_slices` (§5: hold every record,
embed some).

- **Each family is a profile** in the acquisition spec, additive as
  Wikipedia's languages. The Library grows a collection by a family, and
  the collection file lists the families it holds (`slices =
  ["family=person", …]`). Every family fetches the one dump.
- **Processing follows the holding** (app/slice_scope.py). After each scan,
  `documents.in_scope` is narrowed to the documents of the families held.
  The family is read from the pack's code for each entity, without parsing,
  in windows from a cursor. The collection's stages wait until that pass
  has caught up.
  - A family not held is not FTS-indexed and not embedded on this node, so
    it is not searchable here, not even by name.
  - Its records are untouched: invariant 11 holds, and growing by a family
    puts its documents back in scope on the next pass.
- **Proposed first holding** (München, from the 10-30 records; chunks,
  tokens sampled): person, work, place, organization, event, heritage,
  built, scholarly and property, 43.3 M of 77.3 M chunks. Left out: taxon,
  astronomy, molecular, internal and other.

| Choice | Pros | Cons |
|---|---|---|
| The nine families above | ~56 % of the chunks; the families the queries name | Taxa, stars and molecules are not searchable here until grown |
| All fourteen | Everything searchable | ~138 card-days of embedding at today's 6–7 chunks/s, against ~77 for the nine |

## 10. Which encoder (operator, 2026-10-31: "I follow your recommendation")

jina-v3 was chosen to carry the Wikidata Embedding Project's vectors. That
reason is gone: 14 % of them were made from older texts (D2), and our texts
are our own, from the current dump (D1, D4), so none can be carried. Wikidata
embeds everything itself, and its space has no vectors yet, so changing the
encoder costs nothing now.

**Decision:** measure, then switch to bge-m3 (wikipedia's encoder) unless
jina-v3 finds entities better.
- **bge-m3's advantages:** about 4× faster here (~19 card-days for the nine
  families against ~77); exact with flash attention, and batching allowed;
  MIT licence; one model shared with wikipedia.
- **Its cost:** vectors twice as large (1,024 dims).

**The test** (`tools/measure/wikidata_encoder_ab.py`) is a known-item test:
- ~20,000 entities from the nine families, read from the 10-30 record
  parts and cut as the space would embed them;
- 400 questions written by the chat model without the entity's name, half
  in English and half in Italian;
- each encoder ranks the whole sample through the production client, the
  query side included;
- measured: MRR@10, recall@1 and recall@10, a paired randomization test, and
  each by family and by language.

**The rule, reversed by the operator the same evening** ("If bge is as good
we switch to jina because it's less disk space. Use jina gguf for the
comparison so we can evaluate both speed and reliability"): bge-m3 only if
it is BETTER on MRR@10 with p < 0.05; otherwise jina-v3, whose vectors are
half the size.

**jina-v3 on llama.cpp joins the comparison** (`--encoder jina-gguf`). llama.cpp
has the `jina-bert-v3` architecture since build b6311 (late August 2025),
and the node's tree has it too (c0fd786).
- **The weights:** our own f16 conversion from ab036b0 with that tree's
  `convert_hf_to_gguf.py`, which writes the base model and one LoRA a task.
  The bytes are compared with second-state's f16 for provenance. Each side's
  LoRA is merged in (`llama-export-lora`), because VeGa stated no run that
  loaded a second file. Since 2026-10-31 it does (09 §9.3 amended: the
  adapter and its scale are in the weights digest), so base + adapter is
  the other route: one base on disk instead of two merged copies, at the
  cost of the adapter's matmuls per call, and only once the node shows that
  LocalAI's llama.cpp backend opens an absolute `lora_adapters` path as
  given. The two routes are two weights digests, so two spaces: one is
  chosen before the collection embeds.
- **Two models:** `vega-jina-v3-gguf` and `vega-jina-v3-gguf-query`.
- **What llama.cpp does not do:** it does not apply the adapter's prompt
  prefix, so the tool puts the task instruction first. The tool also makes
  the 512 cut.
- **What the score adds:**
  - chunks per second for each encoder (speed);
  - the llama.cpp vectors' cosine to the torch reference's, chunk by chunk
    (reliability);
  - bit-exactness over fresh loads, by `tools/vega_repro_check.py --sweep`
    on the two models, measured apart.

(The rule before:) keep jina-v3 only if it is better on MRR@10 with p < 0.05.
Otherwise wikidata's space is created again on bge-m3 before any vector is
written (RULE 1: nothing was published from it), and the records stay as
they are.

## 11. Hold all fourteen, embed nine (operator, 2026-10-31)

*"hold all 14 and embed 9".* §9 is amended. The node holds and serves
every family's records and keeps its text index whole, so a peer can fetch
any family by slice (`share fetch --slice family=…`). The encoder spends
only on the nine.

The collection file holds `slices = "all"` and names the nine in a new key,
`embed_slices`.
- **The mechanism:** the slice-scope pass marks a document outside them
  "not embedded here" (`embed_done = -1`), and moves it back to 0 when the
  list grows. That keeps it out of the embed stage's candidates without a
  per-round skip. Every count of `embed_done` reads `= 1` (the code that
  summed the column is changed with it). No new column and no index
  rebuild.
- **Its alternative:** a new column (`embed_held`) in the scope index, which
  means rebuilding that index on 75.9 M rows.

Built 2026-10-31: `embed_slices` (07 §7.8), `slice_scope.DECLINED`, the counts read `= 1`, the Library's dense target leaves the declined out. On the node: the file to `slices = "all"` plus `embed_slices` = the nine, then the pass once more (it widens in_scope back and declines the five).

