# 3. Checking

*(vocabulary amended 2026-10-08: SHARD, parts)*

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
that names the part. There is no "close".

**Vectors are within tolerance.** The same weights on different GPUs,
drivers or batch sizes produce different floats. So a vector is correct
when its cosine against a compliant encoder's output for the same chunk
is at least the Codex's `tolerance` — a statistical statement, sampled,
never exhaustive in practice. The encoder on the other side of that
comparison is the checking peer's own, admitted by the gate of §3.3:
nobody holds the reference engine's output for an arbitrary chunk. Two
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
the manifest locked, a sampled part not held by this peer. It is
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
sampled. `validation.json` in the SHARD (02 §2.6) is that block written
out for tools, and `hold check` refuses a copy where the two differ.
Nothing outside the Codex decides how much is sampled: a publisher who
wants weaker checks has to publish a different collection. A `checks`
entry that names no row of this table is refused at `hold collection
create` and at `hold check` alike.

| check | proves | needs | kind |
|---|---|---|---|
| `roots` | every part's bytes have the `root` the snapshot lists — the BitTorrent v2 merkle root of the stored file (09 §9.5) | the copy | exact |
| `records` | every sampled record's `content_hash` recomputes from its own title and sections and it carries an `origin_hash`; each sampled part's `records_root` recomputes from its records' `content_hash` values in order (09 §9.5) | the copy | exact |
| `records-vs-origin` | a seeded sample of parts and, within them, a seeded sample of origin units of at most 256 MiB (below), re-fetched from the origin by the locator each record carries and re-parsed with the Codex's toolchain, reproduces every sampled record line of those units byte for byte — the decompressed lines, not merely the same `records_root`. A line identical but for `origin_hash` is MOVED, not failed: its origin unit was rewritten since the build and parses to the same record; it is named in the detail (amended 2026-10-07: the mirror rewrote half of a sample of gutenberg's book folders without changing one parsed record, and a new image could not be proven equivalent). A line that differs elsewhere too, from an origin unit whose `origin_hash` is no longer the record's, is CHANGED AT THE ORIGIN, not failed: the origin revised it since the build, so it can be neither reproduced nor held against the record; it is named in the detail. Only a line whose origin unit is still the bytes it was parsed from can fail. When more than a tenth of the records re-parsed changed at the origin, or none reproduced, the check is inconclusive: a revised origin and a hash that never matched look the same (amended 2026-10-11, operator's yes: 17 of 4,055 sampled gutenberg records were revised upstream, and the check failed under every image, the Codex's own included) | the origin, the platform image | exact |
| `image-equivalence` | the same seeded sample re-parsed from the same origin bytes by this image and by the Codex's reference image (`--against`, written under that image by tools/reparse_lines.py) gives the same record lines, every one — a proof about the image, never planned, for §3.4 when the origin has moved since the build | the origin, the reference image's lines | exact |
| `chunks` | recomputing chunks from the records reproduces each sampled part's `chunks_root` and its `chunks` count (09 §9.4) — deriving each document's layer from the Codex's rules, never reading it from the record, which carries none (02 §2.3, 06 §6.3), so a wrong layer is caught rather than reproduced | the copy | exact; can be exhaustive |
| `vectors-sample` | in a seeded sample of parts, sixteen chunks each, re-embedded with this peer's compliant encoder, reach cosine ≥ `tolerance` on the calibration quorum (§3.3) | records, a calibrated encoder | statistical |
| `calibration` | the calibration set's reference vectors reproduce on this peer's encoder — the gate of §3.3, run for this collection's space | an encoder | statistical |
| `centres` | re-training from the declaration in `vectors/<space>/index.json` (02 §2.4) yields centres each within cosine ≥ `tolerance` of the published centre at that index version | the copy | statistical |
| `labels` | every vector's stored label is its nearest published centre at the label's index version, ties to the lower centre index | the copy | exact given the published centres |
| `figures` | every plate the sampled parts' records name is in the part's figures container and hashes to what the record line says (09 §9.5); a plate the peer does not hold is not a failure — a figures member is fetchable or not — and is reported as not held | the copy | sampled |
| `license` | the terms member `LICENSE-TERMS` hashes to the Codex's `license.terms_sha256` (09 §9.13); absent for a green collection | the copy | exact |
| `signature` | the snapshot's sidecar carries a valid signature by the key that claims it (09 §9.4) | the copy | exact |
| `anchor` | a certifier's key is listed, valid and unrevoked at its external anchor (§3.7) | the network | exact |
| `origin-seal` | the origin's own seal on the source file validates, and the record was parsed from that file (§3.7) | the copy, or the origin | exact |
| `evaluation` | the score in `space.evaluation` (02 §2.4) reproduces on this copy's chunks when its suite is public, and every target the suite names resolves to a chunk in the copy | the copy, the suite | statistical |

**A never-share collection** proves nothing to anyone, because nobody
else holds it, and needs only the checks that keep its own vectors
consistent: `calibration` — the gate of §3.3 on the run, and the check
of every answer's run before its vectors are written — and
`vectors-sample`, which it runs locally on a timer over its own stored
vectors (02 §2.9). The rest of its plan is not required; a deposit
collection runs none of it, having no SHARD for it to run on.

**The seeded sample.** Every sampled check draws its sample by one
rule, `sample(seed, n, universe)`: sort the universe by id in UTF-8
byte order, score each id by HMAC-SHA256(seed, id) read as an unsigned
64-bit integer from the first eight bytes of the digest, big-endian,
and take the n ids with the lowest scores. The universe of a part
sample is the snapshot's part list (its key `shards`), an id being `<slice>/<n>`; the
universe of a chunk sample is the chunk ids of the part sampled, in
which case n is sixteen. The seed is 32 random bytes chosen by the
verifier from OS entropy — or given by hand as `--seed` — and published
in the attestation; the plan fixes n or a fraction, never the seed. So
two verifiers never draw the same sample unless they mean to, the union
of samples grows with every verifier, and anyone can recompute from an
attestation exactly which parts it covered. `sample` gives the
fraction of parts per sampled check as a decimal string; the default
is `"0.05"`, always at least one part.

