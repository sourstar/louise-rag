# Normattiva multivigenza — review of the snapshot-chain proposal, and the design that replaces it

Review of [`normattiva-multivigenza.md`](normattiva-multivigenza.md)
(2026-10-12) against docs/01–09, CLAUDE.md's invariants, the French
versioned-corpus study that note calls [R2], and the origin itself, which
was measured again for this review on 2026-10-12. **Status: review and
proposal, for the operator's decisions in §6. Nothing implemented.** The
reviewed note stands as the record of the proposal and of what it
measured; the chain design in its §1 and §4 is withdrawn here, with the
numbers that withdraw it.

## 0. Verdict

The snapshot-chain design does not answer the operator's question, and
it cannot be made to by choosing a cadence:

1. `--as-of` as specified and built answers from the current index only
   the records that did **not** change since the as-of snapshot and
   withholds the rest (05 §5.1, `app/ask/asof.py`). A question about the
   law in force in 1980 is a question about acts amended since 1980, so
   the design withholds exactly its subject. It gives no wrong answer and
   no answer.
2. A regular chain holds the state at its cutoffs only. Measured over the
   whole multivigente archive, **6.7 % of all version transitions are
   shorter than a month** (10,016 of 150,580; 2.4 % shorter than a week);
   in the forty codes it is **37 %** (1,215 of 3,281; 344 under a week;
   89 on consecutive days). A monthly chain never holds those versions
   — text the origin gave us about the entity, dropped — which invariant
   11 forbids. A weekly chain still loses 2.4 %.
3. The chain conflates two times. 02 §2.2 defines a snapshot as "the
   origin's state at the cutoff instant"; a snapshot "1980-01" built in
   2026 from today's archive is the origin's 2026 statement about 1980,
   so two peers backfilling in different years may honestly produce two
   snapshots under one id — the dispute of 04 §4.3 by construction — and
   a historical version the origin later corrects can never reach a
   frozen chain.

The replacement is the architecture [R2] found to work and the one the
platform's own vocabulary supports once the two times are kept apart:
**one record per Expression** (every version of every act is a record),
**validity as a hard per-record filter resolved by key** (the French
paper's deterministic version layer, applied before ranking so no two
versions of one act ever compete — invariant 4), and **snapshots left to
mean what 02 §2.2 says they mean**: the transaction time at which we
held what the origin had published. §3 gives it in full; §2 lists what
the review found; §4 names the amendments; §5 answers every open point
of the reviewed note; §6 lists the decisions that are the operator's.

On the way the review found one **blocking defect in the plugin as
built** — discovery ranks a VIGENZA member above an ORIGINALE one, and
the VIGENZA member of a repealed act is its repeal state with no text,
so 79,922 royal decrees would be indexed empty while their text sits in
the repealed-acts collection (§2, D1). Promise 8: fixed before any
collection is created.

## 1. What was measured (2026-10-12)

Everything below was read from the origin's zips on 2026-10-12, without
downloading them: the API's tokenised download URL answers Range, so
each zip's central directory (its last few MB) was fetched into a sparse
file of the full size and listed with `unzip`, and single members were
fetched by their recorded offsets the same way. The method is itself a
finding (§3.10): a member is addressable in tens of KB.

**The multivigente archive, all 22 collections that serve it.**

| collection | members | size (MB) |
|---|---|---|
| Regi decreti | 201,929 | 1,695 |
| DPR | 57,640 | 762 |
| DL e leggi di conversione | 14,459 | 54 |
| Decreti Legislativi | 13,813 | 1,275 |
| Leggi delega e relativi provvedimenti delegati | 5,906 | 619 |
| DL decaduti | 5,779 | 29 |
| Testi Unici | 5,344 | 528 |
| Atti di recepimento direttive UE | 4,136 | 235 |
| Regolamenti governativi | 4,062 | 134 |
| Codici | 3,321 | 1,205 |
| Decreti legislativi luogotenenziali | 3,276 | 14 |
| Leggi finanziarie e di bilancio | 3,027 | 563 |
| Leggi di ratifica | 2,879 | 42 |
| Regolamenti ministeriali | 2,836 | 51 |
| Regolamenti di delegificazione | 911 | 51 |
| DL proroghe | 633 | 30 |
| DPCM | 603 | 13 |
| Regi decreti legislativi | 295 | 1.4 |
| Leggi contenenti deleghe | 277 | 12 |
| Leggi di delegazione europea | 160 | 11 |
| Leggi costituzionali | 95 | 0.9 |
| Atti di attuazione Regolamenti UE | 68 | 1.6 |
| **total M** | **331,449** | **7,330** |
| Atti normativi abrogati (ORIGINALE only) | 123,868 | 477 |

Across the 22: **157,621 distinct acts; 308,201 distinct (act,
version-date) pairs; 168,934 VIGENZA members**, of which **87,798
(52 %) are dated on one of five days** — 2008-12-22, 2009-12-16,
2010-12-16, 2011-02-10, 2025-05-09. Three such members were opened: each
is the act's **repeal state** — every article reads "ABROGATO DAL …"
(two of the three name the repealing act: D.P.R. 13 dicembre 2010 n. 248
and L. 7 aprile 2025 n. 56), the lifecycle is the original publication
plus one event, and the body carries no text. The five dates are
consistent with the *taglia-leggi* repeals and the 2025 one. 4,142 acts
appear in more than one M collection; 90,118 of the 123,868 repealed
acts also appear in an M collection (79,922 of them in Regi decreti).

**Regi decreti.** 91,337 acts; 80,664 have a VIGENZA member; **79,407
end on a mass-repeal date**; 68,273 have exactly two members (the
original and the repeal state). The rest are live, amended royal
decrees with real histories: codice penale (R.D. 1398/1930, 401
versions), codice civile (R.D. 262/1942, 349), R.D. 1592/1933 (349),
ordinamento giudiziario R.D. 12/1941 (186), codice di procedura civile
(R.D. 1443/1940, 189). None of this is scanned material: it is the same
consolidated AKN as every other collection.

**Version gaps.** Consecutive versions of one act less than 31 days
apart: all 22 collections 10,016 of 150,580 transitions (6.7 %), 3,638
under 7 days; Codici 1,215 of 3,281 (37 %), 344 under 7 days, 89 on
consecutive days, none on the same day — 739 of the 1,215 fall in the
2010s and 2020s; Leggi costituzionali 4 of 45 (three pairs fifteen
days apart, one pair one day apart: 1989-01-17 → 1989-01-18).

**Distinct text.** Over the 95 versions of the 50 constitutional laws,
3,304 article occurrences hold **730 distinct article texts**; the 50
latest versions alone hold 553 (551 distinct). Holding every version
costs 1.32× the distinct chunk texts of the current corpus, not 6×.
Measured on the Codici the same evening (the M zip downloaded whole, 1.2
GB, every `<article>` with its tags stripped and whitespace normalised):
3,321 versions of 40 codes hold 2,904,112 article occurrences and
**41,289 distinct article texts**; the 40 latest versions hold 19,444
(19,440 distinct). Every version costs **2.12×** the latest versions'
distinct texts, against 83 versions per act — the carry by chunk hash
(§3.5) leaves 1.4 % of the occurrences for the encoder.

