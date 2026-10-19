# The DHT flood (2026-10-19)

## What happened

On the morning of 2026-10-19, new connections from every device in the
house failed, GitHub's first among them. GitHub was not the cause. The
NAS's conntrack table held 11,731 flows, then 14,194, of which 9,910 were
UDP. They came from 172.16.3.3 (`ix-louise-rag-worker-1`) to thousands of
distinct addresses on DHT ports (6881, 51413, …). The FRITZ!Box keeps a NAT
entry for each flow, and its table was full.

The worker's libtorrent session had the public DHT on and seeded gutenberg's
current snapshot: **3,170 torrents**, one per member file (09 §9.16). Those
are 1,637 record parts (median 5.4 MiB, 50 books each) plus vector bundles
and the other members. libtorrent announces every torrent it holds by its
own infohash, about every 15 minutes (`dht_announce_interval`). Each
announcement is a `get_peers` walk over tens of DHT nodes, then
`announce_peer` to the closest eight. Our torrents are not auto-managed
(app/acquire/torrent_publish.py `seed`, 2026-10-08), so `active_dht_limit`
does not cap them.

Estimated, not measured: 3,170 walks × tens of nodes / 900 s is a few
hundred new UDP flows per second, each held for the conntrack timeout. That
gives about ten thousand open flows, which is what was observed. 04 §4.2 asked
for two announcements per snapshot (the collection and lineage keys), and
the code made exactly those. The other 3,170 were libtorrent's own.

The cost grew with the parts served. Sharing pubmed-v8 (1,358 parts) and
normattiva (7,459) would have made it about four times worse.

## Immediate measure

At 09:07 the DHT was turned off in the node's platform.toml (`[dht] enabled
= false`; backup `platform.toml.bak-20261019-dht`), and the worker was
restarted. Conntrack went from 3,179 flows (2,208 of them the worker's UDP)
to 42 by 09:13, and GitHub answered 5 of 5 probes in every sample.
Peers still reach the node through `[peers]` and digests (`hints_for`).

## The fix (operator's choice, 2026-10-19: "we cannot make a DOS attack by design")

- A member torrent is seeded with `disable_dht | disable_lsd`
  (`seeding.Seeder.ensure` → `torrent_publish.seed(announce=False)` for
  any entry that names a member). A published corpus, which strangers
  fetch by magnet, keeps both.
- A third key per snapshot, the **swarm key**
  `H(collection_id ‖ snapshot_id ‖ "swarm")`, is announced with the swarm's
  port: `[node] peer_port`, else the session's listening port
  (`seeder.sync`, `seeder.swarm_port`).
- A fetcher dials what the swarm key holds as well as what digests name
  (`transport.swarm_hints`). It probes at most once per announcement
  interval per snapshot, so a beat that found nobody does not wait again
  for 15 minutes.
- 04 §4.2 and 09 §9.16 are amended. A sharer makes three announcements per
  snapshot, however many members it serves.

Still per file, bounded by concurrency rather than by the parts served: a
fetch's download handle (`transport.fetch_file`, one file at a time) keeps
the session's DHT, and an origin acquisition by torrent finds its swarm by
the infohash, as any BitTorrent client does.

Tests: tests/acquire/test_seeding_peer.py (a member is seeded off the DHT,
a corpus on it); tests/share/test_seeder.py (the swarm key, its port,
once per interval); tests/share/test_single_survivor.py (a newcomer that
knows no one takes the members through the swarm key, with no hint). That
last test failed with the per-torrent flags alone, which is why the key exists.

## Before the DHT goes back on

Deploy, set `[dht] enabled = true`, and watch the NAS's conntrack count
and the worker's UDP flows for an hour. Expected: the routing table's upkeep
plus three announcements per shared snapshot every 15 minutes, which is
hundreds of flows, not thousands. Record the numbers here.

## After the fix (deployed f85a606, 2026-10-19)

The DHT went back on at 10:35 (`/root/dht-on.sh`), watched for an hour with
an automatic revert above 3,000 flows. The node seeds the same 3,170
gutenberg torrents and now announces only the three keys of its snapshot:

| time  | conntrack flows | the worker's UDP flows |
|-------|-----------------|------------------------|
| 10:35 (DHT off) | 107 | — |
| 10:36 | 110 | 43 |
| 10:37 | 95  | 44 |
| 10:38 | 73  | 15 |

That compares with 14,194 flows (9,910 UDP) before the fix. The rest of the
hour is in `/root/dht-watch.log` on the node.
