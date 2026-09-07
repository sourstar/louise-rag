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

1. **Only useful data travels.** Parsed text, vectors, derived indexes;
   never origin files, markup or anything executable.
2. **A peer takes the part it wants** — one language, one period, with
   or without vectors — and knows from the Codex and the snapshot what
   it is getting before the first byte.
3. **Embedding is shared work.** Whoever holds the records may embed a
   part of them; the results carry the producer's signature and are
   checked before they are used.
4. **Updates are deltas.** A monthly snapshot names exactly which
   records changed; re-embedding is the changed chunks and nothing else.
5. **Trust is reproduction, not votes.** Every artefact can be
   recomputed from the origin with the platform's open code; one honest
   reproduction outweighs any number of assurances.
6. **No node is in charge.** Once one honest peer holds a collection, it
   can keep it available and up to date if everyone else disappears.
7. **Nothing published is deleted.** A superseded collection stays
   available and verifiable; catalogues mark it, nothing removes it.

## Vocabulary

Louise uses plain words and uses them consistently. These are the ones
that carry meaning; the documents that follow introduce the rest where
they are needed.

| term | meaning |
|---|---|
| collection | a plugin's body of material turned into records and vectors — the Gutenberg collection, the PubMed collection |
| Codex | the immutable recipe of a collection; its hash is the collection's identity |
| snapshot | the state of a collection's records at one monthly cutoff; content-addressed |
| holdings | what your copy actually has — which slices, which kinds |
| provenance | which origin, which snapshot, which encoder, which producer |
| work parameter | the embedding work per unit time a collection declares as its holding level; declared by the creator, verified from below by reproduction |
| check | verification of any kind — of bytes against hashes, of records against the origin, of vectors against the encoder |
| share / hold | instead of seed / replicate |
| certified collection | a collection whose records only a named, anchored key may publish |
| open collection | every other collection |

Louise is software and is referred to as *it*.
