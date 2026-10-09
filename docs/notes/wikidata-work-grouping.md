# Can Wikidata group gutenberg's books into works?

Measured 2026-10-09 (Wikidata fetched 15:41 UTC). Read-only: the node's
manifest was read with `sqlite3 -readonly ?mode=ro`; nothing was written
there. Script: `tools/measure/wikidata_grouping.py` (`fetch`, `analyze`);
raw data and tables in `tools/measure/out/` (not committed).

**Answer: no, not today.** Wikidata knows 5.1% of the collection's books
and groups 0.4% of them into works; 0.1% sit in a cross-language group.
None of the Latin or Greek books does. Where Wikidata does state a group
it is right (30/30 hand-checked) and states the original language (38/38
cross-language groups) — the quality is there, the coverage is not.

## Method

- **Books:** `select native_id, lang from documents where in_scope=1` on
  the node's gutenberg manifest — 79,600 books; `native_id` is the PG
  ebook id, `lang` the manifest's language.
- **Wikidata:** the public SPARQL endpoint, one query paged by
  `ORDER BY ?item ?pg LIMIT 2000 OFFSET n` over every `p:P2034` statement
  (all ranks), with per item: P31, P407 (language), P50, P1476, P629
  (edition or translation of) and the inverse P747 (a work stating "has
  edition" → the item). A second query reads each work found: P31, P407,
  P364, P50 and labels. The query text is in the script and in
  `out/wikidata_p2034_raw.json`.
- **Only stated relations** (docs/05 §5.5): nothing is matched by title or
  author. Three groupings:
  - *strict* — the work is the item's P629 target;
  - *P629 or P747* — adds works that state P747 → the item;
  - *broad* — as above, and an item with neither is its own work (an
    item that is the literary work itself, often carrying several PG ids,
    or the target other PG books' P629 point to).
- A group = the collection books sharing a work; cross-language = its
  books carry ≥2 distinct manifest languages.

Wikidata holds **4,196 P2034 statements** (4,117 normal, 4 preferred, 75
deprecated; deprecated ones are ignored) on 4,010 items, **4,068 distinct
PG ids**; 4,063 of them are in the collection. Cross-check: the Wikidata
search API's `haswbstatement:P2034` returns 4,071 hits. P629 is stated on
554 of these items.

## (a)(b) Coverage by language

| lang | books | (a) on Wikidata | (b) P629 stated |
|---|---:|---:|---:|
| **all** | 79,600 | 4,063 (5.1%) | 547 (0.7%) |
| en | 63,131 | 3,432 (5.4%) | 427 (0.7%) |
| fr | 4,219 | 159 (3.8%) | 14 (0.3%) |
| fi | 3,690 | 65 (1.8%) | 14 (0.4%) |
| de | 2,469 | 121 (4.9%) | 21 (0.9%) |
| nl | 1,113 | 26 (2.3%) | 3 (0.3%) |
| it | 1,111 | 48 (4.3%) | 6 (0.5%) |
| es | 899 | 87 (9.7%) | 41 (4.6%) |
| hu | 654 | 11 (1.7%) | 0 |
| pt | 650 | 9 (1.4%) | 1 (0.2%) |
| zh | 436 | 14 (3.2%) | 2 (0.5%) |
| sv | 253 | 7 (2.8%) | 2 (0.8%) |
| el | 216 | 1 (0.5%) | 0 |
| eo | 124 | 11 (8.9%) | 2 (1.6%) |
| la | 108 | 3 (2.8%) | 2 (1.9%) |
| ca | 97 | 1 (1.0%) | 0 |
| da | 85 | 56 (65.9%) | 9 (10.6%) |
| tl | 57 | 1 (1.8%) | 1 (1.8%) |
| (none) | 33 | 0 | 0 |
| pl | 30 | 0 | 0 |
| no | 23 | 2 (8.7%) | 1 (4.3%) |
| enm | 3 | 0 | 0 |
| grc, ang | 0 | — | — |

The manifest has no `grc` or `ang` book: PG tags ancient Greek as `el`
(the 216 `el` books) and Old English is absent. Adding P747 changes (b) by
one book.

## (c)(d) Works that group ≥2 collection books

| grouping | works ≥2 | books in them | cross-language works | their books | sizes (≥2) | original language stated |
|---|---:|---:|---:|---:|---|---:|
| strict P629 | 47 | 119 (0.15%) | 21 | 45 | 2:33 3:10 4:2 6:1 9:1 | 45/47; cross 21/21 |
| P629 or P747 | 47 | 119 | 21 | 45 | same | same |
| broad | 123 | 288 (0.36%) | 38 | 80 | 2:97 3:19 4:3 5:2 6:1 9:1 | 118/123; cross 38/38 |

Cross-language groups (broad) are almost all English + one other:
en+fr 13, en+de 5, en+es 5, en+fi 4, en+it 2, en+no 2, and one each of
da, nl, pt, cs, de+es, en+eo+fi, en+es+tl. Cross-language sizes: 34 of
2 books, 4 of 3. The original language is given by the work's P407 (or
P364); a few name a variety without an ISO code (British English for
*On Liberty*, Homeric Greek for the Iliad and Odyssey, Classical Latin
for *De rerum natura*) — right, but not a manifest language code
without a mapping. Every group: `out/wikidata_crosslang_all.csv`.

## (e) Latin and Greek

| | books | on Wikidata | in a group ≥2 | in a work with an English collection book |
|---|---:|---:|---:|---:|
| la | 108 | 3 | 0 | 0 |
| el | 216 | 1 | 0 | 0 |

Wikidata cannot today tell us which classical books already have a free
English translation in the collection. The Iliad and Odyssey groups exist
(en + es) but the Greek originals are not linked to them.

## (f) Quality

**Hand check:** 30 random cross-language groups (seed 20261009) of the
38, compared by the manifest's title and authors
(`out/wikidata_crosslang_sample.csv`): **30/30 are the same work.**
Notes: *The Moon-Voyage* (12901) is an omnibus that contains *From the
Earth to the Moon*; *Anna Karenina* de is two volumes (44956, 44957) on
one edition item; 71356 is a French text tagged `en` in the manifest
(PG's metadata, not Wikidata's).

**Duplicates:**
- 47 PG ids sit on ≥2 items (all 47 in the collection) — e.g. 1661 on
  three items, 11199 and 2305 on three. They are a work and its edition
  both carrying the id, or a collection and its component works; one
  (55849, Zola's *Truth*) on the work and the edition.
- 4 items state ≥2 P629 (anthology editions).
- 77 items carry ≥2 PG ids (max 9) — a work item holding several
  translations' or volumes' ids, which is how most of the broad groups
  arise.
- The items are mostly works, not editions: P31 literary work 2,495,
  version/edition 904, then 268 / 134 / 122 smaller classes.

## What is left ungrouped

75,537 books (94.9%) have no Wikidata item at all. Of the 4,063 that do,
3,775 sit alone under their work — mostly literary-work items whose
other editions on PG carry no P2034. Grouping the collection into works
through Wikidata would first need the P2034 statements Wikidata lacks
(≈75k), contributed upstream; nothing in the data we hold says which
book is a translation of which, other than the free-text "(translator)"
in the manifest's authors, which is not a stated relation.
