"""No default encoder (operator, 2026-09-18): "A default embedder is wrong,
remove EMBED_URL. A collection that does not specify an embedder is a non
compliant collection."

A collection's encoder is its own file's `[encoder]` and nothing else.
EMBED_URL, EMBED_MODEL, EMBED_API, EMBED_DTYPE, EMBED_CTX and
platform.toml's `[encoder_defaults.<plugin>]` are gone as fallbacks; a file
that names no encoder is refused at load, and the node keeps its other
collections and names the reason where the operator looks.

Every test here loads files as the node does (`strict_encoder`): the rest
of the suite's collection files predate the rule and are given a TEST
encoder by tests/conftest.py.
"""
import pytest
from fastapi.testclient import TestClient

from app import collections as collections_mod, config, platform_config, spaces, web
from tests.conftest import codex_collection

pytestmark = pytest.mark.strict_encoder

#: where this node reaches the Codex's encoder (07 §7.8); the model and the
#: space are the Codex's
NAMED = '[encoder]\nurl = "http://enc:8080"\napi = "openai"\nembed_model = "m"\nquery_model = "m"\n'


def _named_and_bare(data_dir, config_dir):
    """`named` names its encoder; `bare` and the fixture's `gutenberg` name
    none."""
    codex_collection("named", louise_root=data_dir, config_dir=config_dir, toml_extra=NAMED)
    codex_collection("bare", louise_root=data_dir, config_dir=config_dir)


def test_the_env_settings_are_gone():
    for name in ("EMBED_URL", "EMBED_MODEL", "EMBED_API", "EMBED_DTYPE", "EMBED_CTX"):
        assert not hasattr(config, name), name
    assert not hasattr(collections_mod, "env_recipe")
    assert not hasattr(collections_mod, "default_recipe_for_plugin")


def test_a_file_naming_no_encoder_is_refused_and_the_others_are_served(
        isolated_config, data_dir, config_dir):
    _named_and_bare(data_dir, config_dir)
    assert [c.name for c in collections_mod.load_all()] == ["named"]
    refused = {r.name: r for r in collections_mod.refusals()}
    assert set(refused) == {"bare", "gutenberg"}
    assert "non-compliant" in refused["bare"].reason
    assert "[encoder]" in refused["bare"].reason, "it says what to add"


def test_a_collection_without_an_encoder_has_none(isolated_config, tmp_path):
    bare = collections_mod.Collection(
        name="bare", plugin="gutenberg", profile=None, embed_mode_default=None,
        corpus_dir=tmp_path, manifest_path=tmp_path / "m.db",
        fts_dir=tmp_path / "fts", lance_dir=tmp_path / "lance")
    with pytest.raises(collections_mod.NoEncoder, match="bare: non-compliant"):
        collections_mod.recipe_for(bare)


def test_the_boot_refuses_that_collection_and_never_the_node(isolated_config, tmp_path,
                                                            monkeypatch):
    """It used to raise out of the worker's boot: one bad collection took
    every other one down with it (test_process_signals: "exited 1")."""
    monkeypatch.setattr(spaces, "resolve_embed_model_hash", lambda *a: ("h", False))
    bare = collections_mod.Collection(
        name="bare", plugin="gutenberg", profile=None, embed_mode_default=None,
        corpus_dir=tmp_path, manifest_path=tmp_path / "m.db",
        fts_dir=tmp_path / "fts", lance_dir=tmp_path / "lance")
    refused = spaces.refuse_boot([bare])
    assert [(c.name, "non-compliant" in why) for c, why in refused] == [("bare", True)]
    (row,) = spaces.report_boot_status([bare])
    assert row["ok"] is False and "non-compliant" in row["message"], \
        "a verdict, not 'cannot be checked'"


def test_platform_encoder_defaults_are_ignored_and_said_to_be(isolated_config, data_dir,
                                                              config_dir, monkeypatch):
    """Still parsed — an older platform.toml must load — but decides nothing,
    and the System page says so rather than let anyone believe it does."""
    (config_dir / "platform.toml").write_text(
        '[encoder_defaults.gutenberg]\nurl = "http://default:1"\nmodel = "d"\n',
        encoding="utf-8")
    _named_and_bare(data_dir, config_dir)
    assert [c.name for c in collections_mod.load_all()] == ["named"], \
        "no default stands in for the encoder bare did not name"
    monkeypatch.setattr(web, "_ping", lambda url: True)
    eff = web._effective_config()
    assert eff["tier2_platform"]["encoder_defaults"].startswith("IGNORED")
    assert "EMBED_URL" not in eff["tier1"]


def test_the_system_page_lists_what_was_refused(isolated_config, data_dir, config_dir,
                                                monkeypatch):
    _named_and_bare(data_dir, config_dir)
    monkeypatch.setattr(config, "ADMIN_TOKEN", "sekrit")
    monkeypatch.setattr(spaces, "resolve_embed_model_hash", lambda *a: ("h", False))
    monkeypatch.setattr(web, "_ping", lambda url: True)
    from app.api import app
    with TestClient(app) as c:
        page = c.get("/admin/system", headers={"X-Admin-Token": "sekrit"}).text
    assert "Refused collections" in page and "bare" in page and "non-compliant" in page


# ---- what survives of the old parser tests --------------------------------

def test_one_parser_for_every_encoder_table():
    with pytest.raises(collections_mod.ModelKeys, match="`embed_model` is required"):
        collections_mod.parse_recipe({"url": "http://x"}, where="x")
    with pytest.raises(ValueError, match="requires url"):
        collections_mod.parse_recipe({"embed_model": "m", "query_model": "m"}, where="x")
    assert collections_mod.parse_recipe(None, where="x") is None
    assert collections_mod.parse_recipe("not a table", where="x") is None


def test_a_malformed_encoder_defaults_block_still_stops_the_file(isolated_config,
                                                                 monkeypatch, tmp_path):
    """Ignored, but a malformed table is still a malformed file: the
    operator is told about the line they wrote."""
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    (tmp_path / "platform.toml").write_text('encoder_defaults = 3\n', encoding="utf-8")
    with pytest.raises(Exception, match="encoder_defaults must be a table"):
        platform_config.load()
