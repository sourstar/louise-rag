"""The boot report writes NOTHING — the System page's, and `louise health`'s.

`report_boot_status` promised "READ-ONLY: a status page must never mutate
space state", and the pin itself honoured that. Two writes underneath did
not: filling in a pinned width that was still NULL, and an event for an
embed hash that could not be confirmed. The verification pass of the
`louise health` review (2026-09-13) caught both, once health started using
the report: dim NULL -> 768 after one check, and a new events row each run.
And one collection whose manifest could not be opened made the whole report
raise, so a real refusal beside it vanished.
"""
from app import collections as collections_mod, db, spaces


def _hash(monkeypatch, value, weak=False):
    monkeypatch.setattr(spaces, "resolve_embed_model_hash", lambda: (value, weak))


def _events(con):
    return con.execute("SELECT COUNT(*) FROM events").fetchone()[0]


def test_the_report_never_fills_in_the_pinned_width(isolated_config, monkeypatch):
    _hash(monkeypatch, "abc123")
    c = collections_mod.default_collection()
    spaces.validate_boot([c])                    # pinned with no vectors yet: dim NULL
    con = db.connect(c)
    assert con.execute("SELECT dim FROM space").fetchone()[0] is None

    monkeypatch.setattr(spaces, "known_lance_dim", lambda d: 768)
    events = _events(con)
    spaces.report_boot_status([c])
    assert con.execute("SELECT dim FROM space").fetchone()[0] is None, \
        "the read-only report wrote the width"
    assert _events(con) == events

    spaces.validate_boot([c])                    # the worker's boot does write it
    assert con.execute("SELECT dim FROM space").fetchone()[0] == 768


def test_the_report_never_logs_an_unconfirmed_hash(isolated_config, monkeypatch):
    _hash(monkeypatch, "abc123")
    c = collections_mod.default_collection()
    spaces.validate_boot([c])
    con = db.connect(c)
    _hash(monkeypatch, "zzz", weak=True)         # changed, and not confirmable
    events = _events(con)
    spaces.report_boot_status([c])
    assert _events(con) == events


def test_one_collection_that_cannot_be_opened_does_not_hide_the_others(
        isolated_config, tmp_path, monkeypatch):
    _hash(monkeypatch, "model-a")
    good = collections_mod.default_collection()
    spaces.validate_boot([good])
    _hash(monkeypatch, "model-b")                # a confirmed change: refused
    broken = collections_mod.Collection(
        name="broken", plugin="gutenberg", profile=None, embed_mode_default=None,
        corpus_dir=tmp_path / "c", manifest_path=tmp_path,     # a directory
        fts_dir=tmp_path / "f", lance_dir=tmp_path / "l")

    report = {r["collection"]: r for r in spaces.report_boot_status([broken, good])}

    assert report["broken"]["ok"] is False
    assert "cannot be checked" in report["broken"]["message"]
    assert report[good.name]["ok"] is False, "the real refusal is still reported"
