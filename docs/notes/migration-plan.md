# Migration plan — from the current code to the documented architecture

*Working document for the `louise` branch. The specification is
`docs/README.md` and the seven documents it lists; this file says in what
order the code gets there, what each step reuses, and how each step is
known to be done. Update it as steps land; delete it when the last one
does.*

## 0. Ground rules for every step

- **One step, one pull request, tests included.** A step is done when
  its tests pass *and every pre-existing test still passes*. The 4 MB of
  tests under `tests/` is the safety net for the whole migration; nothing
  is deleted from it, tests are moved and renamed as the code they cover
  moves.
- **Old names stay as aliases** until §7.6's migration is complete: a
  new command lands beside the old one, the old one prints a deprecation
  line naming the new one, and both call the same function.
- **Nothing is re-downloaded and nothing is re-embedded.** The corpora on
  disk (Gutenberg, PubMed baseline and update files, Wikipedia dumps),
  the plugins that parse them, and the vectors already computed or
  imported (MedCPT, the BGE-M3 Wikipedia archive, this node's own) are
  the starting point. The migration gives them a Codex and admits them
  through the same gates a stranger's copy would pass.
- **The invariants in CLAUDE.md are unchanged**: idempotent writes,
  deterministic chunking, the GPU boundary (HTTP only), secrets by path,
  licence gates before use, `origin_hash` on every record.
- New dependencies, schema changes and wire-format changes: ask first.
  Step 7 has one.

## 1. The branch, and what is already on it

```
git checkout -b louise
# the docs restructure, CLAUDE.md, README.md, and the first increment:
git add docs CLAUDE.md README.md app/hold app/share app/louise.py tests/hold tests/share tests/run_nopytest.py
git add -A app tests tools   # the 29 files whose doc references moved to docs/notes/
git commit -m "Louise: documentation restructured; hold.codex, hold.snapshot, hold.collection, share.rendezvous with tests"
git push -u origin louise
```

Then, on a machine with the real environment:

```
pip install -r requirements.txt
pytest tests/hold tests/share            # 47 tests; run_nopytest.py is only for sandboxes without pytest
pytest                                    # the whole existing suite must be green too
```

Delete `tests/run_nopytest.py` once pytest runs.

## 2. Steps, in order

Each step names the documents it implements, the existing code it wraps,
the tests that prove it, and what the operator has to do by hand.

### Step 1 — `hold records build` and `hold snapshot create`
*§2.2, §2.3, §6.6. Wraps `app/pack` and `tools/pack_convert.py`.*

- `app/hold/records.py`: `build(collection, snapshot_id)` runs the
  existing pack builder over a corpus directory with the collection's
  plugin, and returns `hold.snapshot.Shard` objects (root, ids, counts,
  chunk totals per shard) instead of writing a manifest. Every record's
  `origin_hash` is required — the existing check stays.
- `app/hold/snapshot.py`: `create(collection, shards, previous)` writes
  `snapshots/<id>.json` as canonical bytes; `delta` uses `compute_delta`
  over the record-uid → `content_hash` maps of two snapshots.
- CLI: `hold records build`, `hold snapshot create|delta|list`; aliases
  `pack build`, `snapshot record|delta|list`.
- **Tests**: the golden Gutenberg mini fixture builds to a pinned
  snapshot hash; building twice gives identical bytes; a record without
  `origin_hash` is refused; `verify_chain` over two consecutive builds.
- **Operator**: nothing.

### Step 2 — `hold embedder check|benchmark`
*§3.3. Wraps `backend check/rebaseline` and `tools/profile_embed.py`.*

- `app/hold/embedder.py`: `check(collection)` runs the calibration gate
  against the Codex's `space.calibration` block (today's backend gate,
  with the reference vectors read from the Codex instead of a per-node
  baseline); `benchmark(collection)` measures tokens/s on the same
  chunks; both return a signed statement (`app/bundles/keys.py` already
  has the node key and Ed25519).
- `--accept` replaces `rebaseline`: it records a passing run as this
  node's baseline for drift detection, not as the collection's reference.
