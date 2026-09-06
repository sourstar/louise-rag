# 3. Checking

Trust in Louise comes from reproduction. Anyone with a copy and the
platform can recompute any part of a collection from its origin and
compare. This document says what "compare" means for each part, which
checks exist, how their results travel, and what a peer may conclude
from checks it did not run itself.

## 3.1 Two kinds of truth

**Bytes are exact.** Parsing is deterministic: the same origin file,
the same toolchain image, the same record, byte for byte. Chunking is a
pure function of the cleaned text and the layer's chunker. Hashes are
hashes. A check on any of these either passes or is a proof of failure
that names the shard. There is no "close".

**Vectors are within tolerance.** The same weights on different GPUs,
drivers or batch sizes produce different floats. So a vector is correct
when its cosine against the reference encoder's output is at least the
Codex's `tolerance` — a statistical statement, sampled, never exhaustive
in practice. The gap between "same model, different build" (cosine
above 0.99) and "different model" (0.3 to 0.7) is wide enough that the
test is sharp; the tolerance window is also the widest perturbation an
adversary can hide in, which is why it is kept as tight as calibration
allows.

This distinction runs through everything below: a failing deterministic
check is a proof; a failing statistical check is a request for
reproduction.

## 3.2 The validation plan

Nothing executable travels inside a collection. Checks are platform
code — open, the same for everyone, pinned by the Codex's toolchain
digest — and a collection ships `validation.json`: a **plan** saying
which checks apply and how much to sample.

| check | proves | needs | kind |
|---|---|---|---|
| `roots` | every shard's bytes match the snapshot's merkle roots | the copy | exact |
| `records-vs-origin` | a seeded sample of shards, re-fetched from the origin and re-parsed with the Codex's toolchain, reproduces the records byte for byte | the origin, the platform image | exact |
| `chunks` | recomputing chunks from the records reproduces every chunk id and text | the copy | exact; can be exhaustive |
| `vectors-sample` | a seeded sample of chunks, re-embedded, reaches cosine ≥ `tolerance` on the quorum | records, a calibrated encoder | statistical |
| `calibration` | the Codex's reference vectors reproduce on this peer's encoder | an encoder | statistical |
| `centres`, `labels` | the index's centres reproduce from the declared training; every vector's stored label is its nearest centre | the copy | exact given the platform image |
| `license` | the terms text hashes to the Codex's `terms_sha256` | the copy | exact |
| `signature` | the snapshot is signed by whoever claims it | the copy | exact |
| `anchor` | a certifier's key matches its external anchor (§3.7) | the network | exact |
| `origin-seal` | the origin's own seal on the source file validates, and the record was parsed from that file (§3.7) | the copy, or the origin | exact |

`hold check <collection>` runs the plan and prints a report; the same
report is what the worker produces in the background. Anything a
collection would need that is not in this table is not a script to ship:
it is a check type or a parser contributed to the platform and reviewed
there.

## 3.3 Before contributing: the gate and the benchmark

