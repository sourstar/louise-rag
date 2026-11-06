"""A document whose FILE is missing — the self-healing error (operator,
2026-10-30, choice "self-healing error").

The live case, 2026-10-30: the rename of wikipedia-v8 re-rooted only the
first path of wikipedia's two-file locators, and 85,943 documents were
written `embed: FileNotFoundError: …` — each out of every selection, seen
only when the embedding looked slow (fixed in 954ed47). A file that is not
where the manifest says is the node's condition far more often than the
document's: a rename, a mount gone, a corpus moved. So such an error:

- names its file: `<stage>: origin missing: <path>`;
- is said ONCE per path, loudly, with what will clear it (`note`);
- is CLEARED BY ITSELF once the file is back: the worker asks the noted
  paths every HEAL_EVERY_S and, for each one that exists again, clears the
  errors naming it, in rowid windows (`heal`). Nobody has to clear them.

The error is still written, so the document leaves the selections and no
stage spins on it; the alternative (a pure deferral) would have touched
every stage's selection query. The noted paths live beside the manifest
(`origin-missing.json`), so a restart keeps healing what it noted before.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path

PREFIX = "origin missing: "
HEAL_EVERY_S = 600.0
#: rows per window of the healing walk (each window its own BEGIN IMMEDIATE)
HEAL_WINDOW = 200_000
_MISSING = re.compile(r"\[Errno (?:2|20)\] (?:No such file or directory|Not a directory): '([^']+)'")
_lock = threading.Lock()
_last_heal: dict[str, float] = {}


def path_of(err) -> str | None:
    """The missing file an error names — an exception (its chain walked, its
    `filename`) or the text an error crossed the parse pool as — or None."""
    seen: set[int] = set()
    e = err
    while isinstance(e, BaseException) and id(e) not in seen:
        seen.add(id(e))
        if isinstance(e, (FileNotFoundError, NotADirectoryError)) and e.filename:
            return os.fsdecode(e.filename)
        e = e.__cause__ or e.__context__
    m = _MISSING.search(str(err))
    return m.group(1) if m else None


def message(path: str) -> str:
    return PREFIX + path


def _registry(collection) -> Path:
    return Path(collection.manifest_path).with_name("origin-missing.json")


def _read(p: Path) -> dict:
    try:
        got = json.loads(p.read_text(encoding="utf-8"))
        return got if isinstance(got, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(p: Path, paths: dict) -> None:
    tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(paths, sort_keys=True), encoding="utf-8")
    os.replace(tmp, p)


def note(collection, path: str) -> bool:
    """`path` noted as missing for this collection; True the first time —
    the caller says it then, once."""
    p = _registry(collection)
    with _lock:
        paths = _read(p)
        if path in paths:
            return False
        paths[path] = int(time.time())
        _write(p, paths)
        return True


def noted(collection) -> list[str]:
    return sorted(_read(_registry(collection)))


def due(collection, now: float | None = None) -> bool:
    """Something noted, and HEAL_EVERY_S since this process last looked."""
    now = time.monotonic() if now is None else now
    key = str(collection.manifest_path)
    if now - _last_heal.get(key, -HEAL_EVERY_S) < HEAL_EVERY_S:
        return False
    _last_heal[key] = now
    return _registry(collection).is_file()


def heal(con, collection) -> dict[str, int]:
    """For every noted path that exists again: the errors naming it cleared
    (and the retry they cost given back), window by window, and the path
    forgotten. `{path: documents cleared}`."""
    p = _registry(collection)
    back = [path for path in noted(collection) if os.path.exists(path)]
    out: dict[str, int] = {}
    if not back or con.in_transaction:
        return out
    top = con.execute("SELECT max(rowid) FROM documents").fetchone()[0] or 0
    for path in back:
        cleared = 0
        needle = message(path)
        for lo in range(0, top, HEAL_WINDOW):
            con.execute("BEGIN IMMEDIATE")
            try:
                cur = con.execute(
                    "UPDATE documents SET error=NULL, retries=max(retries - 1, 0), updated_at=? "
                    "WHERE rowid > ? AND rowid <= ? AND error IS NOT NULL AND instr(error, ?) > 0",
                    (int(time.time()), lo, lo + HEAL_WINDOW, needle))
                cleared += cur.rowcount
                con.execute("COMMIT")
            except BaseException:
                con.execute("ROLLBACK")
                raise
        out[path] = cleared
    with _lock:
        paths = _read(p)
        for path in back:
            paths.pop(path, None)
        if paths:
            _write(p, paths)
        else:
            p.unlink(missing_ok=True)
    return out
