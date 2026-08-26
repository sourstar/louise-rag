# Gazzetta Ufficiale / Normattiva — plugin feasibility and design

**Status: PROPOSAL, nothing built.** This is the research pass that has to
happen before a `gazzetta` plugin is written: what material exists, in what
form, what the platform's own constraints allow, and which encoder the
collection would be pinned to *forever* (invariant 1). Three decisions in
here belong to the operator, not to an implementer — they are collected in
§11. Do not start coding before §11 is answered; the expensive mistake in
this codebase has always been pinning a space before measuring it.

Written 2026-08-26.

**Research caveat, stated up front:** this session's network egress is
restricted — search results were readable, pages were not. Every fact below
that came from a search-result summary rather than from the page itself is
marked *(unverified)*. The three that matter most — the Normattiva OpenData
API's bulk shape, its delta mechanism, and the exact terms text — are
exactly the unverified ones, and §11 asks for them first.

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
carries no URN, and (see §9) parts of it contain personal data.

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
  ready-made evaluation set for the encoder bake-off in §6.

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

The act is the document; the article is the chunk (§4); the issue is a
metadata attribute, not a document. That falls out of what people actually
ask — nobody asks a question of *Gazzetta n. 156 of 2026*, they ask what the
law says — and it is what the origin's identifiers are built around.

**Consequence, and it is the load-bearing one:** the corpus is
**`Mutability.MUTABLE`**. An act's consolidated text changes when it is
amended. That is the same class as Wikipedia, and it is why `chunk_cdc`
exists — but see §4, because legal text has something Wikipedia does not: a
stable, numbered, semantically real unit.

**Recommendation: two source_ids, shipped in order, not one plugin with two
profiles.** Profiles are subsets of one shape (`incremental`/`exclusive`/
`additive`); the normative corpus and the issue stream are different shapes
with different licences, different mutability and different chunkers.

1. **`normattiva`** — the normative corpus, AKN, MUTABLE. Build this.
2. **`gazzetta`** — the issue stream (concorsi, inserzioni, unnumbered
   decrees), HTML, IMMUTABLE. Only after §9 is settled; it is the half with
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
   ~0.8–4 s per query at dim 1024, and **four times that at Qwen3's dim
   4096**. This is an argument about encoder choice (§6), today, not a
   future concern.

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

## 4. Chunking: the article is the unit

`chunker_name()` today would route a MUTABLE source to `chunk_cdc`.
Content-defined chunking is the right *answer to the wrong question* here.
It solves boundary drift, which legal text does have — but it solves it
blindly, cutting mid-comma, when the text already carries the exact unit
both the amendment mechanism and the citation convention use.

**Proposal: an `akn_article` chunker, one chunk per `<article>`.**

- An AKN act gives `<article>`/`<paragraph>` (articolo/comma) explicitly.
  One chunk per articolo, labelled `"Art. 5"` in the chunk's `chapter`
  field — which is what the reader UI shows and what a citation resolves to.
