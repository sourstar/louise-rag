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


ENC = "http://models:8080/upstream/medcpt-article"
#: the shared test recipe is a bi-encoder space (09 §9.3's worked example), so
#: `create` needs the query side's endpoint too
QENC = "http://models:8080/upstream/medcpt-query"
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


# ---- the annotations type (02 §2.10; 07 §7.4, amended 2026-10-19) ----

ANN_ROUTE = "/api/hold/collection/create-annotations"


def _target_and_claims(client):
    """A held target with a snapshot on this test's node, and a claims file
    about two of its records (tests/hold/test_annotations.py's helpers)."""
    from app.hold.annotations import SHARD_RECORDS  # noqa: F401 — the module the route runs
    from tests.hold.test_annotations import TARGET_SNAPSHOT, a_claim, a_target, claims_file
    root = louise._default_root()
    root.mkdir(parents=True, exist_ok=True)
    a_target(root)
    return TARGET_SNAPSHOT, claims_file(a_claim(), a_claim("pubmed:3", "pubmed:1", confidence="0.73"))


def _form(snapshot, name="pubmed-same-work", **over):
    data = {"name": name, "annotates": "pubmed", "from_snapshot": snapshot, "title": "Same works",
            "summary": "Claims.", "annotator": "", "dry_run": "false"}
    data.update(over)
    return data


def test_a_dry_run_checks_and_counts_the_claims_and_writes_nothing(client):
    snapshot, claims = _target_and_claims(client)
    r = client.post(ANN_ROUTE, params=_form(snapshot, dry_run="true"), content=claims)
    assert r.status_code == 200, r.text
    p = r.json()
    assert (p["target"], p["from_snapshot"], p["claims"], p["target_records_named"], p["written"]) == \
        ("pubmed", snapshot, 2, 3, False)
    assert p["by_type"] == {"same-work": 2} and p["annotator"] == "a test annotator"
    assert p["cli"].startswith("louise hold collection create annotations --annotates pubmed") \
        and p["cli"].endswith("--dry-run")
    assert not collections_mod.config_toml_path("pubmed-same-work").exists()
    assert collections_mod.load_annotations() == []


def test_the_create_is_checked_then_queued_and_the_worker_writes_it_in_any_mode(client):
    """The same check as the dry run, then a job of its own kind: it signs
    the first snapshot (the worker's key) and embeds nothing, so unlike
    `collection-create` it does not wait for a mode that embeds."""
    from contextlib import closing
    from app import jobs
    from app.hold import collection as col
    snapshot, claims = _target_and_claims(client)
    r = client.post(ANN_ROUTE, params=_form(snapshot), content=claims)
    assert r.status_code == 202, r.text
    q = r.json()
    assert q["state"] == "queued" and q["preview"]["claims"] == 2 and q["kind"] == jobs.CREATE_ANNOTATIONS
    uploads = list((config.louise_root() / "state" / "claims-uploads").glob("pubmed-same-work-*.jsonl"))
    assert len(uploads) == 1 and uploads[0].read_bytes() == claims, "the upload waits for the worker"
    assert collections_mod.load_annotations() == [], "nothing written by the web process"
    with closing(jobs.connect()) as con:
        done = jobs.run_next(con, "paused")
    assert done.id == q["id"] and done.state == "done", done.error
    d = done.result
    assert (d["exit"], d["name"], d["claims"], d["signed"]) == (0, "pubmed-same-work", 2, True)
    assert d["held"] == str(collections_mod.config_toml_path("pubmed-same-work"))
    assert d["cli"] == ("louise hold collection create annotations --annotates pubmed "
                        f"--from-snapshot {snapshot} --claims FILE --name pubmed-same-work "
                        "--title 'Same works' --summary 'Claims.'")
    (held,) = collections_mod.load_annotations()
    assert held.name == "pubmed-same-work" and held.codex.collection_id == d["collection_id"]
    assert col.show(louise._default_root(), "pubmed-same-work")["annotates"]["target"] == "pubmed"
    assert not uploads[0].exists(), "read once, then gone"
    assert collections_mod.load_all() == [] or "pubmed-same-work" not in [c.name for c in collections_mod.load_all()]


