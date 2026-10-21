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


def test_removing_a_copy_taken_from_peers_forgets_it_through_the_worker(client, monkeypatch):
    """Operator, 2026-10-20 (Perugia): a copy taken from the offer, removed,
    and taken again under another name was refused — retiring kept its
    directory and so its id. A `source = "shard"` collection is FORGOTTEN by
    default (`hold collection forget`, through the worker, the one writer);
    a collection built here is retired, as before; the body may say either."""
    from app.hold import deposit
    codex_collection("copy1", louise_root=config.DATA_DIR,
                     config_dir=collections_mod.collections_dir().parent)
    p = collections_mod.config_toml_path("copy1")
    p.write_text(p.read_text(encoding="utf-8") + '\nsource = "shard"\n', encoding="utf-8")
    assert collections_mod.by_name("copy1").source == "shard"
    asked = []
    monkeypatch.setattr(deposit, "via_worker",
                        lambda op, name, **kw: asked.append((op, name, kw)) or {"collection": name, "removed": ["x"]})
    r = client.post("/admin/library/copy1/remove")
    assert r.status_code == 200, r.text
    assert r.json()["mode"] == "forget" and asked == [("forget", "copy1", {"kinds": []})]
    assert collections_mod.config_toml_path("copy1").exists(), "the worker removes the file; here it was mocked"
    # asked to retire instead, a copy is retired
    r = client.post("/admin/library/copy1/remove", json={"mode": "retire"})
    assert r.status_code == 200 and r.json()["mode"] == "retire" and len(asked) == 1
    assert not collections_mod.config_toml_path("copy1").exists()
    # a collection built here: retire by default, forget on request, a bad mode refused
    assert client.post("/admin/library/gutenberg/remove", json={"mode": "sometimes"}).status_code == 400
    r = client.post("/admin/library/gutenberg/remove", json={"mode": "forget"})
    assert r.status_code == 200 and asked[-1][1] == "gutenberg"
    # no worker: the command to run by hand
    monkeypatch.setattr(deposit, "via_worker",
                        lambda op, name, **kw: (_ for _ in ()).throw(deposit.DepositError("the worker has not done it yet")))
    r = client.post("/admin/library/gutenberg/remove", json={"mode": "forget"})
    assert r.status_code == 409 and "--here" in r.text


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


def test_all_p2p_comes_with_sharing_and_sharing_off_takes_p2p_back_to_all_local(client):
    """07 §7.8, 2026-10-19: two independent controls, one meeting point. The
    page asks; the server writes both settings in one file rewrite."""
    collection = collections_mod.default_collection()
    assert collection.share is False
    r = client.post("/admin/library/gutenberg/embed-mode", json={"mode": "p2p"})
    assert r.status_code == 200 and r.json()["share"] is True and r.json()["share_enabled"] is True
    c = collections_mod.by_name("gutenberg")
    assert c.embed_mode_default == "p2p" and c.share is True and c.embed_mode_note is None
    text = collections_mod.config_toml_path("gutenberg").read_text(encoding="utf-8")
    assert 'embed = "p2p"' in text and "share = true" in text
    assert db.get_scope(db.connect(c))["embed_mode"] == "p2p"
    # sharing off under p2p: the mode goes back to all local in the same write
    r = client.post("/admin/library/gutenberg/share", json={"share": False})
    assert r.status_code == 200 and r.json() == {**r.json(), "share": False, "embed_mode": "all",
                                                 "embed_mode_changed": True}
    c = collections_mod.by_name("gutenberg")
    assert c.embed_mode_default == "all" and c.share is False and c.embed_mode_note is None
    assert db.get_scope(db.connect(c))["embed_mode"] == "all", "the runtime scope follows"
    # sharing on and off under another mode touches the mode not at all
    r = client.post("/admin/library/gutenberg/share", json={"share": True})
    assert r.json()["embed_mode_changed"] is False and collections_mod.by_name("gutenberg").share is True
    r = client.post("/admin/library/gutenberg/embed-mode", json={"mode": "all"})
    assert r.json()["share"] is True and r.json()["share_enabled"] is False
    r = client.post("/admin/library/gutenberg/share", json={"share": False})
    assert r.json()["embed_mode"] == "all" and collections_mod.by_name("gutenberg").share is False
    assert client.post("/admin/library/gutenberg/embed-mode", json={"mode": "all p2p"}).status_code == 400, \
        "the API carries the token, the page the name"
    # the card carries the two axes and the file's word
    r = client.get("/admin/library", headers={"X-Admin-Token": "sekrit"})
    assert r.status_code == 200 and "all p2p" in r.text and "all local" in r.text and "shared" in r.text


def _an_annotations_collection(monkeypatch):
    """A held target (pubmed) and an annotations collection about it, on
    this test's node (tests/hold/test_annotations.py's helpers)."""
    from app.bundles import keys as node_keys
    from tests.hold.conftest import KEY
    from tests.hold.test_annotations import a_target, make
    monkeypatch.setattr(node_keys, "load_signing_key", lambda *a, **k: KEY)
    root = collections_mod.louise_root() / "collections"
    a_target(root)
    codex, snap = make(root)
    return codex, snap


