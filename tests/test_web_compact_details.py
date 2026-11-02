"""The verbose text waits behind one closed line (operator, 2026-11-02:
"Compact all the verbose text information as 'details' or another
appropriate label, as it is done for 'Settings' at the bottom of the Ask
tab. A crowded interface is a bad interface").

Each explanation the pages carried in full is inside a CLOSED
`<details class="more">`, one look on every page; what a reader must see at
once stays in sight — the line that says what a section is, the status, the
data, and every warning that asks for an action.
"""
import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import collections as collections_mod, config, db, spaces, users, web
from app.api import app
from app.verify import backend_gate


@pytest.fixture
def client(isolated_config, monkeypatch):
    monkeypatch.setattr(spaces, "resolve_embed_model_hash", lambda *a: ("abc123", False))
    monkeypatch.setattr(web, "_ping", lambda url: True)
    monkeypatch.setattr(config, "ADMIN_TOKEN", "sekrit")
    with TestClient(app) as c:
        c.headers["X-Admin-Token"] = "sekrit"
        yield c


def _where(html: str, text: str) -> list[str]:
    """For every occurrence of `text`: "summary" (the closed line itself),
    "details" (behind it, closed), "open" (inside a details drawn open — a
    defect here) or "visible"."""
    out, start = [], 0
    assert text in html, f"{text!r} is not on the page at all"
    while (i := html.find(text, start)) != -1:
        start = i + 1
        opened, closed = html.rfind("<details", 0, i), html.rfind("</details>", 0, i)
        if opened <= closed:
            out.append("visible")
            continue
        tag = html[opened:html.index(">", opened) + 1]
        assert 'class="more"' in tag or 'class="settings"' in tag, f"{text!r} in another drawer: {tag}"
        if re.search(r"\sopen[\s>=]", tag):
            out.append("open")
        elif html.rfind("<summary>", opened, i) > html.rfind("</summary>", opened, i):
            out.append("summary")
        else:
            out.append("details")
    return out


def behind(html: str, text: str) -> bool:
    return set(_where(html, text)) == {"details"}


def in_sight(html: str, text: str) -> bool:
    return set(_where(html, text)) <= {"visible", "summary"}


def _main(html: str) -> str:
    return html[html.index("<main>"):html.index("</main>")]


# ---- Library ----------------------------------------------------------------

def test_library_explanations_are_behind_a_closed_line(client):
    page = _main(client.get("/admin/library").text)
    # the counts: their age is the line, how they are recounted is behind it
    assert _where(page, "counts: fresh") == ["summary"]
    assert behind(page, "each collection is recounted every 2 min")
    # the offer: what it is in sight, how taking works behind
    assert in_sight(page, "What live neighbours announce and this node does not hold.")
    assert behind(page, "Nothing is fetched that was not chosen here")
    # adding: the one thing to choose in sight, what the node decides and the
    # command lines behind
    assert in_sight(page, "Choose the source and a name on this node.")
    assert behind(page, "the model on its server that runs the")
    assert behind(page, "louise hold collection create &lt;plugin&gt; --recipe FILE")
    assert behind(page, "louise hold collection create annotations --claims FILE")
    assert behind(page, "The node reads from the claims which held collections")
    # the controls stay where they were
    for control in ('onclick="createCollection()"', 'onclick="checkAnnotations()"',
                    'id="nc_plugin"', 'id="nc_name"', 'id="nc_plan"'):
        assert in_sight(page, control), control


def test_a_refusal_keeps_its_reason_in_sight(client, monkeypatch):
    monkeypatch.setattr(collections_mod, "refusals", lambda: (
        collections_mod.Refusal("broken", Path("broken.toml"), "its file names no [encoder]"),))
    page = _main(client.get("/admin/library").text)
    assert in_sight(page, "its file names no [encoder]"), "the reason is the action: never folded"
    assert behind(page, "Its texts and the peers' vectors may still arrive")


def test_warnings_that_ask_for_an_action_stay_in_sight(client):
    con = db.connect(collections_mod.default_collection())
    for i in range(1, 6):
        con.execute("INSERT INTO documents(uid,source,native_id,path,lang,in_scope,rdf_done,fts_done,"
                    "embed_done,error) VALUES(?,?,?,?,?,1,1,1,0,?)",
                    (f"gutenberg:{i}", "gutenberg", str(i), f"/x/{i}", "en", "fts: boom" if i < 3 else None))
    db.set_control(con, backend_gate.GATE_STATE_KEY, json.dumps(
        {"allowed": False, "reason": "probe cosine 0.41 < tau", "ts": 1}))
    page = _main(re.sub(r"\s+", " ", client.get("/admin/library").text))
    for warning in ("2 blocked", "exhausted retries", "clear errors &amp; retry",
                    "embedding paused", "probe cosine 0.41"):
        assert in_sight(page, warning), warning


