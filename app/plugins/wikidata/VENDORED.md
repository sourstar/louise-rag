# Vendored code in this package

`textify.py` carries, adapted, code from **WikidataTextifier**
(github.com/wmde/WikidataTextifier, commit 0befd7c, 2026-05-25), BSD
3-Clause, Copyright (c) 2025, Wikimedia Deutschland e. V. The notice below
is the upstream LICENSE's; it covers the data classes and their `to_text`
rendering (`src/Textifier/WikibaseTextifier.py`) and the normaliser's
shape (`src/Normalizer/JSONNormalizer.py`).

What was changed, and why (docs/notes/wikidata-build-plan.md §2, §5):

- labels of referenced entities and properties come from a callable over
  the dump's own label table, not from the Wikidata Action API with a
  MariaDB cache (`LazyLabelFactory`): the text is a function of the dump;
- time, coordinate and quantity values are formatted by `format.py`, not
  by `wbformatvalue` on a running Wikibase (D4, revised 2026-10-19);
- aliases keep the dump's order, deduplicated with `dict.fromkeys`; the
  upstream `list({...})` made their order depend on the hash seed;
- a value whose entity has no label in the language is rendered as its id
  (`Q2755432`) instead of `<no label>`: the id is searchable, the
  placeholder is not; a PROPERTY without a label still drops its claim
  from the text, as upstream;
- the `instance of` (P31) statement is rendered FIRST, the rest in the
  dump's order: it is the sticky line the chunker repeats in every window
  (upstream's `sticky_property_ids`), found by its place in the text;
- the rank filter, the `external_ids`/`qualifiers`/`references` defaults
  and the connectives (`language_variables.json`, inlined) are upstream's.

`chunker.py` carries the cutting rule of **WikidataTextEmbedding**
(`src/utils.py`, `chunk_item_text` and `_trim_to_length`, commit c9dc819),
MIT, Copyright (c) 2024 Jonathan Fraine: the whole text when it fits, else
head + sticky statement + the longest run of statements that fits, an
over-long statement cut at the token that fits. Rewritten over our rendered
text (the statement lines) and a `tokenizers` tokenizer named by sha256;
the rule and its parameters are theirs.

---

BSD 3-Clause License

Copyright (c) 2025, Wikimedia Deutschland e. V.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:

1. Redistributions of source code must retain the above copyright notice,
   this list of conditions and the following disclaimer.
2. Redistributions in binary form must reproduce the above copyright notice,
   this list of conditions and the following disclaimer in the documentation
   and/or other materials provided with the distribution.
3. Neither the name of the copyright holder nor the names of its contributors
   may be used to endorse or promote products derived from this software
   without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
POSSIBILITY OF SUCH DAMAGE.
