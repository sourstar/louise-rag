# What each origin says, and what we keep — the promise 9 audit

Run 2026-09-12, the day promise 9 landed (docs/01), across all five
corpus plugins. Method: enumerate every field the origin asserts about
ONE entity, then justify each drop against promise 9's four categories
(wrapping, boilerplate, our corpus-add date, provider packaging). Every
claimed drop got an adversarial second read; six were overturned.

| plugin | keeps today | origin fields | must-keep | unsure |
|---|---|---|---|---|
| pubmed | 16 | 103 | **72** | 8 |
| gutenberg | 12 | 62 | 18 | 6 |
| wikipedia | 8 | 56 | 23 | 13 |
| embedded | 11 | 20 | 4 | 1 |
| genome | 12 | 29 | 10 | 2 |

## Verified by hand, not taken on trust

The audit was run by subagents; these are the findings I then confirmed
against the code and the corpus myself, because a field list is cheap to
assert and expensive to act on.

**pubmed strips the MeSH qualifier while declaring a tree facet over it.**
`medline.py:165` takes `MeshHeadingList/MeshHeading/DescriptorName` only,
so "Neoplasms/genetics" becomes "Neoplasms" — and `source.py:101-104`
declares `subjects` as `{"kind": "tree", "separator": "/", "authority":
{"name": "MeSH"}}`, which the live collection's `codex.json` carries. So
the collection declares a hierarchical facet over a field whose
separator its own parser removes: the facet can never have a single tree
node. Both halves are internally consistent, and all eight checks pass.
That is promise 8's artefact exactly.

**gutenberg flattens two subject vocabularies into one list.** Each
`dcterms:subject` carries a `dcam:memberOf` naming its scheme; a
ten-book sample of this corpus uses LCSH (65) and LCC (12). `rdf.py`
takes `.//rdf:value` from all of them and joins with `" | "`, so
"Science fiction" (an LCSH heading) and "PZ" (an LCC class) become
indistinguishable members of one field — which is also why an
`authority` on a facet over it cannot be honest.

**gutenberg truncates silently.** `[:500]` on title and authors,
`[:2000]` on subjects, `[:1000]` on bookshelves. Measured on the live
manifest: **25 titles and 7 author lists sit at the 500-char cap**, so
their tails are gone. Subjects and bookshelves never reach theirs.

**`PubmedBookArticle` is skipped without even an error ref** —
`discover()` (source.py:133-147) dispatches on `PubmedArticle` /
`DeleteCitation` with no `else`. BUT: zero of 300,000 sampled records
carry that tag, because Bookshelf citations ship in a separate
distribution and not in `pubmed/baseline`. A latent defect worth turning
into a refusal, not an active loss. (I nearly reported it as thousands
of missing citations.)

**Three claimed gutenberg fields are not in this corpus.**
`dcterms:tableOfContents`, `marcrel:trl` and `marcrel:edt` are absent
from the sample; `marcrel:ill` and `dcterms:alternative` do occur. The
audit listed what the format permits, which is not the same as what the
origin sends — the same trap `PubmedBookArticle` set.

## Scale, and what it means for the v1.0 roots

127 must-keep fields over five plugins. This is not a parser tweak:

- **pubmed** is the largest by count (72) but mostly mechanical — the
  fields are all in the XML already and `medline.parse_article` reads
  seven paths where it should read dozens.
- **wikipedia is the hard one.** Its must-keep list includes infobox
  parameters, `<ref>` citation content, `{{Authority control}}`
  identifiers and inline data templates (`{{convert|5|km}}` renders a
  value that is content, while the braces are markup). That is a
  wikitext template parser, not a field addition.
- **genome** has one finding worth its own line: residue CASE carries
  meaning (lowercase = repeat-masked), so upper-casing a sequence loses
  it.
- **embedded** is nearly clean — 4 must-keep of 20.

`content_hash` is `[title, sections]` alone, so every field routed to
`extra`, `keys` or `subjects` moves no chunk id and no vector. ONE
proposal would re-embed: pubmed's `OtherAbstract` into `sections`. That
is the single decision with a vector cost attached; everything else is
records-only.

## Open for the operator

- the 30 "unsure" fields listed per plugin in the appendix
- pubmed's `OtherAbstract`: `sections` (re-embeds) or `extra` (does not)
- whether `PubMedPubDate@PubStatus` dates — when the record entered
  *PubMed's* index, not ours — fall under promise 9(c). Keeping the
  `History` block verbatim resolves it safely and costs almost nothing,
  which is the recommendation.

## Appendix — the field lists

### pubmed — keeps 16 of 103 enumerated origin fields

pubmed parser 5 publishes 9 of the ~95 things a MEDLINE `<PubmedArticle>` asserts about one article. Everything else is dropped, and almost none of it qualifies under the operator's four categories.


