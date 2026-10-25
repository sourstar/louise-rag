# To the Wikidata team — improvements found while building Louise's wikidata collection

*Standing, operator 2026-10-24: "Any improvement to wikidata goes in a report
to be submitted to the wikidata team for integration." The operator files
it; Louise never edits Wikidata itself. One section per item: what, where,
the evidence, the change proposed. Dump of reference: 20261012
(`wikidata-20261012-all.json.bz2`, md5 `7afa3658634fb291fb5c8672631d3fb9`).*

**What goes here**
- a defect in the data: a malformed value, a P279 cycle;
- a gap the build measures: the entities our texts name most that have no
  label in a language, as a worklist for editors;
- a change to WMDE's code: the issue itself stays in that repository's file
  ([wmde-wikidata-textifier.md](wmde-wikidata-textifier.md),
  [wmde-wikidata-embedding-project.md](wmde-wikidata-embedding-project.md))
  and is listed here.

**The form of a data edit.** QuickStatements rows, one edit a row:
`Q…<TAB>L|D|A<lang><TAB>"value"` for a label, a description or an alias
(Help:QuickStatements). A value not taken from a human source is marked
machine-generated: a suggestion for an editor to review, never a batch to
run as it is. Louise writes no machine translation into its own records
either: a record is reproduced from the dump (records-vs-origin).

## Index

| # | kind | what | status |
|---|---|---|---|
| W1 | code, textifier | Italian connectives for `language_variables.json` | written: [wmde-wikidata-textifier.md](wmde-wikidata-textifier.md) §3 |
| W2 | data | Q22844's English description cut short: `South Korean singer (1984 -` | written below |
| W3 | data | six P279 cycles (18 classes) inside the occurrence tree | written below |
| W4 | gap | the most-referenced entities with no label in it, de, fr, es, for the texts of monuments, buildings and events | written below; the full lists in [../wikidata-coverage-2026-10-24/classes.json](../wikidata-coverage-2026-10-24/classes.json) |
| W5 | data | the occurrence (Q1190554) tree holds clinical trials, doctoral theses, statutory instruments, stars and coins | written below; the paths to trace |
| W8 | data | legislation (Q49371) is a subclass of "Wikidata item that needs to be split" (Q91615393), a maintenance class: every act and statutory instrument is thereby a "Wikimedia internal item" | written below |
| W9 | data | cell line (Q21014462) is a direct subclass of taxon (Q16521): 153,635 cell lines count as taxa | written below |
| W6 | code, textifier | `novalue` written as "has <property>", the opposite; special values' qualifiers dropped | written: [wmde-wikidata-textifier.md](wmde-wikidata-textifier.md) §4 |
| W7 | code, textifier | a lexeme, form or sense value written as a Python dict | written: [wmde-wikidata-textifier.md](wmde-wikidata-textifier.md) §5 |
| — | code, textifier | random alias order; a failed format silently drops the value | [wmde-wikidata-textifier.md](wmde-wikidata-textifier.md) §1–2 |
| — | code, embedding project | stale vectors kept as current; text depending on a live page; dropped entity data; the card's counts | [wmde-wikidata-embedding-project.md](wmde-wikidata-embedding-project.md) |

## W2. Q22844 ("Huh Gak"): an English description cut short

**What:** the English description is `South Korean singer (1984 -`, the
parenthesis never closed (dump 20261012; lastrevid 2550930617, modified
2026-09-29T17:02:53Z). The Korean one is complete, `대한민국의 가수`
("South Korean singer"); there is no `mul` description.

**Proposed edit**, for an editor to check against the item's date of birth
(P569) — the year as the description has it:

```
Q22844	Den	"South Korean singer (born 1984)"
```

## W3. Six P279 cycles

A class that is, by P279, a subclass of itself — directly or through
others — makes every closure through it ambiguous. Found with Tarjan's
strongly connected components inside the closures of built structure
(Q811979), building (Q41176), monument (Q4989906), archaeological site
(Q839954), planned event (Q1656682) and occurrence (Q1190554), dump
20261012 (`tools/wikidata_coverage.py classes`, München, 2026-10-24); all
six sit in the occurrence tree. The edit for each is an editor's call:

| cycle | classes |
|---|---|
| 1 | magic (Q81741) → Satanism (Q133363) → occultism (Q178934) → Liberluxpolism (Q141668045) |
| 2 | cosmology (Q338) → astrophysics (Q37547) → space sciences (Q1195766) → cosmic physics (Q25508843) |
| 3 | emotion (Q9415) → emotional process (Q4531715) → experience (Q5489108) |
| 4 | food (Q2095) → food and beverage (Q118451828) → food material component (Q141446099) |
| 5 | corruption (Q366) ↔ abuse of power (Q1975478) |
| 6 | GNSS jamming (Q115019248) ↔ cross-pol jamming (Q141507841) |

(The order inside a cycle is the component's, not necessarily the edges'.)

## W4. Labels missing where the texts need them

Per language, the entities the texts of the 10.4 M selected entities
(monuments, buildings, events, heritage designations) name most often that
have neither a label in that language nor a `mul` one — where a text in
that language falls back to English. The ten most referenced that matter
to a reader (dump 20261012; the first 200 per language in
classes.json):

