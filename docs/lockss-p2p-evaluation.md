# LOCKSS as a reference protocol for the peer layer — evaluation

Requested 2026-08-19; written 2026-08-20. Concerns architecture-v2 §5 (P2P
exchange), §6 (verification ladder) and §9/§8 (mutable sources). **Nothing
in this document has been implemented** — it is an assessment, and §5's
Phase-2 plan stands until amended.

Presented copy: <https://claude.ai/code/artifact/1824d217-d367-402b-8ab4-1b6b697d77f1>

---

## Verdict

| | |
|---|---|
| The protocol's **shape** (audit → repair → alarm) | **Adapt.** Right skeleton; we have nothing like it. |
| Its **security argument** | **Do not inherit.** It is statistical in the peer count, and we will not have the peers. |
| The **mutable-source** idea | **Build now.** It is the strongest part and needs no network at all. |

---

## 1. What the GLN actually does

~80 research libraries, each running a node on its own hardware, preserving
subscribed journals. Membership is paid; publishers opt in with a
machine-readable permission statement; each node crawls from its own
subscribing IP. A **closed, credentialed** network, not an open swarm — several
of its choices only make sense in that light.

Each node periodically calls an *opinion poll* on an Archival Unit it holds,
inviting a random subset of recently-encountered peers. Each invitee hashes its
own copy with a fresh nonce (proof of possession, not a cached digest) and
votes. Outcomes:

- **landslide win** — near-unanimous agreement; sleep until the next poll
- **landslide loss** — near-unanimous disagreement; fetch a repair from a
  disagreeing voter and re-tabulate
- **anything between** — *inconclusive*; raise an alarm for a human. There is
  deliberately no automatic resolution of a split.

Parameters as simulated (SOSP 2003): `N=20` inner circle, `Q=10` quorum,
`D=3` maximum minority, `E=4` polls of silence before eviction, `R=3 months`
mean inter-poll interval.

Three design principles worth stealing regardless:

- **No long-term secrets.** Keys need storage that cannot be replicated,
  audited, repaired or regenerated, and over decades they leak.
- **Use inertia.** Preservation has no need for speed; rate-limiting means an
  attacker cannot apply more effort than the victims do.
- **Intrusion detection is intrinsic.** Bimodal behaviour puts a "moat" of
  nearly-unreachable states around the good ones; entering one rings a bell.
  The detector is the protocol, not a layer above it.

Notably absent: any reputation ledger. LOCKSS argues *against* third-party
reputation — it invites slander, and an attacker can bank good behaviour
cheaply and spend it once, expensively.

## 2. The decisive mismatch: two planes

A LOCKSS poll compares hashes for equality. That works because loyal peers hold
the same bytes. Ours do not, structurally:

| Plane | What a peer holds | Comparable how | Poll applies? |
|---|---|---|---|
| **Content** — cleaned text, chunk ids, per-uid raw hashes | bit-identical by construction (invariant 3; `CHUNK_VERSION` pins the function) | exact equality | **yes, exactly** |
| **Vector** — the embeddings | numerically close, never identical (dtype, batch shape, backend, GPU non-determinism) | calibrated tolerance | **no — every poll splits** |

So: **poll the content plane, never the vector plane.** A peer can prove it
holds the same *text revision* cheaply and exactly. If two nodes agree on the
text and their vectors disagree beyond tolerance, that is a specific and
actionable finding — more than a bare hash mismatch ever tells you.

This also bounds the cost. LOCKSS voters re-hash the whole AU per poll; fine for
text. A vote that required **re-embedding** would be unaffordable and would let
any peer make every other peer burn GPU on demand. Polls must be answerable from
hashes already stored — a hard constraint, and satisfiable.

## 3. Mutable sources, sharpened

A changed hash on Wikipedia is not corruption — but by hash alone it is not
self-evidently a legitimate change either: "the article was edited" and "a peer
corrupted its copy" are the same event. LOCKSS resolves this by fiat (the
publisher's site is authoritative). We have a better disambiguator for free:
**the origin publishes a revision id.** Bind the uid to `(page_id, revid)`:

| Revision | Content hash | Means | Action |
|---|---|---|---|
| differs | differs | the source changed | **supersede** — re-embed affected chunks, delete-then-insert on the new revision |
| same | differs | a copy is wrong | **repair** — the corruption case, now unambiguous |
| same | same | agreement | nothing |

