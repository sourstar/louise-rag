# Gazzetta Ufficiale / Normattiva — plugin feasibility and design

**Status: PROPOSAL, nothing built.** This is the research pass that has to
happen before a `gazzetta` plugin is written: what material exists, in what
form, what the platform's own constraints allow, and which encoder the
collection would be pinned to *forever* (invariant 1). The decisions in
here belong to the operator, not to an implementer — they are collected in
§12. Do not start coding before §12 is answered; the expensive mistake in
this codebase has always been pinning a space before measuring it.

Written 2026-08-26.

**Research caveat, stated up front:** this session's network egress is
restricted — search results were readable, pages were not. Every fact below
that came from a search-result summary rather than from the page itself is
marked *(unverified)*. The three that matter most — the Normattiva OpenData
API's bulk shape, its delta mechanism, and the exact terms text — are
exactly the unverified ones, and §12 asks for them first.

---

## 1. What material exists

There is no single "Gazzetta Ufficiale corpus". There are four distinct
bodies of material with different formats, licences and update semantics,
and conflating them is the first way this plugin goes wrong.

### 1.1 Normattiva OpenData — `dati.normattiva.it` (the one to build on)

The important development, and the reason this is worth doing now: **since
1 January 2026 Normattiva's data are open data under CC BY 4.0**, and in
February 2026 an OpenData portal went live alongside them *(unverified —
ANSA, 2026-02-24, and the portal's own pages)*.

- **Content**: every *numbered* State normative act published in the
  Gazzetta Ufficiale **from 1861 to today**, in its *multivigente* form —
  i.e. each act with all of its versions through time, not just the text as
  first published.
- **Formats**: Akoma Ntoso (`akn`), XML-NIR (the older AIPA/CR40 circular
  format), HTML, JSON, and ELI URIs for identification; PDF/EPUB/RTF for
  human consultation only.
- **Access**: a REST API (`api.normattiva.it`, a `bff-opendata/v1` surface —
  a `collections/collection-predefinite` endpoint and an `atto/dettaglio`
  endpoint are both attested) plus **pre-packaged collections** and
  **user-defined dynamic collections** *(unverified)*. The specification is
  published as `dati.normattiva.it/assets/come_fare_per/API_Normattiva_OpenData.pdf`.
- **Identity**: URN:NIR (`urn:nir:stato:decreto.legislativo:2005-03-07;82`)
  and ELI, both stable and both official.

This is the origin a plugin should target. It is structured, identified,
versioned, machine-readable, bulk-oriented and openly licensed — every
property the other three lack.

### 1.2 `gazzettaufficiale.it` — the Gazzetta itself (IPZS)

The publication of record, free in digital form since 1 January 2013.
Structure: **Parte I** = Serie Generale plus the special series (s1 Corte
costituzionale, s2 Unione europea, s3 Regioni, s4 Concorsi ed esami, s5
Contratti pubblici), **Parte II** = Foglio delle inserzioni (Tue/Thu/Sat).

Addressing is ELI, and it is two-level *(verified against the ELI register's
Italian implementation note and live URLs)*:

    issue:  /eli/gu/{yyyy}/{mm}/{dd}/{numGU}/{serie}/{formato}
            …/eli/gu/2026/07/08/156/sg/pdf     …/eli/gu/2009/01/13/3/s4/pdf
    act:    /eli/id/{yyyy}/{mm}/{dd}/{codiceRedazionale}/{serie}
            …/eli/id/1941/07/16/041U0633/sg

    legacy: /atto/serie_generale/caricaDettaglioAtto/originario
              ?atto.dataPubblicazioneGazzetta=YYYY-MM-DD
              &atto.codiceRedazionale=22A03920

Issues resolve to PDF **and** HTML; individual acts resolve to HTML only —
**there is no per-act XML here**, which is precisely what Normattiva adds.

What the GU has that Normattiva does not: everything **non-normative** —
ministerial decrees that never get a number, comunicati, concorsi, bandi,
inserzioni. That material is only reachable by walking the issue index, it
carries no URN, and (see §10) parts of it contain personal data.

### 1.3 Au.G.U.Sto — historic GU 1860–1946

~660,000 pages of the Gazzetta Ufficiale del Regno, free, **scanned PDF
only** — images, not text. Usable only behind an OCR stage this platform
does not have. **Out of scope for v1**, and worth saying so explicitly so
nobody re-discovers it as an opportunity.

### 1.4 Third-party derived corpora

Useful for *evaluation and bootstrap*, never as the corpus of record:

- `mii-llm/gazzetta-ufficiale` (HF) — GU legislative texts.
- `ahmeabd/italia-corpus` — all Normattiva legislation as Markdown,
  continuously updated, explicitly aimed at RAG.
- `dossier-legal/italian-legal-corpus`, and the Italian slice of
  **MultiLegalPile** (689 GB multilingual legal corpus).
- The NLLP 2023 corpus of **363,000 act titles, 1988–2022**, multi-label
  classified — the one *measured* volume figure in this document, and a
  ready-made evaluation set for the encoder bake-off in §7.

### 1.5 Volume — to be measured, not guessed

363k act titles over 35 years puts the GU's Parte I at roughly 10k acts/year
across all series. Normattiva's *numbered* subset from 1861 is smaller —
order 10⁵ acts total. **Do not put either number in a denominator.** The
LISTING CONTRACT (`app/acquire/spec.py`) exists because every UI total and
ETA reads the recorded listing; the first thing the plugin's spec must do is
enumerate the origin's listing completely and record it.

---

## 2. The first decision: what *is* a document here?

Three candidate units, and they are not interchangeable:

| unit | identity | mutability | comes from |
|---|---|---|---|
| the **issue** (una Gazzetta) | date + number + series | immutable once published | GU site |
| the **act** (un atto) | URN:NIR / ELI | **revised indefinitely** (multivigenza) | Normattiva |
| the **article** (un articolo) | URN + `~art5` | revised, one comma at a time | Normattiva AKN |

The act is the document; the article is the chunk (§5); the issue is a
metadata attribute, not a document. That falls out of what people actually
ask — nobody asks a question of *Gazzetta n. 156 of 2026*, they ask what the
law says — and it is what the origin's identifiers are built around.

**Consequence, and it is the load-bearing one:** the corpus is
**`Mutability.MUTABLE`**. An act's consolidated text changes when it is
amended. That is the same class as Wikipedia, and it is why `chunk_cdc`
exists — but see §5, because legal text has something Wikipedia does not: a
stable, numbered, semantically real unit.

**Recommendation: two source_ids, shipped in order, not one plugin with two
profiles.** Profiles are subsets of one shape (`incremental`/`exclusive`/
`additive`); the normative corpus and the issue stream are different shapes
with different licences, different mutability and different chunkers.

1. **`normattiva`** — the normative corpus, AKN, MUTABLE. Build this.
2. **`gazzetta`** — the issue stream (concorsi, inserzioni, unnumbered
   decrees), HTML, IMMUTABLE. Only after §10 is settled; it is the half with
   the licence and privacy problems.

---

## 3. Constraints the platform already imposes (checked against the code)

Read this section before designing anything; each item was verified in the
tree, not remembered.

1. **`native_id` must be integer-parseable.** Not a convention — a hard
   dependency. `app/layers.py:120` does `int(row["native_id"])` for every
   document that gets chunked, `app/worker.py:260,583` pass
   `int(row["native_id"])` into `embeddings.upsert_book`, and the Lance
   `book_id` column is an integer. Selection and browse queries
   `CAST(native_id AS INTEGER)` and the worker builds
   `idx_documents_native_int` on that expression.
   **A URN cannot be the `native_id`.** The `uid` is the free-form
   identifier (`"normattiva:urn:nir:stato:legge:2005-03-07;82"`); the
   `native_id` has to be a synthetic, stable, *sortable* integer. Proposal:

       native_id = TTYYYYMMDDNNNNNN
                   TT  act-type code from a fixed, versioned table
                       (01 legge, 02 decreto-legge, 03 decreto.legislativo,
                        04 d.P.R., …)
                   YYYYMMDD  date of the act
                   NNNNNN    its number

   Stable, collision-free (type+date+number is the URN's own key), and
   monotone in time — which the genesis archives' shard-by-id-range and
   every `BETWEEN` predicate in `app/worker.py:407` want. The type table
   must be append-only and versioned: renumbering it re-keys the corpus.

2. **The chunker enters `space_id`** (invariant 1 as amended, amendment 41).
   A new chunker is a new space. Choosing it is a one-way door per
   collection — `space repin` exists but it drops every vector, every
   lexical entry and the verification ledger.

3. **Italian is already wired for lexical search.** `app/config.py:220`
   maps `it → italian` and `app/stemming.py` uses PyStemmer/libstemmer.
   Nothing to add; documents just need `language="it"`.

4. **No vector index yet** (STATUS.md, "Known gaps"). Every semantic query
   is a flat scan: measured 15.8M rows × dim 1024 f16 = **13 s**. Scaling
   that, a Normattiva corpus chunked per article (order 1–5M chunks) costs
   ~0.8–4 s per query at dim 1024, and four times that at dim 4096. Note
   what this does and does not constrain: it is an argument about **which
   dimension gets pinned**, not about which model wins — Matryoshka
   truncation separates the two (§7.1).

5. **GPU boundary** (invariant 7): the app never imports torch. Any encoder
   must be reachable over HTTP — TEI or llama.cpp server — and must pass the
   two-step procedure in `docs/approved-encoders.md` before it serves a
   space.

6. **The licence gate needs a byte-exact `TERMS.txt`** (invariant 9,
   `app/licensing.py`). CC BY 4.0's text plus Normattiva's own terms, saved
   byte-exact, never converted; `.gitattributes` already guards line endings
   for the existing ones and must cover this one.

7. **Discovery never prunes unseen documents** (STATUS.md). Abrogated acts
   would live forever. Normattiva marks abrogation explicitly — map it to a
   tombstone the way `pubmed` maps `DeleteCitation` (a `DocRef` with `error`
   set; the orchestrator upserts the row and every `WHERE error IS NULL`
   gate excludes it, idempotently on every rescan). This is free reuse of an
   existing mechanism and should be in v1, not deferred.

---

## 4. The rendition: a canonical text form for embedding

AKN is an excellent *interchange* format and a poor *embedding input*: the
encoder would see markup, `eId` attributes and namespace noise, and the
tokens spent on them are tokens not spent on the norm. So the corpus is
processed into a canonical text form — the **rendition** — under three
constraints, in this order:

1. **structure preserved** — every partition, article number (`5-bis`
   included), comma, lettera, allegato and rubrica survives, and survives
   *recoverably*, not just visually;
2. **readable in a plain text editor** — someone who opens the file with
   `less` reads a law, not a serialisation;
3. **better for the encoder** — prose, no markup, no attribute soup.

### 4.1 The format

One file per act: YAML front matter, then the text. Markdown-shaped because
that is what "readable in an editor" converges on anyway, but the rules
below are the specification — not "whatever a Markdown writer emits".

```
---
urn: urn:nir:stato:decreto.legislativo:2005-03-07;82
eli: https://www.gazzettaufficiale.it/eli/id/2005/05/16/005G0104/sg
tipo: decreto legislativo
numero: 82
data: 2005-03-07
titolo: Codice dell'amministrazione digitale
gazzetta: {serie: sg, numero: 112, data: 2005-05-16, supplemento: 93}
vigenza: 2026-08-26
stato: vigente
rendition: normattiva/1
fonte: "Normattiva, CC BY 4.0 — testo non ufficiale (fa fede la GU)"
---

# Decreto legislativo 7 marzo 2005, n. 82
Codice dell'amministrazione digitale

## Capo I — Principi generali
## Sezione I — Definizioni, finalità e ambito di applicazione

### Art. 1 — Definizioni

1. Ai fini del presente codice si intende per:
   a) allegato informatico: una parte del documento informatico […];
   b) autenticazione del documento informatico: la validazione […];

2. Ai fini del presente codice si intendono altresì per […].

> nota: comma modificato dall'art. 2 del d.lgs. 13 dicembre 2017, n. 217.

### Art. 2 — Finalità e ambito di applicazione
[…]

### Art. 7 — (abrogato)
```

Rules, all of them load-bearing:

- **`# `** the act's denomination, once. The line under it is its title.
- **`## `** a partition — Libro, Parte, Titolo, Capo, Sezione. The nesting
  depth is carried by the Italian partition *word*, which is canonical in
  legal drafting, **not** by the number of `#` — so a codice nested six
  deep does not run out of heading levels.
- **`### Art. N — Rubrica`** an article. `N` is verbatim from the source,
  `5-bis` and `2-septies` included. **This is the only line the chunker
  cuts on** (§5).
- **`N. `** a comma, at the left margin; `a) ` a lettera, indented three
  spaces; `i) ` a numero, six. Indentation is structural, not decorative.
- **`> nota: `** annotations — amendment history, Normattiva's `((…))`
  modification markers resolved into prose. **Quoted lines are excluded
  from the embedded text** and shown to the reader. This is how "the source
  says this passage was amended" survives without polluting the vector with
  editorial apparatus.
- **`(abrogato)`** an article repealed in place: it keeps its heading, has
  no body, and produces **no chunk** — visible to a reader, absent from
  retrieval.
- **Byte-level discipline**: UTF-8 NFC, LF, no trailing whitespace, exactly
  one blank line between blocks, **no hard wrapping** (a re-flow would make
  every diff meaningless and every re-render a false change).

### 4.2 Where it lives, and what travels

**The rendition is to Normattiva what `pack.py` is to PubMed**: a derived
artifact, built once per act, materialised on disk beside the source,
rebuilt from the source whenever its version changes. That framing settles
three questions at once.

- **What is the corpus of record?** The AKN from the origin. `raw_hash` is
  over *those* bytes, so idempotence and change-detection (invariant 2) key
  on the origin's truth, never on our own rendering of it.
- **What travels to peers?** The AKN. A peer re-renders deterministically
  from the same source at the same version and gets the same bytes —
  shipping the rendition would be shipping something the receiver can
  compute, which is the same policy call amendment 35 made for extras.
- **Why materialise it at all, then?** Because constraint 2 is a real
  requirement, not a nicety: an operator debugging a bad answer needs to
  open the exact text the encoder saw. Parse-time-only rendering makes that
  text unobservable.

### 4.3 Versioning — and the global-knob trap, again

`SpaceComponents` already carries a `cleaning_version` (`app/spaces.py:38`)
— and `app/config.py:59` makes it **one global env knob**. Bumping it for a
Normattiva rendering fix would re-pin every collection on the node, which is
precisely the mistake CLAUDE.md records for `CHUNK_VERSION` ("a global knob
that scopes per-thing"), in a second coat.

The way out needs no amendment: **fold the rendition version into the
chunker's own version.** `akn_article` (§5) owns the whole path from source
XML to chunk, so `AKN_CHUNK_VERSION` covers rendering *and* cutting, and it
is bumped by a change to either. That is not a conflation — any rendition
change genuinely does change the text and genuinely must re-embed. It also
keeps the per-chunker versioning that amendment 39 established for
`ABSTRACT_CHUNK_VERSION`, and leaves every existing pin untouched.

The materialised files carry `rendition: normattiva/<version>` in their
front matter, so a version mismatch is a cheap per-file check and a
re-render, not a corpus-wide guess.

### 4.4 How "structure preserved" stops being an aspiration

A test, not a promise: parse the rendition text back into a structural
skeleton — the ordered list of (partition path, article number, comma
numbers) — and assert it equals the skeleton read from the AKN. Round-trip
of the *skeleton*, not of the bytes; the prose is allowed to lose markup,
the structure is not allowed to lose anything.

Golden fixtures must cover the shapes that break renderers: a codice with
six partition levels, a two-article law, an act with allegati and tables,
an act with repealed articles, an act with `((…))` modification markers,
and an old act available only as XML-NIR rather than AKN.

---

## 5. Chunking: the article is the unit

`chunker_name()` today would route a MUTABLE source to `chunk_cdc`.
Content-defined chunking is the right *answer to the wrong question* here.
It solves boundary drift, which legal text does have — but it solves it
blindly, cutting mid-comma, when the text already carries the exact unit
both the amendment mechanism and the citation convention use.

**Proposal: an `akn_article` chunker, one chunk per `### Art.` block.**

- One chunk per articolo. Its `chapter` field (which `_mk` already carries
  and the reader UI already shows) gets the breadcrumb —
  `"Capo I › Sezione I › Art. 1 — Definizioni"` — so a hit knows where in
  the act it sits without re-reading the act.
- Oversized articles (a codice's art. 2359, a technical allegato) split by
  comma; a comma still oversized splits by CDC *within itself*. All three
  levels stay a pure function of (rendition text, chunker id, chunker
  version) — invariant 3 holds.
- `> nota:` lines and `(abrogato)` bodies are excluded (§4.1).
- **The amendment case, which is the whole point:** amending one comma
  changes that article's chunks and nothing else. Fixed-width chunking
  would re-embed the act from the amendment onwards; CDC would limit the
  blast radius to an arbitrary boundary.
- Acts whose source carries no article structure at all degrade **inside
  the same chunker** to CDC over the flat rendition. Keeping the fallback
  inside `akn_article` rather than declaring a second layer avoids needing
  a new layer *rule* — `app/layers.py` knows only `default` and
  `archive:<id>`, and "has article structure" would be an architecture
  amendment. If the fallback turns out common enough to want separate
  accounting, *that* is when to propose one.

New chunker ⇒ new `AKN_CHUNK_VERSION`, versioned independently exactly as
`ABSTRACT_CHUNK_VERSION` is, and covering the rendition too (§4.3). Needs
the "ask first" approval, §12.

---

## 6. Vigenza: index the current text, serve history by key

The multivigente corpus holds N versions of every amended article. Three
options:

- **(a) index only the *vigente* text** — simple, answers "what does the law
  say now", silently wrong for any question about a past state.
- **(b) index every version** — N× the corpus, and RRF happily returns five
  near-identical versions of the same article as five hits. Actively bad.
- **(c) index the vigente text; expose history as a ReferenceSource.**

**Recommend (c)**, and note that invariant 4 already has the category for
it: a ReferenceSource lookup is *facts-by-key*, it goes into the LLM context
as a pinned block, and it **never enters RRF**. "The text of art. 5 d.lgs.
82/2005 as in force on 2019-06-01" is a key lookup, not a ranked list. So is
"what does art. 5 say" — a citation in the query should resolve, not
compete.

That gives a natural phase 2 for this plugin:

    app/refs/normattiva.py
      RefTable "atti"     PK urn      — type, date, number, title, eli, status
      RefTable "articoli" PK urn#art  — text, in-force-from, in-force-to
      RefLink  citation: articoli.cites_urn -> atti.urn

plus an **EntityResolver over citation strings** — `art. 5, comma 3, d.lgs.
7 marzo 2005, n. 82` / `l. 633/1941` / `DPR 445/2000` all normalising to a
URN. Italian legal citation is highly regular; this is a tractable parser
and it is where most of the retrieval quality in this domain actually lives.

---

## 7. Encoders

**The encoder is chosen on retrieval quality on Italian legal text.** What
this node happens to be running is not an input to that decision — it is a
consequence of it, and a procurement problem (`approved-encoders.md`) to be
solved *after* the winner is known.

One filter does apply before quality, and it is not about hardware: **the
space must stay reproducible.** `approved-encoders.md` sets the bar at a
peer reproducing the space's vectors at cosine ≥ 0.999 from pinned weights.
A proprietary API model cannot meet that — it can be revised or withdrawn
under a pinned collection, and no peer can ever reproduce it. So closed
models are excluded as *space owners*, while remaining useful as a **quality
ceiling** in the bake-off: measuring against them tells you what the open
choice is leaving on the table.

### 7.1 Candidates, ranked by measured quality

| model | MMTEB mean / retrieval | dim (MRL) | ctx | weights |
|---|---|---|---|---|
| **KaLM-Embedding-Gemma3-12B-2511** (Tencent, 11.8B) | **72.32 / 75.66** — #1 | 3840 → 2048/1024/512/… | 32k | open (`tencent-kalm-embedding-community`) |
| **Qwen3-Embedding-8B** (Alibaba) | 70.58 / 70.88 | 4096 → 2560/1024/… | 32k | open (Apache-2.0) |
| Qwen3-Embedding-4B / 0.6B | lower (0.6B: 64.34) | 2560 / 1024, MRL | 32k | open |
| gemini-embedding-001 | 68.32 / 67.71 | 3072, MRL | 2k | **closed — reference only** |
| voyage-law-2 | #1 on MMTEB *legal* retrieval | 1024 | 16k | **closed — reference only** |
| BAAI/bge-m3 | well below the leaders | 1024 | 8192 | open |
| intfloat/multilingual-e5-large | below | 1024 | 512 | open |
| jurifindit/DAR-legal-it | not benchmarked on MMTEB | unverified | unverified | open — **the Italian-legal domain specialist**, §7.3 |
| dlicari/Italian-Legal-BERT | — | 768 | 512 | **not an embedder** |

**The two to measure are KaLM-Gemma3-12B and Qwen3-8B.** Both are open,
both take instructions (real asymmetric query/document handling), both have
32k context — an entire codice article with all its commi fits many times
over, so the rendition never has to be mutilated to fit — and, decisively,
**both support Matryoshka truncation**.

That last point kills the objection I raised in the first draft. I argued
against high-dimensional encoders on flat-scan cost (§3.4): dim 4096 is 4×
the bytes per row, and there is still no vector index. With MRL that is not
a trade at all — **you can pin dim 1024 out of a 3840- or 4096-dim model**
and keep most of the quality of the larger model, because MRL trains the
prefix to be a usable vector on its own. The truncation is part of the
pinned recipe, so it is decided once, in the pilot, and measured like
everything else.

**Why not Italian-Legal-BERT, despite the name.** It is a masked language
model — ITALIAN XXL BERT continued on ~3.7 GB of jurisprudence. MLM never
asks unrelated texts to sit apart, so mean-pooled vectors collapse into a
narrow cone. This codebase has already paid for that lesson and written it
down: `dna-search-axes.md` §2, where unrelated DNA sequences score 0.98
cosine for exactly this reason. Domain pretraining without a contrastive
objective is not a retrieval model — it is a good *base* for a fine-tune.

**bge-m3 and mE5-large are now the floor, not the recommendation.** They
are what the bake-off has to *beat*, and they are worth keeping in it for
exactly that reason — if the leaders do not beat them on Italian legal
retrieval, that is a finding, and a cheap collection.

### 7.2 The measurement that actually decides

MMTEB's multilingual mean is not Italian legal retrieval, and choosing on it
alone would repeat the mistake of choosing on availability — a different
proxy, the same error. The evaluation set the choice should hinge on:

**JuriFindIT** — Dalla Noce, Colla, Farhang Doust, De Mattei & Bacciu,
*Findings of EACL 2026*, pp. 4223–4241. The first **Statutory Article
Retrieval** dataset for Italian, and the first to explicitly encode
cross-article references extracted from the national legal code.

| property | value |
|---|---|
| task | SAR — natural-language question → the statutory articles that answer it |
| macro-areas | civil, criminal, anti-money-laundering / counter-terrorism, privacy |
| questions | **895 expert-authored** + **169,301 generated** |
| target articles | **> 23,000** |
| per-entry fields | question, fine-grained topics, broader normative area, **list of relevant statutory article IDs** |

Why this one and not a general benchmark: the ground truth is a **list of
article IDs**, which is exactly the chunk unit §5 proposes. Recall@k against
it measures the thing this plugin is actually for, with no mapping layer in
between. The 895 expert questions are the honest metric; the 169k generated
ones are training-scale, and using them as the *test* set would measure
agreement with the generator.

**Availability: NOT VERIFIED — this is an open item, see §12.4.** What is
established: the paper is in the ACL Anthology
(`2026.findings-eacl.221`), and a Hugging Face organisation `jurifindit`
exists and has published at least one artifact from the work
(`jurifindit/DAR-legal-it`, §7.1). What could **not** be established from
this session — the egress policy blocks every page fetch, so only search
summaries were readable — is the dataset's own repository URL, its licence,
and its file format. The per-entry description implies a JSON/JSONL
question file plus an article collection, i.e. a conventional qrels shape,
**but that is inference, not verification.** Do not plan the pilot around
it until someone has actually opened the page.

Verification is three commands from any unrestricted machine:

    # 1. does the org publish the dataset, and under what licence?
    curl -s https://huggingface.co/api/datasets?author=jurifindit | jq .
    curl -s https://huggingface.co/api/models?author=jurifindit  | jq .
    # 2. the paper's own data-availability statement and repo link
    curl -sL https://aclanthology.org/2026.findings-eacl.221.pdf -o jurifindit.pdf

**If it turns out not to be downloadable**, the fallback is not "give up on
measuring" — it is a smaller, hand-built set, and the plugin's own corpus
makes that cheap: ~50–100 questions written against known articles of the
codice civile and the CAD, with the article ID as ground truth. Lower
statistical power, same shape, and it is the set we would want anyway
(§7.2's third bullet). Secondary signals, both also unverified for
availability: **IT-RAG-Bench** (synthetic Italian, includes codice civile
articles) and the **NLLP 2023** 363k-title classification corpus (§1.4),
which is not retrieval but does give labelled Italian legislative text.

Procedure once a set exists: pilot collection of ~5,000 acts, one per
candidate encoder, recall@k and nDCG@10, `docs/calibration.md` to set the
tolerance, then delete the pilots and create the real collection against the
winner. The pilots are cheap; a wrong pin is a re-index of the whole corpus.

### 7.3 Domain fine-tuning: no longer hypothetical

The first draft filed "fine-tune an encoder on Italian law" under future
work. The JuriFindIT search turned up that someone has already done it and
released the result.

**`jurifindit/DAR-legal-it`** — a `sentence-transformers` dense-retrieval
model for Italian, GTE architecture, trained with
`CachedMultipleNegativesRankingLoss` on ~24,661 pairs, published by the
JuriFindIT authors. The paper's own pipeline goes further: dense encoders
combined with a **heterogeneous legislative graph**, reported as
consistently improving over prior SAR approaches.

Two consequences for this proposal:

1. **It belongs in the bake-off** (§7.1), as the domain-and-language
   specialist against the general leaders. Expect it to punch above its
   size on in-domain queries and below it elsewhere — a ~0.6B-class GTE
   base fine-tuned on 24k pairs is not going to beat an 8–12B general model
   on breadth. That is a hypothesis to measure, not to assume, and it is
   cheap to include. Its base model, dimension and licence are unverified
   for the same reason as the dataset (§7.2).
2. **The legislative graph corroborates §6.** The paper's gain comes partly
   from modelling cross-article references explicitly — which is exactly the
   `RefLink citation: articoli.cites_urn -> atti.urn` edge proposed there.
   That raises the ReferenceSource from "nice phase 2" to "the published
   state of the art says this is where the gain is".

**LEMUR** (EACL SRW 2026) points the same way from the EU side: fine-tuning
multilingual embedding models contrastively on EU legislation consistently
improves top-k retrieval, and the gain **transfers to languages not in the
fine-tuning set** — it is learning content-level legal structure, not
language cues.

The cost stays what it was, and it is worth restating because "a fine-tuned
model exists" makes it tempting: a fine-tuned encoder is a **different
space**. Adopting one later does not migrate — it re-embeds from zero, or it
lives as a second collection. Which is an argument for including
DAR-legal-it in the *pilot*, where switching is free, rather than adopting
it after the real collection exists.

### 7.4 The reranker

Reranking deserves more than a footnote here, because legal answers are won
or lost at precision@5 and because the retrieval shape this plugin produces
is unusually favourable to it: chunks are articles, so a reranker judges a
self-contained legal unit rather than an arbitrary window.

**What is already wired.** `app/retrieval.py:77` posts
`{"query", "texts"}` to `RERANK_URL` and reads back TEI's
`[{"index", "score"}]` — a pointwise cross-encoder contract. It runs over
`max(k * 4, 40)` candidates after RRF (`retrieval.py:234` single-collection,
`:333` federated), and it is **off by default** (empty `RERANK_URL`).

**And what it correctly does not touch:** pinned facts. `_build_pinned_facts`
runs beside the ranked path, never through RRF and never through `_rerank`,
and the prompt tells the model they are "facts, not ranked evidence — use
them but do not cite them with [n]" (`retrieval.py:367`). That is invariant 4
holding at the query layer, and it is exactly what the citation resolver of
§6 needs: a resolved article arrives pinned and unranked, and no reranker
gets to demote it below a passage that merely mentions it.

#### Candidates

| model | multilingual | shape | size | notes |
|---|---|---|---|---|
| **Qwen3-Reranker-8B** | **68.97 MIRACL** | pointwise generative | 8B | highest raw score; 32k ctx; Apache-2.0 |
| **jina-reranker-v3** | 66.50 MIRACL (18 langs), 61.94 nDCG@10 BEIR | **listwise** | ~0.6B | +5.43% over bge-v2-m3 at the same scale; up to 64 docs in one 131k window |
| Qwen3-Reranker-4B / 0.6B | below the 8B | pointwise | 4B / 0.6B | the realistic latency rungs |
| bge-reranker-v2-m3 | 67.64 MIRACL | cross-encoder | 568M | the baseline to beat; Apache-2.0 |
| mxbai-rerank-v2 | — | cross-encoder | small | fast Apache-2.0 option |

Two shapes, and the difference is not cosmetic. **Pointwise** scores each
candidate independently — 40 forward passes per query, which at 8B is
seconds, not milliseconds. **Listwise** (jina-v3) puts the whole candidate
set in one context and ranks them against each other; 40 candidates fit
inside its 64-document window comfortably, so the whole rerank is one
forward pass of a 0.6B model. For a query path where the user is waiting,
that is the difference between a reranker you can leave on and one you
cannot. The client's contract would need a small adapter — it expects a
score per text, and a listwise model returns an ordering.

#### Three things in the current implementation that would bite this plugin

1. **`h["text"][:2000]` is a hard truncation** (`retrieval.py:83`). At the
   ~5 chars/token this repo measured, that is ~400 tokens — so a reranker
   with a 32k context would judge a long articolo on its first two
   paragraphs while the *encoder* saw the whole thing. Legal articles run
   past 2000 characters routinely. The constant should become a per-model
   budget before the reranker is trusted on this corpus.
2. **A failing reranker degrades silently** (`except Exception: return
   hits[:top_n]`). Degrading to RRF order is the right behaviour on a query
   path — but nothing records that it happened, so a skipped rerank and a
   rerank that changed nothing look identical in the timings. One flag in
   the payload fixes it, and this repo's own rule applies: an honest "not
   done" is a value; a blank is indistinguishable from success.
3. **`RERANK_URL` is Tier-1 env and node-global** — it is not in
   `EmbedRecipe` and not on `Collection`, so unlike the encoder it cannot be
   set per collection. For a federated query producing one final ordering
   across collections that is arguably right; it does mean choosing an
   Italian-legal-tuned reranker is a decision for the *whole node*, not for
   this collection. Worth knowing before assuming it is free.

#### The asymmetry that decides how much care to spend

**The reranker is not part of `space_id`.** Swapping it costs one config
change and zero re-embedding, where swapping the encoder costs the corpus.
So: the encoder gets the careful, measured, one-way decision (§7.2); the
reranker gets tried, swapped and re-tried freely, on the same JuriFindIT
set, *after* the collection exists. Do not hold up the collection for it —
but do measure it, because on an article-level corpus it is the cheapest
quality left on the table.

One option worth pricing, since the node already runs it: **listwise
reranking with the local chat model** over `CHAT_URL`. No new service, and
LLM listwise reranking is strong — at the cost of query latency measured in
seconds and of an ordering that is not reproducible run to run. Probably
a research baseline for "how much is the reranker leaving on the table",
not a production path.

**And do not skip the lexical leg.** Legal queries are citation- and
term-heavy, Italian stemming is already wired (§3.3), and RRF over
FTS + dense is the platform's default for good reason. Expect the FTS leg to
carry more weight here than in any existing collection — and expect the
reranker to matter most exactly where it does not: when a query names no
citation at all.

## 8. Acquisition spec sketch

Shape only — the fields marked `# VERIFY` are the ones §12 needs answered
from the API specification PDF before this can be written for real.

```python
NORMATTIVA_SPEC = AcquisitionSpec(
    source_id="normattiva",
    transport=Transport.HTTPS,
    endpoints=(
        Endpoint(transport=Transport.HTTPS,
                 url="https://api.normattiva.it/...",   # VERIFY: base + auth
                 max_conns=2,                            # etiquette: a public service
                 static_files=_predefined_collections()),# VERIFY: bulk unit
    ),
    layout="container-files",      # VERIFY: if collections are archives of AKN
    filters=("*.akn.xml", "*.zip"),
    checksums=ChecksumSource(kind="none"),      # VERIFY: any published digests?
    delta=DeltaSpec(mode="full-resync"),        # VERIFY: incremental feed?
    etiquette=RateLimit(max_conns=2),
    profiles={
        "demo":    Filter(max_files=...),       # one code, e.g. the CAD
        "starter": Filter(include=("codici/*",)),
        "full":    Filter(),
    },
    profile_mode="incremental",
    estimated_bytes={...},                      # VERIFY: probe, never guess
    license_class=LicenseClass.YELLOW,          # §10 — operator's call
    terms_text_path=... / "plugins" / "normattiva" / "TERMS.txt",
)
```

Two rules this spec must not violate, both already paid for elsewhere in
this codebase: the listing must be **enumerated completely** every pass (the
LISTING CONTRACT — every denominator downstream reads it), and no byte moves
outside the central token bucket (invariant 5) — no per-plugin rate limiting
as a substitute.

If the API turns out to be per-act only, with no bulk unit, then **the
plugin must not fetch 10⁵ acts one HTTP request at a time**. In that case
build the corpus once with a supervised, throttled harvest into a
hand-populated collection and treat the origin as delta-only afterwards —
which is a different, and much less pleasant, plugin. Answering this is why
§12.1 is first.

---

## 9. Module layout

Following the existing plugins' shape exactly:

    app/plugins/normattiva/
        __init__.py        SOURCE = NormattivaSource()
        source.py          discover / fetch / parse   (~200 lines, cf. wikipedia)
        akn.py             Akoma Ntoso + XML-NIR -> the structural skeleton
        rendition.py       skeleton -> the canonical text of §4; owns the
                           format's rules and its byte-level discipline
        urn.py             URN:NIR <-> ELI <-> (type, date, number); native_id
                           synthesis (§3.1); citation-string parsing (§6)
        TERMS.txt          CC BY 4.0 + Normattiva terms, byte-exact
    app/acquire/specs.py   + NORMATTIVA_SPEC, + registry entry
    app/collections.py     + PLUGIN_REGISTRY["normattiva"]
    app/chunking.py        + chunk_akn_article, AKN_CHUNK_VERSION
    app/refs/normattiva.py phase 2 — the ReferenceSource of §6
    tests/
        test_normattiva_urn.py         URN <-> native_id, round-trip, collisions
        test_normattiva_akn.py         AKN + XML-NIR -> skeleton, golden fixtures
        test_normattiva_rendition.py   golden renditions; SKELETON ROUND-TRIP
                                       (§4.4); byte discipline; the six shapes
        test_akn_article_chunker.py    golden chunks; notes and (abrogato)
                                       excluded; version isolation (only this
                                       space's id moves)
        test_normattiva_tombstones.py  abrogation -> tombstone, idempotent

`CanonicalDoc` maps cleanly:

    uid        "normattiva:urn:nir:stato:legge:2005-03-07;82"
    title      the act's official title
    creators   the issuing authority
    language   "it"
    date       date of the act (not of publication — that goes in extra)
    subjects   EuroVoc descriptors where present
    keys       URN, ELI, codice redazionale, GU issue — the doc_keys the
               federation layer joins on
    extra      gazzetta issue/date/series, in-force status, vigenza date
    sections   [("Art. 1", "…"), ("Art. 2", "…"), …]

Locator: `"<container path>::<record index>"` if the origin's bulk unit is a
multi-act archive — byte-for-byte the pubmed pattern, and the reason that
pattern is opaque-by-design.

---

## 10. Licence and privacy classification

**Normattiva (CC BY 4.0): propose YELLOW.** Green means no gate at all.
CC BY imposes an attribution obligation that follows the data into every
redistribution — and this platform redistributes, by design, over P2P.
Yellow costs one acknowledgement (a timestamp and a sha256 of the terms
text) and makes the obligation visible at exactly the moments amendment 36
cares about: discovery, import, exchange. Green is defensible; yellow is
cheap and honest. **Operator's call (§12.2).**

Also worth recording in the plugin's docstring: Normattiva's own notice says
its texts are **not official** — the printed Gazzetta prevails. A RAG system
that answers legal questions should surface that, not bury it.

**The GU issue stream: not classified here, and that is deliberate.** Two
unresolved problems, both of which is why §2 puts it second:

- **Terms.** The IPZS site's conditions of use and `robots.txt` could not be
  read from this session (egress blocked). The GU has been free in digital
  form since 2013 and Art. 5 l. 633/1941 excludes official State acts from
  copyright — but the *database* is a separate right (Art. 11, plus the sui
  generis database right), and that is what a bulk harvest touches. Read the
  terms before writing a fetcher.
- **Personal data.** Serie speciale n. 4 (concorsi) and Parte II
  (inserzioni) contain names of individuals — candidate lists, results,
  bankruptcy and succession notices. That is `PrivacyClass.RESTRICTED` at
  best, it is a GDPR question and not a licensing one, and "it was already
  public" is not the end of the analysis. **Do not put concorsi into a
  peer-exchanged collection without a decision on this.**

---

## 11. Build order

1. **Verify §12.** No code before it.
2. `urn.py` + `akn.py` + `rendition.py` + tests, against a handful of
   hand-downloaded acts — pure functions, no network, no manifest. The
   skeleton round-trip test (§4.4) lands here, not later: it is what makes
   "structure preserved" checkable rather than claimed.
3. `source.py` over a hand-populated corpus directory (the `adoption` path
   already supports files that are simply on disk). This gets a real
   collection indexing without touching acquisition — and the materialised
   renditions are readable on disk from this step onward.
4. **Pilot: ~5,000 acts, one collection per candidate encoder**, scored on
   JuriFindIT (§7.2). Measure. Decide. Delete the pilots.
5. `akn_article` chunker with its own version, once §12.3 approves it.
6. `NORMATTIVA_SPEC` and the acquisition path, once §12.1 is answered.
7. Full collection; watch the worker log as a time series before optimising
   anything (the first benchmark, every time).
8. **Reranker**, once the collection is real and queryable: fix the
   2000-character truncation, then measure jina-reranker-v3 (listwise, one
   pass) against Qwen3-Reranker and bge-v2-m3 on the same JuriFindIT set —
   §7.4. Deliberately after the collection, because unlike the encoder it
   costs nothing to change later.
9. Phase 2: `refs/normattiva.py` + citation EntityResolver — §6 — which is
   where the retrieval quality is.

---

## 12. What a human has to decide before this starts

1. **The bulk shape of the Normattiva OpenData API** — read
   `dati.normattiva.it/assets/come_fare_per/API_Normattiva_OpenData.pdf`.
   Three answers needed: is there a bulk download unit (and how big), is
   there an incremental/delta feed or only full re-listing, and is there
   auth/rate limiting? §8 cannot be written without these, and the answer to
   the first one decides whether this is a pleasant plugin or an unpleasant
   one.
2. **Licence class for `normattiva`: yellow or green?** §9. If yellow,
   capture `TERMS.txt` byte-exact — CC BY 4.0's text plus Normattiva's own
   conditions — and add it to `.gitattributes` so no checkout ever converts
   its line endings.
3. **Approval for the rendition format and a new chunker** — the §4 text
   format plus `akn_article` / `AKN_CHUNK_VERSION` (§5), which covers both.
   This is the "ask first" case in CLAUDE.md: it changes cleaning AND
   chunking, and it enters a `space_id`. Worth reviewing the §4.1 example
   as a *format decision*, because every act in the corpus will look like
   it and changing it later re-embeds everything.
4. **Agreement that the pilot happens before the real collection exists**,
   and on what counts as winning (§7.2 proposes recall@k and nDCG@10 on
   JuriFindIT as primary). Also: **which dimension gets pinned** — MRL
   makes that a separate, measurable decision from which model wins, and
   it is the one that sets query latency until there is a vector index.
5. **Whether the GU issue stream is in scope at all**, given §10's privacy
   problem. If it is not, say so in this document and stop re-discovering it.
6. **Verify JuriFindIT's availability, licence and format** (§7.2), from a
   machine that can actually fetch pages. This gates the encoder bake-off:
   with it, the choice is measured on article-level ground truth; without
   it, the fallback is a hand-built 50–100 question set and the decision
   rests on weaker evidence. Same visit answers §7.3's open fields for
   `jurifindit/DAR-legal-it` (base model, dimension, licence).

## Sources

- Normattiva OpenData: <https://dati.normattiva.it/>, its
  [API specification](https://dati.normattiva.it/assets/come_fare_per/API_Normattiva_OpenData.pdf),
  [ELI implementation in Italy](https://dati.normattiva.it/assets/come_fare_per/ELI_implementation_in_Italy.pdf),
  [Akoma Ntoso structuring note](https://dati.normattiva.it/assets/come_fare_per/Portale_Normattiva_2.0-Strutturazione_degli_atti_in_Akoma_Ntoso_26-07-2021.pdf)
- Open-data transition: [ANSA, 2026-02-24](https://www.ansa.it/sito/notizie/economia/2026/02/24/normattiva-al-via-il-portale-open-data_98d61402-72d0-4351-a7c9-94df6d08a8eb.html),
  [Forum Italia thread](https://forum.italia.it/t/i-dati-di-normattiva-sono-open-dal-1-gennaio-2026/48642)
- Gazzetta Ufficiale: <https://www.gazzettaufficiale.it/>,
  [ELI register — Italy](https://eur-lex.europa.eu/eli-register/italy.html)
- Au.G.U.Sto: [Senato project note](https://www.senato.it/application/xmanager/projects/leg18/file/repository/relazioni/biblioteca/moduli/AuGUSto_GU_Regno1860-1946.pdf)
- Derived corpora: [mii-llm/gazzetta-ufficiale](https://huggingface.co/datasets/mii-llm/gazzetta-ufficiale),
  [ahmeabd/italia-corpus](https://github.com/ahmeabd/italia-corpus),
  [ondata/normattiva_2_md](https://github.com/ondata/normattiva_2_md),
  [MultiLegalPile](https://arxiv.org/html/2306.02069v3),
  [Italian Legislative Text Classification for Gazzetta Ufficiale (NLLP 2023)](https://aclanthology.org/2023.nllp-1.6/)
- Encoder quality: [MTEB leaderboard, 2026](https://www.codesota.com/benchmarks/mteb),
  [tencent/KaLM-Embedding-Gemma3-12B-2511](https://huggingface.co/tencent/KaLM-Embedding-Gemma3-12B-2511)
  and [KaLM-Embedding-V2](https://arxiv.org/pdf/2506.20923),
  [Qwen3-Embedding](https://qwenlm.github.io/blog/qwen3-embedding/) /
  [QwenLM/Qwen3-Embedding](https://github.com/QwenLM/Qwen3-Embedding)
  (MRL: [vLLM `is_matryoshka`](https://github.com/vllm-project/vllm/issues/20899)),
  [Gemini Embedding](https://arxiv.org/pdf/2503.07891),
  [Benchmarking multilingual dense retrieval, 2026](https://arxiv.org/pdf/2605.23618)
- Italian legal evaluation: [JuriFindIT (Findings of EACL 2026)](https://aclanthology.org/2026.findings-eacl.221/)
  — Dalla Noce, Colla, Farhang Doust, De Mattei, Bacciu, pp. 4223–4241;
  [`jurifindit` on Hugging Face](https://huggingface.co/jurifindit/DAR-legal-it);
  IT-RAG-Bench (via the benchmarking paper above)
- Legal-domain fine-tuning: [LEMUR (EACL SRW 2026)](https://aclanthology.org/2026.eacl-srw.18.pdf),
  [arXiv version](https://arxiv.org/html/2602.09570)
- Why not a domain MLM: [ITALIAN-LEGAL-BERT](https://ceur-ws.org/Vol-3256/km4law3.pdf),
  and this repo's own [`dna-search-axes.md`](dna-search-axes.md) §2
- Rerankers: [Qwen3-Reranker](https://qwenlm.github.io/blog/qwen3-embedding/),
  [jina-reranker-v3](https://jina.ai/en-US/models/jina-reranker-v3/),
  [reranker comparisons, 2026](https://futureagi.com/blog/best-rerankers-for-rag-2026/),
  [Mixpeek's tested ranking](https://mixpeek.com/curated-lists/best-rerankers)