**What one origin check may read.** The part sample does not bound what
`records-vs-origin` costs: a record part is cut by the collection's
slices and an origin file by the origin's, so a twentieth of PubMed's
parts — 68 of 1,358, sliced by year — reach into 474 to 651 of its
origin files, 18 to 29 GiB to fetch for every check on every peer
(measured 2026-09-20, two seeds). So within the sampled parts the
records are grouped by **origin unit** — the file of the origin a
record's locator points into (09 §9.5) — the units are ranked by
`sample(seed, n, universe)` under the same seed, an id being the unit's
locator without its member, and they are read in that order up to the
last one that keeps the sum of their sizes within **256 MiB**; the
first is always read, whatever it weighs. Every sampled record of a
unit that is read is compared byte for byte; the others are not
compared in this run and the report says so. The seed still decides
everything, so the union of what peers have compared grows with every
check, and anyone can recompute from an attestation which units it
read. The bound is the platform's and not the plan's, for the reason
the plan sets no chunk count (09 §9.3): a creator who could set it could
set it backwards. `--origin-mib` raises it for a run and cannot lower
it (07 §7.3). This amends "nothing outside the Codex decides how much is
sampled" in one respect only: the Codex decides how many parts, and
the platform how much of the origin one check may read for them
(operator, 2026-09-20).

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
texts shipped with the platform, `calibration/<set>.jsonl`,
identified by its SHA-256, spanning the encoder's whole input range up
to the largest `ctx` it serves — a gate that never fills a context
window cannot see a build that is wrong only when one is full (09
§9.12). The Codex names the set and pins the
reference vectors the creator produced for it with the space's encoder
(`space.calibration`), together with their per-dimension mean and
standard deviation, the space's **envelope** (`space.envelope`). All
of it is one SHARD member, `calibration/<set>.safetensors` (09 §9.12),
and a Codex can be written before a single record exists, which is why
creation comes first in 06 §6.6. Two collections on one encoder share
one calibration; 08 §8.3 follows from that.

A PAIR space is the exception (*amended 2026-10-08*,
docs/notes/input-forms.md). Its encoder is fed tokenizer text pairs (02
§2.4, 09 §9.3), and a gate over plain texts would pass a backend that
flattens a pair into a string — the very defect the form is declared to
keep out. So its set is 512 title-and-abstract pairs drawn by a fixed rule
from the collection's own records, carried inside the member rather than
shipped with the platform (09 §9.12), and wherever this section says
"texts" a pair space's are those pairs: the gate, its fingerprint and its
batches, the re-check of a kept statement, T2's challenge and the probe
that asks which run answers all embed a pair as a pair; the query side,
whose input is one text in every space, embeds each pair as one text.

**The gate.** A peer that wants to embed for a collection first passes
the **calibration** check: it embeds the whole calibration set with its
own encoder and must reach the tolerance on the **quorum**. The quorum
is the Codex's `space.calibration.quorum`, `{n: 16, min_pass: 12}`: the
run passes when, over n texts drawn from the set by the seeded rule of
§3.2, at least `min_pass` reach cosine ≥ `tolerance` against their
reference vector and the median of those n does too. This is the only
admission test, and it is per combination — a new engine, a new build,
a new device or driver, a new quantisation is a new test. Which engine
a peer runs is its own choice: the one the Codex freezes (02 §2.4) is
the reference every run is measured against, not a requirement, and a
newer build that is faster is welcome the moment it passes. Nor is
anything pinned under it (*amended 2026-10-08*, operator, docs/notes/vega.md):
the model runner installs and upgrades its backends as it likes, and an
upgrade changes the backend's files, so it is a new run, gated before its
first vector is written. The Codex cannot be updated and keeps the engine
it was created with forever; the engines after it are admitted beside it,
each by its own calibration statement. A run that
passes is validated for the space as it stands, with nobody having to
accept it (operator, 2026-09-20). Its signed statement is what the
digest publishes and what 04 §4.5 weights. A run that fails writes
nothing for the collection — a deferral, never an error on a document —
and `hold collection show` says so.

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

**Validate once; then every answer says which run gave it.** A pass
validates a RUN: one engine as it was actually launched. That is the
engine's software by the digest of the bytes that ran — an image by the
digest that is running (never a tag), or a runner's backend by the
digest of its installed files (09 §9.3) — the weights, the
exact arguments, load options and environment, the device, and the device's driver and
compute runtime — for a backend that carries its own driver, the one it
loads — all named by a **run identity** (09 §9.12). What keeps
later work honest is not a test repeated during the work. It is the
platform that serves the models, in two parts (*amended 2026-10-08*,
docs/notes/vega.md). A model runner — LocalAI on this node — launches,
loads, keeps and evicts the engines, and is the only manager of the
card's memory; what it decides changes when an answer comes, never what
it says, and it is outside the trust path. **VeGa, the Vector Gate**,
makes the provenance of every vector provable. Its wrapper sits between
the runner and each real backend, as the backend's parent: it computes
the run from what it launched — the backend's installed files, the
weights it holds, the options the runner sent it, the driver it loads —
never from what a configuration declares, and for every answer it
issues a **receipt** that binds the run to the input the backend
received and to the vector it returned, by their digests (09 §9.12).
VeGa holds the receipt before the vector goes back, and the writer —
Louise — reads it and checks it before the write. So a runner that
changed an input or a vector, or sent a request to a backend without
the wrapper, is caught by the writer and never trusted. Together they
are a trusted processing platform: a model reloaded with another flag,
a backend upgraded, a driver updated underneath it is each a different
run, and says so on its next receipt. The second part was RAGE, the RAG
Engine, until 2026-10-08: a front that computed the run from the
process the kernel showed and stated it in a header of each answer
(09 §9.12, `Louise-Run`; operator, 2026-10-03). It serves until VeGa
replaces it, and an endpoint behind it is held to the rules below with
its header in place of a receipt.

