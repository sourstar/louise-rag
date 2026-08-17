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


def test_create_writes_isolated_toml_and_collection_appears(client, isolated_config):
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
    assert "no restart" in r.json()["note"]

    names = [c.name for c in collections_mod.load_all()]
    assert "pubmed2" in names
    # creating the FIRST TOML must not disown the synthesized default: its
    # definition gets materialized alongside (observed live: a test TOML
    # silently hid 79k books)
    assert "gutenberg" in names
    assert collections_mod.config_toml_path("gutenberg").is_file()
    gutenberg = collections_mod.by_name("gutenberg")
    assert gutenberg.manifest_path == isolated_config.DATA_DIR / "manifest.db"


def test_create_with_dedicated_corpus_mount(client, tmp_path, monkeypatch):
    mounts_root = tmp_path / "corpora"
    (mounts_root / "pubmed").mkdir(parents=True)
    (mounts_root / "spare").mkdir()
    monkeypatch.setattr(config, "CORPUS_MOUNTS_DIR", mounts_root)

    chosen = (mounts_root / "pubmed").as_posix()
    r = client.post("/admin/library/new/create",
                    json={"name": "withdir", "plugin": "pubmed",
                           "corpus_dir": chosen})
    assert r.status_code == 200
    assert r.json()["corpus_mounted"] is True
    assert collections_mod.by_name("withdir").corpus_dir.as_posix() == chosen

    # one corpus, one collection: the claimed mount is refused for another
    r = client.post("/admin/library/new/create",
                    json={"name": "second", "plugin": "pubmed",
                           "corpus_dir": chosen})
    assert r.status_code == 409
    assert "withdir" in r.json()["detail"]

    # arbitrary container paths are NOT accepted — mounts-root entries only
    for bad in ("/somewhere/else", "../etc", str(tmp_path / "outside")):
        r = client.post("/admin/library/new/create",
                        json={"name": "bad", "plugin": "pubmed", "corpus_dir": bad})
        assert r.status_code == 400, bad
        assert "available" in r.json()["detail"]

    # the library page offers the mounts, marking the claimed one
    page = client.get("/admin/library")
    assert (mounts_root / "spare").as_posix() in page.text
    assert "in use by withdir" in page.text


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


def test_library_page_offers_profile_dropdowns_with_estimates(client):
    r = client.get("/admin/library")
    assert r.status_code == 200
    # the Add panel's per-plugin profile data, ordered by size
    assert "PLUGIN_PROFILES" in r.text
    for profile in ("demo", "starter", "recent", "full"):
        assert profile in r.text
    assert "~80 GB" in r.text     # gutenberg full estimate reaches the UI


def test_grow_options_never_offer_a_rollback(client):
    client.post("/admin/library/new/create",
                json={"name": "pm-recent", "plugin": "pubmed", "profile": "recent"})
    from app import web as web_mod
    collection = collections_mod.by_name("pm-recent")
    names = [o["name"] for o in web_mod._grow_options(collection)]
    assert names == ["recent", "full"], "smaller profiles must not be offered"
    assert "current" in web_mod._grow_options(collection)[0]["label"]

    # a collection whose profile isn't a known preset (hand-populated) gets
    # the full ordered list — nothing to roll back from
    hand = collections_mod.Collection(
        name="hand", plugin="gutenberg", profile=None, embed_mode_default=None,
        corpus_dir=collection.corpus_dir, manifest_path=collection.manifest_path,
        fts_dir=collection.fts_dir, lance_dir=collection.lance_dir)
    assert [o["name"] for o in web_mod._grow_options(hand)] == \
        ["demo", "starter", "recent", "full"]


def test_yellow_source_acquisition_requires_ack_from_the_ui(client):
    # pubmed is yellow-class: create with acquisition but WITHOUT
    # acknowledgement must refuse with the retry marker
    r = client.post("/admin/library/new/create",
                    json={"name": "pm", "plugin": "pubmed",
                           "acquire_enabled": True})
    assert r.status_code == 409
    assert r.json()["detail"].startswith("terms-acknowledgement-required")

    # with explicit consent, the ack is computed server-side into the TOML
    r = client.post("/admin/library/new/create",
                    json={"name": "pm", "plugin": "pubmed",
                           "acquire_enabled": True, "acknowledge_terms": True})
    assert r.status_code == 200
    text = collections_mod.config_toml_path("pm").read_text()
    assert "acquire_enabled = true" in text
    assert 'license_ack = "' in text
    assert collections_mod.by_name("pm").license_ack

    # green sources (gutenberg) need no acknowledgement
    r = client.post("/admin/library/new/create",
                    json={"name": "gb2", "plugin": "gutenberg",
                           "acquire_enabled": True})
    assert r.status_code == 200


