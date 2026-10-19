# 4. Sharing

*(vocabulary amended 2026-10-08: SHARD, parts)*

Sharing is Hold plus a transport. Every operation in this document is
something a single peer can do alone (§2, §3), wrapped so that the
result reaches other peers or comes from them: serving a copy, fetching
a portion, taking a share of the embedding work, publishing the outcome of
a check. Nothing here contains logic that Hold does not have.

## 4.1 The requirement: one peer is enough

Once one honest peer holds a collection, it must be able to keep it
available *and up to date* if every other node disappears, including the
node that created it. Not until the next deadline: it must produce the
next snapshot, embed it, share it, and be found by a newcomer, without
any other node's key, address or state.

This rules out three familiar designs, and §2 already paid for two of
them. A collection's identity cannot be a publisher's key, or nobody
could continue it when the key is gone — so the identity is the Codex's
hash. A collection's description cannot be one signed document that
changes with every bundle, or it would need one signer — so the mutable
part is a coverage map that every peer assembles for itself. And work
cannot be handed out by a coordinator, or it would stop when the
coordinator does — so it is partitioned by a function every peer can
compute (§4.5). A coordinator may still exist, for statistics and credit;
a peer that cannot reach one behaves exactly as if it did not.

"One peer is enough" holds given one of two things: a reachable
distributed hash table, or one peer address given by hand (`share peers
add <address>`, 07 §7.3). Nothing else is ever needed.

## 4.2 Finding things

Snapshot ids are computable from the Codex — the cadence and the cutoff
say that the snapshot closing at the September cutoff is "2026-08"
(02 §2.2). So a peer looking for the current records of a collection
announces under, and queries,

```
H(collection_id ‖ snapshot_id)
```

in a distributed hash table, the way BitTorrent finds the peers of an
infohash: many announcers under one deterministic key, no owner. A
newcomer months later finds the last survivor by computing the key, not
by asking anyone.

**The key, exactly.** `H` is SHA-256, and `H(a ‖ b)` is SHA-256 over the
canonical JSON of the list of strings `[a, b]` — the one concatenation
Louise has (09 §9.1). Every identity is written in its textual form
(`sha256:` and 64 hex digits for a collection or lineage id, 09 §9.1;
`ed25519:` and 64 hex digits for a key, 09 §9.2). The hash table is the
BitTorrent mainline DHT (BEP 5), whose keys are 20 bytes: the
**collection key** for a snapshot is the first 20 bytes of
`H(collection_id ‖ snapshot_id)`, and the **lineage key** is the first
20 bytes of `H(lineage_id ‖ snapshot_id)`, where `lineage_id` is the
root of the `continues` chain (02 §2.7). A sharer announces under both,
for every snapshot it serves. A newcomer that holds a lineage but not
its leading daughter therefore finds the daughter under the lineage key,
and one that holds nothing finds the collection under the collection
key. A searcher probes the current snapshot id first, then each earlier
id back to twelve cadences, and stops at the first key with announcers;
a collection nobody has announced for twelve cadences is found by a peer
address given by hand. Twelve CADENCES and not twelve months: the span
is twelve announcement periods of that collection, generous by the same
factor for a weekly sister as for her monthly mother, and twelve probes
either way.

What is found under a key is a peer address. The port announced there
speaks the peer messages of 09 §9.9 — signed, and the only protocol
between two Louise nodes other than the member swarms. A newcomer sends
`pull codex` and `pull snapshot` to what it found; the Codex chain back
to the root comes with the Codex, the snapshot names every record member
with its root and size, and from those three the member's torrent and
infohash follow (§4.3). From one address, everything.

**The swarm key** (amended 2026-10-19, operator). The members' swarms are
found under a third key per snapshot, the first 20 bytes of
`H(collection_id ‖ snapshot_id ‖ "swarm")`, which a sharer announces with
the port its swarm listens on (07 §7.8 `[node] peer_port`); a fetcher
dials what it finds there and what digests name (09 §9.9 `peer_port`). A
member's torrent is NEVER announced by its own infohash, in the DHT or by
local discovery: a sharer's announcements are three keys per snapshot,
however many members it serves. They were one per file until then, and
3,170 gutenberg members kept about ten thousand UDP flows open on the
node and filled the home router's NAT table, cutting every device in the
house off (docs/notes/dht-flood.md). A cost that grows with the parts
served is a denial of service by design.

For a certified collection the certifier also publishes a signed pointer
to the current snapshot under its own key — there the single writer is
the point — with the anchor URL as fallback. The pointer names the
collection, the snapshot id and hash, a sequence number, and a time
(09 §9.11); it is a BEP 44 mutable item under the certifier's key,
salted with the collection id, and the same signed object is served
beside the anchor document. A peer keeps the highest `seq` it has
verified for a collection and discards any pointer with a lower one — a
captured old pointer, however valid its signature, moves nobody back.

