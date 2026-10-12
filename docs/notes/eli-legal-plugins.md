# ELI as the template for EU legal corpora — and where it clashes with the pack

**Status: §6's rules BUILT 2026-09-14 as `app/plugins/eli.py`, with
Normattiva the first plugin on it (operator's instruction); the manifest
block of §5.3 is not built (a wire-format change). See
[`STATUS.md`](STATUS.md), "Normattiva on a generic ELI layer".**
**Amended 2026-10-12 (operator's decision, normattiva-multivigenza-review.md
§3–§4): the RECORD pins the Expression, not the snapshot — one record per
version of an act, its id sorted by the start of validity, the validity
resolved by key; the snapshot keeps 02 §2.2's meaning, when the origin's
state was held. §5.1's resolution and §6's rule 2 are amended below;
rule 7's key is `in_force`, as 09 §9.5 names it.** Originally:
PROPOSAL for evaluation.
Written 2026-08-26 on the operator's instruction to treat **ELI** (European
Legislation Identifier) as the general shape for legal plugins covering EU
member states, to consider only an **ELI-compliant corpus form**, and to
check that form against the pack architecture.

**Source status.** The Italian implementation specification —
*"ELI – European Legislation Identifier: extension proposal for the Italian
Official Gazette and Normattiva portals"*, **16/04/2018** — was supplied by
the operator and is now **read in full**; everything about Italy below comes
from it. The EUR-Lex ELI register page is still unfetchable (egress), so the
general ELI description in §1–§2 remains from published secondary
descriptions.

Two things about that document are themselves findings. It is an
**extension proposal**, and its §4 — "the situation to be": use cases,
generalised URI schema, metadata — is **empty, headings only**. So the
document describes the AS-IS implementation and its stated goals were never
written up: notably *"address articles and annexes"*, i.e. sub-act
granularity. See §5.8.

---

## 1. What ELI is

Three pillars, and only the first two matter to an ingestion pipeline:

- **Pillar I — web identifiers.** A URI template per jurisdiction, built
  from the act's own coordinates.
- **Pillar II — the ELI ontology.** An OWL vocabulary for legislative
  metadata, structured on FRBR and reusing Metalex's names for the
  skeleton.
- **Pillar III — serialisation.** Recommendations for embedding that
  metadata in legislative websites (RDFa). This is how an origin *publishes*
  metadata; §5.6 explains when it matters to us and when it does not.

## 2. The FRBR triple is the part that matters

The ontology's entry points are **`eli:LegalResource`**,
**`eli:LegalExpression`** and **`eli:Format`**, linked by the FRBR backbone
`is_realized_by` / `is_embodied_by` (and their inverses):

| ELI class | FRBR level | for legislation |
|---|---|---|
| `LegalResource` | **Work** | the act as an abstract legal object — "d.lgs. 82/2005", independent of version or language |
| `LegalExpression` | **Expression** | one *version* of it, at a point in time, in a language |
| `Format` | **Manifestation** | that expression serialised — AKN, XML-NIR, HTML, PDF |

This is not bibliographic pedantry. It is the exact distinction every
question in `gazzetta-plugin.md` §6 was circling without a vocabulary for it:
**an amended act is not a changed document, it is a Work with a new
Expression.** Adopting the triple settles several design questions that were
otherwise matters of taste (§5, §6).

Key properties, beyond the skeleton: `jurisdiction`, `type_document`,
`number`, `date_document`, `date_publication`, `version_date`, `in_force`,
`date_applicability`, and the relation set `cites`, `amends`,
`repeals` / `repealed_by`, `corrects` / `corrected_by`, `commences` /
`commenced_by`, `applies` / `applied_by`, `is_member_of` / `has_member`.

Note one distinction ELI is careful about and most schemas are not: **entry
into force and applicability are different dates** — a text may come into
force today and become applicable in three months.

## 3. The Italian implementation, from the specification

