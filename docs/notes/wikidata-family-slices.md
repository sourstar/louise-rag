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
| a new family, a family split, a newer class tree | a new map is a new slicing block, so a daughter that re-slices | no GPU, but every record and vector file is re-issued: the whole collection moves again for peers |

So the binding constraint is the last row: **the family list must be right
at creation** — binding for good once the collection is published; before
that, a wrong family costs a re-creation and a local re-cut, no GPU. The
family pass logs each family's size before `create`, and the sizes are
reviewed then, not after.

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
