"""Invariant 9 — "not imported, not exchanged" — on three paths that never
asked the gate.

Found by the verification pass of the `louise health` review, 2026-09-13:
a bundle landed in an unacknowledged collection's manifest; `louise
contribute` embedded its corpus and uploaded the vectors; `bundle resweep`
re-embedded its text. Each test has a control that proves the same call is
otherwise made, so the refusal is the gate's and not an ineligible input.
"""
import argparse

from app import cli, collections as collections_mod, licensing


def _withheld(monkeypatch):
    monkeypatch.setattr(licensing, "state_for", lambda c: licensing.MISSING)
    monkeypatch.setattr(licensing, "license_class_for", lambda c: "yellow")


def _free(monkeypatch):
    monkeypatch.setattr(licensing, "state_for", lambda c: licensing.NOT_REQUIRED)


def test_contribute_refuses_an_unacknowledged_collection(isolated_config, monkeypatch,
                                                         capsys):
    from app import contribute
    called = []
    monkeypatch.setattr(contribute, "run_round",
                        lambda *a, **k: called.append("round") or {"ok": True})
    monkeypatch.setattr(contribute, "report_demand",
                        lambda *a, **k: called.append("demand") or {})
    monkeypatch.setattr(cli.signal, "signal", lambda *a: None)
    name = collections_mod.load_all()[0].name

    def run(demand):
        return cli.cmd_contribute(argparse.Namespace(
            collection=name, coordinator="http://coordinator", count=1,
            report_demand=demand))

    _withheld(monkeypatch)
    assert run(False) == 1 and run(True) == 1
    assert called == [], "nothing embedded, nothing reported"
    assert "REFUSED" in capsys.readouterr().err

    _free(monkeypatch)
    run(False)
    run(True)
    assert called == ["round", "demand"], "control: the gate is what refused"


def test_resweep_refuses_an_unacknowledged_collection(isolated_config, monkeypatch, capsys):
    from app.bundles import verify_on_use
    called = []
    monkeypatch.setattr(verify_on_use, "resweep", lambda *a, **k: called.append(1) or
                        {"swept": 0, "promoted": 0, "failed": []})
    name = collections_mod.load_all()[0].name
    args = argparse.Namespace(collection=name)

    _withheld(monkeypatch)
    assert cli.cmd_bundle_resweep(args) == 1
    assert called == []
    assert "REFUSED" in capsys.readouterr().err

    _free(monkeypatch)
    assert cli.cmd_bundle_resweep(args) == 0
    assert called == [1], "control: the gate is what refused"


def test_a_bundle_in_the_inbox_waits_for_the_acknowledgement(isolated_config, monkeypatch):
    """The worker drains a collection's bundle inbox before anything else in
    its step — and asked no gate. Now the bundle stays in the inbox, not
    rejected, and imports once the terms are acknowledged."""
    from app import db, worker
    from app.bundles import importer as bundle_importer
    c = collections_mod.default_collection()
    con = db.connect(c)
    (bundle_importer.inbox_dir(c) / "b1").mkdir(parents=True)
    imported = []
    monkeypatch.setattr(worker.bundle_importer, "import_bundle",
                        lambda con, collection, entry, stop=None:
                        imported.append(entry.name) or {"ok": True, "chunks": 1, "uids": 1})

    _withheld(monkeypatch)
    assert worker._drain_bundle_inbox(con, c) == 0
    assert imported == []
    assert (bundle_importer.inbox_dir(c) / "b1").is_dir(), "left in the inbox, not rejected"

    _free(monkeypatch)
    assert worker._drain_bundle_inbox(con, c) == 1
    assert imported == ["b1"], "control: the gate is what held it"
