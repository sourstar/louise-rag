# Migration plan — from the current code to the documented architecture

*Working document. The specification is
`docs/README.md` and the nine documents it lists; this file says in what
order the code gets there, what each step reuses, and how each step is
known to be done. Update it as steps land; delete it when the last one
does.*

*Rewritten 2026-09-08 against the specification as it stands after the
read-through (`spec-review-2026-09-07.md`) and the seven decisions of
commit `0df266b`. The previous version was written against seven
documents and a Codex that has since gained twelve fields, lost one and
changed how it is hashed.*

## 0. Ground rules for every step

- **One step, one pull request, tests included.** A step is done when
  its tests pass *and every pre-existing test still passes*. The tests
  under `tests/` are the safety net for the whole migration; nothing is
  deleted from it, tests are moved and renamed as the code they cover
  moves.
- **Old names stay as aliases** until §7.6's migration is complete: a
  new command lands beside the old one, the old one prints a deprecation
  line naming the new one, and both call the same function.
- **Nothing is re-downloaded, and almost nothing is re-embedded.** The
  corpora on disk (Gutenberg, PubMed baseline and update files, Wikipedia
  dumps), the plugins that parse them, and the 48 million vectors that
  came from the two archives (MedCPT, the BGE-M3 Wikipedia set) are the
  starting point. The migration gives them a Codex and admits them
  through the same gates a stranger's copy would pass.
  **Amended 2026-09-10** in two places, both narrowing what "nothing
  re-embedded" covers: Gutenberg's own vectors are already gone — the
  move to `qwen3-embedding-4b` changed its `space_id` and emptied its
  Lance table, so it starts from zero and there is nothing to preserve —
  and the ~253,000 chunks this node embedded ITSELF across pubmed and
  wikivec are proposed for re-embedding rather than migration, because
  no build identity was stored per row and re-embedding that many costs
  hours (step 6b). The archives' 48 million are untouched by both.
- **The invariants in CLAUDE.md are unchanged**: idempotent writes,
  deterministic chunking, the GPU boundary (HTTP only), secrets by path,
  licence gates before use, `origin_hash` on every record.
- **09 is the arbiter of bytes.** Where a step touches something that is
  hashed, signed or moved, 09's field table is the specification and the
  narrative documents are the reason. A disagreement between them is a
  bug in the documents: stop and ask, do not choose.
- New dependencies, schema changes and wire-format changes: ask first.
  Step 10 has one.

## 1. The branch

