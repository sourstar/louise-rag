"""The certifier's anchor (03 §3.7; 09 §9.11): the document at the Codex's
`certifier.anchor`, fetched over TLS — the domain's chain is the anchor —
listing the certifier's keys with their validity and the collections it
stands behind. Migration step 10.

A peer fetches it at adoption and every 24 hours with up to four hours of
jitter, drawing from the budget (04 §4.3), and keeps the last document
under `state/anchor/`. Unreachable changes nothing: the last state stands,
shown with its age, `stale` after 30 days — never a mismatch. A document
that does not list the Codex's key, or this collection, is a `mismatch`:
the collection is then open, published by a key that claims to be the
certifier. A key certifies the snapshots whose sidecar signature time is
at or after its `valid_from` and not after its `revoked_at`.

`certification` is what 05 §5.3 puts beside the trust level — the key
that certified the snapshot read and the anchor's state — and
`signed_roots` is what the manifest-only gate of 03 §3.8 admits at
promotion: the roots listed by a held snapshot a certifier key signed.
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

from app.hold import drag as drag_mod, snapshot as sn
from app.hold.codex import CodexError

KIND, VERSION = "louise-certifier", 1
REFRESH_S = 24 * 3600
JITTER_S = 4 * 3600
RETRY_S = 3600                    # after a failed fetch
STALE_S = 30 * 24 * 3600
TIMEOUT_S = 20.0
MAX_BYTES = 1 << 20
ANCHORED, STALE, MISMATCH, UNKNOWN = "anchored", "stale", "mismatch", "unknown"


class AnchorError(CodexError):
    exit_code = 2


class Unreachable(CodexError):
    exit_code = 3


# ---- the document -------------------------------------------------------------------

def parse(doc: Any) -> dict[str, Any]:
    """09 §9.11's anchor document, or why it is not one."""
    from app.share import messages
    if not isinstance(doc, dict) or doc.get("kind") != KIND:
        raise AnchorError(f"not a {KIND} document (09 §9.11)")
    if doc.get("version") != VERSION:
        raise AnchorError(f"anchor document version {doc.get('version')!r}; this build reads {VERSION}")
    keys = doc.get("keys")
    cols = doc.get("collections")
    if not isinstance(keys, list) or not isinstance(cols, list):
        raise AnchorError("an anchor document lists `keys` and `collections`")
    out_keys = []
    for k in keys:
        if not isinstance(k, dict) or not isinstance(k.get("key"), str) or not k["key"].startswith("ed25519:"):
            raise AnchorError("every anchor key is {key: ed25519:…, valid_from: time, revoked_at: time or null}")
        try:
            messages.parse_time(k.get("valid_from"))
            if k.get("revoked_at") is not None:
                messages.parse_time(k["revoked_at"])
        except Exception as e:   # noqa: BLE001 — a time that is not one
            raise AnchorError(f"anchor key {k['key'][:23]}…: {e}") from None
        out_keys.append({"key": k["key"], "valid_from": k["valid_from"], "revoked_at": k.get("revoked_at")})
    if not all(isinstance(c, str) for c in cols):
        raise AnchorError("`collections` is a list of collection ids")
    return {"kind": KIND, "version": VERSION, "keys": out_keys, "collections": list(cols)}


def valid_keys_at(doc: dict[str, Any], time_text: str) -> set[str]:
    """The keys certifying a signature made at `time_text` (03 §3.7)."""
    from app.share import messages
    try:
        t = messages.parse_time(time_text)
    except Exception:   # noqa: BLE001
        return set()
    out = set()
    for k in doc.get("keys", []):
        try:
            if messages.parse_time(k["valid_from"]) <= t and \
                    (k.get("revoked_at") is None or t <= messages.parse_time(k["revoked_at"])):
                out.add(k["key"])
        except Exception:   # noqa: BLE001
            continue
    return out


# ---- the state kept ------------------------------------------------------------------

def state_path(root: Path, name: str, codex) -> Path:
    return Path(root) / name / "state" / "anchor" / f"{drag_mod._hex(codex.collection_id)}.json"