- **Embedding** (`vectors build`, `share contribute`): a vector is
  written only if the answer that carried it names a run validated for
  the space — through VeGa, only if every input of the answer has its
  receipt, the receipt's input digest is that of what was sent and its
  vector digest that of what came back, and the run the receipt names
  is validated. This is checked on every answer, before the write. No
  receipt — VeGa unreachable, a receipt not or no longer held, the
  wrapper unable to deliver one — is a deferral, never an error on a
  document. A receipt that does not match is a refusal: nothing from
  that answer is written. An
  answer from a run not yet validated is not written — a deferral — and
  the gate runs against that run at once; if it passes, work resumes
  under it. Nothing unvalidated is ever stored, so stored vectors carry
  no mark of their run. A document may hold vectors from two validated
  runs, each within tolerance, as a space holds the work of many
  producers.
- **Checking** (T3, T4, `vectors-sample`, `hold vectors check`): a
  check runs under a validated run. An answer that names another makes
  the check inconclusive, and an inconclusive check yields no
  attestation (§3.6).
- **Searching**: the same runs, validated the same way, and through
  VeGa a query vector is taken only with its receipt, checked as a
  document's is: no receipt is a deferral — the search goes on with its
  text leg — and a receipt that does not match withholds the vector leg
  and says why. In a symmetric
  space the queries and the documents come from one run. An asymmetric
  space's query encoder is a run of its own, validated against the
  query reference vectors (09 §9.12). A run not yet tested answers no
  query: while the gate runs against it — at once, and by itself — the
  space's collections on this peer are in quarantine (§3.6), and an
  answer that names such a run is discarded (operator, 2026-09-19;
  until then a pending run's query vector was used, on the ground that
  it is never published). A run that FAILS has been tested: its space's
  vector leg is withheld until a validated run answers, the text leg
  goes on, and the search says so — a peer that fails may still search
  with what it holds (02 §2.4). An encoder that cannot be reached is a
  deferral, and returns nothing to quarantine.

An endpoint that states no run identity cannot be held to a validated
run: a peer may search with it and writes nothing through it. An
encoder declared behind VeGa is not such an endpoint: an answer of it
without its receipts is deferred, and its vectors are neither written
nor searched with.

**The gate also measures what the answers depend on besides the run.**
It embeds the sixteen fingerprint texts (09 §9.12) one at a time, then
again the way the peer contributes: its batching and its concurrency.
Equal bytes pass at once. Otherwise the batched answers are judged
against the run's own run-to-run variation: the sixteen are embedded one
at a time a second time, and every batched vector must be as close to its
single-text answer as the two single-text passes are to each other at
their furthest apart, less f32 rounding (10⁻⁶ of cosine). A run whose
batched answers fall outside that band is validated only for
single-text, one-at-a-time work (*amended 2026-10-12*, operator: equal
bytes failed engines whose batching adds no more than last-bit noise). The batching fault that silently
corrupted 714 books (2026-07) is a property of a run's settings, and it
is measured, never assumed. The sixteen vectors' hash, the run's
**fingerprint**, goes into the statement as evidence, not as a check
the worker repeats. A peer that runs the same run on the same kind of
device reproduces it bit for bit, which is stronger evidence than any
tolerance and is the reproduction the Codex's engine is frozen for
(02 §2.4).

The result is a signed **calibration statement** — "this run, on this
peer, is compliant with this space" — whose bytes are 09 §9.12's
*peer's calibration statement*, a different object from the space
author's calibration member defined in the same section. A peer holds
one per run it has validated. It names the space, the run identity with
the engine, weights and driver it stands for, the fingerprint, the
seed and the sampled texts, the worst and median cosine
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
worst cosine on the calibration set between the reference engine and
every other engine or build the creator can reach. The creator writes a
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
the weight 04 §4.5 uses to give large parts to peers that can finish
them: a peer is eligible for a part when three times the part's
`tokens` divided by its benchmark stays within the seconds of the
collection's work period, `work.per` (02 §2.4).

**Before spending GPU.** A peer contributes vectors to a collection
only after its own `roots` and `chunks` pass on the parts it holds and
at least one `records-vs-origin` attestation — its own or another
key's — exists for the snapshot (04 §4.5). Records are checked before
GPU is spent; there is no vector precondition, which would be circular.

## 3.4 Equivalence: another toolchain, the same collection

The Codex freezes a *reference* toolchain by digest. A newer platform
image — a refactor, a security fix — is not thereby a new collection.
`records-vs-origin` run under the new image, over the plan's sample,
reproducing every sampled part's `records_root`, together with
`chunks` passing under the same image, is a deterministic proof that
the new image is **equivalent** for this collection; the proof travels
as an attestation whose `platform_image` is the image run, and a peer
running the new image contributes as if it ran the reference.
Equivalences live in attestations, never in the Codex; the identity
does not move. An equivalence holds for one image on one collection;
another collection is another proof.

Where the origin has changed in content since the build — texts
corrected, catalogue entries revised upstream — those records are changed
at the origin (§3.2, amended 2026-10-11): named, neither passed nor
failed, and the rest of the sample decides. Where so much changed that the
check is inconclusive under every image, the reference included, it can
show none equivalent. The proof is then `image-equivalence`: a seeded sample re-parsed from the
same origin bytes by the Codex's reference image and by the new one, line
by line, with not one line differing; with `chunks` passing under the new
image it makes the image equivalent exactly as above. What is compared is
what the parser makes of the bytes — "the source compliancy is after the
parsing of the useless information, that only matters" (operator,
2026-10-07, when gutenberg's mirror had moved under its records). It says
nothing about whether the records still match their origin, so no trust
level counts it (§3.6); and the reference's half is only as good as the
image that made it, which must be the one the Codex names.