**Both portals implement ELI, over different objects.** `gazzettaufficiale.it`
implements **all three pillars** and identifies the *publication*;
`normattiva.it` extends that scheme and identifies the *consolidated norm*.
The spec is explicit that consolidated versions are **never republished in
the Gazzetta** — Normattiva is an added-value service, and each consolidated
version points back to the original act, which is the only one the OJ
published.

**Gazzetta Ufficiale — the publication:**

    /eli/gu/{yyyy}/{mm}/{dd}/{numGU}/{tipoSerie}/{formato}
    /eli/gu/{yyyy}/{mm}/{dd}/{numGU}/{tipoSuppl}/{numSuppl}/{tipoSerie}/{formato}
    /eli/id/{yyyy}/{mm}/{dd}/{codiceRedazionale}/{tipoSerie}/{lingua}[/{formato}]

Issues serve `html` (default), `pdf`, `pdfp` (single-page PDF) and `p7m`
(digitally signed); supplements default to `pdf`. **Acts serve `html` only,
`lingua` = `ita` only.**

**Normattiva — the norm, in two patterns that are not equivalent:**

    by type:  /eli/stato/{tipoAtto}/{yyyy}/{mm}/{dd}/{num}/{versione}[/{dataVersione}][/{lingua}][/{formato}]
    by id:    /eli/id/{yyyy}/{mm}/{dd}/{codiceRedazionale}/{versione}[/{dataVersione}][/{lingua}][/{formato}]

`versione` ∈ {`ORIGINAL`, `CONSOLIDATED`}; `dataVersione` is `YYYYMMDD` and
is **ignored** when `versione` is `ORIGINAL`; `lingua` = `ita`, `formato` =
`html`.

**The correction this document forces.** An earlier draft of this proposal
treated `/eli/stato/{type}/{date}/{number}` as the Work key and built the
synthetic `native_id` from that tuple. **The specification says that tuple is
not unique.** Its own example:

    /eli/stato/REGIO_DECRETO-LEGGE/1936/04/16/886/ORIGINAL/
        → two distinct legal acts, disambiguated only by the user picking
          from a result list:
             /eli/id/1937/01/16/036U0886/ORIGINAL
             /eli/id/1936/05/26/036U0886/ORIGINAL

The spec is unambiguous about which form is authoritative: the `/eli/stato/`
pattern is *"used for navigation by users"* and *"can be considered an alias"*;
the `/eli/id/` pattern *"guarantees the uniqueness"*, is *"used to express the
ELI metadata inserted in the pages"*, and is *"the permalink of the
legislation"*.

> **The Work key is (publication date, `codiceRedazionale`)** — not
> (type, date, number). `uid` is the `/eli/id/` permalink. The
> `/eli/stato/` form is an alias worth recording in `keys`, never the
> identity.

**Controlled vocabularies are published as SKOS**, and are the authoritative
source for any type table (§5.2):

    http://www.gazzettaufficiale.it/eli/tables/resource-type
    http://www.gazzettaufficiale.it/eli/tables/issuers
    http://www.gazzettaufficiale.it/eli/tables/versions

**Metadata.** The GU exposes `eli:LegalResource`, `date_document`,
`date_publication`, `id_local`, `is_realized_by`, `type_document`,
`uri_schema`, `version`, `eli:LegalExpression`, `is_embodied_by`, `language`,
`publisher`, `eli:Format`, `format`. Normattiva adds `eli:version`,
`eli:version_date`, `eli:title`, `eli:is_member_of`, `eli:publisher_agent`
and `owl:sameAs`. Serialisation is **RDFa in the page**, with the prefixes
declared on `<html>` (`eli:`, `na:`, `gu:`) — Pillar III is live, which
matters for §5.6.

## 4. Mapping onto Louise