def read_state(root: Path, name: str, codex) -> dict[str, Any] | None:
    p = state_path(root, name, codex)
    if not p.is_file():
        return None
    try:
        got = json.loads(p.read_text(encoding="utf-8"))
        return got if isinstance(got, dict) else None
    except (OSError, ValueError):
        return None


def record(root: Path, name: str, codex, *, document: dict[str, Any] | None, now: float,
           error: str | None = None) -> dict[str, Any]:
    """A fetched document replaces the last; a failure keeps it and notes
    the try."""
    state = read_state(root, name, codex) or {}
    if document is not None:
        state.update(document=document, fetched_at=now, error=None)
    else:
        state.update(tried_at=now, error=error)
    p = state_path(root, name, codex)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.tmp")
    tmp.write_text(json.dumps(state, sort_keys=True, indent=1), encoding="utf-8")
    tmp.replace(p)
    return state


def fetch(codex, *, client=None, stop=None) -> dict[str, Any]:
    """The document at the Codex's anchor, over TLS, no redirects — a
    redirect elsewhere would move the anchor off the domain. Draws the
    bytes read from the budget."""
    import httpx
    if codex.certifier is None:
        raise AnchorError("an open collection has no anchor")
    url = codex.certifier.anchor
    c = client or httpx.Client(timeout=TIMEOUT_S, follow_redirects=False)
    try:
        try:
            r = c.get(url)
        except httpx.HTTPError as e:
            raise Unreachable(f"{url}: {type(e).__name__}: {e}") from None
        body = r.content[:MAX_BYTES + 1]
        try:
            from app.transfer.bucket import Priority
            from app.transfer.manager import Direction, get_manager
            get_manager().bucket(Direction.DOWN).acquire(len(body), Priority.DELTA, stop=stop)
        except Exception:   # noqa: BLE001 — accounting after the fact never loses the document
            pass
        if r.status_code != 200:
            raise Unreachable(f"{url}: HTTP {r.status_code}")
        if len(body) > MAX_BYTES:
            raise AnchorError(f"{url}: more than {MAX_BYTES} bytes; not an anchor document")
        try:
            return parse(json.loads(body))
        except ValueError as e:
            raise AnchorError(f"{url}: not JSON: {e}") from None
    finally:
        if client is None:
            c.close()


def _jitter(codex) -> float:
    return int(hashlib.sha256(codex.collection_id.encode("utf-8")).hexdigest()[:8], 16) % JITTER_S


def due(root: Path, name: str, codex, *, now: float) -> bool:
    """Whether the periodic fetch is due: never fetched; a day plus this
    collection's jitter since the last document; an hour since a failure."""
    if codex.publication != "certified" or codex.certifier is None:
        return False
    st = read_state(root, name, codex)
    if not st:
        return True
    last_ok = st.get("fetched_at")
    tried = st.get("tried_at")
    next_at = (last_ok + REFRESH_S + _jitter(codex)) if last_ok is not None else 0.0
    if tried is not None and (last_ok is None or tried > last_ok):
        next_at = max(next_at if last_ok is not None else 0.0, tried + RETRY_S)
    return now >= next_at


def refresh(root: Path, name: str, codex, *, now: float, client=None, stop=None, log=None) -> dict[str, Any]:
    """Fetch and record, or record the failure; the status after."""
    try:
        doc = fetch(codex, client=client, stop=stop)
        record(root, name, codex, document=doc, now=now)
    except (Unreachable, AnchorError) as e:
        record(root, name, codex, document=None, now=now, error=str(e))
        if log:
            log(f"[{name}] anchor: {e}")
    return status(root, name, codex, now=now)


