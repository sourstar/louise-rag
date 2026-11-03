"""Which model writes wiki-gutenberg's summaries, and from which text — measured
for language accuracy and speed (operator, 2026-11-03: "Models for
summarization compare the performance in terms of language accuracy and speed
for A, B and C. Consider also this solution: 1. always use a book english
translation when available. 2. Select a model for summaries from english and
another model for summaries from non-english"; A Qwen3.8-27B, B
Qwen3.6-35B-A3B, C Gemma 4 31B — wiki-gutenberg-amendments.md).

    python -m tools.measure.summary_eval fetch --out DIR [--per-lang 20] [--booksum 30] \\
        [--classical /root/xlate-classical/out/evalset.json]
    python -m tools.measure.summary_eval run   --out DIR --name qwen3.8-27b \\
        --url http://nas:30437 --model louise-sum-qwen3.8-27b [--limit 5] \\
        [--extra '{"chat_template_kwargs": {"enable_thinking": false}}']
    python -m tools.measure.summary_eval score --out DIR

THE SETS, NOT OURS ("do not reinvent the wheel"):
- WikiLingua (Ladhak et al., Findings of EMNLP 2020; CC BY 3.0 as published
  on the Hub): WikiHow sections in 17 languages, each linked to the English
  section it translates, which carries an English reference summary. Taken:
  the languages gutenberg holds most of — fr de nl it es pt ru zh ja.
  ONE ITEM TESTS BOTH HALVES OF THE PROPOSAL: the model summarises the
  original (what a model "for non-English" does) and the English version
  (what "use the English translation" does), each scored against the same
  English reference.
- BookSum (Kryściński et al., 2021; BSD-3-Clause, research use): chapters of
  Project Gutenberg books with human summaries — English literary text, the
  domain itself.
- Latin and Greek have no published summary set: the node's own reference
  passages (classical_evalset.py: Latin or Greek with a human English
  translation, aligned by explicit numbers) are summarised both ways and the
  two summaries compared.

THE TASK is one prompt for every model and every text: summarise in English
in at most SUMMARY_WORDS words. Summaries are written in English whatever the
source (the claim's text is one language; Louise translates for the reader).
The prompt's sha256 is recorded with the run.

WHAT IS SCORED, DETERMINISTICALLY (no LLM judge):
- language accuracy:
  - `english`: the summary is in English, by the node's own guess
    (app.query_language);
  - chrF (Popović 2015, sacrebleu) of the summary against the English
    reference: `from original` and `from English`, and their gap, which is
    what reading the original costs;
  - `agree`: chrF between the two summaries of one item, the only score the
    classical passages have.
- speed: seconds per summary, prompt and completion tokens from the answer's
  `usage`, generated tokens per second. Warm answers only: one throwaway
  request before the first timed one, at concurrency 1.
- failures: no answer, thinking in the content (stripped and counted).

Resumable: answers append one at a time; other settings under the same name
are refused.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import random
import re
import statistics
import sys
import time
from collections import defaultdict
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

WIKILINGUA = "https://huggingface.co/datasets/esdurmus/wiki_lingua/resolve/main/{config}/train-00000-of-00001.parquet"
BOOKSUM = "https://huggingface.co/datasets/kmfoda/booksum/resolve/main/test.csv"
LANGS = {"fr": "french", "de": "german", "nl": "dutch", "it": "italian", "es": "spanish",
         "pt": "portuguese", "ru": "russian", "zh": "chinese", "ja": "japanese"}
CLASSICAL = ("la", "el", "grc")
LICENCE = ("WikiLingua (Ladhak, Durmus, Cardie, McKeown; Findings of EMNLP 2020), "
           "https://huggingface.co/datasets/esdurmus/wiki_lingua, CC BY 3.0 as published there.\n"
           "BookSum (Kryściński, Rajani, Agarwal, Xiong, Radev; 2021), "
           "https://huggingface.co/datasets/kmfoda/booksum, BSD-3-Clause; its legal note limits "
           "use to research.\n")
#: a section or chapter long enough to summarise and short enough for one
#: request at a 16k context with the answer
TEXT_CHARS = (1500, 8000)
BOOKSUM_CHARS = (3000, 24000)
SUMMARY_WORDS = 120
MAX_TOKENS = 400
SYSTEM = "You summarise texts faithfully. Write only the summary, in plain prose."
_THINK = re.compile(r"<think>.*?</think>\s*", re.DOTALL)


def prompt_for(text: str) -> str:
    return (f"Summarise the text below in English, in at most {SUMMARY_WORDS} words. "
            f"Write only the summary.\n\n{text}")


def prompt_hash() -> str:
    return hashlib.sha256((SYSTEM + "\n" + prompt_for("{text}")).encode("utf-8")).hexdigest()


def _sections(article: Any) -> list[dict[str, Any]]:
    """WikiLingua's `article`: a sequence of sections, which a Parquet
    reader hands back either as a list of dicts or as a dict of lists."""
    if isinstance(article, dict):
        keys = list(article)
        return [dict(zip(keys, vals)) for vals in zip(*(article[k] for k in keys))]
    return list(article or [])


def wikilingua_items(lang: str, rows: list[dict], english: list[dict], per_lang: int,
                     seed: int = 0) -> list[dict[str, Any]]:
    """`per_lang` sections in `lang` whose English section is found, both
    within TEXT_CHARS, drawn with a fixed seed."""
    en: dict[tuple[str, str], dict] = {}
    for row in english:
        for s in _sections(row.get("article")):
            en[(row["url"], s.get("section_name") or "")] = s
    pool = []
    for row in rows:
        for s in _sections(row.get("article")):
            twin = en.get((s.get("english_url") or "", s.get("english_section_name") or ""))
            if not twin or not twin.get("summary"):
                continue
            doc, en_doc = s.get("document") or "", twin.get("document") or ""
            if all(TEXT_CHARS[0] <= len(t) <= TEXT_CHARS[1] for t in (doc, en_doc)):
                pool.append((row["url"], s.get("section_name") or "", doc, en_doc, twin["summary"]))
    pool.sort()
    picked = random.Random(f"{seed}:{lang}").sample(pool, min(per_lang, len(pool)))
    return [{"id": f"wl-{lang}-{k:02d}", "set": "wikilingua", "lang": lang, "text": doc,
             "pair_text": en_doc, "reference": ref}
            for k, (_u, _s, doc, en_doc, ref) in enumerate(sorted(picked))]


def booksum_items(rows: list[dict], n: int, seed: int = 0) -> list[dict[str, Any]]:
    pool = sorted({(r.get("chapter") or "", r.get("summary_text") or "") for r in rows
                   if BOOKSUM_CHARS[0] <= len(r.get("chapter") or "") <= BOOKSUM_CHARS[1]
                   and r.get("summary_text")})
    picked = random.Random(f"{seed}:booksum").sample(pool, min(n, len(pool)))
    return [{"id": f"bs-{k:02d}", "set": "booksum", "lang": "en", "text": ch, "reference": ref}
            for k, (ch, ref) in enumerate(sorted(picked))]


def classical_items(evalset: list[dict]) -> list[dict[str, Any]]:
    return [{"id": f"cl-{e['id']}", "set": "classical", "lang": e["lang"], "text": e["src"],
             "pair_text": e["ref"]} for e in evalset if e.get("lang") in CLASSICAL]


def _parquet_rows(path: Path) -> list[dict]:
    import pyarrow.parquet as pq
    return pq.read_table(path).to_pylist()


def _csv_rows(path: Path) -> list[dict]:
    csv.field_size_limit(sys.maxsize)
    with path.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def fetch(out: Path, *, langs: Iterable[str] = tuple(LANGS), per_lang: int = 20, booksum: int = 30,
          classical: Path | None = None,
          download: Callable[[str, Path], None] | None = None,
          parquet_rows: Callable[[Path], list[dict]] = _parquet_rows,
          csv_rows: Callable[[Path], list[dict]] = _csv_rows) -> int:
    from tools.encoder_bakeoff import _download, _sha256
    download = download or _download
    out.mkdir(parents=True, exist_ok=True)
    (out / "LICENCE.txt").write_text(LICENCE, encoding="utf-8")
    files: dict[str, Path] = {}
    for config in ["english", *(LANGS[l] for l in langs)]:
        target = out / f"wikilingua-{config}.parquet"
        if not target.exists():
            download(WIKILINGUA.format(config=config), target)
        files[config] = target
    english = parquet_rows(files["english"])
    items = []
    for lang in langs:
        items += wikilingua_items(lang, parquet_rows(files[LANGS[lang]]), english, per_lang)
    if booksum:
        target = out / "booksum-test.csv"
        if not target.exists():
            download(BOOKSUM, target)
        files["booksum"] = target
        items += booksum_items(csv_rows(target), booksum)
    if classical is not None:
        items += classical_items(json.loads(classical.read_text(encoding="utf-8")))
    with (out / "items.jsonl").open("w", encoding="utf-8") as fh:
        for it in items:
            fh.write(json.dumps(it, ensure_ascii=False) + "\n")
    sums = {p.name: _sha256(p) for p in files.values()}
    (out / "SHA256SUMS.json").write_text(json.dumps(sums, indent=1, sort_keys=True), encoding="utf-8")
    by = defaultdict(int)
    for it in items:
        by[f"{it['set']}:{it['lang']}"] += 1
    print(f"{len(items)} items: " + ", ".join(f"{k} {v}" for k, v in sorted(by.items())))
    return 0


def items_of(out: Path) -> list[dict[str, Any]]:
    return [json.loads(l) for l in (out / "items.jsonl").read_text(encoding="utf-8").splitlines() if l]


def jobs_of(items: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """(key, text) to summarise: each item's text, and its English version
    under `<id>:en` where it has one."""
    jobs = []
    for it in items:
        jobs.append((it["id"], it["text"]))
        if it.get("pair_text"):
            jobs.append((it["id"] + ":en", it["pair_text"]))
    return jobs


def chat_http(url: str, model: str, extra: dict[str, Any], timeout: float = 900.0
              ) -> Callable[[str], dict[str, Any]]:
    import httpx
    client = httpx.Client(timeout=timeout)

    def ask(prompt: str) -> dict[str, Any]:
        body = {"model": model, "temperature": 0, "seed": 0, "max_tokens": MAX_TOKENS,
                "messages": [{"role": "system", "content": SYSTEM},
                             {"role": "user", "content": prompt}], **extra}
        r = client.post(url.rstrip("/") + "/v1/chat/completions", json=body)
        r.raise_for_status()
        data = r.json()
        return {"content": data["choices"][0]["message"].get("content") or "",
                "usage": data.get("usage") or {}}
    return ask


def run(out: Path, name: str, *, model: str, url: str, extra: dict[str, Any] | None = None,
        limit: int | None = None, ask: Callable[[str], dict[str, Any]] | None = None,
        clock: Callable[[], float] = time.monotonic) -> int:
    items = items_of(out)
    if limit:
        per: dict[str, int] = defaultdict(int)
        kept = []
        for it in items:
            key = f"{it['set']}:{it['lang']}"
            if per[key] < limit:
                per[key] += 1
                kept.append(it)
        items = kept
    run_dir = out / "runs" / name
    run_dir.mkdir(parents=True, exist_ok=True)
    settings = {"model": model, "url": url, "extra": extra or {}, "temperature": 0, "seed": 0,
                "max_tokens": MAX_TOKENS, "summary_words": SUMMARY_WORDS,
                "prompt_sha256": prompt_hash()}
    stated = run_dir / "settings.json"
    if stated.exists():
        if json.loads(stated.read_text(encoding="utf-8")) != settings:
            raise SystemExit(f"{name}: settings differ from this run's first ones ({stated}); "
                             f"use another --name")
    else:
        stated.write_text(json.dumps(settings, indent=1, sort_keys=True), encoding="utf-8")
    answers = run_dir / "answers.jsonl"
    done = ({json.loads(l)["key"] for l in answers.read_text(encoding="utf-8").splitlines() if l}
            if answers.exists() else set())
    todo = [(k, t) for k, t in jobs_of(items) if k not in done]
    ask = ask or chat_http(url, model, extra or {})
    if todo:
        ask(prompt_for("Warm-up. Reply with one word."))       # not timed: the load is paid here
    for key, text in todo:
        t0 = clock()
        error = None
        try:
            got = ask(prompt_for(text))
        except Exception as e:   # noqa: BLE001 — a failed summary is a row, not the end of the run
            got, error = {"content": "", "usage": {}}, f"{type(e).__name__}: {e}"[:300]
        seconds = round(clock() - t0, 3)
        raw = got.get("content") or ""
        usage = got.get("usage") or {}
        row = {"key": key, "summary": _THINK.sub("", raw).strip(), "thinking": bool(_THINK.search(raw)),
               "seconds": seconds, "prompt_tokens": usage.get("prompt_tokens"),
               "completion_tokens": usage.get("completion_tokens"), "error": error}
        with answers.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"{name}: {len(todo)} summaries now, {len(done) + len(todo)} in all")
    return 0


def _chrf(hyp: str, ref: str) -> float:
    import sacrebleu
    return sacrebleu.sentence_chrf(hyp, [ref]).score


def _english(text: str) -> bool:
    from app import query_language
    return query_language.guess(text, query_language.guessable()) == "en"


def _mean(xs: list[float]) -> str:
    return "-" if not xs else f"{statistics.fmean(xs):.1f}"


def score_run(out: Path, name: str, *, chrf: Callable[[str, str], float] = _chrf,
              english: Callable[[str], bool] = _english) -> dict[tuple[str, str], dict[str, Any]]:
    items = {it["id"]: it for it in items_of(out)}
    rows = {r["key"]: r for r in (json.loads(l) for l in (out / "runs" / name / "answers.jsonl")
                                  .read_text(encoding="utf-8").splitlines() if l)}
    groups: dict[tuple[str, str], dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for iid, it in items.items():
        r = rows.get(iid)
        if r is None:
            continue
        g = groups[(it["set"], it["lang"])]
        g["n"].append(1)
        g["failed"].append(int(bool(r["error"]) or not r["summary"]))
        g["thinking"].append(int(r["thinking"]))
        g["english"].append(int(bool(r["summary"]) and english(r["summary"])))
        en = rows.get(iid + ":en")
        if it.get("reference"):
            g["chrf_orig"].append(chrf(r["summary"], it["reference"]))
            if en is not None:
                g["chrf_en"].append(chrf(en["summary"], it["reference"]))
        if en is not None:
            g["agree"].append(chrf(r["summary"], en["summary"]))
        for row in filter(None, (r, en)):
            if not row["error"]:
                g["seconds"].append(row["seconds"])
                if row.get("completion_tokens"):
                    g["gen_tokens"].append(row["completion_tokens"])
                    g["gen_seconds"].append(row["seconds"])
    out_rows = {}
    for key, g in groups.items():
        out_rows[key] = {
            "n": len(g["n"]), "failed": sum(g["failed"]), "thinking": sum(g["thinking"]),
            "english": sum(g["english"]) / len(g["n"]),
            "chrf_orig": g["chrf_orig"], "chrf_en": g["chrf_en"], "agree": g["agree"],
            "median_s": statistics.median(g["seconds"]) if g["seconds"] else None,
            "tok_s": (sum(g["gen_tokens"]) / sum(g["gen_seconds"])) if g["gen_seconds"] else None}
    return out_rows


def score(out: Path, **kw: Any) -> int:
    names = sorted(p.name for p in (out / "runs").iterdir() if (p / "answers.jsonl").exists())
    print("| model | set | language | items | in English | chrF from original | chrF from English "
          "| gap | the two summaries agree (chrF) | failed | thinking | median s | gen tok/s |")
    print("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for name in names:
        for (sset, lang), s in sorted(score_run(out, name, **kw).items()):
            gap = (statistics.fmean(s["chrf_en"]) - statistics.fmean(s["chrf_orig"])
                   if s["chrf_en"] and s["chrf_orig"] else None)
            cells = [name, sset, lang, str(s["n"]), f"{s['english']:.2f}", _mean(s["chrf_orig"]),
                     _mean(s["chrf_en"]), "-" if gap is None else f"{gap:+.1f}", _mean(s["agree"]),
                     str(s["failed"]), str(s["thinking"]),
                     "-" if s["median_s"] is None else f"{s['median_s']:.1f}",
                     "-" if s["tok_s"] is None else f"{s['tok_s']:.1f}"]
            print("| " + " | ".join(cells) + " |")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("fetch")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--langs", default=",".join(LANGS))
    p.add_argument("--per-lang", type=int, default=20)
    p.add_argument("--booksum", type=int, default=30)
    p.add_argument("--classical", type=Path, default=None)
    p = sub.add_parser("run")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--url", required=True)
    p.add_argument("--limit", type=int, default=None, help="items per set and language")
    p.add_argument("--extra", default="{}")
    p = sub.add_parser("score")
    p.add_argument("--out", type=Path, required=True)
    a = ap.parse_args(argv)
    if a.cmd == "fetch":
        return fetch(a.out, langs=[l for l in a.langs.split(",") if l], per_lang=a.per_lang,
                     booksum=a.booksum, classical=a.classical)
    if a.cmd == "run":
        return run(a.out, a.name, model=a.model, url=a.url, extra=json.loads(a.extra), limit=a.limit)
    return score(a.out)


if __name__ == "__main__":
    sys.exit(main())
