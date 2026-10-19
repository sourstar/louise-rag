"""An embedder is held only for a collection that embeds everything
(operator, 2026-10-14; app/worker._release_embedder, model_load.unload): the
start-up run check and a switch to off or on-demand unload it from LocalAI,
unless another collection on "all" uses the same model on the same runner.
Found when gutenberg's idle 4B embedder held 9.9 GB, the card crossed
LocalAI's eviction threshold and every chat load was evicted (500s)."""
from contextlib import closing
from dataclasses import replace

import pytest

from app import collections as collections_mod, db, model_load, worker
from tests.test_config_tiers_embed_mode import _collection

URL = "http://localai:8080"


def _with(c, model, mode):
    recipe = collections_mod.EmbedRecipe(url=URL, api="vega", model=model, dtype="f16", ctx=512)
    return replace(c, embed=recipe, embed_mode_default=mode)


@pytest.fixture
def runner(monkeypatch):
    model_load.reset()
    held = {"vega-4b", "vega-dar"}
    calls = []

    class R:
        def __init__(self, code): self.status_code = code
    monkeypatch.setattr(model_load, "_fetch_system", lambda server: frozenset(held))
    cpu: dict = {}                               # model -> cpu_percent; absent = idle
    monkeypatch.setattr(model_load, "activity", lambda server: {m: cpu.get(m, 0.0) for m in held})
    worker._idle_beats.clear()

    def post(url, json=None, timeout=None):
        calls.append((url, json))
        held.discard(json["model"])
        return R(200)
    monkeypatch.setattr(model_load.httpc, "post", post)
    yield held, calls
    model_load.reset()


def test_unload_asks_localai_only_for_a_model_it_holds(runner):
    held, calls = runner
    assert model_load.unload(URL + "/v1", "vega-4b") is True
    assert calls == [(URL + "/backend/shutdown", {"model": "vega-4b"})]
    assert model_load.unload(URL, "vega-4b") is False, "not held any more: nothing asked"
    assert model_load.unload(URL, None) is False and len(calls) == 1


def test_an_embedder_is_released_unless_its_mode_is_all(tmp_path, runner):
    held, calls = runner
    lazy = _with(_collection(tmp_path, "gutenberg"), "vega-4b", "lazy")
    full = _with(_collection(tmp_path, "normattiva"), "vega-dar", "all")
    assert worker._release_embedder(full, others=[lazy, full]) is False
    assert worker._release_embedder(lazy, others=[lazy, full]) is True
    assert held == {"vega-dar"} and calls == [(URL + "/backend/shutdown", {"model": "vega-4b"})]


def test_a_model_another_collection_embeds_everything_with_stays(tmp_path, runner):
    held, calls = runner
    off = _with(_collection(tmp_path, "a"), "vega-dar", "off")
    full = _with(_collection(tmp_path, "b"), "vega-dar", "all")
    assert worker._release_embedder(off, others=[off, full]) is False and calls == []


def test_the_runtime_mode_wins_over_the_file(tmp_path, runner):
    held, calls = runner
    c = _with(_collection(tmp_path, "gutenberg"), "vega-4b", "lazy")
    con = db.connect(c)
    scope = db.get_scope(con); scope["embed_mode"] = "all"; db.set_scope(con, scope)
    assert worker._release_embedder(c, con, others=[c]) is False, "the Library set 'all'"
    scope["embed_mode"] = "off"; db.set_scope(con, scope)
    assert worker._release_embedder(c, con, others=[c], reason="the switch") is True
    msgs = [r[0] for r in con.execute("SELECT message FROM events WHERE stage='control'")]
    assert any("embedder vega-4b unloaded on the switch" in m for m in msgs)
    con.close()


