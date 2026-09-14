"""The worker must not be PID 1 without an init (observed live 2026-09-13).

rsync forks a receiver; when rsync exits first the receiver is re-parented
to PID 1, and a Python PID 1 never reaps it: 25 zombie rsync processes in
three hours, all under the worker. Docker's `init: true` reaps them. The
app definition carries it for both services, and a worker that finds
itself PID 1 anyway says so at start.
"""
import re
from pathlib import Path

from app import worker

APP = Path(__file__).resolve().parents[1] / "deploy" / "truenas-app.yaml"


def _service_block(text: str, name: str) -> str:
    m = re.search(rf"^  {name}:\n(.*?)(?=^  \S|\Z)", text, re.M | re.S)
    assert m, f"no `{name}` service in {APP.name}"
    return m.group(1)


def test_both_services_run_under_an_init():
    text = APP.read_text(encoding="utf-8")
    for name in ("web", "worker"):
        assert re.search(r"^    init: true\b", _service_block(text, name), re.M), name


def test_a_worker_that_is_pid_1_says_so(monkeypatch, capsys):
    monkeypatch.setattr(worker, "run_forever", lambda: None)
    monkeypatch.setattr(worker.os, "getpid", lambda: 1)
    worker.main()
    assert "PID 1 with no init" in capsys.readouterr().err

    monkeypatch.setattr(worker.os, "getpid", lambda: 4242)
    worker.main()
    assert capsys.readouterr().err == "", "and is silent under an init"
