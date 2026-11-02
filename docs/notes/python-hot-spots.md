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
