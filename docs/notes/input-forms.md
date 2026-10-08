# Input forms: what an encoder is fed is part of the space

*Operator's decision, 2026-10-08 ("Yes"), after pubmed's index was found to
hold two input forms of one model. The spec amendments it names are 02 §2.4,
09 §9.3 and 03 §3.3.*

## What was wrong

A space said "these texts through this model" and never said in what FORM
the texts reach the model. PubMed's index holds 35,417,887 vectors imported
from NCBI's MedCPT archive — title and abstract encoded as a tokenizer
**text pair** (the abstract the second segment) — and ~4.58 M embedded on
this node from Louise's chunk text, "title\n\nabstract". Same model, same
weights; measured on the node 2026-10-08:

| encoding | vs NCBI's archive vectors (min / median) |
|---|---|
| pair, OpenVINO on the Arc GPU | 1.00000 / 1.00000 |
| pair, PyTorch CPU (control) | 1.00000 / 1.00000 |
| plain "title\n\nabstract", same model | 0.98246 / 0.98926 |

The 0.95 tolerance, which exists to absorb ENGINE noise, absorbed a different
INPUT too, so both forms were admitted to one space. And nothing stopped any
code from feeding the low-level client any text for any collection — the
probe that found this did exactly that.

## The rule

**Every layer's encoder input is the model's documented input format,
declared in the Codex and inside `space_id`.** Any peer can reproduce or
verify any vector from the Codex alone; no input drift can hide inside the
tolerance.

## The Codex (version 5)

A layer gains `input`:

- `{"form": "text"}` — the chunk's text as it stands (every collection so
  far; a layer of a Codex before version 5 reads as this);
- `{"form": "pair", "split": "first-section"}` — a tokenizer text pair: the
  record's first section's body (the title) and the rest of its sections'
  bodies joined by "\n\n" (the abstract). Admitted only on a chunker that
  makes ONE chunk per record (`abstract`): a pair is a record's, not a
  window's.

`layers` is already hashed whole into `space_id`, so the form is inside it.
A Codex already written keeps its version and its id (09 §9.3).

## Louise

ONE entry point builds the encoder's input for a chunk from its layer's
form — `embeddings.embed_chunks(collection, chunks)` — and the low-level
client is private to `app/embeddings.py`; a guard test fails on any other
caller (as tests/test_invariant_guards.py does for torch). Probes, checks,
the gate and calibration go through it too, so they measure what the
pipeline would write. A chunk of a pair layer carries its two fields
beside its text (`fields`), which neither its id nor `chunks_root` covers.

The request: the OpenAI-style embeddings body plus one field,
`input_pairs: [[first, second], …]`, in place of `input`. A list of two
strings already means two texts in that style; a separate field is not
ambiguous.

## RAGE

The front accepts `input_pairs` only for a model whose YAML declares the
option `pairs:true` — which therefore enters the run's engine options and so
its `run_id` — and refuses it (400) for any other, never flattening a pair
into a string. LocalAI hands a backend one string per embedding, so the
front encodes each pair as one marked string the pair backend decodes, and
refuses a plain input that carries the marker. On this node the pair backend
is MedCPT's article encoder on OpenVINO's GPU plugin
(`louise-medcpt-article-ov`, beside `louise-medcpt-rerank`, sharing
`medcpt.make_encoder`, which passes `token_type_ids`). llama.cpp keeps the
plain-text spaces; a backend that later learns pairs, on any vendor, serves
pair spaces without touching a collection.

## Before any pair collection exists (found at phase 2, 2026-10-08)

- **The gate measures the form the pipeline writes.** Operator, 2026-10-08
  ("yes"): a pair space's calibration set is 512 title-and-abstract pairs
  from the collection's own records, so the gate and every probe embed
  pairs. A plain-text gate would pass a backend that flattens pairs — the
  defect this note exists for.
- **Paths that re-embed stored text re-chunk from the records** (verify-on-
  use, T3/T4, bundle import, the export challenge): stored rows do not keep
  `fields`, and the records give them back byte for byte.

## Migration

1. pubmed: a version-5 Codex declaring the pair form; NCBI's 35.4 M vectors
   carried (RULE 1: a by-the-book build re-imports exactly them); the 4.58 M
   local ones re-embedded in pair form on the GPU.
2. gutenberg, wikipedia: version-5 Codexes declaring `text`; vectors carried
   (RULE 1, measured: gutenberg within the encoder's own run-to-run variation,
   wikipedia's archive layer at cosine 0.99994–0.99999).
3. The collections built on gutenberg's space follow it.
