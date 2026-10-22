# arXiv — plugin design (2026-10-22)

**PARKED (operator, 2026-10-22): a blueprint kept for future testing and development. Nothing is built, nothing is downloaded from arXiv, and no session works on it until the operator resumes it.**

The first collection, abstracts only, is designed in full in
docs/notes/arxiv-collection.md (operator, 2026-10-22).

Operator, 2026-10-22: "Design a plugin for it, check if there are
pre-embeddings and bulk download options." This is the design; nothing is
built. The three decisions it needs are in §6.

## 1. What arXiv offers in bulk

| what | where | licence | size | notes |
|---|---|---|---|---|
| **Metadata of every paper** (ids, versions and dates, authors, title, abstract, categories, comments, journal-ref, DOI, MSC/ACM classes, report numbers, **the paper's licence**) | arXiv's OAI-PMH endpoint, format `arXivRaw` (the endpoint URL confirmed at build time) | CC0, whatever the paper's own licence | ~2.9 M records; a few GB | incremental by datestamp, resumption tokens; the official per-record form |
| the same, as one JSON file | Kaggle `Cornell-University/arxiv`, weekly (an account needed); the copy in `gs://arxiv-dataset/metadata-v5/` is from August 2020 | CC0 | ~4.5 GB | a derived snapshot, not per-record re-fetchable: not the origin |
| **Full text, LaTeX source** | S3 `s3://arxiv/src/` (requester pays, ~500 MB tars, manifest), or per paper `https://arxiv.org/e-print/<id>` | the paper's own | several TB in all | requester pays: the downloader pays AWS egress |
| PDFs | S3 `s3://arxiv/pdf/` (requester pays); GCS `gs://arxiv-dataset` (Kaggle-sponsored, free, anonymous, weekly) | the paper's own | counted 2026-10-22: 5,092,298 files, 10.8 TB; latest version of each of 3,210,040 papers, 6.85 TB | every one of 31 sampled 1993–2026 has a text layer; prose extracts cleanly, equations do not |

**THE LICENCE SPLITS THE CORPUS.** Most papers carry arXiv's default
licence, a non-exclusive licence to ARXIV to distribute; it grants nobody
else redistribution, so their full text cannot travel between peers.
Authors may choose CC BY, CC BY-SA, CC BY-NC(-SA/-ND), CC0 instead, and the
choice is irrevocable. Common Pile counted the openly licensed ones (CC BY,
CC BY-SA, CC0) at about 317 K papers through 2024, roughly 12 % (their
`arxiv_papers`: LaTeX to text, 2.4 GB of parquet). The metadata, abstracts
included, is CC0 for every paper. arXiv also asks tools built on full text
to link back to arXiv for downloads.

## 2. Pre-embeddings found

| set | coverage | model, dims | date | usable here? |
|---|---|---|---|---|
| Qdrant / Alexandria `arxiv-abstracts-instructorxl-embeddings` | ~2.3 M abstracts | InstructorXL, 768 | 2023 | an archive (06 §6.6 step 5, empty key set) IF InstructorXL runs deterministically on our cards and the chunk is the abstract byte for byte with its instruction; a 2022 model, well below Qwen3-Embedding; nothing after 2023 (~600 K papers to embed anyway) |
| Qdrant `arxiv-titles-instructorxl-embeddings` | ~2.25 M titles | InstructorXL, 768 | 2023 | titles alone: not a retrieval unit we use |
| Semantic Scholar `embeddings-specter_v2` (Datasets API, monthly) | every S2 paper, arXiv included, title + abstract | SPECTER2, 768 | monthly | strong for paper-to-paper similarity; API key needed; the dataset licence must be read before it is credited; query side needs SPECTER2's ad-hoc query adapter |
| many small sets (MongoDB subset, cs.CL 10 K, bge-small, gte-base, ada-002, 480 K, cs 2021 bge-m3) | partial | various | 2022–2026 | partial coverage: not worth a space |

No full-corpus, current embedding of arXiv in a model we already run
(Qwen3-Embedding-4B, bge-m3) exists.

## 3. Two collections, by licence (invariant 9, 04 §4.4)

**`arxiv` — every paper's record, embedded on its title and abstract.**
Green, public. The record keeps EVERYTHING arXivRaw says (invariant 11):
all versions with their dates and sizes, submitter, authors as written
and parsed, categories (primary first), comments, journal-ref, DOI, report
numbers, MSC/ACM classes, licence URL; `extra.<name>` where §9.5 has no
home. The text is the title and the abstract, one layer, one chunk per
paper (an abstract averages ~1,000 characters; the longest fit in one
window). This is the collection a reader searches ("which papers say X"),
and it links every hit to arxiv.org — the full text stays one click away,
as arXiv asks.

**`arxiv-open` — the full text of the openly licensed papers.** Yellow
(CC BY and CC BY-SA require attribution, BY-SA share-alike; one ack covers
them, the per-record licence is kept in the record and shown on each hit).
Only CC BY, CC BY-SA and CC0 are admitted; NC and ND are left out because
a peer cannot promise what others do with what it shares. Text from the
paper's own LaTeX (06 §6.4: the origin form that best fits — the PDF is a
rendering of it; LaTeX keeps sections, equations and captions), parsed by
a vendored LaTeX-to-text step pinned in the image, sections from
`\section`; figures later (record_line 2, the source tar carries them).

