"""Which encoder actually retrieves Italian statute best — measured, on the
one public set that asks that exact question.

    python -m tools.encoder_bakeoff fetch  --out /data/bakeoff
    python -m tools.encoder_bakeoff embed  --out /data/bakeoff \
        --name bge-m3 --url http://nas:30437/upstream/bge-m3 --model bge-m3
    python -m tools.encoder_bakeoff score  --out /data/bakeoff
    python -m tools.encoder_bakeoff report --out /data/bakeoff

WHY THIS EXISTS. `gazzetta-plugin.md` section 7 says the encoder for a legal
collection is chosen on retrieval quality on Italian legal text, and that
what this node happens to run is NOT an input to that decision. Section 7.2
names the set that decides it — JuriFindIT (Dalla Noce et al., Findings of
EACL 2026), the first statutory-article-retrieval set for Italian — and
12.4 recorded its availability as UNVERIFIED because egress blocked the
check. It is verified: `jurifindit/JuriFindIT` is published as parquet.
This tool turns 7.2's procedure into something runnable against the model pool.

WHAT IS MEASURED. The 179 expert questions of the `validation` split against
the 23,617-node `corpus` split, scored with the metric definitions of
sentence-transformers' `InformationRetrievalEvaluator` — the evaluator the
one published competitor, `jurifindit/DAR-legal-it`, reports against. The
definitions are reimplemented here rather than imported because invariant 7
keeps torch out of the app and this must run in the plain dev image; they
are pinned by hand-computed tests (tests/test_encoder_bakeoff.py).

RECALL IS NOT HIT RATE HERE. A question names 4.49 relevant articles on
average and up to 128, so "did we find one" and "what fraction did we find"
are different numbers. The published table is the second one; computing the
first and comparing is a free win that is not real.

WHAT IS COMPARABLE, AND WHAT IS NOT. The models embedded through this tool
are compared to EACH OTHER rigorously: same corpus, same truncation, same
metric code, same run. Comparison to DAR-legal-it's PUBLISHED numbers is
weaker — the authors' truncation, and whether they embedded the 159
structural placeholder nodes, are not recorded anywhere we can read. Treat
their row as a reference, not as a measurement made here, until that model
is served and run through this same path.

QUERY PREFIXES, AND WHY THEY ARE A RUN PARAMETER. This codebase applies
none: `embeddings._side` switches URL for a bi-encoder pair (MedCPT) and
nothing else, so a query reaches the encoder exactly as typed. That is
right for bge-m3, which is symmetric. It is NOT what Qwen3-Embedding
documents — it expects `Instruct: <task>\nQuery: <text>` on the query side
and bare text on the document side — so measuring it bare measures what
this node serves today, not what the model can do. Both are worth knowing
and the second is nearly free: only the query side carries a prefix, so a
variant re-embeds 179 vectors and reuses all 23,617 document vectors.
`--query-prefix` therefore keys the query shards, never the corpus ones,
and the text used is recorded in the run's JSON. A prefix that silently
differed between two rows would be the whole result.

THROUGHPUT COMES FROM CONCURRENCY, NEVER FROM BATCH. `config.EMBED_BATCH`
is 1 on purpose and must stay 1 here: raising it is the batch gate this
codebase already paid for, because llama.cpp's batched embedding is not
deterministic per batch shape and its prompt cache can then replay a
corrupted result into later single-text requests. A bake-off that raised it
would produce a full, plausible, internally consistent table of wrong
numbers — the worst artefact available. Use `EMBED_CONCURRENCY=2` instead
(parallel single-text requests are safe, and the B70 measures throughput
plateauing at 2 anyway — `llama-intel-arc-b70`'s concurrency grid):

    docker run ... -e EMBED_CONCURRENCY=2 ... python -m tools.encoder_bakeoff embed ...

LICENCE. JuriFindIT is CC-BY-NC-SA-4.0 — a gated class under invariant 9.
`fetch` writes LICENCE.txt and a sha256 of every file beside the data so an
acknowledgement can be recorded against bytes that were actually seen.

OTHER SETS, ONE READER (2026-10-28). The French, German and EU collections
choose their encoders on published sets, not on questions of our own
(operator, D4 of docs/notes/legal-fr-de-eu.md: "do not reinvent the wheel").
MTEB and MLEB publish theirs in the BEIR layout (Thakur et al., NeurIPS
2021): a corpus, queries, and qrels of (query-id, corpus-id, score). That
layout is the standard, so this reads it once rather than one reader per
set; `fetch --suite` names the set (SUITES), and one output directory holds
one set. A document is `title + " " + text` as MTEB embeds it; a query
counts when the qrels give it a document with score > 0, as BEIR does. The
metric code is the same for every set.

WHAT LOUISE SERVES IS THE HYBRID (2026-10-28). A search is never the vector
leg alone: the word leg (tantivy, BM25, the language's stemmer) and the
vector leg are fused by RRF (05 §5.1), then reranked. On German case law
BM25 alone beats every dense model (GerDaLIRSmall nDCG@10 59.4 against at
most 54.7, MTEB's results), so an encoder is chosen on what it ADDS to the
word leg. `lexical` ranks the set with app/fts.py itself — a fresh index
built by `fts.index_books`, each question through `fts.search`, the
analyzer, stemmer and query parsing Louise serves, not a BM25 library — and
`score` then fuses each encoder's ranking with it through
`retrieval._merge_rrf`, the search's own fusion (RRF_K, its depth, its tie
rule). The report gives three rows per encoder set: BM25, the encoder, and
the two fused. Both legs read the same truncated text per document; the
reranker after the fusion is not measured here yet.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq

# `app` is imported lazily, inside the functions that embed. Everything else
# here — load, the metrics, score, report — depends only on pyarrow and
# numpy, so a sibling embedder that needs torch (tools/bakeoff_local.py, in
# an image the app's stack is not in) can import this module for the corpus,
# the truncation and the slug and be scored by exactly the same code.

BASE = ("https://huggingface.co/datasets/jurifindit/JuriFindIT/"
        "resolve/refs%2Fconvert%2Fparquet")
FILES = {"corpus.parquet": "corpus/corpus/0000.parquet",
         "questions-validation.parquet": "questions/validation/0000.parquet"}
LICENCE = "CC-BY-NC-SA-4.0 — https://creativecommons.org/licenses/by-nc-sa/4.0/"

_MTEB = {"corpus": "corpus/test-00000-of-00001.parquet",
         "queries": "queries/test-00000-of-00001.parquet",
         "qrels": "qrels/test-00000-of-00001.parquet"}
_BEIR = {"corpus": "corpus.jsonl", "queries": "queries.jsonl", "qrels": "qrels/test.jsonl"}

#: The sets this reads. Licences as their cards state them (2026-10-28); an
#: "unknown" one is resolved before use like any other (invariant 9).
#: `green`: a permissive licence (CC BY, MIT, Apache-2.0), which invariant 9
#: does not gate; every other set is gated until acknowledged.
#: docs/notes/legal-embedders-fr-de-eu.md says which collection each serves.
SUITES: dict[str, dict] = {
    "jurifindit": {"repo": "jurifindit/JuriFindIT", "layout": "jurifindit",
                   "licence": LICENCE, "about": "it; Italian national and EU acts; 179 expert questions"},
    "bsard": {"repo": "mteb/BSARDRetrieval.v2", "layout": "beir", "files": _MTEB,
              "licence": "CC-BY-NC-SA-4.0",
              "about": "fr; Belgian statutes; 222 citizens' questions labelled by jurists"},
    "syntec": {"repo": "mteb/SyntecRetrieval", "layout": "beir", "files": _MTEB,
               "licence": "unknown (the card says so)",
               "about": "fr; a collective agreement; 100 hand-written questions"},
    "legalquad": {"repo": "mteb/LegalQuAD", "layout": "beir", "files": _BEIR,
                  "licence": "CC-BY-4.0", "green": True,
                  "about": "de; court decisions; 200 questions"},
    "gerdalir-small": {"repo": "mteb/GerDaLIRSmall", "layout": "beir", "files": _BEIR,
                       "licence": "MIT", "green": True,
                       "about": "de; case law; queries derived from citations"},
    "gdpr-holdings": {"repo": "isaacus/gdpr-holdings-retrieval", "layout": "beir",
                      "files": {**_BEIR, "qrels": "default.jsonl"},
                      "licence": "CC-BY-NC-SA-4.0",
                      "about": "en; GDPR decisions (MLEB); facts to holdings"},
}

# SETS PUBLISHED IN ANOTHER SHAPE, MADE BEIR AT FETCH by their papers' own
# method, one suite per language: the raw files land in raw/, the adapter
# writes corpus/queries/qrels.jsonl beside them, and `load` reads those like
# any BEIR set. Both cover every EU language — the compromise encoder's
# evidence for the collections beyond the four (legal-embedders-fr-de-eu.md).
_LOCAL = {"corpus": "corpus.jsonl", "queries": "queries.jsonl", "qrels": "qrels.jsonl"}
#: EUR-Lex-Sum (Aumiller et al., EMNLP 2022): the EU's own summaries of its
#: acts; a test summary is the query and its act the gold, among every
#: split's acts — summary-to-act retrieval, as MLEB uses its Irish and UK
#: summaries. Its card says CC BY 4.0 and its loader CC BY-NC-SA 4.0: the
#: stricter is recorded, so it passes the gate as a gated set.
_EURLEX_SUM = {"bg": "bulgarian", "cs": "czech", "da": "danish", "de": "german",
               "el": "greek", "en": "english", "es": "spanish", "et": "estonian",
               "fi": "finnish", "fr": "french", "ga": "irish", "hr": "croatian",
               "hu": "hungarian", "it": "italian", "lt": "lithuanian", "lv": "latvian",
               "mt": "maltese", "nl": "dutch", "pl": "polish", "pt": "portuguese",
               "ro": "romanian", "sk": "slovak", "sl": "slovenian", "sv": "swedish"}
for _code, _name in _EURLEX_SUM.items():
    SUITES[f"eurlex-sum-{_code}"] = {
        "repo": "dennlinger/eur-lex-sum", "layout": "beir", "adapter": "eurlex_sum",
        "remote": {f"{split}.json": f"data/{_name}/{split}.json"
                   for split in ("train", "validation", "test")},
        "files": _LOCAL,
        "licence": "CC-BY-NC-SA-4.0 (its loader; the card says CC BY 4.0 — the stricter is taken)",
        "about": f"{_code}; EU acts; the EU's own summaries as queries (test split)"}
#: LEMUR (Baba Ahmadi et al., EACL 2026 SRW): EU environmental acts; the
#: act's metadata header is the query and its text the gold, over the test
#: split. A law's first row is taken (the card describes rows as pages and
#: as conversions; `fetch` prints how many rows a law has).
_LEMUR = ("bg", "cs", "de", "el", "en", "es", "et", "fi", "fr", "ga", "hr", "hu", "it",
          "lt", "lv", "mt", "nl", "pl", "pt", "ro", "sk", "sl", "sv")
for _code in _LEMUR:
    SUITES[f"lemur-{_code}"] = {
        "repo": "G4KMU/LEMUR", "layout": "beir", "adapter": "lemur",
        "remote": {"test.jsonl": f"languages/{_code}/test.jsonl"}, "files": _LOCAL,
        "licence": "Apache-2.0", "green": True,
        "about": f"{_code}; EU environmental acts; the act's metadata as the query (test split)"}


def _write_beir(out: Path, docs: dict[str, str], queries: dict[str, str]) -> None:
    """`docs` id -> text, `queries` the gold document's id -> query text, in
    the BEIR layout `load` reads: one relevant document per query."""
    def lines(path: Path, rows) -> None:
        path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                        encoding="utf-8")
    lines(out / _LOCAL["corpus"], ({"_id": i, "title": "", "text": t} for i, t in docs.items()))
    lines(out / _LOCAL["queries"], ({"_id": f"q:{i}", "text": t} for i, t in queries.items()))
    lines(out / _LOCAL["qrels"], ({"query-id": f"q:{i}", "corpus-id": i, "score": 1}
                                   for i in queries))


def _adapt_eurlex_sum(raw: Path, out: Path) -> str:
    docs: dict[str, str] = {}
    queries: dict[str, str] = {}
    for split in ("train", "validation", "test"):
        for row in _rows(raw / f"{split}.json"):
            docs.setdefault(row["celex_id"], row["reference"] or "")
            if split == "test" and (row.get("summary") or "").strip():
                queries[row["celex_id"]] = row["summary"]
    _write_beir(out, docs, queries)
    return f"{len(docs)} acts, {len(queries)} test summaries"


def _adapt_lemur(raw: Path, out: Path) -> str:
    docs: dict[str, str] = {}
    queries: dict[str, str] = {}
    rows_of: dict[str, int] = {}
    for row in _rows(raw / "test.jsonl"):
        law = row["law_id"]
        rows_of[law] = rows_of.get(law, 0) + 1
        if law in docs:
            continue
        docs[law] = row.get("text") or ""
        if (row.get("metadata") or "").strip():
            queries[law] = row["metadata"]
    _write_beir(out, docs, queries)
    return (f"{len(docs)} acts, {len(queries)} metadata queries; "
            f"rows per act up to {max(rows_of.values(), default=0)}")


_ADAPTERS = {"eurlex_sum": _adapt_eurlex_sum, "lemur": _adapt_lemur}

# Every model in this bake-off declares ctx 8192, and llama.cpp REFUSES an
# input past its context rather than truncating it, so the cut is ours to
# make and must be identical for every model or the comparison is void.
# Italian legal text runs ~3.2 chars/token on a multilingual BPE; 2.5 is the
# conservative floor, which keeps the worst-tokenising text inside 8192.
DEFAULT_MAX_CHARS = 20_000
SHARD = 512
KS = (1, 3, 5, 10, 20, 40, 60, 80, 100)


@dataclass(frozen=True, slots=True)
class Corpus:
    ids: list[int]
    texts: list[str]


@dataclass(frozen=True, slots=True)
class Queries:
    texts: list[str]
    relevant: list[list[int]]


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _download(url: str, target: Path) -> None:
    """Whole or not at all: a file cut mid-stream would be taken as fetched
    by the next run, and a truncated JSON-lines set loses queries silently."""
    import httpx
    tmp = target.with_name(target.name + ".part")
    try:
        with httpx.stream("GET", url, follow_redirects=True, timeout=300) as r:
            r.raise_for_status()
            with tmp.open("wb") as fh:
                for block in r.iter_bytes():
                    fh.write(block)
        tmp.replace(target)
    finally:
        tmp.unlink(missing_ok=True)


def suite_of(out: Path) -> str:
    """The set a directory holds. A SOURCE.json from before suites existed
    names JuriFindIT's dataset and no suite."""
    try:
        meta = json.loads((out / "SOURCE.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return "jurifindit"
    return meta.get("suite", "jurifindit")


def _local(kind: str, remote: str) -> str:
    return f"{kind}{''.join(Path(remote).suffixes)}"


def fetch(out: Path, suite: str = "jurifindit") -> int:
    if suite not in SUITES:
        raise SystemExit(f"unknown suite {suite!r}; one of {', '.join(SUITES)}")
    if (out / "SOURCE.json").exists() and suite_of(out) != suite:
        raise SystemExit(f"{out} holds suite {suite_of(out)!r}: one directory, one set "
                         f"(the runs in it are scored against it)")
    spec = SUITES[suite]
    out.mkdir(parents=True, exist_ok=True)
    adapter = spec.get("adapter")
    if spec["layout"] == "jurifindit":
        files = {name: f"{BASE}/{suffix}" for name, suffix in FILES.items()}
        base = BASE
    elif adapter:
        base = f"https://huggingface.co/datasets/{spec['repo']}/resolve/main"
        (out / "raw").mkdir(exist_ok=True)
        files = {f"raw/{name}": f"{base}/{remote}" for name, remote in spec["remote"].items()}
    else:
        base = f"https://huggingface.co/datasets/{spec['repo']}/resolve/main"
        files = {_local(kind, remote): f"{base}/{remote}"
                 for kind, remote in spec["files"].items()}
    digests = {}
    for name, url in files.items():
        target = out / name
        if not target.exists():
            _download(url, target)
        digests[name] = _sha256(target)
        print(f"{name}  {target.stat().st_size:,} bytes  sha256:{digests[name]}")
    if adapter:
        said = _ADAPTERS[adapter](out / "raw", out)
        for name in _LOCAL.values():
            digests[name] = _sha256(out / name)
        print(f"made BEIR by its paper's method: {said}")
    licence = spec["licence"]
    (out / "LICENCE.txt").write_text(licence + "\n", encoding="utf-8")
    (out / "SOURCE.json").write_text(
        json.dumps({"suite": suite, "dataset": spec["repo"], "licence": licence,
                    "about": spec["about"], "base": base, "sha256": digests},
                   indent=2) + "\n", encoding="utf-8")
    if spec.get("green"):
        print(f"\nLicence is {licence}: green, no acknowledgement needed (invariant 9 "
              f"gates the yellow and red classes).")
    else:
        print(f"\nLicence is {licence}.\nInvariant 9: record an acknowledgement "
              f"against these digests before the data is used.")
    return 0


def load(out: Path, max_chars: int) -> tuple[Corpus, Queries]:
    if SUITES[suite_of(out)]["layout"] == "beir":
        return _load_beir(out, max_chars)
    c = pq.read_table(out / "corpus.parquet").to_pylist()
    q = pq.read_table(out / "questions-validation.parquet").to_pylist()
    corpus = Corpus(ids=[r["id"] for r in c],
                    texts=[(r["content"] or "")[:max_chars] for r in c])
    queries = Queries(texts=[r["question"] for r in q],
                      relevant=[list(r["relevant_doc_ids"]) for r in q])
    missing = {i for rel in queries.relevant for i in rel} - set(corpus.ids)
    if missing:
        raise SystemExit(f"{len(missing)} relevant ids are not in the corpus — "
                         f"recall@k is undefined against it")
    return corpus, queries


def _rows(path: Path) -> list[dict]:
    if path.suffix == ".parquet":
        return pq.read_table(path).to_pylist()
    with path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _id(row: dict) -> str:
    # MTEB's newer parquet says `id`, BEIR and the older parquet `_id`
    return str(row["_id"] if "_id" in row else row["id"])


def _load_beir(out: Path, max_chars: int) -> tuple[Corpus, Queries]:
    """A BEIR-layout set as this module's Corpus and Queries. Its string ids
    become row positions, which is what `score_run` ranks by."""
    files = SUITES[suite_of(out)]["files"]
    at: dict[str, int] = {}
    texts: list[str] = []
    for r in _rows(out / _local("corpus", files["corpus"])):
        title, body = (r.get("title") or "").strip(), r.get("text") or ""
        at[_id(r)] = len(texts)
        texts.append((f"{title} {body}" if title else body)[:max_chars])
    relevant: dict[str, set[int]] = {}
    missing: set[str] = set()
    for r in _rows(out / _local("qrels", files["qrels"])):
        if float(r["score"]) <= 0:
            continue
        cid = str(r["corpus-id"])
        if cid not in at:
            missing.add(cid)
            continue
        relevant.setdefault(str(r["query-id"]), set()).add(at[cid])
    if missing:
        raise SystemExit(f"{len(missing)} relevant ids are not in the corpus — "
                         f"recall@k is undefined against it")
    rows = {_id(r): r["text"] for r in _rows(out / _local("queries", files["queries"]))}
    absent = set(relevant) - set(rows)
    if absent:
        raise SystemExit(f"{len(absent)} judged queries are not in the queries file")
    keep = [qid for qid in rows if qid in relevant]
    return (Corpus(ids=list(range(len(texts))), texts=texts),
            Queries(texts=[rows[qid] for qid in keep],
                    relevant=[sorted(relevant[qid]) for qid in keep]))


def api_for(model: str, api: str | None = None) -> str:
    """The client the endpoint needs. A VeGa-wrapped model (the node serves
    every GPU encoder as `vega-<name>`) refuses an untagged input, so it is
    asked through the app's `api = "vega"` client: tagged inputs, receipts
    from VEGA_URL verified against the bytes (docs/notes/vega.md). Until
    2026-10-29 this tool sent every model untagged, and on the node every
    GPU run was refused at its first request. `--api` overrides."""
    if api:
        return api
    return "vega" if model.startswith("vega-") else "openai"


def _recipe(url: str, model: str, dim: int | None, ctx: int, concurrency: int,
            api: str = "openai"):
    from app import collections as collections_mod
    return collections_mod.EmbedRecipe(url=url, api=api, model=model,
                                       dtype="f16", ctx=ctx, dim=dim,
                                       concurrency=concurrency)


def served_recipe(name: str):
    """A held collection's encoder exactly as its searches use it —
    `collections.recipe_for`, the worker's own resolution: url, model, api,
    ctx, dim and the space's query `instruction`, which the app prefixes to
    every query it embeds (app/embeddings.py). Normattiva's is "query: ";
    measured bare, its row was not what the node serves (München,
    2026-10-29)."""
    from app import collections as collections_mod
    for c in collections_mod.load_all():
        if c.name == name:
            return collections_mod.recipe_for(c)
    raise SystemExit(f"no collection {name!r} is held here")


def _same_settings(run: Path, settings: dict) -> None:
    """A run's shards are reusable only under the settings that made them —
    the corpus shards most of all, which every query variant shares. Under
    others the run would be one row from two encodings: plausible,
    internally consistent and wrong (as `bakeoff_local` refuses it)."""
    path = run / "settings.json"
    if path.exists():
        before = json.loads(path.read_text(encoding="utf-8"))
        if before != settings:
            changed = sorted(k for k in set(before) | set(settings) if before.get(k) != settings.get(k))
            raise SystemExit(f"{run.name}: embedded before with different settings "
                             f"({', '.join(changed)}); use another --name, or delete {run}")
    else:
        run.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(settings, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _embed_sharded(texts: list[str], where: Path, recipe, *,
                   query: bool) -> np.ndarray:
    """Shard by shard, skipping what is on disk. A run of this length must
    survive an interruption without re-spending the card (invariant 2)."""
    where.mkdir(parents=True, exist_ok=True)
    n_shards = math.ceil(len(texts) / SHARD)
    for s in range(n_shards):
        shard = where / f"{s:05d}.npy"
        if shard.exists():
            continue
        batch = texts[s * SHARD:(s + 1) * SHARD]
        from app import embeddings
        vectors = embeddings.embed_calibration(recipe, batch, query=query)
        np.save(shard, np.asarray(vectors, dtype=np.float32))
        done = min((s + 1) * SHARD, len(texts))
        print(f"  {where.name}: {done}/{len(texts)}", flush=True)
    return np.concatenate([np.load(where / f"{s:05d}.npy")
                           for s in range(n_shards)])


def slug_of(query_prefix: str) -> str:
    """Names the query side of a run. `bare` is the no-prefix case — the one
    this node actually serves — so it reads as a choice, not as a default
    nobody looked at."""
    if not query_prefix:
        return "bare"
    return "q" + hashlib.sha256(query_prefix.encode("utf-8")).hexdigest()[:8]


def embed(out: Path, name: str, url: str, model: str, dim: int | None,
          ctx: int, concurrency: int, max_chars: int,
          query_prefix: str = "", limit: int | None = None, api: str | None = None,
          from_collection: str | None = None) -> int:
    from app import config
    if config.EMBED_BATCH != 1:
        raise SystemExit(
            f"EMBED_BATCH is {config.EMBED_BATCH}, not 1. llama.cpp's batched "
            f"embedding is not deterministic per batch shape and its prompt "
            f"cache can replay a corrupted result into later single-text "
            f"requests, so every number this run produced would be plausible "
            f"and wrong. Raise EMBED_CONCURRENCY for throughput instead.")
    corpus, queries = load(out, max_chars)
    if limit:
        # A smoke run: prove the URL, the model name and the whole pipeline
        # in two minutes rather than finding out 47 minutes into the real
        # one. It is NOT scoreable — see `score`.
        corpus = Corpus(ids=corpus.ids[:limit], texts=corpus.texts[:limit])
        # and the questions too: GerDaLIRSmall's 12,234 made a "smoke" that
        # embedded every one of them (München, 2026-10-29)
        queries = Queries(texts=queries.texts[:limit], relevant=queries.relevant[:limit])
    if from_collection:
        if query_prefix:
            raise SystemExit("--from-collection serves the collection's own query instruction; "
                             "a --query-prefix would add a second one")
        recipe = served_recipe(from_collection)
        url, model, api, ctx, dim = recipe.url, recipe.model, recipe.api, recipe.ctx, recipe.dim
        # the app prefixes it, as a search does; it names the query side
        query_side = recipe.query_instruction or ""
    else:
        api = api_for(model, api)
        recipe = _recipe(url, model, dim, ctx, concurrency, api)
        query_side = query_prefix
    if api == "vega" and not config.VEGA_URL:
        raise SystemExit(f"{model} is a VeGa model and VEGA_URL is unset: its receipts "
                         f"cannot be read (docs/notes/vega.md); run in the worker's env")
    run = out / name
    # a smoke run's shards are not the real run's (its first shard is short)
    _same_settings(run, {"url": url, "model": model, "api": api, "ctx": ctx, "dim": dim,
                         "max_chars": max_chars, "limit": limit})
    slug = slug_of(query_side)
    print(f"{name} [{slug}]: {len(corpus.texts)} documents, "
          f"{len(queries.texts)} queries" + (f"  (LIMIT {limit})" if limit else ""))
    # The corpus side never carries the prefix, so every variant of a model
    # shares these vectors and only the cheap side is re-embedded.
    doc = _embed_sharded(corpus.texts, run / "corpus", recipe, query=False)
    qry = _embed_sharded([query_prefix + t for t in queries.texts],
                         run / f"queries-{slug}", recipe, query=True)
    np.save(run / "corpus.npy", doc)
    np.save(run / f"queries-{slug}.npy", qry)
    (run / f"run-{slug}.json").write_text(json.dumps(
        {"name": name, "slug": slug, "url": url, "model": model, "api": api, "ctx": ctx,
         "max_chars": max_chars, "query_prefix": query_side,
         "from_collection": from_collection,
         "limit": limit, "dim": int(doc.shape[1]),
         "documents": int(doc.shape[0]), "queries": int(qry.shape[0])},
        indent=2) + "\n", encoding="utf-8")
    print(f"{name} [{slug}]: dim {doc.shape[1]}")
    if limit:
        print(f"  smoke run — {limit} documents, NOT scoreable. Delete "
              f"{run} and re-embed without --limit for a real number.")
    return 0


def _dcg(relevances: list[float], k: int) -> float:
    return sum(rel / math.log2(i + 2) for i, rel in enumerate(relevances[:k]))


def metrics_for(ranking: list[int], relevant: set[int], ks) -> dict[str, float]:
    """The definitions of sentence-transformers' InformationRetrievalEvaluator,
    for ONE query. `ranking` is corpus ids, best first."""
    out: dict[str, float] = {}
    for k in ks:
        top = ranking[:k]
        hits = [d in relevant for d in top]
        found = sum(hits)
        out[f"recall@{k}"] = found / len(relevant) if relevant else 0.0
        out[f"ndcg@{k}"] = (
            _dcg([1.0 if h else 0.0 for h in hits], k)
            / _dcg([1.0] * len(relevant), k)) if relevant else 0.0
        rr = 0.0
        for rank, hit in enumerate(hits, start=1):
            if hit:
                rr = 1.0 / rank
                break
        out[f"mrr@{k}"] = rr
        correct = 0
        precisions = 0.0
        for rank, hit in enumerate(hits, start=1):
            if hit:
                correct += 1
                precisions += correct / rank
        out[f"map@{k}"] = (precisions / min(k, len(relevant))) if relevant else 0.0
    return out


def rank_dense(doc: np.ndarray, qry: np.ndarray, corpus: Corpus,
               top: int = max(KS)) -> list[list[int]]:
    """Each query's top documents by cosine, as corpus ids, best first."""
    d = doc / np.linalg.norm(doc, axis=1, keepdims=True)
    q = qry / np.linalg.norm(qry, axis=1, keepdims=True)
    ids = np.asarray(corpus.ids)
    out = []
    for i in range(q.shape[0]):
        sims = d @ q[i]
        best = np.argpartition(-sims, min(top, len(sims) - 1))[:top]
        best = best[np.argsort(-sims[best])]
        out.append([int(x) for x in ids[best]])
    return out


def metrics_of(rankings: list[list[int]], queries: Queries, ks=KS) -> dict[str, float]:
    """The metrics averaged over the queries — one ranking each, any leg."""
    totals: dict[str, float] = {}
    for ranking, relevant in zip(rankings, queries.relevant):
        for key, value in metrics_for(ranking, set(relevant), ks).items():
            totals[key] = totals.get(key, 0.0) + value
    return {key: value / len(rankings) for key, value in totals.items()}


#: the per-query metrics kept beside each row, for the paired test
PAIRED = ("mrr@10", "ndcg@10")


def per_query(rankings: list[list[int]], queries: Queries,
              keys=PAIRED) -> dict[str, list[float]]:
    """Each query's metrics, in query order: what a paired test compares."""
    ks = sorted({int(k.split("@")[1]) for k in keys})
    rows = [metrics_for(r, set(rel), ks) for r, rel in zip(rankings, queries.relevant)]
    return {k: [row[k] for row in rows] for k in keys}


def paired(a: list[float], b: list[float], *, trials: int = 10_000,
           seed: int = 0, chunk: int = 500) -> tuple[float, float, float, float]:
    """(mean of a − b, its 95% bootstrap interval, the two-sided p of Fisher's
    randomization test) over the same queries.

    THE STANDARD, NOT OURS: Smucker, Allan & Carterette (CIKM 2007) compared
    the significance tests used in IR and recommend the randomization test
    (each query's pair of scores swapped at random = its difference's sign
    flipped); the interval is the percentile bootstrap of the mean
    difference. WHY IT IS NEEDED: LegalQuAD has 200 questions, and its first
    rows differed by 1–2 points of MRR@10 — a size the per-query spread
    (~0.4) puts within noise. A fixed seed: the same scores give the same
    report. Trials in chunks, so 12k queries stay a few MB."""
    d = np.asarray(a, dtype=np.float64) - np.asarray(b, dtype=np.float64)
    n = len(d)
    if n == 0:
        return 0.0, 0.0, 0.0, 1.0
    delta = float(d.mean())
    rng = np.random.default_rng(seed)
    boots, extreme = [], 0
    for start in range(0, trials, chunk):
        m = min(chunk, trials - start)
        boots.append(d[rng.integers(0, n, size=(m, n))].mean(axis=1))
        flips = (rng.integers(0, 2, size=(m, n)) * 2 - 1) * d
        extreme += int((np.abs(flips.mean(axis=1)) >= abs(delta) - 1e-12).sum())
    lo, hi = np.percentile(np.concatenate(boots), [2.5, 97.5])
    return delta, float(lo), float(hi), (extreme + 1) / (trials + 1)


def score_run(doc: np.ndarray, qry: np.ndarray, corpus: Corpus,
              queries: Queries, ks=KS) -> dict[str, float]:
    return metrics_of(rank_dense(doc, qry, corpus, max(ks)), queries, ks)


#: the lexical run's directory; its label in the report
LEXICAL, LEXICAL_LABEL = "bm25", "BM25 (Louise)"
#: the record prefix of the chunk ids the index and the fusion see:
#: `<prefix>:<corpus id>:<seq>:<version>`, so `retrieval.record_of` reads the
#: corpus id back as the record
_RECORD = "bakeoff"


def _chunk_id(cid: int) -> str:
    return f"{_RECORD}:{cid}:0:1"


def _record_id(record: str) -> int:
    return int(record.split(":", 1)[1])


def lexical(out: Path, lang: str, max_chars: int, batch: int = 5000) -> int:
    """The word leg as Louise serves it, over this set: a fresh tantivy index
    built by app/fts.py (the language group's analyzer and stemmer), each
    question through `fts.search`. One chunk per document, the same
    truncated text the encoders read."""
    import shutil
    from app import collections as collections_mod, fts
    corpus, queries = load(out, max_chars)
    run = out / LEXICAL
    index_dir = run / "index"
    shutil.rmtree(index_dir, ignore_errors=True)
    run.mkdir(parents=True, exist_ok=True)
    # an ad-hoc collection, as tests/test_fts_batching.py builds one: a plugin
    # with no validity columns and no name index gives the plain schema; the
    # text fields, analyzer and query are the same for every plugin
    c = collections_mod.Collection(
        name="bakeoff", plugin="gutenberg", profile=None, embed_mode_default=None,
        corpus_dir=out, manifest_path=run / "unused.sqlite", fts_dir=index_dir,
        lance_dir=run / "unused-lance")
    entries = [(lang, i, [{"id": _chunk_id(cid), "chapter": "", "text": text}])
               for i, (cid, text) in enumerate(zip(corpus.ids, corpus.texts))]
    for k in range(0, len(entries), batch):
        fts.index_books(entries[k:k + batch], c)
    group = fts.group_for(lang)
    from app import retrieval
    rankings, empty = [], 0
    for text in queries.texts:
        hits = fts.search(group, text, limit=max(KS), collection=c)
        empty += not hits
        rankings.append([_record_id(retrieval.record_of(h)) for h in hits])
    fts.close_all()
    (run / "ranking-bare.json").write_text(json.dumps(rankings) + "\n", encoding="utf-8")
    (run / "run-bare.json").write_text(json.dumps(
        {"name": LEXICAL_LABEL, "slug": "bare", "kind": "lexical", "lang": lang,
         "group": group, "max_chars": max_chars, "documents": len(corpus.ids),
         "queries": len(rankings), "empty": empty}, indent=2) + "\n", encoding="utf-8")
    print(f"{LEXICAL_LABEL}: {len(corpus.ids)} documents in group {group!r}, "
          f"{len(rankings)} queries, {empty} with no hit")
    return 0


def fuse(text: list[int], vector: list[int], depth: int = max(KS)) -> list[int]:
    """The two legs' rankings fused as a search fuses them:
    `retrieval._merge_rrf` itself, at `depth`, its tie rule included."""
    from app import retrieval
    lists = [[{"chunk_id": _chunk_id(cid), "source": source, "lang": ""} for cid in ids]
             for ids, source in ((text, f"fts:{_RECORD}"), (vector, "dense"))]
    merged = retrieval._merge_rrf(lists, depth)
    return [_record_id(retrieval.record_of(m)) for m in merged][:depth]


def _lexical_rankings(out: Path, max_chars: int, n_queries: int) -> list[list[int]] | None:
    meta_path = out / LEXICAL / "run-bare.json"
    if not meta_path.exists():
        return None
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    if meta.get("max_chars") != max_chars or meta.get("queries") != n_queries:
        raise SystemExit(f"the BM25 run was built with max_chars {meta.get('max_chars')} and "
                         f"{meta.get('queries')} queries, this score with {max_chars} and "
                         f"{n_queries}: run `lexical` again")
    return json.loads((out / LEXICAL / "ranking-bare.json").read_text(encoding="utf-8"))


def runs_in(out: Path) -> list[tuple[Path, dict]]:
    """Every (model, query-side) pair that has been embedded, in a stable
    order so two reports of the same directory read the same."""
    found = []
    for run in sorted(p for p in out.iterdir() if p.is_dir()):
        for meta in sorted(run.glob("run-*.json")):
            found.append((run, json.loads(meta.read_text(encoding="utf-8"))))
    return found


def label_of(meta: dict) -> str:
    return meta["name"] if meta["slug"] == "bare" else f"{meta['name']} [{meta['slug']}]"


def score(out: Path, max_chars: int) -> int:
    corpus, queries = load(out, max_chars)
    lexical_rankings = _lexical_rankings(out, max_chars, len(queries.texts))
    for run, meta in runs_in(out):
        slug = meta["slug"]
        if meta.get("kind") == "lexical":
            result = metrics_of(lexical_rankings, queries)
            (run / f"scores-{slug}.json").write_text(
                json.dumps(result, indent=2) + "\n", encoding="utf-8")
            (run / f"perquery-{slug}.json").write_text(
                json.dumps(per_query(lexical_rankings, queries)) + "\n", encoding="utf-8")
            print(f"{label_of(meta)}: mrr@10 {result['mrr@10']:.4f}  "
                  f"ndcg@10 {result['ndcg@10']:.4f}")
            continue
        if meta.get("limit"):
            # Recall@k against a truncated corpus is not a smaller version of
            # the real number, it is a larger one: every document removed is a
            # distractor removed. Refuse it rather than print it with a
            # caveat nobody carries into the next conversation.
            print(f"{label_of(meta)}: SKIPPED — embedded with "
                  f"--limit {meta['limit']}; a partial corpus inflates every "
                  f"metric here")
            continue
        doc = np.load(run / "corpus.npy")
        qry = np.load(run / f"queries-{slug}.npy")
        dense = rank_dense(doc, qry, corpus)
        result = metrics_of(dense, queries)
        (run / f"scores-{slug}.json").write_text(
            json.dumps(result, indent=2) + "\n", encoding="utf-8")
        (run / f"perquery-{slug}.json").write_text(
            json.dumps(per_query(dense, queries)) + "\n", encoding="utf-8")
        print(f"{label_of(meta)}: mrr@100 {result['mrr@100']:.4f}  "
              f"recall@10 {result['recall@10']:.4f}")
        if lexical_rankings is not None:
            fused_rankings = [fuse(t, v) for t, v in zip(lexical_rankings, dense)]
            fused = metrics_of(fused_rankings, queries)
            (run / f"fused-{slug}.json").write_text(
                json.dumps(fused, indent=2) + "\n", encoding="utf-8")
            (run / f"perquery-fused-{slug}.json").write_text(
                json.dumps(per_query(fused_rankings, queries)) + "\n", encoding="utf-8")
            print(f"{label_of(meta)} + BM25 (RRF): mrr@10 {fused['mrr@10']:.4f}  "
                  f"ndcg@10 {fused['ndcg@10']:.4f}")
    return 0


PUBLISHED = {"DAR-legal-it (published)": {
    "recall@3": 0.7111, "recall@5": 0.7602, "recall@10": 0.8037,
    "recall@20": 0.8331, "recall@40": 0.8609, "recall@60": 0.8861,
    "recall@80": 0.8957, "recall@100": 0.9129, "ndcg@5": 0.7419,
    "ndcg@100": 0.7459, "mrr@5": 0.7719, "mrr@100": 0.7780,
    "map@5": 0.6985, "map@100": 0.7015}}


def _print_paired(queried: dict[str, dict[str, list[float]]], metric: str = "mrr@10") -> None:
    """Every row against BM25 (Louise), and the leader against every other
    row: the difference in `metric`, its 95% interval, the randomization p.
    Uncorrected per comparison: with several rows, read p < 0.01 as the bar."""
    have = {k: v[metric] for k, v in queried.items() if metric in v}
    if len(have) < 2:
        return
    n = {len(v) for v in have.values()}
    if len(n) != 1:
        print(f"\n(paired test skipped: the rows were scored on different query counts {sorted(n)})")
        return
    leader = max(have, key=lambda k: float(np.mean(have[k])))
    print(f"\nPaired, {metric}, {n.pop()} queries (Fisher's randomization test, 10,000 trials; "
          f"95% bootstrap interval; uncorrected):")
    pairs = ([(k, LEXICAL_LABEL) for k in have if k != LEXICAL_LABEL] if LEXICAL_LABEL in have else []) \
        + [(leader, k) for k in have if k != leader]
    seen = set()
    for a, b in pairs:
        if (a, b) in seen or (b, a) in seen:
            continue
        seen.add((a, b))
        delta, lo, hi, p = paired(have[a], have[b])
        print(f"  {a}  vs  {b}: {delta:+.4f} [{lo:+.4f}, {hi:+.4f}]  p = {p:.4f}")


def report(out: Path) -> int:
    # the published row is DAR-legal-it's on JuriFindIT, and on no other set
    rows = dict(PUBLISHED) if suite_of(out) == "jurifindit" else {}
    queried: dict[str, dict[str, list[float]]] = {}      # label -> per-query metrics
    prefixes: dict[str, str] = {}

    def keep(label: str, path: Path) -> None:
        if path.exists():
            queried[label] = json.loads(path.read_text(encoding="utf-8"))
    for run, meta in runs_in(out):
        scores = run / f"scores-{meta['slug']}.json"
        if not scores.exists():
            continue
        rows[label_of(meta)] = json.loads(scores.read_text(encoding="utf-8"))
        keep(label_of(meta), run / f"perquery-{meta['slug']}.json")
        fused = run / f"fused-{meta['slug']}.json"
        if fused.exists():
            rows[f"{label_of(meta)} + BM25 (RRF)"] = json.loads(fused.read_text(encoding="utf-8"))
            keep(f"{label_of(meta)} + BM25 (RRF)", run / f"perquery-fused-{meta['slug']}.json")
        if meta.get("query_prefix"):
            prefixes[meta["slug"]] = meta["query_prefix"]
    if not rows:
        print(f"{out}: no scored run yet (`embed`, then `score`)")
        return 0
    # nDCG@10 and MRR@10: what MTEB, MLEB and the legal papers report
    shown = ["recall@3", "recall@5", "recall@10", "recall@100",
             "ndcg@5", "ndcg@10", "ndcg@100", "mrr@5", "mrr@10", "mrr@100", "map@100"]
    width = max(len(name) for name in rows)
    print("| " + "model".ljust(width) + " | "
          + " | ".join(m.rjust(10) for m in shown) + " |")
    print("|" + "-" * (width + 2) + "|"
          + "|".join("-" * 12 for _ in shown) + "|")
    for name, result in rows.items():
        cells = [f"{result[m]:.4f}".rjust(10) if m in result else "-".rjust(10)
                 for m in shown]
        print("| " + name.ljust(width) + " | " + " | ".join(cells) + " |")
    for slug, text in sorted(prefixes.items()):
        print(f"\n[{slug}] query prefix: {text!r}")
    _print_paired(queried)
    if suite_of(out) == "jurifindit":
        print("\nDAR-legal-it's row is its PUBLISHED table, not a run of this tool: "
              "its truncation and corpus handling are unrecorded.")
    print("\nThe rows are comparable to each other exactly. A row with no [slug] embedded "
          "its queries bare; a space that declares a query instruction serves its "
          "queries WITH it — `embed --from-collection <name>` measures that served form.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for cmd in ("fetch", "embed", "lexical", "score", "report"):
        p = sub.add_parser(cmd)
        p.add_argument("--out", type=Path, required=True)
        if cmd == "fetch":
            p.add_argument("--suite", default="jurifindit", choices=sorted(SUITES),
                           help="the set; one directory holds one")
        if cmd == "lexical":
            p.add_argument("--lang", required=True,
                           help="the set's language (ISO 639-1): it picks the "
                                "index's analyzer and stemmer, as a record's does")
        if cmd in ("embed", "lexical", "score"):
            p.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS)
        if cmd == "embed":
            p.add_argument("--name", required=True)
            p.add_argument("--url", default=None)
            p.add_argument("--model", default=None)
            p.add_argument("--from-collection", default=None,
                           help="a held collection's encoder as its searches use it: url, "
                                "model, api, ctx and its query instruction (`recipe_for`)")
            p.add_argument("--dim", type=int, default=None)
            p.add_argument("--ctx", type=int, default=8192)
            p.add_argument("--concurrency", type=int, default=1)
            p.add_argument("--api", choices=("openai", "vega"), default=None,
                           help="the endpoint's client; by default `vega` for a "
                                "`vega-…` model (tagged inputs, receipts), else openai")
            p.add_argument("--query-prefix", default="",
                           help="prepended to QUERIES only, never to "
                                "documents; recorded in the run's JSON. "
                                "Qwen3-Embedding documents "
                                "'Instruct: <task>\\nQuery: '")
            p.add_argument("--limit", type=int, default=None,
                           help="embed only the first N documents, to prove "
                                "the endpoint in minutes. Marks the run "
                                "unscoreable — a partial corpus inflates "
                                "every metric")
    a = ap.parse_args(argv)
    if a.cmd == "fetch":
        return fetch(a.out, a.suite)
    if a.cmd == "embed":
        if not a.from_collection and not (a.url and a.model):
            ap.error("embed needs --url and --model, or --from-collection")
        return embed(a.out, a.name, a.url, a.model, a.dim, a.ctx,
                     a.concurrency, a.max_chars, a.query_prefix, a.limit, a.api,
                     a.from_collection)
    if a.cmd == "lexical":
        return lexical(a.out, a.lang, a.max_chars)
    if a.cmd == "score":
        return score(a.out, a.max_chars)
    return report(a.out)


if __name__ == "__main__":
    raise SystemExit(main())