- **Tests**: with the fixture endpoint mock, `check` refuses a mismatched
  encoder and accepts the reference; `benchmark` is stable within 10 %
  across runs on the mock; the statement verifies with the node's public
  key.
- **Operator**: nothing.

### Step 3 — `hold vectors build|import|export|check`
*§2.4, §3.5. Wraps the embed stage, `app/bundles`, `genesis import`,
`tools/vector_compliance.py`, `tools/vector_sweep.py`.*

- `build(collection, shard)`: the existing embed stage over one shard,
  emitting a signed bundle every few hundred chunks (the bundle format
  is already per-chunk; this is a cadence change). Labels against the
  current centres when the index exists.
- `import(bundle | --archive)`: one path for a peer's bundle and a
  public pre-embedded archive, through the existing T0–T2 ladder plus
  T3 spot recompute; the archive case reuses `genesis` with `producer =
  <archive id>`.
- `check([--drop])`: `vector_compliance` and `vector_sweep` as one
  command — are stored vectors still what this encoder produces; drop
  the ones that are not.
- **Tests**: the existing `tests/bundles`, `tests/verify` and genesis
  tests pass under the new entry points; a bundle with foreign vectors is
  refused at T0; a corrupted stored vector is found and dropped by
  `check --drop`.
- **Operator**: nothing.

### Step 4 — `hold check` and `validation.json`
*§3.2. Wraps `app/verify` and `pack verify`.*

