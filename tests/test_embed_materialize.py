"""`app.cli embed materialize` wrote a collection's env-fallback recipe
into its TOML. There is no fallback now — a collection names its own
encoder or is refused (operator, 2026-09-18) — so the command has nothing
to write, and says so; the writer still round-trips an explicit block.
"""
import pytest
from pathlib import Path

from app import cli, collections as collections_mod
from app.web import _write_collection_toml


def _declare(config_dir: Path, name: str, body: str) -> Path:
    path = config_dir / "collections" / f"{name}.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


EXPLICIT = ('plugin = "gutenberg"\ncorpus_dir = "/corpora/x"\n'
            '[embed]\nurl = "http://e:1"\napi = "openai"\nmodel = "m"\n'
            'dtype = "f16"\nctx = 512\n')


def test_materialize_has_nothing_to_write_for_a_file_that_names_its_encoder(
        isolated_config, config_dir, capsys):
    _declare(config_dir, "gutenberg", EXPLICIT)
    before = (config_dir / "collections" / "gutenberg.toml").read_text(encoding="utf-8")
    assert cli.main(["embed", "materialize", "gutenberg"]) == 0
    assert "nothing to do" in capsys.readouterr().out
    assert (config_dir / "collections" / "gutenberg.toml").read_text(
        encoding="utf-8") == before


@pytest.mark.strict_encoder
def test_a_file_that_names_no_encoder_is_not_given_one(isolated_config, config_dir):
    """The collection is refused at load, and no default is invented for it."""
    _declare(config_dir, "gutenberg", 'plugin = "gutenberg"\ncorpus_dir = "/corpora/x"\n')
    with pytest.raises(SystemExit):
        cli.main(["embed", "materialize", "gutenberg"])
    assert "[embed]" not in (config_dir / "collections" / "gutenberg.toml").read_text(
        encoding="utf-8")


def test_writer_round_trips_concurrency(isolated_config, config_dir, tmp_path):
    """Dropping concurrency on a rewrite silently serialized a CPU
    collection's embeds — the block must carry it."""
    recipe = collections_mod.EmbedRecipe(
        url="http://cpu:1", api="openai", model="m", dtype="f16", ctx=512,
        dim=8, query_url="http://cpu:2", concurrency=4)
    path = _declare(config_dir, "c", 'plugin = "gutenberg"\n')
    _write_collection_toml(
        path, codex=None, plugin="gutenberg", profile=None, acquire_enabled=False,
        corpus_dir=tmp_path, manifest=tmp_path / "m.db", fts=tmp_path / "fts",
        lancedb=tmp_path / "lance", embed=recipe)
    loaded = [c for c in collections_mod.load_all() if c.name == "c"][0]
    assert loaded.embed == recipe
