# gutenberg-graph — the amendments A-K1..K4, worked out

*2026-10-19 night, the architecture session, unattended. D5 (operator: "Go
ahead with the decisions as you recommended, D1 to D5") made gutenberg-graph
an annotations collection AMENDED; `wikidata-implementation.md` §4 names the
four amendments in one line each. This note works them out to the byte so
that the operator can decide them tomorrow — two of them (A-K3, A-K4) change
hashed or wire bytes, and CLAUDE.md asks first for those. NOTHING HERE IS
BUILT: `app/hold/annotations.py` still refuses every claim type but
`same-work` and `translation-of`.*

## 0. What gutenberg-graph is

Claims about gutenberg's TEXT that point into wikidata's ENTITIES: a
mention of an entity in a chunk (entity linking), a relation between two
entities stated by a chunk (extraction), and a community of entities with
its summary (clustering). Each is a claim made by us — by a method with a
model behind it — about another collection's records, which is exactly
02 §2.10's kind. What 02 §2.10 lacks is in four places: the claim TYPES
(A-K1), where a claim's END may point (A-K2), how many collections a claim
collection may annotate (A-K3), and the container of parts at 10⁸–10⁹
claims (A-K4).

## A-K1 — three claim types beside `same-work` and `translation-of`

Today `type` is one of two and every claim has `subject`, `object`, their
languages, `method`, `evidence`, `confidence`, `contributor` and the pinned
hashes (09 §9.5). The new types keep `method`, `evidence`, `confidence`,
`contributor` and the pinned keys, and differ in their ENDS:

| `type` | `subject` | `object` | own keys |
|---|---|---|---|
| `mention` | a chunk of the text target: `{uid, chunk_id, text_hash}` (A-K2) | an entity: a canonical id `wikidata:Q…` (A-K2) | `start`, `end` — code-point offsets in the chunk's text, `end` exclusive; `surface` — the text between them, so a reader needs no chunk to show the mention |
| `relation` | an entity `wikidata:Q…` | an entity `wikidata:Q…` | `relation` — a canonical property id `wikidata:P…`; `evidence_chunk` — the chunk `{uid, chunk_id, text_hash}` the relation was read from |
| `community` | the community: `{id: <string the method mints>, level: integer}` | — (`object` null) | `members` — a sorted list of canonical ids `wikidata:Q…` (no duplicates, at least 2); `summary` — text |

Rules carried over: no floats anywhere (`confidence` and any score a
method states are decimal strings); one claim per (type, subject, object,
own keys that identify it: `start`/`end` for a mention, `relation` for a
relation, `id`+`level` for a community); strings NFC. `subject_lang` /
`object_lang` are REQUIRED for the two existing types and ABSENT for the
new ones (an entity has no one language; the chunk's language is its
record's). `edge_type` stays `annotations:<type>`.

`method` gains a kind for models whose output is not bit-exact
(`wikidata-implementation.md` §4 "Verification"): `{kind: "model", id:
string, version: integer, model: <sha256 of the weights>, prompt: <sha256
of the prompt text>, temperature: "0"}` — what a verifier re-runs. A
`model` method's claims are checked by RE-RUNNING A SAMPLE and counting
agreement; the tolerance is a measurement to make before the first
collection (03 §3.2's plan gets a check `model-agreement`, floor TBD).

## A-K2 — an end may be an entity or a chunk

09 §9.14 already lets `to` be a canonical id `<namespace>:<id>` left
unresolved; `wikidata-refs` registers `wikidata` (app/refs/wikidata.py), so
`wikidata:Q42` resolves on a peer that holds the wikidata collection and
stays a plain key on one that does not. What is new:

- A claim's end may be a CHUNK, not only a record: `{uid, chunk_id,
  text_hash}`, `text_hash` the BLAKE2b-128 of the chunk's text as the
  target's chunker cut it (`embeddings.chunk_text_hash`, already stored per
  chunk). The time rule of 02 §2.10 then holds per chunk: the mention is
  `current` while the chunk's text hash is the one it was made against,
  `stale` once the record's text moved under it (a re-cut moves chunk ids
  AND hashes). `subject_hash` keeps covering the RECORD's `content_hash`,
  so both tests are made from what a peer holds.
- The relations member (09 §9.14) carries `from` = the chunk's RECORD uid
  (a chunk is not a uid), `to` = the entity's canonical id, `type`
  `annotations:mention`; a `relation` claim's edge is `from` = subject
  entity … no: an edge's `from` MUST be a record of a collection the Codex
  annotates (09 §9.14), and an entity is a record of wikidata (A-K3), so
  `from` = `wikidata:<native_id>` of the subject entity, `to` = the object's
  canonical id, `type` `annotations:relation`, and the property goes in the
  claim, not the edge. A `community` claim has no edge of its own; its
  members are edges `from` each member record `to` the community's claim
  uid, type `annotations:member-of`.

## A-K3 — two targets

`annotates` becomes a LIST: `[{collection_id, from_snapshot}, …]`, each the
target it is today, in a fixed order the claims refer to by position
(`subject`'s and `object`'s collection is implied by the uid's plugin
prefix, so no index is needed in the claim). gutenberg-graph annotates
gutenberg (the text) and wikidata (the entities). THIS MOVES HASHED BYTES:
`annotates` is inside `collection_id`, so a list is a new Codex version
(**9**, written for an annotations Codex with more than one target or any
A-K1 type; version 7 keeps its bytes for the two existing collections —
09 §9.3's version rule). `_target_name`, `attach`, `works` and the Library
card read a list; `create annotations` takes `--annotates` more than once
with `--from-snapshot` each.

## A-K4 — Parquet parts for claims

At 10⁸–10⁹ mentions a JSONL part of 50,000 records is 2–20 ×10⁴ parts;
the snapshot's `shards` list alone is hundreds of MB, and every peer reads
it. The design's Parquet packets are the right container: one part = one
Parquet file of `shard_records` rows with the record line's columns, zstd,
row groups of 1,000; `records_root` and `root` computed exactly as for a
JSONL part over the CANONICAL LINE BYTES of each row (the row is rendered
to §9.5's line to hash it, so a Parquet part and a JSONL part of the same
records have the same `records_root` — the hashes do not depend on the
container). `shard_records` for claims 1,000,000. The `records` check reads
Parquet; `hold records show` renders lines. 09 §9.5 gains "The file (B):
Parquet" beside its zstd-frames file, chosen per collection in the Codex's
`slicing.container` (`"jsonl"` default, `"parquet"`), inside `collection_id`.
A WIRE-FORMAT CHANGE: the operator decides; nothing exists until then.

## The order, if approved

1. 02 §2.10, 05 §5.5, 09 §9.3 (version 9, `annotates` list,
   `slicing.container`), §9.5 (the three claim shapes, the `model` method,
   Parquet parts), §9.14 (the edges above) — amendments dated.
2. app/hold/annotations.py: `check_claim` per type; `create` with several
   targets; the chunk end's `text_hash` read from the target's vectors
   table (a peer that holds the target's text but not its vectors recomputes
   the hash from the chunker); Parquet parts behind `pack.records`.
3. The Ask side: `attach` places a mention on its chunk (05 §5.3 `annotations`
   gains `chunk_id`, `start`, `end`, `surface`), a relation on either end,
   a community as a fact; the reader panel highlights mentions.
4. The methods, as `tools/`: GLiNER mentions over gutenberg's chunks, linked
   against wikidata's vectors; relations and communities after.
5. The first instance over gutenberg's Latin and Greek slice, measured.

## Questions for the operator

- A-K3's Codex version 9 and A-K4's `slicing.container`: both inside
  `collection_id`; approve, or keep one target (gutenberg) with wikidata
  ends as plain canonical ids (no version bump; then a relation claim's
  `from` cannot be a wikidata record and `relation` edges are dropped,
  kept in the claim only)?
- A-K4: Parquet parts for annotations collections only, or offered to every
  collection (`slicing.container`)?
- `model` methods: the agreement floor — measured on the first 1,000
  GLiNER mentions re-run twice, or set at a value now?

## Decided (operator, 2026-10-20)

- **Two targets.** A-K3's Codex version 9 and A-K4's `slicing.container`,
  both inside `collection_id`: APPROVED. A relation claim's `from` may be
  a wikidata record; relation edges exist in the graph.
- **Parquet parts for EVERY collection** (`slicing.container`), not for
  annotations collections alone. A wire-format change for every
  collection kind; the operator decided it knowing so.
- **The agreement floor for `model` methods is MEASURED first**, on the
  first 1,000 GLiNER mentions re-run twice, and set from that number —
  never a value written before the measurement.
- Order: as §"The order, if approved" says; nothing of it before the
  wikidata collection exists (wikidata-build-plan.md §4).

