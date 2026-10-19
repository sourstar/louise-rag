"""The Ask page's side of one hit per work (05 §5.3, amended 2026-10-09):
its "show results in" picker is the first language a work is shown in, the
answer's search gets the same preference (so its [n] are the page's hits),
and a hit lists the work's other editions under it."""
from app import api, web
from tests.test_web_pages import client  # noqa: F401


def test_the_page_sends_its_picker_and_the_search_receives_it(client, monkeypatch):  # noqa: F811
    page = client.get("/ask").text
    assert "show_in:selectedLang()" in page
    seen = {}

    def fake_run_search(names, query, k, mode, translate, verify=False, rerank_depth=50,
                        all_languages=False, show_in=None):
        seen["show_in"] = show_in
        return {"hits": [], "timings": {}, "facts": [], "catalog": []}
    monkeypatch.setattr(web.query_service, "run_search", fake_run_search)
    client.post("/ask/query", json={"query": "q", "show_in": "fr"})
    assert seen == {"show_in": "fr"}
    client.post("/ask/query", json={"query": "q", "show_in": None})
    assert seen == {"show_in": None}, "'their own language': no preference sent"


def test_the_answer_s_search_prefers_the_answer_s_language(client, monkeypatch):  # noqa: F811
    seen = {}

    def fake_run_search(names, query, k, mode, translate, verify=False, rerank_depth=50,
                        all_languages=False, show_in=None):
        seen["show_in"] = show_in
        return {"hits": [], "timings": {"total": 1.0}, "facts": [], "catalog": []}
    monkeypatch.setattr(api, "_run_search", fake_run_search)
    monkeypatch.setattr(api.llm, "chat", lambda *a, **k: "no sources")
    client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "q"}],
                                              "answer_lang": "de"})
    assert seen == {"show_in": "de"}


def test_a_hit_lists_its_work_s_other_editions(client, monkeypatch):  # noqa: F811
    hit = {"chunk_id": "gutenberg:3:0:1", "book_id": 3, "lang": "fr", "title": "Don Quichotte",
           "text": "…", "source_collection": "gutenberg", "editions": [
               {"uid": "gutenberg:2", "title": "Don Quixote", "lang": "en", "held": True,
                "rank": 1, "chunk_id": "gutenberg:2:4:1"},
               {"uid": "gutenberg:4", "title": "Don Quijote II", "lang": "es", "held": False,
                "rank": None, "chunk_id": None}]}
    monkeypatch.setattr(web.query_service, "run_search",
                        lambda *a, **k: {"hits": [hit], "timings": {}, "facts": [], "catalog": []})
    html = client.post("/ask/query", json={"query": "q"}).text
    assert "also in:" in html
    assert '/read/gutenberg/2?hl=gutenberg:2:4:1' in html and "Don Quixote" in html
    assert "Don Quijote II" in html and "/read/gutenberg/4" not in html, "not held: named, not linked"


def _page_with_annotations(client, monkeypatch, attach):  # noqa: F811
    from types import SimpleNamespace
    from app.ask import annotations as annotation_facts
    hit = {"chunk_id": "gutenberg:6150:3:1", "book_id": 6150, "lang": "en", "title": "The Iliad",
           "text": "…", "source_collection": "gutenberg"}
    coll = SimpleNamespace(name="gutenberg", plugin="gutenberg",
                           codex=SimpleNamespace(collection_id="cid-gutenberg"))
    monkeypatch.setattr(web.collections_mod, "load_all", lambda *a, **k: [coll])
    monkeypatch.setattr(web.query_service, "run_search",
                        lambda *a, **k: {"hits": [hit], "timings": {}, "facts": [], "catalog": []})
    monkeypatch.setattr(annotation_facts, "attach", attach)
    monkeypatch.setattr(web.db, "connect", lambda *a, **k: None)     # the query log
    monkeypatch.setattr(web.db, "log_query", lambda *a, **k: None)
    return client.post("/ask/query", json={"query": "the wrath of Achilles"}).text


def test_the_page_attaches_annotations_beside_the_hit(client, monkeypatch):  # noqa: F811
    """05 §5.3: found 2026-10-10, the Ask page never called `attach` — only
    the API path carried `annotations`."""
    seen = {}

    def attach(lists, source_of, contexts, root):
        seen["contexts"] = contexts
        for h in lists[0]["hits"]:
            assert id(h) in source_of and h["uid"] == "gutenberg:6150"
            h["annotations"] = [{
                "claim": "annotations:7", "type": "same-work", "relation": "annotations:same-work",
                "state": "current",
                "record": {"uid": "gutenberg:36248", "title": "Ἰλιάς", "lang": "el", "held": True},
                "method": {"kind": "matcher", "id": "wikipedia-sitelinks", "version": 1}}]
        return []
    html = _page_with_annotations(client, monkeypatch, attach)
    assert seen["contexts"] == {"gutenberg": {"collection_id": "cid-gutenberg", "snapshot_id": None}}
    assert "also:" in html and "Ἰλιάς" in html and "/read/gutenberg/36248" in html
    assert "same-work" in html and "matched by wikipedia-sitelinks" in html


def test_annotations_that_cannot_be_attached_are_said_never_fatal(client, monkeypatch):  # noqa: F811
    def attach(lists, source_of, contexts, root):
        raise RuntimeError("claim index unreadable")
    html = _page_with_annotations(client, monkeypatch, attach)
    assert "The Iliad" in html, "the hit is still shown"
    assert "annotations not attached: RuntimeError: claim index unreadable" in html


