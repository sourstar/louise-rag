# Open issues for the P2P architecture review (2026-10-19)

*Collected on the day the Perugia peer (louise-peer2, RX 9060 XT) joined
the network and tried to contribute to gutenberg. Input for the operator's
dedicated review session, beside [`vector-convergence.md`](vector-convergence.md)
and [`vector-convergence-review.md`](vector-convergence-review.md). Nothing
here is decided; each item names what was measured.*

## 1. Vectors a peer already holds are invisible to the network — the largest

**Measured.** The node holds local vectors for **90.9 % of gutenberg**
(9,484,067 of 10,433,676 chunks; 72,176 of 79,680 documents), embedded
under `embed = "all"` before 2026-10-19. None is in a bundle, so every
coverage map — which counts accepted bundles only (02 §2.5, 09 §9.10) —
shows gutenberg 0 % covered, and rendezvous (04 §4.5) assigns its parts to
whichever peer scores highest. The Perugia peer's first `share contribute`
pass built 3 bundles, 1,601 chunks; the node already held **1,186 of them
(74 %)** locally (`lang=af/00001` 1,116 of 1,376; `lang=arp/00001` 64 of 64;
`lang=bg/00001` 6 of 161). Contributing to gutenberg today would be ~91 %
re-embedding. Contribute is paused on the peer until the review.

**Why.** `share contribute` reuses a peer's local vectors when it builds a
bundle (`vectors_for`), so bundling held vectors costs no GPU — but only for
the parts THAT peer leads. Who EMBEDS a part is rendezvous's question; who
BUNDLES vectors already held is governed by nothing.

**Direction to decide (RULE 1: keep provably identical work).** A holder
bundles and announces what it already holds, whoever leads the part;
rendezvous then assigns only what no one holds (~950,000 gutenberg chunks).
Open: how a holder's bundles of non-led parts interact with the leader's
window and the cascade; whether genesis-style "coverage declared by a
holder" is the better form for a whole collection's worth of vectors.

## 2. The embed stage ignored rendezvous — fixed, deployed

`embed = "all"` embedded every part, beside peers contributing the parts
they lead. Option 1 (the node's gutenberg embed set to on-demand, cron jobs
5/6 disabled) is live; option 2 (`share.leading`: the stage leaves another
live peer's parts to it, 45535ea + 2e28bbf, reviewed) is deployed on the
node as f85a606 since 2026-10-19 10:34, so the review runs on the latest
code (operator). The Perugia peer still runs the image before it. Numbers in
scheduling-log.md (2026-10-19).

## 3. A missed attestation cannot be recovered

Attestations travel only as events (04 §4.2). The Perugia peer joined after
the node's 2026-10-11 checks and could not contribute: `share contribute`
needs a `records-vs-origin` attestation for the snapshot. `share attest`
cannot republish a held attestation without rerunning the check, and
`/api/share/attestations/fetch` has no client. Worked around by publishing
the node's held run through `attest.publish` (9 events, 2026-10-19).

## 4. A peer's address is only the one it declares

A neighbour is recorded at the address its own digest declares (09 §9.9),
and torrent hints use it. The node declared its LAN address; a peer off
that LAN could not fetch anything until the node's `[node] address` was
changed to its Tailscale address (2026-10-18). A hand-given, pinned
address for the same key is not used. Candidate: a pinned address wins over
the self-declared one (amends 04 §4.2 / 09 §9.9).

## 5. One DHT announce per seeded torrent floods a home router

The node's ~3,170 seeded member torrents each announced on the DHT: ~10k
UDP flows filled the FRITZ!Box NAT table and took the house offline
(2026-10-19). The node's fix (member torrents with `disable_dht|disable_lsd`,
one per-snapshot swarm key) is 0cf9d68, deployed in f85a606: DHT back on at
the node since 10:35, 73–110 flows in all against 14,194 before
(dht-flood.md). Seeding is now LIMITED by the UP budget, not only counted
(c455393); the node's `[budget] up_max` is 20 Mbit from 17:00. The Perugia
peer keeps its DHT off until it runs that code.

## 6. A served slice list can silently stall a fetch

The node served gutenberg's `lang=la` and `lang=eo` only (`share_slices`);
a peer fetching every slice waited forever on the first part nobody served
— metadata comes from the snapshot, so the 120 s "no metadata" bound never
fires. Fixed by serving all slices (2026-10-19); the stall itself is open:
a wanted part that no neighbour's digest offers should be deferred and
said, not waited on.

---

## The review (2026-10-19, the operator's dedicated session)

*What was decided and built for each item, with its commit on the review
branch; every amendment to a numbered document is dated 2026-10-19 in
place. Measured facts came from the node (München session: f85a606 live,
DHT on since 10:35, 49–110 conntrack flows) and the Perugia peer (image
from 6934e99, contribute paused, gutenberg complete). Tests: the full
suite on the branch tip (3137ab4) in the `louise-rag:local` image, run by
the Perugia session in a separate worktree: 4,660 passed, 3 skipped, 0
failed (786 s). In the review's own container it had passed but for that
host's disk floor — 27–29 GiB free under app/disk_guard.py's 32 GiB
refused every vector import in tests/bundles and tests/integration — and
two tests that fail on the base commit there too (no `rsync` binary; the
web subprocess cannot bind); the image has both.*

