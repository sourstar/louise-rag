# The chat model contest

Which chat model answers on the Ask page, and with which prompt.
`tools/measure/chat_eval.py` scores every answer of a run. The run's file
on the node is `/root/measure/score-<night>.md`, and its rules are
measuring.md §10–§13. Each setting is run twice (operator: two runs spot a
drift), with a pilot read before each whole run.

## How to read the table

Each row is one run: one model, one prompt, one seed, the same questions.
Each question has a known work it is about.

| column | what it counts | better |
|---|---|---|
| cites work | share of answers whose citations include the work the question is about | higher |
| numbers found / adding | share of the numbers an answer states that appear in what the search showed the model (or in the question), then how many answers add a number from nowhere | higher / lower |
| names found / adding | the same for names: people, places, titles | higher / lower |
| sentences citing | sentences that carry a citation [n]; a count, not a score | — |
| supported | share of citing sentences whose names, numbers and quoted spans are all found in the sources they cite (ALCE's citation precision, at the level of names and numbers) | higher |
| spans found | share of quoted spans ("…") found word for word in the cited source | higher |
| answers w/ unsupported | answers (of the run's 50) with at least one citing sentence its source does not support | lower |
| uncited unsupported | answers (of the run's 50) stating, without a citation, something no source says | lower |
| median s | median answer time with the model warm | lower |

Above every row, the run's search line: "every search found the work" is
the share of questions where the work was among the search's results, and
MRR is the mean of 1 / its rank. The model can cite only what search found,
so this caps "cites work".

The noise: two seeds of the same setting differ by up to 0.10 on "cites
work". A difference between models or prompts counts only when both runs
of one setting beat both runs of the other.

## 2026-11-07, night b (search on every leg)

Ran 23:16–01:53, beside wikidata's Nemotron embedding. Every run answered
the same 50 questions (n = 50 in every run, score-nightb.md lines 186–198). The "new" prompt
adds two rules to Ask's SYSTEM (ec21999): cite each source on its own, and
quote a title exactly as its source gives it. Search, on every run: every
search found the work **0.86**, MRR 0.479 (0.67 on the earlier search file,
which ran with the text legs only).

| run | cites work | numbers found / adding | names found / adding | sentences citing | supported | spans found | answers w/ unsupported | uncited unsupported | median s |
|---|---|---|---|---|---|---|---|---|---|
| qwen3-8b old s1 | 0.80 | 1.00 / 0 | 0.93 / 18 | 162 | 0.72 | 0.68 | 28 | 10 | 29.3* |
| qwen3-8b old s2 | 0.80 | 1.00 / 0 | 0.91 / 13 | 155 | 0.74 | 0.72 | 24 | 8 | 6.3 |
| qwen3-8b new s1 | 0.84 | 1.00 / 0 | 0.94 / 16 | 175 | 0.72 | 0.78 | 26 | 7 | 8.1 |
| qwen3-8b new s2 | 0.80 | 0.98 / 1 | 0.92 / 17 | 176 | 0.72 | 0.79 | 27 | 11 | 8.1 |
| qwen3.5-9b-q4 old s1 | 0.72 | 1.00 / 0 | 0.91 / 27 | 292 | 0.73 | 0.58 | 40 | 12 | 9.9 |
| qwen3.5-9b-q4 old s2 | 0.82 | 0.98 / 1 | 0.92 / 22 | 311 | 0.76 | 0.69 | 35 | 9 | 9.6 |
| qwen3.5-9b-q4 new s1 | 0.78 | 1.00 / 0 | 0.94 / 19 | 378 | **0.82** | 0.74 | **32** | 8 | 9.8 |
| qwen3.5-9b-q4 new s2 | 0.76 | 1.00 / 0 | 0.94 / 20 | 391 | **0.85** | 0.71 | **29** | 7 | 7.3 |

\* That run crossed the night's restarts: 19.24 s of its median sits in a
column that is 0 in every other run.

**Reading.**
- On qwen3.5-9b-q4 the new prompt lifts "supported" from 0.73 / 0.76 to
  0.82 / 0.85. It lowers answers with an unsupported sentence from 40 / 35
  of 50 (80 % / 70 %) to 32 / 29 (64 % / 58 %), and raises spans found from
  0.58 / 0.69 to 0.74 / 0.71. Per answer it still trails qwen3-8b
  (24–28 of 50) because it writes about twice as many citing sentences, so
  each answer has more chances to hold one unsupported sentence. Per
  sentence ("supported") it is ahead: 0.82–0.85 against 0.72–0.74.
  Both new runs beat both old ones on "supported", so it counts.
- On qwen3-8b the new prompt changes nothing beyond the seeds' spread.
- qwen3.5-9b writes about twice as many citing sentences (292–391 against
  155–176), and is a few seconds slower.
- Neither model makes up numbers (0–1 answers a run). Names are where
  they add things: 13–27 answers a run.

**State.** The new prompt is live (ec21999, in every deploy since
2026-11-06). qwen3.5-9b-q4 stays Ask's model: put to the operator, with
"keep" as the default. Open in the checker: an answer that repeats a
sentence in a loop is not flagged, and the language guess for a
mixed-language answer is unreliable.
