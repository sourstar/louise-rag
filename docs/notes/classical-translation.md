# Latin and Greek into English: existing translations, two LLMs, MADLAD (2026-10-09)

*Measured on the node 2026-10-09, operator-approved. No spec or product code changed. Scripts:
[`tools/measure/classical_*.py`](../../tools/measure/); raw results on the node in
`/root/xlate-classical/out/` (`results.json`, `coverage.json`, `evalset.json`, the
translations). Earlier method: [translate-before-embed.md](translate-before-embed.md).*

**Unfinished.** Two of my model loads overcommitted the card while Louise was being used,
so I stopped the GPU work (see [Incident](#incident)). Measured: the coverage of
existing human translations, Qwen3-30B-A3B's translation quality and speed, and MADLAD
(quality and retrieval). **Not measured:** Gemma 3 27B, and retrieval with either LLM's
translations.

## Answer so far

1. **Existing human translations come first.** At least **39 of 108 Latin books (41 % of
   the la tokens)** and **102 of 216 Greek books (41 % of el)** already have a
   public-domain English version of the same work in the gutenberg collection. That is a
   lower bound: a hand check found 6 more among 40 books the method had called uncovered.
   Project Gutenberg holds nothing beyond the collection, because the collection is all
   of PG (78,282 texts in `pg_catalog.csv`, all present).
2. **gutenberg's `el` is not Ancient Greek.** Of the 216 books, 215 are Modern Greek:
   monotonic Katharevousa or demotic as far as checked, including translations of the
   ancients (the Φέξης series).
   The only polytonic book is a school Lucian. Ancient Greek in PG appears only inside
   bilingual editions, which the collection files under `en`.
3. **Quality: Qwen3-30B-A3B is far better than MADLAD on Latin** (chrF 52.2 against 35.8,
   better on 20 of 20 passages). It is **better on Ancient Greek** (52.9 against 47.5, 17 of
   20) and **slightly better on Katharevousa** (49.8 against 46.0, a loose reference).
   MADLAD loops or invents on passages longer than its 512-token context.
4. **Cost: Qwen3-30B is slow and takes the whole card.** It generates 9.2 tokens/s beside
   the gutenberg worker, about 48 s per passage. Translating only the books with no
   English version would take **~7 days for la and ~9 days for el**. It needs 19.1 GB of
   VRAM, so it cannot sit beside Louise's chat, MADLAD and reranker.
5. **Retrieval: MADLAD makes it worse again.** On 40 books that have no translation, it
   loses on cross-lingual metadata queries:
   - la: 3 books better, 12 worse; MRR 0.423 → 0.343;
   - el: 1 better, 12 worse; MRR 0.500 → 0.363.

   Whether an LLM's translation beats the raw embedding is **still open**.

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
| **qwen3-30b-a3b** | **52.2 / 17.7** | **52.9 / 18.1** | **49.8 / 12.6** |
| madlad (Louise's) | 35.8 / 6.5 | 47.5 / 16.7 | 46.0 / 12.0 |
| gemma-3-27b-it | not measured | | |

By work, chrF for qwen / madlad:
- Caesar 52.1 / 35.5;
- Augustine 55.8 / 39.5;
- Catullus 44.5 / 28.4;
- Galen 53.6 / 45.6;
- Dionysius 47.8 / 42.6;
- Didache 61.3 / 61.6.

Qwen is better on 20/20 la passages, 17/20 grc and 8/10 el.

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

**MADLAD:** 3.5 s per passage. Its output is not a real translation once a passage
exceeds 512 tokens. **Gemma:** not measured.

**Projection, Qwen3-30B at the measured 0.1085 s per source token** (chars/4), on a card
it holds alone:

| | all books | no English version |
|---|---|---|
| la | 9.42 M tokens → **11.8 days** (the brief's 10.5 M: 13.2 d) | 5.55 M → **7.0 days** |
| el | 12.28 M → **15.4 days** (13.8 M: 17.3 d) | 7.23 M → **9.1 days** |

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

| | Q1 raw | Q1 madlad | Q1 paired b/w/t | Q2 raw | Q2 madlad | Q2 paired | within-lang Q1 raw → madlad | Q2 |
|---|---|---|---|---|---|---|---|---|
| la (20) | .30/.55/.75 · .423 | .25/.45/.45 · .343 | 3 / **12** / 5 | .45/.75/.85 · .594 | .45/.60/.75 · .541 | 5 / **8** / 7 | .557 → .532 | .687 → .603 |
| el (20) | .40/.50/.80 · .500 | .30/.40/.40 · .363 | 1 / **12** / 7 | .35/.55/.90 · .464 | .35/.45/.50 · .409 | 3 / **10** / 7 | .518 → .533 | .477 → .523 |

The raw title was also kept as the query against the MADLAD pool, so only the document
side changed. Q1 MRR is then la 0.356 and el 0.321, still worse.

For comparison, the earlier note's 20 la books (not filtered by coverage, pool 678):
- Q1 MRR 0.518 → 0.483;
- Q2 MRR 0.772 → 0.732.

The trend is the same. **Qwen3-30B and Gemma retrieval: not run.**

## Incident

**Two of my loads overcommitted the card while Louise was in use.**

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

## To finish (needs that window)

On the node, in `/root/xlate-classical`, `finish.sh` runs the remaining steps:
1. Qwen3 retrieval translations, ~1 h;
2. `down` Qwen3;
3. Gemma eval and retrieval, ~2–3 h;
4. `down` Gemma;
5. delete `out/rvectors.npz`, then `embed` and `score`.

Each phase resumes from its file.

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
