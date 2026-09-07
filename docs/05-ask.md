# 5. Asking

A search runs over what a peer holds. It does not fetch, embed or
verify, except T4 (§3.5), which re-embeds what a search cites,
asynchronously; it reads indexes that Hold built and returns records
that Hold checked, and it says what it knows about each.

## 5.1 Two indexes, one answer

Each collection has a **text index** — one segment per record shard,
exact terms with language-aware stemming — and a **vector index**: the
centres and bucket labels of §2.4, probed rather than scanned. A query
goes to both; the two ranked lists are fused by reciprocal rank, and
the fused list is what the reader sees. How many buckets the vector
leg probes is set on the node (07 §7.8); probing changes recall, never
what a returned result is.

**What a search reads.** For each collection, a search reads the
newest snapshot whose members the peer holds for the slices it holds.
A shard re-issued under that snapshot is read in its newest version
only — the superseded file stays on disk (02 §2.2) and is out of both
indexes — and a record listed in the snapshot's `tombstoned` is
excluded, as promise 7 says. An older held snapshot is served and
checked, and searched only when the caller asks for it by date.

**As of a date.** A collection whose records change is a record of what
was true when — for law it is the point, and *multivigenza* is the
whole question: what did article 5 say on 12 March 2019, not what does
it say now. That answer is already on disk, because a snapshot is a
frozen state and a superseded shard is kept under its old snapshot's
name (02 §2.2). `--as-of <date>` names it: the search reads, per
collection, the newest snapshot of the chain whose cutoff is at or
before that date, instead of the newest held, and every result says
which snapshot answered. A peer that does not hold that snapshot's
members for a slice says so and names them rather than answering from a
later one — a wrong date is worse than no answer here. Without
`--as-of` a search reads the newest held snapshot, as above. Of the collections a peer holds in one lineage
(§2.7), the shards a daughter inherits by hash are the same bytes as
the mother's: a shard held under both is read once, keyed by its
`records_root`, so a record never appears twice in one list. Such a
result names the lineage and the collection whose snapshot first
listed the shard.

**The fusion unit is the record.** Each leg — the text index, the
vector index — ranks chunks; chunks of every layer compete as chunks,
with no per-layer normalisation. A record's rank in a leg is the rank
of its best chunk there, and the leg contributes its top 100 records.
Fusion is reciprocal-rank fusion with `k = 60`: a record's score is the
sum over the legs that ranked it of `1 / (60 + rank)`, rank counted
from 1; records are ordered by score descending, ties by the bytes of
`(collection_id, uid)` ascending. The result carries the record's
best chunk — the one that ranked highest in the leg that ranked the
record highest. Two peers holding the same members return the same
fused list for the same query.

**One ranked list per space.** Collections in the same space (same
`space_id`) are searched as one list: their vectors are comparable, so
a legal query can run over the Gazzetta and the Official Journal
together and rank them against each other. Within a space, fusion is
one pass: every leg of every collection in the space enters the same
sum, and a collection's text index contributes to its space's list.
Collections in different spaces are searched separately and presented
separately — the sister of §2.7 is a different list, never merged with
its mother's. A mixed list would have no single question behind it and
no single tolerance behind its vectors, and the fact that rank fusion
would tolerate one does not make it one. A search over N collections in
M spaces therefore returns M lists, each holding up to the requested
number of results, ordered as the collections were named; when none
were named, by the bytes of `space_id`.

**One list per question.** Every leg answers one question, and by
default that question is relevance to the query text. A collection may
contribute a leg that answers another question — the genome
collections' by-sequence leg answers *homology*, their annotation text
answers *function* — and legs answering different questions are never
fused: each question is its own list, presented beside the others and
named. Fusing a homology ranking with a functional ranking yields a
list that answers neither.

Some questions are not searches. "The frequency of rs80359351", "the
laws that modify decree 81/2008", "everything by this author" want an
exact key, not a semantic neighbour, and vectorising the answer makes it
worse. These come from **reference sources** — tables keyed by canonical
id, contributed by plugins like any corpus — and enter the answer as
pinned facts, never ranked, never fused. Ranked lists are fused; facts
are placed, above every list. Mixing the two is a type error the
platform refuses.

## 5.2 The query plan

```
resolve → expand → plan → execute → fuse → rerank
```

