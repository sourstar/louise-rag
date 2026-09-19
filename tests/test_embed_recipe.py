"""Per-collection embedding recipes — architecture-v2.md §14 amendment 9.

A collection's [embed] block selects its own backend (URL, api, model,
dtype, ctx, and a bi-encoder query_url); collections without one keep the
Tier-1 env recipe, byte-for-byte the pre-multi-space behavior.
"""
import numpy as np
import pytest

from app import collections as collections_mod, config, embeddings, spaces


def _write_toml(config_dir, name, body):
    d = config_dir / "collections"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.toml").write_text(body, encoding="utf-8")


MEDCPT_TOML = """\
plugin = "pubmed"

[embed]
url = "http://nas:30440"
api = "openai"
model = "medcpt-article"
dtype = "f16"
ctx = 512
dim = 768
query_url = "http://nas:30441"
"""


def test_embed_block_parses_into_recipe(isolated_config, config_dir):
    _write_toml(config_dir, "medcpt", MEDCPT_TOML)
    c = collections_mod.by_name("medcpt")
    r = c.embed
    assert r.url == "http://nas:30440" and r.model == "medcpt-article"
    assert r.api == "openai" and r.dtype == "f16" and r.ctx == 512
    assert r.dim == 768 and r.query_url == "http://nas:30441"


def test_embed_block_requires_url_and_model(isolated_config, config_dir):
    _write_toml(config_dir, "broken", 'plugin = "pubmed"\n[embed]\nurl = "http://x"\n')
    with pytest.raises(ValueError, match="url and model"):
        collections_mod.load_all()


def test_no_collection_named_means_the_default_collections_own(isolated_config):
    """`recipe_for(None)` is the default collection's own encoder — never a
    node-wide default, which no longer exists (operator, 2026-09-18)."""
    r = collections_mod.recipe_for(collections_mod.default_collection())
    assert collections_mod.recipe_for(None) == r


def _capture_posts(monkeypatch):
    calls = []

    class FakeResponse:
        status_code = 200
        def raise_for_status(self):
            pass
        def json(self):
            return {"data": [{"index": 0, "embedding": [1.0, 0.0]}],
                    "embeddings": [[1.0, 0.0]]}

    def fake_post(url, json=None, timeout=None, headers=None):
        calls.append({"url": url, "json": json})
        return FakeResponse()
    monkeypatch.setattr(embeddings.httpx, "post", fake_post)
    return calls


def test_embed_texts_routes_through_the_recipe(isolated_config, monkeypatch):
    calls = _capture_posts(monkeypatch)
    recipe = collections_mod.EmbedRecipe(
        url="http://medcpt:8080", api="openai", model="medcpt-article",
        dtype="f16", ctx=512)
    embeddings.embed_texts(["hello"], recipe=recipe)
    assert calls[0]["url"] == "http://medcpt:8080/v1/embeddings"
    assert calls[0]["json"]["model"] == "medcpt-article"

    # no recipe -> no default to fall back on: refused, nothing sent
    calls.clear()
    with pytest.raises(collections_mod.NoEncoder):
        embeddings.embed_texts(["hello"])
    assert calls == []


def test_query_routing_uses_the_query_encoder(isolated_config, monkeypatch):
    calls = _capture_posts(monkeypatch)
    recipe = collections_mod.EmbedRecipe(
        url="http://article:1", api="openai", model="medcpt", dtype="f16",
        ctx=512, query_url="http://query:2")
    embeddings.embed_texts(["what is BRCA2"], recipe=recipe, query=True)
    assert calls[0]["url"] == "http://query:2/v1/embeddings"

    # documents keep going to the article encoder
    calls.clear()
    embeddings.embed_texts(["an abstract"], recipe=recipe)
    assert calls[0]["url"] == "http://article:1/v1/embeddings"

    # symmetric recipes: query=True is a no-op
    calls.clear()
    symmetric = collections_mod.EmbedRecipe(
        url="http://sym:3", api="openai", model="m", dtype="f16", ctx=512)
    embeddings.embed_texts(["q"], recipe=symmetric, query=True)
    assert calls[0]["url"] == "http://sym:3/v1/embeddings"


