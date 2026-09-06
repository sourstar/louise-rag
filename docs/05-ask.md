# 5. Asking

A search runs over what a peer holds. It does not fetch, embed or
verify; it reads indexes that Hold built and returns records that Hold
checked, and it says what it knows about each.

## 5.1 Two indexes, one answer

Each collection has a **text index** — one segment per record shard,
exact terms with language-aware stemming — and a **vector index**: the
centres and bucket labels of §2.4, probed rather than scanned. A query
goes to both; the two ranked lists are fused by reciprocal rank, and
the fused list is what the reader sees.

Collections in the same space (same `space_id`) are searched as one
list: their vectors are comparable, so a legal query can run over the
Gazzetta and the Official Journal together and rank them against each
other. Collections in different spaces are searched separately and
presented separately — the sister of §2.7 is a different list, never
merged with its parent's.

Some questions are not searches. "The frequency of rs80359351", "the
laws that modify decree 81/2008", "everything by this author" want an
exact key, not a semantic neighbour, and vectorising the answer makes it
worse. These come from **reference sources** — tables keyed by canonical
id, contributed by plugins like any corpus — and enter the answer as
pinned facts, never ranked, never fused. Ranked lists are fused; facts
are placed. Mixing the two is a type error the platform refuses.

## 5.2 The query plan

```
resolve → expand → plan → execute → fuse
```

*Resolve* finds identifiers in the query — a gene symbol, a norm number,
a PMID — and maps them to canonical ids, species- or
jurisdiction-qualified from the start. *Expand* follows declared links
between reference tables (variant → gene). *Plan* decides which
collections get the free-text query with which filters, and which
reference sources answer by key. *Execute* runs them; *fuse* merges the
ranked lists and places the facts. A query with no identifiers
degenerates to the first and last steps and costs nothing extra.

## 5.3 What a result carries

Every result names its collection, its snapshot, its record and its
chunk, and shows:

- the collection's **trust level** (§3.6), and for a certified
  collection its **certification** beside it — two axes, never one;
- whether the chunk has been **verified on use** (§3.5, T4). The chunks
  a search actually cites are re-embedded once, asynchronously, and
  marked; a chunk that fails is a proof of forgery against its producer;
- for a red-class collection (§4.4), the origin's page in place of the
  text.

On request, a result can be checked for **freshness**: the peer asks
the origin for the current version of that record and compares its
content hash. A mismatch marks the result *newer at the source* and
shows the current text beside the snapshot's, as a fact — never
re-embedded, never fed into the index. Rate-limited by the origin's own
API limits and off by default.

## 5.4 Tools, not a router

A chat interface decides whether a question is semantic, exact or a
traversal by having three tools and letting the model choose: semantic
search over one or more collections, exact lookup in a reference source,
and link traversal from an entity. The tools are exposed through the
Model Context Protocol so any client that speaks it can use a Louise
peer without knowing how a collection is laid out. There is no separate
classifier to train and keep current; the tool descriptions are the
routing.

## 5.5 Relations between records

Some corpora are graphs as much as texts. A statute modifies, cites and
repeals others; those relations are explicit in the source and should be
followed, not inferred from similarity. A collection may therefore
carry a **relations** member — edges between record ids, typed — that
the traversal tool follows. A relation may point outside the collection:
a national law citing a European one carries the European norm's
canonical id and leaves it unresolved. The asking peer says which
collection to resolve such ids against; if it does not hold the
relevant shard, the answer says so and names the shard, and fetching it
is a separate decision. Nothing is resolved across collections without
being asked, and nothing is fetched on a search's behalf.
