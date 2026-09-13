"""THE license gate — CLAUDE.md invariant 9, amendment 36 — for every
kind of collection, including one over a PACK and one run from a CODEX.

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
    to the pack of them);
  - a CODEX decides its collection's class — 02 §2.1: its license.class
    "is authoritative, and the plugin's declared class is only the
    default `hold collection create` writes" — and pins the terms by hash
    (09 §9.3, §9.13), so its acknowledgement is to the terms it pins.
    This module used to read only the plugin's registry class, so a Codex
    tightening green gutenberg to yellow was judged green by every gate
    but the build's presence check (audit, 2026-09-13). "The stricter
    wins", tried first, left a green Codex over a yellow plugin withheld
    for good: nothing records an ack for a green Codex (verification).

`state_for` is the one resolution every gate and every page uses.

`spec`, where a function takes one, is the AcquisitionSpec a caller
already holds — the acquisition engine, which fetches by a spec it was
handed. For a real plugin it is the registry's own, so the answer is the
same; the engine used to keep a private copy of this check instead, which
invariant 9 forbids (audit, 2026-09-13).
"""
import hashlib
import time
from pathlib import Path

from .acquire.spec import LicenseAckRequiredError
from .plugins.base import LicenseClass

NOT_REQUIRED, ACKNOWLEDGED, MISSING = "not_required", "acknowledged", "missing"


def _call(fn, collection, spec):
    # `spec` only when there is one: the module's own functions are what
    # tests substitute, and a substitute need not know about it
    return fn(collection) if spec is None else fn(collection, spec)


def _spec_for(collection, spec):
    if spec is not None:
        return spec
    from .acquire.specs import ACQUIRE_REGISTRY
    return ACQUIRE_REGISTRY.get(collection.plugin)


def _codex_license(collection):
    codex = getattr(collection, "codex", None)
    return getattr(codex, "license", None) if codex is not None else None


def license_class_for(collection, spec=None) -> str:
    """'green' | 'yellow' | 'red' | 'unknown'."""
    lic = _codex_license(collection)
    if lic is not None and getattr(lic, "cls", None):
        return lic.cls                  # the Codex is authoritative (02 §2.1)
    s = _spec_for(collection, spec)
    if s is not None:
        return s.license_class.value
    try:
        plugin = collection.plugin_instance()
    except Exception:   # noqa: BLE001 — an unknown plugin is not a green light
        return "unknown"
    cls = getattr(plugin, "license_class", None)
    return cls.value if isinstance(cls, LicenseClass) else "unknown"


def terms_path_for(collection, spec=None) -> Path | None:
    """The byte-exact terms text: a Codex collection's drag LICENSE-TERMS,
    the spec's file, or the pack's TERMS.txt."""
    lic = _codex_license(collection)
    if lic is not None and getattr(lic, "terms_sha256", None):
        p = Path(collection.manifest_path).parent.parent / "drag" / "LICENSE-TERMS"
        if p.is_file():
            return p
    s = _spec_for(collection, spec)
    if s is not None:
        return s.terms_text_path
    if collection.plugin == "packed":
        p = Path(collection.corpus_dir) / "TERMS.txt"
        return p if p.is_file() else None
    return None


def terms_sha256(path: Path | None) -> str | None:
    if path is None or not Path(path).is_file():
        return None
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def state_for(collection, spec=None) -> str:
    cls = _call(license_class_for, collection, spec)
    if cls in ("green", "unknown"):
        # green needs nothing; "unknown" is a plugin without a license
        # class at all — only a test double (the CorpusSource protocol
        # requires one), never a real source, and never a pack (a pack
        # whose manifest is unreadable is yellow by default)
        return NOT_REQUIRED
    ack = getattr(collection, "license_ack", None)
    if not ack or "/" not in ack:
        return MISSING
    lic = _codex_license(collection)
    pinned = str(getattr(lic, "terms_sha256", None) or "").replace("sha256:", "")
    # a Codex collection's ack is to the terms its Codex PINS
    current = pinned or terms_sha256(_call(terms_path_for, collection, spec))
    if not current:
        return MISSING
    _, _, acked = ack.partition("/")
    return ACKNOWLEDGED if acked.replace("sha256:", "") == current else MISSING


def make_ack(collection) -> str:
    """timestamp + sha256 of the collection's terms text."""
    path = terms_path_for(collection)
    digest = terms_sha256(path)
    if digest is None:
        raise LicenseAckRequiredError(
            f"{collection.name}: no terms text to acknowledge (plugin {collection.plugin})")
    return f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}/{digest}"


def require_ack(collection, *, use: str = "use", spec=None) -> None:
    """Raise unless the collection's data may be used. `use` names the
    action for the message (discovery, archive import, pack build)."""
    if _call(state_for, collection, spec) == MISSING:
        cls = _call(license_class_for, collection, spec)
        path = _call(terms_path_for, collection, spec)
        if _codex_license(collection) is not None:
            # A Codex collection's acknowledgement is state/ack.json, and
            # only `hold collection ack` records it — after checking the
            # terms against the Codex's pin. The Library's button and
            # `corpus add --ack-terms` write a TOML line its loader never
            # reads (verification pass, 2026-09-13).
            how = (f"Record it with: louise hold collection ack {collection.name}")
        else:
            how = (f"Record it in collections/{collection.name}.toml from the "
                   f"Library, or: python -m app.cli corpus add {collection.name} "
                   f"--ack-terms <path>")
        raise LicenseAckRequiredError(
            f"{collection.name}: license_class={cls} requires an acknowledgement "
            f"(timestamp + sha256 of {path or 'the terms text'}) before {use} — "
            f"CLAUDE.md invariant 9. {how}")


def require_ack_or_exit(collection, *, use: str = "use") -> None:
    """`require_ack` for a command-line tool: a REFUSED line and exit 1,
    never a traceback. The tools under tools/ ship in the image and embed,
    parse or publish a collection's data; none asked (audit, 2026-09-13)."""
    try:
        require_ack(collection, use=use)
    except LicenseAckRequiredError as e:
        raise SystemExit(f"REFUSED: {e}") from None
