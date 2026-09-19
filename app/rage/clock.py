"""The card's clock, by the level of the requests in flight.

docs/notes/rage.md §11; docs/09-formats.md §9.12, *the request's level*.
Every request a Louise node makes to a model carries `Louise-Level`. While
one at `translate` or above is in flight a person is waiting, and the front
holds the card's `min_freq` at its maximum (app/gpu_clock.py says why: a
chat call alone on the card took 87-92 s, and 64 s with the clock held).

This replaces the claim in `state/node.sqlite` and the governor app that
read it: RAGE sees every model request itself, so nothing needs to be
written anywhere for it to know that someone waits.

ADVISORY, AS THE SPEC SAYS. The level orders nothing and no answer depends
on it; a request without the header is background work. ABSENT IS ALLOWED:
with no frequency directory mounted the front works and says so.
"""
from __future__ import annotations

import threading
from pathlib import Path

from ..gpu_clock import Governor

HEADER = "Louise-Level"
LEVELS = ("corpus", "translate", "chat", "rerank", "query")     # lowest first
INTERACTIVE = frozenset(LEVELS[1:])


def is_interactive(level: str | None) -> bool:
    return (level or "").strip().lower() in INTERACTIVE


class Clock:
    def __init__(self, freq_dir: Path | None) -> None:
        self._lock = threading.Lock()
        self._waiting = 0
        self._gov: Governor | None = None
        self._absent: str | None = None
        if freq_dir is None:
            self._absent = "no frequency directory configured (RAGE_FREQ_DIR)"
            return
        try:
            self._gov = Governor(Path(freq_dir))
        except (OSError, ValueError) as e:
            self._absent = f"no frequency files at {freq_dir}: {type(e).__name__}: {e}"

    def enter(self) -> None:
        with self._lock:
            self._waiting += 1
            if self._waiting == 1 and self._gov is not None:
                self._gov.step(True)

    def leave(self) -> None:
        with self._lock:
            self._waiting = max(0, self._waiting - 1)
            if self._waiting == 0 and self._gov is not None:
                self._gov.step(False)

    def close(self) -> None:
        """Give the clock back, whatever is in flight: the process is stopping."""
        with self._lock:
            self._waiting = 0
            if self._gov is not None:
                self._gov.release()

    def status(self) -> dict:
        with self._lock:
            if self._gov is None:
                return {"running": False, "error": self._absent, "waiting": self._waiting}
            return {"running": True, "waiting": self._waiting} | self._gov.status()
