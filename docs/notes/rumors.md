# Rumors — the gap between the corpus and Wikidata, recorded (operator, 2026-10-25)

*Operator: "'The gap' shall be recorded, it is an additional pillar that can
be optionally used for searches, the 'rumors'. Review over review of
wikidata the current 'rumors' shrink, but they are part of the collection.
Rumors are unofficial tags, connections and stuff, not part of wikidata but
somehow derived from the corpus with a reasonable degree of confidence.
It's a unique contribution of louise."*

This supersedes adjustment 1 of gutenberg-graph-strategy.md §10 (PR #4,
"the gap is a view, not a log") and the reading of 02 §2.3 behind it. A
rumor's state depends on a snapshot of ANOTHER collection, and its history
— when it was first seen, which Wikidata review closed it, and how — cannot
be recomputed from the record alone once the next review has happened.
Nothing here is built; the spec changes at the end are for approval.

## What a rumor is

A claim of a corpus-derived annotations collection (gutenberg-graph's
exact and reader layers, gutenberg-graph-strategy.md §5) that the CURRENT
wikidata snapshot does not state:

| kind | from | a rumor while |
|---|---|---|
| a new entity | a kept span with no Q-id, or a cluster of spans (`same-entity` candidates) | no Q-id names it |
| a new connection | a `relation` between two Q-ids | Wikidata has no statement with that subject, property and object |
| a conflicting value | a `relation` whose property is single-valued (P2302 / Q19474404) | Wikidata states another value |
| an unofficial tag | a `mention` linking a record to a Q-id Wikidata does not connect it to | no statement connects them |

The confidence floor ("a reasonable degree of confidence") is fixed per
method and measured on a judged sample before the first snapshot (a link
rate without its precision is not a number). A claim below the floor is
not a rumor and is not published.

## Recorded, and never removed

Each snapshot of the claims collection records every claim's **rumor
state** against the wikidata snapshot it was checked against:

| state | meaning |
|---|---|
| `open` | Wikidata does not state it: a current rumor |
| `adopted` | Wikidata now states it, citing OUR passage as its reference: a person reviewed our evidence and agreed (the statement's GUID recorded) |
| `confirmed` | Wikidata now states it on a reference of its own: independent evidence (GUID recorded) |
| `rejected` | Wikidata states it at deprecated rank, or contradicts it with a reference, or the Mismatch Finder review marked our value wrong |
| `known` | Wikidata already stated it when it was found: never a rumor |

`adopted` and `confirmed` are kept apart: a statement a person entered
from our own passage measures our precision, but it is not a second
source, and counting it as one would let the collection confirm itself.
A rejection seldom reaches the dump, since an editor who dismisses a
mismatch rarely writes a deprecated statement. So `rejected` is also
read from the Mismatch Finder's review outcomes for the rows we
uploaded, not from the dump alone.

Review after review the `open` set shrinks; a closed rumor stays in the
collection with its state, the review that closed it and the statement. A
claim never changes for this: claims are identified by their content
(09 §9.5), so the state lives in its own member keyed by claim uid, and
a state change re-issues that member, not the claims. Nothing published is
deleted (promise 7). `hold annotations update` (annotation-collections.md
"Updating") computes the states against the newest held wikidata snapshot.

## In a search — optional, off by default

A search option `rumors`. When it is on:
- **beside each hit**, its open rumors as facts: labelled *rumor*, with
  confidence, passages and the date first seen. Facts are never scored or
  fused (invariant 4).
- **a rumors list**: records reached from the query's entities through open
  rumors (connections, clusters), shown as its own list beside the results.
  It answers a different question — what the corpus suggests and Wikidata
  does not state — so it is presented separately and never fused into the
  ranking (invariant 4, 05 §5.1).
- **a filter**: "records with an open rumor about Q2044". It restricts and
  never boosts (05 §5.2).

| | pros | cons |
|---|---|---|
| **separate list + facts + filter** (recommended) | the reader sees official and unofficial apart; invariant 4 holds; a wrong rumor never displaces an attested hit | one more list to read |
| fused into the main ranking | one list | mixes evidence of two kinds in one order; invariant 4 forbids it as written; a rumor's rank would hide that it is a rumor |
| facts only, no list | simplest | rumors are found only from a hit already found: the connections nobody searched for stay invisible |

If nothing is said: the separate list, facts and filter.

## Back to Wikidata, and the measure of it

Unchanged from gutenberg-graph-strategy.md §8 and §10.3: conflicting
values go to Wikidata's Mismatch Finder, the Gutenberg passage as the
external URL; new connections and entities go in as ordinary edits by a
person, the claim as the reference package; Louise never edits. The
collection's metrics per snapshot and tier:
- open rumors per thousand mentions;
- adopted against rejected. That ratio is Louise's precision, judged by
  Wikidata's own editors; `confirmed` is reported apart.

The open set will not reach zero. Wikidata's review is people, and a
queue of millions will mostly stay open. "It shrinks" is true, but the
metric is the rate, not convergence.

## The specification it needs (for approval; hashed bytes)

1. 09 §9.4 and §9.5: a `rumors` member in a claims collection snapshot,
   listed in `files`. Canonical JSON lines, one per claim uid: `{uid,
   state, against: <wikidata snapshot id>, since: <snapshot first seen>,
   closed_by: <wikidata snapshot id or null>, statement: <Wikidata
   statement GUID or null>}`.
2. 02 §2.10: an annotations collection whose targets include wikidata
   records the rumor states at each update.
3. 05 §5.1 and §5.3: the `rumors` option, the facts, the separate list
   and the filter.
4. 03: the member is checked by recomputing the states from the claims and
   the named wikidata snapshot. It is deterministic given both, so it is
   compared byte for byte.

It is built with gutenberg-graph's first collection, since until then
there is nothing to record. Start small: tier A only (diaries, letters,
memoirs, personal narratives), with a strict floor. Then report the
judged precision and the YIELD on a sample: how many rumors are right,
and how many of the right ones are worth a reader's time rather than a
mundane fact Wikidata merely lacks. Only then add the other tiers.
