"""The coverage map (02 §2.5; 09 §9.10) — this peer's own view of which
chunks have vectors, from which producer, through which tiers: one row per
bundle in the collection's manifest (`state/manifest.sqlite`, 07 §7.8).
Never a member, never signed, never the same on two peers. Migration step
9e, first brick.

- A row is written when a bundle is ACCEPTED here — `hold vectors import`
  after 03 §3.5's ladder (`source: fetched`), an archive's shard
  (`archive`), or this peer's own export (`local`, `accepted_at: null`
  until its own ladder has run over it).
- A claim heard in a neighbour's digest is recorded as `claimed_by` on the
  bundle's row — a row with nothing else known, when the bundle has not
  arrived — and counts for nothing (02 §2.5): a shard is covered here only
  by bundles accepted here.
- Per shard, derived: `covered`, `open`, the leader and the grace window
  of 04 §4.5 (when the live peers are given), and when the shard was first
  seen open. Per producer: chunks, bundles, the signed work in the trailing
  work period. Vitality (04 §4.7): actively-held while any one key's signed
  work — bundles accepted here, attestations held here — reaches
  `work.tokens` per `work.per`.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from typing import Any, Iterable, Sequence

from app.hold import snapshot as sn
from app.share import messages, rendezvous

FETCHED, LOCAL, ARCHIVE = "fetched", "local", "archive"
SOURCES = (FETCHED, LOCAL, ARCHIVE)
TIERS = ("T0", "T1", "T2", "T3")
#: 04 §4.7: a deterministic check reproduces the shards it names whole
DETERMINISTIC = ("roots", "records", "chunks", "records-vs-origin", "license", "signature",
                 "centres", "labels", "figures", "anchor", "origin-seal", "merge")
#: … and a statistical one re-embeds a sample, counted by its chunks
STATISTICAL = ("vectors-sample", "calibration", "evaluation")
ACTIVE, DORMANT = "actively-held", "dormant"

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS coverage ("
    " bundle_id TEXT PRIMARY KEY, shard TEXT NOT NULL, producer TEXT NOT NULL DEFAULT '',"
    " uid_first TEXT, uid_last TEXT, chunks INTEGER NOT NULL DEFAULT 0,"
    " tiers TEXT NOT NULL DEFAULT '{}', accepted_at TEXT, source TEXT NOT NULL,"
    " verified_on_use INTEGER NOT NULL DEFAULT 0, claimed_by TEXT NOT NULL DEFAULT '[]')",
    "CREATE INDEX IF NOT EXISTS idx_coverage_shard ON coverage(shard)",
    "CREATE TABLE IF NOT EXISTS coverage_shards (shard TEXT PRIMARY KEY, opened_at TEXT NOT NULL)",
)
_DUR = re.compile(r"^P(?:(\d+)Y)?(?:(\d+)M)?(?:(\d+)W)?(?:(\d+)D)?"
                  r"(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$")


def ensure_schema(con: sqlite3.Connection) -> sqlite3.Connection:
    for stmt in _SCHEMA:
        con.execute(stmt)
    return con


# ---- names and numbers ---------------------------------------------------------------

def shard_id(codex, shard: str) -> str:
    """04 §4.5's `"<collection_id>/<space>/<slice>/<n>"` from a bundle header's
    `"<slice>/<n>"`."""
    return f"{codex.collection_id}/{codex.space_segment}/{shard}"


def period_seconds(iso: str) -> float:
    """An ISO 8601 duration as seconds; a month counts thirty days and a
    year 365 (04 §4.5)."""
    m = _DUR.match(iso or "")
    if not m or not any(m.groups()):
        raise ValueError(f"not an ISO 8601 duration: {iso!r}")
    y, mo, w, d, h, mi, s = (int(g) if g else 0 for g in m.groups())
    return float(y * 365 * 86400 + mo * 30 * 86400 + w * 7 * 86400 + d * 86400
                 + h * 3600 + mi * 60 + s)


def _tiers_of(report) -> dict[str, str]:
    """`{T0: pass|fail|skipped, …}` from a ladder report (03 §3.5); a tier
    the ladder never reached is `skipped`."""
    out = {t: "skipped" for t in TIERS}
    for t in getattr(report, "tiers", ()):
        out[t.tier] = t.verdict
    return out


# ---- rows -----------------------------------------------------------------------------

def record(con, *, bundle_id: str, shard: str, producer: str, uids: Sequence[str], chunks: int,
           tiers: dict[str, str], source: str, accepted_at: str | None) -> None:
    """One bundle's row, written whole; `claimed_by` and `verified_on_use`
    are kept across re-writes (a re-import is the same bundle)."""
    if source not in SOURCES:
        raise ValueError(f"source is one of {SOURCES}, got {source!r}")
    ensure_schema(con)
    first = str(uids[0]) if uids else None
    last = str(uids[-1]) if uids else None
    con.execute(
        "INSERT INTO coverage(bundle_id, shard, producer, uid_first, uid_last, chunks, tiers, "
        "accepted_at, source) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(bundle_id) DO UPDATE SET "
        "shard=excluded.shard, producer=excluded.producer, uid_first=excluded.uid_first, "
        "uid_last=excluded.uid_last, chunks=excluded.chunks, tiers=excluded.tiers, "
        "accepted_at=excluded.accepted_at, source=excluded.source",
        (bundle_id, shard, producer, first, last, int(chunks), json.dumps(tiers, sort_keys=True),
         accepted_at, source))


def record_from_report(con, codex, report, header: dict[str, Any], *, source: str,
                       now: str) -> dict[str, Any]:
    """After `hold vectors import` (or this peer's own ladder over its own
    bundle): the row, accepted now when the ladder passed."""
    tiers = _tiers_of(report)
    accepted = now if getattr(report, "ok", False) else None
    uids = list(header.get("uids") or [])
    chunks = len(getattr(report, "admitted", ()) or ()) if accepted else 0
    record(con, bundle_id=report.bundle_id, shard=shard_id(codex, header["shard"]),
           producer=report.producer, uids=uids, chunks=chunks, tiers=tiers, source=source,
           accepted_at=accepted)
    note_open(con, codex, header["shard"], now=now)
    return row(con, report.bundle_id)


def record_exported(con, codex, exported: dict[str, Any], *, producer: str,
                    now: str) -> list[dict[str, Any]]:
    """After `hold vectors export`: a `local` row per bundle written, not yet
    accepted — this peer's own ladder has not run over it (09 §9.10)."""
    from pathlib import Path
    out = []
    for b in exported.get("bundles", []):
        try:
            header = json.loads((Path(b["path"]) / "header.json").read_bytes())
            uids = list(header.get("uids") or [])
        except (OSError, ValueError, KeyError):
            uids = []
        record(con, bundle_id=b["bundle_id"], shard=shard_id(codex, exported["shard"]),
               producer=producer, uids=uids, chunks=0, tiers={t: "skipped" for t in TIERS},
               source=LOCAL, accepted_at=None)
        out.append(row(con, b["bundle_id"]))
    note_open(con, codex, exported["shard"], now=now)
    return out


