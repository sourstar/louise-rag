"""A collection's embedding recipe resolves plugin-first, env-last.

The env recipe is the node's TEXT encoder, and not every plugin's space is
that encoder's: pubmed's is MedCPT, and a genome collection (boxed in
attic/ since 2026-09-14) needed DNA. Two genome collections were created
against the node default and pinned a text space over DNA before anyone
noticed, which is what this resolution order exists to prevent. The
examples below use pubmed; the rule is the platform's.

Order, each step a FALLBACK for the one before it and never an override
(CLAUDE.md invariant 10 — env is identity, TOML is desired state):

    collection [encoder]  ->  platform.toml [encoder_defaults.<plugin>]  ->  env

(`[embed_defaults.<plugin>]` and a collection's `[embed]` are the older
spellings of the same two tables, still read, deprecated.)
"""
import pytest

from app import collections as collections_mod, config, platform_config

MEDCPT = """
[embed_defaults.pubmed]
url = "http://medcpt.example:8080/"
api = "openai"
model = "ncbi/MedCPT-Article-Encoder"
dtype = "f16"
ctx = 512
dim = 768
concurrency = 2
device = "cpu"
"""


def _collection(tmp_path, name="p", plugin="pubmed", embed=None):
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
    assert collections_mod.default_recipe_for_plugin("pubmed") is None
    assert collections_mod.recipe_for(_collection(tmp_path)) == \
        collections_mod.env_recipe()


def test_a_plugin_default_beats_the_env_recipe(platform_toml, tmp_path):
    platform_toml(MEDCPT)
    recipe = collections_mod.recipe_for(_collection(tmp_path, plugin="pubmed"))
    assert recipe.model == "ncbi/MedCPT-Article-Encoder"
    assert recipe.ctx == 512 and recipe.dim == 768
    assert recipe.url == "http://medcpt.example:8080", "trailing slash stripped"
    assert recipe.concurrency == 2 and recipe.device == "cpu"
    assert recipe != collections_mod.env_recipe()


def test_a_plugin_without_a_default_still_gets_env(platform_toml, tmp_path):
    platform_toml(MEDCPT)
    assert collections_mod.recipe_for(_collection(tmp_path, plugin="gutenberg")) \
        == collections_mod.env_recipe()


def test_the_collections_own_block_still_wins(platform_toml, tmp_path):
    """The per-plugin default is a NODE default. A collection that declares
    its own recipe has already pinned a space against it."""
    platform_toml(MEDCPT)
    own = collections_mod.EmbedRecipe(
        url="http://other:1", api="openai", model="mine", dtype="f32", ctx=99)
    assert collections_mod.recipe_for(
        _collection(tmp_path, plugin="pubmed", embed=own)) == own


def test_one_parser_for_both_files(platform_toml, tmp_path):
    """The [embed] block in a collection TOML and in [embed_defaults.<x>]
    are the same shape, parsed by the same code — so field names, defaults
    and error messages cannot drift apart between the two files."""
    platform_toml(MEDCPT)
    raw = platform_config.load().embed_defaults["pubmed"]
    assert collections_mod.parse_recipe(raw, where="x") == \
        collections_mod.default_recipe_for_plugin("pubmed")

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

    platform_toml('[embed_defaults]\npubmed = "medcpt"\n')
    with pytest.raises(Exception, match="embed_defaults.pubmed"):
        platform_config.load()


def test_creating_a_collection_writes_its_plugins_recipe(
        platform_toml, tmp_path, monkeypatch):
    """The create flow is where the wrong space got pinned, so it is the
    place that has to read the plugin default."""
    from fastapi.testclient import TestClient
    from app import spaces, web
    from app.api import app

    platform_toml(MEDCPT)
    monkeypatch.setattr(spaces, "resolve_embed_model_hash", lambda: ("abc", False))
    monkeypatch.setattr(web, "_ping", lambda url: True)
    monkeypatch.setattr(config, "ADMIN_TOKEN", "sekrit")
    mounts = tmp_path / "corpora"
    (mounts / "medline").mkdir(parents=True)
    monkeypatch.setattr(config, "CORPUS_MOUNTS_DIR", mounts)

    with TestClient(app) as client:
        client.headers["X-Admin-Token"] = "sekrit"
        r = client.post("/admin/library/new/create",
                        json={"name": "pubmed-x", "plugin": "pubmed",
                              "profile": "demo", "acknowledge_terms": True,
                              "corpus_dir": (mounts / "medline").as_posix()})
        assert r.status_code == 200, r.text

    text = collections_mod.config_toml_path("pubmed-x").read_text()
    assert "MedCPT-Article-Encoder" in text
    assert "ctx = 512" in text
    assert collections_mod.by_name("pubmed-x").embed.dim == 768


def test_the_spec_spelling_is_encoder_defaults(platform_toml, tmp_path):
    """07 §7.8 spells it `[encoder_defaults.<plugin>]`, matching the
    `[encoder]` block a collection file carries. The code shipped
    `embed_defaults`, which reads as the MODE key (`embed = "on-demand"`)
    sitting beside it. Both are read; the documents win on the name."""
    platform_toml(MEDCPT.replace("embed_defaults", "encoder_defaults"))
    recipe = collections_mod.default_recipe_for_plugin("pubmed")
    assert recipe is not None and recipe.model == "ncbi/MedCPT-Article-Encoder"

    # the deprecated alias still resolves, so no node breaks on upgrade
    platform_toml(MEDCPT)
    assert collections_mod.default_recipe_for_plugin("pubmed") == recipe

    # and where a file carries both, the spec name is the one that lands
    platform_toml(MEDCPT + '\n[encoder_defaults.pubmed]\n'
                  'url = "http://spec.example:9/"\nmodel = "spec"\n')
    won = collections_mod.default_recipe_for_plugin("pubmed")
    assert (won.model, won.url) == ("spec", "http://spec.example:9")


def test_a_malformed_encoder_defaults_block_is_named_by_its_own_word(platform_toml):
    """An operator editing `encoder_defaults` must not be told about
    `embed_defaults`: the error has to name the line they wrote."""
    platform_toml('encoder_defaults = 3\n')
    with pytest.raises(Exception, match="encoder_defaults must be a table"):
        platform_config.load()

    platform_toml('[encoder_defaults]\npubmed = "medcpt"\n')
    with pytest.raises(Exception, match="encoder_defaults.pubmed"):
        platform_config.load()