*Resolve* finds identifiers in the query — a gene symbol, a norm number,
a PMID — and maps them to canonical ids, species- or
jurisdiction-qualified from the start. *Expand* follows declared links
between reference tables (variant → gene), one hop from each resolved
id; it never closes transitively, and a further hop is asked for
through the traversal tool (§5.4). *Plan* decides which collections get
the free-text query with which filters, and which reference sources
answer by key. *Execute* runs them; *fuse* merges the ranked lists and
places the facts; *rerank* is §5.6, and changes nothing when the node
runs no reranker. A query with no identifiers degenerates to the
execute and fuse steps and costs nothing extra.

**A canonical id** is `<namespace>:<id>`: a lower-case namespace,
registered by the reference source that owns it (§6.5), and the
origin's own identifier verbatim — `hgnc:11998`, `rs:80359351`,
`pmid:38112034`, `eli:it/stato/decreto.legislativo/2008/04/09/81`.
Where the origin's identifier is not qualified on its own, the
namespace's registration says how it is qualified: a genomic region
carries its assembly, `region:grch38:13:32316461-32316527`. A plugin
writes only registered ids into a record's `keys` (§6.1), and a
namespace is registered before any record uses it, because `keys` are
bytes of the record and therefore of its `content_hash`.

**A filter restricts; nothing boosts.** Two things restrict the legs: a
resolved identifier, and a value given for one of the collection's
declared facets (§5.7) — `subjects` is `Neoplasms/genetics`, `creators`
is `Austen, Jane`, `date` is within a range. Both narrow which records
the legs may return, and the free-text query then ranks within that
narrowing; neither adds a list of its own, and neither moves a score.
A filter that matches nothing returns nothing, which is an answer: the
value is not in what this peer holds, and §5.7 says what is. A boost
would hide that, and would make two peers rank one query differently
unless its weight were pinned in the Codex, so there is none.

In a collection whose
records carry `keys`, a resolved identifier restricts every leg to the
records whose `keys` contain it; the free-text query then ranks within
that restriction. In a collection whose records carry no `keys`, the
identifier restricts nothing and the free-text query runs as it would
have. An identifier that resolves to no reference row and no record is
returned as *unresolved*; it never produces an empty fact.

**Translation is an expand step, off by default.** When it is on, the
peer translates the query into each language its text indexes hold,
using its chat model, and runs the text leg once per variant; the
record's rank in the leg is its best rank across the variants. A
variant is a query, not a leg: a collection indexed in five languages
casts no more votes than one indexed in one. The response lists the
translations used.

## 5.3 What a result carries

Every result names its collection, its snapshot, its record and its
chunk, and shows:

- the collection's **trust level** (§3.6), and for a certified
  collection its **certification** beside it — two axes, never one;
- whether the chunk has been **verified on use** (§3.5, T4). The chunks
  a search cites — every chunk in a list it returns to its caller —
  are re-embedded once, asynchronously, by the worker, and marked; a
  chunk that fails is a proof of forgery against its producer, and is
  still returned, marked;
- for a red-class collection (§4.4), the origin's page in place of the
  text.

The fields, with their values:

| field | value |
|---|---|
| `collection_id` | the Codex hash of the collection whose snapshot first listed the record's shard |
| `lineage_id` | the lineage (§2.7); what a catalogue shows as the collection |
| `snapshot_id` | the snapshot the record was read from |
| `uid` | the record — `<plugin>:<native_id>`; the record's identity everywhere a result travels; a native id is display only |
| `chunk_id` | the record's best chunk (§5.1) |
| `layer` | the layer that cut the chunk |
| `rank` | the record's position in the list as presented |
| `legs` | `{text, vector}` — the record's rank in each leg that ranked it, `null` in a leg that did not |
| `trust_level` | `unverified` · `checked` · `verified` · `flagged` (§3.6) |
| `certification` | `{certified_by, anchor_state}` — the certifier's key and `anchored` · `stale` · `mismatch` (§3.7); `null` for an open collection |
| `verified_on_use` | `local` · `imported` · `verified` · `failed`: this peer produced the vector; produced elsewhere and not yet re-embedded here; re-embedded and within tolerance; re-embedded and outside it. A chunk whose producer is unknown is `imported`, never `local` |
| `producer_key` | the key that signed the chunk's vector — this peer's own key for `local` |
| `text` or `origin_url` | the chunk's text; for a red-class collection the origin's page and never the text |
| `freshness` | present only on request: `current` · `newer-at-source` · `unreachable` |
| `reranked` | whether the list's order is the reranker's (§5.6) or the fused order |

