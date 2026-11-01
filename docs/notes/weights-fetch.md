# Fetching a collection's weights from peers

*Design note, 2026-09-28. Not scheduled; nothing here is built. Seeding
rule, creation from LocalAI's gallery and where the origin lives:
operator, 2026-09-28.*

**The gap.** A peer that takes up a new collection cannot run its encoder
until someone copies the weights into `/models` by hand. The model pool fetches
nothing, by design (`model-pool.md` §10). The spec already says who should
fetch them instead — the node, from other peers — but nothing implements
it, and three formats it needs are not written down yet (§4).

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

**Who does it:** Louise's worker, never the model pool. The model pool keeps reading a
read-only directory and never learns where a file came from.

**Steps**
1. **Trigger.** A collection is held, or `louise model fetch
   <collection | weights digest>` is run, and the weights named by its
   `space.encoder` (and query encoder) are not in the store.
2. **Find holders.** Look up the DHT key, then ask each address that
   answers for the weights' manifest (§4a).
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
7. **Seed**, by the rule below.

**What a node seeds** (operator, 2026-09-28)
- **What.** Every model named by a collection the node keeps: its
  `space.encoder` and, in an asymmetric space, its query encoder.
- **One per digest.** The DHT key is the weights digest, so ten
  collections in one space mean one model seeded once. A peer that keeps
  a collection is therefore a source for its model, and a newcomer finds
  it by the digest, not by the collection.
- **Not a drag member.** The model is never part of any collection's
  files (04 §4.3). It lives in the encoder store, and is served from
  there.
- **When to stop.** When the node stops keeping the last collection that
  names a model, it stops announcing it. The file may then be removed,
  unless a model-pool entry still names that path.
- **Never seeded.** Weights the registry marks as not redistributable.
  Newcomers fetch those from the origin.

**Where the model pool sees them**
- The store root is bind-mounted into the model pool read-only, once, at
  `/models/store`. That replaces today's one bind per model
  (`deploy/truenas-rage.yaml`).
- An entry names `/models/store/<digest hex>/…`.
- A missing file is `absent`, exactly as today.

**Rules the code must keep** (CLAUDE.md)
- *Deferral, not failure.* No holder answering, or the budget exhausted,
  defers the fetch. Neither writes an error against the collection.
- *SIGTERM.* The unit is a file: the current piece finishes, resume data
  is saved, and the process exits 130.
- *Disk.* The sizes are known before the first byte moves (§4a). A fetch
  that would not fit is refused, and the reason is shown.
- *Bounded waits.* Every DHT lookup and peer message has a timeout.
- *Keyed on physical identity.* Staging and the store are keyed by
  digest, never by a model's name.

## 3. At creation: a model from LocalAI's gallery (operator, 2026-09-28)

A creator may pick the encoder from LocalAI's model gallery
(`gallery/index.yaml`). The gallery is used once, to find and fetch the
first copy, and never trusted:

1. **Fetch.** Louise downloads through its own fetcher, within the
   budget and into staging, never through LocalAI's installer.
   - An entry with `files:` gives a `uri` and a `sha256` per file:
     typically a GGUF on Hugging Face.
   - An entry without them, as for the Python backends (rerankers,
     transformers), gives only a model name. Louise then fetches the
     Hugging Face repository at a fixed revision.
2. **Hash.** Louise hashes exactly what was fetched, by the §9.3 rule
   (the file, or the directory map).
3. **Pin.** That digest goes into `space.encoder.weights`. The engine
   goes into the engine object, with its `options` (09 §9.3) taken from the entry's
   backend and options.
4. **Reference vectors.** `hold collection create` then embeds the
   calibration set with those same bytes.
5. **Record the origin.** The entry's `uri`, or the repository and
   revision, is written into the platform's list of encoders (§4d)
   against that digest. It is never written into the Codex: a URL can
   die without changing what the collection is.

After that the gallery plays no part. Peers seed the model among
themselves (§2). If a gallery entry changes or disappears, nothing
breaks, because the digest is the only key.

For a single-file model, the gallery's `sha256` is the same hash as the
Codex's weights digest. So a gallery entry can also serve as an origin
for any collection whose digest it lists, found by lookup, and wrong
bytes still fail the digest check.

**Or the creator's own model** (operator, 2026-09-28). The gallery is one
way to get the first copy, not the only one. A creator may name weights
they already hold: a file or a directory. From creation on, nothing
distinguishes the two:

1. **Hash.** Louise hashes the weights by the §9.3 rule.
2. **Pin.** The digest goes into the Codex.
3. **Reference vectors.** They are embedded from those same bytes.

What differs is only where the first copy comes from:
- **Shareable, open weights: allowed.** The creator's node is the first
  seeder, and every peer that keeps the collection seeds the model after
  it (§2). The list of encoders records them as redistributable with no
  origin. Until a second peer holds them, the collection depends on the
  creator's node being reachable: the single point of failure 04 §4.3
  warns about, gone once others seed.
- **Weights that may not be passed on: refused at creation.** A stranger
  could not obtain them, so no one but the creator could check or extend
  the collection (02 §2.4).

## 4. What the spec does not say yet — decisions needed

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
   found no file or code for it. Per weights digest it needs:
   - whether the weights may be redistributed;
   - the origin the first copy came from (§3): a gallery `uri`, or a
     repository and revision.

   A node fetches from the origin only when no peer holds the weights,
   or when they may not be redistributed.

e. **Gated licences.** Some weights require accepting terms before
   download. Whether that uses the same recorded acknowledgement as a
   yellow or red source (invariant 9) is the operator's decision.

## 5. Out of scope

- **Engine images.** They travel the same way under their image digest
  (04 §4.3). The mechanism would be the same, but the model pool's own image is
  built and pinned, never fetched at run time.
- **Drafting the model pool entry** from the Codex. That is a separate note.

## 6. Done when (tests)

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
- The model pool, with the store mounted, loads an entry that names the promoted
  path. Before promotion, the entry is `absent`.
- Seeding:
  - Two held collections in one space announce one model.
  - Dropping the last collection that names a model stops its
    announcement.
- Creation from a fixture gallery entry:
  - With `files:`, the Codex pins the fetched file's digest.
  - Without them, the Codex pins the directory digest of the fixed
    revision.
  - Either way, the origin is written to the list of encoders and not to
    the Codex.
- Creation from the creator's own weights:
  - Shareable weights pin their digest, are recorded with no origin, and
    are announced by the creator's node.
  - Weights marked not redistributable are refused, and the refusal
    names 02 §2.4.