**ONE BRANCH, `main`, since 2026-09-22** (operator: "I want to keep only
the main branch"). The split below is how it was until then and is left
here because the plan's steps were written under it.

*Was:* `git checkout -b louise` — specs edited on `main` and merged
forward into `louise`, code never landing on `main`. The two were
consolidated by fast-forwarding `main` to `louise` (`main` held nothing
the code branch lacked) and replaying the one spec commit that lived on
`spec/2026-09-21`; nothing was lost, and the three branches were deleted
here and on the remote. Specs and code are now edited together, and a spec
amendment lands in the same commit as the code it governs or beside it.

On a machine with the real environment:

```
docker build --target dev -t louise-rag:dev .
docker run --rm -v "${PWD}:/srv" -w /srv -e PYTHONPATH=/srv louise-rag:dev \
  timeout -s KILL 900 python -m pytest tests/ -q
```

The baseline at the time of writing is **1362 passed, 1 failed**. The
failure is `tests/test_plugins_pubmed_pack.py::
test_pack_is_rebuilt_when_the_container_or_the_format_changes`, which
asserts on `st_mtime_ns` as a proxy for "was rebuilt"; the rebuild
demonstrably runs. It is pre-existing, it is a test defect and not a
code defect, and step 2 replaces the code it covers — fix it there
rather than before.

## 2. What is already on the branch, and what is wrong with it

`app/hold/` and `app/share/` are the first increment, written *before*
the read-through. Two of the three modules are sound; the third is not.

| module | state |
|---|---|
| `app/hold/snapshot.py` (125 lines) | sound in shape, short of the current snapshot: no `chunks_root`, no `relations`, no `files`, `ids: [first, last]` where 09 §9.4 now has `first_id`/`last_id`, `tombstoned` as bare uids where it is now `{uid, reason}` |
| `app/hold/collection.py` (149 lines) | sound; `derive` needs the new flags (step 1) |
| `app/hold/codex.py` (371 lines) | **wrong, and everything hashes through it** — see step 0 |
| `app/share/rendezvous.py` (131 lines) | sound; its tests are the partitioning tests step 9 builds on |

---

## 3. Steps, in order

Each step names the documents it implements, the existing code it wraps,
the tests that prove it, and what the operator has to do by hand.

### Step 0 — Bring the Codex to 09 §9.3 — **DONE 2026-09-08**
*09 §9.3, 02 §2.1, 02 §2.4, 02 §2.7. Rewrites `app/hold/codex.py`.
Nothing else can be right until this is.*

`app/hold/codex.py` implements a Codex that no longer exists. It is
missing twelve fields, has one field the specification struck, and
computes the identity over the wrong bytes. It is the one place in the
migration where *rewrite* beats *wrap*, because every hash in the
system is downstream of it.

What changes:

- **Two hashes, not one.** `collection_id = H(canonical JSON of the
  Codex with exactly the key `descriptive` removed)`; `codex_hash = H(
  the file's own bytes)`. Today there is one hash over everything.
- **The `descriptive` block** — `{title, summary}` — exists, is the one
  key outside the identity, and is never used to sort, merge or resolve.
- **`credits`**, derived at creation from the archive ids the recipe
  names, expanded from the platform's registry (02 §2.7, 06 §6.6). It is
  hashed, so it cannot be edited afterwards. A sister's follow the space:
  her mother's when `space_id` matches, the `--space-from` Codex's when
  that supplied the space, `--archives`' when she names an encoder,
  `[]` when she names nothing.
- **`facets`** — the declared filterable structure (`flat` / `tree` +
  separator / `range`), with `authority: {name}` and no version. Values
  live in records, never here.
- **`work`**, **`modality`**, **`erasure`**, **`privacy`**,
  **`validation`**, **`protocol_version`**, **`profile`**.
- **`update` is an object** — `{cadence: "monthly" · "weekly", day,
  grace_days}` — not three flat fields. `day` reads as day-of-month
  (1–28) when monthly and ISO weekday (1–7) when weekly.
- **`epoch` is `"0"`**, not `""`, for a root; a daughter *and a sister*
  keep their mother's; only a corrective sister mints the salted value.
- **`from_snapshot` → `continues_through`**, and it names the *mother's*
  last snapshot.
- **`space_from`** records the Codex a sister copied her space from.
- **`toolchain.encoder_image` is deleted.** The encoder image is named
  in `space.encoder.image` alone; a third `toolchain` key would be
  refused by 09 §9.18 and would move every `collection_id`.
- **Unknown keys are refused** on any hashed or signed object (09 §9.18).

**Tests** (`tests/hold/test_codex.py`, extended): 09 §9.3's worked
example round-trips to its published `collection_id` and `codex_hash`
byte for byte — this is the single most valuable test in the migration
and it is written first; two Codexes differing only in `descriptive`
have the same `collection_id` and different `codex_hash`; a float
anywhere is refused; an unknown key is refused; a sister's `space_id`
equals her mother's when only `update` changed; a corrective sister's
`epoch` is the salted hash and her sister's is that same salted hash,
not `"0"`.

**Operator**: nothing.

**What landed.** `app/hold/codex.py` rewritten to §9.3 — 33 top-level
fields, twenty blocks, `_only()` enforcing §9.18's unknown-key refusal in
one place rather than scattered. `app/hold/collection.py` follows:
`derive` takes `--space`, `--space-from`, `--layers`, `--update`,
`--work` and the credits rule; `continue_` names `continues_through` and
fills `mother_platform_image`; `replace` carries a `Replaces` object.
`app/louise.py` accepts `--archives` and `--encoder-image` and REFUSES
them, naming the step that builds each, rather than doing something else
quietly. `tests/hold/test_snapshot_and_collection.py` split into
`test_snapshot.py` and `test_collection.py`; `tests/hold/conftest.py`
holds the worked example whole.

**Numbers.** 45 tests in `test_codex.py`, 18 in `test_collection.py`,
81 on the branch in 3.7 s; **1396 passed, 1 failed** over the whole
suite in 182 s — the one failure is §1's pre-existing `st_mtime_ns`
proxy, unchanged.

### Step 1 — The drag on disk, and the collection template — **DONE 2026-09-08**
*02 §2.6, 09 §9.16, 09 §9.3, 07 §7.3, 07 §7.8. Wraps
`app/pack/manifest.py`, `app/collections.py`, `app/plugins/base.py`.*

This is the step that makes "a collection" a thing on disk that a
stranger could receive, and it is where the **generic collection
template** lands.

- `app/hold/collection.py` gains the drag layout — 07 §7.4 fixes the
  module list and there is no `drag.py` in it: the drag IS the
  collection on disk. The member table of 09 §9.16 and the tree of 02
  §2.6 — `codex.json`, `snapshots/`, `records/<slice>/NNNNN.jsonl.zst`,
  `relations/`, `vectors/<space>/`, `buckets/`, `validation.json`,
  `LICENSE-TERMS`, `calibration/`, `seals/`. A file that is neither in
  the snapshot's `files` nor named by a `shards` entry is not a member
  and is not served.
- `hold collection create <plugin> --recipe FILE [--space-from CODEX]`:
  the recipe is a TOML carrying every Codex field the plugin does not
  default. **The template is what the plugin defaults**: origin, parser,
  licence class, privacy, modality, erasure, the chunker family,
  `default_layers`, `default_slicing`, `default_facets`. A new
  collection is therefore a plugin plus a `space` block plus a `work`
  block, and `create` refuses (exit 2) a recipe without those two unless
  `--space-from` supplies the space.
- `hold collection derive` gains `--space FILE`, `--work FILE`,
  `--archives ID …`; `--space-from` copies `space` and `layers` and
  never `cleaning_version`, and refuses a `cleaning_version` mismatch
  naming both values. `create --space-from` refuses the same mismatch
  against the plugin's.
- `hold collection show` names, for a collection that cannot start, the
  reason it cannot (a `space_id`/`dim` mismatch is a refusal for that
  collection and not for the node).
- The pre-acknowledgement member set is exactly `codex.json`,
  `snapshots/*` and `LICENSE-TERMS`.

**Tests**: a recipe with only `space` and `work` builds a Codex for each
of the five plugins; the same recipe twice prints the same id and exits
0; `--space-from` with a differing `cleaning_version` exits 2 naming
both; a yellow collection with no ack refuses fetch, build, embed,
index, share and search, each naming `hold collection ack`; the drag
tree round-trips through `app/hold/collection.py` with no file
unaccounted
for.

**Operator**: nothing.

**What landed.** `app/hold/drag.py` — 09 §9.16's member table, 02 §2.6's
tree, and BEP 52's pieces root implemented rather than delegated to
libtorrent, because `roots` is a check and a peer must be able to say
what a file's root should be without a torrent session.
`app/hold/template.py` — the plugin half and the recipe half meeting;
all six plugins now declare what 06 §6.1 requires (`protocol_version`,
`PARSER_VERSION`, `modality`, `erasure`, `native_id_order`,
`default_facets`), and `default_layers`/`default_slicing` are derived
from `chunking_profile`/`mutability` and `app/pack/slicing.py` so a
plugin declares its cut once, not twice. `hold collection create
<plugin> --recipe FILE [--space-from CODEX]` works end to end from a
TOML recipe. `GenesisArchive` gained what 06 §6.6 says the platform's
registry declares.

**Two things the documents could not be implemented as written.** Both
are recorded rather than papered over: 09 §9.16's `index-<slice>` and
`index-<space>` could be the same member name, fixed on main (94cf2b5)
by refusing a slice segment of sixteen lowercase hex; and 07 §7.4's
module listing did not admit a module backing no command, clarified on
main (d73dcb4).

**One thing that needs the operator.** 09 §9.3 requires
`credits[].key`, "the `ed25519:…` its bundles are signed with", and no
archive that exists is signed by anyone — the first peer to convert
MedCPT or Upstash signs with its own key, which cannot be hashed into
`collection_id`. Implemented honestly (the key is null, crediting is
refused, and the refusal says why), which means `create --archives
medcpt-pubmed` exits 2 today. See
[`archive-signing-keys.md`](archive-signing-keys.md); it blocks the full
PubMed import and nothing else.

**Numbers.** 22 tests in `test_drag.py`, 27 in `test_template.py`, 119
in `tests/hold` in 3.8 s; **1445 passed, 1 failed** over the whole suite
in 211 s — the one failure is §1's pre-existing `st_mtime_ns` proxy,
unchanged. Baseline before step 0 was 1362 passed, 1 failed.

### Step 2 — `hold records build` and `hold snapshot create` — **DONE 2026-09-08**
**DONE 2026-09-08**, in four parts: 2a the snapshot at 09 §9.4, 2b the
roots and the refusals, 2c wiring them into `app/pack/build.py`, 2d the
build driver, `hold snapshot create` and the CLI verbs.

A corpus on disk now becomes a drag end to end, and the drag holds
exactly what 02 §2.6 says — `codex.json`, the record shards,
`snapshots/<id>.json`, `validation.json` — because the build runs in
staging and only record shards are promoted, the way a fetch already
works (04 §4.3).

**What step 2 still owes.** The relations MEMBER is not written by a
build: `edge_line` and `edges_of` exist and are tested, but no plugin
implements `relations()` yet, so there is nothing to write. It arrives
with the Gazzetta plugin (step 10).

**Two errors this step caught in earlier steps**, both by tests that
assert the whole visible state rather than one field: `codex.json` was
being written OUTSIDE the drag (07 §7.8 — `drag/` is "exactly the tree
of 02 §2.6", whose first line is `codex.json`), which would have made it
unservable though it is one of the three members fetchable before an
acknowledgement; and `create` was idempotent by NAME rather than by
identity, so re-running it built a second directory for the same
collection.

**And one contradiction between documents**, found because the CLI has
to resolve what a person types: 09 §9.3 said a collection's directory is
named by its `collection_id`, 07 says in three places that it is the
local name. Fixed on main (63613a7); the directory is the name and
resolves by all four of 07 §7.7's forms.

**One thing needs the node.** `chunks_root` is computed by folding each
chunk into a blob inside the parse pool's children, because re-chunking
in the parent would double the cost of a build (amendment 47). The blob
costs about 46 bytes per chunk across the pipe. On fixtures that is
invisible; on 36M PubMed records it wants timing against a real manifest
before it ships, and the engineering rules say a change to a hot path
whose commit message has no numbers in it is a guess. The numbers are
missing on purpose, and they are the operator's to take.
*02 §2.2, 02 §2.3, 06 §6.6, 09 §9.4, 09 §9.5, 09 §9.14. Wraps
`app/pack/build.py`, `records.py`, `slicing.py`, `cutoff.py`,
`tools/pack_convert.py`.*

- `app/hold/records.py`: `build(collection, snapshot_id)` runs the
  existing pack builder over a corpus directory with the collection's
  plugin and returns shard entries instead of writing a manifest. Every
  record's `origin_hash` is required — the existing check stays.
  **New**: it REFUSES (exit 2) a record whose `native_id` sorts before
  the open shard's `first_id`, naming the record and the shard, so shard
  ranges stay a partition.
  **New**: it writes `relations/<slice>/NNNNN.jsonl.zst` from the
  plugin's optional `relations(doc) -> Iterator[Edge]`, and nothing at
  all when the plugin does not implement it.
- `app/hold/snapshot.py`: `create(collection, shards, previous)` writes
  `snapshots/<id>.json` as canonical bytes with the current shape —
  `first_id`/`last_id`, `bytes`, `chunks`, **`chunks_root`**, `tokens`,
  `relations`, `files`, and `delta.tombstoned` as `{uid, reason}`.
  `chunks_root` is SHA-256 over, in chunk order, each chunk's id as
  UTF-8 followed by the raw 16-byte BLAKE2b-128 of its text; it costs
  the chunking pass the `chunks` count already costs.
- The snapshot id follows the cadence: `YYYY-MM` monthly, `YYYY-Www`
  weekly, naming the period that CLOSED at the cutoff.
- CLI: `hold records build|show|check`, `hold snapshot create|delta|list`;
  aliases `pack build`, `snapshot record|delta|list`.

**Tests**: the golden Gutenberg mini fixture builds to a pinned snapshot
hash; building twice gives identical bytes; a record without
`origin_hash` is refused; an out-of-order `native_id` is refused naming
the record and the shard; `chunks_root` is stable across two builds and
changes when a chunker version does; a weekly collection's ids are ISO
weeks and `verify_chain` accepts consecutive ones; a plugin with
`relations()` writes the member and one without writes nothing. And the
`st_mtime_ns` test of §1 is rewritten to assert on the rebuild's output.

**Operator**: nothing.

### Step 3 — `hold embedder check|benchmark|calibrate` — **check and benchmark DONE (13e, 15); `calibrate` still a stub: it wants two builds of one model to compare**
*03 §3.3, 02 §2.4. Wraps `backend check/rebaseline`,
`tools/profile_embed.py`, `app/bundles/keys.py`.*

- `check(collection)` embeds **the whole calibration set** with this
  peer's encoder and passes when, over `quorum.n` texts drawn from it by
  the seeded rule of 03 §3.2, at least `quorum.min_pass` reach cosine ≥
  `tolerance` and the median of those n does. The whole set is embedded
  because the statement reports over all of it.
- `benchmark(collection)` measures tokens/s on the same chunks. The
  digest's `benchmarks` entry is **keyed by `space_id`**, because that
  is how 04 §4.5 reads it.
- `hold embedder calibrate --encoder-image D --encoder-weights D
  [--against …]` measures the tolerance a space can bear, **before**
  `collection create`, and its output is the recipe's
  `space.tolerance`.
- `--accept` replaces `rebaseline`: it records a passing run as this
  node's baseline for drift detection, not as the collection's
  reference. **Superseded 2026-09-19 (dd9bef4; step 13):** a run is
  validated automatically, the platform states the run on every answer,
  and there is no baseline to accept and nothing to drift from.

**PARTLY DONE 2026-09-08 — and NOT as a wrap.** 3a the calibration set
(`app/hold/calibration.py`, `calibration/text.jsonl`, 512 texts frozen at
`sha256:7d06a2f6…`, REBUILT 2026-09-09 to reach 8192 tokens — the
first set topped out at 174 and could not have caught b10630), 3b the
envelope signer (`app/hold/envelope.py`,
09 §9.2's six domains), 3c the gate itself (`app/hold/embedder.py`) and
09 §9.1's seeded `sample()` in `codex.py`.

**The plan was wrong about wrapping.** `app/verify/backend_gate.py`
records its own first run as a baseline and PASSES, so it admits an
encoder it has never compared to anything, and it thresholds on a
constant `TAU = 0.995` — which 03 §3.1 forbids in as many words. It is
replaced, not wrapped. It also keys its baseline on the collection NAME,
which is the trap CLAUDE.md's "key on physical identity, never a name"
rule exists for, and `BACKEND_GATE=off` could disable it wholesale.

**What 3 still owes.** The three verbs are not CLI verbs yet, and
`calibrate` — which measures a tolerance BETWEEN two encoder builds
before a collection exists — is not written; it needs two endpoints to
compare, so it is the one piece that wants the node rather than a mock.
`--accept` and drift detection are unspecified beyond a phrase (see the
study's findings) and are not implemented.

**Two blockers that are the operator's**, neither of which stops the
Gutenberg PoC, which runs on a TEI encoder: `dnabert2-embed:local` has no
RepoDigest, so `encoder_image` cannot be filled for the genome
collections; and llama.cpp serves no `/info` route, so `encoder_weights`
for the Qwen3 XPU encoder falls through to a weak hash.

**Tests**: with the fixture endpoint mock, `check` refuses a mismatched
encoder and accepts the reference; the pass decision is over the drawn
n and the embedding is over the whole set (assert the call count);
`benchmark` is stable within 10 % across runs on the mock; the statement
verifies with the node's public key; a digest carries one `benchmarks`
entry per space.

**Operator**: nothing.

### Step 4 — `hold vectors build|import|export|check` — **ALL DONE 2026-09-10**
*02 §2.4, 03 §3.5, 09 §9.7. **This step does NOT wrap `app/bundles` and
`app/verify`.** See [`step4-vectors-study.md`](step4-vectors-study.md) —
eight agents against the documents and the code.*

**Rewritten 2026-09-08** after the study and after `a` and `b` below
landed. The step as first written asked to wrap `genesis.py` and to emit
"a signed bundle every few hundred chunks"; both were wrong.

WHY IT CANNOT WRAP. Every tier of `app/verify`'s ladder disagrees with 03
§3.5 in substance and not in wording: T3 is a Wald SPRT over 64 chunks
against a constant `0.95` where §3.5 draws sixteen on the Codex's
`tolerance`; T2 is a beacon-and-age protocol with a golden bank where
§3.5 embeds the calibration set's first sixteen texts; T0's envelope test
reads a "space artifact" that exists for no collection here.
`app/bundles/trust.py` implements trust on first use and REFUSES every
producer key after the first, which 03 §3.5 and 08 §8.1 forbid in as many
words. And `app/bundles/format.py` forces `challenge` to null in both
directions, so it refuses every conformant bundle by construction. This
is an exception to §0's wrap rule, recorded here with the sentences that
force it.

WHAT IS GENUINELY WRAPPED: the exporter's shape, the ledger, and
`format.py`'s *posture* — verify the wire bytes, never re-serialise —
which is right and is carried into `app/hold/bundle.py`.

**Sub-tasks. ALL DONE, 2026-09-10** — `e61d4d2`, `728b127`, `c` by
measurement rather than by code, `6f4c7c2` + `945e5f7`, `dc8a2b2` +
`567574b`, `f01c04f`, `de8d5c4`, `8fd9cab`. The last of those is the verb
`a`–`h` never named; see below.

- **a. DONE — 09 §9.7's bytes** as `app/hold/bundle.py`: the closed
  sixteen-key header, ASCII-only serialisation (NOT §9.1's canonical
  JSON — the one trap that is silent when wrong), `content_digest` as one
  SHA-256 over the concatenation, `row_order_key` for the tuple sort under
  which `:10:` follows `:2:`, the framed payload, `bundle_id`, and no
  trust decision anywhere near `verify`.
- **b. DONE — possession keyed on `content_hash`**, not the `raw_hash`
  the old code sent. Both are 32 hex characters, so the confusion is
  invisible on the wire and refuses every record silently.
  `refused_uids` implements decision 5's per-record refusal; a uid with
  no entry still refuses the bundle whole.
- **c. DONE — and it needed no code.** This read "`space_id` from the
  Codex … nothing this node produces today is admissible anywhere.
  Nothing else in step 4 means anything until this lands." That was
  written before [`step6-preflight.md`](step6-preflight.md) §4 settled
  it, and the settlement was to STOP TRYING TO MAKE THE TWO FUNCTIONS
  AGREE. They name different things: `app/spaces.py:compute_space_id`
  (BLAKE2b, its own preimage) governs legacy packs, `codex.space_id`
  (SHA-256, 09 §9.3) governs Codex collections, and a collection is one
  or the other until `dev adopt` moves it. The alarming half of the
  finding was about the legacy EXPORTER, which `e` below deletes, and
  `app/hold/bundle.py` already refuses a header whose `space_id` is not
  a `sha256:` id — so the legacy bare-hex value cannot travel in a
  conformant bundle even by accident. Asserted by
  `tests/hold/test_bundle.py`, so it cannot quietly stop being true.
- **d. DONE** (`6f4c7c2`, `945e5f7`). `export` writes §9.7's four-file
  DIRECTORY; 07 §7.3 said `--out FILE` and was amended (`f844931`).
  `app/hold/export.py` does the gathering — which records are in the
  shard, which chunks they cut into, which of those this peer holds a
  vector for — and takes `vectors_for` by injection so `app/hold/` keeps
  no dependency on Lance. Two things it turned up: `layers_for` could not
  cut a CODEX collection's documents at all (09 §9.3 nests the chunker
  where `app/layers.py` keeps it flat), which invariant 1 needs since
  every stage is meant to cut through that one function; and the vector
  lookup has to filter on the INDEXED `book_id` rather than on `id`,
  which has no index. 11 tests, all through a real drag.
- **e. DONE** (`dc8a2b2`, `567574b`). `app/hold/admit.py` is 03 §3.5's
  ladder and does NOT wrap `app/verify`: every tier of that ladder
  disagrees with §3.5 in substance rather than in wording, and
  `app/bundles/trust.py` implements the TOFU that §3.5 and 08 §8.1 forbid
  in as many words. The order is the design — whole-bundle refusals, then
  the accepted row set FIXED before any tier sees a matrix, then the
  tiers over that fixed set. `app/hold/land.py` places the admitted rows
  against this peer's own cut of its own records (the text is not in the
  bundle and that is the point), and `embeddings.upsert_chunks` writes
  them keyed on the CHUNK ID rather than the document, because a bundle
  may carry some of a record's chunks and this peer may hold the rest.
  Coverage is derived from those merged chunk ids and never from the
  header's `uids` range, as this step required.

  Two findings worth carrying forward. **T0's envelope cannot judge a
  small bundle**: the relative standard error of a sample deviation is
  about 1/sqrt(2n) — 12 % at 32 rows and 41 % at 3 — so against the band
  of [0.4, 2.0] measured on 2026-09-09 that noise alone refuses honest
  work, which is the same multiple-comparison mistake the band itself was
  fixed for, arriving from the other side. Under `ENVELOPE_MIN_ROWS = 32`
  the structural half still runs and the envelope is recorded as NOT
  applied. 04 §4.5 sizes a real bundle at a few hundred chunks, so this
  is the tail; the mini corpus cuts three, so this file's foreign-vector
  test runs against `tier0` directly. And **`dim`/`dtype` are inside
  `space_id`**, so an honest header that disagrees about either is
  already refused as another space's: those two checks are about a header
  that lies, and are asserted against `check_admissible`.
- **f. DONE** (`567574b`). `licensing.require_ack` now sits on both
  doors. The ARCHIVE half landed early, because its path survives `e` and
  the hole was live: `genesis_fetch` gated the FETCH, so a shard already
  on disk — a local archive whose vectors travel with the corpus, a
  hand-populated cache, a peer copy — reached `import_shard` ack-free,
  which is the bypass the invariant names and records as observed. The
  IMPORTER half is at the top of `hold vectors import`, before the bundle
  is read, and `louise.main` maps `LicenseAckRequiredError` to 07 §7.7's
  exit 2 rather than re-raising it as a CodexError at each call site,
  which would be a second implementation of a check `app/licensing.py`
  owns.
- **g. DONE** (`f01c04f`). `app/hold/admission.py` is 08 §8.2's aggregate
  limit — 64 MB and one thousand objects per hour, whichever binds first,
  over material from keys whose work this peer has not seen reproduced,
  and beyond that a bounded queue that DROPS THE NEWEST. It rests on one
  durable fact, the `reproduced` ledger, and that is why it lands with
  step 4's importer rather than with step 9's intake: the fact
  ACCUMULATES, and a ledger that started on the day `share fetch` was
  written would count every peer who had already contributed as a
  stranger. `platform.toml` gains `[admission]`; the node's own state is
  `<LOUISE_ROOT>/state/node.sqlite`, a sibling of `collections/`.

  **Two readings to disagree with if they are wrong.** (1) `--budget` is
  OFF by default on `hold vectors import`. §8.2 governs what a peer
  "accepts and propagates" from other peers on its own initiative; an
  operator naming a path is not an arrival from a stranger, and
  rate-limiting the operator's own command would refuse the one arrival
  nobody needs protecting from, step 6's included. `share fetch` passes
  it always. (2) `app/bundles/trust.py` is NOT removed, though this step
  says `g` lands "in the same commit that removes TOFU". `python -m
  app.cli bundle import` is the legacy command and keeps working until
  step 12 retires the old names; what is removed is TOFU from the path
  that admits bundles under the documented architecture, and
  `hold vectors import` never had it.
- **h. DONE** (`de8d5c4`). `app/hold/vectors_check.py` is 03 §3.2's
  `vectors-sample` over this peer's own vectors, which step 5's plan
  runner calls and `hold vectors check` runs with one plan entry. THE
  PLAN SETS SHARDS AND NEVER CHUNKS, as this step required, and there is
  now a test: sixteen is `space.calibration.n` and travels with the
  space, so a plan that could set it would let a peer publish an
  attestation that looked like everyone else's and rested on four chunks.
  Two things it turned up. A failure here is not T3's — this judges what
  is ALREADY STORED, whoever put it there — and **the two verdicts are at
  different granularities on purpose**: §3.2's is statistical over the
  calibration quorum, so one bad chunk in three does not condemn a shard,
  while 02 §2.4's `--drop` is per ROW. So a row is emptied while the
  verdict stays `pass`, and neither is a mistake.
- **The verb no sub-task named: `build`** — DONE (`8fd9cab`), and its
  absence from `a`–`h` was a gap in this plan rather than a decision.
  Step 6 runs it at point 5 and calls it "the verb the old step 6 never
  exercised", so the proof-of-concept was blocked on it.
  `app/hold/vectors_build.py` embeds what has no vector yet, shard by
  shard, writing every `EMBED_SLICE_CHUNKS` chunks (07 §7.7's unit is
  "the chunks in flight to the encoder"). Its gate is the one b10630
  cost: a passing calibration statement for THIS `space_id`, read through
  `embedder.read_statement` so the envelope is verified and the gate
  cannot be satisfied by writing a file that says pass. `--delta ID` is
  derived from the shard roots and NOT from `snapshot.reissued`, whose
  answer excludes a shard that is NEW — every shard of a growing corpus.
  It emits no bundles: the size is still an operator decision (below).
`import(bundle | --archive ID)` keeps its shape: one path for a peer's
bundle and a public pre-embedded archive. Admissibility is `space_id`
plus per-record `content_hash`; `collection` and `snapshot_id` are
provenance and never gates. **`--archive` refuses an archive not in the
Codex's `credits`** (06 §6.6) — which is why `credits` is declared at
creation. `--strict` refuses a producer not on this peer's allow list and
never admits below T3; with an empty allow list it admits nothing, which
is why it is off by default.

**Depends on step 2** for two things it cannot be written without:
`documents.content_hash` as an indexed column, written inside the
transaction that closes the shard; and `shard_of` yielding
`"<slice>/<n>"`, because §9.7 makes `shard` a required header field and
"one shard per bundle" a hard constraint.

**Tests**: ALL GREEN, 2026-09-10.
A conformant bundle round-trips through export and import; a bundle with
foreign vectors is refused at T0 (against `tier0` directly — see `e`); a
bundle whose `collection` differs but whose `space_id` and
`content_hash`es match is ADMITTED; a FIRST-SEEN producer key is admitted
(the TOFU regression); one re-issued record has its rows refused and the
import exits 0 while the rest lands; a uid with no possession entry
refuses the bundle whole; a padded bundle does not move T0's statistic;
an uncredited archive is refused; `--strict` on an empty allow list
admits nothing; a corrupted stored vector is found and dropped by
`check --drop`.

**Operator**: two, and they are small. The `refused` field on 09 §9.10's
coverage row — all three documents now promise the refusal is recorded
and no field table has a place for it. And the bundle size: 04 §4.5 says
"a few hundred chunks", §9.10's worked example shows ~15,000, and T3 is
sixteen recomputes per bundle, so the difference is 0.16 % of production
or 5.3 %. Record the chosen number in the commit message. **`e` gave that
choice a floor with a reason**: under 32 chunks T0's envelope is not
applied at all, so a bundle sized in the tens buys a weaker admission
than one sized in the hundreds, whatever else is said for it.

### Step 5 — `hold check`, `validation.json`, attestations — **DONE (app/hold/check.py, tests/hold/test_check.py: 53); attestations not yet shared (step 9)**
*03 §3.1–3.6, 09 §9.8. Wraps `app/verify`, `pack verify`.*

- `app/hold/check.py`: reads the collection's plan, runs the named
  checks at the named sample sizes, prints the report, and emits one
  attestation per completed check (not yet shared). A check that could
  not run is *inconclusive* and produces no attestation.
- Checks in this step: `roots`, `records`, `chunks` (now against
  `chunks_root`, deriving each document's layer from the Codex's rules
  rather than reading it), `records-vs-origin` (comparing every record
  line of the shard byte for byte, not merely the resulting
  `records_root`), `license`, `signature`, `calibration` (step 3),
  `centres`, `labels`, `vectors-sample` (step 4). `anchor` and
  `origin-seal` come in step 10.
- `hold records check` is `hold check --checks roots,records[,
  records-vs-origin][,origin-seal]` — `records` included, which is the
  only local check that proves what is inside a shard.
- The trust list filters attestations and lifts burns. Its allow side
  imposes nothing on a bundle except through `vectors import --strict`.

**Tests**: `tests/pack/test_*verify*` moved; a plan with an unknown check
name is refused; the attestation for a failing deterministic check names
the shard; a `chunks` failure on a chunker that moves boundaries without
changing the count is CAUGHT (this is the test that justifies
`chunks_root`); an inconclusive check writes no attestation.

**Operator**: nothing.

### Step 6 — Proof-of-concept import — ONE collection, end to end — **DONE 2026-09-10**
*Everything above, on real data, at a scale that fits in an afternoon.
**This is the milestone the operator asked for, and the stop before the
full import.***

Pick the smallest real collection this node holds — Gutenberg is the
candidate: a corpus already on disk, green licence, no acknowledgement
to record, and a `native_id` that sorts numerically, so step 2's
ordering refusal will not fire spuriously.

**REWRITTEN 2026-09-08 (operator decision 7). The proof-of-concept
RE-EMBEDS A SMALL CLEAN SLICE. It does not import this node's legacy
vectors** — that became step 6b. Three reasons, and the third is the one
that decided it:

- 09 §9.7 requires the header's `encoder_image`/`encoder_weights` to be
  "equal to the Codex's, or a build attested compliant". The Lance table
  stores **no per-row build identity**, and those rows were written by at
  least three builds — one of them the unapproved b10630.
- `legacy-local` is not a key, and 09 §9.10 types `producer` as one. The
  honest value is THIS NODE'S OWN KEY with `source: "local"` — so the node
  would sign the b10630 damage under its own name, and is burnable for it.
- **The damage would not fall out at import.** At the measured 8 % bad
  fraction, T3's sixteen-chunk quorum fires with probability ≈ 0.006; even
  inside a 21 %-bad book it fires ≈ 0.23. The tier that found the damage
  was the sweep (`tools/vector_sweep.py`), not the ladder. Sweep before
  importing, not after.

Before it runs, **every line of [`step6-preflight.md`](step6-preflight.md)
must read DONE or DELIBERATE.** `create` mints a permanent `collection_id`
and `space_id`, a collection is never edited (02 §2.7), and this plan has
no mechanism for reopening a step marked done.

0. Confirm the encoder digest against the approved-encoder list for this
   device class (`docker inspect --format '{{index .RepoDigests 0}}'`).
   If it differs, **stop**: that is the drift the migration exists to make
   impossible, and it is what b10630 was.
1. `hold embedder calibrate` against both endpoints; record the tolerance,
   which becomes the recipe's `space.tolerance`. This also yields the T0
   envelope measurement the pre-flight asks for.
2. `hold collection create gutenberg-poc --recipe …` **into a scratch
   root**, not the real one. The Codex is a throwaway; its purpose is to
   prove the round trip, not to found the collection. NOTE THIS NEEDS A
   LIVE ENCODER — `create` embeds the calibration set (06 §6.6) — which
   is one step earlier than "steps 3+ need the node" implied.
3. `hold records build` over ~20 books already on disk. No fetch.
4. `hold snapshot create`.
5. `hold vectors build` — the verb the old step 6 never exercised — over
   that slice. ~1,500 chunks, minutes on the GPU. This is the deliberate
   exception to "nothing is re-embedded", and it is small and named.
6. `hold vectors export` to a directory, then `hold vectors import` it
   into a SECOND scratch root with its own key. A peer's bundle, made by
   us, admitted by us through T0–T3 with nothing shared in common but the
   bytes. **This is the actual proof** and the old step 6 contained no
   part of it.
7. `hold check` the whole plan and read the report.

**Done when** the round trip closes, the report is clean, and the numbers
— records, shards, chunks, bundle bytes, admitted, refused, wall clock per
stage — are in `docs/notes/STATUS.md`. **A step whose commit message has
no numbers in it is a guess.**

**Deliberately NOT proved here**: that the legacy vectors are good (step
6b), that anything works at PubMed scale, that sharing works (step 9), or
that `ask search` answers — §4's own graph schedules retrieval at step 11
and the old step 6 called it four steps early.

**Free while the GPU is busy**: cosine the ~1,500 fresh vectors against
the legacy rows for the same books. That is a direct measurement of the
b10630 damage on known-good text, and it costs nothing extra.

**Operator**: runs it on the NAS. First acceptance on real data.

**WHAT RAN, 2026-09-10.** All seven points, against the node's re-pinned
encoder from a local Docker build into two scratch roots. The numbers are
in [`STATUS.md`](STATUS.md); the ones that are findings rather than
figures:

* **Point 0 fired.** The node was still pinned to b10630 — the unapproved
  build — on the very app that serves gutenberg. The stop condition this
  step wrote is the reason that was caught before `create` ran, and not
  after it had minted an identity around it. Re-pinned to b10603 on the
  operator's instruction; [`step6-preflight.md`](step6-preflight.md) item
  0 has the procedure and the verification.
* **The proof is the import, and it passed cleanly.** Peer B held nothing
  but the drag — codex, validation, records, snapshot and its signature,
  the calibration member — and its own key. A's bundle went through T0
  (1,637 rows, envelope cosine **0.891**), T1 (64 triples), T2 (16/16,
  median 1.0000) and T3 (16/16, median 1.0000, B's own encoder
  reproducing A's vectors). The producer key was FIRST SEEN and admitted,
  which is the TOFU regression closed on real data.
* **`hold check` on the importer: seven checks, seven passes, seven signed
  attestations** — including `signature`, which had nothing to check until
  this week because `snapshot create` never wrote a sidecar.
* **The bundle did not split.** 1,637 chunks is well under the 15,000
  decided the same day, so one shard made one bundle of 8,509,256 bytes.
  The splitting path is tested but unexercised here.
* **`--sample` needed an entry per SAMPLED check** at `create` time, not
  just `chunks`: the first recipe was refused with "validation.sample has
  no size for planned check 'vectors-sample'". Correct, and worth knowing
  before step 8's recipes are written.

**Deliberately not proved**, as this step said: nothing at PubMed scale,
no sharing, no `ask`. The "free while the GPU is busy" cosine against
legacy rows did not run and cannot — gutenberg starts from zero vectors,
which is what dissolved step 6b.

### Step 6b — the legacy vectors — MOSTLY DISSOLVED 2026-09-10

*Split out of step 6 by decision 7. It was going to be the expensive
step; it is not one any more, and the reason is worth keeping.*

**It was written about Gutenberg's legacy vectors and Gutenberg has
none.** When the collection moved to `qwen3-embedding-4b` its `space_id`
changed to `45e41e7b…`, and its Lance table was emptied on 2026-08-29:
`data/lancedb/` is 512 bytes and holds no `chunks` table. The manifest
agrees — 79,320 documents, `embed_done` 0. The 9,996 vectors this plan
was written around are under the OLD space `c0ac8a52…`, which belongs to
`gutenberg-pack`, an intermediate product the operator has struck from
the account (2026-09-10) and whose vectors were the known-corrupt 8 %
anyway.

So the sweep-versus-re-embed decision is gone with them: there is nothing
to sweep, ~3.6 GPU-days versus ~8.1 is a choice about a set that no
longer exists, and `tools/vector_sweep.py` has no subject. Gutenberg
starts step 6 at zero vectors under a clean space, which is the best
possible position for a proof of concept.

**What is genuinely left is smaller by two orders of magnitude, and it is
not Gutenberg's.** Counted from each collection's `genesis_imports` on
2026-09-10:

| collection | embedded | from the ARCHIVE | this node's own |
|---|---|---|---|
| pubmed | 35,463,335 | 35,301,888 (37 shards, `final`) | ~161,000 |
| wikivec | 12,814,619 | 12,722,130 (959 shards, `applied`) | ~92,000 |
| wikipedia | 9 | — | 9 |
| gutenberg | 0 | — | — |

**99.4 % of every vector this node holds came from an archive**, and an
archive's vectors have a specified route into the new format that has
nothing to do with signing what we cannot name: `hold vectors import
--archive ID`, refused unless the Codex's `credits` names it (06 §6.6),
re-proved by the same-function sample re-encode that admitted them
originally. They are not legacy vectors in the sense that worried this
step; they are the archive's, and the archive is still there.

That leaves **roughly 253,000 chunks this node embedded itself**, spread
across two collections, as the entire remaining subject of the operator's
question — *may a peer sign vectors whose producing build it cannot
name?* The cheap answer is now available: at ~253k chunks, re-embedding
them costs hours rather than the GPU-days that made the question hard, so
the answer can be **no — re-embed them** without arguing the principle.
Confirm the count per collection before committing to it.

### Step 7 — GATE: what may be in a cleaned corpus besides text — **DECIDED 2026-09-09; NOT A BLOCKER**
*See [`figures-in-the-corpus.md`](figures-in-the-corpus.md) for the
decision and [`figures-amendment-plan.md`](figures-amendment-plan.md) for
what it costs in code.*

**This step is closed and the text below is kept for its reasoning
only.** The operator settled all four open calls on 2026-09-09 and the
amendment landed across all nine documents in `c8adbc6`: a figure is an
optional MEMBER, it never enters `content_hash`, and the record line it
appears in is version 2, declared per collection by the Codex's
`record_line`.

**It does not block step 8.** The three destinations step 8 ferries —
`gutenberg`, `pubmed`, `wikipedia` — are all whole as text, and
Gutenberg-with-plates was decided (operator, 2026-09-08) to be a NEW
collection rather than an amendment of this one. So step 8 runs at
`record_line` 1 throughout.

**What is still owed is CODE, not a decision.** `record_line` itself
landed 2026-09-10 (`248dd3b`) and a new Codex is written at version 2
with `record_line` 1; `record_line` 2 is REFUSED at create, with its
reason, because the rest is unbuilt: `CanonicalDoc` has no `figures`
field, `drag.MEMBER_KINDS` has no figures member, a snapshot has no
`figures` list, and `records.py` writes no `figures` key.
`figures-amendment-plan.md` lists it file by file. It is owed by the
first collection that actually carries plates, and by nothing before.

*The original framing, kept because it is why the answer took the shape
it did:*

The note prefers deciding this "before the record format is
implemented, since questions 2 and 3 land in `content_hash` and the
record line". Step 2 does not pre-empt it: the record line and
`content_hash` of 09 §9.5 are already implemented in
`app/pack/records.py` and step 2 wraps them rather than designing them.
What the decision would change is the same either way — a figure
entering `content_hash` re-derives every record, chunk and vector, which
is why it must be settled before a corpus is imported for keeps and not
before the code that imports one exists.

Gutenberg has books that are not the book without their plates
(architecture, interior design, technology); PubMed has structures,
schemes, gels and plots; a patents collection — on the roadmap — is one
where the drawings are part of the claim.

The specification has no answer today: a record is `sections: [[heading,
text], …]`, `content_hash` covers title and sections only, every chunker
cuts text, and 01's first promise is that only parsed text, vectors and
derived indexes travel. A figure does not go unindexed — it does not
exist.

The note frames five sub-questions (does a figure travel; does it enter
`content_hash`; what is its unit; is it embedded and by what; whose
licence) and four candidate shapes (caption-only text; an optional
member; figure-as-record; a multimodal space). **The operator decides;
this plan does not.** Whatever is decided becomes an amendment to 01,
02 §2.3, 06 §6.3 and 09 §9.5 before step 8 begins.

Until then, step 8 imports only what is whole as text.

### Step 8 — Migrate the collections this node holds
*02 §2.1, 02 §2.7, 07 §7.6. New: `louise dev migrate pack --recipe FILE`
and `louise dev adopt <pack-dir> <collection>`.*

**WHAT IS BEING FERRIED, and what is not (operator, 2026-09-10).** Four
destinations, not seven:

| destination | what goes into it |
|---|---|
| `gutenberg` | itself. Zero vectors, clean space `45e41e7b…` |
| `pubmed` | itself **+ the MedCPT pre-embeddings** |
| `wikipedia` | itself **+ the wikivec pre-embeddings** — **BLOCKED, measured 2026-09-10**, see [`wikivec-merge-blocked.md`](wikivec-merge-blocked.md). The merge cannot carry the archive's 12.7M vectors: the two plugins cut the same articles into different objects (63 paragraphs vs 24 sections on article 12, 2–3× across the sample), a uid carries its plugin's name so the archive's `embedded:*` rows can never be a `wikipedia`-plugin collection's, and the two corpora are different Wikipedia snapshots (6 of 8 sampled leads differ). Recommendation: keep them as two collections, or make them SISTERS — not one a layer of the other |
| genome × 3 | **stay on the old format for now** |

**"In the end the pre-embeddings will be incorporated in the collection
they belong to."** That is the shape both archives already have in the
new format, so nothing new is invented for it: an archive is not a
collection, it is an entry in `credits` and a source `hold vectors import
--archive` draws from. `pubmed` is already exactly that. `wikivec` is the
one that changes — it stops being a collection and becomes
**`wikipedia`'s layer 1**, which is preflight decision 7 (one collection,
two layers: `verbatim` over the documents the archive covers, the default
chunker past its coverage) and is already implemented and tested in
`app/layers.py`.

**One consequence of that merge, and it needs deciding before the
`create`.** `layers` is inside `space_id` (02 §2.4), so a two-layer
`wikipedia` has a space that is neither today's `wikipedia`
(`f20c530c…`) nor today's `wikivec` (`ed34b4b0…`). Today's wikipedia
vectors are 9 and do not matter. Today's wikivec vectors are 12.7 million
and they DO — but they are the archive's, so they arrive by the archive
route, which re-proves them against this node's own encoder rather than
inheriting them on a matching `space_id`. That works only if layer 1
reproduces the archive's function exactly: bge-m3 as Upstash ran it, and
the input `f"{title}\n{text}"` (see the bge-m3 note in
[`step6-preflight.md`](step6-preflight.md)). Get that wrong and the
same-function proof refuses all 959 shards, which is a loud failure
rather than a silent one — but it is 12.7 million vectors of re-import
either way, so prove the function on ONE shard before running the rest.

**Genome stays on the old format**, and the operator has pre-authorised
it: "if there is some issue with the genome collections, put them on old
for now; genome collections were mostly an experiment and for a serious
genomic research they likely need in any case more refinements." There is
such an issue and it is absolute rather than awkward — see the blocker
below: no DNA calibration set exists, so `create` cannot write them a
Codex at all. They keep working exactly as they do now; nothing about the
old format stops on their account.

Step 6 for each remaining collection, plus the pre-Codex packs already
on disk:

- `dev migrate pack <name> --recipe FILE [--platform-image D]` gives a
  pre-Codex pack its Codexes and first snapshot. The recipe carries, per
  space in the pack, what the pack cannot supply — the encoder image and
  weights, tolerance, work, validation, descriptive — and the
  calibration set is embedded per space as `create` does; the first
  space named is the root and the others `derived_from` it.
- `dev adopt <pack-dir> <collection>` is the one-time move of a
  pre-Codex pack's vectors and index into the collection's drag.
- The old id is aliased in the local catalogue for one cadence, so URLs
  and TOML references keep resolving.

**Tests**: a fixture collection with an old-style manifest migrates to a
Codex whose `collection_id` is stable across two runs; record counts
match the old manifest; the admitted-vector count is reported; running
the migration twice is a no-op.

**BLOCKED for the THREE genome collections — and the operator has
already decided the outcome: they stay on the old format** (2026-09-10).
Found 2026-09-09 while looking for a fourth modality to measure T0
against. 09 §9.12 ships one calibration set PER MODALITY; this platform
ships `calibration/text.jsonl` and nothing else. `create` embeds the set
for the collection's modality, `GenomeSource.modality` is `dna`, and
`cal.load("dna")` refuses:

    this platform ships no calibration set for modality 'dna'
    (calibration/dna.jsonl); a Codex naming one cannot be calibrated here

So a genome collection cannot be given a Codex at all — not by `create`,
not by `dev migrate pack`. It is not a gap in this step's code; the
artefact it needs does not exist. What closing it takes:

- **A DNA calibration set**, 512 sequences, built to §9.12's rules and to
  the same standard the text set was rebuilt to on 2026-09-09 — spanning
  the length range up to the space's `ctx` rather than stopping short of
  it, since that defect is what the text set had. `tools/build_calibration_set.py`
  is the shape to follow, not the code: the strata are different (GC
  content, repeat density, coding versus non-coding, and the near-
  duplicate pairs that make a subtle build show itself) and there is no
  "script" axis at all.
- **A decision the operator has already half-taken**, which is why this
  is worth surfacing now rather than at step 8: STATUS.md's open threads
  carry a pending move of the genome collections to DNABERT-S on the Arc,
  and the window is cheap while dm6/hg38 sit at zero embedded. A
  calibration set is per MODALITY and not per encoder, so it survives
  that move — but the `tolerance` it produces does not, and building the
  set before the encoder is chosen means calibrating twice.

Neither is on the critical path to step 6, which is Gutenberg, and
neither is on the critical path to step 8 any more either: the genome
collections are out of scope for the ferry until someone wants them in
it. What this section is FOR, now, is that the decision was taken with
the reason known rather than by drift — and that whoever eventually
builds the DNA set finds the requirements here instead of rediscovering
them.

**Operator**: per collection, on the NAS, with the digests. Record each
outcome in `docs/notes/STATUS.md`.

### Step 9 — `share`: serve, fetch, contribute, attest
**STARTED 2026-10-06.** Sub-steps, in order (each with its tests; the
node is one peer until 9d's two-peer test):
- **9a — members and keys. DONE 2026-10-06** (`app/share/members.py`;
  `tests/share/test_members.py`, 23): bencoding; 09 §9.16's info
  dictionary from (path, length, pieces root) alone and its infohash —
  PROVEN equal, byte for byte and hash for hash, to what libtorrent 2.1
  builds from the file, so two honest publishers land in one swarm; the
  collection, lineage, weights and image keys of 04 §4.2; the members a
  snapshot implies (records, relations, figures, validation, terms,
  calibration, seals) and, from the drag on disk, the text-index members.
- **9b — the seeder. DONE 2026-10-06** (`app/share/seeder.py`;
  `tests/share/test_seeder.py`, 8). In the WORKER's seeding beat (a `hold`
  command never opens a port, 07 §7.4), wrapping app/acquire/seeding.py:
  every public collection in use whose file says `share = true` has its
  current snapshot's members served — one torrent per member file, laid
  out as hard links under `state/share/<snapshot>/<member>/`, built by
  libtorrent and REFUSED when its infohash is not the snapshot's (a
  changed, grown or cut file is not served) — and its collection and
  lineage keys announced through the DHT every fifteen minutes while the
  mode seeds. `share <collection> [--stop]` writes `share = true` or
  `false` into the collection's file; the worker does the rest at its next
  beat. PROVEN on localhost: a leecher knowing only the infohash it
  computed from the snapshot fetched the member's bytes from our seeder.
  Still owed: `--slices`/`--kinds` narrowing (9d). The announced port is
  `[node] address`'s since 9c (`gossip.own_port`) — the one peers reach,
  never an env var: desired state lives in the TOML. A copy in
  quarantine, or never-share, is not served (03 §3.6).
- **9c — the peer messages** (09 §9.9). **DONE 2026-10-06**, in two
  bricks (`app/share/peers.py`, `messages.py`, `api.py`, `gossip.py`;
  `tests/share/test_messages.py`, `test_share_api.py`, `test_peers.py`).
  First: the five routes in the web process, verifying with the SENDER's
  key; peers, neighbours, bans and seen ids in `state/node.sqlite`; this
  node's digest signed and sent once an hour by the worker to every
  neighbour and every `[peers]` address; events forwarded once with one
  hop fewer; pulls answered with the exact stored bytes. `[peers]
  addresses`, `[dht] enabled` and the new `[node] address` in
  platform.toml. Second: `share peers add <address> [--key]` writes the
  `[peers]` list (each address once, the rest of the file byte for byte)
  and `[peers.pins]`; a digest claiming a pinned address under another
  key is discarded and logged at the door; `share peers show`; every byte
  the worker sends draws from the UP bucket first, DELTA class, and a
  stop ends the round where it stands (what is left is forwarded at the
  next beat); the DHT announcement names `[node] address`'s port. 07 §7.3
  amended: a hand-given peer is a neighbour from its first digest, not
  for three intervals on faith — a neighbour is a key at an address.
  Owed to 9d: acting on what digests and events say (the fetch); the
  bytes the WEB answers to a pull — a Codex, a snapshot, attestations,
  kilobytes — leave through a process with no bucket of the worker's and
  are unmetered until pulls grow with the fetch.
- **9d — `share fetch`**: find (DHT key, peers list), pull codex and
  snapshot, fetch members, verify roots, stage, promote; the
  pre-acknowledgement set. The two-peer test on localhost.
  **First brick DONE 2026-10-06** (`app/share/fetch.py`;
  `tests/share/test_fetch.py`, 11): who is asked and in what order
  (`--from`, the neighbours whose digests list the collection or its
  lineage, the `[peers]` list blind, the DHT under 24 probed keys with
  `--dht`); a lineage id resolves to the entry with the newest snapshot
  — the leading daughter by snapshot, not yet by 04 §4.6's adoption
  count (9e, with the coverage map); the Codex chain pulled, each
  canonical and under its own id, the head hashed against the digest's
  `codex_hash`; the snapshot chain pulled newest first and linked by
  hash back to the root's first, to the mother's `continues_through`,
  or to one held here, hashed against the digest's `snapshot_hash`; a
  lie bans the sender (04 §4.2) and the next peer is asked; signed
  snapshots into the drag with sidecars merged by union, an unsigned one
  into `state/staging/`; the chain's other Codexes under
  `state/chain/`; the collection's file written, `[encoder]` from
  `--encoder`. Pulled bytes are accounted to the calling process's DOWN
  bucket (kilobytes). **Second brick DONE 2026-10-06**
  (`app/share/holdings.py`, `transport.py`; `tests/share/test_holdings.py`
  4, `test_transport.py` 4, and 3 more in `test_fetch.py`): the members
  by torrent. `holdings`: what the file asks for — every member of a
  kind it names in a slice it names, `"all"` or a list (the loader now
  accepts a list: part of a collection), the whole-collection members
  always, `terms` first — what the drag holds (presence at the
  snapshot's size), the `held_members` mark in the manifest written
  AFTER the rename, promotion that computes the pieces root in staging
  and sets a wrong file aside under `state/staging/refused/`.
  `transport`: each missing file by the magnet of the infohash the
  snapshot gives, metadata from the swarm, the one file on, paced by a
  rate lease from the DOWN bucket in the DELTA class and credited as it
  lands, stopped between polls with staging kept, the handle removed
  afterwards; the ack gate — nothing but `terms` of a yellow or red
  collection until `hold collection ack`; the free-space check before
  the first byte; a collection nobody serves backs off fifteen minutes
  after one bounded metadata wait. The worker runs it in its own thread
  (`share-fetch`, every 15 s, gated on the mode's fetch activity);
  `share fetch --here` runs it in-process under the lock. The sharer's
  swarm is reached through the DHT and through the `peer_port` its
  digest names — the one wire amendment, 09 §9.9, optional field. 04
  §4.3 amended: a file present without its mark is held and marked, not
  moved back — its root was computed before the rename, or it was built
  here. `hold collection show` lists `snapshots` (verified signatures,
  held) and `holdings`. PROVEN on localhost: two roots in one process,
  the sharer's seeder and the fetcher's session; the fetcher pulled the
  Codex and the signed snapshot over HTTP, then the two record shards by
  the infohashes it computed, verified and promoted them, and a second
  beat had nothing to do. **Owed**: `share <collection> --slices/--kinds`
  narrowing of what is SERVED (the fetch side is done); the DHT probe
  live, with a real swarm (the unit test has a fake session); 04 §4.6's
  adoption count for the leading daughter (9e, with the coverage map);
  the web's pull answers under a budget.
- **9e — `share contribute` and `share attest`**, the coverage map, the
  single-survivor test. **First brick DONE 2026-10-06 — the coverage
  map** (`app/share/coverage.py`; `tests/share/test_coverage.py`, 7):
  09 §9.10's rows in the manifest (`coverage`: one per bundle, with
  tiers, `accepted_at`, `source`, `verified_on_use`, `claimed_by`;
  `coverage_shards`: when a shard was first seen open), written by
  `hold vectors import` after the ladder (`fetched`) and by `hold
  vectors export` (`local`, not accepted until this peer's own ladder
  runs over it); claims from neighbours' digests folded beside the rows
  (`fold_claims`) and counting for nothing; the derived view per shard —
  covered, open, the leader among the live peers (rendezvous over the
  digests' benchmarks, this peer's own from its statements) and the
  window from the later of the shard's first opening and the leader's
  last bundle plus `grace_days`; producers with their signed work; 04
  §4.7's vitality from bundles and attestations in the trailing
  `work.per` (a deterministic check counts the snapshot's tokens of the
  shards it names, `work.tokens` when it names none; a statistical one
  its chunks × ctx); `hold vectors show`. **Second brick DONE 2026-10-06
  — `share attest`** (`app/share/attest.py`; `tests/share/test_attest.py`,
  4): `hold check`'s work factored into `louise.run_check` and shared;
  every attestation the run wrote becomes one `attestation` event signed
  with the node key and kept as seen (hops 8) for the beat to forward;
  the attestation ids published are recorded (`attestations_published`
  in node.sqlite) so none is published twice, while a new run's new
  attestation is. The receiving side: the beat's `store_received` keeps
  an attestation event's payload — verified, for a collection held here
  — at 09 §9.8's path before forwarding; a foreign or forged one is
  dropped; events of the other types are marked acted and left to the
  bricks that act on them (`events_seen.acted`). **Third brick DONE
  2026-10-06 — `share contribute`** (`app/share/contribute.py`;
  `tests/share/test_contribute.py`, 4): the preconditions of 04 §4.5
  (own roots and chunks passed, a records-vs-origin attestation for the
  snapshot by any key, the calibration gate where the collection is not
  gated per answer) refuse before any GPU is spent; the open shards off
  the coverage map, the live peers off the digests, the cascade with its
  windows (the shard's first opening noted when first seen open; a
  successor's window starts where the leader's ended; every window run
  out, the top again); each shard led built with `vectors_build.build`,
  exported with `export.export` into `state/bundles/…/<id>/`, judged by
  this peer's own ladder (T0–T2), accepted as `local`, announced with a
  `bundle` event. `Member.base` lets a member's paths be relative to its
  bundle directory; the seeder serves bundle members beside the
  snapshot's; the digest offers them in `members` and lists the accepted
  bundle ids per shard in `coverage`; the beat folds neighbours' claims
  into the map (`fold_claims`). CLI: `share contribute [<collection>]
  [--once] [--interval S]`, locking each collection. PROVEN in tests:
  alone, a peer builds, bundles, accepts and announces every open shard
  of a built collection, and a second pass finds nothing open; a
  neighbour of equal weight leads the shards the hash gives it until its
  window runs out. **Owed in 9e**: acting on `snapshot`, `codex`,
  `bundle` and `flag` events (fetching a bundle another peer announced,
  through the ladder — the import side of contribute); duplicates as
  checks (the second compliant bundle compared and attested, 04 §4.5);
  the single-survivor test. **Fourth brick DONE 2026-10-07 — the import
  side** (`app/share/accept.py`; `tests/share/test_accept.py`, 3): a
  `bundle` event for a collection held here, acknowledged and in use,
  has its four files fetched by their torrents into staging, each
  checked against the root the event gave, the bundle judged by this
  peer's ladder (T0–T2, T3 skipped in the worker's thread: the chunks
  land *unverified here*), its vectors merged under the writer lock, the
  producer recorded as reproduced (08 §8.2), the coverage row accepted
  as `fetched`, the bundle kept under `state/bundles/` for the seeder;
  refused, set aside and said; an event for a collection not held is
  acted on by being left alone, one for a gated collection waits. Runs
  in the worker's `share-fetch` thread after the members (`accept.beat`).
  **The wire amendment this needed (09 §9.9, for the operator)**: a
  bundle member is FOUR torrents (§9.16) and a fetcher computes each
  infohash from a file's (path, length, pieces root), which neither the
  `bundle` event nor the digest's `members` entry carried — so both now
  carry `files: [[path, root, bytes], …]` for a member no snapshot lists
  (a bundle, a text index), the member root being `H` over that list as
  §9.16 already said. A receiver leaves an event without it alone.
  PROVEN on localhost (`test_accept`): the sharer contributes and serves;
  the fetcher, holding the collection and its records, takes the
  announced bundles in — the vectors land, its map is covered, and it
  serves the bundles itself: 04 §4.1's survivor. **Found on the way, in
  the seeder of 9b**: libtorrent reads a one-file tree at its root —
  `validation`, `terms`, each file of a bundle — as a SINGLE-FILE torrent
  named after the file and serves it from `<save path>/<file>`, while a
  nested path (every record shard) stays `<save path>/<member>/<path>`;
  the seeder had laid every file out the nested way, so a fetcher of
  `validation.json` got the metadata and no piece, ever. Fixed by giving
  a flat file's torrent its own save path (its member directory), and
  on the fetching side by finding the file under either spelling. 9b's
  localhost proof had only record shards and could not see it. **Also
  2026-10-07: `snapshot` events acted on** (`accept.newer_snapshots`, in
  the same beat before the bundles): a snapshot newer than the one held
  here, announced by a peer whose digest gave its address, is pulled from
  that peer with its chain (`fetch.fetch`), promoted when signed; the
  members follow at the next beat. An announcer with no digest yet
  leaves the event waiting; a snapshot held already, or older, is noted.
  **Also 2026-10-07: the trust level** (`app/share/levels.py`;
  `tests/share/test_levels.py`, 5): 03 §3.6 computed locally — the
  attestations counting for the snapshot named within two cadences,
  from keys the trust list does not shut out (the list itself is step
  10's `hold key trust`; empty today, so everyone counts); independence
  by distinct keys and, for a sampled check, distinct seeds; `checked`
  and `verified` by the thresholds, with the producer rule read off the
  coverage map; `flagged` by one deterministic failure under an image
  whose verdicts count (`equivalence.reference_images`) or two
  statistical failures from independent verifiers on the same chunks.
  `hold collection show` prints `trust_level` and, beside it,
  `trust_detail` — keys, passes per check, sample coverage, and what
  holds the level where it is. Not yet counted: an older snapshot's
  attestations for shards the newer one lists unchanged (03 §3.6).
  **Fifth brick DONE 2026-10-07 — the rest of the events, duplicates as
  checks**: `codex` events read as chains (`app/share/catalogue.py`,
  node.sqlite `catalogue`, listed by `share peers show`); `flag` events
  have their attestations pulled from the announcer and kept, and the
  level now tells a stranger's lone deterministic failure (a CLAIM,
  `trust_detail.claims`) from this peer's own or a seconded one (a
  flag), as 03 §3.6 says; a second compliant bundle over chunks held
  here is compared cosine by cosine against the tolerance and published
  as a `vectors-sample` attestation naming both producers, only its
  fresh chunks landing (`accept.compare_duplicate`). **Still owed in
  9e**: local reproduction of a received flag (the named check on the
  named shards, which `hold check` runs by sample today); T4 on use
  (step 11, with the search).

