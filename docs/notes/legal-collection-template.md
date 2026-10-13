# A legal collection in Louise — the template Normattiva left (2026-10-13)

*For whoever builds the next collection of legal texts: another Italian
source (the Gazzetta, a region's statutes), another member state's ELI
origin, a court's decisions. Everything below was paid for on Normattiva
between 2026-09-14 and 2026-10-13; where a rule rests on a measurement, the
number is given and the place it was taken. The specification wins where
this note and the specification disagree; this note says what the
specification does not say about legal text.*

The sources, in reading order:
[`normattiva-multivigenza-review.md`](normattiva-multivigenza-review.md)
(the design and its measurements), [`eli-legal-plugins.md`](eli-legal-plugins.md)
(identity), [`gazzetta-plugin.md`](gazzetta-plugin.md) §7 (encoders and the
pilot), [`STATUS.md`](STATUS.md) at 2026-10-12 and 2026-10-13 (what the first
discovery found), `app/plugins/eli.py`, `app/plugins/normattiva/`,
`app/validity.py`, `app/refs/normattiva.py`, `tools/legal_pilot.py`.

## 1. What a law is, to Louise

A law is a **Work** with several **Expressions** over time. The Work is the
act (ELI's `/eli/id/…`, the URN:NIR); an Expression is one version of its
text in force from a day. Every Expression is **its own record**. Nothing
else survived review: a chain of snapshots holds the state at its cutoffs
only — 6.7 % of Normattiva's version transitions, 37 % in the forty codes,
are shorter than a month (review §1) — and a current-only index never
retrieves the version in force at a past date (R2: 0 % provenance, by
construction).

- `uid = <plugin>:<native_id>`; the identity is in `keys`, the id is a join
  key.
- The record carries the START of its validity (`vigenza_from`); the END is
  **derived** by the node (`app/validity.py`), never written into a record:
  it is the next version's start, or the day another act repealed this one.
- The validity window is a **prefilter on both legs by column range**
  (`valid_from`, `valid_to`, `shown`), so exactly one version per act is
  admitted at a date and no two versions of an act ever meet in a ranked
  list. Without a date, one version per act: the latest with text, a
  repealed act shown in its last text and labelled.
- Valid time (when a rule governed) and transaction time (when we held it)
  are two axes. The record pins the first, the snapshot the second.

## 2. Identity

Use the ELI layer (`app/plugins/eli.py`, the rules in its docstring).

- The record's `keys` hold only ids a reference source registers (05 §5.2):
  the URN verbatim (`urn:nir:stato:legge:2003-10-31;306` — a URI's scheme is
  its namespace, never prefixed twice), the unique ELI by-id form, the Akoma
  Ntoso work path (`akn:it/act/legge/stato/2003-10-31/306`, the id the
  archive's own cross-references use). A key in any other form never
  matches, silently.
- `native_id` is `(days since 1500-01-01) << 45 | blake2b(work key ‖
  expression) mod 2**45`: sortable by the start of validity so arrival stays
  monotonic (02 §2.2), 63 bits exactly, **collision-checked and never
  trusted**. Nine decimal digits of hash collided three times on the first
  discovery of the whole archive — about 77,000 versions start on 2010-12-16
  — as the birthday arithmetic said they would. Do the arithmetic for the
  day on which the most versions start before fixing any id width.
- The version key is the origin's own identifier of the Expression
  (`ORIGINAL`, `CONSOLIDATED/20010201`), read from the member name and
  agreeing with the ELI alias; never an `FRBRExpression` date, which was
  stale on real members; never a per-zip sequence number.
- The Codex pins the parser version that produced the records (02 §2.3).
  Bumping it before a collection exists is free; after, the collection is
  re-created from the same corpus. Four bumps happened in one day; the fifth
  cost a re-creation.

## 3. Versions and their start

- A consolidated version starts on the day its member name carries.
- An **original** starts on its **Expression date**, which is the entry into
  force: measured on 220 originals of 22 collections, it equals the
  preface's "Entrata in vigore" note on 151 of the 154 that carry one and
  follows the vacatio pattern (publication + 15, + 1, + 0) otherwise; the
  one Expression date before publication was a typo, and the note decided
  (`original_start`). A query in the fortnight between publication and entry
  into force returns nothing, which is right.
- Three things close a validity, in order of evidence: a **repeal state**
  (a version whose every article reads "ABROGATO", text-less, kept — the
  origin's last word); a **whole-act repeal stated by another act**
  (`closures()`, typed `repeal`, whole-act destination, from the day the
  destination's `ita@` date names); **neither**, and the act stays
  open-ended, `in_force: false`, labelled "repeal date not in the open data".
  Under-closing is a label; over-closing is a silent mis-date. Be strict.
- A floor on validity at discovery (1948-01-01 for Normattiva) is a decision
  about scope, not fidelity: every version that governs a day after the
  floor is held, including versions that started before it.

## 4. Origin and fidelity

- **Choose the origin form and write down why**, beside the plugin: the
  multivigente Akoma Ntoso export holds every version as a member of its
  own; the originale form of the repealed acts holds what the first does
  not; JSON and ELI forms carry the same text with less structure.
- Keep everything the origin asserts about the act — ids, dates, modifications,
  references, lifecycle, the whole FRBR block — in `extra` under keys fixed
  for the collection's life (invariant 11). "We do not use it yet" is not a
  reason to drop it; the `expression_date` that explained the originals was
  already in `extra.frbr`.
- Every record carries `origin_hash`; the origin unit of a zip member is the
  member, read by Range from the zip's central directory, so
  `records-vs-origin` re-fetches tens of kilobytes, not a gigabyte.
- **Measure the archive before fixing a parameter.** Normattiva's zips
  answer Range: the central directory of a 1.7 GB zip is a 4 MB request, a
  member is one more. Version counts, transitions shorter than a month,
  articles per act, distinct article texts across versions (41,289 of
  2,904,112 in the codes: the carry leaves 1.4 % for the encoder), token
  lengths under the encoder's tokenizer — all were measured this way, from
  the workstation, without downloading the archive.

## 5. Acquisition from a public open-data API

Four defects surfaced on the first fetch of the whole archive, each fixed
before the next step (STATUS, 2026-10-13). They are the checklist.

1. **Nothing a file does escapes its own result.** A delta, an adoption or
   a stream that raises must become one failed file; the pass goes on. The
   zip delta had run outside the per-file boundary and one read timeout
   ended a 7.8 GB fetch.
2. **Probe the origin with curl before designing a fetch path.** This one
   cuts every whole stream at 1,086,390,272 bytes and stalls an open-ended
   resume, while a closed range at the same offset answers in a second. The
   file is fetched in closed 32 MiB ranges (`transport_https.fetch_ranged`),
   each request its own unit with its own timeout and retries.
3. **A 5xx, a read timeout, a dropped connection are the origin's episode,
   not the file's.** Retry them within the pass (five attempts, 15 s in all —
   the episode lasted eight), and classify the recorded error as a deferral
   so a file that still failed is retried quietly and reported only after
   several passes.
4. **A directory that could not be read says nothing about the file.** A
   complete copy stands, the row is untouched, the pass counts it skipped.
   Only a directory READ that says the file differs sends it to the whole
   fetch. A complete 1.2 GB zip was re-fetched and failed on a transient 500
   before this rule.
5. **Keep every request below what the origin serves in one answer, and
   say every decline and deferral.** This origin caps a Range response at
   50 MiB and stalls past it; the two zips with the most members had their
   directory reads time out on a 64 MB tail, their rows went unwritten and
   the pass said "skipped" with the reason in a list nobody printed. The
   directory is read in pieces, the ranged step is 32 MiB, and the delta's
   reasons are events. An unchanged zip is adopted by its directory, never
   rewritten: before this rule 7.8 GB went through tank every pass.

Also: the listing's dates are not a change signal (the zips are rebuilt
daily); the delta reads each zip's directory every pass and moves only the
changed members; two connections at most to a public service.

## 6. Chunks, embedder, reranker

- The chunk is the **article**. A longer article is truncated to the space's
  `ctx`; pick `ctx` from the articles' token lengths under the encoder's own
  tokenizer (the codes: median 241, p99 2,943, a handful at 8,192 — `ctx`
  8192, the engine's window).
- Vectors are **carried by chunk hash across records**: the same article
  text in two versions is embedded once. Every version of the codes costs
  2.12× the distinct texts of the latest ones, not 83×.
- Choose the encoder by **measurement on expert questions in the domain and
  language**, never on a general benchmark: JuriFindIT's 179 expert
  questions moved the ranking twice. A proprietary or unlicensed encoder
  cannot own a pinned space, whatever it scores. The query instruction is
  inside `space_id`; the reranker is outside it and is measured after the
  collection embeds, on uncited questions only — the cited ones never reach
  it.
- The engine that computes the reference vectors is pinned by its **run**
  (VeGa's receipts): the recipe names the engine block and weights digest
  the endpoint states, and `create` holds the endpoint to them. A tree
  digest moved once with no file changed; since then VeGa keeps the per-file
  list behind each run and `hold embedder tree-diff` names what moved.

## 7. Relations

- Edges are typed after ELI — `eli:amends`, `eli:repeals`, `eli:cites` —
  **between Works**, by the canonical id the origin's statement uses (URN or
  AKN path; the archive uses both). `eli:repeals` only for a modification
  typed `repeal` whose destination is the whole act; a repeal of a part
  amends.
- Edges are **never a retrieval signal**. They restrict (a cited act narrows
  the search), they are traversed on request (a separate, labelled list), and
  they close validity. A rank is a function of the text and the question, so
  it is the same on every peer and does not drift as the graph grows.
- Whether widening the candidate set along the edges would find anything
  is a measurement, not an opinion: `tools/legal_pilot.py run` reports how
  many missed gold articles sit one edge away from a hit.

## 8. The reference source

The acts are a reference source (06 §6.5): `atto` and `versione` tables
read from each member's head by Range, owning `urn`, `eli` and `akn`. A
citation in any of its forms — "l. 306/2003", "legge n. 306 del 2003",
"legge 31 ottobre 2003, n. 306", "art. 5, comma 3, della legge 306/2003" —
resolves to the act and pins its versions as **facts, never ranked**
(invariant 4). 84 % of the French benchmark's questions name their article;
provenance there is a key lookup, and the embedder decides only the
uncited remainder.

## 9. What the studies say, and what was done with it

Adopted: every version indexed and the date choosing one by key (R2, R4);
valid time apart from transaction time (R6, R7); citations resolved by key
as facts (R2's 84 %); dense and lexical legs fused by RRF; a vector once
per distinct text (SAT-Graph's idea, applied to vectors). Not adopted, on
purpose: three versions per article as a recall device (every version is
indexed); one record only when text changes (fidelity keeps every
Expression); graph structure as a ranking signal (§7). Pending, measured by
`tools/legal_pilot.py` once the collection embeds: the reranker's worth on
uncited questions; article-level resolution of a citation; the reach of the
edges; LexKairos as a test set for `--as-of`. Scoring is deterministic —
held or not, the version in force or not — never an LLM judge.

## 10. The process that found the defects

- The new collection is the **quarantine's guinea pig**: never listed as in
  use before, and every tool it finds missing is fixed, never worked around.
- **Fix it before the next step.** Each of the first fetch's four findings
  was fixed, tested in the full suite, deployed and verified before the
  build was allowed to start. A wrong artefact is never recalled, only
  superseded.
- Two sessions on one tree and one node: commit by named files, deploy
  main's head after a clean suite on an archive of it, say who deploys when.
  The swap on the node is the operator's; staging and proving beside the
  live tree is not.
- Write the measurement into the note the same day, with the number and
  where it was taken. The next collection reads the note, not the log.

## 11. Checklist for the next legal collection

1. Read the origin's API documentation, then **probe it with curl**: Range
   support, the size of a listing, whether it cuts long responses, what a
   HEAD says. Write the origin form and its reason beside the plugin.
2. Measure the archive by Range: acts, versions, transitions shorter than
   your intended cadence, articles per act, distinct texts across versions,
   the busiest start-of-validity day. Decide the id width from the last.
3. Declare the ELI profile, the validity axis, the slicing (type and year,
   50 acts a part worked), the licence class and its terms text.
4. Write the parser against real members as fixtures; keep every field the
   origin asserts; bump the parser version explicitly for every change.
5. Register the namespaces in a reference source; write `keys` only after.
6. Choose the encoder on expert questions in the language; verify the
   licence; read the engine's run into the recipe; measure `ctx`.
7. Create as the guinea pig, acknowledge the licence, let the worker fetch
   the full profile; watch the first pass as the checklist in §5.
8. Build, snapshot, check; run `tools/legal_pilot.py coverage` first — a
   gold article the collection does not hold is a configuration error, not
   a score — then `run`, then the temporal arm after a person reviews its
   stubs. Record the numbers in the review before pinning anything that
   depends on them.
