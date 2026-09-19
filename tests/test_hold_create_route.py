"""`POST /api/hold/collection/create` — `louise hold collection create` as
one route (07 §7.4), and what the Library's *new collection* is now
(07 §7.6). The form before it wrote a file naming no Codex."""
import tomllib

import pytest
from fastapi.testclient import TestClient

from app import collections as collections_mod, config, louise
from app.api import app
from tests.hold.conftest import KEY, PLATFORM, fake_embed
from tests.hold.test_template import recipe

ROUTE = "/api/hold/collection/create"


@pytest.fixture
def client(tmp_path, isolated_config, monkeypatch):
    root = tmp_path / "louise"
    root.mkdir()
    cfg = tmp_path / "config"
    (cfg / "collections").mkdir(parents=True)
    monkeypatch.setenv("LOUISE_ROOT", str(root))
    monkeypatch.setenv("CONFIG_DIR", str(cfg))
    monkeypatch.setattr(config, "DATA_DIR", root)
    monkeypatch.setattr(config, "ADMIN_TOKEN", "sekrit")
    monkeypatch.setenv("LOUISE_PLATFORM_IMAGE", PLATFORM)
    from app.bundles import keys as node_keys
    monkeypatch.setattr(node_keys, "load_signing_key", lambda: KEY)
    # the calibration set is embedded by the recipe's encoder; offline, the
    # hold fixtures' deterministic stand-in (tests/hold/conftest.py)
    monkeypatch.setattr(louise, "_embed_for_recipe", lambda space, url=None: fake_embed)
    with TestClient(app) as c:
        c.headers["X-Admin-Token"] = "sekrit"
        yield c


ENC = "http://enc:8080"
#: the shared test recipe is a bi-encoder space (09 §9.3's worked example), so
#: `create` needs the query side's endpoint too
QENC = "http://enc:8081"
_REAL_EMBED_FOR_RECIPE = louise._embed_for_recipe


def c_encoder_url(held: str) -> str:
    import tomllib
    return tomllib.loads(open(held, encoding="utf-8").read())["encoder"]["url"]


def test_a_create_that_names_no_encoder_is_refused_and_writes_nothing(client, monkeypatch):
    """A collection names its own encoder, and there is no default one
    (operator, 2026-09-18)."""
    monkeypatch.setattr(louise, "_embed_for_recipe", _REAL_EMBED_FOR_RECIPE)
    r = client.post(ROUTE, json={"plugin": "gutenberg", "recipe": recipe()})
    assert r.status_code == 409 and "--encoder" in r.json()["detail"]
    assert not collections_mod.config_toml_path("gutenberg").exists()


def test_the_route_is_the_command(client):
    r = client.post(ROUTE, json={"plugin": "gutenberg", "recipe": recipe(),
                                 "encoder": ENC, "query_encoder": QENC})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["exit"] == 0 and d["name"] == "gutenberg"
    assert d["held"] == str(collections_mod.config_toml_path("gutenberg"))
    assert d["cli"] == (f"louise hold collection create gutenberg --recipe FILE "
                        f"--encoder {ENC} --query-encoder {QENC}")
    assert c_encoder_url(d["held"]) == ENC, "the file names the encoder it was created with"
    held = tomllib.loads(open(d["held"], encoding="utf-8").read())
    assert held["encoder"]["query_url"] == QENC, "and its query side"
    c = collections_mod.by_name("gutenberg")
    assert c.codex is not None and c.codex.collection_id == d["collection_id"]
    assert (c.slices, c.acquire_enabled, c.share, c.embed_mode_default) == \
        ((), False, False, "off"), "a new collection holds nothing and spends nothing"

    again = client.post(ROUTE, json={"plugin": "gutenberg", "recipe": recipe(),
                                     "encoder": ENC, "query_encoder": QENC})
    assert again.status_code == 200
    assert again.json()["collection_id"] == d["collection_id"], "the hash is the identity"


def test_a_refusal_is_409_names_its_rule_and_writes_nothing(client, monkeypatch):
    monkeypatch.delenv("LOUISE_PLATFORM_IMAGE")
    r = client.post(ROUTE, json={"plugin": "gutenberg", "recipe": recipe()})
    assert r.status_code == 409 and "LOUISE_PLATFORM_IMAGE" in r.json()["detail"]
    bad = client.post(ROUTE, json={"plugin": "gutenberg", "recipe": "[space\n"})
    assert bad.status_code == 409 and "not TOML" in bad.json()["detail"]
    assert client.post(ROUTE, json={"plugin": "nope", "recipe": {}}).status_code == 409
    assert not collections_mod.config_toml_path("gutenberg").exists()


def test_the_librarys_form_is_this_route(client):
    """07 §7.6: the web interface's new collection is `hold collection
    create --recipe FILE`, and the page shows the command line it runs."""
    page = client.get("/admin/library").text
    assert ROUTE in page and 'id="nc_recipe"' in page
    assert "louise hold collection create" in page
    assert "/admin/library/new/create" not in page, \
        "the deprecated route writes a file naming no Codex"


def test_the_route_needs_the_admin_token(client):
    del client.headers["X-Admin-Token"]
    assert client.post(ROUTE, json={"plugin": "gutenberg", "recipe": {}}).status_code == 401
