"""MEASUREMENT (not product code): open LLMs on the node's B70 as Latin -> English and
Greek -> English translators for gutenberg's books (docs/notes/classical-translation.md).

Runs on the node in a throwaway container from louise-rag:local, gutenberg's SHARD and
collection file mounted read-only, exactly as tools/measure/translate_before_embed.py
(whose sampling, pools and scoring it reuses). Every embedding goes through Louise's entry
points with gutenberg's own recipe; MADLAD through `translate.translate`, as Louise calls
it; the LLMs through LocalAI's OpenAI chat endpoint, greedy (temperature 0, top_k 1,
seed 0), the instruction in the request, two requests in flight so the model is never idle
between them (LocalAI's reclaimer evicts an idle unpinned model above its threshold).

Phases, each checkpointed under OUT (a re-run resumes):
  eval      --model M   -> eval_<M>.json        the reference set (classical_evalset.py)
  sample                -> rsample.json         retrieval books: la / el with no human
                                                English translation (coverage.json)
  retrieval --model M   -> rtrans_<M>.json      passages + titles of rsample, translated
  embed                 -> rvectors.npz         every condition, through Louise
  score                 -> results.json         chrF / BLEU, throughput, retrieval, projection
M is a LocalAI model name, or `madlad` (Louise's translate.translate).

Usage: python classical_translation.py --SHARD /data/collections/gutenberg/shard --out /out PHASE
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import translate_before_embed as tbe                     # noqa: E402  (the earlier method)

from app import collections as collections_mod          # noqa: E402
from app import dispatch, embeddings, translate          # noqa: E402

LLM_URL = "http://192.168.178.10:30286"
LANG_NAME = {"la": "Latin", "grc": "Ancient Greek", "el": "Greek"}
PROMPT = ("Translate the following {lang} text into English. Give a faithful and complete "
          "translation and nothing else: no title, no notes, no commentary, no original text."
          "\n\n{text}")
TITLE_PROMPT = ("Translate this {lang} book title into English. Answer with the English "
                "title only.\n\n{text}")
BOOKS_PER_LANG = 20
SEED = "classical-translation-2026-10-09"


def est_tokens(s: str) -> int:
    return max(1, len(s) // 4)          # app/chunking.py's estimate


def safe(model: str) -> str:
    return model.replace("/", "_")


# ---- translators ---------------------------------------------------------------

def llm(model: str, prompt: str, max_tokens: int) -> dict:
    t0 = time.monotonic()
    r = httpx.post(f"{LLM_URL}/v1/chat/completions", timeout=1800, json={
        "model": model, "messages": [{"role": "user", "content": prompt}],
        "temperature": 0, "top_k": 1, "top_p": 1.0, "seed": 0, "max_tokens": max_tokens})
    dt = time.monotonic() - t0
    if r.status_code != 200:
        return {"text": None, "s": round(dt, 2), "error": f"HTTP {r.status_code}: {r.text[:300]}"}
    j = r.json()
    return {"text": j["choices"][0]["message"]["content"].strip(), "s": round(dt, 2),
            "finish": j["choices"][0].get("finish_reason"), "usage": j.get("usage") or {}}


def madlad(text: str) -> dict:
    t0 = time.monotonic()
    got = translate.translate([(text, "en")])
    dt = time.monotonic() - t0
    out = got[0] if got else None
    return {"text": out, "s": round(dt, 2), "error": None if out else "no translation"}


def run_jobs(model: str, jobs: list[dict], path: Path, done: dict) -> dict:
    """jobs: {key, lang, text, kind}; results land in done[key], saved after each answer.
    Two in flight for an LLM (never idle between requests); one at a time for MADLAD."""
    lock = threading.Lock()
    hold = path.parent / "HOLD"                # written by classical_guard.py

    def one(j: dict) -> None:
        if model != "madlad":
            while hold.exists():               # the guard pre-empted the model: wait
                time.sleep(2)
        t_start = time.time()
        if model == "madlad":
            res = madlad(j["text"])
        else:
            prompt = (TITLE_PROMPT if j["kind"] == "title" else PROMPT).format(
                lang=LANG_NAME[j["lang"]], text=j["text"])
            res = llm(model, prompt, 256 if j["kind"] == "title" else
                      min(3072, 3 * est_tokens(j["text"]) + 256))
        res.update(t_start=round(t_start, 2), t_end=round(time.time(), 2),
                   src_est_tokens=est_tokens(j["text"]), kind=j["kind"], lang=j["lang"])
        with lock:
            done[j["key"]] = res
            path.write_text(json.dumps(done, ensure_ascii=False))
            u = res.get("usage") or {}
            print(f"{model} {j['key']}: {res['s']}s prompt={u.get('prompt_tokens')} "
                  f"out={u.get('completion_tokens')} {res.get('error') or ''}", flush=True)

    for attempt in range(6):                   # a pre-empted request failed: ask again
        todo = [j for j in jobs if not (done.get(j["key"]) or {}).get("text")]
        if not todo:
            break
        with ThreadPoolExecutor(1 if model == "madlad" else 2) as ex:
            list(ex.map(one, todo))
    return done


def phase_eval(out: Path, model: str) -> None:
    items = json.loads((out / "evalset.json").read_text())
    path = out / f"eval_{safe(model)}.json"
    done = json.loads(path.read_text()) if path.exists() else {}
    jobs = [{"key": i["id"], "lang": i["lang"], "text": i["src"], "kind": "passage"} for i in items]
    run_jobs(model, jobs, path, done)


# ---- retrieval ------------------------------------------------------------------

def phase_sample(shard_tree: Path, out: Path) -> dict:
    """Up to 20 books each of la and el that coverage.json found NO human English translation
    for, sampled as translate_before_embed.sample does (one document passage, one query
    passage, never overlapping), plus its 500 English distractors (the same seeded draw)."""
    path = out / "rsample.json"
    if path.exists():
        return json.loads(path.read_text())
    cov = json.loads((out / "coverage.json").read_text())
    uncovered = {b["uid"] for b in cov["books"] if not b["covered"]}
    rc = out / "recall_check.json"               # books the hand check found covered after all
    excluded = set(json.loads(rc.read_text())["covered_after_check"]) if rc.exists() else set()
    shards = tbe.shards_by_lang(shard_tree)
    books, skipped = [], {}
    for lang in ("la", "el"):
        uids = [(p, u) for p in shards.get(lang, []) for u in tbe.pack_records.read_index(p).uids
                if u in uncovered]
        tbe.rng_for(SEED, lang).shuffle(uids)
        got = 0
        for p, u in uids:                       # excluded AFTER the shuffle: the draw is stable
            if got >= BOOKS_PER_LANG:
                break
            if u in excluded:
                continue
            b = tbe.sample_book(tbe.pack_records.read_record(p, u))
            if b is None:
                skipped[lang] = skipped.get(lang, 0) + 1
                continue
            books.append(b)
            got += 1
        print(f"sample {lang}: {got} books of {len(uids)} uncovered", flush=True)
    chosen = {b["uid"] for b in books}
    en = [(p, u) for p in shards["en"] for u in tbe.pack_records.read_index(p).uids]
    tbe.rng_for("distractors").shuffle(en)                # the earlier note's draw
    distractors = []
    for p, u in en:
        if len(distractors) >= tbe.DISTRACTORS:
            break
        if u in chosen:
            continue
        body = tbe.body_of(tbe.pack_records.read_record(p, u))
        if len(body) < tbe.MIN_BODY:
            continue
        _, d = tbe.passage_at(body, tbe.rng_for("dis", u).uniform(0.15, 0.85))
        if len(d) >= tbe.PASSAGE_MIN:
            distractors.append({"uid": u, "doc": d})
    data = {"books": books, "distractors": distractors, "skipped": skipped}
    path.write_text(json.dumps(data, ensure_ascii=False))
    return data


def phase_retrieval(out: Path, model: str) -> None:
    data = json.loads((out / "rsample.json").read_text())
    path = out / f"rtrans_{safe(model)}.json"
    done = json.loads(path.read_text()) if path.exists() else {}
    jobs = []
    for b in data["books"]:
        jobs += [{"key": f"{b['uid']}|title", "lang": b["lang"], "text": b["title"], "kind": "title"},
                 {"key": f"{b['uid']}|doc", "lang": b["lang"], "text": b["doc"], "kind": "passage"},
                 {"key": f"{b['uid']}|qpassage", "lang": b["lang"], "text": b["qpassage"],
                  "kind": "passage"}]
    run_jobs(model, jobs, path, done)


def models_done(out: Path) -> list[str]:
    return sorted(p.stem[len("rtrans_"):] for p in out.glob("rtrans_*.json"))


def phase_embed(out: Path, g) -> dict:
    path = out / "rvectors.npz"
    if path.exists():
        z = np.load(path)
        return {k: z[k] for k in z.files}
    data = json.loads((out / "rsample.json").read_text())
    books = data["books"]
    runs: set = set()
    t0 = time.monotonic()

    def chunks(texts: list[str]) -> np.ndarray:
        v = np.asarray(embeddings.embed_chunks(g, texts, runs=runs), dtype=np.float32)
        return v / np.linalg.norm(v, axis=1, keepdims=True)

    def queries(texts: list[str]) -> np.ndarray:
        v = np.asarray([embeddings.embed_query(g, x, runs=runs) for x in texts], dtype=np.float32)
        return v / np.linalg.norm(v, axis=1, keepdims=True)

    def q1(b: dict, title: str) -> str:
        meta = "; ".join(b["meta"])
        return f"{title}. {meta}" if meta else title

    vec = {"distractors": chunks([d["doc"] for d in data["distractors"]]),
           "raw_doc": chunks([b["doc"] for b in books]),
           "raw_q2": queries([b["qpassage"] for b in books]),
           "q1_raw_title": queries([q1(b, b["title"]) for b in books])}
    for m in models_done(out):
        tr = json.loads((out / f"rtrans_{m}.json").read_text())

        def get(b: dict, f: str) -> str:
            return (tr.get(f"{b['uid']}|{f}") or {}).get("text") or b[f if f != "title" else "title"]

        vec[f"{m}_doc"] = chunks([get(b, "doc") for b in books])
        vec[f"{m}_q2"] = queries([get(b, "qpassage") for b in books])
        vec[f"{m}_q1"] = queries([q1(b, get(b, "title")) for b in books])
    np.savez(path, **vec)
    (out / "rembed_meta.json").write_text(json.dumps(
        {"runs": sorted(r or "(none)" for r in runs), "seconds": round(time.monotonic() - t0, 1),
         "vectors": {k: list(v.shape) for k, v in vec.items()}}))
    return vec


# ---- scoring --------------------------------------------------------------------

def throughput(res: dict) -> dict:
    xs = [r for r in res.values() if r.get("text") and r.get("kind") != "title"]
    if not xs:
        return {}
    wall, end = 0.0, 0.0                        # time with at least one request in flight
    for a, b in sorted((r["t_start"], r["t_end"]) for r in xs):
        if a > end:
            wall += b - a
            end = b
        elif b > end:
            wall += b - end
            end = b
    out_tok = sum((r.get("usage") or {}).get("completion_tokens") or 0 for r in xs)
    in_tok = sum((r.get("usage") or {}).get("prompt_tokens") or 0 for r in xs)
    src = sum(r["src_est_tokens"] for r in xs)
    gen_s = [((r.get("usage") or {}).get("timing_token_generation") or 0) / 1000 for r in xs]
    return {"passages": len(xs), "wall_s": round(wall, 1), "s_per_passage": round(wall / len(xs), 2),
            "completion_tokens": out_tok, "prompt_tokens": in_tok, "src_est_tokens": src,
            "out_tok_per_wall_s": round(out_tok / wall, 2) if out_tok else None,
            "gen_tok_per_s": round(out_tok / sum(gen_s), 2) if sum(gen_s) else None,
            "wall_s_per_src_est_token": round(wall / src, 5),
            "failed": sum(1 for r in res.values() if not r.get("text")),
            "truncated": sum(1 for r in xs if r.get("finish") == "length")}


def phase_score(out: Path) -> dict:
    import sacrebleu                                     # throwaway --target install, /out/pylib
    items = json.loads((out / "evalset.json").read_text())
    res: dict = {"eval": {}, "throughput": {}, "retrieval": {}}
    for p in sorted(out.glob("eval_*.json")):
        m = p.stem[len("eval_"):]
        tr = json.loads(p.read_text())
        res["throughput"][m] = throughput(tr)
        per = {}
        for lang in ("la", "grc", "el"):
            xs = [i for i in items if i["lang"] == lang]
            hyp = [(tr.get(i["id"]) or {}).get("text") or "" for i in xs]
            ref = [i["ref"] for i in xs]
            per[lang] = {"n": len(xs), "chrF": round(sacrebleu.corpus_chrf(hyp, [ref]).score, 2),
                         "BLEU": round(sacrebleu.corpus_bleu(hyp, [ref]).score, 2),
                         "by_work": {}}
            for w in sorted({i["work"] for i in xs}):
                ws = [k for k, i in enumerate(xs) if i["work"] == w]
                per[lang]["by_work"][w] = {
                    "n": len(ws),
                    "chrF": round(sacrebleu.corpus_chrf([hyp[k] for k in ws], [[ref[k] for k in ws]]).score, 2),
                    "BLEU": round(sacrebleu.corpus_bleu([hyp[k] for k in ws], [[ref[k] for k in ws]]).score, 2)}
            per[lang]["per_item_chrF"] = {xs[k]["id"]: round(sacrebleu.sentence_chrf(hyp[k], [ref[k]]).score, 1)
                                          for k in range(len(xs))}
        res["eval"][m] = per
    vp = out / "rvectors.npz"
    if vp.exists():
        res["retrieval"] = score_retrieval(out, dict(np.load(vp)))
    for p in sorted(out.glob("rtrans_*.json")):
        res["throughput"][f"retrieval:{p.stem[len('rtrans_'):]}"] = throughput(json.loads(p.read_text()))
    (out / "results.json").write_text(json.dumps(res, indent=1, ensure_ascii=False))
    return res


def score_retrieval(out: Path, vec: dict) -> dict:
    data = json.loads((out / "rsample.json").read_text())
    books = data["books"]
    langs = np.array([b["lang"] for b in books])
    res: dict = {"pool": len(books) + len(vec["distractors"]), "per_book": [], "conditions": {}}
    conds = {"raw": ("raw_doc", "q1_raw_title", "raw_q2")}
    for m in models_done(out):
        conds[m] = (f"{m}_doc", f"{m}_q1", f"{m}_q2")
    rk = {}
    for c, (d, q1, q2) in conds.items():
        pool = np.vstack([vec[d], vec["distractors"]])
        rk[c] = {"q1": tbe.ranks(vec[q1], pool), "q2": tbe.ranks(vec[q2], pool)}
        # Q1 with the SAME query vector against both pools isolates the document side
        rk[c]["q1_rawquery"] = tbe.ranks(vec["q1_raw_title"], pool)
    for lang in ("la", "el"):
        m_ = langs == lang
        row = {}
        for c in conds:
            row[c] = {k: tbe.metrics(v[m_]) for k, v in rk[c].items()}
            if c != "raw":
                for q in ("q1", "q2"):
                    a, b_ = rk["raw"][q][m_], rk[c][q][m_]
                    row[c][f"{q}_paired"] = {"tr_better": int((b_ < a).sum()),
                                             "tr_worse": int((b_ > a).sum()), "tie": int((b_ == a).sum())}
            sel = np.flatnonzero(m_)
            d, q1, q2 = conds[c]
            row[c]["within"] = {"q1": tbe.metrics(tbe.ranks(vec[q1][sel], vec[d][sel])),
                                "q2": tbe.metrics(tbe.ranks(vec[q2][sel], vec[d][sel]))}
        res["conditions"][lang] = row
    for j, b in enumerate(books):
        res["per_book"].append({"uid": b["uid"], "lang": b["lang"], "title": b["title"][:80],
                                **{f"{c}_{q}": int(rk[c][q][j]) for c in conds for q in ("q1", "q2")}})
    return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--SHARD", type=Path, default=Path("/data/collections/gutenberg/shard"))
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--model")
    ap.add_argument("phase", choices=["eval", "sample", "retrieval", "embed", "score"])
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    dispatch.set_default_level(dispatch.Level.CORPUS)     # background work on a shared GPU
    if a.phase == "eval":
        phase_eval(a.out, a.model)
    elif a.phase == "sample":
        phase_sample(a.shard_tree, a.out)
    elif a.phase == "retrieval":
        phase_retrieval(a.out, a.model)
    elif a.phase == "embed":
        g = collections_mod.by_name("gutenberg")
        phase_embed(a.out, g)
    else:
        print(json.dumps(phase_score(a.out)["throughput"], indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
