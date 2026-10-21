"""The Ask page draws the plates a result carries (05 §5.3): until
2026-10-14 `figures` reached API callers only. A held plate is an <img>
on `GET /api/ask/figure`; every other picture the text mentions is a link
to the origin's page that shows it (operator, 2026-10-21: "either the
link or the picture shall be present")."""
from types import SimpleNamespace

from app import web
from app.ask import results as ask_results
from tests.test_web_pages import client  # noqa: F401

H = "blake2b:" + "ab" * 16


def _page(client, monkeypatch, place, *, record_line=2, cls="green",  # noqa: F811
          text="[Illustration: The whale] …"):
    from app.ask import annotations as annotation_facts
    hit = {"chunk_id": "gutenberg:2701:4:1", "book_id": 2701, "lang": "en", "title": "Moby Dick",
           "text": text, "source_collection": "gutenberg"}
    codex = SimpleNamespace(collection_id="cid-gutenberg", record_line=record_line,
                            license=SimpleNamespace(cls=cls))
    coll = SimpleNamespace(name="gutenberg", plugin="gutenberg", codex=codex)
    monkeypatch.setattr(web.collections_mod, "load_all", lambda *a, **k: [coll])
    monkeypatch.setattr(web.query_service, "run_search",
                        lambda *a, **k: {"hits": [hit], "timings": {}, "facts": [], "catalog": []})
    monkeypatch.setattr(annotation_facts, "attach", lambda *a, **k: [])
    monkeypatch.setattr(ask_results, "_place_figures", place)
    monkeypatch.setattr(web.db, "connect", lambda *a, **k: None)
    monkeypatch.setattr(web.db, "log_query", lambda *a, **k: None)
    return client.post("/ask/query", json={"query": "the whale"}).text


def _placing(*figures):
    seen = {}

    def place(lists, source_of, contexts, root):
        seen["contexts"] = contexts
        for h in lists[0]["hits"]:
            if id(h) in source_of:
                assert h["uid"] == "gutenberg:2701" and h["figures"] == []
                h["figures"] = list(figures)
    return place, seen


def test_a_held_plate_is_drawn_from_the_figure_route(client, monkeypatch):  # noqa: F811
    place, seen = _placing({"hash": H, "media_type": "image/jpeg", "caption": "The whale", "state": "held"})
    html = _page(client, monkeypatch, place)
    assert seen["contexts"]["gutenberg"]["red"] is False
    src = f"/api/ask/figure?collection=gutenberg&amp;uid=gutenberg%3A2701&amp;hash=blake2b%3A{'ab' * 16}"
    assert f'<img src="{src}"' in html and f'href="{src}"' in html
    assert "<figcaption" in html and "The whale" in html


def test_a_plate_not_held_or_not_carried_is_linked_at_the_source(client, monkeypatch):  # noqa: F811
    place, _ = _placing({"hash": H, "media_type": "image/png", "caption": "Map", "state": "not-held"},
                        {"hash": H, "media_type": "image/png", "caption": "", "state": "not-carried"})
    html = _page(client, monkeypatch, place, text="[Illustration: Map] and [Illustration] …")
    pics = "https://www.gutenberg.org/cache/epub/2701/pg2701-images.html"
    assert html.count(f'href="{pics}"') == 2, "each mention links the illustrated edition"
    assert "Map — at the source" in html and "illustration — at the source" in html
    assert "this node has not fetched it yet" in html and "this collection does not carry it" in html
    assert "/api/ask/figure" not in html


def test_a_mention_with_no_plates_in_the_records_is_linked_too(client, monkeypatch):  # noqa: F811
    """A collection whose records name no plates (`record_line` 1): the text
    still says "[Illustration: …]", so the reader is sent where it is."""
    html = _page(client, monkeypatch, lambda *a, **k: None, record_line=1)
    assert 'href="https://www.gutenberg.org/cache/epub/2701/pg2701-images.html"' in html
    assert "The whale — at the source" in html


def test_no_plates_asked_of_a_record_line_1_or_red_collection(client, monkeypatch):  # noqa: F811
    called = []
    for kw in ({"record_line": 1}, {"cls": "red"}):
        html = _page(client, monkeypatch, lambda *a, **k: called.append(1), **kw)
        assert "Moby Dick" in html and "<img" not in html and "/api/ask/figure" not in html
    assert called == [], "a red collection's plates are refused by the route (04 §4.4): never drawn"


def test_plates_that_cannot_be_placed_are_said_never_fatal(client, monkeypatch):  # noqa: F811
    def place(*a, **k):
        raise RuntimeError("container unreadable")
    html = _page(client, monkeypatch, place)
    assert "Moby Dick" in html
    assert "figures not placed: RuntimeError: container unreadable" in html
