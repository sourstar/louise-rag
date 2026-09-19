# 7. Commands

The command line, the API and these documents use one vocabulary. Every
noun below is a word from §1's table or introduced in §2–§6; every verb
is one of a short closed set that means the same thing wherever it
appears. If a command's name does not tell you what you get, the name is
wrong.

## 7.1 Three surfaces

```
louise hold   …     your own copy: complete, current, checked
louise share  …     with other peers
louise ask    …     search
```

Everything under `share` is something under `hold` plus a transport, one
to one. A peer that never connects to anyone has the whole of `hold`
and `ask`.

One more word is not a surface:

```
louise node   …     the daemon: the rounds over every held collection, the web interface, sharing
```

`node` runs the three surfaces on a schedule; it adds no operation of its
own. `hold` is not a service (§7.4), so the thing that runs it
continuously has its own name.

## 7.2 Four verbs, and what each produces

| verb | produces |
|---|---|
| `create` | a description — a Codex, a snapshot. No corpus is read and nothing is embedded from it; `hold collection create` does embed the calibration set, 512 texts spanning the encoder's whole input range, which needs a reachable encoder and takes about a minute (06 §6.6) |
| `build` | an artefact derived from its inputs — records from origin files, vectors from records, an index from either |
| `fetch` | bytes arriving here, from the origin or from peers |
| `share` | bytes leaving here, and the announcement that they exist |

The remaining verbs are small and literal: `check` runs the validation
plan or one check; `show` displays state (the only word for it);
`list`; `import` and `export` move signed vector bundles as files;
`derive`, `continue`, `replace` create the three kinds of related
collection (§2.7); `certify` signs a snapshot as a certifier; `ack`
records the acknowledgement of a licence (§4.4); `forget` stops holding;
`apply` makes the configuration files the running state (§7.8); `trust`
and `anchor` manage keys; `benchmark` and `calibrate` measure an
encoder; `search`, `facets`, `lookup` and `traverse` are the four things `ask`
does — the three tools of §5.4 plus the listing of 05 §5.7 — and
`chat` and `serve` expose them to a model and over MCP; `contribute`
and `attest` are `share`'s two wrappers of `hold vectors build` and
`hold check`; `peers` is the one noun `share` has that `hold` has not;
`start`, `stop`, `pause` and `resume` belong to `node` alone. A new verb
that does not fit one of these rows is probably a flag on an existing
one.

## 7.3 The tree