def test_recipe_ctx_bounds_the_char_budget(isolated_config, monkeypatch):
    calls = _capture_posts(monkeypatch)
    recipe = collections_mod.EmbedRecipe(
        url="http://x:1", api="openai", model="m", dtype="f16", ctx=512)
    embeddings.embed_texts(["A" * 10_000], recipe=recipe)
    assert len(calls[0]["json"]["input"][0]) == 512 * 4


def test_413_from_bert_class_servers_shrinks_the_budget_gently(isolated_config, monkeypatch):
    """TEI (MedCPT et al.) answers over-long input with 413 and does not
    auto-truncate; the client must shrink and retry exactly as it does for
    llama-server's 400 — in 20 % steps, not halves: on the MedCPT tokenizer
    ctx*4 chars overshoots 512 tokens by ~5 % for dense text, and halving
    threw away half of the abstract for it."""
    import httpx as httpx_mod
    attempts = []

    class Resp:
        def __init__(self, status):
            self.status_code = status
        def raise_for_status(self):
            if self.status_code >= 400:
                raise httpx_mod.HTTPStatusError(
                    "too long", request=httpx_mod.Request("POST", "http://x"),
                    response=httpx_mod.Response(self.status_code))
        def json(self):
            return {"data": [{"index": 0, "embedding": [1.0, 0.0]}]}

    def fake_post(url, json=None, timeout=None, headers=None):
        attempts.append(len(json["input"][0]))
        return Resp(413 if len(json["input"][0]) > 1000 else 200)
    monkeypatch.setattr(embeddings.httpx, "post", fake_post)

    recipe = collections_mod.EmbedRecipe(
        url="http://tei:1", api="openai", model="medcpt", dtype="f16", ctx=512)
    out = embeddings.embed_texts(["A" * 10_000], recipe=recipe)
    assert len(out) == 1
    assert attempts[0] == 2048 and attempts[-1] <= 1000, \
        "budget must shrink until the server accepts"
    for prev, nxt in zip(attempts, attempts[1:]):
        assert nxt == max(256, int(prev * 0.8)), attempts


def _refusing_server(monkeypatch, status, text, limit=1000):
    """A server that refuses any input longer than `limit` chars with
    `status` and body `text`, and accepts the rest."""
    import httpx as httpx_mod
    attempts = []

    class Resp:
        def __init__(self, status_code):
            self.status_code = status_code
        def raise_for_status(self):
            if self.status_code >= 400:
                raise httpx_mod.HTTPStatusError(
                    "refused", request=httpx_mod.Request("POST", "http://x"),
                    response=httpx_mod.Response(self.status_code, text=text))
        def json(self):
            return {"data": [{"index": 0, "embedding": [1.0, 0.0]}]}

    def fake_post(url, json=None, timeout=None, headers=None):
        attempts.append(len(json["input"][0]))
        return Resp(status if len(json["input"][0]) > limit else 200)
    monkeypatch.setattr(embeddings.httpx, "post", fake_post)
    return attempts


def test_llama_servers_500_too_large_shrinks_the_budget_as_teis_413_does(
        isolated_config, monkeypatch):
    """llama-server now refuses an over-long input with HTTP 500 "input (774
    tokens) is too large to process. increase the physical batch size" — and
    does not truncate. Seen live 2026-09-15 serving MedCPT as GGUF: the client
    raised where TEI's 413 for the same text had always made it shrink. The
    two must cut the SAME text to the same length, or two servers of one
    model embed different inputs."""
    recipe = collections_mod.EmbedRecipe(
        url="http://gguf:1", api="openai", model="medcpt", dtype="f16", ctx=512)
    llama = _refusing_server(
        monkeypatch, 500, '{"error":{"code":500,"message":"input (774 tokens) '
        'is too large to process. increase the physical batch size (current '
        'batch size: 512)","type":"server_error"}}')
    assert len(embeddings.embed_texts(["A" * 10_000], recipe=recipe)) == 1
    tei = _refusing_server(monkeypatch, 413, "Input validation error")
    embeddings.embed_texts(["A" * 10_000], recipe=recipe)
    assert llama == tei, "the same cuts, step for step, on either server"
    assert llama[-1] <= 1000


