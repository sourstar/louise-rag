"""A model's cold start is kept out of the request that found it cold
(app/model_load.py; operator, 2026-10-09: "Timeouts shall not include cold
start").

Found on the node: wikipedia's first search after idle loaded its query
encoder and reranker; the reranker's 8 s budget ran out DURING THE LOAD and
the list fell back unreranked. Against a fake runner that lists what it holds
at `/system` and answers a request for a model it does not hold only after
loading it:
  - the warm goes first, under the LOAD bound; the real request after it,
    under its usual budget (embedding, rerank, translation, chat);
  - two callers finding one model cold send one warm;
  - a load past its bound is a deferral (embedding, nothing on a document)
    and a visible fallback with its reason (the vector leg, the reranker);
  - a runner with no `/system`, or none reachable: exactly as before;
  - through VeGa, the real request's vectors are still taken only with their
    receipts, and the warm's receipt is never read.
"""
from __future__ import annotations

import dataclasses
import json as jsonlib
import threading
import time

import httpx
import pytest

from app import (collections as collections_mod, config, db, embeddings, llm, model_load,
                 platform_config, retrieval, translate, worker)
from app.verify import backend_gate
from tests.test_endpoint_classification import _collection, _row, _untouched
from tests.vega.test_embedding_path import (LOCALAI as VEGA_LOCALAI, MODEL as VEGA_MODEL,
                                            R1, RECIPE as VEGA_RECIPE, localai, vega,
                                            vector_of)

REAL_FETCH = model_load._fetch_system
SERVER = "http://localai:8080"
RERANKER = collections_mod.RerankRecipe(url=f"{SERVER}/v1/rerank", api="jina",
                                        model="louise-rerank-bge")
ENCODER = collections_mod.EmbedRecipe(url=SERVER, api="openai", model="vega-bge-m3",
                                      dtype="f32", ctx=512)


class Runner:
    """LocalAI as Louise meets it: `/system` lists the models it holds; a
    request for one it does not hold LOADS it first (`load_s`), inside that
    request's own timeout."""

    def __init__(self, load_s: float = 0.3) -> None:
        self.load_s = load_s
        self.loaded: set[str] = set()
        self.system_status = 200
        self.system_reads = 0
        self.calls: list[dict] = []
        self.lock = threading.Lock()
        #: each load is a new backend process, as LocalAI's are
        self.pids: dict[str, int] = {}
        self._next_pid = 1000
        #: models LocalAI's reclaimer evicts just before a real request
        #: reaches them (another model's load, between the check and the call)
        self.evict_before: set[str] = set()

    def pid(self, model: str) -> int:
        if model not in self.pids:
            self._next_pid += 1
            self.pids[model] = self._next_pid
        return self.pids[model]

    def evict(self, model: str) -> None:
        with self.lock:
            self.loaded.discard(model)
            self.pids.pop(model, None)

    # GET <server>/system
    def get(self, url, timeout=None, **_kw):
        assert url == f"{SERVER}/system", url
        with self.lock:
            self.system_reads += 1
            held = sorted(self.loaded)
        request = httpx.Request("GET", url)
        if self.system_status != 200:
            return httpx.Response(self.system_status, request=request, text="not found")
        return httpx.Response(200, request=request, json={
            "backends": ["llama-cpp"],
            "loaded_models": [{"id": m, "process": {"pid": self.pid(m)}} for m in held]})

    def _serve(self, url: str, model: str, timeout: float) -> None:
        with self.lock:
            cold = model not in self.loaded
        if cold:
            if self.load_s > timeout:
                time.sleep(timeout)
                raise httpx.ReadTimeout("timed out", request=httpx.Request("POST", url))
            time.sleep(self.load_s)
            with self.lock:
                self.loaded.add(model)

    # POST: embeddings, rerank, completions, chat
    def post(self, url, json=None, timeout=None, headers=None, **_kw):     # noqa: A002
        with self.lock:
            self.calls.append({"url": url, "json": json, "timeout": timeout})
        if not _is_warm(json) and json.get("model") in self.evict_before:
            self.evict(json["model"])
        self._serve(url, json.get("model"), timeout)
        request = httpx.Request("POST", url)
        if url.endswith("/v1/embeddings"):
            n = len(json.get("input") or json.get("input_pairs"))
            return httpx.Response(200, request=request, json={"data": [
                {"index": i, "embedding": [1.0] + [0.0] * 7} for i in range(n)]})
        if url.endswith("/v1/rerank"):
            n = len(json["documents"])
            return httpx.Response(200, request=request, json={"results": [
                {"index": i, "relevance_score": float(i)} for i in range(n)]})
        if url.endswith("/v1/completions"):
            asked = jsonlib.loads(json["prompt"])
            text = jsonlib.dumps({"translations": [f"[{i['to']}] {i['text']}"
                                                   for i in asked["items"]]})
            return httpx.Response(200, request=request, json={"choices": [{"text": text}]})
        if url.endswith("/v1/chat/completions"):
            return httpx.Response(200, request=request, json={
                "choices": [{"message": {"content": "Rome."}}]})
        raise AssertionError(url)

    def real(self) -> list[dict]:
        """The calls that were not a warm."""
        return [c for c in self.calls if not _is_warm(c["json"])]

    def warms(self) -> list[dict]:
        return [c for c in self.calls if _is_warm(c["json"])]


