"""Fair-share scheduling across collections — the resource side of
docs/scheduling.md §10.

WHAT THIS OWNS. Which collection gets a contended resource next, and for
how long. It owns nothing else: it does not know what the work IS, it
never touches a manifest, and it never blocks. A caller asks for a turn,
does its own work, reports what it spent, and hands the turn back.

WHY DEFICIT ROUND-ROBIN. The operator's model is: every collection whose
embedding is unfinished gets an equal share of each resource it actually
needs; a collection that does not consume its slot does not bank it; a
collection may hand a resource back early, and then waits for its next
turn to get it again. Those are, line for line, the guarantees of DRR —
so this is DRR, and the pieces that looked like they needed bookkeeping
do not need any:

  * "unused capacity is redistributed" — a collection with nothing to do
    leaves the rotation, so the others simply come round faster. There is
    no loan to track and nothing to hand back.
  * "a plugin that released waits for its next turn" — re-entry appends at
    the TAIL. That is also what makes an honest early release safe: it
    cannot be used to cut back in.
  * "idle must not cost a plugin its share" — the ENTITLEMENT is the
    quantum, and the quantum is a property of the flow, not of its recent
    behaviour. Going idle resets the DEFICIT (you do not bank credit while
    away); it never reduces the quantum. Decaying the quantum of whoever
    is idle is the tempting version and it is wrong: it punishes exactly
    the collections whose input arrives in bursts.

THE QUANTUM FLOOR IS THE PLUGIN'S MINIMUM EFFICIENT UNIT, not a latency
budget. A collection whose smallest worthwhile piece of work on a resource
costs 14 s cannot be given a 5 s slot — it would overrun every turn or do
nothing at all. So each flow declares a floor and its quantum is
`max(floor, base)`. One consequence is worth stating plainly because it
cannot be tuned away: the worst-case wait for a collection re-entering the
rotation is the sum of the other flows' quanta, so it is DERIVED from
everyone's minimum efficient unit. If that wait is too long the fix is
fewer concurrent flows, or smaller units of work — never a smaller floor,
which would only make the plugins inefficient.

OVERRUN AND DEBT. A unit's cost is not known before it runs, so a turn may
end over its quantum. The overrun is charged as debt and the next turn is
correspondingly shorter — long-run fairness with no preemption, which is
the only kind available here (nothing can interrupt a Lance write or an
encoder call). The debt is CLAMPED at one quantum: a single 300 s unit
against a 5 s quantum would otherwise starve its own collection for sixty
turns. Bounded starvation beats exact fairness, and the real fix is to
bound the unit — `suggest_floor_s` in the snapshot reports what the units
actually cost, so an oversized unit is visible rather than inferred.

NOT A THREAD. No timers, no background work, no waiting: every method
returns immediately. The clock is injectable so the tests are exact.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field

# Resource names. An encoder is named per ENDPOINT, not per collection: two
# collections pointed at one GPU contend, two pointed at different
# containers do not, and the name is what tells them apart.
RES_PARSE = "parse"          # parse-pool children
RES_INDEX = "index"          # tantivy writer / vector index maintenance


def encoder(url: str) -> str:
    return f"encoder:{url}"


# How long a flow with nothing to do is left out of the rotation before it
# is re-admitted for one probing turn. Demand is announced (`demand`), not
# polled, so this is only the safety net for a signal that was never sent —
# but it must exist: a collection stranded by a missed edge would otherwise
# never run again (docs/scheduling.md §6.6 — ask what happens when the
# collection is finished, empty, or errored).
IDLE_REPROBE_S = 60.0

# Unit-cost samples kept per flow, for `suggest_floor_s`.
_SAMPLES = 64
# Below this many samples a measured floor is one document's luck.
_LEARN_MIN = 8


@dataclass
class _Flow:
    collection: str
    floor_s: float
    deficit: float = 0.0
    in_rotation: bool = False
    last_release: float = 0.0
    # accounting — the input the scheduler was missing (§10, proposal 1)
    turns: int = 0
    units: int = 0
    seconds: float = 0.0
    overruns: int = 0
    reserved_turns: int = 0
    reserved_seconds: float = 0.0
    samples: deque = field(default_factory=lambda: deque(maxlen=_SAMPLES))


class Turn:
    """One collection's slot on one resource. Not thread-safe by itself: a
    turn belongs to the thread that acquired it."""

    __slots__ = ("collection", "resource", "quantum_s", "_left", "_sched",
                 "_closed", "_worked", "reserved")

    def __init__(self, sched: "Scheduler", resource: str, collection: str,
                 quantum_s: float, left: float, reserved: bool = False):
        self._sched = sched
        self.resource = resource
        self.collection = collection
        self.quantum_s = quantum_s
        self._left = left
        self._closed = False
        self._worked = False
        # A reserved turn is query work: it is measured like any other, but
        # it neither spends the collection's deficit nor moves it in the
        # rotation. Answering a query must not cost a collection its place.
        self.reserved = reserved

    def may_continue(self) -> bool:
        """True while this turn still has budget. A turn always gets to try
        at least one unit — `acquire` never hands out a turn with no budget
        — so this is asked AFTER the first unit, never before."""
        return not self._closed and self._left > 0.0

    def spent(self, seconds: float, units: int = 1) -> None:
        self._worked = True
        self._left -= max(0.0, seconds)
        self._sched._charge(self.resource, self.collection, seconds, units,
                            over=self._left < 0.0, reserved=self.reserved)

    def release(self, *, idle: bool) -> None:
        """Hand the resource back. `idle=True` means "no more work, or I was
        deferred": the flow leaves the rotation and its deficit is reset, so
        it neither holds a slot it cannot use nor banks credit while away.
        `idle=False` means "my slot is spent": the flow goes to the tail
        carrying its debt, and runs again on its next turn. Either way
        re-entry is at the TAIL — a release is never a way to jump ahead."""
        if self._closed:
            return
        self._closed = True
        self._sched._release(self.resource, self.collection,
                             idle=idle, worked=self._worked,
                             reserved=self.reserved)

    def __enter__(self) -> "Turn":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        # An exception is a deferral, not a claim on the resource.
        self.release(idle=exc_type is not None or not self._worked)


class Scheduler:
    def __init__(self, *, base_quantum_s: float = 10.0,
                 idle_reprobe_s: float = IDLE_REPROBE_S,
                 learn_floor: bool = False, floor_cap_s: float = 60.0,
                 clock=time.monotonic) -> None:
        self._lock = threading.Lock()
        self._flows: dict[str, dict[str, _Flow]] = {}
        self._order: dict[str, deque] = {}
        self._priority: dict[str, deque] = {}
        self.base_quantum_s = base_quantum_s
        self.idle_reprobe_s = idle_reprobe_s
        self.learn_floor = learn_floor
        self.floor_cap_s = floor_cap_s
        self._clock = clock

    def configure(self, **kw) -> None:
        """Set the knobs on the process-wide scheduler at startup. Env is
        identity and TOML is desired state (CLAUDE.md tier rules); this
        holds neither — it is the runtime wiring of values that came from
        one of those."""
        with self._lock:
            for name in ("base_quantum_s", "idle_reprobe_s", "learn_floor",
                         "floor_cap_s"):
                if kw.get(name) is not None:
                    setattr(self, name, kw[name])

    # ---------------------------------------------------------------- setup

    def declare(self, resource: str, collection: str,
                floor_s: float | None = None) -> None:
        """Announce a flow, without disturbing the rotation.

        `floor_s=None` — the normal case — leaves the floor alone, which
        matters because callers re-declare their flows every pass: passing
        a number there would wipe the floor the scheduler has MEASURED
        (`_learn`) on every single pass, and the measurement would never
        take effect. Pass a number only to pin a floor deliberately."""
        with self._lock:
            self._ensure(resource, collection, floor_s)

    def _ensure(self, resource: str, collection: str,
                floor_s: float | None) -> _Flow:
        flows = self._flows.setdefault(resource, {})
        self._order.setdefault(resource, deque())
        self._priority.setdefault(resource, deque())
        flow = flows.get(collection)
        if flow is None:
            flow = _Flow(collection, max(0.0, floor_s or 0.0))
            flows[collection] = flow
        elif floor_s is not None:
            flow.floor_s = max(0.0, floor_s)
        return flow

    def forget(self, resource: str, collection: str) -> None:
        with self._lock:
            self._drop(resource, collection)

    def _drop(self, resource: str, collection: str) -> None:
        self._flows.get(resource, {}).pop(collection, None)
        for q in (self._order.get(resource), self._priority.get(resource)):
            if q is not None and collection in q:
                q.remove(collection)

    def sync(self, resource: str, collections) -> None:
        """Drop flows for collections that no longer exist."""
        live = set(collections)
        with self._lock:
            for name in [n for n in self._flows.get(resource, {}) if n not in live]:
                self._drop(resource, name)

    # --------------------------------------------------------------- demand

    def demand(self, resource: str, collection: str, *,
               floor_s: float | None = None) -> None:
        """"I have work for this resource." Pushed on the transition that
        created the work (a document became embeddable, a shard landed),
        never polled: a flow that becomes runnable just after being skipped
        must not wait for the scheduler to come round and ask."""
        with self._lock:
            flow = self._ensure(resource, collection, floor_s)
            if not flow.in_rotation:
                flow.in_rotation = True
                self._order[resource].append(collection)

    def withdraw(self, resource: str, collection: str) -> None:
        with self._lock:
            flow = self._flows.get(resource, {}).get(collection)
            if flow is not None and flow.in_rotation:
                flow.in_rotation = False
                flow.deficit = 0.0
                self._order[resource].remove(collection)

    def reserve(self, resource: str, collection: str) -> None:
        """Query-driven work (A5): served before the rotation, once. This is
        a RESERVATION, not a share — it does not spend the collection's
        quantum and does not change its place in the rotation."""
        with self._lock:
            self._ensure(resource, collection, None)
            if collection not in self._priority[resource]:
                self._priority[resource].append(collection)

    # -------------------------------------------------------------- serving

    def acquire(self, resource: str) -> Turn | None:
        """The next turn on `resource`, or None when nothing wants it.

        Never blocks and never sleeps: "nothing to do" is the caller's
        problem to idle on, because only the caller knows what else it could
        be doing instead."""
        with self._lock:
            prio = self._priority.get(resource)
            if prio:
                name = prio.popleft()
                flow = self._flows.get(resource, {}).get(name)
                quantum = self._quantum(flow) if flow else self.base_quantum_s
                if flow is not None:
                    flow.reserved_turns += 1
                return Turn(self, resource, name, quantum, quantum,
                            reserved=True)

            self._readmit_stale(resource)
            order = self._order.get(resource)
            flows = self._flows.get(resource, {})
            if not order:
                return None
            # Keep cycling until someone has positive deficit. A flow that
            # overran is paying its debt off a quantum per cycle, and a cycle
            # costs nothing — but returning None while it does would tell the
            # CALLER there is no work, and the worker would go to sleep for
            # WORKER_IDLE_SLEEP with a full queue. (A 9 s unit against a 1 s
            # quantum would have alternated work with a 5 s sleep: a third of
            # the throughput, silently.) The debt clamp bounds this to two
            # cycles; the counter is belt and braces against a zero quantum.
            attempts = 0
            limit = 3 * len(order) + 3
            while order and attempts < limit:
                attempts += 1
                name = order.popleft()
                flow = flows.get(name)
                if flow is None:
                    continue
                quantum = self._quantum(flow)
                if quantum <= 0.0:
                    order.append(name)
                    continue
                flow.deficit += quantum
                if flow.deficit <= 0.0:
                    order.append(name)      # still in debt; back to the tail
                    continue
                flow.turns += 1
                return Turn(self, resource, name, quantum, flow.deficit)
            return None

    def _readmit_stale(self, resource: str) -> None:
        """The safety net. Demand is announced, so a missed edge is
        possible; a collection stranded by one must not stop for good. Any
        flow out of the rotation for longer than the interval is put back
        for one turn, in which its caller either finds work (and stays) or
        releases idle (and drops out again).

        This runs on EVERY acquire, not only when the rotation is empty: a
        probe offered only to an idle scheduler is no safety net at all,
        because a busy collection would hold the rotation open forever and
        the stranded one would never be looked at.

        A flow that has never released (`last_release == 0`) is admitted at
        once, deliberately — at startup no transition has fired yet, and
        waiting out the interval would idle the worker after every restart.
        """
        now = self._clock()
        order = self._order.setdefault(resource, deque())
        for name, flow in self._flows.get(resource, {}).items():
            if flow.in_rotation:
                continue
            if now - flow.last_release >= self.idle_reprobe_s:
                flow.last_release = now
                flow.in_rotation = True
                order.append(name)

    def _quantum(self, flow: _Flow) -> float:
        # The floor is the plugin's minimum efficient unit; the base is the
        # fair share. A flow is never given less than it can use.
        return max(flow.floor_s, self.base_quantum_s)

    def _charge(self, resource: str, collection: str, seconds: float,
                units: int, over: bool, reserved: bool = False) -> None:
        with self._lock:
            flow = self._flows.get(resource, {}).get(collection)
            if flow is None:
                return
            seconds = max(0.0, seconds)
            # measured either way — the operator needs the true consumption —
            # but a reservation is not charged against the flow's share
            flow.seconds += seconds
            flow.units += units
            if reserved:
                flow.reserved_seconds += seconds
                return
            flow.deficit -= seconds
            if units:
                flow.samples.append(seconds / units)
            if over:
                flow.overruns += 1

    def _release(self, resource: str, collection: str, *,
                 idle: bool, worked: bool, reserved: bool = False) -> None:
        with self._lock:
            flow = self._flows.get(resource, {}).get(collection)
            if flow is None:
                return
            if reserved:
                # a reservation never moved the flow, so it has nothing to
                # put back: its place in the rotation is exactly as it was
                return
            flow.last_release = self._clock()
            order = self._order.setdefault(resource, deque())
            if collection in order:
                order.remove(collection)
            if idle:
                # out of the rotation: no slot held, no credit banked
                flow.in_rotation = False
                flow.deficit = 0.0
                return
            # Unused budget is not carried — a slot you did not spend is
            # gone, which is the operator's "does not consume its slot" —
            # and debt is clamped at one quantum so an unbounded unit cannot
            # starve its own collection for many turns (see the header).
            flow.deficit = min(max(flow.deficit, -self._quantum(flow)), 0.0)
            flow.in_rotation = True
            order.append(collection)
            self._learn(flow)

    def _learn(self, flow: _Flow) -> None:
        """The floor is the plugin's minimum efficient unit — so MEASURE it
        rather than configure it: p90 of what this collection's units have
        actually cost on this resource. Below `_LEARN_MIN` samples the
        number is one document's luck and is ignored (the same reasoning as
        stats.MIN_EVENTS), and it is capped, because a pathological unit
        must not be able to claim an unbounded slot — that shows up as an
        overrun and in `suggest_floor_s` instead, where a human sees it."""
        if not self.learn_floor or len(flow.samples) < _LEARN_MIN:
            return
        ordered = sorted(flow.samples)
        p90 = ordered[min(int(len(ordered) * 0.9), len(ordered) - 1)]
        flow.floor_s = min(p90, self.floor_cap_s)

    # ------------------------------------------------------------ telemetry

    def snapshot(self) -> dict:
        """What each flow consumed — the accounting §10 asks for, and the
        only honest basis for judging a scheduling change."""
        out: dict = {}
        with self._lock:
            for resource, flows in self._flows.items():
                total = sum(f.seconds for f in flows.values()) or 1.0
                entry = {}
                for name, f in flows.items():
                    ordered = sorted(f.samples)
                    p90 = ordered[min(int(len(ordered) * 0.9), len(ordered) - 1)] \
                        if ordered else None
                    entry[name] = {
                        "turns": f.turns,
                        "units": f.units,
                        "seconds": round(f.seconds, 3),
                        "share": round(f.seconds / total, 4),
                        "deficit": round(f.deficit, 3),
                        "quantum_s": round(self._quantum(f), 3),
                        "floor_s": round(f.floor_s, 3),
                        "overruns": f.overruns,
                        "in_rotation": f.in_rotation,
                        "reserved_turns": f.reserved_turns,
                        "reserved_seconds": round(f.reserved_seconds, 3),
                        # what the units ACTUALLY cost. Above the quantum
                        # means the units are too big, not the slot too small.
                        "suggest_floor_s": round(p90, 3) if p90 else None,
                    }
                out[resource] = entry
        return out

    def reset(self) -> None:
        with self._lock:
            self._flows.clear()
            self._order.clear()
            self._priority.clear()


_scheduler = Scheduler()


def scheduler() -> Scheduler:
    return _scheduler
