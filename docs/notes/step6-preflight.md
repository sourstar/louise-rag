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
| 5 | the calibration reference | **DONE** 2026-09-09 — set rebuilt, spec amended; two guards left |
| 6 | T0's envelope gate | **OPEN — measure** |
| 7 | layers: one or two | **DONE** 2026-09-09 — one collection, two layers |
| 8 | credits | **DONE** 2026-09-09 — and it opened item 9 |
| 9 | the corpus and community credits | **DONE** 2026-09-09 — `credits` carries a `kind` |

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

## 5. The calibration reference — DONE 2026-09-09 (the set), OPEN (the guards)

`create` embeds the 512-text calibration set and writes the reference
vectors' hash into the Codex. That hash is what every peer's gate is
decided against, forever.

**Measured 2026-09-08, and the set could not do its job.** Longest text
118 words ≈ 174 tokens, median 26 words, against a space `ctx` of 512 —
nothing in it came within a third of the context window.
`approved-encoders.md` on the b10630 incident: *"Short tests do not catch
this, which is precisely why the approval trial (16 texts) and the backend
gate both pass while the corpus takes damage."* The gate as specified would
have passed the exact build that damaged this node's corpus.

**Both halves are now done.** It was a SPEC defect as well as an artefact
one — 09 §9.12 said "512 SHORT texts", in four documents. §9.12 now
requires a set to span the encoder's whole input range up to the largest
`ctx` it serves, and says why one set serves every space (`7bda70d`, on
`main`). The set itself was rebuilt (`d99ca7f`): 5 → 8192 tokens, 58
texts filling an 8192 window, id `sha256:7d06a2f6…`, the 32
near-duplicate pairs carried over verbatim and still last,
`tools/build_calibration_set.py` reproducing the bytes and
`calibration/text.sources.json` recording where each drawn text came
from. A gate run costs about a minute instead of seven seconds, and zero
bandwidth: the member is `512 × dim × f32` whatever the texts weigh.

**Still open, and they are the guards rather than the set:** the
reference is captured from whatever the encoder returned at `create` time
**with no independent check**, and §9.12 already defines the `.sig.json`
provenance to record it. Produce it against a digest-pinned build and
cross-check on a second endpoint before `create` writes it.

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

## 7 and 8. Layers, and credits — DECIDED 2026-09-09, operator

**7. Wikipedia and wikivec are ONE collection with TWO layers**, as 06
§6.3's own worked example spells out. Layer 1 is `verbatim` over the
documents the Upstash archive covers; layer 2 is the default chunker over
everything past that coverage — the post-snapshot articles this node
embeds itself, which is the entire point of two layers. The rule is read
from the Codex's `credits`, never from the platform's build (`9b98012`,
`app/layers.py`, `tests/test_layers_archive_rule.py`).

