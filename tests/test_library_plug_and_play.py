"""RULE 2 (CLAUDE.md; amended 2026-10-25): every Library dialog asks a local
name — and the source, or the claims file — and the node determines the
rest. The encoder is found on the node's model server BY WEIGHTS: a model
counts only if the weights its stated run loads are the Codex's
(app/encoder_resolve.py); where none does, the node says so in plain words.

The fake model server here is the embedding client and the run description
(`embeddings.embed_calibration` states a run per model, `run_check.
describe_run` says which weights that run loaded) — the two calls
`encoder_resolve` makes, so its ordering, its matching and its cache are the
code under test."""
import tomllib
from contextlib import closing
from types import SimpleNamespace

import pytest

from app import api, collections as collections_mod, embeddings, encoder_resolve, jobs, louise, run_check
from app.hold import embedder as emb
from tests.hold.conftest import Q_WEIGHTS, WEIGHTS
from tests.hold.test_template import recipe
from tests.test_hold_create_route import ANN_ROUTE, ROUTE, _target_and_claims, client  # noqa: F401

LOCALAI = "http://localai:30286"
OTHER = "sha256:" + "0e" * 32
#: what each model on the fake server loads — the first, named like the
#: label and listed first, loads OTHER weights and must never be chosen
SERVED = {"vega-medcpt-article-old": OTHER, "vega-medcpt-article": WEIGHTS,
          "vega-medcpt-query": Q_WEIGHTS, "vega-bge-m3": "sha256:" + "4b" * 32,
          "vega-qwen3-chat": None}


@pytest.fixture
def server(monkeypatch):
    """The node's model server: LocalAI behind VeGa at `LOCALAI`, named by
    the platform's [deposit] (nothing held names one), serving `SERVED`."""
    from app import platform_config
    encoder_resolve.reset_cache()
    served = dict(SERVED)
    asked: list[str] = []
    monkeypatch.setattr(platform_config, "load",
                        lambda: SimpleNamespace(deposit=SimpleNamespace(encoder=LOCALAI + "/", api="vega")))
    monkeypatch.setattr(api, "_models_at", lambda url: sorted(served) if url == LOCALAI else [])

    def embed_calibration(recipe, texts, *, runs=None, timeout=300, **kw):
        assert recipe.url == LOCALAI and recipe.api == "vega" and timeout <= encoder_resolve.PROBE_TIMEOUT_S
        asked.append(recipe.model)
        if served.get(recipe.model) is None:
            raise ConnectionError(f"{recipe.model} is a chat model: no embeddings")
        runs.add("sha256:run-of-" + recipe.model)
        return [[0.0] * 4 for _ in texts]

    def describe_run(recipe, run_id, *, query=False):
        model = run_id.removeprefix("sha256:run-of-")
        return emb.StatedRun(run_id=run_id, engine={}, weights=served[model], driver="xe")

    monkeypatch.setattr(embeddings, "embed_calibration", embed_calibration)
    monkeypatch.setattr(run_check, "describe_run", describe_run)
    yield SimpleNamespace(served=served, asked=asked)
    encoder_resolve.reset_cache()


def _toml(table: dict, prefix: str = "") -> str:
    """A recipe as TOML — what an operator writes; TOML has no null, so a
    None is left out, as a recipe omits it (app/hold/template.py)."""
    import json
    scalars, tables = [], []
    for k, v in table.items():
        if v is None:
            continue
        if isinstance(v, dict):
            tables.append((k, v))
        else:
            scalars.append(f"{k} = {json.dumps(v)}")
    out = ([f"[{prefix}]"] if prefix and (scalars or not tables) else []) + scalars
    text = "\n".join(out) + ("\n" if out else "")
    for k, v in tables:
        text += "\n" + _toml(v, f"{prefix}.{k}" if prefix else k)
    return text


def node_recipe(plugin="gutenberg", **over):
    """The node's own recipe for the plugin, as an operator saved it once."""
    path = collections_mod.recipe_path(plugin)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_toml(recipe(**over)), encoding="utf-8")
    return path


def run_job(client, r) -> dict:
    assert r.status_code == 202, r.text
    with closing(jobs.connect()) as con:
        jobs.run_next(con, "running", log=lambda m: None)
    return client.get(f"/api/jobs/{r.json()['id']}").json()


# ---- the encoder, by weights --------------------------------------------------

def test_a_model_counts_only_if_its_stated_run_loads_the_codex_weights(server):
    space = recipe()["space"]
    found = encoder_resolve.on_server(LOCALAI, "vega", sorted(SERVED), space)
    assert found == ("vega-medcpt-article", "vega-medcpt-query")
    assert "vega-medcpt-article-old" in server.asked, "asked, and refused on its weights"
    assert "vega-bge-m3" not in server.asked and "vega-qwen3-chat" not in server.asked, \
        "a model whose name shares nothing with the label is not loaded to be asked"
    asked = len(server.asked)
    assert encoder_resolve.on_server(LOCALAI, "vega", sorted(SERVED), space) == found
    assert len(server.asked) == asked, "cached: a second resolution asks nothing"
    # the same names serving other weights: nothing is chosen, whatever the names say
    encoder_resolve.reset_cache()
    server.served.update({"vega-medcpt-article": OTHER, "vega-medcpt-query": OTHER})
    assert encoder_resolve.on_server(LOCALAI, "vega", sorted(SERVED), space) is None
    # a symmetric space: the query side is the document side's model
    encoder_resolve.reset_cache()
    server.served.update({"vega-medcpt-article": WEIGHTS})
    sym = {**space, "query": {"encoder": None, "instruction": None}}
    assert encoder_resolve.on_server(LOCALAI, "vega", sorted(SERVED), sym) == \
        ("vega-medcpt-article", "vega-medcpt-article")


