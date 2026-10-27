"""The profile's GPU level, applied by the GPU governor (app/gpu_level.py,
app/gpu_governor.py; docs/notes/node-profiles-design.md "Who applies what",
operator 2026-10-27). Fake sysfs trees under tmp_path, as the governor's
own tests use: the power limit (hwmon `power1_max`, microwatts), else the
clock ceiling (`max_freq`), never under the query hold, restored on stop.
"""
from __future__ import annotations

import errno
import os
import threading
import time
from datetime import datetime
from pathlib import Path

import pytest

from app import config, dispatch, gpu, gpu_governor, gpu_level, node_state
from app.gpu_clock import Governor
from app.node_profile import GpuLevel

EXAMPLE = Path(__file__).resolve().parents[1] / "docs" / "notes" / "node-profiles.example.toml"
W = 1_000_000
FULL = GpuLevel("full")
QUIET = GpuLevel("quiet", power_limit="50%", max_clock="50%", fan_max_rpm=1200)


def _card(tmp_path: Path, *, min_mhz=400, top=2800, floor=400, max_freq=2800) -> Path:
    d = tmp_path / "freq0"
    d.mkdir(parents=True)
    for name, v in (("min_freq", min_mhz), ("rp0_freq", top), ("rpn_freq", floor),
                    ("max_freq", max_freq)):
        (d / name).write_text(f"{v}\n")
    return d


def _hwmon(tmp_path: Path, *, power=230 * W, rated=230 * W, writable=True,
           fan_max: int | None = None, pwm=False) -> Path:
    """The card's `device/hwmon/` with its hwmonN inside, as bound at /gpuhwmon."""
    h = tmp_path / "hwmon" / "hwmon3"
    h.mkdir(parents=True)
    (h / "name").write_text("xe\n")
    (h / "fan1_input").write_text("900\n")
    (h / "fan1_input").chmod(0o444)
    if power is not None:
        (h / "power1_max").write_text(f"{power}\n")
        if not writable:
            (h / "power1_max").chmod(0o444)
    if rated is not None:
        (h / "power1_rated_max").write_text(f"{rated}\n")
        (h / "power1_rated_max").chmod(0o444)
    if fan_max is not None:
        (h / "fan1_max").write_text(f"{fan_max}\n")
    if pwm:
        (h / "pwm1").write_text("128\n")
    return h


def _n(p: Path) -> int:
    return int(p.read_text())


class _Log(list):
    def __call__(self, m: str) -> None:
        self.append(m)


# ------------------------------------------------------------ conversions
def test_percent_and_watts_convert_to_microwatts():
    own, rated = 200 * W, 230 * W
    assert gpu_level.power_uw("max", own, rated) == own, "max is the card's own"
    assert gpu_level.power_uw("50%", own, rated) == 115 * W, "N% of power1_rated_max"
    assert gpu_level.power_uw("100%", own, rated) == rated
    assert gpu_level.power_uw("120W", own, rated) == 120 * W, "hwmon counts microwatts"
    assert gpu_level.power_uw("87.5 W", own, rated) == 87_500_000
    with pytest.raises(ValueError):
        gpu_level.power_uw("1400MHz", own, rated)


def test_clock_converts_to_mhz_within_the_cards_range():
    assert gpu_level.clock_mhz("max", 2800, 2800, 400) == 2800
    assert gpu_level.clock_mhz("50%", 2800, 2800, 400) == 1400, "N% of rp0_freq"
    assert gpu_level.clock_mhz("1800MHz", 2800, 2800, 400) == 1800
    assert gpu_level.clock_mhz("5%", 2800, 2800, 400) == 400, "never below the floor"
    assert gpu_level.clock_mhz("9000MHz", 2800, 2800, 400) == 2800, "never above the top"
    with pytest.raises(ValueError):
        gpu_level.clock_mhz("120W", 2800, 2800, 400)


def test_percent_without_a_rated_limit_is_of_the_value_read_at_start(tmp_path):
    h = _hwmon(tmp_path, power=200 * W, rated=None)
    a = gpu_level.LevelApplier(_card(tmp_path), h, log=_Log())
    a.reconcile(QUIET, Governor(tmp_path / "freq0"))
    assert _n(h / "power1_max") == 100 * W
    a.reconcile(GpuLevel("w", power_limit="120W"), Governor(tmp_path / "freq0"))
    assert _n(h / "power1_max") == 120 * W