**What a member says about its own version.** The lifecycle lists the
amendment events **up to the member's own version and no later**: the
end of a version's validity is not in its bytes, it is the next
member's date. The `FRBRExpression` date can be **stale**: Statuto
Sardegna V11 (`VIGENZA_2001-02-01`) carries `ita@1993-10-10` while its
ELI alias says `CONSOLIDATED/20010201` and the member name agrees; V12
is consistent. The ELI alias, not `FRBRthis`, is the identifier that
matches the member. An ORIGINALE member's expression date is its entry
into force (§7: measured on 220 originals the same evening).

**The other export forms.** JSON adds `abrogato: si|no`, `storia` (the
same events as the lifecycle, numbered differently from the member
names' `V<n>`), `aggiornamentiAtto`/`attiAggiornati` with URNs (the
AKN's analysis block), and a per-article `dataVigoreVersione` that was
empty in every sampled element. A repealed act's JSON says `abrogato:
si` and **no date**. The ELI-URI export lists one URI per act as of the
export's date. Nothing in any form states when a version stopped being
in force; the AKN choice stands (06 §6.4), with §3.2's derivation.

**The listing.** `collection-predefinite` today: 54 rows, 23 names, M
advertised for 15 (served for 22, as the reviewed note found);
`dataCreazione` is **2026-10-12 on nearly every row** — the zips are
rebuilt daily, so the field is not a change signal. Size is the only
difference a rebuild shows (§3.10).

**[R2], read against the note.** The note's reading is accurate.
Two refinements: the paper's production index holds **three** versions
per article (first, median, last by start date) as a first-stage-recall
device, and it does not spell out its version layer's rule or boundary
handling. Both are superseded here by indexing every version and
admitting exactly one per act per date before ranking (§3.3).

## 2. Findings

### On the chain design

- **F1 — the as-of semantics answer the complement.** `asof.resolve`
  walks every later snapshot, unions `added`, `changed` and
  `tombstoned`, drops hits on those uids and names their parts under
  `withheld` (`app/ask/asof.py:126-155`). For law the changed set is
  the question. The note read this strictness as the feature ("its
  semantics are already the strict ones this use case needs"); it is
  the reason the design cannot work.
- **F2 — a regular chain loses sub-period versions** (the numbers in
  §1). Invariant 11 and promise 9: a version the origin publishes and we
  never hold is information about the entity, dropped silently. The
  note's "a version unchanged between two cutoffs is the same bytes"
  is true and beside the point; the loss is the versions that never
  stand at a cutoff.
- **F3 — a grid answer is stale within the period.** 07 §7.3 takes the
  newest snapshot whose cutoff is at or before the date, so a query
  dated 1983-05-20 is answered from the 1983-05-01 state, while the
  version of 1983-05-11 governed that day. 05 §5.1's own rule, "a
  wrong date is worse than no answer", condemns this.
- **F4 — valid time and transaction time are one field in the chain
  design.** [R6]/[R7], which the note lists, make the distinction; the
  design does not. Consequences: a backfilled snapshot is not "the
  origin's state at the cutoff" (02 §2.2); two peers backfilling in
  different years may differ under one id; `records-vs-origin` can
  only classify a corrected historical version as "changed at the
  origin" (03 §3.2, amended 2026-10-11) and nothing can ever carry the
  correction into the frozen chain.
- **F5 — a per-query cost that scales with the chain.** Resolving
  1980 loads and walks 560 snapshot files (939 on a chain from 1948),
  per query (`asof.resolve`). CLAUDE.md: a per-item cost that grows
  with how much has been processed is a bug at any speed. `MAX_CHANGED`
  itself is not the problem — the changed set is bounded by the number
  of acts, 157,621 < 500,000 — the walk is.
- **F6 — disk.** Every snapshot that changes one code re-issues the
  part holding it with its neighbours; 939 re-issues of the Codici
  parts kept as superseded versions outweigh the versions themselves,
  which §3 stores once.
- **F7 — "authoritatively, the member's own `FRBRthis`" is wrong**
  (§1): the expression date can be stale; the ELI alias is the
  identifier that agrees with the member name. The reviewed note's §2
  and the plugin's `eli_expression` already use the alias; `frbr` is
  kept as the origin wrote it.
- **F8 — excluding Regi decreti is wrong twice.** The premise ("before
  1946 is scanned material") does not hold for this form, and the
  collection carries two things nothing else does: the live, heavily
  amended royal decrees (§1), and the **repeal dates of 79,922 acts**
  that the repealed-acts collection cannot state (§3.2). The floor on
  validity the note proposes for everything else does the right thing
  here too.
- **F9 — the cadence question dissolves.** With validity on the record
  there is nothing to choose; the collection keeps the ordinary monthly
  cadence for the ordinary reason.

### Defects in the plugin and spec as built, found on the way

- **D1 — BLOCKING: discovery indexes the repeal state and hides the
  text.** `_FORM_RANK = {"VIGENZA": 1, "ORIGINALE": 0}` and the rank
  `(form, vig, v)` keep the newest VIGENZA member per act
  (`app/plugins/normattiva/source.py:58,163-168`). For an act present
  in a live collection and in the repealed-acts collection, the VIGENZA
  member is the repeal state — no text, no chunks — and it beats the
  ORIGINALE member that holds the act. Exposure measured today:
  79,922 royal decrees, plus every repealed act of every other live
  collection. eli-legal-plugins.md §5.7 and 05 §5.5 ("a repealed act
  stays indexed and retrievable") are violated by the current code
  before any multivigenza work. Fixed first (promise 8); §3.9 says how.
- **D2 — no slicing scheme.** `app/pack/slicing.py` has no `normattiva`
  entry, so `DEFAULT_SCHEME` applies: one slice, 10,000 records per
  part. Forty codes hold 8,814 articles (the note's own density), so a
  part of 10,000 acts is two million chunks against 02 §2.3's target of
  about 7,000. §3.7.
- **D3 — `in_force` vs `eli_in_force`.** 09 §9.5 and 06 §6.1 name the
  key `in_force`; the plugin writes `eli_in_force`. One of the two
  moves; it is free until a collection exists.
- **D4 — the delta story is slightly off, both ways.** The note says a
  rebuilt zip "is not re-fetched by itself". The engine adopts a
  present file when the origin's HEAD size equals it and downloads it
  otherwise (`app/acquire/engine.py:236-259,668-670`): a rebuild that
  changed the size IS re-fetched whole, and one that kept the size is
  silently kept. With daily rebuilds of 7.3 GB, both are the wrong
  behaviour; §3.10 fixes the mechanism rather than the schedule.
  `dataCreazione` cannot be the trigger the note hopes for: it moves
  every day.
- **D5 — the origin unit is a whole zip.** `records-vs-origin` reads
  the first sampled unit whatever it weighs (03 §3.2), and here the
  unit is a 1.2 GB zip. The Range method of §1 makes the member the
  unit; 02 §2.3 already has the word for a container made seekable by
  an index — `index-page`. §3.10.
- **D6 — the retraction in the note's §0 rests on two reasons that do
  not hold** once validity is a hard filter and the id is derived from
  the version: §3.13.

## 3. The design

### 3.1 One record per Expression

A record is one act at one Expression — one member of the multivigente
zip (the ORIGINALE and each VIGENZA), and the member of the repealed-acts
zip for an act no M collection holds. The **version key** is the
origin's own Expression identifier, the ELI alias tail — `ORIGINAL`, or
`CONSOLIDATED/<yyyymmdd>` — under the Work key (publication date,
codice redazionale): 10 acts in the archive have an ORIGINALE and a
CONSOLIDATED member on the same day, so the date alone is not the key;
the alias is (ELI rule 2, read back from the member).

```
native_id = (days of vigenza_from since 1500-01-01) << 45
          | blake2b(gu_date ‖ codice ‖ eli_alias_tail) mod 2**45
uid       = normattiva:<native_id>
```

*(The hash was nine decimal digits under `YYYYMMDD * 10**9` until
2026-10-13, when the first discovery of the whole archive collided three
times on 2010-12-16 — the taglia-leggi day, ~77,000 versions — exactly as
n²/2·10⁹ predicts; eli-legal-plugins.md §5.2 as amended, parser 7.)*

Date-sortable in the high digits by the member name's date (a VIGENZA's start, an ORIGINALE's publication; §7), which is
what makes it **monotonic in arrival**: a version the origin publishes
next month starts next month, so it sorts after every id already in
its slice (02 §2.2's requirement). Collision-checked at discovery as
today (`eli.CollisionGuard`). `vigenza_from` is the member's
`VIGENZA_<date>`; for an ORIGINALE it is the origin's expression date
(its entry into force — measured and fixed, §7: `original_start`), with
`eli_date_publication` kept beside it as today. The Work stays in `extra.eli_work` and `urn_nir`,
and reaches `keys` when a reference source registers the namespaces
(05 §5.2; §3.12).

**Slicing** by `extra.eli_type_document` and the year of
`vigenza_from`; §3.7 sizes the parts. A version the origin publishes
late for a past year (a consolidation it missed, a correction that is a
new version rather than a changed one) lands in a slice whose parts may
be closed and would be refused by `records build` (02 §2.2). Discovery
knows the open part's first id, so it marks such a `DocRef` with an
error instead — a document-level error the build never sees, counted
and shown by `hold collection show`, which CLAUDE.md's "an error
removes the document from every selection until a human intervenes"
already provides — and the frequency is measured first (§7). If it is
material, the slice key coarsens; the format's rule is not amended on a
guess.

### 3.2 Validity: `from` on the record, `to` derived by key

`vigenza_from` is origin information and is in the record.
`vigenza_to` is **not**: no form of the origin states it, it is the next
version's `vigenza_from`, and 02 §2.3 says a record carries nothing that
can be computed from it. It is derived node-locally, like the facet
postings (05 §5.7, "the index is local and derived"), by a **version
table** keyed on the Work: for each `eli_work`, its records sorted by
`vigenza_from`; `vigenza_to` of each is the next one's start; the last
is open. The table is rebuilt from the records at each snapshot, travels
nowhere, and is written as columns of the manifest, the text index and
the vector table so that a validity predicate is a column range, never
an id list (§3.3).

Three cases close an act's validity, in this order of evidence:

1. **A repeal-state version** (§1): a member whose every article reads
   "ABROGATO DAL …" and whose body yields no chunk. It is a record like
   any other — the origin's statement, kept (invariant 11) — with
   `in_force: false`, zero chunks, and `vigenza_from` equal to the repeal
   date, which closes the version before it. 87,798 acts are closed
   this way, 79,922 of them the royal decrees the repealed-acts
   collection holds without a date (F8).
2. **An `eli:repeals` edge** from a repealing act's active
   modifications (relations, §3.12), when the repealing act is held and
   its modifications name the repealed one.
3. **Neither**: an act only the repealed-acts collection holds (33,750
   today), whose repeal date the open data does not give. Its one
   version is open-ended, `in_force: false`, and every as-of answer
   that returns it is **labelled "repealed; the repeal date is not in
   the open data"**. That over-includes rather than mis-dates, and says
   so; it is the honest residue of the reviewed note's §7.2.

**Built 2026-10-12 (night): case 2.** The plugin's `closures(doc)` — 06
§6.1's second optional hook, beside `relations()` — yields `(key, day)`
for each active modification typed `repeal` whose destination is a whole
act: the day is the destination's own `ita@YYYY-MM-DD` (the Expression
the repeal creates, which the codes' 2023 members carry on every one of
their 152 typed repeals), or the stating version's start where the href
is a URN without a date. The rdf stage records them in the manifest's
`validity_closures` keyed on the stating uid (delete-then-insert, so a
re-parse that no longer states a repeal withdraws it) and each version's
canonical ids in `validity_works`; `recompute` closes the last version
of a Work that still has text at the earliest closure later than its
start, whichever record arrived first. Strict on purpose: a repeal of a
part, or a modification typed otherwise however its text reads
("abrogato" in an insertion), closes nothing — under-closing is case 3's
label, over-closing a silent mis-date. The same strictness now governs
`eli:repeals` edges; the archive's two href forms (URN on older exports,
AKN path on recent ones) are both read, and records carry the act's AKN
work path as a third key (`akn:`, registered by app/refs/normattiva.py,
parser 6) so a closure or a citation by path finds its act. How many of
the 33,750 repealed-only acts this dates is measured on the live
collection (§7).

### 3.3 What a search does

- **Without `--as-of`**: one record per Work — the **latest version
  that has text**, so a repealed act is shown in the last text it had,
  labelled repealed (eli-legal-plugins.md §5.7; and the reason D1 is a
  defect). A derived boolean column of the version table.
- **With `--as-of D`**: the records with `vigenza_from ≤ D` and
  (`vigenza_to > D` or open) — a prefilter on both legs by column
  range, the path `book_ids` takes today in `_search_one_collection`
  (`app/retrieval.py:563-576`) without the id list, which at 157,621
  acts cannot be an `IN` clause. Versions of one act are contiguous
  and non-overlapping, so **exactly one version per act is admitted**:
  no two versions of an act ever meet in a leg or in RRF, which is
  invariant 4 by construction and [R4]'s "validity is a hard
  constraint, not a relevance signal". An act not yet in force at D or
  already repealed is excluded, as the operator asked.
- **Every result carries its Expression**: `eli_expression`,
  `vigenza_from`, the derived `vigenza_to`, `in_force`, and the
  corpus attribution's "testo non ufficiale" beside it (TERMS.txt),
  so a reader never takes a version for the current text.
- **The boundary day**: a version is in force from its `vigenza_from`
  inclusive; the day before, the previous one. The origin's own
  tabulation in the Italian ELI specification (eli-legal-plugins.md
  §5.1) says the same.

The range filter 05 §5.7 declares is the one piece of the ask layer this
needs that is "not built yet" (`app/ask/filters.py:124`); the as-of
path for a validity-declaring collection is its first use.

### 3.4 Snapshots stay what they are

The collection keeps the monthly cadence. A snapshot is the versions
the origin had published by the cutoff — transaction time, 02 §2.2 as
written. Its delta is almost only `added` (the new versions); nothing in
an older version's record changes when a newer one arrives, because
`vigenza_to` is not in the record, so parts are re-issued only for the
origin's corrections of a published version (`changed`, same uid). The
chain's own `--as-of` keeps its meaning — what did we hold at D — and is
not what a legal query asks; §4 fixes which meaning the flag has on
which collection.

Reproducibility is stated honestly: the origin rewrites its zips in
place and keeps no archive, so a peer that fetches late sees versions the
early peer did not (late consolidations), and the Codex's `cutoff_rule`
(`record-date` on `vigenza_from`) reproduces a snapshot only from peers
once the origin has moved — 02 §2.2's last sentence. That is the case
today for the VIGENTE design too; the note did not say it.

### 3.5 Embedding

Chunks are the articles of each version. Measured on the constitutional
laws, every version together holds 1.32× the distinct article texts of
the latest versions, and on the Codici 2.12× — 41,289 distinct texts
across 2.9 M occurrences, where 83 versions per act had suggested far
more (§1). That number is what is embedded, on one condition: **a vector
is carried by chunk hash across records**, not only within one record
as 06 §6.3 reads today. A space is a function from cleaned text to
vectors (02 §2.4), so an identical chunk text in one space has one
vector; carrying it between two versions of one act is RULE 1 —
provably identical bytes reused — and is named in §4 as an amendment,
not done quietly. The repeal states embed nothing.

### 3.6 Storage

Records once per version: on the order of 3 GB of record parts for the
whole archive (the Codici's 1.2 GB zip of XML becomes roughly 0.4× in
cleaned zstd text; measure on the Codici before quoting it), against the
chain design's re-issued parts. Vectors at 1.3× the current-only
design. The origin zips (7.8 GB with the repealed acts) on tank per
`storage-placement`, superseded by each monthly pull (§3.10 shrinks
the pull).

### 3.7 Parts

Slices `type=<eli_type_document>/vig=<year of vigenza_from>`;
`shard_records` about 50 (02 §2.3's 7,000-chunk target: forty codes at
220 articles each are 8,800 chunks; a DPR at about ten articles makes
small parts, which is the cheaper error). A `normattiva` entry in
`app/pack/slicing.py` replaces `DEFAULT_SCHEME` (D2). Densities per
type are measured before the number is fixed (§7).

### 3.8 The pre-Republic floor

The reviewed note's rule stands and gains its mechanism: a version
whose `vigenza_to` is before 1948-01-01 is not acquired into the
collection — decided at discovery from the member list, declared in the
acquisition spec, so the pruning is reproducible (invariant 11 permits
declared pruning). It keeps the 1942 civil code, drops a decree repealed
in 1935, and keeps a royal decree repealed by the 2010 *taglia-leggi* in
the text it had until then. What the rule cannot decide alone is the
33,750 repealed-only acts with no known end, most of them published
before 1948: keep them open-ended and labelled, or drop those published
before 1948 as "no evidence of force after 1947". §6, decision 3.

### 3.9 Repeal, and the fix for D1

Discovery no longer picks one member per act; it yields every member
(§3.1), so the rank rule disappears with the defect. Where one version
exists in two zips (the 90,118 repealed acts' originals, the 4,142 acts
in two live collections), the copy is chosen deterministically — M
before V before O, then path — and held once; the bytes may differ in
metadata between zips, so the choice is recorded in the record's
`origin` as today. The repeal state is a record with no text (§3.2);
the default search shows the last text (§3.3). `in_force: false` is
written on the origin's word only: on a repeal-state version, and on
every version of an act the repealed-acts collection lists.

### 3.10 Acquisition and checks: the zip as an indexed container

Fetch the 22 M zips and the repealed-acts O zip; the V and O duplicates
of `_NORMATTIVA` go (M holds every act's ORIGINALE and every VIGENZA).
Two things the measurement method of §1 makes possible, both new
acquisition machinery and therefore asked before built (CLAUDE.md):

- **A delta by central directory.** Each pass fetches the tail of each
  zip by Range (a few MB), compares member names and CRC32s with the
  manifest, and fetches **only the changed and new members** by their
  offsets. Member CRCs are over the uncompressed XML, so a recompressed
  rebuild changes nothing. A `DeltaSpec` mode for it (09 §9.17), and the
  honest fallback when Range is refused: the whole zip, as today.
- **`origin.unit: index-page`** (02 §2.3, 06 §6.4): the central
  directory is the index that makes the container seekable, so
  `records-vs-origin` re-fetches one member in tens of KB instead of
  the first sampled 1.2 GB zip (D5), and `freshness` (05 §5.3) gains a
  `current()` for this plugin for free.

No sizes are declared, as today and for the note's reason. The size the
probe returns seeds `estimated_bytes` only.

### 3.11 Checks

Nothing new: `roots`, `records`, `chunks`, `records-vs-origin` (per
member, §3.10), `vectors-sample`, `license`, `signature`. A version the
origin corrected after the build is "changed at the origin" (03 §3.2,
amended 2026-10-11), named, neither passed nor failed; the correction
arrives as `changed` in the next snapshot.

### 3.12 Relations, keys and the reference source

`relations()` (06 §6.1) emits `eli:amends`, `eli:repeals`, `eli:cites`
from the act's `active_modifications` and `<ref>`s — eli-legal-plugins.md
§5.4's main payoff, not built — and the version table reads
`eli:repeals` to close validity (§3.2, case 2). The version table is
also the natural **reference source** of gazzetta-plugin.md §6 and 06
§6.5: tables `atti` (work key, type, dates, force status, repealed-by)
and `versioni` (work, from, to, uid), owning the `eli:it` and `urn:nir`
namespaces, with the citation resolver ("art. 5, l. 306/2003, as of
1980-01-15" → the version record and its section) returning **facts by
key, never ranked** (invariant 4). Once it exists, records write their
ids into `keys` as 05 §5.2 requires — a parser bump, free until a
collection is created.

**Built 2026-10-12 (evening): `app/refs/normattiva.py`.** The reference
source `normattiva-refs` owns `urn` and `eli`; its tables are `atto`
(URN:NIR, type, year, number, date, title, denomination, the Work's ELI,
GU code and date, read from each M member's head — 24 KB by Range, the
two base64 codes decoded) and `versione` (uid, URN, expression,
`valid_from`, member), linked `versione.urn → atto.urn`, so
`expand_bounded` on an act pins its versions as facts exactly as a gene's
variants. The `CitationResolver` turns "l. 306/2003", "legge n. 306 del
2003", "legge 31 ottobre 2003, n. 306" or "art. 5, comma 3, della legge
306/2003" into the act's entity, the article kept on the label; a
`CompositeResolver` runs it beside the gene resolver (`app/refs`), and
`row_of` looks a table row up by the id after the namespace or, where the
namespace is a URI scheme, by the whole id. **The canonical id of an act
is its URN:NIR verbatim** — `urn:nir:stato:legge:2003-10-31;306`,
namespace `urn` — never `urn:urn:…`; 05 §5.2's examples amended to say
so. The plugin at parser 4 writes `keys = [eli by-id form, URN]`, sorted
(05 §5.2, 09 §9.5); free, no collection exists. Relations by `eli:repeals`
closing a repealed-only act's validity (§3.2 case 2) remain open.

### 3.13 Why the reviewed note's retraction does not hold

The note retracted "versions as documents" for two reasons. (1) "It
multiplies the corpus by the amendment count and puts near-identical
versions into the same RRF list" — the multiplication is 1.32× in
distinct text, and no two versions of an act enter one list, because
validity is a prefilter and the default view is one version per Work
(§3.3). [R2]'s P3 — successive versions are mutual nearest neighbours —
is the reason the version must be chosen by key rather than by
similarity, which §3.3 does; it is not a reason to hold one version.
(2) "`native_id` must be an integer" — §3.1's id is an integer,
date-sortable and monotonic in arrival. What does change is
eli-legal-plugins.md §5.1's sentence "the snapshot pins the
Expression": the **record** pins it, the snapshot pins when we held it,
and the ELI Work URI stays the join key for every version. That is the
[R6]/[R7] distinction the reviewed note cites and the chain design
lacks.

## 4. Amendments to name (the spec is in progress; a rule that forces a worse result is changed and named)

1. **05 §5.1, 07 §7.3 — `--as-of` on a validity-declaring collection** (*amended 2026-10-12, built*).
   Where the Codex's `profile` declares a validity axis, `--as-of D`
   selects records by validity on the newest held snapshot (valid time);
   the chain resolution of `asof.py` applies to every other collection
   and, for this one, remains "what we held at D" under a name that says
   so. The result's `snapshot_id` keeps its meaning.
2. **09 §9.3 `profile`** (*amended 2026-10-12, built*) gains the declaration:
   `{"eli": {...}, "validity": {"from": "extra.vigenza_from", "work":
   "extra.eli_work", "kind": "versions-per-work"}}` — opaque to the
   platform except to the ask layer and the index maintainer.
3. **06 §6.3 — vectors carried by chunk hash across records of one
   collection** (RULE 1; *amended 2026-10-12, built*): the same cleaned text in one space has one
   vector.
4. **02 §2.3 / 06 §6.4 — `origin.unit: index-page` for a zip whose
   central directory is fetched by Range**; **09 §9.17 — a `DeltaSpec`
   mode for the central-directory delta**. Both asked, not assumed
   (*decided 2026-10-12, built*: `app/acquire/zipdelta.py`,
   `member_origin.py`; the plugin's spec is `zip-directory`; `hold check`
   without `--origin` re-fetches members by Range on an `index-page` Codex).
5. **05 §5.7 — the range filter**: the validity predicate is a column range on the legs (built 2026-10-12); the general range facet filter of 05 §5.7 stays as it was.
6. **eli-legal-plugins.md §6, rule 2** — "the Expression is pinned by
   the record, read back from what the origin returned; the snapshot
   pins when it was held"; and §5.1 amended likewise.
7. **09 §9.5 / 06 §6.1 — `in_force`**: the plugin writes `in_force`,
   or the documents say `eli_in_force`; one of the two.
8. **normattiva-multivigenza.md §0** — the retraction withdrawn by
   §3.13 here; §1 and §4 withdrawn by §2 here. The note stands as the
   record.

## 5. The open points of the reviewed note, answered

1. **Cadence.** Dissolved (F9): monthly, as any collection.
2. **The repealed-acts residue.** 79,922 of its acts get their repeal
   date from the royal-decree repeal states, more from `eli:repeals`
   edges once relations exist, and the remaining open-ended ones are
   labelled, never silently dated (§3.2). Their text remains the
   original, labelled "original; amended N times before repeal" where
   the metadata says so. `ricerca-asincrona` is not needed for this.
3. **The Constitution.** Not in any predefined collection (confirmed
   today). The API's on-demand collections are built from a search and
   served through the same download endpoint in the same forms, so one
   `RemoteFile` with the query for `urn:nir:stato:costituzione:1947-12-27`
   in form M would add it without new machinery. The exact query is
   untested (§7); until it is, the corpus says the Constitution is
   missing rather than pretending otherwise.
4. **Storage.** §3.6; the origin zips shrink to a central-directory
   pull per pass (§3.10).
5. **Regi decreti.** Included, under the validity floor (F8, §3.8); the
   operator's instruction was given on a premise the measurement shows
   false, so it is put back to the operator (§6).
6. **The delta.** §3.10; `dataCreazione` is not a signal (§1).
7. **The encoder** (the note's §8). Unchanged, and the note's two
   settlements stand: the pipeline exists, and point-in-time is a filter.
   The embedder is trained on one Expression per act; the index holds
   every version, and the filter keeps them from ever competing.

## 6. Decisions that are the operator's

1. **Adopt §3 in place of the chain design** — one record per
   Expression, validity by key, snapshots as transaction time — with
   the amendments of §4. The alternative is to keep the chain and accept
   F1–F6; this review finds no cadence or patch that removes F1 or F2.
2. **Regi decreti back in scope** under the validity floor (F8).
3. **The repealed-only acts with no known end** (33,750): keep
   open-ended and labelled, or drop those published before 1948 under
   the floor as "no evidence of force after 1947" (§3.8).
4. **D1 is fixed before any collection is created**, whatever is
   decided above (promise 8). Under §3 it disappears with the rank rule;
   under the current design the rank must prefer the member with text.
5. **The two acquisition changes of §3.10** (central-directory delta,
   `index-page` unit) — new machinery, asked first.
6. **The naming of `in_force`** (D3).

### 6.1 Recommendations (reviewer, 2026-10-12)

1. **Adopt §3, as one collection** (every version a record; the default
   view is the latest text per Work; no separate "current" collection).
   For: it answers the question with exact dates; holds every version
   (invariant 11); one acquisition, one space, one monthly chain whose
   delta is additions; no per-query chain walk; versions never compete
   (invariant 4). Against: four spec amendments and the range filter to
   build; the ELI note's "snapshot pins the Expression" reverses to "the
   record pins it"; ~1.33× records and chunks; the retroactive-version
   path is unmeasured; late consolidations keep snapshots reproducible
   only from peers (true today as well). Keeping the chain has no
   pros that survive F1 and F2.
2. **Regi decreti in, under the floor.** For: ~1,250 live amended royal
   decrees are law in force (ordinamento giudiziario among them); the
   79,407 repeal states date the repeal of 79,922 acts, which makes the
   as-of exclusion exact for them; the floor removes what ended before
   1948 anyway. Against: the largest zip (1.7 GB, 202k members, ~80k of
   them text-less repeal states to discover and count each pass);
   placeholder dates (1,070 acts dated 1701-01-01) the floor and the
   slicing must tolerate.
3. **Keep the 33,750 undated repealed-only acts, open-ended and
   labelled**, after measuring how many `eli:repeals` edges can date
   (§7). For: a 1925 law repealed by the 2010 *taglia-leggi* was in force
   for 62 years of the Republic and the open data cannot tell it from one
   repealed in 1930 — dropping on publication date would lose
   Republic-era law silently, which promise 9 and "a wrong date is worse
   than no answer" both forbid; a label is an honest unknown. Against:
   ~30k mostly pre-Republic acts stay in the index and can surface in any
   as-of answer after their publication, labelled "repealed, date not in
   the open data"; narrowing later is a new collection, widening later a
   daughter — so keeping is the reversible side.
4. **Fix D1 before anything else, under either design.** For: promise 8;
   79,922 acts indexed empty is a wrong artefact that passes every check.
   Against: none; under the current design the fix is "prefer the member
   with text", and the repeal date is then read from the repeal-state
   member the rank discards, a small addition to discovery.
5. **Both acquisition changes, built together** (they share the
   Range-member fetch), with the whole-zip fallback. For: the monthly pull
   drops from 7.8 GB to the central directories plus changed members; a
   `records-vs-origin` unit becomes tens of KB instead of a 1.2 GB zip;
   `freshness` gains `current()`; comparing member CRCs closes D4's
   same-size silent miss. Against: a new `DeltaSpec` mode and transport
   code to test (zip64, a rebuild mid-pass that moves offsets — caught by
   the member's CRC, answered by re-listing; the tokenised URL's
   lifetime, observed stable within a session); it depends on the origin
   keeping Range, and the fallback is today's cost.
6. **The plugin writes `in_force`**, the key the documents name (09 §9.5,
   06 §6.1), keeping `eli_*` for the ontology fields. For: the ask layer
   and every later legal plugin (the Gazzetta) read one key; free now.
   Against: one asymmetry in `eli.EXTRA_KEYS`, one test to move.

**Operator, 2026-10-12: all six decided as recommended in §6.1.**

### Built 2026-10-12, first part: the plugin, the spec, the slicing (parser 3)

`app/plugins/normattiva/source.py` discovers every member of every zip as a
record — `eli.expression_native_id`, the id of §3.1, with one deviation
from §3.1 as written: an ORIGINALE's id sorts by its **publication date**,
the only date the member name carries, because discovery enumerates
without reading (06 §6.1) — its VALIDITY starts at its Expression date
(`original_start`, fixed by §7's measurement the same evening, parser 5),
which the index carries as a `valid_from` column of its own beside
`valid_to` and `shown`, so the as-of filter never reads the id's digits;
the entry into force the origin states in the preface note is kept beside
it as `extra.entry_into_force`. The version
key is the member name's (`version_key`, agreeing with the ELI alias); the
floor is `VALIDITY_FLOOR = "1948-01-01"` applied at discovery (a version
whose successor starts on or before it is not yielded); one copy per
Expression, M before V before O (D1 gone with the rank rule); the repeal
state is detected (`repealed_in_full`, `repealed_by_text`) and `in_force`
replaces `eli_in_force` (D3). `app/pack/slicing.py` gains the `normattiva`
scheme of §3.7 (`type=<the act type the origin files the member under>/vig=<year>`,
50 records a part) with a `slice_hint` that answers from the manifest row
(D2). `app/acquire/specs.py` fetches the 22 M zips and the repealed acts'
O zip (23 files, 7.8 GB); the V and O duplicates are gone. Tests: 146 in
the affected modules; four real members added as fixtures (three versions
of the Statuto Sardegna, one repeal state). Not yet built: §3.2–3.4's
version table and as-of path, §3.5's carry, §3.10's acquisition changes,
§3.12's relations and reference source — the second part.

**Densities measured for §3.7** (eight VIGENZA members a collection, by
Range, excluding repeal states; articles per act): Codici 409, DPR 123,
Testi Unici 86, Leggi delega 78, Leggi finanziarie 75, Regolamenti
governativi 59, Atti di recepimento 40, Regolamenti ministeriali 19, DL
decaduti 8, Decreti Legislativi 7, Leggi di ratifica 4, Regi decreti 4, DL
e leggi di conversione 1. At 50 records a part the codes' parts run to
about 20,000 chunks and the royal decrees' to 200; one `shard_records` per
collection is the format's, and the middle of the range meets the target.



### Built 2026-10-12, second part: validity by key (`app/validity.py`)

The version table and the search window of §3.2–§3.3, with 05 §5.1,
07 §7.3 and 09 §9.3 amended as §4.1, §4.2 and §4.5 say (§4.3's carry,
§4.4's acquisition changes and §3.12's relations are still to build). The
Codex `profile` carries the declaration (`template.default_profile`, from
the plugin's `default_profile`); the rdf stage records each version and
recomputes its Work's chain in the manifest's `validity` table (`valid_to`
= the next version's start, `shown` = the latest version with text,
`in_force` on the origin's word), one indexed read per document
(`idx_validity_work`, plan-checked); a neighbour whose end or `shown`
moved has its fts and embed marks reset in the same transaction, so its
own stages rewrite its index rows — the fts stage re-adds its chunks with
the three fast fields, the embed stage reuses every vector and updates the
three columns in place (`embeddings.sync_columns`); the index maintainer
drains `synced = 0` rows for a peer whose embed stage does not run. The
legs take the window as a column range — tantivy fast-field ranges on
`valid_from` and `valid_to`, a Lance `where` — never an
id list; the catalog leg is admitted by the table; `--as-of` reaches
`retrieval` as an integer day and a versioned collection is excused from
`asof.resolve`; a result carries `validity: {from, to, in_force,
current}`. Tests: `tests/test_validity.py` (the chain, idempotence and
order, the plan, both legs, the repealed act shown by default and absent
after its repeal, a new version moving its predecessor through its own
stages with zero encoder calls, the maintainer's drain, the retrieval
path). One deviation from §3.3 as written: the tantivy and Lance columns
hold `99999999` for "still in force" (`validity.OPEN`), an index-only
sentinel — a record never carries it, and a result reads it back as null.


### Built 2026-10-12, third part: the carry, the edges, the delta, the member as unit

`app/embeddings.py cross_carry` and the manifest's `chunk_hashes` (§3.5;
06 §6.3 amended); `NormattivaSource.relations()` — `eli:amends`,
`eli:repeals`, `eli:cites` between acts, by URN:NIR and AKN work path (§3.12's
first half; the reference source and the resolver followed in the evening,
§3.12 "Built");
`app/acquire/zipdelta.py` — the directory by Range, the changed members by
Range, the local zip rebuilt from raw bytes, wired into the engine as
`DeltaSpec(mode="zip-directory")` and set on the plugin's spec, every pass
asking each zip's directory and falling back to the whole fetch where it
must (§3.10; 09 §9.17 amended); `app/acquire/member_origin.py` — `hold
check` without `--origin` on an `index-page` Codex re-fetches a sampled
record's member by Range and weighs the unit by the member (§3.10, D5; 02
§2.3 amended). Tests: `tests/acquire/test_zipdelta.py`,
`test_member_origin.py`, `test_engine_zipdelta.py` (against the mock server,
with and without Range, with a delta not worth taking), the carry and edge
tests in `tests/test_validity.py` and `tests/test_normattiva.py`.

## 7. Measurements owed before parameters are fixed

- Chunk density per collection (articles per act, from the V zips'
  members) → `shard_records` (§3.7).
- Distinct article texts across all versions of the Codici: MEASURED
  2026-10-12 — 41,289 distinct across 2,904,112 occurrences in 3,321
  versions, 2.12× the latest versions' 19,440 (§1, §3.5).
- A second central-directory listing of every M zip a month from now,
  diffed against today's → how many versions arrive with a past
  `vigenza_from` (§3.1's refusal path) and how many published versions
  the origin corrects (`changed` rate).
- ORIGINALE members: MEASURED 2026-10-12, ten originals of each of the 22
  collections (220; 113 read from the local zips, 107 by Range). Every
  member states an Expression date; 154 carry the "Entrata in vigore"
  note, and the two agree on 151. Against publication, the Expression
  date is +15 on 132, +1 on 45, +0 on 24 — the vacatio — and 217 of 220
  are one of those or the note. The three others: the Codice della crisi
  (expression 2022-07-15 = the first consolidation, the note's 2020-08-15
  twice postponed — the original text never was in force, and the chain
  gives it an empty interval); a note dated 1939 for 1989; and one
  Expression date BEFORE publication (1989-02-16 for 1989-03-16, the
  note's date, which the first consolidation confirms). Rule, parser 5:
  `vigenza_from` = the Expression date, or the note where the Expression
  date precedes publication, or publication (`original_start`). The id
  keeps its publication-date prefix (discovery reads no member), so
  `valid_from` became the index's third column: both bounds of the as-of
  window are columns, and a query in an act's fortnight before entry into
  force returns nothing.
- The on-demand collection query for the Constitution.
- The articles' token lengths under DAR-legal-it's tokenizer: MEASURED
  2026-10-12 on the 40 codes' latest versions, 8,818 articles — median 241,
  p90 944, p95 1,396, p99 2,943; 2.13 % over 2,048, 0.43 % over 4,096, a
  handful at the model's 8,192 → `space.ctx = 8192`, the engine's own
  window (`normattiva-recipe.toml`).
- The engine block and weights digest of the encoder: READ the same night
  with `hold embedder run` against the node's `vega-dar-legal-it` (served
  that evening, vega.md §12; run sha256:85b9fa1a…) into the recipe.
- Coverage of `eli:repeals` from `active_modifications` for the
  repealed-only acts (§3.2, case 2).
- Whether the 23 predefined collections are the whole database: their
  distinct acts are about 192,000 (157,621 in M + 33,750 repealed-only);
  Normattiva describes itself as every numbered act since 1861. A count
  from `ricerca` would settle it; 06 §6.2's listing contract wants the
  denominator known.

## 8. Tests the design ships with

- Every version of an act is a record; its `native_id` sorts by
  `vigenza_from`; an ORIGINALE and a same-day CONSOLIDATED are two
  records (the 10 cases of §1).
- The version table: `vigenza_to` is the next version's start, open on
  the last; a repeal-state version closes the one before it and yields
  no chunk; a repealed-only act is open-ended and labelled.
- `--as-of D` admits exactly one version per act; a date before the
  first version and one after the repeal admit none of that act; the
  boundary day admits the new version.
- The default view is the latest version with text; a repealed act is
  shown in its last text, labelled.
- The same chunk text in two versions is embedded once (the carry).
- A version in two zips is held once; the chosen copy is deterministic.
- The floor drops a version ending in 1935 and keeps the 1942 civil code.
- A late version for a closed slice is a counted document error, never
  a build refusal and never silence.
- Discovery's rank defect (D1): an act present as a repeal state and as
  an original is searchable by its text.

## 9. How §1 was measured

Each zip's size from `Content-Range` on a one-byte Range request to the
download endpoint; a sparse file of that size; its last 4–48 MB fetched
by Range into place; `unzip -Z1` for the member list; `zipinfo -v` for a
member's local-header offset and compressed size; that byte range
fetched into place; `unzip -p` for the member. The constitutional-laws
zips (AKN M, JSON O and M, ELI-URI O) were downloaded whole (0.9 MB
each). Distinct article texts: `<article>…</article>` elements with
tags, whitespace runs and the `((…))` markers removed, hashed as
strings. Dates compared as `YYYY-MM-DD`; the ORIGINALE's date taken as
its publication date for the gap counts, which makes the first gap of
every act conservative by the vacatio.

## 10. Embedder and reranker, in the light of [R2] (2026-10-12)

**Embedder: `jurifindit/DAR-legal-it`, as decided 2026-09-20, unchanged
by §3.** The measurement stands (gazzetta-plugin.md §7.1b: recall@10
0.8031 against qwen3-embedding-4b-instructed's 0.7748, ndcg@100 0.7704
against 0.7534, dim 768 against 2560, the corpus embedded in minutes
instead of an hour). §3 changes nothing the encoder is trained for: the
version is chosen by key before ranking, so the encoder never has to tell
two versions apart, and the index grows by the 1.32× distinct texts of
§1, which at dim 768 is small. The licence question is closed: the operator states (2026-10-12) that
the weights carry the licence of the material they were trained on,
CC-BY-NC-SA-4.0 — the Hub card itself still declares none — so
commercial peers are consumer-only for this collection, as recorded in
gazzetta-plugin.md §7, and nothing waits on the authors before `create`. Fallback:
qwen3-embedding-4b with `space.query.instruction`. The operator's own
trained encoder (`sourstar/ita-legal-embed`) arrives as a **sister**
(02 §2.7: the same records, a new space, nothing re-fetched) once it
beats DAR on the same 179 questions, which is
`embedder-choice-and-supersession.md`'s path and the reason to pin now.

**What [R2] reveals about the embedder.** (a) Its encoder is
domain-adapted and **proprietary** — "weights, multi-version index and
inference code, all proprietary" — so its result does not transfer, and
such an encoder could not own a Louise space at all (02 §2.4:
obtainable weights). (b) The paper names no base model, no pair count,
no chunk size, and reports no comparison against an off-the-shelf
encoder or BM25 alone; it is not evidence for any model choice. (c) P3
("mutual nearest neighbours under any standard sentence encoder") is
asserted, not measured — and it is the reason version selection must
be a key lookup, which §3.3 makes it; the embedder has no temporal job.
(d) **83.7 % of their questions name the article number**, and the
authors conclude "citing the article is often not evidence of
retrieval". Their 99 % provenance is therefore mostly a citation
resolved by key — in Louise the reference source and resolver of §3.12,
facts never ranked — and the embedder decides only the uncited
remainder, which JuriFindIT's questions measure directly. (e) Their
questions were screened so that the gold value is absent from the
current text: a dense leg finds the article by topic, a number in the
query is the lexical leg's — keep RRF over text and vector (05 §5.1),
as the gazzetta note already insisted. (f) The 8,000-character
"query-windowed extract" is the LLM's context per article, not an
embedding chunk; it coincides with `akn_article`'s `max_chars` default
and says nothing about chunking for embedding.

**Reranker: `jina-reranker-v3` under the p95 ≤ 2 s budget, as decided
2026-08-26, on by default for this collection, measured after the
collection exists.** [R2] tried a fiscal cross-encoder and reports "no
top-1 gain … adds nothing on this niche set" — expected where 84 % of
candidates are a citation and the version layer does the rest. For
Louise the reranker matters only on uncited semantic queries, where it
is unmeasured on Italian law; on gutenberg it measured MRR 0.342 → 0.660
on title queries and 0.121 → 0.223 on descriptive ones (05 §5.6). Two
of gazzetta-plugin.md §7.5's three objections are gone: a candidate is
sent as two whole chunks, never truncated, and every fallback names its
reason under `rerank_withheld`; the third (node-global) is
reranker-scope.md's conclusion. Under §3 the reranker sees one version
per act, because the legs are prefiltered — [R2]'s near-identical
versions never reach it. `RERANK_URL` is outside `space_id`, so this
costs nothing to revisit: measure with and without on the 179 questions
over the built collection, keep it if it holds ≤ 400 ms of the budget;
Qwen3-Reranker-8B offline as the ceiling.

**What to copy from [R2] into the pilot (gazzetta-plugin.md §7.2):** a
temporal provenance arm. Questions dated to a past version whose gold
value is absent from the current text, surfaced from the version diffs
the archive already marks (`((…))` and `passive_modifications`),
scored deterministically — did `--as-of` return the version in force
(provenance), and does the answer carry the value (nugget) — with no
LLM judge. It measures the one thing §3 exists for and costs an
afternoon over the Codici.

Built 2026-10-13 as `tools/legal_pilot.py`: `coverage` resolves the gold
articles BY KEY (the dataset's `file_name` is the act, its `path` the
article; both through the collection's own `validity_works` keys and the
shown version's chunks) and excludes what the collection does not hold;
`run` scores recall, mrr and ndcg over the gold node ids and measures the
REACH of the misses — how many sit one declared edge from a hit — which
answers §7's open point on widening by the edges before anything is built
for it; `temporal-make` mines dated question stubs from the version table
(an article whose text differs between consecutive versions, a numeric
nugget the later text lost) for a person to review, and `temporal-run`
holds `--as-of` to the version's uid and the nugget to its text, with the
undated control. All of it deterministic; it runs once the collection
embeds (legal-collection-template.md §9).

### 10.1 The pilot's coverage run, and what one absent law revealed (2026-10-13)

The harness (`tools/legal_pilot.py`, b1f443e) ran `coverage` on the built,
embedded collection (run `20261013T111846Z`, the other session): **179
questions, 175 covered, 4 excluded; 590 gold articles, 245 held**. By
outcome: not an Italian act 148; no article in the node path 162; act not
held 34; article not among the shown version's chunks 1. Reading them:
"not an Italian act" is JuriFindIT's EU and foreign material, outside this
collection by design. "No article in the node path" is a gold unit that is
not an article — an annex, a preamble, a point of an authority's provision,
a Garante communication or announcement — which the harness scores at
article granularity only (the annexes themselves ARE kept: `akn.py`'s
`Attachment`, rendered under `## Allegati`). Of the 34 "act not held", 29
are authorities' provisions (`provision`, Garante/Consob-style numbers),
which Normattiva does not publish; D.M. 7 novembre 2001 n. 458 IS held,
under `urn:nir:ministero.giustizia:decreto:…` — the harness resolves gold
to `stato` only, a harness fix owed; reg. 12 febbraio 2019 n. 44 and reg.
29 gennaio 2002 n. 4825750 are not state acts by their numbers; and
**L. 9 luglio 1990, n. 185** (arms export; art. 27 and 27-bis) is absent
from the collection AND from every one of the 23 origin zips — no
`LEGGE_19900709_185` member anywhere — while its implementing D.P.C.M.
94/1991 is held.

**What L. 185/1990 reveals: the pre-packaged collections are a thematic
subset of state law.** `collection-predefinite` offers 23 collections —
codes, consolidated texts, constitutional laws, ratification laws,
delegation laws, budget laws, decree-laws with their conversion laws,
legislative decrees, DPR, DPCM, three kinds of regulation, royal decrees,
repealed acts — and NO collection of ordinary laws as such: a law that is
none of those is in no zip. Measured against the year's numbering (shared
by every state act of a year, so the highest number held is a floor of the
year's count), from `validity_works`:

| Year | distinct state acts held | highest number | share |
|---|---|---|---|
| 1960 | 1,823 | 2,036 | 89 % |
| 1980 | 1,081 | 1,265 | 85 % |
| 1990 | 235 | 451 | 52 % |
| 2000 | 262 | 455 | 57 % |
| 2010 | 197 | 277 | 71 % |
| 2020 | 132 | 196 | 67 % |
| 2024 | 129 | 222 | 58 % |

The early decades are carried by the DPR (57,640 members) and Regi decreti
(201,929) collections; since 1990 the collection holds 52–71 % of the
state's numbered acts, and the gap is mostly ordinary laws. §3.10's "M holds
every act's ORIGINALE and every VIGENZA" is true of every act IN a
collection and was read as "every act". Nothing in the records is wrong:
the 318,349 records are what the origin's 23 collections hold, verified
against them (`hold check` PASS). What was wrong is the implied scope —
"Italian state law" is not what this collection is; "Normattiva's 23
pre-packaged collections" is.

**The decision owed (operator).** (a) State the scope: the collection's
card and TERMS name the 23 collections, the pilot keeps its 175 covered
questions, L. 185/1990 is a known absence. (b) Complement the origin: the
API's untested `ricerca/{semplice,avanzata}` and `ricerca-asincrona`
routes ([R14]) are the only published way to reach an act outside the
collections — a search by type and year, or a per-act AKN export by URN —
new acquisition machinery, asked before built (CLAUDE.md), with a second
origin unit beside the zip member and its form recorded with its reason
(invariant 11). The size: roughly 45 % of the Republic's acts since 1990,
about 200 a year. How many matter to a legal question is what the pilot's
`run` will say on the covered side — and [R2]'s "83.7 % of questions name
the article" says what the miss looks like: a cited act that is not held is
a miss by key, the worst kind. Also written by the harness: 200 temporal
stubs for the operator's review (`/data/pilot/temporal-stubs.jsonl`).
