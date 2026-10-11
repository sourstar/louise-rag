"""A bundle another peer announced, taken in (04 §4.5 "how a bundle
travels"; 03 §3.5; 09 §9.7, §9.9, §9.16) — migration step 9e, fourth
brick, the import side of `share contribute`.

A `bundle` event names the collection, the shard, the bundle id, its
member root and its four files. For a collection held here, public,
acknowledged and in use, whose coverage map does not hold the bundle:
the four files are fetched by their torrents (app/share/transport.py)
into `state/staging/`, each checked against the root the event gave,
and the bundle goes through this peer's ladder — T0, T1, T2, and T3
skipped (no encoder is asked in the worker's thread: the chunks land
*unverified here*, which 03 §3.5 allows and `vectors check` repairs).
Passing, its vectors are merged into this peer's table, the producer is
recorded as reproduced (08 §8.2), the coverage row is accepted
(`fetched`), and the bundle rests under `state/bundles/` where the seeder
serves it: every completed fetch makes the fetcher a sharer (04 §4.3).
Refused, the row says so and the files are set aside. An event about a
collection not held here, or one without a file list (an older announcer),
is acted on by being left alone.
"""
from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from typing import Any, Callable

from app.hold import collection as col, shard as shard_mod, envelope as env, snapshot as sn
from app.hold.codex import CodexError, canonical_json
from app.hold.shard import MemberFile
from app.share import attest, contribute, coverage as cov, holdings, members as mem, messages, \
    transport


def pending(node_con, type_: str = "bundle") -> list[dict[str, Any]]:
    """The events of one type received and not yet acted on."""
    return [ev for ev in attest.unacted(node_con)
            if ev["type"] == type_ and ((ev["envelope"] or {}).get("body") or {}).get("type") == type_]


def newer_snapshots(root: Path, collections, node_con, *, client=None, key=None, own_key=None,
                    stop=None, now: float | None = None,
                    log: Callable[[str], None] | None = None) -> dict[str, int]:
    """`snapshot` events (09 §9.9): a snapshot newer than the one held here,
    announced by a peer whose address a digest gave, is pulled from that
    peer with its chain and promoted when signed (app/share/fetch.py); the
    members then follow at the next beat. An announcer with no known
    address leaves the event waiting for its digest; a snapshot held
    already, or older, is acted on by being noted."""
    from app.share import fetch as fetch_mod, peers, seeder
    now = time.time() if now is None else now
    out = {"pulled": 0, "known": 0, "ignored": 0, "waiting": 0, "failed": 0}
    by_id = {}
    for c in collections:
        codex = getattr(c, "codex", None)
        if codex is not None:
            by_id[codex.collection_id] = c
    for ev in pending(node_con, "snapshot"):
        if stop is not None and stop.is_set():
            break
        body = ev["envelope"]["body"]
        payload = body.get("payload") or {}
        cid, sid = str(payload.get("collection_id") or ""), str(payload.get("snapshot_id") or "")
        c = by_id.get(cid)
        if c is None or not sid:
            out["ignored"] += 1
            attest.mark_acted(node_con, ev["id"])
            continue
        current = seeder.current_snapshot(col.shard_dir_of(Path(root), c.name))
        if current is not None and (sid <= current.snapshot_id or sid in fetch_mod.held_snapshots(Path(root), cid)):
            out["known"] += 1
            attest.mark_acted(node_con, ev["id"])
            continue
        digest = peers.digest_of(node_con, str(body.get("key") or "")) or {}
        address = digest.get("address")
        if not address or peers.is_banned(node_con, str(body.get("key")), now=now):
            out["waiting"] += 1                     # no digest yet: its address is unknown
            continue
        try:
            got = fetch_mod.fetch(Path(root), cid, snapshot_id=sid, from_address=address, client=client,
                                  stop=stop, now=now, log=log, key=key, own_key=own_key)
        except fetch_mod.Unreachable as e:
            out["failed"] += 1
            if log:
                log(f"[{c.name}] share: snapshot {sid} announced; {e}; retried next beat"[:400])
            continue
        except CodexError as e:
            out["failed"] += 1
            attest.mark_acted(node_con, ev["id"])
            if log:
                log(f"[{c.name}] share: snapshot {sid} announced by {str(body.get('key'))[:23]}…: "
                    f"{e}"[:400])
            continue
        out["pulled"] += 1
        attest.mark_acted(node_con, ev["id"])
        if log:
            log(f"[{c.name}] share: snapshot {sid} pulled from {address}: "
                f"{got['snapshots']}; the members follow")
    return out


