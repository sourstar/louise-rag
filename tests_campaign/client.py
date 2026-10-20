"""The QUERY CAMPAIGN — live acceptance tests of what each kind of query
promises (docs/05, 02 §2.10), run against a RUNNING node, never part of the
unit suite or its gate (operator, 2026-10-20: "make a test campaign on the
querying … ensure that the query does what it promises; keep all these tests
in a separated section").

    LOUISE_URL=http://192.168.178.10:30042 python -m pytest tests_campaign -q -rA

Without LOUISE_URL every test is skipped. The ground truth is the node's own
data (tests_campaign/ground_truth.py), read on 2026-10-20. Every request is
read-only: the ask routes are open on the LAN (07 §7.4) and change nothing."""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

BASE = os.environ.get("LOUISE_URL", "").rstrip("/")
TIMEOUT = float(os.environ.get("LOUISE_CAMPAIGN_TIMEOUT", "300"))


class Answer:
    """One HTTP answer: status, parsed JSON (or None), the wall clock."""

    def __init__(self, status: int, body: bytes, seconds: float):
        self.status, self.raw, self.seconds = status, body, seconds
        try:
            self.json = json.loads(body)
        except ValueError:
            self.json = None

    def __repr__(self) -> str:
        return f"<{self.status} in {self.seconds:.1f} s: {self.raw[:300]!r}>"


def post(path: str, body: dict) -> Answer:
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return Answer(r.status, r.read(), time.monotonic() - t0)
    except urllib.error.HTTPError as e:
        return Answer(e.code, e.read(), time.monotonic() - t0)


_cache: dict[str, Answer] = {}


def search(query: str, **kw) -> Answer:
    """`POST /api/ask/search` (05 §5.4 louise_search), on gutenberg unless
    told otherwise; answers are cached per body for the session, so tests
    that read the same answer do not pay the query twice."""
    body = {"query": query, "collections": ["gutenberg"], "limit": 10, **kw}
    key = json.dumps(body, sort_keys=True)
    if key not in _cache:
        _cache[key] = post("/api/ask/search", body)
    return _cache[key]


def hits(answer: Answer) -> list[dict]:
    assert answer.status == 200, answer
    lists = answer.json["lists"]
    assert len(lists) == 1, f"one space searched, one list (05 §5.1): {len(lists)}"
    return lists[0]["hits"]


def work_of(hit: dict) -> set[str]:
    """The uids a hit stands for: itself and the editions grouped under it (05 §5.3)."""
    return {hit["uid"], *(e["uid"] for e in hit.get("editions") or [])}
