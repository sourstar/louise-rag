"""The searches-under-load harness (tools/measure/query_under_load.py; the GPU
plan's instrument I4, step 4): scripted searches on a beat and in bursts,
each step's time from the search's own `timings`, failures by kind, and the
corpus side from the worker's card record."""
from __future__ import annotations

import json
import time

from tools.measure import query_under_load as q

TIMINGS = {"languages": 3.0, "catalog": 12.0, "fts": 40.0, "dense": 25.0, "rerank": 90.0,
           "total": 170.0}


def test_a_search_is_one_row_with_its_own_step_times():
    s = q.one("http://web", "tok", "who?", collections=["gutenberg"], burst=False,
              answer=False, timeout=5,
              post=lambda url, token, body, timeout: (200, {"timings": TIMINGS, "hits": []}, 181.0))
    assert s.error is None and s.http_ms == 181.0 and s.timings == TIMINGS


def test_a_failure_is_a_row_by_kind_never_a_crash():
    bad = q.one("http://web", None, "x", collections=None, burst=True, answer=False, timeout=5,
                post=lambda *a: (503, {"detail": "busy"}, 9.0))
    assert bad.error == "HTTP 503" and bad.burst

    def boom(*a):
        raise TimeoutError("read")
    assert q.one("http://web", None, "x", collections=None, burst=False, answer=False,
                 timeout=5, post=boom).error == "TimeoutError"


def test_searches_keep_their_beat_and_bursts_theirs():
    now = {"t": 0.0}
    asked = []
    samples = q.run("http://web", None, minutes=1, every_s=10, burst=2, burst_every_min=0.5,
                    clock=lambda: now["t"], sleep=lambda s: now.__setitem__("t", now["t"] + s),
                    ask=lambda query, burst: asked.append(burst) or q.Sample(at=time.time(),
                                                                            query=query, burst=burst))
    assert len(samples) == len(asked)
    assert asked.count(False) == 6, "one every 10 s for a minute"
    assert asked.count(True) == 2, "one burst of two at 30 s"


def test_the_summary_gives_each_step_and_counts_failures():
    rows = [q.Sample(at=i, query="x", burst=False, http_ms=100.0 + i,
                     timings={**TIMINGS, "rerank": 80.0 + i}) for i in range(20)]
    rows.append(q.Sample(at=99, query="x", burst=True, error="HTTP 503"))
    s = q.summary(rows)
    assert s["searches"] == 21 and s["failed"] == 1 and s["errors"] == {"HTTP 503": 1}
    assert s["steps"]["rerank"]["p50"] == 89.5 and s["steps"]["rerank"]["max"] == 99.0
    assert s["steps"]["http"]["n"] == 20


def test_the_report_says_each_step_against_the_idle_baseline():
    load = {"summary": q.summary([q.Sample(at=0, query="x", burst=False, http_ms=300.0,
                                           timings={"dense": 60.0, "total": 300.0})])}
    idle = {"summary": q.summary([q.Sample(at=0, query="x", burst=False, http_ms=200.0,
                                           timings={"dense": 20.0, "total": 200.0})])}
    said = []
    q.report(load, idle, say=said.append)
    dense = next(l for l in said if l.strip().startswith("dense"))
    assert "vs idle: p50    +40.0" in dense


def test_the_corpus_side_compares_seconds_with_a_query_and_without(tmp_path):
    t0 = 1_762_000_000
    rows = []
    for i in range(10):
        q_on = int(i < 3)
        rows.append({"t": t0 + i, "q": q_on, "cards": {"0000:03:00.0": 0.8},
                     "gates": {"u": {"running": 1, "waiting": 0,
                                     "served": {"wikidata": 0.2 if q_on else 3.0}}}})
    (tmp_path / "2025-11-01.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    c = q.corpus_side(tmp_path, t0, t0 + 9)
    assert c["seconds"] == 10 and c["query_seconds"] == 3 and c["busy"] == 0.8
    assert c["held_per_second_in_query"] == {"wikidata": 0.2}
    assert c["held_per_second_otherwise"] == {"wikidata": 3.0}


def test_main_reads_the_token_from_a_file_and_fails_on_a_failed_search(tmp_path, monkeypatch):
    token = tmp_path / "token"
    token.write_text("s3cret\n")
    seen = {}

    def run(base, tok, **kw):
        seen["token"] = tok
        return [q.Sample(at=0, query="x", burst=False, error="HTTP 500")]
    monkeypatch.setattr(q, "run", run)
    out = tmp_path / "out.json"
    assert q.main(["--url", "http://web", "--token-file", str(token), "--minutes", "0.01",
                   "--out", str(out)]) == 1
    assert seen["token"] == "s3cret"
    assert "s3cret" not in out.read_text(), "the token is never written out"
