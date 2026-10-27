"""`louise node show` (07 §7.3): the node at a glance — its mode and every
runtime override in force, each collection's state and what it is doing
now, the last round's cost as the worker logged it, and the verbs running
under the node (07 §7.4: "a verb that outlives the request … `node show`
lists it"). Migration step 14 — `louise node health` carried the state until
2026-10-07.

READ-ONLY, and it creates nothing: the node's store is peeked
(`node_state.peek`), never opened for writing, so a show on a node that
has not booted reports "paused" as the worker would read it.

A collection's `state` is one of `in-use` · `quarantine` (07 §7.8,
`copy_state.of`) · `refused` (the boot check, or a file that does not
load), and beside it `paused` (`node pause <collection>`) and
`integrity_violated` (03 §3.8) — the same functions the worker asks, so
the show cannot disagree with what runs.
"""
from __future__ import annotations

import json
import time
from contextlib import closing
from typing import Any

#: how far back from the newest `sched` line a line still belongs to the
#: same round's report (the worker writes them together)
ROUND_WINDOW_S = 120


def _paused(c) -> list[str]:
    from app import db
    from app.node_state import PAUSED_KEY
    try:
        names = json.loads(db.get_control(c, PAUSED_KEY, "[]"))
    except (TypeError, ValueError):
        return []
    return sorted(n for n in names if isinstance(n, str)) if isinstance(names, list) else []


def _jobs(c) -> list[dict[str, Any]]:
    from app import jobs as jobs_mod
    rows = c.execute("SELECT * FROM jobs WHERE state IN ('queued', 'running') ORDER BY id").fetchall()
    return [jobs_mod.view(jobs_mod._job(r)) for r in rows]


def overrides() -> dict[str, Any]:
    """Tier 3 in force (07 §7.8): the mode, the collections paused, a
    bandwidth override and when it expires."""
    from app import db, node_state
    mode = node_state.peek_worker_state()
    bw = node_state.peek(db.get_bandwidth_override, None)
    return {"mode": mode, "paused_collections": node_state.peek(_paused, []),
            "bandwidth_bps": bw,
            # the last mode writes and who made them (node_state.set_worker_state)
            "mode_writes": node_state.peek(_mode_writes, [])}


def _mode_writes(con) -> list[dict[str, Any]]:
    import json
    from app import db, node_state
    raw = db.get_control(con, node_state.MODE_HISTORY_KEY)
    try:
        return json.loads(raw)[-10:] if raw else []
    except ValueError:
        return []


def last_round(collections, *, limit: int = 40) -> list[dict[str, Any]]:
    """The newest round's `sched` lines — written through the first
    collection's manifest, the node having no event log of its own."""
    from app import db
    if not collections:
        return []
    first = sorted(collections, key=lambda c: c.name)[0]
    try:
        with closing(db.connect(first)) as con:
            rows = con.execute("SELECT ts, message FROM events WHERE stage='sched' ORDER BY id DESC LIMIT ?",
                               (limit,)).fetchall()
    except Exception:   # noqa: BLE001 — a manifest that cannot be read has no round to report
        return []
    if not rows:
        return []
    newest = rows[0]["ts"] or 0
    lines = [{"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(r["ts"] or 0)), "line": r["message"]}
             for r in rows if (r["ts"] or 0) >= newest - ROUND_WINDOW_S]
    return lines[::-1]


def _row(collection, report, refusal: str | None, paused: set[str]) -> dict[str, Any]:
    from app import copy_state, db
    from app.hold import integrity
    codex = getattr(collection, "codex", None)
    if refusal:
        state = {"state": "refused", "reasons": [refusal]}
    else:
        try:
            state = copy_state.of(collection).to_json()
        except Exception as e:   # noqa: BLE001 — a state that cannot be derived is said, not guessed
            state = {"state": "unknown", "reasons": [f"{type(e).__name__}: {e}"]}
    embed = None
    try:
        with closing(db.connect(collection)) as con:
            embed = db.get_scope(con).get("embed_mode")
    except Exception:   # noqa: BLE001
        pass
    violated: list[str] = []
    if codex is not None and integrity.applies(codex):
        from pathlib import Path
        shard_tree = Path(collection.corpus_dir)
        violated = list(integrity.violations(shard_tree.parent.parent, shard_tree.parent.name, codex))
    stages = [{"stage": s.name, "state": s.state, "waiting": s.waiting, "reason": s.reason,
               **({"over": True} if s.over else {})}
              for s in (report.stages if report else []) if s.state != "idle" or s.waiting]
    doing = next((s["stage"] for s in stages if s["state"] == "running"), None)
    return {"name": collection.name,
            "collection_id": codex.collection_id if codex is not None else None,
            **state, "paused": collection.name in paused, "embed": embed,
            "integrity_violated": violated, "doing": doing, "stages": stages}


def _publish(c) -> dict[str, Any]:
    """The publisher's last pass per collection (app/worker.py `_publisher`),
    the `publish:<name>` control rows."""
    import json
    out: dict[str, Any] = {}
    for r in c.execute("SELECT key, value FROM control WHERE key LIKE 'publish:%'"):
        try:
            out[str(r["key"])[len("publish:"):]] = json.loads(r["value"])
        except (TypeError, ValueError):
            continue
    return out


def show() -> dict[str, Any]:
    from app import collections as collections_mod, health, node_state
    got = collections_mod.load_all(skip_unreadable=True)
    loaded, unreadable = got if isinstance(got, tuple) else (got, [])
    ov = overrides()
    refused = health._boot_refusals(loaded)
    reports, _ = health.check([c for c in loaded if c.name not in refused], mode=ov["mode"])
    by_name = {r.collection: r for r in reports}
    rows = [_row(c, by_name.get(c.name), refused.get(c.name), set(ov["paused_collections"]))
            for c in sorted(loaded, key=lambda c: c.name)]
    for bad in unreadable:
        name, why = (bad if isinstance(bad, tuple) else (str(bad), "does not load"))[:2]
        rows.append({"name": name, "collection_id": None, "state": "refused", "reasons": [str(why)],
                     "paused": False, "embed": None, "integrity_violated": [], "doing": None, "stages": []})
    from app import disk_guard
    from app import node_profile
    try:
        prof = node_profile.active()
        profile = prof.to_dict() if prof is not None else None
    except node_profile.ProfileError as e:
        profile = {"error": str(e)}
    if profile is not None:
        # what the GPU governor has APPLIED (app/gpu_level.py), beside what
        # the profile asks: a power limit or a clock ceiling, the fan, or why
        # not; None when the governor is not running
        from app import gpu
        profile["gpu_applied"] = gpu.governor().get("level")
    return {"mode": ov["mode"], "overrides": ov,
            # the quiet / day / night profile in force (platform.toml [schedule]):
            # what background CPU work and the card may use now, and until when
            "profile": profile,
            # every filesystem under a collection's vectors: below its floor,
            # nothing writes vectors there (app/disk_guard.py)
            "disk": disk_guard.status(loaded),
            "collections": rows,
            "jobs": node_state.peek(_jobs, [], table="jobs"),
            # the publisher's last pass per shared collection (07 §7.8 "all p2p")
            "publish": node_state.peek(_publish, {}, table="control"),
            "last_round": last_round(loaded)}
