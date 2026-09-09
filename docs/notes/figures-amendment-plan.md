# The figures amendment — APPLIED 2026-09-09

*Written 2026-09-09. Pairs with [`figures-in-the-corpus.md`](figures-in-the-corpus.md),
which holds the decision this implements.*

**Applied in `c8adbc6` across all nine documents.** This file is kept as
the record of what the amendment had to decide and why, because the four
wire-format calls below were genuinely open — the drafters disagreed on
each — and the reasoning is worth more than the diff.

The operator settled all four on 2026-09-09. Where a call differs from
what a drafter proposed, the drafter's version is stated too, so nobody
re-derives the rejected one from the journal.

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

## The four calls, as settled

**1. The figure's hash is `H` (SHA-256), not BLAKE2b-128.** §9.1 gives
BLAKE2b three criteria and a figure meets only the first. There are
~275,000 plates, not PubMed's 36 million, so neither the cost of the
function nor the width of the field is the constraint. The one that
decided it: `content_hash`'s subject is on the same line and can be
re-derived, where a figure's hash stands alone over bytes that arrive
separately and is the whole of what a peer checks them against. A
producer chooses the hash it publishes, so the attack is a COLLISION —
2^64 against 128 bits — and the prize is a plate a human looks at.
Bonus: §9.1's "the only two places BLAKE2b appears" stayed true and
needed no amendment. (One drafter proposed BLAKE2b for consistency.)

**2. The container's root goes in the SNAPSHOT, not the sharer's digest.**
`louise-snapshot` → 2. The container is re-issued exactly when its record
shard is — a plate arriving rewrites the line, which moves the shard root
— so the snapshot carries what it was carrying anyway. It also lets a
peer price the plates, often the larger half of a collection, before
fetching a byte, and 07 §7.8's "a file no snapshot lists is not served"
then works unamended. The digest route's only saving was avoiding this
bump, which is illusory in an amendment where the record line bumps
regardless.

**3. `louise-codex` → 2, and it was FORCED, not a judgement.** The
drafter who said "nothing forces it" checked whether an existing field
changes; none does. But carrying the record line's version AT ALL is
itself a change to the Codex schema, and §9.18's rule closes it.

The refinement, against what the other drafter proposed: the shape is a
new `record_line` field rather than an overloaded `version`. §9.18
already versions the record line separately; `version` tracks what wrote
the Codex, `record_line` what is in the shards. Overloading would make a
future v3 Codex still have to imply "has figures", and the meanings pile
up.

The implication worth keeping: `record_line` is inside `collection_id`,
so **a Codex edited to change it is a different collection by
construction**. 02 §2.7's "a collection whose records gain plates is a
new collection" stops being a rule anyone has to remember. Note also that
`version` is NOT in `space_id`, so the bump costs no vector sharing —
what blocks sharing between plain Gutenberg and Gutenberg-with-plates is
the epub's reflowed text moving `content_hash`, not the version.

**4. No per-member licence class.** Figures ride the collection's class,
on all three rows of 04 §4.4 — including red, where they do NOT travel,
because a plate is the origin's content and not something derived from
it, so it falls on the text's side of that line and not the vectors'.
Where plates carry conditions the text does not, the collection takes the
stricter class, by the rule that already lets a collection tighten and
never loosen. Rejected because the acknowledgement is per collection —
"one ack covers records, vectors and drag alike" — and splitting the
class turns one ack into a matrix, for a case no corpus here presents.
Reversible if one ever appears.

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

## What the code now owes

The documents are amended; **no code implements any of it.** What follows
is what has to change before a plates collection can be built, and each
item is a place where the code and the documents now disagree on purpose.

* `app/plugins/base.py` — `CanonicalDoc` gains `figures`, and a `Figure`
  type appears beside it (06 §6.1). `PARSER_VERSION` moves on any plugin
  that starts yielding them.
* `app/hold/codex.py` — `CODEX_VERSION` becomes an instance field, not a
  module constant, so `loads` can accept 1 and 2; the Codex gains
  `record_line`; `_TOP`'s closed list gains it. And `derive`,
  `continue_` and `replace` must PRESERVE it and refuse to change it —
  a sister shares her mother's corpus, so her lines are her mother's.
* `app/hold/drag.py` — `MEMBER_KINDS` gains `figures`, with the member
  name and the container path.
* `app/hold/snapshot.py` — a `figures` list beside `relations`, and
  `louise-snapshot` version 2.
* `app/hold/records.py` — write the `figures` key iff the Codex says
  `record_line` 2, and the container beside the shard. Nothing can check
  that consistency at `create` (there are no records yet), so it lands
  here and in `hold check`'s `records` check.
