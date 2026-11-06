"""MEASUREMENT (read-only): how many of a collection's next documents the
stages would read from their fresh record instead of parsing the origin
(app/record_source.py, docs/notes/one-parse.md), why the others parse, and
what each costs: the record lookup against the plugin's parse, timed on
the same rows.

  python tools/measure/record_source_hits.py wikipedia [--n 2000] [--stage embed|fts]

Prints one JSON object: the reasons counted, ms per row for each source,
and whether the sampled rows' sections are equal both ways.
"""
import argparse
import json
import sqlite3
import time
from collections import Counter

SELECT = {
    "embed": "SELECT * FROM documents WHERE in_scope=1 AND fts_done=1 AND embed_done=0 "
             "AND error IS NULL ORDER BY rowid LIMIT ?",
    "fts": "SELECT * FROM documents WHERE in_scope=1 AND rdf_done=1 AND fts_done=0 "
           "AND error IS NULL ORDER BY rowid LIMIT ?",
}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("collection")
    ap.add_argument("--n", type=int, default=2000)
    ap.add_argument("--stage", choices=sorted(SELECT), default="embed")
    a = ap.parse_args(argv)
    from app import collections as collections_mod, plugin_io, record_source as rs
    c = collections_mod.by_name(a.collection)
    con = sqlite3.connect(f"file:{c.manifest_path}?mode=ro", uri=True, timeout=30)
    con.row_factory = sqlite3.Row
    rows = con.execute(SELECT[a.stage], (a.n,)).fetchall()
    plugin = c.plugin_instance()
    why = Counter()
    t_rec = t_parse = 0.0
    equal = differ = 0
    for r in rows:
        t0 = time.perf_counter()
        rec, reason = rs.lookup(c, r)
        t_rec += time.perf_counter() - t0
        why[reason] += 1
        t0 = time.perf_counter()
        parsed = [tuple(s) for s in plugin_io.sections(plugin, r)]
        t_parse += time.perf_counter() - t0
        if rec is not None:
            if [tuple(s) for s in rec.get("sections") or []] == parsed:
                equal += 1
            else:
                differ += 1
    n = max(1, len(rows))
    print(json.dumps({"collection": c.name, "stage": a.stage, "rows": len(rows), "why": dict(why),
                      "ms_record_lookup": round(1000 * t_rec / n, 3),
                      "ms_parse": round(1000 * t_parse / n, 3),
                      "fresh_sections_equal": equal, "fresh_sections_differ": differ}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
