"""The GPU governor (app/gpu_governor.py, docs/notes/scheduling.md §12).

Measured live (2026-09-20): alone on the card, a chat call took 87 s at a
mean of 990 MHz, and 64 s with the card's min_freq held at its maximum. The
governor holds it while the node's interactive claim is live. Absent, the
System tab says so; nothing else breaks.
"""
import threading
import time

import pytest

from app import config, dispatch, gpu, gpu_governor, node_state


def _card(tmp_path, *, min_mhz=400, max_mhz=2800, floor=400):
    d = tmp_path / "freq0"
    d.mkdir()
    (d / "min_freq").write_text(f"{min_mhz}\n")
    (d / "rp0_freq").write_text(f"{max_mhz}\n")
    (d / "rpn_freq").write_text(f"{floor}\n")
    return d


def _min(d):
    return int((d / "min_freq").read_text())


@pytest.fixture
def store(isolated_config):
    node_state.connect().close()       # the worker makes it at boot
    dispatch.reset()
    return node_state.path()


def test_the_clock_is_held_for_the_claim_and_let_go_after(tmp_path, store):
    d = _card(tmp_path)
    g = gpu_governor.Governor(d)
    assert g.step(False) is False and _min(d) == 400
    assert g.step(True) is True and _min(d) == 2800
    assert g.step(True) is False, "already held: nothing to write"
    assert g.step(False) is True and _min(d) == 400


def test_a_governor_that_died_holding_the_clock_rests_at_the_floor(tmp_path, store):
    """Killed while pinning, it leaves min_freq at the maximum; the next one
    must not take that for the rest value, or the card would stay pinned
    for good."""
    d = _card(tmp_path, min_mhz=2800)
    g = gpu_governor.Governor(d)
    assert g.rest == 400
    g.step(True)
    g.step(False)
    assert _min(d) == 400


def test_a_clock_it_cannot_write_is_reported_not_fatal(tmp_path, store, monkeypatch):
    d = _card(tmp_path)
    g = gpu_governor.Governor(d)

    def refuse(mhz):
        raise PermissionError(13, "Permission denied")
    monkeypatch.setattr(g, "_set_min", refuse)
    assert g.step(True) is False
    assert g.status()["error"].startswith("cannot write min_freq")
    assert g.pinned is False


def test_the_loop_follows_the_claim_and_restores_on_stop(tmp_path, store, monkeypatch):
    monkeypatch.setattr(config, "GOVERNOR_POLL_S", 0.02)
    monkeypatch.setattr(config, "GATE_POLL_S", 0.0)
    d = _card(tmp_path)
    stop = threading.Event()
    t = threading.Thread(target=gpu_governor.run, args=(stop, d), daemon=True)
    t.start()

    def until(pred, what):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if pred():
                return
            time.sleep(0.02)
        raise AssertionError(what)
    until(lambda: gpu.governor()["state"] == "running", "the governor never said it runs")
    with dispatch.interactive(dispatch.Level.CHAT):
        until(lambda: _min(d) == 2800, "the clock was not held while a chat ran")
        until(lambda: gpu.governor().get("pinned") is True, "the tab did not see it held")
    until(lambda: _min(d) == 400, "the clock was not let go after the chat")
    with dispatch.interactive(dispatch.Level.QUERY):
        until(lambda: _min(d) == 2800, "held again")
        stop.set()
        t.join(5)
    assert _min(d) == 400, "a stopping governor gives the clock back"
    assert gpu.governor()["state"] == "absent", "a stopped governor is not running"


def test_missing_frequency_files_are_an_error_the_tab_shows(tmp_path, store, monkeypatch):
    monkeypatch.setattr(config, "GOVERNOR_HEARTBEAT_S", 0.05)
    stop = threading.Event()
    t = threading.Thread(target=gpu_governor.run, args=(stop, tmp_path / "nowhere"),
                         daemon=True)
    t.start()
    deadline = time.monotonic() + 5
    while gpu.governor()["state"] != "error" and time.monotonic() < deadline:
        time.sleep(0.02)
    state = gpu.governor()
    stop.set()
    t.join(5)
    assert state["state"] == "error" and "no frequency files" in state["error"], state


def test_no_heartbeat_reads_as_absent(tmp_path, store, monkeypatch):
    assert gpu.governor() == {"state": "absent"}
    gpu_governor._publish({"pinned": False, "rest_mhz": 400, "max_mhz": 2800, "error": None})
    assert gpu.governor()["state"] == "running"
    monkeypatch.setattr(config, "GOVERNOR_STALE_S", -1.0)
    assert gpu.governor() == {"state": "absent"}, "a stale heartbeat is not a governor"


def test_the_system_tab_says_when_the_governor_is_absent(tmp_path, isolated_config):
    """The operator (2026-09-20): if the governor is not available, say so in
    the System tab — it costs only performance."""
    from app import web
    body = web.TEMPLATES.get_template("_fragments/gpu.html").render(
        state="unset", url="", governor={"state": "absent"})
    assert "GPU governor not running" in body and "87 s against 64 s" in body
    body = web.TEMPLATES.get_template("_fragments/gpu.html").render(
        state="unset", url="", governor={"state": "running", "pinned": True,
                                         "rest_mhz": 400, "max_mhz": 2800})
    assert "clock held at 2800 MHz" in body
    body = web.TEMPLATES.get_template("_fragments/gpu.html").render(
        state="unset", url="", governor={"state": "error", "error": "cannot write min_freq: x"})
    assert "cannot set the clock" in body and "cannot write min_freq" in body


# ---- after the cut-over the clock is RAGE's -------------------------------------------

def test_with_rage_the_system_tab_asks_rage_for_the_clock(monkeypatch):
    """Found the evening RAGE took the models over (2026-09-19): the governor's
    own app was stopped, its heartbeat went stale, and this panel read "GPU
    governor not running" while RAGE was holding the clock."""
    from app import config, gpu
    monkeypatch.setattr(config, "RAGE_URL", "http://rage:30437")
    asked = []

    def answer(url):
        asked.append(url)
        return {"clock": {"running": True, "waiting": 0, "pinned": True,
                          "rest_mhz": 400, "max_mhz": 2800, "error": None}}

    monkeypatch.setattr(gpu, "_get_json", answer)
    assert gpu.governor() == {"state": "running", "pinned": True,
                              "rest_mhz": 400, "max_mhz": 2800}
    assert asked == ["http://rage:30437/louise/gpu"]


def test_a_rage_that_cannot_set_the_clock_or_holds_none_says_so(monkeypatch):
    from app import config, gpu
    monkeypatch.setattr(config, "RAGE_URL", "http://rage:30437/")
    monkeypatch.setattr(gpu, "_get_json", lambda url: {"clock": {
        "running": True, "pinned": False, "error": "PermissionError: /gpufreq/min_freq"}})
    assert gpu.governor() == {"state": "error", "error": "PermissionError: /gpufreq/min_freq"}

    monkeypatch.setattr(gpu, "_get_json", lambda url: {"clock": {"running": False}})
    assert gpu.governor() == {"state": "absent"}, "RAGE_FREQ_DIR unset: no clock is held"


def test_a_rage_that_does_not_answer_never_breaks_the_page(monkeypatch):
    from app import config, gpu

    def gone(url):
        raise OSError("connection refused")

    monkeypatch.setattr(config, "RAGE_URL", "http://rage:30437")
    monkeypatch.setattr(gpu, "_get_json", gone)
    assert gpu.governor() == {"state": "absent"}
