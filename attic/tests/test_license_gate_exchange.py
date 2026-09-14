"""Boxed from tests/test_license_gate_exchange.py with `pack adopt`
(2026-09-14). Reference only: nothing here is collected or run, and
`cli.cmd_pack_adopt` no longer exists (its body is in attic/pack/adopt.py's
history)."""
import argparse

from app import cli, licensing


def _free(monkeypatch):
    monkeypatch.setattr(licensing, "state_for", lambda c: licensing.NOT_REQUIRED)


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
