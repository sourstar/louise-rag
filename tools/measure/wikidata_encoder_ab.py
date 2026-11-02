"""MEASUREMENT (not product code): which encoder for wikidata's space —
jina-v3 (its space today, no vectors yet) or bge-m3 (wikipedia's encoder,
~4x faster on this card, exact with flash attention, MIT). Operator,
2026-10-31: measure, then switch to bge-m3 if it finds entities at least
as well (docs/notes/wikidata-family-slices.md §10).

A KNOWN-ITEM TEST, the standard way to compare retrieval without judges
(e.g. Ogilvie & Callan 2003; MS MARCO's one known answer): a sample of
entities from the families held, a question for some of them written in
words by the chat model WITHOUT the entity's name, and each encoder ranks
the whole sample for each question. The right entity is the only relevant
one: MRR@10, recall@1 and @10, a paired randomization test (Smucker, Allan
& Carterette 2007), by family and by the question's language (English, and
Italian against English text: the reader's language).

HARRIER (operator, 2026-11-02: "Harrier may be the model of reference for all
our non specialistic collections (gutenberg, wikipedia, wikidata,
gutenberg-graph), compare the score also with bge-m3 and wikipedia/wikidata").
`--encoder harrier` is microsoft/harrier-oss-v1-0.6b (our f16 GGUF,
`vega-harrier-oss-v1-0.6b`) with its card's retrieval prompt on the query
side. `sample-table` makes the same known-item test for any collection from
its own vector table: random documents per language, their first chunks as
they were embedded, the title from the manifest. wikipedia is the case
(`queries --prompt wikipedia`), since no judged set exists for it. `score`
compares every pair of encoders it finds.

  sample   OUT_DIR [--entities 20000] [--families person,work,…] [--seed 0]
  sample-table OUT_DIR --collection wikipedia [--langs en,it] [--docs 10000]
           [--max-chunks 6] [--seed 0]
           records read from the SHARD's parts of those families (no
           snapshot needed: the parts the 10-30 build wrote), cut into
           chunks through `hold.land.cut` — the chunks the space embeds.
  queries  OUT_DIR [--n 400]  questions by the chat model (stored: the set)
  embed    OUT_DIR --encoder jina|bge  each side through the PRODUCTION
           client (`embeddings.embed_calibration`, `_embed_texts` underneath) with the collection's recipe —
           wikidata's for jina-v3, wikipedia's for bge-m3; resumable by block
  rerank   OUT_DIR --collection C [--reranker-from G] [--encoders bge,harrier]
           [--depth 10 --depth 40 --depth 50 --depth 100] [--doc-lang en]
           the whole search on the sample, stage by stage (the operator,
           2026-11-02: "Add BM25 fusion and translations too, make separated
           scores … verify if it doesn't pay off using a fancy embedder if the
           gap is filled by the whole retrieval pipeline"): BM25 with the
           plan's translations, each encoder alone and fused with it, and each
           after the reranker at every depth — tools/measure/pipeline_eval.py.
  score    OUT_DIR  the table — quality, speed (chunks/s), jina-v3 on llama.cpp
           against its torch reference — and the verdict (operator, 2026-10-31):
           bge-m3 only if it is BETTER on MRR@10 with p < 0.05, else jina-v3,
           whose vectors are half the size.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import random
import re
import sys
from pathlib import Path
from typing import Callable

TOP = 10
BLOCK = 1000
ENCODERS = {"jina": "wikidata", "bge": "wikipedia",     # the collection whose recipe embeds
            "jina-gguf": "wikidata", "harrier": "wikidata"}
#: microsoft/harrier-oss-v1-0.6b, converted by us (gutenberg-encoder-bakeoff.md)
HARRIER_MODEL = "vega-harrier-oss-v1-0.6b"
#: its card's retrieval prompt (`web_search_query`); documents take none
HARRIER_QUERY = ("Instruct: Given a web search query, retrieve relevant passages that "
                 "answer the query\nQuery: ")
#: jina-v3 on llama.cpp (operator, 2026-10-31: "Use jina gguf for the
#: comparison so we can evaluate both speed and reliability"): the f16 GGUF
#: with each task's LoRA MERGED in (llama-export-lora), one model a side —
#: VeGa states no run that loads a second file (app/vega/loadopts.py
#: SECOND_FILE_FIELDS). llama.cpp reports an adapter's prompt prefix but does
#: not apply it, so the task instruction is put before the text here, as the
#: torch task does in the backend; the Matryoshka cut to 512 is made here too
GGUF_MODELS = {False: "vega-jina-v3-gguf", True: "vega-jina-v3-gguf-query"}
JINA_INSTRUCTIONS = {False: "Represent the document for retrieval: ",
                     True: "Represent the query for retrieving evidence documents: "}
JINA_DIM = 512
NINE = ("person", "work", "place", "organization", "event", "heritage", "built", "scholarly", "property")
PROMPT = {
    "en": ("Here is what Wikidata says about one entity. Write ONE short search query, in English, that "
           "a person looking for this entity might type. Do NOT use its name, label or any alias; "
           "describe it by what it is or did. Answer with the query only. /no_think\n\n{text}"),
    "it": ("Ecco cosa dice Wikidata di un'entità. Scrivi UNA breve domanda di ricerca, in italiano, che "
           "una persona potrebbe digitare per trovarla. NON usare il suo nome, l'etichetta né alias; "
           "descrivila per ciò che è o ha fatto. Rispondi solo con la domanda. /no_think\n\n{text}"),
}


PROMPTS = {
    "wikidata": PROMPT,
    "wikipedia": {
        "en": ("Here is the beginning of an encyclopedia article. Write ONE short search query, in "
               "English, that a person looking for this article might type. Do NOT use its title or "
               "the name of its subject; describe the subject by what it is or did. Answer with the "
               "query only. /no_think\n\n{text}"),
        "it": ("Ecco l'inizio di una voce di enciclopedia. Scrivi UNA breve domanda di ricerca, in "
               "italiano, che una persona potrebbe digitare per trovarla. NON usare il titolo né il "
               "nome del soggetto; descrivilo per ciò che è o ha fatto. Rispondi solo con la "
               "domanda. /no_think\n\n{text}"),
    },
}


def _jsonl(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()] \
        if path.exists() else []


# ---------------------------------------------------------------- sample


def part_files(shard: Path, family: str) -> list[Path]:
    """The current record parts of one family (`records/family=<f>/range=<r>/NNNNN.<ext>`;
    a superseded version carries its snapshot id before the extension and is left out)."""
    out = []
    for p in sorted((shard / "records" / f"family={family}").glob("range=*/*")):
        stem = p.name.split(".", 1)[0]
        if stem.isdigit() and len(stem) == 5 and p.name.count(".") <= 2:
            out.append(p)
    return out


def sample(out: Path, *, entities: int, families: tuple[str, ...], seed: int,
           iter_records: Callable | None = None, cut: Callable | None = None,
           shard: Path | None = None, codex=None) -> dict:
    """Up to entities/len(families) records a family, from parts chosen at
    random, each with its chunks as the space would embed them."""
    if iter_records is None or cut is None or shard is None:
        from app import collections as collections_mod
        from app.hold import collection as col, land
        from app.pack import records as pack_records
        root = collections_mod.louise_root() / "collections"
        name, codex = col.resolve(root, "wikidata")
        shard = col.shard_dir_of(root, name)
        iter_records = pack_records.iter_records
        cut = lambda recs: land.cut(codex, recs)   # noqa: E731
    rnd = random.Random(seed)
    per = max(1, entities // len(families))
    out.mkdir(parents=True, exist_ok=True)
    rows = []
    for fam in families:
        parts = part_files(shard, fam)
        rnd.shuffle(parts)
        got: list[dict] = []
        for p in parts:
            recs = list(iter_records(p))
            rnd.shuffle(recs)
            got += recs[: per - len(got)]
            if len(got) >= per:
                break
        chunks = cut(got)
        by_uid: dict[str, list[str]] = {}
        for cid, c in sorted(chunks.items(), key=lambda kv: (kv[0].rsplit(":", 2)[0], kv[1].get("seq", 0))):
            by_uid.setdefault(cid.rsplit(":", 2)[0], []).append(c["text"])
        for r in got:
            if by_uid.get(r["uid"]):
                rows.append({"uid": r["uid"], "family": fam, "label": r.get("title") or "",
                             "chunks": by_uid[r["uid"]]})
    (out / "corpus.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                                      encoding="utf-8")
    return {"entities": len(rows), "chunks": sum(len(r["chunks"]) for r in rows)}


def _titles(collection, books: list[int]) -> dict[int, str]:
    """book_id -> title, from the manifest read-only (the search's own lookup,
    retrieval._attach_metadata_and_enqueue)."""
    import sqlite3
    out: dict[int, str] = {}
    con = sqlite3.connect(f"file:{collection.manifest_path}?mode=ro", uri=True)
    try:
        for i in range(0, len(books), 500):
            part = books[i:i + 500]
            marks = ",".join("?" for _ in part)
            for native, title in con.execute(
                    f"SELECT native_id, title FROM documents WHERE source=? AND native_id IN ({marks})",
                    [collection.plugin] + [str(b) for b in part]):
                out[int(native)] = title or ""
    finally:
        con.close()
    return out


def sample_table(out: Path, *, collection: str, langs: tuple[str, ...], docs: int, max_chunks: int,
                 seed: int, tables: dict | None = None, titles: Callable | None = None) -> dict:
    """A known-item sample from a collection's OWN vector table: `docs`
    documents spread over `langs`, drawn at random positions, each with its
    first `max_chunks` chunks (by seq) as they were embedded, and its title
    from the manifest. A table split by language is read part by part; an
    unsplit one is filtered on its `lang` column. `family` is the language."""
    import numpy as np
    c = None
    if tables is None or titles is None:
        from app import collections as collections_mod, embeddings, vector_parts
        c = collections_mod.by_name(collection)
        if c is None:
            raise SystemExit(f"no {collection} collection here")
        split = vector_parts.key_of(c)
        if split not in (None, "lang"):
            raise SystemExit(f"{collection} is split by {split!r}; sample-table reads lang parts or one table")
        tables = {lang: embeddings.table_of_book(vector_parts.view(c, lang) if split == "lang" else c, 0)
                  for lang in langs}
        titles = lambda books: _titles(c, books)   # noqa: E731
    rng = np.random.default_rng(seed)
    per = max(1, docs // len(langs))
    out.mkdir(parents=True, exist_ok=True)
    rows = []
    for lang in langs:
        table = tables.get(lang)
        if table is None:
            continue
        ds = table.to_lance()
        total = ds.count_rows()
        books: list[int] = []
        seen: set[int] = set()
        tries = 0
        while len(books) < per and tries < 50 and total:
            tries += 1
            at = np.unique(rng.integers(0, total, size=min(total, 4 * per)))
            for r in ds.take(at.tolist(), columns=["book_id", "lang"]).to_pylist():
                b = int(r["book_id"])
                if b in seen or (r.get("lang") and r["lang"] != lang):
                    continue
                seen.add(b)
                books.append(b)
                if len(books) >= per:
                    break
        named = titles(books)
        for b in books:
            got = (table.search().where(f"book_id = {b}", prefilter=True)
                   .select(["id", "seq", "text"]).limit(100_000).to_list())
            got.sort(key=lambda r: (int(r["seq"]), str(r["id"])))
            texts = [r["text"] for r in got[:max_chunks] if r["text"]]
            if texts:
                rows.append({"uid": f"{collection}:{b}", "family": lang, "label": named.get(b, ""),
                             "chunks": texts})
    (out / "corpus.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                                      encoding="utf-8")
    return {"documents": len(rows), "chunks": sum(len(r["chunks"]) for r in rows),
            "by_language": {lang: sum(1 for r in rows if r["family"] == lang) for lang in langs}}


# ---------------------------------------------------------------- queries


def leaks(query: str, label: str) -> bool:
    """The question names the entity: its label, or any word of it longer
    than three letters (a known-item question must not be a name lookup)."""
    q = query.lower()
    words = [w for w in re.findall(r"\w+", label.lower()) if len(w) > 3]
    return bool(label) and (label.lower() in q or any(re.search(rf"\b{re.escape(w)}\b", q) for w in words))


def queries(out: Path, *, n: int, seed: int, chat: Callable[[str], str] | None = None,
            prompt: str = "wikidata") -> dict:
    if chat is None:
        from app import llm
        chat = lambda prompt: llm.chat([{"role": "user", "content": prompt}],   # noqa: E731
                                       temperature=0.0, max_tokens=60)
    corpus = _jsonl(out / "corpus.jsonl")
    done = {q["uid"] for q in _jsonl(out / "queries.jsonl")}
    rnd = random.Random(seed)
    order = list(range(len(corpus)))
    rnd.shuffle(order)
    kept = len(done)
    dropped = 0
    with open(out / "queries.jsonl", "a", encoding="utf-8") as f:
        for i in order:
            if kept >= n:
                break
            e = corpus[i]
            if e["uid"] in done:
                continue
            lang = "it" if kept % 2 else "en"
            q = chat(PROMPTS[prompt][lang].format(text=e["chunks"][0][:2000])).strip().strip('"').splitlines()[0:1]
            q = q[0].strip() if q else ""
            if not q or leaks(q, e["label"]):
                dropped += 1
                continue
            f.write(json.dumps({"uid": e["uid"], "family": e["family"], "lang": lang, "query": q},
                               ensure_ascii=False) + "\n")
            kept += 1
    return {"queries": kept, "dropped_for_naming_it": dropped}


# ---------------------------------------------------------------- embed


def _recipe(encoder: str):
    from app import collections as collections_mod
    c = collections_mod.by_name(ENCODERS[encoder])
    if c is None:
        raise SystemExit(f"no {ENCODERS[encoder]} collection here: its recipe embeds the {encoder} side")
    recipe = collections_mod.recipe_for(c)
    # the wikidata chunk is cut at 1,024 tokens already: no second cut by a
    # window another corpus was sized for — within what each backend takes
    # (the node, 2026-10-31: a window of 8,192 was refused by both):
    # - jina-v3 (louise-python, `cuts: context_size`): the window must EQUAL
    #   its context_size, wikidata's ctx 1,024 — the recipe as it is;
    # - bge-m3 (llama.cpp, `cuts: tokenizer`): at most context_size − 1,
    #   8,191 for its 8,192
    if encoder == "jina":
        return recipe
    if encoder == "jina-gguf":
        # its own two models; a window above the chunk's 1,024 tokens plus
        # the instruction and the specials (its context_size is 2,048)
        return dataclasses.replace(recipe, model=GGUF_MODELS[False], query_model=GGUF_MODELS[True],
                                   ctx=2047)
    if encoder == "harrier":
        # the base collection's server, api and cut; harrier's model, its
        # retrieval prompt on queries, a window within its context_size 8,192
        return dataclasses.replace(recipe, model=HARRIER_MODEL, query_model=None, query_url=None,
                                   query_instruction=HARRIER_QUERY, dim=None,
                                   ctx=max(int(recipe.ctx or 0), 8191))
    return dataclasses.replace(recipe, ctx=max(int(recipe.ctx or 0), 8191))


def matryoshka(vectors, dim: int = JINA_DIM):
    """The first `dim` components, renormalised: jina-v3's `truncate_dim`."""
    import numpy as np
    v = np.asarray(vectors, dtype="float32")[:, :dim]
    return v / np.linalg.norm(v, axis=1, keepdims=True)