def member_of(payload: dict[str, Any], space: str) -> mem.Member | None:
    """The bundle member the event describes, from its file list; None when
    the list is missing or not what 09 §9.16 shapes."""
    files = payload.get("files")
    if not isinstance(files, list) or not files:
        return None
    try:
        mfs = tuple(MemberFile(str(p), str(r), int(b)) for p, r, b in files)
    except (TypeError, ValueError):
        return None
    if sorted(f.path for f in mfs) != sorted(mem.BUNDLE_FILES):
        return None
    bundle_id = str(payload.get("bundle_id") or "")
    if not bundle_id.startswith("sha256:"):
        return None
    member = mem.Member(f"bundle-{space}-{shard_mod._hex(bundle_id)}", mfs)
    if payload.get("root") != member.root:
        return None                                 # the list does not hash to the root claimed
    return member


def take(root: Path, name: str, codex, payload: dict[str, Any], *, session, con, node_con,
         hints=(), write: Callable[[list[dict]], Any] | None = None, vectors_for=None, stop=None,
         log: Callable[[str], None] | None = None, now: float | None = None, key=None,
         own_key: str | None = None) -> dict[str, Any]:
    """One announced bundle, fetched, judged, landed or set aside."""
    from app import embeddings
    from app.hold import admission as adms, admit as adm, land as landing, records as rec, vectors as vec
    root = Path(root)
    now = time.time() if now is None else now
    say = log or (lambda *_a: None)
    member = member_of(payload, codex.space_segment)
    if member is None:
        return {"outcome": "ignored", "why": "no file list the event's root agrees with"}
    bundle_id = str(payload["bundle_id"])
    known = cov.row(con, bundle_id)
    if known and known["accepted_at"]:
        return {"outcome": "held", "bundle_id": bundle_id}
    final = contribute.bundle_dir(root, name, codex, str(payload.get("shard") or "x/00000"), bundle_id)
    staging = root / name / "state" / holdings.STAGING
    staged_dir = staging / member.name
    for t in member.torrents():
        if transport.landed_whole(staged_dir / t.path, t.length, t.root):
            continue                                # whole from an earlier beat; a holed one is fetched on
        landed_at = transport.fetch_file(session, torrent=t, member=member.name, staging=staging,
                                         hints=hints, stop=stop, log=log)
        if landed_at != staged_dir / t.path:            # libtorrent's layout differed: ours
            (staged_dir / t.path).parent.mkdir(parents=True, exist_ok=True)
            landed_at.replace(staged_dir / t.path)
    for f in member.files:
        got = shard_mod.pieces_root_of(staged_dir / f.path)
        if got != f.root:
            holdings._set_aside(staged_dir / f.path)
            return {"outcome": "refused", "bundle_id": bundle_id,
                    "why": f"{f.path} hashes {got[:23]}…, the event said {f.root[:23]}… — set aside"}
    reference, envelope = contribute.calibration_tensors(root, name, codex)
    header, rows, possession, read_id = vec.read(staged_dir)
    if read_id != bundle_id:
        shutil.rmtree(staged_dir, ignore_errors=True)
        return {"outcome": "refused", "bundle_id": bundle_id,
                "why": f"the bundle reads as {read_id[:23]}…: not the one announced"}
    snapshot = landing.snapshot_named(root, name, header["snapshot_id"])
    records = landing.records_for(root, name, snapshot, header["shard"], header["uids"])
    mine = {r["uid"]: r["content_hash"] for r in records}
    cut = landing.cut(codex, records)
    report = adm.judge(codex, header, rows, possession, bundle_id, envelope=envelope,
                       reference=reference, mine=mine,
                       text_of=lambda cid: landing.input_of(cut, cid), embed=None,
                       seed=os.urandom(32))
    cov.record_from_report(con, codex, report, header, source=cov.FETCHED,
                           now=messages.time_text(now))
    if not report.ok:
        failed = [t.tier for t in report.tiers if t.failed]
        for f in member.files:
            holdings._set_aside(staged_dir / f.path)
        say(f"[{name}] share: bundle {bundle_id[:23]}… from {report.producer[:23]}… refused at "
            f"{failed}; set aside")
        return {"outcome": "refused", "bundle_id": bundle_id, "why": f"refused at {failed}"}
    local = None
    if write is None or vectors_for is None:
        local = rec.local_collection(root, name, codex)
    if write is None:
        write = lambda r: embeddings.upsert_chunks(r, collection=local, dtype=codex.space.dtype)  # noqa: E731
    if vectors_for is None:
        vectors_for = lambda ids: embeddings.vectors_by_chunk_id(ids, collection=local)  # noqa: E731
    # 04 §4.5, duplicates are checks: chunks this peer already holds vectors
    # for are COMPARED, attested and not stored; the first accepted vector
    # stays what is searched
    duplicate = compare_duplicate(root, name, codex, snapshot, report, header, con=con, node_con=node_con,
                                  vectors_for=vectors_for, key=key, own_key=own_key, now=now, log=log)
    if duplicate is not None:
        written = 0
        fresh = [(cid, vec_) for cid, vec_ in report.admitted if cid not in duplicate["compared"]]
        if fresh:
            import dataclasses
            landed = landing.land(codex, dataclasses.replace(report, admitted=tuple(fresh)),
                                  records=records, write=write)
            written = landed["written"]
    else:
        landed = landing.land(codex, report, records=records, write=write)
        written = landed["written"]
    node = adms.connect(root)
    try:
        adms.record_reproduced(node, report.producer, ref=bundle_id, chunks=written)
    finally:
        node.close()
    final.parent.mkdir(parents=True, exist_ok=True)
    if final.is_dir():
        shutil.rmtree(staged_dir, ignore_errors=True)
    else:
        staged_dir.replace(final)
    say(f"[{name}] share: bundle {bundle_id[:23]}… from {report.producer[:23]}… accepted — "
        f"{written} chunks landed"
        + (f", {len(duplicate['compared'])} compared with what was held: {duplicate['result']}"
           if duplicate else "") + "; served from here now")
    return {"outcome": "accepted", "bundle_id": bundle_id, "written": written,
            "producer": report.producer, "dir": str(final), "duplicate": duplicate}


