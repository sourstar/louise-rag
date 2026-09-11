# 1. What Louise is

## The problem

Retrieval-augmented generation needs two things per corpus: the text,
parsed and chunked, and the embeddings of those chunks in some model's
vector space. The text is cheap to obtain and expensive to prepare; the
embeddings are expensive outright — hundreds of GPU-days for Wikipedia,
weeks for PubMed — and they are only useful to people who chose the same
model with the same settings.

Every public embedding corpus so far has been a one-shot dump: computed
once by a sponsor, published, and left to decay as the source moved on.
Nobody else could extend it, because nothing defined what "the same
collection, next month" would even mean, and nothing let a stranger's
vectors be trusted.

Louise is the missing maintenance layer. It defines a collection
precisely enough that anyone can continue it, verify it, and share the
work of embedding it — and it does so without a server, an account, or
anyone in charge.

## What a collection is

A **collection** is a corpus turned into what a RAG system searches:

- the **records** — the parsed text of every document, sliced by period
  or language, in shards;
- the **vectors** — one embedding per chunk, in one model's space, plus
  the labels that make them searchable without loading everything;
- the **Codex** — the exact recipe that produced both: which origin,
  which parser and chunker, which encoder build and settings, what
  licence, what update cadence. The Codex is small, public and
  immutable; its hash *is* the collection's identity.

Two peers holding the same Codex hold the same collection, however far
apart they started. Two peers who compute next month's records from the
same Codex produce the same bytes. That property — the recipe is the
identity, and the recipe is deterministic where it can be — is what the
rest of this documentation builds on.

## Three things you do with a collection

| surface | what it covers | needs another peer? |
|---|---|---|
| **Hold** | everything about keeping your own copy complete, current and checked: create a collection from a source, fetch and build records, embed, verify | no |
| **Share** | everything that involves other peers: serve what you hold, fetch what they hold, take a part of the embedding work, publish the results of your checks | yes |
| **Ask** | search over what you hold, from a chat interface or a tool call | no |

Everything Share does is something Hold does, plus a transport. A peer
that never connects to anyone can still create, maintain and search a
compliant collection on its own — and can rejoin the network later
holding exactly what everyone else holds.

## What Louise promises

1. **Only useful data travels.** Parsed text, figures, vectors, derived
   indexes; never markup, never anything executable, and never an origin
   file — with two exceptions, each carried because the bytes ARE the
   content rather than the packaging around it, and each optional to
   fetch and to serve.

   The first is a certified collection's, because for a sealed law the
   origin file IS the evidence: `origin-<slice>` carries the signed
   originals for whoever needs legal standing, and is never what a record
   is built from (03 §3.7, 09 §9.16).

   The second is a **figure** — a plate, a diagram, a drawing the origin
   carries beside the text. A figure is not parsed and not derived; it
   travels as the origin's own bytes, in `figures-<slice>`, named from
   the record by hash (09 §9.5). It is admitted for the same reason the
   text is: it is what the document says. "Never anything executable"
   binds it hardest of all, because a figure is the one thing here a
   viewer RENDERS — so the admitted types are a closed list, SVG is not
   on it, and the type is checked against the bytes rather than believed
   (08 §8.7).
2. **A peer takes the part it wants** — one language, one period, with
   or without vectors — and knows from the Codex and the snapshot what
   it is getting before the first byte.
3. **Embedding is shared work.** Whoever holds the records may embed a
   part of them; the results carry the producer's signature and are
   checked before they are used. The producer is whoever published the
   bundle, which is who vouches for its bytes rather than necessarily
   who computed them: vectors converted from a pre-embedded archive that
   signs nothing carry the converting peer's signature, and the archive
   is credited in the Codex, where a licence's attribution belongs
   (02 §2.7, 03 §3.5).
4. **Updates are deltas.** Each snapshot names exactly which
   records changed; re-embedding is the changed chunks and nothing else.
   The unit of change is the record — a record has changed when the
   hash of its cleaned text has — and the unit of re-embedding is the
   chunk: a changed record's chunks are recut, every recut chunk whose
   text the record already held keeps that text's vector wherever it now
   sits, and only genuinely new text is embedded (02 §2.2, 06 §6.3).
5. **Trust is reproduction, not votes.** Every artefact can be
   recomputed from the origin with the platform's open code; one honest
   reproduction outweighs any number of assurances.
6. **No node is in charge.** Once one honest peer holds a collection, it
   can keep it available and up to date if everyone else disappears.
7. **Nothing published is deleted.** A superseded collection stays
   available and verifiable; catalogues mark it, nothing removes it. A
   tombstoned record is no exception: its text stays in the closed shard
   of the snapshot that carried it, which remains shareable and
   verifiable as that snapshot, and from the snapshot that tombstones it
   the record is excluded from search and from embedding (02 §2.2).
