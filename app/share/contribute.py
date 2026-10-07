"""`share contribute` (07 §7.3; 04 §4.5; 09 §9.7, §9.9, §9.10, §9.16),
migration step 9e, third brick: the shards this peer leads, embedded,
bundled, accepted by its own ladder, and announced.

- BEFORE CONTRIBUTING (04 §4.5): this peer's own `roots` and `chunks`
  passed on what it holds; a `records-vs-origin` attestation exists for
  the snapshot — its own or another key's; its encoder passed the
  calibration gate for the space (03 §3.3). Records are checked before GPU
  is spent; no vector precondition.
- RENDEZVOUS: over the OPEN shards of the current snapshot (the coverage
  map, app/share/coverage.py), the live peers with a benchmark for the
  space (their digests) and this peer with its own, `rendezvous.mine`
  says which shards this peer leads; a leader whose grace window has run
  out with no bundle hands the shard to the next in the ordering (the
  cascade). Alone, a peer leads everything.
- WORK: `hold vectors build` over those shards (app/hold/vectors_build.py),
  then `hold vectors export` of each (app/hold/export.py) into
  `state/bundles/<space>/<slice>/<NNNNN>/<bundle_id hex>/`, the bundle
  through this peer's own ladder — T0, T1, T2; T3 is for another key's
  encoder — and, passing, accepted in the coverage map (`local`); a
  `bundle` event per bundle kept as seen for the beat to forward. The
  seeder serves the bundles as members when the collection is shared.
`--once` does this and exits; the loop repeats it every interval.
"""
from __future__ import annotations

import os
import shutil
import time
from pathlib import Path
from typing import Any, Callable, Sequence

from app.hold import collection as col, drag as drag_mod, envelope as env, export as exp, \
    land as landing, snapshot as sn, vectors_build as vb
from app.hold.codex import CodexError
from app.share import coverage as cov, members as mem, messages, peers, rendezvous, seeder

INTERVAL_S = 15 * 60.0


class ContributeRefused(CodexError):
    """A rule stood in the way before any GPU was spent (07 §7.7's exit 2)."""
    exit_code = 2


def preconditions(root: Path, name: str, codex, snapshot: sn.Snapshot | None, *,
                  own_key: str | None) -> list[str]:
    """04 §4.5's three, as the reasons this peer may NOT contribute yet;
    empty when it may."""
    from app import copy_state
    from app.hold import runs
    reasons: list[str] = []
    if snapshot is None:
        return ["no snapshot in the drag: nothing to embed for"]
    if own_key is None:
        reasons.append("this peer has no key, so it cannot sign bundles (07 §7.8)")
    bodies = copy_state.attestations(root, name, codex)
    if own_key is not None:
        for check in ("roots", "chunks"):
            if not bodies.of(own_key, check)[0]:
                reasons.append(f"this peer's own {check} has not passed here: `hold check "
                               f"{name} --checks roots,chunks`")
    if not any(b.get("check") == "records-vs-origin" and b.get("result") == "pass"
               and b.get("snapshot_hash") == snapshot.hash for b in bodies):
        reasons.append(f"no records-vs-origin attestation for snapshot {snapshot.snapshot_id} — "
                       f"this peer's or another key's (04 §4.5)")
    from app.hold import integrity
    bad = integrity.violations(root, name, codex)
    if bad:
        reasons.append(f"integrity violated: {len(bad)} record shard(s) no certifier-signed snapshot lists "
                       f"({bad[0]}…) — no bundle is published until they are replaced (03 §3.8)")
    if not runs.applies(codex):
        try:
            vb.require_statement(root, name, codex)
        except vb.BuildRefused as e:
            reasons.append(str(e))
    return reasons


def _ranking_leader(ranking: Sequence[rendezvous.Peer], shard_rows: list[dict[str, Any]],
                    opened_at: str | None, *, now: float, grace_s: float) -> rendezvous.Peer | None:
    """04 §4.5's cascade: the top eligible peer leads; its window runs from
    the later of the shard's opening and its last accepted bundle; when it
    has run out with no bundle, the next in the ordering leads, its window
    starting where the last one ended. When every window has run out the
    cascade starts over at the top. With no opening known, the top leads."""
    if not ranking:
        return None
    since = messages.parse_time(opened_at) if opened_at else None
    for p in ranking:
        last = [messages.parse_time(r["accepted_at"]) for r in shard_rows
                if r["accepted_at"] and r["producer"] == p.key]
        mine = max(last) if last else None
        start = max(x for x in (since, mine) if x is not None) if (since is not None or mine is not None) else None
        if start is None or now < start + grace_s:
            return p
        since = start + grace_s
    return ranking[0]


