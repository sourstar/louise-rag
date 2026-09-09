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
when its cosine against a compliant encoder's output for the same chunk
is at least the Codex's `tolerance` — a statistical statement, sampled,
never exhaustive in practice. The encoder on the other side of that
comparison is the checking peer's own, admitted by the gate of §3.3:
nobody holds the reference build's output for an arbitrary chunk. Two
compliant builds can therefore sit twice as far apart as either sits
from the reference, and the tolerance is chosen knowing that (§3.3). A
cosine is computed in f32: both vectors are upcast from the space's
`dtype`, re-normalised, and multiplied in f32; there is no other
precision and no storage penalty anywhere. Measured on the reference
deployment, the same weights on another vendor's build land above
0.9995 and a different model below 0.7; the gap is wide enough that the
test is sharp. No number in this paragraph is a threshold: `tolerance`
comes from calibration and from nowhere else. The tolerance window is
also the widest perturbation an adversary can hide in, which is why it
is kept as tight as calibration allows.

This distinction runs through everything below: a failing deterministic
check is a proof; a failing statistical check is a request for
reproduction.

**Three outcomes.** A check ends in one of three ways: *pass*, *fail*
or *inconclusive*. Inconclusive means the check could not run — the
origin unreachable, the encoder unreachable or past its bounded wait,
the manifest locked, a sampled shard not held by this peer. It is
neither a failure nor a pass: it produces no attestation and no flag,
it is reported with its reason, and it is retried. A check never waits
without a bound on an origin or an encoder; past the bound it is
inconclusive. `hold check` prints the report and exits by 07 §7.7: 0
when every check passed, 1 when any check failed, 3 when none failed
and any was inconclusive. A statistical check that fails is reported as
a fail; what a peer concludes from it is §3.6.

## 3.2 The validation plan

Nothing executable travels inside a collection. Checks are platform
code — open, the same for everyone, pinned by the Codex's toolchain
digest — and a collection carries a **plan** saying which checks apply
and how much to sample. The plan is the Codex's `validation` block
(02 §2.1): `checks` names the checks that apply, from the table below
and nothing else, and `sample` says, per sampled check, how much is
sampled. `validation.json` in the drag (02 §2.6) is that block written
out for tools, and `hold check` refuses a copy where the two differ.
Nothing outside the Codex decides how much is sampled: a publisher who
wants weaker checks has to publish a different collection. A `checks`
entry that names no row of this table is refused at `hold collection
create` and at `hold check` alike.

| check | proves | needs | kind |
|---|---|---|---|
| `roots` | every shard's bytes have the `root` the snapshot lists — the BitTorrent v2 merkle root of the stored file (09 §9.5) | the copy | exact |
| `records` | every sampled record's `content_hash` recomputes from its own title and sections and it carries an `origin_hash`; each sampled shard's `records_root` recomputes from its records' `content_hash` values in order (09 §9.5) | the copy | exact |
| `records-vs-origin` | a seeded sample of shards, re-fetched from the origin by the locator each record carries and re-parsed with the Codex's toolchain, reproduces every record line of the shard byte for byte — the decompressed lines in order, not merely the same `records_root` | the origin, the platform image | exact |
| `chunks` | recomputing chunks from the records reproduces each sampled shard's `chunks_root` and its `chunks` count (09 §9.4) — deriving each document's layer from the Codex's rules, never reading it from the record, which carries none (02 §2.3, 06 §6.3), so a wrong layer is caught rather than reproduced | the copy | exact; can be exhaustive |
| `vectors-sample` | in a seeded sample of shards, sixteen chunks each, re-embedded with this peer's compliant encoder, reach cosine ≥ `tolerance` on the calibration quorum (§3.3) | records, a calibrated encoder | statistical |
| `calibration` | the calibration set's reference vectors reproduce on this peer's encoder — the gate of §3.3, run for this collection's space | an encoder | statistical |
| `centres` | re-training from the declaration in `vectors/<space>/index.json` (02 §2.4) yields centres each within cosine ≥ `tolerance` of the published centre at that index version | the copy | statistical |
| `labels` | every vector's stored label is its nearest published centre at the label's index version, ties to the lower centre index | the copy | exact given the published centres |
| `figures` | every plate the sampled shards' records name is in the shard's figures container and hashes to what the record line says (09 §9.5); a plate the peer does not hold is not a failure — a figures member is fetchable or not — and is reported as not held | the copy | sampled |
| `license` | the terms member `LICENSE-TERMS` hashes to the Codex's `license.terms_sha256` (09 §9.13); absent for a green collection | the copy | exact |
| `signature` | the snapshot's sidecar carries a valid signature by the key that claims it (09 §9.4) | the copy | exact |
| `anchor` | a certifier's key is listed, valid and unrevoked at its external anchor (§3.7) | the network | exact |
| `origin-seal` | the origin's own seal on the source file validates, and the record was parsed from that file (§3.7) | the copy, or the origin | exact |
| `evaluation` | the score in `space.evaluation` (02 §2.4) reproduces on this copy's chunks when its suite is public, and every target the suite names resolves to a chunk in the copy | the copy, the suite | statistical |

