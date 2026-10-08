"""Switch-over step 3 (docs/notes/vega.md §12): every staged model loaded once,
asked once, compared, and unloaded. Runs inside `vega-proof` (the receipts
service's container: httpx and app.vega at HEAD's bytes), with the inputs
texts.py, convert_article.sh and article_reference.py wrote copied to
/tmp/stage:

    docker exec vega-proof python /tmp/stage/verify.py [model …]  > report.json

- a `vega-` encoder: 64 texts (64 pairs for vega-medcpt-article-ov), tagged, in
  base64, every vector checked against its receipt (`client.verify_response`);
  then the same inputs to RAGE's model of the same weights and options (the
  article pair encoder has none in RAGE: transformers' fp32 vectors instead);
  cosine min/median, bit-identical count, and the run object VeGa holds.
  For two (granite, the article pair encoder) the model is loaded a second time
  and the run must be the same one (the backend's tree did not move).
- the ordinary ones: a rerank (against RAGE's), a translation (against RAGE's),
  a chat completion.
Read-only use of RAGE. Prints one JSON report; nothing from a Louise collection.
"""
from __future__ import annotations

import base64
import json
import math
import os
import statistics
import struct
import sys
import time

import httpx

from app.vega import client

LOCALAI = os.environ.get("LOCALAI_URL", "http://192.168.178.10:30286")
RAGE = os.environ.get("RAGE_URL", "http://192.168.178.10:30437")
VEGA = os.environ.get("VEGA_URL", "http://127.0.0.1:8090")
IN = "/tmp/stage"
BATCH = 32
#: (staged name, RAGE's name, characters per text: the 512-token models get
#: the texts cut, both sides alike, so no input is over their context)
ENCODERS = {
    "vega-granite": ("granite-embedding-107m-multilingual", 1000),
    "vega-bge-m3": ("bge-m3", None),
    "vega-medcpt-query": ("medcpt-query", 1000),
    "vega-medcpt-article": ("medcpt-article", 1000),
    "vega-medcpt-article-ov": (None, None),
    "vega-qwen3-embedding-4b": ("qwen3-embedding-4b", None),
}
RELOAD = {"vega-granite", "vega-medcpt-article-ov"}
RERANK = {"louise-rerank-bge": "bge-reranker", "louise-rerank-medcpt": "medcpt-rerank"}
TRANSLATE = {"louise-translate-madlad": "madlad"}
CHAT = {"louise-chat-qwen3-8b"}

http = httpx.Client(timeout=900)


def floats(b: bytes) -> list[float]:
    return list(struct.unpack(f"<{len(b) // 4}f", b))


def cosine(a, b) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    return dot / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))


def shutdown(model: str) -> int:
    return http.post(f"{LOCALAI}/backend/shutdown", json={"model": model}).status_code


def vega_embed(model: str, items: list) -> tuple[list[list[float]], set[str], float]:
    vectors, runs, t = [], set(), 0.0
    for i in range(0, len(items), BATCH):
        b = client.tag_batch(items[i:i + BATCH])
        t0 = time.monotonic()
        r = http.post(f"{LOCALAI}/v1/embeddings",
                      json={"model": model, "input": list(b.inputs), "encoding_format": "base64"})
        t += time.monotonic() - t0
        if r.status_code != 200:
            raise RuntimeError(f"{model}: HTTP {r.status_code} {r.text[:400]}")
        v, ids = client.verify_response(b, r.content, base_url=VEGA, get=http.get)
        vectors += v
        runs |= ids
    return vectors, runs, t


def rage_embed(model: str, texts: list[str]) -> tuple[list[list[float]], set[str]]:
    out, runs = [], set()
    for i in range(0, len(texts), BATCH):
        r = http.post(f"{RAGE}/upstream/{model}/v1/embeddings",
                      json={"model": model, "input": texts[i:i + BATCH]})
        r.raise_for_status()
        runs.add(r.headers.get("louise-run"))
        out += [client.as_floats(v) for v in client.parse_response(r.content)]
    return out, runs


def compare(mine, theirs) -> dict:
    cos = [cosine(a, b) for a, b in zip(mine, theirs)]
    return {"n": len(cos), "cosine_min": round(min(cos), 7),
            "cosine_median": round(statistics.median(cos), 7),
            "bit_identical": sum(struct.pack(f"<{len(a)}f", *a) == struct.pack(f"<{len(b)}f", *b)
                                 for a, b in zip(mine, theirs)),
            "max_abs_diff": float(f"{max(max(abs(x - y) for x, y in zip(a, b)) for a, b in zip(mine, theirs)):.3g}")}


