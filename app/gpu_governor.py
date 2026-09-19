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
card's frequency directory bind-mounted writable, the node store's
directory, no network. Neither the LAN-facing web nor the worker, which
parses untrusted data, may write the host's sysfs.

ABSENT IS ALLOWED. Without it everything works and chat is slower on an
idle card; the System tab says so (app/gpu.py `governor`). It publishes
its state to the node store every GOVERNOR_HEARTBEAT_S and whenever the
clock changes, so a stale heartbeat reads as "not running".

RESTORED ON EXIT. SIGTERM puts the rest value back. A governor killed while
pinning leaves the card pinned, so the next one takes a `min_freq` already
at the maximum to be that, and rests at the hardware floor (`rpn_freq`).
"""
from __future__ import annotations

import signal
import sys
import threading
import time
from contextlib import closing
from pathlib import Path

from . import config, db, dispatch, node_state

STATUS_KEY = "gpu_governor"


class Governor:
    def __init__(self, freq_dir: Path) -> None:
        self.dir = freq_dir
        self.max = self._read("rp0_freq")
        self.floor = self._read("rpn_freq")
        found = self._read("min_freq")
        self.rest = self.floor if found >= self.max else found
        self.pinned = False
        self.error: str | None = None

    def _read(self, name: str) -> int:
        return int((self.dir / name).read_text().strip())

    def _set_min(self, mhz: int) -> None:
        (self.dir / "min_freq").write_text(str(mhz))

    def step(self, interactive: bool) -> bool:
        """Pin or release to match the claim. True when the clock changed."""
        if interactive == self.pinned:
            return False
        try:
            self._set_min(self.max if interactive else self.rest)
        except OSError as e:
            self.error = f"cannot write min_freq: {type(e).__name__}: {e}"
            return False
        self.pinned, self.error = interactive, None
        return True

    def release(self) -> None:
        if self.pinned:
            self.step(False)

    def status(self) -> dict:
        return {"pinned": self.pinned, "rest_mhz": self.rest, "max_mhz": self.max,
                "error": self.error}


def _publish(payload: dict) -> None:
    """Telemetry into the node store; never raises — a governor that cannot
    report still governs."""
    try:
        with closing(node_state.connect()) as con:
            db._publish_sample(con, STATUS_KEY, payload)
    except Exception:   # noqa: BLE001
        pass


def run(stop: threading.Event, freq_dir: Path | None = None) -> None:
    freq_dir = Path(freq_dir or config.GOVERNOR_FREQ_DIR)
    gov: Governor | None = None
    last_beat = 0.0
    try:
        while not stop.is_set():
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
            changed = gov.step(dispatch.interactive_active())
            now = time.monotonic()
            if changed or now - last_beat >= config.GOVERNOR_HEARTBEAT_S:
                _publish(gov.status())
                last_beat = now
            stop.wait(config.GOVERNOR_POLL_S)
    finally:
        if gov is not None:
            gov.release()
            _publish(gov.status() | {"stopped": True})


def main() -> int:
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    run(stop)
    return 0


if __name__ == "__main__":
    sys.exit(main())
