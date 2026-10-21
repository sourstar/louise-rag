"""The Ask page translates in the order it will be read (operator,
2026-10-21) — in a real browser, because what a clamp shows and what a
reader puts on screen are layout:

  1a. the answer box; 2. every entry's visible text, in order; 3. behind
  each entry, in order, the rest of its passage and what its reader shows
  first; 1b. an entry opened meanwhile takes the model at once, then its
  screen below, then above; 1c. a screen ahead in the scroll's direction;
  1d. the page goes on when the reader has what it needs.

tests/browser/ask_translation_order.js drives the real page over results,
reader fragments and first screens the app renders here."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import reader, web
from app.api import app
from tests import browser_support

SCRIPT = Path(__file__).parent / "browser" / "ask_translation_order.js"
N = 6

pytestmark = pytest.mark.skipif(not browser_support.available(), reason=browser_support.REASON)


def _passage(k: int) -> str:
    return " ".join(f"<{k}.{j}> The whale rose beside the ship and the crew watched it in silence."
                    for j in range(24))


def _doc(k: int) -> dict:
    chunks = [{"seq": i, "chunk_id": f"c:{k}:{i}:1", "chapter": f"Part {k}.{i // 8}",
               "text": f"R{k}.{i} " + "the sea was calm and the light fell on the water " * (3 + i % 5)}
              for i in range(40)]
    return {"header": {"title": f"Book {k}", "reference": f"Anon · #{k} (en)", "lang": "en",
                       "origin_url": None, "text_from": "lexical index"}, "chunks": chunks}


def test_the_ask_page_translates_in_the_order_it_is_read(isolated_config, tmp_path):
    (tmp_path / "ask.html").write_text(TestClient(app).get("/ask").text, encoding="utf-8")
    hits = [{"chunk_id": f"c:{k}:20:1", "book_id": k, "source_collection": "c", "lang": "en",
             "title": f"Title {k}", "authors": "", "chapter": f"Chapter {k}", "text": _passage(k),
             "origin_url": None} for k in range(1, N + 1)]
    results = web.TEMPLATES.env.get_template("_ask_results.html").render(
        result={"hits": hits, "timings": {"fts": 1}})
    (tmp_path / "results.html").write_text(results, encoding="utf-8")
    for k in range(1, N + 1):
        doc, hl = _doc(k), [f"c:{k}:20:1"]
        (tmp_path / f"frag_{k}.html").write_text(web.TEMPLATES.env.get_template("reader.html").render(
            fragment=True, doc=doc, highlights=hl, collection="c", book_id=k, plugin_label="Gutenberg"),
            encoding="utf-8")
        (tmp_path / f"screen_{k}.json").write_text(json.dumps(
            {"lang": "en", "texts": reader.first_screen(doc, hl, 9000)}), encoding="utf-8")
    (tmp_path / "n.txt").write_text(str(N))
    run = subprocess.run([browser_support.NODE, str(SCRIPT), str(tmp_path)], capture_output=True,
                         text=True, timeout=300, env=browser_support.env())
    report = run.stdout + run.stderr
    assert run.returncode == 0 and "ALL PASSED" in run.stdout, report
    assert "FAIL" not in run.stdout, report
