# Serving only Codex collections, and what the audit found underneath

**The operator's rule, 2026-09-11:** Louise shows the converted
collections and nothing else. An unconverted one is kept aside and
appears again only once it has been fully migrated.

Before moving a single file, a five-surface audit ran over the change
(control manifest, worker lifecycle, UI/API, resurrection paths, sharing
and licensing), each finding adversarially verified against the code.
33 findings survived verification, 2 were refuted. The headline: **the
obvious version of this change silently stops the entire node.**

---

## 1. Why a `mv` is not the whole change

`load_all()` globs `collections/*.toml`, so moving a file out does remove
the collection from the Library, from search federation, from the worker
and from discovery. `by_name`'s docstring even says that is the intended
mechanism. But four things do not follow the file.

### The node stops, with no log line — BLOCKING, fixed

`worker_state` is a node-level key stored in the control table of
whichever collection sorts FIRST (`db.NODE_CONTROL_KEYS`, and
`default_collection()` is `load_all()[0]`). On this node that was
`genome-dm6`. Retiring the unconverted collections makes `gutenberg` the
control collection, and its Codex manifest — created by the step-8
migration — has never held node state. `db.worker_state()` defaults to
**`"paused"`**, so every collection stops and nothing says why.

`adopt_node_control` exists for exactly this and its docstring records
the last time it happened ("five collections, no log line, nothing
obviously wrong"). It could not fire: the worker passes
`all_collections[1:]` as the donor list, and when every other collection
leaves at once that list is empty. `_sync_collections` closes those
connections in the same round the new owner asks for them.

Fixed three ways, because one was not enough:

* the worker reads the node keys out of a departing collection's control
  table **before** closing it (`_orphaned_node_control`), and
  `adopt_node_control` takes that mapping as a `fallback` donor. Covers
  the hot order — files moved while the node runs.
* the cold order (stop, move, start) has no donor at all, by
  construction. It is covered by an operator step: copy the node's
  CURRENT `worker_state` into the incoming control collection before the
  move. Not `"running"` — the node may be in `acquire-only` or
  `seed-only`, and hardcoding would silently change what it does.
* whichever order, a control collection with no `worker_state` now logs a
  WARNING naming the default and the way out. The default is correct;
  being silent about it is what cost the time.

### Retired collections kept seeding to peers — BLOCKING, fixed

`Seeder` had no removal path at all: `_handles` was only ever added to.
`tick()` used the live collection list solely to decide what to START, so
every torrent already seeding kept announcing on the DHT and uploading
for the life of the process. Two things that look like stops are not:
`set_paused` is one node-wide flag that resumes everything when the
worker state allows it, and deleting the `.torrent` only stops handles
that have not started yet.

For a yellow-class source that is invariant 9 — "not discovered, not
imported, NOT EXCHANGED" — continuing to serve bytes after the
acknowledgement that permitted them left with its file. `Seeder.retain()`
now removes the torrent from the session for any collection the node no
longer holds, and `tick` calls it before starting anything.

### The create flow writes a legacy TOML by construction — fixed on the read side

`new_collection_create` calls `_write_collection_toml(..., codex=None)` —
hardcoded — and there is no path in the repo that CREATES a Codex-shaped
file; `codex` is only ever carried through a rewrite. Retiring the six
files also frees their corpora, which `_corpus_mounts()` then offers as
unused datasets. So arrangement alone is reversible by the product's own
buttons.

The policy therefore lives on the READ side, where nothing routes around
it: `platform.toml`'s `[node] codex_only = true` makes `load_all()` set
aside any file that does not name a Codex, with the reason recorded in
`collections.refusals()`. A legacy TOML created afterwards is still not
served.

### Process-global state does not follow the file — fixed / operational

`_stats_publisher` kept a write-capable connection per collection with no
eviction, holding `-wal`/`-shm` open on manifests the node had stopped
touching and is about to delete; it now prunes against the live set,
before the empty-list guard. `fts._indexes` and `embeddings._dbs` have no
eviction either and live in BOTH processes, so the move is completed by
recreating **both containers**, not just the worker.

The Network page listed collections straight out of the worker's stats
blob, which is keyed on name and never pruned — a retired collection kept
a row, named, at a frozen rate. Now filtered against `load_all()`.

---

## 2. A refusal is a decision; a parse error is a mistake

The first cut of this made `load_all()` swallow every exception per file.
It broke five deliberate tests, and they were right: *"a typo must not
silently become the default"*. An operator who wrote
`embed_mode = "always"` must be told, not quietly served a node with one
collection missing — that is the same silence this whole change is meant
to remove.

So a malformed file still raises. Only the policy refusal is set aside,
and it is recorded rather than hidden.

---

## 3. Found by the audit, NOT fixed here, and why

These are pre-existing and independent: this change neither creates nor
worsens them, and each is its own piece of work. Recorded so they are
not rediscovered by accident.

| severity | what | where |
|---|---|---|
| serious | **Invariant 9 has no gate on search.** `_license_allows` guards scan and archive; `query_service` federates over `load_all()` unconditionally. A yellow collection with no ack is searched. | app/worker.py:1353 |
| serious | **Invariant 9 has no gate on the outbound seeding path.** Neither `seeding.tick` nor `cmd_torrent_publish` consults `app/licensing.py`. `retain()` closes the retirement leak, not this. | app/acquire/seeding.py |
| serious | **`load_all()` has no per-collection isolation.** `_codex_collection`'s docstring promises "the caller keeps its other collections"; no caller implements it, and `spaces.validate_boot` is all-or-nothing too. Invariant 1 asks for a per-collection refusal. Deliberately left alone here: making it real means deciding which failures are per-collection and which are node-fatal, and doing that as a side effect of a retirement would have broken the typo rule above. | app/collections.py:414, app/spaces.py:280 |
| serious | **Six admin/API endpoints silently retarget** when the control collection changes, two destructive (`POST /api/reset`, `POST /api/scope` act on whatever `db.connect()` resolves to). | app/api.py:52 |
| serious | **Removal never reaches the acquisition or archive-fetch threads**; they stop only on SIGTERM. wikivec's local-archive pass observes neither. | app/worker.py:1059, app/bundles/genesis.py:487 |
| minor | **`louise collection list` reads a different directory** — `DEFAULT_ROOT` treats `$LOUISE_ROOT` as the collections dir; `collections.louise_root()` treats it as the parent. They disagree by one level in every configuration. | app/louise.py:51 |
| minor | Query history and telemetry are orphaned in the old control manifest with no forwarding. | app/web.py:279 |
| minor | The scheduler and sizer keep reporting retired collections' costs, frozen, until restart. | app/worker.py:2183 |
| minor | A retired collection's bundle inbox stops draining with no log line and no page to see it on. | app/bundles/importer.py:187 |

**The one that should be taken next** is node state living in "whichever
collection sorts first" at all. 07 §7.8 already says where it belongs —
`<LOUISE_ROOT>/state/node.sqlite`, which the document lists as holding
"the catalogue, coverage maps, peers seen, runtime overrides". The
current placement is described in the code itself as "an accident of sort
order", and it is the root of the blocking failure above rather than a
neighbour of it.

**Taken 2026-09-13.** `worker_state` and the bandwidth override live in
`state/node.sqlite` now (app/node_state.py), and every reader and writer —
the API, the Library and System pages, the main loop, the stats beat, the
seeder, the index maintainer — opens that one file. `adopt_node_control`,
the removal-time rescue and the cold-order step (§4, step 1) are gone:
there is no control collection left to hand the state to. The worker
moves the old keys out of the manifests once, at boot, and deletes them.
The account is in STATUS.md.

---

## 4. The move, in order

Each step is safe with the PREVIOUS step's code running, which is why the
order is what it is.

1. **Copy the node control state into the incoming control collection**
   (`gutenberg`), reading the current values rather than assuming them.
   This is what makes the cold order safe, and it is harmless under the
   old code.
2. Build the image with the changes above.
3. Move the six unconverted TOMLs to `$CONFIG_DIR/collections-legacy/`.
   **Under CONFIG_DIR, never DATA_DIR** — `/config/collections` holds the
   files, `/data/collections` holds gutenberg's drag, and a glob move
   under the wrong one would take the drag with it. `collections/` must
   still EXIST and still hold `gutenberg.toml`: if that directory goes
   away, `load_all()` synthesizes the legacy default pointing at
   `DATA_DIR/manifest.db` — the 35 G legacy tree queued for deletion.
4. Set `[node] codex_only = true` in `platform.toml`.
5. Recreate **both** containers.
6. Verify: gutenberg is the only collection, the node is NOT paused, the
   refusals name the six, and nothing is still seeding them.

Nothing in the legacy data is touched by any of this. The six
collections' manifests, indexes and vectors stay exactly where they are;
`docs/notes/legacy-to-delete.md` still governs when any of it may go.