**The seeded sample.** Every sampled check draws its sample by one
rule, `sample(seed, n, universe)`: sort the universe by id in UTF-8
byte order, score each id by HMAC-SHA256(seed, id) read as an unsigned
64-bit integer from the first eight bytes of the digest, big-endian,
and take the n ids with the lowest scores. The universe of a shard
sample is the snapshot's shard list, an id being `<slice>/<n>`; the
universe of a chunk sample is the chunk ids of the shard sampled, in
which case n is sixteen. The seed is 32 random bytes chosen by the
verifier from OS entropy — or given by hand as `--seed` — and published
in the attestation; the plan fixes n or a fraction, never the seed. So
two verifiers never draw the same sample unless they mean to, the union
of samples grows with every verifier, and anyone can recompute from an
attestation exactly which shards it covered. `sample` gives the
fraction of shards per sampled check as a decimal string; the default
is `"0.05"`, always at least one shard.

`hold check <collection>` runs the plan and prints a report; the same
report is what the worker produces in the background, once per cadence
for every collection it holds, with a fresh seed each time, and keeps
in the collection's state directory (07 §7.8), where `hold collection
show` displays the last one. Every check the worker or the command
completes yields an attestation (§3.6), which `share` publishes for a
shared collection. Anything a collection would need that is not in
this table is not a script to ship: it is a check type or a parser
contributed to the platform and reviewed there.

## 3.3 Before contributing: the gate and the benchmark

**The calibration set** is corpus-independent: a fixed file of 512
short texts shipped with the platform, `calibration/<set>.jsonl`,
identified by its SHA-256. The Codex names the set and pins the
reference vectors the creator produced for it with the space's encoder
(`space.calibration`), together with their per-dimension mean and
standard deviation, the space's **envelope** (`space.envelope`). All
of it is one drag member, `calibration/<set>.safetensors` (09 §9.12),
and a Codex can be written before a single record exists, which is why
creation comes first in 06 §6.6. Two collections on one encoder share
one calibration; 08 §8.3 follows from that.

**The gate.** A peer that wants to embed for a collection first passes
the **calibration** check: it embeds the whole calibration set with its
own encoder and must reach the tolerance on the **quorum**. The quorum
is the Codex's `space.calibration.quorum`, `{n: 16, min_pass: 12}`: the
run passes when, over n texts drawn from the set by the seeded rule of
§3.2, at least `min_pass` reach cosine ≥ `tolerance` against their
reference vector and the median of those n does too. This is the only
admission test, and it is per combination — a new driver, a new build,
a new quantisation is a new test. The worker re-runs it before every
embedding session for a collection and whenever the endpoint reports a
different build or model identity than the last accepted run; a failure
stops embedding for that collection as a deferral, never as an error on
a document, and is shown by `hold collection show`. `hold embedder
check --accept` is the only way a changed build becomes the one this
peer stands behind: it keeps that run's signed statement, which is what
the digest publishes and what 04 §4.5 weights.

**A peer that DERIVES rather than embeds passes no gate, because there
is no encoder to gate.** Where a space declares a `derivation` (02
§2.7), its vectors are a pure function of another space's, computed by
arithmetic that cannot drift: there is no driver, no build and no
quantisation of the endpoint's choosing to test. What the derived
vectors rest on is the admission the SOURCE vectors already passed in
their own space, and a peer that holds none of those derives nothing.
For the same reason a derived space's reference vectors are its
source's, derived by the same function, and `hold collection create`
writes its calibration member without calling an encoder at all.

There is no separate drift detector, and deliberately so. "Is this
encoder still the same function?" already has an answer — run the gate —
and the worker runs it before every embedding session and whenever the
endpoint reports a different build. A second mechanism beside that would
need its own threshold, and 03 §3.1 has already said where a threshold
may come from.

The result is a signed **calibration statement** — "this encoder image
and these weights, on this peer, are compliant with this space" — whose
bytes are 09 §9.12's *peer's calibration statement*, a different
object from the space author's calibration member defined in the same
section. It names the space, the encoder image and weights
digests, the seed and the sampled texts, the worst and median cosine
OVER THOSE SAMPLED TEXTS — not over all 512, so that the statement
carries the very numbers the pass decision used and anyone holding it
can recheck the verdict from the seed alone — the endpoint's maximum
input length, and the
benchmark below; it is published in the peer's digest (04 §4.2), and
the `calibration` attestation of a collection names it. An endpoint
whose maximum input is below the space's `ctx` produces a statement
that admits the peer to verify chunks within that length and to
contribute nothing.

`hold embedder calibrate` measures the tolerance a space can bear: the
worst cosine on the calibration set between the reference build and
every other build the creator can reach. The creator writes a
`tolerance` at or below that, knowing that two compliant builds may
differ from each other by twice the angle either has to the reference
(§3.1); the tolerance of a space comes from that measurement and never
from prose or from a constant in the code.

**The benchmark.** The same run yields the peer's benchmark: tokens
per second on the calibration set. The measurement is one full pass
over the set's 512 texts, after one discarded warm-up pass, through
the endpoint and batch settings the peer contributes with; the tokens
are counted by the encoder's tokenizer, truncated at the space's `ctx`,
and divided by the wall-clock seconds of the pass; the result is an
integer. It is signed into the calibration statement with the time of
measurement and published with the peer's catalogue digest, and it is
the weight 04 §4.5 uses to give large shards to peers that can finish
them: a peer is eligible for a shard when three times the shard's
`tokens` divided by its benchmark stays within the seconds of the
collection's work period, `work.per` (02 §2.4).

**Before spending GPU.** A peer contributes vectors to a collection
only after its own `roots` and `chunks` pass on the shards it holds and
at least one `records-vs-origin` attestation — its own or another
key's — exists for the snapshot (04 §4.5). Records are checked before
GPU is spent; there is no vector precondition, which would be circular.

## 3.4 Equivalence: another toolchain, the same collection

The Codex freezes a *reference* toolchain by digest. A newer platform
image — a refactor, a security fix — is not thereby a new collection.
`records-vs-origin` run under the new image, over the plan's sample,
reproducing every sampled shard's `records_root`, together with
`chunks` passing under the same image, is a deterministic proof that
the new image is **equivalent** for this collection; the proof travels
as an attestation whose `platform_image` is the image run, and a peer
running the new image contributes as if it ran the reference.
Equivalences live in attestations, never in the Codex; the identity
does not move. An equivalence holds for one image on one collection;
another collection is another proof.

A failure under an image other than the Codex's is an equivalence
failure of that image for this collection, not a defect in the
collection: it is recorded as a failed attestation naming the image,
it flags nothing, and the image contributes nothing to that collection
until it passes. Only a verifier running the Codex's own image, or an
image with a passing equivalence attestation for the collection, can
flag it (§3.6). A peer whose platform image is neither, for a
collection it holds, searches and shares that collection and does not
build its records, create its snapshots or contribute vectors to it
until its own `records-vs-origin` and `chunks` pass under the image it
runs; `hold collection show` says so and names the check to run.

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

Vectors arrive from other peers as signed **bundles** (08's signed
block): chunk ids, vectors, the producer's key, a signature over the
bytes — 09 §9.7 has the bytes. What makes a bundle admissible is what
it was computed *from*, never what it was computed *for*: inside what
is signed it binds the `space_id` — the function that produced the
vectors — and, per record, the `content_hash` of the text it embedded.
A bundle whose `space_id` is not this collection's is refused before
any tier runs, and a record whose `content_hash` in the bundle differs
from the one in this peer's copy has its rows refused — the record was
re-issued since — with the refusal recorded, never counted as a
failure. The `collection_id` and `snapshot_id` in the header are
provenance: they say where the work was done, and gate nothing.

One further refusal, and it is about attribution rather than
correctness. A collection declares at creation which pre-embedded
archives it draws on, in `credits` (02 §2.7). The refusal is on the
ARCHIVE, and it is resolved in two steps because a bundle carries no
archive id: the importing peer looks `producer_pubkey` up in the
platform's registry, which maps each archive to the set of keys its
bundles are signed with (06 §6.6); if that lookup names an archive, the
bundle is refused unless this collection's `credits` names that archive.
A key the registry does not know belongs to no archive, and the bundle
is an ordinary producer's — admitted or not by the ladder below, like
any other. The Codex is immutable, so a collection cannot come to stand
on work it never declared, and the acknowledgement cannot be dropped
after the fact by whoever holds the copy.

The keys live in the registry and not in the Codex on purpose. The
lookup needs the registry either way — a Codex alone cannot tell whether
a key belongs to an archive — so hashing the key into `collection_id`
would buy nothing and cost everything a frozen key costs: no rotation
(§9.2, "a lost key is a new key"), and no way for a collection written
before an archive began signing to ever import it, since 06 §6.6
refuses an archive `credits` does not name. What must never change is
WHO IS CREDITED and on what terms, and that is what the Codex holds.

What that stops is a collection quietly built on an archive it does not
credit. What it does not stop is a peer re-exporting someone else's
vectors under its own key, which no check can distinguish from work it
did — the vectors are correct either way, and 08 §8.4 is why the
protocol does not try: judging whose work a correct vector was is
judging merit. The refusal covers the honest path, which is the one a
licence's attribution clause actually turns on.

Those two bindings are exactly enough. A mother's vectors stay out of a
corrective sister because a corrective sister's `space.epoch`, and so
its `space_id`, is fresh by construction (§2.4) — the collection id was
never what kept them out. Revision-A vectors stay off revision-B text
because the `content_hash` says so. And what the narrower binding used
to forbid is now allowed, correctly: a daughter inherits her mother's
vectors (§2.7), and two sisters over one corpus at different cadences
share every bundle either of them produces. Admission is the ladder below
and nothing else: no producer key needs to be known, trusted or seen
before; the trust list (07 §7.3 `hold key trust`) filters attestations
(§3.6) and lifts burns, and its ALLOW side imposes nothing on a bundle —
an empty allow list, which is what an honest newcomer has (08 §8.1),
admits every producer in existence. The deny side is not an admission
rule either but a mute: a key this peer has explicitly denied is one it
accepts no traffic from at all, bundles included (09 §9.2), and a
quarantined key is one a proof of forgery burned. Only `hold vectors
import --strict` reads the allow side about a bundle, and it is a local
opt-in that admits nothing while the list is empty.

Before a bundle is used, the importing peer runs a ladder of checks,
cheapest first, each gating the next. Costs are for a bundle of ten
thousand chunks whose production took about half a GPU-hour.

| tier | check | cost | catches |
|---|---|---|---|
| **T0 algebraic** | dimension compared four ways — bundle header, vector column width, the Codex's `space.dim`, the local table — and dtype match the space; unit norm; no NaN, no duplicates; for every record, the bundle's set of chunk ids equals the set `chunks` derives locally, or the record is refused whole; the bundle's mean vector has cosine ≥ 0.9 to the envelope's mean and every per-dimension standard deviation lies within half and twice the envelope's (§3.3) | tens of ms, CPU | missing normalisation, random or foreign vectors, a wrong chunk geometry; sanity only, never a security control |
| **T1 geometric** | each vector's cosine profile against the calibration vectors, recorded as evidence; sampled (vector, probe, probe) triples must be geometrically realisable, which real vectors cannot fail and mis-decoded bytes do; and, for `fixed` layers, consecutive chunks overlap by construction, so their cosine must sit in a band measured on the local text — the 1st to 99th percentile of the same statistic over the vectors this peer already holds for that layer, not run until it holds a thousand such pairs or when the bundle has fewer than eight adjacent pairs; for `cdc`, `abstract` and `verbatim` layers the band does not exist and is not run | under a second, CPU | wrong dtype or shape, shuffled or perturbed vectors |
| **T2 challenge** | golden (text, vector) pairs — the calibration set's first sixteen texts and their reference vectors — re-embedded by the producer into the bundle header (09 §9.7); passes on the calibration quorum; no age rule | none for the importer | misconfiguration — proof of a correct oven, not of each loaf; not a security control |
| **T3 spot recompute** | sixteen chunks chosen by the importer's private randomness, re-embedded with the importer's own compliant encoder; fails when fewer than twelve reach `tolerance` or the median is below it — the calibration quorum reused. In a DERIVED space (02 §2.7) the importer embeds with the SOURCE space's encoder and applies the declared derivation before comparing, because a derived space may have no encoder that emits it natively — a truncation at a width the model never publishes — and re-embedding is then impossible by definition | a few seconds, GPU — under one percent of production | bulk substitution: a wrong model, a wrong quantisation, a bundle mostly garbage |
| **T4 verify on use** | a poisoned vector is inert until a search surfaces it; the chunks a search actually cites — those in the list it returns to its caller — are re-embedded once with this peer's compliant encoder, asynchronously, and marked *verified here* forever | a few seconds per answer, once per chunk ever cited | targeted poisoning, which sampling cannot find |

The importer's randomness in T3 is private and never derived from the
bundle, or a producer could re-roll a nonce until the sample missed its
corruption. What sixteen chunks on the quorum catch is stated
honestly: one importer fails a half-corrupt bundle nineteen times in
twenty and a bundle with a third of its vectors wrong about half the
time; a bundle with a few percent of wrong vectors passes T3 nearly
always and is left to T4 and to `vectors-sample` across the network.
The network carries the safety, not one importer: honest import costs
about a fifth of a percent of production, so fifty peers importing one
shared bundle spend, together, under a tenth of what one of them
embedding it would.

A bundle is usable once T0, T1 and T2 pass. When T3 cannot run on this
peer — no compliant encoder, or the encoder past its bounded wait — the
bundle is imported and its chunks are shown as *unverified here*; the
peer publishes no `vectors-sample` attestation for it, and `hold
vectors check` runs T3 over every such bundle the first time a
compliant encoder is available. A tier that could not run is recorded
as not run, never as passed. The T4 mark is local state, keyed on the
chunk id and the encoder that made it; it is never gossiped. What
travels from T4 is a failure.

A **verified failure** — a T3 fail, or a T4 miss that reproduces on a
second recompute, on a peer whose encoder holds a current calibration
statement for the space — is a **proof of forgery**: an attestation of
check `forgery` (09 §9.8) carrying the chunk id, the hash of the
claimed vector, the hash of the recomputed vector and the encoder
image, and for a red-class collection (04 §4.4) the record's
`content_hash` in place of the chunk's text, which never travels. It
**burns** the producer's key on the peer that found it: the key is
quarantined there, its bundles are dropped from search — their chunks
return to *without vectors* and the coverage map says so — and its
attestations are not counted; the quarantine is permanent until an
operator runs `hold key trust <key> --restore`. On every other peer the
proof is a claim until reproduced: a peer holding the record re-embeds
the named chunk with its own compliant encoder, and a miss burns the
key there too and yields its own proof; a peer that cannot re-embed
shows the claim and counts it toward the flag of §3.6.

A producer that finds its own vectors wrong — a faulty build that
passed the gate and then wrote garbage for a week — does not wait to
be burned. It publishes a `withdraw` attestation naming its own bundle
ids (09 §9.8); peers drop those bundles, their chunks return to
*without vectors*, and nothing is burned. `hold vectors check --drop`
(07 §7.3) withdraws every bundle this peer published that held a
dropped vector. A miss found on a withdrawn bundle's vector is not a
forgery.

Two compliant bundles for one chunk are not a conflict. The first
accepted vector is stored and searched; the second is compared with it
and the comparison is published as a `vectors-sample` attestation
naming both producers — 04 §4.5's "duplicates are checks".

## 3.6 Attestations and trust levels

Every completed check yields an **attestation**: collection, snapshot,
check, seed and sampled shards, result — `pass`, `fail`, `forgery`,
`withdraw` or `supersedes` — a detail block per check, platform image,
encoder image for statistical checks, the verifier's key, time. Its id
is the hash of its canonical body and its signature is under the
attestation domain (09 §9.8, 09 §9.2). An inconclusive check yields
none. Attestations travel like any event (04 §4.2), a peer that lacks a
collection's attestations pulls them (09 §9.9), and they are stored
beside the catalogue.

An attestation counts toward a level only for the snapshot it names,
and for at most two cadences from its `time`; when a peer adopts a
newer snapshot, an older snapshot's attestations count for the shards
the newer one lists with the same root, which is what a snapshot
inherits unchanged. An attestation whose `time` is more than an hour
ahead of the receiving peer's clock is dropped.

A collection's level is computed **locally**, from the attestations a
peer holds. A level counts **independent** attestations: two
attestations are independent when they come from distinct keys and
carry distinct seeds where the check is a sampled one; a null seed —
an exhaustive or deterministic check (09 §9.8) — is not compared, since
two honest peers running `roots` over the same shards must agree, and
requiring them to differ would put `checked` out of reach for ever. A
key is counted once. The trust list
(`hold key trust`) is a local allow/deny filter over whose attestations
are counted at all — a denied or quarantined key's are ignored — and is
never a weight: no key counts more than another, and an empty list
counts everyone. A peer's own checks set its own level regardless of
any list. There is no global consensus and no ballot to stuff. Keys
are free (08 §8.1), so the thresholds below are advisory: they say how
many strangers have said the same thing, not that the thing is true.
What protects a peer is its own checks, and the aggregate limit of
08 §8.2 bounds what unverified material can cost it.

| level | meaning |
|---|---|
| **unverified** | only the publisher's word — a signed snapshot, at most a calibration statement; usable, and marked as such everywhere it appears |
| **checked** | at least three independent keys passed `roots`, `chunks` and `calibration`; at least one of them passed `records-vs-origin` on a sample; and, for every producer whose bundles the collection has, at least one key other than that producer passed `vectors-sample` on that producer's vectors |
| **verified** | at least eight independent keys, and the union of their samples covers a quarter of shards for `records-vs-origin` and a tenth for `vectors-sample`; the covered fraction keeps rising and is shown |
| **flagged** | a deterministic check failed — one reproduction is enough, and it names the shard; or a statistical check failed twice, from independent verifiers, the second on the chunk ids the first named (below) |

*The same sample* for a statistical flag is exact: a failing
`vectors-sample` or T3 attestation lists the chunk ids that missed; a
second key re-embeds exactly those chunks with its own compliant
encoder and reports them missing too. A deterministic flag names the
shard; a statistical flag names the producer of those vectors, and
applies to that producer's vectors in that space, never to the
records. A failing deterministic attestation flags only when the
verifier's platform image is the Codex's or one with a passing
equivalence attestation for the collection (§3.4); a failure under any
other image is that image's failure. A `roots` or `chunks` failure on a
shard this peer holds is first a local fault: the shard is excluded
from search and share, re-fetched from peers (04 §4.3), and re-checked;
a flag is published only when a freshly fetched shard whose root
matches the signed snapshot fails `chunks`, or `records-vs-origin`
fails on it. `hold check` reports the first case as a corrupt local
shard with the re-fetch queued, never as flagged. A flag received from
another key is acted on after local reproduction; a peer that cannot
reproduce it — no origin, shard not held — shows it as a claim and
counts it as a flag once two distinct keys report the same shard or
the same chunks failing.

A flagged collection stays searchable and is marked; its publisher's
key loses standing on the peer that flagged it. A flag stands until a
later snapshot passes the check that failed, or a corrective sister's
defect is reproduced (below). Sybil resistance does not come from
counting keys: it comes from every peer's own checks, from the sample
rule that makes a second failure a reproduction of the first, and from
the aggregate limit.

A **corrective sister** (02 §2.7) carries the flag's cause in its
Codex:

```
replaces: {codex: <hash>,
           defect: {check, toolchain: <image under which it fails>,
                    witnesses: [<shard or record ids>], expected, observed,
                    rationale: "<one readable sentence>"}}
