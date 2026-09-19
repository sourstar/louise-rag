"""The node's translation model (app/translate.py): TRANSLATE_URL, MADLAD-400
on the GPU (deploy/madlad-translate). Measured on the node, 2026-09-18: the
query into 51 languages in 1.5 s, one whole entry in 5.9 s — against 9.2 s
for 19 languages and 58–84 s per entry by the chat model, which stays the
fallback."""
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app import collections as collections_mod, config, fts, language_plan, llm, spaces
from app import translate as mt
from app import web
from app.api import app


@pytest.fixture
def client(isolated_config, monkeypatch):
    monkeypatch.setattr(spaces, "resolve_embed_model_hash", lambda *a: ("abc123", False))
    monkeypatch.setattr(web, "_ping", lambda url: True)
    web._translation_cache.clear()
    with TestClient(app) as c:
        yield c
    web._translation_cache.clear()


class _Resp:
    def __init__(self, data, status=200):
        self._data, self.status_code = data, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("bad", request=None, response=None)

    def json(self):
        return self._data


def _server(monkeypatch, known=("de", "en", "fr", "it", "la", "eo", "ja", "zh", "ko"),
            fail=False, calls=None):
    """A stand-in translation server: `<code>:text`, null for an unknown code."""
    monkeypatch.setattr(config, "TRANSLATE_URL", "http://gpu:30448")

    def get(url, timeout=None):
        return _Resp({"languages": list(known)})

    def post(url, json=None, timeout=None, headers=None):
        if calls is not None:
            calls.append([(it["text"], it["to"]) for it in json["items"]])
        if fail:
            raise httpx.ConnectError("refused")
        return _Resp({"translations": [f"{it['to']}:{it['text']}" if it["to"] in known
                                       else None for it in json["items"]]})
    monkeypatch.setattr(mt.httpx, "get", get)
    monkeypatch.setattr(mt.httpx, "post", post)


def test_unset_means_no_translation_model(monkeypatch):
    monkeypatch.setattr(config, "TRANSLATE_URL", "")
    assert not mt.available() and mt.languages() == frozenset() and mt.translate([("a", "de")]) == []


def test_a_failed_call_is_empty_never_the_input(monkeypatch):
    _server(monkeypatch, fail=True)
    assert mt.translate([("a", "de")]) == []
    _server(monkeypatch)
    assert mt.translate([("a", "de"), ("b", "xx")]) == ["de:a", None]


def test_the_mixed_groups_name_their_languages(isolated_config):
    c = collections_mod.default_collection()

    def chunk(n):
        return [{"id": f"gutenberg:{n}:0:1", "book_id": n, "seq": 0, "chapter": "",
                 "text": "balaena", "tokens": 1}]
    for n, lang in ((1, "la"), (2, "eo"), (3, "la"), (4, "und")):
        fts.index_book(lang, n, chunk(n), c)
    fts.index_book("de", 5, chunk(5), c)
    assert fts.group_languages("other", c) == ["eo", "la"], "und names no language"
    assert fts.group_languages("de", c) == ["de"]


def test_a_standard_search_goes_out_in_the_main_languages_only(isolated_config, monkeypatch):
    """Operator, 2026-09-18: "by default use only the translations in the
    main languages" — the tail of "other" only when asked."""
    c = collections_mod.default_collection()
    monkeypatch.setattr(fts, "existing_groups", lambda collection=None: ["de", "other"])
    monkeypatch.setattr(fts, "group_languages",
                        lambda g, collection=None: ["eo", "la"] if g == "other" else [g])
    monkeypatch.setattr(llm, "language_restriction", lambda q: ([], q))
    calls = []
    _server(monkeypatch, calls=calls)
    plan = language_plan.plan("balena", [c], True)
    assert calls == [[("balena", "de")]] and not plan.every
    assert plan.variants("other", ["eo", "la"]) == ["balena"], "other, as typed"
    calls.clear()
    plan = language_plan.plan("balena lang:all", [c], True)
    assert calls == [[("balena", t) for t in ("de", "eo", "la")]]
    assert plan.every and plan.core == "balena", "`lang:all` asks, and is not searched for"


def test_asked_for_every_language_it_goes_out_in_one_call(isolated_config, monkeypatch):
    """The tail of "other" too, by the translation model."""
    c = collections_mod.default_collection()
    monkeypatch.setattr(fts, "existing_groups", lambda collection=None: ["de", "other"])
    monkeypatch.setattr(fts, "group_languages",
                        lambda g, collection=None: ["eo", "la", "fur"] if g == "other" else [g])
    monkeypatch.setattr(llm, "language_restriction", lambda q: ([], q))
    monkeypatch.setattr(llm, "translate_query",
                        lambda q, langs: (_ for _ in ()).throw(AssertionError("chat asked")))
    calls = []
    _server(monkeypatch, calls=calls)
    plan = language_plan.plan("caccia alla balena", [c], True, every=True)
    assert calls == [[("caccia alla balena", t) for t in ("de", "eo", "la")]], \
        "one call, every language it knows; fur it does not know is not asked"
    assert plan.by == "translation model"
    assert plan.variants("other", ["eo", "la", "fur"]) == \
        ["eo:caccia alla balena", "la:caccia alla balena", "caccia alla balena"], \
        "each of other's languages, and as typed for the one no translation reached"