The chunk's text is what the peer holds. For a red-class collection
the peer's own copy of the text — fetched from the origin, never from a
peer (§4.4) — is used locally for fusion, for the reranker and for T4;
what leaves the peer, on the command line, over HTTP or through a tool,
carries `origin_url`. A peer holding vectors and the text index without
the text still searches both legs; it cannot re-embed what it cites,
so its results stay `imported`, and it carries `origin_url` like any
red-class result.

Search continues over the certified subset of a collection marked
*integrity violated* (§3.8), and over what the licence gate admits
(§4.4); whatever either withholds is named in the response, per
collection, as shards withheld and why — never a silent drop.

Facts are placed beside the lists, never in them. Every fact names the
reference source and its `ref_version` (§6.5), the table, the canonical
id, the row's columns, and — when *expand* reached it — the link it was
reached through. An identifier that resolves to no row produces no
fact; the response lists it as unresolved.

On request, a result can be checked for **freshness**: the peer asks
the origin for the current version of that record and compares its
content hash. A mismatch marks the result *newer at the source* and
shows the current text beside the snapshot's, as a fact — never
re-embedded, never fed into the index. For a green collection and,
after the acknowledgement of §4.4, a yellow one, the current text is
shown; for a red collection only the hash is compared and the origin's
page is linked. The check covers at most the top five results of each
list per request, is off by default, and draws from the peer's budget
(§4.3) — additionally capped by the origin's etiquette (§6.2). The
fetched text is never stored.

## 5.4 Tools, not a router

A chat interface decides whether a question is semantic, exact or a
traversal by having three tools and letting the model choose: semantic
search over one or more collections, exact lookup in a reference source,
and link traversal from an entity. The tools are exposed through the
Model Context Protocol so any client that speaks it can use a Louise
peer without knowing how a collection is laid out. There is no separate
classifier to train and keep current; the tool descriptions are the
routing.

The three tools, their parameters and what they return are part of the
platform image and change only with it:

```
louise_search(query, collections?, space?, limit?, as_of?)
    → {lists: [{space_id, question, hits: [<result, §5.3>]}],
       facts: [<fact, §5.3>], unresolved: [<canonical id>],
       withheld: [{collection_id, shards, reason}], translations: [...]}
louise_lookup(id, as_of?)
    → {facts: [<fact, §5.3>], unresolved: [<canonical id>]}
louise_traverse(id, relation?, depth?)
    → {edges: [{from, to, type, snapshot, collection_id,
                resolved, needs: {collection_id, shard} | null}]}
```

`collections` names collections by id, prefix or local name (07 §7.7)
and defaults to every collection the peer holds and may use; `space`
restricts the search to the collections in one space; `limit` is the
number of results per list, 10 by default and at most 100, the depth
of a leg. `louise_lookup` takes one canonical id and answers from the
reference source that owns its namespace. `louise_traverse` follows
the edges of §5.5 from a record's uid or a canonical id, in both
directions, restricted to one relation type when `relation` is given,
`depth` hops deep (1 by default). The command line's `ask search`
(07 §7.3) and the HTTP routes (07 §7.4) return the same objects.

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

The member is `relations/<slice>/NNNNN.jsonl.zst`, one shard beside
each record shard of the same slice and number, holding the edges whose
`from` record is in that shard; it is listed in the snapshot with its
own root like any member (02 §2.2) and re-issued with its shard. Each
line is one edge, `{from, to, type, snapshot}`: `from` is a uid of this
collection; `to` is a uid or a canonical id; `type` is a relation type
the plugin registers, prefixed by the plugin (`eli:cites`, `eli:amends`,
`eli:repeals`); `snapshot` is the snapshot in which the edge was first
listed. The direction is from → to, as the source states it; the peer
indexes edges in both directions locally, so "the laws that modify
decree 81/2008" is the inbound `eli:amends` edges of that decree. The
bytes are 09 §9.14.

Naming the shard an unresolved id lives in requires holding the target
collection's Codex and a snapshot: the target's plugin maps a canonical
id in a namespace it owns to the record's slice and uid, and the shard
is the one whose id range in that snapshot contains the uid. When the
peer holds neither, the answer names the collection asked for and
leaves the id unresolved.

