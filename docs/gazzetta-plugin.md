# Gazzetta Ufficiale / Normattiva — plugin feasibility and design

**Status: PROPOSAL, nothing built.** This is the research pass that has to
happen before a `gazzetta` plugin is written: what material exists, in what
form, what the platform's own constraints allow, and which encoder the
collection would be pinned to *forever* (invariant 1). The decisions in
here belong to the operator, not to an implementer — they are collected in
§12. Do not start coding before §12 is answered; the expensive mistake in
this codebase has always been pinning a space before measuring it.

Written 2026-08-26.

**Standing directive (operator, 2026-08-26):** where a choice trades answer
quality against cost, this document takes the **quality** side; where the
quality path is *definitely* more expensive, it is flagged for confirmation
rather than silently downgraded. With one refinement the operator added when
the first such choice came up: **latency is part of quality, not a cost to
be traded against it** — "no one will use a system that takes ages to answer
on reasonable hardware", and a system nobody uses has no answer quality at
all. So the rule is: maximise quality subject to a stated latency budget,
and never trade *fidelity* (what is stored, what is pinned) for latency when
an engineering fix buys the latency back instead.

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

**Primary source, supplied by the operator 2026-08-26: the portal's own
"Dati disponibili" page.** This section is no longer inference from search
summaries, with one caveat noted at the end.

**Content and versions.** Every *numbered* State normative act published in
the Gazzetta Ufficiale from 1861 to today, in three distinct renditions the
portal names explicitly:

| version | what it is |
|---|---|
| **originaria** | the act as first published |
| **vigente ad una qualsiasi data** ("point-in-time") | the consolidated text in force on a date the caller chooses |
| **multi-vigente** | all versions through time |

That the origin serves **point-in-time as a query parameter** is a load-
bearing discovery, and §6 is rewritten around it.

**Licence, and its history — which matters more than it looks.** The
portal launched in a *sperimentale* phase under **CC BY 4.0 NC**
(non-commercial), moved to plain **CC BY 4.0** for the window 1 July –
31 December 2025, and from **1 January 2026** offers all three versions
under CC BY 4.0 with attribution ("medesima paternità"). So the licence
attached to a copy depends on **when it was fetched** — see §10, where this
changes the classification argument.

**Bulk unit: the *collection* (`collezione`)**, in two flavours, which map
cleanly onto `AcquisitionSpec`:

| flavour | how it is obtained | formats |
|---|---|---|
| **precompiled** | downloaded straight from the portal home page | AKN, XML-NIR, JSON, HTML, PDF, EPUB, RTF, ELI URI |
| **on demand** | computed from the user's own search parameters | AKN, XML-NIR, JSON, HTML, ELI URI |

Each is available in originaria / vigente / multivigente. Note the asymmetry:
the human-facing formats (PDF, EPUB, RTF) exist only for precompiled
collections — which is fine, since none of them is an ingestion input.

**Two normative specifications are published** and are direct inputs to the
plugin, not background reading:

- the profile by which **OASIS LegalDocML / Akoma Ntoso** was adopted for
  Normattiva's acts — the contract `akn.py` parses against;
- the **permanent ELI URI** specification. The chosen URI form is the
  *"maggiormente parlante"* one, **based on the act's coordinates** — live
  examples confirm the shape, and it decomposes exactly along ELI's FRBR
  axes:

      /eli/stato/DECRETO_LEGISLATIVO/2017/03/06/40/CONSOLIDATED/20180504
           └ jurisdiction └ type      └ date   └n  └── Expression ────┘
           └────────── Work: the act's coordinates ─┘

  ELI is adopted as the **general template for EU legal plugins** in
  [`eli-legal-plugins.md`](eli-legal-plugins.md), which also checks it
  against the pack architecture.

That last detail independently corroborates §3.1: the origin itself treats
(type, date, number) as the act's identifying coordinates, which is exactly
the tuple the proposed integer `native_id` is synthesised from.

**Planned, not yet available:** act metadata exported as **DCAT-AP_IT**. If
it lands it is the natural satisfier of the LISTING CONTRACT (§8) — a
machine-readable catalogue of everything published, which is what every
honest denominator downstream needs.

