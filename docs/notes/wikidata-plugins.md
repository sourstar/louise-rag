# Wikidata, gutenberg-wd-kg and "Philippe in the car" — the operator's design

*Designed by the operator in another conversation, 2026-10-17, in the Claude
Docs document "Louise — Collezione Wikidata"
(https://claude.ai/code/artifact/5a99909f-f587-44da-a473-7c81da34d40e).
Copied here 2026-10-18 so the repository holds it. The JSON is reproduced as
written; the doc's Italian prose is translated into English (the doc is the
original). Nothing in it has been checked against docs/01–09 yet. The first-read notes at the end
are not part of the design.*

---

## 1. Louise — the Wikidata collection

### Codex

```json
{
  "collection": "wikidata",
  "source": {"corpus": "philippesaade/wikidata", "snapshot": "2026-05-07", "license": "CC0", "excluded": "scholarly articles"},
  "entity_id": "wikidata_qid",
  "embedding": {"model": "jinaai/jina-embeddings-v3", "model_license": "CC BY-NC 4.0", "dimensions": 1024, "max_tokens": 8192, "task": "retrieval.passage", "normalize": true, "text_recipe": "WikidataTextEmbedding (Wikimedia)"},
  "languages": {"imported": ["en", "fr", "de", "ar"], "extensible": true, "extension_pool": "all languages present in corpus labels/descriptions (400+)", "selection": "one language at a time, on demand"},
  "verification": {"method": "re-embed random sample, compare cosine", "tolerance": "TBD from benchmark"},
  "build": {"base": "precomputed embeddings imported as block 0 (philippesaade/Wikidata_Vectors_0.2)", "continuation": "new data and new languages embedded by Louise peers with the same recipe", "mechanism": "same as Wikipedia and PubMed collections"},
  "credits": ["Wikimedia Deutschland — Wikidata Embedding Project", "Jina AI — jina-embeddings-v3", "DataStax (IBM)", "Philippe Saade — WikidataTextEmbedding, Hugging Face datasets"],
  "store": "LanceDB"
}
```

### Initial manifest

```json
{
  "collection": "wikidata",
  "lineage": "wikidata/jina-v3",
  "blocks": [{"id": 0, "type": "import", "source": "philippesaade/Wikidata_Vectors_0.2", "files": 538, "rows": 53700000, "size_gb": 114.5, "languages": ["en", "fr", "de", "ar"], "status": "to_verify"}],
  "corpus_size_gb": 959.2,
  "next_blocks": [{"type": "language_extension", "language": "<any corpus language, selected one at a time>", "status": "on_demand"}]
}
```

Extension: any language in the corpus, selected one at a time. Example
estimate for Italian on the B70: about 1–3 days (to be confirmed by a
benchmark).

### Sources and notes