def compare_duplicate(root: Path, name: str, codex, snapshot, report, header: dict[str, Any], *, con,
                      node_con, vectors_for, key=None, own_key=None, now: float | None = None,
                      log: Callable[[str], None] | None = None) -> dict[str, Any] | None:
    """04 §4.5: a second compliant bundle for chunks already covered here is
    compared against the vectors held — cosine per chunk against the
    space's tolerance — and the comparison published as a `vectors-sample`
    attestation naming both producers (09 §9.8). None when nothing of it
    was held before; otherwise `{compared, passed, median, failed, result,
    attestation}`."""
    import numpy as np
    from app.hold import check as chk, equivalence
    now = time.time() if now is None else now
    ids = [cid for cid, _ in report.admitted]
    held = vectors_for(ids) if ids else {}
    compared = [cid for cid in ids if cid in held]
    if not compared:
        return None
    tol = float(codex.space.tolerance)
    cosines = []
    failed = []
    for cid, vec_ in report.admitted:
        if cid not in held:
            continue
        a = np.asarray(vec_, dtype=np.float64)
        b = np.asarray(held[cid], dtype=np.float64)
        denom = float(np.linalg.norm(a) * np.linalg.norm(b)) or 1.0
        c = float(np.dot(a, b) / denom)
        cosines.append(c)
        if c < tol:
            failed.append(cid)
    passed = len(compared) - len(failed)
    quorum = codex.space.calibration
    ok = passed * quorum.n >= quorum.min_pass * len(compared)
    producers = sorted({report.producer} | {r["producer"] for r in cov.rows(con, cov.shard_id(codex, header["shard"]))
                                             if r["accepted_at"] and r["producer"]})
    detail = {"chunks": len(compared), "passed": passed,
              "median": f"{sorted(cosines)[len(cosines) // 2]:.6f}", "failed": sorted(failed),
              "producers": producers}
    out: dict[str, Any] = {"compared": compared, "passed": passed, "median": detail["median"],
                           "failed": sorted(failed), "result": chk.PASS if ok else chk.FAIL, "attestation": None}
    image = equivalence.running_image()
    if key is None or own_key is None:
        try:
            from app import identity
            from app.bundles import keys as node_keys
            key = node_keys.load_signing_key()
            own_key = identity.own()
        except Exception:   # noqa: BLE001 — no key: compared, not attested
            key = None
    if key is not None and image is not None:
        outcome = chk.Outcome("vectors-sample", out["result"], detail, None,
                              (cov.shard_id(codex, header["shard"]),))
        doc = chk.attestation(outcome, codex, snapshot, key, platform_image=image,
                              now=messages.time_text(now))
        aid = env.body_id(doc["body"])
        p = attest.attestation_path(root, name, codex, aid)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(canonical_json(doc))
        attest.publish(root, name, codex, [aid], key=key, own_key=own_key, con=node_con, now=now, log=log)
        out["attestation"] = aid
    elif log:
        log(f"[{name}] share: duplicate bundle compared ({out['result']}) and not attested: "
            + ("no key" if key is None else "this process does not know its image (LOUISE_PLATFORM_IMAGE)"))
    return out