# --------------------------------------------------- the power limit first
def test_the_power_limit_is_applied_and_max_freq_left_alone(tmp_path):
    d, h = _card(tmp_path), _hwmon(tmp_path)
    log = _Log()
    a, g = gpu_level.LevelApplier(d, h, log=log), Governor(d)
    assert a.reconcile(QUIET, g, "quiet") is True
    assert _n(h / "power1_max") == 115 * W and _n(d / "max_freq") == 2800
    s = a.status()
    assert (s["name"], s["via"], s["power_w"]) == ("quiet", "power1_max", 115.0)
    assert "never its bytes" in log[-1] and "power1_max 115 W" in log[-1]
    assert a.reconcile(QUIET, g, "quiet") is False, "applied: nothing to write"
    a.reconcile(FULL, g, "day")
    assert _n(h / "power1_max") == 230 * W, "max restores the value read at start"


def test_no_profile_writes_nothing(tmp_path):
    d, h = _card(tmp_path), _hwmon(tmp_path, power=180 * W)
    a = gpu_level.LevelApplier(d, h, log=_Log())
    assert a.reconcile(None, Governor(d)) is False
    assert _n(h / "power1_max") == 180 * W and _n(d / "max_freq") == 2800


# ------------------------------------------------------- the clock fallback
@pytest.mark.parametrize("hw", ["absent", "read-only", "no-hwmon"])
def test_without_a_writable_power_limit_the_clock_is_capped(tmp_path, hw):
    d = _card(tmp_path)
    h = (None if hw == "no-hwmon" else
         _hwmon(tmp_path, power=None) if hw == "absent" else _hwmon(tmp_path, writable=False))
    a, g = gpu_level.LevelApplier(d, h, log=_Log()), Governor(d)
    a.reconcile(QUIET, g, "quiet")
    assert _n(d / "max_freq") == 1400 and g.cap == 1400
    if h is not None and (h / "power1_max").exists():
        assert _n(h / "power1_max") == 230 * W, "a read-only limit is never written"
    assert a.status()["via"] == "max_freq"
    a.reconcile(FULL, g, "day")
    assert _n(d / "max_freq") == 2800


def test_a_power_limit_the_driver_refuses_falls_back_to_the_clock(tmp_path):
    """Writable by its mode, refused by the driver (a locked PL1, a
    read-only mount): the clock carries the level from then on."""
    d, h = _card(tmp_path), _hwmon(tmp_path)
    log = _Log()
    a, g = gpu_level.LevelApplier(d, h, log=log), Governor(d)

    class Locked:
        name = "power1_max"

        def write_text(self, _):
            raise OSError(errno.EOPNOTSUPP, "Operation not supported")
    a.pmax = Locked()
    a.reconcile(QUIET, g, "quiet")
    assert _n(d / "max_freq") == 1400 and a.status()["via"] == "max_freq"
    assert "refused the write" in a.status()["power_note"]


# --------------------------------------------------------- under the hold
def test_the_hold_is_clamped_to_the_cap_and_a_lower_cap_waits_for_it(tmp_path):
    d, h = _card(tmp_path), _hwmon(tmp_path, power=None)
    a, g = gpu_level.LevelApplier(d, h, log=_Log()), Governor(d)
    low = GpuLevel("low", max_clock="25%")                      # 700 MHz
    assert g.step(True) and _n(d / "min_freq") == 1400          # a query holds 1400
    a.reconcile(low, g, "quiet")
    # the driver refuses min_freq > max_freq: the cap is not written under the hold
    assert _n(d / "max_freq") == 2800 and "waits for the query hold" in a.status()["pending"]
    g.step(False)                                               # the query ends
    a.reconcile(low, g, "quiet")
    assert _n(d / "max_freq") == 700 and a.status()["pending"] is None
    g.step(True)
    assert _n(d / "min_freq") == 700, "the next hold is clamped to the cap"
    assert _n(d / "min_freq") <= _n(d / "max_freq")
    a.reconcile(FULL, g, "day")                                 # raised under a hold
    assert _n(d / "max_freq") == 2800
    g.step(True)
    assert _n(d / "min_freq") == 1400, "a raised cap lifts the hold"