```
louise hold
  collection  create <plugin> --recipe FILE --encoder URL [--space-from CODEX]
                                                                       write a Codex; print its id
              derive <codex> [--space-from CODEX | --space FILE | --encoder-weights D --engine FILE] [--layers FILE] [--update FILE] [--work FILE] [--archives ID …] --accept
                                                                       a sister: same corpus, new space or new schedule; refuses one that differs in nothing.
                                                                       Prints what would be inherited — vectors, coverage, credits — and waits for --accept;
                                                                       without it, with no terminal, exit 2. To inherit nothing, change the space (02 §2.7)
              continue <codex> --platform-image D --through ID [--origin FILE] [--origin-reachable-until ID]
                                                                       a daughter: new toolchain, same space
              replace <codex> --defect FILE [--platform-image D] [--space FILE | --encoder-weights D --engine FILE]
                                                                       a corrective sister, with its reproducible defect
              ack <collection> [--yes]                                 print the licence terms; record this peer's acknowledgement
              certify <collection> <snapshot>                          sign a snapshot as this collection's certifier
              forget <collection> [--kinds …] [--yes]                  stop holding: remove this peer's copy and its state
              list [--all]                                             collections held, by lineage; --all includes the dormant
              show <collection>                                        Codex, snapshots, holdings, trust, certification, vitality, configuration, what refuses to start
  config      show [<collection>]                                      the effective configuration, each value with its tier (§7.8)
              apply [<collection>] [--set KEY=VALUE …]                 write the effective values into the files; the node applies them
  records     fetch <collection> [--slice … | --profile P] [--snapshot ID]
                                                                       run the acquisition spec to the cutoff, for these slices or the profile's (06 §6.2)
              build <collection> [--snapshot ID]                       parse, slice, shard, hash
              check <collection> [--origin] [--seal] [--sample F]      records against their own hashes and roots, against the origin, against the seal
              show <collection> (<record-id> | --shard <slice>/<n>) [--raw]   with the layer and chunker derived for it (02 §2.3)
                                                                       one record and its chunks, or a whole shard as JSON Lines
  snapshot    create <collection>                                      freeze the current records as this month's snapshot
              delta <collection> <from> <to>                           what changed between two snapshots
              list <collection>
  embedder    check <collection>                                       the calibration gate (§3.3) against the run the endpoint states; a pass validates it and keeps its signed statement in `state/`
              benchmark <collection>                                   tokens per second on the calibration set
              calibrate --encoder-weights D --engine FILE [--against FILE …] [--set SHA256]
                                                                       the tolerance a space can bear, measured between the reference engine and each --against
                                                                       encoder (a file of `weights` and `[engine]`, 09 §9.3's form)
                                                                       over the calibration set: run BEFORE `collection create`, its output being the recipe's `space.tolerance`
  vectors     build <collection> [--shard <slice>/<n> | --delta ID]    embed; a signed bundle about every 15,000 chunks
              import <collection> (<file> | --archive ID [--retry]) [--strict]
                                                                       a signed bundle, or a public pre-embedded archive, through the ladder
              export <collection> [--shard <slice>/<n>] --out DIR      a signed bundle of what this peer produced — a DIRECTORY of four files (09 §9.7), not a container
              check <collection> [--drop]                              are stored vectors still what this encoder produces; --drop removes the ones that are not
              show <collection>                                        coverage, producers, verification
  index       build <collection> [--text | --vector]                   the text index per shard; the centres and labels of the space
              show <collection>
  check       <collection> [--checks …] [--sample F] [--seed HEX]      run the validation plan; print the report
  key         create                                                   this peer's key, at NODE_KEY_PATH
              trust <key> [--deny | --remove | --restore]              the trust list: allow, deny, forget, lift a burn
              anchor <collection>                                      check a certifier's key against its anchor
              show                                                     this peer's public key; the trust list with each key's state

louise share
  <collection> [--slices …] [--kinds …] [--stop]                       serve what you hold — and announce it
  fetch <collection> [--slice …] [--kinds …] [--snapshot ID]           take a part
  contribute [<collection>] [--once]                                   vectors build, under rendezvous, shared
  attest <collection> [--checks …]                                     check, shared
  peers show                                                           who this peer knows, what they hold
  peers add <address>                                                  a peer given by hand

louise ask
  search <query> [--collections …] [--space …] [--limit N] [--rerank-depth N] [--as-of DATE] [--freshness] [--translate [--all-languages]] [--<facet> VALUE | --not-<facet> VALUE …]
                                                                       the fused list of §5.1, with what §5.3 says about each result; --as-of searches the snapshot
                                                                       current at that date (§5.1); a facet flag restricts the legs and never ranks (§5.2, §5.7)
  facets <collection> [<field>] [--under VALUE]                        the fields this collection can be filtered on, or one field's values with this peer's counts (§5.7)
  lookup <id>                                                          a fact by key, from a reference source
  traverse <id> [--relation T] [--depth N]                             the records reached along declared relations (§5.5)
  chat                                                                 the model with the three tools of §5.4
  serve                                                                expose search, lookup and traversal as MCP tools

louise node
  start [--acquire-only]                                               the daemon; runs until stopped
  stop                                                                 finish the current units, commit, exit
  show                                                                 state per collection, the current stage, the last round's cost, overrides in force
  pause [<collection>] [--acquire-only]                                a runtime override: no work, or acquisition only, until resume; it survives a restart (§7.8)
  resume [<collection>]

louise dev                                                             operator instruments; read-only unless the line says otherwise
  schedule-report      what the worker spent its rounds on
  embedder-profile     the concurrency an endpoint sustains
  migrate schema [<collection>]                                        apply pending schema changes to a state database; writes; never runs on its own
  migrate pack <name> --recipe FILE [--platform-image D]               give a pre-Codex pack its Codexes and first snapshot (§7.6); writes.
                                                                       The recipe carries, per space in the pack, what the pack cannot supply — the encoder's weights
                                                                       and engine, tolerance, work, validation, descriptive — and the calibration set is embedded per space
                                                                       as `create` does; the first space named is the root, the others `derived_from` it
  adopt <pack-dir> <collection>                                        the one-time move of a pre-Codex pack's vectors and index into the collection's drag; writes
  refs-sync            reference tables from their origins; writes
```

`--dry-run`, `--json` and `--yes` are not shown per line; §7.7 says which
commands take them and what they do.

Where the artefact a command produces is defined in another document,
that document's rules are the command's, and these are the ones a
reader needs at the command line:

- **`collection create`.** The recipe is a TOML with every Codex field
  the plugin does not default (02 §2.1; 09 §9.3). The plugin defaults
  what it declares — origin, parser, licence class, privacy, modality,
  erasure, the chunker family, `default_layers`, `default_slicing` and
  `default_facets` (06 §6.1–6.3); it never defaults `space` or `work`,
  and `create` refuses (exit 2) a recipe without them unless
  `--space-from` supplies the space — which is refused in its turn when
  the named Codex's `cleaning_version` differs from the plugin's, since
  the two would not then land in one space (below).
  `toolchain.platform_image` is the digest of the platform the command
  runs in unless the recipe states it. A certified collection is a
  recipe whose `publication` is `certified` and whose `certifier` block
  is filled (02 §2.8). Re-running with the same recipe prints the same
  id and exits 0: the hash is the identity, and writing it twice is
  nothing.
- **`derive`, `continue`, `replace`.** The flags are the fields the
  relation may change and nothing else (02 §2.7); `derive` refuses a
  change outside the sister's set. `--space-from` copies the named
  Codex's `space` block and `layers` byte for byte, and `cleaning_version`
  is never among them: a sister keeps her mother's, since changing it
  would re-derive the corpus she is defined to inherit. `derive` instead
  REFUSES, naming both values, when the named Codex's `cleaning_version`
  differs from the mother's — the resulting `space_id` could not then
  equal the source's, and landing in the source's space is the whole of
  what the flag promises. `create --space-from` refuses the same
  mismatch against the plugin's `cleaning_version` (06 §6.3), for the
  same reason and with the same message: the flag exists so that two
  collections land in one space by construction (02 §2.7), and it never
  copies a cleaner version the plugin could not run. `--archives` names
  the pre-embedded archives a sister in a NEW space draws on, expanded
  into the `kind: "archive"` entries of `credits` from the platform's
  registry as `create` does; it is
  refused when `space_id` matches the mother's, and when `--space-from`
  supplied the space, both of which bring their own archive credits
  (02 §2.7). It reaches no other kind: the corpus entry is the mother's
  on every relation, and the community entry follows `embedding`.
  The defect file is the `replaces.defect` object of 03 §3.6.
