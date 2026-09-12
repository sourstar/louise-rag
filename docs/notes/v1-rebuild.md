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

- **Translated titles.** STATUS 5j says "never titles, authors or ids (a
  bibliographic reference must read as published)", and the reason holds:
  nobody finds an article by a translated title. The operator asked why
  they are never translated (2026-09-12). Three ways: leave it; show the
  published title with a translated one beneath it (satisfies reading
  without breaking citability — the recommendation); or translate it
  outright. The last two need 5j amended on `main` first.
- **The switch costs 78 s** for a page of passages, one model call for
  all of them. It now says so before the wait, which is what made it
  look broken. Cutting the wall clock means smaller parallel batches.
- **wikipedia/wikivec** are not configured collections on this node — a
  corpus and archive state on disk, no TOML — so their §9.5 audit is not
  done. `wikivec-merge-blocked.md` governs that pair when it comes up.

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