A failure under an image other than the Codex's is an equivalence
failure of that image for this collection, not a defect in the
collection: it is recorded as a failed attestation naming the image,
it flags nothing, and the image contributes nothing to that collection
until it passes. Only a verifier running the Codex's own image, or an
image with a passing equivalence attestation for the collection, can
flag it (§3.6). A peer whose platform image is neither, for a
collection it holds, cannot run the origin check that lets a copy out
of quarantine by itself — its failure would say nothing about the
collection — so its operator decides (§3.6). The copy once in use, the
peer searches and shares that collection and does not
build its records, create its snapshots or contribute vectors to it
until its own `records-vs-origin` and `chunks` pass under the image it
runs; `hold collection show` says so and names the check to run.
*Amended 2026-10-09 (RULE 1):* a snapshot SEEDED from another collection's
byte-identical parts (`hold snapshot create --shards-from`), whose Codex
names the same reference image and the same record-deciding fields, builds
no record: the parts are the reference's work. Such a peer may freeze it,
on one condition checked at once — every part's `records_root`,
`chunks_root` and counts as this image computes them equal the source
snapshot's; otherwise the snapshot is withdrawn. (The equivalence proof
needs a snapshot to draw from; a new collection carrying its records had
none.)
*Amended 2026-10-09 (RULE 1), equivalence by version:* an image that runs
the Codex's parser at its parser version and every layer's chunker at its
chunker version IS equivalent — invariant 3 makes any change to parsing,
cleaning or chunking bump the version of what changed, so the same versions
make the same records and chunks. The per-image proof above was being
re-demanded at every deploy (each names its image honestly since
2026-10-07), recomputing a result invariant 3 already keeps; it remains the
way for an image whose versions differ. `chunks` and `records-vs-origin`
keep running in the plan, so a parse changed without its bump is still
caught.
*Amended 2026-10-09 (RULE 1), record twins:* a collection seeded so is its
source's RECORD TWIN — same record-deciding fields, same reference image,
snapshots holding the same parts. `image-equivalence` accepts reference
lines made for the twin (the reference image may not read the newer Codex),
and the twin's copy state carries: a copy listed in use before quarantine
lists its twin too (`[node] quarantine_in_use_before`), since nothing about
its records differs. gutenberg6 was the first (docs/notes/v5-migration.md).

For encoders the word is **compliant**, not equivalent, because the
statement is statistical: an engine that passes calibration may
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

A vector table CARRIED is not a bundle, and none of this ladder runs on
it (amendment A1, *amended 2026-10-09*, docs/notes/v5-migration.md;
02 §2.4). This peer's own table crosses into a collection with the same
records whose space differs only by engine, or by a declared input form
equal to the one its vectors were produced in, and it is used only once
`vectors-sample` (§3.2) under a validated run (§3.3) passes at the
TARGET's tolerance. That attestation is the proof the ladder's T3 would
give a stranger's bundle, over the whole table's sample rather than one
bundle's: the vectors reproduce under the target's function. Rows that
another producer contributed keep their provenance (`chunk_verification`)
and verify-on-use applies to them as before; rows whose form the target
does not declare are dropped, not carried (`louise dev carry --keep`).

One further refusal, and it is about attribution rather than
correctness. A collection declares at creation which pre-embedded
archives it draws on, in `credits` (02 §2.7). The refusal is on the
ARCHIVE, and it is resolved in two steps because a bundle carries no
archive id: the importing peer looks `producer_pubkey` up in the
platform's registry, which maps each archive to the set of keys its
bundles are signed with (06 §6.6); if that lookup names an archive, the
bundle is refused unless one of this collection's `kind: "archive"`
credits names that archive. Only that kind is consulted: the corpus and
community entries name no archive, and a bundle whose key the registry
does not know is an ordinary peer's — which is what the community entry
is the standing acknowledgement of, and why it gates nothing.
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
burned key is one a proof of forgery muted the same way. Only `hold vectors
import --strict` reads the allow side about a bundle, and it is a local
opt-in that admits nothing while the list is empty.

Before a bundle is used, the importing peer runs a ladder of checks,
cheapest first, each gating the next. Costs are for a bundle of ten
thousand chunks whose production took about half a GPU-hour.

| tier | check | cost | catches |
|---|---|---|---|
| **T0 algebraic** | dimension compared four ways — bundle header, vector column width, the Codex's `space.dim`, the local table — and dtype match the space; unit norm; no NaN, no duplicates; for every record, the bundle's set of chunk ids equals the set `chunks` derives locally, or the record is refused whole; the bundle's mean vector has cosine ≥ 0.5 to the envelope's mean, and the 2nd and 98th PERCENTILES of the per-dimension standard-deviation ratios lie within four tenths and twice the envelope's (§3.3) — a percentile and not an extremum, for the reason below, and both literals measured rather than guessed | tens of ms, CPU | missing normalisation, random or foreign vectors, a wrong chunk geometry; sanity only, never a security control |
| **T1 geometric** | each vector's cosine profile against the calibration vectors, recorded as evidence; sampled (vector, probe, probe) triples must be geometrically realisable, which real vectors cannot fail and mis-decoded bytes do; and, for `fixed` layers, consecutive chunks overlap by construction, so their cosine must sit in a band measured on the local text — the 1st to 99th percentile of the same statistic over the vectors this peer already holds for that layer, not run until it holds a thousand such pairs or when the bundle has fewer than eight adjacent pairs; for `cdc`, `abstract` and `verbatim` layers the band does not exist and is not run | under a second, CPU | wrong dtype or shape, shuffled or perturbed vectors |
| **T2 challenge** | golden (text, vector) pairs — the calibration set's first sixteen texts and their reference vectors — re-embedded by the producer into the bundle header (09 §9.7); passes on the calibration quorum; no age rule | none for the importer | misconfiguration — proof of a correct oven, not of each loaf; not a security control |
| **T3 spot recompute** | sixteen chunks chosen by the importer's private randomness, re-embedded with the importer's own compliant encoder; fails when fewer than twelve reach `tolerance` or the median is below it — the calibration quorum reused. In a DERIVED space (02 §2.7) the importer embeds with the SOURCE space's encoder and applies the declared derivation before comparing, because a derived space may have no encoder that emits it natively — a truncation at a width the model never publishes — and re-embedding is then impossible by definition | a few seconds, GPU — under one percent of production | bulk substitution: a wrong model, a wrong quantisation, a bundle mostly garbage |
| **T4 verify on use** | a poisoned vector is inert until a search surfaces it; the chunks a search actually cites — those in the list it returns to its caller — are re-embedded once with this peer's compliant encoder, asynchronously, and marked *verified here* forever | a few seconds per answer, once per chunk ever cited | targeted poisoning, which sampling cannot find |

