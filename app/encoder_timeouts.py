"""An encoder READ TIMEOUT is the endpoint's, until one document proves it its own.

Live 2026-10-09: a LocalAI backend crashed under a GPU overcommit and came
back HUNG; every request read-timed-out, and the worker wrote `ReadTimeout`
onto gutenberg:76722, 34379 and 22283 — each removed from every selection
for the endpoint's fault (CLAUDE.md: deferral is not failure).

So a timeout DEFERS its document, untouched, and HOLDS the endpoint as busy:
no request goes to it for HOLD_S, doubling up to HOLD_MAX_S while it answers
nobody, released by its first answer. A document is blamed only when IT
keeps timing out while the endpoint answers others: BLAME_AFTER timeouts,
each one after the endpoint answered some other document since the one
before. A hung endpoint answers nobody, so it never blames anyone. A
document that timed out is passed over by the selection until the endpoint
has answered someone else (or COOL_S has passed), so one slow document at
the head of the queue neither blocks the rest nor escapes judgement.

Per process, in memory, bounded (MAX_MARKS): the evidence is the endpoint's
state NOW, and a restart forgets it on the safe side — nobody is blamed on
less. Not the manifest's `retries` column: that counts RECORDED errors, and
every selection drops a document at MAX_RETRIES — a timeout count there
would take documents out of the pipeline silently, and write onto every
document a hung endpoint met.

An endpoint is (url, model): one LocalAI address serves every model, and a
backend hangs per model (the run check keys the same way, 687eb75).
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass

#: Timeouts on one document, with the endpoint answering others in between,
#: before the document is blamed with an error.
BLAME_AFTER = 3
#: How long a timed-out document is passed over when the endpoint answers
#: nobody meanwhile — then asked again (a timeout then puts nothing on it).
COOL_S = 600.0
#: The first hold of an endpoint that timed out and answered nobody; it
#: doubles while that goes on, up to HOLD_MAX_S.
HOLD_S = 60.0
HOLD_MAX_S = 600.0
#: The bound on documents remembered; the oldest mark goes first.
MAX_MARKS = 4096

Endpoint = tuple[str, str]


@dataclass(frozen=True)
class _Mark:
    endpoint: Endpoint
    count: int          # timeouts the endpoint's answers to others put on it
    answered: int       # the endpoint's answers when this timeout happened
    at: float           # monotonic


_lock = threading.Lock()
#: endpoint -> documents it has answered in this process
_answered: dict[Endpoint, int] = {}
#: (manifest path, uid) -> its last timeout; dict order is age
_marks: dict[tuple[str, str], _Mark] = {}
#: endpoint -> (held until, monotonic; holds since its last answer)
_holds: dict[Endpoint, tuple[float, int]] = {}


def endpoint_of(recipe) -> Endpoint:
    if recipe is None:
        return ("", "")
    return (str(getattr(recipe, "url", "") or ""), str(getattr(recipe, "model", "") or ""))


def answered(endpoint: Endpoint, place: str | None = None, uid: str | None = None) -> None:
    """A document's vectors came back from `endpoint`: it serves — its hold
    is over — and that document's own count starts again."""
    with _lock:
        _answered[endpoint] = _answered.get(endpoint, 0) + 1
        _holds.pop(endpoint, None)
        if place is not None and uid is not None:
            _marks.pop((place, uid), None)


def forget(place: str, uid: str) -> None:
    """The document moved (an attempt embedded part of it): not stuck."""
    with _lock:
        _marks.pop((place, uid), None)


def timed_out(endpoint: Endpoint, place: str, uid: str, *, now: float | None = None) -> int:
    """Record a timeout of this document; return how many of its timeouts
    stand against it. The first counts one; each later one counts only if
    the endpoint answered another document since the one before — else it
    adds nothing. At BLAME_AFTER the caller blames the document, and the
    mark is dropped."""
    now = time.monotonic() if now is None else now
    key = (place, uid)
    with _lock:
        served = _answered.get(endpoint, 0)
        before = _marks.pop(key, None)
        if before is None or before.endpoint != endpoint:
            count = 1
        elif served > before.answered:
            count = before.count + 1
        else:
            count = before.count
        if count >= BLAME_AFTER:
            return count
        _marks[key] = _Mark(endpoint, count, served, now)
        while len(_marks) > MAX_MARKS:
            del _marks[next(iter(_marks))]
        return count


def passed_over(place: str, uid: str, *, now: float | None = None) -> bool:
    """Is this document waiting for evidence — it timed out, and its
    endpoint has answered nobody since (for less than COOL_S)?"""
    mark = _marks.get((place, uid))
    if mark is None:
        return False
    now = time.monotonic() if now is None else now
    return _answered.get(mark.endpoint, 0) == mark.answered and now - mark.at < COOL_S


def waiting(place: str) -> int:
    """How many of this collection's documents carry a mark: what a
    selection widens by so that passing them over still fills it."""
    if not _marks:
        return 0
    with _lock:
        return sum(1 for p, _ in _marks if p == place)


def hold(endpoint: Endpoint, *, now: float | None = None) -> float:
    """The endpoint timed out and answered nobody: send it nothing for a
    while, twice as long each time until it answers. The seconds held."""
    now = time.monotonic() if now is None else now
    with _lock:
        _, n = _holds.get(endpoint, (0.0, 0))
        seconds = min(HOLD_MAX_S, HOLD_S * 2 ** n)
        _holds[endpoint] = (now + seconds, n + 1)
        return seconds


def held(endpoint: Endpoint, *, now: float | None = None) -> float:
    """Seconds left on the endpoint's hold; 0 when it may be asked."""
    entry = _holds.get(endpoint)
    if entry is None:
        return 0.0
    now = time.monotonic() if now is None else now
    return max(0.0, entry[0] - now)


def any_held() -> bool:
    return bool(_holds)


def watching() -> bool:
    """Is any timeout waiting on evidence? An answer counts only then."""
    return bool(_marks or _holds)


def reset() -> None:
    with _lock:
        _answered.clear()
        _marks.clear()
        _holds.clear()
