"""`share fetch` (07 §7.3; 04 §4.2, §4.3), first brick of migration step
9d: FIND the peers that hold a collection, PULL its Codex chain and its
snapshot chain, VERIFY them, HOLD the collection — codex.json and
snapshots/* in the SHARD, the collection's file beside it (07 §7.8). That
is the pre-acknowledgement set but for LICENSE-TERMS, a member that
arrives by torrent with the other members (second brick).

Where peers come from (04 §4.2), in the order they are asked: an address
given by hand (`--from`); the neighbours whose digest lists the
collection or its lineage; the `[peers]` list, asked blind; the DHT,
under the collection and lineage keys of the current snapshot id and the
eleven before it, when a session with the DHT is given.

What is checked before a byte is kept: every pulled item hashes to what
it says — a lie bans the sender for a day (04 §4.2); a codex.json is
canonical, is the id asked for, and hashes to what the sender's digest
advertised (09 §9.9); a snapshot names the collection, hashes to what the
digest advertised, and its chain links by hash back to the root's first
— or to the mother's snapshot a daughter continues through, or to one
this peer holds — with consecutive ids (09 §9.4). A snapshot with no
verified signature waits in staging and is not promoted (09 §9.4); the
rest go into the SHARD whole, their sidecars merged by union.

The pulls run in the calling process — kilobytes, signed with the node
key — and their bytes are accounted to that process's budget; the
members, the bytes that matter, are the worker's (second brick).
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from app.hold import codex as cx, collection as col, shard as shard_mod, envelope as env, snapshot as sn
from app.hold.codex import CodexError, canonical_json
from app.share import gossip, members as mem, messages, peers
from app.transfer.bucket import TransferAborted

MAX_CHAIN = 240          # snapshots walked back: twenty years monthly
PROBE_CADENCES = 12      # 04 §4.2: the current id and the eleven before it
DHT_WAIT_S = 20.0
DHT_GRACE_S = 2.0        # after the first answer, a moment for the rest
STAGING = "staging"      # under state/: what arrived and is not promoted
CHAIN = "chain"          # under state/: the Codexes the held one comes from


class FetchError(CodexError):
    """Refused: an answer that is not what the id, the digest or the chain says."""
    exit_code = 2


class NotHeld(FetchError):
    """That peer does not hold it; the next one is asked."""


class Unreachable(FetchError):
    """Deferred (07 §7.7): no peer answered; retry later."""
    exit_code = 3


@dataclass
class Source:
    address: str
    key: str | None = None
    entry: dict[str, Any] | None = None   # the digest's entry for the collection, when known
    peer_port: int | None = None


# ---- finding ------------------------------------------------------------------------

def previous_snapshot_id(snapshot_id: str) -> str:
    """The period before, read off the id's own form — the inverse of
    snapshot.next_snapshot_id."""
    if "-W" in snapshot_id:
        y, w = snapshot_id.split("-W")
        return sn._iso_week_id(int(y), int(w), -1)
    y, m = (int(x) for x in snapshot_id.split("-"))
    return f"{y - 1:04d}-12" if m == 1 else f"{y:04d}-{m - 1:02d}"


def probe_ids(now: datetime | None = None) -> list[str]:
    """The ids a searcher probes with no Codex in hand (04 §4.2): the
    current id of each cadence and the eleven before it, newest first."""
    now = now or datetime.now(timezone.utc)
    out: list[str] = []
    for cadence in ("monthly", "weekly"):
        sid = sn.snapshot_id_for(now, cadence)
        for _ in range(PROBE_CADENCES):
            out.append(sid)
            sid = previous_snapshot_id(sid)
    return out


def sources(con, ref: str, *, now: float, from_address: str | None = None) -> list[Source]:
    """Who is asked, in order: the address given by hand; every neighbour
    whose digest lists `ref` as a collection or a lineage; the `[peers]`
    list, blind. Each address once."""
    from app import platform_config
    out: list[Source] = []

    def add(s: Source) -> None:
        if all(x.address != s.address for x in out):
            out.append(s)

    if from_address:
        add(Source(platform_config.normalise_address(from_address)))
    for n in peers.neighbours(con, now=now):
        d = peers.digest_of(con, n["key"]) or {}
        for e in d.get("collections", []):
            if isinstance(e, dict) and ref in (e.get("collection_id"), e.get("lineage_id")):
                port = d.get("peer_port")
                add(Source(n["address"], n["key"], e, port if isinstance(port, int) else None))
                break
    for a in peers.bootstrap_addresses():
        add(Source(a))
    return out


def target_id(found: list[Source], ref: str) -> str:
    """The collection pulled: `ref` when some peer holds it under that id,
    or when nobody says; for a lineage id, the collection with the newest
    snapshot — the leading daughter (04 §4.6; ids sort within a chain)."""
    entries = [s.entry for s in found if s.entry]
    if not entries or any(e.get("collection_id") == ref for e in entries):
        return ref
    best = max((e for e in entries if e.get("lineage_id") == ref and e.get("collection_id")),
               key=lambda e: str(e.get("snapshot_id") or ""), default=None)
    return str(best["collection_id"]) if best else ref


def _http_address(ip: str, port: int) -> str:
    host = f"[{ip}]" if ":" in ip else ip
    return f"http://{host}:{port}"


def find_dht(session, ref: str, *, now: datetime | None = None, wait_s: float = DHT_WAIT_S,
             sleep: Callable[[float], None] = time.sleep,
             clock: Callable[[], float] = time.monotonic) -> list[Source]:
    """The peers announced under `ref`'s collection and lineage keys for
    the newest probed snapshot id that has any (04 §4.2). What is found is
    an address: the port announced speaks the peer messages."""
    from app.acquire import transport_torrent as tt
    with tt.dht_alerts(session):
        return _find_dht(session, ref, now=now, wait_s=wait_s, sleep=sleep, clock=clock)


def _find_dht(session, ref: str, *, now, wait_s, sleep, clock) -> list[Source]:
    from app.acquire import transport_torrent as tt
    from app.share.seeder import _lt
    lt = _lt()
    by_key: dict[bytes, str] = {}
    for sid in probe_ids(now):
        for k in (mem.collection_key(ref, sid), mem.lineage_key(ref, sid)):
            if k in by_key:
                continue                   # a root's lineage key is its collection key
            by_key[k] = sid
            session.dht_get_peers(lt.sha1_hash(k))
    answers: dict[str, set[tuple[str, int]]] = {}
    deadline = clock() + wait_s
    grace: float | None = None
    while clock() < deadline:
        for a in tt.pop_alerts(session):
            if type(a).__name__ != "dht_get_peers_reply_alert":
                continue
            try:
                sid = by_key.get(a.info_hash.to_bytes())
                got = [(str(ip), int(port)) for ip, port in a.peers()]
            except Exception:   # noqa: BLE001 — an alert of another shape says nothing
                continue
            if sid is not None and got:
                answers.setdefault(sid, set()).update(got)
        if answers and grace is None:
            grace = clock() + DHT_GRACE_S
        if grace is not None and clock() >= grace:
            break
        sleep(0.25)
    if not answers:
        return []
    newest = max(answers)
    return [Source(_http_address(ip, port)) for ip, port in sorted(answers[newest])]


def find_swarm(session, collection_id: str, snapshot_id: str, *, wait_s: float = DHT_WAIT_S,
               sleep: Callable[[float], None] = time.sleep,
               clock: Callable[[], float] = time.monotonic) -> list[tuple[str, int]]:
    """`(host, port)` of the swarms announced under the snapshot's swarm key
    (04 §4.2): where its members are served. A member torrent is never
    announced by its own infohash, so this is how a fetcher no digest told
    of a swarm finds one."""
    from app.acquire import transport_torrent as tt
    from app.share.seeder import _lt
    want = _lt().sha1_hash(mem.swarm_key(collection_id, snapshot_id))
    found: set[tuple[str, int]] = set()
    with tt.dht_alerts(session):
        session.dht_get_peers(want)
        deadline = clock() + wait_s
        grace: float | None = None
        while clock() < deadline:
            for a in tt.pop_alerts(session):
                if type(a).__name__ != "dht_get_peers_reply_alert":
                    continue
                try:
                    if a.info_hash.to_bytes() == want.to_bytes():
                        found.update((str(ip), int(port)) for ip, port in a.peers())
                except Exception:   # noqa: BLE001 — an alert of another shape says nothing
                    continue
            if found and grace is None:
                grace = clock() + DHT_GRACE_S
            if grace is not None and clock() >= grace:
                break
            sleep(0.25)
    return sorted(found)


# ---- pulling ------------------------------------------------------------------------

def _account_down(n: int) -> None:
    """A pull's answer, counted against the DOWN bucket after the fact
    (04 §4.3; invariant 5's accounting half) in the class of the digests."""
    if n <= 0:
        return
    try:
        from app.transfer.manager import Direction, get_manager
        get_manager().bucket(Direction.DOWN).note_granted(n, gossip.PRIORITY)
    except Exception:   # noqa: BLE001 — accounting never fails a fetch
        pass


@dataclass
class Puller:
    """This node's side of a pull: its key, the client, where a lie is recorded."""
    key: Any
    own_key: str
    client: Any = None
    stop: Any = None
    con: Any = None
    log: Callable[[str], None] | None = None
    now: float | None = None

    def lie(self, source: Source, why: str) -> None:
        """04 §4.2: bytes whose hash differs from what was advertised. The
        sender is not a neighbour for a day; the event is logged with its key."""
        if self.con is not None and source.key:
            peers.ban(self.con, source.key, now=self.now if self.now is not None else time.time(),
                      why=why)
        if self.log:
            self.log(f"share fetch: {source.address} ({source.key or 'key unknown'}) lied: "
                     f"{why}"[:500])

    def pull(self, source: Source, what: str, collection_id: str, *,
             snapshot_id: str | None = None, since: str | None = None,
             part: str | None = None, have: list[str] | None = None,
             kind: str = "records") -> list[tuple[str, bytes]]:
        body = messages.pull_body(key=self.own_key, what=what, collection_id=collection_id,
                                  snapshot_id=snapshot_id, since=since, part=part, have=have,
                                  kind=kind)
        url = f"{source.address.rstrip('/')}/api/share/{what}/fetch"
        try:
            code, answer = gossip._post(url, env.sign(env.EVENT, body, self.key),
                                        client=self.client, stop=self.stop)
        except TransferAborted:
            raise
        except Exception as e:   # noqa: BLE001 — a peer away is a deferral (07 §7.7)
            raise Unreachable(f"{source.address}: {type(e).__name__}: {e}"[:300]) from None
        if code == 403:
            raise FetchError(f"{source.address}: this node is not a neighbour there today (04 §4.2)")
        if not (200 <= code < 300) or not isinstance(answer, dict) \
                or not isinstance(answer.get("items"), list):
            raise Unreachable(f"{source.address}: HTTP {code} to a pull of {what}")
        items = answer["items"]
        _account_down(sum(len(str(it.get("b64", ""))) for it in items if isinstance(it, dict)))
        try:
            return messages.verify_items(items)
        except messages.MessageError as e:
            self.lie(source, f"pull {what}: {e}")
            raise FetchError(f"{source.address}: {e}") from None
        except (KeyError, TypeError, ValueError, AttributeError) as e:
            raise FetchError(f"{source.address}: a pull of {what} answered items of another "
                             f"shape ({type(e).__name__})") from None


def _parents(codex: cx.Codex) -> list[str]:
    return [r for r in (codex.continues, codex.derived_from,
                        codex.replaces.collection_id if codex.replaces else None) if r]


def pull_codex(p: Puller, source: Source, collection_id: str) -> list[tuple[cx.Codex, bytes]]:
    """The Codex and the ones it comes from, back to the root (09 §9.9),
    each canonical and under its own id; the first is the one asked for and
    hashes to what the digest advertised."""
    items = p.pull(source, "codex", collection_id)
    if not items:
        raise NotHeld(f"{source.address} does not hold {collection_id}")
    chain: list[tuple[cx.Codex, bytes]] = []
    for path, data in items:
        try:
            codex = cx.loads(data)
        except (CodexError, ValueError, KeyError, TypeError) as e:
            raise FetchError(f"{source.address}: {path}: {e}") from None
        if path != f"codex/{shard_mod._hex(codex.collection_id)}.json":
            raise FetchError(f"{source.address}: {path} holds {codex.collection_id}, another id")
        chain.append((codex, data))
    head, head_bytes = chain[0]
    if head.collection_id != collection_id:
        raise FetchError(f"asked {source.address} for {collection_id}, it answered "
                         f"{head.collection_id}")
    advertised = (source.entry or {}).get("codex_hash")
    if advertised and advertised != "sha256:" + hashlib.sha256(head_bytes).hexdigest():
        p.lie(source, "codex.json hashes otherwise than the digest advertised (09 §9.9)")
        raise FetchError(f"{source.address}: codex.json hashes otherwise than its digest "
                         f"advertised — a lie, dropped (09 §9.9)")
    ids = {c.collection_id for c, _ in chain}
    for c, _ in chain:
        for ref in _parents(c):
            if ref not in ids:
                raise FetchError(f"{source.address}: the chain lacks {ref}, which "
                                 f"{c.collection_id} comes from (09 §9.9)")
    return chain


@dataclass
class Pulled:
    snapshot: sn.Snapshot
    data: bytes
    sidecar: dict[str, Any] | None
    keys: list[str] = field(default_factory=list)        # verified signers
    complaints: list[str] = field(default_factory=list)

    @property
    def signed(self) -> bool:
        return bool(self.keys)


def pull_snapshots(p: Puller, source: Source, codex: cx.Codex, *,
                   snapshot_id: str | None = None,
                   held: dict[str, str] | None = None) -> list[Pulled]:
    """The snapshot asked for — the latest the peer holds when none is —
    and the chain beneath it, newest first: back to the root's first, to
    the mother's snapshot a daughter continues through, or to one this
    peer holds already; each links by hash to the one before (09 §9.4)."""
    held = held or {}
    out: list[Pulled] = []
    sid, expect = snapshot_id, None
    for _ in range(MAX_CHAIN):
        items = dict(p.pull(source, "snapshot", codex.collection_id, snapshot_id=sid))
        paths = [k for k in items if k.startswith("snapshots/") and k.endswith(".json")
                 and not k.endswith(".sig.json")]
        if not paths:
            if sid is None:
                raise NotHeld(f"{source.address} holds no snapshot of {codex.collection_id}")
            raise FetchError(f"{source.address} holds no snapshot {sid}, which the chain "
                             f"needs (09 §9.4)")
        if len(paths) != 1:
            raise FetchError(f"{source.address}: a pull of one snapshot answered {len(paths)}")
        data = items[paths[0]]
        try:
            snap = sn._load(data)
        except (CodexError, KeyError, TypeError, ValueError) as e:
            raise FetchError(f"{source.address}: {paths[0]}: {e}") from None
        if snap.collection_id != codex.collection_id \
                or paths[0] != shard_mod.snapshot_path(snap.snapshot_id) \
                or (sid is not None and snap.snapshot_id != sid):
            raise FetchError(f"{source.address}: {paths[0]} is not the snapshot asked for")
        if expect is not None and snap.hash != expect:
            raise FetchError(f"{source.address}: snapshot {snap.snapshot_id} hashes "
                             f"{snap.hash[:23]}… and the one after it names {expect[:23]}…: "
                             f"the chain does not link (09 §9.4)")
        if sid is None:
            advertised = (source.entry or {}).get("snapshot_hash")
            if advertised and advertised != snap.hash:
                p.lie(source, f"snapshot {snap.snapshot_id} hashes otherwise than the digest "
                              f"advertised (09 §9.9)")
                raise FetchError(f"{source.address}: snapshot {snap.snapshot_id} hashes "
                                 f"otherwise than its digest advertised — a lie, dropped")
        raw_sig = items.get(shard_mod.snapshot_sig_path(snap.snapshot_id))
        sidecar: dict[str, Any] | None = None
        if raw_sig is not None:
            try:
                loaded = json.loads(raw_sig)
                sidecar = loaded if isinstance(loaded, dict) else None
            except ValueError:
                sidecar = None
        if sidecar is not None:
            keys, complaints = sn.sidecar_keys(sidecar, snap.hash)
        else:
            keys, complaints = [], ["no sidecar came with it: unsigned"]
        out.append(Pulled(snap, data, sidecar, keys, complaints))
        if snap.previous is None:
            break
        prev_id = previous_snapshot_id(snap.snapshot_id)
        if codex.continues and prev_id == codex.continues_through:
            break                                   # the mother's snapshot: hers to hold
        if held.get(prev_id) == snap.previous:
            break                                   # the rest of the chain is held here
        sid, expect = prev_id, snap.previous
    else:
        raise FetchError(f"{source.address}: {MAX_CHAIN} snapshots back and no first: refused")
    return out


# ---- holding ------------------------------------------------------------------------

def verified_signatures(doc: dict[str, Any] | None, snapshot_hash: str) -> list[dict[str, Any]]:
    """The sidecar's signatures that verify over this snapshot (09 §9.4);
    one that does not is dropped, never kept."""
    from nacl import signing as nacl_signing
    from nacl.exceptions import BadSignatureError
    if not isinstance(doc, dict) or doc.get("kind") != sn.SIDECAR_KIND \
            or doc.get("snapshot") != snapshot_hash:
        return []
    msg = sn.sig_message(snapshot_hash)
    out = []
    for s in doc.get("signatures") or []:
        try:
            nacl_signing.VerifyKey(env.key_bytes(s["key"])).verify(msg, bytes.fromhex(s["sig"]))
            out.append({"key": s["key"], "time": s["time"], "sig": s["sig"]})
        except (BadSignatureError, KeyError, ValueError, TypeError, env.SignatureError):
            continue
    return out


def merge_sidecars(existing: dict[str, Any] | None, incoming: dict[str, Any] | None,
                   snapshot_hash: str) -> dict[str, Any]:
    """Two sidecars of one snapshot, by union (09 §9.4), sorted by (key, time)."""
    seen: dict[tuple[str, str], dict[str, Any]] = {}
    for doc in (existing, incoming):
        for s in verified_signatures(doc, snapshot_hash):
            seen.setdefault((s["key"], s["time"]), s)
    return {"kind": sn.SIDECAR_KIND, "version": 1, "snapshot": snapshot_hash,
            "signatures": [seen[k] for k in sorted(seen)]}


def _write_whole(target: Path, data: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.fetch")
    tmp.write_bytes(data)
    tmp.replace(target)


def held_snapshots(root: Path, collection_id: str) -> dict[str, str]:
    """`{snapshot_id: hash}` of the snapshots this peer's SHARD holds for the
    collection; empty when it is not held."""
    try:
        name = col.name_of(Path(root), collection_id)
    except (CodexError, FileNotFoundError, OSError):
        return {}
    d = col.shard_dir_of(Path(root), name) / "snapshots"
    out: dict[str, str] = {}
    for p in sorted(d.glob("*.json")) if d.is_dir() else []:
        if p.name.endswith(".sig.json"):
            continue
        try:
            out[p.name[:-5]] = sn.load(p).hash
        except Exception:   # noqa: BLE001 — a file that is no snapshot holds no id
            continue
    return out


def hold(root: Path, chain: list[tuple[cx.Codex, bytes]], pulled: list[Pulled], *,
         name: str | None = None, encoder: str | None = None,
         encoder_keys: dict[str, Any] | None = None) -> dict[str, Any]:
    """The Codex into the SHARD (validated against its mother from the
    chain), the chain's other Codexes under state/chain/, every signed
    snapshot into the SHARD with its sidecar merged by union, an unsigned
    one into state/staging/, and the collection's file written (07 §7.8)."""
    root = Path(root)
    head, head_bytes = chain[0]
    by_id = {c.collection_id: c for c, _ in chain}
    mother = next((by_id[r] for r in _parents(head) if r in by_id), None)
    shard_dir = col.write(root, head, mother, name=name)
    cname = col.name_of(root, head.collection_id)
    state = root / cname / "state"
    for c, data in chain[1:]:
        _write_whole(state / CHAIN / f"{shard_mod._hex(c.collection_id)}.json", data)
    promoted: list[str] = []
    kept: list[str] = []
    staged: list[str] = []
    for pl in pulled:
        rel = shard_mod.snapshot_path(pl.snapshot.snapshot_id)
        rel_sig = shard_mod.snapshot_sig_path(pl.snapshot.snapshot_id)
        if not pl.signed:
            _write_whole(state / STAGING / rel, pl.data)
            if pl.sidecar is not None:
                _write_whole(state / STAGING / rel_sig, canonical_json(pl.sidecar))
            staged.append(pl.snapshot.snapshot_id)
            continue
        target = shard_dir / rel
        if target.is_file():
            if target.read_bytes() != pl.data:
                raise FetchError(
                    f"{rel}: this peer holds another snapshot under that id; two snapshots "
                    f"under one id are decided by records-vs-origin on the shard where they "
                    f"differ (04 §4.3) — nothing promoted")
            kept.append(pl.snapshot.snapshot_id)
        else:
            _write_whole(target, pl.data)
            promoted.append(pl.snapshot.snapshot_id)
        sig_target = shard_dir / rel_sig
        existing = None
        if sig_target.is_file():
            try:
                existing = json.loads(sig_target.read_bytes())
            except ValueError:
                existing = None
        merged = merge_sidecars(existing, pl.sidecar, pl.snapshot.hash)
        data = canonical_json(merged)
        if not sig_target.is_file() or sig_target.read_bytes() != data:
            _write_whole(sig_target, data)
    from app import louise
    held_file = louise._write_held_file(root, head, encoder, source="shard",
                                        **(encoder_keys or {}))
    newest = pulled[0]
    return {
        "collection_id": head.collection_id, "name": cname, "chain": len(chain),
        "snapshot_id": newest.snapshot.snapshot_id, "snapshot_hash": newest.snapshot.hash,
        "signatures": len(newest.keys), "unsigned_because": newest.complaints if not newest.signed else [],
        "snapshots": {"promoted": promoted, "kept": kept, "staged": staged},
        "file": str(held_file), "shard": str(shard_dir),
    }


# ---- the command ----------------------------------------------------------------------

def fetch(root: Path, ref: str, *, snapshot_id: str | None = None,
          from_address: str | None = None, encoder: str | None = None,
          encoder_keys: dict[str, Any] | None = None,
          name: str | None = None, client=None, stop=None, session=None,
          now: float | None = None, log: Callable[[str], None] | None = None,
          key=None, own_key: str | None = None) -> dict[str, Any]:
    """`share fetch <collection>` as far as the first brick goes: the
    pre-acknowledgement set — codex.json and snapshots/* — pulled from the
    first peer that answers honestly, and the collection held. `ref` is a
    collection id or a lineage id. `encoder_keys`: the file's `[encoder]`
    api, embed_model and query_model beside `encoder` (07 §7.8, amended
    2026-10-09), checked before anything is pulled."""
    now = time.time() if now is None else now
    if encoder:
        from app import louise
        louise.model_keys(encoder, embed_model=(encoder_keys or {}).get("embed_model"),
                          query_model=(encoder_keys or {}).get("query_model"))
    if key is None or own_key is None:
        from app import identity
        from app.bundles import keys as node_keys
        key = node_keys.load_signing_key()
        own_key = identity.own()
    root = Path(root)
    con = peers.connect()
    try:
        found = sources(con, ref, now=now, from_address=from_address)
        if not found and session is not None:
            found = find_dht(session, ref)
        if not found:
            raise FetchError(f"no peer is known to hold {ref}: name one with --from, add one "
                             f"with `share peers add`, or turn the DHT on (04 §4.2)")
        cid = target_id(found, ref)
        p = Puller(key=key, own_key=own_key, client=client, stop=stop, con=con, log=log, now=now)
        held = held_snapshots(root, cid)
        tried: list[str] = []
        unreachable = 0
        for source in found:
            try:
                chain = pull_codex(p, source, cid)
                # 03 §3.7: for a certified collection the certifier's pointer
                # says which snapshot is current — the DHT, else the anchor
                # domain's well-known path; what is pulled must be that one
                want, pointed = snapshot_id, None
                if want is None and getattr(chain[0][0], "publication", "open") == "certified":
                    from app.share import pointer as pointer_mod
                    pointed = pointer_mod.current(chain[0][0], node_con=con, session=session, client=client,
                                                  now=now, log=log)
                    if pointed is not None:
                        want = pointed["snapshot_id"]
                pulled = pull_snapshots(p, source, chain[0][0], snapshot_id=want, held=held)
                if pointed is not None and pulled and pulled[0].snapshot.hash != pointed["snapshot_hash"]:
                    raise FetchError(f"{source.address}: snapshot {want} hashes {pulled[0].snapshot.hash[:23]}… "
                                     f"and the certifier's pointer names {pointed['snapshot_hash'][:23]}… "
                                     f"(09 §9.11)")
            except Unreachable as e:
                unreachable += 1
                tried.append(str(e))
                continue
            except FetchError as e:          # NotHeld, a lie, a chain that does not link
                tried.append(str(e))
                if log and not isinstance(e, NotHeld):
                    log(f"share fetch: {e}"[:500])
                continue
            out = hold(root, chain, pulled, name=name, encoder=encoder,
                       encoder_keys=encoder_keys)
            out["from"] = source.address
            out["tried"] = tried
            out["pre_ack"] = True
            out["note"] = ("codex.json and snapshots/* held; the members — records, terms, "
                           "calibration — are the second brick of step 9d")
            return out
        text = "; ".join(tried)
        if unreachable == len(found):
            raise Unreachable(f"no peer answered for {cid}: {text}")
        raise FetchError(f"no peer gave {cid} as its id, digest and chain say it: {text}")
    finally:
        con.close()
