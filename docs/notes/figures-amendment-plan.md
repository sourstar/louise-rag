# The figures amendment — drafted, not applied

*Written 2026-09-09. Pairs with [`figures-in-the-corpus.md`](figures-in-the-corpus.md),
which holds the decision this implements.*

**Nothing in this file has been written into docs/01–09.** It is the shape
of the amendment, put in front of the operator before any specification
text moves, because the amendment turns out to be much larger than the
decision record's bill and because **four wire-format calls are genuinely
open** — the drafters disagree on them, and each is a byte every peer must
agree on for the life of the network.

## How this was produced, and what that is worth

Six readers drafted against docs/01–09 in parallel, one or two documents
each, plus one whose only job was to sweep every document for CLOSED LISTS
— places that enumerate what may exist and thereby refuse what is not
enumerated. They produced 100 proposed edits.

The reconciliation pass and all three adversarial passes — citations,
breakage, an adversary against the drafted text — **died on a session
limit and did not run.** So:

* **Verified**: every one of the 88 checkable `current_text` anchors exists
  in the documents, verbatim modulo line wrapping. The edits are
  applicable; nothing quotes a sentence that is not there.
* **NOT verified**: that each cited section says what its reason claims;
  that the edits are consistent with one another (they are not — see
  below); that the drafted text survives an adversary.

Treat what follows as a well-anchored draft with four holes in it, not as
a finished amendment.

## The bill was wrong, in the direction that matters

The decision record says the amendment touches "nine documents, two schema
versions, three closed lists."

| | recorded | found |
|---|---|---|
| documents | nine | nine (01, 02, 04, 06, 07, 08, 09 certain; 03, 05 near-certain) |
| schema versions | two | **at least four forced, two of them not on the list** |
| closed lists | three | **thirteen blocking** |

**Two forced bumps nobody had counted:**

* **The record line, 1 → 2.** §9.5 fixes "a JSON object with exactly these
  keys, written in this order". A `figures` key is a new version by
  §9.18's own rule, and §9.18 refuses an unknown key on read and on write.
* **`protocol_version`, 1 → 2.** §9.17 says the plugin protocol "is fixed
  with this version and not amended into it", and `CanonicalDoc` must gain
  a figures field for a plugin to hand one over at all — which is exactly
  what `app/plugins/base.py` cannot do today.

**Two §9.18 rows have to SPLIT**, because each pairs a schema that moves
with one that does not: `louise-snapshot, louise-snapshot-sig | 1` (the
sidecar does not change) and `record line, shard index (LOUISE-PACK-IDX1)
| 1` (the frame layout does not change).

## The thirteen closed lists

Each of these refuses a figures member as written. Missing any one of them
does not degrade gracefully — it makes the drag unshareable or the check
unnameable.

| document | list | what it refuses today |
|---|---|---|
| 01 §1 promise 1 | what may travel | "parsed text, vectors, derived indexes" — a plate is none of them |
| 01 §1 vocabulary | member kinds | `figures` is not a kind |
| 02 §2.3 | what a record is | becomes false when the line carries figure hashes |
| 02 §2.6 | the drag tree | a directory absent from it is foreign material (07 §7.8) |
| 02 §2.6 | `--kinds` names | a peer cannot ask for the plates, or ask to be spared them |
| 03 §3.2 | `checks` | a plan may not name a check with no row |
| 04 §4.3 | members admitted outside the snapshot rule | a container with no snapshot root stays in staging forever |
| 04 §4.4 | what may be shared per class | figures are on none of the three rows |
| 07 §7.8 | `share`'s refusal sentence | **refuses the whole drag** |
| 09 §9.4 | the snapshot's `files` | and its "not a member and is not served" rule |
| 09 §9.5 | the record line's keys | "exactly these keys" |
| 09 §9.16 | the member register | a name not in it is not a member |
| 09 §9.17 | `CanonicalDoc`'s fields | a plugin cannot hand over what the type has no field for |

Plus `MEMBER_KINDS` in `app/hold/drag.py`, which is the code's copy of the
same list.

## The one design call that is settled, and is right

**Figures go in a CONTAINER, one per record shard, not one file each.**

`figures/<slice>/<NNNNN>.fig.zst`, figures stored once each ordered by
hash, addressed BY HASH INSIDE the container and never by a path.

The reason is arithmetic and it is decisive: §9.16 makes every file of a
member its own BitTorrent v2 torrent, and the measured corpus holds
~275,000 plates against 1,591 record shards. One torrent per plate is two
orders of magnitude more swarm state for the same bytes. It also means
§9.1's first-16-hex path-segment rule does not reach figures at all,
because no directory is ever named after a figure.

## The four open calls — these need the operator

**1. Which hash names a figure: SHA-256 or BLAKE2b-128?**
Every other in-record hash is BLAKE2b-128 (`content_hash`, `origin_hash`),
which argues for consistency. Against it: a figure's hash is *the whole of
what a peer checks fetched bytes against*, where `content_hash`'s subject
sits on the same line and is cheap to re-derive. Figures number in the
hundreds of thousands, not the tens of millions, so BLAKE2b's speed
argument does not bite. The drafters split.

**2. Where is the container's root published — the snapshot, or the
sharer's digest?** This decides whether `louise-snapshot` bumps to 2. The
snapshot is the conservative answer (07 §7.8's "a file no snapshot lists
is not served" then just works); the digest is cheaper but leaves the
container's integrity resting on something unsigned by the certifier.

**3. Does `louise-codex` bump to 2?** One drafter says forced — the record
line has no version field of its own, and an immutable Codex is the only
carrier a collection has for one. Another says nothing forces it. This one
is worth deciding on its merits: it is the difference between "a
collection's record-line version is knowable from its Codex" and "you have
to read a shard to find out".

**4. Per-member licence class.** A green text with yellow-class plates is a
real case (a public-domain book scanned by a library that asserts rights in
the scan), and 04 §4.4's table has one class per collection. The decision
does not settle it and every drafter that met it flagged it.

## Three findings worth acting on independently

* **§8.7's numbers are unmeasured.** 16 MiB per figure, 64 megapixels,
  1,024 figures per record are "defensible but not measured". They are
  caps on attacker-supplied bytes, so they should be measured against the
  real corpus before they are fixed — `tools/gutenberg_plates_recon.py
  --deep` produces exactly that distribution.
* **A plates-only record has zero chunks and is unreachable by either leg
  of a search** (05 §5.3). Where a figure IS the document — a patent
  drawing, a map — the record exists and nothing can find it. Not created
  by this amendment; exposed by it.
* **Two pre-existing defects**, found while sweeping for figure-shaped
  ones and unrelated to figures: 09 §9.4's `files` sentence and 07 §7.8's
  `share` refusal disagree about what admits a member, and 01's vocabulary
  row states something already false about bundles, `index-<slice>` and
  `origin-<slice>`.

## What I would do next

Settle the four calls, then apply the amendment in one pass with the
closed-list table above as the checklist — because the failure mode is not
a bad sentence, it is a member that exists and that `share` refuses, which
is precisely the bug found in `snapshot._files` on 2026-09-08 and which
every test passed through.

The full 100 drafted edits are in this session's workflow journal
(`wf_1ff5024a-da9`), recoverable with the run id if the drafts are wanted
verbatim rather than re-derived.
