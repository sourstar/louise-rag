# v1.0 roots — pubmed and gutenberg, and the flag day they wait behind

Measured and decided 2026-09-12. Operator decision that frames all of it:

> "The resulting collections of this development phase will be the version
> 1.0 of the production phase, thus not daughters or sisters of precedent
> non compliant collections, but built on top either a corpus from the
> source (i.e. gutenberg), or a pre-embedded corpus (i.e. pubmed and
> wikipedia)."

and the standing rule for getting there:

> "to fix an issue in a collection take the fastest route, keeping the
> final result compliant by the books. The final result compliancy is a
> non negotiable condition."

So the v1.0 collections are **roots**: `replaces` null, `derived_from`
null, `epoch` `"0"`. That is not a dodge of §2.7's lineage — the
development collections were never published to a peer, so there is no
relation to record and nothing for `replaces` to inform. It also avoids
the corrective sister's cost, which is not small: a corrective sister's
epoch is `H({replaces: <id>})` (02 §2.4), `epoch` is inside `space_id`,
and §3.5 refuses any bundle whose `space_id` is not the collection's — so
by-the-book correction means **vectors from zero**, 39,958,399 chunks at a
measured **20.5 chunks/s** on the CPU MedCPT encoder = **541 h ≈ 23
days**.

## What was wrong (verified against the spec, not inferred)

§9.5 admits "exactly these keys, written in this order". The live record
had two extra and three required ones wrong.

| field | §9.5 wants | parser 4 wrote | available in origin |
|---|---|---|---|
| `creators` | origin's order, `[]` when none | always `[]` | `AuthorList`, 99.0% |
| `lang` | ISO 639-1, `"und"` unknown | hardcoded `"en"` | `<Language>`, 100% |
| `date` | `YYYY`/`YYYY-MM`/`YYYY-MM-DD` | always `null` | `PubDate`, 100% |
| `layer`, `chunks` | not in the table at all | present | — |

`lang` was the expensive one to have wrong: **9.6% of this corpus is not
English** (360,000-article sample — 22.6% of the oldest container, 2.5%
of the newest), so ~3.8M records claimed a language they are not, and the
Ask page's "show results in English" skipped every one of them as
"already English".

Compliant and NOT changed, so nobody re-opens them: `origin`'s `#`
member form (§9.5 requires it), `validation.required: []` (§9.3: "`[]`
on an open collection"), `facets`, `origin.form` (invariant 11's form
choice IS recorded), `record_line: 1`, `keys: []`.

`extra` stays `{journal, revised}` — `date` left it, because the
top-level field now carries it in §9.5's form and holding it twice in two
shapes is two answers to one question. Everything else MEDLINE carries
(`ArticleIdList`, `PublicationTypeList`, `KeywordList`, ISSN, pagination,
copyright) is deliberately dropped: invariant 11 permits pruning what we
do not use, and `extra`'s keys are fixed for the collection's life, so
this is the decision that cannot be revisited without another root.

## The Codex was missing a field, on every collection

09 §9.3 lists 35 top-level keys. `app/hold/codex.py` knew 34 —
`derivation` was absent entirely, and since every key but `descriptive`
is inside the hash, **every `collection_id` this platform ever computed
was taken over the wrong bytes.** Checked against all five codexes on
the node: each missing exactly `derivation`, none carrying anything
unexpected.