def test_any_other_500_is_still_an_error(isolated_config, monkeypatch):
    """Only the over-budget refusal shrinks the cut: a server that fails for
    any other reason must say so, not have its input quietly shortened ten
    times over."""
    _refusing_server(monkeypatch, 500, '{"error":"CUDA error: out of memory"}',
                     limit=0)
    recipe = collections_mod.EmbedRecipe(
        url="http://gguf:1", api="openai", model="medcpt", dtype="f16", ctx=512)
    with pytest.raises(RuntimeError, match="HTTP 500: .*out of memory"):
        embeddings.embed_texts(["an abstract"], recipe=recipe)


def test_space_components_come_from_the_recipe(isolated_config, config_dir):
    _write_toml(config_dir, "medcpt", MEDCPT_TOML)
    c = collections_mod.by_name("medcpt")
    comp = spaces.gather_components(c)
    assert comp.dtype == "f16"
    assert comp.truncation_policy == "ctx=512"
    # bi-encoder: the query hash differs from the article-model hash, so the
    # space_id moves if the query encoder changes (§3 query_encoder_hash)
    assert comp.query_encoder_hash != comp.embed_model_hash

    # a symmetric encoder keeps the symmetric behavior
    symmetric = collections_mod.Collection(
        name="d", plugin="gutenberg", profile=None, embed_mode_default=None,
        corpus_dir=c.corpus_dir, manifest_path=c.manifest_path,
        fts_dir=c.fts_dir, lance_dir=c.lance_dir,
        embed=collections_mod.EmbedRecipe(url="http://s:1", api="openai", model="s",
                                          dtype="f32", ctx=512))
    comp2 = spaces.gather_components(symmetric)
    assert comp2.query_encoder_hash == comp2.embed_model_hash
    assert comp2.dtype == "f32"


def test_two_collections_two_spaces_distinct_ids(isolated_config, config_dir):
    _write_toml(config_dir, "medcpt", MEDCPT_TOML)
    _write_toml(config_dir, "plain", 'plugin = "gutenberg"\n')
    medcpt = collections_mod.by_name("medcpt")
    plain = collections_mod.by_name("plain")
    id_a = spaces.compute_space_id(spaces.gather_components(medcpt))
    id_b = spaces.compute_space_id(spaces.gather_components(plain))
    assert id_a != id_b


def test_upsert_uses_recipe_dtype_for_new_table(isolated_config, config_dir,
                                                 monkeypatch, tmp_path):
    _write_toml(config_dir, "medcpt", MEDCPT_TOML)
    collection = collections_mod.by_name("medcpt")
    monkeypatch.setattr(
        embeddings, "embed_texts",
        lambda texts, timeout=300, recipe=None, query=False:
            [[1.0, 0.0, 0.0] for _ in texts])
    chunks = [{"id": "pubmed:1:0:1", "book_id": 1, "seq": 0,
                "chapter": "", "text": "abstract text", "tokens": 2}]
    embeddings.upsert_book(1, "en", chunks, collection)
    tbl = embeddings._conn(collection).open_table(embeddings.TABLE)
    import pyarrow as pa
    # the recipe says f16, and the table is the recipe's
    assert pa.types.is_float16(tbl.schema.field("vector").type.value_type)


def test_gate_baseline_records_the_collection_recipe(isolated_config, config_dir):
    from app.verify import backend_gate
    _write_toml(config_dir, "medcpt", MEDCPT_TOML)
    collection = collections_mod.by_name("medcpt")

    def fake_embed(texts):
        return [[1.0, 0.0] for _ in texts]
    backend_gate.record_baseline(collection, fake_embed)
    import json
    baseline = json.loads(backend_gate.baseline_path(collection).read_text())
    assert baseline["embed_url"] == "http://nas:30440"
    assert baseline["embed_model"] == "medcpt-article"
