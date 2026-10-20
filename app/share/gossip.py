"""This node's side of the peer messages (04 §4.2; 09 §9.9), run by the
WORKER, which holds the key: its digest, signed and sent to every
neighbour and every address in the peer list once an hour; the events it
received, forwarded once to its neighbours with one hop fewer; the ids
seen, swept after seven days. Migration step 9c.

The web process only receives (app/share/api.py). Every byte sent here
draws from the node's UP bucket first (04 §4.3: digests and events are
in the budget; CLAUDE.md invariant 5), in the DELTA class — they carry
the current state of the catalogue and are kilobytes an hour. What the
web ANSWERS to a pull (a Codex, a snapshot, attestations — kilobytes too)
leaves through the web process, which has no bucket of the worker's; it
is unmetered, said in the plan, and 9d revisits it when pulls grow.
"""
from __future__ import annotations

import json
import threading
import time
from contextlib import closing
from pathlib import Path
from typing import Any, Callable

from app.hold import collection as col, envelope as env
from app.share import members as mem, messages, peers, seeder
from app.transfer.bucket import Priority, TransferAborted

TIMEOUT_S = 10.0
PRIORITY = Priority.DELTA
_last_digest_at = 0.0
# The addresses this interval's digest went to. A recipient outside it — a
# peer given by hand, a neighbour newly heard from — gets the digest at the
# next beat instead of up to an interval later: the second peer waited for
# the node's first digest, the one that names its swarm, for most of an
# hour (2026-10-07). It is then in the set, so a failed send is retried at
# the interval, never every beat.
_sent_to: frozenset[str] = frozenset()
# An address this interval's digest did NOT reach: retried with a backoff —
# RETRY_S, doubling, capped at the interval — not an hour later. A peer
# restarting at the moment the digest came (the node, 2026-10-19 12:06 CEST)
# cost the Perugia peer a full interval of invisibility: its digest was
# refused and counted as the interval's send. A peer that is down costs one
# refused connection a minute, then every two, four, eight…
RETRY_S = 60.0
_retry_at: dict[str, tuple[float, float]] = {}          # address -> (not before, this delay)


def own_address(cfg=None) -> str:
    """`[node] address` (07 §7.8), else this host and port as the node sees
    them — reachable only on the LAN the container is on."""
    import socket
    from app import config, platform_config
    cfg = cfg or platform_config.load()
    if cfg.peers.address:
        return cfg.peers.address
    return f"http://{cfg.peers.name or socket.gethostname()}:{config.PORT}"


def own_port(cfg=None) -> int:
    """The port a DHT announcement names (04 §4.2: what is found under a key
    is a peer address whose port speaks the peer messages): the port of
    `[node] address` when it is set — the reachable one — else the
    process's own."""
    from urllib.parse import urlsplit
    from app import config, platform_config
    cfg = cfg or platform_config.load()
    if cfg.peers.address:
        parts = urlsplit(cfg.peers.address)
        if parts.port:
            return parts.port
        return 443 if parts.scheme == "https" else 80
    return int(config.PORT)


def leads(collection) -> bool:
    """Does this peer EMBED the collection under rendezvous and PUBLISH what
    it embeds — `embed = "p2p"` or `"all"` in its file, and `share = true`
    (07 §7.8; 04 §4.5, amended 2026-10-20)? Only then is its benchmark for
    the space in its digest, and only then is it scored for the collection's
    parts. A peer whose mode does not embed (`off`, `on-demand`), or that
    does not share, publishes no bundle and must lead nothing: the dev PC's
    peer, embed off and share off, led 355 of gutenberg's open parts on the
    strength of a benchmark alone, and they stalled (München, 2026-10-20).
    Switching a collection away from p2p or all, or its sharing off, thus
    RELEASES every part it leads: its next digest — sent at once on the
    switch — carries no benchmark for the space, and the others' rendezvous
    moves on to the next in the ordering, which embeds only what still
    lacks a bundle; the partial work the peer bundled stays."""
    from app import collections as collections_mod
    share = bool(getattr(collection, "share", False))
    mode, _why = collections_mod.effective_embed_mode(
        getattr(collection, "embed_mode_default", None) or collections_mod.DEFAULT_EMBED_MODE,
        share, getattr(collection, "name", "?"))
    return share and mode in ("p2p", "all")


