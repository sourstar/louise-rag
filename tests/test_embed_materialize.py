"""Env→TOML migration of the embed recipe (invariant 10: TOML is desired
state). `app.cli embed materialize` writes a collection's EFFECTIVE recipe
into its TOML byte-equal — same recipe, same space id, the pin never
notices — and every web TOML rewrite materializes the effective recipe the
same way, so an env-fallback collection becomes explicit on first touch.
"""
from pathlib import Path

from app import cli, collections as collections_mod
from app.web import _write_collection_toml


def _declare(config_dir: Path, name: str, body: str) -> Path:
    path = config_dir / "collections" / f"{name}.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def test_materialize_writes_the_env_recipe_verbatim(isolated_config, config_dir):
    _declare(config_dir, "gutenberg",
             'plugin = "gutenberg"\ncorpus_dir = "/corpora/x"\n')
    before = collections_mod.load_all()[0]
    assert before.embed is None
    effective = collections_mod.recipe_for(before)

    assert cli.main(["embed", "materialize", "gutenberg"]) == 0

    after = collections_mod.load_all()[0]
    assert after.embed is not None
    assert collections_mod.recipe_for(after) == effective, \
        "materialization must be byte-equal — same recipe, same space id"


def test_materialize_is_idempotent(isolated_config, config_dir):
    _declare(config_dir, "gutenberg",
             'plugin = "gutenberg"\ncorpus_dir = "/corpora/x"\n')
    assert cli.main(["embed", "materialize", "gutenberg"]) == 0
    first = collections_mod.load_all()[0].embed
    assert cli.main(["embed", "materialize", "gutenberg"]) == 0
    assert collections_mod.load_all()[0].embed == first


def test_writer_round_trips_concurrency(isolated_config, config_dir, tmp_path):
    """Dropping concurrency on a rewrite silently serialized a CPU
    collection's embeds — the block must carry it."""
    recipe = collections_mod.EmbedRecipe(
        url="http://cpu:1", api="openai", model="m", dtype="f16", ctx=512,
        dim=8, query_url="http://cpu:2", concurrency=4)
    path = _declare(config_dir, "c", 'plugin = "gutenberg"\n')
    _write_collection_toml(
        path, plugin="gutenberg", profile=None, acquire_enabled=False,
        corpus_dir=tmp_path, manifest=tmp_path / "m.db", fts=tmp_path / "fts",
        lancedb=tmp_path / "lance", embed=recipe)
    loaded = [c for c in collections_mod.load_all() if c.name == "c"][0]
    assert loaded.embed == recipe
