"""Consumer node: a pure importer.

Discovers the same corpus, publishes its lazy queue as demand (which is what
steers the producer's lease), then runs the REAL worker loop — with
embed_mode=off, so vectors can only ever arrive through bundle import — and
the real API for the driver's queries. TOFU trust means the first producer it
imports from becomes its anchor, which the driver asserts by pubkey.
"""
import sys
import threading
import time

sys.path.insert(0, "/srv")

from app import collections as collections_mod, contribute, db, discovery, spaces

COORDINATOR = "http://coordinator:8750"


def main() -> int:
    collection = collections_mod.default_collection()
    spaces.validate_boot([collection])
    con = db.connect(collection)
    discovery.sync_collection(con, collection)

    # metadata + lexical index so search works; embed_mode=off makes vectors
    # importable-only, which is the state this flow exists to prove
    from app import worker
    for row in con.execute("SELECT * FROM documents WHERE in_scope=1"):
        row = dict(row)
        if not row["rdf_done"]:
            worker.stage_rdf(con, row, collection)
            row = dict(con.execute("SELECT * FROM documents WHERE uid=?",
                                    (row["uid"],)).fetchone())
        if not row["fts_done"]:
            worker.stage_fts(con, row, collection)
    db.set_scope(con, {"embed_mode": "off"})
    db.set_worker_state(con, "running")

    # every un-embedded document is demand: enqueue, then publish the queue
    now = int(time.time())
    for row in con.execute("SELECT uid FROM documents WHERE embed_done=0"):
        con.execute("INSERT OR REPLACE INTO embed_queue(uid,priority,ts) "
                    "VALUES(?,100,?)", (row["uid"], now))
    for attempt in range(30):
        try:
            result = contribute.report_demand(con, collection, COORDINATOR)
            print(f"consumer: demand reported -> {result}", flush=True)
            break
        except contribute.ContributeError:
            time.sleep(2)

    # The worker must own the MAIN thread: run_forever registers SIGTERM
    # handlers, and signal.signal raises off the main thread. uvicorn is the
    # one that checks and skips signal installation when backgrounded.
    import uvicorn
    from app.api import app

    def serve_api():
        uvicorn.run(app, host="0.0.0.0", port=8742, log_level="warning")

    threading.Thread(target=serve_api, daemon=True).start()
    print("consumer: api thread started, worker taking the main thread", flush=True)
    worker.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