def embed(out: Path, encoder: str, *, encode: Callable | None = None) -> dict:
    import contextlib
    lease = contextlib.nullcontext()
    if encode is None:
        from app import embeddings, encoder_use
        recipe = _recipe(encoder)
        # LEASED while it embeds (München, 2026-11-02, 2d2cf21): the worker's
        # sweep unloads every vega- model no held collection names unless a
        # process leases it — harrier is named by none
        lease = encoder_use.held(recipe, holder=encoder_use.holder(f"wikidata_encoder_ab {encoder}"))
        # embed_calibration: the client's door for inputs that are not a
        # collection's chunks ("a tool's sample"), the same `_embed_texts`
        # underneath, the model pinned while it works (invariant 7's guard,
        # tests/test_invariant_guards.py: nothing outside embeddings calls the client)
        encode = lambda texts, query: embeddings.embed_calibration(   # noqa: E731
            recipe, texts, query=query, asked=False)
    if encoder == "jina-gguf":
        inner = encode
        encode = lambda texts, query: matryoshka(   # noqa: E731
            inner([JINA_INSTRUCTIONS[query] + t for t in texts], query))
    with lease:
        return _embed(out, encoder, encode)


def _embed(out: Path, encoder: str, encode: Callable) -> dict:
    import time

    import numpy as np
    corpus = _jsonl(out / "corpus.jsonl")
    texts = [t for e in corpus for t in e["chunks"]]
    written = 0
    timing = out / f"time-{encoder}.json"
    spent = json.loads(timing.read_text()) if timing.exists() else {"seconds": 0.0, "chunks": 0}
    for b in range(0, len(texts), BLOCK):
        path = out / f"vec-{encoder}-{b // BLOCK:05d}.npy"
        if path.exists():
            continue
        t0 = time.monotonic()
        np.save(path, np.asarray(encode(texts[b:b + BLOCK], False), dtype="float32"))
        spent = {"seconds": spent["seconds"] + time.monotonic() - t0,
                 "chunks": spent["chunks"] + len(texts[b:b + BLOCK])}
        timing.write_text(json.dumps(spent))
        written += 1
        print(f"{encoder}: block {b // BLOCK + 1} of {(len(texts) + BLOCK - 1) // BLOCK}", file=sys.stderr)
    qpath = out / f"qvec-{encoder}.npy"
    qs = _jsonl(out / "queries.jsonl")
    if not qpath.exists() or len(np.load(qpath)) != len(qs):
        np.save(qpath, np.asarray(encode([q["query"] for q in qs], True), dtype="float32"))
    return {"chunks": len(texts), "blocks_written": written, "queries": len(qs),
            "chunks_per_s": round(spent["chunks"] / spent["seconds"], 2) if spent["seconds"] else None}