```

A peer reproduces the defect before honouring the replacement: it runs
the named check on the named witnesses against the old collection,
with the old collection's *own* toolchain — its `platform_image` for
`records-vs-origin` or `chunks`, and for `vectors-sample` any encoder
holding a compliant calibration statement for the old collection's
space — and on a sample of its own of the same size beside the
witnesses. `defect.toolchain` names that image. If the check fails, the
old collection is flagged and the sister is the remedy. If it does not,
the sister is an ordinary sister with no claim on anything: a
different parser or a different encoder produces different bytes and
different vectors by construction, and that is not a defect; neither is
cleaning that drops what a reader wanted, which reproduces faithfully
and fails nothing (§3.4). A defect is a check that fails, never a
judgement that disappoints. The
protocol thereby guarantees that nobody can replace a healthy
collection by pretending a bug. It does not guarantee the fix is good —
that is decided where the parser is reviewed.

A sister that is not corrective — a better encoder over PubMed, a
parser that keeps more of the Gazzetta — states its case with a
`supersedes` attestation naming the collection it improves on and its
`space.evaluation` (02 §2.4). It is a claim, shown beside both
collections; it moves no vectors, flags nothing and marks nothing
superseded. Adoption decides (04 §4.6).

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
its keys under a domain it controls, and the domain's TLS and DNSSEC
chain is the anchor. The Codex's `certifier.anchor` names the domain;
the **anchor document** is
`https://<domain>/.well-known/louise-certifier.json`, a list of keys,
each with `valid_from` and `revoked_at`, and the ids of the collections
the certifier stands behind (09 §9.11). Peers fetch it at adoption and
every 24 hours with jitter, drawing from the budget of 04 §4.3. An
unreachable anchor changes nothing: the last known state stands and is
shown with its age, and after 30 days without a successful fetch it is
shown as *stale* — still never a mismatch. A reachable anchor that does
not list the key, or lists it revoked, makes the collection *open,
published by a key that claims to be X*, shown as such. A collection is
shown as certified on a peer only while `signature` and `anchor` pass
there.