def codexes(node_con, *, log: Callable[[str], None] | None = None) -> dict[str, int]:
    """`codex` events (09 §9.9): the chain read as Codexes and recorded in
    the catalogue once; a chain that does not read is dropped and said."""
    from app.share import catalogue
    out = {"recorded": 0, "known": 0, "dropped": 0}
    for ev in pending(node_con, "codex"):
        body = ev["envelope"]["body"]
        try:
            chain = catalogue.read_chain(body.get("payload") or {})
        except (CodexError, KeyError, TypeError, ValueError) as e:
            out["dropped"] += 1
            attest.mark_acted(node_con, ev["id"])
            if log:
                log(f"share: codex event {ev['id'][:23]}… from {str(body.get('key'))[:23]}… dropped: "
                    f"{e}"[:400])
            continue
        n = catalogue.record(node_con, chain, announced_by=str(body.get("key") or "") or None)
        out["recorded" if n else "known"] += 1
        attest.mark_acted(node_con, ev["id"])
        if log and n:
            log(f"share: {chain[0].relation} {chain[0].collection_id[:23]}… ({chain[0].descriptive.title!r}) "
                f"announced by {str(body.get('key'))[:23]}…: in the catalogue")
    return out


def flags(root: Path, collections, node_con, *, client=None, key=None, own_key=None, stop=None,
          now: float | None = None, log: Callable[[str], None] | None = None) -> dict[str, int]:
    """`flag` events (09 §9.9; 03 §3.6): for a collection held here, the
    attestations the flag names are pulled from the announcer — a peer
    whose digest gave its address — and kept beside the catalogue, where
    the trust level reads them: a stranger's failure is a CLAIM until this
    peer reproduces it or a second key says the same (app/share/levels.py).
    An announcer with no address yet leaves the event waiting."""
    from app.share import fetch as fetch_mod, peers
    now = time.time() if now is None else now
    out = {"kept": 0, "ignored": 0, "waiting": 0, "failed": 0}
    by_id = {}
    for c in collections:
        codex = getattr(c, "codex", None)
        if codex is not None:
            by_id[codex.collection_id] = c
    if key is None or own_key is None:
        try:
            from app import identity
            from app.bundles import keys as node_keys
            key = node_keys.load_signing_key()
            own_key = identity.own()
        except Exception:   # noqa: BLE001 — no key: pulls cannot be signed
            key = own_key = None
    for ev in pending(node_con, "flag"):
        if stop is not None and stop.is_set():
            break
        body = ev["envelope"]["body"]
        payload = body.get("payload") or {}
        cid = str(payload.get("collection_id") or "")
        wanted = {a for a in (payload.get("attestations") or []) if isinstance(a, str)}
        c = by_id.get(cid)
        if c is None or not wanted:
            out["ignored"] += 1
            attest.mark_acted(node_con, ev["id"])
            continue
        if key is None:
            out["waiting"] += 1
            continue
        digest = peers.digest_of(node_con, str(body.get("key") or "")) or {}
        address = digest.get("address")
        if not address or peers.is_banned(node_con, str(body.get("key")), now=now):
            out["waiting"] += 1
            continue
        puller = fetch_mod.Puller(key=key, own_key=own_key, client=client, stop=stop, con=node_con,
                                  log=log, now=now)
        try:
            items = puller.pull(fetch_mod.Source(address, str(body.get("key"))), "attestations", cid)
        except fetch_mod.Unreachable as e:
            out["failed"] += 1
            if log:
                log(f"[{c.name}] share: flag received; {e}; retried next beat"[:400])
            continue
        except CodexError as e:
            out["failed"] += 1
            attest.mark_acted(node_con, ev["id"])
            if log:
                log(f"[{c.name}] share: flag's attestations from {address}: {e}"[:400])
            continue
        kept = 0
        for path, data in items:
            try:
                envelope = json.loads(data)
                att = env.verify(env.ATTESTATION, envelope)
            except (ValueError, env.SignatureError, CodexError, TypeError):
                continue
            aid = env.body_id(att)
            if aid not in wanted or att.get("collection_id") != cid:
                continue
            p = attest.attestation_path(root, c.name, c.codex, aid)
            if not p.is_file():
                p.parent.mkdir(parents=True, exist_ok=True)
                tmp = p.with_name(f".{p.name}.flag")
                tmp.write_bytes(canonical_json(envelope))
                tmp.replace(p)
                kept += 1
        out["kept"] += 1
        attest.mark_acted(node_con, ev["id"])
        if log:
            log(f"[{c.name}] share: flag on {payload.get('check')} received from {str(body.get('key'))[:23]}…: "
                f"{kept} attestation(s) kept — a claim until reproduced here or seconded (03 §3.6)")
    return out


