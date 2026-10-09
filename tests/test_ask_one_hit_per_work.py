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
