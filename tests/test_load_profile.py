"""The load profiler (tools/load_profile.py; docs/notes/rage.md §14).

RAGE is a MockTransport over a small world: a clock that a load advances —
by more when the weights are not in RAM — and a disk counter that a cold
load moves by the size of the weights. The trials are judged by what that
counter says, as on the node: a trial labelled cold whose disk read nothing
is classified warm, never reported as cold.
"""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

import httpx
import pytest

from tools import load_profile as lp

TOKEN = "s3cret-admin-token"
MIB = 1 << 20

RAGE_TOML = """\
[rage]
reserve_mib  = {{ xpu = 1000, cpu = 2000 }}
capacity_mib = {{ xpu = 10000, cpu = 20000 }}
device_names = {{ xpu = "a card" }}

[models.chat]
engine  = {{ name = "llama.cpp", build = "b10630", variant = "sycl" }}
weights = "{models}/chat.gguf"
device  = "xpu"
memory  = {{ xpu = 5000, cpu = 6000 }}
host    = {{ xpu = 5600 }}
cmd.xpu = ["/app/llama-server", "-m", "${{WEIGHTS}}", "--port", "${{PORT}}"]
cmd.cpu = ["/app/llama-server", "-m", "${{WEIGHTS}}", "-ngl", "0", "--port", "${{PORT}}"]

[models.bge-m3]
engine  = {{ name = "llama.cpp", build = "b10630", variant = "sycl" }}
weights = "{models}/bge-m3.gguf"
device  = "xpu"
memory  = {{ xpu = 1400, cpu = 2600 }}
host    = {{ xpu = 1600 }}
cmd.xpu = ["/app/llama-server", "-m", "${{WEIGHTS}}", "--embeddings", "--port", "${{PORT}}"]

[models.madlad]
engine  = {{ name = "louise-madlad-server", build = "2" }}
weights = "{models}/madlad"
device  = "xpu"
memory  = {{ xpu = 8200, cpu = 12000 }}
host    = {{ xpu = 8600 }}
cmd.xpu = ["/venv/bin/python", "-m", "servers.launch", "madlad", "--weights", "${{WEIGHTS}}", "--port", "${{PORT}}"]
"""


