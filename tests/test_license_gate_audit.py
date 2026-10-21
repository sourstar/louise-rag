"""Invariant 9 on the doors a full audit found open (2026-09-13).

The audit mapped 129 paths that use, import or exchange a collection's
data. Beyond those closed by the `louise health` review and its
verification, these were still open: the reader (`/read`, the whole text,
reachable from Ask), the book catalogue API, the worker's inline rescan,
`snapshot delta` (retired with app/cli.py: its successor writes nothing
shareable and reads only snapshots, which a peer holds before any
acknowledgement), the tools shipped in the image, and search's gate,
which failed OPEN on its own error. Shared backstops now sit in the bundle
importer and exporter, so a door that forgets cannot import or export.
Controls prove each call is otherwise made.
"""

import pytest
from fastapi.testclient import TestClient

from app import collections as collections_mod, config, db, discovery, licensing, \
    spaces, web
from app.acquire.spec import LicenseAckRequiredError
from app.api import app


def _withheld(monkeypatch):
    monkeypatch.setattr(licensing, "state_for", lambda c, spec=None: licensing.MISSING)
    monkeypatch.setattr(licensing, "license_class_for", lambda c, spec=None: "yellow")


def _free(monkeypatch):
    monkeypatch.setattr(licensing, "state_for", lambda c, spec=None: licensing.NOT_REQUIRED)


@pytest.fixture
def client(isolated_config, monkeypatch):
    monkeypatch.setattr(spaces, "resolve_embed_model_hash", lambda *a: ("abc123", False))
    monkeypatch.setattr(web, "_ping", lambda url: True)
    monkeypatch.setattr(config, "ADMIN_TOKEN", "sekrit")
    with TestClient(app) as c:
        c.headers["X-Admin-Token"] = "sekrit"
        yield c


def test_the_reader_refuses_the_text_of_an_unacknowledged_collection(client, monkeypatch):
    """/read serves a document's whole text to anyone who can reach Ask."""
    name = collections_mod.load_all()[0].name
    _withheld(monkeypatch)
    assert client.get(f"/read/{name}/999999").status_code == 403
    assert client.get(f"/read/{name}/999999/screen").status_code == 403, \
        "the reader's first screen, fetched ahead for translation, is the same text"
    _free(monkeypatch)
    assert client.get(f"/read/{name}/999999").status_code == 404, \
        "control: past the gate, a missing document is a 404"


@pytest.mark.parametrize("path", ["/api/books", "/api/books/1", "/api/books/1/summary"])
def test_the_catalogue_api_refuses_an_unacknowledged_collection(client, monkeypatch, path):
    _withheld(monkeypatch)
    assert client.get(path).status_code == 403
    _free(monkeypatch)
    assert client.get(path).status_code != 403, "control: the gate is what refused"


def test_the_workers_inline_rescan_asks_the_gate(isolated_config, monkeypatch):
    """The fetch that raised the flag passed the gate when it STARTED; the
    terms can change before it ends."""
    from app import worker
    c = collections_mod.load_all()[0]
    cons = {c.name: db.connect(c)}
    scans = []
    monkeypatch.setattr(discovery, "sync_collection",
                        lambda con, col, **k: scans.append(col.name) or {})
    try:
        _withheld(monkeypatch)
        with worker._acquire_lock:
            worker._acquire_done[c.name] = True
        worker._finish_completed_acquires(cons, [c], {}, pipeline=False)
        assert scans == []

        _free(monkeypatch)
        with worker._acquire_lock:
            worker._acquire_done[c.name] = True
        worker._finish_completed_acquires(cons, [c], {}, pipeline=False)
        assert scans == [c.name], "control: the gate is what held it"
    finally:
        with worker._acquire_lock:
            worker._acquire_done.pop(c.name, None)


def test_the_bundle_importer_and_exporter_are_backstops(isolated_config, tmp_path,
                                                        monkeypatch):
    """Every door refuses in its own words first; these make a door that
    forgets unable to import or export at all."""
    from app.bundles import export as bundle_export, importer as bundle_importer
    c = collections_mod.load_all()[0]
    con = db.connect(c)
    _withheld(monkeypatch)
    with pytest.raises(LicenseAckRequiredError):
        bundle_importer.import_bundle(con, c, tmp_path / "no-bundle")
    with pytest.raises(LicenseAckRequiredError):
        bundle_export.export_bundle(con, c, ["g:1"], tmp_path / "out")
    _free(monkeypatch)
    assert bundle_importer.import_bundle(con, c, tmp_path / "no-bundle")["ok"] is False, \
        "control: past the gate, a missing bundle is an ordinary failure"


def test_search_is_withheld_when_the_gate_itself_fails(monkeypatch):
    """It returned None — searchable — when licensing raised."""
    from app import query_service

    def broken(collection, spec=None):
        raise RuntimeError("terms unreadable")

    monkeypatch.setattr(licensing, "state_for", broken)
    reason = query_service.withheld_reason(object())
    assert reason and "cannot be judged" in reason


def test_a_tool_is_refused_with_a_sentence_not_a_traceback(monkeypatch):
    c = type("C", (), {"name": "pm", "plugin": "pubmed", "license_ack": None})()
    _withheld(monkeypatch)
    monkeypatch.setattr(licensing, "terms_path_for", lambda col, spec=None: None)
    with pytest.raises(SystemExit, match="REFUSED: pm: license_class=yellow"):
        licensing.require_ack_or_exit(c, use="its stored text is embedded")
    _free(monkeypatch)
    licensing.require_ack_or_exit(c, use="its stored text is embedded")   # control
