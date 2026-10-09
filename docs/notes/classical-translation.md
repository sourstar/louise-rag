# Latin and Greek into English: existing translations, two LLMs, MADLAD (2026-10-09/10)

*Measured on the node 2026-10-09/10, operator-approved. No spec or product code changed. Scripts:
[`tools/measure/classical_*.py`](../../tools/measure/); raw results on the node in
`/root/xlate-classical/out/` (`results.json`, `coverage.json`, `evalset.json`, the
translations). Earlier method: [translate-before-embed.md](translate-before-embed.md).*

**Two runs.**
- *First run, 2026-10-09 evening:* Qwen3-30B's quality and MADLAD. It was cut short by
  two card overcommits (see [Incident](#incident)).
- *Second run, 23:17–00:08, in a window the operator granted:* gutenberg's embedding was
  off. Each model was alone on the card, unpinned, with the load checked from the driver.
  It covered Qwen3-30B retrieval, Gemma 3 27B eval and retrieval, the embedding and the
  scoring.

## Answer

**Per language.**
- **Latin (la).**
  - *Use the existing human translations:* 39 or more of 108 books.
  - *For reading the rest:* Gemma 3 27B gives the best translations (chrF 54.4).
    Qwen3-30B-A3B is close (52.2) and 4× faster. Both are far above MADLAD (35.8).
  - *For retrieval:* do **not** translate before embedding. Even the best LLM
    translation (Qwen3) loses on same-book queries:
    - MRR 0.594 → 0.515;
    - 4 books better, 10 worse.

    It is only a wash on metadata queries (0.422 → 0.441, 6 better, 8 worse). Gemma and
    MADLAD lose on both.
- **Ancient Greek.** gutenberg has almost none: its `el` books are Modern Greek, and the
  ancient texts in PG sit inside bilingual editions, which the collection files under
  `en`. On those bilingual texts Qwen3 (52.9) and Gemma (52.2) are equal and above MADLAD
  (47.5). There is nothing to translate.
- **Greek (el, Modern and Katharevousa).**
  - *Retrieval:* do **not** translate. Every translator makes retrieval worse. On metadata
    queries both LLMs have 1 book better and 14 worse, with MRR 0.500 → 0.30. Qwen3's
    encoder reads Greek natively.
  - *Existing human translations:* 102 or more of 216 books.
  - *Reading quality:* Qwen3 49.8 ≈ Gemma 49.4 > MADLAD 46.0, on a loose reference.

**Shared findings.**
1. **Existing human translations.** At least **39 of 108 Latin books (41 % of the la
   tokens)** and **102 of 216 Greek books (41 % of el)** already have a public-domain
   English version of the same work in the gutenberg collection. That is a lower bound: a
   hand check found 6 more among 40 books the method called uncovered. Project Gutenberg
   holds nothing beyond the collection, because the collection is all of PG (78,282 texts
   in `pg_catalog.csv`, all present).
2. **Speed on the B70, quiet card.**
   - *Qwen3-30B-A3B:* 87 output tokens/s, 5.1 s per ~400-token passage, 19.2 GB. That
     beats the 9.2 tokens/s measured beside the gutenberg worker by nearly ten times.
   - *Gemma 3 27B:* 23 tokens/s, 18.8 s per passage, 17.8 GB.
   - *Translating the books with no English version:* Qwen3 needs ~17 h for la and ~23 h
     for el. Gemma needs ~2.7 days and ~3.5 days.
   - *Card:* neither model fits beside Louise's interactive models.
3. **Translation for retrieval is not worth doing in either language.** The 05 §5.2
   query-side translation remains the cross-lingual path (as
   [translate-before-embed.md](translate-before-embed.md) concluded for MADLAD).

## Existing human translations (coverage)

**Method** (`classical_coverage.py`). Each la or el book is compared with the English
records that share an author with it: the same PG agent id (role creator or dubious), or
the same normalised name or alias. Each match is tagged with how it was found:
- **wikidata**: the book's PG id is a Wikidata edition (P2034) of a work (P629) that has
  another edition with an English PG id. Or the book and the candidate point, through
  PG's "Wikipedia page about this book" links, at the same Wikidata item (sitelinks via
  SPARQL).
- **title**: the titles share words (5-letter prefixes, Greek transliterated, the
  authors' names and stop words removed), with a score ≥ 0.6.
- **llm**: Qwen3-30B was shown the book's title and the author's English titles, and
  picked the same work (titles only).

| | books | covered | tokens (chars/4) | covered tokens | wikidata · title · llm | no same-author English record |
|---|---|---|---|---|---|---|
| la | 108 | **39** | 9.42 M | 3.86 M (41 %) | 22 · 6 · 11 | 44 books, 4.37 M tokens |
| el | 216 | **102** | 12.28 M | 5.04 M (41 %) | 66 · 3 · 33 | 102 books, 6.22 M tokens |

(The brief's 10.5 M / 13.8 M tokens are another count. These are the SHARD bodies' chars/4.)

**Precision**, hand-checked on 30 matched books drawn at random (seed
`precision-30`):
- **27 of 30** (90 %) have a correct English version among their matches.
- All 17 found by wikidata or title are right.
- 10 of 13 found by the llm alone are right. The wrong ones are two Lucian volumes
  (matched to *True History* / *Trips to the Moon*, which are not those volumes) and
  Euripides' *Ion* (matched to *Hecuba and other plays*).
- Single pairs are noisier: *Four Plays of Aeschylus* is listed for the *Eumenides*, and
  *A Simple Soul* for Flaubert's *Herodias*.

**Misses.** Recall was checked on the 40 books first drawn as uncovered. Six of them do
have an English version, so true coverage is higher:
- Catullus (PG 20732, 18867);
- *Rebilius Cruso* (Defoe, PG 521);
- the Didot Homeric Hymns (PG 348);
- Marpoutzoglou's Στοχασμοί (Wilde's *Intentions*, PG 887);
- two Χαλιμά volumes (*Arabian Nights*).

The causes: an author-only title ("Catulli Carmina" loses every word), the llm answering
in prose, and books with no creator.

Matches, for example:

| book | English | how |
|---|---|---|
| la 218 *De Bello Gallico I–IV* | 10657 McDevitte & Bohn | title |
| la 33849 *Confessiones* | 3296 Pusey | wikidata + title |
| la 23306 *Meditationes* | 70091 *Six metaphysical meditations* (1680) | wikidata |
| la 46456 *Mysterium Arcae Boulé* | 10067 *The Mystery of the Boule Cabinet* (the original) | title |
| el 29833–6 Θουκυδίδης (Zervos) | 7142 Crawley | wikidata |
| el 31797 Αμλέτος | 1122 *Hamlet* (the original) | wikidata |
| el 39476 Πολιτεία | 150 / 1497 Jowett | wikidata |
| el 34851 Ειδύλλια | 4775 Theocritus (Lang) | llm |

**Relations (finding only, no spec change).** An original↔translation link has the shape
of a 09 §9.14 edge: `{from, to, type, snapshot}`, where `to` may be a canonical id left
unresolved. Its `type` would be a new name the gutenberg plugin registers, for example
`gutenberg:translation-of`, and no existing kind fits. The gutenberg plugin has no
`relations()` today. The larger problem is that 05 §5.5 wants edges that are "explicit
in the source … not inferred from similarity", and the plugin yields them per document
from the origin alone. PG's metadata never says "this is a translation of ebook N".
The links above come from matching and from Wikidata, so they are inferred.

What the origin does state is the work's Wikipedia page, in 144 of the 324 la/el
records. An edge `gutenberg:about-work → <the Wikipedia article>`, per document, would
be origin-stated and fits §9.14 as it stands. Two records pointing at the same article
then give the link through inbound edges. The la and en Wikipedia articles name the
same work differently, though, so joining them needs Wikidata, which is an outside
source. So either a new **derived** relation kind (with its provenance), or the
two-hop form, with resolution outside the record.

## Translation quality (reference set)

`classical_evalset.py` builds 50 passages of 1,200–3,200 source characters, aligned only
by explicit numbers. The texts are read from gutenberg's own SHARD (dumped from it):

| set | passages | source | reference | alignment |
|---|---|---|---|---|
| la | 8 | Caesar BG V–VII (PG 18837) | McDevitte & Bohn (PG 10657) | book + chapter |
| la | 8 | Augustine *Confessiones* (PG 33849) | Pusey (PG 3296) | numbered sections, books whose counts agree |
| la | 4 | Catullus, Ellis (PG 23294; apparatus removed) | Smithers' prose (PG 20732) | poem number |
| grc | 8 | Galen *Nat. Fac.*, Brock's bilingual edition (PG 43383; Book III's transliterated Greek excluded) | same | book + chapter |
| grc | 8 | Dionysius *De comp. verb.*, Roberts (PG 50212) | same | printed page pairs |
| grc | 4 | Didache, Hitchcock & Brown (PG 42053) | same | chapter |
| el (loose) | 10 | Herodotus I–II, Skalidis' Katharevousa (PG 38055) | Macaulay (PG 2707), from the ancient text | book + chapter |

