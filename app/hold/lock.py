"""The one writer per collection (07 §7.7): `state/lock` in the collection's
directory (§7.8). The node holds it for every collection it runs, for as
long as it runs it; a `hold` command that writes takes it or exits 3 naming
the holder — 2 for the four whose later retry is not the answer
(`collection forget`, `vectors check --drop`, `collection migrate --apply`,
`dev adopt`). `show`, `list`, `check` and `ask` never take it: a reader
never blocks a writer or is blocked by one (the state database runs in
write-ahead mode).

An advisory `flock` on the file. Two open descriptions contend, in one
process or two, on one host — two nodes on one machine are two roots and
share nothing (§7.7) — and the kernel drops it with its holder, so a crash
leaves no stale lock, only the holder's last words in the file for the next
taker to read. `node pause <collection>` makes the worker release it at the
end of the current unit; `node resume <collection>` makes it take it again
(app/worker.py).
"""
from __future__ import annotations

import fcntl
import json
import os
import socket
import time
from pathlib import Path
from typing import Any

from .codex import CodexError

LOCK = "lock"
STATE = "state"


class Held(CodexError):
    """Another writer holds the collection. `holder` is what its lock file
    says; `exit_code` is 3 (retry when it is done) or 2 (07 §7.7's four)."""

    def __init__(self, name: str, holder: dict[str, Any] | None, *, exit_code: int = 3):
        self.holder = dict(holder or {})
        self.exit_code = exit_code
        who = (f"{self.holder.get('what') or 'another writer'} (pid "
               f"{self.holder.get('pid', '?')} on {self.holder.get('host', '?')}, since "
               f"{self.holder.get('since', '?')})")
        tail = ("; `node pause <collection>` releases the node's" if exit_code == 2
                else "; retry when it is done")
        super().__init__(f"{name!r} is held by {who} — one writer per collection "
                         f"(07 §7.7){tail}")


def state_dir(root: Path, name: str) -> Path:
    """`<root>/<name>/state`, where 07 §7.8 puts the lock (app/hold/records.py
    lays the directory out the same way)."""
    return Path(root) / name / STATE


def lock_path(state: Path) -> Path:
    return Path(state) / LOCK


class Lock:
    """A taken lock; `release` (or the `with`) gives it back and clears the
    holder's words. Releasing twice is nothing."""

    def __init__(self, name: str, path: Path, f) -> None:
        self.name, self.path, self._f = name, path, f

    def release(self) -> None:
        f, self._f = self._f, None
        if f is None:
            return
        try:
            f.seek(0)
            f.truncate(0)
            f.flush()
            fcntl.flock(f, fcntl.LOCK_UN)
        finally:
            f.close()

    @property
    def held(self) -> bool:
        return self._f is not None

    def __enter__(self) -> "Lock":
        return self

    def __exit__(self, *exc) -> None:
        self.release()


def take(state: Path, name: str, what: str, *, exit_code: int = 3) -> Lock:
    """The collection's write lock, now, or `Held` naming the holder. `what`
    is written into the file for whoever finds it taken."""
    p = lock_path(state)
    p.parent.mkdir(parents=True, exist_ok=True)
    f = open(p, "a+b")            # never truncated by an opener that may lose the race
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.seek(0)
        raw = f.read()
        f.close()
        try:
            who = json.loads(raw.decode("utf-8") or "{}")
        except ValueError:
            who = {}
        raise Held(name, who if isinstance(who, dict) else {}, exit_code=exit_code) from None
    f.seek(0)
    f.truncate(0)
    f.write(json.dumps({"what": what, "pid": os.getpid(), "host": socket.gethostname(),
                        "since": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
                       sort_keys=True).encode("utf-8"))
    f.flush()
    return Lock(name, p, f)


def holder(state: Path) -> dict[str, Any] | None:
    """Who holds the collection's lock now, from its file, or None when
    nobody does. A probe: it takes and gives back the lock when free."""
    p = lock_path(state)
    if not p.exists():
        return None
    with open(p, "a+b") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            f.seek(0)
            try:
                who = json.loads(f.read().decode("utf-8") or "{}")
            except ValueError:
                who = {}
            return who if isinstance(who, dict) else {}
        fcntl.flock(f, fcntl.LOCK_UN)
        return None
