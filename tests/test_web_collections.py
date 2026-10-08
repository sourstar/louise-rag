"""Collection management from the admin UI: remove retires the Tier-2 file
without touching data, and bundle export/import ride the exact same code
paths as the CLI — the upload is delivery, admission stays the importer's
job. Creating a collection is `POST /api/hold/collection/create`
(tests/test_hold_create_route.py); the old create route is gone.
"""
import io
import tarfile
import time

import pytest
from fastapi.testclient import TestClient

from app import collections as collections_mod, config, db, spaces, web
from app.api import app
from app.bundles import importer as bundle_importer
from tests.conftest import codex_collection


@pytest.fixture
def client(isolated_config, monkeypatch, tmp_path):
    monkeypatch.setattr(spaces, "resolve_embed_model_hash", lambda *a: ("abc123", False))
    monkeypatch.setattr(web, "_ping", lambda url: True)
    monkeypatch.setattr(config, "ADMIN_TOKEN", "sekrit")
    monkeypatch.setattr(config, "CORPUS_MOUNTS_DIR", tmp_path / "corpora")
    (tmp_path / "corpora").mkdir(exist_ok=True)
    with TestClient(app) as c:
        c.headers["X-Admin-Token"] = "sekrit"
        yield c


def test_the_old_create_route_is_gone(client):
    """It wrote a file naming no Codex, refused at load since 2026-10-08:
    a 410 that names the route that creates one, and nothing written."""
    r = client.post("/admin/library/new/create",
                    json={"name": "pubmed2", "plugin": "pubmed"})
    assert r.status_code == 410
    assert "/api/hold/collection/create" in r.json()["detail"]
    assert not collections_mod.config_toml_path("pubmed2").exists()
    assert [c.name for c in collections_mod.load_all()] == ["gutenberg"]


def test_a_collection_holds_the_mount_it_reads(client, tmp_path):
    """A collection the node serves holds the mount it reads, and the
    Library's mount list says which collection uses it."""
    mounts_root = config.CORPUS_MOUNTS_DIR
    (mounts_root / "pubmed").mkdir(parents=True)
    (mounts_root / "spare").mkdir()
    chosen = (mounts_root / "pubmed").as_posix()
    codex_collection("withdir", louise_root=config.DATA_DIR,
                     config_dir=collections_mod.collections_dir().parent,
                     corpus_dir=mounts_root / "pubmed")
    assert collections_mod.by_name("withdir").corpus_dir.as_posix() == chosen
    used = {m["path"]: m["used_by"] for m in web._corpus_mounts()}
    assert used == {chosen: "withdir", (mounts_root / "spare").as_posix(): None}


def test_remove_retires_toml_and_keeps_data(client):
    codex_collection("temp1", louise_root=config.DATA_DIR,
                     config_dir=collections_mod.collections_dir().parent)
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


def test_removing_the_last_collection_leaves_zero_collections(client):
    """Louise2, 2026-08-26: the operator could not delete every collection.
    Removing the last file retires it: zero collections, data kept, and
    nothing is synthesized in its place."""
    assert [c.name for c in collections_mod.load_all()] == ["gutenberg"]
    r = client.post("/admin/library/gutenberg/remove")
    assert r.status_code == 200, r.text
    assert "removed-" in r.json()["retired_toml"]
    assert collections_mod.collections_dir().is_dir()
    assert collections_mod.load_all() == []
    assert collections_mod.by_name("gutenberg") is None
    # the node keeps working with none: the Library renders, control state resolves
    assert client.get("/admin/library").status_code == 200
    assert db.worker_state(db.connect()) in ("paused", "running", "seed-only", "acquire-only")
    # a second remove is a 404, not a crash; nothing comes back
    assert client.post("/admin/library/gutenberg/remove").status_code == 404
    assert collections_mod.load_all() == []


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


# the export itself is the worker's now: tests/test_jobs.py,
# test_the_librarys_export_is_built_by_the_worker_and_served_once


