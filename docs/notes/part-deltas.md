# Part deltas: a new part version for the price of what changed (design, open)

*Operator, 2026-10-11: "We should think about a smart way to promote a Part
by downloading only the delta so that over the time there is not much
overhead." Not decided, not built. What follows is the problem measured on
gutenberg and the options, with a recommendation.*

## The problem

A part is the unit of transfer (04 §4.3): one BitTorrent v2 torrent per
file, its root in the snapshot. When one record in it changes, the next
snapshot re-issues the part under the same number with a new root, and a
peer that follows the collection downloads the whole new version.

Gutenberg, measured on its SHARD (2026-10-11):
- 1,637 record parts, 7.9 GB: about 5 MB a part, English 8–9.5 MB, 50
  books each (`shard_records = 50`).
- 1,530 plate containers, 87 GB: about 57 MB each, some over 100 MB. A
  container is re-issued with its part (09 §9.5); if its rebuilt bytes are
  identical its root is too, and nothing moves — not yet confirmed.
- Between the 2026-09 build and today, 290 of 79,583 books changed
  (revised upstream or new), spread over an unknown number of parts. A
  peer following monthly pays roughly the parts those books sit in, plus
  each language's open last part.

So the overhead is "a part per changed book", bounded by `shard_records`,
and it never touches the old version: a peer that does not follow keeps
serving what it has.

## What any delta must keep

- **The part's root stays the proof.** A peer promotes a part only when its
  bytes hash to the root a signed snapshot lists (04 §4.3). A delta is a
  way to *build* those bytes locally; the root still decides.
- **A failed rebuild is a deferral, not an error:** the peer falls back to
  fetching the whole part.
- **Parts stay immutable.** The delta is a new, separate artefact; neither
  the old part nor the new one ever changes.

## Options

**A. A patch member, rebuilt by the platform's own writer.** The producer
publishes, beside a re-issued part, `patch-<slice>-<n>-<from>` (from =
the snapshot of the version it patches): the replaced and added record
lines and the dropped uids, zstd. The peer merges them into the lines of
the version it holds (records are sorted by `native_id`, so the merge is
exact), writes the part with the platform's writer and checks the root.
- Cost: the changed records only — tens of KB for one revised book.
- Catch: the compressed bytes must come out identical, so the writer's
  zstd library, level and frame layout become part of what a part *is*.
  Peers on another zstd build miss the root and fall back to the full part:
  correct, but the saving is lost for them. Pinning the writer is already
  how `image-equivalence` thinks (03 §3.4), so this is the same discipline.
- Spec: a new member kind (09 §9.16) and its snapshot entry; ask first.

**B. Smaller frames, and let BitTorrent skip what it holds.** Write a part
as many small zstd frames (one per record or per few records) at
content-defined boundaries, so an unchanged record stays the same bytes.
BitTorrent v2 hashes files in 16 KiB leaves; a client that seeds the new
torrent from the old file's matching leaves downloads only what differs.
- Cost: whatever the leaf alignment leaves over, more than A.
- Catch: standard clients do not reuse data across torrents; this needs
  our own piece source, and smaller frames compress worse.

**C. Smaller parts.** Lower `shard_records`. Simple and already supported,
but a Codex field — a new collection — and more torrents, more snapshot
entries and more files per peer. It shrinks the waste, never removes it.

**Plates.** Whatever is chosen for records, a plate container should be
rebuilt byte-identically when its plates did not change, so its root
stays and nothing moves; a test should pin that before anything else.

## Recommendation

1. First, a test that a re-issued part's plate container is byte-identical
   when no plate changed (cheap; it may already hold).
2. Then A, behind a flag, as a pure optimisation: the full part remains the
   path every peer can always take, and the patch only ever saves bytes.
3. Measure on gutenberg's next snapshot (2026-10): parts re-issued, bytes a
   follower moves with and without patches.
