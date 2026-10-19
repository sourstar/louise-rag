# Annotation collections: claims beside a corpus (2026-10-09)

*Operator decisions, 2026-10-09. Adopted into 02 §2.10, 05 §5.3 and §5.5,
09 §9.3, §9.4, §9.5 and §9.14 (each marked "amended 2026-10-09"). Code:
`app/hold/annotations.py`, `app/ask/annotations.py`; the first instance's
claims: `tools/annotations_from_matches.py`.*

## Why

[classical-translation.md](classical-translation.md) found that 41 % of
gutenberg's Latin and Greek text already has an English version of the same
work in the collection. Linking them helps search. But Project Gutenberg's
metadata never says "this is a translation of ebook N". The links come from
a matcher, from Wikidata and from people. **They are claims made by us.** A
relation of the gutenberg collection must be "explicit in the source"
(05 §5.5), so these links cannot be relations of gutenberg.

The operator's ruling: they live in a side collection of **annotations**,
"a side contribution to the original corpus that can enrich the knowledge
search without infringing 05 §5.5". And "an annotation fits a collection
from a snapshot date onwards, by design. Thus this snapshot date shall be
mentioned in the codex."

## The shape

An annotations collection is a collection like any other. It has its own
Codex, SHARD, snapshot chain, members, checks and trust level, and it is
shared, fetched and adopted the same way. What differs:

| | an annotations collection |
|---|---|
| Codex | version 7, `plugin: "annotations"`, `annotates: {collection_id, from_snapshot}`; `space`, `work` null; `layers: []`; `origin.spec = {source_id: "annotations", nonce}` (the deposit rule: the annotator is the origin, and the nonce keeps two annotators' collections apart); green, public, open, root only; plan ⊆ `roots`, `records`, `license`, `signature` |
| records | one claim each, a §9.5 line: `lang "und"`, `title ""`, `sections = [["claim", <canonical JSON of the claim>]]`, `extra {}`, `origin "claims.jsonl#<line>"`, `origin_hash` = BLAKE2b-128 of that line |
| relations | each claim as its edge `subject → object`, typed `annotations:<type>` |
| parts | one slice, `all`; numbered by (subject, object, type) bytes; 50,000 claims a part |
| snapshot | `files` = `validation.json`, `LICENSE-TERMS` (null), the seals file (null). No calibration member: there is no space |
| on a node | its collection file names no `[encoder]` and says `kinds = ["records", "relations"]`. `load_all` (what the worker ingests and a search ranks) leaves it out, and `load_annotations` lists it. Sharing (seeder, seeding, digest, fetch), `hold collection show`, `hold check`, traversal and the search's facts read it from there |

Nothing in the target changes. Its records, parts, snapshots and ids stay
byte for byte what they were (01 promise 9, invariant 11).

**05 §5.5 holds.** In this collection the source is the annotator. Its
edges are the annotator's explicit statements, and nothing here is inferred
by the platform. The relations rule reads "explicit in this collection's
source", and here the source is the annotator.

### The claim

```
{type, subject, object, subject_lang, object_lang,
 method, evidence, confidence, contributor,
 subject_hash, object_hash, target_snapshot}
```

- `type`:
  - `same-work`: the two records are versions of one work in different
    languages, and neither is said to be the other's source;
  - `translation-of`: the subject translates the object. The source
    language is `object_lang` and the target language is `subject_lang`.
- `subject`, `object`: uids of the target.
- `method`: one of `{kind: "matcher", id, version}`, `{kind: "wikidata"}`
  or `{kind: "human"}`. What the method saw goes in `evidence`, an object
  with no floats in it.
- `confidence`: a decimal string from 0 to 1.
- `contributor`: who makes the claim.
- `subject_hash`, `object_hash`, `target_snapshot`: written by `create`.
  They are the two target records' `content_hash` in the snapshot the claim
  was checked against, and that snapshot's id. An annotator may state them;
  then they must agree.

A claim made twice in one file is refused. So is the same pair either way
round for `same-work`. The claim travels in `sections` and not in `extra`,
so that `content_hash`, and with it `records_root` and the `records` check,
covers all of it. With an empty title and empty sections every claim would
hash the same.

### The time rule

The simplest rule that keeps it honest:

- No claim is attached to an answer read from a target snapshot **earlier**
  than `annotates.from_snapshot`.
- At or after it, a claim whose two hashes equal the target's content hashes
  as this peer holds them is `current`.
- A claim where one of the hashes differs is `stale`. It is still shown,
  labelled, because the claim was made about other text, and a reader decides
  what that is worth.
- Where this peer's manifest has no hash to compare, the claim is `unknown`.

A deterministic method re-checks by running again and publishing the next
annotations snapshot with new hashes. That re-issues the part under 09 §9.5's
superseded names, and it is not built yet: `create` makes the first snapshot
only and refuses a second. A reading peer never runs a method.

### Search

A result whose record is the subject or the object of a held claim carries
`annotations`, a list. Each entry gives:

- the record at the other end: uid, title and language, as the target holds
  it;
- the claim: its type, role, method, confidence and contributor, and its
  state;
- the annotations collection's id, snapshot and trust level.

The entry is a **fact beside the result**, like a reference source's row or
a figure. It is never scored, never a leg and never fused (invariant 4), and
the lists keep their order and membership. A client may group a work's
versions by these entries.

An annotations copy that this peer may not use attaches nothing. Two cases:
it is in quarantine, or its licence is not acknowledged. Either way the
response names it under `withheld`. `louise_traverse` follows the edges and
resolves them against the target held here.

Cost per search: the claims are indexed once per snapshot, keyed on the
drag's path. Each search then makes one indexed manifest query per target
collection, over the uids that the attached claims name.

### Trust and quarantine

**Quarantine (03 §3.6).** A copy waits for its own `roots` only. The other
tests do not apply:

- there is no space, so no gate and no vector leg;
- there are no chunks;
- the origin is the annotator, so `origin_check` is `not-planned`.

**Trust level.**

- `checked`: three independent keys have passed `roots` and `records`.
- `verified`: eight keys have.

Whether to believe the CLAIMS is the adopting peer's decision, and holding
the collection is that decision. Each claim says how it was made and how
sure its maker is.

## The first instance: gutenberg's Latin and Greek, same work in English

`tools/annotations_from_matches.py` turns `classical_coverage.py`'s
`coverage.json` into claims. It writes one `same-work` claim per matched
pair, with the la/el book as subject and the English record as object, and
method `{matcher, louise-classical-matcher, 1}`.

The type is `same-work`, not `translation-of`, because the matcher does not
know which side is the source:

- gutenberg's `el` is Modern Greek. It is often a translation itself, so
  Zervos' and Crawley's Thucydides are both translations.
- A Latin book may translate an English one (*Mysterium Arcae Boulé*).

**Confidence.** The confidence comes from the pair's strongest signal and
the 30-book hand check (seed `precision-30`). It is (right + 1) / (checked +
2):

| tier | signals | right / checked | confidence |
|---|---|---|---|
| wikidata | `wikidata-edition`, `wikidata-wikipedia` | 17 / 17 (wikidata and title together) | 0.95 |
| title | `title ≥ 0.6` | 17 / 17 (wikidata and title together) | 0.95 |
| llm | `llm` alone | 10 / 13 | 0.73 |

The unit checked was the book: "a correct English version among its
matches". It was not the pair. A book that the llm gave several candidates
carries wrong pairs, so the llm tier's pair precision is below 0.73. For
example, *Ιφιγένεια εν Αυλίδι* was matched to *Iphigenia in Tauris* and
*The Trojan Women*. Every claim's evidence says so.

**Threshold: 0.73, the default.** The 90 % precision (27 of 30) was
measured with all three tiers included, so the cut that gave it is the llm
tier's confidence.

| cut | claims | books | la | el | result |
|---|---|---|---|---|---|
| 0.73 (default) | 357 | 141 | 39 | 102 | every match: the 41 % of the la and of the el tokens |
| `--min-confidence 0.95` | 261 | 97 | 28 | 69 | the wikidata and title tiers, 17 of 17 on the check |

The six books that the recall check linked by hand are not included. They
would be `method: human`.

## Not done here

- **Updating an annotations collection.** A second snapshot would re-check
  claims, add claims or tombstone claims, re-issuing parts. This needs a
  `hold annotations update` that writes superseded part names.
- ~~**Grouping in the lists.**~~ Done 2026-10-09: the search shows each
  work once from the `same-work` / `translation-of` claims held
  (05 §5.3 "One hit per work", `app/ask/works.py`).

## The second instance: gutenberg's works (2026-10-09)

`tools/annotations_from_works.py` turns the one-language measurement's
`records.jsonl` and `wiki_map.json` (`one-language-per-work.md` §1) into
4,376 `same-work` claims over 6,060 books — exactly the 1,694 works that
note measured:

- 4,357 `{kind: wikidata}`: the books whose "Wikipedia page about this
  book" lines resolve to one Wikidata item, as a star on the item's lowest
  ebook number; a book naming several items (an anthology) claims nothing.
  Confidence 0.96875, (30 + 1) / (30 + 2) from the 30/30 hand check.
- 19 `{matcher, gutenberg-editions-note, 1}`: PG's own "several editions of
  this eBook" note, where Wikidata had not already joined the pair.
  Confidence 1. These two can be the same language: `same-work` there
  means editions of one work, which is what grouping needs.

**On the node since the unattended run of 2026-10-10** (München, read-only,
2026-10-19 evening — this paragraph had said "not created yet"): two
annotations collections, both annotating gutenberg `sha256:7340dde6…` from
snapshot 2026-09, both `kinds = ["records", "relations"]`, `embed = off`,
`share = false` (so no peer holds them yet), their bundle folders linked
under `/bundles`:

- `gutenberg-works`, codex `sha256:102cdf11…`: 4,376 claims (4,357
  Wikidata, 19 editions notes) over 6,060 books — the works above;
- `gutenberg-same-work`, codex `sha256:0dbf3721…`: 261 claims — the
  classical matcher at `--min-confidence 0.95` (the wikidata and title
  tiers, 17/17 on the check), not the default cut's 357.

Regenerated to scratch the same day for a check: the works tool gives
4,376 again; the matcher's default cut 357; the two files share 86
unordered pairs. Open for the operator: whether the classical collection
moves to the default cut (357) or the two merge (`tools/annotations_merge.py`,
2026-10-19: a pair stated twice keeps the higher confidence) — either is a
NEW snapshot of an annotations collection, which `hold annotations update`
does not write yet, never a recreate of these; and whether to set `share =
true` so peers group works too. `hold collection create annotations
--dry-run` (2026-10-19) measures a claims file against the target snapshot
before any write. Inputs: the works measurement's `records.jsonl` and
`wiki_map.json` live on the dev PC (`tools/measure/out/onelang/`, 2026-10-09);
the classical `coverage.json` on the node (`/root/xlate-classical/out/`).

The operator's "gutenberg graph collection" (2026-10-19 evening) is NOT
these: it is `gutenberg-graph`, the sibling of gutenberg and wikidata
([wikidata-plugins.md](wikidata-plugins.md), [wikidata-implementation.md](wikidata-implementation.md)),
which waits on the wikidata collection and decisions D1–D5.

- **A target with other annotation collections.** Several annotations
  collections on one target each attach their own facts, labelled by
  collection id. Nothing merges them, deliberately — two ANNOTATORS stay
  two collections; one annotator's two claim files are one collection
  (`annotations_merge.py`).
- **Scale.** The claim index is in memory per copy. That is fine for
  thousands of claims. Millions would want the manifest instead.
