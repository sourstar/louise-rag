"""Nothing wider than a phone's screen — in a real browser.

"Web gui improvement: in mobile mode the text can exceed the width of the
screen, it shall not happen" (operator, 2026-10-21). Whether a box ends past
the screen's edge is a question of layout, so the answer needs a browser:
tests/browser/mobile_width.js loads every page of the app, served here over
a test collection, at 360 CSS pixels (the narrowest common phone) and lists
every element past the right edge. Skipped, saying why, where no browser
and no driver for it are installed.
"""
import os
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest

from app import config, spaces, web
from app.api import app

SCRIPT = Path(__file__).parent / "browser" / "mobile_width.js"
NODE = shutil.which("node")
CHROMIUM = (os.environ.get("PUPPETEER_EXECUTABLE_PATH") or shutil.which("chromium-headless-shell")
            or shutil.which("chromium") or next(
                (str(p) for p in Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers"))
                 .glob("chromium-*/chrome-linux/chrome")), None))


def _driver() -> bool:
    paths = [p for p in os.environ.get("NODE_PATH", "").split(os.pathsep) if p]
    if NODE:
        paths.append(str(Path(NODE).resolve().parents[1] / "lib" / "node_modules"))
    return any((Path(p) / d).is_dir() for p in paths for d in ("puppeteer-core", "playwright"))


pytestmark = pytest.mark.skipif(
    not (NODE and CHROMIUM and _driver()),
    reason="no browser in this image: the dev image has one "
           "(docker build --target dev -t louise-rag:dev .)")

PAGES = ["/ask", "/admin/library", "/admin/network", "/admin/system", "/help", "/admin/login"]
WIDTH = 360


@pytest.fixture
def served(isolated_config, monkeypatch):
    import uvicorn
    monkeypatch.setattr(spaces, "resolve_embed_model_hash", lambda *a: ("abc123", False))
    monkeypatch.setattr(web, "_ping", lambda url: True)
    monkeypatch.setattr(config, "ADMIN_TOKEN", "sekrit")
    # what a live node's Library shows and a test collection does not: the
    # peers' offers, every one named by its 71-character collection_id
    from app.share import offer
    monkeypatch.setattr(offer, "offered_for_node", lambda: [{
        "collection_id": LONG, "title": "gutenberg-book-editions", "plugin": "gutenberg",
        "summary": "Project Gutenberg's books, one record per edition — " + URL,
        "license": "green", "privacy": "public", "publication": "open",
        "served_by": [{"key": LONG, "address": "100.75.37.54:30042", "snapshot_id": LONG,
                       "members": 13174}],
        "first_seen": "2026-10-21T06:14:21Z", "announced_by": LONG, "relation": "twin",
        "lineage_id": LONG, "codex_known": False, "takeable": False,
        "waiting": "its snapshot " + LONG + " is not yet served"}])
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while not server.started:
        assert time.monotonic() < deadline, "the app did not start"
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(10)


#: what has nothing to break on: an id, a URL, a path, a name run together
LONG = "sha256:" + "9f" * 32
URL = "https://pubmed.ncbi.nlm.nih.gov/" + "x" * 90 + "/?from_single_result=" + "y" * 40


def _ask_results() -> str:
    """The Ask results as the app renders them, for a hit whose every field
    is as long and unbreakable as a real one can be."""
    hit = {"source_collection": "gutenberg-book-editions-" + "w" * 30, "book_id": 1,
           "chunk_id": LONG, "title": "Supercalifragilistic" * 6, "authors": "Author" * 15,
           "lang": "en", "sources": ["fts", "dense", "catalog"], "chapter": "CHAPTER_" + "Z" * 70,
           "origin_url": URL, "snippet": ("word " * 50) + "W" * 120, "editions": [],
           "annotations": []}
    result = {"hits": [hit], "withheld": [{"collection": "c" * 60, "reason": URL}],
              "timings": {"embed_ms": 12, LONG: 3},
              "facts": [{"label": "L" * 80, "entity_id": LONG, "kind": "entity",
                         "fields": {"homepage": URL, "id": LONG}}],
              "catalog": [{"lang": "en", "title": "T" * 100}]}
    return web.TEMPLATES.env.get_template("_ask_results.html").render(result=result)


def _reader() -> str:
    doc = {"header": {"title": "T" * 90, "reference": LONG, "lang": "en", "origin_url": URL,
                      "text_from": "lexical index"},
           "chunks": [{"seq": i, "chunk_id": f"b1:{i}", "chapter": "C" * 70,
                       "text": "a passage " * 30 + URL} for i in range(5)]}
    return web.TEMPLATES.env.get_template("reader.html").render(
        fragment=True, doc=doc, highlights=["b1:2"], collection="c", book_id=1,
        plugin_label="Gutenberg")


def _page(base: str, body: str) -> str:
    """A fragment inside the app's own page frame and stylesheet."""
    return ('<!doctype html><html><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<link rel="stylesheet" href="{base}/static/admin.css"></head>'
            f'<body><main><div class="panel">{body}</div></main></body></html>')


def test_no_page_is_wider_than_a_phone(served, tmp_path):
    fragments = []
    for name, body in (("ask-results", _ask_results()), ("reader", _reader())):
        f = tmp_path / f"{name}.html"
        f.write_text(_page(served, body), encoding="utf-8")
        fragments.append(f.as_uri())
    env = dict(os.environ, ADMIN_TOKEN="sekrit", CHROMIUM=CHROMIUM or "")
    if NODE:
        env["NODE_PATH"] = os.pathsep.join(
            [env.get("NODE_PATH", ""), str(Path(NODE).resolve().parents[1] / "lib" / "node_modules")])
    run = subprocess.run([NODE, str(SCRIPT), served, str(WIDTH), *PAGES, *fragments], capture_output=True,
                         text=True, timeout=300, env=env)
    report = run.stdout + run.stderr
    assert run.returncode == 0 and "ALL PASSED" in run.stdout, report