- `app/hold/check.py`: reads the collection's plan, runs the named checks
  with the named sample sizes, prints the report, and emits one
  attestation per completed check (not yet shared). Existing checks:
  `roots`, `records-vs-origin` (today's `records`), `chunks`, `license`,
  `signature`. New in this step: `calibration` (from step 2) and
  `labels`. `anchor` and `origin-seal` come in step 7.
- **Tests**: `tests/pack/test_*verify*` moved; a plan with an unknown
  check name is refused; the attestation for a failing deterministic
  check names the shard.
- **Operator**: nothing.

### Step 5 — Migrate the collections this node holds
*§2.1, §2.7. New: `louise dev migrate codex`. This is the step that
turns what exists into the new structure without redoing any work.*

For each collection in `collections/*.toml` with a manifest on disk:

1. **Write the Codex.** From the TOML `[embed]` block, the plugin's
   declared parser and chunker versions, the licence class and terms
   hash, and the slicing the pack already uses. Two values cannot be
   inferred and are given on the command line: `--platform-image` and
   `--encoder-image` — the digests of the images actually running
   (`docker inspect --format '{{index .RepoDigests 0}}' <image>`). The
   encoder digest is the one the approved-encoder list names for this
   device class; if the node is running a different build, stop: that is
   the drift the migration exists to make impossible.
2. **Build the records** with step 1 from the corpus directory already on
   disk. No fetch.
3. **Admit the vectors** with step 3's `import`, producer `legacy-local`
   for this node's own and the archive id for MedCPT / BGE-M3. Vectors
   that fail the ladder are not migrated; the report says how many and
   where. (Gutenberg's known corrupt fraction is expected to fall out
   here.)
4. **Alias the old id** in the local catalogue for one cadence, so URLs
   and TOML references keep resolving.

- **Tests**: a fixture collection with an old-style manifest migrates to
  a Codex whose `space_id` is stable across two runs; record counts match
  the old manifest; the admitted-vector count is reported; running the
  migration twice is a no-op.
- **Operator**: runs it on the NAS, per collection, with the two digests.
  This is the first step whose acceptance is on real data, not fixtures.
  Record the outcome in `docs/notes/STATUS.md`.

### Step 6 — `share`: serve, fetch, contribute, attest
*§4. Wraps `app/acquire/transport_torrent.py`, `torrent_publish.py`,
`app/transfer`, `app/discovery.py`, `app/contribute.py`. Makes the
coordinator optional.*

- `share <collection>`: publish + seed as one motion; announce under
  `H(collection_id ‖ snapshot_id)` in the DHT (libtorrent's DHT is
  already linked); digests and events over the node API as today.
- `share fetch`: choose members, verify pieces, stage, promote.
- `share contribute`: `rendezvous.mine` over the coverage map's
  uncovered shards → `hold.vectors.build` → `share`. The coordinator's
  `/work` is consulted only if configured, and its answer is advisory.
- `share attest`: `hold.check` → publish the attestations as events.
- Coverage map: per-document bitmap per space, assembled from bundles
  and attestations, exchanged in digests.
- **Tests**: `tests/integration` gets a two-peer test on localhost: peer
  B fetches from A, contributes a shard, A imports it; then the
  **single-survivor test** — B is stopped, A produces the next snapshot,
  embeds it, and a fresh peer C finds A by computing the DHT key. The
  rendezvous tests from step 0 already cover the partitioning.
- **Operator**: two nodes on the LAN for the first real run.

### Step 7 — Certified collections and the Gazzetta plugin
*§2.8, §3.7, §3.8, §6.4. New code; one dependency decision.*

- `hold key anchor`: fetch the well-known URL, compare the fingerprint,
  record rotation and revocation.
- Manifest-only gate at promotion for `publication: certified`; the
  space-halt on a violation found later; the exclusion notice in search.
- `seals/`: capture validation evidence at snapshot time; `origin-seal`
  in live and archival mode. **Dependency**: validating a CAdES
  envelope and a PAdES seal against the EU trusted lists needs a
  signature-validation library (candidates: `pyhanko` for PAdES, CMS
  handling via `asn1crypto`/`cryptography` for CAdES; the Commission's
  DSS is Java). Decide before writing a line.
- The Gazzetta Ufficiale plugin: acquisition of the certified `.pdf.p7m`
  issues, the sealed PDF as the origin form with the reason recorded,
  parsing pinned by the platform image.
- **Tests**: a foreign shard is refused at promotion; a fixture `.p7m`
  with a self-signed chain validates in archival mode against captured
  evidence and fails live against an empty trusted list; the third
  attribution row of §3.8 flags the certifier, not the sharer.
- **Operator**: the certifier key and anchor for the first certified
  collection — this node's own key and a well-known URL on a domain you
  control, marked `is_origin: false`.

### Step 8 — `ask serve` and relations
*§5.4, §5.5.*

- MCP server exposing three tools: semantic search, exact lookup,
  traversal. Results carry snapshot, trust level, certification.
- The `relations` member for the legal collection; the traversal tool
  follows it and reports unresolved cross-collection ids.
- **Tests**: tool schemas validate; a search result carries the fields
  of §5.3; an unresolved reference names the shard it needs.

### Step 9 — Retire the old names
*§7.6.*

Remove the aliases, `app/cli.py`'s old tree and `tools/` scripts now
under `louise dev`. Update `README.md`'s quickstart to the new commands.
Delete this file.

## 3. Dependencies between steps

```
1 records/snapshot ──┬── 4 check ──┐
2 embedder ──────────┼── 3 vectors ┼── 5 migrate ── 6 share ── 9 retire
                     │             │
                     └─────────────┴── 7 certified ── 8 ask
```

Steps 1 and 2 are independent; 3 needs 2; 4 needs 1 and 2; 5 needs 1, 3
and 4; 6 needs 5 to have something real to share; 7 and 8 need 4 and can
proceed in parallel with 6.

## 4. Kick-off prompt for Claude Code

> Read CLAUDE.md, then docs/README.md and the seven documents it lists,
> in order. Then read docs/notes/migration-plan.md and docs/notes/STATUS.md.
> We are on the `louise` branch. Implement **step N** of the migration
> plan exactly as scoped there: wrap the existing code it names rather
> than rewriting it, add the new command beside the old one with a
> deprecation alias, and ship the tests the step lists. Every
> pre-existing test must stay green. If the step conflicts with a
> document, stop and ask; if it needs a dependency, ask before adding
> it. When the step is done, update docs/notes/STATUS.md with what is
> built and what it cost, and mark the step done in this plan.
