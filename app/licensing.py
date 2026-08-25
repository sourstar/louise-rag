"""THE license gate — CLAUDE.md invariant 9, amendment 36 — for every
kind of collection, including one over a PACK.

A yellow- or red-class collection needs a recorded acknowledgement
(timestamp + sha256 of the terms text, byte-exact) before its data is
USED, however the data arrived: acquisition (before the first byte),
discovery of a hand-populated or packed folder, an archive of
pre-embedded vectors, exchange. One rule, one terms text:

  - an origin plugin's terms are its AcquisitionSpec's `terms_text_path`;
  - a PACK carries the origin's terms byte-exact as `TERMS.txt`, and its
    manifest names the class — the SAME sha256, so the origin's ack is
    valid for the pack and vice versa (the operator's rule, 2026-08-27:
    the license that applies to the corpus and its pre-embeddings applies
    to the pack of them).

`state_for` is the one resolution every gate and every page uses.
"""
import hashlib
import time
from pathlib import Path

from .acquire.spec import LicenseAckRequiredError
from .plugins.base import LicenseClass

NOT_REQUIRED, ACKNOWLEDGED, MISSING = "not_required", "acknowledged", "missing"


def license_class_for(collection) -> str:
    """'green' | 'yellow' | 'red' | 'unknown'."""
    from .acquire.specs import ACQUIRE_REGISTRY
    spec = ACQUIRE_REGISTRY.get(collection.plugin)
    if spec is not None:
        return spec.license_class.value
    try:
        plugin = collection.plugin_instance()
    except Exception:   # noqa: BLE001 — an unknown plugin is not a green light
        return "unknown"
    cls = getattr(plugin, "license_class", None)
    return cls.value if isinstance(cls, LicenseClass) else "unknown"


def terms_path_for(collection) -> Path | None:
    """The byte-exact terms text: the spec's file, or the pack's TERMS.txt."""
    from .acquire.specs import ACQUIRE_REGISTRY
    spec = ACQUIRE_REGISTRY.get(collection.plugin)
    if spec is not None:
        return spec.terms_text_path
    if collection.plugin == "packed":
        p = Path(collection.corpus_dir) / "TERMS.txt"
        return p if p.is_file() else None
    return None


def terms_sha256(path: Path | None) -> str | None:
    if path is None or not Path(path).is_file():
        return None
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def state_for(collection) -> str:
    cls = license_class_for(collection)
    if cls in ("green", "unknown"):
        # green needs nothing; "unknown" is a plugin without a license
        # class at all — only a test double (the CorpusSource protocol
        # requires one), never a real source, and never a pack (a pack
        # whose manifest is unreadable is yellow by default)
        return NOT_REQUIRED
    ack = getattr(collection, "license_ack", None)
    if not ack or "/" not in ack:
        return MISSING
    current = terms_sha256(terms_path_for(collection))
    if current is None:
        return MISSING
    _, _, acked = ack.partition("/")
    return ACKNOWLEDGED if acked == current else MISSING


def make_ack(collection) -> str:
    """timestamp + sha256 of the collection's terms text."""
    path = terms_path_for(collection)
    digest = terms_sha256(path)
    if digest is None:
        raise LicenseAckRequiredError(
            f"{collection.name}: no terms text to acknowledge (plugin {collection.plugin})")
    return f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}/{digest}"


def require_ack(collection, *, use: str = "use") -> None:
    """Raise unless the collection's data may be used. `use` names the
    action for the message (discovery, archive import, pack build)."""
    if state_for(collection) == MISSING:
        cls = license_class_for(collection)
        path = terms_path_for(collection)
        raise LicenseAckRequiredError(
            f"{collection.name}: license_class={cls} requires an acknowledgement "
            f"(timestamp + sha256 of {path or 'the terms text'}) recorded in "
            f"collections/{collection.name}.toml before {use} — CLAUDE.md invariant 9. "
            f"Acknowledge it from the Library, or: python -m app.cli corpus add "
            f"{collection.name} --ack-terms <path>")
