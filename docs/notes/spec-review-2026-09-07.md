# Spec review, 2026-09-07 — what was found, what was decided, what is left

The nine numbered documents were reviewed for holes that would prevent an
implementation, and amended in place (commit `specs: close the holes that
blocked an implementation`). This note is the record: how the review ran,
what it found, every decision taken where the documents were silent, and
the questions only the operator can settle.

Read it beside the diff. Nothing here is specification — the documents
are. Where this note and a document disagree, the document wins.

## 1. How the review ran

Fifteen independent readers, each with one lens over the whole
specification: collection identity; checking; sharing; asking; plugins;
commands; adversarial and security; cross-document consistency; the
eleven CLAUDE.md invariants; specification versus the existing code;
every on-disk and on-wire artefact; operability and lifecycle;
condensation fidelity against `architecture-v2.md` and against
`pack-format.md`; and what the later proposals (the Gazzetta, ELI,
collection classes, embedder choice, reranker scope, the embedder pool,
the migration plan) had already found missing.

They returned 512 candidate findings. Merged per target document, that
is roughly 190 distinct gaps: 166 candidates named 02, 82 named 03, 54
named 07, 53 named 04, 49 named 06, 45 crossed documents, 35 named 05,
25 named 08, 3 named 01.

The lenses read the code as well as the documents, which is why so many
findings are of the form *the spec says X, `app/…` does Y*. Where the
code had already fixed something the documents left open — a
canonicalisation, a hash, a framing — the code's answer was taken and
written down, rather than a second answer invented.

Nine drafting agents then amended one document each against a single
binding brief, so that a rule fixed in one place was fixed the same way
everywhere.

## 2. What the review found, in one sentence

The specification named identities, hashes and formats without fixing
the bytes.

`collection_id = H(canonical-json(codex))` is the identity the whole
platform hangs on, and neither `H` nor `canonical-json` was defined. The
shard cut rule was "about 7,000 chunks". The record line, the merkle
root, the bundle, the attestation, the catalogue digest and the snapshot
signature had no format at all. Two honest implementations could not
have produced the same collection, which is the one thing the design
exists to guarantee.

That is what `09-formats.md` is for: eighteen sections, one per
artefact, each with its path in the drag or on the wire, a field table,
the canonical bytes that are hashed or signed, and a worked example from
PubMed, Gutenberg, Wikipedia or the Gazzetta. Documents 02–08 point into
it and restate no field lists.

Beneath the missing bytes were four real contradictions, resolved rather
than papered over:

- **Trust-list weighting.** 03 §3.6 and 04 §4.6 weighted attestations
  and adoption by the reader's trust list; 08 §8.1 rejects exactly that
  as a trust bootstrap in disguise. 08 wins: levels count distinct keys,
  adoption counts distinct keys, and the trust list is a filter over
  whose attestations are counted at all — never a weight.
- **The calibration circularity.** The Codex hashed a calibration block
  of chunk ids, and those chunks belong to records that cannot exist
  before the Codex naming them is written. Calibration is now a
  corpus-independent set of 512 texts shipped with the platform and
  named by hash, which also supplies T0's envelope and T2's golden
  pairs, and makes 08 §8.3's "two sisters on one encoder share one
  calibration" true by construction.
- **The snapshot id.** 02 §2.2 named the month that had not closed; the
  code named the month that had. The code was right, and the example
  changed from `2026-09` to `2026-08`.
- **The work parameter.** 08 §8.5 wrote it into the Codex; the Codex had
  no such field. It exists now as a rate, `work: {tokens, per}`, with
  its two uses stated: vitality (04 §4.7, new) and eligibility (04
  §4.5).

## 3. Decisions taken where the documents were silent

The brief fixed most of these; the drafts fixed the rest. Each is a
choice the operator may overturn — the alternative is named.

**Identity and formats**

| decision | alternative |
|---|---|
| `H` = SHA-256 for every identity and signed object; `sha256:` + 64 lowercase hex | BLAKE2b throughout, one function everywhere |
| `content_hash` and `origin_hash` stay BLAKE2b-128, unprefixed — per-record fingerprints, already in every built pack, 36 M records | one function everywhere, at the cost of rebuilding every pack |
| canonical JSON = RFC 8785 minus floats: a ratio is a decimal string (`"0.95"`) | allow floats with a fixed formatting rule |
| every Codex field present, `null` when unused; an unknown key or version is refused | omit unused fields; ignore unknown keys |
| the Codex carries `descriptive` (title, summary) and the identity is the hash of everything BUT that block | keep `title` hashed, and accept that two names over one recipe are two collections; or move the name out of the Codex, and lose the creator's own words |
| shards: sort by `native_id` under `slicing.order`, cut every `shard_records`, never top up a short shard | cut on chunk count, as the prose said |
| shard `root` = BEP 52 merkle root over the stored `.jsonl.zst`; `records_root` = SHA-256 over `content_hash` values in order | one hash; but then equivalence across compressors is impossible |
| the snapshot is records-only and carries no signature; signatures live in `snapshots/<id>.sig.json` | sign in band, and lose "same bytes, same hash" |
| a superseded shard file is kept as `NNNNN.<snapshot>.jsonl.zst` | overwrite, and lose the ability to serve an old snapshot |
| the index generation is `index.version`; *epoch* means only `space.epoch` | one word for both, which re-keys the space on a retrain |

