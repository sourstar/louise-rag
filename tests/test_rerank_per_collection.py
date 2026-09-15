"""Per-collection rerankers (05 §5.6, 07 §7.8).

Each collection's list is reranked on its own, by that collection's
reranker, and only then fused by rank — two spaces are never put on one
score scale. The top 50 of a list are re-ordered and the rest keep the fused
order below them; a candidate is sent whole; a reranker that is
unreachable, refuses, answers for only some candidates or exceeds `[ask]
latency_budget_ms` leaves the fused order, and the result says so.
"""
import dataclasses

import httpx
import pytest

from app import collections as collections_mod, config, db, platform_config, retrieval


def _col(tmp_path, name, reranker=None):
    d = tmp_path / name
    d.mkdir(exist_ok=True)
    return collections_mod.Collection(
        name=name, plugin="gutenberg", profile=None, embed_mode_default=None,
        corpus_dir=d / "corpus", manifest_path=d / "manifest.db",
        fts_dir=d / "fts", lance_dir=d / "lancedb", reranker=reranker)


def _hits(prefix, n):
    return [{"chunk_id": f"{prefix}:{i}:0:1", "book_id": i, "source": "dense",
             "text": f"{prefix} text {i}"} for i in range(n)]


class _Resp:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


def _server(monkeypatch, *, fail=None, answer=None):
    """A reranker that prefers the LAST candidate — it reverses the list —
    speaking whichever format it is asked in."""
    calls = []

    def post(url, json=None, timeout=None):
        calls.append({"url": url, "json": json, "timeout": timeout})
        if fail is not None:
            raise fail
        jina = "documents" in json
        n = len(json["documents"] if jina else json["texts"])
        indices = answer(n) if answer else range(n)
        if jina:
            return _Resp({"results": [{"index": i, "relevance_score": float(i)}
                                      for i in indices]})
        return _Resp([{"index": i, "score": float(i)} for i in indices])
    monkeypatch.setattr(retrieval.httpx, "post", post)
    return calls


JINA = collections_mod.RerankRecipe(url="http://gpu:30447/v1/rerank", api="jina",
                                    model="bge-reranker-v2-m3")
TEI = collections_mod.RerankRecipe(url="http://cpu:30445/rerank")


def test_the_top_50_are_reordered_and_the_rest_keep_the_fused_order(monkeypatch):
    calls = _server(monkeypatch)
    out, ok = retrieval._rerank_list("q", _hits("c", 60), JINA, 0.4)
    assert ok
    assert [h["book_id"] for h in out[:50]] == list(range(49, -1, -1))
    assert [h["book_id"] for h in out[50:]] == list(range(50, 60))
    assert out[0]["rerank_score"] == 49.0
    body = calls[0]["json"]
    assert len(body["documents"]) == 50 and body["model"] == "bge-reranker-v2-m3"
    assert calls[0]["timeout"] == 0.4, "the node's budget bounds the call"


def test_tei_format_and_every_candidate_sent_whole(monkeypatch):
    calls = _server(monkeypatch)
    hits = _hits("c", 3)
    hits[0]["text"] = "x" * 5000
    out, ok = retrieval._rerank_list("q", hits, TEI, 0.4)
    assert ok and [h["book_id"] for h in out] == [2, 1, 0]
    assert calls[0]["json"]["texts"][0] == "x" * 5000, "never cut to 2,000 characters"


@pytest.mark.parametrize("failure", [httpx.ReadTimeout("over budget"),
                                     httpx.ConnectError("unreachable"),
                                     RuntimeError("refused")])
def test_a_failing_reranker_leaves_the_fused_order_and_says_so(monkeypatch, failure):
    _server(monkeypatch, fail=failure)
    hits = _hits("c", 5)
    assert retrieval._rerank_list("q", hits, JINA, 0.4) == (hits, False)


def test_an_answer_for_only_some_candidates_is_not_an_order(monkeypatch):
    _server(monkeypatch, answer=lambda n: range(n - 1))
    hits = _hits("c", 5)
    assert retrieval._rerank_list("q", hits, JINA, 0.4) == (hits, False)


def test_no_reranker_keeps_the_fused_order_and_calls_nothing(monkeypatch):
    calls = _server(monkeypatch)
    hits = _hits("c", 5)
    assert retrieval._rerank_list("q", hits, None, 0.4) == (hits, False)
    assert calls == []


def test_a_collection_file_reranker_wins_over_RERANK_URL(monkeypatch, tmp_path):
    c = _col(tmp_path, "c")
    monkeypatch.setattr(config, "RERANK_URL", "")
    assert collections_mod.reranker_for(c) is None
    monkeypatch.setattr(config, "RERANK_URL", "http://env/rerank")
    assert collections_mod.reranker_for(c) == collections_mod.RerankRecipe(
        url="http://env/rerank", api="tei")
    assert collections_mod.reranker_for(dataclasses.replace(c, reranker=JINA)) is JINA