def _is_warm(body: dict) -> bool:
    return (body.get("input") == ["warm"] or body.get("input_pairs") == [["warm", "warm"]]
            or body.get("query") == "warm"
            or body.get("max_tokens") == 1 and body.get("messages", [{}])[0].get("content") == "OK"
            or '"text": "OK"' in body.get("prompt", ""))


@pytest.fixture(autouse=True)
def published(monkeypatch):
    """What the publisher thread would write to the event log, kept here
    instead (the real one opens the first collection's manifest)."""
    got: list = []
    monkeypatch.setattr(model_load, "publish", got.append)
    return got


@pytest.fixture
def runner(monkeypatch):
    r = Runner()
    monkeypatch.setattr(model_load, "_fetch_system", REAL_FETCH)
    monkeypatch.setattr(model_load.httpc, "get", r.get)
    monkeypatch.setattr(embeddings.httpc, "post", r.post)
    monkeypatch.setattr(model_load, "load_timeout_s", lambda model: 5.0)
    return r


# ---- the budget starts after the load -----------------------------------------------

def test_the_rerank_budget_starts_after_the_load(runner):
    hits = [{"text": f"t{i}"} for i in range(3)]
    ranked, ok, why = retrieval._reranked("q", hits, RERANKER, 0.1, 50)
    assert ok is True and why is None, "a 0.3 s load no longer spends a 0.1 s budget"
    warm, = runner.warms()
    real, = runner.real()
    assert warm["timeout"] == 5.0, "the warm is bounded by the LOAD bound"
    assert real["timeout"] == 0.1, "the rerank keeps its own budget, whole"
    assert runner.calls.index(warm) < runner.calls.index(real)
    cold, = model_load.recent()
    assert (cold.model, cold.caller, cold.outcome) == ("louise-rerank-bge", "rerank", "loaded")
    assert cold.seconds >= 0.3


def test_an_eviction_after_a_cached_check_is_still_warmed_outside_the_rerank_budget(runner):
    """2026-10-25: a search's query embedding read `/system` (the reranker
    loaded), another model's load then evicted the idle reranker, and the
    rerank's check believed the cached answer: the load went INSIDE the
    budget. The rerank asks fresh."""
    runner.loaded.add("louise-rerank-bge")
    assert "louise-rerank-bge" in model_load.loaded_models(SERVER), "the query side's check"
    runner.evict("louise-rerank-bge")
    hits = [{"text": f"t{i}"} for i in range(3)]
    ranked, ok, why = retrieval._reranked("q", hits, RERANKER, 0.1, 50)
    assert ok is True and why is None, "the 0.3 s load stays out of the 0.1 s budget"
    assert len(runner.warms()) == 1 and runner.real()[0]["timeout"] == 0.1


