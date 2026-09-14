"""The license gate for EVERY way data arrives (invariant 9, amendment
36, and the operator's rule of 2026-08-27 that a pack is under the same
license as the corpus and pre-embeddings it packs): a packed collection
over a yellow pack is not scanned, not fed by an archive and cannot be
created without acknowledging the pack's own terms text — which is the
origin's, byte-exact, so one ack is valid for both.

Boxed from tests/test_license_gate.py with the `packed` plugin
(2026-09-14): the packed-collection half. Reference only: nothing here is
collected or run. The worker and archive gates live on in the original
file, over an unacknowledged pubmed collection."""
import hashlib
import re
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import collections as collections_mod, config, db, discovery, licensing, spaces, web
from app.api import app
from app.pack import build as pack_build, manifest as manifest_mod

FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures"


def _coll(tmp_path, name, plugin, corpus_dir, **kw):
    d = tmp_path / name
    return collections_mod.Collection(
        name=name, plugin=plugin, profile=None, embed_mode_default=None,
        corpus_dir=Path(corpus_dir), manifest_path=d / "manifest.db",
        fts_dir=d / "fts", lance_dir=d / "lancedb", **kw)


@pytest.fixture
def pubmed_pack(isolated_config, tmp_path):
    origin = _coll(tmp_path, "pubmed", "pubmed", FIXTURES / "pubmed-mini")
    assert licensing.state_for(origin) == licensing.MISSING
    with pytest.raises(pack_build.BuildError, match="acknowledgement"):
        pack_build.build(origin, tmp_path / "packs" / "refused", sign=False, log=lambda *_: None)
    origin = replace(origin, license_ack=licensing.make_ack(origin))
    assert licensing.state_for(origin) == licensing.ACKNOWLEDGED
    con = db.connect(origin)
    discovery.sync_collection(con, origin)
    con.close()
    pack_dir = tmp_path / "packs" / "pubmed"
    pack_build.build(origin, pack_dir, sign=False, log=lambda *_: None)
    return origin, pack_dir


def test_a_pack_carries_the_origins_terms_and_the_same_ack_is_valid_for_both(pubmed_pack, tmp_path):
    origin, pack_dir = pubmed_pack
    m = manifest_mod.load(pack_dir)
    terms = pack_dir / "TERMS.txt"
    assert m["license"]["class"] == "yellow" and terms.is_file()
    assert hashlib.sha256(terms.read_bytes()).hexdigest() == m["license"]["terms_sha256"]
    assert terms.read_bytes() == licensing.terms_path_for(origin).read_bytes(), "byte-exact"
    packed = _coll(tmp_path, "pubmed-pack", "packed", pack_dir)
    assert licensing.license_class_for(packed) == "yellow"
    assert licensing.terms_path_for(packed) == terms
    assert licensing.state_for(packed) == licensing.MISSING
    # the origin's ack IS the pack's ack (same terms, same sha256)
    assert licensing.state_for(replace(packed, license_ack=origin.license_ack)) == licensing.ACKNOWLEDGED
    # an ack for OTHER terms is not one
    assert licensing.state_for(replace(packed, license_ack="2026-01-01T00:00:00Z/" + "0" * 64)) == licensing.MISSING
    with pytest.raises(Exception, match="requires an acknowledgement"):
        licensing.require_ack(packed, use="use")
    # green needs nothing; the gate never blocks it
    green = _coll(tmp_path, "g", "gutenberg", tmp_path / "corpus")
    assert licensing.state_for(green) == licensing.NOT_REQUIRED


@pytest.fixture
def client(isolated_config, monkeypatch, tmp_path):
    monkeypatch.setattr(spaces, "resolve_embed_model_hash", lambda: ("abc123", False))
    monkeypatch.setattr(web, "_ping", lambda url: True)
    monkeypatch.setattr(config, "ADMIN_TOKEN", "sekrit")
    monkeypatch.setattr(config, "CORPUS_MOUNTS_DIR", tmp_path / "corpora")
    (tmp_path / "corpora").mkdir(exist_ok=True)
    monkeypatch.setattr(config, "PACK_MOUNTS_DIR", tmp_path / "packs")
    with TestClient(app) as c:
        c.headers["X-Admin-Token"] = "sekrit"
        yield c


def test_creating_a_packed_collection_over_a_yellow_pack_needs_the_ack(client, pubmed_pack):
    origin, pack_dir = pubmed_pack
    body = {"name": "pm-pack", "plugin": "packed", "profile": "hand-populated",
            "corpus_dir": pack_dir.as_posix()}
    # the preview shows the PACK's terms (not a spec's)
    r = client.post("/admin/library/new/preview", json=body)
    assert r.status_code == 200 and r.json()["license_class"] == "yellow"
    assert r.json()["terms_text"] and r.json()["terms_text"] in (pack_dir / "TERMS.txt").read_text(encoding="utf-8")
    r = client.post("/admin/library/new/create", json=body)
    assert r.status_code == 409 and "terms-acknowledgement-required" in r.json()["detail"]
    assert collections_mod.by_name("pm-pack") is None
    r = client.post("/admin/library/new/create", json={**body, "acknowledge_terms": True})
    assert r.status_code == 200, r.text
    created = collections_mod.by_name("pm-pack")
    assert created is not None and created.plugin == "packed"
    assert licensing.state_for(created) == licensing.ACKNOWLEDGED
    _, _, digest = created.license_ack.partition("/")
    assert digest == origin.license_ack.partition("/")[2], "the origin's ack, for the pack"
    # the card and the terms endpoint speak for the pack
    r = client.get("/admin/library/pm-pack/terms")
    assert r.status_code == 200 and r.json()["license_class"] == "yellow"
    state = web._license_state(created)
    assert state["class"] == "yellow" and state["state"] == "acknowledged" and not state["needs_ack"]


def test_a_green_pack_needs_no_ack(client, isolated_config, tmp_path, corpus_dir):
    g = _coll(tmp_path, "g", "gutenberg", corpus_dir)
    con = db.connect(g)
    discovery.sync_collection(con, g)
    con.close()
    pack_dir = tmp_path / "packs" / "gutenberg"
    pack_build.build(g, pack_dir, records_per_shard=2, sign=False, log=lambda *_: None)
    r = client.post("/admin/library/new/create",
                    json={"name": "g-pack", "plugin": "packed", "profile": "hand-populated",
                          "corpus_dir": pack_dir.as_posix()})
    assert r.status_code == 200, r.text
    created = collections_mod.by_name("g-pack")
    assert created.license_ack is None and licensing.state_for(created) == licensing.NOT_REQUIRED
    assert not re.search(r"license_ack", collections_mod.config_toml_path("g-pack").read_text())