def test_a_model_that_states_no_run_does_not_count(server, monkeypatch):
    def no_run(recipe, texts, *, runs=None, timeout=300, **kw):
        return [[0.0] * 4 for _ in texts]          # answers, names no run
    monkeypatch.setattr(embeddings, "embed_calibration", no_run)
    assert encoder_resolve.on_server(LOCALAI, "vega", sorted(SERVED), recipe()["space"]) is None


# ---- create: a name and a plugin ----------------------------------------------

def test_create_takes_a_plugin_and_a_name_and_the_node_decides_the_rest(client, server):
    path = node_recipe()
    r = client.post(ROUTE, json={"plugin": "gutenberg", "name": "books"})
    q = r.json()
    assert "vega-medcpt-article" in q["decided"] and "the very weights" in q["decided"]
    assert (q["slices"], q["embed"]) == ("all", "on-demand")
    j = run_job(client, r)
    assert j["state"] == "done", j
    d = j["result"]
    assert d["name"] == "books" and str(path) in d["cli"] and "--embed-model vega-medcpt-article" in d["cli"]
    held = tomllib.loads(open(d["held"], encoding="utf-8").read())
    assert held["encoder"] == {"url": LOCALAI, "api": "vega", "query_url": LOCALAI,
                               "embed_model": "vega-medcpt-article", "query_model": "vega-medcpt-query"}
    assert (held["slices"], held["embed"]) == ("all", "on-demand"), "every slice, embedding on demand"
    c = collections_mod.by_name("books")
    assert c.codex.collection_id == d["collection_id"] and c.codex.space.encoder.weights == WEIGHTS


def test_create_where_no_model_runs_the_weights_says_so_and_creates_nothing(client, server):
    node_recipe()
    server.served.update({"vega-medcpt-article": OTHER, "vega-medcpt-query": OTHER})
    with closing(jobs.connect()) as con:
        r = client.post(ROUTE, json={"plugin": "gutenberg", "name": "books"})
        assert r.status_code == 409, r.text
        detail = r.json()["detail"]
        assert f"no model on this node's server ({LOCALAI}) serves this collection's encoder (MedCPT" in detail
        assert "install it there" in detail and "nothing was created" in detail
        assert jobs.run_next(con, "running") is None, "nothing queued"
    assert not collections_mod.config_toml_path("books").exists()


def test_create_without_a_node_recipe_says_what_would_supply_it(client, server):
    r = client.post(ROUTE, json={"plugin": "gutenberg", "name": "books"})
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert "no recipe for gutenberg" in detail and str(collections_mod.recipe_path("gutenberg")) in detail
    assert "louise hold collection create gutenberg --recipe FILE" in detail and "Offered by peers" in detail


def test_a_recipe_given_by_a_script_still_creates_as_the_command_does(client, server):
    """The route keeps its flags for operators and scripts: a recipe given in
    the body, with the encoder left to the node, writes what `create` writes
    — no slice, nothing spent — with the encoder the node found."""
    j = run_job(client, client.post(ROUTE, json={"plugin": "gutenberg", "recipe": recipe()}))
    assert j["state"] == "done", j
    held = tomllib.loads(open(j["result"]["held"], encoding="utf-8").read())
    assert (held["slices"], held["embed"]) == ([], "off")
    assert held["encoder"]["embed_model"] == "vega-medcpt-article"


def test_the_library_page_says_which_plugins_the_node_has_a_recipe_for(client):
    path = node_recipe("pubmed")
    page = client.get("/admin/library").text
    assert f'"pubmed": "{path}"' in page and '"gutenberg"' not in page.split("NODE_RECIPES=")[1].split(";")[0]


# ---- the take: the same resolution --------------------------------------------