**Checking and trust**

| decision | alternative |
|---|---|
| three check outcomes: pass, fail, **inconclusive** (an unreachable origin is a deferral, not a failure) | two outcomes, and a flag every time an origin is down |
| the validation plan lives inside the Codex; `validation.json` is that block written out | keep it a separate unsigned file anyone can weaken |
| the seeded sample is HMAC-SHA256(seed, id), n lowest; the verifier chooses the seed and publishes it | let the plan fix the seed, which defeats the coverage rule |
| a statistical flag needs a second key to fail *the chunk ids the first attestation names* | "the same sample", which private randomness makes impossible |
| T3: 16 chunks, pass at ≥ 12 and the median, against the verifier's own compliant encoder | against the reference, which no verifier has |
| a burned key is quarantined locally and permanently, until `hold key trust --restore` | a global burn, which needs consensus |
| a producer may withdraw its own bundles by attestation, without a burn | no withdrawal path, so an honest producer's only exit is a burn |

**Sharing and asking**

| decision | alternative |
|---|---|
| contribution precondition: a peer's own `roots` and `chunks` pass, and a `records-vs-origin` attestation exists, before it spends GPU | 08 §8.2's "no embedding work until vectors are reproduced", which is circular |
| RRF k = 60, top 100 per leg, one pass per space; the fusion unit is the record, its best chunk carried | the code's two-level per-collection budget |
| a translation is a query variant of the text leg, not a leg of its own | one leg per language, so a five-language collection casts five votes |
| only the newest held snapshot is searched | a `--snapshot` flag on `ask search` |
| the reranker is node-local, top 50 of one list, never over facts, never across lists; unreachable leaves the fused order and reports `reranked: false` | pin a reranker in the Codex, which makes it part of the identity |
| red-class collections ship pointer records; text-holding peers run the text checks | no red class, or no checks for red |
| the acknowledgement gates yellow **and** red (the code already did) | yellow only, as 04 §4.4 said |
| vitality: one key must reach `work.tokens` per `work.per`; a fully covered current snapshot also counts as held | sum across keys; or leave a finished collection dormant |

**Commands**

Exit codes 0 done · 1 failed · 2 refused · 3 deferred · 130 interrupted
after a clean commit. `<collection>` resolves as full id, then a
12-hex-or-longer prefix, then a local name, then a lineage id to the
leading daughter with a notice. One writer per collection: a writing
`hold` command takes the lock or exits 3. New: `louise node
start|stop|show` (the daemon is not `hold`), `hold collection ack`,
`hold collection forget`, `hold config show|apply`, `dev migrate
schema`, `dev adopt`, `dev migrate pack`.

## 4. Open questions — for the operator

Ordered by how much depends on the answer.

1. **Vitality of a finished collection.** The rule now says a fully
   covered current snapshot counts as actively-held, so Gutenberg does
   not go dormant for lack of new books. The alternative is that
   `work.tokens` is set against the expected delta rather than measured
   throughput. Which did you mean?
2. **The PubMed origin unit** — the credits half is SETTLED (§10 below); the unit question stands.
   **The PubMed origin unit.** `origin_hash` is the hash of the origin
   unit, and for PubMed that is one baseline container — so ~30,000
   records share one hash and `records-vs-origin` re-fetches 16 MB to
   check one record. The alternative is a per-record extracted unit,
   which is not what the origin checksums. The code today hashes
   per-record.
3. **`continues_through`** (was `from_snapshot`) — **settled 2026-09-07.**
   02 and 09 had contradicted each other outright: one named the mother's
   last snapshot, the other the daughter's first. Two people writing the
   same daughter would have written different Codexes, and 04 §4.6's
   adoption contest would have fired over one collection described from
   two sides. Settled from the mother's side, on the operator's argument
   that a Codex must not name an artefact that does not exist yet and may
   never — the daughter's first snapshot is produced at the next cutoff,
   or not at all if she is never run — and renamed so that neither
   reading survives the name.
4. **Cleaning changes.** With cleaning sitting before the record, a
   sister that bumps `cleaning_version` produces its own shards and no
   longer shares the mother's future snapshots. Acceptable, or should a
   cleaning change be a corrective sister only?