def reproduce_forgeries(root: Path, collections, cons: dict, node_con, *, own_key: str | None = None,
                        now: float | None = None, log: Callable[[str], None] | None = None) -> dict[str, int]:
    """03 §3.5: another key's `forgery` proof — held here by a flag or an
    attestation event — is a CLAIM until reproduced: for a chunk this peer
    holds IMPORTED from the producer the proof names, the chunk is cited
    for T4 (app/bundles/verify_on_use.py), whose second recompute burns
    the key here too and yields this peer's own proof. A chunk not held
    from that producer cannot be reproduced here, and the claim stays a
    claim. Each proof is acted on once (node.sqlite `claims_acted`)."""
    from app import copy_state
    from app.bundles import ledger, verify_on_use
    now = time.time() if now is None else now
    node_con.execute("CREATE TABLE IF NOT EXISTS claims_acted (attestation_id TEXT PRIMARY KEY, acted REAL, "
                     "outcome TEXT)")
    if own_key is None:
        try:
            from app import identity
            own_key = identity.own()
        except Exception:   # noqa: BLE001 — no key: every proof is another's
            own_key = None
    out = {"cited": 0, "not_held": 0, "seen": 0}
    for c in collections:
        codex = getattr(c, "codex", None)
        con = cons.get(c.name)
        if codex is None or con is None:
            continue
        for body in copy_state.attestations(root, c.name, codex):
            if body.get("check") != "forgery" or body.get("result") != "forgery" or body.get("key") == own_key:
                continue
            aid = env.body_id(body)
            if node_con.execute("SELECT 1 FROM claims_acted WHERE attestation_id=?", (aid,)).fetchone():
                continue
            out["seen"] += 1
            detail = body.get("detail") or {}
            chunk_id = detail.get("chunk_id")
            producer = verify_on_use.key_text(detail.get("producer"))
            row = ledger.verification_states(con, [chunk_id]).get(chunk_id) if isinstance(chunk_id, str) else None
            if row is not None and row["state"] == "imported" \
                    and verify_on_use.key_text(row.get("producer")) == producer:
                verify_on_use.cite(con, [chunk_id])
                outcome = "cited"
                out["cited"] += 1
                if log:
                    log(f"[{c.name}] share: forgery claimed by {str(body.get('key'))[:23]}… against "
                        f"{str(producer)[:23]}… on {chunk_id}: held imported from it — cited for T4, which "
                        f"reproduces or clears it here (03 §3.5)")
            else:
                outcome = "not-held"
                out["not_held"] += 1
            node_con.execute("INSERT OR REPLACE INTO claims_acted(attestation_id, acted, outcome) VALUES(?,?,?)",
                             (aid, now, outcome))
    return out