def benchmarks(root: Path, collections, own_key: str | None) -> list[dict[str, Any]]:
    """One entry per space, from the statements this node signed (03 §3.3;
    09 §9.9) — the latest run per space — for the collections this peer
    LEADS (`leads`): embeds under rendezvous and shares."""
    from app.hold import embedder as emb, runs
    out: dict[str, dict[str, Any]] = {}
    if own_key is None:
        return []
    for c in collections:
        codex = getattr(c, "codex", None)
        if codex is None or not leads(c):
            continue
        if not runs.applies(codex):
            # a version-2 collection: its one statement, the row in §9.9's
            # version-1 form (app/share/coverage.py `v2_benchmark`)
            from app.share import coverage as cov
            row = cov.v2_benchmark(root, c.name, codex, own_key)
            if row is not None and row["space_id"] not in out:
                out[row["space_id"]] = {**row, "_time": ""}
            continue
        try:
            stated = runs.statements(root, c.name, codex, "document", signer=own_key)
        except Exception:   # noqa: BLE001 — a collection whose statements cannot be read lists none
            continue
        for body in stated.values():
            try:
                entry = emb.benchmarks_entry(body)
            except (KeyError, TypeError):
                continue
            prev = out.get(entry["space_id"])
            if prev is None or str(body.get("time", "")) > str(prev.get("_time", "")):
                out[entry["space_id"]] = {**entry, "_time": body.get("time", "")}
    return [{k: v for k, v in e.items() if k != "_time"} for e in sorted(out.values(),
                                                                          key=lambda e: e["space_id"])]


def entries(root: Path, collections) -> list[dict[str, Any]]:
    """The digest's collection entries: every Codex collection held, with
    its members listed when this node shares it (04 §4.3) and empty
    otherwise; a never-share collection appears in no digest (09 §9.9)."""
    from app import collections as collections_mod
    from app.hold.codex import lineage_id
    out = []
    resolve = col.resolver(root)
    # the annotations collections held are listed like any (02 §2.10)
    names = {c.name for c in collections}
    try:
        collections = list(collections) + [a for a in collections_mod.load_annotations()
                                           if a.name not in names]
    except Exception:   # noqa: BLE001 — a listing that fails: the rest is still said
        collections = list(collections)
    for c in collections:
        codex = getattr(c, "codex", None)
        if codex is None or codex.privacy != "public":
            continue
        shard_tree = col.shard_dir_of(root, c.name)
        try:
            codex_bytes = (shard_tree / "codex.json").read_bytes()
        except OSError:
            continue
        snap = seeder.current_snapshot(shard_tree)
        shared = seeder.wants_serving(c) and snap is not None
        members = list(mem.members_of(snap, shard_dir=shard_tree)) if shared else []
        members = [m for m in members if all((shard_tree / f.path).is_file() for f in m.files)]
        if shared and codex.space is not None:
            # the bundles this peer holds for the space, served from state/
            # (09 §9.7, §9.16): offered beside the snapshot's members
            members += mem.bundle_members(root / c.name / "state", codex.space_segment)
        # what `share --slices/--kinds` narrowed out is not served, so it is
        # not offered: the node listed every member held while serving two
        # slices, and a peer wanting every slice hung on the first part nobody
        # served (2026-10-18; 04 §4.2 "announcing what you do not serve would
        # be a lie")
        members = [m for m in members if seeder.serves(c, m.name)]
        # a bundle its producer deprecated is still served for a while and
        # listed apart, so no peer starts a fetch of it (operator, 2026-10-19)
        deprecated_members: list = []
        if shared and codex.space is not None:
            from app.share import deprecate
            try:
                members, deprecated_members = deprecate.split_members(root, c.name, codex, members)
            except Exception:   # noqa: BLE001 — an unreadable map: offered as before
                pass
        try:
            lid = lineage_id(codex, resolve)
        except Exception:   # noqa: BLE001 — a chain this node cannot follow: the root is itself
            lid = codex.collection_id
        out.append(messages.collection_entry(codex, snap, members, codex_bytes=codex_bytes,
                                             lineage_id=lid, coverage=coverage_of(root, c.name),
                                             deprecated_members=deprecated_members))
    return out


def coverage_of(root: Path, name: str) -> list[dict[str, Any]]:
    """09 §9.9's `coverage`: per shard, the bundle ids this peer has accepted
    (02 §2.5) — from the coverage map, when the collection has a manifest."""
    from app.share import coverage as cov
    if not (Path(root) / name / "state" / "manifest.sqlite").is_file():
        return []
    by_shard: dict[str, list[str]] = {}
    try:
        with closing(cov.manifest(root, name)) as mcon:
            for r in cov.rows(mcon):
                if r["accepted_at"]:
                    by_shard.setdefault(r["shard"], []).append(r["bundle_id"])
    except Exception:   # noqa: BLE001 — a manifest that cannot be read claims nothing
        return []
    return [{"shard": s, "bundles": sorted(ids)} for s, ids in sorted(by_shard.items())]