- Oversized articles (a codice's art. 2359, an allegato tecnico) split by
  comma, and a comma that is still oversized splits by CDC *within itself*.
  All three levels stay a pure function of (cleaned text, chunker id,
  chunker version) — invariant 3 holds.
- **The amendment case, which is the whole point:** amending one comma
  changes that article's chunks and nothing else. Fixed-width chunking would
  re-embed the act from the amendment onwards; CDC would limit the damage to
  a neighbourhood but at an arbitrary boundary.
- Acts whose AKN carries no article structure (older material, allegati)
  degrade **inside the same chunker** to CDC over the flat text. Keeping the
  fallback inside `akn_article` rather than declaring a second layer avoids
  needing a new layer *rule* — `app/layers.py` today knows only `default`
  and `archive:<id>`, and a rule like "has AKN structure" would be an
  architecture amendment. If the fallback turns out to be common enough to
  want separate accounting, *that* is when to propose the amendment.

New chunker ⇒ new `AKN_CHUNK_VERSION`, versioned independently exactly as
`ABSTRACT_CHUNK_VERSION` is (invariant 3: never a global knob). Needs the
"ask first" approval, §11.

---

## 5. Vigenza: index the current text, serve history by key

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

## 6. Encoders

### Candidates

| model | dim | ctx | served by | verdict |
|---|---|---|---|---|
| **BAAI/bge-m3** | 1024 | 8192 | TEI `cpu-1.8` — **already approved** (`approved-encoders.md`) | **recommended default** |
| intfloat/multilingual-e5-large | 1024 | 512 | TEI | strong alternative; needs `query:`/`passage:` prefixes |
| Qwen3-Embedding-8B Q4_K_M | 4096 | 32k | llama.cpp SYCL on the Arc — the node's production text encoder | quality option, expensive here |
| Qwen3-Embedding-0.6B / 4B | 1024 / 2560 | 32k | llama.cpp / TEI | the middle rung if 8B is too costly |
| dlicari/Italian-Legal-BERT | 768 | 512 | — | **do not use as an embedder** |

**Why bge-m3 as the default.** It is already approved and already running on
this node for wikivec, so it carries zero procurement risk; its 8192-token
context means a whole articolo — usually a whole short act — fits in one
chunk without splitting; it is XLM-R-based, so Italian is first-class rather
than an afterthought; and being multilingual it makes an EU-law leg
retrievable in the same space later, for free. Dim 1024 keeps the flat-scan
cost (§3.4) tolerable.

**Why not Italian-Legal-BERT, despite the name.** It is a masked language
model — ITALIAN XXL BERT continued on ~3.7 GB of jurisprudence. MLM never
asks unrelated texts to sit apart, so mean-pooled vectors collapse into a
narrow cone. This codebase has already paid for that lesson once and written
it down: `docs/dna-search-axes.md` §2, where unrelated DNA sequences score
0.98 cosine for exactly this reason. Domain pretraining without a contrastive
objective is not a retrieval model. It would be a good *starting point* for a
fine-tune we are not set up to do.

**Why Qwen3-8B is the quality option and not the default.** It is better,
it takes instruction prefixes (real asymmetric query/document handling), and
its 4096 dims cost 4× the bytes per row — which, with no vector index, is 4×
on every query — while competing with gutenberg for the same GPU.

**Reranking.** `RERANK_URL` is already wired (`app/retrieval.py:77`) and
unused by default. `BAAI/bge-reranker-v2-m3` on TEI is the natural pairing
with bge-m3 and is worth measuring in the same pass — legal answers live or
die on precision at k=5.

**Do not skip the lexical leg.** Legal queries are citation- and
term-heavy, Italian stemming is already wired (§3.3), and RRF over
FTS + dense is the platform's default for good reason. Expect the FTS leg to
carry more weight here than in any existing collection.

### How to choose — measure, don't reason

The encoder is pinned into `space_id` at collection creation and changing it
means re-embedding from zero. So:

1. Build a **pilot collection of ~5,000 acts**, one per candidate encoder.
2. Score them on an Italian legal eval set — the NLLP 363k-title corpus
   (§1.4) gives labelled material for free, plus ~50 hand-written questions
   with known correct articles.
3. Run the `docs/calibration.md` machinery to set the tolerance, then let
   the backend gate hold it.
4. Throw the pilots away and create the real collection against the winner.

The pilots are cheap. A wrong pin is a re-index of the whole corpus.

---

## 7. Acquisition spec sketch

Shape only — the fields marked `# VERIFY` are the ones §11 needs answered
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
    license_class=LicenseClass.YELLOW,          # §9 — operator's call
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
§11.1 is first.

---

## 8. Module layout

Following the existing plugins' shape exactly:

    app/plugins/normattiva/
        __init__.py        SOURCE = NormattivaSource()
        source.py          discover / fetch / parse   (~200 lines, cf. wikipedia)
        akn.py             Akoma Ntoso + XML-NIR -> CanonicalDoc.sections
        urn.py             URN:NIR <-> ELI <-> (type, date, number); native_id
                           synthesis (§3.1); citation-string parsing (§5)
        TERMS.txt          CC BY 4.0 + Normattiva terms, byte-exact
    app/acquire/specs.py   + NORMATTIVA_SPEC, + registry entry
    app/collections.py     + PLUGIN_REGISTRY["normattiva"]
    app/chunking.py        + chunk_akn_article, AKN_CHUNK_VERSION
    app/refs/normattiva.py phase 2 — the ReferenceSource of §5
    tests/
        test_normattiva_urn.py         URN <-> native_id, round-trip, collisions
        test_normattiva_akn.py         AKN -> sections, golden fixtures
        test_akn_article_chunker.py    golden chunks; version isolation
                                       (only this space's id moves)
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

## 9. Licence and privacy classification

**Normattiva (CC BY 4.0): propose YELLOW.** Green means no gate at all.
CC BY imposes an attribution obligation that follows the data into every
redistribution — and this platform redistributes, by design, over P2P.
Yellow costs one acknowledgement (a timestamp and a sha256 of the terms
text) and makes the obligation visible at exactly the moments amendment 36
cares about: discovery, import, exchange. Green is defensible; yellow is
cheap and honest. **Operator's call (§11.2).**

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

## 10. Build order

1. **Verify §11.** No code before it.
2. `urn.py` + `akn.py` + tests, against a handful of hand-downloaded acts —
   pure functions, no network, no manifest.
3. `source.py` over a hand-populated corpus directory (the `adoption` path
   already supports files that are simply on disk). This gets a real
   collection indexing without touching acquisition.
4. **Pilot: 5,000 acts × 3 encoders**, §6. Measure. Decide. Delete.
5. `akn_article` chunker with its own version, once §11.3 approves it.
6. `NORMATTIVA_SPEC` and the acquisition path, once §11.1 is answered.
7. Full collection; watch the worker log as a time series before optimising
   anything (the first benchmark, every time).
8. Phase 2: `refs/normattiva.py` + citation EntityResolver — §5 — which is
   where the retrieval quality is.

---

## 11. What a human has to decide before this starts

1. **The bulk shape of the Normattiva OpenData API** — read
   `dati.normattiva.it/assets/come_fare_per/API_Normattiva_OpenData.pdf`.
   Three answers needed: is there a bulk download unit (and how big), is
   there an incremental/delta feed or only full re-listing, and is there
   auth/rate limiting? §7 cannot be written without these, and the answer to
   the first one decides whether this is a pleasant plugin or an unpleasant
   one.
2. **Licence class for `normattiva`: yellow or green?** §9. If yellow,
   capture `TERMS.txt` byte-exact — CC BY 4.0's text plus Normattiva's own
   conditions — and add it to `.gitattributes` so no checkout ever converts
   its line endings.
3. **Approval for a new chunker** (`akn_article` + `AKN_CHUNK_VERSION`),
   §4. This is the "ask first" case in CLAUDE.md: it changes chunking and it
   enters a `space_id`.
4. **Which encoder the pilot pins** — or rather, agreement that a pilot
   happens at all before the real collection is created. §6.
5. **Whether the GU issue stream is in scope at all**, given §9's privacy
   problem. If it is not, say so in this document and stop re-discovering it.

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
- Encoders: [ITALIAN-LEGAL-BERT](https://ceur-ws.org/Vol-3256/km4law3.pdf),
  [LEMUR: multilingual law embedding fine-tuning corpus](https://arxiv.org/pdf/2602.09570),
  [Benchmarking multilingual dense retrieval, 2026](https://arxiv.org/pdf/2605.23618)
