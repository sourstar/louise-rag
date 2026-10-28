# Which translator: MADLAD or a newer model? (2026-10-10)

*Operator, 2026-10-10: "Do not reinvent the wheel: if there is already a better
model for translations let's use that: search in LocalAI, our first choice, then
in Hugging Face." Measured on the node, read-only for Louise; no product code or
config changed. Scripts: `tools/measure/translator_choice_{build,search,score,reading}.py`;
node side `/root/xlate-choice/` (`xlate.py`, `run2.sh`/`run3.sh`, `go*.sh`, `out/`).
Local copies and raw outputs: `tools/measure/out/xlate_choice/`.*

**Built 2026-10-12 (operator: "do it"):** `[translate] reading` in
platform.toml (07 §7.8) sends what is read in Latin and Ancient Greek to
Index-Translate-9B (app/translate.py `translate_reading`, the template
measured here); everything else, and every search, stays on MADLAD.

**Complete (2026-10-11).** The operator's GPU pause stopped the first run
before Index-Translate-9B and TranslateGemma-12B; both ran on 2026-10-10/11
(`go4.sh`, `go5.sh`), and all six candidates are scored on the same sets.

## Answer

- **Queries: keep MADLAD.** No candidate moves retrieval. Every one is within
  ±0.006 MRR of MADLAD on the 451 descriptive queries (paired sign tests
  p ≥ 0.91), and within one or two of the 39 titles. Only TranslateGemma-12B
  is faster (2.1 s per query against MADLAD's 3.2 s); it gains nothing, and
  its weights are under the Gemma terms. Unlike Hy-MT2, MADLAD covers
  Finnish, one of gutenberg's four main languages.
- **Reading, modern languages: MADLAD is as good as all but one.** German
  and Finnish into English: MADLAD chrF 57.0 / 51.7, Index-Translate-9B
  59.6 / 54.2, EuroLLM-9B 57.2 / 52.4. MADLAD takes 5.8 s per passage,
  Index-Translate 31 s.
- **Reading, Latin and Greek: Index-Translate-9B is the best of the small
  models.** Latin 51.6, Ancient Greek 51.9, Modern Greek 47.6, against
  34.4 / 47.3 / 43.5 for MADLAD. That is Gemma 3 27B's level (52.5 / 52.0 /
  48.6) at 10.6 GB instead of 17–18, Apache-2.0. EuroLLM-9B (50.4 / 49.3 /
  46.5) is the cheaper runner-up at 6.2 GB.
- **MADLAD cannot be retired on what was measured.** Nothing beats it for
  queries, and no candidate is as cheap for reading: 3B, one batched call.
  A Latin and Greek reading model would sit beside it, not replace it.

## Candidates

| model | where | file | languages (card) | how it is asked |
|---|---|---|---|---|
| MADLAD-400-3B-MT (today) | `louise-translate-madlad` | bf16, torch XPU | 400+ | one `/v1/completions` per batch, the JSON `{"items": [...]}` (app/translate.py) |
| Hy-MT2-1.8B | LocalAI gallery `hy-mt2-1.8b-q8` | Q8_0, 1.9 GB | 33, **no Finnish** | chat: "Translate the following text into {lang}. Note that you should only output the translated result without any additional explanation:\n\n{text}" |
| Hy-MT2-7B | gallery `hy-mt2-7b-q4`, which **resolves to the Q8_0 file** | Q8_0, 8.0 GB | 33, no Finnish | the same |
| EuroLLM-9B-Instruct | gallery `eurollm-9b-instruct` | Q4_K_M, 5.6 GB | EU 24 + zh, ja, ko, … | chat: "Translate the following {src} source text to {tgt}:\n{src}: {text} \n{tgt}: " |
| Index-Translate-9B | gallery `index-translate-9b` (Apache-2.0) | Q8_0, 9.8 GB | 150 | `/v1/completions`: a Qwen3.5 turn ending in an EMPTY think block, with the card's prompt "请将以下文本翻译为{语言}，直接输出翻译结果，不要进行任何解释。\n\n{text}" |
| TranslateGemma-12B | **Hugging Face**, not in the gallery: `mradermacher/translategemma-12b-it-GGUF` | Q4_K_M, 7.3 GB, sha256 `b7aac4b4…a528` | 55, Latin included | `/v1/completions`, the GGUF's own template rendered by the caller: "You are a professional {src} ({code}) to {tgt} ({code}) translator. …" (the template wants a structured content item that LocalAI cannot pass) |

