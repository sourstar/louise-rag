# One language per work? A measurement on gutenberg

*Measured 2026-10-09 on the node. Read-only: nothing on the node was written
(see "What ran"). No spec or product code changed. Scripts:
`tools/measure/one_language_*.py`; raw data in `tools/measure/out/onelang/`
(not committed).*

The operator's request: "use only one language per work and skip the other ones:
either always English, or always the original language, or always the language
of the query if available. Profile to find the best combination."

## Answer

**Do not skip the other editions. Group them when the list is shown.**

- **Every "keep one language" rule loses works the reader was looking for.**
  On the live search path, work-level MRR is 0.231 with no collapsing. Each rule
  is worse:
  - always English (A): 0.133
  - always the original (B): 0.132
  - query language, else English (C): 0.180
  - query language, else the original (D): 0.179

  Against no collapsing, each rule is worse on 57–83 of 451 queries and better
  on 4–7 (sign test p < 10⁻⁴). The edition that matches a query cannot be
  predicted from its language. A French query is often answered by the French
  translation of an English novel. A Greek query is sometimes answered by the
  English Thucydides.
- **What works is one hit per work (W).** Rank each work by its best edition,
  show it once, and show it in the query's language when the work has a book
  in it.
  - MRR rises from 0.231 to 0.236 (50 queries better, 0 worse).
  - The top 10 holds 10 distinct works instead of 7.35.
  - Where the work has a book in the query's language, the reader is shown that
    book 99% of the time (72% today).
  - On title queries ("Don Quixote Cervantes"), where editions do crowd the list:
    MRR 0.366 → 0.398 and R@10 0.615 → 0.692.
- **Fallback, when the work has no book in the query's language:** English (C)
  or the original (D). Under one hit per work they rank identically. As
  filters they tie too (C better on 11 queries, D on 7, p = 0.48). What
  differs is what is shown, on the 36 found queries whose language the work
  lacks:
  - C shows English 89% of the time;
  - D shows the original 42% of the time, and English 56% (where the
    original is not held);
  - today the reader gets English 50%, the original 22%, and a third
    language 28%.

  This is a choice of reading, not of retrieval. English is the safer
  default.
- **If one index language per work were wanted anyway** (to save embedding, say),
  C is the least bad. It beats A on 48 queries vs 22 (p = 0.003) and B on 49 vs
  25 (p = 0.007), and it still loses 22% of MRR to no collapsing.
- **Today's rule (`app/work_dedup.py`, "cur") also costs recall.** MRR falls
  0.231 → 0.207: 21 queries worse, 2 better. It drops a translation whenever the
  original is a candidate, and the translation is often the edition that
  matched.

**What actually crowds the list is not editions.** On the 451 descriptive
queries, the top-10 slots of the live list are:

- 73.5% distinct works;
- 25.3% another chunk of a book already shown;
- 0.8% another edition in another language;
- 0.3% another edition in the same language.

On the 39 title queries, editions take 11.5% of the slots and repeated chunks
32%. The rest of what pushes good results down is two defects in fusion, below.

## Two defects this measurement ran into (spec ≠ code)

1. **Each language group of the text index votes as its own leg.**
   - docs/05 §5.2 says a translated query is "a variant, not a leg: a
     collection indexed in five languages casts no more votes than one indexed
     in one".
   - `retrieval._search_one_collection` appends one list per text index group
     (`lists.append(hits)` inside the group loop). `_best_rank` is applied only
     *within* a mixed group.
   - gutenberg queries therefore enter RRF as 14–17 text lists (16 for most)
     plus 1 vector list. Each group's rank-1 hit, Hungarian and Romanian included, scores
     1/61 like the vector leg's rank 1.
   - The fused list before the reranker finds the work in the top 10 for 14.4%
     of queries. The legs fused as §5.2 says (one text leg, best rank across
     groups) find it for 39.7%. The vector leg alone finds it for 41.9%.
   - The reranker repairs part of the damage (live list: 33.0%), but it can only
     reorder the 50 candidates that the flood let through.
   - In the live top 10, 17.6% of the slots come from the vector leg alone,
     3.7% from both legs, and 78.7% from text groups alone: en 12.5%,
     fr 8.0%, de 7.5%, nl 6.1%, hu 5.7%, …