**Repeal is metadata, not a tombstone.** A repealed act still governs
the facts that predate its repeal; it stays in its shard, indexed and
retrievable, carries `in_force: false` in the record's `extra` and its
`eli:repeals` edge from the repealing act, and is labelled in results.
Filtering by force is a query concern. A tombstone (02 §2.2) is
reserved for a record the origin withdrew — a PubMed deletion — and
never for a change in what a record means.

## 5.6 The reranker

A **reranker** is a query-time choice of the asking peer: optional,
node-local, configured on the node (07 §7.8) and never named by a
Codex. A Codex names no reranker, no host and no URL a peer would send
a query to; nothing a reranker produces is written into a member that
travels. A reranker may influence *which* records a peer embeds next
under on-demand embedding; it never influences *what* a vector or a
record is.

It runs after fusion, over the top 50 of **one** ranked list, and
re-orders those; the rest of the list keeps the fused order below
them. It never runs over facts, and never across lists — each list of
§5.1 is reranked on its own, so two spaces or two questions are never
put on one score scale. A candidate is sent whole: the text of the
record's best chunk, never truncated. The reranker's modality must be
the collection's (`modality` in the Codex, §2.1): a text cross-encoder
scoring DNA is a category error, refused at boot, not a quality knob.

If the reranker is unreachable, refuses, or exceeds the node's latency
budget (07 §7.8), the fused order stands and the list is returned with
`reranked: false`; a list the reranker ordered carries `reranked:
true`. A peer that holds no text for a candidate — vectors without
records — cannot rerank it and returns the fused order the same way.
The fallback is always visible in the result, never silent, so two
answers to one query that differ in order also say why.

## 5.7 Facets: the categories, and what is in them

A filter nobody can enumerate is close to useless — nobody guesses
`Neoplasms/genetics`, and for the Gazzetta Ufficiale the categories are
the map: *diritto dei trasporti*, *navigazione*, and the rest. So a
collection declares in its Codex which of its record fields are
**facets** (02 §2.1), and a peer can list the values it holds for each.

```
ask facets <collection>                          the fields, and how many values each has
ask facets <collection> subjects                 the values, with counts
ask facets <collection> subjects --under "Neoplasms"      one subtree
```

This is a listing, not a search: nothing is ranked, nothing is fused,
relevance never enters it, and so §5.1's two kinds of result are
untouched. What it returns is what this peer can actually filter on.

**A facet is a record field, and the tree is read out of the records.**
`kind: "flat"` is a set of values — creators, languages. `kind: "tree"`
splits each value on the declared `separator` and merges the paths:
PubMed's `"Neoplasms/genetics"` is the descriptor `Neoplasms` and its
qualifier `genetics`, so the tree has two levels, and a taxonomy with
five separators has five. `kind: "range"` is ordered rather than
enumerated — dates — and is listed as bounds, not as every value.
Building it is one pass over the metadata of the records held:

```
Neoplasms          26,443
├── genetics       12,431
├── therapy         8,902
└── diagnosis       5,110
```

**Counts are this peer's, and say so.** A peer holding one slice reports
that slice's counts, because that is what it can search. Two peers with
different holdings report different numbers for one collection, and
neither is wrong.

**A new category needs no new Codex.** When an origin adds a subject —
PubMed coining one next year — it arrives in the records of the next
snapshot like any other content, and the tree gains a node when the
index is rebuilt. Nothing is coordinated, no Codex changes, no
collection is superseded. That is why the Codex declares structure and
never values: had it listed the categories, one new subject would make
a new `collection_id` every month. A renamed term behaves the same way
— records the origin re-issues carry the new name, records it has not
re-issued keep the old, and the tree shows both until the origin
catches up, which is what the records actually say.

`authority` names the controlled vocabulary a field draws on, where
there is one — MeSH, ISO 639-1, Eurovoc — so a reader knows what the
strings are and an interface can link a term to its definition. It
carries no version: the vocabulary is the recipe and stays for the life
of the collection, while its edition is data and rides in the records,
so pinning an edition would make every annual release a new collection
for no gain.

**The index is local and derived.** Facet values are rebuilt from the
records at each snapshot, in `state/` beside the working text index
(07 §7.8), and they are not a member: they travel in no drag, carry no
root and need no check, because anyone holding the records can rebuild
them and anyone who cannot hold the records cannot search them either.