### 1. Held vectors are invisible — DECIDED: a holder bundles what it holds (8d3f40d)

The node had 9,536,499 of 10,477,214 gutenberg chunks locally (91.0 %) and
25 of 1,637 parts covered (1.2 %) after a night of `share contribute`,
because contribute bundled held vectors only for the parts it LED.
`contribute.once` now walks the open parts this peer does not lead first
and exports what it holds for each — the challenge rows are the only GPU;
one read of each open part's records is the only I/O — then builds the
parts it leads. 04 §4.5 amended ("What a peer already holds, it bundles").
The first bundle is the claim, so a holder's bundle closes the part for
the leader too; a partial holding leaves the rest to the leader, whose
`vb.build` embeds only what still lacks a vector after the import. No
change to the cascade: a holder's bundle does not renew the leader's
window, it closes chunks.

**Perugia's first pass on 39fd6e2** (12:09–12:55 CEST, stopped for the
hold of §8): 932 open parts probed, 640 led by this peer, 0 held, 60 left
to the fetch (a neighbour offered a bundle for each), 4 bundles built and
announced (2,957 chunks: lang=bo/00001, br/00001, ca/00002, ceb/00001),
2,772 s. The 640 led parts are parts the NODE holds vectors for: at pass
time the node's holder pass had bundled 385 of its ~1,470, so Perugia
embedded four parts the node had — the transition's race, "duplicates are
checks". Perugia's contribute resumes after the node's holder pass is
through, and is the measurement of what rendezvous leaves to a second GPU
once every held part is offered.

**The node's first holder pass, measured** (München; 39fd6e2 then
0292807, 12:09–13:13 CEST, SIGTERMed by the deploy, exit 130): 422 open
parts probed, 422 held, 431 bundles accepted = 2,466,415 chunks, 0 chunks
embedded ("chunk(s) to embed" lines: none), 0 refused, mine 693 (the
parts rendezvous gives the node among 3 live peers), ~6.7 bundles a
minute. Resumed at 13:14; the remaining ~1,040 held parts take about
2.5 h more. The prediction stood: tens of minutes per few hundred parts,
no GPU but the challenge rows. The residual probe cost per pass (the
parts the node holds nothing of) is measured once the pass is through.

**Seeding after the fixes (München, 0292807, 13:14 CEST):** first publish
"5118 torrent(s) of snapshot 2026-09 served (1948 new)" = 3,170 + 4 × 487
bundles; 35 s later "5126 served (8 new)" — two bundles contribute had
just made: incremental. Warm: `bundle_members` 495 bundles in 0.23 s
(was 8.1 s); `gossip.entries` 2.15 s; offered-not-served 6 (the bundles
of the last minute — `BUNDLE_SCAN_S`'s lag, by design), served-not-offered
0. No failed digest since the start.

**The holder pass, complete** (München, 13:40–15:20 CEST, pass 3): 713
probed, 709 held, 715 accepted, ~7 bundles a minute, none embedded; today's
three passes made 1,296 holder bundles, 1,378 gutenberg bundles on tank.
Two bundles failed the node's own ladder and were kept unannounced (their
parts and reasons: München's log; to record). Then the mine phase: 693
parts the node leads among 3 peers, each partially held — the node had
91 % of gutenberg's vectors, so a holder bundle covers a part only as far
as the node had embedded it, and the coverage view's "1,139 open" after
the pass means 1,139 parts with SOME chunks lacking a bundle, not 1,139
uncovered parts; their leaders embed the rest.

**A redundancy found and fixed (dd58729), measured by München on the
node:** 418 parts carried TWO accepted bundles of this node's, 415 pairs
with identical chunk counts (`lang=af/00001` 1,116 and 1,116; `de/00001`
4,778 and 4,778; …) — 2,491,650 redundant chunks, 12.1 GiB on tank, all
published. Cause: the holder pass bundles every OPEN part it holds vectors
for, and a partially held part (the node had 91 % of gutenberg's vectors,
spread over most parts) stays open after its holder bundle, so each pass
after a restart (39fd6e2 → 0292807 → 6d3084b) bundled the same held
records again under a new id (`created_at` is in the signed header). My
first reading — the mine phase re-shipping a holder bundle's chunks — was
wrong: the parts the node leads were never holder-bundled, and a built
bundle is such a part's first and whole one (en/00081, 9,349 chunks, one
bundle). The fix is the same rule either way: an export leaves out the
records this producer's accepted bundles for the part already claim
(`possession.json`), so a re-run logs "every record held is in a bundle
made here already" and makes nothing; München confirms on the first pass
after 8f6d99f. The 418 later duplicates stay published for now —
"nothing published is deleted" — and are the operator's decision, below.