A peer that wants to embed for a collection first passes the
**calibration** check: it embeds the Codex's reference chunks with its
own encoder and must reach the tolerance on the quorum. This is the
only admission test, and it is per combination — a new driver, a new
build, a new quantisation is a new test. The result is a signed
statement ("this encoder image, on this peer, is compliant with this
space").

The same run yields the peer's **benchmark**: tokens per second on the
reference chunks. It is published with the peer's catalogue digest and
is what §4.5 uses to give large shards to peers that can finish them.

## 3.4 Equivalence: another toolchain, the same collection

The Codex freezes a *reference* toolchain by digest. A newer platform
image — a refactor, a security fix — is not thereby a new collection.
`records-vs-origin` run under the new image, over the plan's sample,
reproducing the records byte for byte is a deterministic proof that the
new image is **equivalent** for this collection; the proof travels as an
attestation, and a peer running the new image contributes as if it ran
the reference. Equivalences live in attestations, never in the Codex;
the identity does not move.

For encoders the word is **compliant**, not equivalent, because the
statement is statistical: an encoder build that passes calibration may
contribute to the space, and stays a distinct producer in provenance.

What a frozen hash does and does not do: it removes ambiguity about
which code produced what. It does not make that code correct. A wrong
parser, frozen, is a wrong parser faithfully reproduced by everyone —
and `records-vs-origin` passes it, because it re-parses with the same
code. Correctness is the business of code review in the platform and,
for certified collections, of the origin's own seal on the bytes
upstream of any parser (§3.7).

## 3.5 Importing a stranger's vectors

Vectors arrive from other peers as signed **bundles**: chunk ids,
vectors, the producer's key, a signature over the bytes. Before a bundle
is used, the importing peer runs a ladder of checks, cheapest first,
each gating the next. Costs are for a bundle of ten thousand chunks
whose production took about half a GPU-hour.

| tier | check | cost | catches |
|---|---|---|---|
| **T0 algebraic** | dimension and dtype match the space; unit norm; no NaN, no duplicates; per-dimension moments inside the space's envelope | tens of ms, CPU | missing normalisation, random or foreign vectors |
| **T1 geometric** | cosine profile against a signed probe set embedded once by the space's author; consecutive chunks overlap by construction, so their cosine must sit in a band measured on the local text | under a second, CPU | wrong model, perturbation |
| **T2 challenge** | golden (text, vector) pairs from the Codex, re-embedded by the producer into the bundle header | none for the importer | misconfiguration — proof of a correct oven, not of each loaf; not a security control |
| **T3 spot recompute** | about sixteen chunks chosen by the importer's private randomness, re-embedded locally, sequential test against `tolerance` | a few seconds, GPU — under one percent of production | bulk poisoning; one importer misses a 5 % poisoned bundle less than half the time, ten independent importers almost never |
| **T4 verify on use** | a poisoned vector is inert until a search surfaces it; the chunks a search actually cites are re-embedded once, asynchronously, and marked verified forever | a few seconds per answer, once per chunk ever cited | targeted poisoning, which sampling cannot find |

The importer's randomness in T3 is private and never derived from the
bundle, or a producer could re-roll a nonce until the sample missed its
corruption. The network carries the safety, not one importer: honest
import costs about a fifth of a percent of production, so fifty peers
importing one shared bundle spend a fiftieth of what fifty peers
embedding it themselves would.

A verified failure is a **proof of forgery** — text, claimed vector,
recomputed vector — and burns the producer's key on the peer that found
it and on every peer that accepts its attestation.

## 3.6 Attestations and trust levels

Every completed check yields an **attestation**: collection, snapshot,
check, seed and sampled shards, result, platform image, encoder image
for statistical checks, the verifier's key, time, signature.
Attestations travel like any event (§4.3) and are stored beside the
catalogue.

A collection's level is computed **locally**, from the attestations a
peer accepts — keys on its trust list count fully, unknown keys a
little, its own checks most. There is no global consensus and no ballot
to stuff.

| level | meaning |
|---|---|
| **unverified** | only the publisher's word; usable, and marked as such everywhere it appears |
| **checked** | at least three independent verifiers passed `roots`, `chunks` and `calibration`, and at least one passed `records-vs-origin` on a sample |
| **verified** | at least eight verifiers, and the union of their samples covers a quarter of shards for `records-vs-origin` and a tenth for `vectors-sample`; the covered fraction keeps rising and is shown |
| **flagged** | a deterministic check failed — one reproduction is enough, and it names the shard; or a statistical check failed twice, from independent verifiers, on the same sample |

A flagged collection stays searchable and is marked; its publisher's key
loses standing on the peer that flagged it. Sybil resistance comes from
the trust list and from weighting a verifier by the contributions of
theirs that others have verified — not from counting keys.

A **corrective sister** (§2.7) carries the flag's cause in its Codex:

```
replaces: {codex: <hash>,
           defect: {check, toolchain: <image under which it fails>,
                    witnesses: [<shard or record ids>], expected, observed,
                    rationale: "<one readable sentence>"}}
```

A peer reproduces the defect before honouring the replacement: it runs
the named check on the named witnesses against the old collection. If it
fails, the old collection is flagged and the sister is the remedy. If it
does not, the sister is an ordinary sister with no claim on anything.
The protocol thereby guarantees that nobody can replace a healthy
collection by pretending a bug. It does not guarantee the fix is good —
that is decided where the parser is reviewed.

## 3.7 Certified collections: the anchor and the seal

For a certified collection the question "where did this packet come
from" is a chain of five links, of which the platform supplies three:

```
external anchor → certifier key → signed snapshot → shard root → bytes
```

Bytes match roots (the transport already checks pieces); roots are
listed in the snapshot; the snapshot is signed. A packet inherits the
signature by hash and needs none of its own. The signature proves that
whoever holds the key signed — not that the key is the Gazzetta's.

**The anchor** is the missing link, and the one Louise cannot supply
alone; it can only make checking it mandatory. The certifier publishes
its key fingerprint under a domain it controls — a well-known URL,
optionally a DNS record — and the domain's TLS and DNSSEC chain is the
anchor. The Codex carries the URL; peers check it at adoption and
periodically. A mismatch makes the collection *open, published by a key
that claims to be X*, shown as such. Key rotation and revocation live at
the same anchor; without revocation a compromised key certifies forever.

**The seal** is stronger than any anchor, where it exists. Official
gazettes seal their electronic editions: the Gazzetta Ufficiale
distributes a certified PDF inside a CAdES envelope, signed by the
officer responsible for the issue; the Official Journal of the EU
carries a qualified seal. Both validate against the EU's List of Trusted
Lists — signed, machine-readable XML the Commission publishes, pointing
at each state's list of trusted certificate authorities — with no trust
in any website. Every record already carries `origin_hash`; the chain
becomes:

```
trusted list → issuer's certificate → seal → origin bytes (origin_hash) → pinned parser → record (content_hash)
```

The record's authenticity then comes from the body that issued the law,
not from whoever built the collection.

Seals expire: certificates lapse, revocation servers go dark. An archive
that cannot prove authenticity in thirty years has failed at the one
thing it was for. So the certifier captures **validation evidence** when
it produces the snapshot — certificate chain, revocation responses of
that moment, a timestamp, the trusted-list snapshot — into
`seals/<snapshot>.json`, one entry per sealed issue, referenced from
each record. `origin-seal` then runs in two modes: *live*, re-fetching
the sealed file and validating against the current trusted list;
*archival*, against the stored evidence, offline, and still working when
the origin is gone. The sealed files themselves may be shared as an
optional member for whoever needs legal standing; the records do not
contain them.

**Reproduction stays mandatory.** For a certified collection over a
public origin, `records-vs-origin` runs regardless of the signature. An
attacker then has to compromise both the key and the origin, and a valid
signature over records that do not match the origin is the proof of
compromise.

## 3.8 Manifest-only: nothing uncertified can exist

In a certified collection only certified content may be embedded or
returned. The rule is enforced where it can be absolute — at the moment
a fetched shard is promoted from staging into the copy: *a shard enters
only if its root is in the current, signed, anchored snapshot*. This is
a refuse-to-start class rule, not a warning.

Everything downstream is defence for a gate that was bypassed — a folder
populated by hand, an old snapshot left on disk:

- **granularity** — a chunk is a pure function of one record, so the
  unit of certification is the shard, the unit of discard is the record
  and all its chunks, and the unit of halt is the space on that peer;
- **halt** — embedding stops for the whole collection and no bundle is
  published, because vectors for chunks outside the snapshot would
  poison everyone's coverage map; the peer marks the collection
  *integrity violated* until it re-fetches and re-checks;
- **search continues** over the certified subset, with the exclusion
  visible in the results — never a silent drop.

Who is at fault is a separate question from what to do, and the answer
does not depend on good faith:

| found | at fault | flagged | caught by |
|---|---|---|---|
| bytes that do not match the claimed root | the sharer served corrupt or forged data | the sharer | the transport, before anything reaches the copy |
| a shard whose root is not in the signed snapshot | someone introduced foreign material | the local source of that shard | the promotion gate |
| a valid, signed shard whose records do not match the origin | the certifier: a pipeline bug or a compromised key | the certifier and the snapshot; the sharer served faithfully what was signed | `records-vs-origin`, `origin-seal` |

The protocol cannot tell a bug from an attack and does not need to: the
response is the same, and the remedy is the same — a later snapshot that
passes. Humans tell the two apart from the anchor's history.

Embedding for a certified collection has three levels, the middle one
costing the certifier only seconds: **certified** (the certifier embeds
and signs — improbable), **endorsed** (any peer embeds; the certifier
runs `vectors-sample` and signs the attestation, which also settles
which of two compliant vector sets is the reference), **open** (any peer
embeds; independent verifiers attest). The Library shows certification
and trust level as two axes — *certified by X · reproduced by N
verifiers* — never as one.