def test_the_library_shows_an_annotations_collection_on_its_own_card(client, monkeypatch):
    """02 §2.10: an annotations collection is held and shared like any
    collection but never ingested or embedded, so `load_all` leaves it out
    — and the Library showed nothing of it (operator, 2026-10-19: "I don't
    see the collection, nor the annotations type"). Its card names the
    target, the target snapshot the claims hold from and the claims count;
    it has no stage bar, no encoder and no profile."""
    codex, snap = _an_annotations_collection(monkeypatch)
    assert "pubmed-same-work" not in [c.name for c in collections_mod.load_all()]
    r = client.get("/admin/library")
    assert r.status_code == 200
    html = r.text
    card = html[html.index('data-collection="pubmed-same-work"'):]
    card = card[:card.index('<input type="file" id="bundle-file"')]
    assert 'data-kind="annotations"' in card
    assert '<span class="badge">annotations</span>' in card
    assert "annotates: pubmed" in card and "from snapshot: 2026-08" in card
    assert "2 claims in snapshot 2026-09" in card
    assert "kinds: records, relations" in card
    # the id in the badge row, as a held card shows the space; not trailing the notes
    assert f'<span class="badge lapis" title="collection_id">id: {codex.collection_id}</span>' in card
    assert card.count(codex.collection_id) == 1
    # the served collection's controls do not apply here
    head = card[:card.index("remove…")]
    assert "grow profile" not in head and "embed mode" not in head and "setEmbedMode" not in head


def test_sharing_an_annotations_collection_writes_its_file_without_an_encoder(client, monkeypatch):
    """The Library's share toggle reaches an annotations collection (its
    route resolved names through `load_all` only, a 404 here) and the
    rewrite carries no `[encoder]` and no `embed`: it has neither."""
    _an_annotations_collection(monkeypatch)
    toml_path = collections_mod.config_toml_path("pubmed-same-work")
    assert "share = true" not in toml_path.read_text(encoding="utf-8")
    r = client.post("/admin/library/pubmed-same-work/share", json={"share": True})
    assert r.status_code == 200, r.text
    assert r.json()["share"] is True and r.json()["embed_mode_changed"] is False
    text = toml_path.read_text(encoding="utf-8")
    assert "share = true" in text and "[encoder]" not in text and "embed =" not in text
    (held,) = collections_mod.load_annotations()
    assert held.share is True and held.embed is None
    r = client.post("/admin/library/pubmed-same-work/share", json={"share": False})
    assert r.status_code == 200
    assert "share = false" in toml_path.read_text(encoding="utf-8")


def test_the_add_panel_offers_the_annotations_type_with_the_held_targets(client):
    """Operator, 2026-10-19: "Add a collection does not show the annotations
    type". The type is offered beside the plugins, and its target list is
    the served collections with the snapshot each holds now."""
    r = client.get("/admin/library")
    html = r.text
    assert '<option value="annotations">' in html
    assert 'id="na_target"' in html and '<option value="gutenberg" data-snapshot=' in html
    assert "/api/hold/collection/create-annotations" in html


def test_a_mode_or_sharing_switch_asks_for_a_digest_at_once(client):
    """04 §4.5 (operator, 2026-10-20): switching away from p2p releases the
    parts this node leads — the next digest says so, and the switch asks
    the worker's gossip beat to send it now, through the control table."""
    from app import node_state
    node_state.take_digest_request()                      # a clean slate
    assert node_state.take_digest_request() is False
    r = client.post("/admin/library/gutenberg/embed-mode", json={"mode": "lazy"})
    assert r.status_code == 200, r.text
    assert node_state.take_digest_request() is True and node_state.take_digest_request() is False, \
        "one request, taken once"
    r = client.post("/admin/library/gutenberg/share", json={"share": True})
    assert r.status_code == 200, r.text
    assert node_state.take_digest_request() is True


def test_an_annotations_card_names_a_records_twin_of_its_target(client, monkeypatch):
    """02 §2.4 A1f: claims about a target apply to the collection a carry
    made of it, byte for byte (368365e), so the card names that collection
    instead of "a collection this node does not hold" (Perugia, 2026-10-21:
    gutenberg-book-editions named the retired gutenberg)."""
    import json
    from types import SimpleNamespace
    from app import web
    from app.hold import carry
    codex, snap = _an_annotations_collection(monkeypatch)
    old = collections_mod.load_annotations()[0]
    (target,) = [t for t in __import__("app.hold.codex", fromlist=["targets_of"]).targets_of(old.codex)]
    root = collections_mod.louise_root() / "collections"
    twin = SimpleNamespace(name="pubmed-v2", codex=SimpleNamespace(collection_id="sha256:" + "7a" * 32))
    state = root / "pubmed-v2" / "state"
    state.mkdir(parents=True)
    assert web._annotations_shelf(old, [twin])["targets"][0]["target"] != "pubmed-v2"
    (state / carry.MARKER).write_text(json.dumps(
        {"done": True, "mode": "move", "from": target.collection_id, "to": twin.codex.collection_id}))
    assert web._annotations_shelf(old, [twin])["targets"][0]["target"] == "pubmed-v2"
