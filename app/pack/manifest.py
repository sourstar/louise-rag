"""The pack manifest — pack-format.md §7. JSON, signed by the node key.

The signature covers the canonical JSON of every field but `signature`
(sorted keys, compact separators), Ed25519 with the node's bundle key —
the same identity bundles carry. `publisher` is the public key, hex.
"""
import hashlib
import json
import time
from pathlib import Path

from nacl import signing
from nacl.exceptions import BadSignatureError

from ..bundles import keys as node_keys

KIND = "louise-pack"
VERSION = 1
FILENAME = "manifest.json"


class ManifestError(RuntimeError):
    """Missing, malformed, or wrongly signed."""


def canonical_bytes(manifest: dict) -> bytes:
    body = {k: v for k, v in manifest.items() if k != "signature"}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def collection_id(plugin: str, name: str, publisher_hex: str | None) -> str:
    seed = f"{plugin}:{name}:{publisher_hex or 'unsigned'}".encode("utf-8")
    return f"urn:louise:{plugin}:{hashlib.blake2b(seed, digest_size=8).hexdigest()}"


def new_manifest(*, plugin: str, name: str, snapshot: str, license_class: str,
                 terms_sha256: str | None, slicing: dict, layers: list[dict],
                 cleaning_version: int, origin: dict | None = None,
                 previous: str | None = None, publisher_hex: str | None = None) -> dict:
    return {
        "kind": KIND, "version": VERSION,
        "collection_id": collection_id(plugin, name, publisher_hex),
        "name": name, "plugin": plugin,
        "license": {"class": license_class, "terms_sha256": terms_sha256},
        "origin": origin or {"plugin": plugin},
        "snapshot": snapshot, "previous": previous,
        "publisher": publisher_hex, "signature": None,
        "layers": layers, "cleaning_version": cleaning_version,
        "slicing": slicing,
        "slices": {}, "shards": [],
        "spaces": [], "text_index": None,
        "update": {"cadence": None, "deadline": None, "grace_days": 7, "coordinator": None},
        "members": [],
        "derived_from": None,
        "built_at": int(time.time()),
    }


def sign(manifest: dict, key: signing.SigningKey) -> dict:
    manifest = dict(manifest)
    manifest["publisher"] = node_keys.public_key_hex(key)
    manifest["signature"] = key.sign(canonical_bytes(manifest)).signature.hex()
    return manifest


def verify(manifest: dict) -> bool:
    """True when signed and the signature checks; False when unsigned.
    Raises ManifestError on a signature that does not verify."""
    sig, pub = manifest.get("signature"), manifest.get("publisher")
    if not sig or not pub:
        return False
    try:
        node_keys.verify_key_from_hex(pub).verify(canonical_bytes(manifest), bytes.fromhex(sig))
    except (BadSignatureError, ValueError, node_keys.NodeKeyError):
        raise ManifestError("manifest signature does not verify") from None
    return True


def write(pack_dir: Path, manifest: dict) -> Path:
    path = Path(pack_dir) / FILENAME
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(manifest, indent=1, ensure_ascii=False, sort_keys=True) + "\n",
                   encoding="utf-8")
    tmp.replace(path)
    return path


def load(pack_dir: Path) -> dict:
    path = Path(pack_dir) / FILENAME
    if not path.is_file():
        raise ManifestError(f"no {FILENAME} in {pack_dir}")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ManifestError(f"{path}: {type(e).__name__}") from None
    if manifest.get("kind") != KIND:
        raise ManifestError(f"{path}: not a {KIND} manifest")
    return manifest


def is_pack_dir(path: Path) -> bool:
    return (Path(path) / FILENAME).is_file()
