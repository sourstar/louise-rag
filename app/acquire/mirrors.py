"""Mirror structure, health and ranking for multi-endpoint sources.

The transfer manager's EWMA already prefers the fastest mirror DURING a
pass, but it forgets everything at restart, never demotes a mirror that
fails, and admits any endpoint whether or not it serves the corpus layout
the plugin expects. This module adds the three missing pieces, all driven
by one tiny fetch per mirror:

STRUCTURE IS THE PROBE. `AcquisitionSpec.probe_file` names a small file at
its OFFICIAL relative path (gutenberg: `1/pg1.rdf`, ~10 KB). A mirror that
serves it at that path proves it carries the official layout; one that
does not — probed live: aleph's siblings answer, but several big mirror
services expose Gutenberg under other module names or the legacy tree —
fails the fetch and is excluded from ranking. The admission rule ("add a
mirror only if it provides exactly the official structure") is thereby
enforced by the code on every benchmark, not by a comment.

PERSISTED RANKING. The benchmark's outcome (seconds per mirror, failures)
is written to the collection's control table (BENCH_KEY). Each acquisition
pass seeds the endpoint order from the last recorded ranking — so a
restart starts with the mirror that won last time — and re-runs the
benchmark first when the record is older than BENCH_TTL_S, so a mirror
that got better takes over unattended. The manager's EWMA still re-ranks
live within the pass: measured beats benchmarked beats declared.

FAILURE DEMOTION. A mirror that fails a real fetch mid-pass is marked down
for DOWN_S and the engine retries the file on the next-ranked mirror
immediately (engine._make_worker); a down mirror re-enters through the
next benchmark. Down-state is process-local on purpose: it describes THIS
process's view of the network right now.
"""
import json
import tempfile
import threading
import time
from pathlib import Path

from .. import db
from .spec import AcquisitionSpec, Endpoint, TransportError

BENCH_KEY = "mirror_bench"
BENCH_TTL_S = 6 * 3600          # re-benchmark at most once per interval cycle
PROBE_TIMEOUT_S = 20.0          # a mirror slower than this on a ~10 KB file loses
DOWN_S = 900                    # a failed mirror sits out 15 minutes

_down: dict[str, float] = {}
_down_lock = threading.Lock()


def mark_down(url: str) -> None:
    with _down_lock:
        _down[url] = time.time() + DOWN_S


def is_down(url: str) -> bool:
    with _down_lock:
        until = _down.get(url)
        return until is not None and until > time.time()


def reset_down_state() -> None:
    """Tests only; a restart starts empty anyway."""
    with _down_lock:
        _down.clear()


def benchmark(con, spec: AcquisitionSpec, *, force: bool = False) -> list[dict]:
    """One small timed fetch per endpoint; ranking recorded in the control
    table. Returns the recorded entries (fastest first, failures last).
    Skipped (returning the stored record) while the stored one is younger
    than BENCH_TTL_S, so frequent passes stay cheap."""
    if not spec.probe_file or len(spec.endpoints) < 2:
        return []
    stored = read_bench(con)
    if stored and not force and time.time() - stored.get("ts", 0) < BENCH_TTL_S:
        return stored["results"]
    from .transports import REGISTRY
    from .spec import RemoteFile
    results = []
    for endpoint in spec.endpoints:
        transport = REGISTRY[endpoint.transport]
        entry = {"url": endpoint.url, "seconds": None, "error": None}
        started = time.time()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                _probe_once(transport, endpoint, spec.probe_file, Path(tmp))
            entry["seconds"] = round(time.time() - started, 3)
        except Exception as e:   # noqa: BLE001 — any failure = this mirror loses
            entry["error"] = f"{type(e).__name__}: {e}"[:200]
        results.append(entry)
    results.sort(key=lambda r: (r["seconds"] is None, r["seconds"] or 0.0))
    db.set_control(con, BENCH_KEY, json.dumps({"ts": int(time.time()),
                                               "probe": spec.probe_file,
                                               "results": results}))
    ok = [r for r in results if r["seconds"] is not None]
    db.log_event(con, "info", "acquire",
                 f"mirror benchmark ({spec.probe_file}): " + "; ".join(
                     (f"{r['url']} {r['seconds']}s" if r["seconds"] is not None
                      else f"{r['url']} FAILED") for r in results)
                 + ("" if ok else " — no mirror passed the structure probe"))
    return results


def _probe_once(transport, endpoint: Endpoint, rel_path: str, tmp: Path) -> None:
    """One probe fetch with its own hard deadline, independent of the
    transport's per-file timeout (rsync's is 300 s: far too generous for a
    10 KB structure probe)."""
    from .spec import RemoteFile
    result: dict = {}

    def _run():
        try:
            transport.fetch_one(endpoint, RemoteFile(rel_path=rel_path),
                                tmp / "probe", 0)
            result["ok"] = True
        except BaseException as e:   # noqa: BLE001 — carried to the caller
            result["error"] = e

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(PROBE_TIMEOUT_S)
    if t.is_alive():
        raise TransportError(f"probe of {endpoint.url}/{rel_path} exceeded "
                             f"{PROBE_TIMEOUT_S:.0f}s")
    if "error" in result:
        raise TransportError(str(result["error"])[:300])
    dest = tmp / "probe"
    if not dest.is_file() or dest.stat().st_size == 0:
        raise TransportError(f"probe of {endpoint.url}/{rel_path}: empty result")


def read_bench(con) -> dict | None:
    raw = db.get_control(con, BENCH_KEY)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def seed_order(con, spec: AcquisitionSpec) -> list[Endpoint]:
    """Endpoints in the order a pass should START with: last benchmark's
    fastest first, unbenchmarked in declared order, failed ones last —
    minus any mirror currently marked down (unless that would leave
    nothing, in which case down-ness is ignored: a wrong ranking still
    beats not fetching)."""
    by_url = {e.url: e for e in spec.endpoints}
    stored = read_bench(con)
    ordered: list[Endpoint] = []
    if stored:
        for r in stored.get("results", []):
            e = by_url.pop(r["url"], None)
            if e is not None:
                ordered.append(e)
    ordered += list(by_url.values())        # newly declared, never benchmarked
    alive = [e for e in ordered if not is_down(e.url)]
    return alive or ordered
