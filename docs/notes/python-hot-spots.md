# Python hot spots: measured, and what a rewrite would buy (2026-11-02)

*The operator: "Would Louise be faster by using something different like rust
or similar, or even c/c++?", then "Propose rewriting only measured hot spots,
propose what to use for the rewriting. Estimate a gain to decide what to do."*

## How it was measured

- **Profiler:** cProfile on each path's real per-document entry point, called
  in-process: the worker's fts and rdf children (`parallel._one`) and the
  records build's held-record child (`parallel._compute("held-record")`).
- **Correction:** each stage was then timed again without the profiler. Native
  work that cProfile charges to its Python caller (regex driven by
  `finditer`, pyarrow, json) was reclassified as native.
- **Data:** the test fixtures, plus clearly labelled synthetic inputs of real
  size: a 20,000-character wikipedia page, and a ~400 KB gutenberg book as txt
  and as epub.
- **Machine:** a 4-vCPU VM, Python 3.13; runs repeat within ±30 %.
- **Caveat:** wikidata's tokenizer was a stand-in of the same pipeline shape,
  since the pinned one was not reachable.
- **Where:** scripts and raw output were in the session's scratch directory
  (`hotspots/`); the figures below are taken from them.

## Where the time goes

| path | per document | pure-Python share | what dominates |
|---|---|---|---|
| wikipedia fts child | 3.7 ms per test page (434 chars); **418 ms per 20k-char page** | 87 %; 96 % on the large page | mwparserfromhell (74 %; 93 % on the large page); its C tokenizer is only 4 % |
| wikidata fts child | 0.7 ms an entity | 51 % | textify 22 %; the Rust tokenizer 14 %; the citation regex 12 % |
| gutenberg txt book (~400 KB) | 225 ms | 5 % | **entity tagging 73 %**: native regex (`citations` 57 %, the month scan 26 %) |
| gutenberg epub book (~400 KB) | 329 ms | 28 % | tagging 179 ms; stdlib `html.parser` and `pg_epub` ~51 ms |
| pubmed fts child | 0.31 ms a record | 67 % | ElementPath's path interpreter, pathlib, `medline.parse_full` |
| records-build child | 0.1–0.5 ms a record (wikidata 1.1 ms with forced windows) | 25–57 % | the tokenizer re-cut (Rust), json loads/dumps (C), **a fixed ~30 µs zstd call per record** |

## What is structural (a new language would not remove it)

1. **mwparserfromhell's `replace`/`remove` is quadratic.**
   - Each call searches the whole tree, then re-parses the replacement.
   - Cost per character grows from 8 to 22 µs between 1.6k and 20k
     characters; on the 20k page these calls are ~70 % of `parse_all`.
   - Our `wikitext.py` drives it, and walks every template 13 times a page
     (`named_template`, 21 %).
2. **Every page and abstract is parsed twice:** once by the rdf stage and once
   by the fts stage (wikipedia, pubmed). The "one parse" series (#63) is meant
   to remove this.
3. **The citation regex has no literal prefix.** Python's `re` tries a ~20-way
   case-insensitive alternation at every character: 0.4 µs a character, on
   every document of every collection.
4. **The records build compresses each record in its own zstd call** (25–42 µs
   fixed), and resolves `layers_for` three times a record.
5. **Small per-document costs:** gutenberg reads its RDF three times and
   re-scans the book directory, and `Path` objects are rebuilt per call.

## The proposal

Each change is proven BYTE-IDENTICAL before it ships, by old and new output
compared on a large real sample. A chunker or cleaner that changed one byte
would change the space and force a re-embed (invariant 3).

| # | change | tool | estimated gain | pros | cons |
|---|---|---|---|---|---|
| A | wikipedia parse: one template walk instead of 13; the cleaned text built in one pass without `replace`/`remove`; the page parsed once for rdf + fts | our own Python | the parse 2–4× faster, most on long pages; with one parse instead of two, the wikitext stages need roughly a third to a half of today's CPU | the largest cost found; no new language, tool or dependency | medium work; must give identical bytes on a large sample of real pages |
| B | the citation and month regexes: a cheap literal prefilter or a rewritten pattern; Google RE2 (C++, linear time) only if that is not enough | the pattern itself; RE2 only if needed | tagging 3–10× faster; a gutenberg book's fts child ~2–3× faster; ~0.5 ms saved an abstract | runs on every document of every collection; tags are not part of the space, so no re-embed risk | RE2 would be a new dependency (operator's yes) with small semantic differences; the tags must match on a sample |
| C | records build: one zstd call per batch of records; layers resolved once per part | our own Python | 20–30 % of a child's CPU | small | **no wall-clock gain today**: the build waits on its parent process (children 42–56 % busy), so this goes with the "child writes its part" redesign |
| D | `cdc_split`'s per-character loop (wikipedia's chunker) | Rust through PyO3 + maturin; Cython as the alternative | the loop 50–100×; ~10 % of a wikipedia record's CPU | a tight loop, the textbook case for compiling; Rust is already our stack (tantivy, tokenizers, lance) | a Rust toolchain in the image build; a chunker, so bytes must be proven identical |
| E | wikidata `textify` | mypyc (compiles the existing typed module, no rewrite) | 2–3× on its 22 % → ~15 % of wikidata's fts child | the same source, compiled | a build step; mypyc's semantic edges need the golden tests |
| F | `json.loads` in the records build | orjson (Rust) | ~15 % of a wikidata record child | drop-in for parsing | new dependency; never for `canonical_line`, whose bytes are hashed |

