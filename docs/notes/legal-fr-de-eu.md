# France, Germany and the EU — three legal collections on Normattiva's template (2026-10-28)

**Status: DECIDED (§8, operator 2026-10-28); nothing is built.** Asked by the operator, 2026-10-28:
"I need the equivalent of normattiva collection for french, german and
European laws, prepare a design note." The decisions that are the
operator's are in §8, each in the one shape (CLAUDE.md, Style).

**Read first:** [`legal-collection-template.md`](legal-collection-template.md)
(what Normattiva paid for, and the checklist) and
[`eli-legal-plugins.md`](eli-legal-plugins.md) (identity, §6's eight
rules, built as `app/plugins/eli.py`). This note does not repeat them; it
says what each origin does to them.

**How the facts were gathered, and how far to trust them.** Three research
passes on 2026-10-28. This cloud session's network denies every origin
(`echanges.dila.gouv.fr`, `legifrance.gouv.fr`, `piste.gouv.fr`,
`gesetze-im-internet.de`, `*.rechtsinformationen.bund.de`,
`recht.bund.de`, `*.europa.eu`), so **no origin was probed**. A fact marked
**[V]** was read first-hand: a Hugging Face card, or the German portal's
own public source repository (`digitalservicebund/ris-search` at
1bc4d79). **[M]** is a verbatim mirror of an origin's files on GitHub.
**[S]** is secondary: search extracts, third-party code and notes. The
template's step 1 — probe the origin with curl before designing a fetch
path — is therefore **owed, from the node**, for all three (§7, step 1).
Nothing below that rests on [S] may be pinned into a Codex before that
probe.

---

## 1. The three at a glance

| | France | Germany (federal) | European Union |
|---|---|---|---|
| Origin | DILA open data **LEGI** (consolidated) + **JORF** (originals) [S] | **NeuRIS**, `rechtsinformationen.bund.de` API [V]; or gesetze-im-internet (GII) [S] | EUR-Lex / **Cellar** (REST + SPARQL) [S] |
| Form | XML per object, tar.gz baseline + daily increments [M][S] | LegalDocML.de (an Akoma Ntoso profile) [V]; GII: `gii-norm` XML [V] | Formex 4 or XHTML for text, CDM notice for metadata [S] |
| Every version? | **Yes**, per article, back to ~1889 for the oldest [M] | **No origin today.** GII is current only; NeuRIS's model holds versions, its own pages list historical versions as *planned* [V] | **Yes**: every OJ original and every consolidated version [S] |
| Record grain | **one version of one article** (§2.2) | one expression of one act, as Normattiva (§3.3) | one version of one act in one language, as Normattiva (§4.2) |
| Identity | `LEGIARTI`/`LEGITEXT`/`JORFTEXT`; ELI on JORF originals only [M][S] | ELI `eli/bund/bgbl-1/{year}/{natId}/…` [V]; `jurabk`/`amtabk` | CELEX; ELI `eli/{type}/{year}/{num}/oj`, **article-level ELI exists** [S] |
| Relations | `LIENS`, typed, article or text level [M] | NeuRIS `ris:verweise` in the schema, populated? [V/unknown] | CDM: amends, based_on, cites, corrigenda, in force, end of validity [S] |
| Licence | Licence Ouverte 2.0 [S] → green | § 5 UrhG, official works [S]; NeuRIS states **no** data licence [V] → green, terms text to settle (§3.8) | CC BY 4.0 content, CC0 metadata; third-party material excluded [S] → green, with an exception (§4.7) |
| Size | 73 codes in force + 29 repealed; ~1.83 M article versions; baseline 1.17 GB [S] | in force (May 2024): 1,797 laws / 52,401 provisions, 2,866 ordinances / 44,475 provisions [S] | ~46,315 acts in force (Oct 2026); 24 languages [S] |
| Expert eval set | **none for French statute law** [V search] | **none expert**; GerLayQA is lay questions, research-only [V] | none multilingual; **JuriFindIT covers EU acts in Italian**, held on the node [V] |

## 2. France — LEGI and JORF

### 2.1 The origin form, and why

