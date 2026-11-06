# Inconsistencies in the origin corpora

*Operator, 2026-10-25: "Add any inconsistency found in the corpora in a
report. Make the parsers such that on a regular basis any inconsistency is
logged and flagged." The second half is 06 §6.1's issues (amended the same
day; `app/issues.py`): every parse reports what it meets, the build and the
node's metadata stage record it per document, and `louise hold issues list
<collection>` lists it. This note is the first half.*

What the SOURCES give us that contradicts itself or the source's own rules —
not our bugs. Current state only. A count is given only where it was
counted, with where; otherwise "not counted". **Flag** is the issue code a
parse records today (06 §6.1), "—" where nothing flags it per document.
**Upstream**: whether it was reported to the source.

## The dateless records (operator's choice A)

*"A record a dated snapshot cannot place is a defect to fix, never a silent
exclusion"* (STATUS, 2026-10-25). Flag: `cutoff-undated`, the platform's,
checked on every record of every `hold records build` of a plugin with a
per-record cutoff (gutenberg, pubmed), with or without a cutoff.

- **How many exist: not counted.** The node's build databases hold 0 skips
  of reason `cutoff: no cutoff date` (gutenberg, gutenberg-p9-retired,
  normattiva, pubmed, pubmed-v6-retired, wikipedia-v8; München,
  2026-10-25), but that 0 says nothing: `hold records build` passes no
  cutoff to the builder (`app/hold/records.py` → `pack/build.build` without
  `until`), so no build has ever applied the exclusion. The first build
  with the flag counts them, cutoff or not.
- PubMed: 0 of 120,000 citations sampled had neither DateRevised nor
  DateCompleted (baseline files pubmed26n0001, 0500, 1000, 1270; München,
  2026-10-25). A pubmed record is undated only when it also has no
  publication date (below).

## Project Gutenberg

| inconsistency | count | Louise today | flag | upstream |
|---|---|---|---|---|
| A book directory without its catalogue entry `pg<N>.rdf` | **73 of 79,753 book directories** (census of the mirror, read-only, München, 2026-10-25 16:45, `/root/rdf_census.py` → `/root/rdf-census.json`). Among them: 186, 195, 1072, 1767, 1789, 5613, 9121, 9126, 9135, 9142. *Correction (2026-10-27): book 10, cited here before as an example, has its `pg10.rdf`.* Every `pg<N>.rdf` present parses and carries `dcterms:issued`: 0 empty, 0 unparseable, 0 without a date | the record is parsed from the text with no title, authors, subjects, language (`und`) or release date; nothing is invented | `rdf-missing` (+ `cutoff-undated`) | no |
| A `pg<N>.rdf` that is empty or not well-formed XML | not counted | as a missing entry | `rdf-unreadable` | no |
| A `pg<N>.rdf` with no `pgterms:ebook` element | not counted | as a missing entry | `rdf-no-ebook` | no |
| A catalogue entry with no `dcterms:issued` (release date) | not counted (see above) | the record has no `extra.issued`; a dated snapshot excludes it and the build says so | `cutoff-undated` | no |
| A ZERO-BYTE `pg<N>-images.epub` beside a good plain epub, dated 2009 | 9 books in the node's mirror (2026-09-12) | the reading rule takes the first form that can be read (`source.pick_form`) | `epub-unreadable` | no |
| An epub whose text references an image file the epub does not contain | not counted | the reference is dropped from the text and the figures together, so `figures[N]` stays the Nth marker | `epub-image-missing` | no |
| An epub whose spine names a document the zip lacks or the manifest does not list | not counted | that document's text is not read | `epub-document-missing` | no |
| A book the catalogue itself declares broken: "This book's encoding is broken, and will not be fixed." | 1: gutenberg:673, the build's only skip (München, 2026-10-25) | not indexable (`UnindexableDocument`), the origin's sentence quoted in its error; readmitted when the entry drops the note | — (an error on the document, 06 §6.1) | no (the origin states it) |
| MARC subfield delimiters (`$b`) inside `dcterms:title` | 4,648 of 79,286 titles, 5.86 % (node, 2026-09-12) | `title` cleaned, the origin's form kept in `extra.title_raw` | — | no |
| A catalogue entry revised in place without its text (pg23054.rdf: a translator's dates) | 17 of 4,055 sampled records changed at the origin (records-vs-origin, 2026-10-11) | `origin_hash` covers the entry (parser 9); the check reports "changed at the origin", the next build re-issues the record | — | n/a (the origin's right) |
| Book folders regenerated with different bytes and the same content | 84 of 87 re-parsed records "moved" (Perugia's records-vs-origin over the network, 2026-10-23) | counted as reproduced (03 §3.2); the mirror keeps no history, so a moved record can never be re-checked against the bytes it was built from | — | n/a |
| `<N>.delete` directories in the mirror | 17, beside 79,770 numeric book directories (München, 2026-10-25) | not read by discovery (not a book number) | — | no |

## PubMed / MEDLINE (NLM)

| inconsistency | count | Louise today | flag | upstream |
|---|---|---|---|---|
| A citation with no publication date of any kind (no PubDate year, no MedlineDate year, no ArticleDate) | not counted | `date` null; the record is kept and sliced `year=unknown` | `no-publication-date` (+ `cutoff-undated` without DateRevised/DateCompleted) | no |
| A PubDate given only as free-text `MedlineDate` ("1953 Nov-Dec", "1998 Spring") | not counted | `date` is its year; the string is kept in `extra.pub_date_raw`. Allowed by NLM's DTD, so not flagged | — | n/a |
| `<ArticleTitle/>` empty and `<VernacularTitle>` full | 771,527 documents, 1.9 % (parser 7's note in `medline.py`; where it was counted is not recorded) | the title is the vernacular one; both kept in `extra` | — | no |
| Neither ArticleTitle nor VernacularTitle has text | not counted | the record's title is empty | `no-title` | no |
| The only abstract is an `OtherAbstract` (POPLINE's "PIP") | 63,921 records cut to their title beside an other abstract, of 39,994,988 (pubmed-v8's snapshot, `tools/pubmed_other_abstract_rows.py`, 2026-10-19) | kept in `extra.other_abstracts`, not in `sections`: found by title only. Operator's decision open ([pubmed-other-abstracts.md](pubmed-other-abstracts.md)) | `other-abstract-only` | no |
| NCBI's MedCPT embeddings archive embedded a text other than the citation's: revisions since NCBI's run, Unicode punctuation folded, HTML entities (`&amp;nbsp;`) | 142,540 texts differ (0.4 %) over 39,993,653 rows, 60,947 of them archive rows; plus the 54,548 POPLINE rows (2026-10-19, `tools/medcpt_text_compare.py`) | those rows dropped and re-embedded from our records (`dev drop-rows`) | — | no |
| An ELocationID or author marked `ValidYN="N"` (an identifier or name NLM knows to be wrong) | not counted | kept with its flag (`extra.elocation_ids[].valid`, `extra.authors[].valid`) | — | n/a (NLM's own mark) |

## Wikipedia (Wikimedia dumps)

| inconsistency | count | Louise today | flag | upstream |
|---|---|---|---|---|
| Malformed wikitext: an unclosed `<ref>`, table markup the parser cannot read, a mis-nested image link | 4, 11 and 1 page(s) in a sample of 2,914 (1,549 enwiki, 1,365 simplewiki; 2026-09-14) | the leftover markup stays in the prose; the page's wikitext is kept whole in `extra.wikitext` | — | no |

Nothing else found in the dumps themselves. The Upstash archive (layer 1)
matched the Hub's LFS hashes on all 1,447 files (2026-09-14) and is a 2024
snapshot by design, not a contradiction.

## Wikidata (dump 20261012)

All written for the Wikidata team in
[upstream-reports/wikidata.md](upstream-reports/wikidata.md); **none filed
yet** (the operator files them).

| inconsistency | count | Louise today | flag |
|---|---|---|---|
| W2: Q22844's English description cut short, `South Korean singer (1984 -` | 1 | kept as the dump says | — |
| W3: P279 cycles (a class its own subclass) | 6 cycles, 18 classes, in the occurrence tree | the family map takes the nearest root; no closure loops | — |
| W5: occurrence (Q1190554) holding clinical trials, theses, statutory instruments, stars, coins | 442,711 classes, 3,895,782 instances in its closure | families chosen on the dump's own P279 paths ([wikidata-family-slices.md](wikidata-family-slices.md)) | — |
| W8: legislation (Q49371) a subclass of a maintenance class, so every act is a "Wikimedia internal item" | 101,161 UK Statutory Instruments, 14,480 acts of parliament on that path | legislation is a root of the `work` family | — |
| W9: cell line (Q21014462) a direct subclass of taxon | 153,635 cell lines | cell line is a root of the molecular family | — |
| References to entities merged into another or deleted (the JSON dump carries no redirects) | 862 references to 331 entities in the 193 M lookups of the selection's texts | written as ids in the text | — |
| W4: entities named often with no label in it/de/fr/es (a gap, not a defect) | it 2,621,573; de 2,110,572; fr 2,042,847; es 2,441,376 | the text falls back to English | — |

## Normattiva (Akoma Ntoso exports)

| inconsistency | count | Louise today | flag | upstream |
|---|---|---|---|---|
| A consolidated version whose `FRBRExpression` date is not its version date (Statuto Sardegna V11: `ita@1993-10-10` for `VIGENZA_2001-02-01`, the ELI alias and member name agreeing on 2001-02-01) | 1 seen (multivigenza review §1); not counted | the version is read from the member name and the ELI alias | `frbr-expression-stale` | no |
| An ORIGINALE whose Expression date precedes its publication (1989-02-16 for 1989-03-16) | 1 of 220 sampled originals (review §7, 2026-10-12) | validity starts at the entry-into-force note, else the publication date | `expression-before-publication` | no |
| An "Entrata in vigore" note dated in the wrong century (1939 for 1989) | 1 of the 154 sampled notes | the Expression date is taken; the note is kept in `entry_into_force` | — | no |
| One Expression in two zips (4,142 acts in two M collections; 90,118 repealed originals also in an M collection), the copies possibly differing in metadata | counted from the zips' directories (review §1, 2026-10-12) | one copy chosen deterministically (M, V, O, then path); `origin` names it | — | n/a |
| The listing's `dataCreazione` is the same day on nearly every row (the zips are rebuilt daily) | 54 rows (2026-10-12) | size is the change signal, not the date | — | no |
| The JSON export's `dataVigoreVersione` empty | every sampled element | AKN is the form read, not JSON | — | no |
| The texts are not official: the printed Gazzetta Ufficiale prevails (the origin's own notice) | — | stated in the credit line and TERMS.txt | — | n/a |

## What flags nothing yet

- Discovery's container reports (a PubMed container that will not
  enumerate, a Normattiva member name outside the pattern, a missing
  multistream index) are logged per scan and counted in discovery's stats,
  not recorded per document: most are transfer states, not the origin's.
- A `<PubmedArticle>` without a PMID is skipped at discovery without a
  word (`pubmed/source.py`): not counted.
- The embedded (wikivec), wikipedia, wikidata and deposit plugins declare
  no issue codes.