**Not proposed:**
- **A Rust wikitext parser** (e.g. `parse_wiki_text`) or **lxml for the epub
  reader.** Either gives different text in edge cases: a new cleaning version,
  and re-embedding wikipedia (52.9 M pages) or gutenberg.
- **A whole-program port.** The rest is already native, or too small to repay
  it.

**Order:** A and B first: they are the largest and need no new language. C
goes with the records-build redesign. D, E and F only after A and B are
measured, since each saves ≤ 15 % of one path.

## A, the walks: done 2026-11-02 (not deployed until the node's check passes)

What changed in `app/plugins/wikipedia/wikitext.py`:
- Each pass walks the tree once (`_walk`) and splices its edits into the
  node lists that hold them (`_rewrite`). Before, every node was looked up
  from the root again (`_do_strong_search`).
- `parse_all` reads the page with one walk, and the templates are indexed
  by name once. Before, it made 18 walks, 13 of them in `named_template`.
- Nothing else moved. The same three `mwparserfromhell.parse` calls happen
  per `parse_all`, and the rdf and fts stages still each parse.

**Measured.** `parse_all`, perf_counter only. Reference and live rounds were
interleaved, 11 rounds, and the median is shown. The 4-vCPU VM was shared
with two other jobs, so read the ratios rather than the absolute times.

| page | before | after | speed-up |
|---|---|---|---|
| test articles, mean of 4 (112–870 chars) | 2,916 µs | 1,987 µs | 1.47× |
| SYNTHETIC 2k chars | 18.9 ms | 11.2 ms | 1.70× |
| SYNTHETIC 5k chars | 49.7 ms | 21.2 ms | 2.34× |
| SYNTHETIC 20k chars | 398 ms | 95 ms | 4.19× |

- The cost per character is now flat at about 4.5 µs. Before, it rose
  from 7 to 20 µs.

**Identity.** The code before is kept verbatim in
`tools/measure/wikitext_reference.py` and compared byte for byte by
`json.dumps(sort_keys=True)`:
- `tests/test_wikitext_identity.py` runs it on every string literal in
  `tests/`, on 3,000 seeded adversarial pages and on 17 named edge cases.
  It also compares the tree's shape after every pass.
- A further 50,000 seeded pages (29.8 M chars) were compared once, outside
  the suite: none differed.
- Real pages go through `tools/measure/wikitext_identity.py`. Its verdict
  must be IDENTICAL on ≥ 100,000 of the node's pages before this deploys.

**What is left.** Of what remains, the three whole-page parses are about
57 % on the 20k page (3 × 17 ms of ~88 ms) and about 80 % on a test page
(3 × 1.1 ms of ~4 ms). Almost all of that is the library's Python tree
builder: its C tokenizer is 10–18 %.
- Two of the three parses were the same string: `parse_all` parsed it, and
  so did `_structured`, since `_tags` is idempotent. Shared since the same
  day (the coordinator: inside one `parse_all` call it is an internal
  detail; the rdf and fts stages each calling it stays the "one parse"
  series). Every read of the tree is taken into strings first, and the
  prose passes then run on that tree (`_structured_code`). If `_tags`
  would still change the string, the prose gets its own parse, as before.