def test_a_refused_claims_file_is_409_and_queues_nothing(client):
    from contextlib import closing
    from app import jobs
    from tests.hold.test_annotations import a_claim, claims_file
    snapshot, claims = _target_and_claims(client)
    bad = claims_file(a_claim("pubmed:1", "pubmed:99"))
    r = client.post(ANN_ROUTE, params=_form(snapshot), content=bad)
    assert r.status_code == 409 and "pubmed:99" in r.json()["detail"]
    r = client.post(ANN_ROUTE, params=_form("2031-01"), content=claims)
    assert r.status_code == 409 and "2031-01" in r.json()["detail"]
    r = client.post(ANN_ROUTE, params=_form(snapshot, annotates="nope"), content=claims)
    assert r.status_code == 409
    r = client.post(ANN_ROUTE, params=_form(snapshot, name=""), content=claims)
    assert r.status_code == 409 and "name" in r.json()["detail"]
    r = client.post(ANN_ROUTE, params=_form(snapshot, name="pubmed"), content=claims)
    assert r.status_code == 409 and "already held" in r.json()["detail"]
    with closing(jobs.connect()) as con:
        assert jobs.run_next(con, "running") is None, "nothing queued"
    assert not list((config.louise_root() / "state").glob("claims-uploads/*"))
    assert collections_mod.load_annotations() == []


def test_an_empty_body_is_refused_as_no_claims_file(client):
    snapshot, _ = _target_and_claims(client)
    r = client.post(ANN_ROUTE, params=_form(snapshot, dry_run="true"), content=b"")
    assert r.status_code == 409 and "request body" in r.json()["detail"]


def test_the_annotations_route_needs_the_admin_token(client):
    snapshot, claims = _target_and_claims(client)
    r = client.post(ANN_ROUTE, params=_form(snapshot, dry_run="true"), content=claims,
                    headers={"X-Admin-Token": ""})
    assert r.status_code == 401


def test_the_librarys_form_offers_the_annotations_type(client):
    page = client.get("/admin/library").text
    assert ANN_ROUTE in page and 'value="annotations"' in page and 'id="na_claims"' in page
    assert "louise hold collection create annotations" in page


# ---- updating it (operator, 2026-10-23: updated, not replaced) ----

def _created(client):
    from contextlib import closing
    from app import jobs
    snapshot, claims = _target_and_claims(client)
    assert client.post(ANN_ROUTE, params=_form(snapshot), content=claims).status_code == 202
    with closing(jobs.connect()) as con:
        assert jobs.run_next(con, "paused").state == "done"
    return collections_mod.load_annotations()[0]


def test_the_update_is_checked_then_queued_and_the_worker_writes_the_next_snapshot(client):
    from contextlib import closing
    from app import jobs
    from app.hold import snapshot as sn
    from tests.hold.test_annotations import a_claim, claims_file
    held = _created(client)
    route = "/api/hold/annotations/pubmed-same-work/update"
    new = claims_file(a_claim(), a_claim("pubmed:2", "pubmed:4"))
    r = client.post(route, params={"dry_run": "true"}, content=new)
    assert r.status_code == 200, r.text
    p = r.json()
    assert (p["added"], p["tombstoned"], p["written"]) == (1, 1, False)
    assert p["cli"] == "louise hold annotations update pubmed-same-work --claims FILE --dry-run"
    r = client.post(route, content=new)
    assert r.status_code == 202, r.text
    q = r.json()
    assert q["kind"] == jobs.UPDATE_ANNOTATIONS and q["preview"]["added"] == 1
    with closing(jobs.connect()) as con:
        done = jobs.run_next(con, "paused")
    assert done.state == "done", done.error
    d = done.result
    assert d["written"] and d["signed"] and d["previous_id"] < d["snapshot_id"]
    (again,) = collections_mod.load_annotations()
    assert again.codex.collection_id == held.codex.collection_id, "the same collection, updated"
    shard = config.louise_root() / "collections" / "pubmed-same-work" / "shard"
    assert sn.load(shard / f"snapshots/{d['snapshot_id']}.json").previous is not None
    assert not list((config.louise_root() / "state").glob("claims-uploads/*update*")), "read once, then gone"


def test_an_empty_body_re_checks_and_nothing_to_do_queues_nothing(client):
    from contextlib import closing
    from app import jobs
    from tests.hold.test_annotations import a_claim, claims_file
    _created(client)
    route = "/api/hold/annotations/pubmed-same-work/update"
    r = client.post(route, content=b"")
    assert r.status_code == 200 and r.json()["why"].startswith("nothing to update"), r.text
    r = client.post(route, content=claims_file(a_claim("pubmed:1", "pubmed:99")))
    assert r.status_code == 409 and "pubmed:99" in r.json()["detail"]
    assert client.post("/api/hold/annotations/nope/update", content=b"").status_code == 409
    with closing(jobs.connect()) as con:
        assert jobs.run_next(con, "running") is None, "nothing queued"