def encoder(model: str, texts: list[str], pairs: list) -> dict:
    rage_model, cut = ENCODERS[model]
    items = [tuple(p) for p in pairs] if model.endswith("-ov") else \
        [t[:cut] if cut else t for t in texts]
    rep: dict = {"inputs": len(items), "form": "pair" if model.endswith("-ov") else "text"}
    t0 = time.monotonic()
    first, runs0, _ = vega_embed(model, items[:1])
    rep["first_call_with_load_s"] = round(time.monotonic() - t0, 1)
    vectors, runs, t = vega_embed(model, items)
    rep["embed_s"] = round(t, 2)
    rep["dim"] = len(vectors[0])
    rep["receipts"] = f"{len(items)}/{len(items)} verified"
    rep["run_ids"] = sorted(runs | runs0)
    rep["run"] = http.get(f"{VEGA}/vega/runs/{rep['run_ids'][0]}").json()
    if rage_model:
        theirs, rage_runs = rage_embed(rage_model, items)
        rep["vs_rage"] = {"model": rage_model, **compare(vectors, theirs),
                          "rage_runs": sorted(x for x in rage_runs if x)}
    else:
        ref = json.load(open(f"{IN}/article-reference.json"))["vectors"]
        rep["vs_transformers_fp32_cpu"] = compare(vectors, [floats(base64.b64decode(v)) for v in ref])
    rep["shutdown"] = shutdown(model)
    if model in RELOAD:
        time.sleep(2)
        _, again, _ = vega_embed(model, items[:1])
        rep["reload_same_run"] = again == set(rep["run_ids"])
        rep["shutdown_2"] = shutdown(model)
    return rep


def rerank(model: str, texts: list[str]) -> dict:
    q = "How do I read a compressed archive file?"
    docs = [t[:1000] for t in texts[:8]] + ["Read and write ZIP and gzip compressed archive files."]
    body = {"query": q, "documents": docs, "top_n": len(docs)}
    t0 = time.monotonic()
    r = http.post(f"{LOCALAI}/v1/rerank", json={"model": model, **body})
    dt = time.monotonic() - t0
    if r.status_code != 200:
        return {"error": f"HTTP {r.status_code} {r.text[:400]}", "shutdown": shutdown(model)}
    mine = {x["index"]: x["relevance_score"] for x in r.json()["results"]}
    rr = http.post(f"{RAGE}/upstream/{RERANK[model]}/v1/rerank", json={"model": RERANK[model], **body})
    rr.raise_for_status()
    theirs = {x["index"]: x["relevance_score"] for x in rr.json()["results"]}
    order = lambda s: sorted(s, key=lambda i: -s[i])          # noqa: E731
    return {"first_call_with_load_s": round(dt, 1), "documents": len(docs),
            "top": order(mine)[0], "same_order_as_rage": order(mine) == order(theirs),
            "max_score_diff_vs_rage": float(f"{max(abs(mine[i] - theirs[i]) for i in mine):.3g}"),
            "shutdown": shutdown(model)}


def translate(model: str) -> dict:
    items = [{"text": "The library opens at nine in the morning.", "to": "it"},
             {"text": "Where is the nearest train station?", "to": "de"},
             {"text": "Water boils at one hundred degrees.", "to": "fr"},
             {"text": "This is a test.", "to": "ja"}]
    prompt = json.dumps({"items": items})
    t0 = time.monotonic()
    r = http.post(f"{LOCALAI}/v1/completions", json={"model": model, "prompt": prompt})
    dt = time.monotonic() - t0
    if r.status_code != 200:
        return {"error": f"HTTP {r.status_code} {r.text[:400]}", "shutdown": shutdown(model)}
    mine = json.loads(r.json()["choices"][0]["text"])
    rr = http.post(f"{RAGE}/upstream/{TRANSLATE[model]}/v1/completions",
                   json={"model": TRANSLATE[model], "prompt": prompt})
    theirs = json.loads(rr.json()["choices"][0]["text"]) if rr.status_code == 200 else None
    return {"first_call_with_load_s": round(dt, 1), "translations": mine.get("translations"),
            "peak_mb": mine.get("peak_mb"),
            "same_as_rage": None if theirs is None else mine.get("translations") == theirs.get("translations"),
            "rage_translations": None if theirs is None else theirs.get("translations"),
            "shutdown": shutdown(model)}


def chat(model: str) -> dict:
    t0 = time.monotonic()
    r = http.post(f"{LOCALAI}/v1/chat/completions", json={
        "model": model, "max_tokens": 200,
        "messages": [{"role": "user", "content": "What is the capital of Italy? Answer in one word. /no_think"}]})
    dt = time.monotonic() - t0
    if r.status_code != 200:
        return {"error": f"HTTP {r.status_code} {r.text[:400]}", "shutdown": shutdown(model)}
    d = r.json()
    return {"first_call_with_load_s": round(dt, 1), "answer": d["choices"][0]["message"]["content"],
            "usage": d.get("usage"), "shutdown": shutdown(model)}


def main() -> int:
    texts = json.load(open(f"{IN}/texts.json"))
    pairs = json.load(open(f"{IN}/pairs.json"))
    wanted = sys.argv[1:] or [*ENCODERS, *RERANK, *TRANSLATE, *CHAT]
    report: dict = {}
    for m in wanted:
        try:
            if m in ENCODERS:
                report[m] = encoder(m, texts, pairs)
            elif m in RERANK:
                report[m] = rerank(m, texts)
            elif m in TRANSLATE:
                report[m] = translate(m)
            elif m in CHAT:
                report[m] = chat(m)
        except Exception as e:                          # noqa: BLE001 — reported, and unloaded
            report[m] = {"error": f"{type(e).__name__}: {e}", "shutdown": shutdown(m)}
        print(json.dumps({m: report[m]}), file=sys.stderr, flush=True)
    print(json.dumps(report, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