**T0's envelope test was measured on 2026-09-09, and BOTH its literals
and its SHAPE were wrong.** They had been written without data — cosine
≥ 0.9, and every per-dimension standard deviation within half and twice
the envelope's — and `space.envelope` is a Codex field, so a wrong test
freezes at `create` and every peer inherits it.

Six runs, each 512 calibration texts against 300 chunks of a real corpus
cut by the collection's own chunker. THREE of them are a live
collection's own space — its encoder, its dimension, its corpus — and
between them they cover every text space the reference deployment runs.
The other three vary one thing each so that what moves a statistic can
be told apart from what does not:

| | encoder | dim | class | corpus | what it is | cosine | ratio min | outside half-to-twice |
|---|---|---|---|---|---|---|---|---|
| A | Qwen3-Embedding-8B | 4096 | decoder | English | a dimension probe — no live space runs at 4096 | 0.752 | 0.375 | **16** |
| B | Qwen3-Embedding-8B | 4096 | decoder | Chinese | the same, on a harder corpus | 0.658 | 0.355 | **33** |
| C | Qwen3-Embedding-4B | 2560 | decoder | English | **`gutenberg`** — the step-6 target | 0.747 | 0.532 | 0 |
| D | MedCPT article | 768 | BERT | English | that encoder on the wrong corpus, a control | 0.933 | 0.453 | 0 |
| E | MedCPT article | 768 | BERT | **PubMed** | **`pubmed`** | 0.882 | 0.674 | 0 |
| F | bge-m3 | 1024 | BERT | **Wikipedia** | **`wikivec`** / `wikipedia` | 0.813 | 0.642 | 0 |

Run F is the most faithful of the six and the least work: the bundle is
not a re-embedding of anything. `wikivec`'s vectors came pre-embedded
from Upstash and its table already holds them, along with the exact
inputs that produced them — `title` and the paragraph, joined — so 300
rows were taken straight out. Nothing about the input format could be
got wrong, because nothing about it was reconstructed. Only the envelope
needed the encoder.

Run E is a different CORPUS SHAPE and not merely a different corpus:
PubMed abstracts under the `abstract` chunker, one chunk per article,
302 to 3,034 characters where every Gutenberg bundle was 3,892 to 4,000.
It is the case the other four could not speak for.

**The two halves are not equally well evidenced, and saying so is the
point.** They fail in different places:

- The COSINE bound of 0.9 refuses **both live text spaces measured** —
  `gutenberg` at 0.747 and `pubmed` at 0.882. That half was simply wrong,
  at every dimension, for collections that exist.
- The DEVIATION bound refuses **none of them**. It was only ever seen to
  fail at 4096 dimensions, and no collection here runs at 4096: the
  reference deployment's spaces are 2560, 1024 and 768, and at all three
  nothing lands outside the old band.

