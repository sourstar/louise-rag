# Who signs a pre-embedded archive's bundles — open, and it blocks crediting

Found while implementing migration step 1 (the platform's archive
registry), 2026-09-08. It does not block the proof-of-concept import, and
it does not block anything that uses this node's own vectors. It blocks
one thing: naming a public archive in `credits`.

## The two documents say different things

06 §6.6 lists what the registry declares: "its id, coverage, producer,
the licence its vectors carry and the attribution that licence
requires". No key.

09 §9.3's `credits` row adds one: each entry is `{archive, producer,
key, license, attribution, coverage}`, "where `key` is the `ed25519:…`
its bundles are signed with, so an uncredited archive's bundle can be
refused (03 §3.5)".

Both are coherent on their own. Together they leave one question open:
where does that key come from?

## Why it is not obvious

`credits` is hashed into `collection_id` (02 §2.7). So `credits[].key`
must be the same on every peer that writes the same recipe, or two
honest creators produce two collections.

But the archives that exist are not signed:

- **MedCPT** (`medcpt-pubmed`) is a directory of `.npy` files on an NCBI
  FTP server. NCBI has no Louise key and has never heard of us.
- **Upstash Wikipedia** (`upstash-wikipedia-2024-06`) is a HuggingFace
  parquet dataset. Same.

The first peer to convert one into bundles signs them with **its own**
node key. That key is per-peer, so it cannot be the value hashed into
the identity — and if it were, every peer would compute a different
`collection_id` for the same recipe.

## The three ways out

**(a) The registry ships the key of a designated publisher.** Somebody
converts the archive once, publishes signed bundles, and the platform
ships that key in the registry — a curated fact, like the calibration
set. Every peer's registry says the same thing, `credits[].key` is
stable, and 09 §9.3's refusal rule works exactly as written. The cost is
that an archive cannot be credited until somebody has done that work and
the platform has shipped a release naming them. It also makes the
platform's maintainers a trust root for archives, which 08 §8.1 is
otherwise written to avoid — though only for *crediting*, never for
admission, which stays 03 §3.5's ladder.

**(b) `credits[].key` becomes nullable.** An archive is credited for its
licence and attribution — which is what 06 §6.6 says the registry is for
— and the refusal 09 §9.3 describes is dropped. An uncredited archive's
bundle is then refused by its `archive` id alone, which is what the
importer knows anyway: `vectors import --archive ID` names the archive,
and 06 §6.6 already refuses an id that is not in `credits`. The key adds
nothing that the id does not, unless a bundle can arrive from a peer
CLAIMING to be an archive — and it cannot, because a peer's bundle is a
peer's bundle, admitted on the ladder like any other.

**(c) The archive's key is whoever converted it, recorded per peer,
outside the Codex.** Then `credits` carries licence and attribution
only, and the coverage map records who produced what. This is (b) with
the bookkeeping named.

## What is implemented meanwhile

`GenesisArchive` carries `producer`, `license`, `attribution` and a
`coverage()` derived from its declared id ranges — everything 06 §6.6
lists. `key` is `None` for both existing archives, and
`expand_archives` REFUSES to credit an archive with no key, saying why.
So nothing is invented, nothing is silently wrong, and no collection can
be created that hashes a per-peer key into its identity.

The consequence today: `hold collection create --archives medcpt-pubmed`
exits 2. Importing MedCPT's vectors into a collection that does not
credit them is refused by 06 §6.6, so PubMed cannot yet draw on MedCPT
through the documented path. **This is the thing to decide before the
full PubMed import**, and it is independent of the figures question.

My reading, for what it is worth: **(b)**. The key's stated job is to
refuse an uncredited archive's bundle, and the archive *id* already does
that job at the only place it is checked. A key that exists to enforce
something the id enforces, at the cost of making the platform a trust
root and blocking every archive nobody has re-published, is paying a
real price for a duplicate.
