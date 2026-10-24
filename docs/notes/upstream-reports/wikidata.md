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
statements that someone found true one step at a time; the chains are to
be traced (the shortest path from each class to Q1190554) before the
report goes, so the editors see which link to question.
