"""The scheduling instrument — docs/notes/scheduling.md §7.

Everything here is READ-ONLY and safe on a live node. It exists because
six reworks of the scheduler were made by inspection instead of by
measurement, and each one missed what the next had to fix:

    python -m tools.sched_report --queries              plan + timing, hot queries
    python -m tools.sched_report --phases wikivec       where an archive pass goes
    python -m tools.sched_report --rates --seconds 120  documents/s per stage
    python -m tools.sched_report --pool                 parse pool health
    python -m tools.sched_report --shares               fair-share accounting
    python -m tools.sched_report --all                  all of the above

Run it INSIDE the worker container (it needs the manifests and the app):

    docker exec ix-louise-rag-worker-1 python -m tools.sched_report --all
"""
import argparse
import sys
import time
from pathlib import Path

from app import collections as c_mod, config, db, parallel, worker
from app.bundles import genesis

BAD_PLAN = ("SCAN documents", "TEMP B-TREE", "LIST SUBQUERY", "CORRELATED")
SLOW_MS = 100.0


def _names(given):
    return given or [c.name for c in c_mod.load_all()]


def _check(con, label: str, sql: str, params) -> bool:
    plan = " | ".join(r[3] for r in con.execute("EXPLAIN QUERY PLAN " + sql, params))
    t0 = time.perf_counter()
    try:
        con.execute(sql, params).fetchone()
        ms = (time.perf_counter() - t0) * 1000
        timing = f"{ms:9.1f} ms"
    except Exception as e:      # noqa: BLE001
        ms, timing = -1.0, f"FAILED {type(e).__name__}"
    bad = [b for b in BAD_PLAN if b in plan]
    if ms > SLOW_MS:
        bad.append(f"SLOW>{SLOW_MS:.0f}ms")
    print(f"  {'BAD ' if bad else 'ok  '}{label:36s} {timing}   {plan[:96]}")
    if bad:
        print(f"       ^ {', '.join(bad)}")
    return not bad


def queries(names) -> bool:
    """Plan + time every query the worker runs on its hot path."""
    ok = True
    for name in _names(names):
        col = c_mod.by_name(name)
        if col is None:
            continue
        con = db.connect(col)
        rows = con.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        print(f"== {name} ({rows:,} documents)")
        anchor = con.execute(
            "SELECT rowid FROM documents WHERE in_scope=1 AND rdf_done=0 AND fts_done=0 "
            "AND embed_done=0 LIMIT 1").fetchone()
        fts_anchor = con.execute(
            "SELECT rowid FROM documents WHERE in_scope=1 AND rdf_done=1 AND fts_done=0 "
            "AND embed_done=0 LIMIT 1").fetchone()
        top = con.execute("SELECT MAX(rowid) FROM documents").fetchone()[0] or 0
        checks = [
            ("rdf selection", worker._RDF_PENDING, (config.MAX_RETRIES,)),
            ("fts selection (fresh)", worker._FTS_FRESH, (config.MAX_RETRIES,)),
            ("fts backlog count", worker._FTS_BACKLOG,
             (config.MAX_RETRIES, config.FTS_BATCH_DOCS)),
            # the phase controller's second watermark: it runs on the same
            # hot path, so it is checked on the same terms
            ("embed backlog count", worker._EMBED_BACKLOG,
             (config.MAX_RETRIES, config.PHASE_EMBED_LOW)),
            ("rdf batch peers", worker._RDF_BATCH_PEERS, (config.MAX_RETRIES, "x", 999)),
            ("fts batch peers (fresh)", worker._FTS_FRESH_PEERS,
             (config.MAX_RETRIES, "x", 999)),
            # NOTE: timed at the STATIC default width. The width the worker
            # actually uses is adapted in ITS process (app/sizing.py) and is
            # invisible from here, so this row is the "before" number by
            # construction — read `--shares` for the live widths.
            ("fts reindex window (default width)", worker._FTS_REINDEX_WINDOWED,
             (0, min(worker._REINDEX_WINDOW, top), config.MAX_RETRIES,
              config.FTS_VERSION, config.ENTITY_VERSION)),
            ("fts reindex peers", worker._FTS_REINDEX_PEERS,
             (0, min(worker._REINDEX_WINDOW, top), config.MAX_RETRIES,
              config.FTS_VERSION, config.ENTITY_VERSION, "x", 99)),
            ("archive eligible-in-range", genesis.eligible_in_range_sql(con),
             (1_000_000, 1_999_999)),
            ("archive pending-in-range", genesis.pending_in_range_sql(con),
             (1_000_000, 1_999_999)),
            ("local-archive candidates page", genesis._CANDIDATES_PAGE_SQL, (-1, 2048)),
        ]
        if anchor is not None:
            checks.append(("prefetch probe (rdf)", worker._RDF_AHEAD,
                           (anchor[0], config.MAX_RETRIES, 40_000)))
        if fts_anchor is not None:
            checks.append(("prefetch probe (fts)", worker._FTS_AHEAD,
                           (fts_anchor[0], config.MAX_RETRIES, 40_000)))
        for label, sql, params in checks:
            ok = _check(con, label, sql, params) and ok
        con.close()
    return ok


