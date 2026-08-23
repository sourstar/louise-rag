"""Install-time companion for the wikipedia plugin, and embed_mode
surviving every TOML rewrite.

Creating a live wikipedia collection can now also create its pre-embedded
companion (<name>-vec, embedded plugin): the frozen published snapshot
whose vectors travel with it, so dense search answers from day one while
the live collection embeds lazily. The companion needs its own corpus
mount and its OWN terms acknowledgement (the embedded plugin is
yellow-class with its own terms text — consent to wikipedia's terms never
implies consent to the dataset's).

Separately: `embed_mode` is Tier-2 desired state in the TOML, and every
endpoint that rewrites a collection's TOML (grow, ack, materialize) must
carry it forward — dropping it would silently reset the collection to
'lazy' at the next restart, the same trap the [embed] block already
guards against.
"""
import re
import tomllib

import pytest
from fastapi.testclient import TestClient

from app import collections as collections_mod, config, spaces, web
from app.api import app


@pytest.fixture
def client(isolated_config, monkeypatch, tmp_path):
    monkeypatch.setattr(spaces, "resolve_embed_model_hash", lambda: ("abc123", False))
    monkeypatch.setattr(web, "_ping", lambda url: True)
    monkeypatch.setattr(config, "ADMIN_TOKEN", "sekrit")
    monkeypatch.setattr(config, "CORPUS_MOUNTS_DIR", tmp_path / "corpora")
    (tmp_path / "corpora").mkdir(exist_ok=True)
    with TestClient(app) as c:
        c.headers["X-Admin-Token"] = "sekrit"
        yield c


def _mount(name):
    folder = config.CORPUS_MOUNTS_DIR / name
    folder.mkdir(parents=True, exist_ok=True)
    return folder.as_posix()


def _create(client, **body):
    payload = {"name": "wiki", "plugin": "wikipedia", "profile": "demo",
               "corpus_dir": _mount("wiki")}
    payload.update(body)
    return client.post("/admin/library/new/create", json=payload)


def test_companion_needs_its_own_terms_ack_then_creates_both(client):
    comp = _mount("wikivec")
    # the embedded plugin's terms are its own gate — a separate, named 409
    r = _create(client, companion_corpus_dir=comp)
    assert r.status_code == 409
    assert r.json()["detail"].startswith("companion-terms-acknowledgement-required")
    assert collections_mod.by_name("wiki") is None, \
        "nothing may be half-created when the companion is refused"

    r = _create(client, companion_corpus_dir=comp,
                acknowledge_companion_terms=True)
    assert r.status_code == 200, r.json()
    assert r.json()["companion"] == "wiki-vec"
    assert "wiki-vec" in r.json()["note"]

    raw = tomllib.loads(
        collections_mod.config_toml_path("wiki-vec").read_text(encoding="utf-8"))
    assert raw["plugin"] == "embedded"
    assert raw["genesis_archive"] == "upstash-wikipedia-2024-06"
    assert raw["acquire_enabled"] is True
    assert raw["license_ack"], "the companion's own ack is recorded"
    assert raw["embed_mode"] == "all", \
        "the companion is born on 'fix': gap-fill the few docs the archive can't cover"
    assert raw["corpus_dir"] == comp
    # isolated paths, never shared with the main collection
    assert "wiki-vec/manifest.db" in raw["paths"]["manifest"]

    main = tomllib.loads(
        collections_mod.config_toml_path("wiki").read_text(encoding="utf-8"))
    assert main["plugin"] == "wikipedia"
    assert "genesis_archive" not in main, \
        "the LIVE collection has no archive — its companion carries the vectors"


def test_companion_refuses_shared_mount_and_foreign_plugins(client):
    shared = _mount("one")
    r = _create(client, corpus_dir=shared, companion_corpus_dir=shared,
                acknowledge_companion_terms=True)
    assert r.status_code == 409
    assert "OWN corpus folder" in r.json()["detail"]

    r = client.post("/admin/library/new/create", json={
        "name": "pm", "plugin": "pubmed", "corpus_dir": _mount("pm"),
        "companion_corpus_dir": _mount("pmvec"),
        "acknowledge_companion_terms": True})
    assert r.status_code == 400
    assert "wikipedia plugin only" in r.json()["detail"]
    assert collections_mod.by_name("pm") is None


def test_embed_mode_survives_the_ack_rewrite(client):
    """The ack endpoint rewrites the TOML; embed_mode must ride along."""
    comp = _mount("wv2")
    r = client.post("/admin/library/new/create", json={
        "name": "wiki2", "plugin": "wikipedia", "profile": "demo",
        "corpus_dir": _mount("wiki2"), "companion_corpus_dir": comp,
        "acknowledge_companion_terms": True})
    assert r.status_code == 200

    # wikipedia (yellow) had no acquisition at create; ack it now — the
    # rewrite must not drop the companion's embed_mode... the companion is
    # untouched; ack wiki2's own TOML after giving it an embed_mode
    toml_path = collections_mod.config_toml_path("wiki2")
    text = toml_path.read_text(encoding="utf-8")
    toml_path.write_text(text.replace('plugin = "wikipedia"',
                                      'plugin = "wikipedia"\nembed_mode = "off"'),
                         encoding="utf-8")
    assert client.post("/admin/library/wiki2/ack", json={}).status_code == 200
    raw = tomllib.loads(toml_path.read_text(encoding="utf-8"))
    assert raw["embed_mode"] == "off", \
        "a TOML rewrite dropped embed_mode — Tier-2 desired state lost"
    assert raw["license_ack"]
