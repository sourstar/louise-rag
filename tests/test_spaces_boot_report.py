"""System page's boot-validation report: same check validate_boot() runs at
process startup, but caught and returned as pass/fail rows instead of
exiting the process (CLAUDE.md invariant 1 stays enforced at real startup —
this is purely a read-only status view for the UI).
"""
from app import collections as collections_mod, spaces


def _patch_hash(monkeypatch, value="abc123", weak=False):
    monkeypatch.setattr(spaces, "resolve_embed_model_hash", lambda *a: (value, weak))


def test_report_is_read_only_and_never_pins(isolated_config, monkeypatch):
    """Observed live: an operator legitimately reset a pin (zero vectors) to
    switch recipes, opened the UI, and the report re-pinned the half-edited
    recipe — the next boot refused. A status view must never mutate."""
    from app import db
    _patch_hash(monkeypatch)
    collection = collections_mod.default_collection()

    report = spaces.report_boot_status([collection])
    assert report == [{"collection": collection.name, "ok": True,
                        "message": "space unpinned — pins at next worker boot"}]
    con = db.connect(collection)
    assert con.execute("SELECT COUNT(*) FROM space").fetchone()[0] == 0, \
        "the read-only report must not have created a pin"

    spaces.validate_boot([collection])   # the worker's startup path DOES pin
    assert con.execute("SELECT COUNT(*) FROM space").fetchone()[0] == 1
    report = spaces.report_boot_status([collection])
    assert report == [{"collection": collection.name, "ok": True,
                        "message": "space pinned & consistent"}]


def test_report_boot_status_reports_failure_without_raising(isolated_config, monkeypatch):
    _patch_hash(monkeypatch, value="model-a")
    collection = collections_mod.default_collection()
    spaces.validate_boot([collection])   # pin

    _patch_hash(monkeypatch, value="model-b")   # confirmed change
    report = spaces.report_boot_status([collection])   # must not raise
    assert len(report) == 1
    assert report[0]["collection"] == collection.name
    assert report[0]["ok"] is False
    assert "model-a" in report[0]["message"] or "model-b" in report[0]["message"]

    # the real startup check still enforces the invariant — the report is a
    # non-raising view, not a weakening of validate_boot() itself
    import pytest
    with pytest.raises(spaces.BootValidationError):
        spaces.validate_boot([collection])