Supersede and repair must be **separate code paths with separate provenance**,
or the manifest cannot later say whether a vector was replaced because the world
changed or because a peer lied.

### Cost, and the prize

English Wikipedia takes ~173,000 edits/day across ~7.2M articles. Re-embedding
*whole articles* on change is on the order of a GB of text per day — a permanent
substantial GPU load for a volunteer-maintained corpus. Wikimedia's figure for
*net new encyclopedic text* is ~11 MB/day. The gap between those two numbers is
the prize, and it is claimable because chunking is deterministic: if a chunk's
cleaned text is unchanged, its vector is unchanged.

**Re-embed changed chunks, not changed documents** — cost then scales with the
size of the edit, not the size of the article. (The true changed-chunk volume
lies between those bounds and needs measuring; 11 MB/day is net-new, not
net-changed.)

### The catch

Fixed-width chunking defeats this. Our chunker cuts fixed character windows over
cleaned text, so inserting a paragraph near the top shifts every later boundary
and a one-word edit can change every downstream chunk. The saving needs
**content-defined chunking** (rolling-hash boundaries, as rsync/borg use), where
an edit perturbs only its neighbourhood.

That bumps `CHUNK_VERSION` and re-embeds every corpus once. Worth it *only* if
mutable sources are a first-class case — and cheaper now than after the corpora
grow.

## 4. Where the guarantees stop

LOCKSS's resistance claim (an adversary holding a third of peers raises the
chance of serving damage by a few percent, over decades) comes out of sampling
20 voters from a churned reference list. §12 of our own doc says thirty
contributors in year one would be success, and to assume we are the entire
community for six months. **You cannot draw an inner circle of 20 from a network
of 3.** With five peers there is no distinction between a landslide and a split;
every disagreement is inconclusive, and inconclusive means alarm.

What a small-network audit does buy:

- **Damage detection** — bit rot, truncated imports, corrupt Parquet, bad
  transfers: caught and repairable. This is most of the practical benefit.
- **Not attack resistance** — with a handful of peers, an adversary controlling
  two controls the vote. Proof-of-effort does not fix that; it exists to stop an
  attacker *outspending* the network, not to substitute for having one.

Claiming otherwise is the worst outcome: a protocol that looks Byzantine-
resistant and isn't changes how much you trust its output.

**Where we already contradict LOCKSS:** we use long-term secrets (ed25519 node
keys, signed bundles) and third-party reputation (trust-on-first-use producer
keys, quarantine). Neither should be reversed — signing is what makes a bundle
useful to a stranger, quarantine is what makes verify-on-use actionable — but
the attack LOCKSS names is a real known limit: a producer can bank honest
bundles and spend the history on one poisoned batch. T4 verify-on-use plus
overlap sampling **bounds** that; it does not refute it.

## 5. Torrent transport

Phase 2 already names BitTorrent v2 with signed snapshots and origin webseeds,
and `app/acquire/transport_torrent.py` is a stub. The plan is right; one piece
matters more than the rest.

