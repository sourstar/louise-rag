"""MEASUREMENT (not product code): the records build's A/B on ONE page range
(docs/notes/scheduling-log.md, 2026-10-24: "the parse pool sized from the cpu
quota; pages streamed"). The same records are cut twice, once by each tree
mounted at /srv, into a SCRATCH pack dir, and the parts must come out
byte-identical.

The plan is copied from the live build's database — the first LIMIT records
of SLICE by (ord, uid), which is the order `pack_slices` cuts them in — into
a fresh build database under OUT, and `pack_build.pack_slices` cuts them as
`hold records build` does (`closed` empty: the collection has no snapshot).
Nothing of the live build is written: its database is opened read-only, the
manifest is only read. Run it with the live build stopped, so the two do not
share the cpus.

Prepended by the caller: COLLECTION, SLICE, LIMIT, OUT, LIVE_BUILD_DB.
Prints one JSON line: wall s, the container's cpu s (cgroup cpu.stat), cpu s
per record, the readings of a 8-second sampler, and every part's entry.
"""
import json
import sqlite3
import sys
import threading
import time
from pathlib import Path

from app import collections as collections_mod, db
from app.pack import build as pack_build, slicing

out = Path(OUT)  # noqa: F821 — prepended
if out.exists():
    raise SystemExit(f"{out} exists: a scratch dir is made fresh for each run")
out.mkdir(parents=True)
collection = collections_mod.by_name(COLLECTION)  # noqa: F821
live = sqlite3.connect(f"file:{LIVE_BUILD_DB}?mode=ro", uri=True)  # noqa: F821
meta = dict(live.execute("SELECT key, value FROM meta").fetchall())
plan = live.execute("SELECT uid, slice, ord FROM plan WHERE slice=? ORDER BY ord, uid LIMIT ?",
                    (SLICE, LIMIT)).fetchall()  # noqa: F821
live.close()

con_build = pack_build._build_db(out)
for k in ("until", "plugin", "records_per_shard"):
    if k in meta:
        pack_build._set_meta(con_build, k, meta[k])
pack_build._set_meta(con_build, "plan_done", "1")
con_build.execute("BEGIN")
con_build.executemany("INSERT INTO plan(uid, slice, ord) VALUES (?, ?, ?)", plan)
con_build.execute("COMMIT")
rps = int(meta.get("records_per_shard") or slicing.scheme_for(pack_build._origin_plugin(collection)).records_per_shard)


def cpu_s() -> float:
    for line in Path("/sys/fs/cgroup/cpu.stat").read_text().splitlines():
        if line.startswith("usage_usec"):
            return int(line.split()[1]) / 1e6
    return float("nan")


readings: list[float] = []
done = threading.Event()


def sampler() -> None:
    last_t, last_c = time.monotonic(), cpu_s()
    while not done.wait(8):
        t, c = time.monotonic(), cpu_s()
        readings.append(round(100 * (c - last_c) / (t - last_t)))
        last_t, last_c = t, c


threading.Thread(target=sampler, daemon=True).start()
con_src = db.connect(collection)
t0, c0 = time.monotonic(), cpu_s()
written, n_rec, n_chunks, n_skip, interrupted = pack_build.pack_slices(
    collection, con_src, con_build, out, records_per_shard=rps, until=meta.get("until") or None,
    log=lambda m: print(m, file=sys.stderr, flush=True), closed={})
wall, cpu = time.monotonic() - t0, cpu_s() - c0
done.set()
parts = [dict(r) for r in con_build.execute(
    "SELECT n, records, chunks, bytes, sha256, root, records_root, chunks_root, first_uid, last_uid "
    "FROM shards ORDER BY slice, n")]
print(json.dumps({"slice": SLICE, "records": n_rec, "chunks": n_chunks, "skipped": n_skip,  # noqa: F821
                  "parts": written, "interrupted": interrupted, "wall_s": round(wall, 1),
                  "cpu_s": round(cpu, 1), "cpu_s_per_record": round(cpu / max(1, n_rec), 5),
                  "mean_util_pct": round(100 * cpu / wall), "readings_8s": readings,
                  "readings_at_one_core": sum(1 for r in readings if 85 <= r <= 115),
                  "part_entries": parts}), flush=True)