def phases(name: str) -> None:
    """Where an archive pass's time goes. Replays an ALREADY-APPLIED shard,
    so it reads exactly what a pass reads and writes nothing."""
    col = c_mod.by_name(name)
    if col is None or not col.genesis_archive:
        print(f"{name}: no genesis archive"); return
    from app import licensing        # invariant 9: documents' text is parsed (audit)
    licensing.require_ack_or_exit(col, use="its documents are parsed")
    con = db.connect(col)
    row = con.execute(
        "SELECT shard FROM genesis_imports WHERE archive=? AND state IN ('applied','final') "
        "ORDER BY updated_at DESC LIMIT 1", (col.genesis_archive,)).fetchone()
    if row is None:
        print(f"{name}: no applied shard to replay"); return
    shard = row["shard"]
    archive = genesis.ARCHIVES[col.genesis_archive]
    cache_dir = (col.corpus_dir if archive.local
                 else col.manifest_path.parent / "genesis" / archive.archive_id)
    print(f"== {name}: replaying {shard}")
    t0 = time.perf_counter()
    rows_iter, _files = genesis._ADAPTERS[archive.adapter](
        Path(cache_dir) if archive.local else cache_dir, shard,
        genesis._not_on_disk, archive.uid_prefix, col, wanted=None)
    t_open = time.perf_counter() - t0
    t0 = time.perf_counter()
    n_docs = n_vecs = 0
    uids = []
    for uid, vecs in rows_iter:
        n_docs += 1
        n_vecs += len(vecs)
        if len(uids) < 200:
            uids.append(uid)
    t_stream = time.perf_counter() - t0
    print(f"  adapter open          {t_open:8.2f}s")
    print(f"  read vectors          {t_stream:8.2f}s   {n_docs} docs / {n_vecs} vectors "
          f"({n_vecs / max(t_stream, .001):.0f} vec/s)")
    t0 = time.perf_counter()
    for uid in uids:
        con.execute("SELECT uid, native_id, path, lang FROM documents WHERE uid=? "
                    "AND error IS NULL AND fts_done=1 AND embed_done=0", (uid,)).fetchone()
    per = (time.perf_counter() - t0) / max(len(uids), 1)
    print(f"  manifest lookup       {per * 1e3:8.2f} ms each  "
          f"(~{per * n_docs:.1f}s for the pass)")
    rows = [dict(r) for r in con.execute(
        "SELECT uid, native_id, path, lang FROM documents WHERE uid IN "
        f"({','.join('?' * len(uids[:100]))})", uids[:100])]
    if rows:
        t0 = time.perf_counter()
        parallel.chunks_for_rows(col, rows)
        per = (time.perf_counter() - t0) / len(rows)
        print(f"  chunk texts (pooled)  {per * 1e3:8.2f} ms each  "
              f"(~{per * n_docs:.1f}s for the pass) — pool: {parallel.health()}")
    print("  the remainder of the pass is WRITES: Lance delete+add per "
          f"{genesis.APPLY_BATCH_DOCS} docs, plus one ledger row per vector")
    con.close()


