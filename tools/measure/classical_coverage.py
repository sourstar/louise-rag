"""MEASUREMENT (not product code): for each of gutenberg's Latin (la) and Greek (el) books,
does a public-domain HUMAN English version of the same work already exist -- first in the
gutenberg collection itself, then on Project Gutenberg generally?
(docs/notes/classical-translation.md, "Existing translations".)

Input (OUT): catalog.jsonl (classical_shard.py `catalog`: one metadata line per record of
gutenberg's SHARD, read-only), pg_catalog.csv (gutenberg.org/cache/epub/feeds, the whole
of PG). Network: Wikidata (SPARQL and wbgetentities), read-only.

A candidate is an English record sharing an AUTHOR with the book: the same PG agent id
(role creator or dubious author), or the same normalised name / alias. The WORK is matched
by, in order of strength:
  wikidata   the book's PG id is an edition on Wikidata (P2034) of a work (P629) that has
             an English edition with a PG id; or the book and the candidate name the same
             Wikidata item through their "Wikipedia page about this book" links
  title      Latin-script or transliterated title words shared with the candidate's
             title (5-letter prefixes, stop words and volume words removed), score >= 0.6, the authors' own names removed
  llm        (optional, `judge` phase) a local LLM shown the book's title and the
             author's English titles picks the same work -- titles only, no text
Phases:  match -> coverage.json (+ judge_todo.json);  judge --model M -> judge_by_<M>.json,
then `match` again folds the judgements in.
Usage: python3 classical_coverage.py OUT match|judge [--model M] [--url LOCALAI]
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path

UA = "louise-measure/1.0 (classical-translation coverage; contact via Project Gutenberg mirror operator)"
AUTHOR_ROLES = {"creator", "dub", "aut"}
STOP = set("""the a an of and in on to with for from by its his her their being or as at
de et in ad cum sive seu sive ex e a ab per pro quae qui quod liber libri libro libros
opera omnia volume vol tome tomus tomos pars part parts book books first second third
selections selected from with notes introduction translated english latin greek edited
edition new complete works carmina historiarum
kai tou tes ton ta to o i oi hoi he ho ek eis peri tomos meros biblion
""".split())
GREEK = dict(zip("αβγδεζηθικλμνξοπρστυφχψωςάέήίόύώϊϋΐΰ",
                 ["a", "b", "g", "d", "e", "z", "e", "th", "i", "k", "l", "m", "n", "x", "o", "p",
                  "r", "s", "t", "y", "ph", "ch", "ps", "o", "s", "a", "e", "e", "i", "o", "y",
                  "o", "i", "y", "i", "y"]))


def fold(s: str) -> str:
    s = unicodedata.normalize("NFD", s.lower())
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return "".join(GREEK.get(c, c) for c in s)


def title_words(t: str, drop: set[str] = frozenset()) -> set[str]:
    t = fold(t.split(" : ")[0])
    return {w[:5] for w in re.findall(r"[a-z]{3,}", t) if w not in STOP and w[:5] not in drop}


def trigrams(t: str) -> set[str]:
    t = fold(t)
    return {t[i:i + 3] for i in range(len(t) - 2)}


def name_key(n: str) -> str:
    n = fold(n)
    n = re.sub(r"\(.*?\)|\d+\??|\b(bce|bc|ad|saint|st|of)\b", " ", n)
    return " ".join(sorted(re.findall(r"[a-z]{2,}", n)))


def get(url: str, data: bytes | None = None, tries: int = 4) -> dict:
    for k in range(tries):
        try:
            req = urllib.request.Request(url, data=data, headers={
                "User-Agent": UA, "Accept": "application/sparql-results+json, application/json"})
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.load(r)
        except Exception as e:                       # noqa: BLE001
            print("retry", k, url[:80], e, file=sys.stderr)
            time.sleep(5 * (k + 1))
    raise RuntimeError(url)


def sparql(q: str) -> list[dict]:
    data = urllib.parse.urlencode({"query": q, "format": "json"}).encode()
    j = get("https://query.wikidata.org/sparql", data)
    return [{k: v["value"] for k, v in b.items()} for b in j["results"]["bindings"]]


def qid(uri: str) -> str:
    return uri.rsplit("/", 1)[-1]


def wikidata_by_pg(pg_ids: list[str]) -> dict[str, dict]:
    """PG id -> {item, works: set, langs: set} for every PG id Wikidata knows."""
    out: dict[str, dict] = {}
    for i in range(0, len(pg_ids), 300):
        vals = " ".join(f'"{p}"' for p in pg_ids[i:i + 300])
        for b in sparql(f"""SELECT ?pg ?item ?work ?lang WHERE {{ VALUES ?pg {{ {vals} }}
              ?item wdt:P2034 ?pg . OPTIONAL {{ ?item wdt:P629 ?work }}
              OPTIONAL {{ ?item wdt:P407 ?lang }} }}"""):
            d = out.setdefault(b["pg"], {"items": set(), "works": set(), "langs": set()})
            d["items"].add(qid(b["item"]))
            if "work" in b:
                d["works"].add(qid(b["work"]))
            if "lang" in b:
                d["langs"].add(qid(b["lang"]))
        time.sleep(1)
    return out


def editions_of(works: list[str]) -> dict[str, set[str]]:
    """work QID -> PG ids of its editions/translations (P629 inverse), and of the work item."""
    out: dict[str, set[str]] = defaultdict(set)
    for i in range(0, len(works), 200):
        vals = " ".join(f"wd:{w}" for w in works[i:i + 200])
        for b in sparql(f"""SELECT ?w ?pg WHERE {{ VALUES ?w {{ {vals} }}
              {{ ?ed wdt:P629 ?w . ?ed wdt:P2034 ?pg }} UNION {{ ?w wdt:P2034 ?pg }} }}"""):
            out[qid(b["w"])].add(b["pg"])
        time.sleep(1)
    return out


def wiki_qids(urls: set[str]) -> dict[str, str]:
    """Wikipedia article URL -> Wikidata QID, through the query service's sitelinks
    (`?article schema:about ?item`), 150 articles per query."""
    iri: dict[str, str] = {}
    for u in urls:
        m = re.match(r"https?://([a-z\-]+)\.wikipedia\.org/wiki/([^#?\s]+)", u)
        if m:
            title = urllib.parse.unquote(m.group(2)).rstrip(".,;").replace(" ", "_")
            enc = urllib.parse.quote(title, safe="_(),'!*:-.;~@$&+=/")
            iri[f"https://{m.group(1)}.wikipedia.org/wiki/{enc}"] = u
    out: dict[str, str] = {}
    keys = sorted(iri)
    for i in range(0, len(keys), 150):
        vals = " ".join(f"<{k}>" for k in keys[i:i + 150] if ">" not in k and "<" not in k)
        for b in sparql(f"SELECT ?article ?item WHERE {{ VALUES ?article {{ {vals} }} "
                        f"?article schema:about ?item }}"):
            if b["article"] in iri:
                out[iri[b["article"]]] = qid(b["item"])
        time.sleep(2)
    return out


def authors(rec: dict) -> tuple[set[str], set[str]]:
    ids, names = set(), set()
    for c in rec.get("detail") or []:
        if c.get("role") in AUTHOR_ROLES:
            if c.get("agent_id"):
                ids.add(c["agent_id"])
            for n in [c.get("name") or ""] + list(c.get("aliases") or []):
                if name_key(n):
                    names.add(name_key(n))
    return ids, names


def phase_match(out: Path) -> None:
    cat = [json.loads(l) for l in (out / "catalog.jsonl").open(encoding="utf-8")]
    books = [r for r in cat if r["lang"] in ("la", "el")]
    english = [r for r in cat if r["lang"] == "en" or "en" in (r.get("languages") or [])]
    in_coll = {r["uid"].split(":", 1)[1] for r in cat}
    by_agent, by_name = defaultdict(list), defaultdict(list)
    for r in english:
        ids, names = authors(r)
        for i in ids:
            by_agent[i].append(r)
        for n in names:
            by_name[n].append(r)

    def candidates(b: dict) -> list[dict]:
        ids, names = authors(b)
        seen, out_ = set(), []
        for r in [x for i in ids for x in by_agent[i]] + [x for n in names for x in by_name[n]]:
            if r["uid"] != b["uid"] and r["uid"] not in seen:
                seen.add(r["uid"])
                out_.append(r)
        return out_

    cands = {b["uid"]: candidates(b) for b in books}
    # Wikidata, by PG id: the books and every candidate
    cache = out / "wikidata_cache.json"
    wd = json.loads(cache.read_text()) if cache.exists() else {}
    if "by_pg" not in wd:
        pgs = sorted({b["uid"].split(":")[1] for b in books} |
                     {c["uid"].split(":")[1] for cs in cands.values() for c in cs})
        wd["by_pg"] = {k: {kk: sorted(vv) for kk, vv in v.items()} for k, v in wikidata_by_pg(pgs).items()}
        cache.write_text(json.dumps(wd))
    if "wiki" not in wd:
        urls = {u for b in books for u in b["wiki"]} | {u for cs in cands.values() for c in cs for u in c["wiki"]}
        wd["wiki"] = wiki_qids(urls)
        cache.write_text(json.dumps(wd))
    if "editions" not in wd:
        works = set()
        for b in books:
            d = wd["by_pg"].get(b["uid"].split(":")[1]) or {}
            works |= set(d.get("works") or []) | set(d.get("items") or [])
            works |= {wd["wiki"][u] for u in b["wiki"] if u in wd["wiki"]}
        wd["editions"] = {k: sorted(v) for k, v in editions_of(sorted(works)).items()}
        cache.write_text(json.dumps(wd))
    pg_csv = {r["Text#"]: r for r in csv.DictReader((out / "pg_catalog.csv").open(encoding="utf-8"))}
    en_pg = {k for k, r in pg_csv.items() if "en" in r["Language"].split("; ")}
    judged = {}
    for p in out.glob("judge_by_*.json"):
        judged.update(json.loads(p.read_text()))

    res, todo = [], []
    for b in books:
        pg = b["uid"].split(":")[1]
        d = wd["by_pg"].get(pg) or {}
        works = set(d.get("works") or []) | set(d.get("items") or []) | \
            {wd["wiki"][u] for u in b["wiki"] if u in wd["wiki"]}
        wd_pgs = {x for w in works for x in wd["editions"].get(w, []) if x != pg and x in en_pg}
        matches = []
        for c in cands[b["uid"]]:
            cpg = c["uid"].split(":")[1]
            why = []
            if cpg in wd_pgs:
                why.append("wikidata-edition")
            cq = {wd["wiki"][u] for u in c["wiki"] if u in wd["wiki"]}
            if cq & works:
                why.append("wikidata-wikipedia")
            drop = {w[:5] for n in authors(b)[1] | authors(c)[1] for w in n.split()}
            tw, cw = title_words(b["title"], drop), title_words(c["title"], drop)
            score = len(tw & cw) / len(tw) if tw else 0.0
            if score >= 0.6:
                why.append(f"title {score:.2f}")
            j = judged.get(b["uid"])
            if j and cpg in j.get("picked", []):
                why.append("llm")
            if why:
                matches.append({"uid": c["uid"], "title": c["title"][:90], "why": why})
        # English editions Wikidata knows of that are NOT in the collection (PG generally)
        outside = sorted(x for x in wd_pgs if x not in in_coll)
        strong = [m for m in matches if any(w.startswith("wikidata") for w in m["why"])]
        conf = "high" if strong else ("medium" if matches else ("outside" if outside else "none"))
        res.append({"uid": b["uid"], "lang": b["lang"], "title": b["title"][:90],
                    "authors": sorted(authors(b)[1])[:2], "est_tokens": b["est_tokens"],
                    "n_candidates": len(cands[b["uid"]]), "matches": matches,
                    "outside_collection_pg": outside, "confidence": conf,
                    "covered": bool(matches) or bool(outside)})
        if cands[b["uid"]] and not [m for m in matches if m["why"] != ["llm"]]:
            tg = trigrams(b["title"])
            ranked = sorted(cands[b["uid"]], key=lambda c: -len(tg & trigrams(c["title"])))
            todo.append({"uid": b["uid"], "lang": b["lang"], "title": b["title"],
                         "authors": sorted(authors(b)[1])[:2],
                         "candidates": [{"pg": c["uid"].split(":")[1], "title": c["title"][:120]}
                                        for c in ranked][:150]})
    summary = {}
    for lang in ("la", "el"):
        xs = [r for r in res if r["lang"] == lang]
        cov = [r for r in xs if r["covered"]]
        summary[lang] = {"books": len(xs), "covered_books": len(cov),
                         "tokens": sum(r["est_tokens"] for r in xs),
                         "covered_tokens": sum(r["est_tokens"] for r in cov),
                         "by_confidence": {c: sum(1 for r in xs if r["confidence"] == c)
                                           for c in ("high", "medium", "outside", "none")},
                         "with_author_candidates": sum(1 for r in xs if r["n_candidates"])}
    (out / "coverage.json").write_text(json.dumps({"summary": summary, "books": res},
                                                  ensure_ascii=False, indent=1))
    (out / "judge_todo.json").write_text(json.dumps(todo, ensure_ascii=False, indent=1))
    print(json.dumps(summary, indent=1))
    print("judge todo", len(todo))


def phase_judge(out: Path, model: str, url: str) -> None:
    todo = json.loads((out / "judge_todo.json").read_text())
    path = out / f"judge_by_{model}.json"
    done = json.loads(path.read_text()) if path.exists() else {}
    for t in todo:
        if t["uid"] in done:
            continue
        lst = "\n".join(f"{c['pg']}: {c['title']}" for c in t["candidates"])
        prompt = (f"A book in {'Latin' if t['lang'] == 'la' else 'Greek'}: \"{t['title']}\" "
                  f"(author: {', '.join(t['authors'])}).\nThese English books by the same author "
                  f"are listed with their ids:\n{lst}\n\nWhich of them, if any, is the same work "
                  f"(a translation of it, or its original)? A part or a selection of the same work "
                  f"counts. Answer with the matching ids only, separated by commas, or NONE. "
                  f"No explanation.")
        body = json.dumps({"model": model, "messages": [{"role": "user", "content": prompt}],
                           "temperature": 0, "top_k": 1, "seed": 0, "max_tokens": 120}).encode()
        ans = None
        for _ in range(10):
            while (out / "HOLD").exists():        # classical_guard.py pre-empted the model
                time.sleep(2)
            req = urllib.request.Request(f"{url}/v1/chat/completions", data=body,
                                         headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=600) as r:
                    ans = json.load(r)["choices"][0]["message"]["content"].strip()
                break
            except Exception as e:                # noqa: BLE001 -- pre-empted mid-request
                print("retry", t["uid"], e, flush=True)
                time.sleep(10)
        if ans is None:
            continue
        ids = {c["pg"] for c in t["candidates"]}
        done[t["uid"]] = {"answer": ans, "picked": [x for x in re.findall(r"\d+", ans) if x in ids]}
        path.write_text(json.dumps(done, ensure_ascii=False, indent=1))
        print(t["uid"], t["title"][:50], "->", ans, flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("out", type=Path)
    ap.add_argument("phase", choices=["match", "judge"])
    ap.add_argument("--model", default="qwen3-30b-a3b")
    ap.add_argument("--url", default="http://127.0.0.1:30286")
    a = ap.parse_args()
    if a.phase == "match":
        phase_match(a.out)
    else:
        phase_judge(a.out, a.model, a.url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
