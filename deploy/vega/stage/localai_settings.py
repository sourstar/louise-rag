"""LocalAI's runtime settings that Louise depends on, applied to the node's
LocalAI through its own API — so a reinstall or a redeploy of the app, which
starts from LocalAI's defaults, never quietly brings an old value back.

    python3 deploy/vega/stage/localai_settings.py [http://127.0.0.1:30286]

It reads the live settings (`GET /api/settings`), sets only the keys below,
writes the whole object back (`POST /api/settings`: a partial body is not
documented to leave the rest alone) and reads them again to verify. Nothing
is printed but the keys it manages, because the object also carries
`api_keys` and `p2p_token`. Applied live, no restart.

memory_reclaimer_threshold, 0.68 → 0.85 (operator, 2026-10-23): the WatchDog
evicts the least recently used model once the card passes it. Measured on the
B70 (32,656 MiB): all eight models Louise loads take 26,693 MiB (82 %) —
vega-qwen3-embedding-4b 8,707, louise-chat-qwen3-8b 7,363,
louise-translate-madlad 6,424, vega-dar-legal-it 1,819, louise-rerank-bge
1,243, louise-rerank-medcpt 424, vega-medcpt-query 391, vega-medcpt-article-ov
322 MiB. At 0.68 (22,206 MiB) the six resident models held the card so high
that the query embedder and the chat model evicted each other on every Ask
question (4.5–6.3 s cold starts each); at 0.85 both stay, and a question went
from 20.9 + 11.1 s to 15.0 + 5.1 s. The embedder's load-time peak still
reaches 85.9 %, so the translator is the one evicted on that load.
"""
from __future__ import annotations

import json
import sys
import urllib.request
from typing import Any

DESIRED: dict[str, Any] = {
    "memory_reclaimer_enabled": True,
    "memory_reclaimer_threshold": 0.85,
}


def merged(current: dict[str, Any], desired: dict[str, Any] = DESIRED) -> dict[str, Any]:
    """`current` with the desired keys set, every other key untouched."""
    out = dict(current)
    out.update(desired)
    return out


def _call(url: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="GET" if body is None else "POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read() or b"{}")


def apply(base: str) -> dict[str, Any]:
    url = base.rstrip("/") + "/api/settings"
    current = _call(url)
    if all(current.get(k) == v for k, v in DESIRED.items()):
        return {k: current.get(k) for k in DESIRED} | {"changed": False}
    _call(url, merged(current))
    now = _call(url)
    wrong = {k: now.get(k) for k, v in DESIRED.items() if now.get(k) != v}
    if wrong:
        raise SystemExit(f"LocalAI did not take the settings: {wrong}")
    return {k: now.get(k) for k in DESIRED} | {"changed": True}


if __name__ == "__main__":
    print(apply(sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:30286"))
