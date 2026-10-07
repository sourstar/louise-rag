"""`hold collection certify <collection> <snapshot>` (07 §7.3; 03 §3.7;
09 §9.11): the certifier's signature appended to the snapshot's sidecar —
the one signature peers treat as certification — and the signed pointer
to the current certified snapshot written under `state/pointer/` for
`share` to publish and the web to serve at
`/.well-known/louise-pointer/<collection_id hex>.json`. Refuses when this
peer's key is not the Codex's `certifier.key`. Migration step 10.

The pointer's `seq` is strictly increasing per collection: a peer keeps
the highest verified `seq` and discards lower ones, so every pointer
written here is one more than the last.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.hold import drag as drag_mod, envelope as env, snapshot as sn
from app.hold.codex import CodexError, canonical_json

KIND, VERSION = "louise-pointer", 1


class CertifyError(CodexError):
    exit_code = 2


def pointer_path(root: Path, name: str, codex) -> Path:
    return Path(root) / name / "state" / "pointer" / f"{drag_mod._hex(codex.collection_id)}.json"


def read_pointer(root: Path, name: str, codex) -> dict[str, Any] | None:
    p = pointer_path(root, name, codex)
    if not p.is_file():
        return None
    try:
        doc = json.loads(p.read_bytes())
    except (OSError, ValueError):
        return None
    try:
        verify_pointer(doc, codex)
    except CodexError:
        return None
    return doc


def verify_pointer(doc: dict[str, Any], codex) -> dict[str, Any]:
    """The body, once the envelope verifies and it is this collection's
    certifier's pointer (09 §9.11)."""
    body = env.verify(env.POINTER, doc)
    if body.get("kind") != KIND or body.get("version") != VERSION:
        raise CertifyError(f"not a {KIND} v{VERSION}")
    if body.get("collection_id") != codex.collection_id:
        raise CertifyError("the pointer names another collection")
    if codex.certifier is None or body.get("key") != codex.certifier.key:
        raise CertifyError("the pointer is not signed by the Codex's certifier key")
    if not isinstance(body.get("seq"), int) or body["seq"] < 1:
        raise CertifyError("the pointer's seq is a positive integer")
    return body


def certify(root: Path, name: str, codex, snapshot_id: str, key, *, now: float) -> dict[str, Any]:
    """Sign the snapshot as the certifier and write the next pointer."""
    from app.share import messages
    root = Path(root)
    if codex.publication != "certified" or codex.certifier is None:
        raise CertifyError("an open collection has no certifier; nothing to certify (02 §2.8)")
    own = env.key_text(key.verify_key)
    if own != codex.certifier.key:
        raise CertifyError("this peer's key is not the Codex's certifier.key: it cannot certify (07 §7.3)")
    p = root / name / "drag" / "snapshots" / f"{snapshot_id}.json"
    if not p.is_file():
        raise CertifyError(f"no snapshot {snapshot_id} in the drag")
    snapshot = sn.load(p)
    time_text = messages.time_text(now)
    sidecar = sn.sign_snapshot(root, name, snapshot, key, now=time_text)
    previous = read_pointer(root, name, codex)
    seq = int(previous["body"]["seq"]) + 1 if previous else 1
    body = {"kind": KIND, "version": VERSION, "collection_id": codex.collection_id,
            "snapshot_id": snapshot.snapshot_id, "snapshot_hash": snapshot.hash, "seq": seq,
            "key": own, "time": time_text}
    doc = env.sign(env.POINTER, body, key)
    pp = pointer_path(root, name, codex)
    pp.parent.mkdir(parents=True, exist_ok=True)
    tmp = pp.with_name(f".{pp.name}.tmp")
    tmp.write_bytes(canonical_json(doc))
    tmp.replace(pp)
    return {"collection_id": codex.collection_id, "snapshot_id": snapshot.snapshot_id,
            "snapshot_hash": snapshot.hash, "seq": seq, "key": own,
            "signatures": len(sidecar.get("signatures") or []), "pointer": str(pp)}