| Mechanism | Gives us | Verdict |
|---|---|---|
| **BEP 19 WebSeed** | the origin's plain HTTP URL as a permanent seed — solves cold-start completely, using URLs already in every `AcquisitionSpec` | **first, and alone if need be** |
| **BEP 52 (BTv2)** | SHA-256 and a merkle tree *per file*: files independently verifiable, identical files deduplicate across torrents — consecutive Wikipedia snapshots share everything unchanged | **yes — the dedup is the mutable-source case** |
| **BEP 46 mutable torrents** | ed25519-signed DHT pointer with a sequence number → "signed mutable pointer to an immutable snapshot", exactly the shape a maintained corpus needs | yes, but re-introduces a long-term secret |
| **DHT discovery** | trackerless discovery at scale | not until someone begs (§12's words) |

Two integration facts:

- **Invariant 5 needs a bridge, not an exception.** libtorrent brings its own
  threads and rate limiter. The precedent fits exactly:
  `acquire/budget.py::rsync_reservation` withdraws a slice of the bucket's
  refill rate and hands it to a subprocess we cannot meter per byte.
  libtorrent takes the same treatment via `download_rate_limit` — a lease, not
  a bypass.
- **The dependency is not small.** libtorrent-rasterbar is a large C++ surface
  with Python bindings; prebuilt wheels keep image builds fast, but it wants its
  own state dir, ports and concurrency model. An argument for staging behind
  webseed-only HTTP first.

## 6. Alternatives

| Option | Fit | Why |
|---|---|---|
| **Signed bundles over HTTPS + mirrors** (Phase 1, today) | **keep as backbone** | Works now, no new runtime, HF/Academic Torrents distribute free. Everything below is an addition, never a replacement. |
| **Merkle checkpoints + witness co-signing** (CT lineage: Sigsum, Go sumdb) | **best fit for the audit plane** | Tamper-evidence over "what each node claims to hold" *without needing a quorum to be meaningful* — works with two participants, which sampled voting does not. Small to implement, composes with signatures we already produce. The modern answer to LOCKSS's 2003 problem. |
| **IPFS** | no | Right instinct on content addressing, but pinning economics, GC and DHT churn are hostile to cold multi-decade archives, and IPNS mutability is slow and unreliable — the exact axis we care about. |
| **Hypercore / Dat** | interesting, wrong ecosystem | Append-only signed logs are versioned by construction, which maps beautifully onto mutable sources. But a Node runtime in the container and a small community — ecosystem risk for a project premised on outliving its sponsor. |
| **Syncthing / rsync between friends** | fine for two nodes | No audit protocol, no poisoning story. Reasonable for splitting a uid range with one friend; nothing to build on. |
| **Running actual LOCKSS software** | no | LOCKSS 2.x is Java microservices on Kubernetes. Against a project whose selling point is one `docker compose up`, a non-starter. Adopt the ideas, not the implementation. |

## 7. Recommendation

Ordered so each stage is useful alone and none depends on later ones existing.

1. **Revision-bound uids for mutable sources** *(no peers required)* — bind
   Wikipedia uids to `(page_id, revid)`; add a *supersede* path distinct from
   repair, with its own provenance. The only item that improves the node running
   tonight: the Wikipedia collection currently goes stale with no mechanism to
   notice.
2. **Content-defined chunking** *(no peers required, enabling)* — rolling-hash
   boundaries. Costs one full re-embed at the `CHUNK_VERSION` bump. Do it before
   the corpora grow, or accept that step 1 degrades to whole-article re-embeds,
   ~100× the GPU for the same freshness.
3. **A co-signed possession checkpoint** *(two peers is enough)* — each node
   publishes a signed Merkle checkpoint over `(uid, revision, content hash)`;
   peers co-sign what they have seen. This is the LOCKSS audit in the form that
   survives having three peers instead of eighty. Divergence is adjudicated by
   the table in §3 and repaired from a peer or the origin — with an alarm, never
   a silent auto-resolve, when adjudication is ambiguous.
4. **Webseed-first torrents** *(when there is a swarm)* — BTv2 with the origin
   as webseed; mutable pointers when there is something to point at. Deferred
   not because it is hard but because a swarm of one is an expensive way to
   download over HTTP.

## 8. Explicitly not

- **Proof-of-effort puzzles.** Memory-bound functions exist to stop an attacker
  outspending the network. At our scale it is pure electricity — on a node whose
  operator switches embedding off to avoid heating the apartment.
- **Polling the vector plane.** It cannot succeed; votes disagree in the last
  bits for legitimate reasons and the protocol has no vocabulary for "close
  enough". T3/T4 already cover this correctly.
- **Sampled voting with an N we do not have.** Either compare with every peer
  (which is what a handful means) or do not call it sampling. Borrowing the
  parameters without the population borrows the appearance of a guarantee.

---

### Sources

- Maniatis, Rosenthal, Roussopoulos, Giuli, Baker, Muliadi, *Preserving Peer
  Replicas By Rate-Limited Sampled Voting in LOCKSS*, SOSP 2003 —
  <https://arxiv.org/abs/cs/0303026>
- LOCKSS Program, *Global LOCKSS Network* — <https://www.lockss.org/gln>
- LOCKSS Program, *Software* (2.x architecture) —
  <https://www.lockss.org/about/software>
- *Wikipedia:Statistics* — <https://en.wikipedia.org/wiki/Wikipedia:Statistics>