def accept_local(con, bundle_id: str, *, chunks: int, tiers: dict[str, str], now: str) -> None:
    """This peer's own bundle, once its own ladder passed over it."""
    ensure_schema(con)
    con.execute("UPDATE coverage SET chunks=?, tiers=?, accepted_at=? WHERE bundle_id=?",
                (int(chunks), json.dumps(tiers, sort_keys=True), now, bundle_id))


def mark_verified_on_use(con, bundle_id: str, chunks: int) -> None:
    """T4 re-embedded and confirmed `chunks` more of this bundle (03 §3.5)."""
    ensure_schema(con)
    con.execute("UPDATE coverage SET verified_on_use = verified_on_use + ? WHERE bundle_id=?",
                (int(chunks), bundle_id))


def claim(con, *, bundle_id: str, shard: str, key: str) -> None:
    """A neighbour's digest says it accepted this bundle (09 §9.9 `coverage`):
    recorded beside the row, or as a row with nothing else known."""
    ensure_schema(con)
    r = con.execute("SELECT claimed_by FROM coverage WHERE bundle_id=?", (bundle_id,)).fetchone()
    if r is None:
        con.execute("INSERT INTO coverage(bundle_id, shard, source, claimed_by) VALUES(?,?,?,?)",
                    (bundle_id, shard, FETCHED, json.dumps([key])))
        return
    keys = _list(r["claimed_by"])
    if key not in keys:
        keys.append(key)
        con.execute("UPDATE coverage SET claimed_by=? WHERE bundle_id=?",
                    (json.dumps(sorted(keys)), bundle_id))