# ---------------------------------------------------------------- score


def best_by_entity(doc: "np.ndarray", owner: list[int], qv: "np.ndarray", n_entities: int):
    """queries x entities: each entity's score for each query, its best
    chunk's cosine (`owner[i]`: the entity of chunk i)."""
    import numpy as np
    doc = doc / np.linalg.norm(doc, axis=1, keepdims=True)
    qv = qv / np.linalg.norm(qv, axis=1, keepdims=True)
    sims = qv @ doc.T                                   # queries x chunks
    best = np.full((len(qv), n_entities), -np.inf, dtype="float32")
    owner_arr = np.asarray(owner)
    for k in range(len(qv)):
        np.maximum.at(best[k], owner_arr, sims[k])
    return best


def score(out: Path) -> dict:
    import numpy as np
    from tools.measure.fusion_calibration import randomization
    corpus = _jsonl(out / "corpus.jsonl")
    qs = _jsonl(out / "queries.jsonl")
    index = {e["uid"]: i for i, e in enumerate(corpus)}
    owner = [i for i, e in enumerate(corpus) for _ in e["chunks"]]
    target = np.asarray([index[q["uid"]] for q in qs])
    rr: dict[str, list[float]] = {}
    hit1: dict[str, list[int]] = {}
    hit10: dict[str, list[int]] = {}
    for enc in ENCODERS:
        # `[0-9]`: "vec-jina-*" would take jina-gguf's blocks as well
        blocks = sorted(out.glob(f"vec-{enc}-[0-9]*.npy"))
        if not blocks or not (out / f"qvec-{enc}.npy").exists():
            continue
        doc = np.concatenate([np.load(p) for p in blocks])
        if len(doc) != len(owner):
            raise SystemExit(f"{enc}: {len(doc)} vectors for {len(owner)} chunks — embed again")
        best = best_by_entity(doc, owner, np.load(out / f"qvec-{enc}.npy"), len(corpus))
        tscore = best[np.arange(len(qs)), target]
        rank = 1 + (best > tscore[:, None]).sum(axis=1)
        # Python numbers, not numpy's: the result is printed as JSON, and a
        # sum of numpy bools is an int64 json cannot write (the node's first
        # score, 2026-10-31)
        rank = [int(r) for r in rank]
        rr[enc] = [1.0 / r if r <= TOP else 0.0 for r in rank]
        hit1[enc] = [int(r == 1) for r in rank]
        hit10[enc] = [int(r <= TOP) for r in rank]

    def mean(xs):
        return round(sum(xs) / len(xs), 4) if xs else None

    def by(key):
        out_: dict = {}
        for v in sorted({q[key] for q in qs}):
            idx = [i for i, q in enumerate(qs) if q[key] == v]
            out_[v] = {enc: mean([rr[enc][i] for i in idx]) for enc in rr} | {"queries": len(idx)}
        return out_

    res = {"entities": len(corpus), "chunks": len(owner), "queries": len(qs),
           "mrr10": {e: mean(v) for e, v in rr.items()},
           "recall1": {e: mean(v) for e, v in hit1.items()},
           "recall10": {e: mean(v) for e, v in hit10.items()},
           "by_family": by("family"), "by_language": by("lang")}
    # SPEED: chunks per second each encoder embedded the sample at
    res["chunks_per_s"] = {}
    for enc in rr:
        t = out / f"time-{enc}.json"
        if t.exists():
            got = json.loads(t.read_text())
            res["chunks_per_s"][enc] = round(got["chunks"] / got["seconds"], 2) if got["seconds"] else None
    # RELIABILITY of jina-v3 on llama.cpp: its vectors against the torch
    # reference's, chunk by chunk (03 §3.1's cross-engine agreement)
    if "jina" in rr and "jina-gguf" in rr:
        a = np.concatenate([np.load(p) for p in sorted(out.glob("vec-jina-[0-9]*.npy"))])
        g = np.concatenate([np.load(p) for p in sorted(out.glob("vec-jina-gguf-[0-9]*.npy"))])
        a = a / np.linalg.norm(a, axis=1, keepdims=True)
        g = g / np.linalg.norm(g, axis=1, keepdims=True)
        cos = np.sort((a * g).sum(axis=1))
        res["jina_gguf_vs_torch"] = {"worst": round(float(cos[0]), 6),
                                     "p01": round(float(cos[int(0.01 * len(cos))]), 6),
                                     "median": round(float(cos[len(cos) // 2]), 6)}
    # EVERY PAIR (2026-11-02, harrier joins): the MRR@10 difference, its
    # randomization p, and on how many questions each side wins
    res["pairs"] = {}
    names = sorted(rr)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            d = [x - y for x, y in zip(rr[a], rr[b])]
            res["pairs"][f"{a} - {b}"] = {"mrr10": round(sum(d) / len(d), 4) if d else None,
                                         "p": round(randomization(d), 4) if d else None,
                                         "better": sum(x > 0 for x in d), "worse": sum(x < 0 for x in d)}
    # THE RULE (operator, 2026-10-31: "If bge is as good we switch to jina
    # because it's less disk space"): bge-m3 only if it finds entities BETTER
    # than jina-v3, on MRR@10 with p < 0.05; otherwise jina-v3 (512 dims)
    jina = "jina-gguf" if "jina-gguf" in rr else ("jina" if "jina" in rr else None)
    if jina and "bge" in rr:
        d = [b - j for b, j in zip(rr["bge"], rr[jina])]
        p = randomization(d)
        res["bge_minus_jina"] = {"against": jina, "mrr10": round(sum(d) / len(d), 4), "p": round(p, 4),
                                 "better": sum(x > 0 for x in d), "worse": sum(x < 0 for x in d)}
        bge_better = res["mrr10"]["bge"] > res["mrr10"][jina] and p < 0.05
        res["verdict"] = ("switch to bge-m3: it finds entities better (p < 0.05)" if bge_better else
                          "keep jina-v3: bge-m3 is not better, and jina-v3's vectors are half the size")
    return res


# ---------------------------------------------------------------- rerank

#: chunks each encoder ranks per question: deep enough for 100 records
CHUNK_DEPTH = 2_000


def _query_encoder(encoder: str):
    """Texts through `encoder`'s query side as `embed` asked its questions
    (`_recipe`, the production client, the encoder leased)."""
    def encode(texts: list[str]) -> list[list[float]]:
        from app import embeddings, encoder_use
        recipe = _recipe(encoder)
        with encoder_use.held(recipe, holder=encoder_use.holder(f"pipeline {encoder}"), query=True):
            return embeddings.embed_calibration(recipe, texts, query=True, asked=False)
    return encode


def rerank(out: Path, collection, *, encoders: list[str], recipe, plans, typed=None, depths=None,
           doc_lang: str | None = None, call=None, index=None, encode=None) -> dict:
    """The whole search on this sample, stage by stage
    (tools/measure/pipeline_eval.py): BM25 with the plan's translations, each
    encoder alone and fused with BM25, and each after `recipe`'s reranker
    at every depth. Writes OUT/pipeline/pipeline.json."""
    import numpy as np

    from tools.measure import pipeline_eval as pe
    corpus = _jsonl(out / "corpus.jsonl")
    qs = _jsonl(out / "queries.jsonl")
    index_of = {e["uid"]: i for i, e in enumerate(corpus)}

    def lang_of(e: dict) -> str:
        fam = e.get("family") or ""
        lang = e.get("lang") or (fam if len(fam) == 2 and fam.isalpha() else None) or doc_lang
        if lang is None:
            raise SystemExit("the records' language is not in the sample (wikidata's families are "
                             "not languages): give --doc-lang, the language its chunks were embedded in")
        return lang
    docs = [pe.Doc(lang=lang_of(e), title=e.get("label") or "", chunks=list(e["chunks"])) for e in corpus]
    questions = [pe.Question(text=q["query"], relevant=frozenset({index_of[q["uid"]]}), lang=q.get("lang"))
                 for q in qs]
    pairs = [(d, j) for d, e in enumerate(corpus) for j in range(len(e["chunks"]))]
    rankings = {}
    rankers = {}
    for enc in encoders:
        blocks = sorted(out.glob(f"vec-{enc}-[0-9]*.npy"))
        if not blocks or not (out / f"qvec-{enc}.npy").exists():
            raise SystemExit(f"{enc}: not embedded in {out}")
        doc = np.concatenate([np.load(b) for b in blocks])
        if len(doc) != len(pairs):
            raise SystemExit(f"{enc}: {len(doc)} vectors for {len(pairs)} chunks — embed again")
        qv = np.load(out / f"qvec-{enc}.npy")
        dn = doc / np.linalg.norm(doc, axis=1, keepdims=True)
        qn = qv / np.linalg.norm(qv, axis=1, keepdims=True)
        sims = qn @ dn.T
        # the per-language vector leg: translations embedded as this
        # encoder's query side (jina's two sides are not reproduced here)
        if enc not in ("jina", "jina-gguf"):
            rankers[enc] = pe.QueryRanker(
                out / "pipeline" / f"qvec-{enc}.jsonl",
                (encode or _query_encoder)(enc), dn, pairs, depth=CHUNK_DEPTH,
                check=([q["query"] for q in qs], qv))
        top = min(CHUNK_DEPTH, sims.shape[1])
        rankings[enc] = []
        for q in range(len(qs)):
            best = np.argpartition(-sims[q], top - 1)[:top]
            best = best[np.lexsort((best, -sims[q][best]))]
            rankings[enc].append([pairs[int(i)] for i in best])
    return pe.evaluate(out / "pipeline", collection, docs, questions, rankings, recipe=recipe,
                       plans=plans, typed=typed, query_rankers=rankers,
                       depths=depths or pe.DEPTHS, call=call, index=index,
                       say=lambda m: print(m, file=sys.stderr))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sample")
    s.add_argument("out", type=Path)
    s.add_argument("--entities", type=int, default=20000)
    s.add_argument("--families", default=",".join(NINE))
    s.add_argument("--seed", type=int, default=0)
    t = sub.add_parser("sample-table")
    t.add_argument("out", type=Path)
    t.add_argument("--collection", required=True)
    t.add_argument("--langs", default="en,it")
    t.add_argument("--docs", type=int, default=10000)
    t.add_argument("--max-chunks", type=int, default=6)
    t.add_argument("--seed", type=int, default=0)
    q = sub.add_parser("queries")
    q.add_argument("out", type=Path)
    q.add_argument("--n", type=int, default=400)
    q.add_argument("--seed", type=int, default=0)
    q.add_argument("--prompt", choices=sorted(PROMPTS), default="wikidata")
    e = sub.add_parser("embed")
    e.add_argument("out", type=Path)
    e.add_argument("--encoder", choices=sorted(ENCODERS), required=True)
    sc = sub.add_parser("score")
    sc.add_argument("out", type=Path)
    r = sub.add_parser("rerank")
    r.add_argument("out", type=Path)
    r.add_argument("--collection", required=True)
    r.add_argument("--reranker-from", default=None,
                   help="another collection's reranker, where this one names none")
    r.add_argument("--encoders", default="bge,harrier")
    r.add_argument("--depth", type=int, action="append", default=None)
    r.add_argument("--doc-lang", default=None,
                   help="the chunks' language where the sample does not say it (wikidata: en)")
    a = ap.parse_args(argv)
    if a.cmd == "sample":
        got = sample(a.out, entities=a.entities, families=tuple(a.families.split(",")), seed=a.seed)
    elif a.cmd == "sample-table":
        got = sample_table(a.out, collection=a.collection, langs=tuple(a.langs.split(",")),
                           docs=a.docs, max_chunks=a.max_chunks, seed=a.seed)
    elif a.cmd == "queries":
        got = queries(a.out, n=a.n, seed=a.seed, prompt=a.prompt)
    elif a.cmd == "embed":
        got = embed(a.out, a.encoder)
    elif a.cmd == "rerank":
        from app import collections as collections_mod
        from tools.encoder_bakeoff import pe_reranker
        from tools.measure import pipeline_eval as pe
        c = collections_mod.by_name(a.collection)
        if c is None:
            raise SystemExit(f"no {a.collection} collection here")
        plans = pe.PlanStore(a.out / "pipeline" / "plans.jsonl", pe.search_plans(c, translate=True))
        typed = pe.PlanStore(a.out / "pipeline" / "plans-typed.jsonl", pe.search_plans(c, translate=False))
        got = rerank(a.out, c, encoders=a.encoders.split(","), recipe=pe_reranker(a.reranker_from or a.collection),
                     plans=plans, typed=typed, depths=a.depth, doc_lang=a.doc_lang)
        got = {k: got[k] for k in ("rows", "by_language", "pairs", "untranslated_plans")}
    else:
        got = score(a.out)
    print(json.dumps(got, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
