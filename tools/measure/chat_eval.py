"""Which chat model should answer Ask — measured on Ask's own loop (operator,
2026-11-03: "Consider replacing the chat model with nemo equivalent";
"Evaluate both A and B" — A Nemotron-3.5-Lightning-30B-A3B, B
Nemotron-3.5-Bolt-E4B; the incumbent Qwen3-8B is the reference row).

    python -m tools.measure.chat_eval run    --out DIR --name lightning --model <chat yaml name> \\
        [--queries tools/measure/one_language_queries.tsv] [--per-lang 10] [--collections gutenberg]
    python -m tools.measure.chat_eval score  --out DIR
    python -m tools.measure.chat_eval sample --out DIR --runs qwen3-8b,lightning,bolt-e4b [--n 30]

WHAT RUNS. Ask's tool loop exactly as the node serves it: `app.ask.chat.ask`,
whose model is `llm.chat_tools` at its production settings, with
`config.CHAT_MODEL` set to the candidate for the run and the node's own MCP
tools (`louise_search`, `louise_lookup`, `louise_traverse`). Run it inside the
node's image, at night (operator), one model at a time.

THE QUESTIONS are the 451 labelled queries of one-language-per-work.md, in 18
languages. Six are Lightning's (en es fr de it ja); the others (el fi nl la
eo hu no sv pl pt ru zh tl) show what happens outside them.
`--per-lang` takes the first N of each language: a deterministic subset.

WHAT IS SCORED, DETERMINISTICALLY (no LLM judge, as the legal pilot):
- tool use: calls per answer, calls that failed (unknown tool, refused
  arguments), loops that hit the round bound;
- the answer's language: `app.query_language.guess` (the node's own
  deterministic guess), set against the query's label. "No guess" is
  counted apart, never as right;
- time per answer.
Quality beyond that is read, not scored: `sample` writes the same questions
with each model's answer under a blind letter, and the key in a separate
file.
"""
from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
import time
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path
from typing import Any

DEFAULT_QUERIES = Path(__file__).with_name("one_language_queries.tsv")
LIGHTNING_LANGS = frozenset({"en", "es", "fr", "de", "it", "ja"})


def read_queries(path: Path, per_lang: int | None = None) -> list[dict[str, str]]:
    rows, seen = [], defaultdict(int)
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split("\t")
        if len(parts) < 3 or not parts[2].strip():
            continue
        qid, lang, text = parts[0], parts[1], parts[2].strip()
        if per_lang is not None and seen[lang] >= per_lang:
            continue
        seen[lang] += 1
        rows.append({"key": f"{qid}:{lang}", "qid": qid, "lang": lang, "query": text})
    return rows


def answer_language(text: str) -> str | None:
    from app import query_language
    return query_language.guess(text or "", set(query_language.word_lists()))


def run(out: Path, name: str, *, model: str, queries: list[dict[str, str]],
        collections: list[str] | None = None,
        ask: Callable[..., dict[str, Any]] | None = None,
        guess: Callable[[str], str | None] = answer_language) -> int:
    run_dir = out / "runs" / name
    run_dir.mkdir(parents=True, exist_ok=True)
    settings = {"model": model, "collections": collections or []}
    stated = run_dir / "settings.json"
    if stated.exists():
        if json.loads(stated.read_text(encoding="utf-8")) != settings:
            raise SystemExit(f"{name}: settings differ from this run's first ones ({stated})")
    else:
        stated.write_text(json.dumps(settings, indent=1, sort_keys=True), encoding="utf-8")
    answers = run_dir / "answers.jsonl"
    done = ({json.loads(l)["key"] for l in answers.read_text(encoding="utf-8").splitlines() if l}
            if answers.exists() else set())
    if ask is None:
        from app import config
        from app.ask import chat
        config.CHAT_MODEL = model          # this process asks the candidate only
        ask = chat.ask
    for q in queries:
        if q["key"] in done:
            continue
        t0 = time.monotonic()
        error = None
        try:
            got = ask(q["query"], collections=collections)
        except Exception as e:   # noqa: BLE001 — a failed answer is a row, not the end of the run
            got, error = {"answer": "", "turns": [], "rounds": 0, "exhausted": False}, \
                f"{type(e).__name__}: {e}"[:300]
        row = {**q, "answer": got.get("answer") or "", "rounds": got.get("rounds", 0),
               "exhausted": bool(got.get("exhausted")),
               "tools": [t.get("tool") for t in got.get("turns", [])],
               "tool_errors": sum(1 for t in got.get("turns", []) if t.get("error")),
               "seconds": round(time.monotonic() - t0, 2), "error": error}
        row["answer_lang"] = guess(row["answer"])
        with answers.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"{name}: {len(queries) - len(done)} asked now, {len(queries)} in all")
    return 0


