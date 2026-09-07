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

## 4. The ten questions, and how they were answered

All ten were put to the operator on 2026-09-07 and 2026-09-08. The
addenda (§7–§11) carry the reasoning; this is the index.

| | question | answer | where |
|---|---|---|---|
| 1 | how a finished collection stays visible | signed work of ANY kind — a bundle where there is embedding left to do, an attestation where there is not. A published popularity count was proposed and refused: free to forge, it would publish what a peer asks rather than what it holds, and it would rank a popular abandoned dump above a maintained one | 04 §4.7, 08 §8.6 |
| 2 | the PubMed origin unit | the CONTAINER — the artefact the origin itself checksums — with `origin` locating the record inside it. `records-vs-origin` samples shards, and a shard is the size of a container, so one re-fetch verifies a sampled shard rather than one record of it | 02 §2.3, 06 §6.4, 09 §9.3 |
| 3 | the daughter's boundary | named from the mother's side, `continues_through`, because a Codex must not name an artefact that does not exist yet and may never | 02 §2.7, 09 §9.3 |
| 4 | the cadence sister | a sister may differ in the space, the schedule, or both; vectors are shared exactly when `space_id` matches. The question also found the bundle over-bound to `collection_id`, and the tail shard closing short | 02 §2.7, 03 §3.5 |
| 5 | the archive rule | the archive's entry moves into the Codex whole — coverage because it changes bytes, licence and attribution because a registry is code and a doctored build could relabel them | 02 §2.7, 06 §6.3, 09 §9.3 |
| 6 | the aggregate limit | the peer's to set: 64 MB and 1,000 objects an hour are defaults in `platform.toml`, and the queue drops the newest | 08 §8.2, 07 §7.8 |
| 7 | the catalogue finding aid | neither a third list nor gone: a filter narrows the legs, `ask facets` enumerates the values, and nothing boosts | 05 §5.2, §5.7 |
| 8 | point-in-time lookup | `--as-of DATE` reads the snapshot current at that date; the data was already on disk | 05 §5.1, 07 §7.3 |
| 9 | chunker retention | never removed. And the encoder itself travels, because an open encoder held on one host is still a single point of failure | 06 §6.3, 02 §2.4, 04 §4.3 |
| 10 | re-embedding per changed chunk | carried over by chunk HASH, never by chunk id: `seq` is a position, so an inserted paragraph would otherwise re-embed a whole document | 01 promise 4, 06 §6.3 |

The cleaning half of question 4 was settled on 2026-09-08 and the answer
was neither option offered: see §14.

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

## 11. Addendum — filters and facets (operator, 2026-09-08)

Question 7 closed. `catalog_search` tangled three different things in
one code path, and separating them is the whole answer:

| | what it is | how it behaves |
|---|---|---|
| legs | text index, vector index | ranked, fused — invariant 4's two types |
| filters | metadata narrowing which records the legs may return | not ranked; they narrow, they do not compete |
| facets | the listing of which values exist | not a search: no ranking, no relevance |

The operator's objection was that a filter alone loses two things. The
first — searching within a category, or outside it — turned out to be
what a filter *is*, and my framing had obscured it. The second was
real and my answer had been a hand-wave: a filter nobody can enumerate
is close to useless, and for the Gazzetta the categories are the map.
That is `ask facets`, a third *surface* rather than a third result type,
so invariant 4 is untouched.

**Values are data; structure is recipe.** The Codex declares which
fields are facets and how each is shaped — `flat`, `tree` with a
separator, `range` — and never which values exist. The values are
already in the records, covered by `content_hash`, and change with
every snapshot.

The test that settled the shape was the operator's: *what happens when
PubMed coins a new subject?* It arrives in the next snapshot's records
like any other content, and the tree gains a node when the local index
is rebuilt. No Codex changes, nothing is coordinated, no collection is
superseded. Had the Codex listed the categories, one new MeSH term
would have minted a new `collection_id` every month.

The same question corrected the proposal as first written. I had put
`authority: {name: "MeSH", version: "2026"}` in the Codex. The version
is wrong: MeSH has annual editions, so pinning one would make every
release either a new collection or a quiet lie, and it buys nothing —
two peers holding the same records build the same tree whatever the
Codex says the vocabulary is. The vocabulary is the recipe and stays;
the edition is data and rides in the records. `authority: {name}` only.

