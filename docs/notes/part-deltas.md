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

**D. A patch packed on request, by any peer holding the new part
(operator, 2026-10-11).** The peer holding the old version asks a peer that
holds the new one — the producer or anyone else serving it — with a signed
pull, the family of 09 §9.9: `pull patch(collection_id, snapshot_id, part,
have)`, where `have` is the hash of each line of the version it holds (50
hashes for a gutenberg part, a few KB). The answering peer decompresses its
new part, compares line hashes, and returns the lines the asker lacks and
the uids to drop. The asker merges them in `native_id` order, writes the
part with the platform's writer, and promotes it only if it hashes to the
root the signed snapshot lists (the direction is the chain's: §"What any
delta must keep").
- Cost: the changed lines; the answering peer needs only the NEW part (no
  old version, no stored patches), so any holder can answer and the load
  spreads like the swarm's.
- Any gap: a peer two snapshots behind sends the same `have` and gets one
  patch to the latest version, never a chain of patches.
- A wrong answer is caught by the root, as a wrong piece is. A miss is
  ambiguous (a lying peer, or a writer built differently), so it costs the
  asker the full part from the swarm and never bans anyone on its own; a
  peer whose patches miss for others too can be scored down (§4.5).
- Same catch as A: the rebuilt bytes must be the writer's, so the writer
  (zstd library, level, frame layout of 09 §9.5) is pinned.
- Spec: one new pull message and its answer (09 §9.9); no new member, no
  new snapshot field. Ask first (wire format).

**D′. The same request, answered in compressed frames (operator,
2026-10-11).** A part already carries its seek index (09 §9.5: every
frame's offset, compressed and raw size, line count, first uid). So the
answer need not be lines: for each frame of the new part it is either
"copy your frame k" (the asker sends a hash per frame it holds) or that
frame's compressed bytes, verbatim, plus the new index frame. The asker
concatenates and gets the new part's exact bytes; the root decides as
before.
- Nothing is recompressed on the asker's side, so the writer need not be
  pinned for the patch to land (the producer's unchanged frames are the
  same bytes because the producer's own writer wrote both versions).
- The cost is per frame, and frames hold 1,000 records today. Measured:
  pubmed `year=2020/00001` is 51 MB in 30 frames, so one changed article
  costs one frame (~1.7 MB) instead of 51 MB; gutenberg `lang=en/00001` is
  9.5 MB in ONE frame of 50 books, so a gutenberg patch is the whole part.
- For gutenberg it needs frames of one book: a writer rule or a Codex field
  (`slicing.frame_records`), i.e. new part bytes — a new collection.
  Measured 2026-10-11 on 24 random gutenberg parts (1,200 books, 431 MB raw;
  a book's raw line p10 52 KB, median 275 KB, p90 753 KB, max 6.6 MB),
  zstd level 9 as the writer:

  | frames | size | vs today | one revised book's patch |
  |---|---|---|---|
  | one per part (today) | 131.2 MB | — | the part, 5–9.5 MB |
  | one per 5 books | 135.8 MB | +3.5 % | ~450 KB |
  | one per book | 142.8 MB | +8.9 % | ~90 KB (median book) |

  One frame per book is the recommendation: +0.7 GB on gutenberg's 7.9 GB,
  a patch the size of the book, and a dropped book removes only its own
  frame, so nothing shifts (the content-defined boundaries below become
  unnecessary). A shared zstd dictionary would win most of the 8.9 % back
  but must travel and be pinned, and 09 §9.5 says no dictionary.
- Frame boundaries are counted from the part's start: an edited or an
  appended record moves only its frame, but a record dropped mid-part
  shifts every frame after it. Content-defined boundaries (a frame ends
  after a record whose uid hash meets a mask) would keep drops cheap too.

**Plates.** Whatever is chosen for records, a plate container should be
rebuilt byte-identically when its plates did not change, so its root
stays and nothing moves; a test should pin that before anything else.

## Recommendation

1. First, a test that a re-issued part's plate container is byte-identical
   when no plate changed (cheap; it may already hold).
2. Then D (it supersedes A: nothing stored, any holder answers, any gap in
   one step), behind a flag, as a pure optimisation: the full part remains
   the path every peer can always take, and the patch only ever saves bytes.
   The same request works for a plate container, by plate hash instead of
   line hash.
3. Measure on gutenberg's next snapshot (2026-10): parts re-issued, bytes a
   follower moves with and without patches.
