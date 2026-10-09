"""tests/deposit/test_deposit_encoder.py — the `[encoder]` a new deposit
collection's file is written with, from platform.toml's `[deposit]` (07 §7.8,
amended 2026-10-09).

On LocalAI (api = "vega") one address serves every model, so the URL names
none: the file takes api and both models from `[deposit]`, and a URL naming
no model with no keys is refused before anything is created — never the
space's encoder label written as the model.
"""
import json
import tomllib

import pytest

from app import collections as collections_mod, louise
from app.hold import collection as col, deposit
from app.hold.codex import CodexError
from tests.deposit.test_deposit_identity import ENCODER_URL, TEXT_SPACE, make, node  # noqa: F401
from tests.hold.conftest import KEY, NOW, PLATFORM, fake_embed

LOCALAI = "http://192.168.178.10:30286"
VEGA = ('[deposit]\nencoder = "%s"\napi = "vega"\n'
        'embed_model = "vega-granite"\nquery_model = "vega-granite-q"\n' % LOCALAI)


def platform(node, text):
    (node.parent.parent / "config" / "platform.toml").write_text(text, encoding="utf-8")


def encoder_block(node, name):
    with open(deposit.held_file(node, name), "rb") as f:
        return tomllib.load(f)["encoder"]


def first_on(node, name, url, embed_model):
    codex = deposit.create(node, name, platform_image=PLATFORM, encoder_url=url,
                           embed_model=embed_model, space=TEXT_SPACE, embed=fake_embed,
                           key=KEY, now=NOW)
    return codex


def test_a_vega_deposit_collection_names_api_and_both_models(node):
    platform(node, VEGA)
    first = first_on(node, "alice", LOCALAI, "vega-granite")
    codex, held = louise.create_deposit(node, "bob", platform_image=PLATFORM,
                                        space_from=first.collection_id)
    assert held == deposit.held_file(node, "bob")
    assert encoder_block(node, "bob") == {"url": LOCALAI, "api": "vega",
                                          "embed_model": "vega-granite",
                                          "query_model": "vega-granite-q"}
    c = collections_mod.by_name("bob")
    assert c.embed.api == "vega" and c.embed.model == "vega-granite"
    assert c.embed.query_model == "vega-granite-q"
    assert c.codex.collection_id == codex.collection_id


def test_the_first_vega_deposit_calibrates_with_vega_and_the_model(node, monkeypatch):
    """The first collection on the encoder embeds the calibration set (06
    §6.6) on the wire and with the model its file will name."""
    from app.bundles import keys as node_keys
    (node.parent.parent / "config" / "deposit-space.json").write_text(
        json.dumps(TEXT_SPACE), encoding="utf-8")
    platform(node, VEGA + 'space = "deposit-space.json"\n')
    asked = []
    monkeypatch.setattr(louise, "_embed_for_recipe",
                        lambda space, url, model=None, **kw:
                        asked.append((url, model, kw.get("api"))) or fake_embed)
    monkeypatch.setattr(node_keys, "load_signing_key", lambda *a: KEY)
    louise.create_deposit(node, "alice", platform_image=PLATFORM)
    assert asked == [(LOCALAI, "vega-granite", "vega")]
    assert encoder_block(node, "alice")["api"] == "vega"


def test_a_url_naming_no_model_without_keys_is_refused_before_anything_exists(node):
    platform(node, '[deposit]\nencoder = "%s"\n' % LOCALAI)
    first = first_on(node, "alice", ENCODER_URL, None)
    with pytest.raises(CodexError, match="names no model"):
        louise.create_deposit(node, "bob", platform_image=PLATFORM,
                              space_from=first.collection_id)
    assert not (node / "bob").exists() and not deposit.held_file(node, "bob").exists()
    assert [n for n, _ in col.list_named(node)] == ["alice"]


def test_write_held_file_never_names_the_label_as_a_model(node):
    codex = first_on(node, "alice", LOCALAI, None)
    with pytest.raises(deposit.DepositError, match="names no model"):
        deposit.write_held_file(node, codex, LOCALAI)
    assert not deposit.held_file(node, "alice").exists()


def test_the_model_container_url_path_is_unchanged(node):
    platform(node, '[deposit]\nencoder = "%s"\n' % ENCODER_URL)
    first = make(node, "alice")
    louise.create_deposit(node, "bob", platform_image=PLATFORM,
                          space_from=first.collection_id)
    model = ENCODER_URL.rsplit("/", 1)[1]
    assert encoder_block(node, "bob") == {"url": ENCODER_URL, "api": "openai",
                                          "embed_model": model, "query_model": model}
    assert encoder_block(node, "alice") == encoder_block(node, "bob")


def test_another_model_at_the_same_address_is_another_space(node):
    """One LocalAI address serves every model: a deposit collection on
    another model there must not have its space copied (02 §2.9)."""
    a = first_on(node, "alice", LOCALAI, "vega-granite")
    deposit.write_held_file(node, a, LOCALAI, api="vega", embed_model="vega-granite")
    assert deposit.space_source(node, LOCALAI, "vega-granite").collection_id == a.collection_id
    assert deposit.space_source(node, LOCALAI, "vega-bge-m3") is None


def test_the_command_line_names_another_endpoint_s_api_and_model(node, capsys):
    first = first_on(node, "alice", LOCALAI, "vega-bge-m3")
    assert louise.main(["hold", "collection", "create", "deposit", "--name", "bob",
                        "--root", str(node), "--space-from", first.collection_id,
                        "--encoder", LOCALAI, "--api", "vega", "--embed-model", "vega-bge-m3",
                        "--platform-image", PLATFORM]) == 0
    assert encoder_block(node, "bob") == {"url": LOCALAI, "api": "vega",
                                          "embed_model": "vega-bge-m3",
                                          "query_model": "vega-bge-m3"}
    assert louise.main(["hold", "collection", "create", "deposit", "--name", "carol",
                        "--root", str(node), "--space-from", first.collection_id,
                        "--encoder", LOCALAI, "--platform-image", PLATFORM]) == 2
    assert "names no model" in capsys.readouterr().err
    assert not (node / "carol").exists()