**8. Credits: the recipe names the archive whose vectors the collection
carries, and the archive's entry names the corpus its vectors were
computed over.** The operator's rule, in their words: "the pre-embeddings
credits the provider of the pre-embeddings and wikipedia (like 'wikipedia
embedded by...'), the part embedded by the community credits wikipedia".

| recipe | `archives =` | why |
|---|---|---|
| wikipedia | `["upstash-wikipedia-2024-06"]` | one entry covers layer 1; its `attribution` names Wikipedia's authors AND Upstash |
| pubmed | `["medcpt-pubmed"]` | `DEFAULT_ARCHIVE_BY_PLUGIN` already says so |
| gutenberg, genome, gazzetta | `[]` | no pre-embedded archive; every vector is this node's or a peer's |

The registry's two attribution strings were rewritten to carry both
parties, because both are owed and the licence clause turns on the corpus
author rather than on whoever we obtained the file from:

    Wikipedia contributors (CC BY-SA 4.0), embedded by Upstash
    as wikipedia-2024-06-bge-m3

    MedCPT article embeddings over PubMed, NCBI/NLM (public domain)

`create` copies these into `credits` whole and hashes them into
`collection_id`, so no later build can alter or drop either name.

**The second half of the operator's rule has nowhere to go, and this is
now the open item.** "The part embedded by the community credits
wikipedia" is about the CORPUS, not about an archive — layer 2 draws on
no archive at all, and its peers belong in the coverage map (02 §2.5)
rather than in `credits`. But the Codex has no field that names whom the
corpus licence requires attributing: `license` is `{class,
terms_sha256}` (09 §9.3), `terms_sha256` hashes the licence TEXT, which
for CC BY-SA states that credit must be given without naming who is owed
it, and `origin` is a locator.

`credits` cannot hold it. `credits` is SPACE-scoped by construction —
02 §2.7, "a sister's `credits` are her mother's when `space_id` matches"
and are her own recipe's when it does not, and 09 §9.3, "a corrective
sister's are `[]`". A corrective sister of Wikipedia therefore carries
every word of Wikipedia's text with an empty `credits`. A corpus credit
is CORPUS-scoped: it must survive every sister, every daughter and every
correction, exactly as `license`, `origin` and `cleaning_version` do.

So the proposal is a third key in the block that already has the right
lifetime: `license: {class, terms_sha256, attribution}` — the credit
line the corpus licence requires, byte-exact, declared by the plugin
(06 §6.1) and copied by `create` like every other licence field, `null`
on green and only on green, by the same rule `terms_sha256` already
follows. It costs one field, it is hashed, and a sister keeps it because
a sister already keeps `license` byte for byte.

It is a CODEX WIRE-FORMAT CHANGE — CLAUDE.md says ask first, and it
changes `collection_id` for every collection — so it is not made here.
The moment to make it is before the first `create` of step 6, and it is
free right now only because nothing has been published beyond this
operator's own machines.

---

## 9. The corpus and community credits — DONE 2026-09-09, operator

Item 8's decision opened this one and the same day closed it. The
operator's rule, verbatim: *"I want to credit the Project Louise community
and the corpus provider, for the part that has been embedded by the
community."*

Neither had anywhere to go. `credits` held pre-embedded archives only;
`license` is `{class, terms_sha256}` and `terms_sha256` hashes the licence
TEXT, which for CC BY-SA says credit must be given without naming who is
owed it; `origin` is a locator. The sharpest form of the gap: a CORRECTIVE
SISTER of a Wikipedia collection would have carried every word of
Wikipedia's text with `credits: []`.

`credits` now carries a `kind` — `corpus`, `archive`, `community` — in one
closed six-key shape, and the kind decides both which keys mean anything
and by which rule the entry descends. Three lifetimes:

| kind | where it comes from | what a relative gets |
|---|---|---|
| `corpus` | the plugin (06 §6.1's `corpus_provider` / `corpus_attribution`) | her mother's, on every relation without exception |
| `archive` | the recipe's `archives`, through the registry | today's rule: the mother's when `space_id` matches, the recipe's when not, none on a corrective sister |
| `community` | `embedding` — present when `open` or `endorsed` | follows `embedding`, which a sister keeps |

Exactly one corpus entry on every collection, GREEN ONES INCLUDED: a
provider that asks for nothing is still the provider, and a rule with an
exception is a rule someone lands on the wrong side of. `credits` is
therefore never empty.

**The trap this could have set, and the reason it did not.** A corpus
entry carries `coverage: "all"`, and 06 §6.3's `archive:<id>` layer rule
reads coverage. Had it read the corpus entry, EVERY document would fall in
the archive layer, be cut by the wrong chunker, and produce different
vectors under a `space_id` two peers agree on — invariant 1's silent
failure mode, introduced by a compliance field. The rule now reads
`kind: "archive"` and nothing else, and a test asserts a corpus-only
collection puts nothing in the archive layer.

Specs `f33b62e` + `2e58d44` on main; code `c5778d2` on `louise`; 19 tests
in `tests/hold/test_credit_kinds.py`.

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