- `apparatus` keeps its own parse. Its string has the reference lists
  taken out, and its passes change the tree differently. A deep copy
  costs ~3× a parse (1,619 against 568 µs on a test article, 38.7 against
  13.5 ms on 20k chars).

The same measurement, the three versions interleaved per round (11
rounds; another job on the VM):

| page | before A | A | A + one parse for two | A → now | before → now |
|---|---|---|---|---|---|
| test articles, mean of 4 | 2,951 µs | 2,019 µs | 1,615 µs | 1.25× | 1.83× |
| SYNTHETIC 2k chars | 19.9 ms | 11.4 ms | 8.9 ms | 1.28× | 2.23× |
| SYNTHETIC 5k chars | 50.4 ms | 24.6 ms | 18.4 ms | 1.33× | 2.74× |
| SYNTHETIC 20k chars | 358 ms | 87.6 ms | 66.8 ms | 1.31× | 5.36× |

Identity, the same bar: the 32 tests of `tests/test_wikitext_identity.py`
(new: two whole-page parses where there were three; every read done before
the prose touches the tree, on a tree shaped as a fresh parse; the tree
after the prose passes shaped as the old `_prose_code` leaves a fresh
parse), and the same 50,000 seeded pages again: none differed.

## B, done (2026-11-02)

`citations` (app/refs/normattiva.py) tries its two patterns only where a
citation can start: before a digit run whose tail is a citation's (one
`\d`-led regex, which `re` skips to in C), within the reach back the
patterns allow (at most 15 or 23 runs of white space, no `;`), on one of
the two-letter openings (`ar`, `le`, `l.`, `de`, …). The month pre-check is
gone: the run's tail holds the month. No new dependency; RE2 was not needed.

The patterns are unchanged byte for byte, and the answer is unchanged byte
for byte against the verbatim old scan (tests/citations_reference.py) on:
the fixtures, 80,000 generated texts (122 M characters) and 240 long ones
(30–300 KB, 34 M), 543,831 citations in all (43,648 dated). 239 texts
raised the same KeyError in both: a month spelled with ſ, ı or İ, which
`re.I` matches and `str.lower` does not map back — the document an fts
error. Fixed in a commit of its own (`normattiva._month`: the month read as
the pattern matched it); those 239 now answer, as the old scan does once
its month table reads the folds. The suite keeps 2,000 of the texts
(tests/test_citations_fast_path.py).

Measured: perf_counter, the median of 5 runs, then the median of 3 such
runs; the 4-vCPU VM had other jobs on it (load 2.5–3.5), so ±30 %.

| input | before | after |
|---|---|---|
| 400 KB of book prose, no digit | 152 ms | 2.1 ms |
| the same with a citation, date or number every 0.5–4 KB | 245 ms | 3.2 ms |
| 1,500-character abstract | 545 µs | 9.2 µs |
| 400-character text | 133 µs | 3.0 µs |
| a normattiva act's XML, 223 KB, 71 citations | 117 ms | 9.8 ms |
| 1,500 characters of it | 855 µs | 86 µs |
| gutenberg ~400 KB txt book: entity tagging | 204 ms | 20 ms |
| the same book: the whole fts child | 176–200 ms | 25–39 ms |

Tagging's largest cost is now `DictResolver`'s token `findall` (~15 ms of
those 20; unchanged).

## Done: C and F (2026-11-02, operator: "I want all the optimizations")

- **C1:** a child's task compresses its lines in batches (64 lines or
  256 KiB). Each record carries `(batch, start, end)`, and the part's writer
  expands each batch once (`pack.build.ship_lines`, `_columns_of`).
- **C2:** the plugin and the layers are resolved once a task
  (`layers.Resolved`, `pack.build.compute_records`), not three times a
  record.
- **F:** held lines are parsed by orjson 3.13.0 (`pack.records.loads_line`).
  - **Found:** orjson does NOT raise on an integer past 64 bits. It returns a
    float (`18446744073709551616` → `1.8446744073709552e+19`).
  - So a line with a run of 19 digits goes to json.loads, and so does any
    line orjson refuses.
  - `canonical_line` stays json.dumps.
- **The held path also skips a `Path` per record.**

**Identity.** Every staged part and every root the build database records
are the bytes of a build by the book (`tests/pack/test_ship_batches.py`).
This holds for wikidata, wikipedia, pubmed and gutenberg, parsed and held,
in-process and in a pool, with a child SIGKILLed. Old commit against new,
pooled, parsed and held: 144 parts of 946 records, all identical.

