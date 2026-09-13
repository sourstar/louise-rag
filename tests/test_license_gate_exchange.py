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


def test_the_cli_bundle_doors_refuse_an_unacknowledged_collection(isolated_config,
                                                                   monkeypatch, capsys):
    """`bundle import` wrote vectors the worker's gated inbox would have
    held; `bundle export` shipped them as a signed, shareable bundle."""
    from app.bundles import export as bundle_export, importer as bundle_importer
    called = []
    monkeypatch.setattr(bundle_importer, "import_bundle",
                        lambda *a, **k: called.append("import") or {"ok": True})
    monkeypatch.setattr(bundle_export, "export_bundle",
                        lambda *a, **k: called.append("export") or {})
    monkeypatch.setattr(cli.signal, "signal", lambda *a: None)
    name = collections_mod.load_all()[0].name
    imp = argparse.Namespace(collection=name, path="/nowhere", no_first_use=False)
    exp = argparse.Namespace(collection=name, uids=["g:1"], limit=1, dest="/nowhere")

    _withheld(monkeypatch)
    assert cli.cmd_bundle_import(imp) == 1
    assert cli.cmd_bundle_export(exp) == 1
    assert called == []
    assert capsys.readouterr().err.count("REFUSED") == 2

    _free(monkeypatch)
    cli.cmd_bundle_import(imp)
    try:
        cli.cmd_bundle_export(exp)
    except Exception:       # noqa: BLE001 — the fake's empty result; the call is what counts
        pass
    assert called == ["import", "export"], "control: the gate is what refused"


def test_pack_adopt_refuses_when_either_side_is_unacknowledged(isolated_config,
                                                                monkeypatch, capsys):
    """Vectors and the text index cross between the two collections."""
    import types

    from app.pack import adopt as pack_adopt
    called = []
    monkeypatch.setattr(pack_adopt, "adopt", lambda *a, **k: called.append(1) or {})
    # two DISTINCT sides, each withheld in turn: with one collection on both
    # sides the test could not tell whether both were checked (verification)
    sides = {n: types.SimpleNamespace(name=n, plugin="gutenberg", license_ack=None)
             for n in ("orig", "twin")}
    monkeypatch.setattr(cli, "_collection_by_name", lambda n: sides[n])
    args = argparse.Namespace(origin="orig", packed="twin", yes=True, fts=False)

    for withheld in ("orig", "twin"):
        monkeypatch.setattr(
            licensing, "state_for",
            lambda c, spec=None, w=withheld:
            licensing.MISSING if c.name == w else licensing.NOT_REQUIRED)
        assert cli.cmd_pack_adopt(args) == 1, f"{withheld} withheld"
        assert called == []
    assert "REFUSED" in capsys.readouterr().err

    _free(monkeypatch)
    try:
        cli.cmd_pack_adopt(args)
    except Exception:       # noqa: BLE001 — the fake's empty result; the call is what counts
        pass
    assert called == [1], "control: the gate is what refused"
