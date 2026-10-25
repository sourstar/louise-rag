"""The last layer of the invariant-9 work (verification pass, 2026-09-13).

- The Codex decides a Codex collection's class ALONE (02 §2.1). "The
  stricter wins" left a green Codex over a yellow plugin withheld for good.
- A Codex collection is told the one command that records its
  acknowledgement; the Library's button no longer answers "ok" for it.
- Health, search and the worker give one answer when licensing itself
  fails: withheld.
- Every tool's gate is tested where it is called, not only the helper.
"""
import importlib
import types
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import collections as collections_mod, config, db, health, licensing, spaces, \
    web, worker
from app.acquire.spec import LicenseAckRequiredError


def _codex_collection(cls, *, plugin="pubmed"):
    lic = types.SimpleNamespace(
        cls=cls, terms_sha256=None if cls == "green" else "sha256:" + "a" * 64)
    return types.SimpleNamespace(
        name="cx", plugin=plugin, license_ack=None, corpus_dir=None,
        codex=types.SimpleNamespace(license=lic),
        manifest_path=Path("/nowhere/cx/state/manifest.sqlite"))


def _withheld(monkeypatch):
    monkeypatch.setattr(licensing, "state_for", lambda c, spec=None: licensing.MISSING)
    monkeypatch.setattr(licensing, "license_class_for", lambda c, spec=None: "yellow")


def test_the_codex_decides_its_class_alone():
    """02 §2.1: the Codex's class "is authoritative, and the plugin's
    declared class is only the default `hold collection create` writes"."""
    c = _codex_collection("green", plugin="pubmed")        # pubmed's plugin is yellow
    assert licensing.license_class_for(c) == "green"
    assert licensing.state_for(c) == licensing.NOT_REQUIRED
    assert licensing.license_class_for(_codex_collection("yellow", plugin="gutenberg")) \
        == "yellow", "and a Codex that tightens is yellow"


def test_a_codex_collection_is_told_the_command_that_records_its_ack():
    with pytest.raises(LicenseAckRequiredError, match="louise hold collection ack cx"):
        licensing.require_ack(_codex_collection("yellow"), use="its data is used")


def test_health_search_and_the_worker_agree_when_the_gate_itself_fails(isolated_config,
                                                                      monkeypatch):
    from app import query_service

    def broken(collection, spec=None):
        raise PermissionError("terms unreadable")

    monkeypatch.setattr(licensing, "state_for", broken)
    c = collections_mod.load_all()[0]
    assert query_service.withheld_reason(c), "search withholds"
    assert "cannot be judged" in health._withheld(c), "health says so"
    assert worker._license_allows(db.connect(c), c, "indexing") is False, \
        "the worker withholds, rather than raising into its round"


def test_the_librarys_ack_button_does_not_pretend_for_a_codex_collection(isolated_config,
                                                                         monkeypatch):
    monkeypatch.setattr(spaces, "resolve_embed_model_hash", lambda *a: ("abc123", False))
    monkeypatch.setattr(web, "_ping", lambda url: True)
    monkeypatch.setattr(config, "ADMIN_TOKEN", "sekrit")
    monkeypatch.setattr(web, "_collection_or_404", lambda name: _codex_collection("yellow"))
    from app.api import app
    with TestClient(app) as client:
        r = client.post("/admin/library/cx/ack", headers={"X-Admin-Token": "sekrit"})
    assert r.status_code == 400
    assert "louise hold collection ack cx" in r.json()["detail"]


# ---- every tool, at its own call site ------------------------------------

TOOLS = [
    ("vector_compliance", lambda m, name, tmp: m.main([name])),
    ("vector_sweep", lambda m, name, tmp: m.main([name])),
    ("build_space_artifact", lambda m, name, tmp: m.main([name, str(tmp)])),
    ("backfill_pubmed_authors", lambda m, name, tmp: m.main(["--collection", name])),
    ("calibrate_space", lambda m, name, tmp: m.record_live([], name, tmp)),
]


@pytest.mark.parametrize("tool,run", TOOLS, ids=[t for t, _ in TOOLS])
def test_every_tool_refuses_at_its_own_call_site(isolated_config, tmp_path, monkeypatch,
                                                 tool, run):
    module = importlib.import_module(f"tools.{tool}")
    # a gate removed must fail fast here, never hang on an encoder
    _withheld(monkeypatch)
    name = collections_mod.load_all()[0].name
    with pytest.raises(SystemExit) as e:
        run(module, name, tmp_path)
    assert "REFUSED" in str(e.value) and "invariant 9" in str(e.value), str(e.value)


def test_the_schedule_reports_phase_replay_refuses_too(isolated_config, monkeypatch):
    from tools import sched_report
    stub = types.SimpleNamespace(name="pm", plugin="gutenberg",
                                 genesis_archive="medcpt-pubmed", license_ack=None)
    monkeypatch.setattr(sched_report.c_mod, "by_name", lambda name: stub)
    _withheld(monkeypatch)
    with pytest.raises(SystemExit, match="REFUSED"):
        sched_report.phases("pm")
