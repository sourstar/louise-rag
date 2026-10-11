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
    """The byte-exact terms text: a Codex collection's SHARD LICENSE-TERMS,
    the spec's file, or the pack's TERMS.txt."""
    lic = _codex_license(collection)
    if lic is not None and getattr(lic, "terms_sha256", None):
        p = Path(collection.manifest_path).parent.parent / "shard" / "LICENSE-TERMS"
        if p.is_file():
            return p
    s = _spec_for(collection, spec)
    if s is not None:
        return s.terms_text_path
    return None


def terms_sha256(path: Path | None) -> str | None:
    if path is None or not Path(path).is_file():
        return None
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


#: 04 §4.4: "an acknowledgement is of a text, identified by its hash … A
#: collection whose Codex names a `terms_sha256` this peer has already
#: acknowledged … is acknowledged by that record." This peer's records are
#: every Codex collection's `state/ack.json` (`hold collection ack`) and
#: every collection TOML's `license_ack` — the parked legacy files
#: included: an operator's acknowledgement of a text does not lapse because
#: the collection that recorded it was set aside. Re-read at most every
#: `_PEER_ACKS_TTL_S`, since every gate asks every round.
_PEER_ACKS_TTL_S = 30.0
# keyed on the two DIRECTORIES read — physical identity, never a name —
# so a set read from one node layout never answers for another
_peer_acks: dict[tuple[str, str], tuple[float, frozenset[str]]] = {}


def reset_peer_acks() -> None:
    _peer_acks.clear()


def _peer_acknowledged() -> frozenset[str]:
    """The terms hashes (bare hex) this peer holds a record of
    acknowledging, from any collection's record."""
    import json
    import tomllib
    from . import collections as collections_mod      # lazy: it imports plugins
    root = collections_mod.louise_root() / "collections"
    tomls = collections_mod.collections_dir()
    key = (str(root.resolve()), str(tomls.resolve()))
    now = time.time()
    hit = _peer_acks.get(key)
    if hit is not None and now - hit[0] < _PEER_ACKS_TTL_S:
        return hit[1]
    found: set[str] = set()
    for p in (sorted(root.glob("*/state/ack.json")) if root.is_dir() else ()):
        try:
            found.add(str(json.loads(p.read_text(encoding="utf-8"))["terms_sha256"])
                      .replace("sha256:", ""))
        except (OSError, ValueError, KeyError, TypeError):
            continue
    for d in (tomls, tomls.parent / "collections-legacy"):
        for p in (sorted(d.glob("*.toml")) if d.is_dir() else ()):
            try:
                ack = tomllib.loads(p.read_text(encoding="utf-8")).get("license_ack")
            except (OSError, ValueError):
                continue
            if isinstance(ack, str) and "/" in ack:
                found.add(ack.partition("/")[2].replace("sha256:", ""))
    _peer_acks[key] = (now, frozenset(found))
    return _peer_acks[key][1]


def state_for(collection, spec=None) -> str:
    cls = _call(license_class_for, collection, spec)
    if cls in ("green", "unknown"):
        # green needs nothing; "unknown" is a plugin without a license
        # class at all — only a test double (the CorpusSource protocol
        # requires one), never a real source, and never a pack (a pack
        # whose manifest is unreadable is yellow by default)
        return NOT_REQUIRED
    lic = _codex_license(collection)
    pinned = str(getattr(lic, "terms_sha256", None) or "").replace("sha256:", "")
    # A CODEX collection is acknowledged by any record of its pinned terms
    # on this peer (04 §4.4), not only by one of its own
    by_peer = ACKNOWLEDGED if pinned and pinned in _peer_acknowledged() else MISSING
    ack = getattr(collection, "license_ack", None)
    if not ack or "/" not in ack:
        return by_peer
    # a Codex collection's ack is to the terms its Codex PINS
    current = pinned or terms_sha256(_call(terms_path_for, collection, spec))
    if not current:
        return MISSING
    _, _, acked = ack.partition("/")
    return ACKNOWLEDGED if acked.replace("sha256:", "") == current else by_peer


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
            how = f"Record it in collections/{collection.name}.toml from the Library."
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


# --------------------------------------------------------------------------
# the web sources of a deposit collection (04 §4.4, 06 §6.7, 09 §9.13)

#: The disclaimer the platform ships, byte-exact: what the operator accepts
#: once for the node before any URL but a keyed git clone is fetched.
WEB_DISCLAIMER = Path(__file__).parent / "plugins" / "deposit" / "WEB-DISCLAIMER.txt"
WEB_ACK_KIND = "louise-web-sources-ack"


def web_ack_path() -> Path:
    """`state/web-sources-ack.json` under LOUISE_ROOT (07 §7.8)."""
    from . import collections as collections_mod
    return collections_mod.louise_root() / "state" / "web-sources-ack.json"


def web_disclaimer_sha256() -> str:
    return "sha256:" + hashlib.sha256(WEB_DISCLAIMER.read_bytes()).hexdigest()


def web_sources_accepted() -> bool:
    """The node's acceptance, compared with the text the RUNNING platform
    ships — never a remembered hash, so a changed disclaimer asks again
    (09 §9.13)."""
    import json
    try:
        ack = json.loads(web_ack_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return (isinstance(ack, dict) and ack.get("kind") == WEB_ACK_KIND
            and ack.get("terms_sha256") == web_disclaimer_sha256())


def accept_web_sources(now: str | None = None) -> dict:
    """`hold collection ack --web-sources`: the time and the hash of the
    text, written atomically (09 §9.13)."""
    import json
    ack = {"kind": WEB_ACK_KIND, "version": 1,
           "terms_sha256": web_disclaimer_sha256(),
           "time": now or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    p = web_ack_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(ack, sort_keys=True), encoding="utf-8")
    tmp.replace(p)
    return ack


def is_keyed_git(url: str) -> bool:
    """A git repository over SSH — `git@host:path.git` or `ssh://…/x.git`
    — which this node clones with its own key."""
    u = url.strip()
    return u.lower().endswith(".git") and (u.startswith("ssh://")
                                           or ("@" in u.split(":", 1)[0] and ":" in u
                                               and "://" not in u))


def web_source_refusal(url: str) -> str | None:
    """Why `url` may not be fetched now, or None (04 §4.4). A git
    repository over SSH with this node's key needs nothing more; every
    other URL needs the node's acceptance of the web-sources disclaimer."""
    from . import config
    if is_keyed_git(url) and config.GIT_PRIVATE_KEY_FILE:
        return None
    if web_sources_accepted():
        return None
    return ("accept the web-sources disclaimer in Louise first: "
            "`louise hold collection ack --web-sources` (04 §4.4)")

