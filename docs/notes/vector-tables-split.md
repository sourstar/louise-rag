# One vector table per value of a search key (2026-11-01)

*Operator, 2026-11-01: "Different tables, make it a standard practice when
separated searches might be done." On Wikidata by family: recommended
(below), the default if the operator says nothing.*

## 1. Why

Wikipedia, after the stable-row-ids rewrite (2026-10-31, STATUS): a search
with no language filter takes 57 ms, the same search with `lang=en` 202 ms.
The time goes on the prefilter's row mask (~58 M English rows); a postfilter
is slower and changes the results (overlap 0.72–0.78). Lance cannot keep one
IVF index per value of a column, so the only way to search one language at
full speed is a table of its own (architecture measurement,
scheduling-log 2026-10-31).

## 2. The rule

A plugin may declare a **table key** (`slicing.TABLE_KEYS`): one of
its slice keys that a search can narrow by. Every collection of that plugin
keeps **one vector table per value of the key**. A plugin that declares none
keeps one table, as before.

When to declare one:
- searches narrow by it (`lang:` today; `family:` with this change);
- it has tens of values, not hundreds. A search with no filter queries every
  table. PubMed's `year` is left out: a date range would span tens of tables
  at once;
- the cost of the filtered search is measured first (§1 is wikipedia's).

| plugin | table key | values |
|---|---|---|
| wikipedia | `lang` | 11 held today |
| wikidata | `family` | 14 (9 embedded) |
| embedded, gutenberg, pubmed, normattiva, the rest | none | one table |

EU law is already one collection per language (operator, 2026-10-28). That is
the same separation, at the collection level.

## 3. What is on disk (09 §9.6, amended)

```
<lance_dir>/layout.json          {"format": 1, "table_key": "lang"}
<lance_dir>/lang=en/chunks.lance
<lance_dir>/lang=it/chunks.lance
…
```

- **The directories.** Each value is a directory named Hive-style (`key=value`,
  the value percent-encoded), holding an ordinary LanceDB table `chunks` with
  today's schema and today's storage options (stable row ids).
- **The switch.** `layout.json` turns the split on. Without it the collection
  has today's single `chunks.lance`. A collection whose plugin declares a key
  and that has no table yet is created split: its first write writes
  `layout.json`. One that already has a single table stays on it until
  `louise dev split-vectors` (§6) moves it.
- **The rest form only.** Bundles, attestations, digests, `space_id` and
  `collection_id` are unchanged. A peer that keeps one table and a peer that
  keeps eleven exchange the same bytes.
- **Invariant 1.** One collection is still one space. Every table is checked
  against the Codex at start, and a mismatch in any of them refuses the
  collection.

## 4. Routing a document

A document's table is its slice's value for the key. It is read from the
manifest row without parsing (`vector_parts.values_of_books`, through `slicing.slice_hint`):

- Wikipedia: the language in the native id. This is pure, with no read.
- Wikidata: the family byte the pack keeps for the entity. This needs one
  indexed manifest read per batch for the dump locator.

A slice key is stable for the record's life (slicing.py's rule), so a
document never moves between tables, and a chunk id lives in exactly one
table. A chunk id names its document (`{source}:{native_id}:…`), so every
read or delete by id routes too. A book the router cannot place (its
manifest row is gone) is looked up in every table by count, and deleted only
where it is found. A Lance delete that matches nothing still commits a
version (measured, pylance 9.0.1), so deletes are never fanned out blind.

## 5. What each operation does

| operation | single table | split |
|---|---|---|
| write (`upsert_book`, `write_pending`, `upsert_chunks`, `sync_columns`) | as before | each document to its table. A batch is one manifest lock and one commit per table touched; the documents are marked done after the last commit, in the same transaction (invariant 2) |
| delete, read by id or book | as before | routed by document |
| search | one query | the query is embedded once. One query runs per table the filter selects, in threads, and the results are merged by exact distance, then chunk id. The key's own filter selects tables and is never a row filter. Other filters apply inside each table |
| scan (carry, export, checks) | one table | the tables one after the other |
| index maintenance | one table | each table on its own: its own partitions (`partitions_for` its rows), deltas and compaction |
| stats | one table | summed |

**The merge is exact.** All the tables hold one space and one metric, and
the index's refine pass (`VECTOR_INDEX_REFINE`) returns exact distances. So
the top k of the union of each table's top k is the answer of one exact
search. Ties go to the chunk id: the same query on the same data gives the
same order (invariant 4 (5)).