5. **The layer rule `archive:<id>`.** The archive's covered id ranges
   live in a platform registry, so the rule is computable from record +
   platform image but not from record + Codex alone. Prefer explicit
   `ids:` ranges in the Codex?
6. **Attestations and the aggregate limit.** 08 §8.2 now budgets 64 MB
   and 1,000 attestations per hour from keys with no reproduced work.
   The attestation count is new here; is it wanted, and is the queue
   bounded?
7. **The catalogue finding aid.** `app/retrieval.py:catalog_search`
   searches titles, creators and subjects and is a third result type
   that invariant 4 does not classify. 05 does not specify it. Does it
   become a specified list type, or does it go?
8. **Point-in-time lookup.** The Gazzetta's *multivigenza* wants "the
   text as it stood on a date". Freshness only looks forward. Add
   `--as-of`, and on which surface?
9. **Chunker retention.** A chunker id and version named by a published
   Codex must stay callable in every later platform image — a permanent
   promise, or a bounded window?
10. **Promise 4 and the embed stage.** The promise now says re-embedding
    is per changed chunk; `app/worker.py` re-embeds whole documents.
    List as an implementation gap in STATUS.md, or soften the promise?

## 5. What the review found and the documents did not take

Not everything a reader flagged was a gap. The recurring ones:

- **The code's flat Codex fields** (`license_class`, `cadence`,
  `license_terms_sha256`) versus the documents' nested blocks. The
  documents' form is canonical; the code changes on the code branch.
- **`app/pack/manifest.py`'s publisher-keyed `collection_id`** — the
  pre-Codex identity. It is not specification; `dev migrate pack`
  converts it, and the old id stays a catalogue alias for one cadence.
- **The Gazzetta note shipping origin Akoma Ntoso to peers**, which
  01's first promise forbids. The note loses to the specification.
- **Reference sources as collections.** They stay node-local tables in
  this version; sharing one as a pack is a later amendment.

## 6. Where the review's raw material is

The 512 candidate findings, the per-document merges and the drafting
agents' change logs are in this session's workflow transcripts, not in
the repository: they are working material, and what survived is in the
documents. What could not be settled from the text is §4 above.

## 7. Addendum, same day — the format is named DRAG

The pack format has a name: **DRAG**, *distributed RAG*. The operator's
decision, taken after the review: DRAG is the **noun**, not only the
format's name. A collection on disk is a drag; `pack/` is `drag/`; a
peer holds, fetches and shares drags. 01's vocabulary gains the word,
09 is retitled *The DRAG format*, and 02 §2.6 is *The drag on disk*.

The name lands in the specification first, before the code branch,
because CLAUDE.md makes the documents' vocabulary the code's vocabulary
— naming it after the implementation started would invert that.

Two places keep the old word on purpose:

- **07 §7.6's migration table and `dev migrate pack`.** Those name the
  *superseded* artefact — the pre-Codex directory with `manifest.json`
  and several spaces. `pack build`, `pack verify`, `pack cat` and `pack
  adopt` were the old commands' real names, and a migration table that
  renamed its own left column would be a lie.
- **The shard index magic, `LOUISE-PACK-IDX1` / `LPK1`** (09 §9.5). It
  is already written into every shard built on the node
  (`app/pack/records.py:43`); changing it would make those shards
  unreadable and buys nothing, since no reader chooses its behaviour
  from it. 09 says so in place, so the next reader does not "fix" it.

**For the code branch**, the rename is 192 occurrences across 30 files
plus `app/pack/` → `app/drag/`, `app/plugins/packed/` → `.../drag/`,
and `KIND = "louise-pack"` → `"drag/1"`. The magic bytes do not move.
The old CLI names stay as the deprecation aliases 07 §7.6 already
promises.

## 8. Addendum — the descriptive block (operator, 2026-09-07)

The first pass moved the display name out of the Codex entirely, to the
catalogue entry each peer publishes. That solved the identity problem —
two names over one recipe stayed one collection — by throwing away
something worth keeping: the creator's own words. A collection needs a
title *and* a sentence saying what it holds, written by whoever wrote
the recipe, travelling with it, signed.

The operator's fix, adopted: two hashes.

```
descriptive = {title, summary}                    -- for people
collection_id = H(canonical-json(codex without "descriptive"))
codex_hash    = H(bytes of codex.json)
```

The identity is the recipe. `codex_hash` covers the file as written, so
a title altered in transit is caught like any other tampering — it is
the file's integrity, never the collection's identity.