**Rotation and revocation** live at the same anchor; without revocation
a compromised key certifies forever. A key certifies the snapshots
signed after its `valid_from`. A key with `revoked_at` invalidates the
snapshots whose sidecar signature time (09 §9.4) is after that instant
and that no later key also signed; what it signed before stays
certified.

**The pointer.** For "which snapshot is current" the certifier
publishes a signed pointer under its own key (04 §4.2) — collection id,
snapshot id, snapshot hash, a sequence number, time, signature
(09 §9.11) — and a peer keeps the highest sequence number it has seen.
The anchor document is where a peer looks when the DHT is unreachable.

**Silence.** The Codex's `succession` says what happens when the
certifier stops. A certifier is *silent* when no certified snapshot has
appeared for the number of cadences `open-after` spans and no newer
pointer is at the anchor. Then the collection continues as an open
daughter — `continues` set, `publication: open` — which any peer may
publish. The certifier's return cannot reclaim that lineage: a later
certified daughter is a new lineage. Under `succession: frozen` the
collection simply stops at its last certified snapshot, still held,
shared and searched.

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
`seals/<snapshot>.json`, one entry per sealed issue, keyed by the
`file_hash` of the sealed file, which every record parsed from it names
in `extra.seal` (09 §9.5) — a record's own `origin_hash` covers that one
document, not the issue, so the reference is explicit. The file's bytes
and how the certifier's signature binds them are 09 §9.15. `origin-seal`
then runs in two modes: *live*, re-fetching the sealed file, checking it
hashes to `file_hash`, and validating against the current trusted list;
*archival*, offline, and still working when the origin is gone. Archival
mode needs the signature and not only the evidence around it, so the
entry carries the envelope itself (`seal`, DER) and the digest that
envelope commits to (`content_sha256`): the signature verifies against
that digest without the sealed bytes, and its certificate path validates
against the captured `chain`, `revocation` and `trusted_list`. A peer
that holds neither the sealed file nor the entry reports *inconclusive*
(§3.1) — it does not report a pass. The
sealed files themselves may be shared as an optional member for whoever
needs legal standing; the records do not contain them.