**Not run:**
- From the gallery:
  - `yanoljanext-rosetta-12b/27b` (Gemma 3 translation fine-tunes);
  - `index-translate-35b-a3b`: about 20 GB, it does not fit beside Louise's
    models;
  - the `-q4`/`-q6` variants of Hy-MT2.
- From Hugging Face: TranslateGemma 4B and 27B.

**Conditions:**
- Greedy decoding everywhere (temperature 0, top_k 1, seed 0).
- Each candidate was alone on the card, beside Louise's pinned models and
  gutenberg's live embedding.
- Each load was checked from the driver: the model plus 2 GiB free.
- Every request was guarded by `/root/GPU_PAUSE` and `out/STOP`.

## Query translation (search)

**What ran:**
- The live re-check's queries: 451 descriptive and 39 titles.
- Each query was translated into the main languages and CJK (en, fr, fi, de,
  zh, ja, ko), minus its own language. All targets of one query were in flight
  at once, as a search sends them.
- Text leg: HEAD's plan, i.e. `LanguagePlan.variants` with gutenberg's main
  languages and the query's own language searched as typed.
- Vector leg: the re-check's, the same for every translator, since it embeds
  the query as typed.
- Fusion: `retrieval._merge_rrf`, scored at the work level, before the
  reranker.

**Check:** `madlad-recheck` (the translations the re-check captured) reproduces
the shipped "rule (label)" row of one-language-per-work.md (0.245 / 0.441 /
0.630; titles 0.484 / 0.821 / 0.949). The harness is therefore the product's.

| translator | descriptive MRR | R@10 | R@50 | vs MADLAD | titles MRR | R@10 | R@50 | vs MADLAD | s / query median (p90) |
|---|---:|---:|---:|---|---:|---:|---:|---|---|
| MADLAD | 0.245 | 0.441 | 0.630 | | 0.484 | 0.821 | 0.923 | | 3.21 (4.70) |
| Hy-MT2-1.8B | 0.250 | 0.457 | 0.639 | +41/−39 | 0.504 | 0.769 | 0.923 | +7/−5 | 3.34 (4.71) |
| Hy-MT2-7B | 0.247 | 0.455 | 0.641 | +35/−36 | 0.507 | 0.769 | 0.949 | +6/−6 | 6.57 (9.64) |
| EuroLLM-9B | 0.247 | 0.448 | 0.630 | +31/−30 | 0.490 | 0.821 | 0.923 | +5/−2 | 6.47 (8.52) |
| Index-Translate-9B | 0.247 | 0.443 | 0.630 | +30/−30 | 0.489 | 0.795 | 0.974 | +5/−4 | 15.19 (21.60) |
| TranslateGemma-12B | 0.251 | 0.446 | 0.623 | +37/−38 | 0.482 | 0.821 | 0.923 | +3/−4 | **2.09** (2.57) |

All six in one run (`score_6models.out`, 2026-10-11); the titles of the first
four moved by a query or two from the first run's figures.

- **Where translation matters most, no candidate helps.** The subset is
  non-English queries whose work has no edition in their language (110).
  - MADLAD: 0.191 / R@50 0.591.
  - Hy-MT2-1.8B: 0.193 / 0.600.
  - EuroLLM: 0.184 / 0.609.
  - Index-Translate-9B: 0.190 / 0.609.
  - TranslateGemma-12B: 0.204 / 0.591 (+16/−13, p 0.71).
  - Hy-MT2-7B: 0.177 / 0.600. It loses, +7/−19 (p 0.03), the only
    significant difference in the table.