**The caveat.** The page describes its phases in the future tense ("sarà
possibile", "si prevede di aggiungere"), i.e. it was written during 2025,
while ANSA reports the portal going live on **2026-02-24** — *after* the
announced 1 January 2026 transition. Either the page is stale or the
timeline slipped. **Confirm the current licence and functional state on the
live portal before relying on either** (§12.1).

This is the origin a plugin should target: structured, identified,
versioned, machine-readable, bulk-oriented and openly licensed — every
property the other three sources lack.

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

7. **Repeal is metadata, not a tombstone — and an earlier draft of this
   document got that wrong.** It proposed mapping abrogation onto PubMed's
   `DeleteCitation` tombstone (a `DocRef` with `error` set, permanently
   excluded by every `WHERE error IS NULL` gate). Adopting ELI
   ([`eli-legal-plugins.md`](eli-legal-plugins.md) §5.7) shows why that
   loses legally relevant content: `eli:repeals` is a **relation between
   Works** and `in_force` a **property of an Expression**. A repealed act
   still governed facts occurring before its repeal, and is asked about
   constantly. PubMed's DeleteCitation is a different assertion entirely —
   the publisher saying a record should never have existed.
   So: a repealed act stays indexed and retrievable, carries
   `in_force: false` and its `repealed_by` edge, and is **labelled**;
   filtering by force-status is a query concern. Tombstones are reserved
   for records actually *withdrawn* from the origin's database.
   The underlying platform gap stands: discovery never prunes unseen
   documents, so a withdrawn act would still live forever.

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
- **What counts as "oversized" is measured, not assumed.** Both leading
  encoders carry 32k of context (§7.1), so almost every articolo fits whole
  — but a long article compressed into one vector loses specificity, and
  which effect wins is an empirical question, not a reasoned one. The pilot
  therefore carries a granularity arm: *whole article, never split* against
  *split at N tokens*, for two or three values of N, scored on the same eval
  set. Cheap (it reuses the pilot infrastructure) and it settles a parameter
  that is otherwise pinned on a guess.
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

## 6. Vigenza: index the vigente text, resolve history against the origin

The multivigente corpus holds N versions of every amended article. Three
options:

- **(a) index only the *vigente* text** — simple, answers "what does the law
  say now", silently wrong for any question about a past state.
- **(b) index every version** — N× the corpus, and RRF happily returns five
  near-identical versions of the same article as five hits. Actively bad.
- **(c) index the vigente text; serve history by key.**

**Recommend (c)**, and invariant 4 already has the category: a lookup by key
is *facts-by-key*, it enters the LLM context as a pinned block, and it
**never enters RRF**. "The text of art. 5 d.lgs. 82/2005 as in force on
2019-06-01" is a key lookup, not a ranked list. So is "what does art. 5
say" — a citation in the query should **resolve**, not compete.

**What the primary source changes (§1.1).** The earlier draft assumed we
would have to *store* every version to answer historical questions. We do
not: the origin serves **"vigente ad una qualsiasi data" as a query
parameter**. Point-in-time is the origin's capability, not ours to rebuild.
That makes option (c) substantially cheaper and moves the design onto
machinery this project already has:

> Historical vigenza is the **freshness-check pattern** of `pack-format.md`
> §12, pointed backwards in time instead of forwards. Rate-limited, **opt-in
> per query**, the fetched text **pinned as a fact** beside the indexed one,
> never re-embedded and never fed to the index.

That precedent settles three things at once. It keeps the origin out of the
default query path, which the §7.5 latency budget requires — a network round
trip cannot sit inside a p95 of 2 s. It reuses an existing, rate-limited,
opt-in mechanism rather than inventing one. And "pinned as a fact" is
exactly the invariant-4 treatment a point-in-time answer deserves.

**What the ReferenceSource then holds** is the cheap, stable part —
identity and structure, not text-through-time:

    app/refs/normattiva.py
      RefTable "atti"     PK urn      — type, date, number, title, eli, status
      RefTable "articoli" PK urn#art  — in-force-from, in-force-to, cites
      RefLink  citation: articoli.cites_urn -> atti.urn

Note what left the schema: `articoli.text` per version. The dates and the
citation graph are small, derivable and worth holding locally; the text of
every historical version is large, and one API call away when actually
asked for. **Multivigente stays an acquisition option, not a requirement** —
a node that wants offline historical answers can fetch it; a node that does
not, still answers the question with a round trip and an honest label.

Plus an **EntityResolver over citation strings** — `art. 5, comma 3, d.lgs.
7 marzo 2005, n. 82` / `l. 633/1941` / `DPR 445/2000` all normalising to a
URN. Italian legal citation is highly regular; this is a tractable parser,
and per JuriFindIT's own result (§7.3) modelling cross-article references is
where the measured gain in this domain lives.

**Open, and it belongs to acquisition rather than to this section:**
whether the corpus is fetched as *vigente-at-fetch-time* (a moving target
that changes under a pinned snapshot) or as *originaria + the modification
graph*. The first is simpler and is what "index the vigente text" assumes;
the second is reproducible in a way a moving target is not, which matters
for a signed pack whose consumers must reproduce it. §12.7.

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

**Correction to an earlier draft of this section.** It proposed a pilot of
"~5,000 acts". That is wrong, and not by a little: recall@k is only defined
if the corpus *contains* the articles the eval set points at. JuriFindIT
targets **> 23,000 statutory articles** across civil, criminal, AML/CTF and
privacy law, so a pilot of arbitrary acts would score near zero for reasons
that have nothing to do with the encoder.

The pilot corpus is therefore **defined by the eval set, not by a size**:
the codici (civile, penale, and the two procedure codes), the AML/CTF
legislation, and the privacy corpus — every act containing a target article,
plus enough neighbouring material to make retrieval non-trivial. Size falls
out of that; it is not an input. A first sanity check before any measurement
is a **coverage assertion**: every target article ID in the eval set resolves
to a chunk in the pilot, and the run aborts if it does not.

Procedure, once the corpus is defined: one pilot collection per candidate
encoder, recall@k and nDCG@10 on the 895 expert questions,
`docs/calibration.md` to set the tolerance, then delete the pilots and create
the real collection against the winner. The pilots are cheap; a wrong pin is
a re-index of the whole corpus.

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

**Where this decision actually sits (operator, 2026-08-26).** Benchmarking
is a **separate activity** from building the collection, and the choice of
embedder belongs to the **collection's creator** — pinning freezes it,
because from that moment the community contributes against it. The
collection therefore publishes its **embedder score**, so any peer can
re-run it and, disagreeing, ask the author for a review; a review that
carries produces a **derived collection** superseding the old one, sharing
its record shards so peers re-fetch only vectors. The mechanism is proposed
in [`embedder-choice-and-supersession.md`](embedder-choice-and-supersession.md).

Two consequences specific to this plugin. First, "should we fine-tune?" is
not a blocking decision here: it is one more candidate the creator
benchmarks, and if a fine-tune wins later it becomes a derived collection
rather than a regret. Second, that path is only cheap **early** — the
re-fetch bill scales with `coverage × dim` — so this collection should
publish its score at creation and invite review *before* mass contribution,
not after.

### 7.4 What dimension to pin — and what the measurement actually produces

The operator's question on this was the right one: *what is the outcome of
the scoring, and what is the practical result?* Answering it properly changes
the recommendation, so the reasoning is here rather than in a decision list.

**What the measurement costs: almost nothing.** Matryoshka truncation is not
a different embedding run — it is taking the first *n* components of a
full-dimension vector and re-normalising. So the pilot embeds **once**, at
full dimension, and every candidate dimension is a numpy slice away. The
output is a four-row table per encoder:

| dim | nDCG@10 | % of full | flat-scan latency, projected | vector bytes |
|---|---|---|---|---|
| 3840 (full) | — | 100% | ~4.6 s | 11.5 GB |
| 2048 | — | — | ~2.5 s | 6.1 GB |
| 1024 | — | — | ~1.2 s | 3.1 GB |
| 512 | — | — | ~0.6 s | 1.5 GB |

The latency column is derived from this repo's own measurement — wikivec,
15.8M rows, dim 1024 f16, **13 s per query**, cost linear in rows × bytes —
projected onto ~1.5M chunks (order 10⁵ acts × 10–20 articles; a working
figure, not a denominator). The quality column is what the pilot fills in.

**What published work says to expect**, as a prior only — these are
general-domain results, not Italian legal retrieval, which is exactly why the
pilot exists: truncating to ~50% of full dimension typically costs **1–4
nDCG@10 points**; to ~25%, **5–10 points**; one multi-model study found
512 dims retaining **94–98%** of full-dimension nDCG@10, with
higher-original-dimension models (4096) truncating *more* gracefully than
2048–2560 ones. Note that 1024-out-of-3840 is the 25% regime, i.e. the one
where the published range gets uncomfortable.

**The practical result, and the recommendation.** Under the quality directive
the answer is not "truncate to keep queries fast":

> Truncation buys latency by **destroying information permanently**, in a
> value pinned into `space_id` and unfixable afterwards. A vector index buys
> the same latency by **changing how vectors are searched**, losing nothing.
> When two paths buy the same thing and one of them is lossy, the lossy one
> is not a trade-off, it is a worse engineering choice.

So: **pin the largest dimension whose quality the pilot cannot distinguish
from full**, and treat the flat-scan latency as the vector index's problem —
STATUS.md's known gap ("no vector index — every semantic query is a flat
scan"), which the IVF-on-disk design in `pack-format.md` §5 already
anticipates. The latency budget of §7.5 is then met by the index, not by
throwing away 3 000 dimensions of a model chosen for its quality.

**[COST]** — this makes the vector index a *prerequisite* for this
collection rather than a later improvement, and that is the one genuinely
expensive consequence of the quality directive in this document. If the
index is not on the table, the honest fallback is dim 2048 (the 1–4 point
regime) rather than 1024, and the pilot table is what makes even that a
number rather than a guess.

### 7.5 The reranker

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
   past 2000 characters routinely. **Under the quality directive this is a
   prerequisite, not a follow-up**: the constant becomes a per-model budget
   *before* the reranker is used on this corpus at all, because measuring a
   32k-context model against a 400-token prefix measures the wrong thing and
   would produce a decision built on it.
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

#### Scope: node, collection, or pack?

The question this section raised in general — may a *pack* require or
recommend a reranker — is answered separately in
[`reranker-scope.md`](reranker-scope.md), because it is a platform
question rather than an Italian-law one. Its conclusion for this plugin:
**optional**, like every plugin today. A reranker binds only when its
output reaches bytes that travel between peers, which happens at build
time and not at query time; nothing in the current pipeline consumes a
rerank ordering durably. So the reranker stays node-global and
operator-set, a published Normattiva pack may carry a signed advisory
hint (model identity and a measured delta, never a URL), and a peer
running its own reranker — or none — is holding the same pack.

#### The asymmetry that decides how much care to spend

**The reranker is not part of `space_id`.** Swapping it costs one config
change and zero re-embedding, where swapping the encoder costs the corpus.
So: the encoder gets the careful, measured, one-way decision (§7.2); the
reranker gets tried, swapped and re-tried freely, on the same JuriFindIT
set, *after* the collection exists. Do not hold up the collection for it —
but do measure it, because on an article-level corpus it is the cheapest
quality left on the table.

**It should also be ON by default for this collection**, which reverses the
platform default of an empty `RERANK_URL`. A reranker is the single largest
uncosted quality gain available at query time, and "off unless configured"
is a cost-side default.

**Which model: `jina-reranker-v3`, under a stated latency budget** (operator
decision, 2026-08-26). Qwen3-Reranker-8B scores ~2.5 MIRACL points higher,
but it is pointwise — 40 forward passes of an 8B model on every query, which
is seconds. jina-v3 is listwise: all 40 candidates in one window, one pass of
a ~0.6B model. This is the latency-is-quality rule doing real work: the 8B
model's extra precision is worth less than a reranker that can stay
permanently on, and a reranker that gets switched off under load contributes
zero. Qwen3-Reranker-8B stays in the bake-off as a **ceiling reference** —
run it offline on the eval set to measure what the latency budget costs, so
the trade is a number rather than an assumption.

Propose the budget explicitly, as a number the design is accountable to:
**p95 end-to-end query latency ≤ 2 s**, of which the reranker owns ≤ 400 ms.
Any component that cannot meet its share is not adopted, however well it
scores.

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

**The bulk unit is now known: the *collection*** (§1.1), in two flavours,
and they map onto the two halves of `AcquisitionSpec` almost exactly.
**Precompiled** collections are a fixed, enumerable list published on the
portal home page — that is `Endpoint.static_files`, the mechanism that
exists precisely for transports with no directory-listing protocol.
**On-demand** collections are computed from search parameters — a
*parameterised* fetch that `static_files` cannot express, and the natural
home for profiles.

```python
NORMATTIVA_SPEC = AcquisitionSpec(
    source_id="normattiva",
    transport=Transport.HTTPS,
    endpoints=(
        Endpoint(transport=Transport.HTTPS,
                 url="https://api.normattiva.it/...",   # VERIFY: base + auth
                 max_conns=2,                            # etiquette: a public service
                 static_files=_precompiled_collections()),  # the portal's own list
    ),
    layout="container-files",      # a collection holds many acts
    filters=("*.akn.xml", "*.zip"),
    checksums=ChecksumSource(kind="none"),      # VERIFY: any published digests?
    delta=DeltaSpec(mode="full-resync"),        # VERIFY: incremental feed?
    etiquette=RateLimit(max_conns=2),
    profiles={
        "demo":    Filter(...),                 # one code, e.g. the CAD
        "starter": Filter(include=("codici/*",)),
        "full":    Filter(),
    },
    profile_mode="incremental",
    estimated_bytes={...},                      # VERIFY: probe, never guess
    license_class=LicenseClass.YELLOW,          # §10
    terms_text_path=... / "plugins" / "normattiva" / "TERMS.txt",
)
```

**Which format to fetch.** AKN is the ingestion input — it is the only one
carrying the structure §4's rendition needs, and the portal publishes the
profile by which it adopted the standard. JSON is worth evaluating as the
*metadata* source for the front matter, where AKN's own metadata is more
awkward to read; that is an implementation choice inside `akn.py`, not an
acquisition one. PDF/EPUB/RTF are never fetched — they exist for human
consultation and, per the extras policy (amendment 35), would be a
local-only convenience at best.

**The LISTING CONTRACT has a candidate satisfier.** The portal plans
**DCAT-AP_IT** metadata export (§1.1). If it ships, it is a machine-readable
catalogue of everything published — which is exactly what the contract asks
for and what every denominator and ETA downstream reads. Until then, the
precompiled collection list is the enumerable surface and the on-demand
query is not enumerable at all, which is a reason to build the collection
from precompiled units where possible.

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

**Normattiva: YELLOW, and the primary source turns this from a preference
into an argument.** The first draft proposed yellow on the general ground
that CC BY's attribution obligation follows the data into every
redistribution, and this platform redistributes by design. That still holds.
But §1.1 adds a decisive fact: **the licence depends on when the data was
fetched.** The portal ran a *sperimentale* phase under **CC BY 4.0 NC**,
then plain CC BY 4.0 from 1 July 2025, then full coverage from 1 January
2026.

A **non-commercial** clause is not merely a stricter attribution rule — it
is a restriction this platform structurally cannot honour downstream, since
a P2P network cannot police what a receiving peer does with a corpus. So:

- Data fetched during the NC window is **not** redistributable under
  Louise's own model, and a collection built from it must not be exchanged.
  Whether any such copy exists is a question for whoever fetched it.
- Data fetched after the transition is CC BY 4.0 and exchanges fine, with
  attribution carried.
- **The two are indistinguishable once on disk.** Nothing in a downloaded
  AKN file records which licence window it came from.

That last point is exactly what invariant 9's mechanism is for, and it is
why yellow is now the right answer rather than merely a defensible one: the
ack records **a timestamp and the sha256 of the terms text**, so the
recorded ack *is* the evidence of which licence a corpus was taken under.
A green classification would keep no such record and would make the NC
question unanswerable after the fact.

Practical consequence for `TERMS.txt`: capture the CC BY 4.0 text **plus
Normattiva's own conditions as published at fetch time**, byte-exact, and
re-ack if the origin's terms change — the sha256 comparison in
`licensing.state_for` already turns a changed terms text into a re-ack
prompt rather than a silent drift.

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

1. **The Normattiva OpenData API — PARTLY ANSWERED 2026-08-26** from the
   portal's "Dati disponibili" page (§1.1). Settled: the bulk unit is the
   **collection**, precompiled or on-demand; the formats; the three
   versions; the licence phases. **Still open**, and still needing
   `dati.normattiva.it/assets/come_fare_per/API_Normattiva_OpenData.pdf`
   plus the "Come scaricare i dati" page:
   *(a)* how big a precompiled collection is, and how many there are —
   `estimated_bytes` must be probed, never guessed;
   *(b)* whether there is an **incremental/delta feed** or only full
   re-listing, which sets `DeltaSpec.mode`;
   *(c)* **auth and rate limits**;
   *(d)* whether **DCAT-AP_IT** has shipped, which would satisfy the
   LISTING CONTRACT outright;
   *(e)* the two normative specs — the AKN adoption profile and the ELI
   permanent-URI spec — which are direct inputs to `akn.py` and `urn.py`.
   Also: **confirm the live licence and functional state**, since the page
   reads as written during 2025 while the portal launched 2026-02-24, after
   the transition it describes in the future tense.
2. **Licence class: YELLOW** — §10 now argues this rather than merely
   preferring it, because the origin ran a **CC BY 4.0 NC** phase and a
   downloaded file does not record which licence window it came from. The
   ack (timestamp + sha256 of the terms text) is the only durable evidence
   of what a corpus was taken under. What remains for a human: capture
   `TERMS.txt` byte-exact — the CC BY 4.0 text plus Normattiva's own
   conditions **as published at fetch time** — add it to `.gitattributes`
   so no checkout converts its line endings, and establish whether any
   NC-window copy exists locally.
3. **Approval for the rendition format and a new chunker** — the §4 text
   format plus `akn_article` / `AKN_CHUNK_VERSION` (§5), which covers both.
   This is the "ask first" case in CLAUDE.md: it changes cleaning AND
   chunking, and it enters a `space_id`. Worth reviewing the §4.1 example
   as a *format decision*, because every act in the corpus will look like
   it and changing it later re-embeds everything.
4. **Is the vector index a prerequisite for this collection?** (§7.4,
   marked **[COST]** — the one genuinely expensive consequence of the
   quality directive here.) The pilot's dimension table decides *what* to
   pin; this decides whether the resulting latency is paid for by an IVF
   index (lossless, and STATUS.md's known gap) or by truncating the vectors
   (lossy, permanent, and pinned into `space_id`). Recommended: the index,
   with dim 2048 as the honest fallback if it is not on the table.
5. ~~**Whether the GU issue stream is in scope.**~~ **RESOLVED
   2026-08-26: Normattiva only.** The numbered normative corpus, 1861 to
   today, CC BY 4.0, structured AKN. The GU issue stream, Au.G.U.Sto and
   EUR-Lex were all considered and declined for now — recorded in §1 and
   §10 so they are not re-discovered as opportunities.
6. **Verify JuriFindIT's availability, licence and format** (§7.2), from a
   machine that can actually fetch pages. This gates the encoder bake-off:
   with it, the choice is measured on article-level ground truth; without
   it, the fallback is a hand-built 50–100 question set and the decision
   rests on weaker evidence. Same visit answers §7.3's open fields for
   `jurifindit/DAR-legal-it` (base model, dimension, licence).
7. ~~**Fetch the corpus as *vigente-at-fetch-time*, or as *originaria + the
   modification graph*?**~~ **RESOLVED 2026-08-26 by ELI compliance**
   ([`eli-legal-plugins.md`](eli-legal-plugins.md) §5.1). It was only a
   moving target because the fetch did not name the date. Normattiva's ELI
   URIs carry the point-in-time as a path segment
   (`…/40/CONSOLIDATED/20180504`), so the collection's **`snapshot` IS the
   ELI point-in-time coordinate** and goes into the fetch URL. Two peers
   fetching a week apart hold identical bytes, because the Expression is
   named rather than implied — and a snapshot delta becomes what it should
   be, the corpus advancing from one Expression set to the next.

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
