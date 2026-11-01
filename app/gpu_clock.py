"""The card's clock, held off the floor while a person waits.

WHY. Chat decodes token by token and leaves the card idle between steps,
so alone on the card it lets the Arc drop its clock: 87-92 s a call at a
mean of 990 MHz, against 64 s with `min_freq` held at `rp0_freq`
(docs/notes/scheduling.md §12, measured 2026-09-20).

HOW HIGH. Half the card's own maximum since 2026-09-23 (`HOLD_FRACTION`),
by operator decision — the sag was the cost, not the peak, and holding the
top meant the card sat at full clock through every gap between batches.
Expressed as a FRACTION of what the card reports, so the rule carries to
any GPU without naming one.

WHO HOLDS IT. Two callers, one mechanism:
- the GPU governor app (app/gpu_governor.py), which follows the node's
  interactive claim;
- The model pool's front, which followed the requests it proxied (model-pool.md@c572c3b
  §11) until it retired on 2026-10-09; its code went on 2026-10-18.

This module and app/gpu_level.py (the profile's GPU level: power limit,
`max_freq`, fan) are the whole of what writes the host's sysfs, and neither
imports anything of the platform.

UNDER THE CAP. The driver refuses `min_freq > max_freq`, so the hold is
clamped to the ceiling in force (`cap`: the `max_freq` read at start, then
whatever app/gpu_level.py writes). A cap raised while the hold is on lifts
the hold with it at the next step; a cap BELOW the hold is not written
while the hold is on (gpu_level waits for it to end).

RESTORED ON EXIT. `release()` puts the rest value back. A holder killed
while pinning leaves the card pinned, so the next one takes a `min_freq`
already at the maximum to be that, and rests at the hardware floor
(`rpn_freq`).
"""
from __future__ import annotations

from pathlib import Path


#: What share of the card's OWN maximum to hold while a person waits
#: (operator, 2026-09-23: "pin it to half of the maximum frequency
#: instead"). A fraction, never a frequency: `rp0_freq` and `rpn_freq` are
#: the card's own numbers, so this is the same instruction on any GPU and
#: no vendor's clock range is written down anywhere (operator, 2026-09-20:
#: vendor independence).
#:
#: WHY HALF. Holding the maximum was bought to stop chat's token-by-token
#: decode letting the clock sag (87-92 s a call at ~990 MHz against 64 s
#: pinned). What it costs is the card sitting at full clock through every
#: gap between batches, which is most of the time. Half the maximum keeps
#: the clock off the floor — which is what the sag was — without paying
#: for the peak while nothing is in flight.
HOLD_FRACTION = 0.5


class Governor:
    def __init__(self, freq_dir: Path) -> None:
        self.dir = freq_dir
        self.max = self._read("rp0_freq")
        self.floor = self._read("rpn_freq")
        # never below the hardware floor, and never above the maximum: a
        # card whose range is narrow enough that half its top is under its
        # own floor simply gets the floor.
        self.hold = max(self.floor, min(self.max, int(self.max * HOLD_FRACTION)))
        # the ceiling in force: `max_freq` as found (a card without the file
        # has its maximum), lowered or raised later by app/gpu_level.py
        try:
            self.cap = self._read("max_freq")
        except (OSError, ValueError):
            self.cap = self.max
        found = self._read("min_freq")
        # A holder killed while pinning leaves min_freq at `hold` (or at the
        # cap it was clamped to); taking that as the rest value would pin the
        # card for ever. Compare against what WE would have written, not
        # against the maximum.
        self.rest = self.floor if found >= self.held() else found
        self.pinned = False
        self.written: int | None = None      # the min_freq this holder last wrote
        self.error: str | None = None

    def _read(self, name: str) -> int:
        return int((self.dir / name).read_text().strip())

    def _set_min(self, mhz: int) -> None:
        (self.dir / "min_freq").write_text(str(mhz))

    def held(self) -> int:
        """The hold as it can be written now: never above the cap, never
        below the floor."""
        return max(self.floor, min(self.hold, self.cap))

    def current_min(self) -> int:
        """`min_freq` as this holder left it: the hold while pinned, else the
        rest value."""
        return self.written if self.pinned and self.written is not None else self.rest

    def set_cap(self, mhz: int) -> None:
        """app/gpu_level.py wrote `max_freq`; the next step follows it."""
        self.cap = mhz

    def step(self, interactive: bool) -> bool:
        """Pin or release to match the claim — and, while pinned, follow a
        cap that moved. True when the clock changed."""
        want = self.held() if interactive else self.rest
        if interactive == self.pinned and (not interactive or want == self.written):
            return False
        try:
            self._set_min(want)
        except OSError as e:
            self.error = f"cannot write min_freq: {type(e).__name__}: {e}"
            return False
        self.pinned, self.error = interactive, None
        self.written = want if interactive else None
        return True

    def release(self) -> None:
        if self.pinned:
            self.step(False)

    def status(self) -> dict:
        return {"pinned": self.pinned, "rest_mhz": self.rest, "max_mhz": self.max,
                "hold_mhz": self.held(), "cap_mhz": self.cap,
                "error": self.error}
