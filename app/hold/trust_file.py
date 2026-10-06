"""The peer's trust file, `state/trust.json` (09 §9.8; 07 §7.8) — local,
never signed, never sent: the keys whose word is counted (`allow`), the
keys ignored (`deny`), and the keys BURNED on this peer by a proof of
forgery (`quarantined`, the field keeping the name it had; 03 §3.5). A
burn is permanent until `hold key trust <key> --restore`.

Read by whoever counts attestations (app/share/levels.py takes the
filter `as_filter` gives); written by a T4 miss that reproduced
(app/bundles/verify_on_use.py through the worker) and by the operator.
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