def rates(names, seconds: int) -> None:
    """Documents per second per stage, sampled over `seconds`."""
    def snapshot():
        out = {}
        for name in _names(names):
            col = c_mod.by_name(name)
            if col is None:
                continue
            con = db.connect(col)
            r = con.execute("SELECT SUM(rdf_done) rdf, SUM(fts_done) fts, "
                            "SUM(embed_done) emb, SUM(in_scope) scope FROM documents").fetchone()
            out[name] = {k: int(r[k] or 0) for k in ("rdf", "fts", "emb", "scope")}
            con.close()
        return out

    first = snapshot()
    print(f"sampling {seconds}s ...")
    time.sleep(seconds)
    second = snapshot()
    print(f"{'collection':16s} {'rdf/s':>9s} {'fts/s':>9s} {'embed/s':>9s}   remaining (embed)")
    for name, now in second.items():
        was = first.get(name, now)
        r = {k: (now[k] - was[k]) / seconds for k in ("rdf", "fts", "emb")}
        left = now["scope"] - now["emb"]
        eta = f"{left / r['emb'] / 3600:.1f} h" if r["emb"] > 0 else "—"
        print(f"{name:16s} {r['rdf']:9.1f} {r['fts']:9.1f} {r['emb']:9.1f}   "
              f"{left:,} ({eta})")


def pool_health() -> None:
    print("parse pool:", parallel.health())
    print(f"  PARSE_PROCESSES={config.PARSE_PROCESSES} "
          f"PARSE_TIMEOUT_S={config.PARSE_TIMEOUT_S} "
          f"PARSE_MAX_FAILURES={config.PARSE_MAX_FAILURES} "
          f"PARSE_TASKS_PER_CHILD={config.PARSE_TASKS_PER_CHILD}")
    print("  children alive: run `docker top <worker container> | grep -c spawn_main`")
    print("  a thread gone quiet: see docs/notes/scheduling.md §7 (py-spy sidecar)")


def shares(names, limit: int = 12) -> None:
    """What each collection actually GOT from each contended resource.

    Read out of the worker's own log rather than measured here: the
    scheduler's accounting lives in the worker process (app/sched.py), and
    a second process cannot see it. The worker publishes a line per
    resource every SHARES_LOG_INTERVAL_S; this prints the most recent ones.
    """
    print("=== shares (from the worker log; app/sched.py accounting):")
    seen = 0
    for name in _names(names):
        col = next((c for c in c_mod.load_all() if c.name == name), None)
        if col is None:
            continue
        con = db.connect(col)
        try:
            rows = list(con.execute(
                "SELECT ts, message FROM events WHERE stage='sched' "
                "ORDER BY ts DESC LIMIT ?", (limit,)))
        except Exception as e:            # noqa: BLE001 — an old manifest
            print(f"  {name}: {e}")
            continue
        finally:
            con.close()
        for ts, message in rows:
            print(f"  [{ts}] {message}")
            seen += 1
        if rows:
            break            # node-wide state: one collection's log has it all
    if not seen:
        print("  nothing logged yet — the worker publishes every "
              f"{worker.SHARES_LOG_INTERVAL_S:.0f}s once a rotation has run,"
              "\n  and only for resources that had turns. SCHED_FAIR_SHARE=0"
              " disables the rotation entirely.")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--queries", action="store_true", help="plan + time the hot queries")
    ap.add_argument("--phases", metavar="COLLECTION", help="where an archive pass's time goes")
    ap.add_argument("--rates", action="store_true", help="documents/s per stage")
    ap.add_argument("--pool", action="store_true", help="parse pool health")
    ap.add_argument("--shares", action="store_true",
                    help="what each collection got from each resource")
    ap.add_argument("--all", action="store_true",
                    help="queries + shares + rates + pool")
    ap.add_argument("--seconds", type=int, default=120, help="rates sampling window")
    ap.add_argument("collections", nargs="*")
    args = ap.parse_args(argv)
    if not any((args.queries, args.phases, args.rates, args.pool, args.shares,
                args.all)):
        ap.print_help()
        return 2
    ok = True
    if args.queries or args.all:
        ok = queries(args.collections) and ok
    if args.phases:
        phases(args.phases)
    if args.shares or args.all:
        shares(args.collections)
    if args.rates or args.all:
        rates(args.collections, args.seconds)
    if args.pool or args.all:
        pool_health()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