def test_a_cap_below_the_rest_value_stops_at_it(tmp_path):
    d = _card(tmp_path, min_mhz=900)
    a, g = gpu_level.LevelApplier(d, None, log=_Log()), Governor(d)
    a.reconcile(GpuLevel("low", max_clock="25%"), g)
    assert _n(d / "max_freq") == 900, "never below the current min_freq"


# ---------------------------------------------------------------- the fan
def test_the_fan_is_firmware_controlled_unless_the_driver_lets_software_cap_it(tmp_path):
    d, h = _card(tmp_path), _hwmon(tmp_path)
    a = gpu_level.LevelApplier(d, h, log=_Log())
    a.reconcile(QUIET, Governor(d), "quiet")
    assert a.status()["fan"] == "firmware-controlled"
    assert _n(h / "fan1_input") == 900 and not (h / "fan1_max").exists(), "nothing guessed"


def test_a_writable_fan_max_is_capped_and_restored(tmp_path):
    d, h = _card(tmp_path), _hwmon(tmp_path, fan_max=3000)
    a, g = gpu_level.LevelApplier(d, h, log=_Log()), Governor(d)
    a.reconcile(QUIET, g, "quiet")
    assert _n(h / "fan1_max") == 1200
    a.restore(g)
    assert _n(h / "fan1_max") == 3000


def test_a_pwm_alone_is_not_an_rpm_cap(tmp_path):
    d, h = _card(tmp_path), _hwmon(tmp_path, pwm=True)
    a = gpu_level.LevelApplier(d, h, log=_Log())
    a.reconcile(QUIET, Governor(d), "quiet")
    assert _n(h / "pwm1") == 128 and a.status()["fan"].startswith("pwm only")


# ---------------------------------------------------------------- restore
def test_restore_gives_the_card_its_own_limits_back(tmp_path):
    d, h = _card(tmp_path), _hwmon(tmp_path, power=210 * W)
    a, g = gpu_level.LevelApplier(d, h, log=_Log()), Governor(d)
    a.reconcile(QUIET, g, "quiet")
    assert _n(h / "power1_max") == 115 * W
    a.restore(g)
    assert _n(h / "power1_max") == 210 * W, "as read at start, not the rated value"
    d2 = _card(tmp_path / "b")
    a2, g2 = gpu_level.LevelApplier(d2, None, log=_Log()), Governor(d2)
    a2.reconcile(QUIET, g2)
    g2.step(True)
    g2.release()
    a2.restore(g2)
    assert _n(d2 / "max_freq") == 2800 and _n(d2 / "min_freq") == 400


def test_a_level_left_by_a_killed_governor_is_not_taken_for_the_cards_own(tmp_path):
    d = _card(tmp_path, max_freq=1400)
    h = _hwmon(tmp_path, power=115 * W)               # quiet's 50 %, left on the card
    log = _Log()
    a = gpu_level.LevelApplier(d, h, levels=[FULL, QUIET], log=log)
    assert a.power_own == 230 * W and a.clock_own == 2800
    a.reconcile(FULL, Governor(d), "day")
    assert _n(h / "power1_max") == 230 * W


# -------------------------------------------------------- a bad profile
def test_a_level_that_cannot_be_applied_is_logged_once_and_the_last_kept(tmp_path):
    d, h = _card(tmp_path), _hwmon(tmp_path)
    log = _Log()
    a, g = gpu_level.LevelApplier(d, h, log=log), Governor(d)
    a.reconcile(QUIET, g, "quiet")
    bad = GpuLevel("bad", power_limit="lots")
    assert a.reconcile(bad, g, "day") is True
    assert a.reconcile(bad, g, "day") is False, "logged once, not every tick"
    assert _n(h / "power1_max") == 115 * W and a.status()["name"] == "quiet"
    assert "lots" in a.status()["error"]
    assert sum("not applied" in m for m in log) == 1


# -------------------------------------------------------- the governor loop
@pytest.fixture
def store(isolated_config):
    node_state.connect().close()
    dispatch.reset()
    return node_state.path()