**This is a flag day.** `_only` requires every field ("every field is
present, a field not in use is null"), so the moment the fix deploys
every existing `codex.json` fails to load and `collections.py:270`
refuses the collection — "not served, shared or searched until they
agree". That includes gutenberg, whose fetch is running.

## A manifest refresh is NOT a rebuild

This got answered wrongly once (2026-09-12, about the translation-dedup
fields) and the distinction is worth stating flatly, because the two look
alike and cost three orders of magnitude apart.

| | what it is | what it costs | does `collection_id` move? |
|---|---|---|---|
| **manifest refresh** | re-run a stage (`rdf_done=0`, and the worker re-reads) so `documents` picks up new parser output | parsing local files — minutes for gutenberg's 79,320 RDFs | **no** |
| **schema addition** | a column on `documents` | one `ALTER TABLE`, applied at connect (db.py already does this for `entity_version` and `content_hash`) | **no** |
| **records rebuild** | `hold records build` writes new shards | re-reads the origin; 16.1 GiB for pubmed | **no**, on its own |
| **re-mint** | the Codex changes — parser version, `record_line`, any hashed field | a new collection beside the old one | **yes** |

`documents` is DERIVED from the parser and can be re-derived at will. The
RECORD is what freezes into the collection's identity. A field that is in
the parser is therefore already available to search after a re-scan — it
is only its appearance in the PUBLISHED records, which is what a peer
fetches, that waits for the build.

## Order, and why it cannot be shuffled

1. **Gutenberg fetch completes.** Non-negotiable: the fetch is driven by
   the gutenberg collection, and the flag day refuses it.
2. Deploy parser 5 + `derivation`.
3. Build the v1.0 roots.
4. `hold check` each.
5. Retire the development trees.

## Where pubmed v1.0's vectors come from

Two routes. Both are compliant; they differ by about a day of wall clock.

**The dev collection's own vectors, as bundles** — `bundle export` from
the dev collection, `bundle import` into the root. Admissible under §3.5
because that test is (`space_id`, per-record `content_hash`) and both
match: `parser` is not in `space_id`, both epochs are `"0"`, and
`content_hash` is BLAKE2b-128 over `[title, sections]` alone, which none
of the three corrected fields touches. Chunk ids are
`<uid>:<seq>:<chunker version>` — none of the three parts moves.
`possession.json` verifies too, since `raw_hash` is over the origin XML
bytes and parser 5 does not touch those. No download.

**The admission rule is the module's own contract, not an inference.**
`app/hold/admit.py`'s header says it in as many words — "Admissibility is
`space_id` plus per-record `content_hash`. `collection` and
`snapshot_id` are PROVENANCE ... and gate nothing ... A peer that gated
on `collection` would refuse exactly the sharing the design exists for"
— and `tests/hold/test_admit.py::test_a_bundle_whose_collection_differs_is_still_admitted`
relabels a bundle's `collection` and asserts it still lands. So a bundle
the development collection exports is admissible to a root with a
different `collection_id`, and that is pinned rather than hoped for.

**And it is not merely faster, it is more correct than copying Lance.**
The Lance `chunks` table carries `lang` and `text` per row, so a
directory copy would carry the stale `"en"` into ~3.8M rows —
reintroducing the bug being fixed. A bundle carries `chunk_id, seq,
vector[dim]` and "no text, ever"; `importer.py:160` re-derives the text
locally and `:177` reads `lang` from the **importing** collection's
`documents`. The corrected values win by construction.

**The archive route** — `hold vectors import --archive`, which
migration-plan.md §step-8 designates for pubmed ("an entry in `credits`
and a source `hold vectors import --archive` draws from"). It re-proves
the vectors against this node's own encoder rather than inheriting them
on a matching `space_id`, which is a stronger guarantee. **But the
shards are gone:** `data/pubmed/genesis/medcpt-pubmed` and
`collections/pubmed/state/genesis/medcpt-pubmed` are both empty — all 37
are `final` and were deleted after import. Re-running it means
re-fetching from
`https://ftp.ncbi.nlm.nih.gov/pub/lu/MedCPT/pubmed_embeddings`;
35,337,233 chunks × 768 dims is ~108 GB at f32, ~54 GB at f16 (estimate
— the ledger's `bytes` column was never populated).

Either way the root lands in **today's** state, not a worse one: the
archive covers 35,337,233 of the collection's 39,958,399 chunks and the
remaining ~4.6M are past its declared range and were never embedded. No
new embedding work is created by the rebuild.

## What the wikivec blockage does and does not imply

`wikivec-merge-blocked.md` found that merge cannot carry its archive's
12.7M vectors. That is specific to it — two plugins cutting the same
articles into different objects, a uid carrying its plugin's name, two
different Wikipedia snapshots — and none of it applies here. Pubmed's
corpus, chunker and uids are unchanged by parser 5.

## Gutenberg, checked the same way

Its v1.0 root is built by the same code, so it got the same audit. It is
in far better shape: `lang` is real ISO 639-1, `creators` is populated in
the origin's order, `date` is a well-formed `YYYY-MM-DD`, `extra` carries
`bookshelves`. One violation, now fixed (parser 6):

**MARC subfield delimiters in titles** — 4,648 of 79,286 (5.86%),
almost all `… : $b subtitle`. §9.5 says `title` is "cleaned". The
pattern takes **letters only**: `$1`, `$3` and `$5` also occur and are
money in real titles ("The $100 Prize Essay on the Cultivation of the
Potato", "The $30,000 Bequest, and Other Stories"), so `\$[a-z0-9]`
would have mutilated Mark Twain to tidy a delimiter. `title` is inside
`content_hash`, which costs nothing here because this root starts from
zero vectors anyway (step 8).

**`date` is the work's publication date — decided, every collection.**
Operator, 2026-09-12: "Work publication date!!! Same decision for any
collection!!!" pubmed already satisfies it (parser 5 writes the
journal's `PubDate`).

Gutenberg **cannot**, so its `date` is `null`. PG's RDF carries
`dcterms:issued` — the date PG released its own digital edition — and
nothing else date-like. Checked across twelve major books: the only MARC
datafields PG keeps are 508 (production note), 520 (an automatically
generated summary), 901 (cover image URLs) and 908 (reading ease). No
260$c, no 264$c. `marc520`'s prose does sometimes say "published in
1871", but it announces itself as generated text, and mining a year out
of it would put a claim in a signed record that nobody made. §9.5 admits
"string or null", and null beats being wrong by 135 years —
`gutenberg:17832` is Verne's *Une ville flottante* of 1871, released by
PG in 2006.

**And the cutoff moved with it.** `cutoff.record_key("gutenberg", …)`
read the record's `date`, so nulling it would have returned "no cutoff
date" for all 79,320 records and excluded the whole corpus from any
dated snapshot. `issued` lives in `extra` now — `pack build --until` is
still denominated in it — and the cutoff reads it there, which is the
shape pubmed always had. The same change exposed that pubmed's cutoff
fell back to `extra.date`, which parser 5 had removed as a duplicate;
it falls back to the top-level publication date instead, so nothing
narrowed.

## Open for the operator

- **wikipedia's `date`.** Audited 2026-09-12. `parse()` sets `date=None`
  while `extra.revised` holds the revision timestamp the dump asserts —
  the same shape as pubmed's gap, data available and the §9.5 field
  empty. For a continuously revised article, is the "work publication
  date" the timestamp of the revision we hold? I would say yes: that
  revision IS the work in hand, and it is the only date the dump gives.
  Needs the operator, like gutenberg's did, and `date` is in the record
  line so changing it later is another root.
- **wikipedia's `subjects`** are `[]`, though the wikitext carries
  `[[Category:…]]`. (The `Category` string in `wikipedia/source.py:26`
  is a NAMESPACE FILTER — it skips `Category:` pages — not extraction.)
  Invariant 11 permits dropping what we do not use, so this is not
  non-compliance; but `subjects` is fixed for the collection's life and
  the Codex's `facets` may want to declare one over it, so it is a
  decide-before-the-root.

  Measured on this node's simplewiki dump, 4,000 pages: **76% carry
  categories, 3.1 on average.** `parse()` already holds the whole
  wikitext (`revision/text`), so extraction is one regex over a string
  in memory — free now, a new `collection_id` and a records rebuild
  later. Not a re-embed at least: `subjects` is outside `content_hash`,
  so chunk ids and vectors would survive a fork.

  AND THERE IS A SECOND HALF TO THE DECISION. Categories mix topics with
  MAINTENANCE: the sample gives `Art` → `['Art', 'Non-verbal
  communication', 'Basic English 850 words', 'Visual arts']`, and enwiki
  adds `Articles with unsourced statements`, `CS1 errors`, `Use dmy
  dates from …`. So: verbatim, or filtered by a known-prefix list?
  Recommendation is VERBATIM — invariant 11's instinct is to keep what
  the origin says, a hand-maintained exclusion list ages badly inside an
  immutable record, and `facets` can hide noisy values at display time
  without baking an editorial judgement into signed bytes.
- **wikipedia/wikivec are not configured collections on this node** — a
  corpus and archive state on disk, no TOML — so nothing above is urgent.
  `wikivec-merge-blocked.md` governs that pair when it comes up.

Audited and NOT a gap, so nobody re-opens them: `creators=[]` on both
wiki plugins (an article has no single author; citation practice names
"Wikipedia contributors", which is not something the origin asserts
per-article), `date=None` on `embedded` (the archive carries no date at
all, so null is the honest answer as it is for gutenberg), and
`embedded`'s `keys=["wikipedia:<lang>:<article>"]`, which is the
cross-reference to the wikipedia collection.

### Settled 2026-09-12 (kept here because the reasoning cost something)

- **Translated titles: YES**, and 5j amended on `main` (`e6a23f5`) before
  the code. The old rule — "never titles ... a bibliographic reference
  must read as published" — assumed the displayed title was the only copy
  of it. The operator's argument is that it is not: "how am I supposed to
  understand a title in Arabic or Hindi?", and the switch is REVERSIBLE,
  so the published title is one selection back and sits in the tooltip
  meanwhile. Authors and ids still do not change — a personal name is not
  translated in any language, an id is not language.
- **The 78 s switch.** Now 25 s for a page of 22 items, by batching per
  SOURCE language and running the batches concurrently (four calls to
  this node's Ollama: 6.85 s wall against 17.13 s summed, so the
  concurrency is real). It also says what it is doing before the wait,
  which is what had made it look broken.
- **Short items came back verbatim** — the failure behind "the title is
  never translated". Three predictions all measured no better: relaxing
  the prompt's "keep names as they are" (no change), demanding every item
  be translated (7 unchanged became 9 — worse), and the per-language
  grouping above (fixed latency, not this). What works is a correction
  loop: a value equal to its own input was already detected, and those
  items go back once in threes, three being the batch size measured to
  succeed where eleven failed. 20 of 22 on a cold cache, and the page
  names the two it cannot save.

## Loose ends

- `docs/09-formats.md`'s `dtype` entry said "`derivation` below" when the
  field is above it in the same table. Fixed on `main` (`5d3be93`).
- Nothing compares a running plugin's `PARSER_VERSION` against the
  Codex's `parser.version`. A parser bumped in place builds records under
  a Codex naming the old parser, silently. Not blocking the rebuild —
  parser 5 goes in with a fresh Codex — but it is a missing refusal.
- The development pubmed's `documents.authors` was backfilled
  (`tools/backfill_pubmed_authors.py`) so the GUI is honest until the
  root replaces it. That run also built the 632 missing container packs,
  which the records rebuild needs, so it is not throwaway work.

## 2026-09-21: the pubmed root exists; its vectors are a decision

The operator: "pubmed with all the fixes we agreed", without re-fetching
anything. Done, except the cut-over:

- **Root created**: `sha256:7541bb4a…`, Codex version 3, relation root,
  parser 8, platform image `08554e60…` (kept on the archive pool and tagged
  `louise-rag:codex-pubmed-v1`), credits `medcpt-pubmed`. Its recipe is
  [`pubmed-v1-recipe.toml.txt`](pubmed-v1-recipe.toml.txt). Its drag is on
  tank beside the development one, not yet swapped in.
- **Its records** are built by its own image from the origin on tank. Then
  validation, snapshot and the Codex's check plan run, unattended.

**Why its vectors cannot be the development collection's.** Today's code
writes only version-3 Codexes, and from version 3 the engine is OUTSIDE
`space_id` (02 §2.4). The development Codex is version 2, which hashed
the TEI image INTO it. Same weights and settings, different `space_id`: §3.5
refuses those vectors, and invariant 1 refuses a vector table from another
space. `--space-from` cannot bridge the two, because create refuses to
write a version-2 space.

**Three ways to give it vectors, none re-fetching:**

| route | what runs | measured cost |
|---|---|---|
| A. re-embed everything | the model pool MedCPT on the Arc | 38 chunks/s: 40M chunks, **~12 days** |
| A'. archive + re-embed the rest | `hold vectors import --archive medcpt-pubmed` over the **22 shards still on disk** (20.7M documents, re-proved against this node's encoder), then the encoder for the ~19M left | the import is hours; the rest **~6 days** |
| C. amend the spec | admit a version-2 space's vectors into its version-3 successor once the producing engine passes the new space's gate | TEI's image may still be on the node; NCBI's archive engine cannot be run, so its 14.7M vectors could only enter through the archive route anyway |

Recommendation: **A'**. It is by the book, fetches nothing and is the
fastest compliant route.
- Those 22 shards were `rejected` on 09-13, while the check was
  embedding with Qwen3-4B instead of MedCPT (fixed by e8e2751). So the
  rejection says nothing about the shards.
- Until the cut-over, the development pubmed stays in use: its only open
  test is the origin check, covered by the operator's decision of
  2026-09-20.

**The cut-over itself, once decided:**
- move the development drag to `louise-drag/_superseded/`;
- set the collection file's `codex` to the root;
- move the development Lance table aside, since it is another space;
- re-derive the manifest (`rdf_done = 0`), because `documents.lang` is
  still parser 4's `en` everywhere.

The text index can stay: chunk ids and text are unchanged, because parser 8
does not touch `sections`.

### Decided: A' (operator, 2026-09-21), and the four facets

The root was re-created with 02 §2.3's four facets (`creators`, `date`,
`lang`, `subjects`) before any record was built under it:
`sha256:5f0944e3…`, the same space `2c05a8c3…`. The first root,
`7541bb4a…`, had the plugin's single facet, carried nothing and was
never used; its files are in `louise-drag/_superseded/`.

**The cut-over, in this order**, in one restart window, with the deploy of
a7cf99c. Each step reverses by a rename:
1. Drag: the development drag's contents go to
   `louise-drag/_superseded/pubmed-c4e3bd74/`, and `pubmed-v1`'s come into
   `pubmed/`. These are renames inside one dataset, so the live bind mount
   sees them.
2. Set aside, as renames: `collections/pubmed/lance` (another space) and
   `state/backend_baseline-pubmed.json` (another reference). The text index
   stays.
3. The collection file's `codex` line becomes the root. Nothing else in
   it changes.
4. The manifest:
   - `embed_done = 0` everywhere, because the new space holds no vectors;
   - `lang`, `authors` and the other parser fields come from the root's
     records, one pass over the shards;
   - this goes BEFORE any vector lands, because the importer and the
     encoder read `lang` from `documents`.
5. The archive ledger (`genesis_imports`, `medcpt-pubmed`) describes the old
   space's imports:
   - the 22 shards on disk go back to `fetched`, and a file already on
     disk is never fetched;
   - the 15 deleted ones become `rejected`, with the detail "not
     re-fetched: operator decision 2026-09-21". A rejected shard is never
     re-read, and its documents go to the local encoder
     (`genesis._candidate_shards`, `worker._covered_ranges`).
6. Start. The root is NOT on `quarantine_in_use_before`, so it passes its
   own tests first:
   - roots, chunks and origin were attested by the build's image;
   - the gate runs at the first embed;
   - the licence is acknowledged by its terms hash.

   Then the 22 shards import, each re-proved against the root's reference
   engine, and the encoder takes the rest.

### CORRECTION, same day: the archive cannot give the root a single vector

Above, the rejection of the 22 archive shards still on disk was put down to
the check embedding with Qwen3-4B. **That was wrong.** Reading the ledger
itself:
- **12 shards** (10–12, 16–17, 19–23, 25–26) were rejected on **2026-09-19**,
  after e8e2751. The detail is "sample re-encode failed: archive vs local
  encoder median cos 0.80–0.93, at most 5 of 16 at ≥ 0.95". The wrong model
  would score near 0; 0.8–0.9 is the same model given different text. The
  archive embedded other versions of these abstracts than the ones the
  records hold. Admitting them would mix spaces, and the proof was right
  to refuse.
- **10 shards** (27–36) were refused at admission: only 12–43 distinct
  vectors among 500 chunks. The archive's data is degenerate there.
- The 15 shards that passed were deleted after import.

So route A' is route A. The root's ~40M chunks are this node's encoder's
work: **~12 days at the 38 chunks/s measured**, which was measured under
three concurrent builds and with the model pool's MedCPT on one slot and 512-token
batches. Proposed to the operator:
- measure a tuned run (more slots, bigger batches). It is another engine
  run for the same space and joins it by passing the gate (02 §2.4);
- build the vectors BESIDE the live collection (`hold vectors build` under
  the root's image, from the parser-8 records, so every vector carries the
  record's real `lang`), then switch in minutes. The development pubmed
  serves searches meanwhile.