**Reproduction stays mandatory.** For a certified collection over a
public origin, `records-vs-origin` runs regardless of the signature. An
attacker then has to compromise both the key and the origin, and a valid
signature over records that do not match the origin is the proof of
compromise. `certifier.is_origin` says whether the certifier is the body
that publishes the origin — the Gazzetta running Louise — or an
intermediary signing what it built from the origin's files; the origin
is the origin either way, the check runs the same way against it, and
the Library says which of the two a reader is trusting.

## 3.8 Manifest-only: nothing uncertified can exist

In a certified collection only certified content may be embedded or
returned. The rule is enforced where it can be absolute — at the moment
a fetched shard is promoted from staging into the copy: *a shard enters
only if its root is in a signed snapshot of the chain* — any snapshot
this peer holds that is signed by a certifier key valid at its
signature time (§3.7), not only the newest, since a shard stays listed
by every snapshot that still carries it. This is a refuse-to-start
class rule, not a warning. A shard
superseded by a re-issue stays on disk under its old snapshot's name
(02 §2.2) and is excluded from search and embedding, never deleted. A
revoked or mismatched anchor does not lift the gate: snapshots signed
after the revocation are not signed for its purposes, nothing later
promotes, and the succession rule of §3.7 decides what the collection
becomes.

Everything downstream is defence for a gate that was bypassed — a folder
populated by hand, an old snapshot left on disk:

- **granularity** — a chunk is a pure function of one record, so the
  unit of certification is the shard, the unit of discard is the record
  and all its chunks, and the unit of halt is the collection on that
  peer;
- **halt** — embedding stops for the whole collection and no bundle is
  published, because vectors for chunks outside the snapshot would
  poison everyone's coverage map; the peer marks the collection
  *integrity violated* until it re-fetches and re-checks: `share fetch`
  replaces every excluded shard, `hold check` passes `roots`,
  `signature` and `anchor`, and the mark clears;
- **search continues** over the certified subset, with the exclusion
  visible in the results — never a silent drop.

Who is at fault is a separate question from what to do, and the answer
does not depend on good faith:

| found | at fault | flagged | caught by |
|---|---|---|---|
| bytes that do not match the claimed root | the sharer served corrupt or forged data | the sharer | the transport, before anything reaches the copy |
| a shard whose root is not in a signed snapshot of the chain | someone introduced foreign material | the local source of that shard | the promotion gate |
| a valid, signed shard whose records do not match the origin | the certifier: a pipeline bug or a compromised key | the certifier and the snapshot; the sharer served faithfully what was signed | `records-vs-origin`, `origin-seal` |

The protocol cannot tell a bug from an attack and does not need to: the
response is the same, and the remedy is the same — a later snapshot that
passes. Humans tell the two apart from the anchor's history.

Embedding for a certified collection has three levels, named by the
Codex's `embedding` field, the middle one costing the certifier only
seconds: **certifier-only** (the certifier embeds and signs —
improbable), **endorsed** (any peer embeds; the certifier runs
`vectors-sample` and signs the attestation, which also settles which of
two compliant vector sets is the reference), **open** (any peer embeds;
independent verifiers attest). The Library shows certification and
trust level as two axes — *certified by X · reproduced by N
verifiers* — never as one.
