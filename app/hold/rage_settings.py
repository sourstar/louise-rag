"""RECOMMENDED RAGE SETTINGS, part of a collection (operator, 2026-11-01:
"A recommended, non binding, setting shall be part of a collection"; choice
B: a separate signed file in the collection's SHARD, one entry per card type,
updatable without touching the collection's identity; docs/09-formats.md
§9.19). RAGE is the RAG Engine, Louise's own (docs/notes/rage.md); its
settings are how it drives an encoder, never the encoder's ENGINE (09 §9.3),
which is the Codex's.

What an entry says: on THIS card type (its driver and PCI vendor:device), for
this collection's space (the file names it, so the weights are fixed),
RAGE's settings — texts a request (`batch`), requests in flight
(`concurrency`) — what they were measured at (the card's busy share on its
compute engine, chunks a second, the window) and on which engine (the
`engine_id` of the validated run that answered, 09 §9.3: what it was
measured on, for a reader; it selects nothing, since any engine whose run
passes the gate may contribute). Who measured is who signed: each signer
keeps ITS entries in its own file, `rage-settings/<key hex>.json` at the
SHARD's root, so two peers never overwrite each other and a re-measurement
replaces only the signer's own entry for that card type.

NON-BINDING, by construction:
- a node's own collection file wins: a `batch` or `concurrency` written in its
  `[encoder]` is the operator's choice and is kept (RULE 2: choices for
  operators live in files, not dialogs);
- an entry is used only for a key the file leaves unset, and only where its
  card type is one of this node's — another card's numbers are not this
  card's;
- whatever is taken goes through `collections.recipe_for`
  (app/rage/settings.py chooses it for this node), so the gate
  validates it the way this peer contributes (`run_check.contributing`)
  before one vector is written: a setting a node cannot reproduce is never
  used unchecked.

Nothing here is a secret: settings and measurements are what a peer may
publish (invariant 8).

THE STANDARD THIS DOES NOT USE: a tuned-configuration cache keyed on the
device — what cuDNN's autotuner, TVM's tophub and Triton Inference
Server's model analyzer keep (best launch settings per GPU model). None of
them travels signed with a dataset; this is the case solved simply, in the
envelope every signed object here already uses (09 §9.2).
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from . import envelope as env
from .codex import CodexError, canonical_json, digest

KIND = "louise-rage-settings"
VERSION = 1
DOMAIN = env.RAGE_SETTINGS
#: the member's directory at the SHARD's root (09 §9.16, `rage-settings`)
DIR = "rage-settings"
_HEX4 = re.compile(r"^[0-9a-f]{4}$")
_SHA = re.compile(r"^sha256:[0-9a-f]{64}$")
_DECIMAL = re.compile(r"^(0|[1-9][0-9]*)(\.[0-9]+)?$")
#: settings an entry may carry, and their bounds — a peer's file is untrusted
SETTINGS = {"batch": (1, 1024), "concurrency": (1, 64)}
BODY_KEYS = {"kind", "version", "key", "time", "collection_id", "space_id", "entries"}
ENTRY_KEYS = {"card", "engine_id", "settings", "measured"}


@dataclass(frozen=True)
class Card:
    """A card TYPE, not a card: the driver it binds and its PCI ids."""
    driver: str
    vendor_id: str
    device_id: str

    def to_dict(self) -> dict[str, str]:
        return {"driver": self.driver, "vendor_id": self.vendor_id,
                "device_id": self.device_id}


@dataclass(frozen=True)
class Recommendation:
    """The settings one entry recommends, and who measured them."""
    batch: int | None
    concurrency: int | None
    signer: str
    measured: dict[str, Any]
    engine_id: str
    card: Card


def path_of(shard_dir: Path, signer: str) -> Path:
    return Path(shard_dir) / DIR / (env.key_bytes(signer).hex() + ".json")


def engine_id_of(engine: dict[str, Any]) -> str:
    """The `engine_id` of a stated run's engine object (09 §9.3)."""
    return digest(canonical_json(engine))


def entry(card: Card, *, engine_id: str, batch: int | None = None,
          concurrency: int | None = None, busy: str | None = None,
          chunks_per_s: str | None = None, window_s: int | None = None,
          time: str) -> dict[str, Any]:
    """One entry, checked as a reader checks it."""
    settings = {k: v for k, v in (("batch", batch), ("concurrency", concurrency))
                if v is not None}
    measured: dict[str, Any] = {"time": time}
    if busy is not None:
        measured["busy"] = busy
    if chunks_per_s is not None:
        measured["chunks_per_s"] = chunks_per_s
    if window_s is not None:
        measured["window_s"] = int(window_s)
    out = {"card": card.to_dict(), "engine_id": engine_id,
           "settings": settings, "measured": measured}
    _check_entry(out, "entry")
    return out