**Measured** (profiler-free, µs a record, the held-record child on the p5
inputs, the median of 3 rounds of 5 [the floor]; 4-vCPU VM, ±30 %):

| collection | before | after | after, json.loads |
|---|---|---|---|
| gutenberg | 146 [135] | 54 [49] | 60 [55] |
| pubmed | 121 [111] | 39 [32] | 39 [37] |
| wikipedia, test pages | 147 [139] | 72 [57] | 79 [67] |
| wikipedia, 20k-char page | 948 [833] | 822 [618] | 859 [755] |
| wikidata, default layer* | 459 [387] | 289 [262] | 322 [284] |
| wikidata, forced windows* | 1,044 [861] | 792 [697] | 1,018 [799] |

\* the stand-in tokenizer. The 20k page is bimodal on this VM in both trees,
so its floor is the reliable figure.

- **The parse path** ("record") gains 14–18 % where the parse is cheap
  (gutenberg, pubmed, wikidata's default layer). It is unchanged within noise where the parse
  dominates (wikipedia).
- **The build's wall clock does not move yet.** It waits on its parent
  (children 42–56 % busy). This waits for the "child writes its part"
  redesign.

## D, done (2026-11-02; not deployed until the node's check passes)

`_cdc_boundaries`' loop runs in `native/louise_cdc`: Rust through PyO3,
a wheel built by maturin (FastCDC's normalized chunking over a Gear hash,
Xia et al. 2016; the `fastcdc` crate cuts bytes with its own table, so it is
not ours).
- **The same arithmetic, not an equivalent.** The gear table and masks are
  handed in from app/chunking.py (one table). The u64 wrapping hash equals
  Python's masked unbounded one, proven in lib.rs's header; offsets count
  code points (`char`, `ord & 0xFF`).
- **The Python loop stays, unchanged,** and answers whenever the extension
  cannot: a `str` with a lone surrogate (no UTF-8 form), a size past 64
  bits, a text no longer than `min`, a tree without the wheel.
- **The image:** stage `cdc-build` (rust 1.97.0 by tag and digest, maturin
  1.15.0, PyO3 0.29.3 and Cargo.lock, `--locked`), one abi3 wheel for
  CPython ≥ 3.12, installed under constraints.txt (`louise-cdc==0.1.0`). The
  build fails unless `tools/measure/cdc_identity.py` says IDENTICAL on 300
  seeded texts.

**Measured** (perf_counter, median of 7 rounds; the 4-vCPU VM, Python 3.13;
real prose: calibration/text.jsonl, Gutenberg in 18 languages):

| input | Python loop | extension | |
|---|---|---|---|
| 4 KB section, 18 languages (179) | 552 µs | 8.8 µs | 63× |
| 4 KB section, ASCII (16) | 499 µs | 2.8 µs | 178× |
| 4 KB section, CJK (22) | 596 µs | 8.2 µs | 73× |
| 20k-char page, one section (76) | 2,867 µs | 44 µs | 65× |
| 20k-char page, 5 sections, `chunk_cdc` (76) | 2,531 µs | 58 µs | 44× |
| SYNTHETIC wikipedia 20k page after `parse_all` (157 sections, 4,680 chars) | 66 µs | 69 µs | — |

Non-ASCII text costs 3× ASCII: CPython encodes it to UTF-8 first. Sections
no longer than `min` take the Python loop on purpose (0.25 µs against 0.44).

**Identity.**
- The version-1 cut is pinned by digest, from b1d6bc1's loop
  (tests/test_cdc_native.py); both paths reproduce it.
- The suite compares the two paths on every string of tests/, every
  fixture file (as UTF-8 and as Latin-1), the calibration prose, 1,000
  seeded texts at three sizes each, and `chunk_cdc`, `chunk_akn_article`
  and `layers.chunks_for_document` on and off. The chunking suite passes
  both ways.
- Outside the suite, 20,000 seeded texts (342 M code points, 28.3 M
  boundaries, 2,541 cuts declined to the Python loop) were IDENTICAL. So
  were 2,000 on Python 3.12.3 with the same wheel.
- Real records: `python -m tools.measure.cdc_identity --collection
  wikipedia-v8 --records 100000`, in the new image. München deploys only
  on IDENTICAL.

**What it buys:** the ~10 % of a wikipedia record's CPU this loop was
(estimated above, not re-measured on real records here). The records
build's wall clock still waits on its parent (C).
