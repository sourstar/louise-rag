"""Which inputs a vega- model's wrapper refuses to cut at a window — each text
sent ALONE, tagged as Louise tags it (app/vega/client.tag_batch), so one
refusal never hides the others the way a batch does (docs/notes/vega.md,
"The window"; char-cut-truncation.md).

    python -m tools.vega_cut_scan --url http://<localai>:30286 \\
        --model vega-bge-m3 --window 1024 [--texts calibration/text.jsonl]

Found 2026-10-18 on the RX 9060 XT: bge-m3 at window 1024 refused 3 of the
512 calibration texts (i=339, 352, 372) through LocalAI v4.11.0's llama.cpp,
where an offline count with HF tokenizers had passed all of them. Plain
HTTP, no GPU import (invariant 7). Exits 1 when anything was refused.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.vega import client  # noqa: E402


def scan(texts: list[dict[str, Any]], *, url: str, model: str, window: int,
         post: Callable[..., Any]) -> list[dict[str, Any]]:
    """The refused inputs: {i, chars, start, status, message}, in file order."""
    refused = []
    for t in texts:
        batch = client.tag_batch([t["text"]], window=window)
        r = post(f"{url.rstrip('/')}/v1/embeddings",
                 json={"model": model, "input": list(batch.inputs)}, timeout=600)
        if r.status_code == 200:
            continue
        try:
            message = r.json().get("error", {}).get("message", r.text)
        except ValueError:
            message = r.text
        refused.append({"i": t.get("i"), "chars": len(t["text"]), "start": t["text"][:60],
                        "status": r.status_code, "message": message[-240:]})
    return refused


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="tools.vega_cut_scan")
    p.add_argument("--url", required=True, help="LocalAI's base URL")
    p.add_argument("--model", required=True, help="a vega- model")
    p.add_argument("--window", type=int, required=True, help="tokens, the space's ctx")
    p.add_argument("--texts", type=Path, default=Path("calibration/text.jsonl"),
                   help="JSON lines with `text` (and `i`)")
    args = p.parse_args(argv)
    import httpx
    texts = [json.loads(line) for line in args.texts.read_text(encoding="utf-8").splitlines()
             if line.strip()]
    refused = scan(texts, url=args.url, model=args.model, window=args.window, post=httpx.post)
    for r in refused:
        print(json.dumps(r, ensure_ascii=False))
    print(f"{len(refused)} of {len(texts)} refused at window {args.window} by {args.model}")
    return 1 if refused else 0


if __name__ == "__main__":
    raise SystemExit(main())
