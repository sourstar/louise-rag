"""The worker's record of the card, second by second: how busy it was, who
was served, who was waiting.

Instruments I1–I3 of docs/notes/gpu-saturation-plan.md, joined:
- I1, the card: the GPU probe's busy samples (GET /busy, app/gpu_busy.py),
  fetched every FLUSH_S;
- I2, who used it: each encoder gate's seconds held per collection
  (`dispatch.Gate.snapshot()["plugins"]`), the requests still running
  included, sampled every TICK_S — request-seconds: four requests running
  through one second hold four;
- I3, the gaps: whether a collection had work it could send now
  (`stats.Recorder.feeders()`, set by the embed feeders) and whether a
  person's query held the node's claim (`dispatch.interactive_active`).

ONE LINE A SECOND, JSON, in `<root>/state/card/<UTC date>.jsonl`:

    {"t": 1730000000, "q": 0,
     "cards": {"0000:03:00.0": 0.82},
     "gates": {"http://localai:8080": {"running": 4, "waiting": 1,
                                       "served": {"wikidata": 0.97}}},
     "feed": {"wikidata": {"in": 4, "backlog": 1}}}

`cards` is None for a second the probe never answered for (LATE_S). Only
gates and feeders with something to say are written. KEEP_DAYS of files are
kept; at the node's ~300 bytes a line that is about 80 MB at most.

A GAP is a second the card was idle (busy under IDLE_BELOW) while some
collection had work waiting and no query held the claim: the engine had
work and did not send it. LOST is the card time those seconds left unused
— Σ (1 − busy) over the seconds with work waiting — which counts a card at
60 % with work waiting as 0.4 s lost. Seconds a query held the claim are
counted apart: corpus work stands aside then by design (step 4).

THE STANDARD THIS DOES NOT USE: a metrics time series is what Prometheus
(OpenMetrics exposition, a scrape every second, PromQL for the windows)
does. The node runs no Prometheus, and adopting it means a server, its
storage and its retention for one instrument; this file and
`summarize` are the case solved simply. Exposing the same figures at a
/metrics route would let a Prometheus scrape them unchanged when one exists.

Telemetry, never desired state (invariant 10); its thread is not a
pipeline thread and never touches a manifest.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable, Iterator

from .. import config, gpu, stats
from . import dispatch

TICK_S = 1.0
#: how often the probe is asked for its samples, and the file appended
FLUSH_S = 15.0
#: a second the probe has not answered for after this is written without
#: its card figure (a probe that is down never stops the record)
LATE_S = 45.0
KEEP_DAYS = 3
#: a card under this share is idle
IDLE_BELOW = 0.05


def directory() -> Path:
    return config.louise_root() / "state" / "card"


def _day(sec: int) -> str:
    return datetime.fromtimestamp(sec, timezone.utc).strftime("%Y-%m-%d")


class CardRecorder:
    """Samples the worker's side every TICK_S, joins the probe's card
    samples every FLUSH_S, appends the joined seconds. Every source is a
    parameter so the tests drive fakes."""

    def __init__(self, probe_url: str, out_dir: Path, *,
                 gates: Callable[[], dict] = dispatch.snapshot,
                 feeders: Callable[[], dict] | None = None,
                 claim: Callable[[], bool] = dispatch.interactive_active,
                 fetch: Callable[[str, float | None], dict] = gpu.busy_since,
                 wall: Callable[[], float] = time.time,
                 keep_days: int = KEEP_DAYS) -> None:
        self.url = probe_url
        self.out_dir = Path(out_dir)
        self._gates = gates
        self._feeders = feeders or (lambda: stats.recorder().feeders())
        self._claim = claim
        self._fetch = fetch
        self._wall = wall
        self._keep_days = keep_days
        self._held: dict[tuple[str, str], float] = {}
        self._pending: dict[int, dict] = {}
        self._cards: dict[int, dict[str, float]] = {}
        self._since: float | None = None
        self._pruned_day: str | None = None
        self.probe_error: str | None = None

    # ---------------------------------------------------------- the worker's side

    def tick(self) -> None:
        sec = int(self._wall())
        gates: dict[str, dict] = {}
        for url, g in self._gates().items():
            served: dict[str, float] = {}
            for plugin, entry in (g.get("plugins") or {}).items():
                held = float(entry.get("held_s", 0.0))
                before = self._held.get((url, plugin))
                self._held[(url, plugin)] = held
                if before is not None and held > before:
                    served[plugin or "-"] = held - before
            if g.get("busy") or g.get("waiting") or served:
                gates[url] = {"running": int(g.get("busy") or 0),
                              "waiting": int(g.get("waiting") or 0), "served": served}
        feed = {name: {"in": int(f.get("in_flight") or 0), "backlog": int(bool(f.get("backlog")))}
                for name, f in self._feeders().items()
                if f.get("in_flight") or f.get("backlog")}
        try:
            query = int(bool(self._claim()))
        except Exception:   # noqa: BLE001 — an unreadable claim holds nothing
            query = 0
        row = {"t": sec, "q": query, "gates": gates, "feed": feed}
        was = self._pending.get(sec)
        self._pending[sec] = row if was is None else _merge(was, row)

    # ---------------------------------------------------------- the card's side

    def _take_cards(self, now: float) -> None:
        since = self._since if self._since is not None else now - 2 * FLUSH_S
        try:
            answer = self._fetch(self.url, since)
            self.probe_error = None
        except Exception as e:   # noqa: BLE001 — the record goes on without the card
            self.probe_error = f"{type(e).__name__}: {e}"
            return
        for pdev, samples in (answer.get("cards") or {}).items():
            for s in samples:
                t = float(s.get("t", 0.0))
                self._since = t if self._since is None else max(self._since, t)
                if s.get("dt") is None or s.get("busy") is None:
                    continue            # a baseline, or a card the probe cannot see: no figure
                self._cards.setdefault(int(t), {})[pdev] = float(s["busy"])

    def flush(self, *, final: bool = False) -> int:
        """Join and append every second whose card figure is in (or late);
        with `final`, every pending second. Returns the lines written."""
        now = self._wall()
        self._take_cards(now)
        newest = max(self._cards, default=None)
        ready = sorted(sec for sec in self._pending
                       if final or (newest is not None and sec <= newest)
                       or now - sec > LATE_S)
        lines: dict[str, list[str]] = {}
        for sec in ready:
            row = self._pending.pop(sec)
            cards = self._cards.get(sec)
            out = {"t": row["t"], "q": row["q"], "cards": cards}
            if row["gates"]:
                out["gates"] = {u: {**g, "served": {p: round(v, 3) for p, v in g["served"].items()}}
                                for u, g in row["gates"].items()}
            if row["feed"]:
                out["feed"] = row["feed"]
            lines.setdefault(_day(sec), []).append(json.dumps(out, separators=(",", ":")))
        oldest_kept = min(self._pending, default=int(now)) - int(LATE_S) - 1
        for sec in [s for s in self._cards if s < oldest_kept]:
            del self._cards[sec]
        if lines:
            self.out_dir.mkdir(parents=True, exist_ok=True)
            for day, rows in lines.items():
                with open(self.out_dir / f"{day}.jsonl", "a", encoding="utf-8") as fh:
                    fh.write("\n".join(rows) + "\n")
        self._prune(now)
        return sum(len(r) for r in lines.values())

    def _prune(self, now: float) -> None:
        today = _day(int(now))
        if self._pruned_day == today:
            return
        self._pruned_day = today
        cutoff = (datetime.fromtimestamp(now, timezone.utc)
                  - timedelta(days=self._keep_days)).strftime("%Y-%m-%d")
        try:
            files = list(self.out_dir.glob("*.jsonl"))
        except OSError:
            return
        for f in files:
            if f.stem < cutoff:
                try:
                    f.unlink()
                except OSError:
                    pass

    def run(self, stop: threading.Event) -> None:
        """Tick every TICK_S on a fixed beat, flush every FLUSH_S, and once
        more on the way out."""
        next_t = time.monotonic()
        last_flush = next_t
        while not stop.is_set():
            try:
                self.tick()
                if time.monotonic() - last_flush >= FLUSH_S:
                    last_flush = time.monotonic()
                    self.flush()
            except Exception:   # noqa: BLE001 — telemetry never ends the worker
                pass
            next_t += TICK_S
            now = time.monotonic()
            if next_t <= now:
                next_t = now + TICK_S
            stop.wait(next_t - now)
        try:
            self.flush(final=True)
        except Exception:   # noqa: BLE001
            pass


def _merge(a: dict, b: dict) -> dict:
    """Two ticks that fell in one second: served adds up, the rest takes
    the larger."""
    gates = {u: dict(g, served=dict(g["served"])) for u, g in a["gates"].items()}
    for url, g in b["gates"].items():
        if url not in gates:
            gates[url] = dict(g, served=dict(g["served"]))
            continue
        mine = gates[url]
        mine["running"] = max(mine["running"], g["running"])
        mine["waiting"] = max(mine["waiting"], g["waiting"])
        for p, v in g["served"].items():
            mine["served"][p] = mine["served"].get(p, 0.0) + v
    feed = {n: dict(f) for n, f in a["feed"].items()}
    for name, f in b["feed"].items():
        mine = feed.setdefault(name, {"in": 0, "backlog": 0})
        mine["in"] = max(mine["in"], f["in"])
        mine["backlog"] = max(mine["backlog"], f["backlog"])
    return {"t": a["t"], "q": max(a["q"], b["q"]), "gates": gates, "feed": feed}


# ------------------------------------------------------------------ reading

def read(since: float, until: float | None = None, out_dir: Path | None = None) -> Iterator[dict]:
    """The recorded seconds in [since, until], oldest first. A torn last
    line (the worker stopped mid-write) is skipped."""
    out_dir = out_dir or directory()
    until = until if until is not None else time.time()
    first, last = _day(int(since)), _day(int(until))
    try:
        files = sorted(f for f in out_dir.glob("*.jsonl") if first <= f.stem <= last)
    except OSError:
        return
    for f in files:
        try:
            fh = open(f, encoding="utf-8")
        except OSError:
            continue
        with fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if since <= row.get("t", 0) <= until:
                    yield row


@dataclass
class Window:
    """A stretch of seconds, summed."""
    start: int
    seconds: int = 0
    card_seconds: int = 0            # seconds with a card figure
    busy_sum: float = 0.0
    query: int = 0                   # seconds a query held the claim
    waiting: int = 0                 # seconds with work waiting, no query
    gaps: int = 0                    # of those, the card idle
    lost: float = 0.0                # of those, Σ (1 − busy)
    served: dict[str, float] = field(default_factory=dict)   # collection -> seconds held

    @property
    def busy(self) -> float | None:
        return self.busy_sum / self.card_seconds if self.card_seconds else None


def summarize(rows: Iterable[dict], *, minute: bool = True,
              card: str | None = None) -> list[Window]:
    """Per minute (or one window over everything): the card's mean busy,
    the seconds a query held the claim, the seconds with work waiting and,
    of those, the gaps and the lost card time; each collection's seconds
    held across the encoder gates. `card` picks one card where several
    report (default: the busiest of each second)."""
    out: dict[int, Window] = {}
    for row in rows:
        t = int(row["t"])
        key = t - t % 60 if minute else 0
        w = out.get(key)
        if w is None:
            w = out[key] = Window(start=t - t % 60 if minute else t)
        w.seconds += 1
        cards = row.get("cards")
        busy = None
        if cards:
            busy = cards.get(card) if card is not None else max(cards.values())
        if busy is not None:
            w.card_seconds += 1
            w.busy_sum += busy
        backlog = any(f.get("backlog") for f in (row.get("feed") or {}).values())
        if row.get("q"):
            w.query += 1
        elif backlog:
            w.waiting += 1
            if busy is not None:
                w.gaps += busy < IDLE_BELOW
                w.lost += 1.0 - busy
        for g in (row.get("gates") or {}).values():
            for plugin, s in g.get("served", {}).items():
                w.served[plugin] = w.served.get(plugin, 0.0) + s
    return [out[k] for k in sorted(out)]
