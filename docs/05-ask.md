# 5. Asking

*(vocabulary amended 2026-10-08: SHARD, parts)*

A search runs over what a peer holds. It does not fetch, embed or
verify, except T4 (§3.5), which re-embeds what a search cites,
asynchronously; it reads indexes that Hold built and returns records
that Hold checked, and it says what it knows about each.

## 5.1 Two indexes, one answer

Each collection has a **text index** — one segment per record part,
exact terms with language-aware stemming — and a **vector index**: the
centres and bucket labels of §2.4, probed rather than scanned. A query
goes to both; the two ranked lists are fused by reciprocal rank, and
the fused list is what the reader sees. How many buckets the vector
leg probes is set on the node (07 §7.8); probing changes recall, never
what a returned result is.

**The catalogue is a third index and a leg of its own** (*amended
2026-10-20*, query campaign, docs/notes/query-campaign.md). A record's
title, creators and subjects are indexed apart from its parts (the
catalogue), and its ranked records go to the fusion as a third leg — in the
catalogue's order, each carried by its chunk the query's words match best,
else its first — one vote per rank, as the other two. Until then a title
reached a book only through the book's own text: "War and Peace" was not
in the top 100 though the book is held, and anthologies quoting a title
won. Made first a variant of the text leg, its rank 4 sat behind seventeen
language groups' ranks 1–3 when the query went to every group as typed
(fused rank 113). A result's `legs` (§5.3) names `catalog` beside `text`
and `vector`.

**Names, for a collection whose records have them** (*amended 2026-10-24*,
operator: wikidata, "names only"). Its plugin gives each record's names —
every label and alias, every language — and its popularity (wikidata: the
wiki pages linking it). The text leg then has one more list, from the
reader's own words: the records one of whose names IS a word sequence of
the query, the longer name first ("Monaco di Baviera" before "Monaco"),
then the more popular; a search limited to languages matches names in
those languages only. Names are index-only: they enter no record, no
section and no vector, and a peer rebuilds them from the records'
`extra`. A multilingual (`mul`) record's sections are indexed each in its
own language (*amended 2026-10-24*), and a search limited to languages
keeps such a record in every leg when one of its sections is in an asked
language.

**A leg's list is the same on every peer with the same members**
(*amended 2026-10-20*). Equal scores order by the record's uid bytes, then
the chunk's place in its record, the earliest first; a group of equal
scores that straddles a list's cut is read whole (bounded) before the list
is cut. An index returns equal scores in the order it was built in, which
differs between peers.

**What a search reads.** For each collection, a search reads the
newest snapshot whose members the peer holds for the slices it holds.
A part re-issued under that snapshot is read in its newest version
only — the superseded file stays on disk (02 §2.2) and is out of both
indexes — and a record listed in the snapshot's `tombstoned` is
excluded, as promise 7 says. An older held snapshot is served and
checked, and searched only when the caller asks for it by date. Of the
collections a peer holds in one lineage (§2.7), the parts a daughter
inherits by hash are the same bytes as the mother's: a part held under
both is read once, keyed by its `records_root`, so a record never
appears twice in one list. Such a result names the lineage and the
collection whose snapshot first listed the part.

**As of a date.** A collection whose records change is a record of what
was true when — for law it is the point, and *multivigenza* is the
whole question: what did article 5 say on 12 March 2019, not what does
it say now. That answer is already on disk, because a snapshot is a
frozen state and a superseded part is kept under its old snapshot's
name (02 §2.2). `--as-of <date>` names it: the search reads, per
collection, the newest snapshot of the chain whose cutoff is at or
before that date, instead of the newest held, and every result says
which snapshot answered. A peer that does not hold that snapshot's
members for a slice says so and names them rather than answering from a
later one — a wrong date is worse than no answer here. Without
`--as-of` a search reads the newest held snapshot, as above.

