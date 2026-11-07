"""A reranked list is one step of a query, and holds ONE claim (2026-11-07;
docs/notes/scheduling.md §12, app/rage/dispatch.py).

The reranker is asked once per language variant, 9–37 ms apart (scheduling-
log 2026-10-25). Claimed per call, the node's claim cleared between two
variants, and corpus work standing aside could send a request there that
then shared the card with the next variant. The gaps between a query's
STEPS — the plan, the legs, the attach — stay corpus's to fill (operator,
2026-10-25): those still read no claim.
"""
import dataclasses
import time

import pytest

from app import cancel, collections as collections_mod, db, language_plan, node_state, retrieval
from app.rage import dispatch

JINA = collections_mod.RerankRecipe(url="http://gpu:30447/v1/rerank", api="jina",
                                    model="bge-reranker-v2-m3", window=8)


class _Resp:
    status_code = 200

    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


def _claim() -> bool:
    """The claim as another process reads it, fresh (not this process's memo)."""
    dispatch._claim_seen.clear()
    return dispatch.interactive_active()


@pytest.fixture
def trace(isolated_config, monkeypatch):
    """Every claim write and every sample, in order: ("write", until) and
    (step, claim held?)."""
    node_state.connect().close()
    seen: list[tuple[str, object]] = []
    real = dispatch._write_claim
    monkeypatch.setattr(dispatch, "_write_claim",
                        lambda until: seen.append(("write", until)) or real(until))
    return seen


def _two_variants(monkeypatch, tmp_path, seen, *, post=None):
    """A search over one collection whose two hits are in two languages, so
    its list is reranked in two calls: the Italian one against the query as
    typed, the English one against its translation."""
    d = tmp_path / "c"
    d.mkdir()
    c = collections_mod.Collection(
        name="c", plugin="gutenberg", profile=None, embed_mode_default=None,
        corpus_dir=d / "corpus", manifest_path=d / "manifest.db", fts_dir=d / "fts",
        lance_dir=d / "lancedb", reranker=JINA)
    hits = [{"chunk_id": "c:1:0:1", "book_id": 1, "lang": "it", "text": "al polo australe in velocipede"},
            {"chunk_id": "c:2:0:1", "book_id": 2, "lang": "en", "text": "the white whale, Moby-Dick"}]
    note = lambda step: seen.append((step, _claim()))   # noqa: E731
    monkeypatch.setattr(retrieval.language_plan, "plan",
                        lambda q, cs, tr, every=False: note("plan") or language_plan.LanguagePlan(
                            core=q, translations={"en": "the white whale"}, status="ok"))
    monkeypatch.setattr(retrieval, "_search_one_collection",
                        lambda *a, **k: note("legs") or {
                            "catalog": [], "merged": hits, "translations": {"en": "the white whale"},
                            "timings": {"catalog": 0.0}, "scope": {}})
    monkeypatch.setattr(retrieval, "_attach_metadata_and_enqueue", lambda con, c, h: note("attach"))
    monkeypatch.setattr(retrieval, "_one_hit_per_work", lambda ranked, *a, **k: ranked)
    monkeypatch.setattr(retrieval.refs, "get_store", lambda: None)
    monkeypatch.setattr(retrieval.refs.RESOLVER, "resolve", lambda q, s: [])
    monkeypatch.setattr(retrieval, "_warm_reranker", lambda recipe: note("warm"))
    # asked just before each variant's call: the moment between two calls
    monkeypatch.setattr(retrieval.model_load, "backend_pid", lambda url, model: note("before-call"))

    def server(url, json=None, timeout=None, headers=None):
        if url.endswith("/v1/tokenize"):
            note("tokenize")
            return _Resp({"tokens": [1, 2, 3]})          # within the window: no cut
        note("call")
        if post is not None:
            post(json)
        return _Resp({"results": [{"index": i, "relevance_score": 1.0}
                                  for i in range(len(json["documents"]))]})
    monkeypatch.setattr(retrieval.httpc, "post", server)
    return c