def fold_claims(root: Path, collections, *, now: float) -> int:
    """The coverage claims in the neighbours' digests, recorded beside this
    peer's map for every collection held (09 §9.10); how many were recorded."""
    from app.share import coverage as cov
    with closing(peers.connect()) as con:
        digests = [d for d in (peers.digest_of(con, n["key"]) for n in peers.neighbours(con, now=now))
                   if d]
    if not digests:
        return 0
    n = 0
    for c in collections:
        codex = getattr(c, "codex", None)
        if codex is None or not (Path(root) / c.name / "state" / "manifest.sqlite").is_file():
            continue
        if not any(e.get("collection_id") == codex.collection_id
                   for d in digests for e in d.get("collections", []) if isinstance(e, dict)):
            continue
        try:
            with closing(cov.manifest(root, c.name)) as mcon:
                n += cov.fold_claims(mcon, codex, digests)
        except Exception:   # noqa: BLE001 — one manifest, not the beat
            continue
    return n


def peer_port(cfg=None) -> int | None:
    """The port this node's swarm listens on, as peers reach it: `[node]
    peer_port` (07 §7.8), else the session's own when one runs — reachable
    on the LAN the container is on — else none (a node that does not share
    names no swarm)."""
    from app import platform_config
    cfg = cfg or platform_config.load()
    if cfg.peers.peer_port:
        return cfg.peers.peer_port
    try:
        from app.acquire import transport_torrent as tt
        if tt._session is not None:
            return int(tt._session.listen_port()) or None
    except Exception:   # noqa: BLE001 — no libtorrent, no port
        pass
    return None


def build_digest(root: Path, collections, *, key, own_key: str, address: str,
                 now: float | None = None, port: int | None = None) -> dict[str, Any]:
    """This node's digest, signed with its key."""
    with closing(peers.connect()) as con:
        neigh = peers.neighbours(con, now=now if now is not None else peers.now())
    body = messages.digest_body(key=own_key, address=address, neighbours=neigh,
                                benchmarks=benchmarks(root, collections, own_key),
                                collections=entries(root, collections), now=now,
                                peer_port=port)
    return env.sign(env.DIGEST, body, key)


def _draw(n: int, *, stop: threading.Event | None) -> None:
    """`n` bytes from the UP bucket at PRIORITY, before they leave. Blocks
    while the budget is spent; raises TransferAborted when `stop` is set."""
    from app.transfer.manager import Direction, get_manager
    manager = get_manager()
    manager.maybe_refresh()
    manager.bucket(Direction.UP).acquire(n, PRIORITY, stop=stop)


def _post(url: str, payload: dict[str, Any], *, client=None,
          stop: threading.Event | None = None) -> tuple[int, Any]:
    import httpx
    data = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    _draw(len(data), stop=stop)
    c = client or httpx.Client(timeout=TIMEOUT_S)
    try:
        r = c.post(url, content=data, headers={"content-type": "application/json"})
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, None
    finally:
        if client is None:
            c.close()


def send_digest(envelope: dict[str, Any], recipients: list[str], *, client=None,
                stop: threading.Event | None = None,
                log: Callable[[str], None] | None = None) -> dict[str, Any]:
    """To every address, each once; a failure is logged and the address is
    tried again with a backoff (`beat`; 04 §4.2: deferred and retried). A
    stop ends the round where it stands. `unreached` names the addresses
    the digest did not reach."""
    sent, failed = 0, 0
    unreached: list[str] = []
    for address in recipients:
        try:
            code, _ = _post(f"{address.rstrip('/')}/api/share/digest/share", envelope,
                            client=client, stop=stop)
            if 200 <= code < 300:
                sent += 1
            else:
                failed += 1
                unreached.append(address)
                if log:
                    log(f"digest to {address}: HTTP {code}")
        except TransferAborted:
            failed += len(recipients) - sent - failed
            break
        except Exception as e:   # noqa: BLE001 — one peer away is not the round's failure
            failed += 1
            unreached.append(address)
            if log:
                log(f"digest to {address}: {type(e).__name__}: {e}"[:200])
    return {"sent": sent, "failed": failed, "unreached": unreached}