* `app/plugins/gutenberg/pg_epub.py` already produces the right shape —
  `Figure(href, media_type, sha256, bytes, caption, seq)` — and predates
  the amendment. Its `sha256` is `H`, which call 1 confirms; `caption`
  goes to `sections` and not to the record line, which the amendment
  also confirms.

## The measurement 08 §8.7 was waiting for — TAKEN 2026-09-09

Run over the real corpus on the NAS: **79,292 book directories, 44,477
illustrated epubs, 786,699 images.** §8.7 marked its three numbers as
"chosen to be obviously sufficient, not measured". They are measured now.

**Cap 1 — at most 16 MiB per figure: HOLDS.**

| p50 | p90 | p99 | p99.9 | max |
|---|---|---|---|---|
| 44 KB | 146 KB | 533 KB | 981 KB | 1.8 MB |

Nothing over 16 MiB; the cap is ~9× the largest real plate. Generous,
but it bounds ATTACKER bytes rather than honest ones, and it now
demonstrably refuses nothing real. Leave it — tightening against
Gutenberg alone would be unwise when patent drawings and maps are the
cases still to come.

**Cap 3 — at most 1,024 figures per record: CONTRADICTED.** Eleven books
exceed it, the worst carrying 3,503 images. That is 0.025 % of the
corpus and it is the wrong 0.025 %: they are the most illustrated books
in it, which is what a plates collection is FOR. A rule that admits a
novel's frontispiece and refuses an illustrated natural history is
backwards. **Raise to 4,096** — above the observed 3,503 with headroom,
still bounding a record against becoming a container in its own right.

**The media-type closed list — VALIDATED, and the cost of refusing SVG
is now known.**

| | share |
|---|---|
| `.jpg` / `.jpeg` | 65.5 % |
| `.png` | 34.2 % |
| `.svg` | 0.31 % (2,407 images) |
| `.gif` | 0.01 % (46) |

So `{image/jpeg, image/png}` covers 99.66 %. SVG is REAL in this corpus,
so refusing it is a live cost rather than a hypothetical one — and the
follow-up measured that cost exactly: **10 books lose at least one
plate, and NO book loses all of them.** The security call stands at a
price of ten books each missing a picture or two.

## What the same run settled about the collection itself

**The plates are already on disk.** `<gutenberg-root>`
is a full Gutenberg mirror — `.mobi`, `.tex`, qrcodes and both
`pg<id>-images.epub` and `-images-3.epub`. The fetch filter's
`exclude=("*images*",)` applies to the `extras` acquisition path and not
to this tree, so **the ~90 GB re-fetch and its exception to "nothing is
re-downloaded" are both unnecessary.**

**"Has an images epub" is not "has plates".** 82 % of books have one,
because Gutenberg generates a cover for nearly everything; 43 % of those
epubs contain exactly that one image. The real question is a threshold,
and the plates turn out to be heavily concentrated:

| threshold | books | images kept | figures member | GPU-days |
|---|---|---|---|---|
| any images epub | 44,486 | 786,699 | 51.0 GB | 14.59 |
| ≥ 2 images | 25,488 | 767,711 | 49.7 GB | 8.11 |
| ≥ 5 | 16,882 | 745,136 | 48.4 GB | 5.51 |
| ≥ 10 | 12,322 | 714,952 (91 %) | 46.4 GB | 4.09 |
| ≥ 25 | 6,997 | 630,969 (80 %) | 40.5 GB | 2.61 |
| ≥ 100 | 1,681 | 376,307 | 24.1 GB | 0.85 |

Going from ≥2 to ≥10 halves the books and halves the GPU cost while
keeping 91 % of the pictures. **The threshold trades embedding cost, not
images** — which makes it a cheap decision rather than a painful one,
and it is the operator's: it decides what the collection IS.

Two things the same scan turned up that belong to other steps: 34,574 of
the 79,292 directories are EMPTY (rsync stubs for titles published only
as epub or mobi, exactly as `pg_text.scan_dir` anticipates), and 44,463
directories hold a `.txt` against STATUS.md's "59,627 indexed,
unembedded documents" — a 15,000 gap worth understanding before step 8.

## Provenance

The full 100 drafted edits are in this session's workflow journal
(`wf_1ff5024a-da9`), recoverable with the run id. The applied text is not
those edits: the four calls above settled differently from several of
them, so the amendment was written against the settled shape with the
closed-list table as its checklist. The drafts' value was the sweep and
the anchors, both of which held up.