**No boost.** A metadata match that moved scores instead of narrowing
was considered and refused: a filter matching nothing is informative
(the value is not here, and facets say what is), where a boost hides
the mismatch, and an unpinned weight makes two peers rank one query
differently. The honest form of a boost is a weight in the Codex, and
no collection has yet needed one. For noisy uncontrolled metadata the
fix is normalising at parse — in the recipe, deterministic, checkable —
not weighting at query time.

The facet index is local and derived: rebuilt from the records at each
snapshot, in `state/` beside the working text index, no member, no root,
no check. Counts are per-peer and say so, because what a peer lists is
what it can actually filter.

## 12. Addendum — vitality, and the origin unit made consistent (2026-09-08)

**Vitality (question 1).** The rule counted bundles, so Gutenberg went
dormant: seventy thousand books, fully embedded, a few hundred more a
month, and nobody could meet the rate. My first patch added a second
clause for fully-covered collections — two rules doing one job, the
second bolted onto the first.

One rule now: a key's signed work counts whether it is a **bundle** or
an **attestation**. They are the same thing at different points in a
collection's life. While there is embedding to do, keeping a collection
alive means embedding it; when there is none, it means continuing to
check it, and a collection nobody checks any more is decaying whatever
its coverage says. The rule that counted only bundles called a finished
collection dormant and a rotting one healthy.

The operator proposed a popularity signal — a peer's queries pinging a
counter, silenceable per collection. It was refused on three grounds,
any one sufficient. It is free to forge, where every other signal in
Louise is backed by reproduction: mint a thousand keys and top the list.
It publishes what a peer asks, where today Louise publishes only what a
peer holds, and for medical and legal corpora that is sensitive. And it
measures the wrong thing: a collection can be popular and completely
unmaintained, which is the decaying one-shot dump 01 exists to replace.
Usage ordering survives as a local library-view convenience, computed on
the peer and never sent.

**The origin unit (question 2), made consistent.** The documents had
answered it both ways: 02 §2.3 and 06 §6.4 said the unit is one PubMed
container, while 09 §9.3's enum and worked example said
`"container-member"` — per record. 09 was the outlier and is corrected.

The container is right for the reason the question was asked: it is the
artefact NLM itself checksums, one `.md5` per file and never one per
abstract, so taking the fragment would mean hashing a boundary we
invented and that two implementations could draw differently. My
original framing of the cost — "16 MB to verify one record" — was
misleading, and that is what made the question look harder than it is:
`records-vs-origin` samples SHARDS, and a PubMed shard is about thirty
thousand records, which is about one container. One re-fetch verifies a
whole sampled shard.

Records in one container share an `origin_hash` and are told apart by
`origin`, the locator that finds each inside its unit. The hash covers
the unit; the locator finds the record.

## 13. Addendum — the origin unit is the document (operator, 2026-09-08)

I got this one wrong twice, and the operator's principle settles it in
one line: **the unit is the unit a query returns** — a book for
Gutenberg, an article for PubMed, a page for Wikipedia.

What I had argued instead was that the unit should be the artefact the
origin publishes and checksums: PubMed's container of thirty thousand
abstracts with an `.md5` beside it, never a checksum for one abstract.
The reasoning was that hashing the fragment means hashing a boundary we
invented rather than one the origin guarantees.

Three things are wrong with it.

**It answers a question nobody asks.** A search returns an article, so
"is this article faithful to its origin?" must have an answer about that
article. A container-wide hash answers "do these thirty thousand
abstracts verify?" and leaves the asked question unanswerable without
fetching all of them.

**It breaks the red class outright.** Every peer holding PubMed fetches
article text from the origin one article at a time (04 §4.4) and must
check what it fetched. Against a container-level hash it cannot check a
single article without downloading sixteen megabytes — which makes the
red class, the licence class of the flagship corpus, close to
unworkable. I had not connected the two when I recommended the
container.

**It conflates two guarantees that should stay apart.** The origin's own
checksums verify that a FETCH arrived intact; that belongs to the
acquisition spec, which already declares them (06 §6.2), and it is
checked once. `origin_hash` verifies that a RECORD is faithful to the
document it was parsed from, and it is checked forever. Taking the
container bought the first's provenance at the cost of the second's
granularity, which is the wrong trade. Both survive when they are kept
separate.

