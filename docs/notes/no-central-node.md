# No central node: a collection outlives everyone but one peer

*Proposal, 2026-09-05. For review — nothing here is built. Amends
`pack-format.md` §7, §11, §13, §14 and `embedding-redundancy.md` §4; the
proposed amendments are numbered 56–60 below for the §17 index once
accepted. The requirement in §1 is the operator's; the analysis is
measured against this node's numbers where it can be, and marked
"estimate" where it cannot.*

---

## 1. The requirement

Once a collection has been adopted by at least one honest peer, it must
be possible to keep it **available** and **up to date** if every other
node disappears — including the node that created it. Not "available
until the next deadline": the surviving peer must be able to produce the
next snapshot, embed it, publish it, and be found by a newcomer, without
any other node's key, endpoint or state.

Call it *single-survivor liveness*. It is stricter than "no single point
of failure", which is usually satisfied by having two of everything. It
says: the protocol must degrade to N = 1 and still be the same protocol.

## 2. What the current design puts in the way

The coordinator is the obvious offender and the least important one.
There are three, and the pack format's own text already argues against
two of them without drawing the conclusion.

**2.1 The coordinator.** `update.coordinator` names a node key or URL;
leases are issued by it; when it is offline, contributors stop. The
design knows: `embedding-redundancy.md` §4 says partitioning "must keep
working when it is offline" and proposes rendezvous hashing for exactly
that. Recognised, designed, not built.

**2.2 The publisher key.** This is the actual central node.
`collection_id` is *publisher key + name*; the manifest is signed by
`publisher`; the DHT catalogue item (BEP 44 mutable) lives under the
publisher's key, and BEP 44 lets only the key holder write it. When the
key holder is gone, **nobody can publish snapshot N+1 under the same
`collection_id`**, and the "latest" pointer in the DHT stops being
refreshed. Another peer can fork under its own key — same records, new
identity, a community split — and that is all it can do. The requirement
is violated by construction, not by an outage.

**2.3 The manifest mixes what never changes with what changes hourly.**
`origin`, `slicing`, `layers`, `spaces[].encoder`, `license` are fixed
at creation. `spaces[].coverage` and the `members` list with the
vectors' info-hashes change with every accepted bundle. A single signed
document that carries mutable state *needs* a single signer — who else
decides which version is current? The signer is 2.2. Remove one and the
other has to go.

The fix for 2.3 is the same fix as for 2.2, and it is the split the
operator has been calling **Codex / Manifest**: an immutable,
content-addressed description that anyone can recompute and therefore
nobody needs to sign, and a mutable coverage view that is gossiped,
local to each node, and never a document with one author. The pack
already does this for one field: `space_id` is *computed* by §3 from
encoder + chunker layers + cleaning version, not declared. The proposal
extends that rule to the collection's identity.

## 3. Why the corpus determinism rule is load-bearing here

The pack requires records to be reproducible byte-for-byte from the
origin (`records-vs-origin`, invariant 11). Until now that was a
*verification* property: a peer can check what it received. Under the
requirement it becomes a *publication* property: two peers who fetch the
origin at the deadline and apply the same Codex produce identical shards
with identical merkle roots. There is no conflict to resolve and no
authority needed to pick a winner — the result does not depend on who
computed it. Permissionless publication of records is a consequence of
determinism, not a new mechanism.

Vectors are not bit-exact (tolerance, not equality), so the same trick
does not apply to them. Two compliant embeddings of the same shard are
two valid artefacts with different producers. §6 turns this from a
problem into the verification budget.

## 4. Redundant computation at scale

Before comparing mechanisms, one fact changes the question: **some
redundancy is mandatory.** The *verified* trust level requires
`vectors-sample` on ≥ 10 % of shards by independent verifiers
(`pack-format.md` §16). The floor is therefore ≥ 1.10×, not 1.00×. A
mechanism that spends complexity driving duplicates to zero is
optimising below the floor and then paying separately for the
verification it eliminated.

| mechanism | source of redundancy | typical factor | N = 1 |
|---|---|---|---|
| coordinator leases (built) | tail collisions only | ≈ 1.02× | **fails** — nobody issues leases |
| competitive bidding on a shared list | disagreement on the ranking (clock skew, propagation) → two winners; plus protocol cost | ≈ 1.05× + machinery (estimate) | works, but the machinery is idle |
| rendezvous hashing (HRW), plain | inconsistent view of live peers: A believes B dead, B is working | ≈ ε = churn not yet propagated, 2–10 % (estimate) | works trivially: the survivor scores highest on everything |
| HRW + coverage map + randomised tie-break | as above, but duplicates are comparable | ≈ ε | works |

