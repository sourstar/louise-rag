# Wikipedia: smart embedding

*Decided 2026-11-02 (the operator): "For Wikipedia implement a 'smart'
embedding option: download and embed english only, plus all the articles
that are either missing or that are longer in another language; in this
case pick only the longer article in another language. The rest of the
material is likely to be redundant. Make it the default option." Asked which
reading, they answered "Interpretation A is fine". Code:
`app/embed_select.py`; tests: `tests/test_embed_select.py`.*

## The rule (reading A)

For each Wikidata item:
- every **English** article is embedded;
- **one** article in another language is embedded too — the longest — when
  the item has no English article, or when that article is longer than the
  English one;
- the item's other articles are **declined**.

An article no Wikidata item names is embedded, since nothing shows it is
redundant.

Declined (`embed_done = -1`, the mark `embed_slices` already uses) means:
- not offered to the encoder;
- still held, word-searchable and shared;
- its record untouched (invariant 11: this chooses what to EMBED, not what
  is held);
- vectors it already has are kept.

## The measures

- **Length:** the manifest's `tokens`, counted with the space's tokenizer.
  One unit across scripts: bytes would favour three-byte scripts.
  An article whose length is not known yet (`tokens = 0`) is never declined
  on a guess. An English article of unknown length keeps nothing out.
- **Ties:** the lower language ordinal (`ids.LANG_ORDINAL`, frozen), then
  the earlier document. The same data gives the same choice.
- **The item:** `wikibase_item` from the pinned run's `page_props`
  (parser 5, [wikipedia-qids.md](wikipedia-qids.md)), read from the packs
  `page_props.py` keeps. It is never read from the `wikidata` collection.
  Language and page come from the document id (`ids.decode_native_id`).

## The pass

- **Runs:** on the scan thread, after the slice scope.
- **Reads:** `(rowid, native_id, tokens)` in rowid windows.
- **Decides:** with numpy over the whole collection, about 1 GB for 52.9 M
  documents, since an item's articles lie anywhere in the manifest.
- **Writes:** the marks in short `BEGIN IMMEDIATE` windows that re-test the
  column under the lock. It resumes from a cursor.
- **Redone when what it was computed for changes:** the choice,
  `embed_slices`, the manifest's last row, or the page_props run. Its cost
  follows events, never rounds.
- **Until it has caught up,** the collection's pipeline waits and asks for
  a scan.
- **Ownership:** while "smart" is on, this pass owns the DECLINED marks and
  applies `embed_slices` itself, and the slice scope leaves them alone.
  Back to `"all"`, the marks are given back and the slice scope recomputes.

## What it does not do

- **Download per article.** Wikipedia publishes each language as one dump,
  so a language is fetched whole or not at all. The saving is in the
  encoder (card days) and in vector disk.
  - The standard route to fetch fewer bytes would be the multistream dump's
    index: byte ranges of 100-page streams, fetched by HTTP Range.
  - It would pay only for a language not yet held, and it needs a fetcher
    that understands ranges. It is not built.
- **Measure content.** "Longer" is length. A content measure (a summariser,
  a coverage score) would cost a model call per article, for a choice the
  operator made on length.

## The file and the commands

- `embed_select = "all" | "smart"` in the collection file (07 §7.8). If
  absent, wikipedia is "smart" and every other plugin "all". "smart" is
  refused where no grouping exists.
- `louise hold config apply <c> --set embed_select=all|smart|default`.
- `louise dev embed-select <c>` previews the counts read-only; `--apply`
  writes the marks now.
