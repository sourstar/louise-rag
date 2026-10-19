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

## 2. The embed stage ignored rendezvous — fixed, not deployed

`embed = "all"` embedded every part, beside peers contributing the parts
they lead. Option 1 (the node's gutenberg embed set to on-demand, cron jobs
5/6 disabled) is live; option 2 (`share.leading`: the stage leaves another
live peer's parts to it) is pushed as 45535ea + 2e28bbf, reviewed, NOT
deployed — the operator's word after this review. Numbers in
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
(2026-10-19). DHT off on both peers; the node's fix (member torrents with
`disable_dht|disable_lsd`, one per-snapshot swarm key) is 0cf9d68, pushed,
not deployed.

## 6. A served slice list can silently stall a fetch

The node served gutenberg's `lang=la` and `lang=eo` only (`share_slices`);
a peer fetching every slice waited forever on the first part nobody served
— metadata comes from the snapshot, so the 120 s "no metadata" bound never
fires. Fixed by serving all slices (2026-10-19); the stall itself is open:
a wanted part that no neighbour's digest offers should be deferred and
said, not waited on.
