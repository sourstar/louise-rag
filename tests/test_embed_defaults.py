"""A collection's embedding recipe resolves plugin-first, env-last.

The env recipe is the node's TEXT encoder. Not every plugin embeds text:
DNA is not a language, and a genome collection embedded with a language
model is not a worse choice, it is a wrong one — the vectors are not
comparable to anything. Two genome collections were created against the
node default and pinned a text space over DNA before anyone noticed, which
is what this resolution order exists to prevent.

Order, each step a FALLBACK for the one before it and never an override
(CLAUDE.md invariant 10 — env is identity, TOML is desired state):

    collection [embed]  ->  platform.toml [embed_defaults.<plugin>]  ->  env
"""
import pytest

from app import collections as collections_mod, config, platform_config

DNA = """
[embed_defaults.genome]
url = "http://dna.example:8080/"
api = "openai"
model = "zhihan1996/DNABERT-2-117M"
dtype = "f16"
ctx = 1024
dim = 768
concurrency = 2
device = "cpu"
"""


def _collection(tmp_path, name="g", plugin="genome", embed=None):
    return collections_mod.Collection(
        name=name, plugin=plugin, profile=None, embed_mode_default=None,
        corpus_dir=tmp_path, manifest_path=tmp_path / "m.db",
        fts_dir=tmp_path / "fts", lance_dir=tmp_path / "lance", embed=embed)


@pytest.fixture
def platform_toml(isolated_config, monkeypatch, tmp_path):
    def write(text):
        monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
        (tmp_path / "platform.toml").write_text(text, encoding="utf-8")
    return write


def test_no_platform_file_means_the_env_recipe_as_before(isolated_config, tmp_path):
    assert platform_config.load().embed_defaults == {}
    assert collections_mod.default_recipe_for_plugin("genome") is None
    assert collections_mod.recipe_for(_collection(tmp_path)) == \
        collections_mod.env_recipe()


def test_a_plugin_default_beats_the_env_recipe(platform_toml, tmp_path):
    platform_toml(DNA)
    recipe = collections_mod.recipe_for(_collection(tmp_path, plugin="genome"))
    assert recipe.model == "zhihan1996/DNABERT-2-117M"
    assert recipe.ctx == 1024 and recipe.dim == 768
    assert recipe.url == "http://dna.example:8080", "trailing slash stripped"
    assert recipe.concurrency == 2 and recipe.device == "cpu"
    assert recipe != collections_mod.env_recipe()


def test_a_plugin_without_a_default_still_gets_env(platform_toml, tmp_path):
    platform_toml(DNA)
    assert collections_mod.recipe_for(_collection(tmp_path, plugin="gutenberg")) \
        == collections_mod.env_recipe()


def test_the_collections_own_block_still_wins(platform_toml, tmp_path):
    """The per-plugin default is a NODE default. A collection that declares
    its own recipe has already pinned a space against it."""
    platform_toml(DNA)
    own = collections_mod.EmbedRecipe(
        url="http://other:1", api="openai", model="mine", dtype="f32", ctx=99)
    assert collections_mod.recipe_for(
        _collection(tmp_path, plugin="genome", embed=own)) == own


def test_one_parser_for_both_files(platform_toml, tmp_path):
    """The [embed] block in a collection TOML and in [embed_defaults.<x>]
    are the same shape, parsed by the same code — so field names, defaults
    and error messages cannot drift apart between the two files."""
    platform_toml(DNA)
    raw = platform_config.load().embed_defaults["genome"]
    assert collections_mod.parse_recipe(raw, where="x") == \
        collections_mod.default_recipe_for_plugin("genome")

    with pytest.raises(ValueError, match="requires url and model"):
        collections_mod.parse_recipe({"url": "http://x"}, where="x")
    assert collections_mod.parse_recipe(None, where="x") is None
    assert collections_mod.parse_recipe("not a table", where="x") is None


def test_a_malformed_embed_defaults_block_is_refused(platform_toml):
    platform_toml('[bandwidth]\nembed_defaults = 3\n')
    assert platform_config.load().embed_defaults == {}   # not at top level

    platform_toml('embed_defaults = 3\n')
    with pytest.raises(Exception, match="embed_defaults"):
        platform_config.load()

    platform_toml('[embed_defaults]\ngenome = "dnabert"\n')
    with pytest.raises(Exception, match="embed_defaults.genome"):
        platform_config.load()


def test_creating_a_genome_collection_writes_the_dna_recipe(
        platform_toml, tmp_path, monkeypatch):
    """The create flow is where the wrong space got pinned, so it is the
    place that has to read the plugin default."""
    from fastapi.testclient import TestClient
    from app import spaces, web
    from app.api import app

    platform_toml(DNA)
    monkeypatch.setattr(spaces, "resolve_embed_model_hash", lambda: ("abc", False))
    monkeypatch.setattr(web, "_ping", lambda url: True)
    monkeypatch.setattr(config, "ADMIN_TOKEN", "sekrit")
    mounts = tmp_path / "corpora"
    (mounts / "dna").mkdir(parents=True)
    monkeypatch.setattr(config, "CORPUS_MOUNTS_DIR", mounts)

    with TestClient(app) as client:
        client.headers["X-Admin-Token"] = "sekrit"
        r = client.post("/admin/library/new/create",
                        json={"name": "genome-dm6", "plugin": "genome",
                               "profile": "dm6",
                               "corpus_dir": (mounts / "dna").as_posix()})
        assert r.status_code == 200, r.text

    text = collections_mod.config_toml_path("genome-dm6").read_text()
    assert "DNABERT-2-117M" in text
    assert "ctx = 1024" in text
    assert collections_mod.by_name("genome-dm6").embed.dim == 768
