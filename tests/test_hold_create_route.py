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
    # a node of its own, holding nothing yet: not `isolated_config`'s, whose
    # default collection is already named gutenberg
    cfg = tmp_path / "hold-config"
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
    monkeypatch.setattr(louise, "_embed_for_recipe", lambda space, url=None, model=None, **kw: fake_embed)
    with TestClient(app) as c:
        c.headers["X-Admin-Token"] = "sekrit"
        yield c


ENC = "http://rage:8080/upstream/medcpt-article"
#: the shared test recipe is a bi-encoder space (09 §9.3's worked example), so
#: `create` needs the query side's endpoint too
QENC = "http://rage:8080/upstream/medcpt-query"
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


def created(client, body: dict) -> dict:
    """The route queues; the worker's job thread runs it (app/jobs.py) —
    here, the same `run_next` it calls, once."""
    from contextlib import closing
    from app import jobs
    r = client.post(ROUTE, json=body)
    assert r.status_code == 202, r.text
    assert r.json()["state"] == "queued"
    with closing(jobs.connect()) as con:
        jobs.run_next(con, "running", log=lambda m: None)
    return client.get(f"/api/jobs/{r.json()['id']}").json()


def test_the_route_is_the_command(client):
    j = created(client, {"plugin": "gutenberg", "recipe": recipe(),
                         "encoder": ENC, "query_encoder": QENC})
    assert j["state"] == "done", j
    d = j["result"]
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

    again = created(client, {"plugin": "gutenberg", "recipe": recipe(),
                             "encoder": ENC, "query_encoder": QENC})
    assert again["state"] == "done"
    assert again["result"]["collection_id"] == d["collection_id"], "the hash is the identity"


def test_a_refusal_is_409_names_its_rule_and_writes_nothing(client, monkeypatch):
    monkeypatch.delenv("LOUISE_PLATFORM_IMAGE")
    r = client.post(ROUTE, json={"plugin": "gutenberg", "recipe": recipe()})
    assert r.status_code == 409 and "LOUISE_PLATFORM_IMAGE" in r.json()["detail"]
    bad = client.post(ROUTE, json={"plugin": "gutenberg", "recipe": "[space\n"})
    assert bad.status_code == 409 and "not TOML" in bad.json()["detail"]
    assert client.post(ROUTE, json={"plugin": "nope", "recipe": {}}).status_code == 409
    assert not collections_mod.config_toml_path("gutenberg").exists()


def test_a_query_encoder_naming_no_model_is_refused_at_once(client):
    """07 §7.8 (amended 2026-10-08): the file names `query_model`, and a
    query endpoint whose URL names none leaves only the document side's
    label to write — the wrong side's name. Refused before any work."""
    r = client.post(ROUTE, json={"plugin": "gutenberg", "recipe": recipe(),
                                 "encoder": ENC, "query_encoder": "http://enc:8081"})
    assert r.status_code == 409 and "query_model" in r.json()["detail"]
    assert not collections_mod.config_toml_path("gutenberg").exists()


def test_the_file_names_both_models(client):
    j = created(client, {"plugin": "gutenberg", "recipe": recipe(),
                         "encoder": ENC, "query_encoder": QENC})
    held = tomllib.loads(open(j["result"]["held"], encoding="utf-8").read())
    assert (held["encoder"]["embed_model"], held["encoder"]["query_model"]) == \
        ("medcpt-article", "medcpt-query")


def test_the_librarys_form_is_this_route(client):
    """07 §7.6: the web interface's new collection is `hold collection
    create --recipe FILE`, and the page shows the command line it runs."""
    page = client.get("/admin/library").text
    assert ROUTE in page and 'id="nc_recipe"' in page
    assert "louise hold collection create" in page
    assert "/admin/library/new/create" not in page, \
        "the old route is gone: its files named no Codex"