def test_a_switch_away_from_all_releases_the_embedder(tmp_path, runner, monkeypatch):
    held, calls = runner
    before = _with(_collection(tmp_path, "gutenberg"), "vega-4b", "all")
    con = db.connect(before)
    after = replace(before, embed_mode_default="lazy")
    monkeypatch.setattr(collections_mod, "load_all", lambda: [after])
    monkeypatch.setattr(worker, "_discover", lambda *a, **k: None)
    worker._sync_collections([before], {before.name: con}, {}, pipeline=False)
    assert (URL + "/backend/shutdown", {"model": "vega-4b"}) in calls
    con.close()


def test_an_embedder_no_held_collection_names_is_swept(tmp_path, runner):
    """deposit-granite removed, vega-granite stayed loaded (2026-10-14): a
    `vega-` model no held collection names, as document or query model, is
    unloaded; chat and the rest are never touched."""
    held, calls = runner
    held |= {"vega-granite", "louise-chat-qwen3-8b"}
    keep = replace(_with(_collection(tmp_path, "normattiva"), "vega-dar", "all"))
    keep = replace(keep, embed=replace(keep.embed, query_model="vega-4b"))
    gone = worker._sweep_unheld_embedders([keep])
    assert gone == ["vega-granite"]
    assert held == {"vega-4b", "vega-dar", "louise-chat-qwen3-8b"}, "the query model and chat stay"


def test_a_removed_collection_s_embedder_leaves_with_it(tmp_path, runner, monkeypatch):
    held, calls = runner
    gone_c = _with(_collection(tmp_path, "deposit"), "vega-4b", "all")
    stays = _with(_collection(tmp_path, "normattiva"), "vega-dar", "all")
    cons = {gone_c.name: db.connect(gone_c), stays.name: db.connect(stays)}
    monkeypatch.setattr(collections_mod, "load_all", lambda: [stays])
    monkeypatch.setattr(worker, "_discover", lambda *a, **k: None)
    worker._sync_collections([gone_c, stays], cons, {}, pipeline=False)
    assert held == {"vega-dar"} and (URL + "/backend/shutdown", {"model": "vega-4b"}) in calls
    cons[stays.name].close()


def test_an_on_demand_embedder_is_unloaded_once_idle_two_beats(tmp_path, runner, monkeypatch):
    """gutenberg and wikipedia (on-demand) still held their embedders: a
    search loads the query encoder, which IS the embedder. A model named only
    by collections not on "all" goes after two idle beats; one in use stays."""
    held, calls = runner
    lazy = _with(_collection(tmp_path, "gutenberg"), "vega-4b", "lazy")
    full = _with(_collection(tmp_path, "normattiva"), "vega-dar", "all")
    assert worker._sweep_unheld_embedders([lazy, full]) == [], "idle one beat: kept"
    assert worker._sweep_unheld_embedders([lazy, full]) == ["vega-4b"], "idle two beats: unloaded"
    assert held == {"vega-dar"}, "the 'all' collection's embedder stays, idle or not"


def test_a_busy_on_demand_embedder_is_not_unloaded(tmp_path, monkeypatch):
    model_load.reset()
    worker._idle_beats.clear()
    held = {"vega-4b"}
    cpu = {"vega-4b": 0.0}
    monkeypatch.setattr(model_load, "_fetch_system", lambda server: frozenset(held))
    monkeypatch.setattr(model_load, "activity", lambda server: {m: cpu[m] for m in held})
    calls = []
    monkeypatch.setattr(model_load.httpc, "post", lambda url, json=None, timeout=None: calls.append(json))
    lazy = _with(_collection(tmp_path, "gutenberg"), "vega-4b", "lazy")
    assert worker._sweep_unheld_embedders([lazy]) == []
    cpu["vega-4b"] = 35.0                        # a search is using it
    assert worker._sweep_unheld_embedders([lazy]) == [] and calls == [], "busy resets the count"
    model_load.reset()


# ---- encoder leases (Perugia, 2026-10-19) ---------------------------------------------

