# Retention follows the origin — DRAFT amendment

*Draft, 2026-10-25. Not applied: the specification and the code say what
they said until the operator approves. Operator, 2026-10-25: "In the early
specs it was written that nothing is deleted but retired corpus entries are
tombstoned; this is not true anymore. It depends on the collection:
gutenberg, for instance, can replace a book making void the principle of
the verifiability; what really matters in the end is the verifiability."*

## What changes

Today every re-issued part keeps its previous version beside it
(`NNNNN.<snapshot id>.jsonl.zst`, 02 §2.3, `app/hold/shard.py`
`keep_superseded`), so an old snapshot can still be served and checked.
For an origin that keeps its history (Wikipedia's revisions, Normattiva's
versions in force) the old version can still be re-fetched and reproduced.
For an origin that replaces in place (Gutenberg rewrites a book's file
under the same number), the old version can only be checked against itself:
its origin bytes are gone. Keeping it keeps bytes nobody can verify against
the source.

Proposed rule (A): **everything held is verifiable, and what is kept follows
the origin.**

| origin declares | superseded part versions | tombstones |
|---|---|---|
| `history` (the origin serves earlier versions) | kept, as today | as today |
| `replaces` (the origin overwrites in place) | dropped once the newer snapshot holds the replacement | as today: a retraction or withdrawal is still listed in `delta.tombstoned` |

## The text, as it would read

**01 promise 7** (replaces "Nothing published is deleted. …"):
> **Nothing published is recalled, and everything held is verifiable.**
> A superseded collection stays available; catalogues mark it, nothing
> removes it. Within a collection, what is kept follows the origin: where
> the origin keeps its history, a superseded part is kept and an old
> snapshot can still be served and checked; where the origin replaces in
> place, the superseded version is dropped once the newer snapshot holds
> its replacement, because it can no longer be checked against the source.
> A tombstoned record is excluded from search and embedding from the
> snapshot that tombstones it (02 §2.2).

Promise 8's "Promise 7 is why" still holds: a peer that adopted a wrong part
keeps it; nothing published is recalled.

**02 §2.3** (after the `records/` listing): the superseded version is
"renamed … and kept" only when the plugin declares `history`; under
`replaces` it is removed at the promotion that replaces it, and a node
serves an old snapshot only while it holds every part that snapshot lists.

**02 §2.2**: "the version it supersedes is kept (below)" → "is kept or
dropped as §2.3 says".

**04 Erasure**: unchanged in meaning; "a tombstoned record stays in its
closed part" gains "until that part is re-issued (02 §2.2)", which 02
already says.

**06 §6.2**: each plugin declares `origin_history: "history" | "replaces"`
and records the reason beside the origin-form note (invariant 11 already
requires that note). Proposed values: gutenberg `replaces`; wikipedia,
normattiva, wikidata `history`; pubmed and the deposit and eli plugins to
be checked against their origins before the value is written.

**CLAUDE.md**: invariant 11 gains one sentence: "A superseded version is
kept where the origin keeps its history; where the origin replaces in
place, the newer version is what is verifiable and the older is dropped
(01 promise 7)."

## Not changed

- No byte that is hashed, signed or moved (09): the declaration is the
  plugin's, outside the Codex; the superseded file name stays 09 §9.5's.
- `erasure` (append-only / retractable / erasable) keeps its meaning.
- Peers keep what they adopted.

## Code, after approval

`keep_superseded` returns None when the collection's plugin declares
`replaces` and removes the old file after the promotion commits; the
serving side already serves only snapshots whose parts are held
(manifest-only, 02 §2.8) — to be confirmed by a test. Tests: a `replaces`
promotion leaves no superseded file; a `history` one keeps it.

## Pros and cons

| choice | pros | cons |
|---|---|---|
| **A** (this draft) | keeps only what can be verified; Gutenberg stops keeping, for every re-issued part, an older version nobody can check against the source | an old Gutenberg snapshot can no longer be served whole by us; a book the origin withdraws leaves our node with the next re-issue (invariant 11 reads "less than the origin GAVE us" — it is no longer given) |
| B: keep superseded versions and archive their origin bytes | old snapshots stay whole AND verifiable | stores every replaced origin file too; the disk cost grows with each Gutenberg revision |
| status quo | old snapshots served whole | keeps bytes nobody can check against the source; the spec claims more than holds |
