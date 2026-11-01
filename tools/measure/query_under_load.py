"""Searches under corpus load: how long each step of a person's query waits
while the worker embeds at full rate (docs/notes/gpu-saturation-plan.md,
instrument I4; step 4: queries first, nothing broken in the middle).

    # idle baseline first (every collection paused), then the same under load
    python -m tools.measure.query_under_load --url http://localhost:8742 \\
        --token-file /run/louise/public/user-token --minutes 10 --out idle.json
    python -m tools.measure.query_under_load --url http://localhost:8742 \\
        --token-file /run/louise/public/user-token --minutes 30 \\
        --baseline idle.json --card-record /data/state/card --out load.json

What it does:
- sends a scripted search (`POST /api/search`, the web's own route: the
  translate, catalog, text, query-embedding and rerank steps as a person's
  search runs them) every `--every` seconds on a fixed beat, and every
  `--burst-every` minutes a burst of `--burst` at once;
- with `--answer`, also asks `/v1/chat/completions` (streamed) and records
  the first token's and the whole answer's time;
- records each search's own `timings` (languages, catalog, fts, dense — the
  query embedding is in it —, rerank, total), the HTTP time, and every
  failure, by kind;
- prints p50 / p95 / max per step, and against `--baseline` (an idle run's
  file) the difference per step;
- with `--card-record` (the worker's per-second record, app/rage/card_record.py),
  the corpus side over the same window: the card's busy, the seconds a query
  held the claim, and each collection's request-seconds held in query seconds
  against the others — the corpus throughput a query costs.

Nothing here writes to a collection. A half-written document is the
corpus side's to check after the window: `tools/measure/chunks_drift.py` over
the documents embedded in it.

The token is read from a FILE (invariant 8), never from the command line.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

#: what a person asks, across the collections' subjects and languages
DEFAULT_QUERIES = (
    "Who wrote Pride and Prejudice?",
    "effetti collaterali della metformina",
    "Treaty of Westphalia consequences",
    "legge 241 del 1990 accesso agli atti",
    "BRCA1 mutation breast cancer risk",
    "Wer war Hildegard von Bingen?",
    "photosynthesis light reactions",
    "la révolution de juillet 1830",
)
#: a search's steps as `retrieval` reports them, plus the HTTP round trip
STEPS = ("languages", "catalog", "fts", "dense", "rerank", "total", "http")


@dataclass
class Sample:
    at: float
    query: str
    burst: bool
    http_ms: float | None = None
    timings: dict[str, float] = field(default_factory=dict)
    error: str | None = None
    first_token_ms: float | None = None
    answer_ms: float | None = None


def _post(url: str, token: str | None, body: dict, timeout: float) -> tuple[int, Any, float]:
    import httpx
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    t0 = time.monotonic()
    r = httpx.post(url, json=body, headers=headers, timeout=timeout)
    ms = (time.monotonic() - t0) * 1000
    try:
        data = r.json()
    except ValueError:
        data = None
    return r.status_code, data, ms


def _stream_answer(url: str, token: str | None, query: str, collections, timeout: float
                   ) -> tuple[float | None, float]:
    """(first token ms, whole answer ms) of a streamed chat answer."""
    import httpx
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    body = {"model": "louise", "stream": True, "messages": [{"role": "user", "content": query}]}
    if collections:
        body["collections"] = collections
    t0 = time.monotonic()
    first = None
    with httpx.stream("POST", url, json=body, headers=headers, timeout=timeout) as r:
        r.raise_for_status()
        for line in r.iter_lines():
            if line.startswith("data:") and '"content"' in line and first is None:
                first = (time.monotonic() - t0) * 1000
    return first, (time.monotonic() - t0) * 1000


def one(base: str, token: str | None, query: str, *, collections, burst: bool,
        answer: bool, timeout: float,
        post: Callable = _post, stream: Callable = _stream_answer) -> Sample:
    s = Sample(at=time.time(), query=query, burst=burst)
    body: dict[str, Any] = {"query": query}
    if collections:
        body["collections"] = collections
    try:
        status, data, ms = post(base.rstrip("/") + "/api/search", token, body, timeout)
        s.http_ms = ms
        if status != 200 or not isinstance(data, dict):
            s.error = f"HTTP {status}"
            return s
        s.timings = {k: float(v) for k, v in (data.get("timings") or {}).items()
                     if isinstance(v, (int, float))}
        if answer:
            s.first_token_ms, s.answer_ms = stream(base.rstrip("/") + "/v1/chat/completions",
                                                   token, query, collections, timeout)
    except Exception as e:   # noqa: BLE001 — every failure is a row, by kind
        s.error = type(e).__name__
    return s


def run(base: str, token: str | None, *, minutes: float, every_s: float, burst: int,
        burst_every_min: float, queries=DEFAULT_QUERIES, collections=None,
        answer: bool = False, timeout: float = 120.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        ask: Callable[..., Sample] | None = None) -> list[Sample]:
    """Searches on a fixed beat (a slow one never shifts the next), bursts
    on theirs, for `minutes`; every one on its own thread."""
    ask = ask or (lambda q, b: one(base, token, q, collections=collections, burst=b,
                                   answer=answer, timeout=timeout))
    out: list[Sample] = []
    lock = threading.Lock()
    threads: list[threading.Thread] = []

    def go(q: str, b: bool) -> None:
        got = ask(q, b)
        with lock:
            out.append(got)

    start = clock()
    end = start + minutes * 60
    n = 0
    next_burst = start + burst_every_min * 60 if burst and burst_every_min else None
    tick = start
    while tick < end:
        q = queries[n % len(queries)]
        n += 1
        t = threading.Thread(target=go, args=(q, False), daemon=True)
        t.start()
        threads.append(t)
        if next_burst is not None and tick >= next_burst:
            for i in range(burst):
                bt = threading.Thread(target=go, args=(queries[(n + i) % len(queries)], True),
                                      daemon=True)
                bt.start()
                threads.append(bt)
            next_burst += burst_every_min * 60
        tick += every_s
        wait = tick - clock()
        if wait > 0:
            sleep(wait)
    for t in threads:
        t.join(timeout)
    return sorted(out, key=lambda s: s.at)


def _pct(values: list[float], p: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(p * (len(ordered) - 1))))]


def summary(samples: list[Sample]) -> dict[str, Any]:
    ok = [s for s in samples if s.error is None]
    steps: dict[str, dict[str, float]] = {}
    for step in STEPS:
        vals = [s.http_ms if step == "http" else s.timings.get(step) for s in ok]
        vals = [v for v in vals if v is not None]
        if vals:
            steps[step] = {"n": len(vals), "p50": statistics.median(vals),
                           "p95": _pct(vals, 0.95), "max": max(vals)}
    for name, attr in (("first_token", "first_token_ms"), ("answer", "answer_ms")):
        vals = [getattr(s, attr) for s in ok if getattr(s, attr) is not None]
        if vals:
            steps[name] = {"n": len(vals), "p50": statistics.median(vals),
                           "p95": _pct(vals, 0.95), "max": max(vals)}
    errors: dict[str, int] = {}
    for s in samples:
        if s.error:
            errors[s.error] = errors.get(s.error, 0) + 1
    return {"searches": len(samples), "failed": sum(errors.values()), "errors": errors,
            "bursts": sum(1 for s in samples if s.burst), "steps": steps}


def corpus_side(card_dir: Path, since: float, until: float) -> dict[str, Any]:
    """The worker's record over the window: the card's busy, query seconds,
    and each collection's request-seconds held a second with a query and
    without one (what a query costs the corpus)."""
    from app.rage import card_record
    rows = list(card_record.read(since, until, card_dir))
    with_q = [r for r in rows if r.get("q")]
    without = [r for r in rows if not r.get("q")]

    def held(rs) -> dict[str, float]:
        out: dict[str, float] = {}
        for r in rs:
            for g in (r.get("gates") or {}).values():
                for p, v in (g.get("served") or {}).items():
                    out[p] = out.get(p, 0.0) + v
        return {p: round(v / len(rs), 3) for p, v in out.items()} if rs else {}
    whole = card_record.summarize(rows, minute=False)
    return {"seconds": len(rows), "query_seconds": len(with_q),
            "busy": None if not whole or whole[0].busy is None else round(whole[0].busy, 4),
            "held_per_second_in_query": held(with_q),
            "held_per_second_otherwise": held(without)}


def report(result: dict[str, Any], baseline: dict[str, Any] | None,
           say: Callable[[str], None] = print) -> None:
    s = result["summary"]
    say(f"{s['searches']} searches ({s['bursts']} in bursts), {s['failed']} failed"
        + (f": {s['errors']}" if s["errors"] else ""))
    base = (baseline or {}).get("summary", {}).get("steps", {})
    for step, v in s["steps"].items():
        line = f"  {step:12s} p50 {v['p50']:8.1f} ms  p95 {v['p95']:8.1f} ms  max {v['max']:8.1f} ms"
        if step in base:
            line += (f"   vs idle: p50 {v['p50'] - base[step]['p50']:+8.1f}, "
                     f"p95 {v['p95'] - base[step]['p95']:+8.1f}")
        say(line)
    c = result.get("corpus")
    if c:
        say(f"  corpus: card busy {c['busy']}, {c['query_seconds']} of {c['seconds']} s with a "
            f"query; request-seconds held a second with a query "
            f"{c['held_per_second_in_query']} against {c['held_per_second_otherwise']}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--url", required=True, help="the web's base URL")
    ap.add_argument("--token-file", type=Path, help="a user token, read from this file")
    ap.add_argument("--minutes", type=float, default=30.0)
    ap.add_argument("--every", type=float, default=10.0, help="seconds between searches")
    ap.add_argument("--burst", type=int, default=5, help="searches at once in a burst")
    ap.add_argument("--burst-every", type=float, default=5.0, help="minutes between bursts")
    ap.add_argument("--collections", nargs="*")
    ap.add_argument("--queries", type=Path, help="one query a line (default: a built-in set)")
    ap.add_argument("--answer", action="store_true", help="also time a streamed chat answer")
    ap.add_argument("--timeout", type=float, default=120.0)
    ap.add_argument("--baseline", type=Path, help="an idle run's --out file")
    ap.add_argument("--card-record", type=Path, help="the worker's /data/state/card")
    ap.add_argument("--out", type=Path)
    a = ap.parse_args(argv)
    token = a.token_file.read_text().strip() if a.token_file else None
    queries = (tuple(q for q in a.queries.read_text().splitlines() if q.strip())
               if a.queries else DEFAULT_QUERIES)
    t0 = time.time()
    samples = run(a.url, token, minutes=a.minutes, every_s=a.every, burst=a.burst,
                  burst_every_min=a.burst_every, queries=queries, collections=a.collections,
                  answer=a.answer, timeout=a.timeout)
    result: dict[str, Any] = {"url": a.url, "since": t0, "until": time.time(),
                              "summary": summary(samples),
                              "samples": [vars(s) for s in samples]}
    if a.card_record:
        result["corpus"] = corpus_side(a.card_record, t0, result["until"])
    baseline = json.loads(a.baseline.read_text()) if a.baseline else None
    report(result, baseline)
    if a.out:
        a.out.write_text(json.dumps(result, indent=1))
    return 0 if result["summary"]["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