def _rows(out: Path, name: str) -> list[dict[str, Any]]:
    path = out / "runs" / name / "answers.jsonl"
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l]


def summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(rows)
    if not n:
        return {"n": 0}
    calls = sum(len(r["tools"]) for r in rows)
    guessed = [r for r in rows if r["answer_lang"] is not None]
    return {"n": n,
            "failed": sum(1 for r in rows if r["error"]),
            "exhausted": sum(1 for r in rows if r["exhausted"]),
            "calls_per_answer": calls / n,
            "tool_errors": sum(r["tool_errors"] for r in rows),
            "lang_right": sum(1 for r in guessed if r["answer_lang"] == r["lang"]),
            "lang_guessed": len(guessed),
            "median_s": statistics.median(r["seconds"] for r in rows)}


def score(out: Path) -> int:
    names = sorted(p.name for p in (out / "runs").iterdir() if (p / "answers.jsonl").exists())
    print("| model | languages | answers | failed | bound hit | calls/answer | tool errors "
          "| answer in the query's language (of those guessed) | median s |")
    print("|---|---|---|---|---|---|---|---|---|")
    for name in names:
        rows = _rows(out, name)
        groups = {"all": rows,
                  "Lightning's six": [r for r in rows if r["lang"] in LIGHTNING_LANGS],
                  "the others": [r for r in rows if r["lang"] not in LIGHTNING_LANGS]}
        for label, sub in groups.items():
            s = summary(sub)
            if not s["n"]:
                continue
            share = s["lang_right"] / s["lang_guessed"] if s["lang_guessed"] else 0.0
            print(f"| {name} | {label} | {s['n']} | {s['failed']} | {s['exhausted']} | "
                  f"{s['calls_per_answer']:.2f} | {s['tool_errors']} | "
                  f"**{share:.2f}** ({s['lang_right']}/{s['lang_guessed']}) | {s['median_s']:.1f} |")
    return 0


def sample(out: Path, runs: list[str], n: int = 30, seed: int = 0) -> int:
    """The same questions, each model's answer under a blind letter; the key
    apart, so the reading is done before the names are known."""
    by_run = {r: {row["key"]: row for row in _rows(out, r)} for r in runs}
    common = sorted(set.intersection(*(set(v) for v in by_run.values())))
    rng = random.Random(seed)
    keys = rng.sample(common, min(n, len(common)))
    lines, key_lines = ["# Blind sample: the same question, one answer per model\n"], []
    for i, k in enumerate(keys, 1):
        q = by_run[runs[0]][k]
        order = runs[:]
        rng.shuffle(order)
        lines.append(f"## {i}. [{q['lang']}] {q['query']}\n")
        for letter, r in zip("ABCDEFG", order):
            lines.append(f"**{letter}**\n\n{by_run[r][k]['answer'].strip() or '(no answer)'}\n")
        key_lines.append(f"{i}: " + ", ".join(f"{l}={r}" for l, r in zip("ABCDEFG", order)))
    (out / "sample.md").write_text("\n".join(lines), encoding="utf-8")
    (out / "sample-key.txt").write_text("\n".join(key_lines) + "\n", encoding="utf-8")
    print(f"{len(keys)} questions in {out / 'sample.md'}; the key in {out / 'sample-key.txt'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--queries", type=Path, default=DEFAULT_QUERIES)
    p.add_argument("--per-lang", type=int, default=None)
    p.add_argument("--collections", default="")
    p = sub.add_parser("score")
    p.add_argument("--out", type=Path, required=True)
    p = sub.add_parser("sample")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--runs", required=True)
    p.add_argument("--n", type=int, default=30)
    a = ap.parse_args(argv)
    if a.cmd == "run":
        cols = [c for c in a.collections.split(",") if c] or None
        return run(a.out, a.name, model=a.model, queries=read_queries(a.queries, a.per_lang),
                   collections=cols)
    if a.cmd == "score":
        return score(a.out)
    return sample(a.out, [r for r in a.runs.split(",") if r], a.n)


if __name__ == "__main__":
    sys.exit(main())
