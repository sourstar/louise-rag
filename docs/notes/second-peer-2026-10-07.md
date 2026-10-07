# The second peer — step 9 live, 2026-10-07

The first time two Louise nodes talked: the node (TrueNAS) and a second
peer on the development PC, Docker Desktop, run from
[`deploy/docker/`](../../deploy/docker/compose.yaml) — the same image, no
NAS. Every sub-step of step 9 ran end to end over the LAN, and each one
found at least one defect that every unit test had passed. They are listed
here with the fix that landed; the commits carry the detail.

## The setup

- **Second peer**: compose project `louise-peer2`, web on host port 30142,
  swarm on 6882, named volumes; its env file is
  `deploy/reference/dev-peer.env` (not in git: it names the LAN). First
  boot (`app/node_init.py`) made its key `ed25519:9f7c969c…b86a`, its admin
  token and its `platform.toml` (`[node] address` = the PC's LAN address).
  `[dht] enabled = false` on it: the swarm is found through digests alone.
- **Node**: `[node] address = "http://192.168.178.10:30042"`,
  `peer_port = 6881` added to `platform.toml` (copy kept as
  `platform.toml.bak-20261007`); the second peer in `[peers]`, pinned.
- Collections exercised: the certified `gutenberg-744ba8430fed` (Codex
  version 2, fetched as `gutenberg-certified`) and `gutenberg-mini-open`,
  made for the test — open, Codex version 4, the node's granite space, the
  same three fixture books.

## What ran, live

| step | what | result |
|---|---|---|
| 9a/9b | the node serves a shared collection's members as torrents | 5 torrents per snapshot, announced |
| 9c | digests both ways, pinned; events forwarded | both neighbours; attestations kept on both sides |
| 9d | `share fetch` — Codex, snapshot chain, pointer, members | members in seconds, verified, promoted |
| 9f | a fetched collection scanned from its shards, indexed, searched | `ask search village` → 2 hits, certification on each |
| 03 §3.6 | quarantine cleared on the second peer by the tooling | roots, chunks, calibration 16/16; records-vs-origin in the Codex's own image |
| 9e | `share attest` both ways | 6 attestations each way, kept |
| 9e | `share contribute` on the second peer | 2 bundles built, own ladder T0–T2 pass, 2 events |
| 9e | the node takes the bundles in | blocked by the event race below; fixed in 8308cb8 |

## Defects found, each fixed with a test

1. A fresh node that holds no collection never gossiped: the stats beat
   skipped everything before its empty guard.
2. A peer given by hand waited up to an hour for the first digest; a
   recipient not yet sent to now gets it at the next beat.
3. A fetched collection **acquired from the origin**: the second peer's
   worker began an rsync listing of the whole Gutenberg mirror for three
   books. Stopped by pausing the collection before a byte was written; a
   drag source now never acquires.
4. The drag wrapper forwarded Gutenberg's corpus readers: "not indexable"
   on every fetched document, empty titles. Hidden; the stages read the
   records.
5. Promoted record shards waited for the six-hour discovery interval; a
   plain scan is now requested at once.
6. The transfer backed off fifteen minutes after an attempt that had no
   peer to ask, and again after the same peer restarted; a hint not tried,
   or a digest newer than the attempt, now ends the backoff.
7. The certifier's pointer was never put into the DHT: private key second,
   and in libsodium's form where libtorrent takes orlp's expanded secret.
   Failures retried every 10 s; now once per interval.
8. One connection per IP per torrent (libtorrent's default) let a
   connection the node still counted from the PC refuse every new one for
   the calibration member. Several peers share one address behind Docker
   Desktop or a home router: allowed.
9. `[dht] enabled` was read by nothing; the DHT readers polled for alerts
   the session never delivers.
10. A collection shared after the last hourly digest had no swarm hint on
    a DHT-off peer; every neighbour naming a swarm port is now asked.
11. `ack --origin-unchecked` and the licence ack read stdin without a
    terminal and crashed; they refuse and name `--yes`.
12. The attestation step marked every other event acted, racing the
    handlers of bundles, snapshots, codexes and flags — so none of those
    four had ever been acted on live.
13. Small ones: share log lines under the wrong collection, `--name`
    dropped by `hold collection create` (found earlier the same day),
    `up.sh` naming images by the containerd store's index digest.

## Open — decisions for the operator

- **The node's `LOUISE_PLATFORM_IMAGE` is stale.** STATUS 2026-09-14 says
  each deploy sets it to the image the containers start in; the current
  runbook never does, and the node still carries `sha256:85fd59f1…`
  (`louise-rag:codex-pubmed`, 2026-09-21). Every Codex created since names
  an image that did not build it: the certified collection, the open test
  collection, the deposit collections. For a version-4 Codex that image
  cannot even read the Codex, so "records-vs-origin in the Codex's own
  image" is impossible on another peer. The second peer passed the check
  in that image for the version-2 certified collection; for the open one
  it took the documented `ack --origin-unchecked`. Setting the variable
  honestly changes which image may build pubmed's, gutenberg's and
  wikipedia's records (03 §3.4), so it is the operator's call.
- **Version-2 collections advertise no benchmark**, so no peer can lead a
  shard of gutenberg, pubmed or wikipedia under rendezvous: their
  statements name an image, and the digest's row (09 §9.9 v2) names a
  run. Sending the older row form is a wire-format change: ask first.
- **`records-vs-origin` is missing from the certified collection's own
  plan**, so only the quarantine gate asks for it.