**Two bundles refused by the node's own ladder:** both `lang=en/00043`, at
T0, in passes 1 and 3; the log named the tier and not the clause.
Hypothesis (München): a record embedded only in part (a book interrupted
mid-way) whose held chunk set is not what `chunks` derives — T0's
per-record rule; the mine phase's built bundle for the part (13,651
chunks) was then accepted. The refusal log now names the failing clause
and the first refused row.

**Open, the general case: the digest's size.** The node's digest entries
grew from 64,938 to 409,873 bytes of canonical JSON with 599 members and
482 coverage rows for gutenberg, heading for ~1.3 MB at 1,600 bundles —
each bundle member carries its four files' roots (09 §9.9, so a fetcher
computes the torrents without asking). Hourly per neighbour, under the
UP budget's DELTA class and `gossip.TIMEOUT_S` = 10 s per post. To
measure when the pass is through: the post time to Perugia at the full
size. If it matters, the standard remedy is `Content-Encoding: gzip` on
the POST (RFC 9110) before any change to what a digest says; hex digests
compress about 2×.

**The general case, not two peers** (operator, 2026-10-19): with many
holders joining a network, each bundling what it holds would make as many
versions of every part. So a shard whose bundle a live neighbour both
claims (`coverage`) and offers (`members`) is left to the fetch beat,
which takes that bundle in and compares it with the vectors held here
("duplicates are checks"); a version that exists and is served is reused,
not remade — RULE 1 at network scale, and one version per part by
construction wherever one exists. A claim nobody serves does not stop a
holder (§7). 04 §4.5 says so.

Open, unchanged: whether a whole collection's worth of held vectors
should instead be declared as genesis-style coverage (one statement, no
1,600 bundles) was not taken up — bundles are what travel, and 2,400 per
collection is 04 §4.5's budget.

### The principles, checked (operator, 2026-10-19: RULE 1, no wheel reinvented, standards, the general case)

- Nothing new on the wire: the attestation pull is 09 §9.9's existing
  message; bundles and attestations are found in the digest's existing
  `members` and `coverage`; the stall and the deferral are a client's
  policy over libtorrent's swarm, which keeps its own peer timeouts.
- Bytes are reused, never remade: held vectors become bundles; a served
  version is fetched rather than duplicated; nothing is re-embedded.
- Standards: BitTorrent v2 torrents and the mainline DHT carry the bytes
  as before; the convergence proposal maps to RFC 9745 / RFC 8594 and
  stays a decision for after the measurement.
- Scale: every new per-beat cost is bounded by digest arrivals or a
  backoff, never by the corpus; the one cost to time on the node is the
  first contribute pass (above).

### 2. The embed stage ignored rendezvous — closed before the review (f85a606)

### 3. A missed attestation cannot be recovered — FIXED (d072051)

