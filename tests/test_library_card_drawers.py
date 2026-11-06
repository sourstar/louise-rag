"""A Library card: status in sight, its prose under Details, its buttons
under Options, and what discards work asks first (operator, 2026-11-02:
"Interface changes: library tab, in each model card hide all the
descriptive text in a Details expandable section and all the buttons in a
Options section, keep embed mode and share check box visible; for the
buttons reprocess/rescan/remove add a confirmation step with a warning on
the effect, to prevent accidental clicks").

The page is read as a browser builds it (html.parser), card by card: which
drawer every control sits in, whether each drawer is closed, what each
confirmation says. tests/test_library_confirm_browser.py clicks through
the same dialogs in a real browser.
"""
from __future__ import annotations

import re
from html.parser import HTMLParser

import pytest
from fastapi.testclient import TestClient

from app import collections as collections_mod, config, db, node_state, spaces, web
from app.api import app


@pytest.fixture
def client(isolated_config, monkeypatch):
    monkeypatch.setattr(spaces, "resolve_embed_model_hash", lambda *a: ("abc123", False))
    monkeypatch.setattr(web, "_ping", lambda url: True)
    monkeypatch.setattr(config, "ADMIN_TOKEN", "sekrit")
    node_state.set_worker_state("running")          # a scan parses: rescan is live
    with TestClient(app) as c:
        c.headers["X-Admin-Token"] = "sekrit"
        yield c


