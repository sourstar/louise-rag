"""The card's clock, held at its maximum while a person waits.

WHY. Chat decodes token by token and leaves the card idle between steps,
so alone on the card it lets the Arc drop its clock: 87-92 s a call at a
mean of 990 MHz, against 64 s with `min_freq` held at `rp0_freq`
(docs/notes/scheduling.md §12, measured 2026-09-20).

WHO HOLDS IT. Two callers, one mechanism:
- the GPU governor app (app/gpu_governor.py), which follows the node's
  interactive claim;
- RAGE's front (app/rage/clock.py), which follows the requests it proxies
  (docs/notes/rage.md §11), and replaces the governor app when it is live.

This module is the whole of what writes the host's sysfs, and it imports
nothing of the platform: RAGE's image carries no manifest layer, and must
not need one to hold a clock.

RESTORED ON EXIT. `release()` puts the rest value back. A holder killed
while pinning leaves the card pinned, so the next one takes a `min_freq`
already at the maximum to be that, and rests at the hardware floor
(`rpn_freq`).
"""
from __future__ import annotations

from pathlib import Path


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
