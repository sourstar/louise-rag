"""P0.6 acceptance: Ask/Library/Network/System pages render from fixture
manifests over real (non-mocked) code paths; admin routes are gated by a
single ADMIN_TOKEN (header or /admin/login cookie); reader-facing routes
stay open with no token at all — "Ask is LAN-open read-only"
(architecture-v2.md §15).
"""
import os
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import collections as collections_mod, config, db, fts, spaces, web
from app.api import app


def _patch_hash(monkeypatch, value="abc123", weak=False):
    monkeypatch.setattr(spaces, "resolve_embed_model_hash", lambda *a: (value, weak))


def _patch_health_pings(monkeypatch):
    # web._ping(), not httpx.Client.get — patching the latter globally would
    # also break TestClient itself, since it's httpx-based.
    monkeypatch.setattr(web, "_ping", lambda url: True)


def _chunk(uid, native_id, seq, chapter, text):
    return {"id": f"{uid}:{seq}:1", "book_id": native_id, "seq": seq,
            "chapter": chapter, "text": text, "tokens": max(1, len(text) // 4)}


def _seed_one_book():
    collection = collections_mod.default_collection()
    con = db.connect(collection)
    uid = "gutenberg:1"
    con.execute(
        "INSERT INTO documents(uid,source,native_id,path,lang,title,authors,rdf_done,fts_done,"
        "updated_at) VALUES(?,?,?,?,?,?,?,1,1,?)",
        (uid, "gutenberg", "1", "/fake/1", "en", "The Lighthouse Keeper", "A. Author",
         int(time.time())))
    fts.index_book("en", 1, [_chunk(uid, 1, 0, "Chapter I",
                    "the lighthouse keeper lit the lamp every single night")], collection)


@pytest.fixture
def client(isolated_config, monkeypatch):
    _patch_hash(monkeypatch)
    _patch_health_pings(monkeypatch)
    monkeypatch.setattr(config, "ADMIN_TOKEN", "sekrit")
    with TestClient(app) as c:
        yield c


def test_ask_page_renders_open_without_auth(client):
    r = client.get("/ask")
    assert r.status_code == 200
    assert "Ask" in r.text


def test_static_assets_the_new_pages_reference_are_actually_served(client):
    # base.html links these; index.html never needed a static mount at all
    # (fully self-contained inline CSS/JS) so this was a real gap until it
    # was added alongside the new pages.
    r = client.get("/static/admin.css")
    assert r.status_code == 200 and "nav{" in r.text
    r = client.get("/static/vendor/htmx.min.js")
    assert r.status_code == 200 and len(r.content) > 1000


def test_ask_query_shows_hit_with_local_verification_badge(client):
    _seed_one_book()
    r = client.post("/ask/query", json={"query": "lighthouse keeper", "mode": "fts"})
    assert r.status_code == 200
    assert "local ✓" in r.text
    assert "lighthouse" in r.text.lower()


def test_browser_page_navigation_redirects_to_login_not_401(client):
    """A human hitting an admin PAGE without a session gets the login form
    with a return path — only clients that accept text/html, so the API
    contract (bare 401) is untouched."""
    r = client.get("/admin/library", headers={"Accept": "text/html"},
                   follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/admin/login?next=/admin/library"


def test_htmx_fragment_polling_still_gets_401_not_a_redirect(client):
    # htmx would happily swap the login page into a log fragment — it must
    # see the honest 401 instead
    r = client.get("/admin/system/log",
                   headers={"Accept": "text/html", "HX-Request": "true"},
                   follow_redirects=False)
    assert r.status_code == 401


def test_legacy_dashboard_is_gone_root_goes_to_ask(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code in (302, 307)
    assert r.headers["location"] == "/ask"


def test_help_page_is_open_and_documents_the_platform(client):
    r = client.get("/help")
    assert r.status_code == 200
    for anchor in ("Embedding safety", "backend gate", "EMBED_BATCH=1",
                    "Adding a collection", "collections/", "hold collection ack",
                    "What a profile is", "starter", "grow profile",
                    "Reusing published embeddings", "genesis import",
                    "CLI reference", "Troubleshooting"):
        assert anchor in r.text, f"help page must document: {anchor}"


def test_ui_is_rebranded_no_legacy_name_or_link(client):
    r = client.get("/ask")
    assert "louise-rag" in r.text
    assert "gutenberg-rag" not in r.text
    assert "legacy dashboard" not in r.text


def test_admin_routes_require_token(client):
    r = client.get("/admin/library")
    assert r.status_code == 401

    r = client.get("/admin/library", headers={"X-Admin-Token": "wrong"})
    assert r.status_code == 401

    r = client.get("/admin/library", headers={"X-Admin-Token": "sekrit"})
    assert r.status_code == 200


def test_admin_token_unset_locks_out_entirely(client, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_TOKEN", "")
    r = client.get("/admin/library", headers={"X-Admin-Token": "anything"})
    assert r.status_code == 401


def test_admin_login_cookie_authorizes_subsequent_navigation(client):
    r = client.post("/admin/login", json={"token": "wrong"})
    assert r.status_code == 401

    r = client.post("/admin/login", json={"token": "sekrit"})
    assert r.status_code == 200
    assert "admin_token" in r.cookies

    r = client.get("/admin/library")   # TestClient persists cookies across calls
    assert r.status_code == 200


def test_reader_routes_stay_open_with_no_token(client):
    _seed_one_book()
    checks = [
        ("get", "/api/status", {}),
        ("get", "/api/stats", {}),
        ("get", "/api/events", {}),
        ("get", "/api/books", {}),
        ("post", "/api/search", {"json": {"query": "lighthouse", "mode": "fts"}}),
    ]
    for method, path, kwargs in checks:
        r = getattr(client, method)(path, **kwargs)
        assert r.status_code != 401, f"{path} must stay open (no admin token supplied)"


def test_mutating_routes_are_gated(client):
    checks = [
        ("post", "/api/control", {"json": {"state": "running"}}),
        ("post", "/api/scope", {"json": {}}),
        ("post", "/api/discover", {}),
        ("post", "/api/reset", {"json": {"stage": "errors"}}),
    ]
    for method, path, kwargs in checks:
        r = getattr(client, method)(path, **kwargs)
        assert r.status_code == 401, f"{path} must require admin token"


def test_library_page_renders_shelf_card(client):
    r = client.get("/admin/library", headers={"X-Admin-Token": "sekrit"})
    assert r.status_code == 200
    # the default fixture collection has acquire_enabled=False, so the
    # "fetched" stage bar is honestly absent — its n/a explanation stands in
    assert "fetch stage: n/a" in r.text
    assert "parsed (rdf)" in r.text
    assert "indexed (lexical)" in r.text and "embedded (dense)" in r.text
    assert "grow profile" in r.text


def test_network_page_honest_empty_state(client):
    r = client.get("/admin/network", headers={"X-Admin-Token": "sekrit"})
    assert r.status_code == 200
    assert "not yet available" in r.text


def test_bandwidth_payload_reports_real_per_class_rates(isolated_config):
    # Not driving the live /admin/network/bandwidth/stream endpoint here: its
    # generator runs forever (by design — a real SSE feed), and a sync
    # generator blocked in time.sleep() inside a worker thread can't be
    # reliably cancelled mid-sleep by anyio, making an in-process end-to-end
    # consumption a hang risk. _bandwidth_payload() is the same payload the
    # endpoint yields every 2s.
    payload = web._bandwidth_payload()
    for direction in ("down", "up"):
        assert set(payload[direction]) == {"interactive", "delta", "backfill", "archive", "seeding"}
    assert "schedule" in payload["limits"]


def test_bandwidth_override_is_tier3_and_expires(client):
    r = client.post("/admin/network/bandwidth/override",
                     json={"rate": "10Mbit", "hours": 6},
                     headers={"X-Admin-Token": "sekrit"})
    assert r.status_code == 200
    assert r.json()["rate_bps"] == 1_250_000

    # in state/node.sqlite, where the worker reads it (app/node_state.py)
    from app import node_state
    assert node_state.bandwidth_override() == 1_250_000
    # never written to platform.toml — the file always wins on restart
    assert not (Path(os.environ["CONFIG_DIR"]) / "platform.toml").exists()

    r = client.post("/admin/network/bandwidth/override", json={"rate": None},
                     headers={"X-Admin-Token": "sekrit"})
    assert r.status_code == 200
    assert node_state.bandwidth_override() is None


def test_bandwidth_override_rejects_a_malformed_rate(client):
    r = client.post("/admin/network/bandwidth/override", json={"rate": "400Furlongs"},
                     headers={"X-Admin-Token": "sekrit"})
    assert r.status_code == 400


def test_bandwidth_override_requires_admin(client):
    r = client.post("/admin/network/bandwidth/override", json={"rate": "1Mbit"})
    assert r.status_code == 401


def test_system_page_shows_platform_toml_tier2_rows(client):
    r = client.get("/admin/system", headers={"X-Admin-Token": "sekrit"})
    assert r.status_code == 200
    assert "platform.toml" in r.text
    assert "bandwidth.schedule" in r.text
    assert "transfer.workers" in r.text
    assert "ask.latency_budget_ms" in r.text, "the reranker's budget (07 §7.8)"


def test_the_system_page_shows_each_collections_reranker_and_pings_it(isolated_config,
                                                                     monkeypatch):
    """The page listed RERANK_URL "(disabled)" and every collection's
    encoder, and no collection's reranker — while each collection's own
    [reranker] was live (2026-09-15)."""
    import dataclasses
    rr = collections_mod.RerankRecipe(url="http://gpu:30447/v1/rerank", api="jina",
                                      model="bge-reranker-v2-m3")
    c = dataclasses.replace(collections_mod.default_collection(), reranker=rr)
    monkeypatch.setattr(collections_mod, "load_all", lambda **kw: [c])
    monkeypatch.setattr(web, "_ping", lambda url: True)
    eff = web._effective_config()
    assert eff["tier2"][0]["reranker"] == "bge-reranker-v2-m3 @ http://gpu:30447/v1/rerank (jina)"
    assert eff["tier2_platform"]["ask.latency_budget_ms"] == 8000
    assert any(h["url"] == rr.url and f"{c.name}.rerank" in h["names"]
               for h in web._health_pings())


def test_system_page_renders_effective_config_and_boot_report(client):
    r = client.get("/admin/system", headers={"X-Admin-Token": "sekrit"})
    assert r.status_code == 200
    assert "Tier 1" in r.text and "Tier 2" in r.text and "Tier 3" in r.text
    assert "Boot-validation report" in r.text


def test_system_log_fragment_renders(client):
    r = client.get("/admin/system/log", headers={"X-Admin-Token": "sekrit"})
    assert r.status_code == 200


# ---------------- STATUS 5j: translated results and the reader ----------------


@pytest.fixture(autouse=True)
def _fresh_translation_cache():
    """`web._translation_cache` is process-global and keyed (lang, text), so
    one test's translation is served to the next for the same pair. Found
    by a test that passed alone and failed in the file — the same coupling
    a long-running node has, one test apart."""
    web._translation_cache.clear()
    yield
    web._translation_cache.clear()

def test_show_results_in_translates_content_only(client, monkeypatch):
    """The drop-down translates snippets and the answer — never titles,
    authors, ids or the page. One model call per page of results, cached
    per (chunk, language); a model failure degrades to the originals and
    says so."""
    _seed_one_book()
    calls = []

    def fake_translate(texts, lang):
        calls.append((tuple(texts), lang))
        return [f"<{lang}> {t}" for t in texts]
    monkeypatch.setattr(web.llm, "translate_texts", fake_translate)
    body = {"lang": "de", "items": [{"id": "gutenberg:1:0:1", "text": "the lighthouse"}]}
    d = client.post("/ask/translate", json=body).json()
    assert d["items"]["gutenberg:1:0:1"] == "<de> the lighthouse" and d["failed"] is False
    client.post("/ask/translate", json=body)
    assert len(calls) == 1, "cached: toggling back and forth never pays the model twice"
    assert client.post("/ask/translate", json={**body, "lang": "original"}).json()["items"] == {}
    monkeypatch.setattr(web.llm, "translate_texts", lambda texts, lang: [])
    d = client.post("/ask/translate", json={"lang": "fr", "items": [{"id": "x", "text": "y"}]}).json()
    assert d["failed"] is True and d["items"] == {}

    r = client.post("/ask/query", json={"query": "lighthouse keeper", "mode": "fts"})
    assert 'data-cid="gutenberg:1:0:1"' in r.text, "snippets carry the translation key"
    assert 'data-collection="gutenberg"' in r.text and 'data-book="1"' in r.text, "hits name their collection"
    page = client.get("/ask").text
    assert 'id="lang"' in page
    # EACH OPTION IN THE LANGUAGE IT SELECTS (operator, 2026-09-12): the
    # label used to read the English "show results in Deutsch" whatever it
    # switched to, which is a control an operator who reads only German
    # cannot use.
    assert "Ergebnisse auf Deutsch" in page
    assert "risultati in italiano" in page and "نتایج به فارسی" in page
    assert "show results in" not in page, "no English prefix survives"
    assert "results in their own language" in page, "the off switch stays in the chrome language"


def test_the_first_screen_is_the_highlighted_chunk_then_its_neighbours():
    """What the reader shows first when a hit is opened, to translate ahead
    of the click (operator, 2026-10-21): the title (it heads the panel),
    the highlighted chunk (the panel centres it), then below and above in
    turn — the strings reader.html renders, chapter headings included."""
    from app import reader
    doc = {"header": {"title": "Book"},
           "chunks": [{"chunk_id": f"b:{i}:1", "chapter": "I" if i < 3 else "II", "text": f"c{i} " * 10}
                      for i in range(6)]}
    # units as rendered: I, c0, c1, c2, II, c3, c4, c5 — c3 at index 5
    assert reader.first_screen(doc, ["b:3:1"], 1) == ["Book", "c3 " * 10]
    assert reader.first_screen(doc, ["b:3:1"], 61) == ["Book", "c3 " * 10, "c4 " * 10, "II"]
    every = reader.first_screen(doc, ["b:3:1"], 10 ** 6)
    assert sorted(every) == sorted(["Book", "I", "II"] + [f"c{i} " * 10 for i in range(6)])
    assert reader.first_screen(doc, [], 31) == ["Book", "I", "c0 " * 10], "from the top when nothing is lit"


def test_the_screen_route_serves_it_and_refuses_what_read_refuses(client):
    _seed_one_book()
    name = collections_mod.default_collection().name
    r = client.get(f"/read/{name}/1/screen?hl=gutenberg:1:0:1&chars=50")
    assert r.status_code == 200
    body = r.json()
    assert body["lang"] == "en" and body["texts"][0] == "The Lighthouse Keeper"
    assert any("lighthouse keeper lit the lamp" in t for t in body["texts"])
    assert client.get(f"/read/{name}/999/screen").status_code == 404
    assert client.get("/read/nope/1/screen").status_code == 404


def test_the_answer_is_asked_in_the_chosen_language_not_translated_after():
    from app import retrieval
    result = {"hits": [], "facts": [], "catalog": []}
    system = retrieval.build_answer_messages("q", result, answer_lang="it")[0]["content"]
    assert "ISO code 'it'" in system
    assert "ISO code" not in retrieval.build_answer_messages("q", result)[0]["content"]


def test_reader_opens_the_document_from_the_index_stores(client):
    """A click on a hit shows the WHOLE document — from the lexical index,
    never the corpus file — with the matched chunk highlighted, a
    reference line, the origin link, and a standalone-tab route."""
    _seed_one_book()
    name = collections_mod.default_collection().name
    r = client.get(f"/read/{name}/1?hl=gutenberg:1:0:1")
    assert r.status_code == 200
    assert "The Lighthouse Keeper" in r.text and "lighthouse keeper lit the lamp" in r.text
    assert 'class="reader-chunk hl"' in r.text
    assert "https://www.gutenberg.org/ebooks/1" in r.text
    assert "A. Author — The Lighthouse Keeper — (en) — gutenberg:1" in r.text
    assert "text from the lexical index" in r.text
    frag = client.get(f"/read/{name}/1?fragment=1&hl=gutenberg:1:0:1").text
    assert 'class="reader-doc"' in frag and "<title>" not in frag, "a fragment for the panel, no page chrome"
    assert client.get(f"/read/{name}/999").status_code == 404
    assert client.get("/read/nope/1").status_code == 404


def test_reader_falls_back_to_the_vector_store_and_says_when_nothing_is_indexed(client):
    import numpy as np
    import pyarrow as pa
    from app import embeddings, reader
    c = collections_mod.default_collection()
    con = db.connect(c)
    con.execute(
        "INSERT INTO documents(uid,source,native_id,path,lang,title,authors,rdf_done,fts_done,"
        "embed_done,updated_at) VALUES('gutenberg:2','gutenberg','2','/fake/2','en','Second Book',"
        "'B. Author',1,0,0,0)")
    con.execute(
        "INSERT INTO documents(uid,source,native_id,path,lang,title,authors,rdf_done,fts_done,"
        "embed_done,updated_at) VALUES('gutenberg:3','gutenberg','3','/fake/3','en','Third Book',"
        "'C. Author',1,0,1,0)")
    doc = reader.document(c, 2)
    assert doc["chunks"] == [] and doc["header"]["text_from"] is None
    assert "Not indexed yet" in client.get(f"/read/{c.name}/2").text

    # embedded (a bundle landed its vectors) but not indexed: Lance holds the text
    schema = pa.schema([
        pa.field("id", pa.string()), pa.field("book_id", pa.int64()), pa.field("lang", pa.string()),
        pa.field("seq", pa.int64()), pa.field("chapter", pa.string()), pa.field("text", pa.string()),
        pa.field("vector", pa.list_(pa.float16(), 4))])
    rows = [{"id": f"gutenberg:3:{i}:1", "book_id": 3, "lang": "en", "seq": i, "chapter": "Ch",
             "text": f"passage {i}", "vector": np.ones(4, dtype=np.float16)} for i in (1, 0)]
    embeddings._conn(c).create_table(embeddings.TABLE, data=rows, schema=schema)
    doc = reader.document(c, 3)
    assert [ch["text"] for ch in doc["chunks"]] == ["passage 0", "passage 1"], "seq order"
    assert doc["header"]["text_from"] == "vector store"


def test_origin_links_per_source():
    from app import reader
    assert reader.ORIGIN["pubmed"]({"native_id": "123"}) == "https://pubmed.ncbi.nlm.nih.gov/123/"
    assert reader.ORIGIN["gutenberg"]({"native_id": "1342"}) == "https://www.gutenberg.org/ebooks/1342"
    assert reader.ORIGIN["wikipedia"]({"native_id": "x", "lang": "de", "title": "Albert Einstein"}) == \
        "https://de.wikipedia.org/wiki/Albert_Einstein"
    assert reader.ORIGIN["wikipedia"]({"native_id": "x", "lang": "de", "title": None}) is None


def test_reader_serves_a_document_mid_reindex_from_the_entries_still_in_the_index(client):
    """Live after the PubMed re-pin: fts_done was reset for every document
    while the old entries stayed searchable until rewritten — a hit that
    came from them must open, so the reader asks the index regardless of
    the mark."""
    from app import reader
    _seed_one_book()
    c = collections_mod.default_collection()
    db.connect(c).execute("UPDATE documents SET fts_done=0 WHERE uid='gutenberg:1'")
    doc = reader.document(c, 1)
    assert doc["chunks"] and doc["header"]["text_from"] == "lexical index"
    assert "lighthouse keeper lit the lamp" in client.get(f"/read/{c.name}/1").text


def test_a_dropped_passage_is_never_cached_as_its_own_translation(client, monkeypatch):
    """THE DEFECT THAT DEFEATED "re-applying the switch must work".

    `llm.translate_texts` used to substitute the ORIGINAL for a passage the
    model dropped, and `/ask/translate` stored that as the translation for
    (chunk_id, lang). The client hides an unchanged passage, so nothing
    showed — and every later switch to that language was served from the
    cache, so the passage could never be translated again for the life of
    the process. Selecting the language a second time is exactly the
    repair the operator asked for.
    """
    _seed_one_book()
    attempts = []

    def drops_the_first(texts, lang):
        attempts.append(tuple(texts))
        # the model answered for the second passage only
        return [None, f"<{lang}> {texts[1]}"] if len(texts) > 1 else [None]
    monkeypatch.setattr(web.llm, "translate_texts", drops_the_first)
    body = {"lang": "de", "items": [{"id": "a", "text": "one"},
                                     {"id": "b", "text": "two"}]}
    d = client.post("/ask/translate", json=body).json()
    assert d["failed"] is False, "a partial result is not a failed call"
    assert d["items"] == {"b": "<de> two"}, "only what really came back"
    assert d["missed"] == ["a"], "and the page is told which did not"

    # the repair: a second pass reaches the model again for the missed one
    monkeypatch.setattr(web.llm, "translate_texts",
                        lambda texts, lang: [f"<{lang}> {t}" for t in texts])
    d = client.post("/ask/translate", json=body).json()
    assert d["items"]["a"] == "<de> one", "re-applying the switch repairs it"
    assert d["items"]["b"] == "<de> two", "and the one that worked is still cached"
    assert d["missed"] == []


def test_a_translation_equal_to_its_input_is_not_cached_either(client, monkeypatch):
    """Same permanent-miss shape by a different route: a model that echoes
    its input would otherwise pin that echo as the translation."""
    _seed_one_book()
    monkeypatch.setattr(web.llm, "translate_texts", lambda texts, lang: list(texts))
    body = {"lang": "de", "items": [{"id": "a", "text": "unchanged"}]}
    d = client.post("/ask/translate", json=body).json()
    assert d["items"] == {} and d["missed"] == ["a"]

    monkeypatch.setattr(web.llm, "translate_texts",
                        lambda texts, lang: [f"<{lang}> {t}" for t in texts])
    assert client.post("/ask/translate", json=body).json()["items"]["a"] == "<de> unchanged"


def test_the_translator_fence_cannot_collide_with_a_citation(monkeypatch):
    """`translate_texts` numbered its own batch `[1] …` while the Ask
    page's snippets began with their own rank marker `[1] `, so the model
    was handed "[1] [1] the lighthouse…" and had to guess which bracket
    was protocol. A passage lost that way came back as a silent drop,
    which the caching above then made permanent.

    The fence is `<<n>>` now, and a citation `[3]` inside a passage is
    content that must survive verbatim.
    """
    from app import llm
    seen = {}

    def fake_chat(messages, **kw):
        seen["prompt"] = messages[0]["content"]
        return '{"1": "erste", "2": "zweite"}'
    monkeypatch.setattr(llm, "chat", fake_chat)

    out = llm.translate_texts(["[1] the lighthouse", "see [3] for more"], "de")
    assert out == ["erste", "zweite"]
    prompt = seen["prompt"]
    assert "<<1>>" in prompt and "<<2>>" in prompt, "the fence is unambiguous"
    assert "[1] the lighthouse" in prompt, "the passage travels verbatim"
    assert "see [3] for more" in prompt, "an inline citation is content"


def test_the_rank_marker_is_outside_the_translated_snippet(client):
    """It rode inside `.snip`, so the server cache — keyed (chunk_id,
    lang) — replayed a stale bracket number for the same chunk at a new
    rank, and it collided with the translator's own fence."""
    _seed_one_book()
    r = client.post("/ask/query", json={"query": "lighthouse keeper", "mode": "fts"})
    assert '<span class="rank">[1]</span>' in r.text
    snip = r.text.split('class="snip tr"', 1)[1]
    assert not snip.lstrip('"> ').startswith("[1]"), "the marker is not in the unit"


def test_withheld_collections_are_named_and_do_not_crash_the_page():
    """Invariant 9's whole point is that a withholding is VISIBLE.

    `query_service.withheld_from` computes the list "so the answer can say
    it is incomplete rather than quietly being so" — its own docstring —
    and nothing rendered it, so a licence gate that withheld a collection
    left no word on screen. Worse: when EVERY collection was withheld,
    `run_search` returns {hits, withheld} with no `timings` key, and
    `_ask_results.html` did `result.timings.items()` — UndefinedError, so
    /ask/query answered 500. The gate's message replaced by a crash, on
    the default all-collections scope.
    """
    tpl = web.TEMPLATES.env.get_template("_ask_results.html")
    # exactly what run_search returns when nothing may be searched
    result = {"hits": [], "withheld": [
        {"collection": "pubmed", "reason": "licence not acknowledged"}]}
    html = tpl.render(result=result)
    assert "pubmed" in html and "licence not acknowledged" in html
    assert "left out of this search" in html
    assert "this is not an empty result" in html,         "an empty page reads as 'nothing matched', which is a different claim"

    # and a withholding BESIDE real hits is still named
    partial = {"hits": [], "timings": {"fts": 3}, "facts": [], "catalog": [],
               "withheld": [{"collection": "wikivec", "reason": "no ack"}]}
    html = tpl.render(result=partial)
    assert "wikivec" in html and "fts 3 ms" in html

    # the ordinary case is unchanged and mentions nothing
    plain = {"hits": [], "timings": {"fts": 3}, "facts": [], "catalog": []}
    html = tpl.render(result=plain)
    assert "left out of this search" not in html


def test_the_target_language_is_named_in_the_prompt_not_only_coded(monkeypatch):
    """Asked to translate "into the language with ISO code 'it'", qwen3:8b
    answers {"1": "it", "2": "it", …} — it reads the code as the thing to
    emit. Measured on the node 2026-09-12: the code form returned 147
    bytes of 11 x "it" in 5.0 s; naming the language returned 4569 bytes
    of 11 real translations in 56.1 s.

    That is the dangerous shape, not a crash: eleven non-empty strings
    parse cleanly, so `/ask/translate` cached them and the Ask page
    painted the literal text "it" over every snippet.
    """
    from app import llm
    seen = {}

    def fake_chat(messages, **kw):
        seen["prompt"] = messages[0]["content"]
        return '{"1": "il faro"}'
    monkeypatch.setattr(llm, "chat", fake_chat)

    llm.translate_texts(["the lighthouse"], "it")
    assert "Italian" in seen["prompt"], "the NAME is what the model acts on"
    assert "'it'" in seen["prompt"], "the code stays, to disambiguate the name"

    # a code we have no name for degrades to the code rather than inventing one
    llm.translate_texts(["the lighthouse"], "qq")
    assert "ISO code 'qq'" in seen["prompt"]


def test_a_language_code_echoed_back_is_never_content(monkeypatch):
    """Belt and braces behind the naming: if the echo happens anyway it
    must surface as a MISS — the page then shows the original and says
    so — never as the passage's new text."""
    from app import llm

    monkeypatch.setattr(llm, "chat",
                        lambda messages, **kw: '{"1": "it", "2": "Italian", "3": "il faro"}')
    out = llm.translate_texts(["a", "b", "the lighthouse"], "it")
    assert out == [None, None, "il faro"], "the code and the name are not translations"


def test_every_picker_language_has_a_name_for_the_prompt():
    """The picker's table and the prompt's table are separate, so a
    language added to one silently regresses to the bare-code failure in
    the other. This is the pin that stops it."""
    from app import llm
    missing = [code for code, _endonym, _phrase in web.RESULT_LANGUAGES
               if code not in llm.LANGUAGE_NAMES]
    assert not missing, f"no prompt name for {missing} — they would echo their code"


def test_a_truncated_batch_keeps_the_passages_that_did_arrive(monkeypatch):
    """One bad passage used to take the whole batch with it: the model
    ran out mid-value, `json.loads` raised, `translate_texts` returned []
    and `/ask/translate` answered failed=True with nothing translated —
    observed live, 11 passages lost to the first one. Losing only the
    tail is strictly better."""
    from app import llm

    # valid pairs, then a value cut off mid-string
    monkeypatch.setattr(llm, "chat", lambda messages, **kw:
                        '{"1": "il faro", "2": "il guardiano", "3": "la temp')
    out = llm.translate_texts(["a", "b", "c"], "it")
    assert out == ["il faro", "il guardiano", None]

    # an escaped quote inside a value is content, not the end of the value
    monkeypatch.setattr(llm, "chat", lambda messages, **kw:
                        r'{"1": "dice \"ciao\" e parte", "2": "la temp')
    out = llm.translate_texts(["a", "b"], "it")
    assert out == ['dice "ciao" e parte', None]


def test_nothing_recovered_is_a_whole_batch_failure_not_n_misses(monkeypatch):
    """The two notes on the page say different things — "translation
    unavailable, showing the originals" vs "2 passage(s) could not be
    translated". Keep them truthful: nothing recovered is the first, a
    partial batch is the second."""
    from app import llm
    monkeypatch.setattr(llm, "chat", lambda messages, **kw: "I cannot do that.")
    assert llm.translate_texts(["a", "b"], "it") == [], "whole-batch failure"

    # an all-echo batch parses, so it is N honest misses, not a failure
    monkeypatch.setattr(llm, "chat", lambda messages, **kw: '{"1": "it", "2": "it"}')
    assert llm.translate_texts(["a", "b"], "it") == [None, None]


def test_the_origins_page_layout_is_flattened_before_the_model_sees_it(monkeypatch):
    """A Gutenberg snippet carries the book's typesetting — centred title
    pages, indented verse, CRLF — and on that input qwen3:8b starts
    reproducing the whitespace and emits newlines until it stops, losing
    the batch. Measured on the node with the language NAMED either way:
    raw 1.6 s / 92 B / 0 of 11; cleaned 56.5 s / 4569 B / 11 of 11.
    """
    from app import llm
    seen = {}

    def fake_chat(messages, **kw):
        seen["prompt"] = messages[0]["content"]
        seen["max_tokens"] = kw.get("max_tokens")
        return '{"1": "il faro"}'
    monkeypatch.setattr(llm, "chat", fake_chat)

    messy = "滌語開路\r\n\r\n\r\n\r\n      民國四年   \r\n\r\n\r\n"
    llm.translate_texts([messy], "it")
    body = seen["prompt"]
    assert "\r" not in body, "CRLF is the origin's, not the protocol's"
    assert "\n\n\n" not in body, "three or more blank lines collapse to one"
    assert "      " not in body, "runs of spaces collapse to one"
    assert "滌語開路" in body and "民國四年" in body, \
        "the WORDS travel untouched; only the layout goes"

    # the budget is taken over the PRE-CLEAN length — the cap covers the
    # output, and 3279 over the uncleaned batch is what the 11/11 run used
    assert seen["max_tokens"] == max(600, 2 * len(messy) // 3 + 200)


def test_flatten_whitespace_leaves_ordinary_prose_alone():
    from app import llm
    plain = "The lighthouse keeper woke.\n\nHe lit the lamp."
    assert llm.flatten_whitespace(plain) == plain


def test_an_empty_badge_is_not_drawn(client):
    """`authors` and `chapter` rendered unconditionally, so every hit drew
    an empty pill for each. `chapter` is empty for EVERY pubmed hit — an
    abstract has no chapter — and the operator read that blank badge after
    `fts:en` as a fix still landing."""
    tpl = web.TEMPLATES.env.get_template("_ask_results.html")
    hit = {"chunk_id": "pubmed:1:0:1", "book_id": 1, "title": "A study",
           "authors": "", "lang": "en", "chapter": "", "text": "x",
           "sources": ["fts:en"], "source_collection": "pubmed"}
    html = tpl.render(result={"hits": [hit], "timings": {"fts": 1}})
    assert '<span class="badge"></span>' not in html, "no empty pills"
    assert 'fts:en' in html and 'pubmed' in html, "the real badges still show"

    # and when they DO have something to say, they are drawn
    full = dict(hit, authors="Curie M", chapter="Chapter I")
    html = tpl.render(result={"hits": [full], "timings": {"fts": 1}})
    assert "Curie M" in html and "Chapter I" in html


def test_a_hit_links_to_the_origin_not_to_this_node(client):
    """`reader.html` has offered "open at the source" all along, but the Ask
    page's reader panel loads the FRAGMENT, which has no header — so the
    only link on an Ask result was `/read/<collection>/<book>`, this
    node's own path. Clicking a PubMed article took the reader to us."""
    _seed_one_book()
    r = client.post("/ask/query", json={"query": "lighthouse keeper", "mode": "fts"})
    assert "https://www.gutenberg.org/ebooks/1" in r.text
    assert "source ↗" in r.text


def test_the_origin_link_is_per_plugin_and_absent_when_unknown():
    from app import reader as reader_mod
    tpl = web.TEMPLATES.env.get_template("_ask_results.html")

    assert reader_mod.ORIGIN["pubmed"]({"native_id": "35114297"}) == \
        "https://pubmed.ncbi.nlm.nih.gov/35114297/"

    # a plugin with no origin page draws no link at all, rather than one
    # pointing nowhere
    hit = {"chunk_id": "genome:1:0:1", "book_id": 1, "title": "chr1", "lang": "en",
           "text": "ACGT", "authors": "", "chapter": "", "origin_url": None}
    html = tpl.render(result={"hits": [hit], "timings": {"fts": 1}})
    assert "source ↗" not in html


def test_the_language_switch_says_it_is_working_before_the_wait(client):
    """One model call for a page of passages measured 78 s, and nothing on
    screen changed for the whole of it — so the switch read as doing
    nothing, reported twice by the operator."""
    r = client.get("/ask")
    js = r.text
    note = js.index("langNote('translating ")
    fetch = js.index("/ask/translate")
    assert note < fetch, "the note must be painted BEFORE the model call, not after"


def test_the_title_is_translatable_and_sent_once_per_text(client):
    """STATUS 5j, amended 2026-09-12 — "how am I supposed to understand a
    title in Arabic or Hindi?" Two passages of one book share its title
    and must not be translated twice or come back disagreeing: the page
    sends each distinct (source language, text) once and paints every
    element that carries it."""
    tpl = web.TEMPLATES.env.get_template("_ask_results.html")
    hits = [{"chunk_id": "gutenberg:76511:0:1", "book_id": 76511, "lang": "fi",
             "title": "Ties mikä lie!", "authors": "Malinen, Vilho", "chapter": "",
             "text": "a", "origin_url": None},
            {"chunk_id": "gutenberg:76511:7:1", "book_id": 76511, "lang": "fi",
             "title": "Ties mikä lie!", "authors": "Malinen, Vilho", "chapter": "",
             "text": "b", "origin_url": None}]
    html = tpl.render(result={"hits": hits, "timings": {"fts": 1}})
    assert html.count('class="doc-title tr" data-lang="fi"') == 2, \
        "both titles are translatable, and say what language they are in"
    js = client.get("/ask").text
    assert "const key=JSON.stringify([src,u.dataset.orig])" in js, "one item per distinct text"
    assert "g.els.push(u)" in js, "painted onto every element that carries it"


def test_authors_and_ids_are_not_given_a_translation_hook(client):
    """The amendment kept this half: a personal name is not translated in
    any language, and an id is not language at all."""
    tpl = web.TEMPLATES.env.get_template("_ask_results.html")
    hit = {"chunk_id": "pubmed:1:0:1", "book_id": 1, "lang": "en", "title": "T",
           "authors": "Curie M", "chapter": "", "text": "x", "origin_url": None}
    html = tpl.render(result={"hits": [hit], "timings": {"fts": 1}})
    authors_span = html.split("Curie M")[0].rsplit("<span", 1)[-1]
    assert "data-cid" not in authors_span and " tr" not in authors_span


def test_the_queue_translates_in_the_order_it_is_read(client):
    """Operator, 2026-10-21 (superseding "everything in one request"): the
    open reader's screen, its window, the answer box, the page's screen
    ahead, every entry's visible text, then what is behind each entry — one
    request at a time, planned in that order. The behaviour is checked in a
    browser (tests/test_ask_translation_order_browser.py); here, the plan's
    order in the page."""
    js = client.get("/ask").text
    plan = js[js.index("function tqPlan(lang){"):js.index("function screenKey(")]
    steps = ["rank:(plan.visible||bar)?0:1", "if(tq.hold)return null", "rank:2", "rank:3", "rank:4",
             "rank:5"]
    at = [plan.index(x) for x in steps]
    assert at == sorted(at), "reader, then the answer, then the page, then what is behind it"
    assert "document.querySelectorAll('#results .tr')" in js
    assert js.count("fetch('/ask/translate/stream'") == 1, "one place sends, for the page and the reader"


def test_original_restores_the_published_text(client):
    js = client.get("/ask").text
    assert "u.textContent=u.dataset.orig" in js, "switching back restores it exactly"
    assert "published as: " in js, "and the tooltip carries it while translated"


def test_translate_batches_by_source_language(client, monkeypatch):
    """A mixed batch of SHORT items comes back with some copied through
    untranslated — 11 titles across Chinese, Greek, French and English
    returned 7 unchanged on the node, while the same English titles in an
    English-only batch translated 3 of 3. Grouping removes the comparison
    that invites it."""
    from app import llm
    calls = []

    def fake(texts, lang):
        calls.append(list(texts))
        return [f"{lang}:{t}" for t in texts]
    monkeypatch.setattr(llm, "translate_texts", fake)

    r = client.post("/ask/translate", json={"lang": "it", "items": [
        {"id": "a", "text": "one", "src": "en"},
        {"id": "b", "text": "two", "src": "en"},
        {"id": "c", "text": "three", "src": "el"},
        {"id": "d", "text": "four", "src": "zh"}]})
    assert r.status_code == 200
    body = r.json()
    assert body["items"] == {"a": "it:one", "b": "it:two",
                             "c": "it:three", "d": "it:four"}
    assert sorted(len(c) for c in calls) == [1, 1, 2], \
        "one call per source language, the two English items together"


def test_one_source_language_failing_is_not_everyones_failure(client, monkeypatch):
    """`failed` means "nothing came back". If Greek fails and English does
    not, the page shows the English and names the Greek — it does not
    discard good content for a uniform screen."""
    from app import llm

    def fake(texts, lang):
        return [] if texts == ["three"] else [f"{lang}:{t}" for t in texts]
    monkeypatch.setattr(llm, "translate_texts", fake)

    body = client.post("/ask/translate", json={"lang": "it", "items": [
        {"id": "a", "text": "one", "src": "en"},
        {"id": "c", "text": "three", "src": "el"}]}).json()
    assert body["items"] == {"a": "it:one"}
    assert body["missed"] == ["c"]
    assert body["failed"] is False, "English came back; that is not a failure"


def test_every_source_language_failing_is_a_failure(client, monkeypatch):
    from app import llm
    monkeypatch.setattr(llm, "translate_texts", lambda texts, lang: [])
    body = client.post("/ask/translate", json={"lang": "it", "items": [
        {"id": "a", "text": "one", "src": "en"},
        {"id": "c", "text": "three", "src": "el"}]}).json()
    assert body["items"] == {} and body["failed"] is True


def test_the_client_sends_each_items_source_language(client):
    js = client.get("/ask").text
    assert "const src=u.dataset.lang||'?'" in js, "every translatable element"
    assert "text:g.text,src:g.src" in js, "sent with its item"
    assert "src:answerLang" in js, "and the answer"


def test_an_item_returned_verbatim_is_retried_in_a_small_batch(client, monkeypatch):
    """The copy-through this endpoint keeps hitting: a batch comes back
    with short items identical to their input. Three attempts to predict
    it all missed (the prompt's "names" clause: no change; "every item
    must be translated": 7 unchanged became 9; grouping by source
    language: fixed latency, not this). A small batch IS measured to work
    — three English titles alone translated 3/3 where eleven returned
    them verbatim — so the detected misses go back in threes."""
    from app import llm
    seen = []

    def fake(texts, lang):
        seen.append(list(texts))
        # first pass: the long one translates, the short ones come back as
        # they went in. second pass (small): everything translates.
        if len(texts) > 2:
            return [f"{lang}:{t}" if len(t) > 8 else t for t in texts]
        return [f"{lang}:{t}" for t in texts]
    monkeypatch.setattr(llm, "translate_texts", fake)

    body = client.post("/ask/translate", json={"lang": "it", "items": [
        {"id": "long", "text": "a passage long enough to be rewritten", "src": "en"},
        {"id": "s1", "text": "short a", "src": "en"},
        {"id": "s2", "text": "short b", "src": "en"}]}).json()

    assert body["items"] == {"long": "it:a passage long enough to be rewritten",
                             "s1": "it:short a", "s2": "it:short b"}
    assert body["missed"] == [], "the retry cleared them, so nothing is reported missed"
    assert len(seen) == 2, "one first pass, one retry — not a loop"
    assert len(seen[1]) <= 3, "and the retry is small, which is the whole point"


def test_what_the_retry_cannot_save_is_still_an_honest_miss(client, monkeypatch):
    from app import llm
    monkeypatch.setattr(llm, "translate_texts", lambda texts, lang: list(texts))
    body = client.post("/ask/translate", json={"lang": "it", "items": [
        {"id": "a", "text": "stubborn", "src": "en"}]}).json()
    assert body["items"] == {} and body["missed"] == ["a"]
    assert body["failed"] is False, "the model answered; it just did not translate"


# ---------------- 2026-09-18: every language, entries in full, streamed ----------------


def _stream(client, body):
    import json as _json
    r = client.post("/ask/translate/stream", json=body)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/x-ndjson")
    return [_json.loads(ln) for ln in r.text.splitlines() if ln.strip()]


def test_an_entry_is_on_the_page_in_full_not_as_a_snippet(client):
    """"The returned entries have to be translated in full" (operator,
    2026-09-18). The page held the first 420 characters, and those were all
    that was ever sent to the model."""
    tpl = web.TEMPLATES.env.get_template("_ask_results.html")
    long_text = "word " * 400 + "THE END"
    hit = {"chunk_id": "gutenberg:1:0:1", "book_id": 1, "lang": "en", "title": "T",
           "authors": "", "chapter": "Chapter I", "text": long_text, "origin_url": None}
    html = tpl.render(result={"hits": [hit], "timings": {"fts": 1}})
    snip = html.split('class="snip tr"', 1)[1].split("</div>", 1)[0]
    assert "THE END" in snip, "the whole entry, to its last word"
    assert 'class="badge chap tr" data-lang="en"' in html, \
        "the chapter heading is the document's words, translated with it"
    assert "more / less" in html, "clamped on screen, opened by a click"


def test_the_stream_sends_short_items_first_then_streams_long_ones_in_order(client, monkeypatch):
    from app import llm
    batched = []
    monkeypatch.setattr(llm, "translate_texts",
                        lambda texts, lang: batched.append(list(texts)) or
                        [f"<{lang}> {t}" for t in texts])
    streamed = []

    def fake_stream(text, lang):
        streamed.append(text[:6])
        yield f"<{lang}> "
        yield text

    monkeypatch.setattr(llm, "translate_stream", fake_stream)
    answer = "the answer, long enough to stream " * 20
    entry = "an entry in full, long enough to stream " * 20
    lines = _stream(client, {"lang": "it", "items": [
        {"id": "__answer__", "text": answer, "src": "en"},
        {"id": "t0", "text": "A Title", "src": "en"},
        {"id": "t1", "text": entry, "src": "en"}]})
    ids = [m.get("id") for m in lines if "text" in m]
    assert ids == ["t0", "__answer__", "t1"], \
        "the title first, then the long ones in the order the page sent"
    assert batched == [["A Title"]], "the short one batched, the long ones never"
    assert streamed == ["the an", "an ent"]
    deltas = [m for m in lines if m.get("id") == "t1" and "delta" in m]
    assert "".join(m["delta"] for m in deltas) == f"<it> {entry}", \
        "the words arrive as they are written"
    assert lines[-1] == {"done": True, "failed": False, "missed": []}

    # the second switch to the same language pays nothing
    monkeypatch.setattr(llm, "translate_stream",
                        lambda text, lang: (_ for _ in ()).throw(AssertionError("paid twice")))
    again = _stream(client, {"lang": "it", "items": [{"id": "x", "text": entry, "src": "en"}]})
    assert again[0] == {"id": "x", "text": f"<it> {entry}".strip()}


def test_the_cache_is_keyed_on_the_text_so_a_second_answer_is_not_served_the_first(
        client, monkeypatch):
    """It was keyed (id, lang), and the answer's id is one fixed string: a
    second question's answer switched into Italian came back as the FIRST
    answer's Italian."""
    from app import llm
    monkeypatch.setattr(llm, "translate_stream", lambda text, lang: iter([f"<{lang}> {text}"]))
    first = "the first answer [1] " * 30
    second = "a different answer [2] " * 30
    _stream(client, {"lang": "it", "items": [{"id": "__answer__", "text": first}]})
    got = _stream(client, {"lang": "it", "items": [{"id": "__answer__", "text": second}]})
    final = [m for m in got if "text" in m][0]
    assert final["text"] == f"<it> {second}".strip()


def test_a_stream_that_echoes_or_breaks_is_a_miss_and_an_unreachable_model_is_asked_once(
        client, monkeypatch):
    from app import llm
    long_a, long_b, long_c = ("alpha " * 100), ("bravo " * 100), ("charlie " * 100)
    monkeypatch.setattr(llm, "translate_stream", lambda text, lang: iter([text]))
    lines = _stream(client, {"lang": "it", "items": [{"id": "a", "text": long_a}]})
    assert {"id": "a", "missed": True} in lines, "the input handed back is not a translation"
    assert lines[-1]["failed"] is True, "nothing came back"

    calls = []

    def down(text, lang):
        calls.append(text)
        raise httpx.ConnectError("refused")
        yield  # pragma: no cover — a generator that fails on first use
    import httpx
    monkeypatch.setattr(llm, "translate_stream", down)
    lines = _stream(client, {"lang": "it", "items": [
        {"id": "a", "text": long_a}, {"id": "b", "text": long_b}, {"id": "c", "text": long_c}]})
    assert len(calls) == 1, "the rest are not queued behind the same failure"
    assert lines[-1] == {"done": True, "failed": True, "missed": ["a", "b", "c"]}


def test_original_through_the_stream_translates_nothing(client, monkeypatch):
    from app import llm
    monkeypatch.setattr(llm, "translate_texts",
                        lambda *a: (_ for _ in ()).throw(AssertionError("called")))
    assert _stream(client, {"lang": "original", "items": [{"id": "a", "text": "x"}]}) == \
        [{"done": True, "failed": False, "missed": []}]


def test_the_page_and_the_server_agree_on_what_is_short(client):
    js = client.get("/ask").text
    assert f"const SHORT_ITEM={web._STREAM_OVER};" in js


def test_ask_writes_the_answer_before_translating_the_entries(client):
    """The model is one queue (OLLAMA_NUM_PARALLEL=1 on the node) and the
    entries are minutes of it: the queue holds the page while the answer is
    written (operator, 2026-10-21: the answer box first)."""
    js = client.get("/ask").text
    body = js[js.index("async function ask()"):]
    hold = body.index("tq.hold=true")
    search = body.index("await search()")
    answer = body.index("fetch('/v1/chat/completions'")
    release = body.index("tq.hold=false")
    entries = body.index("tqPump();")
    assert hold < search < answer < release < entries


def test_a_new_search_or_switch_stops_the_translation_in_flight(client):
    js = client.get("/ask").text
    assert "langAbort.abort()" in js and "signal:ctl.signal" in js
    search = js[js.index("async function search()"):js.index("async function ask()")]
    assert search.index("stopTranslating()") < search.index("fetch('/ask/query'"), \
        "the model is freed before the search needs it"


def test_the_page_names_a_language_restriction_and_a_failed_plan():
    tpl = web.TEMPLATES.env.get_template("_ask_results.html")
    base = {"hits": [], "timings": {"fts": 1}}
    html = tpl.render(result={**base, "language": {"core": "q", "only": ["de", "fr"],
                                                   "status": "ok"}})
    assert "only: de, fr" in html
    html = tpl.render(result={**base, "language": {"core": "q", "only": [],
                                                   "status": "unavailable"}})
    assert "query not translated" in html
    html = tpl.render(result={**base, "language": {"core": "q", "only": [], "status": "ok"}})
    assert "only:" not in html and "not translated" not in html


def test_the_reader_fragment_names_its_language():
    # the reader translates FROM the document's language; without it every
    # document read as unknown, and one already in the target was sent anyway
    tpl = web.TEMPLATES.env.get_template("reader.html")
    doc = {"header": {"title": "T", "reference": "r", "lang": "de", "origin_url": None,
                      "text_from": "lexical index"},
           "chunks": [{"seq": 0, "chunk_id": "b:0", "chapter": "", "text": "Ein Satz."}]}
    html = tpl.render(fragment=True, doc=doc, highlights=[], collection="c", book_id=1,
                      plugin_label="Gutenberg")
    assert 'data-lang="de"' in html and 'class="reader-chunk"' in html


def test_the_reader_is_translated_as_it_is_read(client):
    """"I want also that text to be translated, the visible part first, then
    the same amount of text on bottom and top, then as I scroll, more text is
    translated in the direction of the scrolling" (operator, 2026-09-18).
    The behaviour was checked in headless Chromium (STATUS); here, that the
    page is wired to do it."""
    js = client.get("/ask").text
    opener = js[js.index("async function openReader("):js.index("function closeReader(")]
    assert opener.index("fragment=1") < opener.index("readerOpen();"), "once the text is in"
    assert "readerStop()" in js[js.index("function closeReader("):js.index("function readerPlan(")]
    apply = js[js.index("function applyLanguage("):js.index("// ---- reader panel ----")]
    assert "readerTranslate();" in apply, "the drop-down reaches the open document"
    plan = js[js.index("function readerPlan("):js.index("function readerNote(")]
    assert "if(vis.length)return" in plan, "what is on screen first"
    assert "first?[1,-1]:[dir]" in plan, "both sides at first, then the scroll's side"
    assert "addEventListener('scroll',readerScrolled" in js
    assert "overflow-anchor:none" in js, "the view is held by the page alone"
    pump = js[js.index("function readerTranslate("):js.index("function readerScrolled(")]
    assert "tqKick()" in pump, "the reader's requests are the queue's first ranks"


def test_the_fan_out_badge_counts_in_good_english():
    # "fan-out: 1 main languages" was live for `Rilke lang:de` (2026-09-18)
    tpl = web.TEMPLATES.env.get_template("_ask_results.html")
    base = {"hits": [], "timings": {"fts": 1}}
    one = tpl.render(result={**base, "translations": {"de": "Rilke"},
                             "language": {"core": "Rilke", "only": ["de"], "status": "ok",
                                          "by": "translation model"}})
    assert "fan-out: 1 main language, by the translation model" in one
    two = tpl.render(result={**base, "translations": {"de": "a", "fr": "b"},
                             "language": {"core": "q", "only": [], "status": "ok",
                                          "every": True}})
    assert "fan-out: 2 languages, every one held" in two


def test_the_ask_page_sets_how_many_before_and_after_reranking(client, monkeypatch):
    """"I want to be able to set the maximum number of results a query
    returns before and after reranking, with as default the recommended
    amount" (operator, 2026-09-18): 05 §5.4's 10 and §5.6's 50. The depth
    reaches 300 since 2026-09-21 ("up to 300 per leg"); the results stay at
    most 100."""
    page = client.get("/ask").text
    assert 'id="limit" type="number" min="1" max="100" value="10"' in page
    assert 'id="rerank-depth" type="number" min="1" max="300" value="50"' in page
    assert page.count("...counts()") == 2, "the page's search AND the answer's search"

    seen = {}

    def fake_run_search(names, query, k, mode, translate, verify=False, rerank_depth=50,
                        all_languages=False):
        seen.update(k=k, rerank_depth=rerank_depth, all_languages=all_languages)
        return {"hits": [], "timings": {}, "facts": [], "catalog": []}
    monkeypatch.setattr(web.query_service, "run_search", fake_run_search)
    client.post("/ask/query", json={"query": "q", "limit": 25, "rerank_depth": 80,
                                    "all_languages": True})
    assert seen == {"k": 25, "rerank_depth": 80, "all_languages": True}
    client.post("/ask/query", json={"query": "q"})
    assert seen == {"k": 10, "rerank_depth": 50, "all_languages": False}, \
        "the defaults: the main languages only, unless asked"
    assert 'id="all-langs" type="checkbox"' in page and "all_languages:" in page
    for bad in ({"limit": 0}, {"limit": 101}, {"rerank_depth": 0}, {"rerank_depth": 500}):
        assert client.post("/ask/query", json={"query": "q", **bad}).status_code == 422


def test_the_answers_search_takes_the_same_counts(client, monkeypatch):
    from app import api
    seen = {}

    def fake_run_search(names, query, k, mode, translate, verify=False, rerank_depth=50,
                        all_languages=False):
        seen.update(k=k, rerank_depth=rerank_depth)
        return {"hits": [], "timings": {"total": 1.0}, "facts": [], "catalog": []}
    monkeypatch.setattr(api, "_run_search", fake_run_search)
    monkeypatch.setattr(api.llm, "chat", lambda *a, **k: "no sources")
    r = client.post("/v1/chat/completions", json={
        "messages": [{"role": "user", "content": "q"}], "limit": 30, "rerank_depth": 60})
    assert r.status_code == 200 and seen == {"k": 30, "rerank_depth": 60}


# ---------------- System: only what is global (operator, 2026-09-18) ----------------


def test_tier_one_shows_only_what_is_global(isolated_config, monkeypatch):
    """The page listed EMBED_MODEL, CHUNK_TOKENS, CHUNK_OVERLAP,
    CHUNK_VERSION and CLEANING_VERSION as the node's, while each
    collection's encoder comes from its own file and its chunker and
    cleaning from its Codex. EMBED_URL is gone too: there is no default
    encoder (operator, 2026-09-18)."""
    tier1 = web._effective_config()["tier1"]
    for gone in ("OLLAMA_URL", "EMBED_URL", "EMBED_MODEL", "CHUNK_TOKENS", "CHUNK_OVERLAP",
                 "CHUNK_VERSION", "CLEANING_VERSION"):
        assert gone not in tier1, gone
    for kept in ("CHAT_URL", "CHAT_MODEL", "TRANSLATE_URL", "RERANK_URL", "ADMIN_TOKEN"):
        assert kept in tier1, kept


def test_the_reranker_fallback_says_who_reaches_it(isolated_config, monkeypatch):
    import dataclasses
    c = collections_mod.default_collection()
    own = collections_mod.EmbedRecipe(url="http://gpu:30437", api="openai", model="qwen3-4b",
                                      dtype="f16", ctx=8192)
    rr = collections_mod.RerankRecipe(url="http://gpu:30447/v1/rerank", api="jina")
    monkeypatch.setattr(config, "RERANK_URL", "http://env/rerank")

    monkeypatch.setattr(collections_mod, "load_all", lambda **kw: [c])
    eff = web._effective_config()
    assert f"used by {c.name}" in eff["tier1"]["RERANK_URL"]

    named = dataclasses.replace(c, embed=own, reranker=rr)
    monkeypatch.setattr(collections_mod, "load_all", lambda **kw: [named])
    eff = web._effective_config()
    assert "used by no collection" in eff["tier1"]["RERANK_URL"]
    assert eff["tier2"][0]["embed_recipe"] == "qwen3-4b @ http://gpu:30437", \
        "the encoder the collection runs — its own file's, there is no other"


def test_chat_url_is_the_name_and_ollama_url_is_still_read(monkeypatch):
    """CHAT_URL is the name the documents give it (07 §7.8); a node whose
    environment still says OLLAMA_URL must keep its chat model."""
    monkeypatch.delenv("CHAT_URL", raising=False)
    monkeypatch.setenv("OLLAMA_URL", "http://old:11434/")
    assert config._chat_url() == "http://old:11434"
    monkeypatch.setenv("CHAT_URL", "http://new:30068")
    assert config._chat_url() == "http://new:30068", "the new name wins"
    assert not hasattr(config, "OLLAMA_URL")


def test_node_wide_lines_are_the_nodes_not_the_first_collections(isolated_config, capsys):
    """"Why is gutenberg logging about the other plugins?" (operator,
    2026-09-18). The fair-share lines cover every collection and are
    stored in the first collection's manifest, so the stdout line and the
    System log both named that collection as their subject."""
    c = collections_mod.default_collection()
    con = db.connect(c)
    db.log_event(con, "info", "sched", "index — a: 50%; b: 50%")
    db.log_event(con, "info", "fts", "indexed 3 documents")
    out = capsys.readouterr().out
    assert "[node info:sched]" in out
    assert f"[{c.name} info:fts]" in out, "a collection's own line still names it"
    tagged = {e["stage"]: e["collection"] for e in web._recent_events_all()}
    assert tagged["sched"] == "node" and tagged["fts"] == c.name


def test_the_answer_is_not_pushed_out_of_view_as_it_streams(client):
    """The answer grows ABOVE the results. With the browser's scroll anchoring
    a reader scrolled among the results had the page follow them down as each
    word arrived, and the answer's opening slid out of view — reported as an
    answer "chopped, the beginning missing" (2026-09-21); on the node the text
    reached the page whole every time. Scrolling itself needs a browser to
    check; these pin the two lines that fix it, so neither goes unnoticed."""
    page = client.get("/ask").text
    assert "#results{overflow-anchor:none}" in page, "the results must never be the anchor"
    assert "pane.scrollIntoView({block:'start'})" in page, "the answer starts where it is seen"


# ---------------- The answer's stream says how it ended (2026-10-13) ----------------


def _no_sources(monkeypatch):
    from app import api
    monkeypatch.setattr(api, "_run_search", lambda *a, **k: {
        "hits": [], "timings": {"total": 1.0}, "facts": [], "catalog": []})


def _answer_text(sse: str) -> str:
    import json
    out = []
    for event in sse.split("\n\n"):
        if not event.startswith("data:") or event.endswith("[DONE]"):
            continue
        out.append(json.loads(event[5:])["choices"][0]["delta"].get("content") or "")
    return "".join(out)


def test_an_answer_the_endpoint_cut_is_said_to_the_reader_and_logged(client, monkeypatch, capsys):
    """"The answer was cut" (operator, 2026-10-13): the stream used to end a
    capped answer exactly like a whole one. Now the note is in the answer
    and a warn line names the size and the reason."""
    from app import api, llm
    _no_sources(monkeypatch)

    def capped(messages, temperature=0.3, **kw):
        yield "Il diritto all'"
        return llm.StreamEnd(finish_reason="length", done=True, error=None)
    monkeypatch.setattr(api.llm, "chat_stream", capped)
    r = client.post("/v1/chat/completions",
                    json={"messages": [{"role": "user", "content": "q"}], "stream": True})
    assert r.status_code == 200
    assert _answer_text(r.text) == "Il diritto all'\n[answer cut: the token limit was reached]"
    assert r.text.rstrip().endswith("data: [DONE]")
    assert "answer cut after 15 chars" in capsys.readouterr().out


def test_an_endpoint_that_fails_mid_answer_is_said(client, monkeypatch, capsys):
    from app import api
    _no_sources(monkeypatch)

    def failing(messages, temperature=0.3, **kw):
        yield "Il "
        raise RuntimeError("slot gone")
    monkeypatch.setattr(api.llm, "chat_stream", failing)
    r = client.post("/v1/chat/completions",
                    json={"messages": [{"role": "user", "content": "q"}], "stream": True})
    assert _answer_text(r.text) == "Il \n[answer cut: the endpoint failed (RuntimeError: slot gone)]"
    assert "answer cut after 3 chars" in capsys.readouterr().out


def test_a_whole_answer_is_logged_and_the_stream_kept_alive_while_silent(client, monkeypatch, capsys):
    """Nothing reaches the page during the cold start, the prompt and a
    model's hidden reasoning (19–34 s measured): an SSE comment goes out
    every KEEPALIVE_S instead, which the page's reader skips."""
    import time as time_mod
    from app import api, llm
    _no_sources(monkeypatch)
    monkeypatch.setattr(api, "KEEPALIVE_S", 0.05)

    def slow(messages, temperature=0.3, **kw):
        time_mod.sleep(0.3)
        yield "Il diritto"
        return llm.StreamEnd(finish_reason="stop", done=True, error=None)
    monkeypatch.setattr(api.llm, "chat_stream", slow)
    r = client.post("/v1/chat/completions",
                    json={"messages": [{"role": "user", "content": "q"}], "stream": True})
    assert ": keepalive\n\n" in r.text
    assert _answer_text(r.text) == "Il diritto" and "[answer cut" not in r.text
    assert "answer: 10 chars in" in capsys.readouterr().out
