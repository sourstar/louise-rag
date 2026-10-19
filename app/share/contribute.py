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
- WHAT IT HOLDS, IT BUNDLES (04 §4.5, amended 2026-10-19; RULE 1): before
  any GPU is spent, every open shard this peer does NOT lead is probed for
  the vectors this peer already holds — embedded before it had neighbours
  (`embed = "all"`), carried from a sister, imported — and what it holds
  is exported, judged and announced, whoever leads the shard. The node
  held 91 % of gutenberg's vectors and announced none of them, so every
  coverage map showed 0 % and the Perugia peer re-embedded parts the node
  had (74 % of its first pass; p2p-review-open-issues.md §1). The cost is
  the challenge rows per shard that has vectors (sixteen texts) and one
  read of each open shard's records; rendezvous then assigns only what
  nobody holds. ONE VERSION WHERE ONE EXISTS: a shard whose bundle a live
  neighbour both claims (digest `coverage`) and offers (digest `members`)
  is left to the fetch beat (app/share/accept.py `from_digests`), which
  takes that bundle in and compares it with the vectors held here
  ("duplicates are checks"); with many holders joining a network, each
  bundling what it holds would make as many versions of every part, and a
  version that exists and is served is reused, not remade (RULE 1).
- WORK: `hold vectors build` over the shards this peer leads
  (app/hold/vectors_build.py), then `hold vectors export` of each
  (app/hold/export.py) into
  `state/bundles/<space>/<slice>/<NNNNN>/<bundle_id hex>/`, the bundle
  through this peer's own ladder — T0, T1, T2; T3 is for another key's
  encoder — and, passing, accepted in the coverage map (`local`); a
  `bundle` event per bundle kept as seen for the beat to forward — WHEN
  this peer serves the collection (`serving`): a bundle nobody can fetch
  from its announcer sent the node into two-minute waits per bundle
  (2026-10-19, §7); unannounced, the bundle is still listed under the
  digest's `coverage`, and offered under `members` once this peer serves.
  The seeder serves the bundles as members when the collection is shared.
`--once` does this and exits; the loop repeats it every interval.
"""
from __future__ import annotations

import os
import shutil
import time
from pathlib import Path
from typing import Any, Callable, Sequence

from app import embed_marks as marks
from app.hold import collection as col, shard as shard_mod, envelope as env, export as exp, \
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
        return ["no snapshot in the SHARD: nothing to embed for"]
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


def _leading(codex, snapshot: sn.Snapshot, con, *, live: Sequence[rendezvous.Peer],
             now: float, record_open: bool, redundant=(), union=None) -> list[tuple[str, rendezvous.Peer | None]]:
    """(shard id, current leader) of every open shard, in snapshot order —
    04 §4.5's ordering with the grace cascade. `record_open` writes a shard's
    first-open time when this peer sees it open for the first time (the
    window starts there); the embed stage reads only (app/share/leading.py)."""
    all_rows = cov.rows(con)
    opened = cov.opened_at(con)
    limit = cov.period_seconds(codex.work.per)
    grace_s = float(getattr(codex.update, "grace_days", 7)) * 86400.0
    by_shard: dict[str, list[dict[str, Any]]] = {}
    for r in all_rows:
        by_shard.setdefault(r["shard"], []).append(r)
    tokens = {cov.shard_id(codex, f"{s.slice}/{s.n:05d}"): int(s.tokens) for s in snapshot.shards}
    out: list[tuple[str, rendezvous.Peer | None]] = []
    for v in cov.shards_view(codex, snapshot, all_rows, now=now, redundant=redundant, union=union):
        if not v["open"]:
            continue
        if record_open and v["shard"] not in opened:   # first seen open here: the window starts
            con.execute("INSERT OR IGNORE INTO coverage_shards(shard, opened_at) VALUES(?,?)",
                        (v["shard"], messages.time_text(now)))
            opened[v["shard"]] = messages.time_text(now)
        ranking = rendezvous.ranking(rendezvous.ShardWork(v["shard"], tokens[v["shard"]]),
                                     live, limit)
        lead = _ranking_leader(ranking, by_shard.get(v["shard"], []), opened.get(v["shard"]),
                               now=now, grace_s=grace_s)
        out.append((v["shard"], lead))
    return out


def leaders(codex, snapshot: sn.Snapshot, con, *, live: Sequence[rendezvous.Peer],
            now: float | None = None, redundant=(), union=None) -> dict[str, str | None]:
    """Every open shard's current leader key (None: no eligible peer). Read-only."""
    now = time.time() if now is None else now
    return {sid: (lead.key if lead is not None else None)
            for sid, lead in _leading(codex, snapshot, con, live=live, now=now, record_open=False,
                                      redundant=redundant, union=union)}