The decisive point is the last column but one. With a coverage map that
records producer per chunk (the pack already keeps provenance per
chunk), a duplicate is not waste: the second embedding of a shard,
compared with the first, **is** a `vectors-sample` attestation nobody
had to schedule. Effective waste becomes `max(0, ε − 0.10)`. As long as
the peer view is inconsistent by less than the verification budget, the
waste is zero and the verification is paid for. Bidding spends a
protocol to eliminate duplicates that the trust model then has to
recreate by hand.

Scale, using the Gutenberg number (≈ 31 GPU-days for one node — modelled
from the encoder's measured 3.2 chunks/s; an earlier 213 came from the
pipeline's rate, embedding-redundancy.md):
a hundred peers under HRW with ε = 5 % spend 0.31 days each plus 5 % in
attestations. Bidding reaches the same order with a stateful protocol,
per-block polling, semi-synchronised clocks and a cascade of timeouts to
tune. It buys nothing HRW does not give.

The dominant term is unchanged from `embedding-redundancy.md` §6: a
space re-pin costs 213 days per peer, for everyone, at once. No
assignment mechanism touches it; only the approved-encoder discipline
does, and this node has already paid to learn that (8 % of Gutenberg's
vectors corrupted by an untrialled b10630 build).

## 5. The design

**5.1 Identity — `collection_id` is the hash of the Codex.**

```
codex = {
  kind: "louise-codex", version: 1,
  plugin, origin: <AcquisitionSpec, minus credentials>,
  toolchain: {
    platform_commit: "<git sha>",            -- names the parser, chunker, cleaner, validator
    image_digest: "sha256:…",                -- the platform image: code AND its dependency closure
    encoder_image_digest: "sha256:…"         -- the encoder build; replaces reference_version
  },
  parser: {id, version}, cleaning_version,   -- human-readable aliases of what the toolchain contains
  layers, slicing,
  space: {encoder, dim, dtype, ctx, tolerance, calibration, epoch},   -- epoch: see lineage.md §6
  license: {class, terms_sha256},
  update: {cadence, cutoff: "origin-clock-midnight", grace_days},
  publication: "open",                       -- or "certified"; see certified-collections.md
  continues: <codex hash> | null,            -- daughter; see lineage.md
  derived_from: <codex hash> | null,         -- sister
  replaces: {codex, defect} | null           -- corrective sister
}
collection_id = H(canonical-json(codex))
```

Anyone re-deriving the same parameters gets the same id: they are the
same collection, wherever they started. A different encoder or chunker
is a different Codex and therefore a *derived* collection, exactly as
§17 already says — the rule does not change, only where the id comes
from. The creator's key is recorded as an *attestation* ("first
published by") in the catalogue, not as part of the identity.

**The toolchain is frozen by hash, not by version.** Nothing executable
is in the Codex, as §15 requires: the peer runs its *own* platform code,
and the hashes tell it whether that code is the one the Codex demands.
This is rule 1 of the pack ("the hash is the truth; a version number is
a hint") applied to the tools. `parser: {id, version}` stays as a
readable alias; it no longer carries the guarantee. Three points make
this workable rather than brittle:

- **Hash the closure, not the file.** The bytes a parser produces depend
  on the extraction library, the interpreter and the tokenizer at least
  as much as on the plugin source (`certified-collections.md` §7). An
  image digest — the platform is already container-based — pins all of
  it at once. The encoder gets the same treatment: this node's 8 % of
  corrupt Gutenberg vectors came from an encoder build that an
  *approved list* named by label (b10603) while the node ran b10630. A
  digest in the Codex makes that impossible to do by accident.
- **Equivalence by attestation, or every refactor forks the
  collection.** With the toolchain hash inside the Codex, a changed
  comment in the parser would be a new `collection_id`. So the Codex
  freezes the *reference* toolchain, and any other toolchain must prove
  itself equivalent: `records-vs-origin` run with a different image
  that reproduces the sampled records byte-for-byte is a deterministic
  check and therefore an attestation under §16 ("image Y is equivalent
  to the reference for this collection"). Equivalences live in the
  attestations, not in the Codex; the identity does not move. The
  platform's golden tests (`tests/capture_golden.py`) are the same
  mechanism run locally before a release.
- **The hash removes ambiguity, not bugs.** A wrong parser, hashed and
  frozen, is a wrong parser faithfully reproduced by everyone forever —
  and `records-vs-origin` *passes* it, because it re-parses with the
  same code. The check proves fidelity to the pipeline, not to the
  "true text". No hash fixes that; code review of the plugin in the
  platform does (§15: "reviewed like any code"), and for certified
  collections the origin's seal on the bytes *upstream* of the parser
  does (`certified-collections.md` §4.2). Anyone reading "frozen hash"
  as "correct by construction" has read it wrong.

**5.2 Snapshots are content-addressed and permissionless.**

A snapshot is `{collection_id, snapshot_id, previous, shards: [{slice,
n, root, ids, records, chunks}], deltas}` where `snapshot_id` is the
cadence's deterministic label ("2026-09") and `previous` is the hash of
the prior snapshot. Any eligible peer may produce it. Because records
are bit-exact, every honest producer produces the same bytes; the
snapshot's hash is its identity and signatures on it are attestations
("I computed this too"), which raise its trust level under §16
unchanged. Two different snapshots for the same `snapshot_id` are a
proof that one producer's parser drifted — a deterministic check fails,
the shard is named, the producer is flagged. Nothing to arbitrate.

**5.3 Coverage is gossiped, not signed as a document.**

The mutable half — which shards have compliant vectors, from whom, and
the per-document bitmap of `embedding-redundancy.md` §3 — is the
coverage map: per node, assembled from signed bundles and attestations,
exchanged by the anti-entropy digests of §13. There is no canonical
coverage document because there is no canonical author. A node's view
may lag another's by one gossip interval; that lag is the ε of §4.

**5.4 Partitioning — weighted rendezvous hashing with per-shard
eligibility.**

For each uncovered shard `s` and each peer `p` it believes live, a peer
computes `score(p, s) = H(pubkey_p ‖ shard_id_s) ^ (1 / w_p)` and works
on the shards where its own score is highest — weighted HRW, the
standard construction. The weight `w_p` is the peer's **benchmark**:
tokens/s on the calibration chunks, measured once at backend-gate time
and published with the peer's catalogue digest. A peer with half the
throughput is half as likely to be top on any shard, which is what
"large blocks go to strong peers" means without anyone assigning them.

Eligibility is a filter before the hash: a peer is eligible for `s`
only if `chunks_s / w_p × margin ≤ limit_s`, with `margin` ≈ 3 (this
node's reference is ~1 GPU-hour per 7,000-chunk shard; a peer that
would take more than three hours on that shard is not eligible for it).
Small shards admit weak peers; large shards admit few. Every peer with
any capacity is eligible for something. Backlog needs no separate
queue: a peer does not choose, the hash chooses, which is a blind queue
by construction.

HRW yields a full ordering of eligible peers per shard, not just a
winner. The top peer has `grace` to publish a first partial bundle
(§5.5); if it does not, the second proceeds. This is the cascade the
operator wanted from bidding, with the ranking coming from a hash
instead of a negotiation, and with no first gate — the first bundle *is*
the confirmation.

**5.5 Publish early, publish partial.** A bundle every few hundred
chunks (`embedding-redundancy.md` §5). An abandoned shard loses minutes,
not the hour, and the coverage map sees progress rather than promises.
This is what makes the cascade in 5.4 cheap enough not to need a
protocol.

**5.6 Duplicates are attestations.** When a node receives a second
compliant bundle for chunks it already has vectors for, it compares them
(cosine against the first producer's) and emits a `vectors-sample`
attestation naming both producers. The bundle is kept if the node has
the space, dropped if not; the attestation is kept either way. The
coordinator's `producer_stats` becomes a local, gossiped view rather
than a server table.

**5.7 Fetching the origin at the deadline is HRW too.** Key
`(collection_id ‖ snapshot_id ‖ "fetch")`; the top eligible peer fetches
the delta and publishes the record shards; everyone else fetches from
it and verifies a sample against the origin. `grace_days` in the Codex
is the cascade window. The origin is asked once per snapshot in the
common case, which is the other thing Louise promised.

**5.8 Discovery without a key.** Snapshot ids are deterministic from
the cadence and the cutoff (origin-clock midnight, already decided). A
peer looking for the current snapshot announces under and queries
`H(collection_id ‖ snapshot_id)` — many announcers under one
deterministic key, the way BitTorrent finds peers for an infohash, with
no owner. The BEP 44 mutable item under the publisher's key is dropped
for open collections; a newcomer months later finds the survivor by
computing the key, not by asking anyone. The anti-entropy digests of
§13 remain as they are and cover the case where the DHT is unreachable.

## 6. What this leaves for the coordinator

Nothing required. It stays useful as an *optional* accelerator for the
same reasons `embedding-redundancy.md` §4 gives — credit accounting,
statistics, a demand feed — and a node that cannot reach one behaves
exactly as if it did not exist. `update.coordinator` leaves the Codex.

## 7. Single-survivor check, mechanism by mechanism

| step in the lifecycle (§0.4) | needs | with N = 1 |
|---|---|---|
| initiate | origin, platform | the survivor already did, or re-derives |
| advertise | DHT or one peer | announces under the deterministic key; found when someone looks |
| fetch a part | a peer with the bytes | the survivor |
| embed | records, calibrated encoder, weights of live peers | the survivor scores highest on everything |
| verify | records, encoder | own checks count most (§16 unchanged) |
| update | origin, Codex | permissionless; the survivor is top HRW for "fetch" |
| derive | records by hash, a new Codex | unchanged |

No step names a specific other node.

## 8. Proposed amendments

56. **`pack-format.md` §7** — the manifest is split: a content-addressed
    **Codex** (immutable; `collection_id = H(codex)`) and a
    content-addressed **snapshot** (records only; `snapshot_id`
    deterministic from cadence). `publisher` and `signature` become
    attestations, not identity. `coverage` and the vectors `members`
    leave the document and live in the gossiped coverage map.
57. **`pack-format.md` §11** — leases are optional; the required
    partitioning is weighted HRW with per-shard eligibility (§5.4), the
    weight being the backend-gate benchmark published in the catalogue
    digest. Partial bundles every few hundred chunks are mandatory.
58. **`pack-format.md` §13** — open collections announce under
    `H(collection_id ‖ snapshot_id)`; the BEP 44 mutable item under the
    publisher key is retained only for certified collections
    (`certified-collections.md` §4).
59. **`pack-format.md` §14** — "initiator" and "publisher" are roles any
    eligible peer plays for any snapshot; the cutoff is the origin's
    clock at midnight; the fetch is HRW-assigned with `grace_days` as
    the cascade window.
60. **`pack-format.md` §16** — a second compliant bundle for the same
    chunks yields a `vectors-sample` attestation naming both producers;
    duplicates are the verification budget, not waste.
61. **`pack-format.md` §7, §15 and `approved-encoders.md`** — the Codex
    freezes the reference **toolchain by hash** (platform image digest,
    encoder image digest); `parser.version`, `chunker.version` and
    `reference_version` become readable aliases without guarantee.
    `records-vs-origin` run under a different image that reproduces the
    sample byte-for-byte is a deterministic **equivalence attestation**;
    equivalences live in attestations, never in the Codex. The approved
    encoder list is superseded by the digest in each Codex.
    (`certified-collections.md` renumbers its amendments 62–66.)

## 9. Open questions

1. **Canonical JSON for the Codex hash.** Which canonicalisation
   (RFC 8785 or a stricter subset)? A single byte of difference is a
   different collection; the choice is permanent.
2. **Peer-liveness view for HRW.** The catalogue digests carry it, but
   at what interval, and does a peer count as live one interval after
   its last digest or three? This sets ε.
3. **Weight publication.** The benchmark is self-reported. A peer that
   overstates it wins shards it cannot finish; the cascade covers it,
   but the coverage map should record delivered vs. declared throughput
   so the weight can be discounted locally.
4. **Backlog at launch** — a new Codex over a large corpus starts with
   everything uncovered. HRW handles the assignment; what it does not
   handle is *ordering*: should the newest period be covered first so
   the collection is useful early, or the largest shards first? A
   per-Codex `priority` rule, or leave it to the tie-break.
5. **Migration.** Existing `gutenberg` and `pubmed` manifests carry a
   publisher-keyed `collection_id`. `tools/migrate_manifest.py` would
   emit the Codex and the first content-addressed snapshot; the old id
   stays as an alias in the catalogue for one cadence.
6. **Equivalence for the encoder.** Parser equivalence is bit-exact and
   therefore a proof. Encoder equivalence is statistical (cosine ≥
   `tolerance` on the calibration chunks — today's backend gate). Is a
   passed gate enough to call an encoder image "equivalent" in the
   attestation sense, or does that word need reserving for deterministic
   equivalence, with the encoder case called "compliant" as now? Two
   words for two guarantees is the safer choice; the document above uses
   "equivalent" only for the parser.
