# Wikidata, gutenberg-wd-kg and "Philippe in auto" — the operator's design

*Designed by the operator in another conversation, 2026-10-17, in the Claude
Docs document "Louise — Collezione Wikidata"
(https://claude.ai/code/artifact/5a99909f-f587-44da-a473-7c81da34d40e).
Copied here 2026-10-18 so the repository holds it. The doc's text is
reproduced AS WRITTEN (its prose is Italian, its JSON unchanged); nothing in
it has been checked against docs/01–09 yet. The first-read notes at the end
are not part of the design.*

---

## 1. Louise — Collezione Wikidata

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

### Manifest iniziale

```json
{
  "collection": "wikidata",
  "lineage": "wikidata/jina-v3",
  "blocks": [{"id": 0, "type": "import", "source": "philippesaade/Wikidata_Vectors_0.2", "files": 538, "rows": 53700000, "size_gb": 114.5, "languages": ["en", "fr", "de", "ar"], "status": "to_verify"}],
  "corpus_size_gb": 959.2,
  "next_blocks": [{"type": "language_extension", "language": "<any corpus language, selected one at a time>", "status": "on_demand"}]
}
```

Estensione: qualsiasi lingua del corpus, selezionabile una alla volta. Esempio
stima per l'italiano sulla B70: circa 1-3 giorni (da confermare con benchmark).

### Fonti e note

- [Wikidata Embedding Project](https://www.wikidata.org/wiki/Wikidata:Embedding_Project)
- [Vector Database](https://www.wikidata.org/wiki/Wikidata:Vector_Database)
- [Release 1 ottobre 2025](https://www.wikidata.org/wiki/Wikidata:Embedding_Project/October_1_2025_Release)
- [WikidataTextEmbedding](https://github.com/philippesaade-wmde/WikidataTextEmbedding)
- [Jina Embeddings v3](https://huggingface.co/jinaai/jina-embeddings-v3)
- Download con token Hugging Face e huggingface-cli download.
- Ai nuovi snapshot: verificare i vettori Wikimedia contro i nostri entro tolleranza.

---

## 2. Collezione sorella: gutenberg-wd-kg (Gutenberg Wikidata Knowledge Graph)

Sorella di Gutenberg (Qwen3 Embedding 4B) e di Wikidata (Jina v3). Il Codex
fissa la versione di entrambe.

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

### Manifest iniziale

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

Regola: cambiare solo l'LLM crea una nuova sorella che riusa i pacchetti
menzioni identici. Totale stimato: circa 2-3 settimane sulla B70.

---

## 3. Philippe in auto — guida vocale su storia e geografia

Mentre guidi, Philippe racconta storia, geografia e avvenimenti dei luoghi
intorno, usando Wikidata, gutenberg-wd-kg, Gutenberg e Wikipedia.

### Requisiti

- Riconosce chi parla tramite impronta vocale.
- Presenti impliciti (voci sentite di recente; solo la tua = solo tu) o
  dichiarati ("siamo io e Heike"); la presenza dichiarata vale finché non dici
  il contrario.
- Persona sconosciuta: la saluta e chiede il consenso a registrare l'impronta
  vocale (dato biometrico, GDPR). Se rifiuta: ospite di sessione, nulla salvato.
- Memoria per ascoltatore e per QID: prima la panoramica, poi i dettagli; passa
  oltre salvo richiesta; alla volta successiva riprende da dove era rimasto.
- Con più presenti racconta come un narratore umano ("come Augusto già sa…"),
  senza saltare ciò che altri non hanno sentito.

### Componenti

| Pezzo | Funzione | Già disponibile |
|---|---|---|
| App in auto | GPS + microfono, WebRTC verso LocalAI | Da scrivere (client Realtime API) |
| Voce (STT, TTS, VAD) | Pipeline realtime | LocalAI Realtime API + OmniVoice HQ |
| Riconoscimento voci | Registra e identifica impronte | LocalAI /v1/voice/register e /v1/voice/identify (SpeechBrain ECAPA-TDNN); diarizzazione /v1/audio/diarization (sherpa-onnx). Archivio impronte solo in memoria: va reso persistente da noi |
| Database ascoltatori | Persone, consenso, cosa è stato raccontato per QID e livello | Da scrivere (SQLite) |
| Ricerca per posizione | QID vicini (Wikidata P625) ordinati per importanza e direzione | Da scrivere (indice spaziale sui dati Wikidata) |
| Orchestratore | Presenti + QID vicini + memoria → prompt e tool di Philippe | LocalAGI come base agenti, oppure codice nostro |
| Memoria testuale | Note libere di conversazione | Louise (già sviluppato; LocalRecall non necessario) |

### Tool di Philippe

- `search_nearby(posizione, raggio)`
- `search_graph(qid)`
- `search_rag(collection, query)`
- `mark_told(persone, qid, livello)`
- `register_voice(nome, consenso)`

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
