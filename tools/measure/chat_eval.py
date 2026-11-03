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
languages. THE OPERATOR'S FIVE decide (2026-11-03: "As long as it answers
correctly for english, italian, german, french and spanish I am good with it.
Other users can use another chat model to fit their needs"): en it de fr es;
the others show what happens outside them. `--per-lang` takes the first N of
each language, `--langs` keeps only those named: a deterministic subset.

WHAT IS SCORED, DETERMINISTICALLY (no LLM judge, as the legal pilot):
- tool use: calls per answer, calls that failed (unknown tool, refused
  arguments), loops that hit the round bound;
- the answer's language: `app.query_language.guess` (the node's own
  deterministic guess), set against the query's label. "No guess" is
  counted apart, never as right;
- time per answer;
- PRECISION AND TOOLS (operator, 2026-11-03: "fast, not too big, precise in
  articulating the answer and able to use tools"): the sources an answer
  cites in brackets ([gutenberg:2701]), the share of them found in what its
  tools returned ("grounded"; a cited uid no tool returned is invented), and
  the answers given without any tool call.
Quality beyond that is read, not scored: `sample` writes the same questions
with each model's answer under a blind letter, and the key in a separate
file.

THE BENCHMARK OF THE OPTIMIZATIONS (operator, 2026-11-03: "Use the chat
comparison benchmark to work on the optimizations, suspend the test anytime
to implement improvements"). Every answer records where its time went: the
model's rounds (`model_s`), the tool calls (`tools_s`), and inside the
searches the node's own timings — rerank, the language plan, the dense and
word legs. A run is stopped at any time (each answer is written as it ends
and a run resumes where it stopped), the improvement deployed, and the run
resumed with another `--tag`: `score` gives each tag its own rows, so the
same questions before and after are read side by side. The tag is not a
setting: one run's answers may carry several.
"""
from __future__ import annotations

import argparse
import json
import random
import re
import statistics
import sys
import time
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path
from typing import Any

DEFAULT_QUERIES = Path(__file__).with_name("one_language_queries.tsv")
#: a citation as Ask's prompt asks for it: a record uid or canonical id in brackets
CITATION = re.compile(r"\[([A-Za-z][\w.-]*:[^\]\s,;]+)\]")


def citations(answer: str, turns: list[dict[str, Any]]) -> tuple[int, int]:
    """(cited, grounded): the distinct ids the answer cites, and how many of
    them occur in what its tools returned."""
    cited = set(CITATION.findall(answer or ""))
    seen = " ".join(json.dumps(t.get("result"), ensure_ascii=False, default=str) for t in turns)
    return len(cited), sum(1 for c in cited if c in seen)
OPERATOR_LANGS = frozenset({"en", "it", "de", "fr", "es"})


def read_queries(path: Path, per_lang: int | None = None,
                 langs: frozenset[str] | None = None) -> list[dict[str, str]]:
    rows, seen = [], defaultdict(int)
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split("\t")
        if len(parts) < 3 or not parts[2].strip():
            continue
        qid, lang, text = parts[0], parts[1], parts[2].strip()
        if langs is not None and lang not in langs:
            continue
        if per_lang is not None and seen[lang] >= per_lang:
            continue
        seen[lang] += 1
        rows.append({"key": f"{qid}:{lang}", "qid": qid, "lang": lang, "query": text})
    return rows


def answer_language(text: str) -> str | None:
    from app import query_language
    return query_language.guess(text or "", query_language.guessable())


def _warm_up(url: str, model: str) -> None:
    """One throwaway answer, so the first timed question finds the model loaded."""
    import httpx
    httpx.post(f"{url}/v1/chat/completions", timeout=900,
               json={"model": model, "messages": [{"role": "user", "content": "Say OK."}],
                     "max_tokens": 1})


def run(out: Path, name: str, *, model: str, queries: list[dict[str, str]],
        collections: list[str] | None = None,
        ask: Callable[..., dict[str, Any]] | None = None,
        guess: Callable[[str], str | None] = answer_language,
        pid: Callable[[], int | None] | None = None, tag: str = "",
        redo: bool = False) -> int:
    """WARM ANSWERS ONLY are timed (operator, 2026-11-03): the model is pinned
    for the run and warmed up first, and an answer during which its backend
    was (re)loaded — no backend before, or another pid after — is marked
    `warm: false`, counted, and left out of the time. `tag` labels the
    answers asked now (the configuration they ran on); with `redo` the
    questions already answered under ANOTHER tag are asked again under this
    one, so a configuration is measured on the same questions as the last."""
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
    done = ({(r["key"], r.get("tag", "")) for r in _rows(out, name)} if answers.exists() else set())
    done = {k for k, t in done if t == tag or not redo}
    release: Callable[[], None] = lambda: None
    if ask is None:
        import os

        from app import config, model_load, model_pins
        from app.ask import chat
        config.CHAT_MODEL = model          # this process asks the candidate only
        ask = chat.ask
        holder = f"chat_eval pid {os.getpid()}"
        model_pins.hold(config.CHAT_URL, model, holder)
        release = lambda: model_pins.release(holder)   # its prior pin state restored
        _warm_up(config.CHAT_URL, model)
        pid = pid or (lambda: model_load.backend_pid(config.CHAT_URL, model, 0.0))
    try:
        for q in queries:
            if q["key"] in done:
                continue
            before = pid() if pid else None
            t0 = time.monotonic()
            error = None
            try:
                got = ask(q["query"], collections=collections)
            except Exception as e:   # noqa: BLE001 — a failed answer is a row, not the end of the run
                got, error = {"answer": "", "turns": [], "rounds": 0, "exhausted": False}, \
                    f"{type(e).__name__}: {e}"[:300]
            seconds = round(time.monotonic() - t0, 2)
            after = pid() if pid else None
            turns = got.get("turns", [])
            cited, grounded = citations(got.get("answer") or "", turns)
            row = {**q, "tag": tag, "answer": got.get("answer") or "", "rounds": got.get("rounds", 0),
                   "exhausted": bool(got.get("exhausted")),
                   "tools": [t.get("tool") for t in turns],
                   "tool_errors": sum(1 for t in turns if t.get("error")),
                   "seconds": seconds, "error": error,
                   "warm": pid is None or (before is not None and before == after),
                   "model_s": round(sum(got.get("model_s") or ()), 3),
                   "tools_s": round(sum(t.get("seconds") or 0.0 for t in turns), 3),
                   "search_s": search_seconds(turns),
                   "cited": cited, "grounded": grounded, "no_tool": not turns}
            row["answer_lang"] = guess(row["answer"])
            with answers.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    finally:
        release()
    print(f"{name}: {len(queries) - len(done)} asked now, {len(queries)} in all")
    return 0


#: the search's own timings (app/retrieval.py `timings`, milliseconds) an
#: answer's time is split by; "total" is the search's whole
SEARCH_PARTS = ("languages", "catalog", "fts", "dense", "rerank", "total")


def search_seconds(turns: list[dict[str, Any]]) -> dict[str, float]:
    """The searches' own timings summed over an answer's louise_search
    calls, in seconds: where inside the tool calls the time went."""
    out = dict.fromkeys(SEARCH_PARTS, 0.0)
    for t in turns:
        timings = (t.get("result") or {}).get("timings") if t.get("tool") == "louise_search" else None
        for part in SEARCH_PARTS:
            v = (timings or {}).get(part)
            if isinstance(v, (int, float)):
                out[part] += v / 1000.0
    return {k: round(v, 3) for k, v in out.items()}


def _rows(out: Path, name: str) -> list[dict[str, Any]]:
    path = out / "runs" / name / "answers.jsonl"
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l]


def summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(rows)
    if not n:
        return {"n": 0}
    calls = sum(len(r["tools"]) for r in rows)
    guessed = [r for r in rows if r["answer_lang"] is not None]
    warm = [r["seconds"] for r in rows if r.get("warm", True)]
    return {"n": n,
            "cold": n - len(warm),
            "failed": sum(1 for r in rows if r["error"]),
            "exhausted": sum(1 for r in rows if r["exhausted"]),
            "calls_per_answer": calls / n,
            "tool_errors": sum(r["tool_errors"] for r in rows),
            "lang_right": sum(1 for r in guessed if r["answer_lang"] == r["lang"]),
            "lang_guessed": len(guessed),
            "median_s": statistics.median(warm) if warm else None,
            "no_tool": sum(1 for r in rows if r.get("no_tool")),
            "cited": sum(r.get("cited", 0) for r in rows),
            "grounded": sum(r.get("grounded", 0) for r in rows),
            "uncited": sum(1 for r in rows if "cited" in r and not r["cited"])}


def _labelled(out: Path) -> list[tuple[str, list[dict[str, Any]]]]:
    """(label, rows) per run and tag: a run's answers under each tag are
    their own rows, `name [tag]`; untagged ones are the run's name."""
    groups: list[tuple[str, list[dict[str, Any]]]] = []
    for name in sorted(p.name for p in (out / "runs").iterdir() if (p / "answers.jsonl").exists()):
        by_tag: dict[str, list[dict[str, Any]]] = {}
        for r in _rows(out, name):
            by_tag.setdefault(r.get("tag", ""), []).append(r)
        groups += [(f"{name} [{t}]" if t else name, rows) for t, rows in by_tag.items()]
    return groups


def _median(values: list[float]) -> str:
    return "-" if not values else format(statistics.median(values), ".2f")


def score(out: Path) -> int:
    groups = _labelled(out)
    print("| model | languages | answers | failed | bound hit | calls/answer | tool errors "
          "| answer in the query's language (of those guessed) | cold (not timed) | median s, warm "
          "| no tool call | cited per answer | grounded | answers citing nothing |")
    print("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for name, rows in groups:
        parts = {"all": rows,
                  "the operator's five": [r for r in rows if r["lang"] in OPERATOR_LANGS],
                  "the others": [r for r in rows if r["lang"] not in OPERATOR_LANGS]}
        for label, sub in parts.items():
            s = summary(sub)
            if not s["n"]:
                continue
            share = s["lang_right"] / s["lang_guessed"] if s["lang_guessed"] else 0.0
            print(f"| {name} | {label} | {s['n']} | {s['failed']} | {s['exhausted']} | "
                  f"{s['calls_per_answer']:.2f} | {s['tool_errors']} | "
                  f"**{share:.2f}** ({s['lang_right']}/{s['lang_guessed']}) | {s['cold']} | "
                  f"{'-' if s['median_s'] is None else format(s['median_s'], '.1f')} | "
                  f"{s['no_tool']} | {s['cited'] / s['n']:.1f} | "
                  f"{(s['grounded'] / s['cited']) if s['cited'] else 0.0:.2f} | {s['uncited']} |")
    print("\nWhere a warm answer's time goes (medians, seconds; the search parts are the "
          "node's own timings, summed over an answer's searches):\n")
    print("| model | warm answers | answer | model rounds | tool calls | search | rerank "
          "| language plan | dense | words |")
    print("|---|---|---|---|---|---|---|---|---|---|")
    for name, rows in groups:
        warm = [r for r in rows if r.get("warm", True) and not r["error"] and "model_s" in r]
        cells = [_median([r["seconds"] for r in warm]), _median([r["model_s"] for r in warm]),
                 _median([r["tools_s"] for r in warm])]
        cells += [_median([r["search_s"][p] for r in warm])
                  for p in ("total", "rerank", "languages", "dense", "fts")]
        print(f"| {name} | {len(warm)} | " + " | ".join(cells) + " |")
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
    p.add_argument("--langs", default="", help="only these languages, e.g. en,it,de,fr,es")
    p.add_argument("--collections", default="")
    p.add_argument("--tag", default="", help="the configuration these answers run on "
                   "(e.g. opt0, opt-default+graph); score splits by it")
    p.add_argument("--redo", action="store_true", help="ask again, under --tag, the "
                   "questions answered under another tag")
    p = sub.add_parser("score")
    p.add_argument("--out", type=Path, required=True)
    p = sub.add_parser("sample")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--runs", required=True)
    p.add_argument("--n", type=int, default=30)
    a = ap.parse_args(argv)
    if a.cmd == "run":
        cols = [c for c in a.collections.split(",") if c] or None
        langs = frozenset(l for l in a.langs.split(",") if l) or None
        return run(a.out, a.name, model=a.model, queries=read_queries(a.queries, a.per_lang, langs),
                   collections=cols, tag=a.tag, redo=a.redo)
    if a.cmd == "score":
        return score(a.out)
    return sample(a.out, [r for r in a.runs.split(",") if r], a.n)


if __name__ == "__main__":
    sys.exit(main())
