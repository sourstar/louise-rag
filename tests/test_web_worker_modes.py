"""The node-wide mode switch must SHOW which mode the node is in.

It used to show the opposite: the accent class was applied to every mode
the node was NOT in, so the current state was the one button that looked
inactive. These tests pin the three visual states apart — selected,
clickable, and genuinely-can't-do-anything — because they are the only
place an operator reads the node's mode before changing it.
"""
import re

import pytest
from fastapi.testclient import TestClient

from app import config, db, node_state, spaces, web
from app.api import app

MODES = {"running": "run everything", "acquire-only": "download/seeding only",
         "seed-only": "seeding only",
         "paused": "pause"}


@pytest.fixture
def client(isolated_config, monkeypatch):
    monkeypatch.setattr(spaces, "resolve_embed_model_hash", lambda *a: ("abc123", False))
    monkeypatch.setattr(web, "_ping", lambda url: True)
    monkeypatch.setattr(config, "ADMIN_TOKEN", "sekrit")
    with TestClient(app) as c:
        c.headers["X-Admin-Token"] = "sekrit"
        yield c


def _button(page: str, label: str) -> str:
    """The <button ...>label</button> element, whitespace-collapsed."""
    m = re.search(r"<button\b[^>]*>\s*" + re.escape(label) + r"\s*</button>", page)
    assert m, f"no button labelled {label!r} in the page"
    return " ".join(m.group(0).split())


@pytest.mark.parametrize("state", sorted(MODES))
def test_the_current_mode_is_the_selected_one_and_the_others_are_clickable(
        client, state):
    node_state.set_worker_state(state)
    page = client.get("/admin/library").text

    for mode, label in MODES.items():
        btn = _button(page, label)
        if mode == state:
            # steady accent border + label, and not clickable: you cannot
            # switch to where you already are
            assert 'class="mode selected"' in btn, btn
            assert "disabled" in btn, btn
            assert 'aria-current="true"' in btn, btn
        else:
            # a state you can switch TO — plain until hovered (the hover
            # border lives in admin.css, keyed on .mode:not(.selected))
            assert 'class="mode"' in btn, btn
            assert "disabled" not in btn, btn
            assert f"setWorkerState('{mode}')" in btn, btn


def test_exactly_one_button_is_ever_selected(client):
    for state in MODES:
        node_state.set_worker_state(state)
        page = client.get("/admin/library").text
        assert page.count('class="mode selected"') == 1, state


def test_rescan_is_grey_wherever_the_mode_parses_nothing(client):
    """A scan is PARSING, and only "run everything" parses (operator,
    2026-09-19; app/node_state.py: MODE_RUNS). Greyed because it would
    genuinely do nothing: in "download/seeding only" the scan is owed until
    everything runs again, and in the two modes below it the worker does not
    even take the request. A button that silently queues is worse than one
    that says it can't. It was live in "download/seeding only" until then,
    when that mode still scanned what it downloaded."""
    for state in ("paused", "seed-only", "acquire-only"):
        node_state.set_worker_state(state)
        btn = _button(client.get("/admin/library").text, "rescan all collections")
        assert "disabled" in btn, state
        assert "this mode parses nothing" in btn    # the title says why

    node_state.set_worker_state("running")
    btn = _button(client.get("/admin/library").text, "rescan all collections")
    assert "disabled" not in btn


def test_the_page_says_what_the_current_mode_runs(client):
    """The words come from the table the worker reads, so a button cannot
    promise what the node does not do."""
    for state in MODES:
        node_state.set_worker_state(state)
        page = client.get("/admin/library").text
        assert node_state.MODE_SAYS[state].split(":")[0] in page, state
    for mode, label in node_state.MODE_LABELS:
        assert f">{label}</button>" in page, label


def test_the_css_gives_the_three_states_distinct_looks():
    """The template only assigns classes; if these rules go missing the
    page still renders and every button looks the same."""
    css = (config.__file__.rsplit("app", 1)[0] + "app/static/admin.css")
    text = open(css, encoding="utf-8").read()
    # selected: steady accent, no hover response
    assert "button.mode.selected{border-color:var(--brass);color:var(--brass)" in text
    assert "button.mode.selected:hover{border-color:var(--brass);box-shadow:none}" in text
    # clickable: the border lights up on hover
    assert "button.mode:not(.selected):hover{border-color:var(--brass)" in text
    # non-clickable: grey, not a faded copy of an accented button
    assert "button:disabled{color:var(--muted);border-color:var(--line)" in text