def reproduce_claims(root: Path, collections, node_con, *, own_key: str | None = None,
                     now: float | None = None, log: Callable[[str], None] | None = None,
                     run=None) -> dict[str, int]:
    """03 §3.6: "a flag received from another key is acted on after local
    reproduction". Another key's deterministic failure under an image whose
    verdicts count is a CLAIM (app/share/levels.py `claims`); here the named
    check runs on the named shard alone (`hold check --shards`), once per
    claim (node.sqlite `claims_acted`). Failing here too, it is this peer's
    own failure and flags the collection; passing, the claim stays a claim.
    A claim with no shard, on a shard this copy does not hold, or asking
    for the origin this copy lacks, cannot be reproduced and is shown as a
    claim. `run(args)` is `louise.run_check` (injected for tests)."""
    import argparse
    from app import collections as collections_mod, copy_state
    from app.hold import equivalence
    from app.share import levels
    now = time.time() if now is None else now
    node_con.execute("CREATE TABLE IF NOT EXISTS claims_acted (attestation_id TEXT PRIMARY KEY, acted REAL, "
                     "outcome TEXT)")
    if own_key is None:
        try:
            from app import identity
            own_key = identity.own()
        except Exception:   # noqa: BLE001 — no key: nothing of ours can be written
            return {}
    if run is None:
        from app import louise
        run = louise.run_check
    out = {"reproduced": 0, "not_reproduced": 0, "cannot": 0}
    for c in collections:
        codex = getattr(c, "codex", None)
        if codex is None or not copy_state.of(c).in_use:
            continue
        bodies = copy_state.attestations(root, c.name, codex)
        images = equivalence.reference_images(bodies, own_key, codex)
        for claim in levels.claims(codex, bodies, reference_images=images, own_key=own_key):
            aid = f"claim:{codex.collection_id}:{claim['check']}:{claim['shard']}:{claim['key']}"
            if node_con.execute("SELECT 1 FROM claims_acted WHERE attestation_id=?", (aid,)).fetchone():
                continue
            check = claim["check"]
            # an attestation names `<collection_id>/slice/NNNNN`; the SHARD knows `slice/NNNNN`
            shard = "/".join(str(claim["shard"]).split("/")[-2:]) if claim["shard"] else None
            origin = None
            if check == "records-vs-origin":
                d = getattr(c, "corpus_dir", None)
                origin = str(d) if d and getattr(c, "source", "origin") != "shard" and Path(d).is_dir() else None
            held = shard is not None and any(
                f"{s.slice}/{s.n:05d}" == shard and (Path(root) / c.name / "shard" / s.path).is_file()
                for s in _current_snapshot_shards(root, c.name))
            if not held or (check == "records-vs-origin" and origin is None):
                outcome = "cannot: " + ("no shard named" if shard is None else "shard not held here"
                                        if not held else "no origin here")
                out["cannot"] += 1
            else:
                args = argparse.Namespace(root=Path(root), collection=c.name, checks=check, sample=None,
                                          seed=None, snapshot=None, origin_dir=origin, origin_mib=None,
                                          against=None, shards=shard)
                try:
                    _n, _cx, _snap, _seed, report = run(args)
                    result = report.outcome(check).result
                except Exception as e:   # noqa: BLE001 — a claim that could not run stays a claim
                    result = f"error: {type(e).__name__}: {e}"[:200]
                if result == "fail":
                    outcome, k = "reproduced", "reproduced"
                elif result == "pass":
                    outcome, k = "not reproduced", "not_reproduced"
                else:
                    outcome, k = f"cannot: {result}", "cannot"
                out[k] += 1
            node_con.execute("INSERT OR REPLACE INTO claims_acted(attestation_id, acted, outcome) VALUES(?,?,?)",
                             (aid, now, outcome))
            if log:
                log(f"[{c.name}] share: claim {check} on {shard} from {str(claim['key'])[:23]}…: {outcome} "
                    f"(03 §3.6)")
    return out