def test_a_cold_start_the_rerank_paid_inside_its_budget_is_named_and_recorded(runner, published):
    """Evicted between the check and the request (the reclaimer is another
    process): the request loads it inside its budget and times out. The
    fallback says so, and the event log has the cold start."""
    runner.loaded.add("louise-rerank-bge")
    runner.evict_before.add("louise-rerank-bge")
    hits = [{"text": f"t{i}"} for i in range(3)]
    ranked, ok, why = retrieval._reranked("q", hits, RERANKER, 0.1, 50)
    assert ok is False and ranked == hits
    assert why == "the reranker was evicted before the request and loaded again inside its 0.1 s budget"
    cold, = model_load.recent()
    assert (cold.model, cold.caller) == ("louise-rerank-bge", "rerank") and cold.outcome.startswith("failed: evicted")
    assert "evicted between the check and the request" in model_load.message(cold)


def test_a_rerank_that_is_merely_slow_keeps_its_own_reason(runner, monkeypatch):
    """A warm reranker that does not answer in time is not a cold start."""
    runner.loaded.add("louise-rerank-bge")

    def slow(url, json=None, timeout=None, **_kw):     # noqa: A002
        raise httpx.ReadTimeout("timed out", request=httpx.Request("POST", url))
    monkeypatch.setattr(embeddings.httpc, "post", slow)
    ranked, ok, why = retrieval._reranked("q", [{"text": "t"}], RERANKER, 0.1, 50)
    assert ok is False and why == "the reranker did not answer within 0.1 s"
    assert model_load.recent() == []


def test_corpus_work_keeps_the_cached_answer_and_a_person_s_burst_shares_one_read(runner):
    from app import dispatch
    runner.loaded.add("vega-bge-m3")
    with dispatch.working_as(dispatch.Level.CORPUS):
        embeddings.embed_calibration(ENCODER, ["a"], timeout=0.1)
        time.sleep(model_load.INTERACTIVE_TTL_S + 0.05)
        embeddings.embed_calibration(ENCODER, ["b"], timeout=0.1)
    assert runner.system_reads == 1, "the worker's hot path: one read per SYSTEM_TTL_S"
    for text in ("c", "d", "e"):
        embeddings.embed_calibration(ENCODER, [text], query=True, timeout=0.1)
    assert runner.system_reads == 2, "a query's variants, one read; older answers are not trusted"


def test_a_loaded_model_is_not_warmed_and_system_is_asked_once_per_ttl(runner):
    runner.loaded.add("vega-bge-m3")
    for _ in range(5):
        embeddings.embed_calibration(ENCODER, ["a text"], timeout=0.1)
    assert runner.warms() == [] and len(runner.real()) == 5
    assert runner.system_reads == 1, "cached: a hot path adds no request per call"


def test_query_embedding_timeout_excludes_the_load(runner):
    v = embeddings.embed_calibration(ENCODER, ["a question"], query=True, timeout=0.1)
    assert len(v) == 1
    assert [c["timeout"] for c in runner.calls] == [5.0, 0.1]


def test_a_pair_layer_is_warmed_with_a_pair(runner):
    embeddings.embed_calibration(ENCODER, [("first", "second")], timeout=0.1)
    warm, = runner.warms()
    assert warm["json"]["input_pairs"] == [["warm", "warm"]]


def test_translation_timeout_excludes_the_load(runner, monkeypatch):
    monkeypatch.setattr(config, "TRANSLATE_URL", SERVER)
    monkeypatch.setattr(config, "TRANSLATE_MODEL", "louise-translate-madlad")
    monkeypatch.setattr(translate, "TIMEOUT_S", 0.1)
    monkeypatch.setattr(translate.httpx, "post", runner.post)
    assert translate.translate([("whale", "it")]) == ["[it] whale"]
    assert [c["timeout"] for c in runner.calls] == [5.0, 0.1]


