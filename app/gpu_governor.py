"""The GPU governor — docs/notes/scheduling.md §12.

WHY. Chat decodes token by token and leaves the card idle between steps,
so alone on the card it lets the Arc drop its clock. Measured live
(2026-09-20): one chat call took 87-92 s at a mean of 990 MHz, and 64 s
with the card's minimum frequency pinned at its maximum. While corpus work
runs the clock is already up, so pinning matters exactly when a person is
waiting and nothing else is using the GPU.

WHAT. While the node's interactive claim is live (app/dispatch.py: held
from the first interactive call's start to the last one's end), the card's
`min_freq` is set to its `rp0_freq`. When the claim clears it goes back to
what it was. Polled every GOVERNOR_POLL_S; a chat call lasts a minute, so a
quarter-second lag costs nothing.

WHERE. A TrueNAS app of its own (deploy/truenas-gpu-governor.yaml): the
card's frequency directory and its hwmon directory bind-mounted writable,
the node store's directory, the config directory read-only, no network. Neither the LAN-facing web nor the worker, which
parses untrusted data, may write the host's sysfs.

ABSENT IS ALLOWED. Without it everything works and chat is slower on an
idle card; the System tab says so (app/gpu.py `governor`). It publishes
its state to the node store every GOVERNOR_HEARTBEAT_S and whenever the
clock changes, so a stale heartbeat reads as "not running".

RESTORED ON EXIT. SIGTERM puts the rest value back. A governor killed while
pinning leaves the card pinned, so the next one takes a `min_freq` already
at the maximum to be that, and rests at the hardware floor (`rpn_freq`).

THE PROFILE'S GPU LEVEL (docs/notes/node-profiles-design.md, 2026-10-27).
Every tick it also takes the level platform.toml's [schedule] puts in force
now (app/node_profile.py) and applies a difference (app/gpu_level.py): the
hwmon power limit where writable, else `max_freq`, never under the query
hold; the card's own limits back on a clean stop. platform.toml is re-read
only when the file or the minute changes; a file that does not load is
logged once and the last level kept. No [schedule]: nothing is written.
A limit changes how fast a vector comes, never its bytes. It retires the
root cron deploy/scheduled/gpu-cap.sh.

THE CARD'S HISTORY. Every GOVERNOR_SAMPLE_S it also records the card's fan,
temperatures, average power since the last sample, its clock at that
instant (0 when idle) and whether the claim was live, in `gpu_samples` in
the node store, keeping GOVERNOR_HISTORY_DAYS (a week at 10 s is 3.6 MB,
and pruning on write keeps it there). Nothing shows it yet: the System tab
shows only the live reading, and the history is kept for a widget to come
(operator, 2026-09-19). The xe driver's known issue is a fan that stays
high after a workload, until a GPU process restarts; in the history it is
a high fan on a cool, idle card. The
fan cannot be set from here (xe exposes it read-only); the workaround,
restarting an engine, is not this app's: the engines are LocalAI's
(operator, 2026-09-20: never restart other apps).
"""
from __future__ import annotations

import signal
import sys
import threading
import time
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from . import config, db, dispatch, gpu, gpu_level, node_state
# The clock itself lives in app/gpu_clock.py, which imports nothing of the
# platform (RAGE's front held it too, docs/notes/rage.md §11). It is
# named here because this app, and its tests, know it as the governor's.
from .gpu_clock import Governor  # noqa: F401

STATUS_KEY = "gpu_governor"
SAMPLES_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS gpu_samples (ts INTEGER NOT NULL, fan_rpm INTEGER, "
    "temp_pkg REAL, temp_vram REAL, power_w REAL, act_mhz INTEGER, "
    "interactive INTEGER NOT NULL)",
    "CREATE INDEX IF NOT EXISTS idx_gpu_samples_ts ON gpu_samples(ts)",
)


def xe_hwmon(drm_dir: Path) -> Path | None:
    """The first card whose hwmon is the xe driver's — found each time,
    because both the card's and the hwmon's numbers change across boots."""
    for dev in sorted(drm_dir.glob("card*/device")):
        h = gpu.hwmon_dir(dev)
        try:
            if h is not None and (h / "name").read_text().strip() == "xe":
                return h
        except OSError:
            continue
    return None


class Sampler:
    """One row of the card's history every GOVERNOR_SAMPLE_S."""

    def __init__(self, drm_dir: Path, freq_dir: Path) -> None:
        self.drm_dir, self.freq_dir = drm_dir, freq_dir
        self.last: tuple[float, int] | None = None     # (monotonic, energy µJ)

    def row(self, interactive: bool) -> tuple:
        hwmon = xe_hwmon(self.drm_dir)
        now, energy = time.monotonic(), gpu.energy_uj(hwmon)
        power = None
        if energy is not None and self.last is not None and energy >= self.last[1] \
                and now > self.last[0]:
            power = (energy - self.last[1]) / (now - self.last[0]) / 1e6
        self.last = None if energy is None else (now, energy)
        s = gpu.read_sensors(hwmon) or gpu.GpuSensors()
        act = gpu._num(self.freq_dir / "act_freq")
        return (int(time.time()), s.fans[0] if s.fans else None, s.temp("pkg"),
                s.temp("vram"), power, act, int(interactive))

    def record(self, interactive: bool) -> None:
        """Never raises: a history that cannot be written is a gap in it."""
        try:
            row = self.row(interactive)
            with closing(node_state.connect()) as con:
                for stmt in SAMPLES_SCHEMA:
                    con.execute(stmt)
                store(con, row)
        except Exception:   # noqa: BLE001
            pass