One refinement on the proposal as put: the identity excludes **one
named key**, not a list of fields. "Everything but title and summary"
is a list someone must maintain, and the day a third descriptive field
is added — a homepage, a contact, a language note — whoever adds it
decides, by accident, whether every existing collection keeps its id.
With a single block the question cannot arise: whatever is inside
`descriptive` is outside the identity, for every version, and the rule
is one sentence a reader cannot get wrong.

The block is removed, not nulled, before hashing: a key present with a
null value is a different canonical object from a key absent, and
saying which is meant costs one clause now and an interoperability bug
later.

Two Codexes differing only in `descriptive` are one collection. A peer
holding one and meeting the other adopts nothing, merges nothing and
corrects nothing: it keeps what its own Codex says, and 08 §8.2 is
unchanged in force — displayed, never sorted on, merged on or resolved
by. The digest carries the title so a peer can name a collection it has
not fetched (09 §9.9); `collections/<name>.toml` keeps an optional
local override for the operator's own shelf, never published.

## 9. Addendum — one kinship vocabulary (operator, 2026-09-07)

*Mother*, *daughter*, *sister* — and no other word for how two
collections are related. The rule is scoped to Louise's own entities:
everything else keeps its canonical vocabulary, so that naming the
standard's parts never costs a reader the words they already know. `parent` is gone from the specification, in
prose and in field names: `toolchain.parent_platform_image` is
`mother_platform_image`, and a snapshot shard's `toolchain` is `"own"`
or `"mother"`. 01's vocabulary says it outright, so the rule is
enforceable in review: the collection a new Codex comes from is its
mother, whichever of the three relations links them.

Two uses of the word survive on purpose, and both are about trees, not
kin:

- **09 §9.5's merkle tree** — "each parent node is SHA-256 of its two
  children". That is the BEP 52 hash tree, where parent and child are
  the standard names for a node and the nodes below it. Written as
  *parent node* so a reader sees at once that it is not a collection.
- **CLAUDE.md's engineering rules** — "the parse pool hung with its
  children gone" is about operating-system processes.

The engineering notes in this folder are not rewritten. They are dated
records of what was decided when, and `lineage.md` in particular says
`parent` throughout because that was the word at the time. Where a note
and a document disagree the document wins, which is the rule already
stated at the top of this one.

Fixing the vocabulary also caught a drift the reconciliation pass would
have found: 02 §2.2 wrote a shard's producer as `toolchain: "own" |
"parent"` while 09 §9.4 wrote it `"self" · "parent"`. Two spellings of
one enum, in the two documents that must agree byte for byte. Now
`"own"` and `"mother"` in both.

## 10. Addendum — credits are inherited, not extended (operator, 2026-09-07)

The open half of question 2. My proposal was `descriptive.credits`: free
prose, outside the identity, so that differing wording could not fork a
collection. The operator's rule is better and inverts the framing.

**Credits are not a courtesy the creator extends. They are a consequence
of taking the work.** Where a collection's vectors come from a
pre-embedded archive, the Codex names it in `credits`, and a collection
inheriting those vectors inherits the entry with them — not the
creator's to write, edit or drop. Refusing the credit means refusing the
vectors, which is allowed and costs the GPU-days the archive would have
saved.

That removes the fork risk my version was avoiding, and removes it
better: `credits` is **derived, not composed**. Two creators inheriting
the same vectors derive the same list — archive ids, sorted by bytes —
so it can sit inside the identity where it belongs, hashed like every
other field. Prose never enters it; the platform's registry holds each
archive's producer, licence and required attribution, and
`toolchain.platform_image` pins which registry.

Peer contributors stay out of `credits` deliberately. They are unbounded
and change with every accepted bundle, and the coverage map already
names them bundle by bundle (02 §2.5) — a per-peer, non-content-addressed
structure is exactly where a changing list belongs, and the Codex is
exactly where it does not.

**Creating a sister is an acceptance.** `hold collection derive` shows
what the new collection would inherit and what it would discard —
whether `space_id` matches, how many chunks are already covered, and the
credits that come with them — before writing anything. The creator
accepts, or declines and proceeds from zero with no credits.

The form is shown even when there is nothing to inherit, and that case
is the one worth stopping for: a sister is one changed field away from a
collection whose two hundred GPU-days do not carry, and the difference
between deliberate and accidental is whether anyone was told. A daughter
is not asked — her space is her mother's by construction, so vectors and
credits copy with her. A corrective sister starts from zero and inherits
neither.

**Vocabulary, corrected twice.** The first draft of §9's rule read "the
collection a new Codex comes from is its mother", which asserts a
relative for every collection — but a collection created from a source
has none: a source is a corpus provider, not a relative, and most
collections stand at the root of their own lineage. And the rule is
scoped to Louise's own entities: a merkle tree keeps its parent nodes
and children, a process pool keeps its children, a directory keeps its
parent. Naming what is ours should cost a reader none of the words they
already know.