Between peers that already know each other, two mechanisms carry the
rest, both signed by the sender:

- **digests**, hourly: each peer sends its neighbours a summary of its
  catalogue — collection, latest snapshot, snapshot hash — and of its
  coverage map; a peer that sees a hash it lacks pulls what it needs.
  Bounded by the interval, converges in a few rounds, no fan-out;
- **events**, forwarded once: a new snapshot, a new daughter, an
  attestation, a flag, a bundle. Each carries an id and a hop count; a
  peer never re-forwards what it has seen.

**Digests.** The interval is one hour. A digest carries the sender's
key, its address, its benchmark per space (03 §3.3), and per collection
held: the collection id, the lineage id, the latest snapshot id and
hash, the licence and privacy class, the members offered, and the
coverage the peer has accepted — the bundle ids per part that passed
its own ladder (§4.5); 09 §9.9 fixes the fields. A **neighbour** is any
peer from which a digest arrived in the last three intervals; a peer
sends its digest to its neighbours and to the addresses in its peer
list. A digest also carries up to sixteen live neighbours' addresses, so
one address given by hand becomes a neighbourhood within a few
intervals.

**Events.** An event's id is `H` of the canonical JSON of its body
(09 §9.9), computed by the receiver from the signed body and carried
nowhere in the message, so there is no id to forge and none to suppress
a real event with. The hop
count sits outside the signed body, starts at 8, is decremented on each
forward, and an event at 0 is not forwarded. A peer keeps the ids it has
seen for seven days and drops an event whose time is older than that or
more than one interval in the future.

**Identity is the key.** A peer is one ed25519 key (`hold key create`,
09 §9.2); every digest and event names the key that signed it, and a
receiver learns which key is at which address from the digest itself. A
digest from a key never seen before is accepted on the strength of its
signature alone — identity is the key, not the address, and the aggregate
limit of 08 §8.2 bounds what a flood of fresh keys can cost. An address
added by hand may pin a key; a digest from that address under another
key is discarded and logged, and a digest FROM the pinned key is recorded
at the pinned address whatever address it declares — the hand-given
address is the one this peer can reach, and the declared one is kept
only as what the digest said *(amended 2026-10-19, P2P review: the node
declared its LAN address; a peer off that LAN, holding the node's tailnet
address by hand, sent every digest, pull and swarm dial to the declared
one and reached nothing until the node changed what it declares)*.
Ordinary keys do not rotate; a lost key is a new key.

**Pulls.** The messages are `digest`, `event`, `pull codex`, `pull
snapshot` and `pull attestations(collection_id, since)` (09 §9.9); every
one is signed, and every object pulled is content-addressed. A pull that
does not connect is deferred and retried at the next interval. A pull
that returns bytes whose hash differs from what the digest advertised is
a lie: the bytes are dropped, the sender is not a neighbour for
twenty-four hours — its digests are ignored and it is not scored in
§4.5 — and the event is logged with its key.

**Bootstrap.** Neighbours come from two sources: the `[peers]` list in
`platform.toml` (07 §7.8), which `share peers add <address>` writes, and
the peers found under a collection's or lineage's DHT key. The DHT is
optional (`platform.toml`); a node with it off is reachable only by the
peers that know its address, and is complete in every other respect.

**Names travel with the recipe, and decide nothing.** A digest carries
each collection's title from the `descriptive` block of the Codex the
sender holds (02 §2.1; 09 §9.9), so a peer can name and describe a
collection it has not fetched. Because `descriptive` is outside the
identity, two peers holding one collection under two titles are holding
one collection: neither is corrected, nothing is merged, and each shows
what its own Codex says. An operator who dislikes a title overrides it
for its own listing in `collections/<name>.toml` (07 §7.8); that
override is local and is never published. A name is displayed and
nothing more — never sorted on, merged on or resolved by (08 §8.2) —
and where two collections show one name the catalogue separates them by
their ids and their verification facts.

Announcing what you do not serve would be a lie, and serving without
announcing has no audience; Louise has one verb for both, `share`.

## 4.3 Moving bytes

A collection is shared as one torrent collection whose members are
slice × kind (§2.6). Fetching a portion is naming members; the snapshot
gives their sizes and hashes first. Pieces are verified against merkle
roots as they arrive, so a peer that serves wrong bytes is caught by the
transport and dropped before anything reaches the copy. Fetched parts
land in staging and are promoted into the copy in one step — the step
where a certified collection's manifest-only gate applies (§3.8).