def test_ack_endpoint_records_license_for_existing_collection(client):
    # created WITHOUT acquisition: no ack recorded, card should offer it
    client.post("/admin/library/new/create", json={"name": "pm2", "plugin": "pubmed"})
    assert collections_mod.by_name("pm2").license_ack is None

    r = client.get("/admin/library/pm2/terms")
    assert r.status_code == 200
    assert r.json()["license_class"] == "yellow"
    assert r.json()["terms_text"]

    r = client.post("/admin/library/pm2/ack")
    assert r.status_code == 200
    refreshed = collections_mod.by_name("pm2")
    assert refreshed.license_ack and refreshed.acquire_enabled
    # the ack format matches what the acquisition engine verifies
    stamp, _, digest = refreshed.license_ack.partition("/")
    assert stamp.endswith("Z") and len(digest) == 64


def test_grow_run_declares_the_profile_and_preserves_the_whole_toml(client):
    """Growing must NOT download inside the web request (a multi-GB fetch
    hung the modal live) — it declares the profile in the TOML and the
    worker's hot-reload acquires. The rewrite must carry every field:
    dropping [embed] would flip the collection back to the env recipe."""
    client.post("/admin/library/new/create",
                json={"name": "pm", "plugin": "pubmed",
                       "acquire_enabled": True, "acknowledge_terms": True})
    created = collections_mod.by_name("pm")
    toml_path = collections_mod.config_toml_path("pm")
    toml_path.write_text(
        f'plugin = "pubmed"\nprofile = "demo"\nacquire_enabled = true\n'
        f'license_ack = "{created.license_ack}"\n'
        f'genesis_archive = "medcpt-pubmed"\n'
        f'corpus_dir = "{created.corpus_dir.as_posix()}"\n\n'
        f'[embed]\nurl = "http://enc:1"\nmodel = "medcpt"\ndim = 768\n'
        f'query_url = "http://enc:2"\n\n'
        f'[paths]\nmanifest = "{created.manifest_path.as_posix()}"\n'
        f'fts = "{created.fts_dir.as_posix()}"\n'
        f'lancedb = "{created.lance_dir.as_posix()}"\n', encoding="utf-8")

    r = client.post("/admin/library/pm/grow/run", json={"profile": "recent"})
    assert r.status_code == 200
    assert "worker" in r.json()["note"]

    refreshed = collections_mod.by_name("pm")
    assert refreshed.profile == "recent"
    assert refreshed.acquire_enabled and refreshed.license_ack
    assert refreshed.genesis_archive == "medcpt-pubmed"
    assert refreshed.embed is not None and refreshed.embed.query_url == "http://enc:2"

    assert client.post("/admin/library/pm/grow/run",
                       json={"profile": "gigantic"}).status_code == 400


def test_embed_mode_is_a_per_collection_control(client):
    r = client.post("/admin/library/gutenberg/embed-mode", json={"mode": "all"})
    assert r.status_code == 200

    # written to the COLLECTION's own manifest scope
    collection = collections_mod.default_collection()
    assert db.get_scope(db.connect(collection))["embed_mode"] == "all"

    # another collection keeps its own default — no cross-talk
    client.post("/admin/library/new/create", json={"name": "other", "plugin": "pubmed"})
    other = collections_mod.by_name("other")
    assert db.get_scope(db.connect(other))["embed_mode"] == "lazy"

    assert client.post("/admin/library/gutenberg/embed-mode",
                       json={"mode": "sideways"}).status_code == 400

    # the card renders the selector with the live value
    page = client.get("/admin/library")
    assert "Embed mode" in page.text and "setEmbedMode" in page.text


def test_collection_management_requires_admin(client):
    del client.headers["X-Admin-Token"]
    for path in ("/admin/library/new/create", "/admin/library/gutenberg/remove",
                  "/admin/library/gutenberg/bundle/export",
                  "/admin/library/gutenberg/bundle/import"):
        assert client.post(path, json={}).status_code == 401, path