def fold_claims(con, codex, digests: Iterable[dict[str, Any]]) -> int:
    """Every coverage claim about this collection in the digests given
    (04 §4.2; 09 §9.9); how many claims were recorded."""
    n = 0
    for d in digests:
        key = d.get("key")
        if not isinstance(key, str):
            continue
        for e in d.get("collections", []):
            if not isinstance(e, dict) or e.get("collection_id") != codex.collection_id:
                continue
            for c in e.get("coverage", []) or []:
                if not isinstance(c, dict) or not isinstance(c.get("shard"), str):
                    continue
                for bid in c.get("bundles", []) or []:
                    if isinstance(bid, str):
                        claim(con, bundle_id=bid, shard=c["shard"], key=key)
                        n += 1
    return n


def _list(raw) -> list:
    try:
        v = json.loads(raw) if raw else []
        return v if isinstance(v, list) else []
    except ValueError:
        return []


def _row(r) -> dict[str, Any]:
    try:
        tiers = json.loads(r["tiers"]) if r["tiers"] else {}
    except ValueError:
        tiers = {}
    return {"shard": r["shard"], "bundle_id": r["bundle_id"], "producer": r["producer"] or None,
            "uids": [r["uid_first"], r["uid_last"]] if r["uid_first"] is not None else None,
            "chunks": int(r["chunks"] or 0), "tiers": tiers, "accepted_at": r["accepted_at"],
            "source": r["source"], "verified_on_use": int(r["verified_on_use"] or 0),
            "claimed_by": _list(r["claimed_by"])}


def row(con, bundle_id: str) -> dict[str, Any] | None:
    ensure_schema(con)
    r = con.execute("SELECT * FROM coverage WHERE bundle_id=?", (bundle_id,)).fetchone()
    return _row(r) if r else None


def rows(con, shard: str | None = None) -> list[dict[str, Any]]:
    ensure_schema(con)
    if shard is None:
        got = con.execute("SELECT * FROM coverage ORDER BY shard, accepted_at, bundle_id").fetchall()
    else:
        got = con.execute("SELECT * FROM coverage WHERE shard=? ORDER BY accepted_at, bundle_id",
                          (shard,)).fetchall()
    return [_row(r) for r in got]


def note_open(con, codex, shard: str, *, now: str) -> None:
    """The moment a shard was first seen open is kept (04 §4.5: the window
    runs from the later of it and the leader's last bundle)."""
    ensure_schema(con)
    con.execute("INSERT OR IGNORE INTO coverage_shards(shard, opened_at) VALUES(?,?)",
                (shard_id(codex, shard) if "/" in shard and not shard.startswith("sha256:") else shard, now))


def opened_at(con) -> dict[str, str]:
    ensure_schema(con)
    return {r["shard"]: r["opened_at"] for r in con.execute("SELECT * FROM coverage_shards").fetchall()}


# ---- the derived view ------------------------------------------------------------------

def _grace_seconds(codex) -> float:
    return float(getattr(codex.update, "grace_days", 7)) * 86400.0