So the deviation rule is changed on an ARGUMENT plus a demonstration at a
dimension nobody here uses, not because it was refusing anyone's honest
work. The argument is that a per-dimension gate over thousands of
dimensions is a multiple-comparison problem — a bundle gets one chance to
fail per dimension, so the false-alarm rate grows with the space — and
the demonstration is that the same honest vectors pass at 2560 and put
sixteen dimensions outside at 4096, which no property of the encoder or
the corpus explains. This platform's own T0 reached the same conclusion
independently and by calculation rather than by measurement
(`app/verify/t0.py`: at dim 1024 a 3-sigma per-dimension gate "rejects
94 % of HONEST bundles").

That is a latent defect rather than a live one, and a reader deciding how
much to trust these constants should weigh the two halves differently.
What makes it worth fixing anyway is that the fix costs nothing — the
percentile form holds the ORIGINAL band, so no constant is loosened —
and that the space is shared: a peer embedding into one of these
collections from a 4096-dimensional encoder is a peer the old rule would
have refused, and `space.envelope` freezes at `create` where nobody can
correct it afterwards.

Rows A and B refused bundles whose vectors were entirely correct. Rows C
and D say why, and the last column is the whole diagnosis: nothing lands
outside at 768 or 2560 dimensions, sixteen do at 4096, thirty-three do at
4096 on a harder corpus. `min` over 4096 samples is smaller than `min`
over 2560 for no reason but the count. A rule written as *every*
dimension is therefore a multiple-comparison problem — an honest bundle
gets one chance to fail per dimension, so the false-alarm rate grows with
the space, and widening the band only postpones the failure to a larger
one. Stating the same rule as a PERCENTILE removes the dependence,
because a percentile does not drift with sample count. It holds on all
four: 0.601, 0.557, 0.645 and 0.682 at the 2nd percentile, across a
five-fold span of dimension and two model classes.

The COSINE moves for a different reason and it is worth separating.
Within one model family a size change is nothing — 8B to 4B moved it
0.005. Across a corpus, 0.095. Across a model CLASS, 0.18: MedCPT sits at
0.933 where Qwen3 sits at 0.75, because a BERT-class encoder pooled to
768 dimensions puts a diverse probe set and a single corpus much closer
together than a decoder-class one at 4096 does. So the encoder is not the
small term it looks like from a same-family comparison; what is small is
model SIZE. All four clear the floor, and the binding case is the lowest
of them rather than the average.

So the shape changed and the constants barely had to. Measured over the
five honest bundles and seven deliberately broken ones (the controls run
twice, in the two spaces where each side of the band binds), the 2nd and 98th
percentiles against a band of four tenths to twice gives the honest
bundles 0.157 of margin at the low end and 0.334 at the high one, at 768,
2560 and 4096 dimensions alike, while still refusing: random unit vectors (cosine −0.02),
a foreign model modelled as the same vectors with dimensions permuted
(p2 = 0.277), an un-normalised bundle (p2 = 1.77, which the cosine does
not notice at all), a bundle half real and half random (p98 = 2.28), and
a degenerate bundle of one repeated vector.

**What it does NOT refuse, and this is the honest limit of the tier.** A
bundle a tenth random passes both statistics in every space measured —
its cosine is indistinguishable from clean, and its percentiles sit
inside the band, though only just where the band binds (p98 = 1.930
against a ceiling of 2.0 on the PubMed space, where a quarter random IS
caught). The extremum rule did catch those, and it caught them with the very
statistic that refuses honest work, so there is no version of this test
that sees partial contamination and admits real bundles. That is not a
gap to be closed here: it is why the tier is labelled sanity, why T3
re-embeds sixteen chunks on the importer's private randomness, and why T4
re-embeds what a search actually cites. A partially poisoned bundle is
theirs to catch — though "theirs to catch" is weaker than it sounds and
the arithmetic belongs here rather than in a reader's head. T3 draws
sixteen chunks and fails when fewer than twelve reach `tolerance`, so it
needs five bad draws: against a 10 % bundle that happens **1.7 %** of the
time, and against a 5 % bundle 0.09 %. Detection is the network's, not
one importer's — 175 independent importers to reach 95 % confidence on a
10 % bundle, and 3,493 on a 5 % one — which is exactly why T4 exists and
why it is keyed on what a search actually cites rather than on a sample:
a poisoned chunk nobody reads harms nobody, and one that is read is
re-embedded the first time it is cited. A few percent of wrong vectors is
not caught by admission at all, and no tier here claims otherwise.

**The band is TWO-SIDED, and which side binds depends on the corpus.**
Run E is the one that establishes this, and it overturned the reading the
first four supported. Every Gutenberg bundle was LESS varied than the
calibration set — ratios from 0.36 to 1.54, sitting under 1 — which made
the floor the only side that seemed to matter and the ceiling look like
2.6 times more headroom than anything needed. A PubMed bundle is the
other way round: short technical abstracts, one chunk per article, on an
encoder whose window truncates the probe set hard, and its ratios
STRADDLE 1 — median 1.322, 98th percentile 1.666.

| | worst honest value | bound | margin |
|---|---|---|---|
| low side | p2 = 0.557 (Qwen3 4096, Chinese) | 0.4 | 0.157 |
| high side | p98 = 1.666 (MedCPT 768, PubMed) | 2.0 | 0.334 |

Both hold, neither is generous, and a reading that watched only one of
them would have been wrong about the next corpus. The tier is also
SHARPER in that space, not weaker: re-running the controls against the
MedCPT envelope with a PubMed bundle catches six of seven rather than
five, because contamination there pushes the ratios UP through a ceiling
rather than down toward a floor. Only a bundle a tenth random still
passes, and it passes at p98 = 1.930 against a ceiling of 2.0.

**How far these numbers can be trusted.** Four encoders across two model
classes, dimensions from 768 to 4096, four corpora and three chunkers —
covering EVERY text space the reference deployment runs. What is left is
another MODALITY
— a genome collection, where the alphabet and the encoder family change
at once — and that one cannot be measured at all until a DNA calibration
set exists, since 09 §9.12 ships one set per modality and the platform
ships only `text`. Given that the
cosine has already ranged 0.658 to 0.933 and the deviation percentiles
0.557 to 1.666 over what has been tried, an untested shape can land
outside either bound, and the honest expectation is that it eventually
will.

The band admits honest work; it is not a wall against dishonest work. The
first honest bundle it refuses is evidence to re-measure rather than to
widen.

A stricter test than a percentile exists and is worth naming, since a
peer may already have it: one aggregate decision over the whole
deviation profile — a sum of squared per-dimension z-scores against a
null distribution bootstrapped for bundles of that size — which controls
the false-alarm rate exactly rather than approximately, and keeps the
per-dimension numbers as evidence. It is not required here because it
needs a published null per bundle size, where §9.12's envelope is two
rows and nothing else. A peer that computes it anyway may report it, and
its verdict is evidence like any other; what admits a bundle is the test
in the table.

The importer's randomness in T3 is private and never derived from the
bundle, or a producer could re-roll a nonce until the sample missed its
corruption. What sixteen chunks on the quorum catch is stated
honestly: one importer fails a half-corrupt bundle nineteen times in
twenty (96 %) and a bundle with a third of its vectors wrong about two
times in three (66 %); a bundle with a few percent of wrong vectors
passes T3 nearly always — 98 % of the time at one chunk in ten — and is
left to T4 and to `vectors-sample` across the network.
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

What T3, T4, `vectors-sample` and a bundle's re-embed proof re-embed is
the chunk as the pipeline feeds it. A stored row keeps a chunk's text and
not a pair layer's two segments (02 §2.4, 09 §9.3), so for a pair layer
the chunk is re-cut from its record through the collection's layers, and
used only when the re-cut chunk has the stored id and the stored text;
where the record cannot be read here, or does not give the chunk back, the
check takes its can't-run outcome — inconclusive, or a tier not run —
and never a pass (*amended 2026-10-08*, docs/notes/input-forms.md).

A **verified failure** — a T3 fail, or a T4 miss that reproduces on a
second recompute, on a peer whose encoder holds a current calibration
statement for the space — is a **proof of forgery**: an attestation of
check `forgery` (09 §9.8) carrying the chunk id, the hash of the
claimed vector, the hash of the recomputed vector and the encoder
image, and for a red-class collection (04 §4.4) the record's
`content_hash` in place of the chunk's text, which never travels. It
**burns** the producer's key on the peer that found it: its bundles
are dropped from search — their chunks
return to *without vectors* and the coverage map says so — and its
attestations are not counted; the burn is permanent until an
operator runs `hold key trust <key> --restore`. (A burned key was
also called *quarantined* until 2026-09-19. That word now means one
thing, a copy not yet tested, §3.6; a key is *burned*, which is not
*denied* — denying is an operator's act, and no command creates a
burn.) On every other peer the
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
check, seed and sampled parts, result — `pass`, `fail`, `forgery`,
`withdraw` or `supersedes` — a detail block per check, platform image,
encoder engine and weights for statistical checks, the verifier's key, time. Its id
is the hash of its canonical body and its signature is under the
attestation domain (09 §9.8, 09 §9.2). An inconclusive check yields
none. Attestations travel like any event (04 §4.2), a peer that lacks a
collection's attestations pulls them (09 §9.9), and they are stored
beside the catalogue.

An attestation counts toward a level only for the snapshot it names,
and for at most two cadences from its `time`; when a peer adopts a
newer snapshot, an older snapshot's attestations count for the parts
the newer one lists with the same root, which is what a snapshot
inherits unchanged. An attestation whose `time` is more than an hour
ahead of the receiving peer's clock is dropped.

A collection's level is computed **locally**, from the attestations a
peer holds. A level counts **independent** attestations: two
attestations are independent when they come from distinct keys and
carry distinct seeds where the check is a sampled one; a null seed —
an exhaustive or deterministic check (09 §9.8) — is not compared, since
two honest peers running `roots` over the same parts must agree, and
requiring them to differ would put `checked` out of reach for ever. A
key is counted once. The trust list
(`hold key trust`) is a local allow/deny filter over whose attestations
are counted at all — a denied or burned key's are ignored — and is
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
| **verified** | at least eight independent keys, and the union of their samples covers a quarter of parts for `records-vs-origin` and a tenth for `vectors-sample`; the covered fraction keeps rising and is shown |
| **flagged** | a deterministic check failed — one reproduction is enough, and it names the part; or a statistical check failed twice, from independent verifiers, the second on the chunk ids the first named (below) |

*The same sample* for a statistical flag is exact: a failing
`vectors-sample` or T3 attestation lists the chunk ids that missed; a
second key re-embeds exactly those chunks with its own compliant
encoder and reports them missing too. A deterministic flag names the
part; a statistical flag names the producer of those vectors, and
applies to that producer's vectors in that space, never to the
records. A failing deterministic attestation flags only when the
verifier's platform image is the Codex's or one with a passing
equivalence attestation for the collection (§3.4); a failure under any
other image is that image's failure. A `roots` or `chunks` failure on a
part this peer holds is first a local fault: the part is excluded
from search and share, re-fetched from peers (04 §4.3), and re-checked;
a flag is published only when a freshly fetched part whose root
matches the signed snapshot fails `chunks`, or `records-vs-origin`
fails on it. `hold check` reports the first case as a corrupt local
part with the re-fetch queued, never as flagged. A flag received from
another key is acted on after local reproduction; a peer that cannot
reproduce it — no origin, part not held — shows it as a claim and
counts it as a flag once two distinct keys report the same part or
the same chunks failing.

A flagged collection stays searchable and is marked — except on a peer
whose OWN `records-vs-origin` failed on it, where the copy is in
quarantine (below); its publisher's
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
                    witnesses: [<part or record ids>], expected, observed,
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

**Quarantine: a peer's own tests come before any use** (operator,
2026-09-19). A trust level says what strangers found. Quarantine says
what THIS peer has found, and it comes first. Downloading a collection
is its operator's decision; using it waits for the tests. A copy a peer
holds is in one of three states, each DERIVED from what the peer holds
— its attestations, its calibration statements, its acknowledgements —
and written nowhere:

| state | meaning | cleared by |
|---|---|---|
| **refused** | the copy cannot be right: the Codex's inner hash is not the id its file names, the vector table is not the Codex's space or width, the state database is another platform's (07 §7.8) | a person, and nothing else |
| **quarantine** | the copy is not yet tested on this peer | the five tests below, by themselves; or the two acknowledgements; or, for a copy in use before the peer enforced, its operator's listing (below) |
| **in use** | searched, extended and shared as its file says | — |

A copy leaves quarantine when, on this peer:

1. `roots` and `chunks` pass on every part held;
2. every vector bundle held passes T0, T1 and T2 (§3.5);
3. the gate of §3.3 has RUN against the run its encoder states, and
   against its query encoder's in an asymmetric space. A pass validates
   the run; a failure withholds the vector leg and leaves the rest in
   use (§3.3). What keeps a copy in quarantine is a run not yet tested;
4. the licence acknowledgement exists where the class requires one
   (04 §4.4);
5. this peer's own `records-vs-origin` passes over the plan's sample
   (§3.2) — the size the creator fixed, fetched within the peer's
   budget (04 §4.3).

All five are the peer's own work, against the Codex and the origin,
needing nobody's word. They are possible because a copy carries what
they need — the Codex with its calibration member, the signed snapshot
with every part's roots, the terms — which is also why a collection
without them cannot be created (06 §6.6) and what makes one shareable:
any peer that downloads it can test it. Tests 1 and 5 have nothing to
run on for a never-share collection (02 §2.9): a deposit collection
holds no part, and its origin is the client that wrote it, so it waits
for the gate and the licence alone and its `origin_check` reads
`not-planned` (09 §9.3; operator, 2026-10-06). Every other collection
is judged by all five, whatever samples its plan sets.