def store(con, row: tuple) -> None:
    """One sample in, and everything older than GOVERNOR_HISTORY_DAYS out.
    Pruning on every write is what bounds the file: SQLite reuses the freed
    pages, so the store stops growing once it holds a week."""
    con.execute("INSERT INTO gpu_samples VALUES (?,?,?,?,?,?,?)", row)
    con.execute("DELETE FROM gpu_samples WHERE ts < ?",
                (row[0] - int(config.GOVERNOR_HISTORY_DAYS * 86400),))



def _publish(payload: dict) -> None:
    """Telemetry into the node store; never raises — a governor that cannot
    report still governs."""
    try:
        with closing(node_state.connect()) as con:
            db._publish_sample(con, STATUS_KEY, payload)
    except Exception:   # noqa: BLE001
        pass


def _log(message: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} [gpu-governor] {message}", flush=True)


class DesiredLevel:
    """platform.toml's GPU level for this minute. The loop asks every tick;
    the file is parsed again only when it or the minute changed (a stat per
    tick, keyed on the file's path and identity), so a 0.25 s poll does not
    parse TOML four times a second. Raises what the file's load raises."""

    def __init__(self) -> None:
        self._key: tuple | None = None
        self._value: tuple[Any, BaseException | None] = (None, None)

    def get(self, now: datetime):
        from . import node_profile, platform_config
        path = platform_config.platform_path()
        try:
            st = path.stat()
            ident: tuple = (str(path), st.st_ino, st.st_mtime_ns, st.st_size)
        except OSError:
            ident = (str(path), None)
        key = ident + (now.strftime("%Y-%m-%d %H:%M"),)
        if key != self._key:
            try:
                self._value = (node_profile.active(now), None)
            except Exception as e:   # noqa: BLE001 — any refusal keeps the last level
                self._value = (None, e)
            self._key = key
        value, err = self._value
        if err is not None:
            raise err
        return value


def _declared_levels() -> list:
    """The levels platform.toml declares now, for app/gpu_level.py to tell a
    level left on the card by a killed governor from the card's own."""
    from . import node_profile
    try:
        p = node_profile.load()
    except Exception:   # noqa: BLE001 — the loop reports a bad file
        return []
    return list(p.levels.values()) if p is not None else []


def run(stop: threading.Event, freq_dir: Path | None = None,
        drm_dir: Path | None = None, hwmon_dir: Path | None = None,
        clock: Callable[[], datetime] = datetime.now) -> None:
    freq_dir = Path(freq_dir or config.GOVERNOR_FREQ_DIR)
    drm_dir = Path(drm_dir or config.GOVERNOR_DRM_DIR)
    sampler = Sampler(drm_dir, freq_dir)
    gov: Governor | None = None
    level: gpu_level.LevelApplier | None = None
    desired = DesiredLevel()
    last_beat = last_sample = 0.0

    def status() -> dict:
        return gov.status() | ({"level": level.status()} if level is not None else {})
    try:
        while not stop.is_set():
            if time.monotonic() - last_sample >= config.GOVERNOR_SAMPLE_S:
                sampler.record(dispatch.interactive_active())
                last_sample = time.monotonic()
            if gov is None:
                try:
                    gov = Governor(freq_dir)
                except (OSError, ValueError) as e:
                    # the card's directory is missing or unreadable: say so,
                    # and look again at the next heartbeat
                    _publish({"pinned": False, "error":
                              f"no frequency files at {freq_dir}: {type(e).__name__}: {e}"})
                    stop.wait(config.GOVERNOR_HEARTBEAT_S)
                    continue
                hwmon = (gpu_level.find_hwmon(Path(hwmon_dir or config.GOVERNOR_HWMON_DIR))
                         or xe_hwmon(drm_dir))
                level = gpu_level.LevelApplier(freq_dir, hwmon, levels=_declared_levels(), log=_log)
            # the hold first: a lower cap waits for it to end, in this same tick
            interactive = dispatch.interactive_active()
            changed = gov.step(interactive)
            try:
                active = desired.get(clock())
            except Exception as e:   # noqa: BLE001
                changed |= level.fail(f"platform.toml: {type(e).__name__}: {e}")
            else:
                changed |= level.reconcile(active.level if active else None, gov,
                                           active.profile.name if active else None)
                # a cap raised under a live hold lifts the hold now
                changed |= gov.step(interactive)
            now = time.monotonic()
            if changed or now - last_beat >= config.GOVERNOR_HEARTBEAT_S:
                _publish(status())
                last_beat = now
            stop.wait(config.GOVERNOR_POLL_S)
    finally:
        if gov is not None:
            gov.release()
            if level is not None:
                level.restore(gov)
            _publish(status() | {"stopped": True})


def main() -> int:
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    run(stop)
    return 0


if __name__ == "__main__":
    sys.exit(main())