def test_bundle_export_refuses_when_export_dir_is_not_mounted(client, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "EXPORT_DIR", tmp_path / "not-mounted")
    r = client.post("/admin/library/gutenberg/bundle/export",
                    json={"uids": ["gutenberg:1"]})
    assert r.status_code == 503
    assert "EXPORT_DIR" in r.json()["detail"]
    manifest_dir = collections_mod.default_collection().manifest_path.parent
    assert not list(manifest_dir.glob("bundle-export-*")), "nothing beside the manifest"


def test_library_page_offers_profile_dropdowns_with_estimates(client):
    r = client.get("/admin/library")
    assert r.status_code == 200
    # the Add panel's per-plugin profile data, ordered by size
    assert "PLUGIN_PROFILES" in r.text
    for profile in ("demo", "starter", "recent", "full"):
        assert profile in r.text
    assert "~80 GB" in r.text     # gutenberg full estimate reaches the UI


def test_grow_options_never_offer_a_rollback(client):
    from app import web as web_mod
    base = collections_mod.default_collection()
    collection = collections_mod.Collection(
        name="pm-recent", plugin="pubmed", profile="recent", embed_mode_default=None,
        corpus_dir=base.corpus_dir, manifest_path=base.manifest_path,
        fts_dir=base.fts_dir, lance_dir=base.lance_dir)
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


def test_grow_run_declares_the_profile_and_preserves_the_whole_toml(client):
    """Growing must NOT download inside the web request (a multi-GB fetch
    hung the modal live) — it declares the selection in the file and the
    worker's hot-reload acquires. The rewrite must carry every field:
    dropping [encoder] would leave the collection with no encoder. A Codex
    collection holds every slice or none (07 §7.8), so growing it means
    everything and anything smaller is a 409."""
    from app.acquire import spec as acquire_spec
    from app.acquire.specs import ACQUIRE_REGISTRY
    everything = acquire_spec.everything_profile(ACQUIRE_REGISTRY["gutenberg"])
    toml_path = collections_mod.config_toml_path("gutenberg")
    toml_path.write_text(toml_path.read_text(encoding="utf-8")
                         + 'embed = "all"\n\n[encoder]\nurl = "http://enc:1"\n'
                           'query_url = "http://enc:2"\nembed_model = "m"\nquery_model = "q"\n', encoding="utf-8")
    before = collections_mod.by_name("gutenberg")

    r = client.post("/admin/library/gutenberg/grow/run", json={"profile": everything})
    assert r.status_code == 200, r.text
    assert "worker" in r.json()["note"]

    refreshed = collections_mod.by_name("gutenberg")
    assert refreshed.codex.collection_id == before.codex.collection_id
    assert refreshed.slices == "all" and refreshed.acquire_enabled
    assert refreshed.corpus_dir == before.corpus_dir
    assert refreshed.embed_mode_default == "all"
    assert refreshed.embed is not None and refreshed.embed.url == "http://enc:1"
    assert refreshed.embed.query_url == "http://enc:2"

    smaller = next(p for p in ACQUIRE_REGISTRY["gutenberg"].profiles if p != everything)
    assert client.post("/admin/library/gutenberg/grow/run",
                       json={"profile": smaller}).status_code == 409
    assert client.post("/admin/library/gutenberg/grow/run",
                       json={"profile": "gigantic"}).status_code == 400


def test_embed_mode_is_a_per_collection_control(client):
    r = client.post("/admin/library/gutenberg/embed-mode", json={"mode": "all"})
    assert r.status_code == 200
    assert r.json()["persisted"] is True

    # written to the COLLECTION's own manifest scope for immediate effect...
    collection = collections_mod.default_collection()
    assert db.get_scope(db.connect(collection))["embed_mode"] == "all"
    # ...AND to the TOML as desired state, so a restart keeps it — the
    # operator lost their mode at every restart when this was runtime-only
    # (seven deploys in one day, seven silent resets)
    assert collection.embed_mode_default == "all", \
        "the Library's embed-mode choice must be written to the TOML"
    from app import worker
    con = db.connect(collection)
    worker._apply_tier2_embed_mode(con, collection, "restart")   # the boot path
    assert db.get_scope(con)["embed_mode"] == "all", \
        "a restart must keep the operator's choice"

    # another collection keeps its own default — no cross-talk
    codex_collection("other", louise_root=config.DATA_DIR,
                     config_dir=collections_mod.collections_dir().parent)
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