**Files are torrents.** Every file of a member is one BitTorrent v2
torrent (BEP 52), so a member of one file is one torrent and a member of
several is that many. A file's torrent parameters — its SHARD-relative
path, its length, its pieces root, v2 only, nothing else in the info
dictionary — are fixed by 09 §9.16, so the torrent, and therefore the
infohash, is a function of those three alone: two honest publishers of
the same file land in one swarm, and a peer computes each infohash from
the snapshot without contacting anyone. A member of one file has that
file's pieces root as its root — the root the snapshot lists for a
record part (02 §2.2); a member of several has a root per file and a
member root of `H` over the list of its `[path, root, bytes]` in path
order (09 §9.16). The torrent collection of one (collection, snapshot)
is the set
of its members; the snapshot is its index for record parts, and a
digest or a `bundle` event names every other member the same way, by
name, root and size. Record parts and bundles are
members (the text index never is: each peer builds its own); vectors reach a peer only as bundles (03 §3.5, 09 §9.7). A
request for a `vectors-<space>-<slice>` member or a bucket group
resolves to the accepted bundles the sharer holds for those chunks,
delivered as bundles with their signatures, and the fetcher merges them
itself (02 §2.4).

**A new version of a held part, by patch** (amended 2026-10-12, operator).
Before fetching a record part it holds an older version of, a peer asks a
peer holding the new one for the changed lines (09 §9.9 `pull patch`) and
rebuilds the part; the root decides as for any file, and a rebuild that
misses costs the whole part by torrent. A promotion that replaces a
version a held snapshot lists keeps it under its superseded name (02 §2.3),
on every peer.

**Staging and promotion.** A member is fetched into staging — local
state beside the copy (07 §7.8), never the copy itself — and resumption
is per piece from what staging already holds; nothing in staging is
read by any stage. A member is promoted when its root equals the root of that
member in a signed snapshot of the collection's chain that the peer
holds (02 §2.2, 09 §9.4) — for an open collection any snapshot in the
chain carrying at least one verified signature, its own included; for a
certified collection the current snapshot signed by the anchored
certifier (§3.8). Promotion is one rename into the SHARD and, in the
same transaction of the state database, the holdings mark: the durable
marker is the snapshot's row in the manifest naming that member as held.
The root is computed in staging, before the rename, so a file in the SHARD
has already been verified; a member present in the copy without its mark
is held — built here, or renamed before its mark was written — and is
marked, never moved back. *(Amended 2026-10-06, migration step 9d: this
read "goes back to staging", which would have moved every part a peer
built itself.)* A record part, a relations part, a figures container or a
`files` member whose root is in no snapshot the peer holds stays in
staging until a snapshot naming it arrives, and is
discarded after `grace_days`. The members no snapshot lists are
admitted by their own rule instead, never by this one: a bundle by the
ladder of 03 §3.5, a `centres` or `index-<space>` member by the root
`index.json` gives it.

**Two snapshots under one id.** When two different snapshots carry the
same id (02 §2.2), the part where they differ is named and
`records-vs-origin` on that part decides: it is exact, and one of the
two fails. A peer that computed one of them itself serves its own
meanwhile; a peer that computed neither promotes nothing from either
until the check has run, and if the origin cannot answer
(*inconclusive*, 03 §3.1) both stay in staging, shown as disputed.

**Nobody serving it is a deferral, said at once** (amended 2026-10-19,
P2P review). A digest's `members` is what its sender SERVES (09 §9.9), so
a wanted member that no neighbour's digest offers — and that no swarm
found under the swarm key could hold — is deferred and shown as waiting
for a digest that offers it, never waited on; a fetch that does start is
bounded twice, by the metadata wait when no peer answers and by a stall
bound when a peer answers and sends nothing. The node served two slices
of a collection, its digest offered every member it held, and a peer
wanting every slice waited without end on the first part nobody served:
the one connection it had, to a swarm that did not hold the part, counted
as an answer, and the metadata bound never fired.

**Sizes are known before the first byte.** A fetch that would not fit
in the free space of its target — members declared, staging included —
is not started; the collection is shown as waiting for space with the
shortfall. Running out of space inside a stage is a deferral (07 §7.7):
the unit is left untouched, no error is written to any document, and
the stage retries when space returns. Index maintenance never takes the
disk below the floor a fetch needs.

The origin is a fallback, not the default: a collection's records are
fetched from the origin once per snapshot by whichever peer §4.5 assigns
the fetch to, and from that peer by everyone else. Every completed fetch
makes the fetcher a sharer. That is the whole of how Louise takes load
off the sources it depends on.

**Sharing is automatic.** For a `privacy: public` collection (§4.4),
every member a peer has promoted is served and announced by the running
node from the moment the fetch completes, unless the collection's TOML
says `share = false` (07 §7.8). `share <collection> --slices … --kinds
…` narrows what is served; `share <collection>` names `hold collection
show` as the state it publishes (07 §7.3). A `hold` command never opens a
port (07 §7.4). Serving is a statement about bytes and roots — this
member, this root, in this snapshot — and nothing more; what a peer has
checked about the collection is in its attestations, not in the fact
that it serves it.

