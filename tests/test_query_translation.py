"""Query translation: into the largest language groups first, a bounded
number of them, by a chat model that stays loaded between searches.

Measured 2026-09-15 on gutenberg: 19.2 s of a 24.4 s search was the lexical
leg with translation, 0.6 s without. Ollama unloads a model after five idle
minutes and searches come less often, so nearly every search reloaded
qwen3:8b first; and the 12-language cap kept the alphabetically first
groups (ar, ca, da, …), not the largest.
"""
import dataclasses

from app import catalog, collections as collections_mod, config, db, fts, llm, retrieval


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

        def post(self, url, json=None, headers=None):
            sent.update(json)
            return FakeResponse()
    monkeypatch.setattr(llm.httpx, "Client", FakeClient)
    assert llm.chat([{"role": "user", "content": "hi"}]) == "ok"
    assert sent["keep_alive"] == config.LLM_KEEP_ALIVE == "30m"


def test_the_query_goes_out_in_every_language_the_indexes_hold(isolated_config, monkeypatch):
    """Operator, 2026-09-18: "the search is always made across all the
    languages". It went to the six largest groups and NEVER English, so an
    Italian query ran untranslated against gutenberg's English index and
    all of PubMed (live: "Libro della caccia", "Balena's method")."""
    c = collections_mod.default_collection()
    groups = ["ar", "cjk", "de", "en", "fr", "it", "other"]
    monkeypatch.setattr(fts, "existing_groups", lambda collection=None: groups)
    ran = []
    monkeypatch.setattr(fts, "search", lambda group, q, limit=20, collection=None, langs=None:
                        ran.append((group, q, langs)) or [])
    asked = []
    monkeypatch.setattr(llm, "language_restriction", lambda q: ([], q))
    monkeypatch.setattr(llm, "translate_query", lambda q, langs: asked.append(langs) or
                        {l: f"{l}:{q}" for l in langs})

    retrieval._search_one_collection(db.connect(c), c, "caccia alla balena", 5, "fts",
                                     True, set())
    # Italian is the query's own language (guessed, the reading naming none):
    # searched as typed, never translated into (05 §5.2, amended 2026-10-10)
    assert asked == [["ar", "de", "en", "fr", "ja", "ko", "zh"]], \
        "English included, CJK as its three languages, 'other' not a language"
    assert ("en", "en:caccia alla balena", None) in ran
    assert ("it", "caccia alla balena", None) in ran
    assert sorted(q for g, q, _ in ran if g == "cjk") == \
        ["ja:caccia alla balena", "ko:caccia alla balena", "zh:caccia alla balena"]
    assert [q for g, q, _ in ran if g == "other"] == ["caccia alla balena"], \
        "the catch-all is searched with the query as typed"


def test_a_single_language_collection_is_translated_too(isolated_config, monkeypatch):
    """PubMed holds one group, and `len(groups) > 1` skipped translation
    for it entirely — so a query in any other language never reached it
    lexically."""
    c = collections_mod.default_collection()
    monkeypatch.setattr(fts, "existing_groups", lambda collection=None: ["en"])
    ran = []
    monkeypatch.setattr(fts, "search", lambda group, q, limit=20, collection=None, langs=None:
                        ran.append(q) or [])
    monkeypatch.setattr(llm, "language_restriction", lambda q: ([], q))
    monkeypatch.setattr(llm, "translate_query", lambda q, langs: {"en": "whale hunt"})
    retrieval._search_one_collection(db.connect(c), c, "caccia alla balena", 5, "fts",
                                     True, set())
    assert ran == ["whale hunt"]


def test_cjk_variants_are_one_list_ranked_by_each_records_best():
    """05 §5.2: a variant is a query, not a leg — three CJK variants cast
    one vote, and a record ranks by its best across them."""
    a = [{"chunk_id": "x"}, {"chunk_id": "y"}]
    b = [{"chunk_id": "z"}, {"chunk_id": "x"}]
    out = retrieval._best_rank([a, b], 10)
    assert [h["chunk_id"] for h in out] == ["x", "z", "y"]
    assert retrieval._best_rank([a], 10) is a
    assert len(retrieval._best_rank([a, b], 2)) == 2


def test_one_plan_serves_every_collection_of_a_federated_search(isolated_config, monkeypatch,
                                                                 tmp_path):
    """One model call translates into the union of what the collections
    hold; each used to ask for its own, and the node's Ollama runs one
    request at a time."""
    from app import language_plan
    a = collections_mod.default_collection()
    b = dataclasses.replace(a, name="beta")
    held = {a.name: ["en"], "beta": ["de", "fr"]}
    monkeypatch.setattr(language_plan.fts, "existing_groups",
                        lambda collection=None: held[collection.name])
    monkeypatch.setattr(llm, "language_restriction", lambda q: ([], q))
    asked = []
    monkeypatch.setattr(llm, "translate_query",
                        lambda q, langs: asked.append(langs) or {l: q for l in langs})
    plan = language_plan.plan("baleine", [a, b], True)
    assert asked == [["de", "en", "fr"]]
    assert language_plan.plan("baleine", [a, b], True) is plan, \
        "the answer's search reuses it: no second call, no second opinion"
    assert asked == [["de", "en", "fr"]]


