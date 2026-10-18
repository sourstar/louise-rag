# Wikidata, gutenberg-wd-kg and "Philippe in the car" — the operator's design

*Designed by the operator in another conversation, in the Claude Docs
document "Louise — Wikidata and gutenberg-wd-kg collections"
(https://claude.ai/code/artifact/5a99909f-f587-44da-a473-7c81da34d40e).
Copied here from its revision 15 (2026-10-18); the doc is the original and
wins if they differ. Nothing in it has been checked against docs/01–09 yet.
The first-read notes at the end are not part of the design.*

---

## SHARD packet types

Every Codex declares its collection's packet types. Each type has a Parquet
schema and a join key (e.g. `chunk_id`); the Manifest lists packets per type.
New types can be added without changing the standard. Existing types: corpus
and embeddings. New types, introduced with gutenberg-wd-kg:

| Type | Columns | Key |
|---|---|---|
| mentions | chunk_id, start, end, qid, score | chunk_id |
| relations | subject_qid, relation, object_qid, chunk_id | chunk_id |
| communities | community_id, level, qids, summary | community_id |

- **Mention**: a name in the text resolved to an entity ("il Merisi" on
  p. 12 → Caravaggio, Q42207).
- **Relation**: a link between two entities stated by the text (Caravaggio —
  quarrelled with — Baglione).
- **Community**: a group of tightly linked QIDs found by Leiden on the
  relation graph, plus an LLM summary; used for global questions.

Rules:
- A collection can be a sibling of any number of other collections; its Codex
  pins the version of each.
- Louise is designed for local use: reference models must run locally.

---

## Collection: wikidata

### Purpose

- Wikidata QIDs are Louise's system-wide shared entity IDs for the GraphRAG
  layer.
- Wikidata's own triples (`claims` in the dump) are the backbone graph.
  Relations extracted from other collections live in separate sibling
  collections anchored to QIDs; Wikidata's graph is never modified.
- Entities missing from Wikidata (minor people and places in Gutenberg
  books): Louise generates its own IDs, linkable to a QID once one exists.

### Data

- Origin: Wikidata Embedding Project (Wikimedia Deutschland with Jina AI and
  DataStax, late 2025). Its online vector database (DataStax Astra) is not
  used; we use the published datasets and recipe.
- Corpus: `philippesaade/wikidata`, snapshot 2026-05-07, ~959 GB of Parquet
  in 7,449 files, ~74.5M entities. Scholarly articles (~46M) removed; items
  without a Wikipedia page included. Fields: id, labels, descriptions,
  aliases, sitelinks, claims. All Wikidata languages (400+). License CC0.
- Vectors: `philippesaade/Wikidata_Vectors_0.2`, ~114.5 GB in 538 files,
  ~53.7M rows, English, French, German and Arabic only (no Italian). License
  CC0 on Hugging Face.
- The exact text chunks are not published: they are regenerated from the
  dump with Wikimedia's textifier.
- Total download ≈ 1 TB.

### Embedder

- `jinaai/jina-embeddings-v3`: 570M parameters, 1024 dimensions, 8,192
  tokens, 100+ languages, task-specific LoRA adapters (passage-retrieval
  adapter for documents).
- License CC BY-NC 4.0: fine for Louise as long as it stays free and
  non-commercial. Declared in the Codex so users know these vectors must not
  go into commercial products.
- Runs locally (~1–2 GB). Community GGUF conversions are unverified on
  llama.cpp SYCL; the safe path is Python (sentence-transformers or
  transformers on XPU), i.e. a custom Louise PyTorch backend in LocalAI.

### Codex

```json
{
  "collection": "wikidata",
  "creator_script": {"repo": "philippesaade-wmde/WikidataTextEmbedding", "commit": "TBD"},
  "source": {"corpus": "philippesaade/wikidata", "snapshot": "2026-05-07", "license": "CC0", "excluded": "scholarly articles", "entities": 74500000, "fields": ["id", "labels", "descriptions", "aliases", "sitelinks", "claims"]},
  "entity_id": "wikidata_qid",
  "chunker": "WikidataTextEmbedding textifier",
  "embedding": {"model": "jinaai/jina-embeddings-v3", "model_license": "CC BY-NC 4.0 (non-commercial use only)", "dimensions": 1024, "max_tokens": 8192, "task": "retrieval.passage", "normalize": true},
  "languages": {"imported": ["en", "fr", "de", "ar"], "extensible": true, "extension_pool": "all languages present in corpus labels/descriptions (400+)", "selection": "one language at a time, on demand"},
  "verification": {"method": "re-embed random sample, compare cosine", "tolerance": "TBD from benchmark"},
  "build": {"base": "precomputed embeddings imported as block 0 (philippesaade/Wikidata_Vectors_0.2)", "continuation": "new data and new languages embedded by Louise peers with the same recipe", "mechanism": "same as Wikipedia and PubMed collections"},
  "credits": ["Wikimedia Deutschland — Wikidata Embedding Project", "Jina AI — jina-embeddings-v3", "DataStax (IBM)", "Philippe Saade — WikidataTextEmbedding, Hugging Face datasets"],
  "store": "LanceDB"
}
```

### Initial Manifest

```json
{
  "collection": "wikidata",
  "lineage": "wikidata/jina-v3",
  "blocks": [{"id": 0, "type": "import", "source": "philippesaade/Wikidata_Vectors_0.2", "license": "CC0", "files": 538, "rows": 53700000, "size_gb": 114.5, "languages": ["en", "fr", "de", "ar"], "status": "to_verify"}],
  "corpus_size_gb": 959.2,
  "next_blocks": [{"type": "language_extension", "language": "<any corpus language, selected one at a time>", "status": "on_demand"}]
}
```

### Build and updates

- Block 0: import the published Parquet vectors into LanceDB. Verify by
  regenerating the cleaned text with the textifier and re-embedding a random
  sample: it must match within the cosine tolerance.
- Data released after the snapshot: embedded by Louise peers with the same
  recipe.
- New Wikimedia snapshot: check its vectors against ours within the
  tolerance.
- Language extension: any corpus language, one at a time. Example: Italian on
  the B70 ≈ 1–3 days (assumes ~13M chunks × ~300 tokens at 20–40k tokens/s;
  to confirm with the benchmark).
- Language extensions can be offered back to Wikimedia: Wikidata
  talk:Embedding_Project, Philippe Saade (Wikimedia Deutschland), or
  published as a Hugging Face dataset.

### Storage and download

- LanceDB with float16 vectors: ~120 GB for the 4 imported languages, ~30 GB
  for each added language.
- Index and vectors on SSD, corpus text on HDD (current setup). Build the
  index on NVMe; serving it from a cheaper SATA SSD is fine (queries stay in
  the tens of milliseconds). On an HDD, queries would take seconds.
- Download with a Hugging Face token and `huggingface-cli download`,
  moderate parallelism. Hugging Face limits requests, not bandwidth: 5-minute
  windows, ~3,000 requests per IP without an account.

### Sources

- [Wikidata Embedding Project](https://www.wikidata.org/wiki/Wikidata:Embedding_Project)
- [Vector Database](https://www.wikidata.org/wiki/Wikidata:Vector_Database) (links to both Hugging Face datasets, changelog)
- [October 1, 2025 release](https://www.wikidata.org/wiki/Wikidata:Embedding_Project/October_1_2025_Release)
- [Discussion page (license)](https://www.wikidata.org/wiki/Wikidata_talk:Embedding_Project)
- [WikidataTextEmbedding](https://github.com/philippesaade-wmde/WikidataTextEmbedding)
- [Jina Embeddings v3](https://huggingface.co/jinaai/jina-embeddings-v3)
- [Corpus dataset](https://huggingface.co/datasets/philippesaade/wikidata)
- [Vectors dataset](https://huggingface.co/datasets/philippesaade/Wikidata_Vectors_0.2)

---

## Sibling collection: gutenberg-wd-kg (Gutenberg Wikidata Knowledge Graph)

Sibling of `gutenberg` (Qwen3 Embedding 4B) and `wikidata` (Jina v3); the
Codex pins both versions. Called "kg" because it holds mentions, relations
and community summaries, not only a graph.

One sibling holds both the mention model and the relation LLM. Changing only
the LLM creates a new sibling that reuses the identical mention packets.
Several graphs built with different models (different quality) can coexist
over the same base collections.

### Pipeline

1. **Mentions**: GLiNER (NER) over every Gutenberg chunk, ~8B tokens. In
   LocalAI, PR #12140 adds GLiNER 2.5 to the vllm-cpp backend (merge status
   to check). Settings TBD.
2. **Entity linking**: each mention is searched in the wikidata collection
   with Jina v3 (labels, descriptions, aliases) → top-10 candidate QIDs; an
   LLM picks the right QID from context, or none. Embedding the mentions
   takes only hours.
3. **Relations**: Gemma 4 26B A4B, Q8_0, thinking off, temperature 0. Only
   chunks with at least two mentions. Input: chunk text + the QIDs found;
   output JSON: subject_qid, relation, object_qid, chunk_id. Context needed:
   4–8k tokens.
4. **Communities**: multi-level Leiden on the relation graph (large generic
   and small specific communities). For each community the LLM writes a
   summary from its entities, relations and source Gutenberg passages
   (16–32k tokens of context). Computed once from the graph; queries only
   read them; recomputed as a new block when the graph grows.

Conflicts with Wikidata: a Gutenberg relation that contradicts Wikidata is
kept and never overwrites it; it is flagged as conflicting, with book and
chunk as source. At answer time the LLM shows both versions with their
sources.

### Scope

- Mentions: all of Gutenberg.
- Relations and communities: initially history, art and architecture (LoC
  classes C, D, E, F and N, which includes NA); extensible to any of the 21
  LoC classes, one at a time.

### LLM choice

- Gemma 4 26B A4B (MoE, 26B total, 4B active). Vectara hallucination
  leaderboard (HHEM): 5.2%, i.e. 94.8% of summaries without invented content;
  12th of 85.
- Local models ranked higher in the visible part of the leaderboard: Phi-4
  14B 3.7% (answers only 80.7% of documents, English only, 16k context),
  Llama 3.3 70B 4.1% (too large for 32 GB), Gemma 3 12B 4.4%, Qwen3 8B 4.8%,
  Mistral Small 2501 24B 5.1%.
- Reasoning raises hallucinations 2–3× → thinking off. HHEM-2.1-Open runs
  locally to measure our own summaries.
- B70 (32 GB): BF16 (~52 GB) doesn't fit; Q8_0 ≈ 27 GB, leaving ~30k tokens
  of context, which is enough.
- 16 GB GPUs: Q8_0 with experts offloaded to the CPU (`--n-cpu-moe`), ~2–4×
  slower, needs at least 32 GB of system RAM. Q3_K_M (~12–13 GB, 32k
  context) fits but loses quality (malformed JSON, wrong relations): not for
  packets others must verify. A different quantization is a different LLM
  setting, so it would be a new sibling.

### Codex

```json
{
  "collection": "gutenberg-wd-kg",
  "siblings": [
    {"collection": "gutenberg", "version": "<hash Codex>"},
    {"collection": "wikidata", "snapshot": "2026-05-07", "version": "<hash Codex>"}
  ],
  "scope": {
    "mentions": "all Gutenberg",
    "relations": {"initial": ["C", "D", "E", "F", "N"], "extensible": "any LoC class, selected one at a time"},
    "communities": {"initial": ["C", "D", "E", "F", "N"], "extensible": "any LoC class, selected one at a time"}
  },
  "mention_model": {"type": "NER", "model": "GLiNER (LocalAI)", "settings": "TBD"},
  "entity_linking": {"candidates": "wikidata vectors, top-k QID", "k": 10, "disambiguation": "LLM picks QID from context, or none"},
  "llm": {"model": "Gemma 4 26B A4B", "quant": "Q8_0", "thinking": false, "temperature": 0, "prompt_hash": "TBD"},
  "relation_input": "chunks with >= 2 mentions",
  "community_algorithm": "Leiden, multi-level",
  "packet_types": {
    "mentions": {"schema": ["chunk_id", "start", "end", "qid", "score"], "key": "chunk_id"},
    "relations": {"schema": ["subject_qid", "relation", "object_qid", "chunk_id"], "key": "chunk_id"},
    "communities": {"schema": ["community_id", "level", "qids", "summary"], "key": "community_id"}
  },
  "verification": "TBD (LLM extraction is not bit-exact; tolerance criterion to define)",
  "format": "Parquet"
}
```

### Initial Manifest

```json
{
  "collection": "gutenberg-wd-kg",
  "blocks": [
    {"type": "mentions", "scope": "all Gutenberg", "status": "planned", "eta_days": "2-5"},
    {"type": "relations", "scope": "history/art/architecture", "status": "planned", "eta_days": "~7"},
    {"type": "communities", "scope": "history/art/architecture", "status": "planned", "eta_days": "2-5"}
  ]
}
```

Estimates on the B70, assuming the selected classes are ~15% of Gutenberg:
mentions 2–5 days, relations ~1 week, communities 2–5 days; total ~2–3
weeks. Relations on all of Gutenberg would take ~3–6 weeks.

### Queries it enables

- Point questions ("Who protected Caravaggio in Rome?"): graph from the QID,
  book passages as sources.
- Broad questions ("Main themes of Gothic architecture?"): community
  summaries.
- Change over time ("How did Florence change from the 14th to the 16th
  century?"): Wikidata dates + Gutenberg relations.
- Links between two entities ("Bernini and Borromini?"): graph paths between
  the two QIDs.

The chat LLM chooses through tools between community summaries and
chunks/graph. Wikipedia joins through QIDs.

### Open items

- GLiNER settings; relation prompt and its hash; verification tolerance for
  relations; Jina verification tolerance (wikidata); WikidataTextEmbedding
  commit hash.
- Wikipedia collection: check in the code whether each article's QID
  (`wikibase_item`) is stored. It is not in pages-articles.xml (it is in
  page_props.sql or the Enterprise HTML dump). Add it if missing (corpus
  rule: useful corpus information is never discarded). This is a forward
  derivation: corpus packets regenerated with a QID column, no re-embedding.

---

## Philippe in the car — voice guide to history and geography

While you drive, Philippe tells you about the history, geography and events
of the places around you, using Wikidata, gutenberg-wd-kg, Gutenberg and
Wikipedia.

### Requirements

- Recognizes who is speaking by voiceprint.
- Listeners present are implicit (voices heard recently; only yours = only
  you) or declared ("it's me and Heike"); a declared presence holds until you
  say otherwise.
- Unknown person: Philippe greets them and asks for consent to record their
  voiceprint (biometric data, GDPR). If they decline: session guest, nothing
  saved.
- Memory per listener and per QID: overview first, then details; moves on
  unless asked for more; next time it resumes where it left off.
- With several listeners it narrates like a human storyteller ("as Augusto
  already knows…"), without skipping what the others haven't heard.

### Components

| Piece | Function | Already available |
|---|---|---|
| In-car app | GPS + microphone, WebRTC to LocalAI | To write (Realtime API client) |
| Voice (STT, TTS, VAD) | Realtime pipeline | LocalAI Realtime API + OmniVoice HQ |
| Voice recognition | Enrolls and identifies voiceprints | LocalAI /v1/voice/register and /v1/voice/identify (SpeechBrain ECAPA-TDNN); diarization /v1/audio/diarization (sherpa-onnx). The voiceprint store is in memory only: we must make it persistent |
| Listener database | People, consent, what was told per QID and level | To write (SQLite) |
| Location search | Nearby QIDs (Wikidata P625) ranked by importance and direction | To write (spatial index on Wikidata data) |
| Orchestrator | Listeners present + nearby QIDs + memory → Philippe's prompt and tools | LocalAGI as agent base, or our own code |
| Text memory | Free-form conversation notes | Louise (already developed; LocalRecall not needed) |

### Philippe's tools

- `search_nearby(position, radius)`
- `search_graph(qid)`
- `search_rag(collection, query)`
- `mark_told(people, qid, level)`
- `register_voice(name, consent)`

---

## Measured against the origin (2026-10-18 — the origin's data wins)

Operator: "their specs are the source of the truth". What the published
datasets actually hold, read from their rows and files:

| | the design | measured | how |
|---|---|---|---|
| vector width | 1024 | **512**, float32 little-endian, unit norm | 10 rows (ar, en) decoded from `Wikidata_Vectors_0.2` through the datasets-server `rows` API: 2,048 bytes each, norm 1.000000. The dataset card agrees: "embedding size: 512" — jina-v3's Matryoshka cut, renormalised |
| rows | 53.7M | **53,666,646**: en 22,667,152 (227 files), fr 11,978,401 (120), de 11,180,375 (112), ar 7,840,718 (79). The card's table (44.57M) predates the July merges | every vector file's Parquet footer, read by HTTP Range |
| row id | — | `<QID>_<lang>_<n>`, n from 1 per (entity, language) | WikidataTextEmbedding `main.py`: `_id = f"{item.id}_{LANG}_{i + 1}"` over `chunk_item_text(item, tokenizer, max_length=1024, lang)` |
| chunk length | 8,192 tokens | **1,024 tokens** (jina-v3's tokenizer) | the same call |
| entities with vectors | — | at least one Wikipedia sitelink; a label in the language (or `mul`); a description or a statement | the dataset card |
| engine | local | Jina's hosted API (`api.jina.ai/v1/embeddings`, `dimensions`, `task: retrieval.passage`, `late_chunking: false`) | `src/JinaAI.py` (`JinaAIAPIEmbedder`); the pipeline needs `JINA_API_KEY` |
| text | the "textifier" | WikidataTextifier (`wmde/WikidataTextifier`, BSD-3), imported in-process, pinned to `main` only; labels it lacks are fetched from the Wikibase API | `scripts/run_pipeline_with_wdtextifier.sh`, `main.py` |
| corpus rows | — | every claim's value carries the referenced entity's labels in all languages, so a row is self-contained | one row read through the viewer |

### Can Wikimedia's vectors be carried? (2026-10-18, night, on the node)

**The probe.** Their own code at pinned commits: WikidataTextifier `0befd7c`
(the code live from 25 May to 4 August 2026; `804e7c6` and `fa32546` give the
same results), WikidataTextEmbedding `c9dc819`, with the versions their
`uv.lock` pins (beautifulsoup4 4.14.3 matters: a newer one parses the property
page to nothing). It ran offline. Three pieces of their stack had to be
rebuilt to do that:
- **Labels:** a label factory filled from each corpus row's own labels. Their
  cleaner wrote into every row exactly the label dictionaries their factory
  had resolved.
- **Dates and coordinates:** formatted by their pinned Wikibase,
  `wikibase/wikibase:mw1.44.0` (`sha256:069ede5e…`) on `mariadb:10.11`
  (`sha256:0d0945c5…`), through `wbformatvalue`. This is their own design.
  Without it a date silently drops out of the text ("has date of birth").
- **Claim order and filter:** from the page
  "MediaWiki:Wikibase-SortedProperties" at revision 2550808357 (2026-09-29;
  13,532 properties). It is edited about monthly, so its revision is a
  parameter of the text.

Corpus rows were converted back to the dump's shape. jina-v3 ran locally on
the B70 (`ab036b0`, with `xlm-roberta-flash-implementation` at `bd55a5e`
inside the model folder; `model.encode(task="retrieval.passage",
truncate_dim=512)`, one text per call), on an internal Docker network that
reaches only the formatter.

**Results, 1,000 random English entities of `chunk_100` (1,003 chunks):**

| | |
|---|---|
| chunk counts equal to the archive's | 999 / 1,000 |
| exact (cosine ≥ 0.9999 to the archive vector) | 860 (85.7 %) |
| ≥ 0.999 | 874 (87.1 %) |
| below 0.99 | 97 (9.7 %); one at −0.07 (another text entirely) |
| median | 0.999958 |

**The engine is not the difference.** The same text through the local model
and through Jina's API agrees to 1e-6 for a bare label ("24-hour urine
collection." → 0.999999), and to 3–5e-5 for a full entity. The differences
are in the TEXT.

**Why the text differs, from their code** (`src/wikidataVectorCache.py`
`filter_for_update`). An entity is re-embedded only when its `modified`
date is later than its cached vector. So the archive keeps vectors computed
in earlier runs: uploads since March 2026, then the `20260507` branch
merged into main on 17 and 30 July. Those were made with older text code,
older labels of referenced entities and an older property page. Example:
for Q9208205 their vector is the text "24-hour urine collection." (with a
full stop); every textifier since February, and the live API today, write
it without one.

**What follows (RULE 1, promise 8).** A carried vector must be our chunk's
vector, within the engine's variation. About 14 % of the archive's rows are
vectors of another text: they would pass a 0.95 import gate and fail a
verify-on-use at any honest tolerance. They cannot be told apart without
re-embedding, because the dates that would tell them apart (`modified`) are
among the fields the corpus dropped. Carrying the whole archive would be the
artefact promise 8 forbids. Ways out, for the operator:
1. **Ask Wikimedia for the chunk texts** (their vector database stores each
   row's `content`; the public API does not return it). With the texts, a
   `verbatim` layer carries 100 %, the wikivec precedent.
2. **Embed it ourselves** with their recipe: 53,666,646 chunks over the four
   languages. One text per call measured 4.4 chunks/s on the B70, i.e. ~141
   days of continuous GPU. Batch 32 (with jina's `torch.unique` overflow on
   XPU worked round) measured 45/s, i.e. ~14 days: about 41 nights inside the
   quiet hours.
3. **Carry, then re-embed everything over time to find the 14 %.** This
   costs option 2's GPU anyway; its only gain is being searchable sooner.

**The corpus form (invariant 11, a question for the operator).** Their
cleaner (`WikidataJSONCleaner.clean_entity`) drops content that is about
the entity:
- `snaktype`, so "no value" and "unknown value" become indistinguishable;
- each statement's id (GUID);
- a property's `datatype`;
- sitelink badges;
- `modified`, `lastrevid` and `type`.

The official dump keeps them all. Choosing the HF corpus (the design) means
holding less than Wikidata says. Choosing the dump means a record's text
depends on other entities' labels from the same dump, which is pinned per
snapshot. **The collection is not created until this is answered.**

**Throughput on the B70** (128 entity texts, 2026-10-18 04:10; batches
need `torch.unique` on the CPU, the XPU overflow's workaround):

| | 1 text | batch 8 | batch 32 |
|---|---|---|---|
| fp32 | 4.6 texts/s | 21.7 | 45.0 (cosine to one-at-a-time ≥ 0.9999999) |
| bf16 | 5.7 | 33.6 | 73.1 (drifts up to 1.4e-4) |

At fp32 batch 32, embedding the four languages (53.7 M chunks) is ~14 days
of GPU, about 41 nights inside the quiet hours. LocalAI calls a backend one
text at a time, so the batch has to be assembled inside the backend, from
concurrent calls. Until then it is 4.6/s.

**The phrase tables.** The textifier knows six languages (`ar de en es fr
zh`, `language_variables.json`). Any other (Italian included) gets English
scaffolding ("Attributes include:") around its own labels.

**Other facts for the build:**
- jina-v3's `encode` on the B70 fails on a batch (`torch.unique`:
  "numel: integer multiplication overflow") and works one text at a time.
- `HF_HUB_OFFLINE` alone did not stop the remote-code download warning.
  The probe ran with no internet to prove the load is offline, and the
  backend must too.

Probe scripts, data and the formatter's containers: on the node, in
`/mnt/tank/apps/localai-probe/wikidata/`.

## First-read notes (Claude, 2026-10-18 — not part of the design)

Where the design meets docs/01–09 or CLAUDE.md and a decision is needed
before building. Not yet a full check.

1. **Vocabulary.** "blocks", "lineage", "packet types" are not the terms
   docs/01 and docs/07 fix (Codex, layers, records, SHARD parts). The doc's
   words map onto them; the code uses the documents'. New SHARD packet types
   are a wire-format change (09): CLAUDE.md asks first.
2. **Block 0 = Wikimedia's precomputed vectors.** Carrying an external
   space's vectors: the space identity (02 §2.4, 09 §9.3) and the
   calibration gate decide whether ours and theirs are one space — the
   wikivec precedent (Wikipedia layer 1). The doc's verification ("re-embed
   a random sample, within the cosine tolerance") is that gate.
3. **Serving jina-v3.** The doc's "custom Louise PyTorch backend in LocalAI"
   now exists: louise-python (backend-consolidation.md). jina-v3 is a fifth
   task there (its LoRA adapters: `retrieval.passage` for documents,
   `retrieval.query` for queries), not a new backend.
4. **Licence.** jina-v3 is CC BY-NC 4.0, as DAR-legal-it is NC: commercial
   peers consume, never contribute (memory: legal-encoder-decided). The
   corpus and Wikimedia's vectors are CC0.
5. **Pruning.** Dropping scholarly articles prunes whole entities (allowed,
   invariant 11); every field the origin gives about a kept entity stays.
6. **Derived collections.** gutenberg-wd-kg is built from two others by
   models whose output is not bit-exact ("verification: TBD"): how such a
   collection is identified and verified is not yet in docs/02 or 09.
7. **Louise's own IDs** for entities missing from Wikidata are new
   system-wide identifiers (docs/05 §5.2: `keys` hold only ids a reference
   source registers).
8. **Wikipedia's QID** (open item): to check in the code before the forward
   derivation.
9. **Philippe** handles biometric voiceprints (GDPR, consent): an
   application on top of Louise, outside the collection specs.
