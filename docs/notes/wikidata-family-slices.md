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
| `internal` | its P31 is under Wikimedia internal item (Q17442446) | 9,680,790 |
| `heritage` | under built structure (Q811979), monument (Q4989906), archaeological site (Q839954), architectural element (Q391414), or it has a heritage designation (P1435) | to count (its roots' instances overlap) |
| `event` | under planned event (Q1656682), military operation (Q645883), war (Q198), calamity (Q1689296), massacre (Q3199915), explosion (Q179057), geographically localized event (Q123349687), severe weather (Q741507), demonstration or protest (Q130260549) | to count (the families replace occurrence, W5) |
| `person` | under human (Q5) | to count |
| `organization` | under organization (Q43229) | to count |
| `place` | under human settlement (Q486972), administrative territorial entity (Q56061), geographical feature (Q618123) | to count |
| `work` | under creative work (Q17537576) | to count |
| `taxon` | under taxon (Q16521) | to count |
| `property` | a property (P…) | ~12 k |
| `other` | none of the above | the rest |

The roots' labels are checked against the pinned dump's label table when
the map is built (a wrong id shows as a wrong label in the log), and the
map's size per family is logged; the numbers above marked "to count" come
from that pass.

**The pinned map** — every class under a root, with its family, from the
creation dump's P279 closure; a SHARD member (`slicing/families.json`, its
sha256 in the slicing description), built once by `create` from the pack.

**An entity's family** — from its own statements, at the dump being read:
its non-deprecated P31 values looked up in the pinned map, its P1435;
the first family in precedence order. A P31 class NOT in the map (created
after the creation dump) is walked up the CURRENT dump's P279 links to the
nearest class the map has (breadth first, the smallest id at a tie) —
deterministic per dump; such classes are new, so the walk rarely moves an
existing record. No class found: `other`.

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

## 6. Effort and order

1. Parser 2's history section (approved the same day; independent).
2. The family pass in the pack (P279 edges → map; P31/P1435 → each
   entity's family code; ~30 min on the node, read-only for the chunks),
   the map as a member at `create`, the slicer and `slice_hint`, tests —
   about 1.5 days.
3. On the node: the pass, then Monday's sequence on the new target —
   records build about a day later than planned; discovery and the text
   index do not wait.

## 7. What would reopen it

- A newer class tree that matters (a family badly mis-assigned by the
  pinned map): a daughter with a new map.
- `other` growing large over the years (new classes the walk cannot place).
- The family list itself (a family a person wants that is not here: add it
  in the next map, never by editing this one).
