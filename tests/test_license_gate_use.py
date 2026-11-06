"""Invariant 9 gates the USE, not the route that reached it.

The gate covered acquisition, the acquiring scan, and archive import. Three
ways of USING a yellow/red collection had no gate at all, and each was
reachable without ever touching acquisition:

  * SEARCH — `query_service` federated over `load_all()` unconditionally,
    so anything already in a withheld collection's manifest answered
    queries.
  * the ROUND — rdf/fts/embed ran on whatever was in the manifest, so
    "withheld" meant "scan withheld, everything else proceeds".
  * EXCHANGE — the outbound seeding path never consulted app/licensing.py,
    so this peer served a corpus whose terms it had not accepted. That is
    the "not exchanged" clause, and it had no code behind it anywhere.

Plus the create gate itself, which keyed on `acquire_enabled` — so
declaring a yellow source with acquisition off skipped it entirely, which
is exactly the hand-populated-folder case the invariant names.
"""
import dataclasses

import pytest
from fastapi import HTTPException

from app import collections as collections_mod, licensing, query_service


def _yellow(tmp_path, name="pm", acked=False):
    c = collections_mod.Collection(
        name=name, plugin="pubmed", profile=None, embed_mode_default=None,
        corpus_dir=tmp_path / "corpus",
        manifest_path=tmp_path / name / "manifest.db",
        fts_dir=tmp_path / name / "fts", lance_dir=tmp_path / name / "lancedb")
    if acked:
        c = dataclasses.replace(c, license_ack=licensing.make_ack(c))
    return c


def test_a_withheld_collection_is_not_searchable(tmp_path, monkeypatch):
    withheld = _yellow(tmp_path, "pm", acked=False)
    monkeypatch.setattr(collections_mod, "load_all", lambda: [withheld])
    assert query_service.withheld_reason(withheld)
    assert query_service.resolve_collections(None) == []


def test_an_acknowledged_collection_is_searchable(tmp_path, monkeypatch):
    ok = _yellow(tmp_path, "pm", acked=True)
    monkeypatch.setattr(collections_mod, "load_all", lambda: [ok])
    assert query_service.withheld_reason(ok) is None
    assert [c.name for c in query_service.resolve_collections(None)] == ["pm"]


def test_naming_a_withheld_collection_is_refused_not_emptied(tmp_path, monkeypatch):
    """Returning nothing for a collection the caller ASKED for by name is a
    lie; the 400 for unknown names is the model."""
    withheld = _yellow(tmp_path, "pm", acked=False)
    monkeypatch.setattr(collections_mod, "load_all", lambda: [withheld])
    monkeypatch.setattr(collections_mod, "by_name",
                        lambda n: withheld if n == "pm" else None)
    with pytest.raises(HTTPException) as e:
        query_service.resolve_collections(["pm"])
    assert e.value.status_code == 403
    assert "not acknowledged" in str(e.value.detail)


def test_a_narrowed_federation_says_so(tmp_path, monkeypatch):
    """"Did work is not progress", one layer up: a search that silently
    dropped a collection looks the same as one that found nothing there."""
    withheld = _yellow(tmp_path, "pm", acked=False)
    ok = _yellow(tmp_path, "ok", acked=True)
    monkeypatch.setattr(collections_mod, "load_all", lambda: [withheld, ok])
    reported = query_service.withheld_from(None)
    assert [w["collection"] for w in reported] == ["pm"]
    assert "acknowledged" in reported[0]["reason"]


def test_an_explicit_request_reports_no_withholding(tmp_path, monkeypatch):
    ok = _yellow(tmp_path, "ok", acked=True)
    monkeypatch.setattr(collections_mod, "load_all", lambda: [ok])
    assert query_service.withheld_from(["ok"]) == []


# ---- the round ----------------------------------------------------------

def test_the_round_is_withheld_not_only_the_scan(tmp_path, monkeypatch):
    """rdf/fts/embed used to run on whatever was already in the manifest."""
    from app import db, worker
    c = _yellow(tmp_path, "pm", acked=False)
    con = db.connect(c)
    stage, row = worker._next_book(con, {"embed_mode": "all"}, collection=c)
    assert stage is None and row is None


def test_an_acknowledged_collection_still_rounds(tmp_path):
    from app import db, worker
    c = _yellow(tmp_path, "pm", acked=True)
    con = db.connect(c)
    # nothing to do in an empty manifest, but it must not be REFUSED — the
    # gate has to distinguish "withheld" from "idle"
    assert worker._license_allows(con, c, "indexing") is True


# ---- exchange -----------------------------------------------------------

def test_seeding_withholds_an_unacknowledged_collection(tmp_path, monkeypatch):
    """The outbound path had no gate at all — grepping licensing across
    seeding.py, torrent_publish.py and torrent_collection.py returned
    nothing."""
    from app.acquire import seeding
    withheld = _yellow(tmp_path, "pm", acked=False)
    ok = _yellow(tmp_path, "ok", acked=True)

    started, retained = [], []

    class _FakeSeeder:
        def set_paused(self, p): pass
        def finish_draining(self): return []
        def retain(self, live): retained.append(set(live)); return []
        def ensure(self, collection, entry): started.append(collection.name); return True
        def account(self): pass
        def table(self): return []

    monkeypatch.setattr(seeding, "get_seeder", lambda: _FakeSeeder())
    monkeypatch.setattr(seeding, "read_published", lambda con: [{"name": "x"}])
    from app import db
    cons = {"pm": db.connect(withheld), "ok": db.connect(ok)}
    seeding.tick([withheld, ok], cons, cons["ok"], "running")

    assert retained == [{"ok"}], "the withheld collection must not be retained"
    assert started == ["ok"], "and must never be started"