*04. Wraps `app/acquire/transport_torrent.py`, `torrent_publish.py`,
`app/transfer`, `app/discovery.py`, `app/contribute.py`,
`app/share/rendezvous.py`. Makes the coordinator optional.*

- `share <collection>`: publish + seed as one motion; announce under
  `H(collection_id ‖ snapshot_id)` and `H(lineage_id ‖ snapshot_id)`
  (libtorrent's DHT is already linked); digests and events over the node
  API as today. A searcher probes the current id, then each earlier id
  back to twelve cadences.
- `share fetch`: choose members, verify pieces, stage, promote. Before
  an acknowledgement only `codex.json`, `snapshots/*` and
  `LICENSE-TERMS` are fetchable.
- `share contribute`: `rendezvous.mine` over the coverage map's
  uncovered shards → `hold.vectors.build` → `share`. The coordinator's
  `/work` is consulted only if configured, and its answer is advisory.
- `share attest`: `hold.check` → publish the attestations as events.
- The digest carries `codex_hash` per collection, and a `codex.json`
  pulled from a peer whose bytes hash otherwise is dropped.
- Coverage map: per-document bitmap per space, assembled from bundles
  and attestations, exchanged in digests.

**Tests**: `tests/integration` gets a two-peer test on localhost: peer B
fetches from A, contributes a shard, A imports it; then the
**single-survivor test** — B is stopped, A produces the next snapshot,
embeds it, and a fresh peer C finds A by computing the DHT key. A
`codex.json` with a substituted `descriptive` block is dropped on the
pull path. The rendezvous tests already cover the partitioning.

**Operator**: two nodes on the LAN for the first real run.

### Step 10 — Certified collections and the Gazzetta plugin
*02 §2.8, 03 §3.7, 03 §3.8, 06 §6.4, 09 §9.15. New code; one dependency
decision.*

- `hold key anchor`: fetch the well-known URL, compare the fingerprint,
  record rotation and revocation.
- Manifest-only gate at promotion for `publication: certified`; the
  space-halt on a violation found later; the exclusion notice in search.
- `seals/`: capture validation evidence at snapshot time. Each entry now
  carries **`seal`** (the signature envelope, DER as base64) and
  **`content_sha256`** (the digest the signature commits to), so
  archival mode verifies the signature offline without the sealed bytes
  — `file_hash` is BLAKE2b-128 of the file and is what *live* mode
  re-fetches against.
  **Dependency**: validating a CAdES envelope and a PAdES seal against
  the EU trusted lists needs a signature-validation library (candidates:
  `pyhanko` for PAdES, CMS via `asn1crypto`/`cryptography` for CAdES;
  the Commission's DSS is Java). **Decide before writing a line.**
- The Gazzetta Ufficiale plugin: acquisition of the certified `.pdf.p7m`
  issues, the sealed PDF as the origin form with the reason recorded
  beside the plugin, parsing pinned by the platform image.

**Tests**: a foreign shard is refused at promotion; a fixture `.p7m`
with a self-signed chain validates in archival mode against captured
evidence *with the network unplugged*, and fails live against an empty
trusted list; the third attribution row of 03 §3.8 flags the certifier,
not the sharer.

**Operator**: the certifier key and anchor for the first certified
collection — this node's own key and a well-known URL on a domain you
control, marked `is_origin: false`.

### Step 11 — `ask serve`, facets, relations
*05. Wraps `app/query_service.py`, `app/retrieval.py`, `app/refs`.*

**STARTED 2026-10-07. First brick DONE** (`app/ask/`; `tests/ask/`, 23):
`results.search` gives `query_service.run_search`'s answer 05 §5.4's
shape — one list per `space_id` (a Codex-less collection is its own,
`legacy:<plugin>`), `question: "relevance"`, each result with §5.3's
fields: collection and lineage, the snapshot read, uid, chunk and layer,
rank, the legs that ranked it (`_merge_rrf` now keeps each leg's rank,
the text leg's best across its language groups), the trust level
(`app/share/levels.py`), certification (null until step 10), the
verification state and producer key, the text or — red — the origin's
page, figures (null), `reranked`; `facts`, `unresolved`, `withheld`,
`legs_withheld`, `translations` beside. `lookup.lookup`: a fact by key
from the source that owns the namespace, every column, `ref_version`.
`traverse`: 09 §9.14's edge shards indexed into the manifest once per
root (`relations`, both directions), walked `depth` hops, unresolved
edges naming the shard where the uid is ours; `pack.records.write_lines`
writes §9.5's framing over edge lines (no plugin emits relations yet).
`facets`: the Codex's facets, counted from the manifest's columns (lang,
authors, subjects — flat, tree with every prefix path, `--under`), cached
under `index/facets/` per snapshot; a collection over 500k documents is
counted by the index maintainer only. The maintainer counts facets and
indexes edges once per snapshot (`worker._maintain_ask_indexes`). `mcp`:
the three tools over JSON-RPC, stdio (`ask serve`) and `POST /api/ask/mcp`,
no library. `/api/ask/{search,lookup,traverse,facets}` as 07 §7.4 says.
CLI `ask search|facets|lookup|traverse|serve`. Second brick: `filters`
(05 §5.2) — the facet pass writes postings beside the counts
(`index/facets/postings.sqlite`, per snapshot, by the maintainer);
`app/ask/filters.book_ids_for` resolves `{field: value | {not: value}}`
to the records admitted (a tree value with its subtree; facets
intersected; exclusions subtracted), refused when the facet is not
declared, is a range, is not indexed for the current snapshot, or admits
more than 20 000 records; `query_service.run_search(filters=)` hands the
set to both legs as `book_ids` (catalog, FTS and dense — `retrieval`
takes it; a Codex-less collection admits nothing under a filter); an
empty set returns nothing before any leg runs. Third brick: T4 on the
chunks a search cites (05 §5.3, 03 §3.5) — every search queues the
imported, unverified chunks it returns (`verify_on_use.cite`, from
`retrieval._attach_metadata_and_enqueue`, so the web's Ask and
`/api/search` cite too) in the manifest's `verify_queue`; the worker's
index-maintainer turn drains it in bounded passes of 32
(`worker._verify_cited`, only in a mode that embeds), re-embedding with
the collection's own encoder and marking `verified`; a miss is
recomputed a second time and is a failure only when it reproduces
(`inconclusive` otherwise: left imported, cited again later); a
reproduced miss writes a `forgery` attestation (`app/hold/forgery.py`:
both vectors by `H` over big-endian `space.dtype` bytes, the text or —
red — the record's `content_hash`, the encoder named: 09 §9.8 amended)
at §9.8's path, publishes it, and burns the producer's key in
`state/trust.json` (`app/hold/trust_file.py`; `levels.counting` now
takes the file's `deny` and `burn`); the legacy providers-table
quarantine and the local re-embed of the producer's documents stay as
they were. An encoder that does not answer leaves the queue as it was.
Fourth brick: `as_of` (05 §5.1; `app/ask/asof.py`) — the date read at
00:00 in `origin.clock` against the cutoff rule (`update.day` of the
period after), the newest HELD snapshot at or before it chosen; what the
one index can answer for it is exactly the records unchanged since
(same `content_hash`, same chunks), so hits on records added, changed
or tombstoned by any later snapshot are dropped (`as_of.excluded`) and
the chosen snapshot's shards holding them are named under `withheld`
(held or not — held ones are not indexed for search); every result's
`snapshot_id` is the snapshot that answered; a chain with a gap, a date
before the first held snapshot, or a Codex-less collection is withheld
whole and said; more than 500 000 changed records is refused (exit 3).
Fifth brick: `ask chat` (05 §5.4; `app/ask/chat.py`, `/api/ask/chat`)
— the chat model with the three tools declared as functions
(`llm.chat_tools`, both dialects of CHAT_URL), each call run through
the MCP dispatcher and handed back compacted (text trimmed, lists cut,
the object itself kept in `turns`), rounds bounded at six then an
answer without tools; CHAT_URL unset refuses (exit 2);
`/v1/chat/completions` stays retrieval-then-answer for its clients.
07 §7.3 amended: `--filter FACET=VALUE` in place of per-facet flags.
**Owed in step 11**: `freshness`, figures on results and `GET
/api/ask/figure`, the `needs` of an edge into another collection (its
plugin's id→shard mapping), results carrying `certification` (step 10);
`hold key trust <key> --restore|--deny` over the trust file goes with
step 10's `hold key`. Known trade-off kept from before: a burned
producer's vectors are re-embedded locally rather than dropped from
search at once (03 §3.5 says dropped) — they serve until the worker's
delete-then-insert replaces them.

- MCP server exposing `louise_search`, `louise_lookup`, `louise_traverse`.
  Results carry snapshot, trust level, certification.
- `louise_search(query, collections?, space?, filters?, limit?, as_of?,
  freshness?, translate?)`. `freshness` and `translate` are per-request
  booleans defaulting to false and are **not** node settings.
  `louise_lookup(id)` takes no `as_of`.
- `ask facets <collection> [<field>] [--under VALUE]`: the fields a
  collection can be filtered on, or one field's values with this peer's
  counts.
- `question` is the literal `"relevance"` on every leg; there is one
  list. The rule that legs answering different questions are never fused
  is implemented and costs nothing until the search-axes amendment
  declares a second.
- The `relations` member is followed by the traversal tool, which
  reports unresolved cross-collection ids rather than hiding them.
- `--as-of DATE` is `YYYY-MM-DD`, read at 00:00:00 in the collection's
  `origin.clock`; a shorter form exits 2.

**Tests**: tool schemas validate; a search result carries the fields of
05 §5.3; a `filters` clause restricts the legs and never ranks; a
`ReferenceSource` lookup enters the context as a pinned block and never
RRF (this is CLAUDE.md invariant 4 and it gets a test here); an
unresolved reference names the shard it needs.

### Step 12 — Retire the old names
*07 §7.6.* **Runs LAST, after steps 13 and 14**, which were added on
2026-09-19 and keep their numbers rather than renumber this one.

Remove the aliases, `app/cli.py`'s old tree and the `tools/` scripts now
under `louise dev`. Update `README.md`'s quickstart to the new commands.
With the legacy collection shape go the two things that exist only for
it: `app/verify/backend_gate.py` — a legacy collection has no Codex, so
no calibration member to gate against, and it keeps this gate until it
is gone — and `codex_only = false`.
Delete this file.

### Step 13 — The engine in the Codex, the run in the answer, RAGE
*02 §2.4, 03 §3.3, 07 §7.8, 09 §9.3, 09 §9.12 (0d85f7f, dd9bef4,
8bbc66a). The design is [`rage.md`](rage.md); the review that shaped it
is [`rage-review.md`](rage-review.md). Wraps `app/hold/codex.py`,
`app/hold/embedder.py`, `app/embeddings.py`, `app/llm.py`,
`app/translate.py`, the rerank client, `app/dispatch.py`,
`app/gpu_governor.py`, `deploy/dnabert2`, `deploy/madlad-translate`,
`deploy/medcpt-rerank`.*

It supersedes two things step 3 says. `--accept` and "drift detection"
are gone: dd9bef4 validates a run automatically and the platform states
the run on every answer, so there is no baseline to accept and nothing
to drift from. And `encoder_image` alone is gone from every object that
carried it: the engine, by `engine_id`, replaces it.

Six parts, in this order; each is a commit series of its own with its
tests, and every pre-existing test stays green after each.

- **13a — Codex v3. DONE 2026-09-19.** What landed, and what it found:
  - `app/hold/codex.py`: `Engine` and `engine_id`; an encoder in one of two
    shapes — an engine from version 3, an image below it — that `validate`
    AND the file reader tie to the Codex's version; `query_vectors` in the
    calibration block; `space_id` without the engine at 3 and unchanged at
    2. A version-2 Codex loads, keeps its `space_id` byte for byte and is
    never rewritten.
  - the artefacts that named an image each moved one version and still
    read the old one: bundle header 2 (`bundle.py`, `export.py`),
    attestation 3 (`check.py`), calibration member and its signature 2
    (`calibration.py`, `collection.py`) with the QUERY side's reference
    vectors, the statement 2 and the digest's `benchmarks` row
    (`embedder.py`). An artefact written for a version-2 collection keeps
    the image and its old version: those bytes are what its peers hold.
  - the gate takes the run's FINGERPRINT and measures BATCH INVARIANCE in
    the same pass, and gates a bi-encoder's query run against its own
    reference (`role="query"`). A version-2 statement names the run; for an
    endpoint that states none the verdict is reported and nothing is kept.
  - `create` reads `[space.encoder.engine]` from the recipe and refuses an
    image alone; `--query-encoder URL` for a bi-encoder, on the command and
    on its route; `derive` keeps the mother's version in her space (a
    version-3 sister of a version-2 mother would land in another space) and
    writes 3 in a new one.
  - FOUND: gutenberg's recipe names a `[space.query.encoder]` identical to
    its document encoder. Under version 3 that is an asymmetric space on
    paper — a second, identical reference to pin and gate. A symmetric
    model names no query encoder; both recipe notes say so now, and say
    they made version-2 Codices and cannot make another.
  - 66 tests failed and 132 errored when `CODEX_VERSION` became 3; the
    fixtures are the spec's version-3 worked example now, with a version-2
    twin (`pubmed_v2`) that pins the old shape.
  Scope as first written: `space.encoder = {weights, engine, label}`, the
  engine object and `engine_id`, `space_id` without the image, a
  non-null `space.query.encoder` as `{weights, engine}`,
  `space.calibration.query_vectors`, the version bumps of 09 §9.18
  (codex 3, bundle header 2, attestation 3, digest 2, calibration
  objects 2), the statement at version 2 with `run`, `engine`,
  `driver`, `fingerprint`, `batch_invariant`, and 09 §9.3's weights
  digest for a directory. Version 2 Codexes are still read and never
  rewritten: `version` is inside `collection_id`.
  *Tests*: the v3 worked example of 09 §9.3 byte for byte, its
  `collection_id` and `space_id` pinned; a v2 Codex still loads and its
  `space_id` does not move; one engine → one `engine_id` across key
  order; the manifest digest golden, a symlink refused.
- **13b — The three servers. DONE 2026-09-19, as a launcher.** Not argv
  in each server, as first scoped: the servers keep reading their
  environment, which is right for an app of their own, and
  `deploy/rage/servers/launch.py` takes `--weights --device --host
  --port`, sets the variables each server reads, switches the Hugging
  Face hub off, and runs the server IN THE SAME PROCESS, so
  `/proc/<pid>/cmdline` is the template RAGE checks. One source per
  server, two ways to start it, and today's apps untouched but for
  binding the `HOST` they are given. The `build` string is declared in
  `rage.toml`'s `engine`, where 09 §9.3 reads it.
  *Tests* (`tests/rage/test_servers_launch.py`): the arguments become
  each server's variables (OpenVINO's `GPU`/`CPU` among them); absent
  weights are an error naming the path; every argument is required.
- **13c — `app/rage/`, the front. DONE 2026-09-19** (95 tests, 6 s;
  [`rage.md`](rage.md) §17 lists each piece and its proof). `rage.toml`
  → llama-swap yaml;
  admission; load, unload, restart; the run identity; `Louise-Run` on
  every proxied answer; the clock; the admin token; the persisted run
  table. It imports no GPU stack (invariant 7) and nothing of `app/`
  that opens a manifest. Three things differ from the scope written
  below, each found while building: the yaml is ONE GROUP that never
  swaps, not "no groups" (llama-swap's default for a model in no group
  is one model at a time); a card's name and capacity are DECLARED in
  `rage.toml`, because reading them meant importing the GPU probe, which
  nothing may import; and the API writes `rage.d/<id>.toml`, never the
  operator's hand-written `rage.toml`.
  *Tests* (`tests/rage/`): the generated yaml golden — one group, no
  swapping, ttl 0; the run id from a fake `/proc` tree — stable across two
  launches with different ports and host names, different for a changed
  flag, weights byte or image; no match or two matches → unstated, no
  header; refuse to start without a digest in `RAGE_IMAGE`, the message
  naming the variable and never a value; admission arithmetic, `503`
  with what is loaded, single-flight under ten concurrent first
  requests; the header on a streamed answer; the front never merges two
  requests; `oom_score_adj` written for a child; admin routes 401
  without the token, the token read from its file; the clock held while
  a `chat` request is in flight and released after, restored on
  SIGTERM.
- **13d — The image, and the first proof. BUILT ON THE NODE AND PUBLISHED
  2026-09-19** (`ghcr.io/sourstar/rage@sha256:8ee29dfa…0489`, PUBLIC and
  verified by an anonymous pull; superseded on the node 2026-09-20 by
  `…d7686483…7aa4`, the front that reads the driver from the PCI bus; trial-built locally first,
  which found and fixed two defects — [`rage.md`](rage.md) §17). On the
  node, with the card and no model loaded: llama-server, torch XPU and
  OpenVINO all see the Arc Pro B70 from the image. **The proof with real
  models is the cut-over evening (13f)**: the per-model apps go off first. `deploy/rage/Dockerfile`, `deploy/rage/rage.example.toml` and
  `deploy/truenas-rage.yaml` exist and `tests/rage/test_deploy_files.py`
  holds them to the code: the example loads, every program it starts is
  in the image, the image carries exactly what the front imports, the
  models are bound read-only, the token is a path. Nothing has been
  built, published or run: that needs the node, a registry and the
  operator's values. Only the node can settle what is left: torch seeing
  the card, llama-server itself (it aborts without an Intel GPU), every
  number of §14. The llama-swap
  archive's name and checksum ARE verified, and the real v256 binary was
  run locally on the generated yaml with stub children — no swapping,
  per-model unload, the run identity matched through a real `/proc`
  ([`rage.md`](rage.md) §17). Scope as first written:
  `deploy/rage/Dockerfile`,
  `deploy/truenas-rage.yaml` (an app of its own: an optional helper
  never shares Louise's app), the image published by digest.
  [`rage.md`](rage.md) §14 is the done-when list, run on the node.
  **The operator's stop**: the numbers go in STATUS.md and in the commit
  message — load, unload and device-change times per model, one full
  gate per encoder, each model's measured memory.
- **13e — The clients. DONE 2026-09-19** (`tests/test_run_check.py`, 30;
  `tests/test_louise_level.py`, 8). Where it differs from the scope
  below, each found while building. The worker CHECKS at the write and
  never gates there: several document threads reach the check at once,
  so the gate lives in `backend_gate.embed_allowed` — one thread, once
  per (space, endpoint) — and the write only defers. A pass validates a
  run FOR THE SPACE, so a statement kept under one collection counts for
  every collection of that space on the peer. The query encoder of an
  asymmetric space is gated by itself, from the same place. For a
  version-3 collection the run check replaces the behavioural baseline
  and no setting turns it off. `share contribute` does not exist yet
  (step 9); `vectors build` is the command that refuses. A CHECK runs
  under a validated run too (03 §3.3): `hold check`, `hold vectors
  check`, T3 and an export's challenge embed through
  `run_check.checking`, and an answer from any other run makes them
  inconclusive (exit 3; T3 is SKIPPED, as for a peer with no encoder).
  As scoped:
  Every writer reads `Louise-Run` and checks it

  against this peer's statements BEFORE the write, inside no
  transaction (the HTTP call stays outside; the check is a dictionary
  lookup). An answer naming an unvalidated run writes nothing — a
  deferral, never an error on a document — and the worker runs the gate
  against that run at once. An endpoint that states no run: search with
  it, write nothing (`vectors build`, `share contribute` exit 2). The
  four clients send `Louise-Level`. `app/llm.py` moves to
  `/v1/chat/completions`. `RAGE_URL` in `config.py`.
  *Tests* (`tests/test_run_check.py`): an answer naming an unvalidated
  run writes no row and marks nothing; a run that changes mid-document
  stops the write and the document stays selectable; a run-less
  endpoint serves a query and refuses a write; the gate is triggered
  once per (space, run), not once per answer; `Louise-Level` on each
  client's request; chat streams through the OpenAI shape.
- **13f — The cut-over, then the retirements. THE CUT-OVER IS DONE
  (2026-09-19 night; numbers in [`STATUS.md`](STATUS.md)); the retirements
  wait on [`legacy-to-delete.md`](legacy-to-delete.md) §7; chat moved
  to RAGE the same night.** As planned: Not "one URL at a
  time", as first written: the operator's rule (2026-09-19) is that the
  per-model apps are SHUT OFF BEFORE RAGE LOADS — together they would
  hold every model twice on the card, and RAGE takes one of their ports
  (30437). The order is in `deploy/truenas-rage.yaml` and
  [`rage.md`](rage.md) §16: record today's vectors FIRST (13d's proof
  compares against them, and once the apps are off there is nothing left
  to compare with), `node pause`, stop the five apps, install RAGE,
  switch the URLs (three collection files in place, `TRANSLATE_URL`,
  `RAGE_URL`), `node resume`. Ollama stops only after 13e's `llm.py`.
  Back out: stop RAGE, start the apps, restore the URLs. So 13d's proof
  and 13f's cut-over are one evening on the node, and need a small tool
  first: `louise dev` recording each endpoint's vectors for the sixteen
  fingerprint texts and a sample of the calibration set.
  Then the governor app, Ollama, TEI and the
  per-model apps go: each gets an entry in
  [`legacy-to-delete.md`](legacy-to-delete.md) with its conditions
  (complete, confirm, only then delete). `backend_gate.py` is NOT
  retired here (step 12).

**Operator**: the registry and image name, a registry login on the node
stored by path, RAGE's port, its state directory, the chat GGUF
([`rage.md`](rage.md) §16).

### Step 14 — Quarantine — **REPORTING DEPLOY BUILT 2026-09-19; ENFORCED ON THE NODE 2026-09-20 (cdaff93)**
*The second deploy, as the operator decided it (2026-09-20)*: enforce, with
the three live copies listed by collection id under `[node]
quarantine_in_use_before` — they cannot run roots-and-chunks or the origin
check yet — and the NEXT collection tested for real (spec main 8b025a2,
03 §3.6; [`STATUS.md`](STATUS.md), 2026-09-20 afternoon).
*Built:* `app/copy_state.py`, the one derivation; `[node] quarantine =
"report" | "enforce"` in `platform.toml`, default `report`; the search
(`withheld`, `shards: "all"`; `legs_withheld` for a failed run), the
worker's embed, bundle-import and archive-import gates (the gate FIRST,
quarantine second, and the archive FETCH goes on), the export door, the
four `hold vectors` commands, `louise health`, the Library card, `hold
collection show`, `hold collection ack --origin-unchecked`, and `hold
check` noting an origin check that could not run
(`tests/test_quarantine.py`, 27). *Measured* (dev container, 2026-09-19):
one evaluation is 0.20 ms warm whether 10 or 510 attestations are held —
it was 2.6 ms at 510 until the directory's own mtime became the
signature, a per-item cost caught before it shipped — and 95 ms cold
per 500 signatures, once per change. ON THE NODE (deployed 2026-09-19,
026985b): 0.13–0.25 ms warm per collection. The reporting deploy showed
what stands between it and enforcing: gutenberg and wikipedia have no
record shards in their drags, and `records-vs-origin` has no fetch step
([`STATUS.md`](STATUS.md), "DEPLOYED 2026-09-19"). *Not built:* nothing runs `roots`,
`chunks` or `records-vs-origin` by itself — they are `hold check`, by
hand or cron. A scheduled run changes what runs when and moves origin
bytes: [`scheduling.md`](scheduling.md) §6 wants it measured on the node
first. It is the one thing between this and "clears by itself"; until
then enforcing means running `hold check` per Codex collection first.
`node show` is not built; `louise health` carries the state. *Settled
while building:* test 2 (T0–T2 of every bundle held) has no open state,
because nothing lands without them; a peer with no key is told its checks
leave no record (no unsigned substitute was invented); `--origin-unchecked`
also refuses when the origin check was never TRIED, since nothing then
says it cannot run.
*An amendment to name on `main`, not yet made:* 03 §3.3 has a failed run
withhold the vector leg "and the search says so", and 05 §5.7's response
object has no place to say it — `withheld` names copies left out WHOLE.
Today's response carries `legs_withheld: [{collection, leg: "vector",
reason}]` beside `withheld`; 05 should adopt that, or name the leg inside a
`withheld` entry, before step 11 builds `louise_search`'s object.

*03 §3.3, §3.4, §3.6; 05 §5.3; 07 §7.3, §7.8; 09 §9.3 (98ee4dc).
Reasoning: [`rage-review.md`](rage-review.md) §18. Wraps
`app/collections.py`, `app/licensing.py`, `app/retrieval.py`, the
worker's stage selection, `share`, `hold collection show`.*

- A copy's state — `refused`, `quarantine`, `in-use` — is ONE function
  of what the peer holds (its own attestations, its calibration
  statements, the licence ack, the origin ack, the refuse-to-start
  checks), called by the worker, the web, `share` and `hold collection
  show` alike. It writes nothing and caches on physical identity (the
  collection's path), never its name. The licence gate stays
  `app/licensing.py`'s resolution, called from it, never re-implemented
  (invariant 9).
- The five exit conditions of 03 §3.6; `hold collection ack
  --origin-unchecked`, refused while another test is open or has failed,
  or when the origin check has in fact run.
- A quarantined copy enters no search, in either leg, and is named under
  `withheld` with `shards: "all"`; it is not announced, served, embedded
  for, contributed to, imported into or certified. Its fetch, its five
  tests and its gate go on.
- An untested run re-quarantines every collection of its space; a FAILED
  run withholds the vector leg only; an unreachable encoder or origin
  returns nothing to quarantine.
- **Codex collections only.** A legacy collection is never quarantined.
- **Two deploys.** The first computes and SHOWS the states and withholds
  nothing, while the gates and the origin samples run; the second
  enforces, once `hold collection show` reads `in-use` for every
  collection meant to be. A switch in `platform.toml` (desired state),
  removed at step 12. Without this every Codex collection would leave
  search at once on the day of the deploy.
- Per-round cost: one state evaluation per collection, each a handful of
  lookups; the origin sample is the plan's, bounded, within the transfer
  budget (invariant 5), and a deferral when the origin is away. Time it
  on the live manifests before it ships (CLAUDE.md: a plan check is not
  enough).

*Tests* (`tests/test_quarantine.py`): each reason enters quarantine and
clears by its own test; the state is derived — no row, no file is
written for it, and two calls with the same holdings agree; a changed
run re-quarantines every collection of the space and only those; a
failed run leaves the text leg searchable and says so; a legacy
collection is always `in-use`; a quarantined copy appears under
`withheld` and in no list; `share` announces nothing of it;
`--origin-unchecked` is refused with a test open, accepted with the
origin away, and records time and key; a later `records-vs-origin`
failure re-quarantines; in the reporting deploy nothing is withheld.

**Operator**: the word to enforce, after the reporting deploy.

### Step 15 — RAGE over LocalAI: the run identity alone — **ACCEPTED 2026-10-03; 15a, 15b, 15d, 15e DONE**
*Why:* [`rage-over-localai.md`](rage-over-localai.md). LocalAI takes the
model lifecycle (launch, load, preload, cache, evict); RAGE keeps one job,
the run identity of every answer, read from what was launched. Spec
amended the same day: 03 §3.3, 09 §9.3 (`options`), §9.12, §9.18 (Codex
4, calibration statement 3), 02 §2.4 and §2.7.
- **15a — The formats. DONE 2026-10-03** (`tests/hold/test_engine_options.py`,
  17; four mutations of the code each caught; whole suite 3439 passed).
  What landed:
  - `CODEX_VERSION` 4 and `OPTIONS_VERSION`. `Engine.options` is written
    only when present, so a version-3 engine keeps its `engine_id`.
  - `validate_options` enforces the rules in 09 §9.3: no absolute path (a
    URL included), no float, never null, never empty.
  - `derive`: a sister in a new space is written at `CODEX_VERSION`, as
    its docstring always said; in her mother's space she keeps the
    mother's version unless her engine carries `options`.
  - Statements are written at version 3 with the engine checked before
    signing; `read_statement` reads versions 1–3 and refuses the rest.
  - Five tests that pinned the old versions moved with them.

  As scoped: Codex version 4: an engine MAY carry `options`,
  absent for an engine started by `args` (`app/hold/codex.py`,
  `CODEX_VERSION`, the engine's keys at its `_only` check; readers 1–4).
  The peer's calibration statement version 3 beside 2
  (`app/hold/embedder.py`). *Tests:* a version-3 Codex's engines keep
  their `engine_id` byte for byte; a version-3 `space` copied by
  `space_from` into a version-4 Codex keeps its `space_id`; `options` in
  a version-3 Codex is refused as an unknown key; an `options` value
  naming an absolute path other than `${WEIGHTS}` or `${MODELS}` is
  refused, and so is a non-integer JSON number; version-2 statements
  still read.
- **15b — P1, the LocalAI patch. DONE 2026-10-03.** `GET /system` lists
  each loaded model's `load_options` and `load_options_hash`
  (`deploy/localai/patches/`, base 58830f7). Two Ginkgo specs in the
  patch; the load spec fails with the hook removed. Recorded with every
  LocalAI action item in [`localai-bugs.md`](localai-bugs.md);
  `tests/rage/test_localai_patches.py` (4) holds the series to that list.
- **15c — Go/no-go, on the node. RUN 2026-10-04, unattended (operator):
  bge-m3 PASSES; the operator decides.** Results in rage-over-localai §12.
  wikipedia's own gate passed three times out of three on the new engine
  (16/16, every one of the 512 cosines ≥ 0.999004), at 10,700 tokens/s
  against the live engine's 9,509. The MedCPT reranker on the card matched
  the live one to 3e-8. It found LocalAI bug B9 (a NaN vector fails the
  whole request), now retried by Louise's client. Not run: the spill test,
  and DAR-legal-it and MADLAD on the card. Each would have squeezed the
  live models.

  As scoped (rage-over-localai §8.1–§8.2): LocalAI
  at the base with the series applied, its llama-cpp SYCL backend, on the
  B70. bge-m3 gated by hand: fingerprint, batch invariance, the
  fingerprint again under spill, tokens per second against today's
  llama-server. **The operator's stop**, like 13d. If it fails, today's
  RAGE keeps running.
- **15d — The front over LocalAI. DONE 2026-10-03**, beside today's front
  and chosen by `RAGE_RUNNER=localai` (`tests/rage/test_localai_front.py`,
  37, against a fake LocalAI that does what 58830f7 does; seven mutations
  of the code each caught; all RAGE tests 213).
  - New modules: `localai.py` (client), `loadopts.py` (09 §9.3 options),
    `observer.py` (instances and runs), `localai_config.py`,
    `front_localai.py`.
  - `wire.py` holds what both fronts share, so 15g can delete the old one.
  - Where it differs from the scope below:
    - The device is read, not declared: the card is the one its DRM
      clients report (`drm-pdev`), or the CPU when it holds none. A model
      that fell back to the CPU is stated as a CPU run.
    - `name`, `build` and `variant` come from LocalAI: `localai/<backend>`,
      its `/version`, null. `rage.toml` declares only the cards.
    - The before/after check is (pid, start ticks) from `/proc`, because
      every LocalAI load is a new process. `load_options_hash` is not
      needed for it.
    - With no process before the request, the answer is attributed only
      to a process that started before its headers arrived.
    - `PUT` writes JSON, which is YAML, so no YAML library is added.
    - `LOCALAI_API_KEY` is stripped and the §10 switches are forced on the
      LocalAI that `__main__` starts.
  - **The URL names the model, and nothing else does** (operator,
    2026-10-03: "the user should not bother about choosing the right
    model"). The front sets the body's `model` to the URL's. It first
    refused a mismatch, which would have made every client spell the
    model twice.
    - The refusal also hid a defect: Louise's query side sends the
      DOCUMENT encoder's name to the query encoder's URL
      (`embeddings._side`).
    - Under LocalAI that would have embedded a MedCPT query with the
      article encoder, under the query encoder's run.
    - Fixed at the source too (2026-10-03): `_side` names the model the
      query URL names (`collections.model_named_by`), and names none for
      a URL outside the model container. An endpoint that needs a name
      then refuses rather than embedding with the wrong model
      (`tests/test_embed_recipe.py`).
    - Tested: `test_a_query_is_embedded_by_the_query_encoder_its_url_names`.

  As scoped: `app/rage/runs.py` `match` reads
  `/system` (pid, `started_at`, `load_options`) and `/proc` in place of
  llama-swap's argv. The options are made canonical by 09 §9.3. The
  check before and after each answer compares (pid, start) and
  `load_options_hash`, and the weights are hashed at every new (pid,
  start). *Tests*, against a fake `/system` and a fake `/proc`:
  - a reload with unchanged options states the same run;
  - one changed option states another run;
  - a default LocalAI filled in, absent from the YAML, is in the run;
  - an option naming another path states no run;
  - a process replaced mid-answer gets no header;
  - a body naming another model than its URL is answered by the URL's;
  - a new run records an event naming the options that changed since
    the model's previous run (rage-over-localai §10);
  - **the admin guard**: `load` and `unload` reach LocalAI's
    `/backend/load` and `/backend/shutdown` only with the token, and a
    request without it is 401 and never reaches LocalAI;
  - **the panel**: `GET /louise/gpu` gives the card's real memory split
    by model from `size_vram`, with the rest as a stranger's; each process
    is labelled with its model by `process.pid`; and the events are a
    model loaded or unloaded between two `/system` readings, and a new run;
  - **the restart**: it shuts down and loads the named model only, and
    with unchanged options the reload states the same run.
  - **`PUT` and `DELETE`** write LocalAI's model YAML (operator,
    2026-10-03; rage-over-localai §7):
    - `PUT` writes `<id>.yaml` and calls `/models/reload`;
    - `PUT` refuses with 422 a YAML that leaves an option of §10's list
      unpinned;
    - `PUT` of a loaded model shuts it down, and its next load states a
      new run with the changed options in the event;
    - `DELETE` shuts the model down, removes its YAML and reloads, and
      the model is gone from `/system` and from LocalAI's model list;
    - either one on a hand-written YAML is 409, and without the token
      401.
  The front's `park` route goes with `placement.py`.
- **15e — The image. DONE 2026-10-03; built and published from the node 2026-10-04:**
  `ghcr.io/sourstar/rage@sha256:b203a361a09bd515f0ca1e9293cf30a7a516384fd3785ceecc1941d7bc1a092d`
  (tag `localai-58830f7-1`, from dd84df6; base `server-intel-b10630` by digest).
  Built in it: LocalAI `58830f7+patches.17b54353635e`, torch 2.14.0+xpu,
  transformers 4.46.3 (MADLAD), sentence-transformers 5.1.0 (DAR-legal-it),
  OpenVINO 2026.3.1 (MedCPT); every backend imported at build. Not yet run:
  that is 15c.
  - **Done** (`tests/rage/test_localai_deploy.py`, 8):
    - `deploy/rage/Dockerfile.localai`: LocalAI from the series' base with
      the series applied; `protoc` by checksum; the web UI built; LocalAI's
      SYCL llama.cpp backend from the same commit, by digest, in the
      read-only system backends path; galleries off. Every base is pinned
      by digest; the Intel base is passed, as before.
    - `deploy/rage/localai-models/`: the six llama.cpp models' YAML, every
      §10 option pinned.
    - `deploy/truenas-rage-localai.yaml`: the weights mounted read-only
      inside the writable models directory.
    - The observer refuses a backend whose program lies outside the
      image's backends path (09 §9.12).
  - **Built here**: every LocalAI stage, giving
    `58830f7+patches.17b54353635e`. **Run here**: the `smoke` target on a
    CPU (rage-over-localai §11), real LocalAI under RAGE's front. A run
    was stated on every answer.
  - **The smoke test found** a random seed per load (now pinned, and
    required by `PUT`) and LocalAI bug B8 (reload never forgets a deleted
    YAML; `DELETE` says so).
  - **A defect, fixed before 15e-2:** LocalAI copies `env:` into the load
    call as `EnvVars`, and the options' path rule refused the six YAMLs'
    `NEO_CACHE_DIR`: no run on the node. 09 §9.3 amended (operator,
    2026-10-03): the environment is the engine's `env`, stated once.
    `test_every_model_yaml_states_a_run_once_localai_has_completed_it`.
  - **Not built in the cloud sandbox:** the final stage, whose Intel base is
    on ghcr.io's blob storage. Built on the node instead (above).
  - **15e-2, DONE the same day** (`tests/test_localai_backends.py`, 23;
    three mutations of the backends each caught):
    - `deploy/localai/backends/`: `louise-dar-legal-it` (`Embedding`),
      `louise-medcpt-rerank` (`Rerank`), `louise-madlad` (`Predict`). Each
      wraps its server's model code unchanged and reads its settings from
      the load call; a misspelt option refuses the load. Their YAMLs are in
      `deploy/rage/localai-models/`, pinned like the six.
    - The image: stubs generated from the series' `backend.proto`,
      LocalAI's `grpc_auth` from the same commit, one torch (pinned
      2.14.0), each venv with the pins its server was proven with.
    - **The translator's batch travels in one prompt** (operator): LocalAI
      calls `Predict` once per prompt. `app/translate.py` speaks it under
      `TRANSLATE_API=completion`, beside today's `/translate` until 15g.
      A contract test runs the client against the backend.
    - MedCPT is read as `api = "jina"`; the cut-over steps in the app file
      say what changes in the pubmed collection's file.
    - **Run here** (rage-over-localai §11.1): the MedCPT backend on a real
      LocalAI, on a CPU. It found `run.sh` started relatively, which made
      `args` relative; fixed and tested. DAR-legal-it and MADLAD need
      torch, which this sandbox cannot download: they run at 15c.
    - The app file has no host path for DAR-legal-it's weights (nor does
      today's): a placeholder the operator fills from DEPLOYMENT.md.

  As scoped: LocalAI built from the base with the series,
  backends installed at build time from images pinned by digest, gallery
  installs at run time off, LocalAI on loopback with no key,
  `LOCALAI_DISABLE_HARDWARE_DEFAULTS=true` and
  `LOCALAI_DISABLE_GUESSING=true`. The models path is a writable
  directory under RAGE's state holding the YAMLs, with the weights
  bind-mounted read-only inside it, and `HF_HOME` is set outside it.
  Every model's YAML pins every option
  LocalAI would otherwise fill in (rage-over-localai §10: `context_size`,
  `batch`, `threads`, `gpu_layers`, `parallel`), so that only an edit
  changes a run. A test holds each YAML to that list. The three
  servers become LocalAI Python backends: DNABERT-2 → `Embedding`, MedCPT
  → `Rerank`, MADLAD → `Predict`. The app's translator client moves to
  the completion shape. `tests/rage/test_deploy_files.py` holds them to
  the code, as in 13d.
- **15f — The cut-over. DONE 2026-10-05**, run by the operator (phase 1
  05:31–05:37 UTC, phase 2 to 05:44). Every model answered with a run.
  The suite passed on the node (3533). Louise resumed (`running`), and
  gutenberg's backend gate reproduced its baseline on the new engine.
  Calibration answers before the swap and after, per collection and side:
  - gutenberg document 0.999999, query 0.999999;
  - pubmed document 1.000000, query 0.999833;
  - wikipedia 0.999993;

  each the lowest of 512 cosines; every median is 1.0. Found:
  - LocalAI's 500 "exceeds the available context size" was not shrunk on.
    Fixed in 1609aa9, deployed by the operator at 06:12 UTC. Until then a
    chunk over the model's context would have become an error on its
    document. None did; no
    document carries an embedding error since the swap.

  The plan as written before the run:
  Operator, 2026-10-04: "suspend dar-legal for now, go on with the rest",
  "run unattended". The permission classifier refused the deploy from the
  agent's session, so it is two scripts for the operator. They were placed
  on the node with the commit's tree:

      ssh root@192.168.178.10
      bash /mnt/ssd5/apps/louise-rage-15c/tree/deploy/rage/15f/phase1.sh 2>&1 | tee /mnt/ssd5/apps/louise-rage-15c/cutover/phase1.log
      # ends "phase 1 done"; then
      bash /mnt/ssd5/apps/louise-rage-15c/tree/deploy/rage/15f/phase2.sh 2>&1 | tee /mnt/ssd5/apps/louise-rage-15c/cutover/phase2.log
      # anything wrong, at any point:
      bash /mnt/ssd5/apps/louise-rage-15c/tree/deploy/rage/15f/backout.sh

  - **Phase 1** (`deploy/rage/15f/phase1.sh`). It stops at the first
    failure.
    1. Backs up both apps' definitions (Louise's with its token in a
       0600 file, never printed), `rage.toml` and `pubmed.toml`. Tags
       `louise-rag:pre-15f` and `rage:pre-15f`.
    2. Pauses Louise.
    3. Copies the eight served YAMLs in and cuts `rage.toml` to the cards.
    4. Updates `louise-rage` to the new image,
       `ghcr.io/sourstar/rage@sha256:e64893cf…4e26` (built from main
       6246334, with the half-maximum clock), with the volumes of
       `deploy/truenas-rage-localai.yaml`.
    5. Edits pubmed's two `[reranker]` lines to LocalAI's `/v1/rerank` and
       `api = "jina"`, refusing any other diff.
    6. Deploys Louise from the tree, staged and then promoted per
       DEPLOYMENT.md, builds it, runs the suite, and sets
       `TRANSLATE_API=completion`.
    7. Asserts that web and worker run the new image.
  - **Phase 2** (`deploy/rage/15f/phase2.sh`).
    1. Sends one request to each of the eight models and requires a run on
       every one.
    2. Resumes Louise (`running`, the mode it had).
    3. Records every collection's calibration answers again and prints
       their cosines against the ones recorded before the swap
       (`record15f.py`; the "before" records are in
       `/mnt/ssd5/apps/louise-rage-15c/cutover/`).

    The worker then re-gates each new run by itself.
  - **Backout** (`deploy/rage/15f/backout.sh`): both apps from the
    backups, `rage.toml`, `pubmed.toml`, `src.old` and
    `louise-rag:pre-15f`. Louise is left paused for the operator to
    resume.
  - **Proven before it, on the node, beside the live RAGE** (2026-10-04):
    - every one of the eight models under LocalAI against the live one:
      encoders at cosine 1.000000, rerankers in the same order (bge
      exact, MedCPT within 2e-8);
    - MADLAD correct through the one-prompt batch;
    - chat answering word for word as today, once `chat.yaml` asked for
      the model's own template (944fd50);
    - main 6246334 + 944fd50: 3532 tests on the node.
  - **DAR-legal-it: suspended** (operator). No collection uses it; its YAML
    is in `localai-models/suspended/`.

  As scoped: as 13f: record today's vectors first, `node
  pause`, swap the image, re-gate every space (a new engine, 03 §3.3),
  `node resume`. To back out, deploy today's image by its digest.
- **15g — Retirements. DONE 2026-10-05** in the repository (legacy-to-delete §8;
  suite 3378 on the node). The node keeps the backups until the operator
  removes them. As scoped, through [`legacy-to-delete.md`](legacy-to-delete.md):
  `admission.py`, `placement.py`, `swap.py`, `swapyaml.py`,
  `deploy/rage/servers/`, and the lifecycle keys of `rage.toml`
  (`memory`, `priority`, `preload`, `park`, `cmd`).

**Operator**: file [`localai-bugs.md`](localai-bugs.md) upstream; run 15c
on the node.
### Step 16 — Deposit collections and the LocalAGI routes — **specified 2026-09-29; 16a–16e DONE on a side branch, merged into main 2026-10-05; 16f DONE 2026-10-06; `hold collection forget` DONE 2026-10-06 — STEP 16 DONE**
*01 (vocabulary), 02 §2.9, 03 §3.2, 04 §4.4, 06 §6.7, 07 §7.2–7.4,
§7.6–7.8, 09 §9.13, §9.17. Design and reasons:
[`deposit-collections.md`](deposit-collections.md). Wraps
`app/hold/codex.py`, `app/hold/collection.py`, `app/layers.py`,
`app/retrieval.py`, `app/licensing.py`, `app/auth.py`, `app/api.py`,
the transfer manager and the worker's stage selection.*

- **16a — the plugin and the Codex. DONE 2026-09-29**
  (`tests/deposit/test_deposit_identity.py`, 21; `test_deposit_config.py`, 5).
  `app/plugins/deposit/`, `app/hold/deposit.py`; `hold
  collection create deposit --name`, with the nonce in `origin.spec`,
  the node's `[deposit] encoder` or `--encoder`/`--space-from`, and the
  space block copied whole — no calibration run — when a deposit
  collection on that encoder already exists. `hold deposit
  add|erase|list|show`. Proven by `tests/deposit/test_deposit_identity.py`: two
  collections from one set of defaults have two ids; a re-added key
  replaces; `native_id`s ascend node-wide.
- **16b — parse. DONE 2026-09-30** (`tests/deposit/test_deposit_parse.py`,
  14). The PDF extraction runs in a child process under a 60 s deadline.
  `.txt`, `.md`, `.pdf` through `pypdfium2`, pinned
  in `requirements.txt` (operator's yes, 2026-09-29); every chunk cut
  through `layers.chunks_for_document`. Proven by golden files: the
  same bytes give the same record and the same chunks, PDF metadata
  kept, a scan and non-UTF-8 text refused.
- **16c — no trace. DONE 2026-09-30** (`tests/deposit/test_deposit_erasure.py`,
  10). Measured here (1 KB deposits, vectors written): one purge 122 ms at
  100 deposits, 130 ms at 500, 240 ms at 1,000 — the rewrite is whole, so
  it grows with the collection; `reset` pays it once. Measured to need it:
  without the purge, an erased deposit's text stays in the vector table's
  data file; and two documents in one text-index segment keep the deleted
  one counted there, its terms in the dictionary, until the rewrite.
  Erase removes the origin bytes, the rows, the
  text-index postings (segments merged), the vectors including old
  table versions (cleaned up), with `SECURE_DELETE` never turned off.
  Proven by `tests/deposit/test_erasure.py`: deposit, embed, erase, and
  the deposit's bytes appear in no file under the collection.
- **16d — the LocalAGI routes. DONE 2026-09-30**
  (`tests/deposit/test_deposit_localagi.py`, 17; `app/localagi.py`).
  AND RUN FOR REAL: LocalAGI's own `pkg/localrag` client, built from its
  checkout at `7e0947d`, against a live node — create, store, count,
  search, list, read, delete, sources, reset, create again — exit 0
  (`tools/localagi_check/`, which 16f re-runs). Found on the way: the
  client names an upload with its temp PATH (the key is the base name);
  multipart parsed with the standard library, not `python-multipart`;
  and `run_search`'s all-withheld answer had no `timings`, a KeyError in
  `/api/search` that predates this step (fixed at the source). Sources
  answer 501 until 16e.
  **A defect in 16a/16d, found at the start of 16e and fixed before it
  (2026-09-30):** `hold deposit add` ran the fts and rdf stages in the
  CALLING process — the web's, for a LocalAGI upload — and tantivy allows
  one writer per index while the worker keeps its own open (its catalog
  backfill alone holds one for every collection). Reproduced with the real
  worker running: a second process's add failed `CatalogUnavailable …
  LockBusy`. Single-process tests could not show it. Now every deposit
  write is a job (`app/jobs.py` DEPOSIT) on its own worker thread
  (`serve_deposits`), the web and the CLI queue and wait, reads stay in the
  web, and a job's row is forgotten once read (it names a key). Proven the
  same way: `tools/localagi_check` against uvicorn AND `python -m
  app.worker` as two processes — exit 0, no error in the worker log.
  07 §7.4's table on `app/api.py`,
  `DEPOSIT_TOKEN_FILE`, deposit collections withheld from every open
  `ask` route and the Ask page. The upload commits record, chunks and
  text index in its request and never calls the encoder; vectors are
  the worker's next round. Proven by
  `tests/deposit/test_localagi_contract.py` — every request LocalAGI's
  `pkg/localrag/client.go` (at `7e0947d`) sends, decoded the way it
  decodes the answer — and `tests/deposit/test_auth.py`.
- **16e — URL sources. DONE 2026-09-30**
  (`tests/deposit/test_deposit_sources.py`, 29, and the LocalAGI route
  test). `sanitize_url` checked against the OUTPUT OF LOCALRECALL'S OWN
  FUNCTION, run with Go on eight URLs. And run for real: LocalAGI's client
  (`tools/localagi_check … <source-url>`) against uvicorn and `python -m
  app.worker` — AddSource, the worker's sources thread fetching within
  two seconds, ListSources with its last fetch, the entry under
  LocalRecall's key, RemoveSource. Settled while building: sources are
  fetched only where the mode runs FETCH and PARSE; one write lock per
  deposit collection (the deposit and sources threads both write, and a
  purge rewrites whole indexes); `create` writes the empty `origin/`
  (the worker's discovery reported it missing every round — seen in the
  two-process run). `hold source add|erase|list`, `[[sources]]` in
  the collection's file, `hold collection ack --web-sources`,
  `GIT_PRIVATE_KEY_FILE`; every fetch through the transfer manager.
  Proven by `tests/deposit/test_sources.py`: keys equal LocalRecall's
  `sanitizeURL` on its own cases; nothing is requested before the
  disclaimer; one deposit per source, replaced whole, kept on a failed
  refresh.
- **The worker.** Embedding deposits and refreshing sources change what
  runs when: [`scheduling.md`](scheduling.md) §6's checklist and `dev
  schedule-report` numbers before 16d and 16e are deployed.
- **16f — the operator's stop,** like 6: one LocalAGI agent with
  `local_rag_url` on the node — memory stored, found, compacted; a file
  uploaded from its UI; one URL source.
  - **The encoder (operator, 2026-10-06): granite-embedding-107m-multilingual,**
    the spec's default, chosen over bge-m3 and qwen3-embedding-4b; its LocalAI
    YAML is in `deploy/rage/localai-models/`, its weights mount in
    `deploy/truenas-rage-localai.yaml`, and the node steps — weights placed,
    RAGE updated, the space file written from the run RAGE states, the token
    file, `platform.toml`, the first collection — are the deposit runbook.

## 4. Dependencies between steps

```
0 codex ── 1 drag/template ──┬── 2 records/snapshot ──┬── 5 check ──┬── 6 POC ── 7 GATE ── 8 migrate ── 9 share ── 12 retire
                             │                        │             │
                             └── 3 embedder ── 4 vectors ───────────┘
                                                      │
                                                      └── 10 certified ── 11 ask
```

- **0 blocks everything.** Every hash is downstream of the Codex.
- 3 is independent of 2; 4 needs 3; 5 needs 2 and 4.
- **6 is the operator's stop**: real data, one collection, numbers in
  STATUS.md.
- **7 is a decision, not code**, and it blocks 8 alone.
- 9 needs 8 to have something real to share.
- 10 and 11 need 5 and can proceed in parallel with 8 and 9.
- **13 and 14 (added 2026-09-19) run before 12, which stays last.**

```
0 ── 13a codex v3 ─────────────┐
     13b servers ── 13c front ──┼── 13d image + proof (operator's stop) ──┐
3, 4 ───────────────────────────┴── 13e clients ───────────────────────────┴── 13f retire apps
5, 8, 13e ── 14 quarantine (report, then enforce) ── 12 retire
13a ── 15a formats ───────────────────────────── 15d front ──┐
13f ── 15b patch ── 15c node go/no-go (operator's stop) ── 15e image ──┴── 15f cut-over ── 15g retire
```

- 13a needs only 0. 13b and 13c need nothing of the plan: the front
  hashes the engine object's SHAPE (09 §9.3) and none of Codex v3's
  code, so RAGE can be built and proved while 13a is in progress.
- 13e needs 13a (the statement's version 2), 13c (a platform that states
  a run) and 3–4 (the gate and `vectors build` it guards).
- 13d is an operator's stop like 6: real models, the node, numbers.
- 14 needs 5 (the checks it derives from), 8 (collections with a Codex)
  and 13e (validated runs). 9's `share` honours it from the day it
  enforces.
- **15 (added 2026-10-03)** needs 13f (a RAGE in service to replace) and
  13a (the Codex it extends). 15d needs 15a, because a LocalAI run's
  engine carries `options`. 15c is the operator's stop, and nothing past
  it starts until it passes.
- **16 (added 2026-09-29 as step 15; renumbered 2026-10-05 when main's step 15 became RAGE over LocalAI)** needs 3 and 4 (the gate and `vectors
  build`) and 13e (validated runs), and nothing of 2, 5 or 8–12: a
  deposit collection has no drag, no snapshot and no published checks
  (02 §2.9). Its sub-steps run in order, 16a first.

## 5. Kick-off prompt for Claude Code

> Read CLAUDE.md, then docs/README.md and the nine documents it lists,
> in order — 09 last and in full, since it is the reference the others
> point into. Then read docs/notes/migration-plan.md and
> docs/notes/STATUS.md. Implement
> **step N** of the migration plan exactly as scoped there: wrap the
> existing code it names rather than rewriting it (step 0 is the one
> exception and says so), add the new command beside the old one with a
> deprecation alias, and ship the tests the step lists. Every
> pre-existing test must stay green. If the step conflicts with a
> document, stop and ask; if it needs a dependency, ask before adding
> it. When the step is done, update docs/notes/STATUS.md with what is
> built and what it cost — with numbers — and mark the step done in
> this plan.
