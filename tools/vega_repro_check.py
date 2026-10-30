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
import (invariant 7). Exits 1 when any text came back as more than one vector.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
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


def measure(texts: list[str], *, url: str, model: str, loads: int, repeats: int,
            window: int | None, post: Callable[..., Any], seed: int = 0) -> dict[str, Any]:
    """{texts, vectors, identical, differ: [index…]}: `differ` lists the texts
    that came back as more than one byte string."""
    base = url.rstrip("/")
    seen: list[set[bytes]] = [set() for _ in texts]
    for load in range(loads):
        post(f"{base}/backend/shutdown", json={"model": model}, timeout=120)
        for repeat in range(repeats):
            order = list(range(len(texts)))
            random.Random(seed + load * repeats + repeat).shuffle(order)
            for k in order:
                batch = client.tag_batch([texts[k]], window=window)
                r = post(f"{base}/v1/embeddings", json={
                    "model": model, "input": list(batch.inputs), "encoding_format": "base64"},
                    timeout=600)
                if r.status_code != 200:
                    raise RuntimeError(f"text {k}: HTTP {r.status_code}: {r.text[-240:]}")
                (vector,) = client.parse_response(r.content)
                if not isinstance(vector, bytes):
                    raise RuntimeError(f"text {k}: the answer is numbers, not the float32 "
                                       f"bytes (base64) a byte compare needs")
                seen[k].add(vector)
    differ = [k for k, s in enumerate(seen) if len(s) > 1]
    return {"texts": len(texts), "vectors": len(texts) * loads * repeats,
            "identical": len(texts) - len(differ), "differ": differ}


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
    out = measure(texts, url=args.url, model=args.model, loads=args.loads,
                  repeats=args.repeats, window=args.window, post=httpx.post)
    differ = f"; differ: {out['differ']}" if out["differ"] else ""
    print(f"{args.model}: {out['identical']}/{out['texts']} texts ({args.n} spread, "
          f"{out['texts'] - min(args.n, len(every))} swept) bit-identical over "
          f"{out['vectors']} vectors ({args.loads} fresh loads x {args.repeats} shuffled "
          f"passes){differ}")
    return 1 if out["differ"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