- **`ack`.** Prints `LICENSE-TERMS` from the copy (04 §4.4; 09 §9.13)
  and, on a terminal, waits for consent; `--yes` records without asking.
  The acknowledgement is written to the collection's state directory
  (§7.8). A yellow or red collection is not fetched, built, embedded,
  indexed, shared or searched until it exists: every such command exits
  2 and names this one.
- **`forget`.** Removes the copy, or with `--kinds` only those members,
  and the local state that goes with them; the Codex, this peer's
  attestations and anything another peer holds are untouched (01,
  promise 7 is about the network; a peer may stop holding). Withdraws
  the announcement first. Exits 2 while the node holds the collection
  (§7.7).
- **`records fetch`.** Runs the acquisition spec (06 §6.2) for the
  named slices — by default the slices of the collection's file (§7.8),
  `all` for everything — drawing from the peer's one budget (04 §4.3).
  A slice fetched once is recorded in the file so the monthly cycle
  continues it.
- **`records build`.** Refuses (exit 2) a record without `origin_hash`
  (02 §2.3), refuses one whose `native_id` sorts before the open shard's
  `first_id` (02 §2.2), and writes a tombstone for a document the origin
  retracted (06 §6.1). It writes `relations/<slice>/NNNNN.jsonl.zst`
  beside each record shard from the plugin's `relations()` (06 §6.1),
  sorted and framed as 09 §9.14 fixes, and nothing at all when the
  plugin does not implement it. `hold snapshot create` lists what is
  there in the snapshot's `relations`, `[]` when there is none. A re-run
  over the same origin files writes the same bytes.
- **`records show`.** A record id is a uid, `pubmed:38112034`. `--shard`
  streams the shard as stored (09 §9.5); `--raw` prints records without
  their chunks.
- **`snapshot create`.** Signs the snapshot's sidecar with this peer's
  key when one exists — the attestation *I computed this too* of 02 §2.2
  (09 §9.4). Re-run for an existing id: identical bytes → nothing to do,
  exit 0; different bytes → refused, exit 2, the differing shard named;
  a snapshot is not created while the fetch for its cutoff is
  incomplete — exit 3.
- **`certify`.** Appends the certifier's signature to the sidecar and
  writes the pointer of 03 §3.7 (09 §9.11) for `share` to publish.
  Refuses (exit 2) when this peer's key is not the Codex's
  `certifier.key`.
