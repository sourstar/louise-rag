"""The Library names each collection's embedder (operator, 2026-11-02:
"Library tab, add a tag with the embedder used").

The name is the one `collections.recipe_for` resolves — the worker's own
resolution, so the page shows what runs (invariant 10) — without VeGa's
`vega-` prefix, and nothing else invented; the full served name is in the
tooltip. The weights' quantization follows only where the collection's
Codex states it (its reference engine's weights file, `options.Model`). No
encoder, no tag — and a recipe that does not resolve never breaks the page.
"""
import re
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app import collections as collections_mod, config, spaces, web
from app.api import app
from tests.conftest import TEST_ENCODER

TAG = re.compile(r'<span class="badge lapis embedder" title="([^"]*)">([^<]*)</span>')


@pytest.fixture
def client(isolated_config, monkeypatch):
    monkeypatch.setattr(spaces, "resolve_embed_model_hash", lambda *a: ("abc123", False))
    monkeypatch.setattr(web, "_ping", lambda url: True)
    monkeypatch.setattr(config, "ADMIN_TOKEN", "sekrit")
    with TestClient(app) as c:
        c.headers["X-Admin-Token"] = "sekrit"
        yield c


def _served_as(monkeypatch, model, query_model=None):
    """`recipe_for` answering with a VeGa-served model, as on a live node."""
    recipe = replace(TEST_ENCODER, model=model, query_model=query_model or model)
    asked = []

    def recipe_for(collection):
        asked.append(getattr(collection, "name", None))
        return recipe
    monkeypatch.setattr(collections_mod, "recipe_for", recipe_for)
    return asked


def _tags(page: str) -> list[tuple[str, str]]:
    return TAG.findall(page)


def test_the_card_names_the_embedder_recipe_for_resolves(client, monkeypatch):
    asked = _served_as(monkeypatch, "vega-qwen3-embedding-4b")
    page = client.get("/admin/library").text
    assert "gutenberg" in asked, "the tag is the worker's resolution, asked per collection"
    assert _tags(page) == [("served model: vega-qwen3-embedding-4b", "embedder: qwen3-embedding-4b")]
    # the prefix, and only the prefix, is left out of the text
    assert "embedder: vega-" not in page


def test_the_tag_follows_recipe_for_not_the_files_own_words(client, monkeypatch):
    """The collection's file names the test encoder; what is shown is what
    `recipe_for` answers — the one resolution, never a second reading."""
    default = collections_mod.default_collection()
    assert default.embed is not None and default.embed.model == TEST_ENCODER.model
    _served_as(monkeypatch, "vega-bge-m3")
    page = client.get("/admin/library").text
    assert [t for _, t in _tags(page)] == ["embedder: bge-m3"]
    assert f"embedder: {TEST_ENCODER.model}" not in page


def test_an_asymmetric_encoder_names_both_sides_in_the_tooltip(client, monkeypatch):
    _served_as(monkeypatch, "vega-medcpt-article", "vega-medcpt-query")
    assert _tags(client.get("/admin/library").text) == [
        ("served model: vega-medcpt-article · queries: vega-medcpt-query", "embedder: medcpt-article")]


def test_no_prefix_no_stripping(client, monkeypatch):
    _served_as(monkeypatch, "qwen3-embedding-8k:latest")
    assert [t for _, t in _tags(client.get("/admin/library").text)] == ["embedder: qwen3-embedding-8k:latest"]


@pytest.mark.parametrize("error", [collections_mod.NoEncoder("no [encoder]"), ValueError("unreadable")])
def test_no_encoder_no_tag_and_the_page_still_renders(client, monkeypatch, error):
    def recipe_for(collection):
        raise error
    monkeypatch.setattr(collections_mod, "recipe_for", recipe_for)
    r = client.get("/admin/library")
    assert r.status_code == 200, "a recipe that does not resolve never breaks the page"
    main = r.text[r.text.index("<main>"):r.text.index("</main>")]
    assert _tags(r.text) == [] and "embedder:" not in main
    card = r.text[r.text.index('data-collection="gutenberg"'):]
    assert re.search(r">Embed mode\s*</label>", card), "no device badge without a recipe"


def test_the_quantization_is_the_codexs_own_statement(isolated_config):
    """`Q4_K_M` from the weights file the Codex's reference engine loaded
    (`options.Model`, Codex version 4) — read by the gate's own
    `quant_level`; a Codex whose engine names no file states none."""
    c = collections_mod.default_collection()
    recipe = replace(TEST_ENCODER, model="vega-qwen3-embedding-4b")
    engine = c.codex.space.encoder.engine
    assert web._embedder_tag(c, recipe)["quant"] is None, "args alone: `${WEIGHTS}` names no file"
    quantised = _with_engine_options(c, {"Model": "Qwen3-Embedding-4B-Q4_K_M.gguf", "ModelFile": "${WEIGHTS}"})
    assert web._embedder_tag(quantised, recipe) == {
        "name": "qwen3-embedding-4b", "quant": "Q4_K_M", "served": "vega-qwen3-embedding-4b"}
    assert web._embedder_tag(_with_engine_options(c, {"Model": "bge-m3.gguf"}), recipe)["quant"] is None
    assert engine is not None
    # no Codex at all, no space (an annotations collection): nothing stated, nothing raised
    assert web._embedder_tag(replace(c, codex=None), recipe)["quant"] is None
    assert web._embedder_tag(c, None) is None


def test_the_card_shows_the_quantization_where_the_codex_states_it(client, monkeypatch):
    c = _with_engine_options(collections_mod.default_collection(),
                             {"Model": "Qwen3-Embedding-4B-Q4_K_M.gguf", "ModelFile": "${WEIGHTS}"})
    monkeypatch.setattr(collections_mod, "load_all", lambda *a, **k: [c])
    _served_as(monkeypatch, "vega-qwen3-embedding-4b")
    assert _tags(client.get("/admin/library").text) == [
        ("served model: vega-qwen3-embedding-4b", "embedder: qwen3-embedding-4b · Q4_K_M")]


def test_the_template_draws_the_tag_from_the_shelf():
    from pathlib import Path
    library = (Path(web.__file__).parent / "templates" / "library.html").read_text(encoding="utf-8")
    assert "{% if c.embedder %}" in library and "c.embedder.served" in library


def _with_engine_options(c, options):
    space = c.codex.space
    engine = replace(space.encoder.engine, options=options)
    codex = replace(c.codex, space=replace(space, encoder=replace(space.encoder, engine=engine)))
    return replace(c, codex=codex)