def test_chat_timeout_excludes_the_load(runner, monkeypatch):
    monkeypatch.setattr(config, "CHAT_API", "openai")
    monkeypatch.setattr(config, "CHAT_URL", SERVER)
    monkeypatch.setattr(config, "CHAT_MODEL", "louise-chat-qwen3-8b")
    monkeypatch.setattr(config, "LLM_TIMEOUT", 0.1)
    monkeypatch.setattr(llm.httpx, "post", runner.post)

    class Client:
        def __init__(self, timeout=None):
            self.timeout = timeout

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, json=None, headers=None):      # noqa: A002
            return runner.post(url, json=json, timeout=self.timeout, headers=headers)
    monkeypatch.setattr(llm.httpx, "Client", Client)
    assert llm.chat([{"role": "user", "content": "capital of Italy?"}]) == "Rome."
    assert [c["timeout"] for c in runner.calls] == [5.0, 0.1]
    warm = runner.warms()[0]["json"]
    assert warm["max_tokens"] == 1 and warm["model"] == "louise-chat-qwen3-8b"


# ---- one warm per model ---------------------------------------------------------------

def test_two_callers_finding_one_model_cold_send_one_warm(runner):
    barrier = threading.Barrier(2)
    out: list = []

    def ask():
        barrier.wait()
        out.append(embeddings.embed_calibration(ENCODER, ["a question"], query=True,
                                                timeout=0.1))
    threads = [threading.Thread(target=ask) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert len(out) == 2
    assert len(runner.warms()) == 1, "single-flight per (server, model)"
    assert len(runner.real()) == 2 and all(c["timeout"] == 0.1 for c in runner.real())


def test_a_waiter_is_bounded_too(monkeypatch):
    monkeypatch.setattr(model_load, "_fetch_system", lambda server: frozenset())
    monkeypatch.setattr(model_load, "load_timeout_s", lambda model: 0.2)
    started = threading.Event()
    release = threading.Event()

    def slow(bound):
        started.set()
        release.wait(5)
    t = threading.Thread(target=lambda: model_load.ensure_loaded(SERVER, "m", slow, caller="a"))
    t.start()
    started.wait(5)
    t0 = time.monotonic()
    with pytest.raises(model_load.LoadTimeout, match="longer than 0.2 s"):
        model_load.ensure_loaded(SERVER, "m", lambda b: None, caller="b")
    assert time.monotonic() - t0 < 2
    release.set()
    t.join(5)


# ---- a load past its bound ---------------------------------------------------------------

@pytest.fixture
def slow_runner(runner, monkeypatch):
    runner.load_s = 0.5
    monkeypatch.setattr(model_load, "load_timeout_s", lambda model: 0.1)
    return runner


def test_a_load_past_its_bound_defers_embedding_and_errors_no_document(
        isolated_config, tmp_path, monkeypatch, slow_runner):
    monkeypatch.setattr(worker, "_endpoint_paused", {})
    monkeypatch.setattr(config, "BACKEND_GATE", "off")
    backend_gate.reset_cache()
    c = _collection(tmp_path, monkeypatch, concurrency=1)
    c = dataclasses.replace(c, embed=collections_mod.EmbedRecipe(
        url=SERVER, api="openai", model="qwen", dtype="f16", ctx=512, dim=8))
    con = db.connect(c)
    with pytest.raises(model_load.LoadTimeout) as e:
        embeddings.embed_chunks(c, ["a chunk"], timeout=60)
    assert "model loading took longer than 0.1 s (qwen)" in str(e.value)
    assert worker.endpoint_unavailable(e.value), "a deferral"
    assert slow_runner.real() == [], "the real request is not sent past a failed load"
    assert worker._execute_step(con, c, "embed", _row(con)) is False
    assert _untouched(con), "never an error on a document"
    backend_gate.reset_cache()


def test_a_load_past_its_bound_withholds_the_vector_leg_with_its_reason(
        isolated_config, tmp_path, monkeypatch, runner):
    monkeypatch.setattr(worker, "_endpoint_paused", {})
    monkeypatch.setattr(config, "BACKEND_GATE", "off")
    backend_gate.reset_cache()
    c = _collection(tmp_path, monkeypatch, concurrency=1)
    c = dataclasses.replace(c, embed=collections_mod.EmbedRecipe(
        url=SERVER, api="openai", model="qwen", dtype="f16", ctx=512, dim=8))
    con = db.connect(c)
    worker._execute_step(con, c, "embed", _row(con))            # a vector table exists
    runner.loaded.clear()                                       # evicted since
    runner.load_s = 0.5
    monkeypatch.setattr(model_load, "load_timeout_s", lambda model: 0.1)
    model_load.reset()
    result = retrieval.search(con, "abstract", mode="dense", translate=False, collection=c)
    leg, = result["legs_withheld"]
    assert leg["leg"] == "vector"
    assert leg["reason"] == "model loading took longer than 0.1 s (qwen)"
    backend_gate.reset_cache()


def test_a_load_past_its_bound_is_the_fused_order_with_its_reason(slow_runner):
    hits = [{"text": f"t{i}"} for i in range(3)]
    ranked, ok, why = retrieval._reranked("q", hits, RERANKER, 8.0, 50)
    assert (ranked, ok) == (hits, False)
    assert why == "model loading took longer than 0.1 s (louise-rerank-bge)"
    assert slow_runner.real() == []
    cold, = model_load.recent()
    assert cold.outcome == "timeout" and "still loading after 0.1 s" in model_load.message(cold)


def test_the_reason_reaches_the_search_result(slow_runner, monkeypatch, tmp_path):
    hits = [{"chunk_id": f"a:{i}:0:1", "book_id": i, "source": "dense", "text": f"t{i}"}
            for i in range(3)]
    monkeypatch.setattr(retrieval.refs.RESOLVER, "resolve", lambda q, s: [])
    monkeypatch.setattr(retrieval, "_search_one_collection",
                        lambda con, c, q, k, mode, tr, ids, plan=None, depth=0: {
                            "catalog": [], "merged": hits, "translations": {},
                            "timings": {"catalog": 0.0}, "scope": {}})
    monkeypatch.setattr(retrieval, "_attach_work_fields", lambda con, c, h, q: h)
    monkeypatch.setattr(retrieval, "_attach_metadata_and_enqueue", lambda con, c, h: None)
    monkeypatch.setattr(collections_mod, "reranker_for", lambda c: RERANKER)
    from types import SimpleNamespace
    col = SimpleNamespace(name="wikipedia", plugin="wikipedia")
    out = retrieval.search(None, "q", collection=col, translate=False)
    assert out["reranked"] is False
    assert out["rerank_withheld"] == [{"collection": "wikipedia", "reason":
                                       "model loading took longer than 0.1 s (louise-rerank-bge)"}]


# ---- nothing known: nothing changes ----------------------------------------------------

def test_a_runner_without_system_is_asked_as_before(runner):
    runner.system_status = 404
    for _ in range(3):
        embeddings.embed_calibration(ENCODER, ["a text"], timeout=60)
    assert runner.warms() == [] and len(runner.real()) == 3
    assert runner.system_reads == 1, "a server without the route is left alone"
    hits = [{"text": "t"}]
    retrieval._reranked("q", hits, RERANKER, 0.1, 50)
    assert runner.warms() == [], "the rerank too: its budget holds the load, as before"


def test_an_unreachable_system_changes_nothing(runner, monkeypatch):
    def down(url, timeout=None, **_kw):
        raise httpx.ConnectError("refused", request=httpx.Request("GET", url))
    monkeypatch.setattr(model_load.httpc, "get", down)
    ranked, ok, why = retrieval._reranked("q", [{"text": "t"}], RERANKER, 0.1, 50)
    # the load spent the budget, as it always did — and the fallback says so
    # (05 §5.6, amended 2026-10-10: every fallback carries its reason)
    assert (ok, why) == (False, "the reranker did not answer within 0.1 s")
    assert runner.warms() == []


def test_a_model_unnamed_is_never_warmed(runner):
    recipe = collections_mod.EmbedRecipe(url=SERVER, api="openai", model="", dtype="f32",
                                         ctx=512)
    embeddings.embed_calibration(recipe, ["a text"], timeout=60)
    assert runner.system_reads == 0 and runner.warms() == []


def test_server_of_is_the_runners_root():
    assert model_load.server_of("http://h:30286/v1/rerank") == "http://h:30286"
    assert model_load.server_of("http://h:30286") == "http://h:30286"


# ---- through VeGa ---------------------------------------------------------------------

def test_the_vega_receipt_path_is_still_verified_after_a_warm(localai, vega, monkeypatch):
    held: set[str] = set()
    monkeypatch.setattr(model_load, "_fetch_system",
                        lambda server: frozenset(held) if server == VEGA_LOCALAI else None)
    monkeypatch.setattr(model_load, "load_timeout_s", lambda model: 5.0)
    post = localai.post

    def loading(url, **kw):
        held.add(VEGA_MODEL)
        return post(url, **kw)
    monkeypatch.setattr(embeddings.httpc, "post", loading)
    seen: set = set()
    vectors = embeddings.embed_calibration(VEGA_RECIPE, ["one", "two"], runs=seen, raw=True)
    assert vectors == [vector_of("text", "one"), vector_of("text", "two")]
    assert seen == {R1}, "the receipts' run is the answer's run"
    warm, real = localai.bodies
    from app.vega import tag as vtag
    assert [vtag.parse(s)[1:] for s in warm["input"]] == [("text", "warm")], \
        "the warm is tagged: the wrapper refuses an untagged input"
    assert warm["model"] == VEGA_MODEL and warm["encoding_format"] == "base64"
    assert [vtag.parse(s)[1:] for s in real["input"]] == [("text", "one"), ("text", "two")]
    assert vega.reads == 1, "the real batch's receipts are read; the warm's never"


# ---- the bound ------------------------------------------------------------------------

def test_models_load_timeout_s_is_platform_toml(tmp_path):
    p = tmp_path / "platform.toml"
    p.write_text("[models]\nload_timeout_s = 45\n", encoding="utf-8")
    assert platform_config._load_one(p).models.load_timeout_s == 45
    p.write_text("", encoding="utf-8")
    assert platform_config._load_one(p).models.load_timeout_s == 120, "07 §7.8's default"
    p.write_text("[models]\nload_timeout_s = 0\n", encoding="utf-8")
    with pytest.raises(Exception, match="load_timeout_s"):
        platform_config._load_one(p)


def test_a_measured_model_has_its_own_bound_and_others_the_nodes(monkeypatch, tmp_path):
    bounds = tmp_path / "model_loads.json"
    bounds.write_text(jsonlib.dumps({"models": {"vega-bge-m3": {"load_timeout_s": 30}}}),
                      encoding="utf-8")
    monkeypatch.setattr(model_load, "BOUNDS_FILE", bounds)
    model_load.reset()
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / "platform.toml").write_text("[models]\nload_timeout_s = 77\n", encoding="utf-8")
    monkeypatch.setenv("CONFIG_DIR", str(cfg))
    assert model_load.load_timeout_s("vega-bge-m3") == 30
    assert model_load.load_timeout_s("never-measured") == 77


