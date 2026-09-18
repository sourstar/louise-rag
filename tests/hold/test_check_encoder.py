"""`hold check` embeds with the encoder the collection's worker embeds with.

It read EMBED_URL alone. On the node that is the Qwen3-4B endpoint, and
`hold check pubmed` sent MedCPT's texts there: "the encoder returned 2560
dimensions, the space is 768" (2026-09-18), so calibration and
vectors-sample could never pass. Behind a 768-wide endpoint it would have
compared pubmed's vectors with another model's. Invariant 10: the value in
effect comes from the same resolution function the worker uses.
"""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from app import collections as collections_mod, louise
from app.hold import embedder as emb

MEDCPT = collections_mod.EmbedRecipe(url="http://medcpt-article:8080", api="openai",
                                     model="MedCPT-Article", dtype="f32", ctx=8192,
                                     query_url="http://medcpt-query:8080", concurrency=4)


def _codex(plugin="pubmed"):
    return SimpleNamespace(plugin=plugin, space=SimpleNamespace(
        dtype="f16", ctx=512, dim=768, encoder=SimpleNamespace(label="medcpt")))


def _collection(embed):
    return SimpleNamespace(name="pubmed", plugin="pubmed", embed=embed, codex=None)


def test_a_collection_the_node_runs_uses_the_workers_encoder(monkeypatch):
    monkeypatch.setenv("EMBED_URL", "http://qwen3-4b:8080")
    monkeypatch.setattr(collections_mod, "by_name", lambda name: _collection(MEDCPT))
    r = louise._recipe_for(_codex(), "pubmed")
    assert r == replace(collections_mod.recipe_for(_collection(MEDCPT)),
                        dtype="f16", ctx=512, dim=768), "the worker's, in the Codex's space"
    assert r.url == MEDCPT.url != "http://qwen3-4b:8080"


def test_without_its_own_encoder_it_falls_back_as_the_worker_does(monkeypatch):
    node_default = collections_mod.EmbedRecipe(url="http://node-default:8080", api="openai",
                                               model="m", dtype="f32", ctx=1024)
    monkeypatch.setattr(collections_mod, "by_name", lambda name: _collection(None))
    monkeypatch.setattr(collections_mod, "default_recipe_for_plugin",
                        lambda plugin: node_default if plugin == "pubmed" else None)
    assert louise._recipe_for(_codex(), "pubmed").url == "http://node-default:8080"


def test_a_collection_held_without_a_file_takes_the_plugin_default_then_env(monkeypatch):
    monkeypatch.setattr(collections_mod, "by_name", lambda name: None)
    monkeypatch.setattr(collections_mod, "default_recipe_for_plugin", lambda plugin: None)
    monkeypatch.setenv("EMBED_URL", "http://env:8080")
    r = louise._recipe_for(_codex(), "pubmed")
    assert (r.url, r.model, r.dim) == ("http://env:8080", "medcpt", 768)
    monkeypatch.delenv("EMBED_URL")
    with pytest.raises(emb.GateDeferred):
        louise._recipe_for(_codex(), "pubmed")