# ---- which annotations collections a search consults (05 §5.3, amended 2026-10-19) ----

def _two_annotations(monkeypatch):
    from types import SimpleNamespace
    held = [SimpleNamespace(name=n, title=t, codex=SimpleNamespace(
                descriptive=SimpleNamespace(title=t),
                annotates=SimpleNamespace(collection_id="cid-gutenberg", from_snapshot="2026-09")))
            for n, t in (("gutenberg-book-editions", "Editions"), ("gutenberg-same-work", "Same work"))]
    monkeypatch.setattr(web.collections_mod, "load_annotations", lambda *a, **k: held)
    monkeypatch.setattr(web.query_service.collections_mod, "load_annotations", lambda *a, **k: held)
    return held


def test_the_page_offers_each_annotations_collection_as_a_chip_and_remembers_the_scope(client, monkeypatch):  # noqa: F811
    """An annotations collection is not a search leg (invariant 4), so it was
    not selectable; the operator (2026-10-19): selectable, to affect the
    search or not, and the last selection kept. On, its work claims group
    editions and its claims are shown; off, it is not consulted."""
    from types import SimpleNamespace
    _two_annotations(monkeypatch)
    coll = SimpleNamespace(name="gutenberg", plugin="gutenberg",
                           codex=SimpleNamespace(collection_id="cid-gutenberg", plugin="gutenberg"))
    monkeypatch.setattr(web.collections_mod, "load_all", lambda *a, **k: [coll])
    page = client.get("/ask").text
    assert 'id="annotation-chips"' in page and "Annotations consulted" in page
    assert 'class="chip ann on" data-name="gutenberg-book-editions"' in page
    assert "→ gutenberg" in page, "the chip says what the collection is about"
    assert "annotations:scopeAnnotations()" in page, "both the search and the answer send the choice"
    assert "localStorage.setItem('ask.scope'" in page and "annotations_off" in page


def test_the_choice_reaches_the_search_and_the_facts(client, monkeypatch):  # noqa: F811
    """The chosen names go to `run_search` (the editions grouping) and, resolved,
    to `attach` (the claims beside the hits); None sends nothing, as before,
    so every one held is consulted."""
    from app.ask import annotations as annotation_facts
    held = _two_annotations(monkeypatch)
    seen = {}

    def fake_run_search(names, query, k, mode, translate, verify=False, rerank_depth=50,
                        all_languages=False, show_in=None, **kw):
        seen["run"] = kw
        return {"hits": [{"chunk_id": "gutenberg:6150:3:1", "book_id": 6150, "lang": "en",
                          "title": "The Iliad", "text": "…", "source_collection": "gutenberg"}],
                "timings": {}, "facts": [], "catalog": []}

    def attach(lists, source_of, contexts, root, **kw):
        seen["attach"] = kw
        return []
    from types import SimpleNamespace
    coll = SimpleNamespace(name="gutenberg", plugin="gutenberg", codex=SimpleNamespace(collection_id="cid-gutenberg"))
    monkeypatch.setattr(web.collections_mod, "load_all", lambda *a, **k: [coll])
    monkeypatch.setattr(web.query_service, "run_search", fake_run_search)
    monkeypatch.setattr(annotation_facts, "attach", attach)
    monkeypatch.setattr(web.db, "connect", lambda *a, **k: None)
    monkeypatch.setattr(web.db, "log_query", lambda *a, **k: None)
    client.post("/ask/query", json={"query": "q"})
    assert seen == {"run": {}, "attach": {}}, "no choice: every one held, exactly as before"
    client.post("/ask/query", json={"query": "q", "annotations": ["gutenberg-same-work"]})
    assert seen["run"] == {"annotations": ["gutenberg-same-work"]}
    assert seen["attach"] == {"collections": [held[1]]}
    client.post("/ask/query", json={"query": "q", "annotations": []})
    assert seen["run"] == {"annotations": []} and seen["attach"] == {"collections": []}, "none consulted"
    r = client.post("/ask/query", json={"query": "q", "annotations": ["nope"]})
    assert r.status_code == 400 and "unknown annotations collection" in r.text


def test_run_search_resolves_the_choice_once_and_hands_it_to_the_grouping(monkeypatch, isolated_config):
    """`query_service.run_search` → `retrieval.search` → `works.one_hit_per_work`:
    the resolved collections, so a name is checked once and the grouping
    reads only those; None stays None (every one held)."""
    from app import query_service, retrieval
    from app.ask import works
    held = _two_annotations(monkeypatch)
    seen = {}

    def fake_search(con, query, **kw):
        seen["search"] = kw.get("annotations", "absent")
        return {"hits": [], "timings": {}}
    monkeypatch.setattr(retrieval, "search", fake_search)
    query_service.run_search(None, "q", 5, "fts", False, annotations=["gutenberg-book-editions"])
    assert seen["search"] == [held[0]]
    query_service.run_search(None, "q", 5, "fts", False)
    assert seen["search"] == "absent"
    # and the grouping is given exactly that list
    grouped = {}
    monkeypatch.setattr(works, "one_hit_per_work",
                        lambda hits, collection, prefer, annotations=None: grouped.setdefault("a", annotations) or hits)
    from app.language_plan import LanguagePlan
    plan = LanguagePlan.__new__(LanguagePlan)
    plan.query_lang = "en"
    retrieval._one_hit_per_work([], None, plan, None, [held[1]])
    assert grouped["a"] == [held[1]]
    with __import__("pytest").raises(Exception, match="unknown annotations"):
        query_service.resolve_annotations(["nope"])