def status(root: Path, name: str, codex, *, now: float | None = None) -> dict[str, Any]:
    """`{state, why, fetched_at, age_s}` — 09 §9.11's `anchored` · `stale` ·
    `mismatch`, `unknown` before the first document; `state` None for an
    open collection."""
    now = time.time() if now is None else now
    if codex.publication != "certified" or codex.certifier is None:
        return {"state": None, "why": "an open collection has no anchor", "fetched_at": None, "age_s": None}
    st = read_state(root, name, codex)
    doc = (st or {}).get("document")
    if not doc:
        why = "the anchor has not been fetched here"
        if st and st.get("error"):
            why += f"; the last try failed: {st['error']}"
        return {"state": UNKNOWN, "why": why, "fetched_at": None, "age_s": None}
    fetched_at = float(st.get("fetched_at") or 0.0)
    age = max(0.0, now - fetched_at)
    out = {"fetched_at": fetched_at, "age_s": age}
    keys = {k["key"] for k in doc.get("keys", [])}
    if codex.certifier.key not in keys:
        return {**out, "state": MISMATCH,
                "why": "the anchor does not list the Codex's certifier key: open, published by a key that "
                       "claims to be the certifier (03 §3.7)"}
    if codex.collection_id not in doc.get("collections", []):
        return {**out, "state": MISMATCH, "why": "the anchor does not list this collection among those the "
                                                 "certifier stands behind (09 §9.11)"}
    if age > STALE_S:
        return {**out, "state": STALE, "why": f"the last successful fetch is {int(age // 86400)} days old"}
    return {**out, "state": ANCHORED, "why": "the anchor lists the certifier's key and this collection"}


# ---- what the anchor settles for a snapshot --------------------------------------------

def certifier_signatures(root: Path, name: str, codex, snapshot) -> list[dict[str, Any]]:
    """The sidecar signatures over `snapshot` that verified AND were made
    by a certifier key valid at their time: the anchor's keys where a
    document is held, else the Codex's `certifier.key` alone."""
    if codex.publication != "certified" or codex.certifier is None or snapshot is None:
        return []
    drag = Path(root) / name / "drag"
    p = drag / drag_mod.snapshot_sig_path(snapshot.snapshot_id)
    if not p.is_file():
        return []
    try:
        doc = json.loads(p.read_bytes())
    except (OSError, ValueError):
        return []
    verified, _ = sn.sidecar_keys(doc, snapshot.hash)
    anchor_doc = (read_state(root, name, codex) or {}).get("document")
    out = []
    for s in doc.get("signatures") or []:
        key = s.get("key")
        if key not in verified:
            continue
        if anchor_doc:
            if key not in valid_keys_at(anchor_doc, str(s.get("time"))):
                continue
        elif key != codex.certifier.key:
            continue
        out.append({"key": key, "time": s.get("time")})
    return out


def certification(root: Path, name: str, codex, snapshot, *, now: float | None = None) -> dict[str, Any] | None:
    """05 §5.3's `certification`: `{certified_by, anchor_state}`; None for
    an open collection; `certified_by` None when no certifier key signed
    the snapshot read."""
    if codex is None or codex.publication != "certified" or codex.certifier is None:
        return None
    sigs = certifier_signatures(root, name, codex, snapshot)
    return {"certified_by": sigs[0]["key"] if sigs else None,
            "anchor_state": status(root, name, codex, now=now)["state"]}


def signed_roots(root: Path, name: str, codex) -> set[str]:
    """03 §3.8's gate: the transport roots of every member listed by a held
    snapshot of the chain that a certifier key signed — any such snapshot,
    not only the newest, since a shard stays listed by every snapshot that
    still carries it."""
    from app.share import members as mem
    drag = Path(root) / name / "drag"
    d = drag / "snapshots"
    out: set[str] = set()
    if not d.is_dir():
        return out
    for p in sorted(d.glob("*.json")):
        if p.name.endswith(".sig.json"):
            continue
        try:
            snapshot = sn.load(p)
        except Exception:   # noqa: BLE001 — a file that is no snapshot lists nothing
            continue
        if not certifier_signatures(root, name, codex, snapshot):
            continue
        for m in mem.members_of(snapshot, drag_dir=drag):
            for f in m.files:
                out.add(f.root)
    return out