When the fifth CANNOT RUN — the origin is unreachable or past its
bounded wait, or this peer's platform image is neither the Codex's nor
equivalent (§3.4), so that its failure would say nothing about the
collection — and the other four hold, the check is deferred and
retried, and the peer asks its operator, showing what passed, what
could not run and when it was last tried. `hold collection ack
<collection> --origin-unchecked` records an informed decision, with its
time and the peer's key, beside the licence acknowledgement, and the
copy is then in use like any other, with no mark and no warning; `hold
collection forget` rejects it. There is no timeout and no default, as
there is none for a licence. The check still runs when it can, as it
does on every copy in use, and the attestation the peer could not
publish before is published then.

**Copies in use before a peer enforced quarantine** (operator,
2026-09-20). A peer that already used copies the day it began to enforce
cannot test them all at once: they were made before the tests existed.
Its operator lists them in the node's desired state, by collection id
and never by name (`platform.toml`, `[node] quarantine_in_use_before`).
For a listed copy that decision stands in for tests 1 and 5 while they
HAVE NOT RUN or CANNOT RUN, and for nothing else: not the gate, which the
peer runs by itself; not the licence (04 §4.4); not a test that failed,
which quarantines a listed copy like any other. Nothing is pretended:
the copy is in use, `hold collection show` prints the decision beside
each test still open and what clears it, no attestation is written, and
`origin_check` reads what it is (09 §9.3). The tests still run when
they can, and once they pass the entry says nothing. A copy that is not
listed is tested — the list is how an existing peer crosses over, and
the next collection it creates or downloads is not in it.

While in quarantine a copy enters no search, in either leg, and the
response names it as withheld with the reason (05 §5.3). It is not
announced and not served; nothing is embedded for it, contributed to
it, imported into it or certified. What goes on is what makes it
testable: the fetch its operator asked for, the five tests, the gate.

A copy RETURNS to quarantine in two cases. When its encoder states a
run this peer has not tested — a model relaunched with another flag, a
new image, a new driver — until the gate has run, which it does at
once; every collection of that space on the peer goes with it. And
when this peer's own `records-vs-origin`, under the Codex's image or an
equivalent one, FAILS on a part whose root matches the signed
snapshot: the flag is published as above, and the copy is not used
here until a person decides, because a copy that does not reproduce
its origin is not one to search. A `roots` or `chunks` failure stays
what this section already makes it, one part's local fault. An
encoder or an origin that cannot be reached is a deferral: it returns
nothing to quarantine.

`hold collection show` prints the state, every reason, and the test or
the command that clears each (07 §7.8). Quarantine applies to a copy
with a Codex: nothing else carries what the tests need.

## 3.7 Certified collections: the anchor and the seal

For a certified collection the question "where did this packet come
from" is a chain of five links, of which the platform supplies three:

```
external anchor → certifier key → signed snapshot → part root → bytes
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
*archival*, offline, and still working when the origin is gone. A trusted
list is trusted for who signed it, not for its hash: the LOTL by a key the
Official Journal announced, a national list by a key the LOTL's pointer for
its territory names — verified by the certifier at capture, re-verified in
archival mode wherever both lists are held, and in live mode for the
current lists (*amended 2026-10-07*). Archival
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
a fetched part is promoted from staging into the copy: *a part enters
only if its root is in a signed snapshot of the chain* — any snapshot
this peer holds that is signed by a certifier key valid at its
signature time (§3.7), not only the newest, since a part stays listed
by every snapshot that still carries it. This is a refuse-to-start
class rule, not a warning. A part
superseded by a re-issue stays on disk under its old snapshot's name
(02 §2.2) and is excluded from search and embedding, never deleted. A
revoked or mismatched anchor does not lift the gate: snapshots signed
after the revocation are not signed for its purposes, nothing later
promotes, and the succession rule of §3.7 decides what the collection
becomes.

Everything downstream is defence for a gate that was bypassed — a folder
populated by hand, an old snapshot left on disk:

- **granularity** — a chunk is a pure function of one record, so the
  unit of certification is the part, the unit of discard is the record
  and all its chunks, and the unit of halt is the collection on that
  peer;
- **halt** — embedding stops for the whole collection and no bundle is
  published, because vectors for chunks outside the snapshot would
  poison everyone's coverage map; the peer marks the collection
  *integrity violated* until it re-fetches and re-checks: `share fetch`
  replaces every excluded part, `hold check` passes `roots`,
  `signature` and `anchor`, and the mark clears;
- **search continues** over the certified subset, with the exclusion
  visible in the results — never a silent drop.

Who is at fault is a separate question from what to do, and the answer
does not depend on good faith:

| found | at fault | flagged | caught by |
|---|---|---|---|
| bytes that do not match the claimed root | the sharer served corrupt or forged data | the sharer | the transport, before anything reaches the copy |
| a part whose root is not in a signed snapshot of the chain | someone introduced foreign material | the local source of that part | the promotion gate |
| a valid, signed part whose records do not match the origin | the certifier: a pipeline bug or a compromised key | the certifier and the snapshot; the sharer served faithfully what was signed | `records-vs-origin`, `origin-seal` |

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