The default-licence papers' full text is NOT a collection: it could only
be a never-share, local-only one, and a peer network gains nothing from
it. Their records and abstracts are in `arxiv`.

## 4. The plugin

- `source_id = "arxiv"`, uid `arxiv:<id>` with the id as arXiv writes it
  (`2310.01234`, `hep-th/9901001`), `native_id_order = "bytes"` (old ids
  sort before new ones by their archive name, which is acceptable: the
  order only needs to be fixed).
- `mutability = mutable` (a new version replaces the text; the record
  keeps every version's metadata), `erasure = retractable` (withdrawn
  papers: the tombstone stays, 04 §4.4).
- Acquisition: OAI-PMH `arXivRaw`, harvested once in full, then by
  datestamp; one request at a time with the delay arXiv's terms ask,
  every byte through the transfer manager (invariant 5); resumable at the
  resumption token (invariant 2). `origin_hash` over the OAI record's
  bytes; `current()` re-fetches one record by `GetRecord`, so
  `records-vs-origin` and freshness both work.
- `arxiv-open` adds one fetch per admitted paper: `e-print/<id>` (the
  version the record names), never the S3 bulk (requester pays, and 88 %
  of it is text we may not keep). ~317 K fetches, polite rate: days, once.
- Facets: primary category, categories, year, licence.
- RULE 2: the Library shows "arXiv" and "arXiv — open full text"; a user
  picks a name and takes it; nothing asks for an endpoint or a model.

## 5. Cost, if embedded locally (the recommendation below)

`arxiv`: ~2.9 M chunks of ~250 tokens ≈ 0.75 B tokens. At ~1,500
tokens/s (Qwen3-Embedding-4B, a peer's published benchmark for gutenberg's space) that is ~140
GPU-hours: about three days on the node and Perugia together. New papers
(~25 K a month) are an hour a month. `arxiv-open`: ~317 K papers, ~10 K
tokens each ≈ 3 B tokens, ~4× the abstracts; worth it only after the
first collection is in use.

## 6. Decisions for the operator

**D1 — the encoder of `arxiv`.**

| choice | effect |
|---|---|
| **Qwen3-Embedding-4B, embedded here** | the model gutenberg already runs, measured bit-exact; ~140 GPU-hours once, then ~1 h a month |
| InstructorXL, importing Alexandria's 2.3 M vectors | saves ~110 of those hours; a weaker 2022 model, a new engine to deploy and prove deterministic (torch), 600 K newer papers embedded anyway |
| SPECTER2, importing Semantic Scholar's | best paper-to-paper similarity, monthly updates; an API key and a dataset licence to clear, a query adapter, a new engine |

Recommendation: Qwen3-Embedding-4B. Silence: the same.

**D2 — the open full text.**

| choice | effect |
|---|---|
| **later, after `arxiv` is in use** | nothing fetched now; decided on what readers actually search |
| now, beside `arxiv` | ~317 K e-print fetches and a LaTeX parser to build and audit first |

Recommendation: later. Silence: later.

**D3 — Common Pile's converted text** (for `arxiv-open`, when it comes):
not used as the origin — it cannot be re-fetched per paper, and its
conversion is not ours to verify (invariant 11); at most a cross-check of
our parser.
