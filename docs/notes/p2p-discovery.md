# Discovering peers and collections — proposal

Companion to the torrent transport (`app/acquire/transport_torrent.py`,
`app/acquire/torrent_publish.py`), which is implemented and tested. **Nothing
in this document is implemented yet**; §5's Phase-2 plan stands until amended.

The transport answers "given a torrent, get the bytes". It leaves two
questions open, and they are genuinely different problems that want different
answers:

| | question | shape of the answer |
|---|---|---|
| **Collections** | what does a peer *hold*, and is it usable by me? | a signed document |
| **Peers** | who is out there at all? | a bootstrap list, then a swarm |

Conflating them is the standard mistake. A DHT finds *peers for an infohash* —
it cannot tell you whether that infohash contains a corpus in your embedding
space, under a licence you may redistribute, at a revision newer than yours.
That is a catalogue question, and it must be answered before any infohash is
worth looking up.

---

## 1. Collections: a signed catalogue

Each node publishes one document describing what it holds:

```json
{
  "node_id": "…",
  "pubkey": "…64 hex…",
  "published_at": "2026-08-20T09:14:03Z",
  "collections": [
    {
      "name": "genome-sacCer3",
      "plugin": "genome",
      "profile": "sacCer3",
      "space_id": "773a4f77a00126541cd66ddb810ce7b8",
      "space_weak": false,
      "documents": 17,
      "chunks": 3459,
      "corpus": {
        "info_hash": "…",
        "magnet": "magnet:?xt=urn:btmh:…",
        "bytes": 3820548,
        "files": 1,
        "snapshot": "2026-08-20T06:00:00Z"
      },
      "share": {"class": "green", "corpus": true, "vectors": true}
    }
  ],
  "sig": "…128 hex, ed25519 over the canonical form…"
}
```

Four properties, each load-bearing:

**Signed with the node key we already have.** `app/bundles/` already signs
vector bundles with an ed25519 node key, domain-separated and length-framed.
The catalogue reuses that machinery exactly — a new signing scheme would be a
new thing to get wrong, and this one has already been reviewed.

**`space_id` is the compatibility check, and it is the first field a consumer
reads.** A corpus in another embedding space is not "slightly different", it
is useless: the vectors do not compare. Publishing the space id means a peer
can discard 90% of a catalogue before moving a byte. `space_weak` is published
alongside because an unconfirmed model hash is a materially weaker claim and
hiding that would be dishonest.

**`share` is computed from the licence gate, never hand-written.** A
yellow-class source that this node acquired under an acknowledgement does not
thereby become redistributable. The catalogue builder must derive this from
`spec.license_class` and the per-plane switches in §8, and a red-class
collection must be *absent* rather than listed as unavailable — its existence
on this node can itself be sensitive.

**The corpus torrent is a snapshot, not a stream.** Torrents are immutable by
construction. A new snapshot is a new infohash and a new catalogue entry;
`snapshot` dates it. That is the honest model, and it is why the mutable-source
work in the LOCKSS evaluation matters — see §4 below.

### Where it is served

Three transports, same bytes, in increasing order of independence:

1. **HTTP**, at `/p2p/catalog.json` on the existing web process —
   unauthenticated (it contains no secrets and is signed), read-only. This is
   all that is needed for a known peer.
2. **Inside the torrent**, as a `catalog.json` at the root of the corpus
   torrent, so a peer who has the bytes also has the description.
3. **BEP 46 mutable torrent item** — an ed25519-signed DHT entry with a
   sequence number, pointing at the current catalogue. This is the piece that
   makes a node findable *without* a URL, and it is deliberately last: it
   reintroduces a long-term secret, which LOCKSS argues hard against, and we
   should not pay that price until there is a network to justify it.

---

## 2. Peers: three tiers, adopted in order of need

### Tier 1 — an explicit peer list (build this, it is what we test)

```toml
# platform.toml
[[peers]]
name    = "alice-nas"
catalog = "https://alice.example:8742/p2p/catalog.json"
pubkey  = "…64 hex…"        # pinned: the catalogue must be signed by THIS key
```

