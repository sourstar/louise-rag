# Normattiva multivigenza: the law as it stood, by snapshot chain

Operator instruction, 2026-10-12: a court decision taken in the past can
only be interpreted against the law in force at the time, so the corpus
must answer "what did this article say in January 1980", and results not
in force then must be excluded. Written for review before any code moves.

**Status: proposal. Nothing implemented.** The acquisition today is
`VIGENTE` where offered (`app/acquire/specs.py` `_NORMATTIVA`), which
cannot answer the question at all.

## 0. Two designs I proposed in conversation and am retracting

Recorded so the review does not have to re-derive why they are wrong.

**Retracted: versions as documents** — `native_id` gaining a version
component so each Expression becomes its own record. §5.1 of
[`eli-legal-plugins.md`](eli-legal-plugins.md) already considered and
rejected it: it "multiplies the corpus by the amendment count and puts
near-identical versions of one article into the same RRF list"
(`gazzetta-plugin.md` §6 option (b)). Confirmed independently from
outside — see [R2] §3.1, property P3: successive versions are "mutual
nearest neighbours under any standard sentence encoder". It is also
impossible as written: §5.2 requires `native_id` to be integer-parseable
(`CAST(native_id AS INTEGER)`, `idx_documents_native_int`, Lance's
`book_id`), so a string id carrying a vigenza date is not available.

**Retracted: "a new id scheme costs a new collection."**
`collection_id = H(canonical-json(codex))`, and `native_id` is not in the
Codex. The operator corrected this; it was asserted from an unread
section and used to manufacture urgency. There is no deadline of that
kind.

## 1. The documented answer, which already exists

`app/ask/asof.py` (05 §5.1, 07 §7.3 `--as-of DATE`) implements
*multivigenza* by its own docstring: a search reads, per collection, **the
newest snapshot of the chain whose cutoff is at or before the date**,
instead of the newest held.

Its semantics are already the strict ones this use case needs:

- Records whose content did not change between the as-of snapshot and the
  newest are answered from the one index — same `content_hash`, same
  chunks, so a hit is a hit on the as-of state.
