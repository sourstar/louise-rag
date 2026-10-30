"""CLAUDE.md invariant 12, measured: a vega- model's vectors of the same texts
over fresh loads and shuffled orders, compared as the float32 bytes the
backend produced (base64). Every text must come back bit-identical, every
time — not within a tolerance.

    python -m tools.vega_repro_check --url http://<localai>:30286 \\
        --model vega-bge-m3 [--n 16] [--loads 2] [--repeats 2] [--window 1024] \\
        [--texts calibration/text.jsonl] [--sweep 512]

THE SWEEP (2026-10-30). `--sweep` adds that many prefixes of the file's
longest text (a pair's second segment), word by word from one word up to the
window: every input length band, not only the file's. A kernel that is not
reproducible at some shapes is found only by an input of that shape. MedCPT's
32 pairs measured bit-exact on a tree whose GPU plugin then gave 7 of 160
pubmed pairs different bytes, all of 65–73 tokens, a band none of the 32 had
(München, run 39578bfb).

Each load begins with `POST /backend/shutdown`, so the next request loads the
model afresh; each repeat sends every text ALONE, tagged as Louise tags it
(app/vega/client.tag_batch), in its own seeded order. `--window` cuts an
over-long text at that many tokens, as a space that cuts by tokens does
(through the token ids where the backend embeds them, P2). The texts are `n`
spread over the file, so the lengths are the file's. Plain HTTP, no GPU
import (invariant 7). Prints REPRODUCIBLE and exits 0; NOT REPRODUCIBLE (each
differing text with its max |diff|) and exits 1; NOT MEASURED and exits 2 when
a text was refused or the model never came back: read the exit code, never
the absence of a word.
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.vega import client  # noqa: E402


def item(row: dict) -> str | tuple[str, str]:
    """A line's input: its `text`, or its `pair` ([first, second]) — the pair
    form 09 §9.3 feeds a pair layer (MedCPT's article encoder), tagged as such."""
    if "pair" in row:
        first, second = row["pair"]
        return (str(first), str(second))
    return row["text"]


def spread(texts: list[str], n: int) -> list[str]:
    """`n` texts at evenly spaced places in the file, every one if fewer."""
    if n >= len(texts):
        return list(texts)
    return [texts[i * len(texts) // n] for i in range(n)]


def sweep(texts: list, count: int, window: int | None) -> list:
    """`count` prefixes of the longest text (of a pair's second segment, its
    first kept whole), from one word up to `window` words — or all of them,
    for no window — spread evenly. A word is at least one token for every
    tokenizer in use, so they reach the window and pass through every band
    below it."""
    if count <= 0 or not texts:
        return []
    longest = max(texts, key=lambda t: len(t[1]) if isinstance(t, tuple) else len(t))
    first, body = longest if isinstance(longest, tuple) else (None, longest)
    words = body.split()
    top = min(len(words), window) if window else len(words)
    if top <= 0:
        return []
    n = min(count, top)
    sizes = sorted({1 + (k * (top - 1)) // max(1, n - 1) for k in range(n)})
    return [(first, " ".join(words[:m])) if first is not None else " ".join(words[:m])
            for m in sizes]


#: LocalAI's answers while a model it was told to stop is still loading again:
#: it dials the stopped process ("connection refused") and says so as gRPC's
#: Unavailable. Asked again, never taken for the model's answer.
LOADING = re.compile(r"Unavailable|connection refused|is loading|not loaded", re.I)
#: after each shutdown, before the first text (a model reloads in seconds)
SETTLE_S = 5.0
#: how long one text is asked again while the model loads, then the
#: measurement fails: bounded, as every wait on something that can die
LOAD_WAIT_S = 300.0


def _embed(post, base: str, model: str, tagged: list[str], k: int, *,
           sleep: Callable[[float], None], clock: Callable[[], float]):
    deadline = clock() + LOAD_WAIT_S
    while True:
        r = post(f"{base}/v1/embeddings", json={
            "model": model, "input": tagged, "encoding_format": "base64"}, timeout=600)
        if r.status_code == 200:
            return r
        if r.status_code >= 500 and LOADING.search(r.text) and clock() < deadline:
            sleep(1.0)
            continue
        raise RuntimeError(f"text {k}: HTTP {r.status_code}: {r.text[-240:]}")


def _max_abs(vectors: set[bytes]) -> float:
    """The largest element difference between the byte strings a text came
    back as, read as float32."""
    from array import array
    rows = [array("f", v) for v in vectors]
    return max(abs(a - b) for x in rows for y in rows for a, b in zip(x, y))


def measure(texts: list[str], *, url: str, model: str, loads: int, repeats: int,
            window: int | None, post: Callable[..., Any], seed: int = 0,
            sleep: Callable[[float], None] | None = None,
            clock: Callable[[], float] | None = None) -> dict[str, Any]:
    """{texts, vectors, identical, differ: [index…], max_abs: {index: float}}:
    `differ` lists the texts that came back as more than one byte string.

    THE LOAD IS WAITED FOR (2026-10-30). After `/backend/shutdown`, LocalAI
    answered gutenberg's qwen3-4b "Unavailable … connection refused" for
    seconds: the stopped process was dialled. Every text failed in ~5 s, and
    a harness that took "no `differ` printed" for clean would have resumed
    the collection on eight failed runs (München). The first text waits
    SETTLE_S; a text answered "loading" is asked again for up to LOAD_WAIT_S;
    any other refusal fails the measurement."""
    sleep = sleep or time.sleep
    clock = clock or time.monotonic
    base = url.rstrip("/")
    seen: list[set[bytes]] = [set() for _ in texts]
    for load in range(loads):
        post(f"{base}/backend/shutdown", json={"model": model}, timeout=120)
        sleep(SETTLE_S)
        for repeat in range(repeats):
            order = list(range(len(texts)))
            random.Random(seed + load * repeats + repeat).shuffle(order)
            for k in order:
                batch = client.tag_batch([texts[k]], window=window)
                r = _embed(post, base, model, list(batch.inputs), k, sleep=sleep, clock=clock)
                (vector,) = client.parse_response(r.content)
                if not isinstance(vector, bytes):
                    raise RuntimeError(f"text {k}: the answer is numbers, not the float32 "
                                       f"bytes (base64) a byte compare needs")
                seen[k].add(vector)
    differ = [k for k, s in enumerate(seen) if len(s) > 1]
    return {"texts": len(texts), "vectors": len(texts) * loads * repeats,
            "identical": len(texts) - len(differ), "differ": differ,
            "max_abs": {k: _max_abs(seen[k]) for k in differ}}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="tools.vega_repro_check")
    p.add_argument("--url", required=True, help="LocalAI's base URL")
    p.add_argument("--model", required=True, help="a vega- model")
    p.add_argument("--n", type=int, default=16, help="texts, spread over the file")
    p.add_argument("--loads", type=int, default=2, help="fresh loads")
    p.add_argument("--repeats", type=int, default=2, help="shuffled passes per load")
    p.add_argument("--window", type=int, default=None, help="tokens, the space's ctx")
    p.add_argument("--texts", type=Path, default=Path("calibration/text.jsonl"),
                   help="JSON lines with `text`, or `pair`: [first, second]")
    p.add_argument("--sweep", type=int, default=512,
                   help="prefixes of the longest text, one word up to the window (0: none)")
    args = p.parse_args(argv)
    import httpx
    every = [item(json.loads(line)) for line in
             args.texts.read_text(encoding="utf-8").splitlines() if line.strip()]
    texts = spread(every, args.n) + sweep(every, args.sweep, args.window)
    try:
        out = measure(texts, url=args.url, model=args.model, loads=args.loads,
                      repeats=args.repeats, window=args.window, post=httpx.post)
    except Exception as e:                       # noqa: BLE001 — said, and not clean
        print(f"{args.model}: NOT MEASURED: {type(e).__name__}: {e}")
        return 2
    differ = (f"; differ: {out['differ']}, max |diff| "
              f"{max(out['max_abs'].values()):.3g}" if out["differ"] else "")
    print(f"{args.model}: {'REPRODUCIBLE' if not out['differ'] else 'NOT REPRODUCIBLE'}: "
          f"{out['identical']}/{out['texts']} texts ({args.n} spread, "
          f"{out['texts'] - min(args.n, len(every))} swept) bit-identical over "
          f"{out['vectors']} vectors ({args.loads} fresh loads x {args.repeats} shuffled "
          f"passes){differ}")
    return 1 if out["differ"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