def shards_view(codex, snapshot: sn.Snapshot | None, all_rows: list[dict[str, Any]], *,
                peers: Sequence[rendezvous.Peer] = (), opened: dict[str, str] | None = None,
                now: float | None = None) -> list[dict[str, Any]]:
    """09 §9.10's `shards`: per record shard of the snapshot, the chunks it
    has, the chunks accepted here, whether it is open, and — with the live
    peers given — the leader and the end of its grace window (04 §4.5)."""
    if snapshot is None:
        return []
    now = time.time() if now is None else now
    opened = opened or {}
    by_shard: dict[str, list[dict[str, Any]]] = {}
    for r in all_rows:
        by_shard.setdefault(r["shard"], []).append(r)
    limit = period_seconds(codex.work.per)
    grace = _grace_seconds(codex)
    out = []
    for s in sorted(snapshot.shards, key=lambda s: (s.slice.encode("utf-8"), s.n)):
        sid = shard_id(codex, f"{s.slice}/{s.n:05d}")
        mine = by_shard.get(sid, [])
        accepted = [r for r in mine if r["accepted_at"]]
        covered = min(int(s.chunks), sum(r["chunks"] for r in accepted))
        is_open = covered < int(s.chunks)
        leader = window_ends = None
        if is_open and peers:
            lead = rendezvous.leader(rendezvous.ShardWork(sid, int(s.tokens)), peers, limit)
            if lead is not None:
                leader = lead.key
                last = [messages.parse_time(r["accepted_at"]) for r in accepted
                        if r["producer"] == lead.key]
                since = max(last) if last else (
                    messages.parse_time(opened[sid]) if sid in opened else None)
                if since is not None:
                    window_ends = messages.time_text(since + grace)
        out.append({"shard": sid, "chunks": int(s.chunks), "covered": covered, "open": is_open,
                    "leader": leader, "window_ends": window_ends,
                    "bundles": [r for r in mine]})
    return out


def _within(time_text: str | None, *, now: float, seconds: float) -> bool:
    if not time_text:
        return False
    try:
        t = messages.parse_time(time_text)
    except Exception:   # noqa: BLE001 — a time that is not one counts for nothing
        return False
    return now - seconds <= t <= now + 3600


def signed_work(codex, snapshot: sn.Snapshot | None, all_rows: list[dict[str, Any]],
                attestations: Iterable[dict[str, Any]] = (), *, now: float | None = None
                ) -> dict[str, dict[str, int]]:
    """04 §4.7: per key, the tokens of its signed work in the trailing
    `work.per` — bundles accepted here (chunks × ctx) and attestations held
    here (a deterministic check: the snapshot's tokens of the shards it
    names, or `work.tokens` when it names none; a statistical one: the
    chunks it re-embedded × ctx)."""
    now = time.time() if now is None else now
    per = period_seconds(codex.work.per)
    ctx = int(codex.space.ctx)
    tokens_of: dict[str, int] = {}
    if snapshot is not None:
        for s in snapshot.shards:
            tokens_of[shard_id(codex, f"{s.slice}/{s.n:05d}")] = int(s.tokens)
            tokens_of[f"{codex.collection_id}/{s.slice}/{s.n:05d}"] = int(s.tokens)
    out: dict[str, dict[str, int]] = {}

    def add(key, tokens, kind):
        if not key:
            return
        e = out.setdefault(key, {"tokens": 0, "bundles": 0, "attestations": 0})
        e["tokens"] += int(tokens)
        e[kind] += 1

    for r in all_rows:
        if r["accepted_at"] and _within(r["accepted_at"], now=now, seconds=per):
            add(r["producer"], r["chunks"] * ctx, "bundles")
    for b in attestations:
        if b.get("collection_id") != codex.collection_id or b.get("result") != "pass":
            continue
        if not _within(b.get("time"), now=now, seconds=per):
            continue
        check = b.get("check")
        if check in DETERMINISTIC:
            named = [tokens_of.get(s, 0) for s in (b.get("shards") or [])]
            add(b.get("key"), sum(named) if named else int(codex.work.tokens), "attestations")
        elif check in STATISTICAL:
            detail = b.get("detail") or {}
            n = detail.get("chunks", detail.get("n", detail.get("resolved", 0))) or 0
            add(b.get("key"), int(n) * ctx, "attestations")
    return out


def vitality(codex, snapshot, all_rows, attestations=(), *, now: float | None = None) -> str:
    """04 §4.7: actively-held while any single key's signed work reaches
    `work.tokens` per `work.per`; dormant otherwise."""
    work = signed_work(codex, snapshot, all_rows, attestations, now=now)
    return ACTIVE if any(w["tokens"] >= int(codex.work.tokens) for w in work.values()) else DORMANT


