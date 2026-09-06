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

## 7.2 Four verbs, and what each produces

| verb | produces |
|---|---|
| `create` | a description — a Codex, a snapshot. Light; no heavy computation |
| `build` | an artefact derived from its inputs — records from origin files, vectors from records, an index from either |
| `fetch` | bytes arriving here, from the origin or from peers |
| `share` | bytes leaving here, and the announcement that they exist |

The remaining verbs are small and literal: `check` runs the validation
plan or one check; `show` displays state (the only word for it);
`list`; `import` and `export` move signed vector bundles as files;
`derive`, `continue`, `replace` create the three kinds of related
collection (§2.7); `certify` signs a snapshot as a certifier; `trust`
and `anchor` manage keys; `benchmark` and `calibrate` measure an
encoder. A new verb that does not fit one of these rows is probably a
flag on an existing one.

## 7.3 The tree

```
louise hold
  collection  create <plugin> [--space-from CODEX] [--certified …]   write a Codex; print its id
              derive <codex> --encoder … | --layers …                a sister: same corpus, new space
              continue <codex> --from-snapshot ID …                   a daughter: new toolchain, same space
              replace <codex> --defect FILE                           a corrective sister, with its reproducible defect
              certify <collection> <snapshot>                          sign a snapshot as this collection's certifier
              list                                                     collections held, by lineage
              show <collection>                                        Codex, snapshots, holdings, trust, certification
  records     fetch <collection> [--snapshot ID]                       run the acquisition spec to the cutoff
              build <collection> [--snapshot ID]                       parse, slice, shard, hash
              check <collection> [--origin] [--seal] [--sample F]      records against roots, origin, seal
              show <collection> <record-id> [--raw]                    one record and its chunks
  snapshot    create <collection>                                      freeze the current records as this month's snapshot
              delta <collection> <from> <to>                           what changed between two snapshots
              list <collection>
  embedder    check <collection> [--accept]                            the calibration gate; --accept records a passing run as this peer's baseline
              benchmark <collection>                                   tokens per second on the reference chunks
              calibrate <collection>                                   measure the tolerance a space can bear (space authors)
  vectors     build <collection> [--shard N | --delta ID]              embed; bundles every few hundred chunks
              import <file | --archive ID> [--strict]                  a signed bundle, or a public pre-embedded archive, through the ladder
              export <collection> [--shard N] --out FILE               a signed bundle of what this peer produced
              check <collection> [--drop]                              are stored vectors still what this encoder produces; --drop removes the ones that are not
              show <collection>                                        coverage, producers, verification
  index       build <collection> [--text | --vector]
              show <collection>
  check       <collection> [--checks …] [--sample F] [--seed N]        run the validation plan; print the report
  key         create                                                   this peer's key
              trust <key> [--remove]                                   the trust list
              anchor <collection>                                      check a certifier's key against its anchor
              show

louise share
  <collection> [--slices …] [--kinds …]                                serve what you hold — and announce it
  fetch <collection> [--slice …] [--kinds …] [--snapshot ID]           take a part
  contribute [<collection>] [--once]                                   vectors build, under rendezvous, shared
  attest <collection> [--checks …]                                     check, shared
  peers show                                                           who this peer knows, what they hold

louise ask
  search <query> [--collections …] [--space …]
  chat
  serve                                                                expose search, lookup and traversal as MCP tools

louise dev                                                             operator instruments; read-only unless told otherwise
  schedule-report      what the worker spent its rounds on
  embedder-profile     the concurrency an endpoint sustains
  migrate              schema migrations, run explicitly, never on their own
  refs-sync            reference tables from their origins
```

## 7.4 The API

The Python API is the same tree with dots:

```
app/hold/     collection.py  records.py  snapshot.py  embedder.py  vectors.py  index.py  check.py  key.py
app/share/    serve.py  fetch.py  contribute.py  attest.py  peers.py
app/ask/      search.py  plan.py  tools.py
app/plugins/  base.py  <source>/
```

`hold.records.build(collection, snapshot)` is what `louise hold records
build` calls and nothing else; `share.contribute(collection)` calls
`hold.vectors.build` and then `share.serve`. A test that greps for a
command's name finds the function that implements it and the section of
these documents that describes it. Every function is idempotent and
resumable: a write is delete-then-insert on its key, a stage marks itself
done only after its durable commit, and an interrupted run continues
rather than restarting.

The **plugin protocol** (§6.1) is versioned; a plugin declares the
version it implements, and that version is part of the platform image
the Codex names by digest. "A plugin conforms to the standard" is
therefore a statement that hashes.

The node also exposes `hold` and `ask` over HTTP for the web interface,
and `share` is HTTP between peers by nature; but `hold` is not a
service. Scripts, tests and a peer maintaining a collection alone do not
start a server to build a snapshot.

## 7.5 Rules for anyone adding a command

1. The noun is a word from these documents. If it is not, either the
   concept is missing from the documents or the command is.
2. The verb is from §7.2, with its meaning there.
3. `show` is the only word for state.
4. The help line says what you get, not what happens inside.
5. Nothing lives outside the tree. An instrument is `dev`, listed and
   marked as such.
6. A `share` command names the `hold` command it wraps.

## 7.6 Migration from the previous names

The previous command tree was organised by internal artefact. Old names
are accepted as hidden aliases for one release, with a deprecation
notice naming the new command.

| previous | now |
|---|---|
| `corpus add` / `grow` / `status` | `hold collection create` / `hold records fetch` / `hold collection show` |
| `pack build`, `tools/pack_convert` | `hold records build` |
| `pack verify` | `hold check` |
| `pack cat` / `dump` | `hold records show` / `--raw` |
| `pack adopt` | `dev migrate` (a one-time move of vectors into a packed twin) |
| `snapshot record` / `delta` / `list` | `hold snapshot create` / `delta` / `list` |
| `snapshot vectors` | `hold vectors build --delta` |
| `space repin` | `hold collection derive` |
| `backend check` / `rebaseline` | `hold embedder check` / `--accept` |
| `tools/calibrate_space`, `build_space_artifact` | `hold embedder calibrate` |
| `bundle keygen` / `trust` | `hold key create` / `trust` |
| `bundle export` / `import` / `status` | `hold vectors export` / `import` / `show` |
| `bundle resweep`, `tools/vector_compliance`, `vector_sweep` | `hold vectors check [--drop]` |
| `genesis import` / `status` / `retry` | `hold vectors import --archive` / `hold vectors show` / `--retry` |
| `index vector build` / `status` | `hold index build` / `show` |
| `embed materialize` | `hold collection show --config` |
| `contribute` | `share contribute` |
| `torrent publish`, `torrent seed` | `share <collection>` |
| `tools/sched_report`, `profile_embed`, `migrate_manifest`, `sync_refs` | `louise dev …` |
