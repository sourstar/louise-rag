"""`corpus add` says when it was stopped, and a signal exits 130.

A stop in the listing — gutenberg's rsync listing is ~12 minutes, the
likeliest moment for a Ctrl-C — now ends the pass before any transfer, and
the result's counts are all zero. Printed bare, "total=0" read as an origin
with no files (review, 2026-09-13). And docs/07 §7.7 / invariant 2: a
signal finishes the unit and exits 130; this command exited 0.
"""
import argparse

from app import cli, collections as collections_mod

_ZERO = {"fetched": 0, "skipped": 0, "failed": 0, "aborted": 0, "adopted": 0, "total": 0}


def _run(monkeypatch, result: dict, signalled: bool) -> int:
    monkeypatch.setattr(cli.signal, "signal", lambda *a: None)

    def fake_run(con, collection, spec, profile, stop=None):
        if signalled:
            cli._stop.set()
        return result

    monkeypatch.setattr(cli.engine, "run_acquisition", fake_run)
    args = argparse.Namespace(collection=collections_mod.load_all()[0].name,
                              ack_terms=None, profile="demo", dry_run=False)
    try:
        return cli.cmd_add(args)
    finally:
        cli._stop.clear()


def test_a_stop_in_the_listing_says_so_and_exits_130(isolated_config, monkeypatch, capsys):
    rc = _run(monkeypatch, {**_ZERO, "stopped": "rsync --list-only aborted by stop event"},
              signalled=True)
    out = capsys.readouterr().out
    assert rc == 130
    assert "interrupted before any transfer" in out and "nothing was recorded" in out, out


def test_a_stop_mid_transfer_still_exits_130(isolated_config, monkeypatch, capsys):
    rc = _run(monkeypatch, {**_ZERO, "fetched": 3, "aborted": 2, "total": 5}, signalled=True)
    assert rc == 130
    assert "partial downloads were checkpointed" in capsys.readouterr().out


def test_an_unstopped_run_keeps_its_exit_codes(isolated_config, monkeypatch):
    assert _run(monkeypatch, {**_ZERO, "fetched": 1, "total": 1}, signalled=False) == 0
    assert _run(monkeypatch, {**_ZERO, "failed": 1, "total": 1}, signalled=False) == 1