Zero infrastructure, works through NAT in one direction, and is exactly the
"friends list" LOCKSS uses to resist Sybil attacks — its reference lists are
churned *from* a friends list precisely so that an attacker cannot come to
dominate whose opinion you ask. §12 of the architecture says *"don't build the
DHT until someone begs"*; with three peers, a list in a file is not a
limitation, it is the correct design.

Pinning `pubkey` per peer is what makes the catalogue's signature mean
anything. An unpinned signature only proves *someone* signed it.

### Tier 2 — the coordinator as a rendezvous

`app/coordinator/` already exists and nodes already talk to it for leases and
demand-sets. Adding "return the catalogue URLs of nodes that reported recently"
is a small endpoint on a service that is already deployed, already
authenticated, and already knows who is participating. It removes the manual
step of exchanging URLs without introducing a new component.

The coordinator must stay a *directory*, never an authority: it hands out
locations, and every claim is still verified against the pinned key.

### Tier 3 — DHT and peer exchange

Once a peer holds an infohash from a catalogue, libtorrent's DHT and PEX find
the swarm with no help from us — it is already implemented, and currently
switched on by default (`TORRENT_DHT`). This is the only tier that scales past
a social network, and it is also the only one that is free, because BitTorrent
did the work.

Note the ordering: **the DHT is not how you find collections.** It is how a
swarm assembles once a catalogue has told you which swarm you want.

---

## 3. What next week's test should actually do

Two machines, the smallest real corpus on the node (`genome-sacCer3`, 3.8 MB —
one file, one assembly, and a space both ends can verify).

**On the NAS (publisher):**

```bash
python -m app.cli torrent publish genome-sacCer3
# prints: files, bytes, info hash, and a magnet URI
python -m app.cli torrent seed genome-sacCer3
# prints peers / upload rate every 5s until Ctrl-C
```

**On the remote machine (consumer):** a collection whose spec endpoint is the
magnet, then the ordinary acquisition path — no new verbs, because the
transport is just another transport:

```toml
plugin = "genome"
profile = "sacCer3"
acquire_enabled = true
corpus_dir = "/corpora/genome-sacCer3"
```

What the test proves, in order of what is most likely to break:

1. **Reachability.** The seeder must be dialable. Behind NAT, forward 6881/TCP
   and UDP, or set `TORRENT_UPNP=1` (off by default: punching a hole in
   someone's router is not something software should do unasked). If neither is
   possible the DHT will not save you — that is what tier 1 and a direct
   `connect_peer` are for.
2. **Metadata over a magnet.** A `.torrent` file is parsed locally; a magnet
   must be resolved from the swarm, and that is the first thing that fails when
   no peer is reachable. Test the `.torrent` path first, then the magnet.
3. **The `.part` guarantee.** Watch the corpus directory during the transfer:
   no file may appear at its final name until it is complete. This is covered
   by a test, and it is the one whose failure mode is silent corpus corruption.
4. **Bandwidth.** The transfer draws from the same token bucket as everything
   else (a rate lease, as rsync does). Set a `[bandwidth]` block on both ends
   and confirm the Stats page's fetch rate reflects it.
5. **Reciprocity.** Then reverse the roles. A node that only downloads is a
   leech, and the seeding path is the half that has never run against a real
   peer.

Before any of it, on both ends: `python -c "import libtorrent"`. The transport
imports lazily and fails with an actionable message, but finding that out
before you start is cheaper.

---

## 4. The thing this does not solve

A torrent is immutable. Wikipedia is not. Publishing a corpus snapshot as a
torrent is exactly right for `genome-*` and Project Gutenberg — assemblies and
public-domain books do not change — and it is only half an answer for a
maintained corpus, where the interesting question is *what changed since the
snapshot I already have*.

BEP 52's per-file merkle trees are the mechanism that makes the answer cheap:
consecutive snapshots share every file that did not change, so a peer holding
last month's torrent fetches only the difference. Realising that saving needs
the revision-bound uids and content-defined chunking from
[`lockss-p2p-evaluation.md`](lockss-p2p-evaluation.md) §3 — without them a
one-word edit changes every chunk downstream and the diff is the whole corpus.

Which is to say: the transport is done, the discovery design above is
straightforward, and the genuinely hard remaining problem is the mutable-source
one — the same conclusion the LOCKSS evaluation reached from the other
direction.
