"""The card's sensors in the System tab, and the history the governor keeps.

The xe driver's known issue: after a workload the fan stays high until a GPU
process restarts, and the driver exposes the fan read-only (no pwm). What can
be done is to see it: the live fan, temperatures, power and clock in the GPU
panel, and a week of samples every 10 s, kept for a widget to come and not
shown yet (operator, 2026-09-19/20; the workaround, restarting an engine, is never
Louise's).
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


def test_the_governor_keeps_a_week_of_samples(tmp_path, store, monkeypatch):
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
        rows = con.execute("SELECT fan_rpm, act_mhz FROM gpu_samples ORDER BY ts, rowid").fetchall()
    assert [tuple(r) for r in rows] == [(1910, 0), (1910, 2800), (1910, 0)], "the stale one is gone"


def test_a_sample_every_10_s_and_the_store_stops_growing_at_a_week(tmp_path):
    """Operator, 2026-09-19: every 10 s, deleted after a decent time so the
    disk is never flooded. A week at 10 s is 60,480 rows, 3.6 MB; a second
    day of insert-and-prune through the governor's own `store` leaves the
    row count and the file where they were."""
    import sqlite3
    assert config.GOVERNOR_SAMPLE_S == 10
    every, keep = int(config.GOVERNOR_SAMPLE_S), int(config.GOVERNOR_HISTORY_DAYS * 86400)
    path = tmp_path / "node.sqlite"
    con = sqlite3.connect(path, isolation_level=None)
    con.execute("PRAGMA journal_mode=WAL")
    for stmt in gpu_governor.SAMPLES_SCHEMA:
        con.execute(stmt)

    def sample(ts):
        return (ts, 1900 + ts % 200, 70.5 + ts % 10, 75.5, 180.25 + ts % 50, 2350, 0)

    def size():
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return path.stat().st_size

    t0 = 1_700_000_000
    con.execute("BEGIN")
    con.executemany("INSERT INTO gpu_samples VALUES (?,?,?,?,?,?,?)",
                    (sample(t0 + i * every) for i in range(keep // every)))
    con.execute("COMMIT")
    week = size()
    assert week < 5e6, f"a week at 10 s is {week / 1e6:.2f} MB"
    con.execute("BEGIN")
    for i in range(86400 // every):                   # one more day, sample by sample
        gpu_governor.store(con, sample(t0 + keep + i * every))
    con.execute("COMMIT")
    assert con.execute("SELECT COUNT(*) FROM gpu_samples").fetchone()[0] == keep // every + 1
    assert size() <= week * 1.05, "pruned pages are reused: the file does not grow"
    con.close()


def test_the_clock_is_the_first_gts(tmp_path):
    dev = tmp_path / "device"
    for gt, act, rp0 in (("gt0", 2350, 2800), ("gt1", 0, 1500)):
        f = dev / "tile0" / gt / "freq0"
        f.mkdir(parents=True)
        (f / "act_freq").write_text(f"{act}\n")
        (f / "rp0_freq").write_text(f"{rp0}\n")
    assert gpu.gt_clock(dev) == (2350, 2800), "render and compute, not media"
    assert gpu.gt_clock(tmp_path / "amd") == (None, None)


def _render(sensors, **extra):
    from app import web
    dev = gpu.GpuDevice(card="card0", pdev="0000:03:00.0", driver="xe", vendor_id="8086",
                        device_id="e223", vendor="Intel", name="Arc Pro B70",
                        vram_total=None, vram_used=None, sensors=sensors)
    return web.TEMPLATES.get_template("_fragments/gpu.html").render(
        state="ok", url="x", devices=(dev,), notes=(), governor=None, **extra)


def test_the_tab_shows_the_live_reading_and_no_history(isolated_config):
    body = _render(gpu.GpuSensors(fans=(1910,), temps=(("pkg", 73.0), ("vram", 78.0)),
                                  power_w=181.4, power_cap_w=230.0,
                                  clock_mhz=2350, clock_max_mhz=2800))
    assert "fan 1910 RPM" in body and "pkg 73 °C" in body and "181 W of 230 W cap" in body
    assert "clock 2350 of 2800 MHz" in body
    assert "<table" not in body and "hour" not in body, "the history waits for its widget"
    assert "clock idle" in _render(gpu.GpuSensors(fans=(713,), clock_mhz=0, clock_max_mhz=2800))
    assert "clock" not in _render(gpu.GpuSensors(fans=(713,))), "no clock files, no clock"