- **The DILA open-data bulk, not the Légifrance API.** `echanges.dila.gouv.fr/OPENDATA/LEGI/` holds a full baseline (`Freemium_legi_global_20250713-140000.tar.gz`, 1,171,027,316 bytes by a third party's HEAD) and one increment per working day (`LEGI_YYYYMMDD-HHMMSS.tar.gz`, 1–8 MB), plus a deletion list (`liste_suppression_legi.dat`); the current state is the baseline plus every increment in order [S]. No authentication [S]. The PISTE API needs OAuth2 client credentials and terms accepted per environment, with quotas visible only inside the portal [S]: one GET and a daily delta against thousands of authenticated calls.
- **JORF beside LEGI**, `OPENDATA/JORF/` (baseline 1.6 GB, two increments a day) [S], for one reason: the **originals** as published, the only French files that carry an **ELI** (`ID_ELI`, `ELI_ALIAS`) [S]. LEGI's consolidated versions carry none [S].
- **XML, not the HTML pages.** One file per object, UTF-8, under a path built from its id; `ARTICLE`, `TEXTE_VERSION`, `TEXTELR` (structure), `SECTION_TA` [M]. The body (`CONTENU`) is XHTML-transitional plus DILA's own tags [M].
- **Increments are applied in order, never skipped.** A file is rewritten when the origin closes a version (its `ETAT` and `DATE_FIN` change), so a skipped increment leaves a version open that the origin closed. The acquisition's unit is the increment; its checkpoint is the last increment applied (invariant 2).
- **Known gaps, stated on the card:** JORF's open data drops images, so formulas published as images are missing [S]; nominative acts (naturalisations, changes of name) are left out of JORF [S]; arrêtés are a selection, not all [S].

### 2.2 The record is ONE VERSION OF ONE ARTICLE — the one departure from Normattiva

Normattiva versions the **act**: each consolidation is a member holding
the whole text. LEGI versions the **article**: each `LEGIARTI` file is one
version of one article with its own `DATE_DEBUT`, `DATE_FIN` and `ETAT`,
and lists its sibling versions (`VERSIONS/LIEN_ART debut fin id`) [M]. A
text at a date is its dated tree of links filtered to that date [M].

Copying Normattiva's grain would mean assembling a whole-text record at
every date on which any article of it changed. A code changes on many
days a year, and each such record would repeat the whole code: storage
grows with (articles × change dates), the very per-item cost that scales
with the corpus (CLAUDE.md). So:

> **The record is one article version (`LEGIARTI`). The Work is the
> article — the chain of versions the origin lists together. The text
> (`CID`: the `LEGITEXT` of a code, the `JORFTEXT` of an act) is the
> grouping, in `extra`.**

What carries over unchanged: the chunk is the article (template §6), so
here the record *is* the chunk; the validity axis is the platform's
(`profile.validity`, `versions-per-work`, `from` = the version's start,
`work` = the chain); `--as-of` admits one version per article; vectors are
carried by chunk hash, so an article whose text did not change between
versions is embedded once.

**The Work key needs a measurement before it is pinned.** No origin id
names an article independently of its versions [M]: `LEGIARTI` names a
version, `CID` names the text, and legi.py records that "no id truly
links the versions" [S]. The proposed key is the **smallest `LEGIARTI`
of the chain's version list**, stable as long as the origin does not
add an older version. It does add them: Code civil art. 9's chain starts
at 1889 with recently back-filled ids [M]. A back-filled version with a
larger id leaves the key unchanged; one with a smaller id would move it.
**Measure on the node:** over the baseline and every increment since,
how often a chain's minimum id changes. Zero or near zero, and the key
holds with a guard (a moved key is an Issue, 06 §6.1); otherwise the key
becomes (`CID`, the article's first `NUM` in the chain) and `TRANSFERE`
moves are followed.

**Validity, as Normattiva's.** The start is `DATE_DEBUT`. The end stays
**derived** by the node (`app/validity.py`: the next version's start, or
a closure), because the origin rewrites `DATE_FIN` when it closes a
version and a record carries nothing computable (02 §2.3). The origin's
`DATE_FIN` and `ETAT` are **kept** in `extra` (invariant 11) and checked
against the derived end; a disagreement is an Issue, never a silent
choice. Three origin quirks the parser must name: `2999-01-01` is
"open" [M]; `2222-02-22` is a placeholder start [S]; a `MODIFIE_MORT_NE`
version ends before it starts [S] and is kept with an empty validity
(it was never in force, and the origin says so).

**Repeal is metadata** (eli-legal-plugins §5.7): a version with `ETAT`
`ABROGE` (`ABROGE_DIFF` when future) and a `LIEN typelien=ABROGATION`
naming the repealing act [M] closes its article and carries `in_force:
false` with that act's words.

### 2.3 Identity

- `native_id` from the ELI layer: `(days since 1500-01-01) << 45 |
  blake2b(work key ‖ LEGIARTI) mod 2**45`, sorted by `DATE_DEBUT`. Not the
  `LEGIARTI` digits themselves: they follow creation order, and the
  back-filled 1889 versions carry new numbers. A placeholder start
  (`2222-02-22`) sorts by its publication date instead, as an Italian
  original does. **Count the busiest start day before fixing the width**
  (template §2: the decimal layout lost 329,000 ids in an hour).
- `keys`: `legi:LEGIARTI…`, `legi:LEGITEXT…`, `jorf:JORFTEXT…`, `nor:…`
  (12 characters, since ~1986, not always present [M]), and `eli:fr:…`
  for what JORF carries: text `/eli/{nature}/{yyyy}/{m}/{d}/{NOR}/jo/texte`,
  article `…/jo/article_N`, the numbered alias
  `/eli/loi/2022/3/3/2022-309/jo/article_1` [S]. ELI here identifies
  **originals, not consolidated versions** [S].
- Duplicates: one id can sit under two text folders; legi.py keeps a
  table of them [S]. One record per id, the second sighting an Issue.

### 2.4 Relations

`LIENS` carry `typelien` (ABROGATION/ABROGE, MODIFICATION/MODIFIE,
CREATION/CREE, CODIFICATION/CODIFIE, CITATION, TRANSFERT, RATIFIE, …),
`sens`, and the target by id, text id or title only [M]. **The direction
depends on the form**: for the verb forms and CITATION, `sens` gives the
carrying file's role; for the noun forms, the linked element's [S,
consistent with the mirrored samples]. Typed after ELI as Normattiva's
are (`eli:amends`, `eli:repeals`, `eli:cites`), **article to article
where the origin gives an article id** — finer than Italy, where edges
are between acts. Edges restrict and close validity; they never rank
(template §7).

### 2.5 The reference source and the citation grammar

Tables: `texte` (CID, nature, number, NOR, dates, title, code
abbreviation), `article` (LEGIARTI → CID, `NUM` normalised, start,
origin's end, état). Namespaces `legi`, `jorf`, `nor`, and `eli` under
the shared owner of §5.2.

The resolver must read, at least [S]:

- **Code articles**: `article L. 121-1 du code de la consommation`,
  `art. L121-1 C. consom.`, `C. civ., art. 1240`, `art. 1240 C. civ.`;
  prefixes `L.`, `R.`, `D.`, `A.`, `LO`, `R*`; `1er`, `bis`, `ter`; the
  CGI's `39 octies A`, `1600-0 H`; ranges `L. 121-1 à L. 121-7`;
  sub-parts `I`, `2°`, `a)`, `alinéa`. LEGI's `NUM` drops the dot and the
  space (`L121-1`): both sides are normalised to one form.
- **Code names and their abbreviations** — CGI, LPF, CSP, CSS, CGCT,
  CPC / C. pr. civ., CPP / C. pr. pén., CMF, CPI, CCH, CASF, CESEDA, CRPA,
  CJA — a table taken from the codes' own titles in `texte`, never typed
  by hand.
- **Acts**: `loi n° 78-17 du 6 janvier 1978`, `loi n°2016-1321`, `loi du 29
  juillet 1881` (no number), `loi organique n° …`, `décret n° 2019-1 du 3
  janvier 2019`, `ordonnance n° 2016-131`, `arrêté du 21 juin 2016` (NOR
  only), `décret-loi`; two-digit years until 1999, four since.
- **Gazette**: `JORF n°0235 du 8 octobre 2016, texte n° 1`.

A French citation usually names its **article**, and here the article is
the record: a cited article resolves to its versions as facts, never
ranked (invariant 4), at the date asked.

### 2.6 Scope, size, licence

- Scope [S]: the 73 codes in force and 29 repealed; laws, ordinances and
  decrees exhaustively since 1945; a selection of arrêtés; JORF since
  1990. ~1.83 M article-version rows (in force 718,399; modified
  440,767; repealed 363,947; ~241,000 with an empty `ETAT` — to explain
  on the node before parsing) [S, Sept 2026]; ~5.25 M XML files [S].
  **Measure on the node** (template §11 rule 12) the scope against the
  law's own numbering before calling it "French law".
- Licence: **Licence Ouverte / Open Licence 2.0** (Etalab), commercial
  reuse allowed [S]; attribution to DILA with the download URLs and the
  file name and date [S] — the Codex's `credits`. DILA's personal-data
  warning (`OPENDATA/AVERTISSEMENT-Donnees_a_caractere_personnel.pdf`) is
  read before the class is fixed. Class **green** unless that warning
  says otherwise; the terms text byte-exact (invariant 9).

### 2.7 Vectors that already exist

`AgentPublic/legi` on Hugging Face (etalab-2.0, a government publisher)
ships LEGI with every `ETAT`, already embedded with **bge-m3** [V]. If the
space's encoder is bge-m3 and its chunk is the article, those vectors are
an archive to carry (RULE 1, as Wikipedia's Upstash vectors were): kept
where a sample re-embedded on our engine agrees within the tolerance,
recomputed where it does not. Their chunking and their text form (does
the vector cover the article's number and heading?) are the first thing
to read.

## 3. Germany — federal law

### 3.1 The deciding fact

**No origin serves every version of German federal law today.**

- **gesetze-im-internet (GII)**, BMJ / juris: one `/{slug}/xml.zip` per
  law listed in `gii-toc.xml` [S], **current text only**: old versions
  are overwritten and not kept [S, and its DTD's own header: "aktuelle
  Bundesgesetze" V].
- **NeuRIS**, `rechtsinformationen.bund.de`, the federal government's
  portal (DigitalService), public test phase since April 2025 [S]: API at
  `testphase.rechtsinformationen.bund.de/v1/`, no key, 600 requests a
  minute per IP [V]. Its model is built for history — each API item is
  an **expression**, articles have `Fassungen` and `temporalData`
  validity periods, the test data holds future versions [V] — but its own
  pages list "historical and not yet in force versions" as **planned**
  and today's laws "in der jeweils aktuell gültigen Fassung" [V], and its
  docs warn "Incomplete dataset" [V]. A bulk endpoint
  (`GET /v1/bulk-zip-links`, S3 zips per kind) is in the code [V]; whether
  it is live is not known.
- **The Bundesgesetzblatt** (`recht.bund.de`), authentic and electronic
  since 2023, is **PDF**; LegalDocML.de is "planned" with no date [S].

So "every version" is not available from any origin; what can be held
today is the current version, and the history from our first snapshot
on. That is the decision of §8, D1.

### 3.2 The origin form, if NeuRIS (the recommendation of D1)

- **LegalDocML.de**, namespace `http://rechtsinformationen.bund.de/schema/norm/0.1`
  [V] — Akoma Ntoso, like Normattiva's: FRBR Work / Expression /
  Manifestation, `lifecycle` events (Ausfertigung, Inkrafttreten,
  Außerkrafttreten), `temporalData` periods, each `article@period`
  pointing at one [V], and a `ris:` block per document and article
  (`abkuerzung`, `inkraft`, `ausserkraft`, `vollzitat`, `standangabe`,
  `doknr`, `gegenstandslos`, `bedingtesInkrafttreten`, `verweise`) [V].
  The ELI layer and Normattiva's AKN reader (`akn.py`) are the starting
  point; the profile's namespaces and the `ris:` block are what is new.
- **The API, paced**: `/v1/legislation` lists expressions,
  `/v1/legislation/changelog` gives the increments, the manifestation
  URLs give the XML [V]. 600 a minute is a ceiling, not a budget: the
  transfer manager paces it (invariant 5), two in flight as with
  Normattiva. The bulk zip, if live, replaces the listing.
- **The changelog is the delta**; a law whose expression is unchanged is
  not fetched again.

### 3.3 Grain, identity, validity

- **One record per expression of a law**, as Normattiva's, the chunk the
  article (§, Art.). The article's `ris:doknr` is a revision id shared by
  every expression that holds that unchanged article [V]: the vector carry
  by chunk hash finds the same thing, and the doknr is the check.
- **ELI** [V]: `eli/bund/bgbl-1/{year}/{natId}/{pointInTime}/{version}/deu/…`,
  `natId` = `s{page}` before 2023 (`eli/bund/bgbl-1/1979/s1325`) and the
  issue number since (`…/2024/383/2024-12-19/1/deu`). The Work is the
  path up to `natId`; the expression is `{pointInTime}/{version}`, read
  back from the answer (eli rule 2). Articles carry eIds (`art-z1`,
  `art-z1_abs-z1`) [V]; whether their GUIDs survive across versions is
  not known — measured before anything keys on them.
- `keys`: `eli:de:…`; the abbreviations `jurabk` (juris's, mandatory) and
  `amtabk` (the official one, optional — `SGB 5` vs `SGB V`) [V] under a
  `jurabk` namespace; `doknr` (BJNR… a law, BJNE… a provision) [S].
- **Validity from the origin's own periods** (`temporalData`), the end
  still derived and checked against `ausserkrafttreten`, as in §2.2.
  `bedingtesInkrafttreten` (conditional entry into force) has no date
  until the condition is met: an open start, said on the record, never
  guessed.

### 3.4 Relations, reference source, citations

`ris:verweise` (type, citing norm, cited norm by abbreviation, division
and provision, dates) and `standangabe` types (aufh, neuf, hinweis, sonst,
stand) are in the schema [V]; whether production fills them is to read on
the first members. GII has only free-text `standangabe` and footnotes [V].

The resolver must read [V for the forms NeuRIS's fixtures use, S
otherwise]: `§ 823 Abs. 1 S. 1 BGB`, `§§ 823 ff.`, `Art. 3 Abs. 1 GG`,
`§ 1 Satz 2 Nr. 3 Buchst. a Halbs. 2`, `Alt.`, `i.V.m.`, and `a.F.` /
`n.F.`, which name a **version** (the resolver maps them to validity, the
date from context or asked); the Gazette, `BGBl. I S. 42` (year from
context; "S." is Seite here and Satz above), `BGBl. 2025 I Nr. 240` since
2023, `BGBl. III FNA 400-2`, `RGBl. S. 195`; and the `Vollzitat` chain
("in der Fassung der Bekanntmachung vom … , zuletzt geändert durch
Artikel …"). The abbreviation table (`BGB`, `GG`, `StGB`, `SGB V`, …)
comes from `jurabk`/`amtabk`, never typed by hand.

### 3.5 Scope, size

Federal law only. In force, May 2024: 1,797 laws with 52,401 provisions
and 2,866 ordinances with 44,475 provisions [S]; GII's TOC lists
~6,800–6,900 entries including announcements [S]; a third party counted
5,495 items on NeuRIS against 6,139 on GII [S, undated] — **the scope gap
is measured on the node and stated on the card**. Länder law is on
sixteen portals, mostly run by juris, with their own terms and mostly no
bulk XML [S]: a separate design, not this one.

### 3.6 Licence

Laws and ordinances are official works without copyright, § 5(1) UrhG
[S]; the BGBl is free to reuse, commercially too [S]. NeuRIS states **no
data licence anywhere** — its impressum has a liability disclaimer only,
its code is GPL-3.0 [V]. GII is a juris compilation, and no source
addresses the database right of § 87a UrhG over it [S]: one more reason
for NeuRIS, the government's own. Class **green**; the terms text
(invariant 9, byte-exact) is § 5 UrhG's wording plus the portal's own
statement of free access, until the portal publishes terms — part of
D1's choice.

## 4. European Union — EUR-Lex / Cellar

### 4.1 The origin form, and why

- **Cellar**, the Publications Office's repository: REST by content
  negotiation on `/resource/celex/{CELEX}` and on ELI URIs, a 303 to the
  manifestation, no authentication [S]; the **SPARQL endpoint**
  (`publications.europa.eu/webapi/rdf/sparql`, Virtuoso) for the
  inventory — every act, every consolidated version, the relations, in
  force and end of validity [S]. A Virtuoso answer can be silently
  truncated at a timeout [S], so the inventory is read in windows with
  counts checked, never in one query.
- **Not the Data Dump** (`datadump.publications.europa.eu`, June 2025):
  EU Login, sector 3 in force only, no history, no consolidated
  versions [S] — it cannot hold "every version". **Not the SOAP web
  service**: registration and a daily call limit [S].
- **Text: Formex 4** (the OJ's production XML, article structure, thin
  metadata) or **XHTML**; metadata from the **CDM notice** [S]. For
  consolidated versions only HTML/XHTML and PDF are confirmed; Formex for
  them is not; AKN4EU for consolidations is *planned* in a 2026 tender
  [S]. **The probe decides** which form carries the structure of a
  consolidated version, and the form is recorded beside the plugin with
  its reason (invariant 11).
- **Increments**: Cellar's RSS/Atom feeds [S]; the SPARQL inventory's
  modification dates as the check.
- Etiquette: no published limit; fewer than five concurrent requests and
  backoff on 429/503 is the practice reported [S]. Two in flight, paced
  by the transfer manager.

### 4.2 Grain and validity — Normattiva's, per language

- **One record per Expression per language**: the OJ original (CELEX
  `3YYYY…`) and each consolidated version (`0` + CELEX + `-YYYYMMDD`)
  [S]. Corrigenda are OJ documents of their own (`R(xx)`) folded into
  later consolidations [S]: kept as records, typed, linked
  `eli:corrects`.
- **The consolidation's date is the start of its validity**: EUR-Lex
  defines it as the day the latest included amendment becomes applicable
  [S] — not the day the consolidation was produced. The original starts
  at its entry into force or its application, which ELI keeps apart
  (eli-legal-plugins §2) and which the CDM notice states; **which of the
  two starts validity is a choice to make once, for all three
  jurisdictions** — the template measured it for Italy (`original_start`).
- **Consolidated texts have no legal effect; only the OJ is authentic**
  (electronically since 2013) [S]: every consolidated record says so.
- End: derived, and checked against `cdm:resource_legal_date_end-of-validity`
  and `cdm:resource_legal_in-force` [S].
- **One collection per language** (operator, 2026-10-28: "European
  laws, one collection per language, prepare the 4 main languages …
  Better to not mix up too many things in one collection"; the exception
  is written in invariant 11 and 02 §2.3). English, French, German and
  Italian are prepared first; the other twenty are collections made
  later, on the same plugin, each with its language as a parameter.
  *Superseded:* "language is a slice", the proposal before §8. Completeness differs by language: an acceding state receives the
  acquis in force at accession, Maltese had a backlog, Irish is complete
  only since 2022 [S] — stated per slice on the card.

### 4.3 Identity

- CELEX: sector, year, type, number (`3 2016 R 0679`) [S]; the
  consolidated form carries its date.
- ELI [S]: `http://data.europa.eu/eli/{type}/{year}/{num}/oj`,
  consolidated `/eli/reg/2024/1689/2026-07-27`, language `/oj/eng`; and
  **article-level ELI** (`eli/reg/2019/1241/art_2/oj`, the subdivisions
  specification v2) — where Italy has none (eli-legal-plugins §5.8), the
  EU's chunk can carry an official identifier.
- `native_id`: the ELI layer's, hashed over (CELEX, language), sorted by
  the start of validity. `keys`: `celex:…`, `eli:eu:…`.

### 4.4 Relations

CDM: amends, `based_on`, `work_cites_work`, corrigenda, adopts, in force,
end of validity — at act level; some edges carry the modified location
as an axiom annotation [S]. The exact predicates for repeal and
correction are **not confirmed** [S]: read on the probe.

### 4.5 The reference source, and the payoff across collections

The resolver reads, in every language the collection holds [S; the
French, German and Italian forms are the research pass's own, to check
against EUR-Lex texts]:

- since 2015, `Regulation (EU) 2016/679` — one numbering for every type
  (`(EU|Euratom|CFSP) YYYY/N`); before, `Regulation (EC) No 1907/2006`,
  `Directive 2006/123/EC`; `règlement (UE) 2016/679`, `directive
  2006/123/CE`; `Verordnung (EU) 2016/679`, `Richtlinie 2006/123/EG`;
  `regolamento (UE) 2016/679`, `direttiva 2006/123/CE`;
- articles: `Article 6(1)(f)`, `article 6, paragraphe 1, point f)`,
  `Artikel 6 Absatz 1 Buchstabe f`, `articolo 6, paragrafo 1, lettera f)`;
- the OJ: `OJ L 119, 4.5.2016, p. 1`, `JO L 119 du 4.5.2016, p. 1`, `ABl.
  L 119 vom 4.5.2016, S. 1`, `GU L 119 del 4.5.2016, pag. 1`, and since
  October 2023 `OJ L, 2024/1689, 12.7.2024` (no page);
- CELEX itself (`32016R0679`).

**This is where the three collections stop being three** (eli-legal-plugins
§8, question 5): a French, German or Italian act transposing a directive
cites it, and the EU resolver turns that citation — in the national
language — into the EU act's versions as facts, beside the national
article. The resolver is a pure function of the query and the tables
(invariant 3); a question to Normattiva that names a directive gets the
directive pinned.

### 4.6 Scope and size

~46,315 acts in force on 21 October 2026, by a public tracker [S]; ever
produced, 144,783 regulations, 4,411 directives and 39,996 decisions [S].
No official count of consolidated versions was found: **counted by
SPARQL on the probe**, per type and per language, with the busiest
start-of-validity day for the id width. Scope proposed: sector 3
(legislation) with sector 0 (its consolidations); treaties (sector 1)
next; case law (sector 6) out of this design.

### 4.7 Licence — green, with one exception to write down

EUR-Lex's legal notice: free reuse, commercial included, under Decision
2011/833/EU; C(2019) 1655 makes **CC BY 4.0** the default for content and
consolidated texts, **CC0** for metadata [S]. **Excluded**: material
under industrial property rights, software, and **third-party content**
such as the International Accounting Standards reproduced in their
regulations [S]. Holding and sharing such an act is redistribution of
third-party text. So the collection is green, and the acts that
reproduce third-party material are either **held under their own terms**
(a yellow slice, acknowledged) or **left out by a written pruning rule**
(invariant 11: nothing is pruned unless written, in the guidelines and
the Codex). Which acts these are is listed on the probe; the choice is
the operator's when the list exists, not before.

## 5. What the three share

### 5.1 One validity model, three ways of stating the end

The platform's model holds for all three: every version a record, the
start in the record, the end derived node-side (`app/validity.py`), one
version per Work at a date. Each origin **also states** an end — LEGI's
`DATE_FIN`, NeuRIS's `ausserkrafttreten` and `temporalData`, the CDM's
end of validity — which Normattiva's did not. The rule: the derived end
is what the index uses; the stated end is kept (invariant 11) and
compared; a disagreement is an Issue (06 §6.1), counted per collection.
Under-closing is a label, over-closing a silent mis-date (template §3):
when the two disagree, the later end stands until the Issue is read.

### 5.2 The `eli` namespace needs one owner

`app/refs/normattiva.py` registers `urn`, `eli` and `akn` (`NAMESPACES`);
05 §5.2 lets a record write into `keys` only ids a reference source
registers, and one namespace has one owner. Three more jurisdictions
writing `eli:fr:…`, `eli:de:…`, `eli:eu:…` need either `eli` moved to a
**shared ELI reference source** (`app/refs/eli.py`) into which each
jurisdiction's tables feed, or per-jurisdiction namespaces. The first is
what 06 §6.5's jurisdiction-qualified canonical ids (`eli:it:…`) already
assume. It is a small platform change and comes **before** the first new
collection writes a key. The native namespaces stay with their sources:
`legi`, `jorf`, `nor` (France), `jurabk` (Germany), `celex` (EU).

### 5.3 Spaces and encoders

Normattiva's space is `jurifindit/DAR-legal-it`, chosen on 179 expert
questions (template §6). For the new three the research found **no open
legal embedder for French statutes** (lemone-embed: tax law only, synthetic
training [V]; the maastrichtlawtech models: Belgian law [V]), **none for
German law with a licence** [V], and **no EU-legal multilingual embedder**
— only general models with open licences: Qwen3-Embedding-0.6B/8B
(Apache-2.0), bge-m3 (MIT), multilingual-e5-large (MIT),
gte-multilingual-base (Apache-2.0), German_Semantic_V3 and
jina-embeddings-v2-base-de (Apache-2.0), and Euler-Legal-Embedding-V1, a
Qwen3-Embedding-8B legal fine-tune (Apache-2.0) [V].

Since every candidate is multilingual, the three collections can share
**one space** — a sister by `space_from` (lineage.md §4: "the Gazzetta and
EUR-Lex land in one legal space by construction") — so that a question
in any language reaches every collection's articles in one list, and an
EU article answers a French question. That is §8, D3.

### 5.4 Evaluation — the gap the template does not allow

The template chooses the encoder on **expert questions in the domain and
language**. What exists [V]:

| set | language, law | questions | gold | licence | fit |
|---|---|---|---|---|---|
| JuriFindIT | Italian; national **and EU** acts | 895 expert | articles | CC BY-NC-SA 4.0, already acknowledged on the node | **the EU collection's Italian slice, directly**; its card: MRR@5 BM25 38.0, Qwen3-Embedding-8B 75.2, its fine-tune 77.2 |
| GerLayQA | German; BGB, StGB, ZPO | 21.5k BGB, 1.6k StGB, 1.3k ZPO, **lay** | § cited by lawyers' answers | data "strictly non-commercial, scientific research" (the HF copy's Apache-2.0 is wrong) | German, lay not expert |
| LegalKit | French; 16 codes | 53k **synthetic** (LLaMA-3-70B) | articles | CC BY 4.0 | French codes, not expert |
| SyntecRetrieval | French; a collective agreement | 100 manual | articles | unknown | not statute |
| BSARD, LLeQA | French; **Belgian** law | expert | articles | CC BY-NC-SA 4.0 | the wrong law |
| GerDaLIR | German; court decisions | 123k | decisions | MIT | not statute |

So: the EU's Italian slice is measurable today; French and German statute
retrieval is not, by the template's own standard. §8, D4.

### 5.5 Every collection is the quarantine's guinea pig

As Normattiva was (template §10): created never listed in use, every
tool it finds missing fixed, never worked around; fetched with the
template §5's five rules; its first two passes read by their counters.

## 6. Cost, roughly

| | records | text to embed | source bytes |
|---|---|---|---|
| France | ~1.83 M article versions [S]; distinct texts fewer (the carry by hash) | the articles, once per distinct text | 1.17 GB LEGI + 1.6 GB JORF compressed [S] |
| Germany | today's expressions: ~6,000 laws and ordinances, ~97,000 provisions in force [S] | the provisions, once per revision (`doknr`) | not measured |
| EU | ~46,000 acts in force × their consolidations × **languages held** | per language slice embedded (D2) | not measured; SPARQL counts first |

The EU is the largest by far and almost all of it is languages: D2 sets
its cost. All three fit the node's existing stages; none needs a new
platform mechanism beyond §5.1's check and §5.2's namespace owner.

## 7. The order of work, per collection (the template's checklist)

1. **Probe from the node** (this session cannot): Range, listing sizes,
   whether long responses are cut, what a HEAD says; LEGI's baseline and
   one increment; NeuRIS's `bulk-zip-links` and the changelog; one
   Cellar HEAD per manifestation and a counted SPARQL window. Write the
   origin form and its reason beside each plugin.
2. **Measure by Range / API**: versions per Work, transitions shorter than
   a month, the busiest start-of-validity day (the id width), distinct
   texts across versions, token lengths under the chosen encoder (`ctx`);
   France: how often a chain's smallest `LEGIARTI` moves (§2.2), the
   ~241,000 empty `ETAT`s; the EU: which consolidated form carries
   structure, which acts reproduce third-party material (§4.7).
3. The shared pieces: `eli` under one owner (§5.2); the stated-end check
   (§5.1).
4. Per collection: the profile (ELI, validity axis, slicing — France by
   code/text and year, Germany by year, the EU by language then year),
   the licence class and its terms text; the parser against real files
   as fixtures, every field the origin asserts kept, the version bumped
   for every change; the reference source and its resolver; the encoder
   per D3 and D4; create as the guinea pig; fetch; build, snapshot, check
   (with `validation.json` written first: wikipedia-v8 lost a 43-minute
   read to its absence, 2026-10-28); `tools/legal_pilot.py coverage`,
   then `run`.

**The proposed order: France, then the EU, then Germany.** France has
every version, a bulk form, an open licence and the article grain that
is the one new mechanism — the most to learn, the least to wait for. The
EU second, because its resolver is what joins it to Normattiva and to
France. Germany last, because its history is not published yet (D1).

## 8. Decisions for the operator

**DECIDED, operator, 2026-10-28** (verbatim: "D1 yes, D2 grow collection
parameter per language, D3 one encoder per language if it fits, find
proper embedders like DAR legal it, D4 do not reinvent the wheel, find
papers, we already have something"):

- **D1 → A.** Germany from the NeuRIS API now.
- **D2 → grown per language** — *amended the same evening*: "European
  laws, one collection per language, prepare the 4 main languages …
  Better to not mix up too many things in one collection". The EU is
  **one collection per language**; the language is the plugin's
  parameter. English, French, German and Italian are prepared now, and
  another language is a new collection on the same plugin.
- **D3 → one encoder per language where one fits** (with the amendment:
  "find a proper embedder for the main 4 languages proposed, for the
  others use a compromise so far"):
  - English, French, German and Italian each get a legal-domain retriever
    chosen as DAR-legal-it was for Italian.
  - The other languages get one general multilingual model, the
    compromise, until a fitting one is found.
  - One collection is one space (invariant 1), so a language's space is
    shared with its national collection by `space_from`. French = LEGI +
    EU-fr; German = NeuRIS + EU-de; Italian = Normattiva's DAR-legal-it
    space + EU-it. A question in one language then reaches its national
    law and the EU's in one list.
  - The sharing is checked at create: `space_from` copies the space block
    and the layers, so the EU plugin's chunking must be the national
    collection's (§4.2: articles, as Normattiva).
- **D4 → published sets, no set of our own.** The encoder is measured on
  the evaluation sets the papers already publish (§5.4, and the research
  of 2026-10-28 below), preferring expert questions over lay ones and lay
  over synthetic, and statutes over case law. Each set passes the licence
  gate before use (invariant 9; GerLayQA is research-only). This replaces
  the template's "expert questions in the domain and language" for these
  three collections.

The questions as they were put follow.

**D1 — Germany's origin.** Where German federal law comes from, given
that no origin publishes its past versions today (§3.1).

| choice | effect | pros | cons |
|---|---|---|---|
| **A. NeuRIS API now** (recommended) | current expressions now; each new expression a record from our first snapshot; past versions as NeuRIS publishes them | Akoma Ntoso + ELI + FRBR, the form Normattiva already reads; the government's own portal (no juris database right); versions arrive with no change of origin | a test phase: "incomplete dataset", the API may change under the plugin; no data licence text to record; history before our first snapshot absent until NeuRIS ships it |
| B. GII now | current text now, NeuRIS later | stable for years, one zip per law, simple XML | current only, overwritten; a later move to NeuRIS is a new origin form, so a new collection (vectors carried by chunk hash); juris's § 87a database right unaddressed |
| C. Wait for NeuRIS versions | nothing until then | the full history from day one | no German law at all meanwhile, for an unknown time |
| status quo | no German collection | nothing to build | none of German law reachable |

If the operator says nothing: **A**, built third.

**D2 — Which EU languages are embedded.** Every language is held as a
slice of the one EU collection (invariant 11); what costs is embedding
it.

| choice | effect | pros | cons |
|---|---|---|---|
| **A. English, French, German, Italian embedded; the rest held** (recommended) | four slices searchable by meaning, twenty by words | the languages of the three national collections plus the EU's working language; a quarter of the encoder cost | a question in Spanish or Polish finds the text by words only until its slice is embedded |
| B. All 24 embedded | every language searchable by meaning | complete | ~6× A's encoder time on the node's one card, for languages nobody here asks in yet |
| C. English only embedded | one slice | cheapest | national-language citations still resolve, but the text answers only in English; the Italian slice JuriFindIT measures is not embedded |
| status quo | no EU collection | nothing to build | no EU law, and no directive resolved from a national act |

If the operator says nothing: **A**; another slice is embedded whenever
asked, nothing re-fetched.

**D3 — One legal space or one per collection.** Whether France, Germany
and the EU share an encoder and its space, so one question searches all
three as one list.

| choice | effect | pros | cons |
|---|---|---|---|
| **A. One shared space**, a multilingual encoder chosen by D4's measurement (Qwen3-Embedding-8B is the candidate to beat) (recommended) | sisters by `space_from`; one list across the three, in any language | cross-language and cross-collection answers (an EU article for a French question); one encoder served, one calibration | the encoder is a compromise across three languages; a pin is irreversible, so a better French-only model later means a new space for France; Normattiva stays in its own (DAR-legal-it) |
| B. One space per collection | three encoders, chosen per language | each the best for its language, when one exists | none exists today with a licence for French or German statutes, so B picks three general models anyway; three engines on one card; no list across collections |
| status quo | — | — | there is no space until one is chosen |

If the operator says nothing: **A**.

**D4 — How the encoder is chosen without expert questions in French and
German.** The template requires expert questions; none exist for French
or German statutes (§5.4).

| choice | effect | pros | cons |
|---|---|---|---|
| **A. Write a small expert set** — ~150 questions per language with gold articles, by lawyers (recommended) | the template's standard, for each language | the same bar Normattiva met; reusable for every later model | needs people who know French and German law, and weeks |
| B. Measure on what exists: JuriFindIT's EU questions (Italian), GerLayQA (lay German, research licence), LegalKit (synthetic French) | an answer this week | no one to recruit; the EU's Italian slice measured on expert questions | French and German measured on lay or synthetic questions, which the template says moved Normattiva's ranking twice; GerLayQA's licence gates it (invariant 9) |
| C. Pick Qwen3-Embedding-8B on its public results | no measurement here | fastest | the general-benchmark choice the template forbids, pinned irreversibly |

If the operator says nothing: **B now to start the build, A before the
space is pinned** — a space is pinned at `create`, so A must land before
the first collection is created; B ranks the candidates meanwhile.

## 9. Sources

France: `echanges.dila.gouv.fr/OPENDATA/` (LEGI, JORF, DTD_LEGIFRANCE);
the Etalab Licence Ouverte 2.0; legi.py (github.com/Legilibre/legi.py);
DTD mirror github.com/Legilibre/dtd-legi; LegalKit, lemone-embed,
maastrichtlawtech models, AgentPublic/legi on Hugging Face.
Germany: github.com/digitalservicebund/ris-search at 1bc4d79 (`api-docs/`,
`frontend/src/public/openapi.json`, the XSDs); `gesetze-im-internet.de/gii-toc.xml`
and `gii-norm.dtd`; bundestag.de hib 1009624 (counts);
github.com/trusthlt/eacl24-german-legal-questions (GerLayQA).
EU: `op.europa.eu/en/web/cellar`; `eur-lex.europa.eu/content/legal-notice/legal-notice.html`;
Decision 2011/833/EU; C(2019) 1655; the EUR-Lex consolidation help page
(`consleg.html`); `jurifindit/JuriFindIT`, `AndreaSimeri/GDPR`,
MultiEURLEX, LEMUR on Hugging Face; michalovadek.github.io/euverse
(counts).