def _check_entry(e: Any, where: str) -> None:
    if not isinstance(e, dict) or set(e) != ENTRY_KEYS:
        raise CodexError(f"{where}: an entry is exactly {{card, engine_id, settings, measured}}")
    card, settings, measured = e["card"], e["settings"], e["measured"]
    if not isinstance(card, dict) or set(card) != {"driver", "vendor_id", "device_id"} \
            or not isinstance(card["driver"], str) or not card["driver"] \
            or not all(isinstance(card[k], str) and _HEX4.match(card[k])
                       for k in ("vendor_id", "device_id")):
        raise CodexError(f"{where}: `card` is {{driver, vendor_id, device_id}}, the ids four "
                         f"lowercase hex digits")
    if not isinstance(e["engine_id"], str) or not _SHA.match(e["engine_id"]):
        raise CodexError(f"{where}: `engine_id` is a sha256 id")
    if not isinstance(settings, dict) or not settings or not set(settings) <= set(SETTINGS):
        raise CodexError(f"{where}: `settings` names one or more of {sorted(SETTINGS)}")
    for k, v in settings.items():
        lo, hi = SETTINGS[k]
        if not isinstance(v, int) or isinstance(v, bool) or not lo <= v <= hi:
            raise CodexError(f"{where}: `settings.{k}` is a whole number in [{lo}, {hi}]")
    if not isinstance(measured, dict) or not isinstance(measured.get("time"), str) or \
            not set(measured) <= {"time", "busy", "chunks_per_s", "window_s"}:
        raise CodexError(f"{where}: `measured` is {{time[, busy, chunks_per_s, window_s]}}")
    for k in ("busy", "chunks_per_s"):
        if k in measured and not (isinstance(measured[k], str) and _DECIMAL.match(measured[k])):
            raise CodexError(f"{where}: `measured.{k}` is a decimal string (09 §9.1)")
    if "window_s" in measured and (not isinstance(measured["window_s"], int)
                                   or isinstance(measured["window_s"], bool)
                                   or measured["window_s"] < 1):
        raise CodexError(f"{where}: `measured.window_s` is a whole number of seconds")


def _card_key(c: dict[str, str]) -> str:
    return f"{c['driver']}:{c['vendor_id']}:{c['device_id']}"


def record(shard_dir: Path, key, *, collection_id: str, space_id: str,
           new: dict[str, Any], time: str) -> Path:
    """Sign and write this signer's file with `new` in it, replacing its own
    entry for the same card type; the others it holds are kept."""
    _check_entry(new, "entry")
    signer = env.key_text(key.verify_key)
    p = path_of(shard_dir, signer)
    entries: list[dict[str, Any]] = []
    if p.is_file():
        body = read(p)
        if body["collection_id"] == collection_id and body["space_id"] == space_id:
            entries = [e for e in body["entries"]
                       if _card_key(e["card"]) != _card_key(new["card"])]
    entries.append(new)
    entries.sort(key=lambda e: _card_key(e["card"]))
    body = {"kind": KIND, "version": VERSION, "key": signer, "time": time,
            "collection_id": collection_id, "space_id": space_id, "entries": entries}
    envelope = env.sign(DOMAIN, body, key)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_bytes(canonical_json(envelope))
    os.replace(tmp, p)
    return p


def read(path: Path) -> dict[str, Any]:
    """A signer's file, verified: its signature, its kind, its entries, and
    that its name is its signer's. Raises CodexError otherwise."""
    try:
        envelope = json.loads(Path(path).read_bytes())
    except (OSError, ValueError) as e:
        raise CodexError(f"{path}: not readable JSON ({type(e).__name__})") from None
    if not isinstance(envelope, dict):
        raise CodexError(f"{path}: not an envelope")
    body = env.verify(DOMAIN, envelope)
    if body.get("kind") != KIND or body.get("version") != VERSION:
        raise CodexError(f"{path}: not a {KIND} version {VERSION}")
    if set(body) != BODY_KEYS:          # 09 §9.18: an unknown key is refused
        raise CodexError(f"{path}: the body is exactly {sorted(BODY_KEYS)}")
    if Path(path).name != env.key_bytes(body["key"]).hex() + ".json":
        raise CodexError(f"{path}: the file is named for another signer than its body's")
    for k in ("collection_id", "space_id"):
        if not isinstance(body.get(k), str) or not body[k].startswith("sha256:"):
            raise CodexError(f"{path}: `{k}` is a sha256 id")
    if not isinstance(body.get("entries"), list) or not body["entries"]:
        raise CodexError(f"{path}: `entries` is a non-empty list")
    for i, e in enumerate(body["entries"]):
        _check_entry(e, f"{path} entries[{i}]")
    if len({_card_key(e["card"]) for e in body["entries"]}) != len(body["entries"]):
        raise CodexError(f"{path}: one entry per card type")
    return body


def read_all(shard_dir: Path, *, collection_id: str, space_id: str
             ) -> tuple[list[dict[str, Any]], list[str]]:
    """Every signer's verified file for THIS collection and space, and why
    each other one was set aside."""
    d = Path(shard_dir) / DIR
    bodies, refused = [], []
    try:
        files = sorted(d.glob("*.json"))
    except OSError:
        return bodies, refused
    for f in files:
        try:
            body = read(f)
        except CodexError as e:
            refused.append(str(e))
            continue
        if body["collection_id"] != collection_id or body["space_id"] != space_id:
            refused.append(f"{f}: for another collection or space")
            continue
        bodies.append(body)
    return bodies, refused


def recommend(bodies: Iterable[dict[str, Any]], cards: Iterable[Card], *,
              own: str | None = None) -> Recommendation | None:
    """The entry for this node: one of its card types. This node's own
    measurement first; else the most recently measured; ties by signer, so
    every node reading the same files picks the same."""
    wanted = {_card_key(c.to_dict()) for c in cards}
    found = []
    for body in bodies:
        for e in body["entries"]:
            if _card_key(e["card"]) in wanted:
                found.append((body["key"] == own, e["measured"]["time"], body["key"], e))
    if not found:
        return None
    _, _, signer, e = max(found, key=lambda f: (f[0], f[1], f[2]))
    s = e["settings"]
    return Recommendation(batch=s.get("batch"), concurrency=s.get("concurrency"),
                          signer=signer, measured=dict(e["measured"]),
                          engine_id=e["engine_id"], card=Card(**e["card"]))
