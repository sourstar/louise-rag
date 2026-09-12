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
    monkeypatch.setattr(spaces, "resolve_embed_model_hash", lambda: (value, weak))


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
                    "Adding a collection", "collections/", "license_ack",
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


def test_bandwidth_payload_reports_real_per_class_rates():
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

    con = db.connect()
    assert db.get_bandwidth_override(con) == 1_250_000
    # never written to platform.toml — the file always wins on restart
    assert not (Path(os.environ["CONFIG_DIR"]) / "platform.toml").exists()

    r = client.post("/admin/network/bandwidth/override", json={"rate": None},
                     headers={"X-Admin-Token": "sekrit"})
    assert r.status_code == 200
    assert db.get_bandwidth_override(db.connect()) is None


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
    """`web._translation_cache` is process-global and keyed (id, lang), so
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
    snip = r.text.split('class="snip"', 1)[1]
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
