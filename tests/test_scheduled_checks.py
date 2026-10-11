"""tests/test_scheduled_checks.py — the plan in the background (03 §3.2;
app/scheduled_checks.py): due when a snapshot has no scheduled check or one
cadence has passed since; run as a child process, its report kept; a stop
ends the child and records nothing; a timed-out run is recorded as such;
nothing runs in a mode that does not parse; one collection per turn."""
import inspect
import sys
import time
from types import SimpleNamespace

from app import scheduled_checks as sc

NOW = 1_800_000_000.0


def test_due_by_snapshot_and_by_cadence(tmp_path):
    codex = SimpleNamespace(update=SimpleNamespace(cadence="weekly"))
    assert not sc.due(tmp_path, "c", codex, None, now=NOW), "no snapshot: nothing to check"
    assert sc.due(tmp_path, "c", codex, "2026-W40", now=NOW), "never checked"
    sc.record(tmp_path, "c", "2026-W40", {"exit": 0, "outcome": "pass", "secs": 1.0}, now=NOW)
    assert not sc.due(tmp_path, "c", codex, "2026-W40", now=NOW + 86400), "checked this week"
    assert sc.due(tmp_path, "c", codex, "2026-W41", now=NOW + 86400), "a new snapshot"
    assert sc.due(tmp_path, "c", codex, "2026-W40", now=NOW + 8 * 86400), "a cadence later"
    monthly = SimpleNamespace(update=SimpleNamespace(cadence="monthly"))
    assert not sc.due(tmp_path, "c", monthly, "2026-W40", now=NOW + 8 * 86400)
    assert sc.last(tmp_path, "c")["outcome"] == "pass"


def test_the_origin_goes_with_the_check_when_there_is_one(tmp_path):
    corpus = tmp_path / "origin"
    corpus.mkdir()
    argv = sc.argv_for(SimpleNamespace(name="gutenberg", corpus_dir=corpus, source="origin"))
    assert argv[1:6] == ["-m", "app.louise", "hold", "check", "gutenberg"] and argv[-2:] == ["--origin", str(corpus)]
    fetched = sc.argv_for(SimpleNamespace(name="g", corpus_dir=corpus, source="shard"))
    assert "--origin" not in fetched, "a fetched copy has no origin here: its check says so"


def _py(code: str) -> list[str]:
    return [sys.executable, "-c", code]


def test_a_run_is_a_child_its_report_kept_and_a_stop_ends_it():
    c = SimpleNamespace(name="c")
    got = sc.run_one(c, stop=lambda: False, argv=_py('print(\'{"outcomes": []}\')'))
    assert got["exit"] == 0 and got["outcome"] == "pass" and got["report"] == {"outcomes": []}
    got = sc.run_one(c, stop=lambda: False, argv=_py("import sys; print('boom', file=sys.stderr); sys.exit(1)"))
    assert got["outcome"] == "fail" and got["stderr_tail"] == ["boom"] and got["report"] is None
    t0 = time.time()
    flag = {"stop": False}
    import threading
    threading.Timer(1.5, lambda: flag.update(stop=True)).start()
    got = sc.run_one(c, stop=lambda: flag["stop"], argv=_py("import time; time.sleep(60)"))
    assert got["outcome"] == "stopped" and time.time() - t0 < 30, "ended at the stop, not at the end"
    got = sc.run_one(c, stop=lambda: False, timeout_s=1.0, argv=_py("import time; time.sleep(60)"))
    assert got["outcome"] == "timed out"


def test_a_turn_runs_one_due_collection_only_in_a_mode_that_parses(isolated_config, monkeypatch):
    from app import collections as collections_mod, node_state
    from app.share import seeder
    monkeypatch.setattr(seeder, "current_snapshot", lambda d: SimpleNamespace(snapshot_id="2026-09"))
    ran, lines = [], []

    def fake(c, stop):
        ran.append(c.name)
        return {"exit": 0, "outcome": "pass", "secs": 2.0, "report": None}
    monkeypatch.setattr(node_state, "peek_worker_state", lambda: "paused")
    assert sc.turn(stop=lambda: False, log=lines.append, now=NOW, run=fake) is None and ran == []
    monkeypatch.setattr(node_state, "peek_worker_state", lambda: "running")
    assert sc.turn(stop=lambda: False, log=lines.append, now=NOW, run=fake) == "gutenberg"
    root = collections_mod.louise_root() / "collections"
    assert sc.last(root, "gutenberg")["snapshot_id"] == "2026-09" and ran == ["gutenberg"]
    assert sc.turn(stop=lambda: False, log=lines.append, now=NOW + 60, run=fake) is None, "done until due again"
    assert any("scheduled check pass" in m for m in lines)
    # a stopped run is not recorded: it runs again
    sc.state_path(root, "gutenberg").unlink()
    assert sc.turn(stop=lambda: False, log=lines.append, now=NOW,
                   run=lambda c, stop: {"outcome": "stopped", "secs": 1.0}) == "gutenberg"
    assert sc.last(root, "gutenberg") is None


def test_the_worker_starts_the_runner_and_show_reports_the_last():
    from app import louise, worker
    assert "scheduled_checks.loop" in inspect.getsource(worker)
    assert "scheduled_check" in inspect.getsource(louise._show_shard)