def shards_i_lead(codex, snapshot: sn.Snapshot, con, *, me: str,
                  live: Sequence[rendezvous.Peer], now: float | None = None) -> list[sn.Shard]:
    """The open shards whose current leader is this peer."""
    now = time.time() if now is None else now
    all_rows = cov.rows(con)
    opened = cov.opened_at(con)
    limit = cov.period_seconds(codex.work.per)
    grace_s = float(getattr(codex.update, "grace_days", 7)) * 86400.0
    by_shard: dict[str, list[dict[str, Any]]] = {}
    for r in all_rows:
        by_shard.setdefault(r["shard"], []).append(r)
    out = []
    for v in cov.shards_view(codex, snapshot, all_rows, now=now):
        if not v["open"]:
            continue
        if v["shard"] not in opened:            # first seen open here: the window starts
            con.execute("INSERT OR IGNORE INTO coverage_shards(shard, opened_at) VALUES(?,?)",
                        (v["shard"], messages.time_text(now)))
            opened[v["shard"]] = messages.time_text(now)
        work = rendezvous.ShardWork(v["shard"], int(next(
            s.tokens for s in snapshot.shards
            if cov.shard_id(codex, f"{s.slice}/{s.n:05d}") == v["shard"])))
        ranking = rendezvous.ranking(work, live, limit)
        lead = _ranking_leader(ranking, by_shard.get(v["shard"], []), opened.get(v["shard"]),
                               now=now, grace_s=grace_s)
        if lead is not None and lead.key == me:
            slice_, n = v["shard"].rsplit("/", 2)[1:]
            out.append(next(s for s in snapshot.shards if s.slice == slice_ and s.n == int(n)))
    return out


def bundle_dir(root: Path, name: str, codex, shard: str, bundle_id: str) -> Path:
    """09 §9.7: `state/bundles/<space>/<slice>/<NNNNN>/<bundle_id hex>/`."""
    return (Path(root) / name / "state" / "bundles" / codex.space_segment / shard
            / drag_mod._hex(bundle_id))


def calibration_tensors(root: Path, name: str, codex):
    """`(reference, envelope)` — 09 §9.12's two tensors of the calibration
    member this peer holds, what T0 and T2 are judged against."""
    from safetensors.numpy import load_file
    member = Path(root) / name / "drag" / drag_mod.calibration_paths(codex.space.calibration.set)[0]
    tensors = load_file(str(member))
    return tensors["vectors"].tolist(), tensors["envelope"]


def _own_ladder(root: Path, name: str, codex, snapshot: sn.Snapshot, where: Path):
    """This peer's own bundle through T0–T2 (03 §3.5); T3 would re-embed
    with the encoder that produced it and prove nothing."""
    from app.hold import admit as adm, vectors as vec
    reference, envelope = calibration_tensors(root, name, codex)
    header, rows, possession, bundle_id = vec.read(where)
    records = landing.records_for(root, name, snapshot, header["shard"], header["uids"])
    mine = {r["uid"]: r["content_hash"] for r in records}
    return adm.judge(codex, header, rows, possession, bundle_id, envelope=envelope,
                     reference=reference, mine=mine, embed=None), header


def announce(con, codex, snapshot: sn.Snapshot, header: dict[str, Any], member: mem.Member, *,
             key, own_key: str, now: float) -> str:
    """09 §9.9's `bundle` event, kept as seen for the beat to forward."""
    uids = list(header.get("uids") or [])
    payload = {"collection": codex.collection_id, "space_id": codex.space_id,
               "snapshot_id": snapshot.snapshot_id, "shard": header["shard"],
               "uids": [uids[0], uids[-1]] if uids else [], "bundle_id": header["bundle_id"],
               "root": member.root, "bytes": member.bytes,
               # the four files, from which a fetcher computes the four torrents
               # (09 §9.16; §9.9 amendment of 2026-10-07)
               "files": member.to_dict()["files"]}
    body = messages.event_body(key=own_key, type_="bundle", payload=payload, now=now)
    eid = messages.event_id(body)
    from app.share import attest
    attest.ensure_schema(con)
    if not peers.seen(con, eid):
        peers.mark_seen(con, eid, now=now, time_text=body["time"], type_="bundle", hops=peers.HOPS,
                        envelope=env.sign(env.EVENT, body, key))
        attest.mark_acted(con, eid)                 # ours: nothing to take in here
    return eid