| ELI | Louise | fit |
|---|---|---|
| Work URI (`/eli/id/…`, the permalink) | `CanonicalDoc.uid` | clean |
| Work key (publication date + `codiceRedazionale`) | derived integer `native_id` | lossy, hashed, collision-checked (§5.2) |
| `/eli/stato/…` alias | `keys` | clean — an alias, and **not unique** (§3) |
| Article / annex | **no ELI identity exists** | §5.8 — our own sub-reference |
| Expression (version + point-in-time) | **nothing** | §5.1 — the real clash |
| Expression language | `documents.lang`, and a profile per language | clean (the `embedded` plugin's `additive` precedent) |
| Format | what acquisition fetches (AKN) | clean |
| `cites` / `amends` / `repeals` … | `RefLink` typed traversal edges | clean, and the main template payoff (§5.4) |
| ELI date set | `CanonicalDoc.date` (one field) | under-specified — §5.5 |
| `jurisdiction` | **nothing in the manifest** | §5.3 |

## 5. Clash analysis

### 5.1 The platform has no Expression axis — and the snapshot IS one

**The clash.** `documents` holds exactly one current content per `uid`, with
`raw_hash` for change detection. ELI says a Work has many Expressions
*simultaneously* — they are parallel resources, not a history. There is no
column for "which Expression is this".

**Why the obvious fixes are wrong.** Making `uid` the *Expression* URI
multiplies the corpus by the amendment count and puts near-identical
versions of one article into the same RRF list (`gazzetta-plugin.md` §6's
rejected option (b)). Making it the Work URI and letting content drift is
what the current design does — and it is unreproducible, which is the open
question that document recorded as §12.7.

**Amended 2026-10-12:** the resolution below was measured against the
multivigente archive and withdrawn — a snapshot chain holds the state at
its cutoffs only, and 6.7 % of all version transitions (37 % in the codes)
are shorter than a month (normattiva-multivigenza-review.md §1–§2). The
record pins the Expression: `uid` names one version of one Work, the Work
URI stays in `extra.eli_work` (and in `keys`, since `app/refs/normattiva.py`
registers `eli` and `urn` — the by-id form and the URN:NIR verbatim), and `--as-of`
selects by validity. The paragraphs below stand as the record of the
earlier reasoning; what survives of it is rule 2 — the Expression is read
back from what the origin returned.

**The resolution, and it is a genuinely good one:** `uid` is the **Work**
URI, and the collection's **`snapshot` pins the Expression**. The pack
manifest already carries `"snapshot": "2026-08-26"`; under ELI that string
is not merely a build date, it is the **point-in-time coordinate**, and it
belongs in the fetch URL:

    …/DECRETO_LEGISLATIVO/2017/03/06/40/CONSOLIDATED/20260826

> **A pack is the set of Works of a corpus, each at one Expression, named by
> the snapshot.** Two peers fetching a week apart no longer hold different
> bytes for the same act: they hold *the same* bytes if they use the same
> snapshot, because the point-in-time is in the URL rather than in the
> calendar.

That closes `gazzetta-plugin.md` §12.7 outright. "Vigente at fetch time" was
only a moving target because the fetch did not name the date; ELI makes
naming it the *compliant* way to fetch. And a snapshot delta becomes exactly
what it should be — the corpus advancing from one Expression set to the
next.

**A subtlety the specification is explicit about, and it improves the
design.** The point-in-time *in the metadata* is **not** the one the caller
asked for: it is the date of the **consolidation**. Ask for the text in
force on 2010-01-01 and, if the last update was 2008-03-15, the metadata
answers `CONSOLIDATED/20080315`; ask for a date preceding the first update
and it answers `ORIGINAL`. The spec tabulates every combination.

So the requested date is a **query**, and the returned `eli:version` +
`eli:version_date` is the **answer** — and it is the answer that identifies
the Expression. Two peers requesting different snapshot dates with no
amendment between them get the *same* Expression, and should record it as
the same thing. Hence:

> **Record the Expression from the returned metadata, never from the
> requested URL.** `raw_hash` then changes exactly when the consolidation
> changes, which is what invariant 2's delete-then-insert idempotence
> wants, and a re-fetch at a later snapshot that crosses no amendment is
> correctly a no-op.

**Verdict: no clash, once the snapshot is read as an ELI coordinate and the
Expression is taken from the response.** This is the single most useful
thing ELI contributes here.

### 5.2 URI identity vs integer `native_id`

**The clash is real and structural.** ELI's premise is URI addressability;
Louise requires `native_id` to be integer-parseable (`layers.py:120`,
`worker.py:260`, Lance's `book_id`, every `CAST(native_id AS INTEGER)`
selection, `idx_documents_native_int`).

**And reading the specification made it worse before it made it better.**
The earlier plan — a sortable integer `TTYYYYMMDDNNNNNN` over
(type, date, number) — is dead twice over: that tuple is **not unique**
(§3), and the unique key contains `codiceRedazionale`, which is
**alphanumeric** (`005G0020`, `036U0886`, `22A03920`) and cannot be parsed
as an integer at all.

**What survives:** `uid` and `keys` carry the URIs losslessly, and
`native_id` becomes a *derived* integer rather than a readable encoding:

    native_id = int(YYYYMMDD_publication) * 10**9
              + (blake2b(codiceRedazionale) mod 10**9)

Properties, stated honestly. It is **stable** (a pure function of the Work
key), **date-sortable** in the high digits — so browse ordering and the
`BETWEEN` range predicates in `worker.py:407` still mean something — and
**not reversible**, which is fine because `uid` is the identity and this is
only a join key. It can **collide** within a single publication date; that
is a one-in-10⁹ event per pair, and discovery must **check for it against
the manifest and fail loudly** rather than trust the arithmetic. An
unchecked hash key is the kind of thing that works until a corpus is large
enough to matter.

The act-type vocabulary keeps its rule, now with an address:

> Type codes come from the origin's published SKOS vocabulary —
> `gazzettaufficiale.it/eli/tables/resource-type` — never from a
> hand-invented taxonomy, and the local table is append-only.

They are needed for metadata and filtering even though they are no longer
part of the key.

**Verdict: resolvable, with a rule.** Worth noting for the roadmap that
relaxing `native_id` to accept strings would remove this entirely, and would
also fix the "locators are absolute paths" class of problem — but it touches
every plugin and is not this proposal's to ask for.

### 5.3 The manifest has no `jurisdiction`

For a *general* EU template this matters: a peer holding several legal packs
has no machine-readable way to say "this one is Italian state law, that one
is French". Today it is implicit in `plugin` and `name` — a naming
convention, not a field.

**Proposed additive fields** (same signing argument as
[`reranker-scope.md`](reranker-scope.md) §5 — `canonical_bytes` covers every
field but `signature`, so an added field is signed automatically and an
older reader still verifies):

```json
"eli": {"jurisdiction": "IT", "authority": "stato",
        "uri_template": "/eli/{jurisdiction}/{type}/{yyyy}/{mm}/{dd}/{number}/{version}/{pointintime}/{lang}/{format}",
        "ontology_version": "1.4",
        "work_uri_key": "eli_work", "expression": {"version": "CONSOLIDATED", "pointintime": "20260826"}}
```

**Verdict: a gap, not a clash.** Additive, cheap, and it is what makes the
template reusable across countries rather than a one-off for Italy.

### 5.4 `cites` / `amends` / `repeals` map straight onto `RefLink`

`RefLink` is defined in `plugins/base.py` as "a declared, typed traversal
edge `expand()` may follow". ELI's relation set is exactly a declared, typed
edge vocabulary — and a *standard* one.

**This is the template's main payoff.** If every EU legal plugin declares its
`RefTable`s and `RefLink`s using ELI property names, then a French, Spanish
or Irish plugin exposes the same graph shape as the Italian one, and
cross-jurisdiction `expand()` works without per-country glue. It also gives
the citation EntityResolver a target vocabulary instead of an ad-hoc one.

**Verdict: no clash — a strong fit, and the reason to adopt ELI at all.**

### 5.5 One `date` field is not enough

`CanonicalDoc.date` is a single optional string. ELI distinguishes
`date_document`, `date_publication`, `version_date`, `in_force` and
`date_applicability`, and the difference between the last two is legally
load-bearing.

**Proposed convention** rather than a schema change: `CanonicalDoc.date`
stays the act's own date (`date_document`), and every EU legal plugin puts
the rest in `extra` under **fixed ELI-derived keys**, so the shape is
identical across jurisdictions:

    extra = {"eli_date_publication": …, "eli_version_date": …,
             "eli_in_force": …, "eli_date_applicability": …,
             "eli_type_document": "DECRETO_LEGISLATIVO", "eli_number": "40",
             "eli_work": "…", "eli_expression": "…"}

**Verdict: no clash; a convention worth fixing once for all such plugins.**

### 5.6 Pillar III (RDFa) is not our ingestion path — except when it is

We fetch bulk collections of AKN, not HTML with embedded RDFa — so Pillar III
is not our *ingestion* path. But the specification shows it **is live in
Italy**: the ELI metadata is serialised as RDFa on the pages, with `eli:`,
`na:` and `gu:` prefixes declared on `<html>`. That makes it a **second,
independent source for the same metadata**, useful for exactly one thing:
checking that what `akn.py` parsed out of the AKN agrees with what the
origin publishes about the act. A cheap cross-check on a sample, not a
pipeline.

It is *not* irrelevant to the template. A member state that implements ELI
Pillar III but publishes **no bulk download** has, in the RDFa of its own
pages, a machine-readable metadata source — which is a legitimate fallback
shape for a future plugin, subject to the etiquette and rate-limit rules
that govern any crawl (invariant 5: no bytes outside the central budget).

**Verdict: no clash; record it as the fallback acquisition shape.**

### 5.7 The one place ELI *corrects* the existing proposal: repeal is not deletion

`gazzetta-plugin.md` §3.7 proposed mapping abrogation to a **tombstone**,
reusing PubMed's `DeleteCitation` mechanism — a `DocRef` with `error` set,
permanently excluded from every stage by the `WHERE error IS NULL` gates.

**Under ELI semantics that is wrong, and it would lose legally relevant
content.** `eli:repeals` / `repealed_by` is a *relation between Works*, and
`in_force` is a *property of an Expression*. A repealed act does not cease
to exist and does not cease to matter: it governed facts occurring before
its repeal, and lawyers ask about it constantly. PubMed's DeleteCitation is
a genuinely different thing — the publisher asserting the record should
never have been there.

**The correction:** repeal is **metadata, not a tombstone**. A repealed act
stays indexed and retrievable, carries `in_force: false` and its
`repealed_by` edge, and is **labelled** in results; filtering by force-status
is a query concern. Reserve tombstones for what they mean elsewhere —
a record *withdrawn* from the database.

**Verdict: a real defect found by adopting the standard**, which is the best
argument for adopting it.

### 5.8 There is no ELI identity for an article — and that is where our chunks live

The specification's own stated goal was to *"address articles and annexes,
covering all the particular cases of publication on the Italian OJ"*. Its
§4, where that extension would be defined, is **empty** — headings with no
content, in a document dated 2018.

So, as things stand, **ELI identifies acts, not articles**, while
`gazzetta-plugin.md` §5 makes the *articolo* the chunk. The consequence is
narrow but should be recorded rather than discovered later:

- A chunk's identifier is **ours**, not the origin's — the Work URI plus a
  local sub-reference (`…/005G0020/CONSOLIDATED/20080315#art5`). It must be
  clearly marked as a local extension, never presented as an official ELI
  URI.
- Anything keyed on it — the citation `RefLink`s of §5.4, JuriFindIT's
  article-level ground truth — is therefore keyed on **our** convention. If
  the extension is ever published, the local form has to migrate to it.
- Two natural allies exist for the sub-reference: **Akoma Ntoso `eId`**,
  which the origin's own AKN carries and which is standardised, and
  **URN:NIR**, which addresses articles natively. Preferring AKN's `eId`
  over an invented anchor is the cheap way to stay migratable.

**Verdict: a gap in the standard, not a clash with the pack** — but the one
place where "ELI-compliant" cannot be claimed for the part of the corpus
that actually gets embedded.

## 6. The compliant corpus form, as rules

A legal plugin for an EU member state is **ELI-compliant** in this codebase
when all of the following hold:

1. **`uid` is the jurisdiction's *unique* ELI URI** — the permalink form the
   implementation designates as guaranteeing uniqueness, not a
   human-readable alias that may resolve to several acts (§3). Record the
   alias, URN:NIR and any local id in `keys`.
2. **The Expression is pinned by the record** (*amended 2026-10-12*), one
   record per version of a Work, its id sorted by the start of validity,
   and **read back from the response**: the recorded Expression is the
   origin's own identifier of the version (`ORIGINAL`, `CONSOLIDATED/<date>`),
   never a request parameter and never an `FRBRExpression` date, which the
   origin leaves stale. The snapshot pins when the versions were held
   (02 §2.2), and `--as-of` selects by validity (05 §5.1, amended).
3. **`native_id` is derived from the unique Work key** — hashed where that
   key is not numeric — **and collision-checked at discovery**, never
   trusted (§5.2). Type codes come from the origin's published SKOS
   vocabulary, append-only.
4. **Language is a profile**, `profile_mode="additive"`, encoded into
   `native_id` where a jurisdiction is multilingual — the `embedded`
   plugin's established pattern.
5. **The ELI date set and type/number go into `extra` under fixed keys**
   (§5.5).
6. **Relations are `RefLink`s named after ELI properties** (§5.4).
7. **Force-status is metadata, never a tombstone** (§5.7), under the key
   09 §9.5 names, `in_force` (*amended 2026-10-12*: the plugin wrote
   `eli_in_force`).
8. **The manifest declares its ELI profile** — jurisdiction, URI template,
   ontology version, pinned expression (§5.3).

Nothing in that list is Italy-specific, which is the point.

## 7. What this changes in the Gazzetta proposal

- **§12.7 is resolved** — fetch `CONSOLIDATED` at the snapshot's
  point-in-time, and record the Expression the origin returns (§5.1).
- **§3.7's tombstone plan is withdrawn** — repeal is metadata (§5.7).
- **§3.1's `native_id` scheme is superseded, not corroborated.** The
  readable `TTYYYYMMDDNNNNNN` over (type, date, number) is dead: that tuple
  is not unique and the unique key is alphanumeric. A derived, collision-
  checked hash key replaces it (§5.2). This is the clearest case in the
  whole proposal of a specification overturning a design that reasoning
  alone had made look sound.
- **§6's ReferenceSource gains a standard vocabulary** (§5.4).
- **§4/§5's chunk identity is a local extension**, and should ride on Akoma
  Ntoso `eId` rather than an invented anchor (§5.8).
- **§1.1's ELI bullet is now specification-backed**, and the GU's own ELI
  namespace is a second identity worth recording in `keys`.

## 8. Open questions

1. ~~Read the Italian implementation spec.~~ **Done 2026-08-26** — §3 and
   the corrections in §5.1, §5.2, §5.6 and §5.8 come from it. What it does
   **not** answer, and the API spec might: whether the bulk/API surface
   accepts the same ELI coordinates as the web portal, i.e. whether a
   collection can be requested at a point-in-time at all.
2. **Does the AKN the origin serves carry ELI metadata inline?** Still open.
   If it does, `akn.py` gets the ontology properties for free and §5.5's
   `extra` convention is a straight copy. The spec only documents RDFa in
   HTML pages (§5.6), which is a different surface from the AKN download.
6. **Has the 2018 extension proposal ever been completed?** Its §4 is empty
   (§5.8). If an article-level ELI has since been defined, our local
   sub-reference should adopt it rather than compete with it.
3. **`native_id` as a string** — worth proposing platform-wide (§5.2), or
   is the synthetic integer good enough forever?
4. **Should `eli.expression` be per-space or per-collection?** Per-collection
   is simpler; per-space would let one pack carry the same Works at two
   points in time, which is either useful or a trap.
5. **Cross-jurisdiction federation**: if two ELI-compliant packs are queried
   together, do their `cites` edges resolve across jurisdictions (an Italian
   act citing an EU directive)? That is where the template stops being
   bookkeeping and starts being a feature.
