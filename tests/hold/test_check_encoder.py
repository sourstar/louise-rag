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
    monkeypatch.setattr(collections_mod, "by_name", lambda name: _collection(MEDCPT))
    r = louise._recipe_for(_codex(), "pubmed")
    assert r == replace(collections_mod.recipe_for(_collection(MEDCPT)),
                        dtype="f16", ctx=512, dim=768), "the worker's, in the Codex's space"
    assert r.url == MEDCPT.url


@pytest.mark.strict_encoder
def test_without_its_own_encoder_it_defers_and_says_why(monkeypatch):
    """No fallback: "a default embedder is wrong … a collection that does
    not specify an embedder is a non compliant collection" (operator,
    2026-09-18). It used to take the plugin default, then EMBED_URL."""
    monkeypatch.setattr(collections_mod, "by_name", lambda name: _collection(None))
    with pytest.raises(emb.GateDeferred, match="names no \\[encoder\\]"):
        louise._recipe_for(_codex(), "pubmed")


@pytest.mark.strict_encoder
def test_a_collection_held_without_a_file_defers_too(monkeypatch):
    monkeypatch.setattr(collections_mod, "by_name", lambda name: None)
    monkeypatch.setenv("EMBED_URL", "http://env:8080")   # read by nothing now
    with pytest.raises(emb.GateDeferred, match="non-compliant"):
        louise._recipe_for(_codex(), "pubmed")
