"""Which recommended RAGE setting this node takes (app/rage/settings.py,
09 §9.19): only for a key the collection file leaves unset, only for this
node's card type, and through `collections.recipe_for`, so the gate measures
what the worker sends."""
from __future__ import annotations

import dataclasses
from types import SimpleNamespace

import pytest
from nacl import signing

from app import collections as collections_mod, config, gpu, run_check
from app.rage import settings as rs
from app.hold import rage_settings as es, envelope as env
from tests.hold.conftest import pubmed

ME = signing.SigningKey(b"\x01" * 32)
B70 = es.Card("xe", "8086", "e223")
ENGINE = "sha256:" + "e" * 64


def _gpu_json(*cards):
    return gpu.to_json(gpu.GpuSnapshot(devices=tuple(
        gpu.GpuDevice(card=f"card{i}", pdev=f"0000:0{i}:00.0", driver=c.driver,
                      vendor_id=c.vendor_id, device_id=c.device_id, vendor=None, name=None,
                      vram_total=None, vram_used=None)
        for i, c in enumerate(cards))))


@pytest.fixture
def node(tmp_path, monkeypatch):
    """A node whose probe reports one B70, with a collection `g` of the
    pubmed fixture's Codex."""
    rs.reset()
    monkeypatch.setenv("LOUISE_ROOT", str(tmp_path))
    monkeypatch.setattr(config, "GPU_PROBE_URL", "http://probe:9")
    calls = []

    def fetch(url):
        calls.append(url)
        return _gpu_json(B70)
    monkeypatch.setattr(gpu, "_get_json", fetch)
    monkeypatch.setattr(run_check, "signer", lambda key=None: None)
    codex = pubmed()
    shard = tmp_path / "collections" / "g" / "shard"

    def collection(**raw):
        recipe = collections_mod.parse_recipe(
            {"url": "http://enc", "embed_model": "m", "query_model": "m", **raw}, where="g")
        return SimpleNamespace(name="g", codex=codex, embed=recipe)

    def recommend(batch=16, concurrency=4, card=B70):
        es.record(shard, ME, collection_id=codex.collection_id, space_id=codex.space_id,
                  new=es.entry(card, engine_id=ENGINE, batch=batch, concurrency=concurrency,
                               time="2026-11-01T20:00:00Z"),
                  time="2026-11-01T20:00:00Z")
    yield SimpleNamespace(collection=collection, recommend=recommend, calls=calls, shard=shard)
    rs.reset()


def test_the_file_records_which_keys_it_names():
    r = collections_mod.parse_recipe({"url": "http://e", "embed_model": "m", "query_model": "m",
                                      "batch": 0}, where="x")
    assert r.stated == frozenset({"batch"})


def test_without_a_file_the_recipe_is_the_files(node):
    c = node.collection()
    assert collections_mod.recipe_for(c) is c.embed


def test_what_the_file_leaves_unset_takes_the_recommendation(node):
    node.recommend()
    r = collections_mod.recipe_for(node.collection())
    assert (r.batch, r.concurrency) == (16, 4)


def test_what_the_file_names_wins(node):
    node.recommend()
    r = collections_mod.recipe_for(node.collection(batch=8))
    assert (r.batch, r.concurrency) == (8, 4)
    c = node.collection(batch=8, concurrency=1)
    assert collections_mod.recipe_for(c) is c.embed


def test_a_recommended_concurrency_never_turns_a_gpu_encoder_into_a_cpu_one(node):
    """`spends` is inferred from concurrency when the file names no device."""
    node.recommend()
    c = node.collection()
    assert c.embed.spends == "gpu"
    assert collections_mod.recipe_for(c).spends == "gpu"


def test_another_cards_entry_changes_nothing(node):
    node.recommend(card=es.Card("amdgpu", "1002", "7590"))
    c = node.collection()
    assert collections_mod.recipe_for(c) is c.embed


def test_the_gate_measures_what_the_worker_sends(node, monkeypatch):
    """`run_check.contributing` reads the same recipe: a recommendation that
    changes the batching changes what the gate must have measured."""
    monkeypatch.setattr(config, "EMBED_CONCURRENCY", 1)
    monkeypatch.setattr(config, "EMBED_BATCH", 1)
    c = node.collection()
    before = run_check.contributing(collections_mod.recipe_for(c))
    node.recommend(batch=16, concurrency=4)
    after = run_check.contributing(collections_mod.recipe_for(c))
    assert before is None
    assert after == {"embed_batch": 16, "embed_concurrency": 1, "lanes": 4}


def test_a_new_measurement_is_picked_up_without_a_restart(node):
    node.recommend(batch=16)
    c = node.collection()
    assert collections_mod.recipe_for(c).batch == 16
    node.recommend(batch=32)
    assert collections_mod.recipe_for(c).batch == 32


def test_the_probe_is_asked_once(node):
    node.recommend()
    c = node.collection()
    for _ in range(5):
        collections_mod.recipe_for(c)
    assert len(node.calls) == 1


def test_a_silent_probe_means_no_recommendation_and_is_not_asked_every_call(node, monkeypatch):
    node.recommend()
    calls = []

    def down(url):
        calls.append(url)
        raise OSError("connection refused")
    monkeypatch.setattr(gpu, "_get_json", down)
    c = node.collection()
    for _ in range(5):
        assert collections_mod.recipe_for(c) is c.embed
    assert len(calls) == 1
    now = [0.0]
    rs.reset()
    assert rs.node_cards(fetch=down, clock=lambda: now[0]) == ()
    now[0] += rs.CARDS_RETRY_S + 1
    assert rs.node_cards(fetch=lambda url: _gpu_json(B70), clock=lambda: now[0]) == (B70,)


def test_no_probe_url_no_recommendation(node, monkeypatch):
    node.recommend()
    monkeypatch.setattr(config, "GPU_PROBE_URL", "")
    rs.reset()
    c = node.collection()
    assert collections_mod.recipe_for(c) is c.embed and node.calls == []


def test_a_bad_file_never_stops_the_collection(node):
    node.shard.joinpath(es.DIR).mkdir(parents=True)
    node.shard.joinpath(es.DIR, "0" * 64 + ".json").write_text("{not json")
    c = node.collection()
    assert collections_mod.recipe_for(c) is c.embed


def test_the_cards_are_types_not_cards(monkeypatch):
    rs.reset()
    monkeypatch.setattr(config, "GPU_PROBE_URL", "http://probe:9")
    assert rs.node_cards(fetch=lambda url: _gpu_json(B70, B70)) == (B70,)
    rs.reset()