def test_the_old_route_is_gone_and_points_here(client):
    """The form before this one wrote a file naming no Codex, refused at
    load since 2026-10-08. It answers 410 before reading the body — valid,
    invalid or absent — writes nothing, and names this route."""
    for body in ({"name": "g", "plugin": "gutenberg", "profile": "hand-populated",
                  "corpus_dir": "/nowhere"}, {"nonsense": 1}, None):
        r = client.post("/admin/library/new/create", json=body)
        assert r.status_code == 410, r.text
        detail = r.json()["detail"]
        assert ROUTE in detail and "hold collection create" in detail
    assert not collections_mod.config_toml_path("g").exists(), "nothing written"


def test_the_route_needs_the_admin_token(client):
    del client.headers["X-Admin-Token"]
    assert client.post(ROUTE, json={"plugin": "gutenberg", "recipe": {}}).status_code == 401


def test_the_name_given_is_the_name_held(client):
    """`--name` was parsed and never passed until 2026-10-07: the first
    certified collection came up as `gutenberg-744ba8430fed`. The route's
    body carries it, its CLI line names it, and the file is the name's."""
    j = created(client, {"plugin": "gutenberg", "recipe": recipe(),
                         "encoder": ENC, "query_encoder": QENC, "name": "gutenberg-mini"})
    assert j["state"] == "done", j["error"]
    d = j["result"]
    assert d["name"] == "gutenberg-mini"
    assert d["held"] == str(collections_mod.config_toml_path("gutenberg-mini"))
    assert d["cli"].endswith(f"--query-encoder {QENC} --name gutenberg-mini")
    assert collections_mod.by_name("gutenberg-mini").codex.collection_id == d["collection_id"]


# ---- a URL that names no model (LocalAI, api = "vega"; amended 2026-10-09) ----

LOCALAI = "http://192.168.178.10:30286"


def test_a_url_naming_no_model_without_its_key_is_refused_at_once(client):
    """Never the space's label in its place: the label names no model any
    server is asked for, and a file naming it can neither embed nor search."""
    r = client.post(ROUTE, json={"plugin": "gutenberg", "recipe": recipe(),
                                 "encoder": LOCALAI, "query_encoder": LOCALAI})
    assert r.status_code == 409 and "--embed-model" in r.json()["detail"]
    r = client.post(ROUTE, json={"plugin": "gutenberg", "recipe": recipe(),
                                 "encoder": LOCALAI, "query_encoder": LOCALAI,
                                 "api": "vega", "embed_model": "vega-medcpt-article"})
    assert r.status_code == 409 and "--query-model" in r.json()["detail"]
    r = client.post(ROUTE, json={"plugin": "gutenberg", "recipe": recipe(), "encoder": ENC,
                                 "query_encoder": QENC, "api": "grpc"})
    assert r.status_code == 409 and "--api" in r.json()["detail"]
    assert not collections_mod.config_toml_path("gutenberg").exists()


def test_a_vega_create_names_api_and_models_and_calibrates_with_them(client, monkeypatch):
    asked = []
    monkeypatch.setattr(louise, "_embed_for_recipe",
                        lambda space, url, model=None, **kw:
                        asked.append((model, kw.get("api"))) or fake_embed)
    j = created(client, {"plugin": "gutenberg", "recipe": recipe(), "encoder": LOCALAI,
                         "query_encoder": LOCALAI, "api": "vega",
                         "embed_model": "vega-medcpt-article",
                         "query_model": "vega-medcpt-query"})
    assert j["state"] == "done", j
    assert asked == [("vega-medcpt-article", "vega"), ("vega-medcpt-query", "vega")]
    held = tomllib.loads(open(j["result"]["held"], encoding="utf-8").read())["encoder"]
    assert held == {"url": LOCALAI, "api": "vega", "query_url": LOCALAI,
                    "embed_model": "vega-medcpt-article", "query_model": "vega-medcpt-query"}
    assert ("--api vega --embed-model vega-medcpt-article --query-model vega-medcpt-query"
            in j["result"]["cli"])
    assert collections_mod.by_name("gutenberg").embed.api == "vega"


def test_the_librarys_form_carries_the_model_keys(client):
    page = client.get("/admin/library").text
    assert 'id="nc_embed_model"' in page and 'id="nc_query_model"' in page
    assert 'id="nc_api"' in page and "embed_model,query_model" in page
