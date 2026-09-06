# 4. Sharing

Sharing is Hold plus a transport. Every operation in this document is
something a single peer can do alone (§2, §3), wrapped so that the
result reaches other peers or comes from them: serving a copy, fetching
a part, taking a share of the embedding work, publishing the outcome of
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

## 4.2 Finding things

Snapshot ids are computable from the Codex — the cadence and the cutoff
say that this month's snapshot is "2026-09". So a peer looking for the
current records of a collection announces under, and queries,

```
H(collection_id ‖ snapshot_id)
```

in a distributed hash table, the way BitTorrent finds the peers of an
infohash: many announcers under one deterministic key, no owner. A
newcomer months later finds the last survivor by computing the key, not
by asking anyone. For a certified collection the certifier also
publishes a signed pointer to the current snapshot under its own key —
there the single writer is the point — with the anchor URL as fallback.

Between peers that already know each other, two mechanisms carry the
rest, both signed by the sender:

- **digests**, hourly: each peer sends its neighbours a summary of its
  catalogue — collection, latest snapshot, snapshot hash — and of its
  coverage map; a peer that sees a hash it lacks pulls what it needs.
  Bounded by the interval, converges in a few rounds, no fan-out;
- **events**, forwarded once: a new snapshot, a new daughter, an
  attestation, a flag. Each carries an id and a hop count; a peer never
  re-forwards what it has seen.

Announcing what you do not serve would be a lie, and serving without
announcing has no audience; Louise has one verb for both, `share`.

## 4.3 Moving bytes

A collection is shared as one torrent collection whose members are
slice × kind (§2.6). Fetching a part is naming members; the snapshot
gives their sizes and hashes first. Pieces are verified against merkle
roots as they arrive, so a peer that serves wrong bytes is caught by the
transport and dropped before anything reaches the copy. Fetched shards
land in staging and are promoted into the copy in one step — the step
where a certified collection's manifest-only gate applies (§3.8).

The origin is a fallback, not the default: a collection's records are
fetched from the origin once per snapshot by whichever peer §4.5 assigns
the fetch to, and from that peer by everyone else. Every completed fetch
makes the fetcher a sharer. That is the whole of how Louise takes load
off the sources it depends on.

Every byte a peer moves — fetching, sharing, contributing — draws from
one budget on that peer. There are no per-tool rate limits standing in
for it.

## 4.4 Licence classes

Not every source may be redistributed as text. Each plugin declares a
class, and the class decides what travels:

| class | examples | what may be shared |
|---|---|---|
| **green** | public domain, CC0: Gutenberg, reference genomes, public vector archives | records, vectors, index |
| **yellow** | open with conditions: Wikipedia (CC BY-SA), most reference databases (attribution) | records, vectors, index — after the peer has recorded its acknowledgement of the terms |
| **red** | text under publisher copyright with free bulk access: PubMed abstracts | vectors and pointers; the text is never exchanged, and a reader shows the origin's page |

The acknowledgement is a timestamp and the hash of the terms text, and it
is required before a yellow collection's data is *used*, however it
arrived: a hand-populated folder and a peer copy are not a bypass. The
terms text travels byte-exact inside the collection, so one
acknowledgement covers records, vectors and pack alike. A red
collection is searchable in full; only the reader changes.

## 4.5 Sharing the work

Every peer holding a collection's records may embed a part of them. The
question is how peers avoid choosing the same part without anyone
assigning parts, and how a peer that starts and stops does not waste the
others' time.

**Rendezvous.** For every shard without vectors, each peer computes a
score for every peer it believes alive:

```
score(peer, shard) = H(peer_key ‖ shard_id) ^ (1 / weight_peer)
```

and works on the shards where its own score is the highest. No messages
are needed to partition — only an approximate set of live peers, which
the digests already carry. When a peer disappears, only its shards are
reassigned, and to a deterministic successor rather than to everyone at
once. When a peer is alone, it scores highest on everything and takes it
all, which is §4.1 satisfied trivially.

**Weight and eligibility.** The weight is the peer's benchmark (§3.3),
so a peer with half the throughput is half as likely to be top on any
given shard. Before scoring, a peer is *eligible* for a shard only if
its expected time on it — chunks over benchmark, times a margin of about
three for other load — stays under the collection's limit for that
shard size. Small shards admit weak peers; large shards admit few; every
peer with any capacity is eligible for something. A backlog at launch
needs no separate mechanism: the peer does not choose, the hash does.

**Partial publication.** A contributor emits a signed bundle every few
hundred chunks, not at the end of the shard. An abandoned shard loses
minutes, not an hour, and the coverage map shows progress rather than
promises. Rendezvous yields a full ordering of eligible peers per shard,
not just a winner; if the top peer has published nothing within the
Codex's `grace_days`, the second proceeds. No claim, no confirmation, no
arbiter — the first bundle is the claim.

**Duplicates are checks.** With inconsistent views of who is alive,
two peers will sometimes embed the same shard. A peer that receives a
second compliant bundle for chunks it already has compares them and
publishes a `vectors-sample` attestation naming both producers. The
trust levels of §3.6 need a tenth of shards re-embedded independently
anyway; as long as duplication stays under that, it is not waste but the
verification budget, paid by accident.

**The fetch is assigned the same way.** For the key
`(collection_id ‖ snapshot_id ‖ fetch)`, the top eligible peer fetches
the origin's delta at the cutoff and shares the resulting shards; the
others fetch from it and check a sample against the origin. `grace_days`
is the cascade window here too.

The numbers, for a hundred peers embedding a corpus that would take one
of them two hundred GPU-days: two days each, plus a few percent of
duplicates that become attestations. The one cost no partitioning
touches is a change of space — a new encoder build, a new chunker —
which invalidates every vector every peer holds at once. That is why
the space is pinned by digest (§2.4) and why changing it is a sister,
announced as such (§2.7).

## 4.6 When two daughters appear

An open collection's origin changes and two peers publish two
daughters (§2.7). Their Codexes propagate — kilobytes, harmless. What
must not propagate is GPU spend on both.

If they parse the new origin the same way, their snapshots have the same
hash and they are one collection under two names; a peer holding both
says so with an attestation and catalogues merge them.

If they differ, the choice is made at the records stage, where it is
cheap:

- **adoption** of a daughter is the number of distinct peer keys
  announcing its records for the new snapshot, weighted by the local
  trust list;
- from the first daughter seen for a lineage, fetching records of any
  daughter is allowed and embedding for all of them is not — for
  `grace_days`, which the cadence already has;
- when the window closes, only the daughter with the most adoption is
  eligible for embedding; ties go to the lower Codex hash; the leader
  changes only if a rival exceeds it by a margin for several digest
  intervals in a row;
- the others are never deleted: marked *not leading*, excluded from
  work, still shared and searchable by whoever holds them.

Adoption counts can be inflated by minting keys; the trust-list weight
dampens this and does not remove it. The worst case is bounded: the
network embeds the attacker's daughter, which is still a valid
derivation from the real origin — a parsing preference imposed by a
Sybil, and the community can fork again. Where the choice really settles
is the platform: parsers live there, reviewed as code, and the next
release's image is the toolchain new peers get by default. Adoption
avoids waste in the meantime.

Certified collections have none of this. One daughter, signed.