- **Why nobody wins.** The vector leg carries most of the cross-language
  recall. The text leg needs only the content words in the four main
  languages, and every translator gets those right.
- **Hy-MT2 does not know Finnish.** The product would ask it nothing for `fi`
  and search the Finnish index as typed, which is what the harness did.
- **EuroLLM runs on.** In 21 of its 490 queries (4.3 %), one translation is
  more than 3× the query's length: an appended "Note: …", an explanation or
  a repetition. Hy-MT2 and MADLAD each have 2.
- **Time.**
  - MADLAD's median was measured in the previous run (08:50–09:10), under
    the same conditions: gutenberg embedding, other candidates unloaded.
  - The four targets plus CJK take 3.2 s, against 4.0 s for the old 19
    (one-language-per-work.md, "Time"), so that open timing is now done.

## Reading (Ask page: entries, answers)

**Sets:**
- Classical: classical-translation.md's eval set.
- Modern: `evalset_modern.json`, German and Finnish passages with
  public-domain English references.
- Each candidate ran every other passage (the "half" subset). MADLAD,
  Hy-MT2-1.8B and the LLMs of 2026-10-09 are scored on the same half.

Scores are chrF (sacrebleu 2.4.3, corpus level), each passage into English:

| model | la (10) | grc (10) | el (5) | de (10) | fi (10) | s / passage (classical · modern) | tok/s |
|---|---:|---:|---:|---:|---:|---|---:|
| MADLAD | 34.4 | 47.3 | 43.5 | 57.0 | 51.7 | 2.7 · 5.8 | |
| Hy-MT2-1.8B | 39.4 | 35.7 | 38.7 | 48.2 | 40.4 | 20.7 · 17.7 | 16 |
| Hy-MT2-7B | 48.4 | 49.4 | 45.6 | 56.0 | 49.7 | 52.0 · 38.5 | 7 |
| EuroLLM-9B | **50.4** | 49.3 | 46.5 | **57.2** | **52.4** | 41.9 · 32.6 | 10 |
| Qwen3-30B-A3B (2026-10-09) | 50.4 | 52.9 | 50.3 | | | 94.2 | 4.5 |
| Gemma 3 27B (2026-10-09) | **52.5** | 52.0 | 48.6 | | | 37.5 | 11 |
| Index-Translate-9B | **51.6** | **51.9** | 47.6 | **59.6** | **54.2** | 39.6 · 31.4 | |
| TranslateGemma-12B | 49.0 | 40.9 | 45.6 | 55.6 | 51.6 | 48.7 · 8.5 | |

TranslateGemma's Ancient Greek runs long (length ratio 1.09, the only one
above 1): it adds rather than translates.

BLEU follows chrF; it is in `reading_scores.out`. On the full sets, MADLAD
scores la 35.8, de 58.6, fi 51.3, and Hy-MT2-1.8B 39.5 / 48.5 / 41.0.

### Known difference: the answer box and the reader (2026-10-22, left as it is)

