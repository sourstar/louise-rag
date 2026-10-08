"""The peer's trust file, `state/trust.json` (09 §9.8; 07 §7.8) — local,
never signed, never sent: the keys whose word is counted (`allow`), the
keys ignored (`deny`), and the keys BURNED on this peer by a proof of
forgery (`quarantined`, the field keeping the name it had; 03 §3.5). A
burn is permanent until `hold key trust <key> --restore`.

Read by whoever counts attestations (app/share/levels.py takes the
filter `as_filter` gives) and by every bundle door (`muted`: the legacy
inbox, app/bundles/trust.py, and `hold vectors import`); written by a T4
miss that reproduced (app/bundles/verify_on_use.py through the worker)
and by the operator. It is the ONE trust list: the per-collection
`providers` table keeps only bundle counts.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

KIND, VERSION = "louise-trust", 1


def path(root: Path | None = None) -> Path:
    """`<louise root>/state/trust.json`, beside node.sqlite. `root` is the
    louise root, not its `collections/`."""
    from app import config
    root = config.louise_root() if root is None else Path(root)
    return root / "state" / "trust.json"


def _empty() -> dict[str, Any]:
    return {"kind": KIND, "version": VERSION, "allow": [], "deny": [], "quarantined": []}


def read(root: Path | None = None) -> dict[str, Any]:
    """The file, or the empty list when there is none; a file that does not
    read is the empty list too — nothing is trusted by accident."""
    p = path(root)
    if not p.is_file():
        return _empty()
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _empty()
    if not isinstance(doc, dict) or doc.get("kind") != KIND:
        return _empty()
    out = _empty()
    for field in ("allow", "deny", "quarantined"):
        v = doc.get(field)
        out[field] = list(v) if isinstance(v, list) else []
    return out


def write(doc: dict[str, Any], root: Path | None = None) -> Path:
    p = path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.tmp")
    tmp.write_text(json.dumps(doc, sort_keys=True, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(p)
    return p


def burned(root: Path | None = None) -> set[str]:
    return {str(e.get("key")) for e in read(root)["quarantined"] if isinstance(e, dict) and e.get("key")}


def as_filter(root: Path | None = None) -> dict[str, set[str]]:
    """What `levels.counting` takes: `{allow, deny, burn}` as sets of keys."""
    doc = read(root)
    return {"allow": {str(k) for k in doc["allow"]}, "deny": {str(k) for k in doc["deny"]},
            "burn": burned(root)}


def burn(key: str, attestation: str | None, *, root: Path | None = None,
         now: float | None = None) -> dict[str, Any]:
    """Record the burn (03 §3.5): `{key, attestation, time}`, once per key —
    a second proof against a burned key changes nothing."""
    from app.share import messages
    doc = read(root)
    if key not in {e.get("key") for e in doc["quarantined"] if isinstance(e, dict)}:
        doc["quarantined"].append({"key": key, "attestation": attestation, "time": messages.time_text(now)})
        write(doc, root)
    return doc


def restore(key: str, *, root: Path | None = None) -> bool:
    """`hold key trust <key> --restore`: the one way a burn ends. Whether
    there was one to end."""
    doc = read(root)
    kept = [e for e in doc["quarantined"] if not (isinstance(e, dict) and e.get("key") == key)]
    if len(kept) == len(doc["quarantined"]):
        return False
    doc["quarantined"] = kept
    write(doc, root)
    return True


def key_text(producer: str | None) -> str | None:
    """A producer as 09 §9.2 writes a key: a bundle header of the legacy
    inbox and its ledger keep bare hex, a proof and this file carry
    `ed25519:<hex>`. One form, or a burn would never shut the key out."""
    if not producer:
        return None
    if producer.startswith("ed25519:"):
        return producer
    if len(producer) == 64 and all(c in "0123456789abcdefABCDEF" for c in producer):
        return "ed25519:" + producer.lower()
    return producer


def muted(key: str | None, root: Path | None = None) -> str | None:
    """03 §3.5: a key this peer DENIED, or one a proof of forgery BURNED,
    is one it accepts no traffic from at all, bundles included. `"burned"`,
    `"denied"`, or None when the key is not muted. Either form of key."""
    k = key_text(key)
    if k is None:
        return None
    doc = read(root)
    if k in {str(e.get("key")) for e in doc["quarantined"] if isinstance(e, dict)}:
        return "burned"
    if k in {str(x) for x in doc["deny"]}:
        return "denied"
    return None


def refusal(key: str | None, root: Path | None = None) -> str | None:
    """Why a bundle signed by `key` is refused by the trust list, in the
    words that name the one command that changes it; None when it is not."""
    why = muted(key, root)
    if why is None:
        return None
    k = key_text(key)
    lift = "--restore" if why == "burned" else "--remove"
    return (f"producer {k} is {why} on this peer's trust list ({path(root)}); "
            f"a muted key's bundles are refused (03 §3.5). Lift it with "
            f"`louise hold key trust {k} {lift}`")