2. **The fusion unit is the chunk, not the record.**
   - docs/05 §5.1: "The fusion unit is the record … a record's rank in a leg is
     the rank of its best chunk."
   - `_merge_rrf` keys on `chunk_id`, so a book appears several times. That is
     25.3% of the top-10 slots (32% on title queries), and the Ask page shows
     each one (`_ask_results.html` iterates hits).

Side observation, in the same image and env as the web:
`llm.language_restriction` returned no parseable answer on all 451 queries.
The chat model returns an empty string for that prompt. So every plan is
`status: unavailable`, a restriction such as "solo testi in tedesco" is never
honoured, and no plan is ever cached.

These are reported, not fixed: the task was read-only.

## 1. Work groups, from what the origin states

- **Records.** All 79,562 gutenberg records were read from the SHARD with
  `app.pack.records.iter_records` (`docker exec … python -`, stdout only):
  `one_language_records.py`.
- **Wikipedia lines.** PG's RDF description (`extra.description`) carries
  "Wikipedia page about this book: <url>" on **11,142 books**, and an editions
  note ("Project Gutenberg has several editions of this eBook: #N …") on 68.
- **Wikidata items.** Each URL was mapped to its Wikidata item through the
  Wikipedia API (`prop=pageprops&ppprop=wikibase_item`, redirects followed,
  ≤ 50 titles a call, sequential, backoff on 429). 7,804 distinct titles gave
  7,745 items, and 11,091 books resolved. Script: `one_language_works.py
  wikidata`.
- **A work** is a Wikidata item, unioned with the books of an editions note.
  - A book naming several items is an anthology, not an edition (79 books).
    It is left alone, because chaining through it had joined *King Lear* to
    Grillparzer.
  - No similarity is used anywhere.
