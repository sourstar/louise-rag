"""Invariant 9 — "not imported, not exchanged" — on a path that never
asked the gate.

Found by the verification pass of the `louise health` review, 2026-09-13:
a bundle landed in an unacknowledged collection's manifest. The other two
doors that pass found open — `contribute` and `bundle resweep` — were
retired with app/cli.py (2026-10-08); their successors, `share contribute`
and `hold vectors check`, are tested in tests/hold/test_license_gate_hold.py.
The test has a control that proves the same call is otherwise made, so
the refusal is the gate's and not an ineligible input.
"""
from app import collections as collections_mod, licensing


def _withheld(monkeypatch):
    monkeypatch.setattr(licensing, "state_for", lambda c: licensing.MISSING)
    monkeypatch.setattr(licensing, "license_class_for", lambda c: "yellow")


def _free(monkeypatch):
    monkeypatch.setattr(licensing, "state_for", lambda c: licensing.NOT_REQUIRED)


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
