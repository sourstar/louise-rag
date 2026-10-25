# 7. Commands

*(vocabulary amended 2026-10-08: SHARD, parts)*

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
`start`, `stop`, `pause` and `resume` belong to `node` alone; `add` and
`erase` put a deposit, or a URL source, into a deposit collection and
take it out leaving no trace (02 §2.9, 06 §6.7). A new verb
that does not fit one of these rows is probably a flag on an existing
one.

## 7.3 The tree

```
louise hold
  collection  create <plugin> --recipe FILE --encoder URL [--query-encoder URL] [--api A] [--embed-model M] [--query-model M]
              [--space-from CODEX] [--calibration-set FILE]
                                                                       the model flags: required where a URL names no model (§7.8, amended 2026-10-09)
                                                                       write a Codex; print its id. A pair space (09 §9.3) names its calibration set's file,
                                                                       the pairs `tools/build_pair_calibration_set.py` drew from the collection's records
                                                                       (09 §9.12); `--space-from` a pair space reads it from that space's member
                                                                       (amended 2026-10-08)
              create deposit --name N [--encoder URL [--api A] [--embed-model M] [--query-model M]] [--space-from CODEX | --space FILE]
                                                                       a deposit collection (02 §2.9): no recipe; the node's deposit encoder unless one is named
              create annotations --claims FILE --name N [--annotates C [--from-snapshot S] [--annotates C2 --from-snapshot S2 …]] [--title T] [--summary S] [--annotator A] [--snapshot ID] [--container jsonl|parquet] [--dry-run]
                                                                       an annotations collection (02 §2.10; amended 2026-10-09): no recipe, no encoder; each claim's
                                                                       target hashes read from C's snapshot S; writes the parts, the relations, the first snapshot
                                                                       (signed with the node's key when there is one) and the file; prints its id. `--dry-run`
                                                                       (2026-10-19) checks the file against S exactly as the write would and prints the counts.
                                                                       Without --annotates the targets are the held collections the claims' uids name, each
                                                                       from its newest snapshot; a target without --from-snapshot, its newest (2026-10-25, §7.4)
                                                                       (claims, target records named, by type and method), writing nothing. Several claims
                                                                       files for one collection are merged first with `tools/annotations_merge.py` (the
                                                                       wikidata claims whole, a matcher claim only where its books are not one work yet)
              derive <codex> [--space-from CODEX | --space FILE | --encoder-weights D --engine FILE] [--layers FILE] [--update FILE] [--work FILE] [--archives ID …] --accept
                                                                       a sister: same corpus, new space or new schedule; refuses one that differs in nothing.
                                                                       Prints what would be inherited — vectors, coverage, credits — and waits for --accept;
              derive … [--encoder URL [--query-encoder URL] [--api A] [--embed-model M] [--query-model M]] [--calibration-set FILE] [--name N]
                                                                       a sister in a new space embeds her own reference at --encoder (her calibration, once,
                                                                       signed with the node's key) and her file names it; in a space a held collection is in,
                                                                       she inherits its member and its file's [encoder] (amended 2026-10-09)
                                                                       without it, with no terminal, exit 2. To inherit nothing, change the space (02 §2.7)
              continue <codex> --platform-image D --through ID [--origin FILE] [--origin-reachable-until ID]
                                                                       a daughter: new toolchain, same space
              replace <codex> --defect FILE [--platform-image D] [--space FILE | --encoder-weights D --engine FILE]
                                                                       a corrective sister, with its reproducible defect
              ack <collection> [--yes]                                 print the licence terms; record this peer's acknowledgement
              ack <collection> --origin-unchecked [--yes]              print what passed and what could not run; record the decision to use a copy whose origin could not be checked (03 §3.6)
              ack --web-sources [--yes]                                print the web-sources disclaimer; record this node's acceptance, once (04 §4.4)
              certify <collection> <snapshot>                          sign a snapshot as this collection's certifier
              forget <collection> [--kinds …] [--yes] [--dry-run] [--here]  stop holding: remove this peer's copy and its state; --here when no worker runs
              rename <collection> <new-name> [--dry-run]               the same collection under another local name; nothing recomputed
              list [--all]                                             collections held, by lineage; --all includes the dormant
              show <collection>                                        Codex, snapshots, holdings, trust, certification, vitality, configuration, and the copy's state — refused, quarantine or in use — with every reason
  annotations update <collection> [--claims FILE] [--against TARGET=SNAPSHOT …] [--snapshot ID] [--dry-run]
                                                                       (amended 2026-10-23, operator: updated, not replaced; 02 §2.10) the next snapshot of the SAME
                                                                       annotations collection: the claims checked against each target's newest snapshot (or
                                                                       --against), a new FILE the whole set from now on (kept claims keep their uids, left out ones
                                                                       tombstoned), no FILE a re-check of the claims held; signed with the node's key; prints
                                                                       what changed. The Library's annotations card: *re-check claims*, *update with a claims file*
  deposit     add <collection> FILE [--key K] [--here]                 store a file as a deposit; an existing key is replaced whole
              erase <collection> <key> [--yes] [--here]                remove a deposit, leaving no trace (02 §2.9)
              list <collection>                                        the collection's deposit keys
              show <collection> <key>                                  a deposit's text and its chunk count
  source      add <collection> <url> [--interval MIN]                  subscribe a deposit collection to a URL; fetched now, then every interval (06 §6.7)
              erase <collection> <url> [--yes]                         end the subscription and erase its deposit
              list <collection>                                        the subscriptions, each with its last fetch
  config      show [<collection>]                                      the effective configuration, each value with its tier (§7.8)
              apply [<collection>] [--set KEY=VALUE …]                 write the effective values into the files; the node applies them
  records     fetch <collection> [--slice … | --profile P] [--snapshot ID]
                                                                       run the acquisition spec to the cutoff, for these slices or the profile's (06 §6.2)
              build <collection> [--snapshot ID]                       parse, slice, cut into parts, hash
              check <collection> [--origin] [--seal [--live DIR]] [--sample F]
                                                                       records against their own hashes and roots, against the origin, against the seal
              seal <collection> <snapshot> --files DIR --trusted-list FILE --lotl FILE --lotl-signers FILE
                   [--format cades|pades|xades] [--revocation FILE …] [--fetch-revocation] [--url URL]
                                                                       the certifier captures the seals' evidence (03 §3.7; 09 §9.15) into seals/<snapshot>.json
                                                                       before `snapshot create` signs it; the LOTL verified by a key the Official Journal
                                                                       announced (`--lotl-signers`), the national list by a key the LOTL's pointer names,
                                                                       both kept by their hash for archival mode; revocation responses embedded, given or
                                                                       fetched now on the budget (added 2026-10-07: 07 named the member and no command to
                                                                       write it; the lists' signatures, XAdES and revocation the same day)
              show <collection> (<record-id> | --shard <slice>/<n>) [--raw]   with the layer and chunker derived for it (02 §2.3)
                                                                       one record and its chunks, or a whole part as JSON Lines
  snapshot    create <collection>                                      freeze the current records as this month's snapshot
              create <to> --shards-from <from>[:<snapshot>] [--snapshot ID]
                                                                       seed the parts from another collection's byte-identical ones — refused unless every field
                                                                       that decides a record (02 §2.7), record_line, facets and the layers' chunkers agree — each
                                                                       linked (a copy across devices) and checked against the source snapshot's root, the staging
                                                                       claim and build database written as a build would leave them; then the ordinary create, of
                                                                       the source snapshot's period unless --snapshot names one (added 2026-10-09)
              delta <collection> <from> <to>                           what changed between two snapshots
              list <collection>
  embedder    check <collection>                                       the calibration gate (§3.3) against the run the endpoint states; a pass validates it and keeps its signed statement in `state/`
  embedder    tree-diff <before> <after>                               what differs between two backend trees, path by path: digests read from VEGA_URL or JSON files (vega.md §12); exit 1 when they differ
              benchmark <collection>                                   tokens per second on the calibration set
              calibrate --encoder-weights D --engine FILE --against FILE … --ctx N [--set SHA256 | --pairs FILE]
                                                                       the tolerance a space can bear, measured between the reference engine and each --against
                                                                       encoder (a file of `weights` and `[engine]`, 09 §9.3's form)
                                                                       over the calibration set: run BEFORE `collection create`, its output being the recipe's `space.tolerance`
                                                                       (JSON: per-encoder cosines, and `tolerance` — the worst cosine minus three standard
                                                                       deviations, rounded down to six places). Each file also names its endpoint as an
                                                                       `[encoder]` block does (§7.8): top-level `url`, with `api` and `model` (the body's model, one side: an `[encoder]`'s `embed_model`) where needed;
                                                                       `--engine`'s may omit `weights`. `--ctx` is the space's, the texts cut as the gate cuts
                                                                       them; `--set` defaults to the platform's text set. Exit 3 when an endpoint is unreachable
                                                                       (amended 2026-10-08: the endpoint and `--ctx` were unstated). `--pairs` is a pair space's
                                                                       set, its file, every lane embedding the pairs as pairs (09 §9.12, amended 2026-10-08)
              rebaseline <collection> [--yes]                          a Codex below version 3 only: accept a changed backend by re-recording the probe baseline (`app/verify/backend_gate.py`); exit 2 for version 3, whose runs `check` validates
              run --encoder URL [--api A] [--model M] [--query] [--pair]   the run the endpoint states now — run id, engine, weights, driver — as JSON, for writing a
                                                                       recipe's `space.encoder`; exit 3 when unreachable. `create` (and `derive`) hold the runs that
                                                                       embedded a new reference at an `api = "vega"` endpoint to the recipe's engine and weights,
                                                                       and refuse a mismatch before anything is written (added 2026-10-09)
  vectors     build <collection> [--shard <slice>/<n> | --delta ID]    embed; a signed bundle about every 15,000 chunks
              import <collection> (<file> | --archive ID [--retry]) [--strict]
                                                                       a signed bundle, or a public pre-embedded archive, through the ladder
              export <collection> [--shard <slice>/<n>] --out DIR      a signed bundle of what this peer produced — a DIRECTORY of four files (09 §9.7), not a container
              check <collection> [--drop]                              are stored vectors still what this encoder produces; --drop removes the ones that are not
              show <collection>                                        coverage, producers, verification
  index       build <collection> [--text | --vector]                   the text index per part; the centres and labels of the space
              show <collection>
  check       <collection> [--checks …] [--sample F] [--seed HEX]      run the validation plan; print the report
              [--live DIR]                                             `origin-seal` in live mode: the sealed files under DIR re-validated now (03 §3.7)
              [--shards S,…] [--origin DIR] [--against FILE]           --shards: these parts alone, how a received claim is reproduced (03 §3.6);
                                                                       --against: image-equivalence's reference lines (03 §3.4) (both 2026-10-07)
  key         create                                                   this peer's key, at NODE_KEY_PATH
              trust <key> [--deny | --remove | --restore]              the trust list: allow, deny, forget, lift a burn
              anchor <collection>                                      check a certifier's key against its anchor
              show                                                     this peer's public key; the trust list with each key's state

louise share
  <collection> [--slices …] [--kinds …] [--stop]                       serve what you hold — and announce it
  fetch <collection> [--slice …] [--kinds …] [--snapshot ID]           take a portion
        [--from ADDRESS] [--dht] [--encoder URL] [--name N]            … from a peer given by hand, or found
        [--api A] [--embed-model M] [--query-model M]                  … the rest of the file's [encoder] (§7.8, amended 2026-10-09)
  contribute [<collection>] [--once] [--interval S]                    vectors build, under rendezvous, shared
  attest <collection> [--checks …]                                     check, shared
  peers show                                                           who this peer knows, what they hold
  peers add <address> [--key <key>]                                    a peer given by hand; --key pins its key
  offer                                                                the collections neighbours announce that this node does not hold, and who serves each (04 §4.2); `fetch` takes one (added 2026-10-20)

louise ask
  search <query> [--collections …] [--space …] [--limit N] [--rerank-depth N] [--as-of DATE] [--freshness] [--translate [--all-languages]] [--filter FACET=VALUE | --filter FACET=!VALUE …]
                                                                       the fused list of §5.1, with what §5.3 says about each result; --as-of searches the snapshot
                                                                       current at that date (§5.1); --filter restricts the legs and never ranks (§5.2, §5.7; amended
                                                                       2026-10-07 from per-facet flags: one flag, the facet named in its value, since the facets are
                                                                       the collection's and not known before it is)
  facets <collection> [<field>] [--under VALUE]                        the fields this collection can be filtered on, or one field's values with this peer's counts (§5.7)
  lookup <id>                                                          a fact by key, from a reference source
  traverse <id> [--relation T] [--depth N] [--resolve-in C …]           the records reached along declared relations (§5.5); --resolve-in names the collections an
                                                                       edge into another collection is resolved against, nothing else is (added 2026-10-07)
  chat <question> [--collections …] [--json]                           the model with the three tools of §5.4 and nothing else; needs CHAT_URL (§7.8)
  serve                                                                expose search, lookup and traversal as MCP tools

louise node
  start [--acquire-only | --seed-only]                                 the daemon; runs until stopped
  stop                                                                 finish the current units, commit, exit
  show                                                                 state per collection, the current stage, the last round's cost, overrides in force
  pause [<collection>] [--acquire-only | --seed-only]                  a runtime override: the node's mode — nothing, seeding only, or download and seeding only — until resume; it survives a restart (§7.8)
  resume [<collection>]
  health [<collection>] [--errors]                                     every stage's state per collection; exit 1 when a stage has work it cannot start (added 2026-10-08)

louise dev                                                             operator instruments; read-only unless the line says otherwise
  schedule-report      what the worker spent its rounds on
  embedder-profile     the concurrency an endpoint sustains
  migrate schema [<collection>]                                        apply pending schema changes to a state database; writes; never runs on its own
  refs-sync            reference tables from their origins; writes
  genesis recheck <collection>                                         reopen final archive shards that still have documents the archive could serve; writes
  genesis recount <collection> [--yes]                                 an archive ledger's docs and chunks recounted from chunk_verification; writes only with --yes (added 2026-10-08)
  drop-rows <collection> --ids FILE [--yes]                            02 §2.4's remedy: the rows FILE lists (a chunk id a line) dropped and their documents marked to embed
                                                                       again — what `vectors check --drop`, which leaves an empty row, does not; the collection's lock held; writes (added 2026-10-19)
  carry <from> <to> [--copy] [--keep producer:<archive>] [--rebuilt|--reconcile|--resign|--recut] [--yes]  amendment A1 (02 §2.4): the manifest (after a TRUNCATE checkpoint), index/ and lance/ moved
                                                                       — or copied, reflinked where it can — into a collection with the same records whose space
                                                                       differs only by engine or input form, both write locks held; refused unless the agreement of
                                                                       `snapshot create --shards-from` holds, the space's function (weights, dim, dtype, ctx,
                                                                       pooling, normalise, query, epoch) is equal, the target's latest snapshot names the source's
                                                                       parts, and the target is empty; re-pins the space row to the target's Codex and clears the
                                                                       source's coverage rows — but in the SAME space keeps them under their own part ids, as
                                                                       provenance (never the target's coverage), hard-links the bundle tree with --copy and names
                                                                       the predecessors in the carry marker (04 §4.5, 2026-10-22); --predecessor-rows does that
                                                                       after a same-space carry made before. --keep: then only the rows that archive produced (chunk_verification
                                                                       imported or verified) stay; every other is deleted and its document embedded again, a window
                                                                       at a time, resumable, the table's row count checked against the kept count (exit 1 if it
                                                                       differs). Before use, `hold check <to> --checks vectors-sample`. A dry run without --yes;
                                                                       writes (added 2026-10-09). --rebuilt (A1c, 2026-10-11): only the parser and its image may
                                                                       differ; no snapshot is compared and the target builds its own records; --reconcile, after
                                                                       that build and snapshot, keeps the vectors of every document whose content_hash did not move,
                                                                       resets the rest to embed again and sets each txt_hash to its record's origin_hash. --rebuilt
                                                                       also admits an origin that only ADDS files beside those read (A1d, 2026-10-18); --resign, where
                                                                       the source holds no records: the target's own discovery once, every moved document taking its
                                                                       new locator and signature with txt_hash unset and every mark and vector kept, then a seeded
                                                                       sample re-cut by the target's parser must hold exactly the chunk texts the vector table holds
                                                                       (exit 1 otherwise). --recut (A1e, 2026-10-18): the target's space differs only by cutting
                                                                       by tokens; then every row over the source's ctx x 4 characters that no archive made is
                                                                       dropped and its document embedded again, in one read of the table; a dense-script row
                                                                       refuses it, nothing written — dropped too where every input was embedded alone (a pair layer)
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
  2 and names this one. `--origin-unchecked` is the other thing an
  operator may acknowledge (03 §3.6): that this peer's own
  `records-vs-origin` could not run — the origin unreachable, or this
  platform image neither the Codex's nor equivalent (03 §3.4) — while
  every other test of the copy passed. It prints what passed, what
  could not run and when it was last tried, waits for consent as above,
  and records the decision with its time and the peer's key beside the
  licence acknowledgement; the copy is then in use like any other, with
  no mark. It is refused (exit 2) while another test is still open or
  has failed, and when the origin check has in fact run: a decision
  stands in for a test that cannot run, never for one that failed.
- **`rename`.** The same collection under another local name (*added
  2026-10-13*): a name is this peer's own (§7.8) — no Codex, record,
  chunk or vector holds it — so nothing is recomputed. The directory, its
  folders under the shared roots (an origin folder another held
  collection also links stays where it is), the absolute paths the
  manifest stores, a per-collection pause and the held file move, the
  held file last; the new name must be free. It takes the collection's
  lock, so a running node pauses the collection first (§7.7); an
  interrupted rename is finished by running it again.
- **`forget`.** Removes the copy, or with `--kinds` only those members,
  and the local state that goes with them; the Codex, this peer's
  attestations and anything another peer holds are untouched (01,
  promise 7 is about the network; a peer may stop holding). On a
  running node it runs in the worker, the one writer of a collection's
  indexes (§7.4's deposit writes go the same way), which retires the
  collection's threads first; `--here` runs it in this process when no
  worker runs; `--dry-run` names what would go. A never-share
  collection's deposits are erased first (02 §2.9). `--kinds` takes
  `vectors` today. Nothing is announced yet, so nothing is withdrawn.
  `--here` exits 2 while the node holds the collection (§7.7), as the
  other three do.
- **`records fetch`.** Runs the acquisition spec (06 §6.2) for the
  named slices — by default the slices of the collection's file (§7.8),
  `all` for everything — drawing from the peer's one budget (04 §4.3).
  A slice fetched once is recorded in the file so the monthly cycle
  continues it. *As built (2026-10-08, step 12):* the old `corpus
  add|grow`, wrapped — `--profile P` (default: the file's `profile`) and
  `--dry-run`; `--slice` and recording what was fetched in the file are
  not built yet. A missing acknowledgement is exit 2, naming `hold
  collection ack`.
- **`records build`.** Refuses (exit 2) a record without `origin_hash`
  (02 §2.3), refuses one whose `native_id` sorts before the open part's
  `first_id` (02 §2.2), and writes a tombstone for a document the origin
  retracted (06 §6.1). It writes `relations/<slice>/NNNNN.jsonl.zst`
  beside each record part from the plugin's `relations()` (06 §6.1),
  sorted and framed as 09 §9.14 fixes, and nothing at all when the
  plugin does not implement it. `hold snapshot create` lists what is
  there in the snapshot's `relations`, `[]` when there is none. A re-run
  over the same origin files writes the same bytes.
- **`records show`.** A record id is a uid, `pubmed:38112034`. `--shard`
  streams the part as stored (09 §9.5); `--raw` prints records without
  their chunks.
- **`snapshot create`.** Signs the snapshot's sidecar with this peer's
  key when one exists — the attestation *I computed this too* of 02 §2.2
  (09 §9.4). Re-run for an existing id: identical bytes → nothing to do,
  exit 0; different bytes → refused, exit 2, the differing part named;
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
  part's vector file (02 §2.4); the bundle is kept for `share`.
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
- **`vectors reembed`.** Every vector of a collection made again by the
  encoder that serves it now — for vectors whose run does not give back
  its own bytes (invariant 12). It marks every embedded document; the
  embed stage's version walk replaces each document's vectors, keeping
  none of its own, and in a versioned collection carries a text from a
  document already re-embedded (06 §6.3, amended 2026-10-21). The old
  vectors serve search until replaced. No lock: the marks are written in
  windows of rows, one transaction each, beside the running node.
- **`vectors validity-columns`.** The repair for a versioned collection
  (05 §5.1's validity) whose vector table was written without its three
  columns: the table is rebuilt in place with the columns read from the
  manifest's version table, every vector kept, nothing re-embedded; the
  vector index is not carried, so `index build --vector` follows. Until
  it runs, the embed stage defers every document of that collection and
  no search as of a date is answered from vectors (the leg is withheld
  and says so). Takes the collection's lock: the node must not be running
  it (`node pause <collection>` releases the lock).
- **`vectors show`.** Prints the coverage map (09 §9.10): per part of
  the current snapshot the chunks accepted here, whether it is open, and
  — among the live peers whose digests carry a benchmark for the space,
  this peer included — its leader and the end of the leader's grace
  window (04 §4.5); the producers with the signed work of each in the
  trailing work period; the collection's vitality (04 §4.7). An
  inspection: it reads the manifest and the attestations held, and no
  acknowledgement gates it. Rows are written by `vectors import` (a
  bundle accepted, `fetched`), by `vectors export` (this peer's own,
  `local`, accepted once its own ladder has run over it) and by the
  claims in neighbours' digests, which count for nothing (02 §2.5).
- **`index build`.** `--text` builds the text index of every held part
  that lacks one. `--vector` trains the centres when the space has none
  and ships them as a member (02 §2.4); when it has, it labels every
  held vector against the newest version held and never moves a centre.
  *As built (2026-10-08, step 12):* `--vector` builds the local vector
  table's book_id and vector indexes (what `index vector build` did); the
  centres member is not built yet. `--text`, and no flag, exit 2: the
  node's fts stage builds the text index as documents land, and nothing
  rebuilds it on demand. `index vector prune` has no successor: the node
  prunes superseded Lance versions hourly.
- **`records show`, `snapshot delta`.** `records show` reads the current
  snapshot. `snapshot delta` composes the deltas the snapshots after
  `<from>` up to `<to>` already carry (09 §9.4) and prints them as one
  `delta` object; a period between the two not held, or a broken
  `previous` link, is exit 2.
- **`config show`, `config apply`.** `show` prints, per collection, the
  embed mode, the encoder, the reranker and the share settings, each
  labelled `environment`, `files`, `state database` — or `codex` for what
  the space fixes. `apply` replaces each runtime embed-mode override by
  the file's value, as the node does at start, and names (exit 1) a file
  the node refuses. `--set` is not built yet (2026-10-08).
- **`check`.** Runs the plan (03 §3.2) with the seed given or a fresh
  one. A check's outcome is `pass`, `fail` or `inconclusive` (03 §3.1);
  the first two yield an attestation (09 §9.8), signed when this peer
  has a key and stored in the collection's state directory; without a
  key the report counts on this peer alone. `records check` is `hold
  check --checks roots,records[,records-vs-origin][,origin-seal]` and
  nothing else — `records` (03 §3.2) is the only local check that proves
  what is inside a part rather than that the file matches the snapshot,
  and leaving it out made `--sample F` on this command affect nothing
  unless `--origin` was given. `--sample F` raises the plan's sample for
  this run and cannot
  lower it; the attestation records the sample actually taken.
  `--origin-mib N` raises what `records-vs-origin` may read of the origin
  for this run — 256 MiB by default — and cannot lower it either (03 §3.2).
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
  more, and nothing of a copy that is refused or in quarantine (03
  §3.6): what this peer has not tested it does not offer. `--stop`
  withdraws the announcement. It refuses (exit 2) a
  `never-share` collection and announces a `restricted` one to trust-list
  peers only (04 §4.4). Sharing is automatic after a completed fetch for
  a `public` collection unless its file says `share = false`.
- **`share fetch`.** Takes `codex.json`, `snapshots/*` and
  `LICENSE-TERMS` first — the members a peer may hold before an
  acknowledgement — then the members named; pieces are checked against
  roots and parts promoted from staging as 04 §4.3 says. What arrived
  is what `hold collection show` then lists as held. `<collection>` is a
  collection id or a lineage id (§7.6); who is asked, in order: `--from
  ADDRESS`, a peer given by hand; the neighbours whose digest lists it;
  the `[peers]` list, blind; and with `--dht` the DHT under the
  collection and lineage keys of the current snapshot id and the eleven
  before it (04 §4.2). The Codex comes with its chain and the snapshot
  with its chain, each link checked by hash (09 §9.4, §9.9); a peer
  whose bytes hash otherwise than its digest advertised is a liar and
  banned for a day (04 §4.2), and the next peer is asked. A snapshot
  with no verified signature waits in `state/staging/` and is not
  promoted (09 §9.4). `--encoder URL` goes into the collection's file
  as `hold collection create`'s does; without it the file names no
  encoder, and the copy is held and shared but not embedded or searched
  until the operator adds one (§7.8). `--name` is the local name. No
  peer known is exit 2; every peer down is exit 3. `--slice` and
  `--kinds` are written into the collection's file (§7.8), joined with
  what it already says — no `--slice` means every slice, `"all"` — as is
  `share = true` for a public collection, since sharing is automatic after
  a completed fetch (04 §4.3; until 2026-10-19 the file said `share =
  false`, and a fetched copy's records and bundles were offered to nobody
  until `share <collection>` was run by hand) — and
  the node's worker fetches what the SHARD lacks of them at its next
  beat, member by member: `LICENSE-TERMS` first and, for a yellow or red
  collection, nothing else until the acknowledgement is recorded
  (04 §4.4); each file by the torrent whose infohash the snapshot gives,
  into `state/staging/`, its pieces root computed and compared to the
  snapshot's, then one rename into the SHARD and the holdings mark
  (04 §4.3). `--here` fetches the members in this process instead, for
  a node whose worker is not running; it takes the collection's lock
  (§7.7). The worker fetches without being asked, too: a `snapshot`
  event (09 §9.9) announcing a snapshot newer than the one held here is
  answered by pulling it, with its chain, from the announcer — a peer
  whose digest gave its address — and the members follow at the next
  beat. The attestations the sharer holds for the collection come with
  it, by `pull attestations` (04 §4.2), and the worker pulls them again
  once an interval from every neighbour whose digest lists the
  collection, from the newest one held here less one interval: an
  attestation travels as an event forwarded once and kept seven days, so
  a peer that joined after a check never received it and could not
  contribute (2026-10-19). *(Flags added 2026-10-06 with migration step
  9d; the worker's pull 2026-10-07; the attestation pull 2026-10-19.)*
- **`share contribute`.** Chooses parts by 04 §4.5, on a collection
  whose `roots` and `chunks` pass on the parts this peer holds and for
  whose snapshot a `records-vs-origin` attestation exists; then `hold
  vectors build`, then `share` of the bundles. While either embeds it
  holds an ENCODER LEASE in `state/node.sqlite` (renewed per batch,
  expiring on its own; *amended 2026-10-19*), so the worker's sweep of
  idle embedders leaves the model alone — a GPU-bound backend reads idle
  by CPU while it embeds. `--once` first bundles
  the vectors this peer already holds for every open part it does not
  lead (04 §4.5, amended 2026-10-19: no GPU beyond the challenge rows;
  one read of each such part's records), then takes the parts this
  peer currently leads, builds and shares them, and exits instead
  of looping until stopped. The worker runs the same pass without the
  build as its PUBLISHER for every shared collection it runs (§7.8,
  *amended 2026-10-19*); the command is for a node whose worker does not
  run the collection, since both need its writer lock. A bundle is announced with its event only
  where this peer SERVES the collection — `share = true` in its file and
  a mode that seeds; otherwise it is accepted here, listed under the
  digest's coverage, and offered as a member once this peer serves
  (04 §4.5, amended 2026-10-19). Exactly: the open parts of the current
  snapshot are read off the coverage map (09 §9.10), the live peers off
  the digests (a benchmark for the space, this peer's own from its
  statements); the top of the ordering leads, its window running from
  the later of the part's first opening here and its last accepted
  bundle, the next in line taking over when it runs out with no bundle,
  its own window starting there, and the top again when every window
  has run out. Each part led is built, exported into
  `state/bundles/<space>/<slice>/<n>/<id>/`, passed through this peer's
  own ladder (T0–T2; T3 is for another key's encoder), accepted in the
  map as `local`, and announced with a `bundle` event; one refused by its
  own ladder is kept and not announced. Without `--once` the pass repeats
  every `--interval` seconds (default 900) until a signal, which finishes
  the batch in flight and exits 130 (§7.7). It takes the collection's
  lock (§7.7), so on a node whose worker runs it the collection is paused
  first. A restricted or never-share collection is skipped: its bundles
  would leave for nobody. The receiving side is the worker's: a `bundle`
  event for a collection held here, acknowledged and in use, has its
  four files fetched by the torrents the event's file list gives
  (09 §9.9), each checked against its root, the bundle judged by this
  peer's ladder with T3 skipped (no encoder is asked in that thread; the
  chunks land *unverified here*, 03 §3.5, for `vectors check` to
  confirm), its vectors merged, its coverage row accepted as `fetched`,
  and the bundle kept under `state/bundles/` where the seeder serves it
  — every completed fetch makes the fetcher a sharer (04 §4.3). One the
  ladder refuses is set aside and said. Duplicates are checks (04
  §4.5): a second compliant bundle over chunks this peer already holds
  vectors for is compared against them — cosine per chunk against the
  space's tolerance — and the comparison is published as a
  `vectors-sample` attestation naming both producers; the first accepted
  vector stays what is stored and searched, the rest of the second
  bundle lands. A `flag` event has the attestations it names pulled
  from the announcer and kept, where the trust level reads them: a
  stranger's failure is a claim until this peer reproduces it or a
  second key says the same (03 §3.6); a `codex` event's chain enters
  the catalogue (`share peers show`). *(2026-10-06/07, migration step
  9e.)*
- **`share attest`.** Runs `hold check` with the named checks and a
  fresh seed and publishes the resulting attestations as events (04
  §4.2). It never republishes an old one: the attestation ids published
  are recorded in the node's state database, and the events wait there
  for the worker's beat to forward to every neighbour. The receiving
  side is the worker's too: an `attestation` event that arrives for a
  collection held here, whose payload verifies as an attestation of it
  (09 §9.8), is kept at the attestation's path beside the catalogue,
  where the copy's state, the trust level and vitality read it (03
  §3.6; 04 §4.7); one for a collection not held here is not kept. Takes
  `hold check`'s flags (`--checks`, `--seed`, `--sample`, `--snapshot`,
  `--origin`). `--republish` runs no check: this key's held attestations
  that no event of this peer ever carried are published now, once each —
  for a check that ran before the peer had neighbours, whose attestations
  a newcomer would otherwise have to pull (above). *(2026-10-06, migration
  step 9e; `--republish` 2026-10-19.)*
- **`share peers`.** `add` is one of the two bootstraps, beside
  `platform.toml [peers]` (§7.8) — it writes that list, each address
  once, and with `--key` pins the address to a key (04 §4.2: a digest
  from that address under another key is discarded and logged). A peer
  given by hand receives this peer's digests from the worker's next beat
  and is a neighbour from the moment its own digest arrives: a neighbour
  is a key at an address (09 §9.9), and an address alone has no key to
  list. *(Amended 2026-10-06; it read "a neighbour until three digest
  intervals pass without a digest from it", which named a neighbour with
  no key.)* `show` lists the peer list and every peer heard from — whether
  it is a neighbour now, its ban, and what its last digest says it holds;
  and the catalogue — every Codex announced by a `codex` event, held
  here or not, by id and lineage with its title for display (08 §8.2:
  a name decides nothing).
- **`ask`.** `search` returns the result of 05 §5.3; `lookup` and
  `traverse` are the MCP tools `louise_lookup` and `louise_traverse` of
  05 §5.4, one to one, and `search` is `louise_search`. `chat` needs
  `CHAT_URL`; `serve` needs nothing but what is held.
- **`node start`.** Each round gives every held collection its turn at
  the stages of 06 §6.6, in that order — fetch, build, snapshot at the
  cutoff, embed (under rendezvous when the node shares), index, check by
  the plan, share — calling the same functions the commands call and
  nothing else. A collection that refuses to start is skipped and says
  why (§7.8). The node is in one of four **modes**, and a mode is a
  statement of what it does (operator, 2026-09-19):

  | mode | fetches | seeds | parses | indexes |
  |---|---|---|---|---|
  | *paused* (`pause`) | — | — | — | — |
  | *seeding only* (`--seed-only`) | — | yes | — | — |
  | *download and seeding only* (`--acquire-only`) | yes | yes | — | — |
  | *running* (`resume`) | yes | yes | yes | yes |

  Parsing is reading what was fetched: the scan of the origin into the
  state database, `records build`. Indexing is the text index, the vector
  index, and taking in vectors others computed. In *download and seeding
  only* what lands is scanned when the node runs again; until 2026-09-19
  that mode also built, which is parsing under a name that says download.
  A scan already running when the mode changes ends at its next unit
  (§7.7). **Embedding is not a mode's business**: it happens only where
  everything runs, and there each collection's own `embed` setting
  (§7.8: `off`, `on-demand`, `all`) decides it. No mode changes that
  setting and none is changed by it. `stop` ends the daemon at its units
  (§7.7), exit 0.
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
backs no command sits beside them (`hold/shard.py`, the member names and
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
`node show` lists it. So does a verb that SIGNS, asked of the web
process, which holds only the public key (§7.8; operator, 2026-09-29):
`hold collection create` and the Library's bundle export are queued in
`state/node.sqlite` and run by the worker, and `GET /api/jobs/<id>`
follows one. A create waits while the node's mode does not embed. The web interface is a client of these routes and
shows the command line of every action it offers.

*Amended 2026-10-19.* `hold collection create annotations` (§7.3; 02
§2.10) is `POST /api/hold/collection/create-annotations`, whose input
that is not a flag is the claims file: the flags are query parameters
(`name`, `annotates`, `from_snapshot`, `title`, `summary`, `annotator`,
`snapshot`), the file is the raw request body (`application/x-ndjson`,
as the bundle import takes its tar — no multipart, which the platform's
dependencies do not carry), and `dry_run` is `--dry-run` — the claims checked against the
target snapshot and counted, nothing written, answered at once (200).
Without it the same check runs first (a refusal is the 409 above and
queues nothing), then the create is queued for the worker (202) like
`create` — it signs the first snapshot — but it embeds nothing, so it
runs in every mode. The Library's *new collection* offers the type.

*Amended 2026-10-20 (operator).* The offer (04 §4.2) is `GET
/api/share/offer/show` — `share offer` — and taking a collection from it
is `POST /api/share/fetch` — `share fetch <collection>` with the flags
as the body's keys (`collection_id`, `name`, `slices` as `"all"` or a
list, `kinds`, `encoder`, `api`, `embed_model`, `query_model`,
`snapshot`). A collection nobody announced, a local name already held,
or encoder keys that do not read are refused at once (409) and queue
nothing; otherwise the fetch is queued for the worker (202), which signs
the pulls, runs in every mode, and `GET /api/jobs/<id>` follows it. The
Library's *Offered by peers* is a client of both.

*Amended 2026-10-25 (CLAUDE.md RULE 2: "A regular user chooses what only
they can know — a local name — and nothing else").* Every Library dialog
asks a local name, plus what only the user has — the source (plugin) of a
new collection, the claims file of an annotations collection — and SENDS
nothing else; the routes keep every key above for the command line and
scripts, and work when only those are given. The node determines the
rest:

- **The encoder** (`encoder`, `api`, `embed_model`, `query_model`, and
  `query_encoder` on create), for a take and a create alike, by WEIGHTS:
  the server is the node's model server (the address its collection
  files share, else `platform.toml`'s `[deposit] encoder`); the models
  are a held collection's in the same space (the same document and query
  weights), else the models on that server whose STATED RUN — a VeGa
  receipt or `Louise-Run`, its description verified against the run id
  (03 §3.3, 09 §9.12) — loads exactly the Codex's `space.encoder.weights`
  (and `space.query.encoder.weights` for the query side). A model counts
  only if its weights digest matches; its name orders which models are
  asked first and never decides, and a model that states no run does not
  count (app/encoder_resolve.py). Where none matches, a TAKE is held
  anyway — its texts and the peers' vectors arrive, embed `off`; its file
  names no `[encoder]`, so it is shown under *Held, not served* with its
  reason and is not searched until one is added (the 2026-09-18 rule
  above stands) — and the answer says in plain words which model to
  install; a CREATE is
  refused (409) in the same words and creates nothing, since a Codex
  holds reference vectors only its own encoder computes (06 §6.6).
  `GET /api/encoders/suggest[?collection_id=]` answers what the node
  decided, for the take dialog to say before anything is queued.
- **The recipe** of `POST /api/hold/collection/create` without a
  `recipe`: the node's own for the plugin, `recipes/<plugin>.toml` (§7.8);
  without one, the 409 says where it goes and that a collection a peer
  offers is taken with none. What the file then takes and spends is the
  node's too: every slice and the default kinds, `embed = "on-demand"`
  (`slices`, `embed` in the body change it; a create given a `recipe`
  writes what the command writes, no slice and `off`).
- **An annotations collection's targets**: `annotates` and
  `from_snapshot` may be omitted — each claim end's uid names its
  target's plugin by its prefix (09 §9.3), the target is the collection
  of that plugin held here (where several are, the one whose newest
  snapshot holds every record named; several still, nothing is guessed
  and the 409 says to name it with `--annotates`), held from its newest
  snapshot, as `hold annotations update` re-checks; a target named
  without its snapshot takes its newest. The title and summary are
  derived from the targets and the claims, the annotator is the claims'
  one contributor (several: the 409 says to give a file with one, or
  `--annotator`). `hold collection create annotations` takes the same
  omissions.
- **What a take holds**: the whole collection, every slice and kind, the
  latest snapshot, embed `p2p` where the encoder resolved and `off` where
  it did not (above).

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

**Two routes are not under `/api` at all** (*added 2026-10-07*): the
documents 09 §9.11 fixes at a certifier's domain, served by the node for
whatever domain fronts it —
`GET /.well-known/louise-certifier.json`, this node's anchor document
(its key from `[certifier]`, §7.8, and the held collections whose Codex
names that key), and
`GET /.well-known/louise-pointer/<collection_id hex>.json`, the pointer
`hold collection certify` wrote. Both open; both nothing but what the
node would gossip.

Who may call what:

- `hold` and `node` routes require the token read from
  `ADMIN_TOKEN_FILE`, presented as a bearer token. With no token file
  configured they answer 401 — never open.
- `ask` routes are read-only and open on the LAN by default, so a guest
  and an MCP client (§5.4) need no token. They return nothing secret —
  and so they never return a deposit collection, which is searched only
  with the deposit token or the admin token. Never-share is not only
  about peers: a guest on the LAN is not the depositor either.

*Amended 2026-10-22 (operator: "a search-only gui (aka user gui). A user
token giving only access to the ask tab").* **Users.** A user is a local
name, a role and a token (`app/users.py`): `user` reaches Ask, the
reader, its pictures and translation, Connect, `/v1` and the `ask`
routes, MCP included; `admin` reaches everything, as the admin token
does. Until the first ACTIVE user exists, the `ask` routes stay open as
above; from then on they need a token, presented as a bearer token (an
OpenAI client's API key, an MCP client's header) or as the cookie
`/login` sets. Login, Help, `/healthz`, the peers' routes and the
well-known documents stay open. A token is printed once, by `user add`
and `user reissue`; only its SHA-256 is kept, in `users.toml` (§7.8).

| command | route (admin) |
|---|---|
| `louise user add <name> [--role user\|admin] [--expires never\|90d\|<date>]` | `POST /admin/api/users` `{name, role, expires}` → `{…, token}` |
| `louise user list [--json]` | `GET /admin/api/users` |
| `louise user role <name> <role>`, `louise user expire <name> <expiry>` | `PATCH /admin/api/users/{name}` `{role?, expires?}` |
| `louise user reissue <name>` — the old token dies at once; a revoked user is active again | `POST /admin/api/users/{name}/reissue` → `{…, token}` |
| `louise user revoke <name>` — the entry stays | `POST /admin/api/users/{name}/revoke` |
| `louise user remove <name>` | `DELETE /admin/api/users/{name}` |

The person's own: `GET /login`, `POST /login` `{token}` (a cookie for 30
days or until the token expires), `POST /logout`, `GET /api/me` →
`{name, role, expires}`. A dialog never manages users (RULE 2: choices
for operators live in the command line); System lists them, read-only.

**The OpenAI-compatible endpoint** names its models (*same amendment*):
`GET /v1/models` lists `louise` — every collection — and
`louise/<name>` for each collection a search may use, so a client that
sends nothing but a model name (Open WebUI) chooses what is searched by
choosing the model; a body's own `collections` wins; `gutenberg-rag`,
the old name, is still taken as `louise`. A body with `sources: true` gets the hits the answer's [n] number beside it, `louise_sources` (in a stream, the first chunk, before any word; an OpenAI client ignores the key): one search for both, which the Open WebUI pipe (`deploy/openwebui/`) shows as its sources. And `search_id`, the `X-Louise-Search` header of the Ask page's `/ask/query`: the answer is written from that search's hits instead of searching again (one question, one search; kept 10 minutes; unknown or expired, the answer searches as before). **MCP over HTTP**
(`POST /api/ask/mcp`) is the Streamable HTTP transport with JSON
answers: 202 with no body for a notification, a batch answered as a
batch, `GET` 405 (no server-initiated stream). **Connect** (`/connect`)
shows each viewer how to reach these from Open WebUI, an agent or a
script, filled in with the address they used and their own token.
- the LocalAGI routes below require the token read from
  `DEPOSIT_TOKEN_FILE`, reads included, and that token opens nothing
  else: an agent that can write its memory cannot pause the node.
- `share` is not an HTTP API of the node: it is the signed peer-to-peer
  messages of 09 §9.9 and the torrents of 09 §9.16. A sender is
  identified by its key and nothing else, and the aggregate limit of
  08 §8.2 bounds what an unknown key can cost.

**OpenAI's vector stores and files** (*amended 2026-10-22*, operator: "add the
OpenAI missing interface calls"; `app/openai_stores.py`). A third alias: code
written against OpenAI's SDK sets `base_url` to this node. A vector store is
a collection, its id the local name. A collection built from its origin is
read-only here: listed, searched, its records listed and read. A deposit
collection is the writable kind:

| route | is |
|---|---|
| `GET /v1/vector_stores`, `GET /v1/vector_stores/{id}` | the collections a search may use; deposit ones only for the deposit token or an admin |
| `POST /v1/vector_stores {name, file_ids, metadata}` | `hold collection create deposit`, then each file attached |
| `POST /v1/vector_stores/{id} {metadata}` | the store's metadata (kept in `state/openai/`; a rename is refused) |
| `DELETE /v1/vector_stores/{id}` | `hold collection forget` of a deposit collection |
| `POST /v1/vector_stores/{id}/search {query, max_num_results, filters, ranking_options}` | `ask search` over that collection, not logged; filters `eq`, `ne`, `in`, `and` on its facets; `score_threshold` cuts the list |
| `POST /v1/vector_stores/{id}/files {file_id, attributes}`, `…/file_batches` | `hold deposit add`, the key being the file's name |
| `GET …/files`, `GET …/files/{file_id}`, `GET …/files/{file_id}/content` | the deposits, or a curated collection's records by uid (a cursor on the primary key) and their text |
| `POST …/files/{file_id} {attributes}`, `DELETE …/files/{file_id}` | the attributes; `hold deposit erase` |
| `POST /v1/files` (multipart), `GET`, `GET {id}`, `GET {id}/content`, `DELETE {id}` | uploads waiting for a store, in `state/openai/files/` |

Every write, and every route about a deposit collection, needs the deposit
token or an admin; reading a curated store is an `ask` route. Errors are
OpenAI's `{error: {message, type, param, code}}`. Not offered: the
Responses API's `file_search` tool (a second chat API), chunking strategies
(a collection's chunker is fixed in its Codex), expiry (stored, echoed).

**The LocalAGI routes** (operator, 2026-09-29;
`notes/deposit-collections.md`). The second protocol alias beside
`/v1/chat/completions`: the knowledge-base API that LocalAGI's HTTP
client speaks (`pkg/localrag/client.go`), so that an agent pointed at
this node (`LOCALAGI_LOCALRAG_URL`, or an agent's `local_rag_url`) uses
it as its knowledge base unchanged. Each route is one command of the
tree, answered in LocalAGI's shape rather than §7.7's:

| route | is |
|---|---|
| `POST /api/collections` `{name}` | `hold collection create deposit --name` — 201 |
| `GET /api/collections` | `hold collection list`: deposit collections, and every held collection read-only |
| `GET /api/collections/{c}/entries` | `hold deposit list` |
| `GET /api/collections/{c}/entries/{key}` | `hold deposit show` — the whole text and `chunk_count` |
| `DELETE /api/collections/{c}/entry/delete` `{entry}` | `hold deposit erase`, then the remaining keys |
| `POST /api/collections/{c}/search` `{query, max_results}` | `ask search` over `{c}` alone |
| `POST /api/collections/{c}/reset` | `hold deposit erase` of every key |
| `POST /api/collections/{c}/upload` (multipart `file`) | `hold deposit add`, the key being the BASE NAME of the part's file name — LocalAGI's client sends the path it uploaded from, and Go's own server keeps the last component; the answer is the bare object `{status, filename, key}` |
| `POST`, `DELETE`, `GET /api/collections/{c}/sources` | `hold source add`, `erase`, `list` |

Every other answer is the envelope `{success, message, data, error:
{code, message, details}}`; success is 200 except create's 201, and a
failure carries §7.7's status with the envelope. A search result is
`{id, content, metadata, similarity}`: the uid, the chunk text, the
record's citation fields as strings, and the vector leg's cosine — 0
for a hit the text index alone found. A write to a collection that is
not a deposit collection answers 409; a source added before the
web-sources disclaimer is accepted answers 409 and fetches nothing
(04 §4.4). A write through these routes is DONE BY THE WORKER: the web
queues it and waits (a bounded wait, then 503 with the job still queued),
because tantivy allows one writer per index directory and the worker keeps
its writers open. `hold deposit add` and `erase` queue the same way on a
running node; `--here` writes in the command's own process, for a root no
worker runs on. A search through these routes is not written to the query
log, which the open `/api/stats` shows: an agent's queries are its
memory's business. A deposit is searchable by text as soon as its upload is
answered — record, chunks and text index are committed in the request —
and by vector once the node's next round has embedded it; the request
never waits on the encoder.

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
command is `louise`; `python -m app.louise` is the same parser. The old
names lived one release as hidden aliases and are **retired
(2026-10-08)**: `python -m app.cli` is gone and `louise <old …>` is an
unknown command. The table says where each went.

| previous | now |
|---|---|
| `corpus add` / `grow` (`--profile P`) | `hold records fetch --profile P` — both fetched; neither created |
| `corpus add --ack-terms FILE` | `hold collection ack` |
| the web interface's *new collection* | `hold collection create --recipe FILE`; the annotations type, `hold collection create annotations` (*amended 2026-10-19*). It asks the source and a local name — and the claims file — only; the recipe is `recipes/<plugin>.toml`, the encoder, the targets and the rest are the node's (§7.4, *amended 2026-10-25*) |
| `corpus status` | `hold collection show` |
| `pack build`, `tools/pack_convert` | `hold records build` |
| `pack verify` | `hold check` |
| `pack cat` / `dump` | `hold records show` / `hold records show --shard` |
| `pack adopt`, a pack with `manifest.json` and several spaces | none: every pre-Codex pack was migrated in step 8, and `dev adopt` and `dev migrate pack` were dropped unbuilt (*amended 2026-10-08*) |
| `snapshot record` / `delta` / `list` | `hold snapshot create` / `delta` / `list` |
| `snapshot vectors` | `hold vectors build --delta` |
| `space repin` | `hold collection derive`, then `hold collection forget <mother> --kinds vectors` when the mother's vectors are no longer wanted here. Nothing is re-pinned in place: a shared space is never destroyed under the peers that hold it |
| `backend check` / `rebaseline` | `hold embedder check`, `hold embedder rebaseline`. The probe baseline and its cosine threshold give way to validating each run once and checking the run identity on every answer (03 §3.3). The batch-integrity check becomes the gate's `batch_invariant`. A Codex below version 3 keeps the probe baseline (`app/verify/backend_gate.py`); its changed backend is accepted with `hold embedder rebaseline` (*2026-10-08*) |
| `tools/calibrate_space`, `build_space_artifact` | `hold embedder calibrate` |
| `bundle keygen` / `trust` / `trust --quarantine` | `hold key create` / `trust` / `trust --deny` |
| `bundle export` / `import` / `status` | `hold vectors export` / `import` / `show` |
| `bundle resweep`, `tools/vector_compliance`, `vector_sweep` | `hold vectors check [--drop]` |
| `genesis import` / `status` | `hold vectors import <collection> --archive ID` / `hold vectors show` |
| `genesis retry` | `hold vectors import <collection> --archive ID --retry` |
| `index vector build` / `status` | `hold index build --vector` / `show` |
| `index vector prune` | none: the node prunes hourly |
| `health` | `node health` |
| `genesis recheck` / `recount` | `dev genesis recheck` / `recount` |
| `pack status`, `bundle public-key` | none: `hold collection show`, `hold key show` |
| `embed materialize` | `hold config apply` (it writes; `hold config show` displays) |
| `contribute` | `share contribute` |
| `torrent publish`, `torrent seed` | `share <collection>`; serving is the node's |
| `python -m app.worker`, `python -m app.api` | `louise node start` |
| `/api/control`, the Library's pause and embed-mode controls | `node pause` / `resume`, `hold config apply --set embed=…` |
| `/admin/…`, `/api/search`, `/api/verify` | `/api/hold/…`, `/api/ask/search`; T4 needs no route — it runs on use |
| `/v1/chat/completions` | kept as an alias of `/api/ask/chat` for clients that speak that protocol |
| LocalAGI's `/api/collections/…` | kept as aliases of `hold deposit`, `hold source` and `ask search` for LocalAGI (§7.4) |
| `tools/sched_report`, `profile_embed`, `migrate_manifest`, `sync_refs` | `dev schedule-report`, `dev embedder-profile`, `dev migrate schema`, `dev refs-sync` (`dev load-profile` retired with RAGE, 2026-10-18: it measured reloads through RAGE's admin API) |

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
message on stderr names the unit — the part, the document, the bundle,
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
answering from a later one. For a collection whose Codex `profile`
declares a validity axis (09 §9.3; *amended 2026-10-12*) the date selects
instead, in the newest held snapshot, the version of each Work whose
validity covers it — the start on the record, the end derived as the next
version's start, the day a version takes force belonging to it (05 §5.1). `<record-id>` is a uid,
`<plugin>:<native_id>` (02 §2.3). `<key>` is `ed25519:` and 64 hex
characters (09 §9.2). `--shard` takes `<slice>/<n>` —
`year=1975-1979/00003` — because part numbers restart per slice; the
flag keeps the old word until the code follows.
`--slice` takes slice values as they appear in the SHARD's paths.
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
answer: `collection forget`, `vectors check --drop` and `dev migrate schema`
— and `collection migrate --apply`, the Codex-field
migration of 2026-09, the same way. `node pause <collection>` releases
the lock at the end of the current unit, and those then run (built
2026-10-06; `dev migrate schema` 2026-10-07). A reader never blocks a
writer or is blocked by one: the state database runs in write-ahead
mode, and a writer that reads first begins immediately, never deferred.
Two nodes on one machine are two roots (§7.8) and share nothing.

**Interruption.** On SIGTERM or SIGINT a command finishes its current
unit, commits and exits 130, within the time one unit takes; a second
signal does not shorten that. The unit per stage:

| stage | unit |
|---|---|
| `records fetch`, `share fetch` | the current file, at byte granularity — a checkpoint mid-file, the byte offset committed |
| `records build` | the document; a part cut short is re-cut on resume |
| `snapshot create` | atomic: the file is written last and is there entire or not at all |
| `vectors build` | the batch: the chunks in flight to the encoder, each written only from an answer naming a validated run (03 §3.3) |
| `vectors import`, `share contribute` | the bundle, marked applied in the same transaction as its last row |
| `index build` | the part |
| `check`, `share attest` | the sampled part; an attestation is written only for a completed check |
| `forget` | the member; a member is gone entire or still there |
| `deposit add`, `deposit erase`, `source erase` | the deposit: stored or erased entire, never half |
| a source's refresh | the source: its deposit is replaced entire or kept as it was |

A re-run after 130 continues from the last committed unit and writes the
same bytes it would have written uninterrupted. The node stops every
collection at its unit and exits 130 on a signal, 0 on `node stop`.

## 7.8 Configuration and the state directory

Three tiers, sorted by how often they change. Every value has exactly
one home.

| tier | carries | changes by |
|---|---|---|
| **environment** — identity | `LOUISE_ROOT` (the one directory), `NODE_KEY_PATH`, `NODE_PUBLIC_KEY_PATH`, `ADMIN_TOKEN_FILE`, `DEPOSIT_TOKEN_FILE`, `GIT_PRIVATE_KEY_FILE`, `CHAT_URL`, `TRANSLATE_URL`, `TRANSLATE_MODEL` (the model TRANSLATE_URL's server is asked for, as `CHAT_MODEL` is for chat; amended 2026-10-09), `RERANK_URL`, `GPU_PROBE_URL`, `RAGE_URL`, `VEGA_URL` | restart |
| **files** — desired state | `platform.toml` and `collections/<name>.toml` under `LOUISE_ROOT` | editing the file; `hold config apply` |
| **files** — desired state | `users.toml` beside `platform.toml` (*amended 2026-10-22*): the users of §7.4, each token as its SHA-256 only; when each was last seen is `state/users-seen.json`, not desired state | `louise user …`, the admin API — never by hand |
| **files** — desired state | `recipes/<plugin>.toml` beside `collections/` (*amended 2026-10-25, RULE 2*): this node's recipe for a plugin (06 §6.6) — what the Library's *new collection*, which names a plugin and a local name only, creates from | editing the file |
| **state database** — runtime overrides | the node's mode — `node pause`, `--seed-only`, `--acquire-only` — in force until `resume` | the commands; a restart keeps them |

Desired state is never written into the database, and a runtime override
never survives the node applying the files — at start, at `hold config
apply`, and when a file changes. The one exception is the node's mode
(§7.3) — which no file sets, so there is nothing to
apply over it: it survives a restart until `resume`. An expiry would turn
a node the operator holds at acquire-only back into a running one, encoder
and all, on every restart (operator decision, 2026-09-13). What `hold config show` prints comes
from the same resolution the node uses, each value labelled with its
tier; a value shown anywhere else comes from that resolution too.

`LOUISE_ROOT` has no default; a node without it refuses to start.
`NODE_PUBLIC_KEY_PATH` names the key's public half in 09 §9.2's textual
form, one line, for a process that recognises what this peer signed and
signs nothing — the web process, which is given it INSTEAD of
`NODE_KEY_PATH` (operator, 2026-09-29). The file is trusted — whoever
replaces it decides which signatures count as this peer's — so it is kept
beside the key, read-only, never under `state/` or the files; one writable
by group or others is refused, and a process given both refuses to start
when they disagree. `hold key create` writes it with the key when
`NODE_PUBLIC_KEY_PATH` is set; for a key that has none, `hold key show` prints the line to put there.
`NODE_KEY_PATH` unset means this peer cannot sign — no bundles, no
attestations, no `share` of its own work; import, check and search work
without it. `ADMIN_TOKEN_FILE` unset means the `hold` and `node` routes
answer 401 (§7.4); `DEPOSIT_TOKEN_FILE` unset means the LocalAGI routes
do. `GIT_PRIVATE_KEY_FILE` names the SSH key a deposit collection's
`.git` sources are cloned with (06 §6.7); unset, such a source is
cloned without one and needs the web-sources disclaimer like any
URL. There is no default encoder: a collection names
its own in its file's `[encoder]`, and a file that names none is a
non-compliant collection — refused, not served, shared or searched, the
reason named by `hold collection show` — and everything that would need
its encoder (`vectors build`, T3, T4) defers (exit 3). `TRANSLATE_URL`
unset means the chat model translates (05 §5.2); `CHAT_URL`
unset refuses `ask chat` (exit 2); `RERANK_URL` unset returns
`reranked: false` (05 §5.6) for every collection whose file names no
`[reranker]` of its own; `GPU_PROBE_URL` unset leaves the node's GPU
panel empty and says why, and nothing else changes; `RAGE_URL` — the
node's model container (03 §3.3) — unset leaves the System page without
the container's models and their controls, and the node then learns a
run only from the answers it gets, which is enough to check every one
of them. `VEGA_URL` is where the receipts of an `api = "vega"` encoder
are read (`/vega/receipts`, `/vega/runs/<run_id>`); unset, every answer of
such an encoder is a deferral and nothing is written or searched through
it (amended 2026-10-08, docs/notes/vega.md). Secrets are named
by path only — the key, the token — never by value in an environment
variable, a file, a log line or a message.

**`platform.toml`** — the node:

```toml
[node]       http_port = 8742   peer_port = 6881   name = "nas"   address = "http://nas:8742"   # address: where THIS node answers pulls, as peers reach it (09 §9.9; added 2026-10-06)
             # peer_port: where this node's swarm listens, as peers reach it; named in its digest (09 §9.9) so a
             # fetcher with the DHT off still finds the swarm; absent, the session's own port is named when one runs
[budget]     down_max = "400Mbit"   up_max = "40Mbit"   schedule = "01:00-07:00 full; else 30%"
             # up_max is the swarm's upload ceiling too: the session uploads no faster (invariant 5; until
             # 2026-10-19 seeding was only counted); `[bandwidth]`, the older spelling, still loads
[peers]      addresses = ["http://peer.example.org:8742"]   # the bootstrap of 04 §4.2, where the peer answers the
             # messages of 09 §9.9 (a bare host:port is http); `share peers add` writes here, each address once
[peers.pins] "http://peer.example.org:8742" = "ed25519:…"   # a digest from that address under another key is discarded
[share]      patch_cache_s = 604800   # how long a part patch computed for a peer is kept for the next (09 §9.9;
             # added 2026-10-12): 0 deletes it once served
[translate]  reading = { la = "index-translate-9b", grc = "index-translate-9b" }   # what is READ (the Ask
             # page's results, the reader) in a listed source language goes to that model, one item per call,
             # falling back to TRANSLATE_MODEL; searches never use it. Node-wide, in no Codex (added 2026-10-12)
[dht]        enabled = true
[admission]  bytes_per_hour = "64MB"   objects_per_hour = 1000   queue = "256MB"
             # 08 §8.2: what this peer accepts per hour from keys whose work it has not seen
             # reproduced. Local, never negotiated; 0 accepts nothing from strangers
[ask]        latency_budget_ms = 8000                   # the reranker's bound at the default depth
             translate_share = 0.90                     # a translated query goes out only into each collection's
             # MAIN languages: its one-language text indexes, largest first by documents, until they hold this
             # share of the collection (0 < share <= 1; 1.0 is every one of them). The others are searched by
             # the vector leg, and by text only in the query's own language (05 §5.2; amended 2026-10-10)
[models]     load_timeout_s = 120                       # the bound on LOADING a model its runner reports as not
             # loaded (LocalAI's GET /system): Louise warms it with a minimal request first, so no request's
             # budget or timeout holds a cold start; a model measured on the node has its own bound
             # (app/model_loads.json, docs/notes/model-profile.md), this is every other's. A load past it is a
             # deferral (embedding) or a fallback that says "model loading took longer than N s" (05 §5.6)
             # (added 2026-10-09)
[certifier]  valid_from = "2026-10-07T00:00:00Z"        # this node as a certifier (09 §9.11): from when its own key certifies, and
             # revoked_at = "…"                          # when it was revoked — the anchor document it serves at
                                                        # /.well-known/louise-certifier.json says so; the collections listed there are
                                                        # the held ones whose Codex names this key (added 2026-10-07)
             # of 50; a deeper list gets it in proportion (05 §5.6)
[deposit]    encoder = "http://nas:30450/upstream/granite-embedding-107m-multilingual"
             # the encoder a deposit collection is created with when none is named (02 §2.9);
             # granite-embedding-107m-multilingual, LocalAGI's own default, unless changed here.
             # The SERVER, as [encoder]'s url; with it, the rest of the [encoder] block a new deposit
             # collection's file is written with (amended 2026-10-09):
             api = "openai"                     # openai (default) · ollama · vega, as [encoder]'s
             # embed_model = "…"   query_model = "…"
             #                                  # optional; absent, the model the URL names (…/upstream/<model>),
             #                                  # else `create deposit` is refused — never the encoder's label.
             #                                  # Required, both, with api = "vega" (LocalAI: one address, every model)
             space = "deposit-space.toml"       # its space block (a TOML, or a JSON file by its `.json` name), beside this file: read once, by the
                                                # first deposit collection on it; later ones copy its space
             # or: space_from = "sha256:…"      # a Codex to copy the space from instead
[deposit.collections.alice]  space_from = "sha256:…"    # one name's encoder, for a collection a client creates
```

**`collections/<name>.toml`** — one held collection; the file existing
is the collection being held:

```toml
codex  = "sha256:…"                            # the collection_id this name holds; shard/codex.json's inner hash must equal it (09 §9.3)
title  = "PubMed"                              # optional: overrides descriptive.title for this peer's own listing; never published
slices = ["year=1975-1979", "year=1980-1984"]  # or "all"; a list is PART of the collection, held by `share fetch --slice` (§7.3)
kinds  = ["records", "vectors"]
embed  = "on-demand"                           # off | on-demand | all | p2p: what the node's encoder spends, never what search uses;
                                               # all = every part itself ("all local"), p2p = the parts rendezvous gives this node,
                                               # published as it goes ("all p2p", 04 §4.5) — p2p needs share = true (below; 2026-10-19)
share  = true
share_slices = ["year=1980-1984"]              # optional: serving narrowed to these slices (`share --slices`); absent = all held
share_kinds  = ["records"]                     # optional: and these member kinds (`share --kinds`); terms, validation,
                                               # calibration and seals are served whatever it says (amended 2026-10-07)
source = "origin"                              # origin | shard: where the documents are read from — the plugin's corpus, or
                                               # the record parts held (the value was "drag" until 2026-10-11); `share fetch` writes "shard", and such a collection
                                               # is never `records build`-able here (amended 2026-10-07: the key did not exist)

[encoder]                                      # required: a collection names its own encoder, there is no default
                                               # the endpoint that serves the Codex's encoder: a MODEL on the node's model
                                               # container RAGE, never a device — RAGE places the model on a device, and
                                               # states the run, device included, with every answer (03 §3.3)
url = "http://nas:30450/upstream/medcpt-article"   # the SERVER, never the endpoint: the client appends
                                               # /v1/embeddings (openai, vega) or /api/embed (ollama) — a url that
                                               # already ends in either is wrong (amended 2026-10-09)
query_url = "http://nas:30450/upstream/medcpt-query"   # a bi-encoder's query side, a server too; absent when symmetric
embed_model = "medcpt-article"                 # required: the model documents are embedded with
query_model = "medcpt-query"                   # required: the model questions are embedded with; one model doing both
                                               # is named twice. Where the URL names a model (`…/upstream/<model>`) the
                                               # key is that model, else the file is refused; where it names none
                                               # (LocalAI's /v1/embeddings, vega) the key is the request body's `model`,
                                               # each side its own (amended 2026-10-08: one `model` key named the
                                               # document side only; no alias, an old file is refused until
                                               # tools/migrate_model_keys.py rewrites it)
api = "openai"                                 # openai (default) · ollama · vega: LocalAI's /v1/embeddings with a `vega-` model
                                               # named by `embed_model`/`query_model`; every vector, a query's too, taken only with its VeGa
                                               # receipt, whose run is the answer's (amended 2026-10-08, docs/notes/vega.md)

[reranker]                                     # optional (05 §5.6): this node's reranker for this collection
url = "http://nas:30447/v1/rerank"             # the full endpoint, path included — unlike [encoder]'s url
                                               # (amended 2026-10-09)
api = "jina"                                   # jina: {query, documents} (llama.cpp's /v1/rerank) · tei: {query, texts}
rerank_model = "bge-reranker-v2-m3"            # required with the block: the request body's `model`; equal to the model
                                               # a `…/upstream/<model>` URL names (amended 2026-10-08)
window = 2048                                  # optional: the longest candidate read, in the model's tokens; a longer one
                                               # is cut before it is sent (05 §5.6, amended 2026-10-21). 0: uncut

[[sources]]                                    # a deposit collection only (06 §6.7): one per subscribed URL
url = "https://example.org/sitemap.xml"
interval = 60                                  # minutes
```

The encoder endpoint is the collection's `[encoder]` and nothing else: a
node has no default encoder, and a file that names none is refused
(operator, 2026-09-18). `hold collection create` writes the endpoint it
was given (`--encoder`, `--query-encoder`) into the file it creates, with
`api` (`--api`, openai unless given) and `embed_model` and `query_model`:
the model each side is given (`--embed-model`, `--query-model`), else the
model its URL names; a symmetric encoder's query side is the document
side's model. A URL that names no model (LocalAI's one address, `vega`)
and no flag naming it is refused before anything is created — never
answered with the space's encoder label, which names no model any server
is asked for — and so is a flag that is not the model its URL names. The
calibration set is embedded on that `api` with those models. The flags
are node-local, beside `--encoder`, and never in the recipe, whose
`space` block is hashed into the Codex; the create route takes
them as `api`, `embed_model` and `query_model`, and `share fetch
--encoder` takes the same three flags (amended 2026-10-09; 2026-10-08
fell back to the label). The Library sends none of them: where a route is
given no `encoder`, the NODE determines all four — its model server, and
the model there whose stated run loads the Codex's weights, each side its
own — and where nothing it runs loads them it says so in plain words
instead of asking (§7.4, *amended 2026-10-25*, RULE 2). A deposit
collection's file (02 §2.9) takes `[deposit]`'s `encoder`, `api`,
`embed_model` and `query_model` — the models, where `[deposit]` names
none, from the URL, under the same refusal. `[deposit]` is checked at
load as an `[encoder]` is: `api` one of the three, both models with
`vega`, a model the URL names agreeing with it. A creator naming another
endpoint (`--encoder`) names its `--api` and models with the flags, else
gets the default `api` and the model its URL names (amended 2026-10-09). A file whose
`[encoder]` or `[reranker]` lacks its model keys, still has `model`, or
names a model its URL does not is refused for that collection, its
reason where refusals are shown; the node keeps the others. The reranker resolves
the collection's `[reranker]`, then `RERANK_URL`; with neither, the list
keeps its fused order and says so (05 §5.6). `embed
= on-demand` computes vectors lazily, for the chunks a search needs; a
vector computed that way is bundled and shared like any other. Search
runs over whatever vectors exist regardless of the mode.

**Two axes, one meeting point** (amended 2026-10-19, operator). `embed`
says what this node's ENCODER spends: `all` ("all local" in the Library)
embeds every part itself, whoever leads it, and its duplicates become
checks (04 §4.5); `p2p` ("all p2p") embeds the parts rendezvous gives
this node, with the cascade, and publishes as it goes — the embed stage
does the first and the worker's PUBLISHER the second (*amended
2026-10-19*): on its own thread, every `PUBLISH_BEAT_S`, where the mode
embeds, the open parts whose vectors the node holds are bundled through
the node's own ladder, recorded in the coverage map and announced; a
pass is bounded by `PUBLISH_BUDGET_S` and resumes at the next beat. It
is `share contribute`'s pass without the build, run under the lock the
worker already holds, so a node no longer has to pause a collection to
publish it — and it runs for EVERY shared collection whatever its
`embed` says, since sharing means every embedding held leaves as
bundles (below): the parts a night under `p2p` finished leave in the
morning under `on-demand`, not at the next night's flip. `share` says what
this node makes AVAILABLE — its records, and every embedding it holds as
bundles (04 §4.3), whatever `embed` says and wherever the vectors came
from; a node whose embed is `off` still bundles what an archive or an
earlier run gave it, and every node takes peers' bundles in. The two
meet in one place: `p2p` publishes, so it needs `share = true`. Switching
`embed` away from `p2p`/`all`, or `share` off, releases the parts this
node leads under rendezvous: the next digest, sent at once, carries no
benchmark for the space (04 §4.5, *amended 2026-10-20*). The
Library asks before writing either pair — setting `all p2p` with sharing
off enables sharing; disabling sharing under `all p2p` switches the mode
to `all local`; proceed or abort — and a file edited by hand to say
`p2p` with `share = false` runs as `all`, shown as such on its card.
Beside the drop-down the card reads the two axes: the bounty list (the
open parts of the current snapshot — any peer may embed any of them, 04
§4.5; the parts this node is first for, and those it leaves to the fetch)
and availability (chunks bundled here, chunks taken from peers).

An `[encoder]` URL names a model and never a device. Where the model
runs is the model container's placement, and a change of device is a
change of run, which the gate tests by itself (03 §3.3): nothing in a
collection's file has to follow it. Every request the node makes to a
model endpoint carries the request header `Louise-Level` (09 §9.12).

An encoder that answers as a wrong endpoint would — 404, 405, 401, 403,
a redirect, a body that is not embeddings — pauses that collection's
embedding with the answer as its reason, shown where the gate's pauses
are, until its `[encoder]` changes or the endpoint answers again (asked
every five minutes). A busy one (408, 429, a 5xx) or an unreachable one
defers the batch. Neither is ever an error on a document; only a 400,
413 or 422 about one input, the batch split down to it, is (amended
2026-10-09). A request that times out defers too, and holds the encoder
as busy — nothing sent to it for a minute, doubling to ten while it
answers no one; a search withholds its vector leg and says why. A
document is blamed for a timeout only after three, each one after the
encoder answered other documents since the last, so a hung encoder
blames no one (amended 2026-10-09).

`hold collection create` and `share fetch` write the
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
│       ├── shard/           the copy, the SHARD (`drag/` until 2026-10-11): exactly the tree of 02 §2.6 — what `share` serves, and nothing else; the bytes may live on other storage, mounted here
│       ├── state/           manifest.sqlite, cursor, ack.json, attestations/, bundles/, lock — this peer's alone
│       ├── index/           the text index of the parts held, and the facet values (05 §5.7)
│       ├── lance/           the vector table search reads
│       └── origin/          the origin files the records are parsed from — 06 §6.2's corpus directory, kept for records-vs-origin; the bytes may live on other storage, mounted here
├── state/                   node.sqlite: the catalogue, coverage maps (09 §9.10), peers seen, runtime overrides; trust.json; web-sources-ack.json (09 §9.13)
└── keys/                    the peer key, at NODE_KEY_PATH
```


One mount per kind, a folder per collection (amended 2026-10-12, operator):
with `SHARDS_ROOT` and `ORIGINS_ROOT` set (environment: this node's
paths), `create` makes `<name>/shard` a link to `<SHARDS_ROOT>/<name>` and
`<name>/origin` a link to `<ORIGINS_ROOT>/<name>`; a custom origin is
linked by hand. The paths above, which every manifest stores, do not move,
and a new collection needs no new mount. With `BUNDLES_ROOT` set (amended
2026-10-19, operator), `<name>/state/bundles` is likewise a link to
`<BUNDLES_ROOT>/<name>`: the bundles a peer makes and takes in (09 §9.7)
are a second copy of the vectors its table holds — about 5 KB a chunk, some
55 GB for Gutenberg — and rest with the SHARDs on the bulk storage, not on
the disk that keeps what queries read. An existing collection's bundles
move there with `dev adopt-bundles <collection>`: copied with their sizes
and times (the seeder's root cache stays valid), verified byte for byte,
the old directory set aside as `state/bundles.moved-<time>` for the
operator to delete, the link put in its place. `rename` and `forget` follow
the link as they do the SHARD's.

A fast drive for derived data (*amended 2026-10-20*, operator: "a cache
folder on a fast drive, like nvme or ssd, to be used for such
operations"): with `LOUISE_CACHE_DIR` set (environment: this node's path),
every cache a plugin BUILDS FROM ITS ORIGIN and seeks in while it parses —
pubmed's per-container pack, wikipedia's page-props pack, wikidata's pack
and label table — lives under `<LOUISE_CACHE_DIR>/<kind>/<tag>/`, the tag
the origin's resolved path (physical identity: two corpora may hold a
file of one name). Such a cache is re-creatable and never exchanged; the
archive it derives from, the SHARDs, the bundles and the state stay where
they are. Unset, each cache stays beside its origin, as before. A cache
already built beside its origin is carried in once, whole or not at all,
and left where it was for the operator to delete; a plugin's own setting
(`PUBMED_PACK_DIR`) wins where set. Measured on the node: wikidata's parse
is ~95 % label lookups, 133–179 entities/s a core from the HDD pool while
the scan writes, 1,763 from RAM.

`dev mark-embedded <collection> [--dry-run]` (amended 2026-10-19): the
documents whose every chunk has a vector here and whose manifest row still
says `embed_done = 0`, marked as the embed stage marks them. The one-off
repair for what `share contribute`'s build embedded and never marked (372
documents on the node); the build marks what it completes since, after the
write. Per shard of the current snapshot, one lookup of its chunk ids.

`dev withdraw-covered <collection> [--dry-run] [--yes]` (added
2026-10-22) is the producer's side of 03 §3.6's `withdraw` for bundles
another bundle already covers: this peer's accepted bundles of the
collection whose every record is held, with the same content_hash, by an
accepted bundle that is not one of them — another producer's, or a
same-space predecessor's of anyone (04 §4.5) — counted, then, asked or
`--yes`, named in ONE `withdraw` attestation (09 §9.8) published for the
beat to forward; their directories and coverage rows go here, their
vectors stay. Candidates never cover one another. First use: the 117
bundles the node's held path made for gutenberg 6f030405 after the carry
cleared the predecessor's rows.

`dev deprecate-duplicates <collection> [--dry-run] [--yes]` (amended
2026-10-19, operator) is the producer's side of 03 §3.6's `deprecate`:
this peer's accepted bundles that share a `content_digest` with another of
its own for the same part (the holder pass bundled 418 partially held
parts again after every restart, a new id each time since `created_at` is
in the signed header), and this peer's bundles whose CHUNK rows equal an
accepted bundle of another producer made earlier (`content_digest` covers
the challenge rows too, which each producer embeds itself; a peer
re-signed 44 parts of the node's rows, 2026-10-19). It lists the pairs,
keeps the earliest of each as the CURRENT copy, and — asked, or `--yes`
— writes ONE attestation
(`check` and `result` `deprecate`, `detail.bundles` pairing each
deprecated id with its current one, 09 §9.8), keeps it at the attestation
path and publishes it as an event for the beat to forward. NOTHING is
removed by the command: the copies stay served for one day
(`deprecate.KEEP_S`) and the digest lists them under `deprecated_members`
from its next send; at the fetch beat after the window a holder removes a
deprecated copy where it holds the current one too (its vectors stay — the
current copy covers them), and keeps it where it does not. Every peer
reads the attestation the same way: never starts a fetch of a deprecated
copy while a live neighbour offers the current one, finishes a fetch in
flight past half its bytes and drops one before, falls back to a
deprecated copy when the current one is offered by nobody or came back
nobody-served, and counts two equivalent copies held together once. Takes
the collection's lock; exit 2 without `--yes` off a terminal, or when the
process cannot name its image (an attestation names it). The `withdraw`
attestation (a FAULTY bundle; peers drop it and its vectors) has no
command yet — `hold vectors check --drop` is the spec's.

A deposit collection (02 §2.9) has the same tree, and its `shard/` holds
only `codex.json` and the calibration member. Its `origin/` holds the
deposits, byte-exact — one directory per deposit, named by its deposit
number, with the file under the name the client gave it — and is the
only copy of them anywhere; `state/`, `index/` and `lance/` hold what is
derived from them, and erasing a deposit removes it from all four.

`shard/` holds only what the Codex or a snapshot names, so a member is
hashed as it lies and `share` refuses a SHARD directory holding a file
that neither the Codex names, nor a snapshot of the chain lists — as a
record part, a relations part, a figures container, or in its `files`
(09 §9.4) — nor 09 §9.6 defines as a local derived file under
`vectors/`.

The list is spelled out because it is a refusal and a refusal that
omits a member kind does not omit a file, it refuses THE WHOLE SHARD.
Until the figures amendment this sentence said "as a shard or in its
`files`", which named neither the relations parts nor the figures
containers — both of which 09 §9.4 lists in their own top-level arrays
rather than in `shards` or `files`. A collection with relations was
unshareable and nothing said why.

`index/`, `lance/`, `origin/`, the configuration files and the keys never travel,
and neither does the state database, which holds the cursor of 06 §6.1,
the acknowledgement of 09 §9.13 and the attestations of 09 §9.8. Two
things it keeps DO travel, by their own routes rather than as SHARD
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

**A copy's state is per collection, and derived.** A copy is *refused*,
in *quarantine* or *in use* (03 §3.6): computed at start and between
rounds from what the peer holds, and stored nowhere.

*Refused* — refuse-to-start — is a collection whose
`shard/codex.json` has an inner hash other than its file's `codex`
(09 §9.3), whose vector table's space or width
is not its Codex's (02 §2.4), or whose state database carries a schema
other than this platform's. Only a person clears it.

*Quarantine* is a copy this peer has not finished testing: a check of
03 §3.6 still open, an encoder stating a run not yet tested, a licence
not acknowledged (04 §4.4), or an origin that could not be checked and
awaits its operator's decision. It clears by itself as the tests pass,
or by `hold collection ack` — for the licence, and with
`--origin-unchecked` for the origin.

Either way the copy is not served, not shared and not searched; the
node keeps running its other collections, `node show` lists it with its
state, and `hold collection show` prints every reason with the test or
the command that clears it.