def _shard_of(snapshot: sn.Snapshot, sid: str) -> sn.Shard:
    slice_, n = sid.rsplit("/", 2)[1:]
    return next(s for s in snapshot.shards if s.slice == slice_ and s.n == int(n))


def open_shards(codex, snapshot: sn.Snapshot, con, *, me: str, live: Sequence[rendezvous.Peer],
                now: float | None = None, redundant=(), union=None) -> tuple[list[sn.Shard], list[sn.Shard]]:
    """`(led, others)`: the open shards whose current leader is this peer,
    and the open shards it does not lead — led by another live peer, or by
    nobody eligible. Records each shard's first-open time (the window
    starts there). `redundant`: deprecated copies held beside their current
    one, counted toward no part (`deprecate.redundant_here`)."""
    now = time.time() if now is None else now
    led, others = [], []
    for sid, lead in _leading(codex, snapshot, con, live=live, now=now, record_open=True, redundant=redundant,
                              union=union):
        (led if lead is not None and lead.key == me else others).append(_shard_of(snapshot, sid))
    return led, others


def shards_i_lead(codex, snapshot: sn.Snapshot, con, *, me: str,
                  live: Sequence[rendezvous.Peer], now: float | None = None) -> list[sn.Shard]:
    """The open shards whose current leader is this peer."""
    return open_shards(codex, snapshot, con, me=me, live=live, now=now)[0]


def bundle_dir(root: Path, name: str, codex, shard: str, bundle_id: str) -> Path:
    """09 §9.7: `state/bundles/<space>/<slice>/<NNNNN>/<bundle_id hex>/`."""
    return (Path(root) / name / "state" / "bundles" / codex.space_segment / shard
            / shard_mod._hex(bundle_id))


def calibration_tensors(root: Path, name: str, codex):
    """`(reference, envelope)` — 09 §9.12's two tensors of the calibration
    member this peer holds, what T0 and T2 are judged against."""
    from safetensors.numpy import load_file
    member = Path(root) / name / "shard" / shard_mod.calibration_paths(codex.space.calibration.set)[0]
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


def offered_elsewhere(codex, con, node_con, *, now: float, skip=None, deprecated=None,
                      nobody=None) -> dict[str, list[str]]:
    """Per open shard id, the bundle ids a live neighbour both CLAIMS for it
    (its digest's `coverage`, folded into the map as `claimed_by`) and
    OFFERS (its digest's `members`), and this peer has neither accepted nor
    refused — what `accept.from_digests` takes in at the fetch beat. A
    holder leaves such a shard to that fetch rather than bundling a second
    version of it."""
    if node_con is None:
        return {}
    from app.share import accept
    offered = {p["bundle_id"] for p, _hints in accept.listed_bundles(node_con, codex, con, now=now, skip=skip,
                                                                     deprecated=deprecated, nobody=nobody)}
    if not offered:
        return {}
    out: dict[str, list[str]] = {}
    for r in cov.rows(con):
        if r["bundle_id"] in offered and not r["accepted_at"] and r["claimed_by"]:
            out.setdefault(r["shard"], []).append(r["bundle_id"])
    return out