- **Original language**, in order:
  1. the source language of an LCSH translation heading ("Epic poetry, Greek --
     Translations into English" → el);
  2. else Wikidata's P407/P364 on the item, read by label: a variety maps to
     PG's code (Homeric Greek → el, Classical Latin → la), and among equally
     stated languages a held one wins (Kidnapped: English, not Scots);
  3. else unknown.

| | works | books |
|---|---:|---:|
| works with ≥ 2 books | 1,694 | 6,060 |
| … spanning ≥ 2 languages | **775** | **3,006** |
| … if grouped only by the identical URL (no Wikidata) | 434 | 1,581 |

- **Sizes.** 896 works of 2 books, 349 of 3, … up to Don Quixote with 61.
- **Language combinations.** en+fr 146, en+fi 129, de+en 69, en+fi+fr 33,
  en+nl 27, de+en+fi 23, el+en 21, en+it 16, en+es 16.
- **Original language of the cross-language works.**
  - Source: P407 for 582, LCSH for 173, unknown for 20.
  - Held in the collection: 607. Known but not held: 148 (Tolstoy, Ibsen, …).
  - Languages: en 250, fr 187, de 93, el 49, ru 34, la 24, no 21, es 16,
    it 16, …

| lang | books | Wikipedia line | in a work ≥ 2 | in a cross-language work |
|---|---:|---:|---:|---:|
| en | 63,146 | 7,556 (12.0%) | 4,083 (6.5%) | 1,473 (2.3%) |
| fr | 4,220 | 1,048 (24.8%) | 591 (14.0%) | 419 (9.9%) |
| fi | 3,690 | 541 (14.7%) | 404 (10.9%) | 376 (10.2%) |
| de | 2,470 | 589 (23.8%) | 248 (10.0%) | 216 (8.7%) |
| nl | 1,113 | 194 (17.4%) | 150 (13.5%) | 122 (11.0%) |
| it | 1,111 | 162 (14.6%) | 85 (7.7%) | 75 (6.8%) |
| es | 899 | 290 (32.3%) | 138 (15.4%) | 80 (8.9%) |
| hu | 654 | 96 (14.7%) | 64 (9.8%) | 37 (5.7%) |
| pt | 651 | 101 (15.5%) | 31 (4.8%) | 13 (2.0%) |
| zh | 436 | 152 (34.9%) | 66 (15.1%) | 8 (1.8%) |
| sv | 253 | 34 (13.4%) | 15 (5.9%) | 13 (5.1%) |
| el | 216 | 101 (46.8%) | 75 (34.7%) | 73 (33.8%) |
| eo | 124 | 62 (50.0%) | 36 (29.0%) | 36 (29.0%) |
| la | 108 | 43 (39.8%) | 29 (26.9%) | 22 (20.4%) |

This is about six times what Wikidata's P2034 gives
([wikidata-work-grouping.md](wikidata-work-grouping.md): 123 works, 38
cross-language). PG links far more books to a Wikipedia page than Wikidata
links to PG.

**Quality.** 30 random cross-language works (seeded,
`out/onelang/crosslang_sample.txt`) were checked by hand: **30/30 are one
work**, and the original language is right in 29 (*The Man of Destiny* is
unknown). A same-author, same-title probe on the 150 sampled works shows the
groups are incomplete: a second volume often lacks the Wikipedia line (*Hans of
Iceland* vol. 2, *Onnen kultapoika* 2/2). Grouping only ever removes books that
are in a group, so an ungrouped edition stays visible.

## 2. Query set

- **Works.** 150 works with books in ≥ 2 languages, drawn by a seeded
  stratified sample (`one_language_select.py`), so the choice was not the
  writer's:
  - 95 with the original held and not English: fr 22, de 18, el 12, it 10,
    es 10, la 6, nl 5, fi 4, others 8;
  - 30 with English as the original;
  - 25 with the original not held.
- **Queries.** 451 queries were written by hand from each work's subjects and
  MARC 520 summary (`one_language_queries.tsv`). They describe content and
  never use the bare title. Each work is asked:
  - in English;
  - in its original language (for English originals, in a held translation's
    language);
  - and in a third language: "the anger of Achilles and his quarrel with
    Agamemnon…", "l'ira di Achille…", "der Zorn des Achilles…".
- **By query language:** en 150, fr 72, de 68, it 41, es 30, fi 24, el 13,
  nl 11, la 9, sv 7, eo 6, no 5, hu 5, ru 3, pl 3, zh 2, tl 1, pt 1.
- **The relevant answer** is the work (any of its books). The *Inferno* and
  *Divine Comedy* works accept each other, and *Ali Baba* accepts the *Nights*.
- **Query language** is the label of the set. Louise has no query-language
  detector. The only signal it has is the Ask page's "show results in" picker
  (`web.RESULT_LANGUAGES`), so C and D would need one, or that picker.
- **Title supplement.** 39 bare title queries for the largest cross-language
  works (`one_language_titles.tsv`), to look at edition crowding.

## 3. What ran

**The real search path,** `retrieval.search(con, q, k=50, translate=True,
rerank_depth=50)`, as the Ask page calls it:

- the language plan with MADLAD query translation into 19 languages;
- the text index's language groups;
- the vector leg (vega-qwen3-embedding-4b);
- RRF;
- the bge reranker on the top 50.

k = 50 with depth 50 keeps the default page's legs and reranked order, so the
default top 10 is a prefix of this list.

**Where it ran.** A throwaway container (`one_language_run.sh`):

- the live web container's image, network namespace and env (tokens left out);
- **every mount read-only**: /data, the SHARDs, the config;
- one query at a time, ≈ 57 s each, ≈ 7.5 h in all.

**Changes in that process only** (`one_language_search.py`):

- The manifest is opened `?mode=ro`.
- Tantivy takes a lock file even to read, so each text index was read through
  a tmpfs view: symlinks, plus a copied meta.json.
- `work_dedup.prefer_originals` returns every hit. The baseline is "no
  collapsing", and what the real rule would have kept is recorded as "cur".
- Nothing is written: no T4 citation queue, no lazy-embed queue, no event log.
- The reference-source step is skipped. Gutenberg records carry no `keys`, so
  it cannot restrict anything there.
- The dispatch claim cannot be written. In consequence the encoder, shared with
  another measurement, failed intermittently. A query whose vector leg or
  translations were missing was retried (1 of 451 needed it). None was scored
  without both.

**Captured legs.** The legs were captured as they entered RRF, and recomputing
the fusion from them reproduces the search's fused order on **451/451**
queries. Other fusions are therefore computed exactly, with the skipped
editions taken out of every leg *before* fusion, as an index without them would
behave:

- **code**: today's RRF;
- **spec**: §5.1–5.2 as written: two legs, records, the language groups as
  variants of one text leg;
- **vector**: the vector leg alone.

These three are without the reranker. "live" is the reranked list the search
returned, with the skipped editions removed after the fact.

## 4. Strategies

A filter keeps, for each work with books in ≥ 2 languages, the books of one
language:

- **A**: English, else the original.
- **B**: the original, else English.
- **C**: the query's language, else English.
- **D**: the query's language, else the original.
- Neither held: the work's most frequent language.

Two strategies that group instead of filtering:

- **E-x** keeps each work at its best edition's rank and drops its other
  editions.
- **W-x** also drops later chunks of the same book: one hit per work.

E and W show rule x's edition. Their ranks do not depend on x.

## 5. Results (451 queries)

### Live list (reranked), work level

| strategy | MRR | R@1 | R@5 | R@10 | shown in query lang | … when the work has it | distinct works in top 10 |
|---|---:|---:|---:|---:|---:|---:|---:|
| no collapsing | 0.231 | 0.182 | 0.299 | 0.330 | 0.544 | 0.717 | 7.35 |
| cur (work_dedup today) | 0.207 | 0.162 | 0.273 | 0.299 | 0.541 | 0.730 | 7.42 |
| A English | 0.133 | 0.104 | 0.175 | 0.191 | 0.372 | 0.533 | 7.49 |
| B original | 0.132 | 0.102 | 0.171 | 0.197 | 0.483 | 0.662 | 7.47 |
| C query, else en | 0.180 | 0.149 | 0.226 | 0.244 | 0.764 | 0.988 | 7.45 |
| D query, else orig | 0.179 | 0.149 | 0.222 | 0.242 | 0.771 | 0.988 | 7.45 |
| **W-C one hit per work** | **0.236** | **0.182** | **0.313** | **0.335** | **0.755** | **0.991** | **10.00** |

"Shown in query lang" counts the queries whose work was found in the top 10.
On the 36 found queries whose language the work lacks, the edition shown is:

- W-C: English 89%;
- W-D: the original 42%, English 56%;
- no collapsing: English 50%, the original 22%, another language 28%.

W-A shows English on almost every query, but the query's language on only 28%.

### By relation of the query language to the work (MRR, live)

| relation | n | none | cur | A | B | C | D | W-C |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| query = English = original | 31 | 0.214 | 0.214 | 0.202 | 0.202 | 0.202 | 0.202 | 0.220 |
| query = English, original other | 119 | 0.177 | 0.168 | 0.134 | 0.091 | 0.125 | 0.134 | 0.181 |
| query = original (not English) | 117 | 0.285 | 0.250 | 0.154 | 0.222 | 0.222 | 0.221 | 0.288 |
| query = other language, held | 96 | 0.306 | 0.249 | 0.097 | 0.076 | 0.245 | 0.245 | 0.314 |
| query = other language, not held | 88 | 0.158 | 0.158 | 0.118 | 0.102 | 0.118 | 0.102 | 0.164 |

Even a query in the original language loses when only the original is kept:
0.285 → 0.222. A translation is sometimes the edition that matched.

### By query language (MRR, live)

| lang | n | none | cur | A | B | C | D | W-C |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| en | 150 | 0.184 | 0.177 | 0.148 | 0.114 | 0.141 | 0.148 | 0.189 |
| fr | 72 | 0.212 | 0.187 | 0.080 | 0.113 | 0.188 | 0.189 | 0.218 |
| de | 68 | 0.237 | 0.218 | 0.175 | 0.156 | 0.154 | 0.165 | 0.244 |
| it | 41 | 0.187 | 0.159 | 0.111 | 0.096 | 0.142 | 0.124 | 0.197 |
| es | 30 | 0.229 | 0.195 | 0.162 | 0.112 | 0.100 | 0.050 | 0.232 |
| fi | 24 | 0.213 | 0.213 | 0.046 | 0.088 | 0.208 | 0.208 | 0.213 |
| el | 13 | 0.587 | 0.471 | 0.115 | 0.500 | 0.577 | 0.577 | 0.588 |
| nl | 11 | 0.545 | 0.545 | 0.182 | 0.273 | 0.455 | 0.455 | 0.545 |
| la | 9 | 0.173 | 0.173 | 0.038 | 0.123 | 0.123 | 0.123 | 0.192 |
| other (sv eo no hu ru pl zh tl pt) | 33 | 0.314 | 0.230 | 0.170 | 0.105 | 0.254 | 0.251 | 0.317 |

### Paired (live, by reciprocal rank)

| comparison | better | worse | p (sign) |
|---|---:|---:|---:|
| A vs none | 7 | 83 | < 10⁻⁴ |
| B vs none | 7 | 82 | < 10⁻⁴ |
| C vs none | 5 | 57 | < 10⁻⁴ |
| D vs none | 4 | 60 | < 10⁻⁴ |
| cur vs none | 2 | 21 | 10⁻⁴ |
| W-C vs none | 50 | 0 | < 10⁻⁴ |
| C vs A | 48 | 22 | 0.003 |
| C vs B | 49 | 25 | 0.007 |
| C vs D | 11 | 7 | 0.48 |
| A vs B | 30 | 32 | 0.90 |

### The same strategies under other fusions (no reranker; skipped editions removed before fusion)

| fusion | none MRR / R@10 | A | B | C | D | W-C |
|---|---|---|---|---|---|---|
| code (today's RRF) | 0.082 / 0.144 | 0.043 / 0.071 | 0.039 / 0.069 | 0.052 / 0.078 | 0.050 / 0.078 | 0.083 / 0.144 |
| spec (05 §5.1–5.2) | 0.198 / 0.397 | 0.137 / 0.295 | 0.111 / 0.257 | 0.148 / 0.326 | 0.147 / 0.310 | 0.198 / 0.397 |
| vector leg only | 0.247 / 0.419 | 0.177 / 0.344 | 0.159 / 0.284 | 0.206 / 0.359 | 0.196 / 0.337 | 0.247 / 0.419 |
| live (code + reranker) | 0.231 / 0.330 | 0.133 / 0.191 | 0.132 / 0.197 | 0.180 / 0.244 | 0.179 / 0.242 | 0.236 / 0.335 |

The ordering of the strategies is the same under every fusion: no collapsing
or one hit per work first, then C ≈ D, then A and B. Under spec and vector, W
gains nothing over no collapsing, because they already count each record once.

### Title queries (39)

| strategy | MRR | R@10 | distinct works in top 10 |
|---|---:|---:|---:|
| no collapsing | 0.366 | 0.615 | 5.62 |
| cur | 0.319 | 0.564 | 5.90 |
| A / B / C / D | 0.127 / 0.201 / 0.179 / 0.179 | 0.256 / 0.359 / 0.333 / 0.333 | 6.4–6.6 |
| **W-C** | **0.398** | **0.692** | **10.00** |

- In 32 of the 36 found, ≥ 2 books of the work are in the top 50, and in 31
  they span ≥ 2 languages.
- "Don Quixote Cervantes" (en) finds the work at rank 4 through the de, es,
  fi, hu and nl editions. None of its English books is in the top 50: each
  language group's title match ranks 1 in its own list.

## Recommendation

1. **Keep every edition in the index.** Collapse by work at display time: one
   hit per work, ranked by its best edition.
   - The edition shown is the query's language when the work has it; else
     English (C); D (else the original) is the alternative, and it is a
     reading choice, not a retrieval one.
   - The other editions go behind the hit ("also in fr, fi, nl"), which is
     what `work_dedup.mentions_translation` already wants for queries about
     translations.
   - It needs the reader's language. The Ask page's "show results in" is it.
     With none, show the edition that matched.
2. **The work key** should be the origin's statement: the record's Wikipedia
   line, joined across languages by Wikidata sitelinks (775 cross-language
   works), plus PG's editions note. The (author, LCSH) key of
   `app/work_dedup.py` should go. The identical URL alone gives 434 works
   without any external lookup. The sitelink join is a reference lookup, so it
   belongs in a reference source (05 §5.1), not in the record.
3. **Fix the two fusion defects first.** They cost more than any language rule
   could win:
   - one text leg per §5.2, not one per language group;
   - the record as the fusion unit per §5.1.

   Pre-rerank R@10 is 0.144 today and 0.397 per spec. Repeated chunks alone are
   a quarter of the page.

## Limits

- **Groups cover 3,006 books in cross-language works**, 3.8% of the
  collection. A work with no Wikipedia line on any book is invisible to every
  strategy, and the groups miss some volumes.
- **Queries are written by one writer, from metadata**, which favours works
  whose summary names distinctive things. Recall is low overall (R@10 0.33
  live): many targets are not in the top 50 under any strategy, and these tie.
  The differences come from the 62–90 queries a strategy changes.
- **Query languages are skewed** to en/fr/de/it. el, nl and la have 9–13
  queries each, so their rows are indicative only.
- **"live" removes skipped editions after the reranker.** An index without
  them would rerank a different 50. The leg-level fusions answer that, without
  a reranker, and agree on the ordering.
- **The node was shared.** Another measurement used MADLAD and the encoder
  throughout. This changed latency (≈ 57 s/query), not results: a query
  missing a leg was re-run.

## Reproduce

From the repo root (Git Bash; Docker for Python):

1. `ssh node 'docker exec -i ix-louise-rag-worker-1 python -' <
   tools/measure/one_language_records.py > out/onelang/records.jsonl`
2. `python tools/measure/one_language_works.py wikidata|groups --out
   tools/measure/out/onelang`
3. `python tools/measure/one_language_select.py --out …` (draws the 150 works).
4. `sh tools/measure/one_language_loop.sh` (and
   `TSV=tools/measure/one_language_titles.tsv sh …`). This copies the harness
   to `/root/onelang` on the node and runs the pending queries in the
   read-only container. It resumes.
5. `python tools/measure/one_language_score.py --out tools/measure/out/onelang`
   (`--queries …titles.tsv --name score_titles` for the supplement), then
   `one_language_summary.py score.json`.

## Fixed (2026-10-09)

The three defects, in the code, scored by
`tools/measure/one_language_rescore.py`: the PRODUCT fusion and grouping
run on the captured legs (today's fusion recomputed from them still
reproduces the live fused order, 451/451). Pre-rerank — the legs carry no
text, so the reranker is not replayed; it orders the top 50 of whichever
list it is given, so R@50 is its pool.

| 451 descriptive queries | MRR | R@1 | R@10 | R@50 | distinct works top 10 |
|---|---:|---:|---:|---:|---:|
| before (16 text lists + vector, chunks) | 0.082 | 0.047 | 0.144 | 0.381 | 9.82 |
| one text leg, records, ties by uid | 0.187 | 0.086 | 0.388 | 0.590 | 9.80 |
| … ties: own words, then query language (shipped) | 0.219 | 0.129 | 0.397 | 0.594 | 9.81 |
| … + one hit per work (query lang, else en) | 0.219 | 0.129 | 0.397 | 0.594 | 10.00 |

- Fusion (shipped): 217 queries better, 29 worse (sign test p < 10⁻⁴).
- The leg's rank is the record's POSITION in the merged text leg. Letting
  each variant's rank 1 count as rank 1 (sixteen records sharing it) gives
  R@10 0.237: the flood again.
- One hit per work: 4 better, 0 worse; the work shown in the query's
  language, where it has a book in it, 71.8 % → 88.7 % of found queries.
  The rest: that edition was not among the candidates, so the best-ranked
  one is shown and the query-language edition is listed first under it.
- **39 title queries, pre-rerank.** Ties by uid: R@10 0.821 → 0.615.
  With the shipped tie-break (own words, then the query's language):
  MRR 0.344 → 0.501, R@1 0.154 → 0.359, R@5 0.564 → 0.641, R@10 0.821 →
  0.744 (0.769 grouped), R@50 0.923 → 0.872; 17 better, 12 worse
  (p = 0.46).
  - Why R@10 cannot come back to 0.821 without breaking §5.2: the three
    queries still lost (Don Quixote, Les Misérables, Robinson Crusoe,
    asked in English) have NO English edition among the English group's
    hits. The old fusion won them with 2–3 editions in the top 10, each
    rank 1 in ITS language group and each scoring like the vector leg's
    rank 1: several votes for one work. Now those editions sit at text
    positions 6–19 behind the other groups' rank-1s. Only knowing which
    records are one work could lift them, and work claims may not enter
    ranking (invariant 4, 05 §5.3); per-hit BM25 scores were not captured
    and are not comparable across language indexes anyway. An oracle that
    knows the answer reaches 0.846, the bound of any tie-break.
  - What the reader saw was never 0.821: the live (reranked) list had
    title R@10 0.615, 0.692 grouped (§5). Post-rerank needs a live re-run.
- Language reading: `llm.language_restriction` now sends Qwen3's
  `/no_think` (LocalAI ignores `enable_thinking`; verified on the node:
  120 tokens of `reasoning`, `content` "", versus the JSON in 25 tokens),
  400 tokens, a 30 s bound, reads the last JSON object, caches per query,
  and returns the query's language.
- Work groups: an annotations collection (`tools/annotations_from_works.py`,
  4,376 claims = the 1,694 works of §1), not yet created on the node.
