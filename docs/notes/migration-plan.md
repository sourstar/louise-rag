# Migration plan — from the current code to the documented architecture

*Working document for the `louise` branch. The specification is
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

```
git checkout -b louise            # specs stay on main; code lands here
```

Specs are edited on `main` and merged forward into `louise`; code never
lands on `main`. On a machine with the real environment:

```
docker build -t louise-rag:dev .
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

### Step 2 — `hold records build` and `hold snapshot create`
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

### Step 3 — `hold embedder check|benchmark|calibrate`
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
  reference.

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

### Step 4 — `hold vectors build|import|export|check`
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

**Sub-tasks, in order. `a`, `b` and `c` are DONE (`e61d4d2`, `728b127`,
and `c` by measurement rather than by code — see below).**

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
- **d. `export` writes the four-file DIRECTORY** of §9.7. 07 §7.3 spells
  `--out FILE`; 09 defines a directory and fixes no container. Read
  `--out` as a directory and amend 07.
- **e. `import`** through T0–T3, replacing `app/verify`'s tiers. Fix the
  accepted row set BEFORE any tier runs and slice the sixteen
  `calibration:*` challenge rows off first — otherwise a producer pads a
  bundle with rows it knows will be refused and dilutes T0's statistic by
  the padding factor. Derive coverage from merged chunk ids, never by
  expanding the header's `uids: [first, last]` range: that was only
  accidentally correct under whole-bundle refusal.
- **f. HALF DONE.** `licensing.require_ack` in the importer and in
  the archive path (invariant 9). The ARCHIVE half landed early, because
  its path survives `e` and the hole was live: `genesis_fetch` gated the
  FETCH, so a shard already on disk — a local archive whose vectors
  travel with the corpus, a hand-populated cache, a peer copy — reached
  `import_shard` ack-free, which is the bypass the invariant names and
  records as observed. The check now sits at `import_shard`, the
  chokepoint both entry points reach, with a test. The IMPORTER half
  waits for `e`, which rewrites the module it belongs in.
- **g. The admission budget** (08 §8.2) in the same commit that removes
  TOFU — 07 §7.8 already gives the surface.
- **h. `check([--drop])`**: `vector_compliance` and `vector_sweep` as one
  command. `vectors-sample` draws **sixteen** chunks per sampled shard,
  always; the plan sets how many shards and never how many chunks.

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

**Tests**: a conformant bundle round-trips through export and import; a
bundle with foreign vectors is refused at T0; a bundle whose `collection`
differs but whose `space_id` and `content_hash`es match is ADMITTED; a
FIRST-SEEN producer key is admitted (the TOFU regression); one re-issued
record has its rows refused and the import exits 0 while the rest lands;
a uid with no possession entry refuses the bundle whole; a padded bundle
does not move T0's statistic; an uncredited archive is refused; `--strict`
on an empty allow list admits nothing; a corrupted stored vector is found
and dropped by `check --drop`.

**Operator**: two, and they are small. The `refused` field on 09 §9.10's
coverage row — all three documents now promise the refusal is recorded
and no field table has a place for it. And the bundle size: 04 §4.5 says
"a few hundred chunks", §9.10's worked example shows ~15,000, and T3 is
sixteen recomputes per bundle, so the difference is 0.16 % of production
or 5.3 %. Record the chosen number in the commit message.

### Step 5 — `hold check`, `validation.json`, attestations
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

### Step 6 — Proof-of-concept import — ONE collection, end to end
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

### Step 7 — GATE: what may be in a cleaned corpus besides text
*Blocks importing for keeps — step 8 — and nothing before it. See
[`figures-in-the-corpus.md`](figures-in-the-corpus.md).*

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
| `wikipedia` | itself **+ the wikivec pre-embeddings** |
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
*07 §7.6.*

Remove the aliases, `app/cli.py`'s old tree and the `tools/` scripts now
under `louise dev`. Update `README.md`'s quickstart to the new commands.
Delete this file.

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

## 5. Kick-off prompt for Claude Code

> Read CLAUDE.md, then docs/README.md and the nine documents it lists,
> in order — 09 last and in full, since it is the reference the others
> point into. Then read docs/notes/migration-plan.md and
> docs/notes/STATUS.md. We are on the `louise` branch. Implement
> **step N** of the migration plan exactly as scoped there: wrap the
> existing code it names rather than rewriting it (step 0 is the one
> exception and says so), add the new command beside the old one with a
> deprecation alias, and ship the tests the step lists. Every
> pre-existing test must stay green. If the step conflicts with a
> document, stop and ask; if it needs a dependency, ask before adding
> it. When the step is done, update docs/notes/STATUS.md with what is
> built and what it cost — with numbers — and mark the step done in
> this plan.