# ---- System -----------------------------------------------------------------

def test_system_explanations_are_behind_a_closed_line(client):
    page = _main(client.get("/admin/system").text)
    assert behind(page, "Encoders pinned in LocalAI while Louise processes with them")
    assert in_sight(page, "Nothing held now."), "the state of the pins is data"
    assert in_sight(page, "No users: Ask is open to anyone on this network.")
    assert behind(page, "creates the first one, and from then on Ask needs a token")
    users.add("ada")
    page = _main(client.get("/admin/system").text)
    assert in_sight(page, "<td>ada</td>")
    assert behind(page, "louise user add | list | role | expire | reissue | revoke | remove")


def test_the_gpu_panel_keeps_its_badges_and_readings_and_folds_the_prose():
    """The System tab's GPU panel: the badge and the readings in sight, the
    why behind a line that SURVIVES the 5-second refresh (`hx-preserve`)."""
    from app import gpu, gpu_models as gm
    from tests.test_gpu_models import COLLECTIONS, GIB, SYSTEM
    gpu_html = web.TEMPLATES.get_template("_fragments/gpu.html")
    body = gpu_html.render(state="unset", url="", governor={"state": "absent"})
    assert in_sight(body, "GPU governor not running") and in_sight(body, "there is no probe to ask")
    assert behind(body, "87 s against 64 s") and behind(body, "deploy/truenas-app.yaml")
    dev = gpu.GpuDevice(card="card0", pdev="0000:03:00.0", driver="xe", vendor_id="8086",
                        device_id="e223", vendor="Intel", name="Arc Pro B70",
                        vram_total=32 * GIB, vram_used=18 * GIB)
    body = gpu_html.render(state="ok", url="x", devices=(dev,), notes=(),
                           models=gm.panel(COLLECTIONS, get_json=lambda url: SYSTEM), model_bar=gm.bar,
                           governor={"state": "absent"})
    assert in_sight(body, "VRAM 32") and in_sight(body, "<th>model</th><th>used by</th>")
    assert behind(body, "which reads the driver's per-process counters")
    source = (Path(web.__file__).parent / "templates" / "_fragments" / "gpu.html").read_text(encoding="utf-8")
    drawers = re.findall(r"<details[^>]*>", source)
    assert drawers and all(re.search(r'\sid="[^"]+"', d) and "hx-preserve" in d for d in drawers), drawers


# ---- Connect ----------------------------------------------------------------

def test_connect_keeps_the_values_in_sight_and_the_lists_behind(client):
    client.headers.pop("X-Admin-Token")          # a reader on the LAN, before any user exists
    page = _main(client.get("/connect", headers={"host": "louise.lan:30042"}).text)
    for value in ("http://louise.lan:30042/v1", "http://louise.lan:30042/api/ask/mcp", "No key is needed"):
        assert in_sight(page, value), value
    assert in_sight(page, "The models appear by themselves.")
    assert behind(page, "searches everything, and each other one searches one collection")
    assert behind(page, "<code>rerank_depth</code>")


# ---- one look, closed by default --------------------------------------------

@pytest.mark.parametrize("path", ["/admin/library", "/admin/system", "/admin/network", "/connect", "/ask"])
def test_every_drawer_is_closed_and_has_a_line_to_open_it(client, path):
    page = _main(client.get(path).text)
    for tag in re.findall(r"<details[^>]*>", page):
        assert not re.search(r"\sopen[\s>=]", tag), f"{path}: drawn open: {tag}"
    opened = [m.end() for m in re.finditer(r"<details[^>]*>", page)]
    for at in opened:
        assert page[at:].lstrip().startswith("<summary>"), f"{path}: a drawer without its line"


def test_the_drawers_look_like_the_ask_tabs_settings():
    css = (Path(web.__file__).parent / "static" / "admin.css").read_text(encoding="utf-8")
    settings = re.search(r"details\.settings>summary\{([^}]*)\}", css).group(1)
    more = re.search(r"details\.more>summary\{([^}]*)\}", css).group(1)
    assert more == settings, "one look: the Settings line's"
    block = css[css.index("details.more{"):]
    block = block[:block.index("\n\n")] if "\n\n" in block else block
    assert "#" not in block, "colours from the theme's tokens only, so every theme, light or dark"
    templates = Path(web.__file__).parent / "templates"
    for name in ("library.html", "system.html", "connect.html"):
        assert '<details class="more">' in (templates / name).read_text(encoding="utf-8"), name
