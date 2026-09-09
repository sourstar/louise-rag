# Step 6 pre-flight — what becomes permanent the moment it runs

*Written 2026-09-08, on the `louise` branch, before step 6.*

**Read this before running `hold collection create` on real data.**

Step 6 is a point of no return and the migration plan does not treat it as
one. The moment `create` runs against the real corpus it mints a
`collection_id` and a `space_id` that are permanent: a collection is never
edited (02 §2.7), so anything wrong at that instant is wrong for the life
of the collection and for every peer that ever adopts it. A defect found
the day after costs a corrective sister and a re-embed; a defect found by a
peer costs our reputation for the collection.

The plan's structure is one-step-one-PR forward and it has no recorded
mechanism for reopening a step marked done — so these have nowhere to be
fixed and no gate that stops them reaching the identity. This file is that
gate. **Every line must read DONE or DELIBERATE before step 6 runs.**

| # | what freezes | state |
|---|---|---|
| 1 | shard numbering | **DONE** 2026-09-08 |
| 2 | the chunker's id | **DONE** 2026-09-09 |
| 3 | the chunker's parameters | **DONE** 2026-09-09 |
| 4 | `space_id`'s function | **DONE** 2026-09-09 — needed no change |
| 5 | the calibration reference | **OPEN — operator** |
| 6 | T0's envelope gate | **OPEN — measure** |
| 7 | layers: one or two | **OPEN — operator** |
| 8 | credits | **OPEN — operator** |

---

## 1. Shard numbering — DONE

`app/pack/build.py` numbered shards from 0; 09 §9.4 types `n` as "the
shard number within the slice, FROM 1" and 02 §2.2 says "shards are
numbered 00001… per slice". Fixed 2026-09-08, with a test.

Not a filename detail: the shard path is in the snapshot's shard entry, in
the member name (09 §9.16) and in a bundle header's `shard` field (§9.7),
so two peers that number differently disagree about which shard a bundle
covers — and `space_id` would not catch it, because numbering is not in it.

## 2. The chunker's id — DONE

`app/plugins/gutenberg/source.py` declares `chunking_profile = "book"`.
06 §6.3's registry has four chunkers and `book` is not one of them:
`fixed`, `cdc`, `abstract`, `verbatim`. What the book chunker actually
does — fixed windows with an overlap, paragraph-aligned — IS `fixed`.

The id is inside `space_id` (invariant 1), so this freezes at `create`.

**It also disables T1 forever.** 03 §3.5 gates the overlap-band check on
`fixed` layers, so a Gutenberg collection whose layer says `book` is never
T1-checked by anyone, for the life of the collection.

RESOLVED by ADDING `fixed` beside `book` rather than renaming anything
(`4e1e559`). `book` is untouched and stays grandfathered; a new Codex
recipe declares `fixed`. The two share `_fixed_windows`, so
`fixed{tokens: 1000, overlap: 120}` cuts byte-identically to `book` at
the config defaults AND produces the same chunk ids, since
FIXED_CHUNK_VERSION is 1 exactly as config.CHUNK_VERSION is. A Codex
collection therefore inherits a legacy pack's chunk ids and its vectors
carry over. Nothing in the legacy path moved.

## 3. The chunker's parameters — DONE

06 §6.3, immediately above its registry table: **"a chunker reads no node
configuration."**

`app/chunking.py` reads `config.CHUNK_TOKENS` (default 1000) and
`config.CHUNK_OVERLAP` (default 120) **from the environment**.

So two nodes agree on a `space_id` and disagree on chunks. That is
invariant 1's failure mode written out: the space id promises the vectors
are comparable and the chunks are not the same chunks. It is worse than a
wrong id, because nothing detects it — both peers compute the id the
document prescribes and both are honest.

There are TWO `Layer` types and only one is right:

* `app/layers.py`'s — `{chunker: str, version: int, rule: str}`, **no
  params field at all**, which is why the parameters have nowhere to live
  and end up in the environment.
* `app/hold/codex.py`'s — `Chunker(id, version, params)`, which is 09
  §9.3's shape and carries them.