- **`embedder check`.** The calibration check of 03 §3.3 against the
  calibration member (09 §9.12), for the run the endpoint states (its
  `Louise-Run`): pass or fail at the Codex's `tolerance`. A pass
  validates that run and keeps its signed statement in
  `state/calibration/` (09 §9.12: "kept in `state/` and published in this
  peer's digest"), one per run. A failure is kept too, so the run is not
  retried against the same set. It stores no vectors: the statement
  already carries the seed, so anyone can reproduce the sample and
  recheck the verdict, and a 512-row baseline beside it would be a second
  answer to a question the statement answers. Of the endpoint's output it
  keeps only the fingerprint, a hash. The node runs the same check by
  itself the first time an answer names a run it has not validated (03
  §3.3). `vectors build` and `share contribute` write a vector only from
  an answer naming a validated run; an endpoint that states no run
  identity is refused for them (exit 2).
- **`vectors build`.** `--delta ID` embeds exactly `delta.added ∪
  delta.changed` of snapshot `ID` — a snapshot id, never a file. About every
  fifteen thousand chunks a bundle (09 §9.7) is signed and merged into the
  shard's vector file (02 §2.4); the bundle is kept for `share`.
- **`vectors import`.** A bundle binds its space and, per record, the
  content hash it embedded; its `collection` and `snapshot_id` are
  provenance and gate nothing (03 §3.5; 09 §9.7). `import` refuses at
  T0 (exit 1 — the bundle is the failing unit) one whose `space_id` is
  not `<collection>`'s. A record whose `content_hash` in
  `possession.json` differs from this copy's has that record's rows
  refused and the refusal recorded — the record was re-issued since —
  and the import exits 0, never a failure. An archive import records the
  archive id as
  producer; `--retry` puts the archive's rejected shards back in line.
  `--strict` refuses a bundle whose producer is not on this peer's
  allow list and never admits below T3. It is a local choice and the
  only place the allow side decides a bundle (03 §3.5); with an empty
  allow list — the newcomer's (08 §8.1) — it admits nothing at all,
  which is why it is off by default.
- **`vectors check`.** Is `hold check --checks vectors-sample` over this
  peer's own vectors; `--drop` turns a failing row into an empty one
  (02 §2.4) so the coverage map stops claiming it.
- **`index build`.** `--text` builds the text index of every held shard
  that lacks one. `--vector` trains the centres when the space has none
  and ships them as a member (02 §2.4); when it has, it labels every
  held vector against the newest version held and never moves a centre.
- **`check`.** Runs the plan (03 §3.2) with the seed given or a fresh
  one. A check's outcome is `pass`, `fail` or `inconclusive` (03 §3.1);
  the first two yield an attestation (09 §9.8), signed when this peer
  has a key and stored in the collection's state directory; without a
  key the report counts on this peer alone. `records check` is `hold
  check --checks roots,records[,records-vs-origin][,origin-seal]` and
  nothing else — `records` (03 §3.2) is the only local check that proves
  what is inside a shard rather than that the file matches the snapshot,
  and leaving it out made `--sample F` on this command affect nothing
  unless `--origin` was given. `--sample F` raises the plan's sample for
  this run and cannot
  lower it; the attestation records the sample actually taken.
- **`key`.** `create` writes a new seed at `NODE_KEY_PATH` in the form
  of 09 §9.2, refuses to overwrite one, and prints the public key; a key
  file readable by anyone but its owner is refused at load, never
  repaired. There is no rotation: a lost key is a new key, and the old
  one's standing does not transfer. The trust list is peer-wide: `trust
  <key>` allows, `--deny` denies, `--remove` deletes the entry, and
  `--restore` lifts a burn (03 §3.5) — the one state a command never
  creates, only a proof of forgery does.
- **`share <collection>`.** Records the shared set in the collection's
  file (§7.8), hashes the members (09 §9.16), announces them under the
  keys of 04 §4.2, and returns; the node serves. With the node not
  running, exit 0 with a notice that nothing is served yet. What it
  publishes is exactly what `hold collection show` lists as held, never
  more; `--stop` withdraws the announcement. It refuses (exit 2) a
  `never-share` collection and announces a `restricted` one to trust-list
  peers only (04 §4.4). Sharing is automatic after a completed fetch for
  a `public` collection unless its file says `share = false`.
- **`share fetch`.** Takes `codex.json`, `snapshots/*` and
  `LICENSE-TERMS` first — the members a peer may hold before an
  acknowledgement — then the members named; pieces are checked against
  roots and shards promoted from staging as 04 §4.3 says. What arrived
  is what `hold collection show` then lists as held.
- **`share contribute`.** Chooses shards by 04 §4.5, on a collection
  whose `roots` and `chunks` pass on the shards this peer holds and for
  whose snapshot a `records-vs-origin` attestation exists; then `hold
  vectors build`, then `share` of the bundles. `--once` takes the shards
  this peer currently leads, builds and shares them, and exits instead
  of looping until stopped.
- **`share attest`.** Runs `hold check` with the named checks and a
  fresh seed and publishes the resulting attestations as events (04
  §4.2). It never republishes an old one.
- **`share peers`.** `add` is one of the two bootstraps, beside
  `platform.toml [peers]` (§7.8); a peer given by hand is a neighbour
  until three digest intervals pass without a digest from it.
- **`ask`.** `search` returns the result of 05 §5.3; `lookup` and
  `traverse` are the MCP tools `louise_lookup` and `louise_traverse` of
  05 §5.4, one to one, and `search` is `louise_search`. `chat` needs
  `CHAT_URL`; `serve` needs nothing but what is held.
- **`node start`.** Each round gives every held collection its turn at
  the stages of 06 §6.6, in that order — fetch, build, snapshot at the
  cutoff, embed (under rendezvous when the node shares), index, check by
  the plan, share — calling the same functions the commands call and
  nothing else. A collection that refuses to start is skipped and says
  why (§7.8). `--acquire-only` fetches and builds and spends nothing on
  an encoder. `stop` ends the daemon at its units (§7.7), exit 0.
  `pause` and `resume` are runtime overrides: they live in the state
  database and survive a restart until `resume` (§7.8).
- **`dev migrate schema`.** Creating the schema of a new state database
  is not a migration and happens on first open. Changing an existing
  schema happens here only; a process that finds a schema other than its
  own refuses to write that collection and says so in `hold collection
  show`.

## 7.4 The API

The Python API is the same tree with dots:

```
app/hold/     codex.py  collection.py  config.py  records.py  snapshot.py  embedder.py  vectors.py  index.py  check.py  key.py
app/share/    serve.py  fetch.py  contribute.py  attest.py  peers.py
app/ask/      search.py  plan.py  tools.py
app/node/     worker.py  web.py  control.py
app/plugins/  base.py  <source>/
```

These are the modules the command tree names, one per noun, and the
listing is that surface rather than a directory listing: a module that
backs no command sits beside them (`hold/drag.py`, the member names and
roots of 09 §9.16, which `records`, `snapshot`, `check`, `serve` and
`fetch` all read) and is free to exist, but it never becomes the place a
command's behaviour lives.

`hold.records.build(collection, snapshot)` is what `louise hold records
build` calls and nothing else; `share.contribute(collection)` calls
`hold.vectors.build` and then `share.serve`; `share <collection>` is
`share.serve(collection, slices, kinds)`, and `share serve <collection>`
is its long form. A test that greps for a command's name finds the
function that implements it and the section of these documents that
describes it. Every function is idempotent and resumable: a write is
delete-then-insert on its key, a stage marks itself done only after its
durable commit, and an interrupted run continues rather than
restarting — §7.7 names the unit at which each stage stops.

The **plugin protocol** (§6.1) is versioned; a plugin declares the
version it implements (`protocol_version`), and that version is part of
the platform image the Codex names by digest. "A plugin conforms to the
standard" is therefore a statement that hashes.

The node also exposes `hold` and `ask` over HTTP for the web interface,
and `share` is HTTP between peers by nature; but `hold` is not a
service. Scripts, tests and a peer maintaining a collection alone do not
start a server to build a snapshot.

Over HTTP every command is one route, named by the tree:

```
/api/<surface>/<noun>/<verb>        POST /api/hold/records/fetch    GET /api/hold/collection/show
                                    POST /api/ask/search            POST /api/node/pause
```

`show` and `list` are `GET`; everything else is `POST`. The body's keys
are the command's flags without their dashes; the response is the
command's `--json` output (§7.7). The exit code is the status: 0 → 200,
1 → 500, 2 → 409, 3 → 503, and the body carries `exit` and the message.
A verb that outlives the request answers 202 and runs under the node;
`node show` lists it. The web interface is a client of these routes and
shows the command line of every action it offers.

**One route is not JSON, and it is the only one.**

```
GET /api/ask/figure?collection=<id>&uid=<uid>&hash=<id>
```

answers with the figure's bytes and its `Content-Type` — never base64,
never wrapped. It is an exception to the rule above and is written out
so that nobody adds a second one by analogy: a search result carries a
plate's HASH (05 §5.3) and a client fetches the picture separately, so
that a result stays small however illustrated the book is. Encoding a
1.8 MB plate into JSON would put 2.4 MB inside a search response, and
several of them inside one answer.

It is an `ask` route because it is read-only and returns nothing secret,
and it obeys the same licence rule as the text beside it: a collection's
class decides whether its figures travel at all (04 §4.4), so a
red-class figure is never served, exactly as its text never is. The
peer answers 404 for a hash it does not hold and for one the record
marks `carried: false` — the result object already told the caller
which of those it was, before the request was made.

Who may call what:

- `hold` and `node` routes require the token read from
  `ADMIN_TOKEN_FILE`, presented as a bearer token. With no token file
  configured they answer 401 — never open.
- `ask` routes are read-only and open on the LAN by default, so a guest
  and an MCP client (§5.4) need no token. They return nothing secret.
- `share` is not an HTTP API of the node: it is the signed peer-to-peer
  messages of 09 §9.9 and the torrents of 09 §9.16. A sender is
  identified by its key and nothing else, and the aggregate limit of
  08 §8.2 bounds what an unknown key can cost.

## 7.5 Rules for anyone adding a command

1. The noun is a word from these documents. If it is not, either the
   concept is missing from the documents or the command is.
2. The verb is from §7.2, with its meaning there.
3. `show` is the only word for state.
4. The help line says what you get, not what happens inside.
5. Nothing lives outside the tree. An instrument is `dev`, listed and
   marked as such.
6. A `share` command names the `hold` command it wraps, and the state it
   publishes is what that command shows.
7. A command that writes takes `--dry-run`; a command that removes takes
   `--yes`; `show`, `list` and `check` take `--json` (§7.7).

## 7.6 Migration from the previous names

The previous command tree was organised by internal artefact. The
command is `louise`; `python -m app.louise` is the same parser. Old
names are accepted for one release as hidden aliases — `louise <old …>`
and `python -m app.cli <old …>` both — each dispatching to the new
function and printing `deprecated: use louise <new …>` on stderr.

| previous | now |
|---|---|
| `corpus add` / `grow` (`--profile P`) | `hold records fetch --slice P` — both fetched; neither created |
| `corpus add --ack-terms FILE` | `hold collection ack` |
| the web interface's *new collection* | `hold collection create --recipe FILE` |
| `corpus status` | `hold collection show` |
| `pack build`, `tools/pack_convert` | `hold records build` |
| `pack verify` | `hold check` |
| `pack cat` / `dump` | `hold records show` / `hold records show --shard` |
| `pack adopt` | `dev adopt` (the one-time move of a pre-Codex pack's vectors and index into the collection's drag) |
| a pack with `manifest.json` and several spaces | `dev migrate pack <name>`: one Codex per space, sisters of one another, and the first snapshot; the old id stays a catalogue alias for one cadence |
| `snapshot record` / `delta` / `list` | `hold snapshot create` / `delta` / `list` |
| `snapshot vectors` | `hold vectors build --delta` |
| `space repin` | `hold collection derive`, then `hold collection forget <mother> --kinds vectors` when the mother's vectors are no longer wanted here. Nothing is re-pinned in place: a shared space is never destroyed under the peers that hold it |
| `backend check` / `rebaseline` | `hold embedder check`. The probe baseline and its cosine threshold give way to validating each run once and checking the run identity on every answer (03 §3.3). The batch-integrity check becomes the gate's `batch_invariant` |
| `tools/calibrate_space`, `build_space_artifact` | `hold embedder calibrate` |
| `bundle keygen` / `trust` / `trust --quarantine` | `hold key create` / `trust` / `trust --deny` |
| `bundle export` / `import` / `status` | `hold vectors export` / `import` / `show` |
| `bundle resweep`, `tools/vector_compliance`, `vector_sweep` | `hold vectors check [--drop]` |
| `genesis import` / `status` | `hold vectors import <collection> --archive ID` / `hold vectors show` |
| `genesis retry` | `hold vectors import <collection> --archive ID --retry` |
| `index vector build` / `status` | `hold index build` / `show` |
| `embed materialize` | `hold config apply` (it writes; `hold config show` displays) |
| `contribute` | `share contribute` |
| `torrent publish`, `torrent seed` | `share <collection>`; serving is the node's |
| `python -m app.worker`, `python -m app.api` | `louise node start` |
| `/api/control`, the Library's pause and embed-mode controls | `node pause` / `resume`, `hold config apply --set embed=…` |
| `/admin/…`, `/api/search`, `/api/verify` | `/api/hold/…`, `/api/ask/search`; T4 needs no route — it runs on use |
| `/v1/chat/completions` | kept as an alias of `/api/ask/chat` for clients that speak that protocol |
| `tools/sched_report`, `profile_embed`, `migrate_manifest`, `sync_refs` | `dev schedule-report`, `dev embedder-profile`, `dev migrate schema`, `dev refs-sync` |

## 7.7 Contracts: exit codes, arguments, output, concurrency, interruption

These hold for every command in the tree. A script, the node's round
loop and the web interface all decide "retry later" or "stop and tell
someone" from them, so a command that breaks one is a bug, however
sensible its own behaviour looks.

**Exit codes.**

| exit | meaning |
|---|---|
| 0 | done, or nothing to do |
| 1 | failed: an error, recorded against the unit it names — a check that failed, a bundle refused as forged, a record that would not build |
| 2 | refused: a rule stood in the way before anything was done — an invariant, a licence, a missing acknowledgement, an unknown collection, no key, a schema not this platform's |
| 3 | deferred: nothing is wrong and nothing was done — an origin, an encoder or a peer was unreachable, a lock was held; retry later |
| 130 | interrupted after a clean commit (SIGTERM or SIGINT) |

A command never exits 1 for a deferral and never 0 for a failure. The
message on stderr names the unit — the shard, the document, the bundle,
the file — and for exit 2 names the rule and the command that satisfies
it (`hold collection ack …`).

**Arguments.** Wherever a command takes `<collection>` it accepts, in
this order:

1. a full collection id, with or without its `sha256:` prefix;
2. a unique prefix of the id of at least 12 hex characters;
3. a local name — the collection's directory under
   `<LOUISE_ROOT>/collections/` (§7.8);
4. a lineage id, which resolves to the leading daughter (04 §4.6) held
   here, with a notice naming the collection id it chose.

Ambiguity is exit 2, naming the candidates. A title or a plugin name
never resolves (08 §8.2). `<codex>` in `derive`, `continue` and
`replace` resolves by the first three only: those commands name one
Codex, and a lineage is not one. `<snapshot>` and `ID` after
`--snapshot` are snapshot ids — `2026-08` on a monthly cadence,
`2026-W36` on a weekly one (09 §9.4). `DATE` after `--as-of` is
`YYYY-MM-DD` and nothing shorter or longer (exit 2, since `2019` and
`2019-03` would each name a different instant to two implementers); it
is read as `00:00:00` of that day in the collection's `origin.clock`,
and the search takes, per collection, the newest snapshot of the chain
whose cutoff is at or before it (05 §5.1) — a date after every cutoff
therefore names the chain's newest snapshot, and one before every cutoff
names none, which the response reports for that collection rather than
answering from a later one. `<record-id>` is a uid,
`<plugin>:<native_id>` (02 §2.3). `<key>` is `ed25519:` and 64 hex
characters (09 §9.2). `--shard` takes `<slice>/<n>` —
`year=1975-1979/00003` — because shard numbers restart per slice.
`--slice` takes slice values as they appear in the drag's paths.
`<pack-dir>` is a filesystem path to a pre-Codex pack (§7.6); it is the
one argument in the tree that is a path and not a name, which is why it
says so. A
facet flag is named for its field — `--subjects`, `--creators`,
`--not-subjects` — and is accepted only for a field the collection's
Codex declares (02 §2.1); any other is exit 2, listing the fields that
are. A `tree` facet matches a value and everything under it, so
`--subjects Neoplasms` takes `Neoplasms/genetics` with it; a `range`
facet takes `FROM..TO`, either end omitted for open.
`--profile` takes a profile name the collection's acquisition spec
declares (06 §6.2) and expands to that profile's slices; it may not be
combined with `--slice`. `--space` and `--work` take a TOML holding
that Codex block in the form 09 §9.3 fixes; `--encoder-weights` and
`--engine` are shorthand for a `--space` file changing only
`space.encoder` — the weights digest, and the engine as a TOML file in
09 §9.3's form — and any other field a new encoder needs — `dim`,
`ctx`, `pooling`, `normalise`, `query`, `tolerance` — is given in
`--space`, the calibration and envelope being computed from the named
encoder as `create` computes them.
`--kinds` takes the kind names of 02 §2.6; the default is records and
vectors. `--checks` takes the check names of 03 §3.2. `--seed` takes 32
bytes as hex (03 §3.2).

**Output.** Stdout is for the result, stderr for progress and log
lines; a pipe never sees a progress bar. `--json` on every `show`,
`list` and `check` prints, as one JSON object, the artefacts shown in the
form 09 defines — the Codex (09 §9.3), snapshots and their sidecars (09
§9.4), the coverage map (09 §9.10), attestations (09 §9.8), the search
result of 05 §5.3 — under keys named by the noun, plus the effective
configuration with each value's tier for `hold collection show` and
`hold config show`. A test that compares that output with the files on
disk finds them equal.

`--dry-run` prints the plan — the files or members, the bytes to move,
the records or chunks to process, the licence state — and exits 0
without writing a byte or moving one. It is accepted by `records fetch`,
`records build`, `vectors build`, `index build`, `vectors import`,
`share fetch`, `derive`, `continue`, `replace` and `forget`. `--yes` is
required by `forget`, `vectors check --drop` and `key trust --deny |
--remove` when stdin is not a terminal, and skips the confirmation when
it is; `ack --yes` records without printing the terms.

**One writer per collection.** A collection has one write lock,
`state/lock` in its directory (§7.8). The node holds it for every
collection it runs, for as long as it runs it. A `hold` command that
writes takes the lock or exits 3 naming the holder; `show`, `list` and
`check` never take it, and neither does `ask` — `embedder check` does,
because it writes a statement. Four commands exit 2, not 3, while
the node holds the collection, because retrying them later is not the
answer: `collection forget`, `vectors check --drop`, `dev migrate schema`
and `dev adopt`. `node pause <collection>` releases the lock at the end
of the current unit, and those four then run. A reader never blocks a
writer or is blocked by one: the state database runs in write-ahead
mode, and a writer that reads first begins immediately, never deferred.
Two nodes on one machine are two roots (§7.8) and share nothing.

**Interruption.** On SIGTERM or SIGINT a command finishes its current
unit, commits and exits 130, within the time one unit takes; a second
signal does not shorten that. The unit per stage:

| stage | unit |
|---|---|
| `records fetch`, `share fetch` | the current file, at byte granularity — a checkpoint mid-file, the byte offset committed |
| `records build` | the document; a shard cut short is re-cut on resume |
| `snapshot create` | atomic: the file is written last and is there entire or not at all |
| `vectors build` | the batch: the chunks in flight to the encoder, each written only from an answer naming a validated run (03 §3.3) |
| `vectors import`, `share contribute` | the bundle, marked applied in the same transaction as its last row |
| `index build` | the shard |
| `check`, `share attest` | the sampled shard; an attestation is written only for a completed check |
| `forget` | the member; a member is gone entire or still there |

A re-run after 130 continues from the last committed unit and writes the
same bytes it would have written uninterrupted. The node stops every
collection at its unit and exits 130 on a signal, 0 on `node stop`.

## 7.8 Configuration and the state directory

Three tiers, sorted by how often they change. Every value has exactly
one home.

| tier | carries | changes by |
|---|---|---|
| **environment** — identity | `LOUISE_ROOT` (the one directory), `NODE_KEY_PATH`, `ADMIN_TOKEN_FILE`, `CHAT_URL`, `TRANSLATE_URL`, `RERANK_URL`, `GPU_PROBE_URL` | restart |
| **files** — desired state | `platform.toml` and `collections/<name>.toml` under `LOUISE_ROOT` | editing the file; `hold config apply` |
| **state database** — runtime overrides | `node pause` and `--acquire-only`, in force until `resume` | the commands; a restart keeps them |

Desired state is never written into the database, and a runtime override
never survives the node applying the files — at start, at `hold config
apply`, and when a file changes. The one exception is the node's mode —
`pause` and `--acquire-only` — which no file sets, so there is nothing to
apply over it: it survives a restart until `resume`. An expiry would turn
a node the operator holds at acquire-only back into a running one, encoder
and all, on every restart (operator decision, 2026-09-13). What `hold config show` prints comes
from the same resolution the node uses, each value labelled with its
tier; a value shown anywhere else comes from that resolution too.

`LOUISE_ROOT` has no default; a node without it refuses to start.
`NODE_KEY_PATH` unset means this peer cannot sign — no bundles, no
attestations, no `share` of its own work; import, check and search work
without it. `ADMIN_TOKEN_FILE` unset means the `hold` and `node` routes
answer 401 (§7.4). There is no default encoder: a collection names
its own in its file's `[encoder]`, and a file that names none is a
non-compliant collection — refused, not served, shared or searched, the
reason named by `hold collection show` — and everything that would need
its encoder (`vectors build`, T3, T4) defers (exit 3). `TRANSLATE_URL`
unset means the chat model translates (05 §5.2); `CHAT_URL`
unset refuses `ask chat` (exit 2); `RERANK_URL` unset returns
`reranked: false` (05 §5.6) for every collection whose file names no
`[reranker]` of its own; `GPU_PROBE_URL` unset leaves the node's GPU
panel empty and says why, and nothing else changes. Secrets are named
by path only — the key, the token — never by value in an environment
variable, a file, a log line or a message.

**`platform.toml`** — the node:

```toml
[node]       http_port = 8742   peer_port = 6881   name = "nas"
[budget]     down_max = "400Mbit"   up_max = "40Mbit"   schedule = "01:00-07:00 full; else 30%"
[peers]      addresses = ["peer.example.org:6881"]      # the bootstrap of 04 §4.2, beside `share peers add`
[dht]        enabled = true
[admission]  bytes_per_hour = "64MB"   objects_per_hour = 1000   queue = "256MB"
             # 08 §8.2: what this peer accepts per hour from keys whose work it has not seen
             # reproduced. Local, never negotiated; 0 accepts nothing from strangers
[ask]        latency_budget_ms = 8000                   # the reranker's bound (05 §5.6)
```

**`collections/<name>.toml`** — one held collection; the file existing
is the collection being held:

```toml
codex  = "sha256:…"                            # the collection_id this name holds; drag/codex.json's inner hash must equal it (09 §9.3)
title  = "PubMed"                              # optional: overrides descriptive.title for this peer's own listing; never published
slices = ["year=1975-1979", "year=1980-1984"]  # or "all"
kinds  = ["records", "vectors"]
embed  = "on-demand"                           # off | on-demand | all: what the node spends, never what search uses
share  = true

[encoder]                                      # required: a collection names its own encoder, there is no default
url = "http://nas:30440"                       # the endpoint that serves the Codex's encoder, on the node's model
                                               # container RAGE, which states the run with every answer (03 §3.3)
query_url = "http://nas:30441"                 # a bi-encoder's query side; absent when symmetric

[reranker]                                     # optional (05 §5.6): this node's reranker for this collection
url = "http://nas:30447/v1/rerank"             # the full endpoint
api = "jina"                                   # jina: {query, documents} (llama.cpp's /v1/rerank) · tei: {query, texts}
```

The encoder endpoint is the collection's `[encoder]` and nothing else: a
node has no default encoder, and a file that names none is refused
(operator, 2026-09-18). `hold collection create` writes the endpoint it
was given (`--encoder`) into the file it creates. The reranker resolves
the collection's `[reranker]`, then `RERANK_URL`; with neither, the list
keeps its fused order and says so (05 §5.6). `embed
= on-demand` computes vectors lazily, for the chunks a search needs; a
vector computed that way is bundled and shared like any other. Search
runs over whatever vectors exist regardless of the mode.

`hold collection create`, `share fetch` and `dev migrate pack` write the
file; `hold collection forget` removes it. The name is `--name` when
given, otherwise the plugin's name, or the plugin's name and the first
12 hex characters of the id when that is taken. A file's `codex` is
never edited: a name holds one collection for its life, and another
collection is another name.

**The state directory.** Everything under one root:

```
<LOUISE_ROOT>/
├── platform.toml
├── collections/
│   ├── pubmed.toml
│   └── pubmed/
│       ├── drag/            the copy: exactly the tree of 02 §2.6 — what `share` serves, and nothing else
│       ├── state/           manifest.sqlite, cursor, ack.json, attestations/, bundles/, lock — this peer's alone
│       ├── index/           the text index of the shards held, and the facet values (05 §5.7)
│       ├── lance/           the vector table search reads
│       └── origin/          the origin files the records are parsed from — 06 §6.2's corpus directory, kept for records-vs-origin; the bytes may live on other storage, mounted here
├── state/                   node.sqlite: the catalogue, coverage maps (09 §9.10), peers seen, runtime overrides; trust.json
└── keys/                    the peer key, at NODE_KEY_PATH
```

`drag/` holds only what the Codex or a snapshot names, so a member is
hashed as it lies and `share` refuses a drag directory holding a file
that neither the Codex names, nor a snapshot of the chain lists — as a
record shard, a relations shard, a figures container, or in its `files`
(09 §9.4) — nor 09 §9.6 defines as a local derived file under
`vectors/`.

The list is spelled out because it is a refusal and a refusal that
omits a member kind does not omit a file, it refuses THE WHOLE DRAG.
Until the figures amendment this sentence said "as a shard or in its
`files`", which named neither the relations shards nor the figures
containers — both of which 09 §9.4 lists in their own top-level arrays
rather than in `shards` or `files`. A collection with relations was
unshareable and nothing said why.

`index/`, `lance/`, `origin/`, the configuration files and the keys never travel,
and neither does the state database, which holds the cursor of 06 §6.1,
the acknowledgement of 09 §9.13 and the attestations of 09 §9.8. Two
things it keeps DO travel, by their own routes rather than as drag
members: a bundle, as the member `bundle-<space>-<bundle id>` served
from `state/bundles/` (09 §9.7, §9.16), and an attestation, as an event
(09 §9.9). Lance holds the vectors a search reads and is a local
rebuild of what the bundles carry, never a member. The trust list is
peer-wide, one file
for every collection. Two nodes on one machine are two roots, two ports
and two keys.

**Pickup.** A running node re-reads `collections/` between rounds: a new
file joins the next round, an edit — slices, kinds, embed, share,
encoder — applies at the next round, and the change is logged with the
collection's name. An edited `[encoder]` re-runs `embedder check`
before the next embed. `hold config apply` is the same reload on demand
and, with `--set`, the way a script or the web interface edits one value
without opening the file; `--set` writes the file, then applies it.

**Refuse-to-start is per collection.** A collection whose
`drag/codex.json` has an inner hash other than its file's `codex`
(09 §9.3), whose vector table's space or width
is not its Codex's (02 §2.4), or whose state database carries a schema
other than this platform's, is not served, not shared and not searched;
the node keeps running its other collections, `node show` lists it, and
`hold collection show` prints the reason. A missing acknowledgement is
not this: the collection is held and withheld from every use until
`hold collection ack` runs (04 §4.4).
