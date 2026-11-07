# Freezing a held collection

*Design note, 2026-11-07. Built the same day. The amendments of §8 are
applied to docs 02, 04, 07 and 09.*

The operator, 2026-11-07, verbatim:

> A peer should be able to "freeze" a collection, meaning that no updated
> are fetched and it stays as it is (you can still embed locally). The idea
> is: in freeze you only decide that you want to keep that version in time
> (no embedding engine available, or whatever reason), but you can still
> share (if you check the shared box) the parts that are still aligned with
> the non frozen state.

## 1. What FROZEN means

A frozen collection **keeps the version it holds**: the snapshot its SHARD
holds (02 §2.2), the documents its manifest holds, the vectors its table
holds. Three things stop, each a deferral (CLAUDE.md: deferral is not
failure: no error is written to any document, and the work is owed, not
lost):

| what stops | where | why it is an update |
|---|---|---|
| **acquisition from the origin**: the fetch stage (06 §6.6) does nothing — no corpus fetch, no remote archive download; a fetch already running ends at its unit (07 §7.7: rsync's `.part` kept, the byte offset committed) | `worker._acquire`, `_acquire_archive`, the fetch's stop | it is how origin updates arrive |
| **discovery's scan of the origin**: the corpus is not enumerated, so no document is re-opened because its origin file changed on disk and none is added; a scan already running ends at its next batch, its cursor committed that far | `worker._scan_only` | a corpus mounted from elsewhere changes under Louise without a fetch; "it stays as it is" covers that too |
| **intake from peers**: no member of the snapshot is fetched, no newer snapshot is pulled into the copy, no bundle is taken in — by event or by digest | `share/transport.beat`, `share/accept.beat` | a peer's newer snapshot and its members are the collection's next version |

**Why the scan waits whole, and is not run "without re-opening".** The scan
cursor advances past every container it reads (discovery.py: the plugin is
told what is committed). A frozen scan that read a changed file and left
the document alone would move the cursor past the change, and the scan after
the unfreeze would skip it for good: the change lost, not owed. Not scanning
at all is the only form that is both "stays as it is" and "catches up". What
the scan does after it — the slice scope and the embed selection, both over
the manifest as it is (app/slice_scope.py, app/embed_select.py) — still
runs, so a change to `slices`, `embed_slices` or the embed policy applies to
the version held.

Everything else runs **on the version held**: parsing (rdf), the text
index, the vector index, local embedding of what is pending, search, the
plan's checks (03 §3.2), attestations, the settle of a run, a remote
archive's shards already downloaded, a bundle already in the inbox, and a
producer's withdrawal or a deprecated copy's expiry (03 §3.6, 09 §9.8:
removals the network asks for, not intake — a frozen node must not keep
serving a bundle its producer disowned).

**Local embedding.** "You can still embed locally": the collection's embed
setting still decides it, and under `p2p` the rendezvous partition (04 §4.5)
is lifted — a frozen copy takes no bundle in, so a part it left to a leader
would never get vectors here. It embeds every pending document itself, as
under `all`, and leads nothing for anyone (§3.4).

**Commands a person types are not stopped.** Freeze governs what the node
does on its own, as pause does. `hold records fetch`, `share fetch` or `hold
records build` on a frozen collection are the operator's own act and run;
the next beat then serves what the SHARD holds by the same rules.

## 2. What freeze is NOT

- **Not a pause.** `node pause <collection>` runs nothing for it (07 §7.7):
  its lock is released, no parse, no index, no embedding, no search work of
  the worker's. A frozen collection is run; only its updates stop.
- **Not a fork, a daughter or a sister** (02 §2.7). No Codex is written, no
  `collection_id`, `lineage_id` or snapshot changes, nothing is published
  about the collection. It is the same collection held at an earlier
  snapshot by choice — what any peer that falls behind already is — and
  unfrozen it catches up like one.
- **Not a narrowing of what is held** (`slices`, `kinds`): nothing is
  dropped, and invariant 11 is untouched (§5).

## 3. Sharing while frozen: the ALIGNED parts

### 3.1 What identifies a part, and what a peer announces

- A record part's content is its **`root`**, the BitTorrent v2 pieces root
  of its stored file (02 §2.2; 09 §9.4); `records_root` names its records.
  The transport serves a file as a **torrent**, and 09 §9.16 makes the
  torrent — so the infohash a fetcher computes from ITS snapshot — a
  function of the member name, the SHARD-relative path, the length and the
  root, and of nothing else. Relations and figures parts and the
  whole-collection files (`terms`, `validation`, `slicing`, calibration,
  seals) are listed the same way, with a path, a root and a size.
- A peer announces a collection's latest snapshot in its **digest**: per
  collection, `snapshot_id` and `snapshot_hash` (09 §9.9), and the members
  it serves by name and member root. The snapshot itself, which lists every
  part's root, is pulled with `pull snapshot` (04 §4.2).

### 3.2 Aligned, exactly

**A file is aligned when its torrent is that file's torrent in the newest
snapshot the frozen node knows of**: same path, same root, same size (the
member name follows from the path). A bundle is aligned when the record
part it covers (`state/bundles/<space>/<slice>/<NNNNN>/…`, 09 §9.7) is.
With no newer snapshot known, the newest is the node's own and everything
held is aligned.

"The newest it knows of" is the newest it has **read**: its own snapshot,
or a newer one a live neighbour's digest lists, which the frozen node pulls
from that neighbour at the share-fetch beat (`pull snapshot`, the chain
down to what it holds, verified as a fetched snapshot is: its hash the one
the digest advertised, its chain linked, at least one signature verified)
and keeps **aside**, under `<collection>/state/frozen/snapshots/` — never in
the SHARD, never promoted, no member following it. The copy does not move.
On unfreeze the directory is removed; the normal beat then takes the
snapshot in as for any peer.

Until a newer snapshot announced is read, the last one read applies. The
alternative — offer nothing while an announced snapshot is not in hand —
was rejected:

| until the announced snapshot is read | pros | cons |
|---|---|---|
| **the last snapshot read applies** (built) | a digest that lies (an id nobody can hand over) changes nothing: no defence may need to know whom to trust (08) | for one beat (15 s) after a new snapshot is announced, a part that just changed may still be served; it is a valid part of a valid snapshot, and a fetcher on the newer one computes another infohash and never asks for it |
| offer nothing | never serves a part that changed upstream | any peer announcing a snapshot id it cannot hand over switches off a frozen node's sharing for as long as it likes |

### 3.3 Offered how — with the existing announcement and serving code

- **The seeder** (`share/seeder.publish`) serves one torrent per file: under
  freeze, a file that is not aligned is not served, and a bundle whose part
  is not aligned is not served. A served set that changes — a newer snapshot
  read, the freeze set or lifted — republishes (`aligned_with` beside
  `snapshot_id` in `share_served`). A frozen collection with nothing aligned
  announces no DHT key (04 §4.2: announcing what one does not serve is a lie).
- **The digest** (`share/gossip.entries`) lists a member a snapshot lists
  only when EVERY file of it is aligned, and a bundle only when its part is
  — exactly 09 §9.9's `members`, "what this peer SERVES". This is today's
  rule for a member partly held (listed only whole) applied to alignment. A
  member partly aligned is not listed, and its aligned files are served by
  torrent all the same: a fetcher reaches them in the swarm, the frozen node
  being one of the hints every digest that lists the collection gives
  (`transport.hints_for`). The entry still names the snapshot this peer
  holds (`snapshot_id`, `snapshot_hash`), which is the truth; a frozen
  collection with no file aligned is not announced at all (09 §9.9,
  amended 2026-10-21: what cannot be taken is not announced).
- **The publisher** (the worker's `_publish_pass`, `contribute.once`)
  bundles only aligned parts, so no `bundle` event names a part of a version
  the network has left. A receiver judges a bundle against its OWN current
  snapshot's records by uid (accept.take), so a bundle of an aligned part
  is as good to it as one cut from the newer snapshot.

### 3.4 Leading

A frozen copy LEADS NOTHING (04 §4.5): `gossip.leads` is false for it, so
its digest carries no benchmark row on its account and no peer's
rendezvous waits on it. A part ranked to it by a peer on the newer snapshot
would be a part it may not hold in that version, and it takes no bundle in;
that is the stall of 2026-10-20 (the dev PC's peer leading 355 parts it
would never embed) by another road.

## 4. Where it lives

`state/node.sqlite`, the control table, beside `paused_collections`:
`frozen_collections` (the names, sorted) and `frozen_collections_history`
(every freeze and unfreeze, newest last: `at`, `collection`, `frozen`, `by`,
200 kept) — the same shape as the pause's, written by
`node_state.set_frozen`. `node_state.frozen_since(name)` gives `{by, at,
when}` or None, read creating nothing (`peek`), for the Library card and
the status display to show.

It is a **runtime choice that survives a restart**, by the same exception
as the node's mode and a collection's pause (CLAUDE.md invariant 10; 07
§7.8): no file sets it, so there is nothing to reset it to, and an expiry
would let a restart fetch the very updates the operator froze out. It is
not desired state: nothing writes it into a collection's file, and `hold
config apply` does not touch it. `hold collection rename` carries it to the
new name, as it carries the pause.

## 5. The rules it touches

- **Invariant 2 (idempotence, resumable).** Every stop is at an existing
  unit boundary: a fetch at its byte checkpoint, a scan at its committed
  batch, a members beat between files (staging kept). Nothing is marked
  done that was not committed; nothing half-applied waits on the unfreeze.
- **Invariant 5 (bandwidth).** The one new transfer, the aside pull of a
  newer snapshot, goes through `fetch.Puller`, which posts through
  `gossip._post` (the UP bucket) and accounts the answer to the DOWN bucket,
  as every pull does.
- **Invariant 9 (licence gates).** Unchanged: freeze only withholds. A
  yellow or red collection without its ack is withheld by the same gates
  whether or not it is frozen, and freezing never lets anything through.
- **Invariant 10 (config tiers).** A Tier-3 runtime override with the
  pause's exception, stated in 07 §7.8. The effective value shown anywhere
  comes from `node_state.frozen_collections` / `frozen_since`, which the
  worker, the seeder, the digest and the transport all read.
- **Invariant 11 (fidelity).** A frozen collection holds everything it
  held: nothing is pruned, dropped or rewritten, and every record keeps its
  `origin_hash` and locator. Its records stay verifiable against the origin
  form the Codex records, by the same rules as any held snapshot: where the
  origin keeps its history (dated dumps, a baseline plus updates, a
  catalogue that only grows) for as long as it does; where it replaces in
  place, `records-vs-origin` names a line the origin revised since as
  CHANGED AT THE ORIGIN, never failed, and turns inconclusive past a tenth
  (03 §3.2) — what an old snapshot already meets. Freeze adds no new case.
- **Deferral is not failure; a thread never waits unbounded.** Every guard
  returns at once; the aside pull is bounded by the pull's own timeout and
  retried after `accept.DIGEST_RETRY_S`.
- **Per-round cost.** The worker reads the frozen set once a round (one
  row); the seeder once a beat, then a stat of the aside directory per
  frozen collection; the alignment (one snapshot load) only when that stat
  or the freeze changed.

## 6. Unfreeze

Everything resumes and catches up as normal: the main loop sees the name
leave the set and asks a full discovery for it (`acquire=True`: the fetch,
then the scan — what the freeze held back, and anything a stopped fetch
left), as the resume after a pause does; the share-fetch beat pulls the
newer snapshot into the copy and its members follow; bundles waiting as
events are taken in (they were left unacted), and the digest's offers are
read again. `state/frozen/` is removed. A collection unfrozen while paused
catches up when it is resumed.

## 7. Pause and freeze together

They compose by the stricter: pause runs nothing; freeze stops updates.
Paused and frozen, nothing runs. Resumed while frozen, it runs everything
but its updates. Unfrozen while paused, nothing runs until resume, and then
it catches up. Each has its own set, history and command; neither changes
the other.

## 8. The amendments (applied)

**02 §2.7**, after "Taking a part of a collection … fewer members.":

> Freezing one is none of these either (*amended 2026-11-07*, operator: "in
> freeze you only decide that you want to keep that version in time"). A peer
> that freezes a collection keeps the version it holds and stops following
> it: no new Codex, no new `collection_id`, nothing published about the
> collection. It is the same collection, held at an earlier snapshot by
> choice, and unfrozen it catches up as any peer that fell behind (07 §7.3
> `node freeze`; 04 §4.3).

**04 §4.3**, after "Sharing is automatic":

> **A frozen copy shares what is still current** (*amended 2026-11-07*,
> operator). A collection its peer froze (07 §7.3 `node freeze`) keeps the
> version it holds: it fetches nothing from the origin and takes nothing in
> — no member, no snapshot, no bundle. Shared, it serves and announces only
> what is ALIGNED: a file a snapshot lists whose torrent (09 §9.16: member,
> path, size, root) is that file's in the newest snapshot it has read — its
> own, or a newer one a neighbour's digest lists, which it pulls and keeps
> aside under `state/frozen/`, never in the copy — and a bundle whose part
> is aligned. Its digest lists a member only when every file of it is
> aligned; a file of a member partly aligned is served by torrent, reached
> in the swarm. With no newer snapshot read, everything held is aligned. The
> digest names the snapshot it holds, as any digest does.

**04 §4.5**, at the end of the rendezvous rules:

> A frozen copy (§4.3) leads nothing: its digest carries no benchmark on its
> account, and it embeds every pending part itself, under `p2p` as under
> `all`, since it takes no bundle in; it bundles only aligned parts
> (*amended 2026-11-07*).

**07 §7.3**, the tree under `louise node` and the notes beside `pause`;
**07 §7.8**, the state-database row and the exception (the mode, a
collection's pause, a collection's freeze), and `frozen/` in the state
directory's line.

**09 §9.9**, the `benchmarks` and `members` rows: a frozen collection does
not count toward `benchmarks`, and its `members` are the aligned ones (04
§4.3). These are rules about which entries are present, in the fields and
meanings as they stand.

## 9. Wire format and schema: none changed

- **Wire**: no message, field or shape changes. The digest carries fewer
  members, under its own definition ("what this peer SERVES"), and no
  benchmark row on a frozen collection's account, under its own rule
  ("for the collections it embeds under rendezvous and shares"). The aside
  pull is the existing `pull snapshot`.
- **Schema**: none. Two keys in node.sqlite's existing `control` table, and
  one local directory, `state/frozen/`, holding snapshot files as pulled.

Considered and not done, each needing a wire change, so asked before built
(CLAUDE.md):

| | pros | cons |
|---|---|---|
| list a partly aligned record member in the digest with its `files`, as a bundle's are (09 §9.9) | a fetcher's deferral rule (`offered_by_neighbours`) would see the frozen node's aligned files even when it is the only one serving them | a digest field whose meaning changes for record members; every receiver's reading of `members` reviewed |
| announce the newer snapshot's swarm key from the frozen node | a fetcher that finds swarms by the DHT alone would reach the aligned files | announcing under a snapshot the node does not hold (04 §4.2 ties the three keys to "every snapshot it serves") |

Status quo, built: neither; the aligned files are reached in the swarm
through the digest's hints.

## 10. Open: a question for the operator

**What it is:** whether a frozen collection takes in other peers' vectors
(bundles) for the version it holds — vectors only, its records unchanged.

| choice | effect | pros | cons |
|---|---|---|---|
| **A — no bundle intake while frozen** (built; the task's reading of "no updates are fetched") | only this node's own encoder adds vectors | the copy stays exactly as frozen; nothing of anyone else's arrives | a node frozen because it has no encoder gets no vectors for what is pending |
| B — bundles whose records are the ones held are taken in (the ladder's T0 already checks it, accept.take) | vectors from peers fill the version held | the operator's own example ("no embedding engine available") gets its vectors | the vector table changes while "frozen" |

Recommendation: B, since a bundle that passes T0 here describes the
version held and is no update of it. If the operator says nothing, A stays.

## 11. What the Library card and the status display should show

Left to the worker building app/status.py (not touched here). From
`node_state.frozen_since(name)`:

- a badge **Frozen** on the card, with "since `<when>`, by `<by>`" (both
  may be None for an entry before the history: then "frozen", no date);
- one sentence: "Kept at snapshot `<id>`: nothing is fetched or taken in;
  it is parsed, indexed, embedded here and searched as it is." When shared,
  "Shared: only the parts still current upstream" — with `share_served`'s
  `aligned_with` (the newer snapshot read, or none) and its torrent count;
- the action **Unfreeze** (`louise node unfreeze <name>`), beside resume;
- in `node show`, `frozen_collections` beside `paused_collections`, with
  who and when, as the pauses are.
