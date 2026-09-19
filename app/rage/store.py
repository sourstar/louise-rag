"""The runs this front has stated, kept across its own restarts.

docs/09-formats.md §9.12: the platform "describes a run on request (`GET
/louise/runs/<run_id>`)", and that is where a calibration statement's
`engine`, `weights` and `driver` come from. A statement may be written
minutes after the answer that named the run, so a front that restarted in
between must still be able to describe it.

RAGE's OWN FILE, in its state directory — never Louise's node store: the
two are separate apps, and an optional helper does not write the database
of what it helps. It holds nothing but what the front computed: no desired
state (that is `rage.toml`), and nothing a restart must not see.

One statement writes a row, and a run's object is fixed by its id, so a
second insert of the same id is the same row: INSERT OR IGNORE, no read
before the write, nothing to race (CLAUDE.md: a writer that reads first
takes BEGIN IMMEDIATE — this one does not read first).
"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any, Mapping

_SCHEMA = ("CREATE TABLE IF NOT EXISTS runs ("
           "run_id TEXT PRIMARY KEY, object TEXT NOT NULL, first_stated INTEGER NOT NULL)")


class RunStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as con:
            con.execute(_SCHEMA)
            con.commit()

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, timeout=10.0)
        con.execute("PRAGMA journal_mode=WAL")
        return con

    def put(self, run_id: str, run: Mapping[str, Any]) -> None:
        with closing(self._connect()) as con:
            con.execute("INSERT OR IGNORE INTO runs VALUES (?,?,?)",
                        (run_id, json.dumps(dict(run), sort_keys=True), int(time.time())))
            con.commit()

    def get(self, run_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as con:
            row = con.execute("SELECT object FROM runs WHERE run_id=?", (run_id,)).fetchone()
        return json.loads(row[0]) if row else None