8. **Nothing proceeds with a known defect. "Fix it after" is never, ever
   an option.** Not for one more run, not for the batch already queued,
   not for the deploy that is otherwise ready, not for the hour it would
   take to do it properly. When a defect is found, everything stops and
   it is fixed first — including work in flight and work unrelated to
   it.

   Promise 7 is why. Nothing published is deleted, so a wrong artefact
   is never recalled, only superseded, and every peer that adopted it in
   the meantime still holds it. Promise 5 is what makes it dangerous
   rather than merely useless: trust here is reproduction, so the
   artefact that matters is not the one that crashes — it is the one
   that passes every check and is wrong. A collection whose vectors were
   cut by a window its own `collection_id` does not describe is exactly
   that: both halves internally consistent, both computed honestly,
   nothing anywhere detecting the disagreement, and the error only
   visible to someone who re-derives from the origin. That artefact is
   indistinguishable from a correct one at every surface a peer can
   check, which is precisely why it must never be produced at all.

   A peer that is the only holder of a collection has no margin
   whatsoever — no second peer to re-derive from, and no later version
   that un-publishes the first. But the rule is not a property of being
   alone. It is a property of publishing into a network that cannot
   forget.

   This binds whoever is doing the work, human or otherwise. Offering
   the choice — proceed now, repair afterwards — is itself the error,
   and it is not the operator's to weigh: by the time the artefact
   exists the decision has already been made for everyone who fetches
   it.

## Vocabulary

Louise uses plain words and uses them consistently. These are the ones
that carry meaning; the documents that follow introduce the rest where
they are needed.

| term | meaning |
|---|---|
| collection | a plugin's body of material turned into records and vectors — the Gutenberg collection, the PubMed collection |
| Codex | the immutable recipe of a collection; its hash is the collection's identity |
| lineage | the chain of Codexes linked by `continues`, `derived_from` and `replaces` (02 §2.7); the `lineage_id` is the root Codex's id along `continues`, and it is what a catalogue shows as *the collection* — one entry with a history. Nothing but the hash chain makes two collections kin, in particular not their titles |
| daughter / sister / corrective sister | the three relations between a new Codex and the one it comes from (02 §2.7): a daughter continues the collection under a new toolchain in the same space; a sister inherits the corpus whole — the same records, so a peer holding one re-fetches nothing to take the other — and changes only what is built on it: a new space, a new schedule, or both, sharing the mother's vectors exactly when the space is unchanged; a corrective sister replaces a collection whose defect has been reproduced. The collection a related Codex comes from is its **mother**; one created from a source has none, and stands at the root of its own lineage. *Sister* is also the general word, as 08 uses it, for any new collection standing beside an existing one, related or not |
| snapshot | the state of a collection's records at one cutoff of its update cadence; content-addressed |
| drag | a collection on disk, and the name of the format it is written in — DRAG, *distributed RAG*: the Codex, the snapshots, the records, the vectors and everything else a peer shares, in one directory (02 §2.6, 09) |
| shard | a run of one slice's records, sorted by native id and cut at the Codex's `shard_records`; closed once full, re-issued under the same number while it is still filling or when a record in it changes. The unit of transfer, of embedding and of verification (02 §2.2–2.3) |
| member | what a peer fetches or shares as one unit (02 §2.6): one slice of one kind — records, vectors, buckets, index, relations, figures — or a whole-collection file such as the calibration member or the licence terms; 09 §9.16 gives the closed list of kinds. The snapshot names the size and root of each member it lists, before the first byte moves; a bundle and an `index-<slice>` are named by their own rules instead (04 §4.3) |
| holdings | what your copy actually has — which slices, which kinds |
| space | a function from cleaned text to vectors: the encoder build and its settings, the layers and the cleaning version, named by `space_id` (02 §2.4). One collection, one space; collections in one space are searched as one ranked list (05 §5.1), and vectors from different spaces are never compared |
| bundle (signed block) | a batch of vectors on a collection's own records, signed by the key that produced them; the unit in which embedding work travels — 03 §3.5, 09 §9.7; 08 §8.6 says *signed block* |
| attestation | the signed result of one completed check — collection, snapshot, check, seed and sampled shards, result, the verifier's key (03 §3.6, 09 §9.8); it travels as an event, and trust levels are counted from attestations, never from assurances |
| pointer record | the record a red-class collection ships in place of text: the same line with `sections` empty and `extra.origin_url` set (02 §2.3, 04 §4.4); the text is fetched from the origin by every peer that holds the collection, and vectors, pointers and index travel while the text never does |
| provenance | which origin, which snapshot, which encoder, which producer |
| work parameter | the embedding work per unit time a collection declares as its holding level; declared by the creator, verified from below by reproduction |
| actively-held / dormant | the two observed states of a collection on a peer's catalogue, defined in 08 §8.6: actively-held while some key's signed work for it — a bundle where there is embedding to do, an attestation where there is not (04 §4.7) — keeps arriving at the rate the work parameter states, dormant when they have stopped — still findable and fetchable, no longer listed as active. Each peer computes the state for itself by the rule of 04 §4.7; nobody declares it |
| check | verification of any kind — of bytes against hashes, of records against the origin, of vectors against the encoder |
| share / hold | instead of seed / replicate |
| certified collection | a collection whose records only a named, anchored key may publish |
| open collection | every other collection |

Louise is software and is referred to as *it*.
