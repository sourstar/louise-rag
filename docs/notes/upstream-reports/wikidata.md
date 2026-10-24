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
| W3 | data | P279 cycles inside the architecture and event trees | measured by `tools/wikidata_coverage.py classes` on 20261012, owed |
| W4 | gap | the most-referenced entities with no label in it, de, fr, es, for the texts of monuments, buildings and events | the same run, owed |
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

## W3. P279 cycles (to be filled from the run)

A class that is, by P279, a subclass of itself — directly or through
others — makes every closure through it ambiguous. The run lists each
cycle inside the closures of architectural structure (Q811979), monument
(Q4989906), archaeological site (Q839954), event (Q1656682) and occurrence
(Q1190554), found with Tarjan's strongly connected components, with the
classes' labels; the edit proposed for each is an editor's call.

## W4. Labels missing where the texts need them (to be filled from the run)

Per language (it, de, fr, es), the entities named most often by the texts
of monuments, buildings and events that have neither a label in that
language nor a `mul` one — the places where a text in that language falls
back to English. Most-referenced first, with their English label and the
count, as a worklist. No translation is proposed with them.
