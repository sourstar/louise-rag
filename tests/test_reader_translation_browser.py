"""The Ask reader, translated as it is read — in a real browser.

"I want also that text to be translated, the visible part first, then the
same amount of text on bottom and top, then as I scroll, more text is
translated in the direction of the scrolling. The goal is to limit the
translation effort while giving a seamless translation experience"
(operator, 2026-09-18).

What is on screen, what a scroll brings into view and whether the view
moves are questions of layout, so the answer needs a browser:
tests/browser/reader_translation.js drives headless Chromium over the Ask
page and a reader fragment that the app renders here. The dev image carries
the browser (`docker build --target dev -t louise-rag:dev .`); an image
without one, the runtime, skips this and says why.
"""
import random
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import web
from app.api import app
from tests import browser_support

SCRIPT = Path(__file__).parent / "browser" / "reader_translation.js"
NODE = browser_support.NODE

pytestmark = pytest.mark.skipif(not browser_support.available(), reason=browser_support.REASON)


def _fragment() -> str:
    """The reader.html fragment for a 150-paragraph English book, chapters of
    ten, paragraph 60 highlighted: `[n] ` opens each so the script can tell
    which paragraphs a request carried."""
    rng = random.Random(7)
    words = "the keeper lit the lamp every night and watched the sea for ships".split()
    chunks = [{"seq": seq, "chunk_id": f"b1:{seq}", "chapter": f"CHAPTER {seq // 10 + 1}",
               "text": f"[{seq}] " + " ".join(rng.choice(words)
                                              for _ in range(rng.randint(40, 260)))}
              for seq in range(150)]
    doc = {"header": {"title": "The Lighthouse", "reference": "Anon · #1 (en)", "lang": "en",
                      "origin_url": None, "text_from": "lexical index"},
           "chunks": chunks}
    return web.TEMPLATES.env.get_template("reader.html").render(
        fragment=True, doc=doc, highlights=["b1:60"], collection="c", book_id=1,
        plugin_label="Gutenberg")


def test_the_reader_is_translated_as_it_is_read_in_a_browser(isolated_config, tmp_path):
    page = TestClient(app).get("/ask").text
    assert 'value="it"' in page and 'value="de"' in page, "the script picks these two"
    (tmp_path / "ask.html").write_text(page, encoding="utf-8")
    (tmp_path / "fragment.html").write_text(_fragment(), encoding="utf-8")
    run = subprocess.run([NODE, str(SCRIPT), str(tmp_path)], capture_output=True,
                         text=True, timeout=300, env=browser_support.env())
    report = run.stdout + run.stderr
    assert run.returncode == 0 and "ALL PASSED" in run.stdout, report
    assert "FAIL" not in run.stdout, report