def test_a_query_that_names_languages_restricts_every_leg(isolated_config, monkeypatch):
    """"Unless specified in the query". A restriction is a FILTER (05 §5.2:
    a filter restricts, nothing boosts): the groups of other languages are
    not searched, a mixed group keeps only those languages, the dense leg
    is prefiltered, and every leg ranks by the query WITHOUT the
    instruction."""
    c = collections_mod.default_collection()
    monkeypatch.setattr(fts, "existing_groups",
                        lambda collection=None: ["de", "en", "other"])
    ran = []
    monkeypatch.setattr(fts, "search", lambda group, q, limit=20, collection=None, langs=None:
                        ran.append((group, q, langs)) or [])
    dense = []
    monkeypatch.setattr(retrieval.embeddings, "search",
                        lambda q, k=20, langs=None, collection=None, book_ids=None:
                        dense.append((q, langs)) or [])
    monkeypatch.setattr(llm, "language_restriction",
                        lambda q: (_ for _ in ()).throw(AssertionError("lang: needs no model")))
    monkeypatch.setattr(llm, "translate_query",
                        lambda q, langs: {l: f"{l}:{q}" for l in langs})

    retrieval._search_one_collection(db.connect(c), c, "whale hunt lang:de,la", 5,
                                     "hybrid", True, set())
    assert ("de", "de:whale hunt", None) in ran
    assert ("other", "la:whale hunt", ["la"]) in ran, "the mixed group, filtered to Latin"
    assert not [g for g, _, _ in ran if g == "en"], "English is not searched at all"
    assert dense == [("whale hunt", ["de", "la"])]


def test_the_restriction_is_read_only_where_the_model_quotes_the_query(monkeypatch):
    """qwen3:8b asked for codes alone restricted "Russian novels about war"
    to Russian and a plain Italian query to Italian (node, 2026-09-18). The
    instruction must be QUOTED from the query and must not be all of it;
    anything else searches every language — the default, never a silent
    narrowing."""
    replies = iter([
        '{"phrase": "solo testi in tedesco", "only": ["de"]}',
        '{"phrase": "Geschichte des Walfangs", "only": ["de"]}',   # the whole query
        '{"phrase": "only German books", "only": ["de"]}',         # not in the query
        '{"phrase": "", "only": ["it"]}',                          # no instruction quoted
        'not json at all',
    ])
    monkeypatch.setattr(llm, "chat", lambda *a, **k: next(replies))
    assert llm.language_restriction("la caccia alla balena, solo testi in tedesco") == \
        (["de"], "la caccia alla balena")
    assert llm.language_restriction("Geschichte des Walfangs") == \
        ([], "Geschichte des Walfangs")
    assert llm.language_restriction("Walfang") == ([], "Walfang")
    assert llm.language_restriction("la caccia alla balena") == ([], "la caccia alla balena")
    assert llm.language_restriction("x") is None, "no answer is 'unavailable', not 'none'"


def test_an_unreachable_model_searches_every_language_and_says_so(isolated_config,
                                                                  monkeypatch):
    from app import language_plan
    c = collections_mod.default_collection()
    monkeypatch.setattr(language_plan.fts, "existing_groups", lambda collection=None: ["en"])
    monkeypatch.setattr(llm, "language_restriction", lambda q: None)
    monkeypatch.setattr(llm, "translate_query", lambda q, langs: {})
    plan = language_plan.plan("q", [c], True)
    assert plan.summary() == {"core": "q", "only": [], "status": "unavailable", "by": "",
                              "every": False}
    assert language_plan.plan("q", [c], True) is not plan, "a failure is never cached"


def test_the_text_index_filters_a_mixed_group_by_language(isolated_config):
    c = collections_mod.default_collection()

    def chunk(n):
        return [{"id": f"gutenberg:{n}:0:1", "book_id": n, "seq": 0, "chapter": "",
                 "text": "balaena magna", "tokens": 2}]
    fts.index_book("la", 1, chunk(1), c)
    fts.index_book("eo", 2, chunk(2), c)
    assert {h["book_id"] for h in fts.search("other", "balaena", collection=c)} == {1, 2}
    assert [h["book_id"] for h in fts.search("other", "balaena", collection=c,
                                             langs=["la"])] == [1]


def test_the_catalog_keeps_only_the_restricted_languages(isolated_config):
    c = collections_mod.default_collection()
    con = db.connect(c)
    for n, lang in ((1, "de"), (2, "en")):
        con.execute("INSERT INTO documents(uid,source,native_id,path,lang,title,authors,"
                    "updated_at) VALUES(?,?,?,?,?,?,?,0)",
                    (f"gutenberg:{n}", "gutenberg", str(n), f"/f/{n}", lang, "Moby Dick", "M"))
    con.execute("UPDATE documents SET rdf_done=1")
    catalog.backfill(con, c, budget_s=60)            # the catalog index (app/catalog.py)
    assert {r["book_id"] for r in retrieval.catalog_search(con, "Moby", collection=c)} == {1, 2}
    assert [r["book_id"] for r in retrieval.catalog_search(
        con, "Moby", langs=("de",), collection=c)] == [1]


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