def producers(codex, all_rows, work: dict[str, dict[str, int]]) -> list[dict[str, Any]]:
    """09 §9.10's `producers`: chunks accepted here, bundles, and the signed
    work in the trailing period."""
    out: dict[str, dict[str, Any]] = {}
    for r in all_rows:
        if not r["accepted_at"] or not r["producer"]:
            continue
        e = out.setdefault(r["producer"], {"key": r["producer"], "chunks": 0, "bundles": 0})
        e["chunks"] += r["chunks"]
        e["bundles"] += 1
    for key, e in out.items():
        e["tokens_per"] = {"per": codex.work.per, "tokens": work.get(key, {}).get("tokens", 0)}
    return [out[k] for k in sorted(out)]


def manifest(root, name: str) -> sqlite3.Connection:
    """The collection's manifest (07 §7.8), opened for the map alone —
    autocommit, rows as mappings, the map's tables ensured. For writes the
    stages use their own `db.connect`; this is for reading what is held,
    which no acknowledgement gates."""
    from pathlib import Path
    p = Path(root) / name / "state" / "manifest.sqlite"
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(p, timeout=30, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=30000")
    return ensure_schema(con)


def own_benchmarks(root, name: str, codex, own_key: str | None) -> list[dict[str, Any]]:
    """This peer's `benchmarks` entries for the collection (09 §9.9), from
    the statements it signed — what its digest carries (app/share/gossip.py)."""
    if own_key is None:
        return []
    from app.hold import embedder as emb, runs
    try:
        stated = runs.statements(root, name, codex, "document", signer=own_key)
    except Exception:   # noqa: BLE001 — no statements, no benchmark
        return []
    out = []
    for body in stated.values():
        try:
            out.append(emb.benchmarks_entry(body))
        except (KeyError, TypeError):
            continue
    return out


def live_peers(codex, node_con, *, now: float, own_key: str | None = None,
               own_benchmarks: Sequence[dict[str, Any]] = ()) -> list[rendezvous.Peer]:
    """04 §4.5's live set for this collection's space: every neighbour whose
    digest carries a benchmark for the space, and this peer with its own."""
    from app.share import peers as peers_mod
    out: list[rendezvous.Peer] = []
    for n in peers_mod.neighbours(node_con, now=now):
        d = peers_mod.digest_of(node_con, n["key"]) or {}
        for b in d.get("benchmarks", []) or []:
            if isinstance(b, dict) and b.get("space_id") == codex.space_id:
                try:
                    w = float(b.get("tokens_per_s") or 0)
                except (TypeError, ValueError):
                    w = 0.0
                if w > 0:
                    out.append(rendezvous.Peer(n["key"], w))
                break
    if own_key:
        for b in own_benchmarks:
            if b.get("space_id") == codex.space_id and float(b.get("tokens_per_s") or 0) > 0:
                out.append(rendezvous.Peer(own_key, float(b["tokens_per_s"])))
                break
    return out


def show(codex, snapshot: sn.Snapshot | None, con, *, attestations: Iterable[dict[str, Any]] = (),
         peers: Sequence[rendezvous.Peer] = (), now: float | None = None) -> dict[str, Any]:
    """What `hold vectors show --json` prints (09 §9.10)."""
    now = time.time() if now is None else now
    all_rows = rows(con)
    attestations = list(attestations)
    view = shards_view(codex, snapshot, all_rows, peers=peers, opened=opened_at(con), now=now)
    work = signed_work(codex, snapshot, all_rows, attestations, now=now)
    return {
        "collection_id": codex.collection_id,
        "space_id": codex.space_id,
        "snapshot_id": snapshot.snapshot_id if snapshot else None,
        "chunks": sum(v["chunks"] for v in view),
        "covered": sum(v["covered"] for v in view),
        "shards": view,
        "producers": producers(codex, all_rows, work),
        "vitality": ACTIVE if any(w["tokens"] >= int(codex.work.tokens) for w in work.values())
        else DORMANT,
    }