def test_a_leased_embedder_is_left_alone_until_the_lease_expires(tmp_path, isolated_config, monkeypatch):
    """`share contribute` embedded with vega-qwen3-embedding-4b at 142
    chunks/min while LocalAI reported its backend at 0–2.5 % CPU, 0.0 one
    sample in four: the sweep stopped it seven times in 3.4 h, 19 s a cold
    start. A process embedding outside the worker leases the model; the
    sweep skips a leased model however idle it reads, and a lease a dead
    process left expires on its own."""
    from app import node_state
    model_load.reset()
    worker._idle_beats.clear()
    held = {"vega-4b"}
    monkeypatch.setattr(model_load, "_fetch_system", lambda server: frozenset(held))
    monkeypatch.setattr(model_load, "activity", lambda server: {m: 0.0 for m in held})
    calls = []

    class R:
        status_code = 200

        def json(self):
            return {}
    monkeypatch.setattr(model_load.httpc, "post",
                        lambda url, json=None, timeout=None: (calls.append(json), held.discard(json["model"]), R())[-1])
    off = _with(_collection(tmp_path, "gutenberg"), "vega-4b", "off")
    node_state.lease_encoder(URL, "vega-4b", "share contribute pid 1", now=1000.0, ttl_s=600.0)
    real = node_state.leased_encoders
    monkeypatch.setattr(node_state, "leased_encoders", lambda con=None, now=None: real(con, 1300.0))
    # three idle beats under the lease: nothing goes
    for _ in range(3):
        assert worker._sweep_unheld_embedders([off]) == [] and calls == []
    # the lease ran out (the process died without releasing): two idle beats, then unloaded
    monkeypatch.setattr(node_state, "leased_encoders", lambda con=None, now=None: real(con, 1700.0))
    assert worker._sweep_unheld_embedders([off]) == []
    assert worker._sweep_unheld_embedders([off]) == ["vega-4b"] and calls == [{"model": "vega-4b"}]
    model_load.reset()


def test_the_lease_is_renewed_per_batch_and_released_with_the_command(tmp_path, isolated_config, monkeypatch):
    from app import louise, node_state
    recipe = collections_mod.EmbedRecipe(url=URL + "/v1/embeddings", api="vega", model="vega-4b",
                                         dtype="f16", ctx=512)
    seen = []
    embed = louise._leasing(lambda chunks: seen.append(len(chunks)) or [], recipe,
                            holder="share contribute pid 7")
    assert node_state.leased_encoders() == set()
    embed([1, 2, 3])
    assert node_state.leased_encoders() == {(URL, "vega-4b")} and seen == [3]
    embed([4])
    assert node_state.leased_encoders() == {(URL, "vega-4b")}, "renewed, one row"
    with closing(node_state.connect()) as con:
        assert con.execute("SELECT count(*) FROM encoder_leases").fetchone()[0] == 1
    louise._release_lease("share contribute pid 7")
    assert node_state.leased_encoders() == set()
    # a store that cannot be written never stops the embed
    monkeypatch.setattr(node_state, "lease_encoder", lambda *a, **k: (_ for _ in ()).throw(OSError("ro")))
    assert embed([5]) == [] and seen == [3, 1, 1]


def test_the_lease_queries_walk_the_primary_key(isolated_config):
    """A read a settle beat and a write a batch: plan-checked (CLAUDE.md)."""
    from app import node_state
    with closing(node_state.connect()) as con:
        for sql, args in (("SELECT server, model FROM encoder_leases WHERE until > ?", (0.0,)),
                          ("DELETE FROM encoder_leases WHERE holder=?", ("x",))):
            plan = " ".join(str(r[-1]) for r in con.execute("EXPLAIN QUERY PLAN " + sql, args))
            assert "TEMP B-TREE" not in plan, plan
        plan = " ".join(str(r[-1]) for r in con.execute(
            "EXPLAIN QUERY PLAN INSERT OR REPLACE INTO encoder_leases VALUES (?,?,?,?)", ("s", "m", "h", 1.0)))
        assert "TEMP B-TREE" not in plan