`pull attestations` (09 §9.9) had a server and no client. Now: `share
fetch` pulls the sharer's attestations with the collection; the worker's
gossip beat pulls once an interval per collection from the neighbours
whose digest lists it, from the newest attestation held less one
interval (`attest.pull_due`); `share attest --republish` publishes this
key's held, never-published attestations without rerunning the check.
07 §7.3 amended. What stays true: a leecher with no origin copy cannot
run `records-vs-origin` itself (Perugia: *inconclusive*, "no origin to
re-fetch from"); it contributes on the node's attestation, as 04 §4.5
allows ("its own or another key's").

### 4. The declared address — FIXED: the pinned address wins (176be6c)

`peers.record_digest` records a digest from a pinned key at the address
`[peers.pins]` binds to it; the digest is kept whole and `share peers
show` names the declared address beside the recorded one. Digests,
events, pulls and swarm hints all read the recorded address, so one pin
fixes all four. 04 §4.2 and 09 §9.9 amended. Perugia had the pin and the
node still had to change what it declares; with this, the pin alone
suffices.

### 5. The DHT flood — closed before the review (0cf9d68, f85a606)

### 6. A served slice list stalls a fetch — FIXED, three ways (f8d708b)

Root cause, from Perugia's record: `fetch_file` counted one connected
peer as "answered" and the metadata bound never fired; the beat never
returned; the control row kept the previous beat's account. Now:
- a second bound, `STALL_TIMEOUT_S` = 300 s with no byte after a peer
  answered — "stalled"; `nobody_served()` names both phrases for the
  beats' backoff;
- a wanted member no neighbour's digest offers, when the swarm key found
  no swarm either, is **deferred and said**, once per member, never
  waited on; the beat backs off until a digest offers it or names a
  swarm (04 §4.3 amended);
- the beat writes a `running` control row before the fetch, so a stuck
  beat is visible;
- and the cause on the node's side: `gossip.entries` listed every member
  held while the seeder served two slices. The digest now offers what
  `seeder.serves` says (09 §9.9 amended: `members` is what the sender
  serves).

### 7. A bundle announced from a peer that serves nothing — NEW, FIXED (8d3f40d, a990821)

Found by the München session during the review: Perugia's digest
listed coverage for 2 bundles and `members: []`; its bundle events sent
the node into 120 s "no metadata" waits (10:38, 10:40). Two causes, both
fixed:
- `share fetch` wrote `share = false`, against 04 §4.3 ("sharing is
  automatic after a completed fetch unless the file says `share =
  false`"); it now writes `share = true` for a public collection. **The
  Perugia peer's existing file still says false: run `louise share
  gutenberg` there once** (or edit the file), and its records and
  bundles are served from the next seeder sync.
- `share contribute` announced a `bundle` event whatever the peer
  served. It announces only where this peer serves the collection
  (`share = true` and a mode that seeds, `louise._serves_here`);
  otherwise the bundle is accepted here, listed under the digest's
  `coverage`, and offered under `members` once this peer serves. 04 §4.5
  amended.
- And the recovery path for a missed `bundle` event, the same gap as §3
  for vectors: `accept.from_digests` takes in every bundle a neighbour's
  digest offers (a `bundle-<space>-<id>` member with its files) that
  this peer has not accepted, through `accept.take` as the event would
  have; digests are re-read only when one arrived or after
  `BACKOFF_S`, and a bundle nobody served waits that long.

### 8. Bundles made after the publish were never seeded — FOUND LIVE, FIXED

Measured by the München session on 39fd6e2, minutes after the node's
deploy: the digest offered 331 `bundle-…` members (that day's `share
contribute`), and the seeder served none — its published list was the
3,170 torrents of 2026-10-18 22:10, before any bundle existed.
`seeder.sync` published only when the snapshot id or the selection
changed, while `gossip.entries` read the held bundles live. Every fetch of
a node bundle was a 120 s "no metadata"; Perugia's deploy was held before
it took the offers in. Pre-existing — it is why the node's fetch of
Perugia's bundle failed too (§7) — and exposed the moment bundles were
offered and taken in from digests. Fixed:
- `publish` is incremental within a snapshot: an entry published before
  whose torrent and link are in place is kept, not hashed again; a bundle
  landing costs its own four files, never gutenberg's 3,170;
- `sync` lists the held bundles at most once a minute per collection
  (`bundles_fingerprint`, a listing and stats, no hashing) and publishes
  when the set differs from the published one; the control row records
  it. Test: a bundle landing after the publish is served a minute later
  with four files hashed and the records not again; the digest offers
  exactly what is served; a removed bundle's torrents go.
- And the second half, measured by München on the first fix (7d23449):
  `bundle_members` hashed all four files of EVERY held bundle before the
  incremental check could keep anything — 385 bundles, 10.7 GiB, 8.1 s a
  call warm, on every publish AND every digest (pre-existing there),
  heading for 55 GB and 40 s a minute while contribute produces bundles;
  a cost that grows with what is held, never with the work. The roots are
  now cached under `state/share/bundle-roots.json`, keyed on each file's
  physical identity (path, size, mtime_ns): a bundle is hashed once, again
  only when a file moved; a directory gone leaves the cache; a cache that
  does not read is recomputed. Test: two bundles, eight hashes; a publish
  and a digest, none; a third bundle, four; a moved file, its bundle
  again; a broken cache, everything once.

**Storage — DECIDED (operator, 2026-10-19) and BUILT:** the bundles rested
in `collections/<c>/state/bundles` on the node's SSD — 9.7 GB at 5,206 B
per chunk, about 55 GB for gutenberg alone, beside the same vectors in
Lance. They move to tank: a dataset `tank/apps/louise-bundles` mounted as
`/bundles` (worker rw, web ro, as `/shards`), gutenberg's directory moved
by hand (München: rsync, verify, `state/bundles -> /bundles/gutenberg`),
and the code half the way `SHARDS_ROOT` works: with `BUNDLES_ROOT` set,
`create` links `<name>/state/bundles` to `<BUNDLES_ROOT>/<name>`; `rename`
and `forget` follow the link; `accept.take` lands a bundle across
filesystems whole or not at all (copy beside, one rename); `dev
adopt-bundles <collection>` moves an existing collection's bundles —
sizes and times kept, so the root cache holds; verified byte for byte;
the old directory set aside as `state/bundles.moved-<time>`; a scratch of
a dead export not carried. 07 §7.8 amended;
deploy/16f/flatten_mounts.py knows the mount and the variable.

**Deployed on the node 13:40 CEST** (München; 6d3084b = cab58c7's code;
full suite there: 4,695 passed, 1 skipped): `BUNDLES_ROOT=/bundles` in web
and worker, set by flatten_mounts.py; `adopt-bundles` answered "linked
already" for gutenberg and "linked" (0 files) for normattiva, pubmed-v8,
wikipedia, gutenberg-same-work and gutenberg-works; tank/apps/louise-bundles
16.4 GB, 655 gutenberg bundles; the 14 GB SSD copy deleted (operator).
First publish after the restart: "5790 torrent(s) … served (8 new)" =
3,170 + 4 × 655 — every earlier entry kept across the restart, none
rehashed. Holder pass 2 (13:14–13:39, SIGTERMed by the deploy): probed
165, held 165, accepted 168; 655 holder bundles in all so far. The pass
runs continuously until 17:00 (operator: heavy lifting allowed until then).

**Perugia's DHT, measured** (39fd6e2, 10:10–11:00Z, inside the worker
container, 1,637 member files + 7 bundles): udp 1, udp6 0, tcp 2–6 — no
flood on the peer either.

### 9. A digest refused at a restart cost an hour — FIXED (Perugia's find)

The peer's first digest after its redeploy (10:07:13Z) hit the node while
the node itself restarted: `ConnectError`, counted as the interval's
send, and the next attempt reached the node at 11:07. With many peers,
restarts are the common case. An address the digest did not reach is now
retried after `RETRY_S` (60 s), then two, four, eight minutes, capped at
the interval; a peer that stays down costs one refused connection per
step. Test: the unreached peer gets the digest a minute on, the reached
one not again; a peer that stays down is tried at 60, 120, 240, 480 s.
The attestation pull keeps its hourly cadence (04 §4.2: a pull that does
not connect is retried at the next interval; it is a recovery path).

### 10. The embed drop-down: all local / all p2p, and sharing as its own axis — BUILT (step 1)

Operator's design (2026-10-19), from the question "is a p2p embed mode
planned?": the Library's `embed` drop-down reads `off`, `on-demand`, `all
local`, `all p2p`; the file and the API carry `off | lazy | all | p2p`.
`all` embeds every part itself again, whoever leads it (its duplicates
are checks); `p2p` is the partition of 04 §4.5 — the parts rendezvous
gives this node, with the cascade — which this morning's deploy had put
under `all`. Sharing is the other axis and independent: with sharing on,
everything the node holds is served and bundled whatever `embed` says;
every node takes peers' bundles in. The one meeting point: `p2p`
publishes, so it needs sharing — the page asks (two confirmations:
setting `all p2p` with sharing off enables sharing; disabling sharing
under `all p2p` switches to `all local`; proceed or abort), the server
writes both in one file rewrite, and a hand-edited file saying `p2p` with
`share = false` runs as `all` and its card says so. The card shows the
two axes beside the drop-down: the bounty list (open parts, parts this
node leads and leaves to peers, from `contribute.leaders`, cached 120 s)
and availability (chunks bundled here, chunks taken from peers, from the
coverage map). 07 §7.8 and 04 §4.5 amended. The worker's six "backfill
under all" checks accept both modes; the partition filter runs under
`p2p` only. Step 2 — publishing inside the worker, retiring the cron and
the pause — stays open (scheduling.md §6 first).

### 11. 418 duplicate holder bundles on the node — DEPRECATED, not withdrawn (operator's design)

München's measurement of the first holder day: the pass bundled 418
partially held parts a second time after each restart — a new id each
time, `created_at` being in the signed header, the same `content_digest`
— 2,491,650 redundant chunks, 12.1 GiB on tank, and every peer taking the
node's offers fetches both copies to find them identical. dd58729 (§1,
"exclude claimed records") stops it for the future.

For the 418 I first built 03 §3.6's `withdraw` (763a309: `app/share/
withdraw.py`, the receiving side at the beat, `tests/share/test_withdraw.
py`). The operator then set the design: a redundant copy is VALID, so it
is **deprecated**, not withdrawn — "even when a seeder is holding more
equivalent copies, only one must be without the deprecated tag; keep the
deprecated copies for a while, allowing a leecher to decide whether to
complete the fetching or drop it; a leecher must ignore deprecated
copies for new fetchings; think about the corner case in which a
non-deprecated piece is not available anymore but deprecated ones still
are"; retention one day ("we have a lot of deprecated copies to get rid
of"). `withdraw` stays as built for its spec'd case (a faulty bundle;
peers drop it and its vectors; no command yet, `hold vectors check
--drop` is the spec's); the CLI `dev withdraw-duplicates` was removed
before anyone ran it.

Built (`app/share/deprecate.py`; 03 §3.6, 04 §4.5, 07 §7.3, 09 §9.8 and
§9.9 amended):

- **The statement.** `dev deprecate-duplicates <collection> [--dry-run]
  [--yes]`: per part, this producer's accepted bundles sharing a
  `content_digest`; the earliest is the CURRENT copy; one `deprecate`
  attestation (`check` and `result` `deprecate`, `detail.bundles =
  {deprecated_id: current_id}`), signed, kept at the attestation path,
  published as an event. Nothing is removed by the command. Peers derive
  "deprecated" from the attestations they hold (`deprecated()`, cached
  with them); only the producer's own signature counts (`by_producer`).
- **The seeder.** Keeps serving deprecated copies; its digest lists them
  under `deprecated_members`, not `members` (`gossip.entries` →
  `split_members`; a stranger's deprecation changes nothing). After
  `KEEP_S` = 1 day a holder removes a deprecated copy at its fetch beat
  only where it also holds the current one (`expire`; vectors untouched,
  the current copy covers them). A peer whose only copy of the part is
  deprecated keeps it indefinitely and serves it as deprecated: it is its
  coverage, and the network's fallback.
- **The leecher.** `listed_bundles` never asks for a `deprecated_members`
  entry while a live neighbour offers the current equivalent under
  `members` and it is not held here; a stale `members` entry for a
  deprecated id is treated the same. **Fallback** (the operator's corner
  case): a deprecated copy is listed, flagged `deprecated: true` with its
  `current`, when no live neighbour offers the current one, or the
  current's last fetch came back nobody-served (`accept._nobody_served`,
  4 × BACKOFF_S). **In flight:** `take` finishes a fetch of a bundle
  deprecated meanwhile when at least half its bytes landed (`staged_
  fraction`), drops the partial otherwise or when the current copy is
  held here; the fallback's listing is a fetch like any other.
- **Counting.** `coverage.shards_view(redundant=)`: a deprecated copy
  accepted beside its current one counts toward nothing
  (`redundant_here`), threaded through `show`, `leaders`/`open_shards`,
  `leading.skip_test`, the Library card and `hold vectors show`. The
  defect it fixes is older than deprecation — see the open points.

Tests: tests/share/test_deprecate.py (7), test_coverage unchanged.

**To run on the node, once, under the pause:** `louise dev
deprecate-duplicates gutenberg --dry-run` (the 418 pairs, nothing
written), then `--yes` with LOUISE_PLATFORM_IMAGE set. The copies leave
the digest's `members` at its next send, Perugia stops asking for them,
and the node removes them at the beat after one day. Tank gets its 12.1
GiB back a day later, not at once.

**Open points of the deprecation design** (operator: "log any issue
about deprecated design for further improvement"):

1. ~~*Equivalent bundles from DIFFERENT producers inflate `covered`
   too.*~~ FIXED the same evening, found live by Perugia (§14): the
   count is the union of the accepted bundles' chunk ids
   (`app/share/counting.py`), cached per part on its bundle set.
2. *A deprecated copy held alone is kept and served forever.* Nothing
   prunes it once the current copy is widely held; that needs a view of
   who holds what across the swarm (the digests' `coverage` gives it)
   and a rule for "widely".
3. *The fallback's two signals are coarse.* "No live neighbour offers the
   current" misses a live seeder that lists it and does not serve it
   until one nobody-served attempt (BACKOFF_S later); the nobody-served
   memory is per process (lost at a restart) and lasts 4 × BACKOFF_S.
4. *The half-landed rule is a heuristic on file sizes.* libtorrent sizes
   a file before filling it, so `staged_fraction` is an upper bound; a
   fetch that is 50 % sized and 10 % written is finished. Piece-level
   progress from the session would be exact.
5. *A deprecated copy whose attestation a peer has not read yet is never
   fetched* — not even as a fallback (it cannot know the equivalence).
   The hourly pull closes the gap within an hour; a `deprecated_members`
   entry could carry the current id to close it at once (a wire
   addition).
6. *One hop only.* If the current copy is itself later deprecated or
   withdrawn, chains are not followed: a leecher falling back to copy B
   whose current A is deprecated for C fetches B when C is unavailable
   only if it holds both attestations and A is not offered.
7. *Older readers* never see `deprecated_members` and so never fall
   back; a stale `members` listing by an older seeder is handled.
8. *`KEEP_S` is a code constant* (one day), not in the collection file;
   the digest grows by the deprecated entries (418 × ~200 B) for that
   day.
9. *The producer's own `expire` needs the beat*: on the node the worker's
   beat runs `expire`; the cron CLI does not. The 418 go at the first
   beat after the window, not at a fixed time.
10. *`redundant_here` costs two point queries per deprecated id* (in
    chunks of 400) at each `shards_view` rebuild — 836 rows for the
    418, once a REBUILD_S at most; fine today, a list-sized cost by
    the engineering rules if deprecations pile up.

### 12. A led part a neighbour already offers was re-embedded — FIXED (Perugia's find)

Perugia's first contribute pass under 6d3084b (15:39–16:04 CEST, 1,500 s):
`probed 780, mine 636, left_to_fetch 156, held 1, built 576 chunks`. Its
probe afterwards: 632 of the 636 `mine` parts had a neighbour's bundle
offered — a fetched, unaccepted coverage row — and only 4 were truly
unheld. `open_shards` counts a part open until a bundle is ACCEPTED here,
and §1's "one version where one exists" check (`offered_elsewhere`) ran
only for parts led ELSEWHERE: a led part whose bundle the node's holder
pass had offered an hour before went to the GPU. Fixed in
`contribute.once`: the check runs for both lists; a led part a live
neighbour both claims and offers is `left_to_fetch` too, and the fetch
beat takes it in; a part with a claim nobody serves, or none, is built as
before. Test: `test_a_leader_leaves_a_led_shard_whose_bundle_a_neighbour_offers_to_the_fetch`.
Perugia stopped contribute at 16:04 (SIGTERM, `interrupted: true`, 07
§7.7) until this is deployed.

Perugia's intake since the operator's `ack --origin-unchecked` (13:27Z):
116 bundles, 665,823 chunks by 13:52Z; the first hour 287 bundles,
1,687,191 chunks — 4.8 bundles/min, ~28 k chunks/min, no deferred,
stalled or refused lines, 12–19 TCP sockets; the node's digest 1,096,042
bytes. Coverage at 13:52Z: 1,255 offered-unaccepted rows over 881 parts,
114 fetched and accepted, 8 local — every accepted row stamped
13:27:44Z, the beat's clock, while the log shows them landing one by one
over the hour. Fixed with it: `take` and the contribute pass stamp a row,
and date its `bundle` event or `vectors-sample` attestation, at the
moment of the judgement, not with the beat's or the pass's `now` (the
node's holder pass ran for hours; 04 §4.5's grace window reads the
leader's last bundle from that stamp).

### 13. The contribute build left `embed_done = 0` — FIXED (München's find)

München, read-only on the node: 372 in-scope gutenberg documents have
every chunk in Lance (363 with rows == chunks, 47,148 chunks) and
`embed_done = 0`, `updated_at` from September. `share contribute`'s build
(`hold.vectors_build.build`) writes vectors through a callable and never
touched `documents` — the worker's embed stage marks a document in its
own closure. Consequences: under `all local` the worker embeds them again
(GPU, and a delete-then-insert of the same bytes); the Library's "7,471
more would embed" is off by 372; a query's lazy path may re-embed.

Fixed: `app/embed_marks.py` is the one place a document is marked
embedded (`mark_embedded`: `embed_done`, `embed_version`, `updated_at`,
the imported-provenance rows and the queue entry gone; a versioned
collection's rows marked synced only when the writer carried the columns)
— the worker's closure wraps it. `share contribute` marks, after each
shard's build, the records whose every chunk now has a vector
(`complete_uids`: the chunks through the layers, the vectors asked of the
table, never `documents.chunks`), interrupted or not. The one-off
reconcile for the 372: `louise dev mark-embedded gutenberg [--dry-run]`
(07 §7.3). Its first form looked the chunk ids up per shard — 1,600
lookups over a 10.5 M-row Lance table; München's dry run ran 54 minutes
at 124 % CPU, 3.3 GB RSS, and was stopped — a cost of shards × table.
Now ONE scan of the table's `id` column keeping the candidates' ids
(`embed_done = 0`: 7,8xx documents on the node), then the shards once
(carry.py learnt the same lesson on pubmed: ~16 h of lookups against a
minute's scan). Run on the node (München, 18:44 CEST, 457077b): dry run
candidates 7,471, rows 9,581,988, shards 1,637, complete and unmarked
429, 117.5 s; the run under the pause marked 429 in 74.1 s — 429 rather
than 363 because the 16:00–17:00 mine phase had completed more documents
since the count. Tests: tests/share/test_contribute.py,
tests/hold/test_embed_marks.py.

The node's deprecation run (München, 17:23 CEST): the dry run counted
421 copies over 418 parts, 2,461,390 chunks — three parts (en/00013,
en/00062, en/00221) were held as two bundles each, both duplicated, and
München's hand count had summed their second originals too. One
attestation (`sha256:ceb60b9f…`, published as event `sha256:6628720b…`),
served until 2026-10-20T16:20:40Z. Digest after: members 1,092,
deprecated_members 421, coverage 973, 1,087,077 bytes. Tank unchanged
(35.1 G used, 7.46 T free): the copies go a day later.

München's other finding: the en/00043 T0 hypothesis (a chunks = 0 record
in `possession.json` with no rows) does not hold — `export.grouped` skips
a record it holds no row for, so such a record is in neither. The
refusal stays unexplained; f3d2633's log line names the clause if it
recurs, and en/00043 has an accepted built bundle now.

### 14. The held path re-signed another producer's rows; the coverage sum closed parts over a gap — FIXED (Perugia's find, BLOCKING)

Perugia's first pass on 9412219 (18:22 CEST) accepted and announced 44
"held" bundles and embedded nothing: their vectors were the node's,
landed from its bundles. lang=en/00160 (6,607 chunks): the node's pair
of 6,327 each and Perugia's 68a8b614… of the same 6,327 and the same
uids — a third copy, re-signed. Two defects:

1. **The held path** excluded only THIS producer's accepted bundles' uids
   (`claimed_uids`), so rows landed from another producer's bundle were
   exported as one's own. Now the uids of EVERY accepted bundle held for
   the part are excluded, whoever produced it: a holder bundles what it
   embedded and nobody bundled. The pass JSON's `claimed` lists the
   probed parts that held nothing to bundle for that reason.
2. **The count.** `shards_view` summed the chunks of every accepted
   bundle and capped at the part: 6,327 + 6,327 → 6,607, the part
   closed with 280 chunks embedded by nobody, and rendezvous would never
   assign it again. 9412219's `redundant` set covered a producer's own
   deprecated copies only. Now a part with several accepted bundles is
   covered for the UNION of their chunk ids, read from the held
   `vectors.parquet` files (challenge rows left out) and cached per part
   on its sorted bundle ids (`state/share/coverage-union.json`); a
   bundle whose files are not here counts for nothing (open is noise,
   closed over a gap is a loss). `counting.view_kwargs` hands `redundant`
   and `union` to every view: `show`, `leaders`/`open_shards`,
   `leading.skip_test`, the Library card, `hold vectors show`.

Measured on the node (München, 0118423 deployed 19:20 CEST): `hold
vectors show gutenberg` before, on the sum — covered 5,714,993 of
10,477,214 chunks, 1,479 parts open, 158 closed; after, on the union —
covered 5,677,748 (−37,245), 1,523 open, 114 closed: the 44 parts the
sum had closed over Perugia's copies are open again. The first `show`
read the multi-bundle parts' files once, 26 s; the second, 1 s from the
cache.

Perugia's 44 copies: `dev deprecate-duplicates` now also finds this
peer's bundles whose CHUNK rows equal another producer's earlier bundle
(`deprecate.foreign_duplicates`: identical `possession.json`, then the
chunk rows' digest — `content_digest` differs, each producer embedding
its own challenge rows) and `publish` accepts a current copy of another
producer. Perugia runs it once the fix is deployed there; the node, which
took the 44 in as duplicates (compared, not stored), removes them at the
beat after the window as it holds the originals.

Two more from the same pass: `share contribute --once` returned 0 after
SIGTERM (07 §7.7 says 130) — fixed; and the process spent 15 minutes
after its last log line, pure Python, sys-heavy, RSS growing 4 MB per 5
s, with no py-spy in the image to name the frame — `louise` now
registers SIGUSR1 to print every thread's stack (`faulthandler`), so the
next such wait is named, not guessed. Perugia's `mine 635 /
left_to_fetch 34` after the 16:05 probe's 632 offered: by 18:22 the
intake had accepted most of the node's offers, so the open led parts
left are the ones the node covers only partially — their completion is
the leader's work, small per part (the missing chunks only), and now
counted right.

### Still open after the review

- **Vector convergence** ([vector-convergence.md](vector-convergence.md),
  [vector-convergence-review.md](vector-convergence-review.md)): not
  decided here. The review's recommendation stands — fix rendezvous
  (done, §2) and the held-vector gap (done, §1), then MEASURE the parts
  with more than one announced version across the live peers before
  choosing (A), (B) or (C). With §1 built, the node's bundles will be the
  first version of 1,612 parts and Perugia's 3 bundles the second of 3;
  "duplicates are checks" (04 §4.5) covers that scale.
- **`share contribute` needs the collection's writer lock** (München 5a,
  Perugia 6b): the worker holds it for the collection's life, so the
  node runs contribute as a cron CLI with `node pause gutenberg` around
  it (23:00–07:00), stopping acquisition and the catalogue meanwhile.
  The worker should run contribute itself, as a stage under its lock —
  a scheduling change: docs/notes/scheduling.md §6 first, with numbers.
- **A `docker exec` CLI is SIGKILLed by the deploy's stop** (München 5b:
  exit 137, not 07 §7.7's 130). Deploy script, not platform code.
- **Perugia's `records-vs-origin`** is *inconclusive* without an origin
  copy; it now receives the node's attestation (§3). Whether a leecher
  should sample the origin over the network (`--origin` as a URL) is
  06 §6.2's etiquette question, not taken up.
- **The dev-PC peer** (ed25519:9f7c969c…) lists gutenberg with
  `coverage []`, `members []`: the same `share = false` as Perugia.
- **The node's `share_slices`** was removed on 2026-10-19 to unstick
  Perugia; with §6 it can be narrowed again safely.

### Deploy order (the node first, then the peers)

1. Node (München): deploy the branch; nothing to run by hand — the next
   `share contribute` pass bundles the held vectors (watch its duration
   and the digest size: ~1,600 bundle members and coverage rows); the
   next digest offers the served members only.
2. Perugia: deploy; `louise share gutenberg` once (or set `share = true`);
   contribute may resume — its first pass will probe 1,6xx open parts
   and hold nothing of most of them, then lead what rendezvous gives it
   (the node now bundles ~1,470 parts from held vectors within a pass
   or two, so most parts close before Perugia embeds them).
3. Both: `share peers add <address> --key <key>` for the other, so the
   pin — not the declared address — is what contact uses (§4).
