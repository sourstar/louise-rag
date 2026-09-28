# Fetching a collection's weights from peers

*Design note, 2026-09-28. Not scheduled; nothing here is built.*

**The gap.** A peer that takes up a new collection cannot run its encoder
until someone copies the weights into `/models` by hand. RAGE fetches
nothing, by design (`rage.md` §10). The spec already says who should
fetch them instead — the node, from other peers — but nothing implements
it, and three formats it needs are not written down yet (§3).

## 1. What the spec already fixes

- **Weights are content-addressed** (02 §2.4). The Codex names them by
  digest: of the file, or, for a directory model, of the canonical map
  from each file's path to its digest (09 §9.3).
- **The digest is the whole check.** There is no signature and no trust
  involved (04 §4.3, *the encoder travels too*).
- **Holders announce on the DHT.** A peer holding the weights announces
  them under the first 20 bytes of `H(weights digest)` (09 §9.16).
- **Peers serve from an "encoder store".** Torrents are served only from
  the drag, `state/bundles/`, or "the platform's encoder store" (09 §9.16).
- **Some weights may not be passed on.** The platform's list of encoders
  records whether a peer may redistribute them. For those it may not, it
  names the origin and a newcomer fetches from there (04 §4.3).
- **One budget for every byte** (invariant 5): the transfer manager's
  token bucket.

## 2. The design

**Who does it:** Louise's worker, never RAGE. RAGE keeps reading a
read-only directory and never learns where a file came from.

**Steps**
1. **Trigger.** A collection is held, or `louise model fetch
   <collection | weights digest>` is run, and the weights named by its
   `space.encoder` (and query encoder) are not in the store.
2. **Find holders.** Look up the DHT key, then ask each address that
   answers for the weights' manifest (§3a).
3. **Download.** Use the existing BitTorrent v2 transport
   (`app/acquire/transport_torrent.py`), which already:
   - writes `.part` files and promotes them with `os.replace`;
   - resumes per piece;
   - takes a rate lease from the central bucket.

   Files land in staging, `state/encoders/staging/<digest>/`.
4. **Verify.**
   - Each file against the manifest.
   - Then the whole model against the Codex digest, by the §9.3 rule
     (symbolic links refused).
   - A mismatch discards the download and drops that holder.
5. **Promote.** One rename into the store, `<store>/<digest hex>/`. A
   model is either whole in the store or absent: never partly there.
6. **Fall back to the origin**, only when no peer holds the weights, or
   they may not be redistributed and the registry names an origin (for
   example a Hugging Face repository at a pinned revision). Same budget,
   same digest check.
7. **Seed.** After promotion, announce under the DHT key, but only if
   the registry says the weights may be redistributed.

**Where RAGE sees them**
- The store root is bind-mounted into RAGE read-only, once, at
  `/models/store`. That replaces today's one bind per model
  (`deploy/truenas-rage.yaml`).
- An entry names `/models/store/<digest hex>/…`.
- A missing file is `absent`, exactly as today.

**Rules the code must keep** (CLAUDE.md)
- *Deferral, not failure.* No holder answering, or the budget exhausted,
  defers the fetch. Neither writes an error against the collection.
- *SIGTERM.* The unit is a file: the current piece finishes, resume data
  is saved, and the process exits 130.
- *Disk.* The sizes are known before the first byte moves (§3a). A fetch
  that would not fit is refused, and the reason is shown.
- *Bounded waits.* Every DHT lookup and peer message has a timeout.
- *Keyed on physical identity.* Staging and the store are keyed by
  digest, never by a model's name.

## 3. What the spec does not say yet — decisions needed

a. **The manifest message.** The fetcher knows only the SHA-256 digest.
   A BitTorrent v2 torrent is identified by each file's *pieces root*,
   which is a different hash, so the infohash cannot be computed from the
   Codex alone. Proposal: a 09 §9.9 peer message that, for a weights
   digest, answers `[[path, bytes, sha256, pieces root], …]`. The fetcher
   checks that the answer's own paths and hashes reproduce the Codex
   digest before trusting it. That check makes a lying holder cost only
   one round trip.

b. **The torrent's shape for weights.** 09 §9.16 defines torrents only
   for drag members. Proposal: the same rule — one BitTorrent v2 torrent
   per file, with the info dictionary fixed to name, length and pieces
   root — and the name `weights-<first 16 hex of the digest>`, so two
   honest holders land in one swarm.

c. **Where the encoder store lives.** It needs a line in 07 §7.8's tree,
   and a Tier-2 setting for its path.

d. **The platform's list of encoders.** The spec names it (04 §4.3). I
   found no file or code for it. Per weights digest it needs: whether they
   may be redistributed, and the origin (repository and revision) to fetch
   from otherwise.

e. **Gated licences.** Some weights require accepting terms before
   download. Whether that uses the same recorded acknowledgement as a
   yellow or red source (invariant 9) is the operator's decision.

## 4. Out of scope

- **Engine images.** They travel the same way under their image digest
  (04 §4.3). The mechanism would be the same, but RAGE's own image is
  built and pinned, never fetched at run time.
- **Drafting the RAGE entry** from the Codex. That is a separate note.

## 5. Done when (tests)

- A fixture holder serves a single-file model and a directory model. The
  fetcher lands both in the store, and their digests reproduce the
  Codex's.
- A holder whose manifest does not reproduce the digest is dropped before
  any byte is fetched. One that serves wrong bytes is dropped and the
  staging files are discarded.
- An interrupted fetch resumes from staging. A SIGTERM mid-file exits 130
  and leaves nothing in the store.
- No holder and no origin: a deferral, with no error on the collection.
- Weights marked not redistributable are fetched from the origin and
  never announced.
- The fetch draws from the central bucket: a test with a tiny ceiling
  shows the rate held.
- RAGE, with the store mounted, loads an entry that names the promoted
  path. Before promotion, the entry is `absent`.