def claimed_uids(root: Path, name: str, codex, con, sid: str, *, own_key: str | None = None) -> frozenset[str]:
    """The uids EVERY accepted bundle held here for the shard already claims
    (their `possession.json`, 09 §9.7), whoever produced it: what a further
    export of the shard leaves out. A holder bundles the vectors it
    EMBEDDED and nobody bundled — never the rows it landed from another
    producer's bundle, which would be a third copy signed as its own
    (Perugia, 2026-10-19: 44 bundles of the node's rows); and a part
    bundled from held vectors and then completed by its leader costs a
    bundle of the new records only, never the part twice (RULE 1;
    operator, 2026-10-19: minimum redundancy). `own_key` is kept for the
    callers that pass it and narrows nothing."""
    import json
    out: set[str] = set()
    for r in cov.rows(con, shard=cov.shard_id(codex, sid)):
        if not r["accepted_at"]:
            continue
        p = bundle_dir(root, name, codex, sid, r["bundle_id"]) / "possession.json"
        try:
            out.update(json.loads(p.read_bytes()).keys())
        except (OSError, ValueError, AttributeError):
            continue                               # a bundle not here (set aside, moved): claims nothing
    return frozenset(out)


def _bundle_shard(root: Path, name: str, codex, snapshot: sn.Snapshot, shard: sn.Shard, *, key, own_key: str,
                  embed, vectors_for, con, node_con, now: float, say, out: dict[str, Any],
                  serving: bool, held: bool, held_books=None, stop=None) -> str:
    """One shard's held vectors — the records no accepted bundle held here
    claims yet — exported into bundles, each through this peer's own
    ladder, recorded in the map and, serving, announced. Returns
    `"exported"`, `"nothing"` (this peer holds no vector of the shard),
    `"claimed"` (everything it holds is in a bundle held here already) or
    `"interrupted"` (a stop between two documents; nothing written).

    DECIDED BEFORE ANY CHUNKING when `held_books` is given: the unclaimed
    records' documents are asked of the vector table by `book_id`, and only
    those with a vector here are chunked to list their chunk ids — chunking
    a part's whole books in pure Python to find nothing held cost 9.5
    minutes a pass and waited out every SIGTERM (Perugia, 2026-10-19)."""
    sid = f"{shard.slice}/{shard.n:05d}"
    scratch = root / name / "state" / "bundles" / f".export-{os.getpid()}-{shard.n:05d}"
    shutil.rmtree(scratch, ignore_errors=True)
    claimed = claimed_uids(root, name, codex, con, sid, own_key=own_key)
    only = None
    if held_books is not None:
        records = landing.records_for(root, name, snapshot, sid, None)
        unclaimed = [r for r in records if r["uid"] not in claimed]
        if not unclaimed:
            return "claimed" if claimed else "nothing"

        def _book(r) -> int | None:
            try:
                return int(r.get("native_id") or str(r["uid"]).rsplit(":", 1)[-1])
            except (TypeError, ValueError):
                return None
        books = {b for b in (_book(r) for r in unclaimed) if b is not None}
        with_vectors = held_books(sorted(books)) if books else set()
        only = frozenset(r["uid"] for r in unclaimed if _book(r) in with_vectors)
        if not only:
            return "claimed" if claimed else "nothing"
    try:
        exported = exp.export(root, name, shard=sid, dest=scratch, vectors_for=vectors_for,
                              embed=embed, key=key, snapshot_id=snapshot.snapshot_id,
                              exclude_uids=claimed, only_uids=only, stop=stop)
    except exp.ExportInterrupted:
        shutil.rmtree(scratch, ignore_errors=True)
        out["interrupted"] = True
        return "interrupted"
    except exp.NothingHeld:
        return "claimed" if claimed else "nothing"
    except exp.ExportError as e:
        out["refused"].append(f"{sid}: {e}")
        say(f"[{name}] share contribute: {sid}: {e}")
        return "exported"
    what = "held" if held else "built"
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
                 "tiers": tiers, "dir": str(final), "from": what}
        # stamped and announced at the moment, not with the pass's clock: the
        # node's holder pass ran for hours and would have dated 1,296 rows and
        # events at its start (Perugia found the same on the fetch beat)
        at = time.time()
        cov.record(con, bundle_id=b["bundle_id"], shard=cov.shard_id(codex, sid), producer=own_key,
                   uids=list(header.get("uids") or []), chunks=len(report.admitted) if report.ok else 0,
                   tiers=tiers, source=cov.LOCAL, accepted_at=messages.time_text(at) if report.ok else None)
        cov.note_open(con, codex, sid, now=messages.time_text(at))
        out["bundles"].append(entry)
        if report.ok:
            out["accepted"].append(b["bundle_id"])
            if serving:
                announce(node_con, codex, snapshot, {**header, "bundle_id": b["bundle_id"]}, member,
                         key=key, own_key=own_key, now=at)
                out["events"] += 1
                say(f"[{name}] share contribute: {sid}: bundle {b['bundle_id'][:23]}… "
                    f"({b['chunks']} chunks, {what}) accepted and announced")
            else:
                out["unannounced"] += 1
                say(f"[{name}] share contribute: {sid}: bundle {b['bundle_id'][:23]}… "
                    f"({b['chunks']} chunks, {what}) accepted, NOT announced: this peer does not serve "
                    f"{name} (04 §4.2: announcing what you do not serve would be a lie) — in the "
                    f"digest's coverage now, offered as a member once it serves")
        else:
            failed = [t.tier for t in report.tiers if t.failed]
            # the clause that failed, not the tier alone: lang=en/00043 was
            # "refused at ['T0']" twice on the node and nobody could say why
            # (München, 2026-10-19)
            why = "; ".join(f"{t.tier}: {t.why}" for t in report.tiers if t.failed)
            refused_rows = len(report.refused)
            out["refused"].append(f"{sid}: {b['bundle_id'][:23]}… refused at {failed} — {why}"
                                  + (f"; {refused_rows} row(s) refused: {report.refused[0][1]}" if refused_rows else ""))
            say(f"[{name}] share contribute: {sid}: own bundle refused at {failed} — {why}"
                + (f"; {refused_rows} row(s) refused, e.g. {report.refused[0][0]}: {report.refused[0][1]}"
                   if refused_rows else "")
                + " — not announced; the vectors stay here for `vectors check`")
    shutil.rmtree(scratch, ignore_errors=True)
    return "exported"