RESOLVED (`4e1e559`). `app/layers.py`'s Layer gained `params`, the
dispatch passes them, and `fixed` refuses a layer that does not say.
`space_payload` was checked and does NOT carry params, so the legacy
space id every existing vector is keyed to did not move; `as_manifest`
emits them only when non-empty, so existing manifests are byte-identical.
Both are pinned by tests.

## 4. `space_id` is computed by two different functions — DONE, and needed no change

They are not variants. They hash different things:

* `app/spaces.py:compute_space_id` — BLAKE2b over
  `{embed_model_hash, dtype, normalization, pooling, chunk_version,
  cleaning_version, truncation_policy, …}`
* `app/hold/codex.py:space_id` — SHA-256 over
  `{cleaning_version, encoder:{image,weights}, layers,
  space:{ctx,dim,dtype,epoch,normalise,pooling,query}}`

09 §9.3 fixes the second — and the right answer turned out to be to STOP
TRYING TO MAKE THEM AGREE. They name different things: the legacy id
governs legacy packs, `codex.space_id` governs Codex collections, and a
collection is one or the other until `dev adopt` moves it. The alarming
version of this finding was about the LEGACY EXPORTER, which step 4
deletes. `app/hold/bundle.py` already refuses a bare-hex `space_id`, so
a conformant bundle cannot carry the legacy value — now asserted by a
test (`4e1e559`) so it cannot quietly stop being true. No code changed.

## 5. The calibration reference — OPEN, operator

`create` embeds the 512-text calibration set and writes the reference
vectors' hash into the Codex. That hash is what every peer's gate is
decided against, forever.

**Measured 2026-09-08, and the set cannot do its job.** Longest text 118
words ≈ 174 tokens, median 26 words, against a space `ctx` of 512 — nothing
in it comes within a third of the context window. `approved-encoders.md` on
the b10630 incident: *"Short tests do not catch this, which is precisely
why the approval trial (16 texts) and the backend gate both pass while the
corpus takes damage."* The gate as specified would have passed the exact
build that damaged this node's corpus.

It is a SPEC defect, not an implementation one — 09 §9.12 says "512 SHORT
texts". The proposal is one word out of §9.12 and a length ladder reaching
the largest `ctx` in use; cost is ~1 minute per gate run instead of 7
seconds, and **zero bandwidth** (the member is `512 × dim × f32`, fixed).
Re-freezing is free until step 6 and impossible after.

Two further guards the workflow raised and nobody has taken: the reference
is captured from whatever the encoder returned at `create` time **with no
independent check**, and §9.12 already defines the `.sig.json` provenance
to record it. Produce it against a digest-pinned build and cross-check on a
second endpoint before `create` writes it.

## 6. T0's envelope gate has never been measured — OPEN

03 §3.5's T0 compares a bundle's vectors against the calibration member's
`envelope` tensor. Its literals have never been checked against real data,
and the envelope's hash is a Codex field — so an envelope that refuses
honest bundles freezes at `create` too.

One afternoon, no decision required: embed the 512 calibration texts,
embed one real Gutenberg shard, print the mean-vector cosine and the max
and min per-dimension SD ratio across all dimensions. If honest bundles
would be refused, amending 03 §3.5's literals is a spec change every peer
must adopt — which is why it is cheaper to know now.

## 7 and 8. Layers, and credits — OPEN, operator

* **Wikipedia and wikivec**: one collection with two layers, as 06 §6.3's
  own worked example spells out, or two collections as today? `layers` is
  inside `space_id`, so the first `create` freezes the choice.
* **Which archives each recipe credits**: 06 §6.6 refuses an uncredited
  archive and a Codex is never edited, so a missing credit is a new
  collection rather than an amendment.

---

## The rule this file exists to enforce

Nothing here is exotic. Every item is a place where a document says one
thing and the code says another, and where the disagreement becomes
permanent at one instant. The cheap moment is before that instant and there
is no second one.

Related: [`step4-vectors-study.md`](step4-vectors-study.md) found items 2,
3 and 4; the calibration measurement is in this session's commits; the
migration plan's step 6 is being rewritten around decision 7 (the PoC
re-embeds a clean slice rather than importing the legacy vectors).
