"""Collection management from the admin UI: create writes the Tier-2 TOML
(with per-collection path isolation), remove retires it without touching
data, and bundle export/import ride the exact same code paths as the CLI —
the upload is delivery, admission stays the importer's job.
"""
import io
import tarfile
import time

import pytest
from fastapi.testclient import TestClient

from app import collections as collections_mod, config, db, spaces, web
from app.api import app
from app.bundles import importer as bundle_importer


@pytest.fixture
def client(isolated_config, monkeypatch):
    monkeypatch.setattr(spaces, "resolve_embed_model_hash", lambda: ("abc123", False))
    monkeypatch.setattr(web, "_ping", lambda url: True)
    monkeypatch.setattr(config, "ADMIN_TOKEN", "sekrit")
    with TestClient(app) as c:
        c.headers["X-Admin-Token"] = "sekrit"
        yield c


def test_create_writes_isolated_toml_and_collection_appears(client):
    r = client.post("/admin/library/new/create",
                    json={"name": "pubmed2", "plugin": "pubmed"})
    assert r.status_code == 200
    toml_path = collections_mod.config_toml_path("pubmed2")
    assert toml_path.is_file()
    text = toml_path.read_text()
    # the shared-path trap: a second collection MUST NOT inherit the first
    # collection's manifest/fts/lance defaults
    assert "pubmed2/manifest.db" in text
    assert "pubmed2/fts" in text and "pubmed2/lancedb" in text
    assert "RESTART THE WORKER" in r.json()["note"]

    names = [c.name for c in collections_mod.load_all()]
    assert "pubmed2" in names


def test_create_rejects_bad_names_dup_and_unknown_plugin(client):
    assert client.post("/admin/library/new/create",
                       json={"name": "../evil", "plugin": "pubmed"}).status_code == 400
    assert client.post("/admin/library/new/create",
                       json={"name": "x", "plugin": "nope"}).status_code == 400
    assert client.post("/admin/library/new/create",
                       json={"name": "twice", "plugin": "pubmed"}).status_code == 200
    assert client.post("/admin/library/new/create",
                       json={"name": "twice", "plugin": "pubmed"}).status_code == 409


def test_remove_retires_toml_and_keeps_data(client):
    client.post("/admin/library/new/create", json={"name": "temp1", "plugin": "pubmed"})
    collection = collections_mod.by_name("temp1")
    con = db.connect(collection)   # materialize the manifest
    con.close()
    manifest = collection.manifest_path
    assert manifest.exists()

    r = client.post("/admin/library/temp1/remove")
    assert r.status_code == 200
    assert not collections_mod.config_toml_path("temp1").exists()
    retired = r.json()["retired_toml"]
    assert "removed-" in retired
    assert manifest.exists(), "removal must never delete data"
    assert collections_mod.by_name("temp1") is None


def test_remove_refuses_the_synthesized_default(client):
    # no collections/ TOMLs -> the default gutenberg collection is synthesized
    r = client.post("/admin/library/gutenberg/remove")
    assert r.status_code == 400
    assert "synthesized" in r.json()["detail"]


def test_bundle_import_upload_lands_in_inbox_unwrapped(client):
    collection = collections_mod.default_collection()

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for fname in ("header.json", "vectors.parquet", "MANIFEST.sig"):
            data = b"placeholder"
            info = tarfile.TarInfo(f"bundle-x/{fname}")
            info.size = len(data)
            info.mtime = int(time.time())
            tar.addfile(info, io.BytesIO(data))

    r = client.post("/admin/library/gutenberg/bundle/import?filename=bundle-x.tar.gz",
                    content=buf.getvalue(),
                    headers={"Content-Type": "application/gzip"})
    assert r.status_code == 200
    entry = bundle_importer.inbox_dir(collection) / r.json()["entry"]
    # the wrapping folder is unwrapped: the entry itself holds the files
    assert (entry / "header.json").is_file()
    assert (entry / "MANIFEST.sig").is_file()


def test_bundle_import_rejects_garbage_and_traversal(client):
    r = client.post("/admin/library/gutenberg/bundle/import",
                    content=b"this is not a tarball",
                    headers={"Content-Type": "application/gzip"})
    assert r.status_code == 400

    evil = io.BytesIO()
    with tarfile.open(fileobj=evil, mode="w:gz") as tar:
        data = b"evil"
        info = tarfile.TarInfo("../../escape.txt")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    r = client.post("/admin/library/gutenberg/bundle/import",
                    content=evil.getvalue(),
                    headers={"Content-Type": "application/gzip"})
    assert r.status_code == 400
    assert not (collections_mod.default_collection().manifest_path.parent.parent
                / "escape.txt").exists()


def test_bundle_export_400_when_nothing_embedded(client):
    r = client.post("/admin/library/gutenberg/bundle/export", json={})
    assert r.status_code == 400
    assert "no embedded documents" in r.json()["detail"]


def test_collection_management_requires_admin(client):
    del client.headers["X-Admin-Token"]
    for path in ("/admin/library/new/create", "/admin/library/gutenberg/remove",
                  "/admin/library/gutenberg/bundle/export",
                  "/admin/library/gutenberg/bundle/import"):
        assert client.post(path, json={}).status_code == 401, path