def test_a_two_variant_list_holds_the_claim_from_its_warm_to_its_last_call(
        trace, monkeypatch, tmp_path):
    c = _two_variants(monkeypatch, tmp_path, trace)
    out = retrieval.search(db.connect(c), "la balena bianca", k=2, collection=c)
    assert out["reranked"] is True
    steps = [s for s in trace if s[0] != "write"]
    assert [s for s, _ in steps if s == "call"] == ["call", "call"], "one call per variant"

    first = next(i for i, s in enumerate(trace) if s[0] == "warm")
    last = max(i for i, s in enumerate(trace) if s[0] == "call")
    inside = trace[first:last + 1]
    assert all(held for step, held in inside if step != "write"), \
        f"the claim read inactive inside the list: {inside}"
    assert inside.count(("tokenize", True)) == 2 and inside.count(("before-call", True)) == 2
    assert not [w for w in inside if w[0] == "write" and w[1] == 0.0], \
        f"the claim was cleared between two variants: {inside}"
    assert trace[last + 1] == ("write", 0.0), "cleared once, when the list's last call is done"


def test_the_gaps_between_a_query_s_steps_stay_unclaimed(trace, monkeypatch, tmp_path):
    """The operator's 2026-10-25 decision: the plan, the legs and the attach
    are not the rerank's, and corpus work fills them."""
    c = _two_variants(monkeypatch, tmp_path, trace)
    retrieval.search(db.connect(c), "la balena bianca", k=2, collection=c)
    held = {step: claim for step, claim in trace if step in ("plan", "legs", "attach")}
    assert held == {"plan": False, "legs": False, "attach": False}
    assert not _claim() and dispatch._board.live == 0


def _hits():
    return [{"chunk_id": "c:1:0:1", "book_id": 1, "lang": "it", "text": "al polo australe in velocipede"},
            {"chunk_id": "c:2:0:1", "book_id": 2, "lang": "en", "text": "the white whale, Moby-Dick"}]


@pytest.mark.parametrize("raised", [RuntimeError("the cut failed"), cancel.Cancelled()])
def test_a_list_that_raises_clears_its_claim(trace, monkeypatch, tmp_path, raised):
    _two_variants(monkeypatch, tmp_path, trace)

    def fail(texts, recipe):
        assert _claim(), "raised inside the list's claim"
        raise raised
    monkeypatch.setattr(retrieval, "fit_to_window", fail)
    with pytest.raises(type(raised)):
        retrieval._rerank_list("la balena bianca", _hits(), JINA, 5.0,
                               queries={"en": "the white whale"})
    assert trace[-1] == ("write", 0.0) and not _claim() and dispatch._board.live == 0


def test_a_list_cut_by_its_budget_clears_its_claim(trace, monkeypatch, tmp_path):
    """The first variant spends the whole budget: the second is never sent,
    the fused order stands, and the claim goes with the list."""
    _two_variants(monkeypatch, tmp_path, trace, post=lambda body: time.sleep(0.15))
    out, ok = retrieval._rerank_list("la balena bianca", _hits(), JINA, 0.05,
                                     queries={"en": "the white whale"})
    assert ok is False and [h["book_id"] for h in out] == [1, 2]
    assert [s for s, _ in trace if s == "call"] == ["call"], "the second variant was cut"
    assert trace[-1] == ("write", 0.0) and not _claim() and dispatch._board.live == 0


def test_a_list_with_nothing_to_rerank_claims_nothing(trace):
    assert retrieval._rerank_list("q", [], JINA, 5.0) == ([], False)
    assert retrieval._rerank_list("q", _hits(), None, 5.0)[1] is False
    assert trace == [], "no reranker or no hits: no claim written"


def test_a_federated_search_holds_each_list_s_claim(trace, monkeypatch, tmp_path):
    """The lists rerank in parallel on pool threads; each holds its own claim
    and the board counts them: the claim clears when the last list ends."""
    c = _two_variants(monkeypatch, tmp_path, trace)
    b = dataclasses.replace(c, name="b", reranker=dataclasses.replace(JINA, url="http://b/v1/rerank"))
    monkeypatch.setattr(retrieval, "_search_one_collection",
                        lambda con, col, *a, **k: {
                            "catalog": [], "merged": [dict(h) for h in _hits()],
                            "translations": {"en": "the white whale"},
                            "timings": {"catalog": 0.0}, "scope": {}})
    monkeypatch.setattr(retrieval.db, "connect", lambda col: None)
    out = retrieval.federated_search([c, b], "la balena bianca", k=2)
    assert out["reranked"] == {"b": True, "c": True}
    calls = [held for step, held in trace if step in ("before-call", "call", "tokenize")]
    assert len(calls) == 12 and all(calls), trace
    assert not _claim() and dispatch._board.live == 0
