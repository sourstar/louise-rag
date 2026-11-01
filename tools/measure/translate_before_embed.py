"""MEASUREMENT (not product code): does translating a Project Gutenberg
passage to English before embedding improve retrieval, for languages the
encoder (qwen3-embedding-4b) may not cover?

Run on the node in a throwaway container, gutenberg's SHARD and collection
file mounted read-only (docs/notes/translate-before-embed.md has the setup
and the results). Every embedding goes through Louise's entry points with
gutenberg's own recipe — `embeddings.embed_chunks` for passages,
`embeddings.embed_query` for queries; every translation through
`translate.translate`, as Louise calls MADLAD.

Phases, each checkpointed to OUT (a re-run resumes):
  sample    -> sample.json        passages + queries, seeded
  translate -> translations.json  MADLAD -> English, timed
  embed     -> vectors.npz
  score     -> results.json       recall@1/5/10, MRR, per language, RAW vs TRANSLATED

Usage: python translate_before_embed.py --SHARD /data/collections/gutenberg/shard --out /out
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import sys
import time
from pathlib import Path

import numpy as np

from app import collections as collections_mod
from app import embeddings, translate
from app.rage import dispatch
from app.pack import records as pack_records
from app.plugins import shard as shard_plugin

SEED = "xlate-measure-2026-10-08"
#: not in Qwen3's 119-language list (qwenlm.github.io/blog/qwen3, the list
#: the Qwen3-Embedding card defers to: "100+ languages ... inherits Qwen3")
RARE = ["la", "eo", "fy", "enm", "arp", "gla", "sco", "sa", "oji", "nah",
        "myn", "mi", "iu", "br", "bo", "nap", "rmq", "ia"]
CONTROLS = ["en", "fr", "de", "fi", "nl"]
BOOKS_PER_LANG = 20
DISTRACTORS = 500
PASSAGE_MIN, PASSAGE_TARGET, PASSAGE_MAX = 1200, 1600, 2000   # chars (~300/400/500 est. tokens)
MIN_BODY = 4 * PASSAGE_TARGET
LANG_BUDGET_S = 15 * 60
EN_STOP = set("""the of and to a in that is was he for it with as his on be at by i
this had not are but from or have an they which you were her she there would their we
him been has when who will more no if out so said what up its about into than them can
only other new some could time these two may then do first any my now such like our over
man me even most made after also did many before must through back years where much your
way well down should because each just those people how too little state good very make
world still own see men work long get here between both life being under never day same
another know while last might us great old year off come since against go came right used
take three""".split())


def est_tokens(s: str) -> int:
    return max(1, len(s) // 4)          # app/chunking.py's estimate


def en_share(s: str) -> float:
    words = re.findall(r"[a-zA-Z]+", s.lower())
    return round(sum(w in EN_STOP for w in words) / len(words), 3) if words else 0.0


def rng_for(*parts: str) -> random.Random:
    return random.Random(hashlib.sha256("|".join((SEED,) + parts).encode()).hexdigest())


# ---- sampling ---------------------------------------------------------------

def shards_by_lang(shard_tree: Path) -> dict[str, list[Path]]:
    out: dict[str, list[Path]] = {}
    for p in shard_plugin.shard_files(shard_tree):
        lang = p.parent.name.split("=", 1)[1]
        out.setdefault(lang, []).append(p)
    return out


def body_of(rec: dict) -> str:
    return "\n\n".join(b for _, b in rec.get("sections") or [] if b and b.strip())


def passage_at(body: str, frac: float) -> tuple[int, str]:
    """A passage starting at the first paragraph break after `frac` of the
    body, paragraphs added up to the target, cut on whitespace at the max."""
    start = int(len(body) * frac)
    nl = body.find("\n", start)
    if nl != -1 and nl - start < 1500:
        start = nl + 1
    text = body[start:start + PASSAGE_MAX + 400]
    out, size = [], 0
    for para in re.split(r"(\n+)", text):
        out.append(para)
        size += len(para)
        if size >= PASSAGE_TARGET:
            break
    p = "".join(out).strip()
    if len(p) > PASSAGE_MAX:
        cut = p.rfind(" ", PASSAGE_TARGET, PASSAGE_MAX)
        p = p[:cut if cut > 0 else PASSAGE_MAX].strip()
    p = re.sub(r"[ \t]+", " ", p)
    return start, p


def subjects_query(rec: dict) -> list[str]:
    extra = rec.get("extra") or {}
    lcsh = (extra.get("subjects_by_scheme") or {}).get("LCSH")
    subjects = list(lcsh) if lcsh else [s for s in rec.get("subjects") or []
                                        if not re.fullmatch(r"[A-Z]{1,3}", s)]
    shelves = [s.replace("Category:", "").strip()
               for s in (extra.get("bookshelves") or "").split("|") if s.strip()]
    return subjects + [s for s in shelves if s not in subjects]


def sample_book(rec: dict) -> dict | None:
    body = body_of(rec)
    if len(body) < MIN_BODY:
        return None
    r = rng_for("book", rec["uid"])
    d_start, doc = passage_at(body, r.uniform(0.15, 0.45))
    q_start, qp = passage_at(body, r.uniform(0.55, 0.85))
    if len(doc) < PASSAGE_MIN or len(qp) < PASSAGE_MIN or q_start < d_start + len(doc):
        return None
    return {"uid": rec["uid"], "lang": rec["lang"], "title": rec.get("title") or "",
            "languages": (rec.get("extra") or {}).get("languages"),
            "meta": subjects_query(rec), "doc": doc, "qpassage": qp,
            "body_chars": len(body), "en_share": en_share(doc)}


def sample(shard_tree: Path, out: Path) -> dict:
    path = out / "sample.json"
    if path.exists():
        return json.loads(path.read_text())
    shards = shards_by_lang(shard_tree)
    books: list[dict] = []
    skipped: dict[str, int] = {}
    for lang in RARE + CONTROLS:
        uids = [(p, u) for p in shards.get(lang, []) for u in pack_records.read_index(p).uids]
        rng_for("lang", lang).shuffle(uids)
        got = 0
        for p, u in uids:
            if got >= BOOKS_PER_LANG:
                break
            b = sample_book(pack_records.read_record(p, u))
            if b is None:
                skipped[lang] = skipped.get(lang, 0) + 1
                continue
            books.append(b)
            got += 1
        print(f"sample {lang}: {got} books ({len(uids)} in shard, {skipped.get(lang, 0)} too short)",
              flush=True)
    chosen = {b["uid"] for b in books}
    en = [(p, u) for p in shards["en"] for u in pack_records.read_index(p).uids]
    rng_for("distractors").shuffle(en)
    distractors = []
    for p, u in en:
        if len(distractors) >= DISTRACTORS:
            break
        if u in chosen:
            continue
        rec = pack_records.read_record(p, u)
        body = body_of(rec)
        if len(body) < MIN_BODY:
            continue
        _, d = passage_at(body, rng_for("dis", u).uniform(0.15, 0.85))
        if len(d) >= PASSAGE_MIN:
            distractors.append({"uid": u, "doc": d})
    data = {"books": books, "distractors": distractors, "skipped": skipped}
    path.write_text(json.dumps(data, ensure_ascii=False))
    return data


# ---- translation ------------------------------------------------------------

def translate_all(data: dict, out: Path) -> dict:
    path = out / "translations.json"
    tr = json.loads(path.read_text()) if path.exists() else {"titles": {}, "books": {}, "calls": [],
                                                             "stopped": {}}

    def save() -> None:
        path.write_text(json.dumps(tr, ensure_ascii=False))

    titles = [b for b in data["books"] if b["lang"] != "en" and b["uid"] not in tr["titles"]]
    for i in range(0, len(titles), 16):
        part = titles[i:i + 16]
        t0 = time.monotonic()
        got = translate.translate([(b["title"], "en") for b in part])
        dt = time.monotonic() - t0
        tr["calls"].append({"kind": "titles", "n": len(part), "s": round(dt, 2), "ok": bool(got)})
        for b, t in zip(part, got or [None] * len(part)):
            tr["titles"][b["uid"]] = t
        save()

    for lang in RARE + CONTROLS:
        if lang in tr["stopped"]:
            continue
        spent = sum(c["s"] for c in tr["calls"] if c.get("lang") == lang)
        todo = [b for b in data["books"] if b["lang"] == lang and b["uid"] not in tr["books"]]
        for k, b in enumerate(todo):
            done = sum(1 for c in tr["calls"] if c.get("lang") == lang)
            if done and spent / done * (done + len(todo) - k) > LANG_BUDGET_S:
                tr["stopped"][lang] = f"projected > {LANG_BUDGET_S}s after {done} books"
                print(f"translate {lang}: STOPPED ({tr['stopped'][lang]})", flush=True)
                break
            items = [(b["doc"], "en"), (b["qpassage"], "en")]
            t0 = time.monotonic()
            got = translate.translate(items)
            if not got:                       # one at a time, inside the 120 s bound
                got = [(translate.translate([it]) or [None])[0] for it in items]
            dt = time.monotonic() - t0
            spent += dt
            in_tok = sum(est_tokens(t) for t, _ in items)
            out_tok = sum(est_tokens(t) for t in got if t)
            tr["calls"].append({"kind": "book", "lang": lang, "uid": b["uid"], "s": round(dt, 2),
                                "in_tok": in_tok, "out_tok": out_tok,
                                "ok": all(got)})
            tr["books"][b["uid"]] = {"doc": got[0], "qpassage": got[1]}
            print(f"translate {lang} {b['uid']}: {dt:.1f}s in~{in_tok} out~{out_tok} tok",
                  flush=True)
            save()
        else:
            tr["stopped"].pop(lang, None)
        save()
    return tr


# ---- embedding --------------------------------------------------------------

def embed_all(g, data: dict, tr: dict, out: Path) -> dict[str, np.ndarray]:
    path = out / "vectors.npz"
    if path.exists():
        z = np.load(path)
        return {k: z[k] for k in z.files}
    runs: set = set()
    t0 = time.monotonic()
    books = data["books"]
    tb = tr["books"]

    def chunks(texts: list[str]) -> np.ndarray:
        v = np.asarray(embeddings.embed_chunks(g, texts, runs=runs), dtype=np.float32)
        return v / np.linalg.norm(v, axis=1, keepdims=True)

    def queries(texts: list[str]) -> np.ndarray:
        v = np.asarray([embeddings.embed_query(g, t, runs=runs) for t in texts], dtype=np.float32)
        return v / np.linalg.norm(v, axis=1, keepdims=True)

    def tr_or_raw(b: dict, field: str) -> str:
        t = (tb.get(b["uid"]) or {}).get(field)
        return t if t else b[field]

    vec = {
        "distractors": chunks([d["doc"] for d in data["distractors"]]),
        "raw_doc": chunks([b["doc"] for b in books]),
        "tr_doc": chunks([tr_or_raw(b, "doc") for b in books]),
        "q1": queries([q1_text(b, tr) for b in books]),
        "raw_q2": queries([b["qpassage"] for b in books]),
        "tr_q2": queries([tr_or_raw(b, "qpassage") for b in books]),
    }
    np.savez(path, **vec)
    (out / "embed_meta.json").write_text(json.dumps(
        {"runs": sorted(r or "(none)" for r in runs), "seconds": round(time.monotonic() - t0, 1),
         "vectors": {k: list(v.shape) for k, v in vec.items()}}))
    return vec


def q1_text(b: dict, tr: dict) -> str:
    title = b["title"] if b["lang"] == "en" else (tr["titles"].get(b["uid"]) or b["title"])
    meta = "; ".join(b["meta"])
    return f"{title}. {meta}" if meta else title


# ---- scoring ----------------------------------------------------------------

def ranks(queries: np.ndarray, docs: np.ndarray) -> np.ndarray:
    """Rank (1-based) of doc i for query i, against every doc in the pool."""
    s = queries @ docs.T
    own = s[np.arange(len(queries)), np.arange(len(queries))]
    return (s > own[:, None]).sum(axis=1) + 1


def metrics(r: np.ndarray) -> dict:
    if len(r) == 0:
        return {}
    return {"n": int(len(r)), "r@1": float((r <= 1).mean()), "r@5": float((r <= 5).mean()),
            "r@10": float((r <= 10).mean()), "mrr": float((1.0 / r).mean())}


def score(data: dict, tr: dict, vec: dict, out: Path) -> dict:
    books = data["books"]
    ok = np.array([bool((tr["books"].get(b["uid"]) or {}).get("doc"))
                   and bool((tr["books"].get(b["uid"]) or {}).get("qpassage")) for b in books])
    # only books translated in full are scored, in BOTH conditions: same queries, same pool size
    idx = np.flatnonzero(ok)
    pool_raw = np.vstack([vec["raw_doc"][idx], vec["distractors"]])
    pool_tr = np.vstack([vec["tr_doc"][idx], vec["distractors"]])
    rk = {
        "q1_raw": ranks(vec["q1"][idx], pool_raw), "q1_tr": ranks(vec["q1"][idx], pool_tr),
        "q2_raw": ranks(vec["raw_q2"][idx], pool_raw), "q2_tr": ranks(vec["tr_q2"][idx], pool_tr),
    }
    langs = [books[i]["lang"] for i in idx]
    res: dict = {"pool": int(len(pool_raw)), "scored_books": int(len(idx)), "langs": {}, "groups": {}}
    groups = {"rare": RARE, "controls_non_en": [l for l in CONTROLS if l != "en"], "en": ["en"]}
    for name, sel in [(l, [l]) for l in RARE + CONTROLS] + list(groups.items()):
        m = np.array([l in sel for l in langs])
        if not m.any():
            continue
        row = {k: metrics(v[m]) for k, v in rk.items()}
        for q in ("q1", "q2"):
            a, b_ = rk[f"{q}_raw"][m], rk[f"{q}_tr"][m]
            row[f"{q}_paired"] = {"tr_better": int((b_ < a).sum()), "tr_worse": int((b_ > a).sum()),
                                  "tie": int((b_ == a).sum())}
        row["en_share_median"] = float(np.median([books[i]["en_share"] for i in idx[m]]))
        (res["langs"] if name in RARE + CONTROLS else res["groups"])[name] = row
    # the rare set restricted to passages that are NOT mostly English (bilingual
    # books — grammars, readers — carry English text the raw encoder reads anyway)
    m = np.array([l in RARE and books[i]["en_share"] < 0.15 for l, i in zip(langs, idx)])
    if m.any():
        res["groups"]["rare_non_english_text"] = {k: metrics(v[m]) for k, v in rk.items()}
    # WITHIN-LANGUAGE ranks: each query against only its own language's sampled
    # passages, no distractors. In the mixed pool a RAW passage is also findable
    # by its language alone (the query says "Frisian poetry", only 20 passages
    # are Frisian; a Frisian query passage finds Frisian text); translation
    # erases that cue. This view asks only "which of these books is it?".
    langs_arr = np.array(langs)
    within: dict = {}
    for lang in RARE + CONTROLS:
        sel = np.flatnonzero(langs_arr == lang)
        if len(sel) < 5:
            continue
        g_idx = idx[sel]
        within[lang] = {
            "q1_raw": metrics(ranks(vec["q1"][g_idx], vec["raw_doc"][g_idx])),
            "q1_tr": metrics(ranks(vec["q1"][g_idx], vec["tr_doc"][g_idx])),
            "q2_raw": metrics(ranks(vec["raw_q2"][g_idx], vec["raw_doc"][g_idx])),
            "q2_tr": metrics(ranks(vec["tr_q2"][g_idx], vec["tr_doc"][g_idx])),
        }
    res["within_language"] = within
    res["per_book"] = [{"uid": books[i]["uid"], "lang": books[i]["lang"],
                        "en_share": books[i]["en_share"],
                        **{k: int(v[j]) for k, v in rk.items()}} for j, i in enumerate(idx)]
    calls = [c for c in tr["calls"] if c["kind"] == "book"]
    cost: dict = {}
    for lang in RARE + CONTROLS:
        cs = [c for c in calls if c.get("lang") == lang]
        if cs:
            s = sum(c["s"] for c in cs)
            cost[lang] = {"books": len(cs), "seconds": round(s, 1),
                          "s_per_passage": round(s / (2 * len(cs)), 2),
                          "in_tok_per_s": round(sum(c["in_tok"] for c in cs) / s, 1),
                          "out_tok_per_s": round(sum(c["out_tok"] for c in cs) / s, 1),
                          "failed": sum(1 for c in cs if not c["ok"])}
    res["cost"] = cost
    res["stopped"] = tr["stopped"]
    res["skipped_short"] = data["skipped"]
    (out / "results.json").write_text(json.dumps(res, indent=1))
    return res


def examples(data: dict, tr: dict, out: Path, n: int = 2) -> None:
    ex = []
    for lang in RARE:
        for b in [b for b in data["books"] if b["lang"] == lang][:n]:
            t = (tr["books"].get(b["uid"]) or {}).get("doc") or ""
            ex.append({"lang": lang, "uid": b["uid"], "title": b["title"],
                       "title_en": tr["titles"].get(b["uid"]), "q1": q1_text(b, tr),
                       "en_share": b["en_share"], "source": b["doc"][:350], "translation": t[:350]})
    (out / "examples.json").write_text(json.dumps(ex, ensure_ascii=False, indent=1))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--SHARD", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--phase", default="all", choices=["sample", "translate", "embed", "all"])
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    dispatch.set_default_level(dispatch.Level.CORPUS)   # background work on a shared GPU
    g = collections_mod.by_name("gutenberg")
    if g is None:
        print("no gutenberg collection", file=sys.stderr)
        return 2
    data = sample(a.shard_tree, a.out)
    if a.phase == "sample":
        return 0
    tr = translate_all(data, a.out)
    examples(data, tr, a.out)
    if a.phase == "translate":
        return 0
    vec = embed_all(g, data, tr, a.out)
    res = score(data, tr, vec, a.out)
    print(json.dumps(res["groups"], indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