## 6. Moving an existing table: `louise dev split-vectors <collection>`

This works like `dev rewrite-vectors` (app/vector_rewrite.py), with every
stage recorded before the next:

1. **copy.** The single table, checked out at the version the split began
   from, a group of fragments at a time. Each row goes to its value's
   staging table (`split.staging/<key>=<value>/`). After each group, the
   state records each staging table's Lance version. A run stopped between
   two commits of one group restores those versions on resume (Lance's own
   `restore`), so no row lands twice.
2. **verify.** Each table holds exactly the rows routed to it, the schemas
   match, and a sample of rows is compared byte for byte.
3. **index.** The book_id BTREE and the vector index on each table, before
   the swap.
4. **swap.** Under the vector write lock, refused if the source moved: the
   staging tables are moved in and `layout.json` is written last. The old
   table is set aside until `--drop-old`.

The collection must be paused. Nothing is re-embedded (RULE 1).

| | copy | indexes | temporary disk |
|---|---|---|---|
| wikipedia (146.4 M rows) | ~30–60 min (the rewrite copied at ~11 GiB/min) | 11 builds, ~5–7 h together (one build of the whole table: 6 h 31 min) | ~330 GiB until `--drop-old` |
| wikidata (embedding since 2026-10-31) | minutes today | small | small |

## 7. Costs, measured and estimated

- **One-language search:** about 202 → ~50 ms on wikipedia (to be measured
  after the move).
- **Unfiltered search, wikipedia:** about the same index work as today. The
  single 146 M-row table is capped at 4,096 partitions (`partitions_for`), so
  each of its partitions holds ~36 k rows. The language tables' partitions
  hold fewer, and 11 × 30 probes scan about as many PQ codes as 30 probes of
  the one table. What grows is 11 exact-distance passes against one, and 11
  queries' overhead. Latency should be about that of the largest table
  (English). To be measured.
- **Unfiltered search, wikidata (the 9 families):** ~1.5× the PQ codes
  scanned (sum over tables of 30 × rows/partitions) and 9 exact passes.
- **Maintenance:** one table per value to compact and refresh. Each is
  smaller, so each pass is shorter.

## 8. Standards

- **Hive-style partition directories** (`key=value`) for the layout.
- **List partitioning with partition pruning**, as PostgreSQL names it: the
  filter on the key picks the partitions, and the partitions are not
  filtered row by row.
- **Milvus's partition key** is the vector-database precedent: one key per
  collection, a search restricted by it reads only its partitions.

Lance (pylance 9.0.1) has no partitioned table. Lance Namespace
(`DirectoryNamespace`) organises tables in a hierarchy but routes no rows and
merges no searches. It is the standard alternative for the LAYOUT, and would
pay if the tables moved to object storage or a REST catalogue. Adopting it
would take the table opens through `connect_namespace`, nothing else.

## 9. Order of work

1. `app/vector_parts.py`: layout, routing, views (a part is the collection
   with its own `lance_dir`). Dispatch at the entry of every public function
   of `app/embeddings.py`, and the search fan-out and merge. Single-table
   collections are unchanged, and every existing test stays green.
2. `vector_index` and the worker's maintainer per table. Invariant 1's check
   per table. Stats summed.
3. Bundles (importer, genesis, export, verify-on-use), carry, the reader,
   spaces, `louise` commands, tools.
4. `dev split-vectors`.
5. `family:` in the query (wikidata), as `lang:` is.

Then on the node: wikidata (small now), then wikipedia (§6), each measured
before and after (`one_language_live_search`).

## 10. Wikidata by family: the recommendation

| choice | pros | cons |
|---|---|---|
| **one table per family** (recommended) | a `family:person` search at full speed. Growing or dropping a family's embedding adds or drops a table, with no deletes in a 43 M-row table. A peer's family fetch is one table. Cheapest now, while embedding has just started | every search without a family filter (all of them until `family:` exists) does ~1.5× the index work and 9 exact passes. Two tiny tables (property 14 k, scholarly 115 k) |
| one table (status quo) | the cheapest unfiltered search. Nothing to build | a family filter later pays the row mask (like `lang=en`'s 202 ms), or a rewrite of hours. Dropping a family means deletes in the big table |
