"""What discards work asks first — in a real browser (operator, 2026-11-02:
"for the buttons reprocess/rescan/remove add a confirmation step with a
warning on the effect, to prevent accidental clicks").

The Library page as the app renders it, served by tests/browser/
library_confirm.js, which answers every request itself — a POST is only
recorded, never sent to a route. A button inside the closed Options line
cannot be clicked until the line is opened; "reprocess lexical index…"
shows its warning, and Enter (Cancel has the focus) or Cancel posts
nothing; the labelled button posts once. "remove…" the same, with the name
typed. Skipped, saying why, where no browser and no driver are installed.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config, node_state, spaces, web
from app.api import app
from tests import browser_support

SCRIPT = Path(__file__).parent / "browser" / "library_confirm.js"
STATIC = Path(__file__).resolve().parent.parent / "app" / "static"

pytestmark = pytest.mark.skipif(not browser_support.available(), reason=browser_support.REASON)


def test_reprocess_and_remove_post_only_once_confirmed(tmp_path, isolated_config, monkeypatch):
    monkeypatch.setattr(spaces, "resolve_embed_model_hash", lambda *a: ("abc123", False))
    monkeypatch.setattr(web, "_ping", lambda url: True)
    monkeypatch.setattr(config, "ADMIN_TOKEN", "sekrit")
    node_state.set_worker_state("running")
    with TestClient(app) as c:
        page = c.get("/admin/library", headers={"X-Admin-Token": "sekrit"}).text
    (tmp_path / "library.html").write_text(page, encoding="utf-8")
    run = subprocess.run([browser_support.NODE, str(SCRIPT), str(tmp_path), str(STATIC)],
                         capture_output=True, text=True, timeout=120, env=browser_support.env())
    assert run.returncode == 0, run.stdout[-2000:] + run.stderr[-2000:]
    got = json.loads(run.stdout.strip().splitlines()[-1])

    assert got["closed"] == {"open": False, "buttonVisible": False}, "the buttons wait behind Options"

    warn = got["reprocess"]
    assert warn["shown"] and warn["focused"] == "confirm-cancel", "Cancel has the focus"
    assert warn["buttons"] == ["Cancel", "Reprocess gutenberg"]
    assert "Reprocess the lexical index of gutenberg?" in warn["text"]
    assert "The lexical index is rebuilt from the start" in warn["text"]
    assert "The vectors are kept: nothing is embedded again." in warn["text"]
    assert got["afterEnter"] == {"shown": False, "posts": 0}, "Enter is Cancel"
    assert got["afterCancel"] == {"shown": False, "posts": 0}
    assert len(got["afterConfirm"]) == 1
    post = got["afterConfirm"][0]
    assert post["path"] == "/admin/library/gutenberg/reset" and json.loads(post["body"]) == {"stage": "fts"}

    rm = got["remove"]
    assert rm["shown"] and rm["focused"] == "confirm-cancel"
    assert rm["buttons"] == ["Cancel", "Remove gutenberg"]
    assert "It holds 0 documents: 0 indexed, 0 embedded." in rm["text"]
    assert "It cannot be undone" in rm["text"] and "Nothing on disk is deleted" in rm["text"]
    assert got["removeAfterCancel"] == {"shown": False, "posts": 0}
    assert len(got["removeAfterConfirm"]) == 1
    post = got["removeAfterConfirm"][0]
    assert post["path"] == "/admin/library/gutenberg/remove" and json.loads(post["body"]) == {"mode": "retire"}
