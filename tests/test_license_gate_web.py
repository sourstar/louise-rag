"""Invariant 9 at the web's and the API's doors.

Found by the verification pass of the `louise health` review, 2026-09-13:
the Library's Rescan and bundle-export buttons, POST /api/discover and the
summary endpoint all ran for a collection whose terms were never
acknowledged — discovering its corpus, shipping its vectors, sending its
text to the LLM. The worker, the CLI and `hold` asked; these did not. Each
answers 403 now, naming what is missing; the tests carry controls where
the call can be made cheaply.
"""
import pytest
from fastapi.testclient import TestClient

from app import collections as collections_mod, config, discovery, licensing, spaces, web
from app.api import app


@pytest.fixture
def client(isolated_config, monkeypatch):
    monkeypatch.setattr(spaces, "resolve_embed_model_hash", lambda *a: ("abc123", False))
    monkeypatch.setattr(web, "_ping", lambda url: True)
    monkeypatch.setattr(config, "ADMIN_TOKEN", "sekrit")
    with TestClient(app) as c:
        c.headers["X-Admin-Token"] = "sekrit"
        yield c


def _withheld(monkeypatch):
    monkeypatch.setattr(licensing, "state_for", lambda c: licensing.MISSING)
    monkeypatch.setattr(licensing, "license_class_for", lambda c: "yellow")


def _free(monkeypatch):
    monkeypatch.setattr(licensing, "state_for", lambda c: licensing.NOT_REQUIRED)


def test_the_rescan_doors_refuse_an_unacknowledged_collection(client, monkeypatch):
    scans = []
    monkeypatch.setattr(discovery, "sync_collection",
                        lambda con, c, **k: scans.append(c.name) or {"seen": 0})
    name = collections_mod.load_all()[0].name

    _withheld(monkeypatch)
    library = client.post(f"/admin/library/{name}/discover")
    api = client.post("/api/discover")
    assert library.status_code == 403 and api.status_code == 403, (library.text, api.text)
    assert "acknowledgement" in library.json()["detail"]
    assert scans == []

    _free(monkeypatch)
    assert client.post("/api/discover").status_code == 200
    assert scans, "control: the gate is what refused"


def test_the_librarys_bundle_export_refuses_an_unacknowledged_collection(client,
                                                                        monkeypatch):
    from app.bundles import export as bundle_export
    called = []
    monkeypatch.setattr(bundle_export, "export_bundle",
                        lambda *a, **k: called.append(1) or {})
    name = collections_mod.load_all()[0].name
    _withheld(monkeypatch)
    r = client.post(f"/admin/library/{name}/bundle/export",
                    json={"uids": ["g:1"], "limit": 1})
    assert r.status_code == 403 and called == []


def test_no_summary_is_made_of_an_unacknowledged_collection(client, monkeypatch):
    """A summary sends the book's text to the LLM: use of the corpus."""
    from app import summarize
    ran = []
    monkeypatch.setattr(summarize, "get_cached", lambda con, book_id: None)
    monkeypatch.setattr(summarize, "summarize_book",
                        lambda con, book_id, force=False: ran.append(book_id))

    _withheld(monkeypatch)
    r = client.post("/api/books/1/summary")
    assert r.status_code == 403 and ran == []

    _free(monkeypatch)
    assert client.post("/api/books/1/summary").json()["status"] in ("started", "running")