def once(root: Path, name: str, codex, *, key, own_key: str | None, embed, vectors_for, write,
         live: Sequence[rendezvous.Peer], con, node_con, now: float | None = None, stop=None,
         log: Callable[[str], None] | None = None, batch: int = 64,
         before_write: Callable[[], None] | None = None, serving: bool = True,
         collection=None, held_books=None) -> dict[str, Any]:
    """One pass (07 §7.3 `--once`): first the vectors this peer already
    holds for the open shards it does not lead, bundled (no GPU beyond the
    challenge rows); then the shards this peer leads, built, exported,
    accepted by its own ladder and — `serving` — announced."""
    root = Path(root)
    now = time.time() if now is None else now
    say = log or (lambda *_a: None)
    shard_dir = col.shard_dir_of(root, name)
    snapshot = seeder.current_snapshot(shard_dir)
    reasons = preconditions(root, name, codex, snapshot, own_key=own_key)
    if reasons:
        raise ContributeRefused(f"{name}: not contributing yet — " + "; ".join(reasons))
    assert snapshot is not None and own_key is not None
    me_live = list(live)
    from app.share import accept, counting, deprecate as deprecate_mod
    deprecated = deprecate_mod.deprecated(root, name, codex)
    view = counting.view_kwargs(root, name, codex, con)
    mine, others = open_shards(codex, snapshot, con, me=own_key, live=me_live, now=now, **view)
    out: dict[str, Any] = {"snapshot_id": snapshot.snapshot_id, "peers": len(me_live),
                           "mine": [f"{s.slice}/{s.n:05d}" for s in mine], "built": 0,
                           "held": [], "probed": 0, "left_to_fetch": [], "bundles": [], "accepted": [],
                           "refused": [], "events": 0, "unannounced": 0, "interrupted": False,
                           "marked": 0, "claimed": []}
    common = dict(key=key, own_key=own_key, embed=embed, vectors_for=vectors_for, con=con,
                  node_con=node_con, now=now, say=say, out=out, serving=serving, held_books=held_books, stop=stop)
    if not mine and not others:
        say(f"[{name}] share contribute: no open shard among {len(me_live)} peer(s)")
        return out
    if not serving:
        say(f"[{name}] share contribute: this peer does not serve {name} (share = false in its file, or a "
            f"mode that does not seed): bundles are accepted here and announced once it serves")
    # RULE 1 — what this peer already holds for a shard another peer leads is
    # bundled first, whoever leads it: no GPU, and the leader embeds only
    # what still lacks a bundle (04 §4.5, amended 2026-10-19)
    from app.share import withdraw as withdraw_mod
    elsewhere = offered_elsewhere(codex, con, node_con, now=now, skip=withdraw_mod.withdrawn(root, name, codex),
                                  deprecated=deprecated, nobody=accept.nobody_served_until())
    for shard in others:
        if stop is not None and stop.is_set():
            out["interrupted"] = True
            break
        sid = f"{shard.slice}/{shard.n:05d}"
        if cov.shard_id(codex, sid) in elsewhere:
            # a version exists and is served: the fetch beat takes it in and
            # compares it with what is held here — not a second version
            out["left_to_fetch"].append(sid)
            continue
        out["probed"] += 1
        got = _bundle_shard(root, name, codex, snapshot, shard, held=True, **common)
        if got == "interrupted":
            break
        if got == "exported":
            out["held"].append(sid)
        elif got == "claimed":
            out["claimed"].append(sid)         # every row held is in a bundle held here already, anyone's
    if out["held"]:
        say(f"[{name}] share contribute: {len(out['held'])} of {out['probed']} open shard(s) led elsewhere "
            f"bundled from vectors held here, no GPU spent (RULE 1)")
    if out["claimed"]:
        say(f"[{name}] share contribute: {len(out['claimed'])} of {out['probed']} open shard(s) led elsewhere hold "
            f"only rows an accepted bundle already carries: nothing to bundle (a holder bundles what it embedded "
            f"and nobody bundled)")
    def _fetch_line() -> None:
        if out["left_to_fetch"]:
            say(f"[{name}] share contribute: {len(out['left_to_fetch'])} open shard(s) left to the fetch beat: a "
                f"neighbour offers a bundle for each (one version where one exists)")
    if not mine:
        if not out["interrupted"]:
            say(f"[{name}] share contribute: no open shard this peer leads among {len(me_live)} peer(s)")
        _fetch_line()
        return out
    for shard in mine:
        sid = f"{shard.slice}/{shard.n:05d}"
        if stop is not None and stop.is_set():
            out["interrupted"] = True
            break
        if cov.shard_id(codex, sid) in elsewhere:
            # led here, but a version exists and is served: the fetch beat
            # takes it in — not the GPU (Perugia, 2026-10-19: 632 of 636 led
            # parts had a neighbour's bundle offered and were re-embedded)
            out["left_to_fetch"].append(sid)
            continue
        built = vb.build(codex, shards=[shard],
                         records_for=lambda s: landing.records_for(root, name, snapshot,
                                                                   f"{s.slice}/{s.n:05d}", None),
                         vectors_for=vectors_for, embed=embed, write=write, batch=batch, stop=stop,
                         log=say, before_write=before_write)
        out["built"] += built.chunks_written
        # the documents the build completed are marked embedded in the
        # manifest, AFTER the vectors are written (invariant 2) — the build
        # never did, and the worker would have embedded them again (372 on
        # the node, München 2026-10-19); interrupted, what is whole is marked
        if built.chunks_written:
            try:
                records = landing.records_for(root, name, snapshot, sid, None)
                out["marked"] += marks.mark_complete(con, collection, codex, records, vectors_for)
            except Exception as e:   # noqa: BLE001 — a mark is bookkeeping; the vectors are written
                say(f"[{name}] share contribute: {sid}: marking the documents embedded: "
                    f"{type(e).__name__}: {e}"[:300])
        if built.interrupted:
            out["interrupted"] = True
            break
        got = _bundle_shard(root, name, codex, snapshot, shard, held=False, **common)
        if got == "interrupted":
            break
        if got == "nothing":
            out["refused"].append(f"{sid}: built and nothing held to export")
            say(f"[{name}] share contribute: {sid}: built, and this peer holds no vector of it to export")
        elif got == "claimed":
            say(f"[{name}] share contribute: {sid}: every record held is in a bundle made here already")
    _fetch_line()
    return out