def test_a_translation_model_that_fails_hands_back_to_the_chat_model(isolated_config,
                                                                      monkeypatch):
    c = collections_mod.default_collection()
    monkeypatch.setattr(fts, "existing_groups", lambda collection=None: ["de", "other"])
    monkeypatch.setattr(fts, "group_languages",
                        lambda g, collection=None: ["la"] if g == "other" else [g])
    monkeypatch.setattr(llm, "language_restriction", lambda q: ([], q))
    asked = []
    monkeypatch.setattr(llm, "translate_query",
                        lambda q, langs: asked.append(langs) or {l: f"chat:{q}" for l in langs})
    _server(monkeypatch, fail=True)
    plan = language_plan.plan("balena", [c], True)
    assert asked == [["de"]], "the main languages, by the chat model"
    assert plan.by == "chat model" and plan.status == "ok"
    asked.clear()
    language_plan.plan("balena", [c], True, every=True)
    assert asked == [["de", "la"]], "asked for all of them, the chat model does them all"


def _stream(client, body):
    r = client.post("/ask/translate/stream", json=body)
    assert r.status_code == 200
    return [json.loads(ln) for ln in r.text.splitlines() if ln.strip()]


def test_the_page_is_translated_in_rounds_it_can_watch(client, monkeypatch):
    """Short items in one call, the first long one alone (the answer), then
    the entries three at a time — never the chat model."""
    calls = []
    _server(monkeypatch, calls=calls)
    monkeypatch.setattr(llm, "translate_stream",
                        lambda *a: (_ for _ in ()).throw(AssertionError("chat asked")))
    monkeypatch.setattr(llm, "translate_texts",
                        lambda *a: (_ for _ in ()).throw(AssertionError("chat asked")))
    long = lambda n: f"entry {n} " + "word " * 100  # noqa: E731
    items = [{"id": "__answer__", "text": long(0)}, {"id": "t", "text": "Moby Dick"}] + \
            [{"id": f"e{n}", "text": long(n)} for n in range(1, 6)]
    lines = _stream(client, {"lang": "it", "items": items})
    assert [len(c) for c in calls] == [1, 1, 3, 2]
    assert calls[1][0][0].startswith("entry 0"), "the answer first among the long ones"
    assert [m["id"] for m in lines if "text" in m] == \
        ["t", "__answer__", "e1", "e2", "e3", "e4", "e5"]
    assert lines[-1] == {"done": True, "failed": False, "missed": []}


def test_a_name_that_stays_a_name_is_the_models_answer(client, monkeypatch):
    """The chat model's copy-through was a miss; a deterministic model that
    returns "Moby Dick" for "Moby Dick" has answered."""
    monkeypatch.setattr(config, "TRANSLATE_URL", "http://gpu:30448")
    monkeypatch.setattr(mt, "translate", lambda items: [t for t, _ in items])
    d = client.post("/ask/translate", json={"lang": "it", "items": [
        {"id": "a", "text": "Moby Dick"}]}).json()
    assert d["items"] == {"a": "Moby Dick"} and d["missed"] == []


def test_the_translation_model_down_mid_page_hands_the_rest_to_the_chat_model(client,
                                                                              monkeypatch):
    monkeypatch.setattr(config, "TRANSLATE_URL", "http://gpu:30448")
    rounds = []

    def flaky(items):
        rounds.append(len(items))
        return [f"it:{t}" for t, _ in items] if len(rounds) == 1 else []
    monkeypatch.setattr(mt, "translate", flaky)
    monkeypatch.setattr(llm, "translate_stream", lambda text, lang: iter([f"chat:{text}"]))
    lines = _stream(client, {"lang": "it", "items": [
        {"id": "t", "text": "a title"}, {"id": "e", "text": "entry " * 100}]})
    texts = {m["id"]: m["text"] for m in lines if "text" in m}
    assert texts["t"] == "it:a title"
    assert texts["e"].startswith("chat:entry"), "the entry still translated, by the chat model"


def test_the_system_page_names_and_pings_the_translation_model(isolated_config, monkeypatch):
    monkeypatch.setattr(config, "TRANSLATE_URL", "http://gpu:30448")
    monkeypatch.setattr(web, "_ping", lambda url: True)
    assert web._effective_config()["tier1"]["TRANSLATE_URL"] == "http://gpu:30448"
    assert any("TRANSLATE_URL" in h["names"] and h["url"] == "http://gpu:30448/health"
               for h in web._health_pings())
    monkeypatch.setattr(config, "TRANSLATE_URL", "")
    assert "chat model translates" in web._effective_config()["tier1"]["TRANSLATE_URL"]


def test_each_mixed_group_is_searched_in_every_language_it_holds(isolated_config, monkeypatch):
    from app import db, retrieval
    c = collections_mod.default_collection()
    monkeypatch.setattr(fts, "existing_groups", lambda collection=None: ["other"])
    monkeypatch.setattr(fts, "group_languages",
                        lambda g, collection=None: ["eo", "la"] if g == "other" else [g])
    ran = []
    monkeypatch.setattr(fts, "search", lambda group, q, limit=20, collection=None, langs=None:
                        ran.append(q) or [])
    monkeypatch.setattr(llm, "language_restriction", lambda q: ([], q))
    _server(monkeypatch)
    retrieval._search_one_collection(db.connect(c), c, "balena lang:all", 5, "fts", True,
                                     set())
    assert sorted(ran) == ["eo:balena", "la:balena"]