def _until(pred, what):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if pred():
            return
        time.sleep(0.02)
    raise AssertionError(what)


def test_the_governor_applies_the_level_at_a_profile_change_and_restores_on_stop(
        tmp_path, store, monkeypatch, capsys):
    monkeypatch.setattr(config, "GOVERNOR_POLL_S", 0.02)
    monkeypatch.setattr(config, "GOVERNOR_HEARTBEAT_S", 0.05)
    cfg = Path(os.environ["CONFIG_DIR"])
    (cfg / "platform.toml").write_bytes(EXAMPLE.read_bytes())
    d, h = _card(tmp_path), _hwmon(tmp_path)
    now = [datetime(2026, 10, 27, 7, 0)]                    # quiet, in the operator's schedule
    stop = threading.Event()
    t = threading.Thread(target=gpu_governor.run, daemon=True,
                         kwargs=dict(stop=stop, freq_dir=d, drm_dir=tmp_path / "nodrm",
                                     hwmon_dir=h.parent, clock=lambda: now[0]))
    t.start()
    level = lambda: (gpu.governor().get("level") or {})   # noqa: E731
    try:
        _until(lambda: _n(h / "power1_max") == 115 * W, "quiet's power limit was not applied")
        _until(lambda: level().get("name") == "quiet", "the status does not carry the level")
        assert level()["via"] == "power1_max" and level()["fan"] == "firmware-controlled"
        assert _n(d / "max_freq") == 2800, "a writable power limit leaves the clock alone"
        now[0] = datetime(2026, 10, 27, 9, 0)                # day: full
        _until(lambda: _n(h / "power1_max") == 230 * W, "full did not restore the card's own")
        _until(lambda: level().get("name") == "full", "the status did not follow")
        now[0] = datetime(2026, 10, 27, 12, 30)              # quiet again
        _until(lambda: _n(h / "power1_max") == 115 * W, "quiet again")
        # a bad platform.toml: logged once, the last level kept
        (cfg / "platform.toml").write_text('[schedule]\nprofile = "else nowhere"\n')
        now[0] = datetime(2026, 10, 27, 12, 31)
        _until(lambda: "platform.toml" in (level().get("error") or ""), "the refusal not shown")
        time.sleep(0.2)
        assert _n(h / "power1_max") == 115 * W and level()["name"] == "quiet"
    finally:
        stop.set()
        t.join(5)
    assert _n(h / "power1_max") == 230 * W, "a stopping governor gives the power limit back"
    out = capsys.readouterr().out
    assert out.count("platform.toml") == 1, out
    assert "never its bytes" in out


def test_a_card_without_a_profile_is_left_as_it_is(tmp_path, store, monkeypatch):
    monkeypatch.setattr(config, "GOVERNOR_POLL_S", 0.02)
    d, h = _card(tmp_path, max_freq=2000), _hwmon(tmp_path, power=180 * W)
    stop = threading.Event()
    t = threading.Thread(target=gpu_governor.run, daemon=True,
                         kwargs=dict(stop=stop, freq_dir=d, drm_dir=tmp_path / "nodrm",
                                     hwmon_dir=h.parent))
    t.start()
    _until(lambda: gpu.governor()["state"] == "running", "the governor never said it runs")
    stop.set()
    t.join(5)
    assert _n(h / "power1_max") == 180 * W and _n(d / "max_freq") == 2000


def test_the_system_tab_shows_the_level(isolated_config):
    from app import web
    body = web.TEMPLATES.get_template("_fragments/gpu.html").render(
        state="unset", url="", governor={
            "state": "running", "pinned": False, "rest_mhz": 400, "max_mhz": 2800,
            "hold_mhz": 1400, "level": {"name": "quiet", "profile": "quiet", "via": "power1_max",
                                        "power_w": 115.0, "power_limit": "50%",
                                        "power_own_w": 230.0, "fan": "firmware-controlled",
                                        "pending": None, "error": None}})
    assert "held at 1400 MHz" in body, "the hold, not the card's maximum"
    assert "GPU level <b>quiet</b>" in body and "power limit 115.0 W" in body
    assert "fan firmware-controlled" in body and "never its bytes" in body
