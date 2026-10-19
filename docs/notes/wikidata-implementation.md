# wikidata and gutenberg-graph — how they become Louise collections

*Plan, 2026-10-18 (night, unattended). The design is
[wikidata-plugins.md](wikidata-plugins.md) (the operator's, with what was
measured against the origin). This note maps it onto docs/01–09, names each
amendment, lists the pitfalls past collections paid for, and separates what
is built from what waits for a decision.*

## 1. Decisions for the operator (nothing below them is pinned until answered)

| | question | why it is the operator's | recommendation |
|---|---|---|---|
| **D1** | **Origin form.** HF corpus (`philippesaade/wikidata`, as designed) or the official Wikidata JSON dump of the same date? | Invariant 11: Wikimedia's cleaner drops `snaktype` (no value vs unknown value), statement ids, a property's `datatype`, sitelink badges, `modified`/`lastrevid`. "If you cannot decide, ASK." The 2026-05-07 dump is **gone** from dumps.wikimedia.org (the oldest entity dump listed on 2026-10-18 is 20260902; archive.org shows none). So: **with D2 = embed ourselves, the current official dump** (complete; twice a week upstream, so the collection can follow it monthly, where the HF corpus is a one-off). **With D2 = carry Wikimedia's texts, the HF corpus** (the same snapshot as their vectors), its losses on the card. |
| **D2** | **The archive's vectors.** 85.7 % reproduce exactly; ~14 % are vectors of an older text (their cache keeps vectors of entities whose `modified` did not move). | Promise 8: carrying them all is a wrong artefact that passes every check. | **Ask Wikimedia for the chunk texts** (Philippe Saade, Wikidata talk:Embedding_Project): with them a `verbatim` layer carries 100 % (the wikivec precedent). Meanwhile embed nothing in bulk. Failing that, embed it ourselves (53.7 M chunks: ~41 nights at batch 32). |
| **D3** | **Languages and layers.** An entity is one record in every language; the design adds languages one at a time. | Invariant 1: one chunker per layer, every document in exactly ONE layer; the chunker is in `space_id`. | One layer whose chunker cuts the record's text in each of a declared **language list** (a chunker parameter); chunk ids carry the language. A language added = a new chunker parameter = a new Codex in the lineage, with **amendment A-W1**: a chunker whose output for every record is a superset of its mother's (only new languages' chunks added) carries the mother's vectors as they are (RULE 1), and embeds only the new language. |
| **D4** | **Dates and coordinates.** Their text formats them through a pinned Wikibase (`wbformatvalue`, mw1.44.0). | A chunker that calls a service is new to the platform. | The formatter as a **pinned image** the chunker runs beside it (digests in the chunker's parameters), deferral when it is unreachable (never an error on a document), and a Python port only if it is proven equal over every distinct value of the corpus. |
| **D5** | **gutenberg-graph's shape** (§4): an annotations collection, amended. | Wire format (09 §9.5, §9.14) and 02 §2.10. | Amendment A-K1..K4 below. |
| **D6** — DECIDED 2026-10-18 (operator: "Yes match it, if in the future they fix it") | **A new dependency: `tokenizers`** (Hugging Face's, Rust, no torch) in the platform image. The chunker cuts at 1,024 tokens of jina-v3's tokenizer; the app has no real tokenizer today (`_est_tokens` is chars/4). | CLAUDE.md: new dependencies, ask first. | Add it, pinned. The alternative, asking the encoder backend to tokenize, makes chunking depend on the GPU service. |

## 2. wikidata — the mapping

| design | Louise |
|---|---|
| `collection: wikidata` | plugin `wikidata`, a `CorpusSource` (app/plugins/wikidata), registered in `PLUGIN_REGISTRY` and `ACQUIRE_REGISTRY` |
| `source.corpus` | origin form per D1; acquisition by `Transport.HF_HUB` (metered by the transfer manager, invariant 5) or the dump over HTTPS |
| one entity | one record; `uid = wikidata:<QID>`; native id order = QID number; every field the origin gives kept, `extra` for what has no §9.5 home (labels, descriptions, aliases in every language, sitelinks, claims whole) |
| `entity_id: wikidata_qid` | a reference source `wikidata-refs` registering namespace `wikidata` (QIDs, PIDs) — then, and only then, records write `keys` (05 §5.2; memory keys-registered-only) |
| `chunker: WikidataTextEmbedding textifier` | chunker `wikidata-text` v1: Wikimedia's code vendored at the commits measured (WikidataTextifier 0befd7c BSD-3, WikidataTextEmbedding c9dc819 MIT, notices kept), parameters: language list (D3), property-page revision (pinned bytes), formatter image digests (D4), max 1,024 tokens of jina-v3's tokenizer |
| `embedding` | space: jina-v3 `ab036b0` + remote code `bd55a5e` (weights digest over the folder), adapter `retrieval.passage`, **dim 512** (measured, not 1024), normalised; query encoder: the same weights, `retrieval.query`; dtype f16 like the other spaces unless the gate says otherwise; engine = `louise-python` task `jina-v3` behind VeGa |
| `model_license: CC BY-NC 4.0` | the space's encoder licence, on the card; commercial peers consume, never contribute (as DAR-legal-it) |
| `build.base` (block 0) | per D2: an archive entry `wikidata-embedding-project-0.2` in `genesis.ARCHIVES` (no keys: a hub dataset signs nothing), credited `kind: "archive"` in the recipe, an adapter reading `id`/`vector` (base64 float32) and mapping `<QID>_<lang>_<n>` to chunk ids |
| `verification` | the calibration gate (03 §3.3) and `vectors-sample`; tolerance measured on the engine against itself (the probe: 1e-6 short, 3–5e-5 long) |
| `languages.extension` | per D3, a daughter Codex per language with A-W1 |
| `store: LanceDB` | as every collection |

## 3. Pitfalls past collections paid for, and where each is answered

From legal-collection-template.md §11–12, STATUS "Lessons", and memory.

| pitfall | here |
|---|---|
| Guinea pig: never list the new collection as "in use before"; fix the tooling it finds missing | wikidata is the next guinea pig after normattiva |
| Measure the origin before pinning (scope, ids, sizes) | done in part: 7,449 corpus files / 74.5 M entities; vectors 512-d, 22.67 M en rows; id layout `<QID>_<lang>_<n>`. Still to do: collide QID ids at scale (P vs Q namespaces in one native order) |
| "Their specs are the source of the truth" | the 1024 → 512 correction; the 53.7 M vs 44.6 M row count to reconcile from the files |
| A record parsed from bytes `origin_hash` does not cover (gutenberg rdf) | HF form: one row is one unit, self-contained. Dump form (D1): the labels of referenced entities come from other units — `tests/origin_coverage.py` would flag it; a pinned label table per snapshot is the design to write |
| Keys only from a registered namespace | `wikidata-refs` before any key |
| Fidelity: keep every field | the cleaner's losses (D1); `extra` keys fixed for the collection's life |
| Collide ids at real scale before the layout is pinned (normattiva 3ba1e0a) | to run over all 7,449 files' footers (ids are in the statistics: no download) |
| Read the second fetch pass's counters, not the first's | for the guinea pig's first passes |
| Read the vector table schema after the first embedded row | as normattiva §11.9 |
| Storage: origins and SHARDs on tank, what queries read on SSD | ~1 TB origin on `/corpora`; Lance (~55 GB at 512-d f16 for 4 languages) on ssd4 — measure before creating |
| Save and tag the Codex's platform image the day it is created | codex-images-kept |
| Quiet hours: no sustained embedding 07:00–23:00 | any bulk embed or KG pass runs at night; the collection file's embed mode flipped by cron like gutenberg's |
| One GPU job at a time; never overcommit the card | the KG's Gemma Q8_0 (~27 GB) cannot share the B70 with the encoders |
| Licence gate before the first byte | CC0 corpus = green; the encoder's NC licence on the card |
| `dev carry` MOVE took a live collection offline for 10 h | `--copy` always |

## 4. gutenberg-graph — an annotations collection, amended

02 §2.10 already has the kind: claims made by a method about another
collection's records, shared, adopted, checked; no space, no origin to
re-fetch. What it lacks for this design, as named amendments:

- **A-K1 claim types.** `mention` (target chunk, start, end, QID, score),
  `relation` (subject QID, relation, object QID, evidence chunk), `community`
  (id, level, QIDs, summary). Today the set is closed at `same-work`,
  `translation-of` (app/hold/annotations.py `CLAIM_TYPES`).
- **A-K2 ends.** A claim's end may be a canonical id `wikidata:Q…` (09 §9.14
  already allows `<namespace>:<id>` left unresolved) and pinned to a CHUNK of
  the target (`chunk_id` + the chunk's text hash), not only a record.
- **A-K3 two targets.** `annotates` a list: gutenberg (the text) and wikidata
  (the entity space the links point into), each with its snapshot.
- **A-K4 the packets.** The design's Parquet packets are the claims' parts in
  Parquet (09 §9.5 parts are JSONL today): at ~10⁸–10⁹ mentions, JSONL is
  the wrong container. A wire-format change: CLAUDE.md asks first.
- **Verification** of LLM output that is not bit-exact ("verification: TBD"):
  the method's id, model digest, prompt hash and temperature 0 make a claim
  reproducible in principle; a check re-runs a sample and counts agreement.
  The tolerance is a measurement to make.

Models (licences read from the Hub, 2026-10-18):
- **GLiNER 2.5** (`fastino/gliner2.5-multi-v1` cf5593a, Apache-2.0). LocalAI's
  `vllm-cpp` gained it on 2026-09-23 (PR #12140, after the v4.11.0 the node
  runs). Alternatively a `louise-python` task (torch; one backend per engine).
- **Gemma 4 26B A4B** (`google/gemma-4-26B-A4B-it` 4d7ae49, Apache-2.0;
  `unsloth/gemma-4-26B-A4B-it-GGUF` c099eb4 has `gemma-4-26B-A4B-it-Q8_0.gguf`).
  ~27 GB on the B70, so it runs alone, at night.

Order: it needs the wikidata collection (entity linking searches its vectors),
so it follows wikidata.

## 5. Built so far (2026-10-18)

- `louise-python` task `jina-v3` (9875259): measured on the B70 offline,
  cosine 0.9999994 to Wikimedia's vector for the same text. Not installed on
  the node (a new tree is a new run for the five live models).
- The probe (node, `/mnt/tank/apps/localai-probe/wikidata/`): Wikimedia's code
  at pinned commits, their formatter, the archive's vectors — the numbers in
  wikidata-plugins.md.
