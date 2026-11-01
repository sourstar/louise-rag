"""The card's busy share from its DRIVER (app/gpu_pmu.py), and a probe that
cannot see a card saying so (app/gpu_busy.py).

München, 2026-11-01: the probe (CAP_PERFMON, the host's PID namespace) was
refused every model server's fdinfo — reading it is a ptrace-read check —
and printed 0 % of a card gpu_busy.py read at 34–37 %. The xe PMU's layout
below is the kernel's documented one (xe_pmu.rst); each test builds it as
sysfs files, and perf_event_open is faked at the fd.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from app import gpu_busy as gb
from app import gpu_pmu as pm

PDEV = "0000:03:00.0"
AMD = "0000:13:00.0"


def xe_sysfs(root: Path, pdev: str = PDEV) -> Path:
    d = pm.xe_pmu_dir(pdev, root)
    (d / "events").mkdir(parents=True)
    (d / "format").mkdir()
    (d / "type").write_text("42\n")
    (d / "cpumask").write_text("0\n")
    for name, spec in {"event": "config:0-11", "engine_instance": "config:12-19",
                       "engine_class": "config:20-27", "gt": "config:60-63",
                       "function": "config:44-59"}.items():
        (d / "format" / name).write_text(spec + "\n")
    (d / "events" / "engine-active-ticks").write_text("event=0x02\n")
    (d / "events" / "engine-total-ticks").write_text("event=0x03\n")
    return d


class FakePerf:
    """Engines that exist: {(gt, class, instance)}; counters set by the test."""

    def __init__(self, engines: set[tuple[int, int, int]]) -> None:
        self.engines = engines
        self.fds: dict[int, tuple[str, tuple[int, int, int]]] = {}
        self.values: dict[tuple[str, tuple[int, int, int]], int] = {}
        self.next_fd = 1000

    def decode(self, config: int) -> tuple[int, tuple[int, int, int]]:
        return (config & 0xFFF, ((config >> 60) & 0xF, (config >> 20) & 0xFF, (config >> 12) & 0xFF))

    def open(self, pmu_type: int, config: int, cpu: int) -> int:
        assert pmu_type == 42 and cpu == 0
        event, eng = self.decode(config)
        if eng not in self.engines:
            raise OSError(2, "No such file or directory")
        self.next_fd += 1
        self.fds[self.next_fd] = ("active" if event == 2 else "total", eng)
        return self.next_fd

    def read(self, fd: int) -> int:
        return self.values.get(self.fds[fd], 0)

    def set(self, eng, active: int, total: int) -> None:
        self.values[("active", eng)] = active
        self.values[("total", eng)] = total


def test_a_config_is_built_from_the_pmus_own_format(tmp_path):
    pmu = pm.XePmu(xe_sysfs(tmp_path), open_event=lambda *a: 1, read_event=lambda fd: 0)
    assert pmu.config({"event": 2, "gt": 1, "engine_class": 4, "engine_instance": 3}) == \
        2 | (3 << 12) | (4 << 20) | (1 << 60)
    with pytest.raises(ValueError, match="does not fit"):
        pmu.config({"gt": 16})


def test_the_xe_pmu_reads_each_class_over_its_instances(tmp_path):
    xe_sysfs(tmp_path)
    perf = FakePerf({(0, 4, 0), (0, 4, 1), (0, 0, 0), (0, 1, 0)})
    found, why = pm.sources([(PDEV, "xe", tmp_path / "dev")], tmp_path,
                            open_event=perf.open, read_event=perf.read)
    pmu = found[PDEV]
    assert pmu.kind == "pmu" and len(pmu._pairs) == 4 and why == {}
    for eng in perf.engines:
        perf.set(eng, 0, 1000)
    assert pmu.sample() is None, "the first read is a baseline"
    perf.set((0, 4, 0), 900, 2000)          # ccs0 90 % busy
    perf.set((0, 4, 1), 300, 2000)          # ccs1 30 %
    perf.set((0, 0, 0), 0, 2000)
    perf.set((0, 1, 0), 50, 2000)
    got = pmu.sample()
    assert got == pytest.approx({"ccs": 0.6, "rcs": 0.0, "bcs": 0.05}), \
        "a class is its instances' busy over their time: tops at 1.0"


def test_a_counter_that_went_back_or_an_impossible_share_gives_no_figure(tmp_path):
    xe_sysfs(tmp_path)
    perf = FakePerf({(0, 4, 0)})
    pmu = pm.XePmu(pm.xe_pmu_dir(PDEV, tmp_path), open_event=perf.open, read_event=perf.read)
    pmu.open()
    perf.set((0, 4, 0), 500, 1000)
    pmu.sample()
    perf.set((0, 4, 0), 100, 2000)
    assert pmu.sample() == {}, "a reset: no figure, never a negative one"
    perf.set((0, 4, 0), 1300, 3000)
    assert pmu.sample() == {}, "120 %: dropped"


def test_a_card_without_the_pmus_engine_events_says_why(tmp_path):
    d = xe_sysfs(tmp_path)
    (d / "events" / "engine-active-ticks").unlink()
    found, why = pm.sources([(PDEV, "xe", tmp_path / "dev")], tmp_path,
                            open_event=FakePerf(set()).open, read_event=lambda fd: 0)
    assert found == {} and "no xe PMU engine activity" in why[PDEV]


def test_a_pmu_that_opens_nothing_is_no_source(tmp_path):
    xe_sysfs(tmp_path)
    found, why = pm.sources([(PDEV, "xe", tmp_path / "dev")], tmp_path,
                            open_event=FakePerf(set()).open, read_event=lambda fd: 0)
    assert found == {} and "CAP_PERFMON" in why[PDEV]


def test_amdgpu_reads_its_firmwares_busy_percent(tmp_path):
    dev = tmp_path / "amd"
    dev.mkdir()
    (dev / "gpu_busy_percent").write_text("37\n")
    found, why = pm.sources([(AMD, "amdgpu", dev), ("0000:20:00.0", "nouveau", tmp_path)],
                            tmp_path)
    assert found[AMD].sample() == {"gfx": 0.37}
    assert "nouveau" in why["0000:20:00.0"]


# ---------------------------------------------------------------- the sampler

class Clock:
    def __init__(self) -> None:
        self.ns = 10**12

    def wall(self) -> float:
        return self.ns / 1e9

    def mono(self) -> int:
        return self.ns


class Fixed:
    kind = "pmu"

    def __init__(self, shares):
        self.shares = list(shares)

    def sample(self):
        return self.shares.pop(0) if self.shares else None


def test_a_cards_figure_comes_from_its_driver_and_fdinfo_says_who():
    clock = Clock()
    files = gb.DrmFiles()
    files.unreadable = 790                    # the node: nobody's fdinfo is ours to read
    sampler = gb.BusySampler(lambda: files, lambda: [PDEV],
                             devices=lambda: ({PDEV: Fixed([None, {"ccs": 0.41, "bcs": 0.02}])}, {}),
                             wall=clock.wall, mono_ns=clock.mono)
    sampler.sample()
    clock.ns += 10**9
    sampler.sample()
    first, second = sampler.since()["cards"][PDEV]
    assert first["busy"] is None and first["source"] == "pmu", "the PMU's baseline: no figure yet"
    assert second["busy"] == 0.41 and second["engines"] == {"bcs": 0.02, "ccs": 0.41}
    assert "blind" not in second


def test_a_card_nobody_can_see_is_unknown_never_zero():
    """The defect: 790 processes refused, nothing read, and 0.0 printed."""
    clock = Clock()
    files = gb.DrmFiles()
    files.unreadable = 790
    sampler = gb.BusySampler(lambda: files, lambda: [PDEV],
                             devices=lambda: ({}, {PDEV: "no xe PMU engine activity"}),
                             wall=clock.wall, mono_ns=clock.mono)
    sampler.sample()
    clock.ns += 10**9
    sampler.sample()
    s = sampler.since()["cards"][PDEV][-1]
    assert s["busy"] is None and s["engines"] == {}
    assert "no xe PMU engine activity" in s["blind"] and "790 process(es) unreadable" in s["blind"]


def test_fdinfo_alone_counts_only_when_every_process_could_be_read(tmp_path, monkeypatch):
    proc = tmp_path / "proc"
    d = proc / "100"
    (d / "fd").mkdir(parents=True)
    (d / "fdinfo").mkdir()
    os.symlink("/dev/dri/renderD128", d / "fd" / "3")
    (d / "fdinfo" / "3").write_text(f"drm-client-id:\t7\ndrm-pdev:\t{PDEV}\n"
                                    f"drm-cycles-ccs:\t0\ndrm-total-cycles-ccs:\t0\n")
    (proc / "1" / "fd").mkdir(parents=True)
    label = lambda p: gb.Holder(int(p.name), "bge-m3", "localai")
    files = gb.drm_files(proc, label)
    assert files.unreadable == 0 and len(files) == 1
    real = os.listdir

    def refused(path):
        if str(path) == str(proc / "1" / "fd"):
            raise PermissionError(13, "Permission denied")      # ptrace-read refused
        return real(path)
    monkeypatch.setattr(gb.os, "listdir", refused)
    files = gb.drm_files(proc, label)
    assert files.unreadable == 1 and len(files) == 1


def test_the_recorder_keeps_no_figure_for_a_card_the_probe_cannot_see(tmp_path):
    from app import card_record as cr
    asked = {"t": 1_762_000_000.0}

    def fetch(url, since):
        return {"cards": {PDEV: [{"t": 1_762_000_000.4, "dt": 1.0, "busy": None,
                                  "blind": "no source"}]}}
    rec = cr.CardRecorder("u", tmp_path, gates=lambda: {}, feeders=lambda: {},
                          claim=lambda: False, fetch=fetch, wall=lambda: asked["t"])
    rec.tick()
    asked["t"] += 1
    assert rec.flush(final=True) == 1
    import json
    row = json.loads(next(tmp_path.glob("*.jsonl")).read_text().splitlines()[0])
    assert row["cards"] is None, "unknown, never 0"


def test_a_cards_busy_is_its_busiest_compute_engine_not_a_resident_copy_context():
    """München, perf 2026-11-01: bcs 78.4 % beside ccs 78.3 %, 92.7 beside
    91.9, 0 at idle — the copy engine counted active while compute runs."""
    assert gb.card_busy({"bcs": 0.927, "ccs": 0.919, "rcs": 0.0}) == 0.919
    assert gb.card_busy({"bcs": 0.6, "ccs": 0.1, "rcs": 0.3}) == 0.3
    assert gb.card_busy({"gfx": 0.37}) == 0.37
    assert gb.card_busy({"vcs": 0.2, "bcs": 0.4}) == 0.4, "no compute engine named: the busiest"
    assert gb.card_busy({}) == 0.0