class _Cards(HTMLParser):
    """Every held collection's card (`.panel[data-collection]`, not the
    refused ones): its drawers, its controls with the drawer each sits in,
    and its confirmation <template>s with their text."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.cards: dict[str, dict] = {}
        self._card: dict | None = None
        self._depth = 0
        self._drawers: list[dict] = []
        self._summary: dict | None = None
        self._template: dict | None = None
        self._button: dict | None = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if self._card is None:
            if (tag == "div" and "data-collection" in a and "panel" in (a.get("class") or "")
                    and a.get("data-kind") != "refused"):
                self._card = {"name": a["data-collection"], "kind": a.get("data-kind"),
                              "drawers": [], "controls": [], "templates": {}}
                self._depth = 1
            return
        if tag == "div":
            self._depth += 1
        if tag == "details":
            d = {"drawer": a.get("data-drawer"), "class": a.get("class"), "open": "open" in a, "summary": ""}
            self._card["drawers"].append(d)
            self._drawers.append(d)
        elif tag == "summary":
            self._summary = self._drawers[-1]
        elif tag == "template":
            self._template = {"attrs": a, "text": ""}
        elif tag in ("button", "select", "input") and self._template is None:
            ctl = {"tag": tag, "attrs": a, "text": "",
                   "drawer": self._drawers[-1]["drawer"] if self._drawers else None}
            self._card["controls"].append(ctl)
            if tag == "button":
                self._button = ctl

    def handle_endtag(self, tag):
        if self._card is None:
            return
        if tag == "div":
            self._depth -= 1
            if self._depth == 0:
                self.cards[self._card["name"]] = self._card
                self._card = None
        elif tag == "details":
            self._drawers.pop()
        elif tag == "summary":
            self._summary = None
        elif tag == "template":
            t = self._template
            t["text"] = " ".join(t["text"].split())
            self._card["templates"][t["attrs"]["data-confirm"]] = t
            self._template = None
        elif tag == "button":
            self._button = None

    def handle_data(self, data):
        if self._summary is not None:
            self._summary["summary"] += data
        if self._template is not None:
            self._template["text"] += data
        if self._button is not None:
            self._button["text"] += data


def _cards(html: str) -> dict[str, dict]:
    p = _Cards()
    p.feed(html)
    return p.cards


def _buttons(card: dict) -> dict[str, dict]:
    return {" ".join(c["text"].split()): c for c in card["controls"] if c["tag"] == "button"}


def _function(html: str, name: str) -> str:
    """The body of `function name(` in the page's script, up to the next
    top-level function."""
    start = re.search(r"(async )?function " + re.escape(name) + r"\(", html).start()
    nxt = re.search(r"\n(async )?function |\n// ", html[start + 10:])
    return html[start:start + 10 + (nxt.start() if nxt else len(html))]


def _seed(con):
    """Five documents: three indexed, two of them embedded, two blocked."""
    rows = [(1, 1, 1, None), (2, 1, 1, None), (3, 1, 0, None), (4, 0, 0, "fts: boom"), (5, 0, 0, "rdf: bad")]
    for i, fts, emb, err in rows:
        con.execute("INSERT INTO documents(uid,source,native_id,path,lang,in_scope,rdf_done,fts_done,"
                    "embed_done,error) VALUES(?,?,?,?,?,1,1,?,?,?)",
                    (f"gutenberg:{i}", "gutenberg", str(i), f"/x/{i}", "en", fts, emb, err))


# ---- the two closed lines ---------------------------------------------------

def test_every_card_has_a_closed_details_and_a_closed_options_line(client):
    card = _cards(client.get("/admin/library").text)["gutenberg"]
    assert [(d["drawer"], d["summary"].strip()) for d in card["drawers"]] == [
        ("details", "Details"), ("options", "Options")]
    for d in card["drawers"]:
        assert d["class"] == "more", "the one look: details.more, the Ask tab's Settings line"
        assert not d["open"], f"{d['drawer']} is drawn open"


def test_embed_mode_and_share_stay_in_sight_and_every_button_is_under_options(client):
    card = _cards(client.get("/admin/library").text)["gutenberg"]
    select = [c for c in card["controls"] if c["tag"] == "select"]
    share = [c for c in card["controls"] if c["tag"] == "input" and c["attrs"].get("type") == "checkbox"]
    assert len(select) == 1 and "setEmbedMode(" in select[0]["attrs"]["onchange"]
    assert len(share) == 1 and "setShare(" in share[0]["attrs"]["onchange"]
    assert select[0]["drawer"] is None and share[0]["drawer"] is None, "the two controls stay in sight"
    buttons = _buttons(card)
    assert {"grow profile…", "clear errors & retry…", "reprocess lexical index…", "rescan…",
            "export bundle", "import bundle…", "remove…"} <= set(buttons)
    assert {label: b["drawer"] for label, b in buttons.items()} == {label: "options" for label in buttons}


def test_the_prose_is_under_details_and_the_status_in_sight(client):
    con = db.connect(collections_mod.default_collection())
    _seed(con)
    html = client.get("/admin/library").text
    card_html = html[html.index('data-collection="gutenberg"'):]
    card_html = card_html[:card_html.index('data-drawer="options"')]
    details_at = card_html.index('data-drawer="details"')
    in_sight, details = card_html[:details_at], card_html[details_at:]
    for status in ("parsed (rdf)", "indexed (lexical)", "embedded (dense)", "space: ", "Embed mode",
                   ">2 blocked</span>"):
        assert status in in_sight, status
    for prose in ("fetch stage: off", "delta freshness:", "on-demand queue:",
                  "failed a stage or exhausted retries —"):
        assert prose in details and prose not in in_sight, prose


# ---- what discards work asks first ------------------------------------------

def test_reprocess_rescan_clear_and_remove_go_through_a_confirmation(client):
    html = client.get("/admin/library").text
    buttons = _buttons(_cards(html)["gutenberg"])
    assert buttons["clear errors & retry…"]["attrs"]["onclick"] == "confirmAction('errors', 'gutenberg')"
    assert buttons["reprocess lexical index…"]["attrs"]["onclick"] == "confirmAction('reprocess', 'gutenberg')"
    assert buttons["rescan…"]["attrs"]["onclick"] == "confirmAction('rescan', 'gutenberg')"
    assert buttons["remove…"]["attrs"]["onclick"] == "removeCollection('gutenberg', 'origin')"
    # nothing on a card posts from its own click
    for name, card in _cards(html).items():
        for c in card["controls"]:
            on = c["attrs"].get("onclick") or ""
            for direct in ("libReset(", "libDiscover(", "doRemove(", "postJSON(", "rescanAll("):
                assert direct not in on, f"{name}: {c['text'].strip()!r} posts directly: {on}"
    # the top panel's rescan of every collection asks too
    assert re.search(r'<button onclick="confirmAction\(\'rescan-all\'\)"[^>]*>rescan all collections…</button>', html)
    # the confirmation posts only from its labelled button; Cancel has the focus
    confirm = _function(html, "confirmAction")
    assert "showModal(" in confirm and "postJSON" not in confirm
    assert "confirm-go').onclick=()=>{hideModal();CONFIRMED[what](name)}" in confirm
    assert "getElementById('confirm-cancel').focus()" in confirm
    table = html[html.index("const CONFIRMED={"):html.index("};", html.index("const CONFIRMED={"))]
    for what, call in (("errors", "libReset(name,'errors')"), ("reprocess", "libReset(name,'fts')"),
                       ("rescan", "libDiscover(name)"), ("'rescan-all'", "rescanAll()")):
        assert re.search(re.escape(what) + r":\(?\w*\)?=>" + re.escape(call), table), (what, table)
    remove = _function(html, "removeCollection")
    assert "showModal(" in remove and "postJSON" not in remove, "remove… opens its dialog and posts nothing"
    assert "getElementById('confirm-cancel').focus()" in remove
    assert "postJSON('/admin/library/'+name+'/remove'" in _function(html, "doRemove")


def test_each_warning_names_the_effect_and_the_cards_numbers(client):
    _seed(db.connect(collections_mod.default_collection()))
    html = client.get("/admin/library").text
    t = _cards(html)["gutenberg"]["templates"]
    assert set(t) == {"errors", "reprocess", "rescan", "remove"}
    assert t["reprocess"]["attrs"]["data-label"] == "Reprocess gutenberg"
    assert "The lexical index is rebuilt from the start: the 3 documents indexed now are chunked and indexed again" \
        in t["reprocess"]["text"]
    assert "The vectors are kept: nothing is embedded again." in t["reprocess"]["text"]
    assert "about as long as indexing them took the first time" in t["reprocess"]["text"], "no rate measured yet"
    assert t["rescan"]["attrs"]["data-label"] == "Rescan gutenberg"
    assert "Every file of its corpus folder is examined again, none skipped" in t["rescan"]["text"]
    assert "(5 documents known now)" in t["rescan"]["text"]
    assert "parsed, indexed and embedded again" in t["rescan"]["text"]
    assert "the page waits until the scan is done" in t["rescan"]["text"]
    assert t["errors"]["attrs"]["data-label"] == "Clear errors on gutenberg"
    assert "The 2 blocked documents lose the error recorded on them" in t["errors"]["text"]
    assert "the errors stay in the System log" in t["errors"]["text"]
    assert t["remove"]["text"] == "It holds 5 documents: 3 indexed, 2 embedded."
    remove = _function(html, "removeCollection")
    for effect in ("<b>It cannot be undone</b>", "deletes this node's copy now",
                   "Nothing on disk is deleted", "Remove ${esc(name)}</button>", "held.innerHTML"):
        assert effect in remove, effect
    top = html[html.index('<template data-confirm="rescan-all"'):]
    top = " ".join(top[:top.index("</template>")].split())
    assert 'data-label="Rescan all collections"' in top and "Rescan all 1 collection?" in top


def test_the_reprocess_warning_quotes_the_time_at_the_measured_rate(isolated_config):
    shelf = web._collection_shelf(collections_mod.default_collection(), {}, "running")
    shelf.update(fts_done=30_000, stage_rates={**shelf["stage_rates"], "fts": 10_000})
    html = web.TEMPLATES.env.get_template("library.html").render(
        request=None, active="library", shelves=[shelf], counts_age=0, plugins=[],
        plugin_profiles={}, plugin_extras={}, corpus_mounts=[], corpus_mounts_dir="", worker_state="running")
    text = _cards(html)["gutenberg"]["templates"]["reprocess"]["text"]
    assert "the 30.000 documents indexed now" in text
    assert "At the indexing rate measured here (10.000 documents an hour) that takes ~3h." in text


def test_a_copy_taken_from_peers_defaults_its_removal_to_forget(isolated_config):
    """The shelf carries `source`, so the dialog's default matches the
    route's: a SHARD copy is forgotten, one built here retired. Without it
    every card called itself "built here from a corpus"."""
    shelf = web._collection_shelf(collections_mod.default_collection(), {}, "running")
    assert shelf["source"] == "origin"
    shelf["source"] = "shard"
    html = web.TEMPLATES.env.get_template("library.html").render(
        request=None, active="library", shelves=[shelf], counts_age=0, plugins=[],
        plugin_profiles={}, plugin_extras={}, corpus_mounts=[], corpus_mounts_dir="", worker_state="running")
    assert _buttons(_cards(html)["gutenberg"])["remove…"]["attrs"]["onclick"] == "removeCollection('gutenberg', 'shard')"


def test_a_cards_rescan_stays_grey_where_the_mode_parses_nothing(client):
    node_state.set_worker_state("paused")
    rescan = _buttons(_cards(client.get("/admin/library").text)["gutenberg"])["rescan…"]
    assert "disabled" in rescan["attrs"] and "parses nothing" in rescan["attrs"]["title"]


def test_an_annotations_card_has_the_same_two_lines(client, monkeypatch):
    from tests.test_web_collections import _an_annotations_collection
    _an_annotations_collection(monkeypatch)
    card = _cards(client.get("/admin/library").text)["pubmed-same-work"]
    assert card["kind"] == "annotations"
    assert [(d["drawer"], d["summary"].strip(), d["open"]) for d in card["drawers"]] == [
        ("details", "Details", False), ("options", "Options", False)]
    share = [c for c in card["controls"] if c["attrs"].get("type") == "checkbox"]
    assert len(share) == 1 and share[0]["drawer"] is None
    others = [c for c in card["controls"] if c not in share]
    assert others and all(c["drawer"] == "options" for c in others), others
    assert _buttons(card)["remove…"]["attrs"]["onclick"] == "removeCollection('pubmed-same-work', 'shard')"
    assert card["templates"]["remove"]["text"] == "It holds 2 claims in snapshot 2026-09."