def test_the_take_finds_the_encoder_on_the_server_by_weights(server, monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from app import config
    from tests.hold.conftest import pubmed
    from tests.test_web_offer import announced
    root = tmp_path / "louise"
    (root / "collections").mkdir(parents=True)
    (tmp_path / "config" / "collections").mkdir(parents=True)
    monkeypatch.setenv("LOUISE_ROOT", str(root))
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr(config, "DATA_DIR", root)
    monkeypatch.setattr(config, "ADMIN_TOKEN", "sekrit")
    codex = pubmed()
    announced(codex)
    queued = []
    monkeypatch.setattr(jobs, "submit", lambda kind, args: queued.append(args) or 7)
    monkeypatch.setattr(collections_mod, "load_all", lambda **kw: ([], []) if kw.get("skip_unreadable") else [])
    server.served.update({"vega-medcpt-article": codex.space.encoder.weights,
                          "vega-medcpt-query": codex.space.query.encoder.weights})
    with TestClient(api.app) as c:
        c.headers["X-Admin-Token"] = "sekrit"
        r = c.post("/api/share/fetch", json={"collection_id": codex.collection_id, "name": "pubmed-here"})
        assert r.status_code == 202, r.text
        assert (queued[0]["encoder"], queued[0]["embed_model"], queued[0]["query_model"], queued[0]["embed"]) == \
            (LOCALAI, "vega-medcpt-article", "vega-medcpt-query", "p2p")
        # no model loads its weights: held, searched by its text, the reason in plain words
        encoder_resolve.reset_cache()
        server.served.update({"vega-medcpt-article": OTHER, "vega-medcpt-query": OTHER})
        r = c.post("/api/share/fetch", json={"collection_id": codex.collection_id, "name": "pubmed-there"})
        assert r.status_code == 202
        assert (queued[1]["encoder"], queued[1]["embed"]) == ("", "off")
        assert "not searched here" in r.json()["missing"] and "install it there" in r.json()["missing"]


# ---- annotations: a name and a claims file -------------------------------------

def test_annotations_take_a_name_and_a_claims_file_and_the_node_finds_the_target(client):
    from app.hold import collection as col
    snapshot, claims = _target_and_claims(client)
    r = client.post(ANN_ROUTE, params={"name": "pubmed-same-work", "dry_run": "true"}, content=claims)
    assert r.status_code == 200, r.text
    p = r.json()
    assert (p["target"], p["from_snapshot"], p["claims"], p["annotator"]) == \
        ("pubmed", snapshot, 2, "a test annotator"), "the target, its newest snapshot, the contributor"
    assert f"--annotates pubmed --from-snapshot {snapshot}" in p["cli"]
    r = client.post(ANN_ROUTE, params={"name": "pubmed-same-work"}, content=claims)
    assert r.status_code == 202, r.text
    with closing(jobs.connect()) as con:
        done = jobs.run_next(con, "paused")
    assert done.state == "done", done.error
    shown = col.show(louise._default_root(), "pubmed-same-work")
    assert shown["annotates"]["target"] == "pubmed"
    (held,) = collections_mod.load_annotations()
    assert held.codex.descriptive.title.startswith("Annotations on"), "the title derived"


def test_claims_about_a_source_this_node_does_not_hold_say_so(client):
    from tests.hold.test_annotations import a_claim, claims_file
    _target_and_claims(client)
    r = client.post(ANN_ROUTE, params={"name": "x", "dry_run": "true"},
                    content=claims_file(a_claim("gutenberg:1", "gutenberg:2")))
    assert r.status_code == 409
    assert "this node holds no gutenberg collection" in r.json()["detail"]


def test_two_held_collections_that_could_be_the_target_are_not_guessed_between(client):
    from tests.hold.test_annotations import a_target
    snapshot, claims = _target_and_claims(client)
    a_target(louise._default_root(), name="pubmed-too", cleaning_version=2)
    r = client.post(ANN_ROUTE, params={"name": "x", "dry_run": "true"}, content=claims)
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert "pubmed, pubmed-too" in detail and "--annotates <collection>" in detail
    # named on the command line's flag, it is as named — the newest snapshot its own
    r = client.post(ANN_ROUTE, params={"name": "x", "dry_run": "true", "annotates": "pubmed-too"},
                    content=claims)
    assert r.status_code == 200 and r.json()["target"] == "pubmed-too", r.text


def test_the_command_takes_a_claims_file_and_a_name_alone_and_keeps_its_flags(tmp_path, monkeypatch, capsys):
    import json
    from app.bundles import keys as node_keys
    from app.hold import collection as col
    from tests.hold.conftest import KEY, PLATFORM
    from tests.hold.test_annotations import TARGET_SNAPSHOT, a_claim, a_target, claims_file
    monkeypatch.setattr(node_keys, "load_signing_key", lambda *a, **k: KEY)
    a_target(tmp_path)
    claims = tmp_path / "claims.jsonl"
    claims.write_bytes(claims_file(a_claim()))
    base = ["hold", "collection", "create", "annotations", "--root", str(tmp_path), "--claims", str(claims)]
    assert louise.main(base + ["--name", "same", "--dry-run"]) == 0
    preview = json.loads(capsys.readouterr().out)
    assert (preview["target"], preview["from_snapshot"]) == ("pubmed", TARGET_SNAPSHOT)
    assert louise.main(base + ["--name", "same", "--platform-image", PLATFORM, "--snapshot", "2026-09"]) == 0
    capsys.readouterr()
    assert col.show(tmp_path, "same")["annotates"]["target"] == "pubmed"
    # a target named without its snapshot: its newest
    assert louise.main(base + ["--name", "again", "--annotates", "pubmed", "--dry-run"]) == 0
    assert json.loads(capsys.readouterr().out)["from_snapshot"] == TARGET_SNAPSHOT
