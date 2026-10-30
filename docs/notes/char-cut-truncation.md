# The platform cuts long inputs by characters, not to `ctx` tokens

*Found 2026-10-18 while checking the live collections for the B70 softmax bug
([gpu-silent-wrong-vectors.md](gpu-silent-wrong-vectors.md) §5). A known defect:
it is fixed before anything that depends on it proceeds (CLAUDE.md).*

## 1. The defect

09 §9.3: `ctx` is "the encoder's input window in tokens; a chunk is truncated
to it". The platform holds no tokenizer (invariant 7), so `embeddings._embed_batch`
cuts every input to a character budget before sending it: `ctx × 4`, lower
for dense scripts (`_cut`; a pair keeps its title and cuts the abstract). For
Latin text that budget is 330–540 tokens at ctx 512 and the window it lands on
varies with the text, so an input longer than the budget reaches the encoder
SHORTER than `ctx` tokens. Its vector is the vector of another text than the
space declares. Two producers that truncate to `ctx` tokens, as the
specification says, cannot be reproduced on those inputs.

## 2. Evidence

- **Pubmed** (MedCPT, ctx 512, tolerance 0.999914). `vectors-sample`, seed
  `aa7b98d0…`, fails 3 of its 68 sampled parts: 74 of 1,088 chunks under the
  tolerance, all of them NCBI's imported vectors (`genesis:medcpt-pubmed`),
  all long abstracts (median 2,255 characters; 1,183 for the imported ones
  that pass). Cosines 0.95–0.9999, the same on the card and on the CPU. With
  the cut disabled, the encoder truncating at its own 512 tokens as NCBI's
  `max_length=512` does, **73 of the 74 reproduce**, and 18 vectors embedded
  HERE fail instead: they were made with the cut.
- **Wikipedia** (bge-m3, ctx 1,024, tolerance 0.95). 2026-09-14
  ([wikipedia-two-layers.md](wikipedia-two-layers.md)): the archive's rows
  past the budget reproduce at 0.961–0.999 through the platform, and at
  1.0000 when cut at exactly 1,024 tokens. The tolerance of 0.95 hid it.
- Pubmed's earlier pass (2026-08, STATUS) was at tolerance 0.95, which hid it
  the same way. The tolerance measured for the v6 space exposed it.

## 3. Reach

Every collection with inputs longer than its budget, and in two ways: the
archive vectors it holds fail `vectors-sample` (pubmed now: the collection
fails its own plan), and the vectors it embedded locally for those inputs are
not the function its space names. The first step of the fix measures, per
collection, the share of chunks over the budget (pubmed's long abstracts;
wikipedia's archive rows over ~4,000 characters and every layer-2 CDC piece
over 1,024 tokens, up to 7,103; gutenberg and normattiva to be counted).

## 4. The fix — approved 2026-10-18, built

The one place that holds the tokenizer cuts: the encoder. In a space that
cuts by tokens, Louise sends a VeGa encoder every input WHOLE, and the tag
names the space's `ctx` as its `window` (vega.md §9, "The window"; 09 §9.3
amended). The window is inside the receipt's input digest.

**The cut is the SPACE's, not the platform's** (corrected the same day, after
the rollout). The first deploy cut every space by tokens, and gutenberg's run
failed its re-check on start: its calibration reference was made through the
character cut, and a calibration text cut to exactly 8,192 tokens is another
input — which llama.cpp also refused, since it embeds at most
`context_size − 1` tokens. How an input is cut is part of the function a
space's vectors are of, like its input form. So `EmbedRecipe.cut` comes from
the Codex's space alone (`characters` unless the space declares `tokens`),
every live Codex stays on the character cut it was made with, and each
collection's next Codex — wikipedia's parser-5 switch first — declares the
token cut, its references made through it, its llama.cpp models' context
above the window (the wrapper refuses a window that is not).

VeGa's wrapper cuts as its package declares:

- `tokenizer` (llama.cpp: bge-m3, Qwen3, MedCPT's query side, granite), the
  wrapper with the backend's own `TokenizeString`/`Detokenize`: the first
  `window − specials` tokens, the special tokens counted at load, sent as the
  longest prefix of the input whose tokens are exactly those, else as their
  pieces read back (SentencePiece normalises its pieces, so the prefix comes
  first). Measured on bge-m3's tokenizer over the 512 calibration texts: 622
  of 622 over-long inputs cut exactly at windows of 128, 512 and 1,024. The
  answer is refused if the backend reports another count. llama.cpp embeds
  whatever it is given and cuts nothing.
  *Amended 2026-10-30:* a text whose UTF-8 bytes keep it under the window is
  sent whole without being counted (`token_bound`). Its bound is the longest
  of its NFC/NFD/NFKC/NFKD forms in bytes, plus one, because every token covers
  at least a byte of the normalised text. The count cost 10.7 % of bge-m3's
  `embedding()` on wikipedia's chunks (München's py-spy). The text sent is the
  same, so its vector is too. The check on the answer becomes "not over the
  window". An answer over the window means the bound was beaten: the input is
  then cut by its tokens and asked again, so a wrong bound costs a request,
  never a vector.
- `context_size` (`louise-python`: MedCPT's article side, DAR), the task cuts
  at its load-time context size — a pair longest-first, as NCBI's
  `max_length=512` — and the window must equal it. Measured on the node: every
  Python model's `context_size` is its space's `ctx` (512, 8,192).

The character cut stays only for an encoder that is not behind VeGa. An input
that cannot be cut exactly is refused as itself (an error on its document,
never a deferral retried for ever).

**Deploy order.** The wrappers first (`build_vega_package.sh`, `CUTS=context_size`
for `vega-louise-python`), then Louise: the wrapper before this one refuses a
tag with a key it does not know (a deferral, which pauses embedding). Each
wrapper rebuild is a new tree, so a new run per model, which the calibration
gate validates before a vector is written.

## 5. What each collection needs after it

The calibration reference vectors were made through the same cut, so a Codex
whose long calibration items were cut names reference vectors of other texts.
Counted on the node (the items longer than the character budget):

| collection | ctx | tolerance | calibration items cut | local vectors to re-embed |
|---|---|---|---|---|
| wikipedia | 1,024 | 0.95 | 181 of 512 (35 %) | layer 2's over-budget pieces; the archive's 144 M stay |
| pubmed | 512 | 0.999914 | 57 of 512 (11 %) | its own (non-NCBI) vectors of over-budget abstracts |
| gutenberg | 8,192 | 0.95 | 10 of 512 (2 %) | only the few non-Latin chunks over 8,192 tokens, which LocalAI refused and Louise cut shorter (1609aa9). Its chunker is `fixed{1000, 120}` cutting at 4,000 CHARACTERS, kept byte-identical to the legacy `book` cut so its vectors carried (recipe, preflight item 2): no chunk ever reached the old budget. The tolerance stays 0.95 by decision: no second endpoint to measure one (one Arc, one model server; preflight item 5) |
| normattiva | 8,192 | 0.95 | 10 of 512 (2 %) | the same |

A corrected Codex for each: version 8, its space declaring `cut = "tokens"`
(09 §9.3, *amended 2026-10-18*) — the cut is the function's, so it is INSIDE
`space_id` and the corrected Codex is another space — the reference recomputed
through the fixed path by `hold collection create`, and the vector table
carried by amendment A1e (02 §2.4, `louise dev carry --recut`): the rows the
character cut left whole and an archive's stay, and only the local vectors
whose input was cut are embedded again. Wikipedia's comes with its parser-5 switch (wikipedia-qids.md §3),
with its tolerance then measured across devices. Pubmed's then passes its
`vectors-sample` again (73 of its 74 misses reproduce). On the node, with the
operator: the counts of local vectors to re-embed are measured first.

**Pubmed, measured 2026-10-18** (read-only, 24 s in the worker container; a
seeded sample of 2,000 local documents): 39,993,653 vectors, 35,417,887 of
them NCBI's (a `chunk_verification` row each), so ~4.58 M local (11.4 % of
the embedded documents in the sample). 340 of the 2,000 local chunks, 17 %
(95 % CI 15.4–18.7 %), had a title + abstract over the 2,048-character
budget (median 2,313 characters): **~778,000 local vectors to re-embed**
(~705–855 k). The imported 35.4 M stay: they were cut by NCBI at 512 tokens,
as the corrected Codex cuts. One chunk per document, so at the 23–41
docs/s pubmed embedded in September, about one night (quiet hours).