def test_the_shipped_bounds_name_every_model_louise_uses():
    """app/model_loads.json is the node's profile (docs/notes/model-profile.md)
    reduced to one bound per model; a bound under the slowest cold start
    measured would turn every first load into a fallback."""
    raw = jsonlib.loads(model_load.BOUNDS_FILE.read_text(encoding="utf-8"))
    for name in ("vega-qwen3-embedding-4b", "vega-bge-m3", "vega-granite", "vega-medcpt-query",
                 "vega-medcpt-article", "vega-medcpt-article-ov", "louise-rerank-bge",
                 "louise-rerank-medcpt", "louise-translate-madlad", "louise-chat-qwen3-8b"):
        m = raw["models"][name]
        assert m["load_timeout_s"] >= max(30, m["cold_max_s"]), name


def test_a_cold_start_is_published_by_another_thread(monkeypatch):
    got: list = []
    done = threading.Event()

    def publish(c):
        got.append((c.model, threading.current_thread().name))
        done.set()
    monkeypatch.setattr(model_load, "publish", publish)
    monkeypatch.setattr(model_load, "_fetch_system", lambda server: frozenset())
    model_load.ensure_loaded(SERVER, "m", lambda bound: None, caller="test")
    assert done.wait(5)
    assert got == [("m", "model-load-log")], "never by the thread doing the work"