**The encoder travels too.** A collection's encoder is named by the
digest of its weights, and its reference engine by the digest of the
image holding it (02 §2.4). So both are artefacts like any other and
move like one:
- a peer that holds an encoder's weights announces them under the first
  20 bytes of `H` of the weights digest;
- a peer that holds an engine's image announces it the same way, under
  the image digest (09 §9.16);
- a peer that needs either fetches from whoever answers and checks the
  bytes against the digest.

The digest is the whole check, so no signature and no trust are
involved. The image travels so that the reference stays reproducible
the day its registry forgets it. Neither is part of any SHARD, because
one encoder serves every collection in its space and would otherwise be
copied into each.

Weights carry licences of their own, and some forbid redistribution.
The platform's list of encoders records for each whether it may be
passed on; a peer announces only those it may, and for the rest names
the origin so a newcomer fetches from there. The bytes draw from the
same budget as everything else.

What this buys: the day a model's host goes down, the collections in
that space keep being verified and keep accepting contributions,
because the weights are already in the network. A collection whose
encoder exists in one place is a collection with a single point of
failure outside the network — §4.1 forbids that inside the network and
should not tolerate it at the edge.

Every byte a peer moves — fetching, sharing, contributing — draws from
one budget on that peer. There are no per-tool rate limits standing in
for it. The budget covers every byte, in both directions: origin and
peer fetches, serving, bundle exchange, digests and events, anchor
checks (§3.7), freshness checks (05 §5.3), the live mode of
`origin-seal`, and the DHT. It is a pair of token buckets, up and down,
each with an interactive reserve, and five classes served in strict
priority when the link is contended: *interactive* (a search, its
freshness check, T4), *delta* (the current snapshot's members),
*backfill* (older parts, the corpus), *archive* (pre-embedded vector
archives, useless without the corpus and so always behind it), and
*seeding*, which takes what is left and never reserves. A transport that
cannot be metered per byte takes a rate lease from the bucket. An
origin's etiquette — connections, hours, a rate it asks for (06 §6.2) —
is a further cap applied under the budget, never a limit in place of
it. The numbers live in `platform.toml` (07 §7.8).

## 4.4 Licence classes

Not every source may be redistributed as text. Each plugin declares a
class, the Codex's `license.class` records it and is authoritative
(02 §2.1), and the class decides what travels:

| class | examples | what may be shared |
|---|---|---|
| **green** | public domain, CC0: Gutenberg, reference genomes, public vector archives | records, vectors, index, figures; no acknowledgement |
| **yellow** | open with conditions: Wikipedia (CC BY-SA), most reference databases (attribution) | records, vectors, index, figures — after the peer has recorded its acknowledgement of the terms |
| **red** | text under publisher copyright with free bulk access: PubMed abstracts | vectors, pointer records and index — after the same acknowledgement; the text is never exchanged, and a reader shows the origin's page. FIGURES DO NOT TRAVEL either: a plate is the origin's content and not something derived from it, so it falls on the text's side of this line and not the vectors' |

THE CLASS IS THE COLLECTION'S, AND FIGURES DO NOT GET ONE OF THEIR OWN.
Where the plates carry conditions the text does not — a public-domain
book whose scans a library asserts rights in — the collection takes the
stricter class, by the same rule that lets a collection tighten its
plugin's declared class and never loosen it. A per-member class was
considered and refused: the acknowledgement is per collection, one ack
covering "records, vectors and SHARD alike", and splitting the class
would turn one acknowledgement into a matrix for a case no corpus here
presents.

The acknowledgement is a timestamp and the hash of the terms text, and it
is required before a yellow or red collection's data is *used*, however
it arrived: a hand-populated folder and a peer copy are not a bypass. The
terms text travels byte-exact inside the collection, so one
acknowledgement covers records, vectors and SHARD alike.

**The terms member.** The terms text is the member `LICENSE-TERMS` at
the SHARD's root (02 §2.6, 09 §9.13), bytes exact, and its SHA-256 is the
Codex's `license.terms_sha256`; a green collection has `terms_sha256:
null` and no terms member. The members a peer may fetch before it has
acknowledged anything are exactly `codex.json`, `snapshots/*` and
`LICENSE-TERMS`: enough to read what one would be agreeing to. The
`license` check (03 §3.2) hashes that member, and a member whose hash
differs from the Codex is refused at promotion.

**The record.** `hold collection ack <collection>` prints the terms and
records the acknowledgement — the time and the terms hash — in the
collection's state directory (07 §7.8, 09 §9.13). Every gate resolves it
through one function and compares it against the terms member the
collection carries, never against a remembered hash: an acknowledgement
is of a text, identified by its hash, and stays valid for as long as the
collection's terms hash to it. A collection whose Codex names a
`terms_sha256` this peer has already acknowledged — a sister or daughter
under the same terms — is acknowledged by that record. The
acknowledging peer is each peer for itself: a sharer without one does
not announce or serve the gated members, a fetcher without one does not
request them, and neither embeds, indexes or searches them.

**Red.** A red collection's records member is the same record lines with
`sections` empty and `extra.origin_url` set — **pointer records** (02
§2.3, 09 §9.5). The snapshot's roots are over the pointer parts, which
is what travels; `content_hash` remains the hash of the cleaned text, so
a peer that has the text can verify it. Every peer that holds a red
collection fetches the text from the origin itself (`hold records
fetch`), under the origin's etiquette and the budget, into local state
that never travels and is never a member (07 §7.8). The fetch of §4.5
assigns the snapshot and the pointer parts, never the text. The checks
and tiers that need text — `chunks`, `records-vs-origin`,
`vectors-sample`, T1, T3 and T4 (03 §3.2, §3.5) — run where the text is,
and a peer holding vectors without text runs only T0 and T2, may serve
what it holds, and does not attest. A red collection is searchable in
full on a peer that has the text; only the reader changes. PubMed is
red.

**Privacy.** Beside its licence class a collection has a `privacy`
(02 §2.1): *public* — announced and shared as above; *restricted* —
never announced in the DHT and shared, in digests and members, only with
peers on the local trust list; *never-share* — no `share` at all: not
announced, not served, not named in any digest. The plugin declares the
default and a collection may tighten it, never loosen it.

**Erasure.** A collection also declares how content may leave it
(`erasure`, 02 §2.1): *append-only* — nothing leaves without a trace,
and a removal is a tombstone with a reason; *retractable* — the
publisher may retract a record, and the tombstone stays; *erasable* —
removal may be mandatory and must leave no trace. Sharing cannot
un-publish: once a part is served, no later snapshot reaches the peers
who hold it. So `erasure: erasable` requires `privacy: never-share`, and
a SHARD build refuses the pair otherwise; every collection that travels
is append-only or retractable, and a tombstoned record stays in its
closed part, excluded from search and embedding (01, promise 7).
What "no trace" means in bytes is 02 §2.9.

**The web sources of a deposit collection.** A deposit collection may
subscribe to URLs (06 §6.7), and their text is not the depositor's.
Two rules cover them, in place of a per-source acknowledgement:

- a **git repository cloned over SSH with this node's key** needs
  nothing more: the key is the operator's, so is the authorisation to
  read what it opens;
- **every other URL** needs the **web-sources disclaimer**, accepted
  once per node (`hold collection ack --web-sources`): all fetched
  content is the property of its owners, its licences must be
  respected, the collection is never shared, and the operator who
  subscribes a collection to a URL is liable for any infringement. The
  text ships with the platform, byte-exact; the acceptance is recorded
  as any acknowledgement is — the time and the hash of the text
  (09 §9.13) — and resolved by the same function. Until it exists, no
  such source is fetched and a request to add one is refused.

The collection itself stays green and never-share: nothing it holds
ever leaves the node, which is why one standing acceptance is the
right size for it.

## 4.5 Sharing the work

Every peer holding a collection's records may embed a part of them. The
question is how peers avoid choosing the same part without anyone
assigning parts, and how a peer that starts and stops does not waste the
others' time. The unit in which the work travels is the **bundle**
(08's signed block): a batch of vectors on the collection's own records,
signed by the key that produced them, naming its collection, space,
snapshot and chunk range (03 §3.5, 09 §9.7).

**Before contributing.** A peer contributes vectors to a collection
only when its own `roots` and `chunks` pass on the parts it holds, at
least one `records-vs-origin` attestation exists for the snapshot — its
own or another key's — and its encoder has passed the calibration gate
for the space (03 §3.3). Records are checked before GPU is spent; there
is no vector precondition, which would be circular. During the window
of §4.6 no daughter of the lineage is embedded at all.

**Rendezvous.** For every part without vectors, each peer computes a
score for every peer it believes alive:

```
shard_id = "<collection_id>/<space>/<slice>/<n>"
h        = (first 64 bits of H(peer_key ‖ shard_id), as an unsigned integer, + 0.5) / 2^64
score    = ln(h) / w
```

and works on the parts where its own score is the highest. `H(a ‖ b)`
is §4.2's hash; `<space>` is the path segment of 02 §2.4, `<n>` the
part's number as written in its path (`00001`); `w` is the peer's
benchmark in tokens per second as published in its digest; the score is
computed in IEEE-754 double; the highest wins, and a tie goes to the
lower peer key. The same `shard_id` is the string an attestation uses to
name a part (09 §9.8); the name keeps the old word, as the attestation's
`shards` key does — bytes already written keep their names. No messages are needed to partition — only an
approximate set of live peers, which the digests already carry: a peer
is **live** when a digest from it arrived within the last three
intervals, and a key known only from another peer's digest is not scored
until its own digest is seen. When a peer disappears, only its parts
are reassigned, and to a deterministic successor rather than to everyone
at once. When a peer is alone, it scores highest on everything and takes
it all, which is §4.1 satisfied trivially. Scores are recomputed only
when the live set or the set of open parts changes — a digest arrived,
a bundle was accepted — and a peer keeps, per part, only the head of
the ordering, extending it when the cascade needs the next candidate.

**What a peer already holds, it bundles** (amended 2026-10-19, P2P review;
RULE 1). Rendezvous assigns the parts WITHOUT vectors; a peer that already
holds vectors for an open part — embedded before it had neighbours,
carried from a sister, imported from an archive — bundles and announces
them whoever leads the part, before any GPU is spent, because bundling
held vectors costs the challenge rows and nothing else. The first bundle
is the claim: the part closes for the chunks it covers, and its leader
embeds only what still lacks an accepted bundle. The node held 91 % of
gutenberg's vectors and announced none, every coverage map showed 0 %,
and a new peer's first pass re-embedded parts the node had (74 % of its
chunks). Who EMBEDS a part is rendezvous's question; who BUNDLES vectors
already held is everyone's who holds them — with one restraint, so that
many holders joining a network do not make as many versions of every
part: a part whose bundle a live neighbour both claims and offers is
left to the fetch, which takes that bundle in and compares it with the
vectors held here ("duplicates are checks", below); a version that exists
and is served is reused, not remade.

**Weight and eligibility.** The weight is the peer's benchmark for this
collection's space — the `benchmarks` entry of its digest whose
`space_id` matches (§3.3; 09 §9.9), a peer with no such entry being
unscored here, since a rate measured on another encoder says nothing
about this one —
so a peer with half the throughput is half as likely to be top on any
given part. Before scoring, a peer is *eligible* for a part only if
its expected time on it — chunks over benchmark, times a margin of about
three for other load — stays under the collection's limit for that
part size. The limit is the collection's work period: with `tokens` the
part's token count as the snapshot lists it (chunks × `space.ctx`, an
upper bound, 02 §2.2) and `work.per` the Codex's work period (02 §2.4),

```
eligible  ⇔  3 × tokens / w  ≤  seconds(work.per)
```

where a calendar month counts as thirty days and a year as 365. A
contributor must be able to finish a part within one work period, with
a factor of three for other load. Small parts admit weak peers; large
parts admit few; every peer with any capacity is eligible for
something. A backlog at launch needs no separate mechanism: the peer
does not choose, the hash does. A peer embeds only parts whose records
it holds and has checked; for a red collection that means the text.

The benchmark is self-reported. What bounds a peer that overstates it is
the cascade below, and one local rule: a key that led a part on this
peer's view and published no bundle for it within `grace_days` is not
scored on that collection until a bundle from it is accepted, so an
inflated number costs the collection one window, not one per part.

**Partial publication.** A contributor emits a signed bundle **about
every fifteen thousand chunks**, not at the end of the part — 09 §9.10's
worked example is one part covered by two bundles of 14,800 and 15,200,
and this sentence used to say "every few hundred", which the numbers do
not support. At three hundred, a fully covered PubMed has some 120,000
coverage rows and 120,000 members to announce, and a digest's `coverage`
and `members` lists run to megabytes; at fifteen thousand it has about
2,400. T3's sixteen recomputes are 5.3 % of production at three hundred
and 0.16 % at fifteen thousand. What the small number was FOR — losing
minutes rather than an hour to an abandoned part — is bought instead by
where vectors are written: a contributor commits them locally in slices
as it goes (07 §7.7's unit for `vectors build` is the batch in flight to
the encoder), so an interrupted part loses a batch whatever the bundle
size is. A bundle is a unit of SHARING, and sharing wants few large ones.
There is a floor as well as a ceiling: below about thirty rows T0's
envelope test (03 §3.5) cannot be applied at all, because the standard
error of a sample deviation is around `1/sqrt(2n)` and the band would
refuse honest work, so a bundle sized in the tens buys a weaker admission
than one sized in the hundreds.

A bundle is split on RECORD boundaries and never inside a record.
`possession.json` maps a uid to the content hash its producer embedded
against (09 §9.7), so a record whose rows straddled two bundles would be
claimed by both — and a record re-issued since would then have to be
refused twice, out of two bundles that need not arrive together. A record
whose own chunks exceed the target is one bundle over the target, not a
record cut in half; bundles are therefore uneven, as §9.10's example
shows.

An abandoned part loses
minutes, not an hour, and the coverage map shows progress rather than
promises. Rendezvous yields a full ordering of eligible peers per part,
not just a winner; if the top peer has published nothing within the
Codex's `grace_days`, the second proceeds. No claim, no confirmation, no
arbiter — the first bundle is the claim. Exactly: a part is *open*
while any chunk in it has no accepted bundle; its leader is the top
eligible peer; the leader's claim is renewed by each bundle it publishes
for the part; the window runs from the later of the moment the part
became open and the leader's last accepted bundle; when it expires the
next peer in the ordering leads and embeds only the chunks that still
lack an accepted bundle. Every bundle names its chunk range, and the
coverage map is the set of accepted bundles per part (02 §2.5, 09 §9.10).

**How a bundle travels.** A bundle is a member (§4.3): its producer
announces it with a `bundle` event naming the collection, space,
snapshot, part, chunk range, bundle id, root and size, and lists it in
its digest's coverage once it has accepted it itself. A producer that
does not SERVE the collection — its file says `share = false`, or its
mode does not seed — announces no event (§4.2: announcing what you do
not serve would be a lie); the bundle is accepted on that peer, listed
under its digest's `coverage`, and offered under `members` from the
moment it serves *(amended 2026-10-19: a peer announced bundles nobody
could fetch from it, and the node waited two minutes on each)*. A peer
that sees the event and lacks the range fetches the member, runs the
ladder of 03 §3.5, and, if it passes, merges the vectors into its own
file for that part (02 §2.4) and reports the bundle id in its next
digest. A peer that lacks a bundle a neighbour's digest OFFERS — a
`bundle-<space>-<id>` member with its files (09 §9.9) — fetches it the
same way without its event: an event is forwarded once and kept seven
days, and the digest is the recovery path for every peer that missed it
*(amended 2026-10-19)*. A
coverage claim counts — for the cascade, for adoption, for §4.7 — only
when the bundle it names has been fetched and passed T0–T3. A bundle's
id is the hash of its signed bytes (09 §9.7); a producer that restarts
and re-emits the same bundle produces the same id, and an importer that
holds that id ignores it. Admission is 03 §3.5's ladder and the
producer's calibration, nothing else; a key is refused only after this
peer has denied it outright or a proof of forgery has burned it
(03 §3.5, 09 §9.2), never for being absent from an allow list.

**Duplicates are checks.** With inconsistent views of who is alive,
two peers will sometimes embed the same part. A peer that receives a
second compliant bundle for chunks it already has compares them and
publishes a `vectors-sample` attestation naming both producers. The
first accepted vector for a chunk is what is stored and searched; the
second is compared, attested and not stored, unless the first is later
flagged, in which case the next accepted producer's bundle is imported
in its place. The trust levels of §3.6 need a tenth of parts
re-embedded independently anyway; as long as duplication stays under
that, it is not waste but the verification budget, paid by accident.

**The fetch is assigned the same way.** For the key
`"<collection_id>/<snapshot_id>/fetch"`, used in place of `shard_id`
and with no size limit, the top eligible peer fetches the origin's delta
at the cutoff and shares the resulting parts; the others fetch from it
and check a sample against the origin. `grace_days` is the cascade
window here too. Eligible here is every live peer that holds the
previous snapshot and, for a gated class, the acknowledgement. When a
peer's sample against the origin fails, it publishes the failing
`records-vs-origin` attestation naming the part and the snapshot hash,
stops fetching from that snapshot's sharers, and the next peer in
rendezvous order fetches; the two snapshots then stand under one id
until §4.3 settles them. A late fetcher — the second in line, or the
survivor of §4.1 a month on — reproduces the state at the cutoff by the
plugin's `cutoff_rule` (02 §2.2): which dumps or update files the origin
had published at that instant, not what it publishes now. When the
origin no longer offers those artefacts, the snapshot is reproducible
only from peers, and the next daughter says so with
`origin_reachable_until` (02 §2.7). For a red collection the fetcher
produces the snapshot and the pointer parts; the text never leaves the
peer that fetched it.

The numbers, for a hundred peers embedding a corpus that would take one
of them two hundred GPU-days: two days each, plus a few percent of
duplicates that become attestations. The one cost no partitioning
touches is a change of space — new weights, a new chunker — which
invalidates every vector every peer holds at once. That is why the
space is pinned by digest (§2.4) and why changing it is a sister,
announced as such (§2.7). A new engine is not a change of space: it
passes the gate and contributes to the space it was admitted to (§3.3).

## 4.6 When two daughters appear

An open collection's origin changes and two peers publish two
daughters (§2.7). Their Codexes propagate — kilobytes, harmless. What
must not propagate is GPU spend on both.

If they parse the new origin the same way, their snapshots list the
same `records_root` for every part — never the same snapshot hash,
which carries each one's own `collection_id` — and they are one
collection under two names; a peer holding both
says so with an attestation and catalogues merge them.

If they differ, the choice is made at the records stage, where it is
cheap:

- **adoption** of a daughter is the number of distinct peer keys
  announcing its records for the new snapshot in their digests, counting
  a key only once its first such announcement is at least one digest
  interval old; never weighted by any trust list — the trust list is a
  local allow or deny filter over whose word is counted at all (03 §3.6),
  not a weight;
- from the first daughter seen for a lineage, fetching records of any
  daughter is allowed and embedding for all of them is not — for
  `grace_days`, which the cadence already has;
- when the window closes, only the daughter with the most adoption is
  eligible for embedding; ties go to the lower Codex hash; the leader
  changes only if a rival exceeds it by twenty percent for three
  consecutive digest intervals, as this peer observes them;
- the others are never deleted: marked *not leading*, excluded from
  work, still shared and searchable by whoever holds them.

Adoption counts can be inflated by minting keys: keys are free and the
count is advisory, as every threshold in §3.6 is; the one-interval age
and the aggregate limit of 08 §8.2 slow this and do not remove it. The
worst case is bounded: the network embeds the attacker's daughter, which
is still a valid derivation from the real origin — a parsing preference
imposed by a Sybil, and the community can fork again. Where the choice
really settles is the platform: parsers live there, reviewed as code,
and the next release's image is the toolchain new peers get by default.
Adoption avoids waste in the meantime.

Certified collections have none of this. One daughter, signed.

## 4.7 Vitality: active and dormant

Holding a collection is a sustained activity, not a single payment
(08 §8.6). The Codex declares the level as a rate — `work: {tokens,
per}`, the work parameter of 02 §2.4 — and this section is the rule by
which a peer computes, from what it has seen, whether that rate is being
met.

A collection is **actively-held** on a peer's catalogue while some key's
signed work for it, seen by that peer, totals at least `work.tokens`
per `work.per`; otherwise it is **dormant**. Exactly: over the trailing
period of length `work.per` (§4.5's seconds), the peer takes each key it
has seen produce **signed work** for the collection and asks whether
that key reached the rate. Signed work is of two kinds and they count
alike:

- a **bundle** — vectors on the collection's own records — counting its
  chunk count × `space.ctx` tokens, the same upper bound the snapshot
  uses per part;
- an **attestation** — a completed check, signed, naming its seed and
  the parts it sampled (03 §3.6) — counting, for a deterministic check,
  the `tokens` the snapshot lists for the parts it names, and otherwise
  the tokens of the chunks it
  re-embedded, and `work.tokens` for a deterministic check, which
  reproduces a whole part rather than a sample of it.

Both are counted from what the peer holds, and from what reaches it:
bundles from a live neighbour's digest, which reports the coverage it
has accepted, and attestations from the events that carry them and from
`pull attestations` (§4.2; 09 §9.9). If any single key reaches
`work.tokens`, the collection is actively-held; if none does, it is
dormant.

Two kinds, one rule, because they are the same thing at different points
in a collection's life. While there is embedding left to do, keeping a
collection alive means embedding it. When there is none — Gutenberg is
finished, seventy thousand books, a few hundred more a month — keeping
it alive means continuing to check it, and a collection nobody checks
any more is decaying whatever its coverage says. A rule that counted
only bundles would call a finished collection dormant and a rotting one
healthy; counting signed work of either kind calls both correctly, and
adds no mechanism, since attestations already exist, already travel and
already carry a key and a seed.

Nothing counts that is not signed work: a peer's own use of a collection
is not a signal, and is never published (§4.2). What a peer queries is
its own business, and a count of queries would be free to forge, which
is the one thing a vitality signal must not be.

The default `per` is the update cadence, so a collection embedded or
checked at the pace its snapshots arrive is active by construction, and
one that nobody embeds or checks becomes dormant one period later.

Dormant is a state, not a verdict. A dormant collection is still
findable under its keys (§4.2), still fetchable, still searchable and
still checkable; it is shown as dormant and left out of the default
listing of `hold collection list`. The state is computed locally by
each peer from the signed work it has seen — no one declares it, no one
decides it — and it flips back the moment some key's bundles reach the
rate again; anyone may be that key. The signal is "someone continues",
never "the creator continues": the key that first published the Codex
is recorded in catalogues as a fact and used for nothing, and no
history of who held the collection is used — only the changing set of
who currently does.

The declared rate cannot be inflated (08 §8.5): the tokens it claims
live in bundles whose vectors `vectors-sample` reproduces on the
collection's own records, and a rate nobody meets makes the collection
dormant, which is true information. A creator who wants a heavier
encoder writes a higher rate and excludes weaker hardware from §4.5's
eligibility by that alone; a creator who writes a low one declares a
collection that works at a low level, and it stays there until strangers
choose to reproduce it.
