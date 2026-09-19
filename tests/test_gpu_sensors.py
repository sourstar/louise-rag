"""The card's sensors in the System tab, and the history the governor keeps.

The xe driver's known issue: after a workload the fan stays high until a GPU
process restarts, and the driver exposes the fan read-only (no pwm). What can
be done is to see it: the live fan, temperatures and power in the GPU panel,
and a week of one-a-minute samples, summed by the hour, where a stuck fan is a
high fan minimum in an hour that was idle and cool (operator, 2026-09-20; the
workaround belongs to RAGE only).
"""
import time
from contextlib import closing

import pytest

from app import config, gpu, gpu_governor, gpu_probe, node_state


def _hwmon(tmp_path, *, fan=1910, pkg=73000, vram=78000, energy=1_000_000_000, cap=230_000_000):
    h = tmp_path / "card0" / "device" / "hwmon" / "hwmon14"
    h.mkdir(parents=True)
    (h / "name").write_text("xe\n")
    (h / "fan1_input").write_text(f"{fan}\n")
    (h / "temp2_input").write_text(f"{pkg}\n")
    (h / "temp2_label").write_text("pkg\n")
    (h / "temp3_input").write_text(f"{vram}\n")
    (h / "temp3_label").write_text("vram\n")
    (h / "energy1_input").write_text(f"{energy}\n")
    (h / "energy1_label").write_text("card\n")
    (h / "energy2_input").write_text("5\n")
    (h / "energy2_label").write_text("pkg\n")
    (h / "power1_cap").write_text(f"{cap}\n")
    return h


def test_the_sensors_are_read_by_label(tmp_path):
    h = _hwmon(tmp_path)
    assert gpu.hwmon_dir(h.parent.parent) == h
    s = gpu.read_sensors(h, power_w=180.0)
    assert s.fans == (1910,)
    assert s.temp("pkg") == 73.0 and s.temp("vram") == 78.0
    assert s.power_w == 180.0 and s.power_cap_w == 230.0
    assert gpu.energy_uj(h) == 1_000_000_000, "the card's counter, not the package's"


def test_the_panel_power_is_the_energy_counters_rate(tmp_path, monkeypatch):
    h = _hwmon(tmp_path)
    gpu_probe._last_energy.clear()
    readings = iter([1_000_000_000, 1_050_000_000])       # +50 J
    monkeypatch.setattr(gpu_probe, "energy_uj", lambda hw: next(readings))
    clock = iter([100.0, 100.25])                          # over 0.25 s
    monkeypatch.setattr(gpu_probe.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(gpu_probe.time, "sleep", lambda s: None)
    assert gpu_probe.card_power("0000:03:00.0", h) == pytest.approx(200.0)


def test_sensors_survive_the_trip_from_the_probe_to_the_page():
    dev = gpu.GpuDevice(card="card0", pdev="0000:03:00.0", driver="xe", vendor_id="8086",
                        device_id="e223", vendor="Intel", name="Arc Pro B70",
                        vram_total=1, vram_used=0,
                        sensors=gpu.GpuSensors(fans=(713,), temps=(("pkg", 76.0),),
                                               power_w=150.0, power_cap_w=230.0))
    back = gpu.from_json(gpu.to_json(gpu.GpuSnapshot(devices=(dev,))))
    assert back.devices[0].sensors == dev.sensors


@pytest.fixture
def store(isolated_config):
    node_state.connect().close()
    return node_state.path()


def test_the_governor_keeps_a_week_of_samples_and_the_tab_sums_them_by_hour(
        tmp_path, store, monkeypatch):
    h = _hwmon(tmp_path)
    freq = tmp_path / "freq0"
    freq.mkdir()
    (freq / "act_freq").write_text("0\n")
    sampler = gpu_governor.Sampler(tmp_path, freq)
    sampler.record(False)                         # a cool, idle card, fan at 1910
    (h / "energy1_input").write_text("1030000000\n")
    (freq / "act_freq").write_text("2800\n")
    sampler.record(True)                          # busy, someone waiting
    with closing(node_state.connect()) as con:
        rows = con.execute("SELECT fan_rpm, temp_pkg, act_mhz, interactive, power_w "
                           "FROM gpu_samples ORDER BY rowid").fetchall()
        assert [tuple(r)[:4] for r in rows] == [(1910, 73.0, 0, 0), (1910, 73.0, 2800, 1)]
        assert rows[0]["power_w"] is None, "no interval yet"
        assert rows[1]["power_w"] is not None and rows[1]["power_w"] > 0
        # a sample older than the history is pruned at the next write
        con.execute("INSERT INTO gpu_samples VALUES (?,?,?,?,?,?,?)",
                    (int(time.time()) - int(config.GOVERNOR_HISTORY_DAYS * 86400) - 10,
                     9999, 1.0, 1.0, 1.0, 0, 0))
    (freq / "act_freq").write_text("0\n")         # idle again
    sampler.record(False)
    with closing(node_state.connect()) as con:
        assert con.execute("SELECT COUNT(*) FROM gpu_samples WHERE fan_rpm=9999").fetchone()[0] == 0
    hours = gpu.history(12)
    assert len(hours) == 1 and hours[0]["samples"] == 3
    assert hours[0]["fan_min"] == 1910 and hours[0]["pkg_max"] == 73.0
    assert hours[0]["idle_pct"] == pytest.approx(200 / 3)
    assert hours[0]["interactive_pct"] == pytest.approx(100 / 3)


def test_no_samples_no_history(tmp_path, isolated_config):
    assert gpu.history() == []


def test_the_tab_shows_the_sensors_and_the_history(isolated_config):
    from app import web
    dev = gpu.GpuDevice(card="card0", pdev="0000:03:00.0", driver="xe", vendor_id="8086",
                        device_id="e223", vendor="Intel", name="Arc Pro B70",
                        vram_total=None, vram_used=None,
                        sensors=gpu.GpuSensors(fans=(1910,), temps=(("pkg", 73.0), ("vram", 78.0)),
                                               power_w=181.4, power_cap_w=230.0))
    body = web.TEMPLATES.get_template("_fragments/gpu.html").render(
        state="ok", url="x", devices=(dev,), notes=(), governor=None,
        history=[{"hour": "07:00", "samples": 60, "fan_min": 1900, "fan_max": 2000,
                  "pkg_max": 45.0, "vram_max": 50.0, "power_avg": 60.0,
                  "idle_pct": 100.0, "interactive_pct": 0.0}])
    assert "fan 1910 RPM" in body and "pkg 73 °C" in body and "181 W of 230 W cap" in body
    assert "hour (UTC)" in body and "1900–2000" in body and "100%" in body