def forward_events(*, client=None, now: float | None = None, stop: threading.Event | None = None,
                   log: Callable[[str], None] | None = None) -> int:
    """Every event received and not yet forwarded goes to every neighbour
    with its hops as kept (already one fewer); forwarded once, whatever the
    outcome at each neighbour. A stop leaves the rest for the next beat."""
    now = peers.now() if now is None else now
    n = 0
    with closing(peers.connect()) as con:
        pending = peers.to_forward(con)
        neigh = peers.neighbours(con, now=now)
        for ev in pending:
            message = {**ev["envelope"], "hops": ev["hops"]}
            try:
                for peer in neigh:
                    if peer["key"] == ev["envelope"]["body"].get("key"):
                        continue                        # not back to its originator
                    try:
                        _post(f"{peer['address'].rstrip('/')}/api/share/event/share", message,
                              client=client, stop=stop)
                    except TransferAborted:
                        raise
                    except Exception as e:   # noqa: BLE001
                        if log:
                            log(f"event to {peer['address']}: {type(e).__name__}: {e}"[:200])
            except TransferAborted:
                break
            peers.mark_forwarded(con, ev["id"])
            n += 1
        peers.sweep(con, now=now)
    return n


def beat(collections, *, now: float | None = None, client=None,
         stop: threading.Event | None = None,
         log: Callable[[str], None] | None = None, force: bool = False) -> dict[str, Any]:
    """The worker's beat: forward what arrived; once an interval, send this
    node's digest. Nothing without a key (a node that cannot sign says
    nothing), nothing without recipients."""
    global _last_digest_at, _sent_to
    from app import collections as collections_mod, identity
    from app.bundles import keys as node_keys
    from app.share import attest as attest_mod
    now = peers.now() if now is None else now
    # what the web sent in answer to pulls since the last beat, charged to
    # this process's UP bucket: one budget for the node (app/share/answered.py)
    try:
        from app.share import answered
        answered.drain()
    except Exception:   # noqa: BLE001 — accounting never stops the beat
        pass
    root = collections_mod.louise_root() / "collections"
    # what arrived is acted on before it is forwarded: an attestation for a
    # collection held here is kept beside its catalogue (03 §3.6; 09 §9.8)
    with closing(peers.connect()) as con:
        acted = attest_mod.store_received(root, con, log=log)
    try:
        claims = fold_claims(root, collections, now=now)
    except Exception:   # noqa: BLE001 — claims are hints; the beat goes on
        claims = 0
    out: dict[str, Any] = {"acted": acted, "claims": claims,
                           "forwarded": forward_events(client=client, now=now, stop=stop, log=log),
                           "digest": None}
    from app import node_state
    requested = node_state.take_digest_request()
    due = force or requested or now - _last_digest_at >= peers.INTERVAL_S
    if not due:
        with closing(peers.connect()) as con:
            fresh = [r for r in peers.recipients(con, now=now)
                     if r not in _sent_to or (r in _retry_at and _retry_at[r][0] <= now)]
        if not fresh:
            return out
    try:
        key = node_keys.load_signing_key()
        own_key = identity.own()
    except Exception as e:   # noqa: BLE001 — no key: no digest, said once per interval
        if due:
            _last_digest_at = now
        else:
            _sent_to = _sent_to | frozenset(fresh)     # said once, not every beat
        if log:
            log(f"no digest: this node has no key to sign one ({type(e).__name__})")
        return out
    if due:
        with closing(peers.connect()) as con:
            recipients = peers.recipients(con, now=now)
    else:
        recipients = fresh
    if not recipients:
        _last_digest_at, _sent_to = now, frozenset()
        return out
    envelope = build_digest(root, collections, key=key, own_key=own_key,
                            address=own_address(), now=now, port=peer_port())
    out["digest"] = send_digest(envelope, recipients, client=client, stop=stop, log=log)
    out["digest"]["recipients"] = recipients
    unreached = set(out["digest"].pop("unreached", ()))
    for r in recipients:
        if r in unreached:
            delay = min(peers.INTERVAL_S, _retry_at[r][1] * 2) if r in _retry_at else RETRY_S
            _retry_at[r] = (now + delay, delay)
        else:
            _retry_at.pop(r, None)
    if due:
        _last_digest_at, _sent_to = now, frozenset(recipients)
        # the attestations the neighbours hold and this peer missed as events
        # (04 §4.2 pull attestations; p2p-review-open-issues.md §3), once an
        # interval, from the neighbours whose digest lists each collection
        try:
            with closing(peers.connect()) as con:
                out["attestations"] = attest_mod.pull_due(root, collections, key=key, own_key=own_key, con=con,
                                                          now=now, client=client, stop=stop, log=log)
        except Exception as e:   # noqa: BLE001 — a pull is a recovery path; the beat is done
            if log:
                log(f"attestation pull: {type(e).__name__}: {e}"[:300])
    else:
        _sent_to = _sent_to | frozenset(recipients)
    return out
