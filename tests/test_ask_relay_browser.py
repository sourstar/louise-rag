"""The Ask page's relay in a real browser (app/relay.py; operator,
2026-10-28: "the web gui session is not reset by the browser dropping a
query … if the query is stuck or dropped I should see a notification"):
a dropped connection is picked up where it stopped, stuck and dropped
questions are said, and a reload picks the question up again.

tests/browser/ask_relay.js drives the real page against a scripted node."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import web
from app.api import app
from tests import browser_support

SCRIPT = Path(__file__).parent / "browser" / "ask_relay.js"
N = 3

pytestmark = pytest.mark.skipif(not browser_support.available(), reason=browser_support.REASON)


def test_the_ask_page_survives_a_dropped_connection_and_says_what_happened(isolated_config, tmp_path):
    (tmp_path / "ask.html").write_text(TestClient(app).get("/ask").text, encoding="utf-8")
    hits = [{"chunk_id": f"c:{k}:0:1", "book_id": k, "source_collection": "c", "lang": "en",
             "title": f"Title {k}", "authors": "", "chapter": f"Chapter {k}",
             "text": f"The whale {k} rose beside the ship.", "origin_url": None} for k in range(1, N + 1)]
    (tmp_path / "results.html").write_text(web.TEMPLATES.env.get_template("_ask_results.html").render(
        result={"hits": hits, "timings": {"fts": 1}}), encoding="utf-8")
    (tmp_path / "n.txt").write_text(str(N))
    run = subprocess.run([browser_support.NODE, str(SCRIPT), str(tmp_path)], capture_output=True,
                         text=True, timeout=300, env=browser_support.env())
    report = run.stdout + run.stderr
    assert run.returncode == 0 and "ALL PASSED" in run.stdout, report
    assert "FAIL" not in run.stdout, report