- [Wikidata Embedding Project](https://www.wikidata.org/wiki/Wikidata:Embedding_Project)
- [Vector Database](https://www.wikidata.org/wiki/Wikidata:Vector_Database)
- [Release of 1 October 2025](https://www.wikidata.org/wiki/Wikidata:Embedding_Project/October_1_2025_Release)
- [WikidataTextEmbedding](https://github.com/philippesaade-wmde/WikidataTextEmbedding)
- [Jina Embeddings v3](https://huggingface.co/jinaai/jina-embeddings-v3)
- Download with a Hugging Face token and `huggingface-cli download`.
- At each new snapshot: check Wikimedia's vectors against ours, within the
  tolerance.

---

## 2. Sister collection: gutenberg-wd-kg (Gutenberg Wikidata Knowledge Graph)

A sister of Gutenberg (Qwen3 Embedding 4B) and of Wikidata (Jina v3). The
Codex pins the version of both.

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
  "entity_linking": {"candidates": "wikidata vectors, top-k QID", "k": 10},
  "llm": {"model": "Gemma 4 26B A4B", "quant": "Q8_0", "thinking": false, "temperature": 0, "prompt_hash": "TBD"},
  "community_algorithm": "Leiden, multi-level",
  "packet_types": {
    "mentions": {"schema": ["chunk_id", "start", "end", "qid", "score"], "key": "chunk_id"},
    "relations": {"schema": ["subject_qid", "relation", "object_qid", "chunk_id"], "key": "chunk_id"},
    "communities": {"schema": ["community_id", "level", "qids", "summary"], "key": "community_id"}
  },
  "format": "Parquet"
}
```

### Initial manifest

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

Rule: changing only the LLM creates a new sister that reuses the identical
mention packets. Estimated total: about 2–3 weeks on the B70.

---

## 3. Philippe in the car — a voice guide to history and geography

While you drive, Philippe tells the history, geography and events of the
places around you, using Wikidata, gutenberg-wd-kg, Gutenberg and Wikipedia.

### Requirements

- Recognises who is speaking by voiceprint.
- Who is present is implicit (voices heard recently; only yours = only you)
  or declared ("it's me and Heike"); a declared presence holds until you say
  otherwise.
- An unknown person: Philippe greets them and asks for consent to record
  their voiceprint (biometric data, GDPR). If they refuse: a guest for the
  session, nothing saved.
- Memory per listener and per QID: the overview first, then the details; it
  moves on unless asked; next time it picks up where it left off.
- With several people present it narrates as a human storyteller would ("as
  Augusto already knows…"), without skipping what the others have not heard.

### Components

| Piece | Function | Already available |
|---|---|---|
| In-car app | GPS + microphone, WebRTC to LocalAI | To be written (Realtime API client) |
| Voice (STT, TTS, VAD) | Realtime pipeline | LocalAI Realtime API + OmniVoice HQ |
| Voice recognition | Registers and identifies voiceprints | LocalAI /v1/voice/register and /v1/voice/identify (SpeechBrain ECAPA-TDNN); diarization /v1/audio/diarization (sherpa-onnx). The voiceprint store is in memory only: we must make it persistent |
| Listener database | People, consent, what has been told per QID and level | To be written (SQLite) |
| Search by position | Nearby QIDs (Wikidata P625) ordered by importance and direction | To be written (a spatial index over the Wikidata data) |
| Orchestrator | Who is present + nearby QIDs + memory → Philippe's prompt and tools | LocalAGI as the agent base, or our own code |
| Text memory | Free-form conversation notes | Louise (already developed; LocalRecall not needed) |

### Philippe's tools

- `search_nearby(position, radius)`
- `search_graph(qid)`
- `search_rag(collection, query)`
- `mark_told(people, qid, level)`
- `register_voice(name, consent)`

---

## First-read notes (Claude, 2026-10-18 — not part of the design)

Points where the design meets docs/01–09 or CLAUDE.md and a decision is
needed before building. Not yet a full check.

1. **Encoder licence.** jina-embeddings-v3 is CC BY-NC 4.0, as DAR-legal-it
   is NC: commercial peers can consume the collection but not contribute
   (memory: legal-encoder-decided).
2. **Vocabulary.** "blocks", "lineage", "manifest", "packet_types" are not
   the terms docs/01 and docs/07 fix (Codex, layers, records, SHARD parts).
   The doc's words map onto them; the code uses the documents' words.
3. **Block 0 = Wikimedia's precomputed vectors.** Carrying an external
   space's vectors: the space identity (docs/02 §2.4, 09 §9.3) and the
   calibration gate decide whether ours and theirs are one space — the
   wikivec precedent (Wikipedia layer 1). The doc's own "verification:
   re-embed random sample, compare cosine; tolerance TBD" is that gate.
4. **Serving jina-v3.** Not one of louise-python's four tasks
   (docs/notes/backend-consolidation.md); it would be a fifth task there
   (task adapters as LoRA, `retrieval.passage` for documents and
   `retrieval.query` for queries), not a new backend.
5. **"excluded: scholarly articles".** Pruning whole entities is allowed
   (CLAUDE.md invariant 11: a corpus may be pruned); what is kept of each
   entity is not (invariant 11's keep-everything rule applies to every
   field the origin gives about a kept entity).
6. **gutenberg-wd-kg** is a derived collection over two others (mentions,
   relations, communities as Parquet): how such a collection is identified,
   built and verified is not yet in docs/02 or 09 — a spec amendment to name.
7. **Philippe** handles biometric voiceprints (GDPR, consent): an
   application on top of Louise, outside the collection specs; its listener
   database is new state.
