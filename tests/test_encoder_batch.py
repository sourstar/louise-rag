"""A collection's own batch: `[encoder] batch` (2026-10-31, München's
production profile — wikipedia's embedding bound by one chunk a request,
the card ~10 % busy). Per collection, because whether a batch answers as
one text at a time does is the engine's property, gated at the batching
used before a batched answer is written."""
from dataclasses import replace

import pytest

from app import collections as collections_mod, config, embeddings, run_check

BASE = {"url": "http://localai:8080", "embed_model": "m", "query_model": "m"}


def test_the_batch_is_read_from_the_collections_encoder_block():
    assert collections_mod.parse_recipe(BASE, where="t").batch == 0
    assert collections_mod.parse_recipe(dict(BASE, batch=16), where="t").batch == 16
    for bad in (-1, 2.5, "16", True):
        with pytest.raises(ValueError, match="batch"):
            collections_mod.parse_recipe(dict(BASE, batch=bad), where="t")


def test_a_collections_batch_wins_over_the_nodes(monkeypatch):
    monkeypatch.setattr(config, "EMBED_BATCH", 1)
    recipe = collections_mod.parse_recipe(BASE, where="t")
    assert embeddings.batch_size(recipe) == 1
    assert embeddings.batch_size(replace(recipe, batch=16)) == 16
    monkeypatch.setattr(config, "EMBED_BATCH", 4)
    assert embeddings.batch_size(recipe) == 4, "0: the node's"


def test_the_gate_measures_at_the_collections_batch(monkeypatch):
    """`contributing` is what the gate's contribute pass reproduces and what
    `runs.check` compares with the batching a run was gated at."""
    monkeypatch.setattr(config, "EMBED_BATCH", 1)
    monkeypatch.setattr(config, "EMBED_CONCURRENCY", 1)
    recipe = collections_mod.parse_recipe(BASE, where="t")
    assert run_check.batched(recipe) is False and run_check.contributing(recipe) is None
    batched = replace(recipe, batch=16)
    assert run_check.batched(batched) is True
    assert run_check.contributing(batched) == {"embed_batch": 16, "embed_concurrency": 1, "lanes": 1}


def test_the_client_sends_the_collections_batch(monkeypatch):
    monkeypatch.setattr(config, "EMBED_BATCH", 1)
    monkeypatch.setattr(config, "EMBED_CONCURRENCY", 1)
    sizes: list[int] = []
    monkeypatch.setattr(embeddings, "_embed_batch",
                        lambda b, timeout, recipe: sizes.append(len(b)) or [[0.0]] * len(b))
    recipe = replace(collections_mod.parse_recipe(BASE, where="t"), batch=16)
    out = embeddings._embed_texts([f"t{i}" for i in range(40)], recipe=recipe, asked=True)
    assert len(out) == 40 and sizes == [16, 16, 8]
