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

  sample   OUT_DIR [--entities 20000] [--families person,work,…] [--seed 0]
           records read from the SHARD's parts of those families (no
           snapshot needed: the parts the 10-30 build wrote), cut into
           chunks through `hold.land.cut` — the chunks the space embeds.
  queries  OUT_DIR [--n 400]  questions by the chat model (stored: the set)
  embed    OUT_DIR --encoder jina|bge  each side through the PRODUCTION
           client (`embeddings._embed_texts`) with the collection's recipe —
           wikidata's for jina-v3, wikipedia's for bge-m3; resumable by block
  score    OUT_DIR  the table, and the verdict: bge-m3 unless jina-v3 is
           better on MRR@10 with p < 0.05.
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
ENCODERS = {"jina": "wikidata", "bge": "wikipedia"}   # the collection whose recipe embeds
NINE = ("person", "work", "place", "organization", "event", "heritage", "built", "scholarly", "property")
PROMPT = {
    "en": ("Here is what Wikidata says about one entity. Write ONE short search query, in English, that "
           "a person looking for this entity might type. Do NOT use its name, label or any alias; "
           "describe it by what it is or did. Answer with the query only. /no_think\n\n{text}"),
    "it": ("Ecco cosa dice Wikidata di un'entità. Scrivi UNA breve domanda di ricerca, in italiano, che "
           "una persona potrebbe digitare per trovarla. NON usare il suo nome, l'etichetta né alias; "
           "descrivila per ciò che è o ha fatto. Rispondi solo con la domanda. /no_think\n\n{text}"),
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


# ---------------------------------------------------------------- queries


def leaks(query: str, label: str) -> bool:
    """The question names the entity: its label, or any word of it longer
    than three letters (a known-item question must not be a name lookup)."""
    q = query.lower()
    words = [w for w in re.findall(r"\w+", label.lower()) if len(w) > 3]
    return bool(label) and (label.lower() in q or any(re.search(rf"\b{re.escape(w)}\b", q) for w in words))


def queries(out: Path, *, n: int, seed: int, chat: Callable[[str], str] | None = None) -> dict:
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
            q = chat(PROMPT[lang].format(text=e["chunks"][0][:2000])).strip().strip('"').splitlines()[0:1]
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
    # window another corpus was sized for
    return dataclasses.replace(recipe, ctx=max(int(recipe.ctx or 0), 8192))


def embed(out: Path, encoder: str, *, embed_texts: Callable | None = None) -> dict:
    import numpy as np
    if embed_texts is None:
        from app import embeddings
        recipe = _recipe(encoder)
        embed_texts = lambda texts, query: embeddings._embed_texts(   # noqa: E731
            texts, recipe=recipe, query=query, asked=False)
    corpus = _jsonl(out / "corpus.jsonl")
    texts = [t for e in corpus for t in e["chunks"]]
    written = 0
    for b in range(0, len(texts), BLOCK):
        path = out / f"vec-{encoder}-{b // BLOCK:05d}.npy"
        if path.exists():
            continue
        np.save(path, np.asarray(embed_texts(texts[b:b + BLOCK], False), dtype="float32"))
        written += 1
        print(f"{encoder}: block {b // BLOCK + 1} of {(len(texts) + BLOCK - 1) // BLOCK}", file=sys.stderr)
    qpath = out / f"qvec-{encoder}.npy"
    qs = _jsonl(out / "queries.jsonl")
    if not qpath.exists() or len(np.load(qpath)) != len(qs):
        np.save(qpath, np.asarray(embed_texts([q["query"] for q in qs], True), dtype="float32"))
    return {"chunks": len(texts), "blocks_written": written, "queries": len(qs)}


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
        blocks = sorted(out.glob(f"vec-{enc}-*.npy"))
        if not blocks or not (out / f"qvec-{enc}.npy").exists():
            continue
        doc = np.concatenate([np.load(p) for p in blocks])
        if len(doc) != len(owner):
            raise SystemExit(f"{enc}: {len(doc)} vectors for {len(owner)} chunks — embed again")
        best = best_by_entity(doc, owner, np.load(out / f"qvec-{enc}.npy"), len(corpus))
        tscore = best[np.arange(len(qs)), target]
        rank = 1 + (best > tscore[:, None]).sum(axis=1)
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
    if "jina" in rr and "bge" in rr:
        d = [b - j for b, j in zip(rr["bge"], rr["jina"])]
        p = randomization(d)
        res["bge_minus_jina"] = {"mrr10": round(sum(d) / len(d), 4), "p": round(p, 4),
                                 "better": sum(x > 0 for x in d), "worse": sum(x < 0 for x in d)}
        jina_better = res["mrr10"]["jina"] > res["mrr10"]["bge"] and p < 0.05
        res["verdict"] = "keep jina-v3: it finds entities better (p < 0.05)" if jina_better else \
            "switch to bge-m3: it finds entities at least as well"
    return res


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sample")
    s.add_argument("out", type=Path)
    s.add_argument("--entities", type=int, default=20000)
    s.add_argument("--families", default=",".join(NINE))
    s.add_argument("--seed", type=int, default=0)
    q = sub.add_parser("queries")
    q.add_argument("out", type=Path)
    q.add_argument("--n", type=int, default=400)
    q.add_argument("--seed", type=int, default=0)
    e = sub.add_parser("embed")
    e.add_argument("out", type=Path)
    e.add_argument("--encoder", choices=sorted(ENCODERS), required=True)
    sc = sub.add_parser("score")
    sc.add_argument("out", type=Path)
    a = ap.parse_args(argv)
    if a.cmd == "sample":
        got = sample(a.out, entities=a.entities, families=tuple(a.families.split(",")), seed=a.seed)
    elif a.cmd == "queries":
        got = queries(a.out, n=a.n, seed=a.seed)
    elif a.cmd == "embed":
        got = embed(a.out, a.encoder)
    else:
        got = score(a.out)
    print(json.dumps(got, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
