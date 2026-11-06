"""The Ask switch on a phone (operator, 2026-11-01: rename "AI summary" to
"Search + AI summary", "make it such it fits the row on the mobile
interface; do not reduce the font size, use more than one line if you must.
Both labels on one line would be preferable") — in a real browser, because
fitting is layout. tests/browser/ask_switch.js measures the two choices."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api import app
from tests import browser_support

SCRIPT = Path(__file__).parent / "browser" / "ask_switch.js"
STATIC = Path(__file__).resolve().parent.parent / "app" / "static"

pytestmark = pytest.mark.skipif(not browser_support.available(), reason=browser_support.REASON)


@pytest.mark.parametrize("width", [360, 320])
def test_both_choices_share_one_row_inside_the_screen_at_full_size(tmp_path, isolated_config, width):
    (tmp_path / "ask.html").write_text(TestClient(app).get("/ask").text, encoding="utf-8")
    run = subprocess.run([browser_support.NODE, str(SCRIPT), str(tmp_path), str(STATIC), str(width)],
                         capture_output=True, text=True, timeout=120, env=browser_support.env())
    assert run.returncode == 0, run.stderr[-2000:]
    got = json.loads(run.stdout.strip().splitlines()[-1])
    assert got["texts"] == ["Search only", "Search + AI summary"]
    assert got["fonts"] == ["12px", "12px"], "the font is not reduced"
    a, b = got["boxes"]
    assert abs(a["top"] - b["top"]) < 1, "both choices on one line"
    assert a["left"] >= 0 and b["right"] <= got["width"], "inside the screen"
    if width == 360:
        assert b["height"] < 2 * (got["lineHeight"] or 15) + 14, "one line of text each at 360 px"