| origin field | what it holds | where it goes |
|---|---|---|
| `MedlineCitation/@Status` | the citation's indexing state: "MEDLINE" \| "In-Process" \| "Publisher" \| "PubMed-not-MEDLINE" \| "In-Data-Re | extra.status |
| `MedlineCitation/@VersionID` | the citation version number for versioned records (preprints, updated citations), e.g. "2" | extra.version_id |
| `MedlineCitation/@VersionDate` | the date that version was issued, e.g. "2019-03-12" | extra.version_date |
| `MedlineCitation/@IndexingMethod` | "Automated" or "Curated" — how the MeSH headings on this record were assigned | extra.indexing_method |
| `MedlineCitation/PMID/@Version` | which version of this PMID the element carries; CDATA #REQUIRED, so 100%. e.g. "1" | extra.pmid_version |
| `MedlineCitation/DateCompleted` | Year/Month/Day MEDLINE finished indexing the record; 46.5% of articles | extra.date_completed, as its own key. medline.py:186-195 loo |
| `MedlineCitation/Article/@PubModel` | Print \| Print-Electronic \| Electronic \| Electronic-Print \| Electronic-eCollection. #REQUIRED, so 100% | extra.pub_model |
| `Article/Journal/ISSN (+@IssnType Electronic\|Print)` | the journal's ISSN, e.g. "0022-1767" with IssnType="Print"; 100% of articles | extra.issn (and extra.issn_type) |
| `Article/Journal/JournalIssue/Volume` | the journal volume, e.g. "147" | extra.volume |
| `Article/Journal/JournalIssue/Issue` | the issue number, e.g. "3" | extra.issue |
| `Article/Journal/JournalIssue/PubDate/Season` | the season of publication in place of a month, e.g. "Spring", "Winter" | extra.pub_date_raw (the PubDate's own text, verbatim). `_ymd |
| `Article/Journal/JournalIssue/PubDate/MedlineDate` | a free-text date where the structured one does not fit, e.g. "1953 Nov-Dec", "1998 Spring", "1976-1977" | extra.pub_date_raw |
| `Article/Journal/Title` | the journal's full title, e.g. "Journal of immunology (Baltimore, Md. : 1950)"; 100% of articles | extra.journal_title |
| `ArticleTitle/AbstractText: mml:math and DispFormula` | MathML formulas inline in the title or abstract, e.g. a fraction or a summation | the formula itself belongs in `sections` in a form that surv |
| `Article/Pagination/StartPage` | first page, e.g. "1401" | extra.pagination (an object, or the MedlinePgn string) |
| `Article/Pagination/EndPage` | last page, e.g. "1409" | extra.pagination |
| `Article/Pagination/MedlinePgn` | the page range as MEDLINE writes it, e.g. "1401-9"; 95.5% of articles | extra.pages |
| `Article/ELocationID (+@EIdType doi\|pii, @ValidYN)` | the article's DOI or publisher item id, e.g. EIdType="doi" → "10.4049/jimmunol.1401123"; 99.1% of articles | `keys` as `doi:10.4049/…` (its natural §9.5 home |
| `Article/Abstract/CopyrightInformation` | the abstract's copyright line, e.g. "Copyright © 2023 Elsevier Inc. All rights reserved."; 65.0% of articles | extra.copyright. The (B) reading |
| `Article/AuthorList/@CompleteYN` | Y \| N (default Y) — N means the author list is truncated | extra.authors_complete |
| `Article/AuthorList/@Type` | authors \| editors — whether the listed people wrote the work or edited it | extra.author_list_type |
| `Article/AuthorList/Author/ForeName` | the given name(s) in full, e.g. "Marco Antonio" | extra.authors[].fore_name. medline.py:72-75 reads LastName + |
| `Article/AuthorList/Author/Suffix` | name suffix, e.g. "Jr", "3rd" | extra.authors[].suffix (and append it in the `creators` disp |
| `Article/AuthorList/Author/Identifier (+@Source, in practice "ORCID")` | the author's persistent identifier, e.g. Source="ORCID" → "0000-0002-1825-0097" | extra.authors[].orcid (and `keys` as `orcid:0000-0002-1825-0 |
| `Article/AuthorList/Author/AffiliationInfo/Affiliation` | the author's institution as free text, e.g. "Department of Immunology, University of Bologna, Italy. marco.ros | extra.authors[].affiliations. THE PROMPT ASKED DIRECTLY: dro |
| `Article/AuthorList/Author/AffiliationInfo/Identifier` | the institution's id, e.g. Source="GRID"/"ISNI"/"RINGGOLD" → "grid.6292.f" | extra.authors[].affiliation_ids |
| `Article/AuthorList/Author/@ValidYN` | Y \| N (default Y) — N marks a name NLM knows to be wrong | extra.authors[].valid |
| `Article/AuthorList/Author/@EqualContrib` | Y \| N — marks co-first/equal-contribution authors | extra.authors[].equal_contrib |
| `Article/Language (the second and later elements)` | additional languages the article is published in; 0.37% of this corpus carries more than one (medline.py:50-53 | extra.languages (the full list, in the origin's order). medl |
| `Article/DataBankList/DataBank/DataBankName` | the external database an accession belongs to, e.g. "ClinicalTrials.gov", "GENBANK", "figshare" | extra.databanks[].name |
| `Article/DataBankList/DataBank/AccessionNumberList/AccessionNumber` | the accession, e.g. "NCT00000125", "AF123456" | extra.databanks[].accessions, and `keys` as `nct:NCT00000125 |
| `Article/DataBankList/@CompleteYN` | Y \| N — whether the databank list is complete | extra.databanks_complete |
| `Article/GrantList/Grant/GrantID` | the grant number, e.g. "R01 CA123456" | extra.grants[].id |
| `Article/GrantList/Grant/Acronym` | the funding institute acronym, e.g. "CA" | extra.grants[].acronym |
| `Article/GrantList/Grant/Agency` | the funder, e.g. "NCI NIH HHS" | extra.grants[].agency |
| `Article/GrantList/Grant/Country` | the funder's country, e.g. "United States" | extra.grants[].country |
| `Article/GrantList/@CompleteYN` | Y \| N — whether the grant list is complete | extra.grants_complete |
| `Article/PublicationTypeList/PublicationType (+@UI)` | the article's type, one or more; #REQUIRED so 100% of articles. e.g. "Journal Article" (UI D016428), "Review", | extra.publication_types (with the @UI values, so the MeSH-au |
| `Article/VernacularTitle` | the article's title in its original language and script, e.g. "Wirkung der Kalibrierung auf Übertragungsraten" | extra.title_vernacular (not `title`, which §9.5 makes one st |
| `Article/ArticleDate (Year, Month, Day) + @DateType="Electronic"` | the electronic publication date, e.g. 2023-11-19; 80.8% of articles | extra.date_electronic. On the 80.8% that carry it, PubDate a |
| `MedlineCitation/MedlineJournalInfo/Country` | the journal's country of publication, e.g. "United States"; 100% of articles | extra.journal_country |
| `MedlineCitation/MedlineJournalInfo/MedlineTA` | the journal's MEDLINE title abbreviation, e.g. "J Immunol"; #REQUIRED, so 100% | extra.journal_ta |
| `MedlineCitation/MedlineJournalInfo/NlmUniqueID` | NLM's canonical journal id, e.g. "2985117R"; 100% of articles | extra.nlm_unique_id |
| `MedlineCitation/MedlineJournalInfo/ISSNLinking` | the linking ISSN that ties print and electronic versions, e.g. "0022-1767"; 100% | extra.issn_linking |
| `MedlineCitation/ChemicalList/Chemical/RegistryNumber` | CAS registry or EC number for a substance discussed, e.g. "50-81-7", "EC 3.4.21.-", or "0" when none | extra.chemicals[].registry_number |
| `MedlineCitation/ChemicalList/Chemical/NameOfSubstance (+@UI)` | the substance name and its MeSH id, e.g. "Ascorbic Acid" (UI D001205) | extra.chemicals[].name / .ui. Keep it out of `subjects`: `de |
| `MedlineCitation/SupplMeshList/SupplMeshName (+@Type Disease\|Protocol\|Organism\|Anatomy` | supplementary concept records — rare diseases, chemo protocols, organisms not in the main MeSH tree, e.g. "Fan | extra.suppl_mesh |
| `MedlineCitation/CommentsCorrectionsList/CommentsCorrections/@RefType` | the relation to another article: RetractionIn \| RetractionOf \| ErratumIn \| ErratumFor \| ExpressionOfConcer | extra.comments_corrections[].ref_type. SECOND-WORST DROP, an |
| `MedlineCitation/CommentsCorrectionsList/CommentsCorrections/RefSource` | the cited article as free-text citation, e.g. "JAMA. 2024 Mar 5;331(9):789" | extra.comments_corrections[].source |
| `MedlineCitation/CommentsCorrectionsList/CommentsCorrections/PMID` | the PMID of the related article, when PubMed has it | extra.comments_corrections[].pmid |
| `MedlineCitation/CommentsCorrectionsList/CommentsCorrections/Note` | a free-text note on the relation | extra.comments_corrections[].note |
| `MedlineCitation/GeneSymbolList/GeneSymbol` | gene symbols the article is about, e.g. "BRCA1", "TP53" (chiefly on pre-1996 records) | extra.gene_symbols, and a candidate for `keys` as `hgnc:…` o |
| `MedlineCitation/MeshHeadingList/MeshHeading/DescriptorName/@UI` | the MeSH descriptor's canonical id, e.g. "D009369" | extra.mesh[].ui, and `keys` as `mesh:D009369`. medline.py:16 |
| `MedlineCitation/MeshHeadingList/MeshHeading/DescriptorName/@MajorTopicYN` | Y \| N (default N) — whether this heading is a MAJOR topic of the article (starred in MEDLINE) | extra.mesh[].major |
| `MedlineCitation/MeshHeadingList/MeshHeading/DescriptorName/@Type` | "Geographic" on place headings, e.g. "Italy" | extra.mesh[].type |
| `MedlineCitation/MeshHeadingList/MeshHeading/QualifierName (+@UI, +@MajorTopicYN)` | the MeSH subheading that narrows the descriptor, e.g. "genetics" (UI Q000235) under "Neoplasms" → the indexed  | `subjects`, joined to its descriptor as "Neoplasms/genetics" |
| `MedlineCitation/PersonalNameSubjectList/PersonalNameSubject (LastName, ForeName, Initial` | the person the article is ABOUT — biographies, obituaries, historical pieces, e.g. "Fleming, Alexander" | extra.personal_name_subjects |
| `MedlineCitation/OtherID (+@Source NASA\|KIE\|PIP\|POP\|ARPL\|CPC\|IND\|CPFH\|CLML\|NRCBL` | an id assigned by a cooperating body, e.g. Source="NLM" → "PMC1234567" on older records | extra.other_ids |
| `MedlineCitation/OtherAbstract (+@Type, +@Language) with its AbstractText+ and CopyrightI` | a second abstract: the author's own in the original language, a publisher's version, or a plain-language summa | `sections`, as its own (heading, text) pair |
| `MedlineCitation/KeywordList/@Owner` | who supplied the keywords: NLM \| NLM-AUTO \| NASA \| PIP \| KIE \| NOTNLM \| HHS. "NOTNLM" means the authors' | extra.keywords[].owner (or a parallel extra.keyword_owners) |
| `MedlineCitation/KeywordList/Keyword (+@MajorTopicYN)` | the authors' own keywords, e.g. "CRISPR", "single-cell RNA-seq"; KeywordList on 65.4% of articles | extra.keywords |
| `MedlineCitation/CoiStatement` | the conflict-of-interest declaration, e.g. "The authors declare no competing interests."; 46.9% of articles | extra.coi_statement. Not `sections`: it is not about the sub |
| `MedlineCitation/SpaceFlightMission` | the mission a space-life-sciences article belongs to, e.g. "STS-90" | extra.space_flight_missions |
| `MedlineCitation/GeneralNote (+@Owner)` | a free-text note the indexing unit attached to the citation, e.g. "Part of a symposium issue" | extra.general_notes. The (D) reading |
| `PubmedData/History/PubMedPubDate @PubStatus in {received, accepted, revised, aheadofprin` | the article's own lifecycle dates to the second, e.g. received 2023-06-14, accepted 2023-10-02, epublish 2023- | extra.history (the PubStatus → date map, verbatim). These ar |
| `PubmedData/PublicationStatus` | how the article was published: "ppublish" \| "epublish" \| "aheadofprint" \| "epublish-ppublish"; #REQUIRED, s | extra.publication_status |
| `PubmedData/ArticleIdList/ArticleId (+@IdType doi\|pii\|pmc\|pmcid\|mid\|pubmed\|medline\` | every id PubMed holds for the article, e.g. IdType="doi" → "10.1038/s41586-023-06789-9", IdType="pmc" → "PMC10 | `keys` as `doi:…` and `pmc:PMC10662169` (canonical `<namespa |
| `PubmedData/ObjectList/Object (@Type, Param@Name)` | structured objects PubMed attaches to the article — trial registrations, gene and molecule references, e.g. Ty | extra.objects |
| `PubmedData/ReferenceList/Reference/Citation` | one bibliography entry as free text, e.g. "Smith J, et al. Widget fatigue. Nature. 2019;570:112-7."; Reference | extra.references[].citation. What an article cites is an ass |
| `PubmedData/ReferenceList/Reference/ArticleIdList/ArticleId` | the resolved ids of a cited work, e.g. IdType="pubmed" → "31178123" | extra.references[].ids |
| `PubmedData/ReferenceList/ReferenceList (nested)` | sub-grouped bibliographies, e.g. a chapter's own list inside an article's | extra.references, keeping the grouping (the rule names "its  |
| `PubmedBookArticle (a sibling ENTITY, not a field) — BookDocument/Book/Sections/ItemList/` | NCBI Bookshelf citations — GeneReviews, StatPearls, NBK chapters — which the DTD admits as children of PubmedA | a decision, not a key: either parse PubmedBookArticle into t |

**Unsure (8) — for the operator:**

- `MedlineCitation/@Owner` — Two readings. (D) provider wrapping: it names which of NLM's cooperating units keyed the record, which is the provider's own production bookkeeping. Entity information: it is the provenance of every descriptive field on 
- `Article/Journal/JournalIssue/@CitedMedium` — Two readings. (D) provider wrapping: it records which physical copy NLM's indexer had in hand, i.e. the provider's own citation practice, not a property of the article. Entity information: together with @IssnType it says
- `Article/Abstract/AbstractText/@NlmCategory` — Two readings. (D) provider wrapping: it is NLM's mapping of the publisher's own @Label onto a fixed vocabulary — provider-added, and @Label (which we keep) is the entity's own. Entity information: the two are independent
- `Article/AuthorList/Author/CollectiveName/@Investigators` — Two readings. (A) wrapping: it is an intra-file XML cross-reference, and the thing it points at (InvestigatorList) is itself entity information we should keep — so once both are kept the pointer may be redundant. Entity 
- `MedlineCitation/CitationSubset` — Two readings, and I will not guess. (D) provider wrapping: a subset is how NLM partitions ITS OWN corpus for its own products, which is provider bookkeeping wrapped around the entity. Entity information: the codes are su
- `MedlineCitation/MeshHeadingList/MeshHeading/DescriptorName/@AutoHM` — Two readings. (D) provider wrapping: it describes NLM's internal indexing workflow rather than the article. Entity information: it is the provenance of a subject term we publish as fact, i.e. how much that term is worth 
- `MedlineCitation/NumberOfReferences` — Two readings. (D) provider wrapping: it is a count NLM computed over the bibliography for its own display, and it is derivable from ReferenceList once that is kept. Entity information: on older records that carry the cou
- `PubmedData/History/PubMedPubDate @PubStatus in {entrez, pubmed, pubmedr, medline` — Genuinely ambiguous between (C)/(D) and entity information, and the two statements of the rule disagree. The original quote allows dropping "the date the entity has been added to the corpus" — PubMed IS a corpus and thes

**Claimed droppable, overturned on review (3):**

- `MedlineCitation (wrapper)` → must-keep
- `MedlineCitation/@Owner` → must-keep
- `Article/Journal/JournalIssue/@CitedMedium` → must-keep

### gutenberg — keeps 12 of 62 enumerated origin fields

GUTENBERG FIELD AUDIT — 14 must-keep, 4 unsure, and 3 defects that lose the entity's own text.


| origin field | what it holds | where it goes |
|---|---|---|
| `dcterms:alternative` | an alternative title the origin asserts: "Oedipus Trilogy" (pg31), "Alice in Wonderland" (pg11) — 4 of the 25  | extra.alt_titles |
| `pgterms:agent/@rdf:about` | the PG agent identifier, "2009/agents/7" — the stable key that groups an author's books across name variants | extra.creators_detail[].agent_id |
| `pgterms:alias` | the author's other names: "Dodgson, Charles Lutwidge", "Dodgson, Charles L."; "Σοφοκλής", "Sófocles", "Sophokl | extra.creators_detail[].aliases |
| `pgterms:birthdate` | the author's birth year as an integer, 1832; negative for BCE (-496, Sophocles) | extra.creators_detail[].birth |
| `pgterms:deathdate` | the author's death year, 1898 / -406 | extra.creators_detail[].death |
| `pgterms:webpage` | authority link(s) for the author: https://en.wikipedia.org/wiki/Lewis_Carroll; pg31 carries two (en + hu) | extra.creators_detail[].webpages |
| `marcrel:trl / marcrel:edt (and marcrel:ill, ctb, aui, com, … in the wider catalogue)` | the ROLE wrapper around a contributing agent: pg31 marcrel:trl = Storr, Francis (translator); pg19/pg28 marcre | extra.creators_detail[].role |
| `dcterms:language (repeatable)` | RFC4646 value, "en"; a bilingual edition carries two | lang (first) + extra.languages (all, in origin order) |
| `dcterms:subject > dcam:memberOf` | WHICH scheme each subject belongs to: http://purl.org/dc/terms/LCSH (99 of 126 in the local sample) vs .../LCC | subjects = the LCSH headings; extra.lcc = the LCC class code |
| `dcterms:type > DCMIType rdf:value` | what the entity IS: "Text" for all 22 local books; PG also publishes "Sound" (audiobooks), "Image", "Dataset", | extra.dcmi_type |
| `dcterms:description` | varies per book: a real description of the work, "Three Greek plays" (pg31), or a pointer, "Wikipedia page abo | extra.description |
| `dcterms:tableOfContents` | the work's own contents statement: "Oedipus the King -- Oedipus at Colonus -- Antigone." (pg31, pg28) | extra.toc |
| `dcterms:rights` | the rights status of the ENTITY, and it VARIES: "Public domain in the USA." (20 of 22) vs "Copyrighted. Read t | extra.rights |
| `pgterms:marc908` | "Reading ease score: 87.5 (6th grade). Easy to read." — 22 of 25 RDFs | extra.reading_ease |
| `text body: everything before the first chapter heading when that heading starts at chara` | up to ~200 characters of the book's own opening matter | sections |
| `text body: a chapter heading whose body is empty` | a part divider like "BOOK II" with no text before the next heading | sections |
| `text body: a chapter heading longer than 120 characters` | the tail of a long heading, e.g. a chapter-summary heading in a Victorian novel | sections |
| `epub: a BROKEN <img> reference (target not in the zip) and its alt text` | an <img src> naming a file the epub does not contain | sections (caption) + figures[] with carried:false |

**Unsure (6) — for the operator:**

- `pgterms:marc508` — READING 1, (D): it is PG's bookkeeping about its own transcription process, not an assertion about the book — the volunteers are not contributors to the WORK. READING 2, entity information: they are the contributors to t
- `pgterms:marc520` — READING 1, (D): the provider generated this text with a machine; the origin work asserts none of it, and putting generated prose into a signed record risks a reader taking a hallucinated claim ("published in 1865") as an
- `other pgterms:marcNNN datafields across the full 79k mirror` — UNAUDITED, and this is the one gap that cannot be closed from this machine: 25 RDFs and a twelve-book spot check are not a census. Every one of these would be entity information (ids, edition, series, publication details
- `text file header: "Credits: Arthur DiBianca and David Widger"` — identical question to marc508 above — (D) provider production bookkeeping, or the contributors to the bytes we index. If marc508 is kept, this copy is redundant and droppable with no loss.
- `epub: spine items marked linear="no"` — pg_epub.py:184 skips them. READING 1, (A)/(D): the origin itself declares this matter to be outside the book's reading order, so skipping it follows the origin rather than overriding it. READING 2: if any PG generator ma
- `epub: the NAV / NCX table of contents` — not read at all; `sections` come from a regex over flattened text (pg_text.split_chapters), so the origin's DECLARED structure — which the operator's rule names ("its structure") — is replaced by our guess at it. READING

### wikipedia — keeps 8 of 56 enumerated origin fields

Read app/plugins/wikipedia/source.py and wikitext.py in full (authoritative via Read/PowerShell — Git Bash returned stale OneDrive copies mid-session, twice), plus plugins/base.py, plugin_io.py, pack/build.py, pack/records.py, docs/09 §9.5, docs/06 §6.4, the plugin's tests and docs/notes/v1-rebuild.md + records-origin-gap.md. Where behaviour was in doubt I MEASURED it: a probe against mwparserfromhell 0.7.2 in the louise-rag:dev image settled six rows.


| origin field | what it holds | where it goes |
|---|---|---|
| `page/redirect@title` | the redirect TARGET, e.g. <redirect title="Moon" />. source.py:205 reads only `page.find("redirect") is not No | extra.redirect_to |
| `revision/id` | `1198450203` — the id of the exact revision we parsed. Never read | extra.revision |
| `revision/timestamp` | `2024-01-24T14:22:10Z` — when this revision was published at the origin. Kept, but TRUNCATED: `(...)[:10]` (so | date (YYYY-MM-DD) + extra.revised (full ISO-8601 instant, un |
| `wikitext: [[Category:X]] links` | `[[Category:Non-verbal communication]]` — the origin's subject classification. 76% of pages carry them, 3.1 on | subjects |
| `wikitext: {{Infobox …}} parameter VALUES` | the article's structured facts — birth/death dates, coordinates, population, taxon rank, ISBN, box office. VER | extra.infobox (template name + parameter map) |
| `wikitext: {{Short description\|Human creative activity}}` | a one-line description of the entity. VERIFIED DROPPED with all templates | extra.short_description |
| `wikitext: {{Coord\|48\|51\|N\|2\|21\|E}}` | the entity's geographic coordinates. Dropped with all templates | extra.coordinates (or inside extra.infobox) |
| `wikitext: {{Authority control}} / VIAF, ISNI, LCCN, GND, MusicBrainz ids` | external canonical ids for the entity. Dropped with all templates | keys (as `viaf:…`, `isni:…`; sorted by bytes per §9.5) |
| `wikitext: data templates inline in prose — {{convert\|5\|km}}, {{birth date\|1879\|3\|14` | VERIFIED: `{{convert\|5\|km}}` vanishes and the sentence reads "… activity." with the quantity gone. tests/tes | sections (rendered value in place) |
| `wikitext: <ref>…</ref> inline citation content` | VERIFIED, and worse than dropped when the ref is plain text: `activity.<ref>Smith 2001, ISBN 0-123</ref>` stri | extra.references (and out of the body |
| `wikitext: == References == / Notes / Sources / Bibliography / Footnotes sections` | dropped WHOLE by wikitext.py:14-15 + 26-27 (_BOILERPLATE), heading and body. The module docstring calls them " | extra.references |
| `wikitext: == External links == section` | dropped whole by _BOILERPLATE — the entity's official site, its IMDb/Crunchbase/museum page | extra.external_links |
| `wikitext: == See also == section` | dropped whole by _BOILERPLATE. VERIFIED: `* [[Aesthetics]]` disappears with the section | extra.see_also (or keys) |
| `wikitext: == Further reading == section` | dropped whole by _BOILERPLATE | extra.further_reading |
| `wikitext: external link URLs in prose` | `[https://example.org Label]` strips to "Label" (ExternalLink.__strip__ prefers the title); the URL is lost. A | extra.external_links |
| `wikitext: [[File:…]] / [[Image:…]] filename, and <gallery> entries` | VERIFIED: `[[File:Art.jpg\|thumb\|right\|200px\|A ''painting'' by Monet]]` strips to "thumb\|right\|200px\|A p | extra.images (filenames + seq); captions cleaned into sectio |
| `wikitext: tables {\| … \|}` | VERIFIED: cell values SURVIVE but flattened structure-free — `{\|! Year !! Value \|- \| 1503 \|\| high \|}` be | sections (versioned table flattening) |
| `wikitext: heading LEVEL / section nesting depth` | `==` vs `===` vs `====`. get_sections(flat=True) plus reading only headings[0] (wikitext.py:23-25) flattens ev | extra.section_levels (list of ints, parallel to sections) |
| `wikitext: a section whose body is empty` | wikitext.py:33 `if body:` drops the pair entirely, so a pure container heading ("History", whose prose is all  | sections (keep the pair with an empty body) |
| `wikitext: interlanguage links [[fr:Art]]` | VERIFIED leaking: they strip to the literal text "fr:Art", "de:Kunst" in the body — unless they trail a _BOILE | keys (`wikipedia:<lang>:<title>`) |
| `wikitext: #REDIRECT [[Target]] and a redirect's own categories/{{R from …}}` | source.py:205-206 short-circuits to sections=[] and never calls to_sections, so a redirect page's wikitext is  | extra.redirect_to + subjects |
| `lang, as written to the record` | DEFECT, not a drop. source.py:204 reads `raw.ref.extra.get("lang") or "en"`, but plugin_io.canonical_doc (plug | lang |
| `origin (the §9.5 locator), as written for wikipedia` | build.py:188 calls plugin_io.origin_locator, which splits on `#`; wikipedia declares no LOCATOR_SEP, so the wh | origin |

**Unsure (13) — for the operator:**

- `page/ns` — READING 1 (must-keep): the operator's rule lists "its classification", and `ns` is the origin's own classification of the page. READING 2 (droppable-D): it is the wiki's filing system, and for this collection it is const
- `revision/parentid` — READING 1 (must-keep): an id the origin asserts about our revision — it names the diff that produced the text we hold. READING 2 (droppable-D): it points at a version we do not hold and never will; the edit-history chain
- `revision/contributor/username` — READING 1 (droppable-D): a pages-articles dump holds ONE revision, so this is the last editor, not the article's authorship — which is the whole contributor history the dump does not carry. Putting it in `creators` would
- `revision/contributor/id` — Same group and same two readings as contributor/username; it adds only the account's internal id
- `revision/contributor/ip` — The same field as username, but keeping it publishes personal data (a home IP) into immutable, signed, peer-replicated records that 01 promise 7 says are never deleted, on a plugin declaring privacy_class=PUBLIC. This is
- `revision/minor` — READING 1 (droppable-D): a property of the EDIT ACTION, asserted by the editor about their own workflow. READING 2 (must-keep): the origin asserts it about our revision. Same group as parentid/comment/origin — one decisi
- `revision/comment` — READING 1 (must-keep): it is the only statement of WHY the text we hold differs from its parent — provenance about our revision. READING 2 (droppable-D): it describes the edit, not the subject, is editorial workflow of t
- `revision/origin` — Same class and same two readings as parentid: an id of a revision we do not hold, versus an id the origin asserts about the content we do
- `revision/sha1 (and text@sha1)` — READING 1 (droppable-D): 06 §6.4 rules explicitly on this class — "the ORIGIN's own checksums verify that a FETCH arrived intact", `origin_hash` verifies the record, and conflating them is the wrong trade. READING 2 (mus
- `wikitext: a category's sort key, [[Category:Art| ]]` — READING 1 (droppable-A): the sort key is a rendering directive for the category's listing page. READING 2 (must-keep): it is often the entity's sort name ("Einstein, Albert"), information present nowhere else. Either way
- `wikitext: {{DEFAULTSORT:Einstein, Albert}}` — READING 1 (must-keep): it is the origin asserting the entity's filing name — surname-first for people — which no other field carries. READING 2 (droppable-A): a sorting directive for category listings, i.e. rendering
- `wikitext: internal wikilink TARGETS` — READING 1 (must-keep): a link target is an id of another entity, and the rule lists "its ids" and "its structure"; the link graph is also real retrieval signal. READING 2 (droppable-A): `[[…|…]]` is wikitext syntax and t
- `non-article namespace PAGES — Category:, Template:, Portal:, Book:, Draft: …` — Not a field drop but a collection-scope choice, and it has a field consequence: a Category: page carries the CATEGORY HIERARCHY (its own parent categories), which is what would let the Codex declare `subjects` as a `tree

**Claimed droppable, overturned on review (1):**

- `mediawiki/siteinfo/dbname + siteinfo/base` → must-keep — `extra.wiki` = siteinfo/dbname ("simplewiki"), and siteinfo/base feeds the top-level `origin` (the page URL); `lang` read from `mediawiki/

### embedded — keeps 11 of 20 enumerated origin fields

The archive carries exactly five columns per row — `id`, `url`, `title`, `text`, `embedding` — confirmed three ways: the code that reads them (source.py:82/103-106 and genesis.py:635/638), the live Hub schema for all 11 configs, and the README's own loading example. One entity = one article = N consecutive rows; the only non-column facts the origin asserts about it are its language (the `data/{lang}/` path segment) and its location (shard file + row range).


| origin field | what it holds | where it goes |
|---|---|---|
| `row column `id` (whole string) — e.g. "10_0"` | The origin's primary key for one row = one paragraph = one of our chunks. Format "{article}_{seq}". Verified a | extra.row_ids |
| `the entity's location in the origin — shard file + row range, e.g. "data/tr/003.parquet"` | Which origin bytes this record was parsed from. Built as the locator at source.py:414-417 ("<abs shard path>:: | `origin` (§9.5), fixed: give EmbeddedSource a class-level LO |
| `the origin's repo REVISION (HF commit) the bytes came from` | Which version of the repo was fetched. The transport defaults revision to "main" (transport_hf_hub.py:24) and  | not a record key |
| `the provider's processing rules — paragraphs title-prefixed before embedding; paragraphs` | Stated in the README; the first is reproduced by embed_input (source.py:136-142) because the vectors depend on | not a record key |

**Unsure (1) — for the operator:**

- ``id` → seq part — e.g. the "0" in "10_0"` — TWO READINGS. (D)-provider-wrapping: it is the producer's own chunk bookkeeping — their row numbering inside their parquet — and our sections preserve the order 1:1, so nothing about the article is lost. ENTITY INFORMATI

**Claimed droppable, overturned on review (1):**

- ``id` → seq part — e.g. the "0" in "10_0"` → may-drop — (D) provider row bookkeeping; but add a parse-time seq==position guard instead of extra.row_ids

### genome — keeps 12 of 29 enumerated origin fields

ADVISORY, with two items that are not. Genome is not on the v1.0 root list, and STATUS.md's step-8 scope (operator, 2026-09-10) fixes that the three genome collections "stay on the old format, because no DNA calibration set exists so `create` cannot write them a Codex at all" — so nothing here blocks gutenberg/pubmed/wikipedia. Three things are nonetheless time-critical.


| origin field | what it holds | where it goes |
|---|---|---|
| `FASTA header, everything after the first whitespace (the description)` | Free text the origin puts on the header line after the name. Empty in UCSC bigZips today (headers are a bare " | extra.header_description ("" when the origin's header has no |
| `Residue CASE — lowercase vs uppercase` | UCSC's per-base repeat annotation. Verified against the origin's own README (hgdownload.soe.ucsc.edu/goldenPat | sections |
| `Strand` | A reference FASTA is the plus/forward strand of the assembly, by convention — the origin asserts it by shippin | extra.strand: "+" |
| `<db>.chromAlias.txt (sibling file in the same origin directory)` | THE ACCESSIONS. From the origin's README: "sequence name alias file, one line for each sequence name. First co | keys (canonical ids per 06 §6.5: "refseq:NC_000001.11", "gen |
| `bigZips/README.txt — the assembly date` | "The Dec. 2013 assembly of the human genome (GRCh38 …)" for hg38; "April 2011" for sacCer3 (both quoted from t | date (top-level, "2013-12") |
| `bigZips/README.txt — the producing consortium / source` | "GRCh38 Genome Reference Consortium Human Reference 38" for hg38; "Saccharomyces Genome Database" and NCBI Gen | creators (e.g. ["Genome Reference Consortium"]) |
| `Species / organism and NCBI taxid` | "human (GRCh38)", "yeast (S. cerevisiae)" and the taxid (9606, 10090) — which organism this chromosome is from | extra.taxid (integer) + extra.organism; optionally the organ |
| `bigZips/genes/*.gtf.gz (refGene, ncbiRefSeq, ensGene)` | GENE NAMES and the whole annotation layer: gene symbol, transcript id, exon and CDS coordinates, per-feature s | not a record field |
| `record `origin_hash`` | Invariant 11 / 09 §9.5: "BLAKE2b-128 of this document's own origin bytes, the ones `origin` locates" | origin_hash |
| `Non-ASCII bytes anywhere in the record` | Any byte outside ASCII in a FASTA the plugin opens | no field |

**Unsure (2) — for the operator:**

- `<db>.fa.out.gz (RepeatMasker .out) and chromTrf.tar.gz` — Two readings, both defensible. AS ENTITY INFORMATION: these are annotations about this sequence, the same kind of assertion as a gene, and the soft-mask case we lose in the CASE row is their lossy projection — keeping ca
- `<db>.agp.gz / chromAgp.tar.gz` — Reads as the entity's own structure (which sequenced fragments this chromosome is made of, and which way round each went in — plus a second source of per-contig accessions, which pulls it toward the chromAlias row's must

**Claimed droppable, overturned on review (1):**

- `bigZips/README.txt — licence/citation prose and download instructions` → must-keep