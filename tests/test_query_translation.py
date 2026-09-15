"""Query translation: into the largest language groups first, a bounded
number of them, by a chat model that stays loaded between searches.

Measured 2026-09-15 on gutenberg: 19.2 s of a 24.4 s search was the lexical
leg with translation, 0.6 s without. Ollama unloads a model after five idle
minutes and searches come less often, so nearly every search reloaded
qwen3:8b first; and the 12-language cap kept the alphabetically first
groups (ar, ca, da, …), not the largest.
"""
from app import collections as collections_mod, config, db, fts, llm, retrieval


def test_the_chat_call_asks_ollama_to_keep_the_model_loaded(monkeypatch):
    sent = {}

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"message": {"content": "ok"}}

    class FakeClient:
        def __init__(self, timeout=None):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def post(self, url, json=None):
            sent.update(json)
            return FakeResponse()
    monkeypatch.setattr(llm.httpx, "Client", FakeClient)
    assert llm.chat([{"role": "user", "content": "hi"}]) == "ok"
    assert sent["keep_alive"] == config.LLM_KEEP_ALIVE == "30m"


def test_translation_goes_to_the_largest_groups_first(isolated_config, monkeypatch):
    c = collections_mod.default_collection()
    sizes = {"ar": 5, "de": 2464, "el": 216, "en": 63024, "es": 898, "fi": 3686,
             "fr": 4205, "it": 1110, "nl": 1109, "other": 9000}
    monkeypatch.setattr(fts, "existing_groups", lambda collection=None: sorted(sizes))
    monkeypatch.setattr(fts, "group_sizes", lambda collection=None: sizes)
    monkeypatch.setattr(fts, "search", lambda group, q, limit=20, collection=None: [])
    asked = []
    monkeypatch.setattr(llm, "translate_query", lambda q, langs: asked.append(langs) or {})
    monkeypatch.setattr(config, "TRANSLATE_MAX_LANGS", 4)

    retrieval._search_one_collection(db.connect(c), c, "a whale hunt", 5, "fts", True, set())
    assert asked == [["fr", "fi", "de", "it"]], "largest first, English and 'other' never"


def test_group_sizes_counts_each_groups_own_index(isolated_config):
    c = collections_mod.default_collection()

    def chunk(n, text):
        return [{"id": f"gutenberg:{n}:0:1", "book_id": n, "seq": 0, "chapter": "",
                 "text": text, "tokens": 2}]
    fts.index_book("fr", 1, chunk(1, "une chasse à la baleine"), c)
    fts.index_book("fr", 2, chunk(2, "la mer et le vent"), c)
    fts.index_book("de", 3, chunk(3, "eine Waljagd auf See"), c)
    sizes = fts.group_sizes(c)
    assert sizes.get("fr") == 2 and sizes.get("de") == 1, sizes