def test_the_collection_file_declares_its_reranker(isolated_config, config_dir):
    d = config_dir / "collections"
    d.mkdir(parents=True, exist_ok=True)
    (d / "r.toml").write_text(
        'plugin = "gutenberg"\n[reranker]\nurl = "http://gpu:30447/v1/rerank"\n'
        'api = "jina"\nmodel = "bge-reranker-v2-m3"\n', encoding="utf-8")
    assert collections_mod.by_name("r").reranker == JINA
    (d / "r.toml").write_text(
        'plugin = "gutenberg"\n[reranker]\nurl = "http://x"\napi = "cohere"\n',
        encoding="utf-8")
    with pytest.raises(ValueError, match="api must be one of"):
        collections_mod.load_all()
    (d / "r.toml").write_text('plugin = "gutenberg"\n[reranker]\napi = "jina"\n',
                              encoding="utf-8")
    with pytest.raises(ValueError, match="needs a url"):
        collections_mod.load_all()


def test_the_budget_is_platform_toml_ask_latency_budget_ms(tmp_path):
    p = tmp_path / "platform.toml"
    p.write_text("[ask]\nlatency_budget_ms = 1500\n", encoding="utf-8")
    assert platform_config._load_one(p).ask.latency_budget_ms == 1500
    p.write_text("", encoding="utf-8")
    assert platform_config._load_one(p).ask.latency_budget_ms == 400, "07 §7.8's default"
    p.write_text("[ask]\nlatency_budget_ms = 0\n", encoding="utf-8")
    with pytest.raises(Exception, match="latency_budget_ms"):
        platform_config._load_one(p)


def _stub_pipeline(monkeypatch, lists):
    monkeypatch.setattr(retrieval, "_search_one_collection",
                        lambda con, c, q, k, mode, tr, tagged: {
                            "catalog": [], "merged": lists[c.name], "translations": {},
                            "timings": {"catalog": 0.0}, "scope": {}})
    monkeypatch.setattr(retrieval, "_tagged_book_ids", lambda con, c, ids: set())
    monkeypatch.setattr(retrieval, "_attach_work_fields", lambda con, c, hits, q: hits)
    monkeypatch.setattr(retrieval, "_attach_metadata_and_enqueue", lambda con, c, hits: None)
    monkeypatch.setattr(retrieval.refs, "get_store", lambda: None)
    monkeypatch.setattr(retrieval.refs.RESOLVER, "resolve", lambda q, s: [])
    monkeypatch.setattr(retrieval, "_rerank_budget_s", lambda: 0.4)


def test_federated_search_reranks_each_collection_on_its_own(isolated_config, monkeypatch,
                                                             tmp_path):
    a = _col(tmp_path, "a", dataclasses.replace(JINA, url="http://a/v1/rerank"))
    b = _col(tmp_path, "b", dataclasses.replace(JINA, url="http://b/v1/rerank"))
    _stub_pipeline(monkeypatch, {"a": _hits("a", 5), "b": _hits("b", 5)})
    calls = _server(monkeypatch)

    out = retrieval.federated_search([a, b], "q", k=4)
    sent = {c["url"]: c["json"]["documents"] for c in calls}
    assert all(t.startswith("a ") for t in sent["http://a/v1/rerank"])
    assert all(t.startswith("b ") for t in sent["http://b/v1/rerank"]), \
        "each list reaches only its own reranker"
    assert out["reranked"] == {"a": True, "b": True}
    # fused BY RANK after each list's own reranking: both rank-1s lead
    assert [(h["source_collection"], h["book_id"]) for h in out["hits"][:2]] == \
        [("a", 4), ("b", 4)]


def test_a_collection_without_a_reranker_keeps_its_order_beside_one_with(
        isolated_config, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "RERANK_URL", "")
    a = _col(tmp_path, "a", JINA)
    b = _col(tmp_path, "b")
    _stub_pipeline(monkeypatch, {"a": _hits("a", 3), "b": _hits("b", 3)})
    _server(monkeypatch)
    out = retrieval.federated_search([a, b], "q", k=6)
    assert out["reranked"] == {"a": True, "b": False}
    assert [h["book_id"] for h in out["hits"] if h["source_collection"] == "b"] == [0, 1, 2]


def test_search_says_whether_its_list_was_reranked(isolated_config, monkeypatch, tmp_path):
    c = _col(tmp_path, "c", JINA)
    _stub_pipeline(monkeypatch, {"c": _hits("c", 3)})
    _server(monkeypatch)
    out = retrieval.search(db.connect(c), "q", k=3, collection=c)
    assert out["reranked"] is True and [h["book_id"] for h in out["hits"]] == [2, 1, 0]
    _server(monkeypatch, fail=httpx.ReadTimeout("over budget"))
    out = retrieval.search(db.connect(c), "q", k=3, collection=c)
    assert out["reranked"] is False and [h["book_id"] for h in out["hits"]] == [0, 1, 2]