**A versioned collection answers a date by validity, not by the chain**
(*amended 2026-10-12*, docs/notes/normattiva-multivigenza-review.md §3).
A collection whose Codex `profile` declares a validity axis (09 §9.3)
holds every version of every Work as its own record — the law, one
record per act at each consolidation — and the chain cannot answer its
question: a snapshot holds the state at its cutoffs only, and 6.7 % of
the Italian archive's versions (37 % in the codes) last less than a
month, so a regular chain never holds them; what did change since the
as-of snapshot is exactly what the question is about. For such a
collection `--as-of D` selects, in the NEWEST held snapshot, the records
whose validity covers D: the record carries the START of its validity
(the origin's own statement), the END is derived locally as the next
version's start (02 §2.3: a record carries nothing computable from it)
and kept in the index beside the text and the vectors, never in a
record; versions of one Work are contiguous, so exactly one is admitted
per Work and no two versions ever meet in a leg (invariant 4). Without
`--as-of` such a collection shows one record per Work, its latest
version that has text — so a repealed act is shown in the last text it
had, labelled (§5.5). The result says which version it is (§5.3,
`validity`). The snapshot chain keeps its own meaning for it — when the
origin's state was held — and is not what a date asks of it.

**The fusion unit is the record.** Each leg — the text index, the
vector index — ranks chunks; chunks of every layer compete as chunks,
with no per-layer normalisation. A record's rank in a leg is the rank
of its best chunk there, counted among the leg's distinct records — a
leg is ONE ranked list of records, so it casts one vote per rank
(*amended 2026-10-09*: the text leg's variants, §5.2, merge into that
one list before fusion; the code had fused one list per language group,
and chunks rather than records). The leg contributes its top records to
the depth the request needs: the larger of `limit` and `rerank_depth`
(§5.4), and never fewer than 20 — so 50 by default, at most 300.
That depth is in RECORDS (*amended 2026-10-10*): a list asks its index
for chunks until they hold that many records — the vector leg starting
at twice the depth, a text variant at the depth, each doubling while
the list is short and the index has more — and never past eight times
the depth in chunks. Asking for 50 chunks had given the vector leg a
median 43 records on gutenberg, and changed the text leg's top 50 on 51
of 118 measured queries.
Fusion is reciprocal-rank fusion with `k = 60`: a record's score is the
sum over the legs that ranked it of `1 / (60 + rank)`, rank counted
from 1; records are ordered by score descending, ties by the bytes of
`(collection_id, uid)` ascending. The result carries the record's
best chunk — the one that ranked highest in the leg that ranked the
record highest — or, where the reranker ordered the list, the chunk
that scored the record there (§5.6, *amended 2026-10-10*). Two peers holding the same members return the same
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
default that question is relevance to the query text: `question` is the
literal `"relevance"`, every collection's legs carry it, and in this
version every collection's legs carry nothing else — so there is one
list, and the rule below costs nothing until there is not. A collection
may contribute a leg that answers another question — the genome
collections' by-sequence leg answers *homology*, their annotation text
answers *function* — and legs answering different questions are never
fused: each question is its own list, presented beside the others and
named. Where a non-default leg is DECLARED, and how two collections
establish that their legs answer the same question, is the search-axes
amendment; nothing in this version declares one, and an implementation
that writes `"relevance"` and fuses within it is complete.

Fusing a homology ranking with a functional ranking yields a
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
`pmid:38112034`, `eli:it:id/2003/11/15/003G0333` (the by-id form, the
one that is unique — docs/notes/eli-legal-plugins.md rule 1). An
identifier that is itself a URI carries its scheme as its namespace and
is written verbatim, never prefixed twice:
`urn:nir:stato:legge:2003-10-31;306`, namespace `urn`.
Where the origin's identifier is not qualified on its own, the
namespace's registration says how it is qualified: a genomic region
carries its assembly, `region:grch38:13:32316461-32316527`. A plugin
writes only registered ids into a record's `keys` (§6.1), and a
namespace is registered before any record uses it, because `keys` are
bytes of the record and therefore of its `content_hash`.

**A filter restricts; nothing boosts** (one exception, measured: the
catalogue's first record keeps the first place after the reranker, §5.6). Two things restrict the legs,
and only when the reader gives them: an identifier (a filter on `keys`),
and a value for one of the collection's declared facets (§5.7) — `subjects` is `Neoplasms/genetics`, `creators`
is `Austen, Jane`, `date` is within a range. Both narrow which records
the legs may return, and the free-text query then ranks within that
narrowing; neither adds a list of its own, and neither moves a score.
A filter that matches nothing returns nothing, which is an answer: the
value is not in what this peer holds, and §5.7 says what is. A boost
would hide that, and would make two peers rank one query differently
unless its weight were pinned in the Codex, so there is none (the
catalogue's first place, §5.6, is a fixed position, not a weight, and is
the same on every peer).

**An identifier the query names pins its facts and restricts nothing**
(*amended 2026-10-25*, operator). It is resolved and its facts are pinned
beside the lists (§5.4); the legs run as they would have, and no rank
moves. A reader who wants only the records tagged with an identifier gives
it as a filter, `keys` = `hgnc:1101`: every leg is then restricted to the
records tagged with it, and the free-text query ranks within that
restriction. It is bounded and refused when too broad, as a facet value
is (§5.7). A collection with no record tagged with it admits nothing
under it. Restricting by default was measured, and it lost. On TREC
Precision Medicine 2017–18 (80 topics, NIST's judgements), NDCG@10 was
0.3143 unrestricted, 0.3002 restricted to the records tagged with any
named identifier, and 0.2983 with all of them, each about 2 s slower: a
relevant record the tagger missed was hidden (docs/notes/query-campaign.md).
An identifier that resolves to no reference row and no record is returned
as *unresolved*; it never produces an empty fact.

**Translation is an expand step, off by default.** When it is on, the
peer translates the query into each collection's MAIN languages and,
when the request asks for every language, into every language its text
indexes hold; it runs the text leg once per variant; the record's rank
in the leg is its best rank across the variants. The main languages
(*amended 2026-10-10*) are the collection's one-language text indexes,
taken largest first by the documents each holds, until they hold the
node's declared share of the collection's documents (`[ask]
translate_share`, 07 §7.8; 0.90 unless set). The peer counts them from
its indexes, never by reading the corpus per search. A mixed index (the
catch-all, CJK) counts toward the share and is searched as before:
CJK with a variant per language it holds, the catch-all as typed. The
other one-language indexes are not searched by the text leg, unless the
query is written in their language (below); the vector leg still covers
them. A query limited to some languages (below) is translated into
those, main or not. Measured offline on the captured gutenberg legs
(`docs/notes/one-language-per-work.md`, "Do we need query translation?"):
at 0.90 the main languages are en, fr, fi, de (92 % of the books), and
against translating into all nineteen, fused before the reranker, 451
descriptive queries went MRR 0.231 → 0.245, R@50 0.605 → 0.630 (74
better, 33 worse), and 39 title queries R@10 0.769 → 0.821, R@50
0.897 → 0.949. An index searched with the query as typed in place of
a translation lost in every arrangement measured. The response names
the main languages used (`main_languages`). A variant is a query, not a leg: a collection
indexed in five languages casts no more votes than one indexed in one.
Concretely (*amended 2026-10-09*): the leg orders its records by their
best rank across the variants, and a record's rank in the leg is its
position in that order — sixteen variants each ranking a different
record first give the leg ranks 1 to 16, not sixteen ranks 1. Equal
best ranks go first to a rank the query AS TYPED reached (the variant
that is the reader's own words), then to a record written in the
language the query is in, then by uid bytes; these order ties and never
move a record past a better rank. Measured on 451 gutenberg queries
(`docs/notes/one-language-per-work.md`): the work in the fused top 10
for 14.4 % of them with one list per language group, 39.7 % this way.
The peer translates with its translation model (`TRANSLATE_URL`, 07
§7.8) where it has one, and with its chat model where it has not; the
response lists the translations used and which model made them. It is
turned on per request and by nothing else: `translate: true` on
`louise_search` (§5.4), `--translate` on `ask search` (07 §7.3); every
language is `all_languages: true` / `--all-languages`, or `lang:all` in
the query.

**A query may limit the languages of what it returns.** `lang:de` (or
`lang:de,fr`) in the query, or an instruction the peer reads in it ("only
texts in German"), restricts every leg to records written in those
languages. It is a filter like any other: it narrows, never boosts, and
the response names it. The same reading names the language the query is
written in, which a work's one hit is shown in (§5.3); a peer that reads
it with a chat model turns the model's thinking off for the call and
bounds it by its own timeout, and a reading that fails is said
(`status: unavailable`) and asked again next time, never cached
(*amended 2026-10-09*: the node's chat model thought through its whole
budget and answered nothing, on every query measured). Where the reading
is unavailable or names no language, the peer guesses it without a
model (*amended 2026-10-10*): a script only one held language writes
(Greek, Cyrillic, kana, Hangul, Han) names it; otherwise the query's
frequent function words and letters only some languages write are
counted against short word lists the platform ships, among the
languages the collection's text indexes hold, and a clear winner is the
query's language — no clear winner, no language. The response says who
named it (`query_lang_by`: `reading` · `guess`). The language the
query is written in is never translated into: its index is searched
with the query as typed, the reader's own words (*amended 2026-10-10*:
the translation model "translated" 292 of 472 measured queries into
their own language, rewriting them, and that rewrite was what their
language's index saw; fused before the reranker, 451 descriptive
queries MRR 0.219 → 0.231, R@10 0.404 → 0.421). When nobody names the
language, the plan is as before; searching the query as typed in every
index as well was measured and lost (MRR 0.212 → 0.187, 49 better, 79
worse). The guess is
deterministic and only does what the reading does: it orders ties and
picks the edition shown, and never restricts. Measured on 490 labelled
queries: 86.9 % right, 0.8 % wrong, the rest (bare titles, mostly)
unnamed; the work shown in the query's language for 0.79 of found
queries, against 0.57 with no language and 0.83 with the reading.

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
| `collection_id` | the Codex hash of the collection whose snapshot first listed the record's part |
| `lineage_id` | the lineage (§2.7); what a catalogue shows as the collection |
| `snapshot_id` | the snapshot the record was read from |
| `uid` | the record — `<plugin>:<native_id>`; the record's identity everywhere a result travels; a native id is display only |
| `chunk_id` | the record's best chunk (§5.1); in a reranked list, the chunk that scored the record (§5.6, *amended 2026-10-10*) |
| `layer` | the layer that cut the chunk, derived from the Codex (02 §2.3) |
| `rank` | the record's position in the list as presented |
| `legs` | `{text, vector, catalog}` — the record's rank in each leg that ranked it, `null` in a leg that did not |
| `trust_level` | `unverified` · `checked` · `verified` · `flagged` (§3.6) |
| `certification` | `{certified_by, anchor_state}` — the certifier's key and `anchored` · `stale` · `mismatch` (§3.7); `null` for an open collection |
| `verified_on_use` | `local` · `imported` · `verified` · `failed`: this peer produced the vector; produced elsewhere and not yet re-embedded here; re-embedded and within tolerance; re-embedded and outside it. A chunk whose producer is unknown is `imported`, never `local` |
| `producer_key` | the key that signed the chunk's vector — this peer's own key for `local` |
| `text` or `origin_url` | the chunk's text; for a red-class collection the origin's page and never the text |
| `figures` | the plates whose `[Illustration…]` markers fall inside this chunk, in order: `{hash, media_type, caption, state}` — NEVER the bytes. `null` where the collection carries no figures at all, `[]` where this chunk has none. `state` is `held` · `not-held` · `not-carried`, below |
| `annotations` | the claims an annotations collection held here makes about this record (02 §2.10; *amended 2026-10-09*), `[]` where none does. Each entry is `{collection_id, snapshot_id, claim, type, relation, role, record: {uid, title, lang, held}, subject_lang, object_lang, method, confidence, contributor, state, trust_level}`. `record` is the record at the claim's other end, as the target holds it, and `role` is this record's end, `subject` or `object`. *Amended 2026-10-20*: a `mention` entry carries `chunk_id`, `start`, `end` and `surface`, so the reader places it in the chunk it was read from and highlights it; a `relation` is attached at either entity's record; a `community` is a fact beside each member (02 §2.10, 09 §9.5). `state` is `current`, `stale` or `unknown`, below |
| `editions` | present only on a work's one hit (below; *amended 2026-10-09*): the work's other records, `{uid, title, lang, held, rank, chunk_id}` — `rank` and `chunk_id` where the record was in the list before the grouping, `null` where it was not |
| `freshness` | present only on request: `current` · `newer-at-source` · `unreachable` · `unavailable` — the last when the plugin has no per-record current form for this record (one read from a static archive, a copy built before records carried a content hash), said rather than guessed (*amended 2026-10-07*) |
| `reranked` | whether the list's order is the reranker's (§5.6) or the fused order |

**Figures are placed, never ranked.** Nothing embeds a picture, so a
figure is never scored, never fused and never a leg: it is a fact about
the chunk that was returned, of the same kind as a reference source's
row (§5.1) — looked up, not retrieved. What makes a figure findable is
its CAPTION, which is text, lives in `sections` like any other text and
is inside `text` above whenever the chunk carrying it matches.

Which plates belong to a chunk is decided by the markers in that chunk:
09 §9.5 makes the Nth `[Illustration…]` marker of a record its
`figures[N]`, so a chunk carrying markers 3 and 4 carries those two
entries and no others.

**The three states exist so that a reader is never left guessing which
kind of absence it is facing.** `held` — this peer has the bytes.
`not-held` — the collection carries this plate and this peer does not
have the member yet, so asking again later works. `not-carried` — the
origin has an image here that the collection does not admit (09 §9.5's
`carried: false`), so asking again NEVER works and the reader should
stop looking. `figures: null` is the fourth and commonest case: this
collection has no plates at all, and a caption in the text is all there
has ever been.

The distinction is not pedantry. A record whose text says
`[Illustration: A TABLEAU OF THE DAY.]` announces a picture; a reader
told nothing more will go hunting for something that may not exist
anywhere.

**The bytes come separately, and never in this object.** A result
carries the hash; `GET /api/ask/figure` returns the image itself
(07 §7.4), and a client renders it beside the chunk that cited it. Two
consequences worth stating: a search result stays small however
illustrated the book is, and a TEXT MODEL NEVER SEES THE PICTURE — what
reaches its context is the caption, already in `text`. The plate is for
the person reading the answer, not for the model composing it, unless a
multimodal client fetches it and passes it on.

**Annotations are placed, never ranked** (*amended 2026-10-09*). An
annotations collection's claim, for example "this Latin book and that
English one are the same work" (02 §2.10), is a fact about a returned
record, of the kind a reference source's row and a figure are. It sits
beside the result and never enters a list: it is never scored, never fused
and never a leg (invariant 4): no score, rank or reranker input depends
on it. Every way a peer shows results carries them — the Ask page beside
each hit ("also: …, matched by …") as the API does (*amended
2026-10-10*: the page had shown `editions` and never the claims).

**One hit per work** (*amended 2026-10-09*). After the reranker and
before the cut to `limit`, a list shows each work once: the records that
a held, usable annotations collection's `same-work` or `translation-of`
claims join (transitively) are one work, which keeps the position of its
best-ranked record. The record shown is the best-ranked of the work's
records in the reader's language — the language asked for results in
(the Ask page's "show results in"), else the language the query is
written in (§5.2), else English — or, when none of the work's records in
those languages is in the list, its best-ranked record; the others are
listed under it as `editions`, so nothing is dropped. A record no claim
names is shown as it ranked. Keeping one language per work instead lost
the work the reader wanted on 57–83 of 451 measured queries; grouping
was better on 50 and worse on none (`docs/notes/one-language-per-work.md`).
Nothing asks Wikidata or any other source at search time: the groups are
the claims the peer holds.

Which claims apply:

- **Collection.** The claim belongs to an annotations collection that this
  peer holds and may use, and whose `annotates.collection_id` is the
  result's collection.
- **Snapshot.** The result was read from a target snapshot at or after
  `annotates.from_snapshot`.

The state of a claim:

- `current` while both records' content hashes equal the ones the claim was
  checked against;
- `stale` when one of them differs. A stale claim is still attached,
  labelled;
- `unknown` where this peer holds no hash to compare.

An annotations copy in quarantine, or one whose licence is not
acknowledged, attaches nothing. It is named under `withheld` as any copy
is.

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
collection, as parts withheld and why — never a silent drop. A copy in
quarantine (§3.6) — not yet tested on this peer — enters no list, in
either leg, and is named the same way: `shards` is `"all"` and the
reason is the test or the acknowledgement it waits for. The key
`shards`, like a traversal's `needs.shard` (§5.4), keeps the old word:
bytes already written keep their names.

A copy can also be searched with ONE LEG MISSING: its encoder's run
failed the gate, or an answer named a run this peer has not tested yet
(03 §3.3). The text leg goes on, the vector leg is not used, and the
response says so under `legs_withheld` — the collection, the leg, the
reason — beside `withheld`, which names only what was left out whole. A
result list from such a copy is a text-leg list, and a reader has to be
able to tell.

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
fetched text is never stored. Like translation it is turned on per
request: `freshness: true` on `louise_search` (§5.4), `--freshness` on
`ask search`. Neither belongs in `platform.toml`. Both spend the
caller's latency and the peer's budget, and a node-wide switch would
make one question cost differently on two peers with no way for the
caller to say no.

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
louise_search(query, collections?, annotations?, space?, filters?, limit?,
              rerank_depth?, as_of?, freshness?, translate?, all_languages?)
    → {lists: [{space_id, question, hits: [<result, §5.3>]}],   -- question is "relevance" (§5.1)
       facts: [<fact, §5.3>], unresolved: [<canonical id>],
       withheld: [{collection_id, shards, reason}],
       legs_withheld: [{collection_id, leg, reason}],     -- leg is "vector"; the copy IS searched, on its text leg (§5.3)
       translations: [...]}   -- shards: a list, or "all" for a copy in quarantine (§5.3)
    filters: {keys?: <canonical id> | {not: <canonical id>},      -- §5.2, amended 2026-10-25
              <facet field>: <value> | {not: <value>}}
louise_lookup(id)
    → {facts: [<fact, §5.3>], unresolved: [<canonical id>]}
louise_traverse(id, relation?, depth?, resolve_in?)   -- resolve_in: the collections an edge into another collection is resolved against (§5.5; added 2026-10-07)
    → {edges: [{from, to, type, snapshot, collection_id,
                resolved, needs: {collection_id, shard} | null}]}
```

`collections` names collections by id, prefix or local name (07 §7.7)
and defaults to every collection the peer holds and may use.
`annotations` (*amended 2026-10-19*) names, by local name, the
annotations collections consulted — absent, every one held; `[]`, none.
They are never legs (invariant 4): the choice decides whether a
collection's work claims group a work's editions into one hit (§5.3)
and whether its claims come beside the hits. The Ask page offers each
held one as a chip and remembers the choice in the browser. `space`
restricts the search to the collections in one space; `limit` is the
number of results per list, 10 by default and at most 100.
`rerank_depth` is how many of each list the reranker orders (§5.6), 50
by default and at most 300; a leg goes at least that deep (§5.1), so
the reranker has that many candidates. `louise_lookup` takes no `as_of`: `--as-of` selects among
snapshots (§5.1) and a reference source has none — it is node-local
tables synced from the origin (06 §6.5) — so every fact names its
`ref_version` instead. It takes one canonical id and answers from the
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
relevant part, the answer says so and names the part, and fetching it
is a separate decision. Nothing is resolved across collections without
being asked, and nothing is fetched on a search's behalf.

The member is `relations/<slice>/NNNNN.jsonl.zst`, one part beside
each record part of the same slice and number, holding the edges whose
`from` record is in that part; it is listed in the snapshot with its
own root like any member (02 §2.2) and re-issued with its part. Each
line is one edge, `{from, to, type, snapshot}`: `from` is a uid of this
collection; `to` is a uid or a canonical id; `type` is a relation type
the plugin registers, prefixed by the plugin (`eli:cites`, `eli:amends`,
`eli:repeals`); `snapshot` is the snapshot in which the edge was first
listed. The direction is from → to, as the source states it; the peer
indexes edges in both directions locally, so "the laws that modify
decree 81/2008" is the inbound `eli:amends` edges of that decree. The
bytes are 09 §9.14.

**Links the source does not state are annotations** (*amended
2026-10-09*). A link that someone other than the origin finds — a matcher,
Wikidata, a person — is not a relation of the target collection, however
sure it is. Two books being the same work is an example. Such links are
claims, and they live in an **annotations collection** (02 §2.10), whose
source IS the annotator.

Its relations member carries each claim as an edge `subject → object`
typed `annotations:<type>`, for example `annotations:same-work` or
`annotations:translation-of`. Both ends are uids of the collection it
annotates, so `from` there is a record of the TARGET, not of the
annotations collection; its part is the part holding the claim. This
section's rule holds unchanged: the edges are explicit in their
collection's source. *Amended 2026-10-20*: with several targets (02
§2.10) a `mention`'s edge runs from the chunk's record to the entity, a
`relation`'s from the subject entity's record to the object entity, and a
community's members point at the community claim (09 §9.14); an entity
end is resolved against the entity target held here and stays a canonical
id where it is not held.

`louise_traverse` follows them like any edge and resolves them against the
target held here: holding the annotations collection is asking for it. They
are also placed beside search results (§5.3). Nothing in the target
collection changes.

Naming the part an unresolved id lives in requires holding the target
collection's Codex and a snapshot: the target's plugin maps a canonical
id in a namespace it owns to the record's slice and uid, and the part
is the one whose id range in that snapshot contains the uid. When the
peer holds neither, the answer names the collection asked for and
leaves the id unresolved.

**Repeal is metadata, not a tombstone.** A repealed act still governs
the facts that predate its repeal; it stays in its part, indexed and
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

It runs after fusion, over the top `rerank_depth` of **one** ranked
list — 50 unless the request says otherwise (§5.4) — and re-orders
those; the rest of the list keeps the fused order below them. The
depth is the caller's, like `limit`: the reranker is the asking peer's
own and so is the time it spends, and the latency budget below still
bounds it. It never runs over facts, and never across lists — each list of
§5.1 is reranked on its own, so two spaces or two questions are never
put on one score scale. A candidate is the RECORD (*amended
2026-10-10*): its two best matched chunks — its best chunk (§5.1), then
the one placed highest in any list — each sent whole, never truncated,
under one line naming the record, its title and authors (at most 300
characters); the record takes the better of the two scores, and the
result carries that chunk. The reranker had read the leg's one best
chunk, bare, and a byline or a heading of the right words beat the
passage that was the answer: "Hamlet Shakespeare" put eleven plays
first on the same 22 characters, "by William Shakespeare". Measured
live on gutenberg (`docs/notes/one-language-per-work.md`): 39 title
queries MRR 0.342 → 0.660, R@10 0.718 → 0.872 (26 better, 0 worse); 40
descriptive ones MRR 0.121 → 0.223, R@10 0.375 → 0.425. Three chunks
scored no better than two. The latency budget grows with the texts sent
as it grows with the depth. The reranker's modality must be
the collection's (`modality` in the Codex, §2.1): a text cross-encoder
scoring DNA is a category error, refused at boot, not a quality knob.

**Each candidate is judged against the question in its own language**
(*amended 2026-10-23*). Where the search translated the question (§5.2,
*expand*), a candidate written in one of those languages is scored
against that translation; any other against the question as typed. It is
one question in several languages, not two questions. A cross-language
pair scores far below a same-language one on a multilingual cross-encoder,
so judging every candidate against the typed question demoted every
record not in its language: on gutenberg "la balena bianca e l'ossessione
del capitano" scored Moby-Dick's English passage −5.298, last of five,
and +1.204, first, against its own English translation (the node,
2026-10-23); the operator saw "results returned only in the language of
the query". No translation is asked for here: the variants are the ones
the language plan made.

**The catalogue's first record keeps the first place** (*amended
2026-10-21*, docs/notes/query-campaign.md run 4). After the reranker's
order, the record the catalogue leg (§5.1) ranks first moves to position
1, and nothing else moves. It is the one exception to §5.2's "nothing
boosts" and to fusion by reciprocal rank alone, kept because it measures
best (operator, 2026-10-25: "The final goal is the quality of the
information retrieval"; CLAUDE.md invariant 4). The reranker judges passages: a biography's
passage that names "Moby Dick" beat the novel's own pages, and once the
instructed vector leg brought books about a work among the candidates,
titles fell from MRR 0.738 before the reranker to 0.578 after it.
Measured post-rerank on the API's path with a bit-exact reranker, 40
labelled titles:
- the catalogue's first record first: MRR **0.782**, R@1 0.700, R@10 0.975;
- the reranker's order fused with the fused order by reciprocal rank: 0.720,
  0.575, 1.000;
- the reranker's order alone: 0.578, 0.425, 0.925.
A query whose catalogue leg ranks nothing, every descriptive query
measured, keeps the reranker's order.

**Each candidate is cut to the reranker's window** (*amended 2026-10-21*).
The collection file's `[reranker] window` (07 §7.8; 2,048 tokens unless it
says otherwise, 0 for none) is the longest candidate the reranker reads, in
its own tokens. A longer candidate is counted by the model's tokenizer and
cut in proportion until it fits, so the model's context is the window and
not the longest chunk the corpus holds. Measured on live searches, title
and chunk together: gutenberg's median 878 tokens, p99 1,170, longest
3,335; wikipedia's p99 584. At 2,048, 99 % of candidates are read whole,
where a context sized for the longest needed four times the memory.

If the reranker is unreachable, refuses, or exceeds the node's latency
budget (07 §7.8) — set for the default depth of 50, and given to a
deeper list in proportion, so 300 candidates have six times as long —
the fused order stands and the list is returned with
`reranked: false`; a list the reranker ordered carries `reranked:
true`. A peer that holds no text for a candidate — vectors without
records — cannot rerank it and returns the fused order the same way.
The fallback is always visible in the result, never silent, so two
answers to one query that differ in order also say why: every fallback
names its reason under `rerank_withheld` — over the budget, busy with
other work for the whole budget, refused with its status, unreachable,
an answer that could not be read, an answer for only some candidates,
or a load past its bound (below) (*amended 2026-10-10*: 4 of 980 live
fallbacks had said nothing). A peer that runs no reranker has no
fallback to explain.

**The budget is the rerank's, never the model's load** (*amended
2026-10-09*). A reranker whose runner reports it as not loaded is
loaded first, by a minimal request bounded by the model's load bound
(07 §7.8, `[models] load_timeout_s`), and the latency budget starts
after it; the same holds for the query encoder, the translation model
and the chat model, each under its own timeout. A load that outlasts
its bound leaves the fused order with `reranked: false` and, beside the
lists, `rerank_withheld: [{collection, reason}]` with the reason "model
loading took longer than N s"; a query encoder's does the same to the
vector leg, in `legs_withheld`. A runner that cannot say what it holds
is asked as before, and its load counts against the budget.

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

**Every cost of the listing is bounded, and a bound reached is said,
never a partial count passed off as the count** (*amended 2026-10-07*,
after the first pass over 33 million records took the worker down). A
field with more than 200 000 distinct values is not counted on that
peer and the listing says why; a tree whose full paths pass that bound
is counted to depth 1 — PubMed's descriptors without their qualifiers —
and the listing says `depth: 1`. The index a filter reads (§5.2) holds
only values narrow enough for a filter to use, at most 20 000 records
each, and no more than 20 million rows per field; a broader value is
refused by its count, a deeper one than the depth counted is refused
naming the value to filter by, and the listing says per field whether
it is `filterable` here.

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
records at each snapshot and live in `index/` beside the text index of
the parts held (07 §7.8); they are not a member: they travel in no
SHARD, carry no root and need no check, because anyone holding the
records can rebuild them, and anyone who cannot hold the records cannot
search them either.
