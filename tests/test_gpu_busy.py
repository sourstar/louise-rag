"""The card's busy time (app/gpu_busy.py), and the probe's GET /busy.

The xe fdinfo shape is the node's own (kernel 6.18, Arc Pro B70); amdgpu's
is the kernel's documented one (Documentation/gpu/drm-usage-stats.rst).
The sampler runs against a fake /proc and a fake clock: each test writes the
counters the kernel would have, steps the clock, and reads the samples.
"""
from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from app import gpu_busy as gb
from app import gpu_probe as gp

PDEV = "0000:03:00.0"
OTHER = "0000:07:00.0"


def xe(client: int, engines: dict[str, tuple[int, int]], *, pdev: str = PDEV,
       capacity: dict[str, int] | None = None) -> str:
    lines = [f"drm-driver:\txe", f"drm-client-id:\t{client}", f"drm-pdev:\t{pdev}",
             "drm-total-vram0:\t1024 KiB", "drm-resident-vram0:\t1024 KiB"]
    for engine, (busy, total) in engines.items():
        lines += [f"drm-cycles-{engine}:\t{busy}", f"drm-total-cycles-{engine}:\t{total}"]
    for engine, n in (capacity or {}).items():
        lines.append(f"drm-engine-capacity-{engine}:\t{n}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- parsing

def test_xe_counters_pair_each_engine_with_its_own_time_base():
    c = gb.parse_counters(xe(7, {"ccs": (500, 1000), "rcs": (0, 1000)}, capacity={"ccs": 4}))
    assert (c.pdev, c.client) == (PDEV, "7")
    assert c.cycles == {"ccs": (500, 1000), "rcs": (0, 1000)}
    assert c.capacity == {"ccs": 4} and c.engine_ns == {}


def test_amdgpu_busy_nanoseconds_and_capacity_are_read_apart():
    c = gb.parse_counters(
        "drm-driver:\tamdgpu\ndrm-pdev:\t0000:13:00.0\ndrm-client-id:\t3\n"
        "drm-memory-vram:\t1024 KiB\ndrm-engine-gfx:\t2000 ns\ndrm-engine-compute:\t5 ns\n"
        "drm-engine-capacity-compute:\t2\ndrm-engine-video-enhance:\t0 ns\n")
    assert c.engine_ns == {"gfx": 2000, "compute": 5, "video-enhance": 0}
    assert c.capacity == {"compute": 2} and c.cycles == {}


def test_a_file_that_is_no_drm_client_has_no_counters():
    assert gb.parse_counters("pos:\t0\nflags:\t02\n") is None
    assert gb.parse_counters("drm-pdev:\tx\ndrm-cycles-ccs:\t1\n") is None
    assert gb.parse_counters(xe(1, {})).empty


def test_a_cycles_line_without_its_time_base_is_not_used():
    c = gb.parse_counters(f"drm-client-id:\t1\ndrm-pdev:\t{PDEV}\ndrm-cycles-ccs:\t5\n")
    assert c.cycles == {} and c.empty


# ---------------------------------------------------------------- shares

def _c(engines: dict[str, tuple[int, int]], capacity=None) -> gb.Counters:
    return gb.parse_counters(xe(1, engines, capacity=capacity))


def test_a_share_is_busy_ticks_over_the_clients_own_time_base():
    got = gb.shares(_c({"ccs": (100, 1000), "bcs": (0, 1000)}),
                    _c({"ccs": (850, 2000), "bcs": (10, 2000)}), wall_ns=10**9)
    assert got == pytest.approx({"ccs": 0.75, "bcs": 0.01})


def test_capacity_divides_so_a_class_tops_at_one():
    got = gb.shares(_c({"ccs": (0, 0)}, {"ccs": 4}), _c({"ccs": (2000, 1000)}, {"ccs": 4}),
                    wall_ns=10**9)
    assert got == {"ccs": 0.5}


@pytest.mark.parametrize("before,after", [
    ({"ccs": (900, 1000)}, {"ccs": (100, 2000)}),     # busy went backwards: a reset
    ({"ccs": (0, 2000)}, {"ccs": (10, 1000)}),        # the time base went backwards
    ({"ccs": (0, 1000)}, {"ccs": (1100, 2000)}),      # 110 %: impossible
])
def test_an_impossible_reading_is_none_never_a_number(before, after):
    assert gb.shares(_c(before), _c(after), wall_ns=10**9) is None


def test_rounding_within_the_slack_is_capped_at_one():
    assert gb.shares(_c({"ccs": (0, 1000)}), _c({"ccs": (1010, 2000)}), 10**9) == {"ccs": 1.0}


def test_a_time_base_that_did_not_move_gives_nothing():
    assert gb.shares(_c({"ccs": (5, 1000)}), _c({"ccs": (5, 1000)}), 10**9) == {}


def test_nanoseconds_are_read_against_the_wall_clock():
    before = gb.Counters(PDEV, "1", engine_ns={"gfx": 0, "compute": 0}, capacity={"compute": 2})
    after = gb.Counters(PDEV, "1", engine_ns={"gfx": 250_000_000, "compute": 1_000_000_000},
                        capacity={"compute": 2})
    assert gb.shares(before, after, wall_ns=10**9) == pytest.approx({"gfx": 0.25, "compute": 0.5})


# ---------------------------------------------------------------- the sampler

class Clock:
    def __init__(self) -> None:
        self.ns = 1_000 * 10**9

    def step(self, s: float = 1.0) -> None:
        self.ns += int(s * 1e9)

    def wall(self) -> float:
        return self.ns / 1e9 + 1_700_000_000 - 1_000

    def mono(self) -> int:
        return self.ns


class FakeProc:
    """A /proc of DRM descriptors: (pid, fd) -> fdinfo text."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.labels: dict[int, str] = {}

    def put(self, pid: int, fd: int, text: str, label: str = "bge-m3") -> None:
        d = self.root / str(pid)
        (d / "fd").mkdir(parents=True, exist_ok=True)
        (d / "fdinfo").mkdir(exist_ok=True)
        if not (d / "fd" / str(fd)).is_symlink():
            os.symlink("/dev/dri/renderD128", d / "fd" / str(fd))
        (d / "fdinfo" / str(fd)).write_text(text)
        self.labels[pid] = label

    def end(self, pid: int) -> None:
        d = self.root / str(pid)
        for sub in ("fd", "fdinfo"):
            for f in (d / sub).iterdir():
                f.unlink()
            (d / sub).rmdir()
        d.rmdir()

    def holder(self, pid_dir: Path) -> gb.Holder:
        pid = int(pid_dir.name)
        return gb.Holder(pid=pid, label=self.labels.get(pid, "?"), container="localai")


@pytest.fixture
def rig(tmp_path):
    proc, clock = FakeProc(tmp_path / "proc"), Clock()
    proc.root.mkdir()
    sampler = gb.BusySampler(lambda: gb.drm_files(proc.root, proc.holder), lambda: [PDEV, OTHER],
                             wall=clock.wall, mono_ns=clock.mono, rescan_s=10.0)
    return proc, clock, sampler


def _last(sampler: gb.BusySampler, pdev: str = PDEV) -> dict:
    return sampler.since()["cards"][pdev][-1]


def test_a_first_reading_is_a_baseline_and_the_second_a_share(rig):
    proc, clock, sampler = rig
    proc.put(9317, 3, xe(7, {"ccs": (0, 0)}))
    sampler.sample()
    first = _last(sampler)
    assert first["dt"] is None and first["new"] == 1 and first["busy"] == 0.0
    proc.put(9317, 3, xe(7, {"ccs": (820, 1000)}))
    clock.step()
    sampler.sample()
    s = _last(sampler)
    assert s["dt"] == 1.0 and s["busy"] == 0.82 and s["engines"] == {"ccs": 0.82}
    assert s["processes"] == [{"pid": 9317, "label": "bge-m3", "container": "localai",
                               "busy": 0.82}]
    assert "new" not in s and "dropped" not in s


def test_two_descriptors_of_one_client_are_counted_once(rig):
    proc, clock, sampler = rig
    for fd in (3, 4):                   # a dup: the same client id
        proc.put(9317, fd, xe(7, {"ccs": (0, 0)}))
    sampler.sample()
    for fd in (3, 4):
        proc.put(9317, fd, xe(7, {"ccs": (400, 1000)}))
    clock.step()
    sampler.sample()
    assert _last(sampler)["busy"] == 0.4


def test_two_servers_sum_on_the_card_and_each_keeps_its_own(rig):
    proc, clock, sampler = rig
    proc.put(100, 3, xe(7, {"ccs": (0, 0)}), label="bge-m3")
    proc.put(200, 3, xe(9, {"ccs": (0, 50)}), label="qwen3-embedding-4b")
    proc.put(300, 3, xe(11, {}), label="an-enumeration")       # holds no engine counters
    sampler.sample()
    proc.put(100, 3, xe(7, {"ccs": (600, 1000)}), label="bge-m3")
    proc.put(200, 3, xe(9, {"ccs": (300, 1050)}), label="qwen3-embedding-4b")
    clock.step()
    sampler.sample()
    s = _last(sampler)
    assert s["busy"] == 0.9
    assert [(p["label"], p["busy"]) for p in s["processes"]] == [
        ("bge-m3", 0.6), ("qwen3-embedding-4b", 0.3)]


def test_the_cards_share_is_capped_at_one(rig):
    proc, clock, sampler = rig
    proc.put(100, 3, xe(7, {"ccs": (0, 0)}))
    proc.put(200, 3, xe(9, {"ccs": (0, 0)}))
    sampler.sample()
    proc.put(100, 3, xe(7, {"ccs": (700, 1000)}))
    proc.put(200, 3, xe(9, {"ccs": (700, 1000)}))
    clock.step()
    sampler.sample()
    assert _last(sampler)["busy"] == 1.0


def test_the_card_is_as_busy_as_its_busiest_engine(rig):
    proc, clock, sampler = rig
    proc.put(100, 3, xe(7, {"ccs": (0, 0), "bcs": (0, 0)}))
    sampler.sample()
    proc.put(100, 3, xe(7, {"ccs": (900, 1000), "bcs": (100, 1000)}))
    clock.step()
    sampler.sample()
    s = _last(sampler)
    assert s["busy"] == 0.9 and s["engines"] == {"bcs": 0.1, "ccs": 0.9}


def test_a_counter_reset_is_dropped_and_counted_never_averaged_in(rig):
    proc, clock, sampler = rig
    proc.put(100, 3, xe(7, {"ccs": (900_000, 1_000_000)}))
    sampler.sample()
    proc.put(100, 3, xe(7, {"ccs": (5, 1_001_000)}))
    clock.step()
    sampler.sample()
    s = _last(sampler)
    assert s["dropped"] == 1 and s["busy"] == 0.0 and s["processes"] == []
    proc.put(100, 3, xe(7, {"ccs": (505, 1_002_000)}))
    clock.step()
    sampler.sample()
    assert _last(sampler)["busy"] == 0.5          # the next interval reads again


def test_a_card_nobody_uses_has_samples_at_zero(rig):
    proc, clock, sampler = rig
    proc.put(100, 3, xe(7, {"ccs": (0, 0)}))
    sampler.sample()
    clock.step()
    sampler.sample()
    other = _last(sampler, OTHER)
    assert other["busy"] == 0.0 and other["processes"] == [] and other["dt"] == 1.0


def test_a_server_that_ends_leaves_and_a_new_one_is_found_at_the_next_look(rig):
    proc, clock, sampler = rig
    proc.put(100, 3, xe(7, {"ccs": (0, 0)}))
    sampler.sample()
    proc.end(100)
    proc.put(200, 3, xe(9, {"ccs": (0, 0)}), label="new-server")
    clock.step()
    sampler.sample()                    # 100 gone; 200 not yet looked for
    s = _last(sampler)
    assert s["processes"] == [] and "new" not in s
    clock.step(10)
    sampler.sample()                    # the look: 200 baselined
    assert _last(sampler)["new"] == 1
    proc.put(200, 3, xe(9, {"ccs": (250, 1000)}), label="new-server")
    clock.step()
    sampler.sample()
    assert [(p["label"], p["busy"]) for p in _last(sampler)["processes"]] == [("new-server", 0.25)]


def test_a_descriptor_that_now_names_another_client_starts_over(rig):
    proc, clock, sampler = rig
    proc.put(100, 3, xe(7, {"ccs": (0, 0)}))
    sampler.sample()
    proc.put(100, 3, xe(8, {"ccs": (999_000, 1_000_000)}))   # closed, reopened: a new client
    clock.step()
    sampler.sample()
    s = _last(sampler)
    assert s["busy"] == 0.0 and "dropped" not in s


def test_since_returns_only_later_samples_and_keep_bounds_the_series(tmp_path):
    clock = Clock()
    sampler = gb.BusySampler(lambda: [], lambda: [PDEV], wall=clock.wall, mono_ns=clock.mono,
                             keep_s=3.0)
    times = []
    for _ in range(5):
        sampler.sample()
        times.append(round(clock.wall(), 3))
        clock.step()
    kept = sampler.since()["cards"][PDEV]
    assert [s["t"] for s in kept] == times[-3:]
    assert [s["t"] for s in sampler.since(times[-2])["cards"][PDEV]] == times[-1:]
    assert sampler.since()["period_s"] == 1.0


def test_run_keeps_its_beat_and_stops():
    calls = []
    stop = threading.Event()
    sampler = gb.BusySampler(lambda: [], lambda: [PDEV], period_s=0.01)
    real = sampler.sample

    def sample():
        calls.append(1)
        if len(calls) == 3:
            stop.set()
        if len(calls) == 2:
            raise OSError("a bad read")         # never ends the series
        real()
    sampler.sample = sample
    t = threading.Thread(target=sampler.run, args=(stop,))
    t.start()
    t.join(timeout=5)
    assert not t.is_alive() and len(calls) == 3


# ---------------------------------------------------------------- the probe

def test_the_probe_samples_the_hosts_drm_files_with_the_panels_labels(tmp_path, monkeypatch):
    proc = tmp_path / "proc"
    d = proc / "9317"
    (d / "fd").mkdir(parents=True)
    (d / "fdinfo").mkdir()
    (d / "cmdline").write_text("\0".join(
        ["/app/llama-server", "-m", "/models/bge-m3-FP16.gguf", "--embeddings"]) + "\0")
    (d / "cgroup").write_text("0::/docker/" + "a" * 64 + "\n")
    os.symlink("/dev/dri/renderD128", d / "fd" / "3")
    os.symlink("socket:[9]", d / "fd" / "4")
    (d / "fdinfo" / "3").write_text(xe(7, {"ccs": (0, 0)}))
    monkeypatch.setattr(gp, "PROC", proc)
    monkeypatch.setattr(gp, "SYS_DRM", tmp_path / "no-sys")
    sampler = gp.busy_sampler()
    sampler.sample()
    (d / "fdinfo" / "3").write_text(xe(7, {"ccs": (300, 1000)}))
    sampler.sample()
    s = sampler.since()["cards"][PDEV][-1]
    assert s["busy"] == 0.3
    assert s["processes"][0]["label"] == "bge-m3-FP16"
    assert s["processes"][0]["container"] == "a" * 12


@pytest.fixture
def served(monkeypatch):
    server = gp.ThreadingHTTPServer(("127.0.0.1", 0), gp._Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def test_busy_is_503_until_the_sampler_runs(served, monkeypatch):
    monkeypatch.setattr(gp, "SAMPLER", None)
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(served + "/busy", timeout=5)
    assert e.value.code == 503


def test_busy_answers_the_samples_after_since(served, monkeypatch):
    clock = Clock()
    sampler = gb.BusySampler(lambda: [], lambda: [PDEV], wall=clock.wall, mono_ns=clock.mono)
    for _ in range(3):
        sampler.sample()
        clock.step()
    monkeypatch.setattr(gp, "SAMPLER", sampler)
    with urllib.request.urlopen(served + "/busy", timeout=5) as r:
        assert r.headers["Content-Type"] == "application/json"
        everything = json.load(r)
    assert len(everything["cards"][PDEV]) == 3
    since = everything["cards"][PDEV][0]["t"]
    with urllib.request.urlopen(f"{served}/busy?since={since}", timeout=5) as r:
        assert len(json.load(r)["cards"][PDEV]) == 2
    for bad in ("x", "nan", "inf"):
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(f"{served}/busy?since={bad}", timeout=5)
        assert e.value.code == 400