def _current_snapshot_shards(root: Path, name: str):
    d = Path(root) / name / "shard" / "snapshots"
    ids = sorted(p for p in d.glob("*.json") if not p.name.endswith(".sig.json")) if d.is_dir() else []
    return sn.load(ids[-1]).shards if ids else ()


def beat(collections, cons: dict, node_con, *, session_factory, root: Path, stop=None,
         now: float | None = None, log: Callable[[str], None] | None = None, client=None,
         key=None, own_key=None) -> dict[str, Any]:
    """Every pending `codex`, `flag` and `snapshot` event, then every
    pending `bundle` event, acted on once: taken in for a collection held
    here and in use, left where it is not."""
    from app import copy_state, licensing
    now = time.time() if now is None else now
    out: dict[str, Any] = {"accepted": 0, "refused": 0, "ignored": 0, "held": 0, "deferred": 0}
    for name, fn in (("codexes", lambda: codexes(node_con, log=log)),
                     ("flags", lambda: flags(root, collections, node_con, client=client, key=key,
                                             own_key=own_key, stop=stop, now=now, log=log)),
                     ("forgeries", lambda: reproduce_forgeries(root, collections, cons, node_con, own_key=own_key,
                                                               now=now, log=log)),
                     ("claims", lambda: reproduce_claims(root, collections, node_con, own_key=own_key,
                                                         now=now, log=log)),
                     ("snapshots", lambda: newer_snapshots(root, collections, node_con, client=client, key=key,
                                                           own_key=own_key, stop=stop, now=now, log=log))):
        try:
            out[name] = fn()
        except Exception as e:   # noqa: BLE001 — the other kinds still get their turn
            if log:
                log(f"[warn:share] {name} events: {type(e).__name__}: {e}"[:400])
    by_id = {}
    for c in collections:
        codex = getattr(c, "codex", None)
        if codex is not None:
            by_id[codex.collection_id] = c
    for ev in pending(node_con):
        if stop is not None and stop.is_set():
            break
        payload = (ev["envelope"]["body"].get("payload") or {})
        c = by_id.get(str(payload.get("collection") or ""))
        if c is None or cons.get(c.name) is None:
            out["ignored"] += 1
            attest.mark_acted(node_con, ev["id"])
            continue
        try:
            usable = licensing.state_for(c) != licensing.MISSING and copy_state.of(c).in_use \
                and c.codex.privacy == "public"
        except Exception:   # noqa: BLE001 — a gate error withholds
            usable = False
        if not usable:
            out["deferred"] += 1                    # left unacted: the gate may clear
            continue
        hints = transport.hints_for(node_con, c.codex.collection_id, now=now)
        try:
            got = take(root, c.name, c.codex, payload, session=session_factory(), con=cons[c.name],
                       node_con=node_con, hints=hints, stop=stop, log=log, now=now, key=key, own_key=own_key)
        except (transport.TransferAborted, transport.AcquireAborted):
            break                                   # the next beat resumes from staging
        except (transport.TransportError, CodexError, OSError, KeyError, ValueError) as e:
            out["deferred"] += 1
            if log:
                log(f"[{c.name}] share: bundle {str(payload.get('bundle_id'))[:23]}…: "
                    f"{type(e).__name__}: {e}"[:400])
            if isinstance(e, transport.TransportError) and "no metadata" in str(e):
                break                               # nobody serves it now: the rest can wait
            continue
        out[got["outcome"]] += 1
        attest.mark_acted(node_con, ev["id"])
    return out