The answer box is WRITTEN in the chosen language by the chat model
(`retrieval.build_answer_messages`, `answer_lang`: "write your whole answer
in the language with ISO code …"); the entries and the reader are
TRANSLATED by the translation model. Two models, so a word taken from the
sources can come out two ways: the operator asked in Italian and read
"barbican" in the answer (kept from the English source) and "barbacane" in
the reader. Left as it is (operator, 2026-10-22: "Leave it but document
it"; "barbacane" is a term of ancient military architecture, so a chat
model not knowing it is acceptable — the translation model is the one
trained for such terms). The two remedies considered, if it is reopened: a sharper answer
instruction (translate every word taken from the sources, keeping only
personal names, titles and quotations), at no extra cost; or the answer
post-translated by the translation model, one call more per answer and the
[n] citations at risk (the page already falls back to the original when
they move).

Within the translation model, one passage now gets one translation
(52b1433): an entry's visible lines are translated first, then replaced by
the reader's whole-paragraph translation when the queue brings it, so the
entry and the reader agree and the rest of the passage is not translated
on its own.

## Footprint

| model | VRAM (driver) | host RSS | cold, from disk / from page cache | warm, one short request |
|---|---|---|---|---|
| MADLAD (model-profile.md) | 6,394 → 7,160 MiB | 3,561 MiB | 16.7–38.0 s | a 1-item batch 7.1 s beside the worker |
| Hy-MT2-1.8B | 2,622 MiB | 924 MiB at load, **9,215 MiB after its hour** | 32–37 s (first load of the day) | 0.19–0.36 s |
| Hy-MT2-7B (Q8) | 8,621–8,639 MiB | 1,010–1,221 MiB | 37–48 s / 8.6–10.0 s | 0.5–0.6 s |
| EuroLLM-9B (Q4_K_M) | 6,185–6,197 MiB | 718–735 MiB | 16.5 s / 7.0 s | 0.38–0.47 s |
| Index-Translate-9B (Q8) | 10,601 MiB, 12,363 under load | 1,637–1,882 MiB | 61.1 s / 9.8 s; 24.3 s | 0.39–0.44 s |
| TranslateGemma-12B (Q4_K_M) | 9,018–9,065 MiB | 1,300–2,150 MiB | 18.1 s / 6.3–9.0 s | 0.11–0.75 s |

The 9 GB RSS of Hy-MT2-1.8B appeared once, over 490 queries and 90 passages,
with `cache_ram:0`. Neither the 7B (1.0 GB) nor EuroLLM (0.7 GB) showed it.

## What went wrong on the way

1. **EuroLLM's backend went to garbage and kept answering 200 OK.**
   - After about 1 h 10 min (query 290 of 490), every translation became
     random multilingual text, and each query took 30–50 s instead of 7 s.
   - A restart fixed it. The garbled lines (kept in
     `out/q_eurollm-9b-instruct.garbled-1300.jsonl`) were dropped and
     re-run.
   - A llama.cpp SYCL translator in the product would need an output sanity
     check (length ratio, script). MADLAD did not show this here.
2. **Index-Translate ignored "no thinking".**
   - LocalAI v4.11 drops the gallery YAML's `chat_template_kwargs:
     enable_thinking: false`, and the request's as well. The model reasoned,
     and answered "" with `finish_reason: length`.
   - The workaround: `/v1/completions` with the GGUF's template rendered with
     `enable_thinking=false`, i.e. an empty `<think>\n\n</think>` block.
   - It is in `xlate.py`, and it works: 490 queries and 45 passages, none
     empty.
3. **Gallery names do not mean what they say.** `hy-mt2-7b-q4` installs the
   Q8_0 file (`resolved_variant: hy-mt2-7b-q8`).
4. **The previous runner kept loading models after its agent stopped.** The
   runners now:
   - check `GPU_PAUSE` and `STOP` before every request;
   - run a watcher that kills the run and unloads the model when either
     appears, when the card's free memory falls under 1 GiB, or when host
     MemAvailable plus reclaimable ARC falls under 4 GiB;
   - evict no one else's model.
5. **TranslateGemma would not load, and the run "succeeded" anyway.**
   - llama.cpp refused the GGUF's own chat template at init ("for example:
     --no-jinja --chat-template chatml"). Fixed with `options: use_jinja:false`
     in the YAML: the caller renders the template anyway.
   - The runner's `rc=0` covered 490 queries that were all HTTP 503s, written
     as results with an `errors` field. Its retries then kept LocalAI's
     load cooldown (which grows after each failure) running; one load after
     the cooldown expired worked. The failed outputs are kept in
     `out/failed-jinja/` and `out/failed-cooldown/`.
   - Check a run's errors, not its exit code.

## Recommendation

1. **Keep MADLAD as TRANSLATE_URL's model** for queries and for the Ask page.
   Nothing measured beats it in either job at its cost.
2. **Do not adopt Hy-MT2.** It has no Finnish, and its 1.8B reads worse than
   MADLAD everywhere except Latin.
3. **If Latin and Greek reading matter**, Index-Translate-9B (gallery, Q8_0,
   10.6 GB, Apache-2.0) is the model: Gemma 3 27B's quality on la / grc / el
   at 60 % of its VRAM, and the best German and Finnish reader too. It would
   be a second, reading-only model, used where the source is `la`, `grc` or
   `el`, loaded on demand (not pinned: it does not fit beside every pinned
   model with the reclaimer's margin). EuroLLM-9B (6.2 GB) is the fallback
   if the card is short.
4. **Do not adopt TranslateGemma-12B.** It is the fastest query translator
   and gains nothing for it; it reads Ancient Greek worse than MADLAD, and
   its weights carry the Gemma terms.
5. **Code changes if an LLM translator is ever used** (none now):
   - `app/translate.py` speaks the louise-madlad batch shape. An llama.cpp
     translator needs one request per item in that model's prompt shape (the
     table above), the items of a batch sent concurrently up to the slot
     count, and the language list from config, since `{"languages": true}`
     is MADLAD's backend's own.
   - Index-Translate and TranslateGemma need `/v1/completions` with their
     templates rendered by the client.
   - EuroLLM needs its answer cut at the first line or note.
   - Every one needs the sanity check of item 1 above.
   - A new `[translate] model / shape` config key: ask first.

## Why MADLAD runs on torch, not llama.cpp (2026-10-27)

*Operator: "why we went for the python path instead of the obvious
llamacpp". It costs a 17–38 s cold load and 7.2 GB on the card, against
4–8 s for the llama.cpp models (model-profile.md).*

- **The decision is on record, in the code**
  (`deploy/madlad-translate/server.py`'s docstring, node, 2026-09-18). A
  MADLAD GGUF loaded in llama-server, but its encoder never ran: the server
  wrote `<2cs><2cs>…`, and Ollama hung. Only llama.cpp's completion tool
  translated correctly, one text per process with 18 s of load. So MADLAD
  went on PyTorch XPU. On 2026-10-03 that server's model code became a
  LocalAI Python backend unchanged (rage-over-localai.md), and it now runs
  on louise-python. Its generation settings are greedy (`BEAMS` 1),
  96-token sentence segments, batches of 32, and
  `max_new = min(512, 2 × longest + 16)`.
- **llama.cpp's library runs T5**, MADLAD's architecture
  (`llama_model_has_encoder`, `llama_encode` in `include/llama.h`), but
  only its `tools/completion` CLI calls it.
- **Its server does not.** At c0fd786 (the commit our backend builds),
  `tools/server` never calls `llama_encode`, and neither does LocalAI's
  `backend/cpp/llama-cpp/grpc-server.cpp` at v4.12.1, which is built from
  that server. So no LocalAI llama.cpp backend can serve an encoder-decoder
  model, and the "obvious" path does not exist through LocalAI.
- **The alternatives, if the load time matters:**
  - MADLAD on OpenVINO through optimum-intel's seq2seq export. It would run
    in the same louise-python backend, where MedCPT already loads on
    OpenVINO in 2.4–3.6 s.
  - Encoder-decoder support patched into our llama.cpp backend (a P3).
    That is a larger change to carry, and the place for it is upstream
    llama.cpp's server.
    - **Upstream status, checked 2026-10-27:**
      - The T5 author's own server branch (fairydreaming `t5-server`,
        Dec 2024) was never merged ("not very optimal").
      - Issue #26565 (Aug 2026) was closed as stale with no maintainer
        reply and no PR. It names the fix: `update_slots()` has no
        `llama_model_has_encoder()` branch.
      - Its prototype encodes the prompt, turns prompt caching off, and
        decodes from the decoder-start token, one request at a time.
      - `llama_context::cross` is one buffer per context, so it holds only
        with a single slot. Our models already run `parallel: 1`.
      - Only `/completions` with a raw prompt is right for T5. A chat
        template corrupts its input.
    - **As our P3:** a llama.cpp patch carried through LocalAI's
      `backend/cpp/llama-cpp/patches/` (as upstream carries its own two).
      - Cost: a rebase at every llama.cpp bump of its hottest function,
        and the client sends one request per target language in place of
        today's one batched call. Whether that is faster than torch's 3.2 s
        per query is unmeasured.
  - CTranslate2, the standard runtime for MADLAD/T5. It has no Intel GPU
    path (CUDA and CPU only).
  - Whichever runtime, its output is compared with today's on the 451 + 39
    queries before it serves. Translation is never hashed into a space, so
    the change needs no recalibration.

## Reproduce

**On the node, in `/root/xlate-choice`:**
- `setsid nohup ./go4.sh > out/go4.log 2>&1 &` runs each model through
  `run3.sh MODEL NEED_MIB COLD CMD`. That does the fit check, the cold and
  warm timings, and starts the watcher; then `xlate.py reading|queries`
  runs, resumable.
- `touch out/STOP` ends everything at once and unloads the model.

**Locally, from the repo root (Docker, `louise-rag:dev`):**
- `python tools/measure/translator_choice_build.py tools/measure/out/onelang_live/live_results.jsonl tools/measure/out/xlate_choice tools/measure/translator_choice_search.py OUT.py madlad hy-mt2-1.8b-q8 …`
- `sh tools/measure/one_language_rep_run.sh OUT.py > search.jsonl`: read-only
  container on the node, text index only, about 2 min.
- `python tools/measure/translator_choice_score.py --old tools/measure/out/onelang --live tools/measure/out/onelang_live --results search.jsonl --times tools/measure/out/xlate_choice`
- Reading: `python tools/measure/translator_choice_reading.py tools/measure/out/xlate_choice/reading/old tools/measure/out/xlate_choice/reading`
  (python:3.12-slim + sacrebleu 2.4.3).

## Waves: the simpler MADLAD-only step (proposed 2026-10-27, deferred)

*Operator, 2026-10-27: "No, keep things as they are, record the llama.cpp
improvement, I will do it in a separated session." MADLAD stays on torch.
Nothing below is built.*

*That session: 2026-10-27 night (operator: "Review the proposed improvings of
llama.cpp to use madlad model, without breaking the existing interfaces …
implement and test the code, I give you the exclusive use of the CPU/GPU
tonight"). BUILT as P4 (localai-bugs.md): the wave scheduler in the server,
under P3's guard, plus Louise's client side — app/translate.py's t5 shape
(one raw `<2xx> segment` prompt per segment, the torch backend's cut, a
call's segments sent at once, the clean-up; the shape determined from the
server, never configured). The model runs on its OWN backend
(`louise-sycl-f16-llama-cpp-t5`, deploy/vega/stage/models/louise-translate-madlad-gguf.yaml),
so the shared embedder backend was not rebuilt and no embedder became a new
engine. The gates below are run by tools/measure/t5waves.py; their results
are recorded in STATUS.md as they land.*

*The operator asked for a simpler patch than the per-sequence memory
(llama-cpp-encoder-decoder.md §9), one that impacts no other model.
Measured first (localai-bugs.md P3, "On the card"):*
- *one slot: 3.03 s per translation, ~18 s per question;*
- *time per output token is flat in the input length (×1.10 from 7 to 192
  tokens). So decode steps carry a fixed cost, not the cross recompute,
  and batching is the lever, not a cross K/V cache.*

**What it is:** a server-only addition to P3 (a P3b), under the same guard
(architecture `t5` with a decoder, generation tasks only). The library is
not touched.
- MADLAD's model file alone asks for several slots (`parallel:7`). LocalAI's
  backend already turns the unified KV cache on by default
  (`grpc-server.cpp:604`), which several T5 slots need.
- The server never encodes while a slot of the model is generating.
- When none is, every slot waiting to start is encoded in ONE encode call,
  each on its own sequence id. The library already keeps the rows of one
  call tagged per sequence, and the encoder masks sequences apart
  (llama-graph.cpp:496-499).
- Those slots then decode together, one batched step for all, as the
  server does for any model.
- Shared-prefix grouping stays off for these slots: a child would copy its
  parent's decoder state and never encode.
- A short gathering window (a few ms) keeps one question's languages, sent
  at once by the client, in one wave.

**Expected:** ~one translation's time per question (~3 s here), against
~18 s; not measured.
**Size:** ~60–100 lines on top of P3 (an estimate).
**Other models:** no code of theirs changes. Rebuilding the shared backend
still makes every SYCL embedder a new engine, measured and gated once
(invariant 12).

| Option | Pros | Cons |
|---|---|---|
| P3, one slot (status quo of the test) | Each translation alone, reproducible per translation | ~18 s per question: slower than torch |
| **P3b, waves** | Server only, T5 only, small; per-question time like torch's; 9.5–11 s cold against 17–38 s | A translation's bytes depend on its wave's members; waves depend on arrival; the client sends one request per segment (architecture's change) |
| Per-sequence memory (§9 stage 1) | Slots fully independent; upstream's shape | ~900 lines in the library's busiest files |
| Keep torch | Measured, batched, serving | 17–38 s cold, a Python process, 7.2 GB |

**Gates before MADLAD-on-llama.cpp replaces torch:**
1. Retrieval on the 451 + 39 queries, as this note's measurement ran it.
2. Waves' own byte test: the same question twice gives the same bytes
   (llama-cpp-encoder-decoder.md T3).
3. Invariant 12 on the rebuilt backend's embedders.
4. Architecture's client change: one `/v1/completions` per segment, the
   `<2xx>` prompt, and transformers' `clean_up_tokenization_spaces`
   replacements applied to the output.

**Where the work stands (2026-10-27), left in place for that session:**
- The patch: `deploy/localai/patches/0003-llama-cpp-encoder-decoder.patch`.
  It is a LocalAI commit adding
  `backend/cpp/llama-cpp/patches/0003-server-encoder-decoder.patch` on
  llama.cpp c0fd786.
- On the node (all created by this work; nothing loads them unless asked):

  | What | Where | Size |
  |---|---|---|
  | Test backend | `/mnt/ssd5/apps/localai/backends/louise-sycl-f16-llama-cpp-p3test` | 438 MB |
  | Test backend tar | `/mnt/ssd5/apps/localai/backends/.louise-llama-cpp-sycl_f16-p3test.tar` | 282 MB |
  | Model files | `/mnt/ssd5/apps/localai/models/madlad-p3-test.yaml` (bf16), `madlad-p3-test-f16.yaml` (one slot, `parallel:1`, `cache_ram:0`, context 512) | — |
  | GGUFs | `/mnt/ssd5/apps/models/madlad/madlad400-3b-mt-{bf16,f16}.gguf` | 5.5 GB each |
  | Scripts and results | `/root/madlad-gguf`: `t5cmp.py` (compare against torch; `URL#MODEL` for LocalAI), `t10.py` (cost per output token), `cmp-card-f16.jsonl` | 1.6 MB |
  | Pre-P2-rollout backends | `/mnt/ssd5/apps/localai/backends-bak-20261027` | 709 MB |

- On the dev PC:
  - `/tmp/lcpp`, the llama.cpp working tree with the patch (218 MB);
  - `/tmp/t5f`, `/tmp/t5b`, `/tmp/tmp.9KixJbUrfa` (141 MB together);
  - the Docker volume `lcpp-cpu-build`, used for the CPU check.