class World:
    """The node as the profiler sees it: RAGE, the disk, the clock."""

    COLD_S, WARM_S, UNLOAD_S = 6.0, 1.5, 0.3

    def __init__(self, root: Path) -> None:
        models = root / "models"
        (models / "madlad").mkdir(parents=True)
        (models / "chat.gguf").write_bytes(b"c" * (2 * MIB))
        (models / "bge-m3.gguf").write_bytes(b"b" * MIB)
        (models / "madlad" / "model.safetensors").write_bytes(b"m" * MIB)
        (models / "madlad" / "config.json").write_text('{"model_type": "t5"}')
        self.config = root / "rage.toml"
        self.config.write_text(RAGE_TOML.format(models=models), encoding="utf-8")
        self.token_file = root / "token"
        self.token_file.write_text(TOKEN + "\n", encoding="utf-8")
        self.sizes = {"chat": 2 * MIB, "bge-m3": MIB,
                      "madlad": MIB + len('{"model_type": "t5"}')}
        self.by_path = {models / "chat.gguf": "chat", models / "bge-m3.gguf": "bge-m3",
                        models / "madlad" / "model.safetensors": "madlad",
                        models / "madlad" / "config.json": "madlad"}
        self.t = 1000.0
        self.disk = 0
        self.cached: set[str] = set()
        self.loaded: dict[str, str] = {}
        self.idle: dict[str, int] = {}
        self.refuse: set[str] = set()
        self.interrupt_on: str | None = None
        self.events: list[str] = []
        self.loads: list[tuple[float, float]] = []
        self.unauthorised = 0
        self.fs = "ext4"
        self.can_drop = True

    # the clock
    def clock(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds

    def _read(self, model: str) -> None:
        if model not in self.cached:
            self.disk += self.sizes[model]
            self.cached.add(model)

    # RAGE
    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and request.headers.get("Authorization") != f"Bearer {TOKEN}":
            self.unauthorised += 1
            return httpx.Response(401, json={"detail": "no"})
        if path == "/louise/models":
            rows = []
            for m in ("bge-m3", "chat", "madlad"):
                if m in self.loaded:
                    rows.append({"id": m, "state": "ready", "device": self.loaded[m],
                                 "idle_s": self.idle.get(m, 999)})
                else:
                    rows.append({"id": m, "state": "unloaded", "device": None})
            return httpx.Response(200, json=rows)
        if path == "/louise/gpu":
            return httpx.Response(200, json={"cards": {}})
        m = re.fullmatch(r"/louise/models/([^/]+)/(load|unload)", path)
        if m:
            model, verb = m.groups()
            self.events.append(f"{verb} {model}")
            if verb == "unload":
                if model not in self.loaded:
                    return httpx.Response(404, json={"error": "not loaded"})
                del self.loaded[model]
                self.t += self.UNLOAD_S
                return httpx.Response(200, json={"id": model, "state": "unloaded"})
            if model == self.interrupt_on:
                raise KeyboardInterrupt
            if model in self.refuse:
                return httpx.Response(503, json={"error": f"{model} needs 8200 MiB; 1125 free"},
                                      headers={"Retry-After": "30"})
            t0 = self.t
            self.t += self.WARM_S if model in self.cached else self.COLD_S
            self._read(model)
            self.loads.append((t0, self.t))
            body = json.loads(request.content) if request.content else {}
            self.loaded[model] = (body or {}).get("device") or "xpu"
            return httpx.Response(200, json={"id": model, "state": "ready"})
        return httpx.Response(404)


class FakeHost(lp.Host):
    def __init__(self, world: World) -> None:
        super().__init__(proc=Path("/nonexistent"))
        self.w = world

    def snapshot(self) -> lp.Snapshot:
        return lp.Snapshot(disk={"nvme0n1": self.w.disk}, arc={})

    def fstype(self, path: Path) -> str | None:
        return self.w.fs

    def memory(self) -> dict[str, int]:
        return {"MemTotal": 63_000}

    def can_drop_caches(self) -> bool:
        return self.w.can_drop

    def evict(self, files, how: str) -> None:
        for p in files:
            self.w.events.append(f"evict {self.w.by_path[p]}")
            self.w.cached.discard(self.w.by_path[p])

    def read_all(self, files) -> float:
        for p in files:
            self.w._read(self.w.by_path[p])
        return 0.5


class FakeBackground:
    """An embedding answering 4 times a second alone and once a second while
    a load runs, on the world's clock."""

    def __init__(self, world: World) -> None:
        self.w = world
        self.t0: float | None = None
        self.stopped = False

    def start(self) -> None:
        self.t0 = self.w.t

    def stop(self) -> None:
        self.stopped = True

    def completions(self) -> list[float]:
        out, t = [], self.t0
        while t < self.w.t:
            during = any(a <= t < b for a, b in self.w.loads)
            t += 1.0 if during else 0.25
            out.append(t)
        return out


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


def _run(world: World, *args: str, background=None, out: Path | None = None) -> int:
    client = httpx.Client(transport=httpx.MockTransport(world.handler))
    argv = ["--rage", "http://rage:30437", "--config", str(world.config),
            "--token-file", str(world.token_file), *args]
    if out is not None:
        argv += ["--out", str(out)]
    return lp.main(argv, client=client, host=FakeHost(world), background=background,
                   clock=world.clock, sleep=world.sleep)


def _records(out: Path) -> list[dict]:
    return [json.loads(line) for line in out.read_text().splitlines()]


# ---- what the host reads -----------------------------------------------------

def test_diskstats_counts_whole_disks_once_and_in_bytes() -> None:
    text = ("   7       0 loop0 10 0 80 0 0 0 0 0 0 0 0 0 0 0 0 0 0\n"
            " 259       0 nvme0n1 100 0 2048 5 0 0 0 0 0 0 0 0 0 0 0 0 0\n"
            " 259       1 nvme0n1p1 90 0 1024 5 0 0 0 0 0 0 0 0 0 0 0 0 0\n"
            "   8       0 sda 3 0 8 1 0 0 0 0 0 0 0 0 0 0 0 0 0\n")
    assert lp.disk_read_bytes(text) == {"nvme0n1": 2048 * 512, "sda": 8 * 512}
    assert lp.disk_read_bytes(text, ("nvme0n1p1",)) == {"nvme0n1p1": 1024 * 512}


def test_arc_counters_are_the_ones_a_load_moves() -> None:
    text = ("13 1 0x01 123 33456 1234 5678\n"
            "name                            type data\n"
            "hits                            4    999\n"
            "demand_data_hits                4    120\n"
            "demand_data_misses              4    7\n"
            "size                            4    9663676416\n")
    assert lp.arc_counters(text) == {"demand_data_hits": 120, "demand_data_misses": 7,
                                     "size": 9663676416}


def test_the_filesystem_is_the_longest_mount_that_holds_the_path() -> None:
    mountinfo = (
        "23 1 0:22 / / rw - ext4 /dev/sda1 rw\n"
        "40 23 0:50 / /mnt/ssd5 rw - zfs ssd5 rw\n"
        "41 23 0:51 / /mnt/ssd5x rw - ext4 /dev/sdb1 rw\n"
        "42 40 0:52 / /mnt/ssd5/models\\040dir rw - zfs ssd5/models rw\n")
    assert lp.fstype_of(Path("/mnt/ssd5/models dir/chat.gguf"), mountinfo) == "zfs"
    assert lp.fstype_of(Path("/mnt/ssd5x/a.gguf"), mountinfo) == "ext4"
    assert lp.fstype_of(Path("/mnt/ssd5/apps/x"), mountinfo) == "zfs"


@pytest.mark.parametrize("read,expected", [
    (2 * MIB, "cold"), (MIB, "cold"), (MIB - 1, "partial"),
    (int(0.05 * 2 * MIB) + 1, "partial"), (int(0.05 * 2 * MIB), "warm"), (0, "warm")])
def test_a_trial_is_cold_or_warm_by_what_the_disk_read(read: int, expected: str) -> None:
    assert lp.cache_seen(read, 2 * MIB) == expected


def test_the_embedding_lost_to_a_load_is_counted_in_seconds() -> None:
    quiet = [i * 0.5 for i in range(20)]               # 20 answers in 10 s: 2 per second
    during = [11.0, 12.0, 13.0, 14.0]                  # 4 answers in a 5 s load
    d = lp.disturbance(quiet + during, (0.0, 10.0), (10.0, 15.0))
    assert d["per_s_quiet"] == 2.0
    assert d["per_s_during_load"] == 0.8
    assert d["embedding_lost_s"] == 3.0                # 10 expected - 4 = 6 answers = 3 s


# ---- a run -------------------------------------------------------------------

def test_cold_then_warm_is_measured_and_rage_is_put_back(world: World, tmp_path: Path,
                                                         capsys) -> None:
    world.loaded["chat"] = "xpu"
    out = tmp_path / "profile.jsonl"
    code = _run(world, "--models", "chat,bge-m3", "--reps", "1", "--skip-raw", out=out)
    assert code == lp.EXIT_OK
    trials = [r for r in _records(out) if r["kind"] == "trial"]
    seen = {(r["model"], r["cache_wanted"]): r for r in trials}
    assert len(trials) == 4
    for model, size in (("chat", 2.0), ("bge-m3", 1.0)):
        cold, warm = seen[(model, "cold")], seen[(model, "warm")]
        assert cold["cache_seen"] == "cold" and cold["read_mib"] == size
        assert cold["load_s"] == World.COLD_S
        assert warm["cache_seen"] == "warm" and warm["read_mib"] == 0.0
        assert warm["load_s"] == World.WARM_S
        assert cold["unload_s"] == World.UNLOAD_S
    # a cache is emptied only once the model holds no page of it
    first = world.events.index("evict chat")
    assert world.events[first - 1] == "unload chat"
    assert world.loaded == {"chat": "xpu"}             # loaded at the start, loaded again
    printed = capsys.readouterr()
    assert TOKEN not in printed.out + printed.err + out.read_text()
    assert "chat" in printed.out and "cold" in printed.out


def test_a_label_the_disk_contradicts_is_reported_as_the_disk_saw_it(world: World,
                                                                      tmp_path: Path) -> None:
    class NoEvict(FakeHost):
        def evict(self, files, how: str) -> None:       # ZFS under fadvise: nothing leaves
            pass

    world.cached.add("chat")
    out = tmp_path / "p.jsonl"
    client = httpx.Client(transport=httpx.MockTransport(world.handler))
    code = lp.main(["--rage", "http://rage:30437", "--config", str(world.config),
                    "--token-file", str(world.token_file), "--models", "chat",
                    "--cache", "cold", "--reps", "1", "--skip-raw", "--out", str(out)],
                   client=client, host=NoEvict(world), clock=world.clock, sleep=world.sleep)
    assert code == lp.EXIT_OK
    trial = [r for r in _records(out) if r["kind"] == "trial"][0]
    assert trial["cache_wanted"] == "cold" and trial["cache_seen"] == "warm"
    assert lp.summarise(_records(out))[0]["cache"] == "warm"


def test_a_load_that_does_not_fit_is_recorded_and_the_run_goes_on(world: World,
                                                                  tmp_path: Path) -> None:
    world.refuse.add("madlad")
    out = tmp_path / "p.jsonl"
    code = _run(world, "--models", "madlad,bge-m3", "--reps", "1", "--skip-raw", out=out)
    assert code == lp.EXIT_OK
    trials = [r for r in _records(out) if r["kind"] == "trial"]
    refused = [r for r in trials if r["model"] == "madlad"]
    assert all(r["status"] == 503 and "1125 free" in r["refused"] for r in refused)
    assert all(r["load_s"] is None and r["unload_s"] is None for r in refused)
    assert all(r["load_s"] is not None for r in trials if r["model"] == "bge-m3")


def test_a_model_in_use_refuses_the_run_before_anything_moves(world: World, capsys) -> None:
    world.loaded["chat"] = "xpu"
    world.idle["chat"] = 5
    assert _run(world, "--models", "chat", "--skip-raw") == lp.EXIT_REFUSED
    assert world.events == []
    assert "louise node pause" in capsys.readouterr().err


def test_dry_run_touches_nothing_and_needs_no_token(world: World, capsys) -> None:
    def never(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"dry run sent {request.method} {request.url}")

    code = lp.main(["--rage", "http://rage:30437", "--config", str(world.config),
                    "--models", "chat", "--busy", "bge-m3", "--reps", "2", "--dry-run"],
                   client=httpx.Client(transport=httpx.MockTransport(never)),
                   host=FakeHost(world), clock=world.clock, sleep=world.sleep)
    assert code == lp.EXIT_OK
    assert "8 trials" in capsys.readouterr().out          # idle and busy, cold and warm, 2 reps


def test_unreachable_rage_is_a_deferral(world: World) -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    code = lp.main(["--rage", "http://rage:30437", "--config", str(world.config),
                    "--token-file", str(world.token_file), "--skip-raw"],
                   client=httpx.Client(transport=httpx.MockTransport(down)),
                   host=FakeHost(world), clock=world.clock, sleep=world.sleep)
    assert code == lp.EXIT_DEFERRED


def test_a_wrong_token_is_refused_and_named(world: World, capsys) -> None:
    world.token_file.write_text("wrong\n")
    assert _run(world, "--models", "chat", "--skip-raw") == lp.EXIT_REFUSED
    err = capsys.readouterr().err
    assert "admin token" in err and "wrong" not in err


def test_drop_caches_without_the_right_to_is_refused(world: World) -> None:
    world.can_drop = False
    assert _run(world, "--cold-by", "drop-caches", "--skip-raw") == lp.EXIT_REFUSED
    assert world.events == []


def test_fadvise_on_zfs_says_it_will_not_make_a_cold_trial(world: World, capsys) -> None:
    world.fs = "zfs"
    _run(world, "--models", "bge-m3", "--reps", "1", "--skip-raw", "--cache", "warm")
    assert "ARC" not in capsys.readouterr().err           # no cold trial asked: nothing to say
    _run(world, "--models", "bge-m3", "--reps", "1", "--skip-raw")
    assert "primarycache=metadata" in capsys.readouterr().err


def test_an_interruption_puts_rage_back_and_exits_130(world: World, tmp_path: Path) -> None:
    world.loaded["chat"] = "xpu"
    world.interrupt_on = "bge-m3"
    code = _run(world, "--models", "chat,bge-m3", "--reps", "1", "--skip-raw",
                out=tmp_path / "p.jsonl")
    assert code == lp.EXIT_INTERRUPTED
    assert world.loaded == {"chat": "xpu"}
    assert len([r for r in _records(tmp_path / "p.jsonl") if r["kind"] == "trial"]) == 2


def test_beside_an_embedding_the_load_costs_it_seconds(world: World, tmp_path: Path) -> None:
    background = FakeBackground(world)
    out = tmp_path / "p.jsonl"
    code = _run(world, "--models", "chat", "--busy", "bge-m3", "--reps", "1",
                "--skip-raw", "--window", "20", background=background, out=out)
    assert code == lp.EXIT_OK
    trials = [r for r in _records(out) if r["kind"] == "trial"]
    idle = [r for r in trials if r["background"] == "idle"]
    busy = {r["cache_wanted"]: r for r in trials if r["background"] == "busy:bge-m3"}
    assert len(idle) == 2 and all(r["busy"] is None for r in idle)
    assert busy["cold"]["busy"]["per_s_quiet"] == pytest.approx(4.0, abs=0.1)
    # 6 s of load at 1/s instead of 4/s: 18 answers, 4.5 s of embedding
    assert busy["cold"]["busy"]["embedding_lost_s"] == pytest.approx(4.5, abs=0.3)
    assert busy["warm"]["busy"]["embedding_lost_s"] == pytest.approx(1.1, abs=0.3)
    assert background.stopped
    assert "bge-m3" not in world.loaded                   # not loaded at the start: unloaded


def test_the_raw_phase_reads_cold_then_warm_and_hashes(world: World, tmp_path: Path) -> None:
    out = tmp_path / "p.jsonl"
    assert _run(world, "--models", "madlad", "--reps", "1", out=out) == lp.EXIT_OK
    raw = [r for r in _records(out) if r["kind"] == "raw"]
    assert len(raw) == 1
    assert raw[0]["first_read_seen"] == "cold" and raw[0]["first_read_mib"] == 1.0
    assert {"warm_read_s", "sha256_s"} <= set(raw[0])


def test_the_embedding_loop_keeps_requests_in_flight_and_stops() -> None:
    seen: list[str] = []
    lock = threading.Lock()

    def answer(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        with lock:
            seen.append(body["input"][0])
        assert request.headers["Louise-Level"] == "corpus"
        assert request.url.path == "/upstream/bge-m3/v1/embeddings"
        time.sleep(0.005)
        return httpx.Response(200, json={"data": [{"embedding": [0.1]}]})

    loop = lp.EmbeddingLoop("http://rage:30437", "bge-m3", concurrency=2, chars=300,
                            client=httpx.Client(transport=httpx.MockTransport(answer)))
    loop.start()
    time.sleep(0.2)
    loop.stop()
    n = len(loop.completions())
    assert n > 5 and loop.errors == 0
    assert len(set(seen)) == len(seen)                    # no text twice: no cache serves it
    time.sleep(0.05)
    assert len(loop.completions()) == n                   # stopped means stopped