- A record **added, changed or tombstoned** by any later snapshot is not
  answered from a later state. Its hits are **dropped**, and the shards
  holding such records are named in `withheld` (05 §5.1: "a wrong date is
  worse than no answer").
- The chain between the two snapshots must be held whole, or the
  collection is withheld.
- `MAX_CHANGED = 500_000` uids; beyond that, `TooFarBack` (exit 3).

§5.1 of the ELI note supplies the model the chain needs: **`uid` is the
Work URI and the snapshot pins the Expression.** "A pack is the set of
Works of a corpus, each at one Expression, named by the snapshot." The
point-in-time belongs in the fetch URL (`…/CONSOLIDATED/20260826`), and
the Expression is recorded **from the returned metadata, never from the
request** — so a re-fetch crossing no amendment is correctly a no-op.

**So the work is not a new axis on records. It is building the snapshot
chain backwards from the multivigente archive** — §5.1's "corpus advancing
from one Expression set to the next", run in reverse. No `native_id`
change, no record-level version field, invariant 4 untouched, nothing
enters RRF differently.

## 2. What the origin actually offers (measured 2026-10-12)

API `https://api.normattiva.it/t/normattiva.api/bff-opendata/v1/api/v1`,
no auth; the download answers 302 to a tokenised URL that serves Range.
Licence CC BY 4.0 (`app/plugins/normattiva/TERMS.txt`).
`collections/collection-predefinite` lists 23 collections in three forms:

| Form | Collections | Acts |
|---|---|---|
| ORIGINALE — as enacted | 23 | 286,567 |
| **MULTIVIGENTE** | 15 | 109,231 |
| VIGENTE — in force now | 16 | 66,739 |

**MULTIVIGENTE is a superset, not a third form.** Measured on `Leggi
costituzionali` M (910,258 bytes): 50 acts, **95 version members**, every
act carrying `ORIGINALE_V0`, latest vigenza 2026-02-24, up to 19 versions
on one act. Members are
`<dataGU>_<codice>_(ORIGINALE|VIGENZA_<date>)_V<n>.xml`, which the
existing `_MEMBER` regex in `source.py` already parses.

**The listing under-reports what the download serves — do not build the
spec from it.** `collection-predefinite` advertises MULTIVIGENTE for only
15 collections, and an earlier draft of this note took that as the limit.
Probing `collection-preconfezionata` directly (Range, `Content-Range`
total) shows M served for **22 of 23**, including every collection the
listing omits:

| Collection | M | V | M/V |
|---|---|---|---|
| Codici | **1,149.5 MB** | 10.0 MB | 115x |
| Decreti Legislativi | 1,215.6 MB | 64.1 MB | 19x |
| DPR | 726.9 MB | 174.8 MB | 4x |
| Leggi delega e provvedimenti delegati | 590.4 MB | 35.7 MB | 17x |
| Leggi finanziarie e di bilancio | 536.5 MB | 6.9 MB | 78x |
| Testi Unici | 504.0 MB | 9.0 MB | 56x |
| Atti di recepimento direttive UE | 223.9 MB | 23.0 MB | 10x |

The only collection with no M and no V is **`Atti normativi abrogati`**
(124,045 acts, ORIGINALE only) — and that is correct by construction: a
repealed act has no consolidation to produce, so its original text is all
there is.

Two consequences. The substantive, most-cited law **does** have a full
version history available, so the central limitation an earlier draft
recorded here does not exist. And the listing is not a statement of
availability — the existing `_NORMATTIVA` table was derived from the
2026-09-14 listing (60 rows; today's has 54, having silently dropped
`Codici` V, which still downloads). **Form availability must be probed,
and the probe recorded, not read off the listing.**

## 3. The collection set, and the pre-Republic cut

Operator: anything before 1946 is scanned material; `Regi decreti` out of
scope; keep everything "from the Constitution onwards".

Sizes probed by Range (`Content-Range` total, one byte each), 2026-10-12:

| | |
|---|---|
| M for all 22 that serve it, + O for `Atti normativi abrogati` | **7.27 GB** |
| less `Regi decreti` M (1,616.8 MB) and `Regi decreti legislativi` M (1.4 MB) | **~5.69 GB** |

Of which: 4.95 GB is the seven collections the listing hides (§2), 423 MB
the thirteen it advertises and we keep, and 454 MB the repealed acts in O.

Both dropped collections are royal instruments, wholly pre-1946, so they
go at download rather than at ingest.

**The cut cannot be a floor on the publication date.** `Codici` holds 8
pre-1948 acts, and they are the most-cited law in Italy — codice penale
(GU 1930-10-26, `VIGENZA_2026-07-16`), codice di procedura civile
(1940-10-28, `VIGENZA_2026-03-05`), **codice civile** (1942-04-04,
`VIGENZA_2026-04-29`). All in force, all served as the modern
consolidation rather than as a scan. A 1948 floor on `date` deletes them.

**Proposed rule: the floor is on validity, not enactment.** Exclude a
version whose validity ends before 1948-01-01; keep anything in force at
any point from 1948 onward, whatever year it was enacted. The 1942 civil
code is kept; a decree repealed in 1935 is dropped; `Decreti legislativi
luogotenenziali` (measured: 3,276 members spanning 1943–1946 — 2,323
pre-1946, 953 in 1946) is kept with its pre-Republic versions excluded.
The rule is declared in the acquisition spec, so the pruning is
reproducible — invariant 11 permits pruning, not silent pruning.

**Open: the Constitution is not in the predefined collections.** Not in
`Codici` (no 1947 member, no "costitu"), not in `Leggi costituzionali`
(earliest members 1947-02-22, 1947-06-19, 1947-11-07 — the amending laws;
the Constitution's own GU date is 1947-12-27). It would have to come from
`ricerca`. The corpus is not complete without it.

## 4. The chain: the one real design question

`asof.target_id` resolves a date through `codex.update.cadence` and
`update.day`, so the chain is **regular** — not one snapshot per
amendment date. Building back to 1948 means choosing a cadence:

| Cadence | Snapshots 1948→2026 | Answers |
|---|---|---|
| yearly | 79 | "the law in 1980" |
| quarterly | 315 | "the law in Q1 1980" |
| monthly | 939 | "the law in January 1980" |

The operator's example asks for monthly. Monthly is also where
`MAX_CHANGED = 500_000` needs checking: the bound is not how much changed
per month but **how many uids differ between the as-of snapshot and the
newest** — for a 1980 query, every act amended in 46 years. That number
must be measured against the multivigente archive before a cadence is
fixed; above the bound, `--as-of 1980-01-15` returns `TooFarBack` and the
capability fails at the depth it was asked for. **Measure this first; it
may invalidate the design.**

Dependent second question: a historical snapshot is **reconstructed** from
the multivigente members in force at its cutoff, not fetched. §5.1's rule
still holds — each member carries its own `FRBRthis`
(`…/ita@1983-05-11/…`) and lifecycle, so the reconstruction reads the
Expression rather than inventing it.

## 5. What changes in the code

Smaller than either retracted design. Nothing in `eli.py`, nothing in the
record shape, no new extra keys.

1. **`_NORMATTIVA` (`app/acquire/specs.py`)** — 21 rows: M for the 13
   remaining collections offering it, V/O for the 8 that do not, with new
   `estimated_bytes` from the probe. The docstring's rationale is
   rewritten: multivigente is fetched *because* the chain needs it, and
   §5.1 is why it does not pollute the ranked list.
2. **`source.py` `discover()`** — today `best[nid]` keeps one member per
   act, ranked `(_FORM_RANK[form], vig, v)`, i.e. the latest vigenza.
   That stays correct for building *one* snapshot; the rank comparison
   gains the cutoff as a parameter instead of always taking the newest.
   The docstring's "Multivigente would put every version of an article
   into the same ranked list" is right and stays — it is the reason the
   version never reaches the index.
3. **The validity floor** — a declared filter, applied where the member
   date and the act's version chain are both in hand.
4. **The chain builder** — new, and the bulk of the work: for each cutoff
   in the cadence, select the in-force member per act and emit that
   snapshot. Reuse before recompute (RULE 1): a version unchanged between
   two cutoffs is the same bytes and the same `content_hash`, so its
   vectors carry across untouched. That is what makes 939 snapshots
   affordable rather than 939 full builds.

## 6. Tests to ship with it

- Existing fixtures cover both member forms
  (`legge-2003-306-vigente.xml`,
  `legge-costituzionale-1967-1-originale.xml`); add one multivigente act
  with ≥3 versions.
- Version in force at a cutoff: before the first amendment → `ORIGINALE`;
  between two → the earlier; after the last → the newest; the boundary day
  itself included, per the origin's tabulation (§5.1).
- The floor: a version ending 1935 dropped, the 1942 civil code kept.
- An act present in two collections still held once.
- Chain reuse: a version unchanged across two cutoffs yields one
  `content_hash` and is not re-embedded.
- `records build` monotonicity unaffected — `native_id` unchanged, so the
  open-part refusal of docs/02 §2.3 cannot trigger.

## 7. Open, for the review

1. **Cadence**, and whether monthly survives `MAX_CHANGED` (§4). Measure
   before choosing.
2. **Closed 2026-10-12: M is served for 22 of 23** (§2), the earlier
   "central limitation" was an artefact of trusting the listing. The
   residue is `Atti normativi abrogati` — ORIGINALE only, correctly, since
   a repealed act has no consolidation. What it means for as-of: a query
   dated before an act's repeal gets that act's original text, which is
   the right answer when the act was never amended and a stale one when it
   was. 124,045 acts is too many to ignore and too many to fix by
   per-act fetches; `ricerca-asincrona` (`nuova-ricerca` /
   `conferma-ricerca` / `check-status`, email delivery) is **untested** and
   the only candidate. Decide whether that residue is accepted and
   documented, or chased.
3. **The Constitution** must be sourced separately (§3).
4. **Storage** — 1.14 GB of origin zips plus the reconstructed chain;
   origins on tank per `storage-placement`.
5. **`Regi decreti`** stays excluded by instruction; note it is the only
   collection where multivigente is offered and dropped.

## 8. Reference material

Marked by how far I actually got into each: **[read]** first-hand,
**[abstract]** abstract or dataset card only, **[search]** from search
summaries, not the source.

### Point-in-time and versioned legal retrieval — the basis of §1 and §4

- **[R2] [read]** Cymbler, Guez, Fabre, *Temporal Misgrounding in Legal
  RAG: A Versioned-Corpus Benchmark for French Tax Law*, arXiv
  2608.09393v1, 10 Aug 2026; ICML 2026 AI4Law. **The load-bearing
  reference.** 32,436 article-versions of the French tax code
  (1938–2031), 209 expert-reviewed questions, deterministic nugget
  scoring (regex + numeric-with-tolerance, explicitly never
  LLM-as-judge). Over eleven models: no retrieval 3.0% strict;
  current-version-only RAG 2.7% and **0% provenance — it never once
  retrieved the date-applicable version**; versioned date-conditioned
  retrieval 98.3% end-to-end, 99.1% with an oracle article. Their own
  caveat, which matters: Condition B "fails partly by construction", as
  questions were screened so the current text lacks the gold value (208
  of 209). The robust findings are the 0% provenance (structural — a
  single-version index cannot hold the version) and that the end-to-end
  retriever matches its oracle ceiling, which puts the residual gap in
  first-stage article recall, not version selection. §3.1 P1–P3 (stable
  identifier / date-dependent correctness / cross-version similarity) and
  the four failure modes are the clearest statement of the problem in
  print. Their production design: index three versions per article,
  dense + BM25 fused by RRF, top-5 articles, then a deterministic version
  layer — no ontology, no graph.
- **[R3] [search]** de Martim, *SAT-Graph RAG: An Ontology-Driven Graph
  RAG for Legal Norms*, arXiv 2505.00039. Point-in-time via an
  ontology-driven knowledge graph; [R2] contrasts itself against it and
  reports it is evaluated as a qualitative case study on the Brazilian
  Constitution rather than a scored benchmark. Its useful idea for us: a
  new temporal version is created only when a component's text actually
  changes, instead of duplicating whole documents.
- **[R4] [search]** Prior et al. 2026, arXiv 2605.23497 — 312
  expert-validated German statutory QA pairs with version-filtering RAG
  conditions; concludes temporal validity belongs as a hard constraint,
  not a relevance signal. [R2] criticises its LLM-as-judge scoring as
  inheriting the bias it measures.
- **[R5] [search]** Fan et al. 2026a, arXiv 2605.25920 — training-cutoff
  bias and absent temporal constraints in legal search agents, 13-task RL
  benchmark.
- **[R6] [search]** *Beyond Probabilistic Similarity*, arXiv 2606.09724 —
  taxonomy of versioning models (snapshot, timestamped triples,
  event-centric, bitemporal). Snapshot-based is what §1 uses.
- **[R7] [search]** *Deterministic Legal Agents*, arXiv 2510.06002 —
  separates valid time from transaction time for reproducibility.
- **[R8] [search]** LexKairos, arXiv 2608.09106 — benchmark for
  identifying the statute version in force at a timestamp.

### Italian and statutory-article retrieval — context for the encoder

- **[R9] [abstract]** Dalla Noce, Colla, Farhang Doust, De Mattei,
  Bacciu, *JuriFindIT: an Italian legal retrieval dataset*, Findings of
  EACL 2026, pp. 4223–4241. The first Italian statutory-article-retrieval
  dataset: four macro-areas (civil, criminal, AML/CTF, privacy), 895
  expert-authored and 169,301 generated questions, 23,000+ linked
  articles, and the first to encode inter-article references explicitly.
  Method: dense encoders plus a heterogeneous legislative graph.
  **Results tables unread** — "consistent improvements over prior SAR
  approaches" is not a number. Hub artefacts: dataset
  `jurifindit/JuriFindIT` (CC-BY-NC-SA-4.0; its `corpus.reference` column
  is a ready-made citation graph, and the upload's 21.5K synthetic
  questions are a subset of the paper's 169,301) and model
  `jurifindit/DAR-legal-it` (305M, GTE arch, base
  `Snowflake/snowflake-arctic-embed-m-v2.0`,
  `CachedMultipleNegativesRankingLoss`, 24,661 pairs; published
  mrr@5 0.7719 / mrr@100 0.7780 on their validation split).
- **[R10] [search]** *A Statutory Article Retrieval Dataset in French*
  (BSARD), ACL 2022, Maastricht; code at
  `github.com/maastrichtlawtech/bsard`. The original SAR dataset —
  ~1,100 citizen questions over ~22,600 Belgian articles. The closest
  thing to a reference implementation of this task.
- **[R11] [search]** bBSARD, RegNLP 2025 — bilingual extension; reports
  BM25 remaining competitive against many zero-shot dense models, which
  the Italian thesis below independently repeats.
- **[R12] [search]** *CuSINeS: Curriculum-driven Structure Induced
  Negative Sampling for Statutory Article Retrieval*, arXiv 2404.00590.
  Structural difficulty of negatives, difficulty recomputed from the
  model as it trains, curriculum schedule; model-agnostic, gains across
  four dense models on BSARD.
- **[R13] [read, secondary]** Montebovi, *Domain-Adaptive Neural
  Retrieval for Italian Legal Documents: A Testbed Using AGCM Data*,
  University of Pisa, AY 2025/2026 (advisors Comandè, Licari). The basis
  of the operator's side-project encoder. Its ablation is the reason not
  to start from an MLM-only encoder: fine-tuned ModernBERT 0.78 MRR@10,
  below zero-shot BGE-M3 at 0.84. Its Hit Rate@10 = 1.00 for six of
  seven models indicates an easy candidate pool, so its numbers are not
  comparable with [R9]'s.

### The origin

- **[R14] [read]** Normattiva open data, `dati.normattiva.it`, API
  `api.normattiva.it/t/normattiva.api/bff-opendata/v1`. Endpoints used
  here: `collections/collection-predefinite`,
  `collections/download/collection-preconfezionata`,
  `tipologiche/estensioni` (AKN, XML, PDF, EPUB, RTF, ELI URI, JSON,
  HTML), `tipologiche/classe-provvedimento`. Not tested:
  `ricerca/{semplice,avanzata,predefinita}`,
  `ricerca-asincrona/{nuova-ricerca,conferma-ricerca,check-status}`.
  Licence CC BY 4.0 per `app/plugins/normattiva/TERMS.txt`; the portal's
  own notice that its texts are **not official** — the printed Gazzetta
  Ufficiale prevails — is surfaced in TERMS.txt rather than buried.
- **[R15] [read]** Légifrance PISTE (DILA), for contrast only: its
  `/consult/getArticleByCid` returns an article's complete version
  history where `/consult/getArticle` returns the current version. This
  is the endpoint [R2]'s corpus rests on, and the shape of what §7.2 is
  looking for on the Italian side.

### Internal, and authoritative over all of the above

- `docs/notes/eli-legal-plugins.md` — §5.1 (the Expression axis and the
  snapshot resolution), §5.2 (`native_id` as a derived integer), §5.5
  (one `date` field is not enough), §5.7 (repeal is not deletion), §6
  (the compliant corpus form).
- `docs/notes/gazzetta-plugin.md` — §6 option (b), rejected; §12.7, which
  §5.1 closes.
- `docs/02` §2.2 (snapshot cutoffs), §2.3 (`native_id_order`, the
  monotonic promise, and `records build`'s refusal), §2.4 (`space_id`).
- `docs/05` §5.1 (`as_of`, `withheld`, "a wrong date is worse than no
  answer"), §5.2 (registered-id `keys` only).
- `docs/07` §7.3 (`--as-of DATE`), §7.7 (resumable units), §7.8.
- `docs/09` §9.5 (record shape and `extra`).
- `app/ask/asof.py`, `app/plugins/eli.py`, `app/plugins/normattiva/`,
  `app/acquire/specs.py` `_NORMATTIVA`.
