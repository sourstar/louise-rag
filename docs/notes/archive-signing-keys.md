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

## Corrected 2026-09-08, after reading the documents properly

The paragraph that stood here recommended (b) on the ground that "the
archive id already does that job at the only place it is checked". That
was wrong, and the error is worth keeping visible because it is easy to
repeat.

**There are two doors, not one, and they hold different handles.** The
LOCAL door is `hold vectors import --archive ID`, where the importer has
an archive id and 06 §6.6 refuses an id that `credits` does not name.
The NETWORK door is a gossiped bundle, whose header carries
`producer_pubkey` and — by 09 §9.7's closed field list — no archive id
at all. 03 §3.5 guards that one: "A bundle whose `producer_pubkey` is a
registered archive's key is refused unless an entry in this collection's
`credits` names that key." The id cannot reach the network door. The key
is not a duplicate.

**But the rule never fires on the path the vectors actually take.**
09 §9.6 fixes it: the per-space vector files are "assembled from
accepted bundles (§9.7), never a member's bytes, never fetched as such —
vectors travel only as bundles (04 §4.3)". MedCPT's vectors therefore
reach a second peer as bundles signed by the CONVERTING PEER'S node key,
so `credits[].key` is compared against a producer key that is by
construction not an archive's. `app/bundles/genesis.py` already writes
`producer = f"genesis:{archive.archive_id}"`, with the comment "There is
no signing key: an archive is admitted by the same-function proof, not a
signature" \x{2014} a third answer, in the code, that neither document
describes.

**And the rule is not self-contained in the Codex.** Deciding "is this
producer a registered archive's key?" needs the platform's registry,
which every peer already holds. So `credits[].key` freezes a LOCAL and
CHANGEABLE fact into an IMMUTABLE identity, with no rotation path:
09 §9.2, "Ordinary keys do not rotate: a lost key is a new key".

**(b) has a trap that was not visible before.** Creating PubMed now with
`credits[].key: null` hashes that null into `collection_id` for ever.
When MedCPT is later published signed, it is a new registry entry, and
06 §6.6 forbids importing an archive `credits` does not name \x{2014} so
adopting it means re-creating the collection under a new id. Nullable
looks cheap and is not.

## The recommendation, revised: (c)

Take the key OUT of `credits`. The registry maps an archive id to a key
SET; 03 §3.5's refusal looks an arriving `producer_pubkey` up in the
registry to get an archive id, then checks `credits` by id. The same
refusal, the same strength, and the mutable half lives where mutable
facts belong \x{2014} keys rotate, and an archive may start signing, without
touching any collection's identity.

`credits` keeps `producer`, `license`, `attribution` and `coverage`.
Note that it is NOT a pure compliance record and cannot be reshaped as
one: 06 §6.3's `archive:<archive id>` layer rule reads
`credits[].coverage` to decide which layer a document falls in, and so
how it is chunked and what its vectors are. `coverage` survives any
version of this decision.

**The strongest argument against (c):** it moves a rule that 09 states
inside a hashed object out into platform code, where two peers on
different releases can disagree about which keys an archive uses. The
answer is that they can disagree today too \u2014 the registry is already
the second input to 03 §3.5's rule and 09 never specified it \u2014 so (c)
does not add the divergence, it stops pretending the Codex prevents it.

## Two adjacent things this turned up

- 01 promise 3 says "the results carry the producer's signature and are
  checked before they are used". Archive vectors converted locally carry
  the converter's signature, not the producer's. The promise is true by
  the letter and misleading in spirit; it deserves a clause.
- **Who partitions an archive's shards across peers is specified
  nowhere in 01\u201309.** 04 \u00a74.5's rendezvous covers record shards only.
  It decides whether NCBI is fetched 37 times network-wide or thousands
  of times, and it belongs beside this decision.
