"""`hold config apply` — was `embed materialize`, which wrote a collection's
env-fallback recipe into its TOML. There is no fallback now — a collection
names its own encoder or is refused (operator, 2026-09-18) — so applying
the files writes no encoder into one, and names the file the node refuses;
the writer still round-trips an explicit block.
"""
import dataclasses
import json

import pytest

from app import collections as collections_mod, louise
from app.web import _write_collection_toml
from tests.conftest import codex_collection


def test_apply_leaves_a_file_that_names_its_encoder_as_it_is(
        isolated_config, config_dir, capsys):
    path = config_dir / "collections" / "gutenberg.toml"
    path.write_text(path.read_text(encoding="utf-8")
                    + '[encoder]\nurl = "http://e:1"\napi = "openai"\n', encoding="utf-8")
    before = path.read_text(encoding="utf-8")
    assert louise.main(["hold", "config", "apply", "gutenberg"]) == 0
    assert json.loads(capsys.readouterr().out)["applied"][0]["name"] == "gutenberg"
    assert path.read_text(encoding="utf-8") == before


@pytest.mark.strict_encoder
def test_a_file_that_names_no_encoder_is_not_given_one(isolated_config, config_dir, capsys):
    """The collection is refused at load, and no default is invented for it."""
    path = config_dir / "collections" / "gutenberg.toml"
    assert "[encoder]" not in path.read_text(encoding="utf-8")
    assert louise.main(["hold", "config", "apply", "gutenberg"]) != 0
    capsys.readouterr()
    assert "[encoder]" not in path.read_text(encoding="utf-8")


def test_writer_round_trips_concurrency(isolated_config, data_dir, config_dir, tmp_path):
    """Dropping concurrency on a rewrite silently serialized a CPU
    collection's embeds — the block must carry it."""
    codex = codex_collection("c", louise_root=data_dir, config_dir=config_dir)
    space = collections_mod.by_name("c").codex.space
    recipe = collections_mod.EmbedRecipe(
        url="http://cpu:1", api="openai", model=space.encoder.label or "",
        dtype=space.dtype, ctx=space.ctx, dim=space.dim, query_url="http://cpu:2",
        concurrency=4)
    path = config_dir / "collections" / "c.toml"
    _write_collection_toml(
        path, codex=codex, plugin="gutenberg", profile=None, acquire_enabled=False,
        corpus_dir=tmp_path, manifest=tmp_path / "m.db", fts=tmp_path / "fts",
        lancedb=tmp_path / "lance", embed=recipe)
    loaded = collections_mod.by_name("c")
    # the query instruction is the Codex's, never the file's
    assert dataclasses.replace(loaded.embed, query_instruction=None) == recipe