| language | entities without a label | their references | most referenced (references) |
|---|---|---|---|
| it | 2,621,573 | 18,104,667 | guard Q798373 (309,894); constituency of the House of Commons Q27971968 (151,768); monument in Fornminnesregistret Q21287602 (145,510); branch post office Q61443690 (129,187); cultural heritage monument in Hesse Q85171284 (105,388); object classified as a historical monument Q61058403 (102,929) |
| de | 2,110,572 | 11,202,133 | branch post office Q61443690 (129,187); UK Statutory Instrument Q7604686 (101,168); object listed as historical monument Q61058419 (45,335) |
| fr | 2,042,847 | 9,830,211 | guard Q798373 (309,894); forward Q10498241 (197,791); archaeological monument of local importance of Ukraine Q130454268 (35,192); Wiki Loves Monuments in Brazil Q105730568 (30,894) |
| es | 2,441,376 | 14,742,513 | guard Q798373 (309,894); forward Q10498241 (197,791); cultural heritage monument in Hesse Q85171284 (105,388); UK Statutory Instrument Q7604686 (101,168) |

A label for one of these fixes every text that names it: "object
classified as a historical monument" alone appears in 102,929 texts
without an Italian label. No translation is proposed with them.

## W5. What the occurrence tree holds

Occurrence (Q1190554, "something that happens") is the root of events.
Its P279 closure holds 442,711 classes and 3,895,782 instances; among its
most instantiated classes are clinical trial (Q30612, 391,649), doctoral
thesis (Q187685, 110,291), UK Statutory Instrument (Q7604686, 101,163),
time of day (Q1260524, 87,914), motion (Q452237, 84,223), individual motion
(Q96739634, 75,431), science project (Q1298668, 71,477), red giant branch
star (Q66619666, 68,896) and coin (Q41207, 57,007) — none of them an event
in the sense a reader means. Each comes in through a chain of P279
statements that someone found true one step at a time. The shortest chain
for each (dump 20261012, `tools/wikidata_coverage.py classes --top 300`,
classes-niche.json):

| class | instances | shortest P279 chain to occurrence |
|---|---|---|
| clinical trial (Q30612) | 391,649 | Q30612 clinical trial → Q7247798 product testing → Q1003030 test → Q1190554 occurrence |
| doctoral thesis (Q187685) | 110,291 | Q187685 doctoral thesis → Q1266946 thesis → Q42240 research → Q3249551 process → Q1190554 occurrence |
| UK Statutory Instrument (Q7604686) | 101,163 | Q7604686 UK Statutory Instrument → Q100532772 statutory instrument → Q2795484 implementing regulation → Q694045 delegated legislation → Q49371 legislation → Q428148 regulation → Q3249551 process → Q1190554 occurrence |
| time of day (Q1260524) | 87,914 | Q1260524 time of day → Q1190554 occurrence |
| motion (Q452237) | 84,223 | Q452237 motion → Q22445448 request → Q11024 communication → Q52948 interaction → Q3249551 process → Q1190554 occurrence |
| science project (Q1298668) | 71,477 | Q1298668 science project → Q170584 project → Q104637332 planned process → Q3249551 process → Q1190554 occurrence |
| red giant branch star (Q66619666) | 68,896 | Q66619666 red giant branch star → Q3929897 red giant branch → Q50089 asymptotic giant branch → Q6472 stellar evolution → Q5418608 evolution → Q1150070 change → Q29017603 control → Q1914636 activity → Q3249551 process → Q1190554 occurrence |
| gas station (Q128302279) | 63,651 | Q128302279 gas station → Q205495 filling station → Q126793 retail → Q601401 trade → Q8187769 economic activity → Q125506646 transfer → Q52948 interaction → Q3249551 process → Q1190554 occurrence |
| coin (Q41207) | 57,007 | Q41207 coin → Q693464 cash → Q1368 money → Q247506 financial instrument → Q93288 contract → Q327197 legal transaction → Q609298 social interaction → Q52948 interaction → Q3249551 process → Q1190554 occurrence |
| obituary (Q309481) | 23,988 | Q309481 obituary → Q21557777 announcement of death → Q567303 announcement → Q1190554 occurrence |

Most run through process (Q3249551), by way of interaction, activity,
legal transaction or change; the link to question is the one that makes a
document, an object or a celestial body a kind of happening.


## W8. Legislation under a maintenance class

`legislation` (Q49371) has the P279 statement → `Wikidata item that needs
to be split` (Q91615393). That class is a maintenance marker, not a kind of
thing, and through it every act, statute and statutory instrument lands under
`Wikimedia internal item`, next to categories and templates (dump 20261012,
`louise dev wikidata-classes`, 2026-10-25):

| class | shortest P279 chain to Wikimedia internal item |
|---|---|
| UK Statutory Instrument (Q7604686), 101,161 items | Q7604686 → Q100532772 statutory instrument → Q2795484 implementing regulation → Q694045 delegated legislation → **Q49371 legislation → Q91615393 Wikidata item that needs to be split** → Q16222597 Wikidata item → Q21281405 Wikidata internal entity → Q17442446 Wikimedia internal item |
| act of parliament (Q1076579), 14,480 items | Q1076579 → Q7243147 primary legislation → **Q49371 → Q91615393** → Q16222597 → Q21281405 → Q17442446 |

The same edge is also legislation's only route to creative work (Q91615393
→ Q16222597 Wikidata item → Q15138389 Wikimedia article page → Q191067
article → Q88392887 part of a work → Q17537576). Removing it leaves
legislation under regulation → process → occurrence (W5). What a reader
means by legislation (a written work, a legal norm) is reached by neither
route. Suggested: drop P279 Q91615393 from Q49371 (an item that needs
splitting is marked by P31 or a template, not P279). Then consider P279 of a
document or legal-norm class. Louise works around it by naming legislation
a root of its `work` family (wikidata-family-slices.md §5).

## W9. Cell lines as taxa

`cell line` (Q21014462) has the P279 statement → `taxon` (Q16521), so
153,635 cell lines (Cellosaurus imports) are taxa to anyone who reads the
class tree. A cell line is a population of cells of one organism, not a
taxonomic group. Its organism is already stated per item (P703, found in
taxon). Suggested: P279 → `biological material` or `cell culture`, not
taxon.