def once(root: Path, name: str, codex, *, key, own_key: str | None, embed, vectors_for, write,
         live: Sequence[rendezvous.Peer], con, node_con, now: float | None = None, stop=None,
         log: Callable[[str], None] | None = None, batch: int = 64,
         before_write: Callable[[], None] | None = None) -> dict[str, Any]:
    """One pass (07 §7.3 `--once`): the shards this peer leads now, built,
    exported, accepted by its own ladder and announced."""
    root = Path(root)
    now = time.time() if now is None else now
    say = log or (lambda *_a: None)
    drag_dir = col.drag_of(root, name)
    snapshot = seeder.current_snapshot(drag_dir)
    reasons = preconditions(root, name, codex, snapshot, own_key=own_key)
    if reasons:
        raise ContributeRefused(f"{name}: not contributing yet — " + "; ".join(reasons))
    assert snapshot is not None and own_key is not None
    me_live = list(live)
    mine = shards_i_lead(codex, snapshot, con, me=own_key, live=me_live, now=now)
    out: dict[str, Any] = {"snapshot_id": snapshot.snapshot_id, "peers": len(me_live),
                           "mine": [f"{s.slice}/{s.n:05d}" for s in mine], "built": 0,
                           "bundles": [], "accepted": [], "refused": [], "events": 0,
                           "interrupted": False}
    if not mine:
        say(f"[{name}] share contribute: no open shard this peer leads among {len(me_live)} peer(s)")
        return out
    for shard in mine:
        sid = f"{shard.slice}/{shard.n:05d}"
        if stop is not None and stop.is_set():
            out["interrupted"] = True
            break
        built = vb.build(codex, shards=[shard],
                         records_for=lambda s: landing.records_for(root, name, snapshot,
                                                                   f"{s.slice}/{s.n:05d}", None),
                         vectors_for=vectors_for, embed=embed, write=write, batch=batch, stop=stop,
                         log=say, before_write=before_write)
        out["built"] += built.chunks_written
        if built.interrupted:
            out["interrupted"] = True
            break
        scratch = root / name / "state" / "bundles" / f".export-{os.getpid()}-{shard.n:05d}"
        shutil.rmtree(scratch, ignore_errors=True)
        try:
            exported = exp.export(root, name, shard=sid, dest=scratch, vectors_for=vectors_for,
                                  embed=embed, key=key, snapshot_id=snapshot.snapshot_id)
        except exp.ExportError as e:
            out["refused"].append(f"{sid}: {e}")
            say(f"[{name}] share contribute: {sid}: {e}")
            continue
        for b in exported["bundles"]:
            final = bundle_dir(root, name, codex, sid, b["bundle_id"])
            if final.is_dir():
                shutil.rmtree(b["path"], ignore_errors=True)        # the same bundle, held already
            else:
                final.parent.mkdir(parents=True, exist_ok=True)
                Path(b["path"]).replace(final)
            cov.record(con, bundle_id=b["bundle_id"], shard=cov.shard_id(codex, sid), producer=own_key,
                       uids=[], chunks=0, tiers={t: "skipped" for t in cov.TIERS}, source=cov.LOCAL,
                       accepted_at=None)
            report, header = _own_ladder(root, name, codex, snapshot, final)
            tiers = cov._tiers_of(report)
            member = mem.bundle_member(codex.space_segment, final)
            entry = {"bundle_id": b["bundle_id"], "shard": sid, "chunks": b["chunks"],
                     "member": member.name, "root": member.root, "bytes": member.bytes,
                     "tiers": tiers, "dir": str(final)}
            cov.record(con, bundle_id=b["bundle_id"], shard=cov.shard_id(codex, sid), producer=own_key,
                       uids=list(header.get("uids") or []), chunks=len(report.admitted) if report.ok else 0,
                       tiers=tiers, source=cov.LOCAL, accepted_at=messages.time_text(now) if report.ok else None)
            cov.note_open(con, codex, sid, now=messages.time_text(now))
            out["bundles"].append(entry)
            if report.ok:
                out["accepted"].append(b["bundle_id"])
                announce(node_con, codex, snapshot, {**header, "bundle_id": b["bundle_id"]}, member,
                         key=key, own_key=own_key, now=now)
                out["events"] += 1
                say(f"[{name}] share contribute: {sid}: bundle {b['bundle_id'][:23]}… "
                    f"({b['chunks']} chunks) accepted and announced")
            else:
                failed = [t.tier for t in report.tiers if t.failed]
                out["refused"].append(f"{sid}: {b['bundle_id'][:23]}… refused at {failed}")
                say(f"[{name}] share contribute: {sid}: own bundle refused at {failed} — not "
                    f"announced; the vectors stay here for `vectors check`")
        shutil.rmtree(scratch, ignore_errors=True)
    return out