The objection that remains — that the fragment's boundary is ours — is
answered by machinery already in place: how a document is extracted from
a container is parser behaviour, pinned by the platform image, and a
change to it bumps `PARSER_VERSION`. Two peers on one image cut the same
bytes.

`origin.unit` survives with a different job. It no longer says what the
hash covers — that is always the one document — but how a record's bytes
are REACHED, which is what tells a verifier the cost of checking one:
`file`, `container-member`, `index-page`.

**And it broke the seals, which nobody had noticed.** 09 §9.15 keyed a
record to its seal evidence by shared `origin_hash`: "the records parsed
from it carry the same `origin_hash`, which is how a record finds its
entry — no other reference is needed." That worked only while records
sharing a sealed file shared a hash. Under the correct rule they do not,
so the link is now explicit: the seal entry keys on `file_hash`, and a
record carries that hash in `extra.seal`. This also closes a finding
from the review itself, which had flagged that `seal_ref` existed in the
notes and had no field in the record schema.

## 14. Addendum — what makes a sister a sister (operator, 2026-09-08)

The last open item, and the operator answered it by defining the word
rather than choosing between the options I had offered.

**A sister shares the corpus. That is what makes it one.** A peer
holding a sister holds her mother's records already and re-fetches
nothing to take her. Everything else about the relation follows from
that, and the changeable set is simply what no record depends on:
`space`, `layers`, `update`.

So a cleaning change was never a sister. Cleaning output *is* the
record, so changing `cleaning_version` re-derives every record from the
origin — different `content_hash`, different shards, different chunk
ids — and there is no corpus left to inherit. The documents had been
carrying this as an "exception" in prose, sixteen lines under a table
that said a sister's records are "inherited by hash" and "identical to
the mother's — same bytes". An exception to a definition is the
definition failing, and it is the same failure as the origin unit: a
rule stated by example, so each reader resolved it differently.

The question that exposed it was "what makes a sister a sister?" — and
the answer makes the relation CHECKABLE rather than declared. A
sister's `records_root` equals her mother's at every snapshot they
share. Anyone holding both Codexes can verify that; nobody can verify a
declaration.

`cleaning_version` therefore joins the fields a sister keeps, and
`--cleaning-version` leaves `hold collection derive`, which is no
longer the command for that change.

**And it settles the sister / corrective-sister line generally.** I had
framed it as a question about what changed. It is not: both start from
zero and share only the origin. What separates them is **what is
claimed about the mother**. A sister claims nothing and both stand. A
corrective sister claims the mother is defective, must carry a
reproducible defect, and becomes the remedy once a peer reproduces it —
which is what stops anyone superseding a healthy collection by
preferring their own taste. A cleaning change is therefore a corrective
sister when the old cleaning is being called wrong, and otherwise a
collection of its own over the same origin, with no relation claimed.

**A correction to the correction.** §14 as first written said a cleaning
change is a corrective sister "when the old cleaning is being called
wrong". That was wrong, and the operator caught it: a corrective sister
corrects an error BY OUR RULES — a named check that fails — and cleaning
that butchers useful information is bad collection design, not an error.
It fails nothing: the records reproduce under the Codex's own toolchain,
the chunks recompute, every check passes. 03 §3.4 had already said as
much — a wrong parser, frozen, is faithfully reproduced by everyone and
`records-vs-origin` passes it — and 03 §3.6 that different bytes by
construction are not a defect. I had contradicted two passages of the
document I was amending.

So a cleaning change is a new collection over the same origin and
nothing else. The exception proves the rule: a cleaner that is
non-deterministic makes a check fail, and that IS a defect. The
difference is never how bad the cleaning is, only whether a check
fails — which is 08 §8.4 again, the protocol not judging merit. A
better collection earns its place by being chosen, never by declaring
the other broken.

**One consequence to decide.** A record carries `layer`, the index of
the layer whose rule matched. It is derivable from the record and the
Codex, so it need not be stored — but while it is stored, a sister that
changes `layers` writes different record bytes, and a peer must
re-fetch shards whose text it already holds. Dropping `layer` from the
record line would make a layers-sister share shard bytes exactly, which
is what the definition promises. That is a wire-format change, and
CLAUDE.md says to ask first.