One Augustine item (VIII.7) turned out to be off by one paragraph. It is kept, and it
costs every model the same.

The LLM is asked through LocalAI's chat endpoint, greedy (temperature 0, top_k 1,
seed 0). The instruction is in the request: "Translate the following {Latin|Ancient
Greek|Greek} text into English. Give a faithful and complete translation and nothing
else…". MADLAD is called through `translate.translate`, as Louise calls it. Scores are
from sacrebleu 2.4.3 (corpus chrF and BLEU):

| | la chrF / BLEU | grc chrF / BLEU | el (loose) chrF / BLEU |
|---|---|---|---|
| **gemma-3-27b-it** | **54.4 / 21.5** | 52.2 / 18.8 | 49.4 / 12.8 |
| **qwen3-30b-a3b** | 52.2 / 17.7 | **52.9** / 18.1 | **49.8** / 12.6 |
| madlad (Louise's) | 35.8 / 6.5 | 47.5 / 16.7 | 46.0 / 12.0 |

By work, chrF for gemma / qwen / madlad:
- Caesar 56.1 / 52.1 / 35.5;
- Augustine 56.8 / 55.8 / 39.5;
- Catullus 45.6 / 44.5 / 28.4;
- Galen 52.2 / 53.6 / 45.6;
- Dionysius 47.4 / 47.8 / 42.6;
- Didache 61.4 / 61.3 / 61.6.

Passage by passage:
- *Gemma against Qwen:* Gemma is better on 17/20 la passages, but only 5/20 grc and 4/10
  el.
- *Against MADLAD:* Gemma is better on 20/20, 18/20 and 10/10. Qwen is better on 20/20,
  17/20 and 8/10.
- *Tokens:* Gemma's tokenizer reads polytonic Greek in about a third of Qwen's prompt
  tokens.

Examples (first words):

| | text |
|---|---|
| la BG 5.55, source | *Treveri vero atque Indutiomarus totius hiemis nullum tempus intermiserunt, quin trans Rhenum legatos mitterent…* |
| reference | But the Treviri and Indutiomarus let no part of the entire winter pass without sending ambassadors across the Rhine… |
| qwen | The Treveri and Indutiomarus did not cease throughout the entire winter from sending envoys across the Rhine, stirring up the tribes, promising money… |
| madlad | The 19th century saw the development of a new type of bridge, the bridge of the Rhine, which was designed to connect the Rhine with the Rhine… |
| grc Dionysius p. 198 | *…ἀλλ’ οὐχ ἥ γε Ἰσοκράτους καὶ τῶν ἐκείνου γνωρίμων αἵρεσις ὁμοία ταύταις ἦν…* |
| qwen | Yet Isocrates’ choice and that of his followers were not similar to these… |
| madlad | d’ as in dialogues grace, the d’ as in words enagonies need. But the heresy of Isocrates… |
| el Hdt. 1.123 | *Ο Κύρος, φθάσας εις ανδρικήν ηλικίαν ήτο ο ανδρειότατος…* |
| qwen | Cyrus, having reached manhood, was the most courageous and beloved of his peers… |
| madlad | Cyrus, when he had reached the age of manhood, was the most valiant and beloved of his peers… |

## Speed and memory on the B70

**Qwen3-30B-A3B-Instruct-2507 Q4_K_M.**
- *Memory:* 19,090–19,162 MiB on the card at context 8192 (LocalAI `size_vram`, the same
  in debugfs). The card stood at 28.6–28.8 GB beside the pinned encoder.
- *Load:* a few seconds, from the page cache, right after the download.
- *Speed:* the 50 reference passages took 2,425 s with at least one request in flight:
  22,265 output tokens and 62,456 prompt tokens. That is **9.2 output tokens/s and
  48.5 s per passage** (two in flight with `parallel:1`, so the requests queue).
- *Conditions:* this was beside the gutenberg worker, which embedded through
  `vega-qwen3-embedding-4b` on the same card the whole time. Polytonic Greek costs
  ~1 prompt token per character.

**On a quiet card** (the second run: gutenberg's embedding off, each model alone, the
80 retrieval passages of ~300–500 tokens, two requests in flight):

| model | VRAM | output tok/s | s / passage | s per source token |
|---|---|---|---|---|
| qwen3-30b-a3b | 19,162 MiB | **87.0** | 5.1 | 0.0113 |
| gemma-3-27b-it | 17,825 MiB | **22.9** | 18.8 | 0.0413 (eval set 0.0422) |
| madlad | ~6.4–7.2 GB | — | 3.2 | 0.0069 (its long-passage output is not usable) |

So the 9.2 tok/s above was the shared card, not the model. Qwen3 is a mixture of experts
with 3 B active parameters, so it runs ~4× faster than the dense Gemma.

**Projection** (chars/4 tokens, card held alone):

| | all books | no English version |
|---|---|---|
| la, Qwen3 | 9.42 M tokens → **1.2 days** | 5.55 M → **17 h** |
| la, Gemma | → 4.5 days | → **2.7 days** |
| el, Qwen3 | 12.28 M → **1.6 days** | 7.23 M → **23 h** |
| el, Gemma | → 5.9 days | → **3.5 days** |

The brief's 10.5 M and 13.8 M tokens would add 12 %.

## Retrieval (books with no English version)

The method is [translate-before-embed.md](translate-before-embed.md)'s own functions.
- *Books:* 20 la and 20 el books with no English version, drawn with seed
  `classical-translation-2026-10-09` after removing the 6 the recall check found.
- *Passages:* one document passage and one query passage per book.
- *Pool:* 540, with the earlier note's 500 English distractors.
- *Embedding:* gutenberg's recipe through `embed_chunks` / `embed_query`. One run,
  `sha256:ccfa9a11…`, on VeGa.
- *Queries:* Q1 is the title plus LCSH subjects plus bookshelves, with the title
  translated in the translated condition. Q2 is the book's other passage.
- *Measures:* r@1/5/10 · MRR.

All conditions were embedded in one pass, in the second run. "paired" counts the books
where the translated condition ranked the target better / worse / the same as raw.

| condition | Q1 | Q1 paired | Q2 | Q2 paired | within-lang Q1 · Q2 (MRR) |
|---|---|---|---|---|---|
| **la raw** | .30/.55/.75 · **.422** | | .45/.75/.85 · **.594** | | .557 · .687 |
| la qwen3 | .35/.45/.65 · .441 | 6 / 8 / 6 | .40/.65/.85 · .515 | 4 / **10** / 6 | **.660** · .673 |
| la gemma | .20/.40/.45 · .296 | 3 / **12** / 5 | .40/.55/.70 · .485 | 4 / **9** / 7 | .551 · .667 |
| la madlad | .25/.45/.45 · .343 | 3 / **12** / 5 | .45/.60/.75 · .541 | 5 / **8** / 7 | .532 · .603 |
| **el raw** | .40/.50/.80 · **.500** | | .35/.55/.90 · **.464** | | .518 · .477 |
| el qwen3 | .20/.45/.45 · .304 | 1 / **14** / 5 | .35/.40/.55 · .413 | 2 / **10** / 8 | .581 · .541 |
| el gemma | .20/.35/.40 · .278 | 1 / **14** / 5 | .35/.45/.50 · .404 | 1 / **10** / 9 | .541 · .529 |
| el madlad | .30/.40/.40 · .363 | 1 / **12** / 7 | .35/.45/.50 · .409 | 3 / **10** / 7 | .534 · .523 |

The raw title was also kept as the Q1 query against each translated pool, so only the
document side changed. Q1 MRR is then:
- la: qwen .404, gemma .335, madlad .356;
- el: .355, .320, .321.

All below raw.

**What it says.**
- *In the mixed pool, translation loses for every translator.* A translated passage
  becomes one more English passage among 500 English distractors. The raw passage keeps
  its language, which the query and the other passage share. This is the effect
  translate-before-embed.md saw.
- *Within one language the LLMs help Q1.* With no distractors (only "which of these books
  is it?"), Qwen3's Latin goes 0.557 → 0.660 and its Greek 0.518 → 0.581. Q2 stays about
  level.
- *Better translation helps only a little here.* Gemma translates Latin best by chrF, yet
  retrieves worse than Qwen3. Why was not examined.
- *Comparison:* the earlier note's 20 la books (not filtered by coverage, pool 678) moved
  from Q1 MRR 0.518 → 0.483 and Q2 0.772 → 0.732 with MADLAD. The trend is the same.

## Incident

**Two of my loads overcommitted the card while Louise was in use.**

**The traffic was another measurement.** It was not a person: the chat, MADLAD, rerank
and query-encoder traffic came from `/root/onelang`, the one-language search run
(tools/measure/one_language_*). Its debug runs started at 18:26, and its 451-query run
in container `onelang-search` started at 19:09, at ~3.5 queries/min.

**The window run did not start.** The operator granted a window at 19:30 and gutenberg's
embedding was off. My clear-card step began, but that run kept loading models: four times
between 19:32 and 19:34 my step unloaded models its searches had just loaded. I stopped
before loading anything. The six pins were restored live and the YAMLs are
byte-identical to `pin-backup6`. LocalAI was not restarted, so as not to break that run.
`finish2.sh` holds the remaining steps, each run alone on the card and checked from the
driver.

*First load.*
- *Collision:* at 18:29:35 (16:29 UTC) the card read 14.4 GB used while LocalAI listed
  9.6 GB. Louise's chat was mid-load, and LocalAI lists a model only once its load has
  finished. My pre-check took that reading. Loading Qwen3-30B (19.1 GB) beside it filled
  the card: 32,599 of 32,656 MiB at 18:29:55.
- *What crashed:* `louise-chat-qwen3-8b` and `vega-qwen3-embedding-4b`. The worker
  deferred and recovered in 25 s.

*Second load.*
- *The guard:* I then ran `classical_guard.py`. It shut the translator down whenever
  LocalAI started loading anything else, and held it until only the pinned models were
  on the card.
- *Collision:* at 18:48:41 Louise loaded chat 15 s after a release. The guard's
  `POST /backend/shutdown` answered **404 "not loaded"** for the Qwen3-30B that
  `/system` had listed 3 s earlier. Chat and the encoder crashed again.
- *Hung encoder:* the encoder came back hung: every request timed out after 300 s. The
  worker wrote **`ReadTimeout` errors on three gutenberg documents**:
  `gutenberg:76722`, `34379` and `22283`, at 18:53, 18:58 and 19:03.
- *Fix:* I killed the hung backend process at 19:08, and LocalAI reloaded it. Embedding
  resumed (226 answers/min).

**The three documents need the operator's "clear errors & retry".**

**Unpinning hurt search too.** To fit Qwen3-30B, the four search models were unpinned
(allowed in the brief). With them unpinned, LocalAI's reclaimer evicted
`louise-rerank-bge` during the operator's searches, and two `/v1/rerank` calls answered
500 (18:47:39 and 18:47:42).

**Restored.** The pins are back: live through `PUT /models/toggle-pinned/<m>/pin`, and
the four YAMLs byte-identical to their backups (the sha256s match). LocalAI was not
restarted afterwards, so as not to interrupt the operator again.

**Lesson.** A guard cannot make a 19 GB model safe beside Louise:
- LocalAI loads without a fit check;
- it cannot stop a model that is still loading;
- its shutdown can miss a model it lists.

With Louise's interactive models able to load at any moment (chat 7.4 GB, MADLAD 7.2,
rerankers and query encoders 3.3), only ~3 GB is safe beside the pinned encoder. The two
LLMs need **a window in which Louise's chat, translation and search are not used**, or
the gutenberg worker paused as well.

## The window run (2026-10-09 23:15 to 2026-10-10 00:10)

The operator suspended gutenberg's embedding (`embed = "off"` in its collection file) and
accepted a degraded Louise.

**Preparation.**
- The six pinned YAMLs were backed up to `pin-backup6`: the four search models,
  `vega-qwen3-embedding-4b` and `vega-granite`.
- They were unpinned and LocalAI was restarted. The card read 25 MiB.

**Each step** (`finish2.sh`):
1. Shut down every model LocalAI listed.
2. Read the driver (debugfs `vram0_mm`) twice, 10 s apart.
3. Go on only when nothing was listed, the driver figure matched what was listed (no
   load in progress), and free memory was at least the model plus 2 GiB.
4. Run the model alone.

Translation and embedding never overlapped.

**One outside load.** `louise-chat-qwen3-8b` was loaded twice by another client at 23:18,
beside Qwen3. The card peaked at 26.6 of 32.6 GB, and the reclaimer evicted chat within
seconds. There was no overcommit.

**Restored.**
- The six pins are back, and their YAMLs are byte-identical to the backups (sha256
  checked).
- LocalAI was restarted.
- A gutenberg search through Louise's API answered: 5 hits, dense leg and rerank. It took
  60 s, all cold loads: MADLAD's language list 37 s, rerank 12.6 s, dense 7.2 s.
- After it, `vega-qwen3-embedding-4b` and `louise-rerank-bge` were loaded again.

gutenberg's embedding was left off, for the operator to turn back on.

## Setup and what stays

**Weights** are in `/mnt/ssd5/apps/localai/models/weights-translate/` (sha256 checked
against the Hub's LFS ids, in `SHA256SUMS.computed`):

| file | size | source | licence |
|---|---|---|---|
| `Qwen3-30B-A3B-Instruct-2507-Q4_K_M.gguf` | 18,556,686,752 B | unsloth/Qwen3-30B-A3B-Instruct-2507-GGUF (3.4 M downloads, the most of the non-gated Qwen3-30B-A3B GGUFs) | Apache-2.0 |
| `gemma-3-27b-it-Q4_K_M.gguf` | 16,546,688,736 B | unsloth/gemma-3-27b-it-GGUF (Google's own GGUF is gated) | Gemma Terms of Use |

**Models** (`/mnt/ssd5/apps/localai/models`, not pinned, not loaded; generic, for any
client):
- `qwen3-30b-a3b`: the 2507 instruct release, non-thinking, so no `/no_think` is
  needed.
- `gemma-3-27b-it`: text only.

Both run on `intel-sycl-f16-llama-cpp`, `gpu_layers 999`, context 8192, the model's own
chat template, and the vendor's sampling defaults. No translation prompt is baked in.

**Reproduce.** On the node:
1. `run.sh` starts the throwaway container. It mounts the SHARD and `gutenberg.toml`
   read-only, and sacrebleu is in `out/pylib`.
2. `classical_shard.py catalog|dump`, then `classical_evalset.py` (host python3),
   `classical_coverage.py match|judge`, and `classical_translation.py
   eval|sample|retrieval|embed|score`.
