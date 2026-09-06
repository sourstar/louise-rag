"""The Codex — a collection's immutable recipe, and the identities derived
from it. docs/02-collections.md §2.1, §2.4, §2.7, §2.8.

Everything here is a pure function of the Codex's content. No I/O, no
network, no crypto: this module decides *what* a collection is, and two
peers must reach the same answer from the same bytes.

Numbers: the canonical form forbids floats. A float's textual form is not
guaranteed stable across languages and versions, and a single byte of
difference is a different collection, forever. Tolerances and the like are
carried as decimal strings ("0.95") and parsed where they are used.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, asdict
from typing import Any, Callable, Iterator

CODEX_KIND = "louise-codex"
CODEX_VERSION = 1

_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_DECIMAL_RE = re.compile(r"^-?(0|[1-9][0-9]*)(\.[0-9]+)?$")
_ANCHOR_RE = re.compile(r"^https://[^\s/]+/.+$")


class CodexError(ValueError):
    """The Codex is not a valid recipe. The message names the rule broken."""


# --------------------------------------------------------------------------
# canonical form and hashing

def canonical_json(obj: Any) -> bytes:
    """Deterministic JSON: sorted keys, no whitespace, UTF-8, no floats.

    Floats are refused rather than formatted: no formatting rule is stable
    enough to hang an identity on. None is allowed (it serialises as null).
    """
    _reject_floats(obj, path="$")
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def _reject_floats(obj: Any, path: str) -> None:
    if isinstance(obj, bool):
        return
    if isinstance(obj, float):
        raise CodexError(f"{path}: floats are not allowed in a Codex; "
                         f"use a decimal string")
    if isinstance(obj, dict):
        for k, v in obj.items():
            if not isinstance(k, str):
                raise CodexError(f"{path}: object keys must be strings")
            _reject_floats(v, f"{path}.{k}")
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            _reject_floats(v, f"{path}[{i}]")


def digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------------------
# the Codex

@dataclass(frozen=True)
class Toolchain:
    platform_image: str      # sha256:… — parser, chunker, cleaner, checks, and their libraries
    encoder_image: str       # sha256:… — the encoder build


@dataclass(frozen=True)
class Layer:
    chunker_id: str
    chunker_version: int
    rule: str = "default"


@dataclass(frozen=True)
class Space:
    encoder: str             # readable name; the guarantee is the image digest
    dim: int
    dtype: str
    ctx: int
    tolerance: str           # decimal string, e.g. "0.95"
    calibration: dict[str, Any] = field(default_factory=dict)
    epoch: str = ""          # "" for a root or a daughter; salted for a corrective sister


@dataclass(frozen=True)
class Certifier:
    key: str                 # the certifier's public key, hex
    anchor: str              # https URL where the key fingerprint is published
    is_origin: bool = False


@dataclass(frozen=True)
class Defect:
    """A reproducible statement of what was wrong with a replaced collection."""
    check: str               # "records-vs-origin" | "vectors-sample"
    toolchain: str           # image digest under which the check fails
    witnesses: tuple[str, ...]
    expected: str
    observed: str
    rationale: str


@dataclass(frozen=True)
class Codex:
    plugin: str
    origin: dict[str, Any]
    toolchain: Toolchain
    parser_id: str
    parser_version: int
    cleaning_version: int
    layers: tuple[Layer, ...]
    slicing: dict[str, Any]
    space: Space
    license_class: str       # green | yellow | red
    license_terms_sha256: str
    cadence: str = "monthly"
    cutoff: str = "origin-clock-midnight"
    grace_days: int = 7
    publication: str = "open"            # open | certified
    certifier: Certifier | None = None
    embedding: str = "open"              # open | certifier-only (certified only)
    succession: str = "frozen"           # frozen | open-after:<ISO-8601 duration>
    # lineage — at most one of the three
    continues: str | None = None         # daughter: parent codex id
    from_snapshot: str | None = None     # daughter: the first snapshot that is hers
    origin_reachable_until: str | None = None
    derived_from: str | None = None      # sister: the codex whose corpus is shared
    replaces: str | None = None          # corrective sister: the defective codex id
    defect: Defect | None = None

    # ---- derived identities ------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["kind"] = CODEX_KIND
        d["version"] = CODEX_VERSION
        d["layers"] = [asdict(l) for l in self.layers]
        if self.defect is not None:
            d["defect"] = asdict(self.defect)
            d["defect"]["witnesses"] = list(self.defect.witnesses)
        if self.publication == "certified":
            d["content"] = "manifest-only"     # fixed, never configurable (§2.8)
        return d

    def canonical(self) -> bytes:
        return canonical_json(self.to_dict())

    @property
    def collection_id(self) -> str:
        """The identity. Two Codexes with the same bytes are one collection."""
        return digest(self.canonical())

    @property
    def space_id(self) -> str:
        """space_id = H(encoder_image, layers, cleaning_version, epoch) (§2.4).

        Not the parser, not the origin, not the slicing: a sister shares
        none of those and still has a different space; a daughter changes
        the toolchain and keeps the space.
        """
        return digest(canonical_json({
            "encoder_image": self.toolchain.encoder_image,
            "layers": [asdict(l) for l in self.layers],
            "cleaning_version": self.cleaning_version,
            "epoch": self.space.epoch,
        }))

    @property
    def relation(self) -> str:
        if self.replaces is not None:
            return "corrective-sister"
        if self.continues is not None:
            return "daughter"
        if self.derived_from is not None:
            return "sister"
        return "root"


def corrective_epoch(replaced_codex_id: str) -> str:
    """The epoch that makes a corrective sister's space fresh even when its
    encoder, layers and cleaning equal the parent's (§2.7)."""
    return digest(canonical_json({"replaces": replaced_codex_id}))


# --------------------------------------------------------------------------
# validation

def validate(codex: Codex, parent: Codex | None = None) -> None:
    """Refuse a Codex that breaks a rule. `parent` is the codex named by
    `continues` / `derived_from` / `replaces`, when the caller has it; the
    rules that need it are skipped otherwise and re-checked on adoption.
    """
    if codex.plugin == "":
        raise CodexError("plugin is required")
    for name, val in (("platform_image", codex.toolchain.platform_image),
                      ("encoder_image", codex.toolchain.encoder_image)):
        if not _DIGEST_RE.match(val):
            raise CodexError(f"toolchain.{name} must be a sha256: digest, got {val!r}")
    if not codex.layers:
        raise CodexError("at least one layer is required")
    if codex.license_class not in ("green", "yellow", "red"):
        raise CodexError(f"license_class must be green|yellow|red, got {codex.license_class!r}")
    if not _DECIMAL_RE.match(codex.space.tolerance):
        raise CodexError(f"space.tolerance must be a decimal string, got {codex.space.tolerance!r}")
    if codex.cadence != "monthly":
        raise CodexError("only the monthly cadence is defined")
    if codex.grace_days < 0:
        raise CodexError("grace_days must be >= 0")

    # publication
    if codex.publication not in ("open", "certified"):
        raise CodexError(f"publication must be open|certified, got {codex.publication!r}")
    if codex.publication == "certified":
        if codex.certifier is None:
            raise CodexError("a certified collection names its certifier")
        if not codex.certifier.key:
            raise CodexError("certifier.key is required")
        if not _ANCHOR_RE.match(codex.certifier.anchor):
            raise CodexError("certifier.anchor must be an https URL")
        if codex.embedding not in ("open", "certifier-only"):
            raise CodexError("embedding must be open|certifier-only")
        if not (codex.succession == "frozen" or codex.succession.startswith("open-after:")):
            raise CodexError("succession must be frozen|open-after:<duration>")
    else:
        if codex.certifier is not None:
            raise CodexError("only a certified collection names a certifier")
        if codex.embedding != "open":
            raise CodexError("an open collection's embedding is open")

    # lineage — at most one relation
    relations = [r for r in (codex.continues, codex.derived_from, codex.replaces) if r is not None]
    if len(relations) > 1:
        raise CodexError("a Codex names at most one of continues, derived_from, replaces")
    if codex.continues is not None and codex.from_snapshot is None:
        raise CodexError("a daughter names the first snapshot that is hers (from_snapshot)")
    if codex.continues is None and (codex.from_snapshot or codex.origin_reachable_until):
        raise CodexError("from_snapshot and origin_reachable_until belong to a daughter")
    if codex.replaces is not None:
        if codex.defect is None:
            raise CodexError("a corrective sister carries a reproducible defect")
        if codex.defect.check not in ("records-vs-origin", "vectors-sample"):
            raise CodexError("defect.check must be an existing check type")
        if not codex.defect.witnesses:
            raise CodexError("defect.witnesses names at least one shard or record")
        if not _DIGEST_RE.match(codex.defect.toolchain):
            raise CodexError("defect.toolchain must be an image digest")
        if codex.space.epoch != corrective_epoch(codex.replaces):
            raise CodexError("a corrective sister's space.epoch is salted with the "
                             "replaced codex id (corrective_epoch)")
    elif codex.defect is not None:
        raise CodexError("only a corrective sister carries a defect")

    if parent is not None:
        _validate_against_parent(codex, parent)


def _validate_against_parent(codex: Codex, parent: Codex) -> None:
    pid = parent.collection_id
    if codex.relation == "daughter":
        if codex.continues != pid:
            raise CodexError("continues does not name the given parent")
        if codex.space != parent.space or codex.toolchain.encoder_image != parent.toolchain.encoder_image \
                or codex.layers != parent.layers or codex.cleaning_version != parent.cleaning_version:
            raise CodexError("a daughter keeps the parent's space: same encoder image, "
                             "layers, cleaning and epoch — this is a sister in disguise")
        if codex.plugin != parent.plugin:
            raise CodexError("a daughter continues the same plugin")
        if codex.publication != parent.publication:
            raise CodexError("a daughter keeps the parent's publication mode")
    elif codex.relation == "sister":
        if codex.derived_from != pid:
            raise CodexError("derived_from does not name the given parent")
        if codex.space_id == parent.space_id:
            raise CodexError("a sister has a different space; same space is the same collection")
        if codex.origin != parent.origin or codex.toolchain.platform_image != parent.toolchain.platform_image:
            raise CodexError("a sister shares the parent's corpus: same origin and platform image")
    elif codex.relation == "corrective-sister":
        if codex.replaces != pid:
            raise CodexError("replaces does not name the given parent")
        if codex.origin != parent.origin:
            raise CodexError("a corrective sister re-derives the same origin")


# --------------------------------------------------------------------------
# lineage

def lineage_id(codex: Codex, resolve: Callable[[str], Codex | None]) -> str:
    """The root Codex's id along `continues` (§2.7). `resolve` returns the
    Codex for an id the peer holds, or None; an unresolvable parent stops
    the walk at the last known link, which is honest — the peer cannot
    claim a root it has never seen."""
    seen: set[str] = set()
    cur = codex
    while cur.continues is not None:
        if cur.collection_id in seen:
            raise CodexError("lineage cycle")
        seen.add(cur.collection_id)
        parent = resolve(cur.continues)
        if parent is None:
            return cur.continues
        cur = parent
    return cur.collection_id


def walk_lineage(codex: Codex, resolve: Callable[[str], Codex | None]) -> Iterator[Codex]:
    yield codex
    cur = codex
    while cur.continues is not None:
        parent = resolve(cur.continues)
        if parent is None:
            return
        yield parent
        cur = parent


# --------------------------------------------------------------------------
# (de)serialisation

def from_dict(d: dict[str, Any]) -> Codex:
    if d.get("kind") != CODEX_KIND:
        raise CodexError(f"not a Codex: kind={d.get('kind')!r}")
    if d.get("version") != CODEX_VERSION:
        raise CodexError(f"unsupported Codex version {d.get('version')!r}")
    tc = d["toolchain"]
    sp = d["space"]
    cert = d.get("certifier")
    dfc = d.get("defect")
    return Codex(
        plugin=d["plugin"], origin=d["origin"],
        toolchain=Toolchain(tc["platform_image"], tc["encoder_image"]),
        parser_id=d["parser_id"], parser_version=d["parser_version"],
        cleaning_version=d["cleaning_version"],
        layers=tuple(Layer(l["chunker_id"], l["chunker_version"], l.get("rule", "default"))
                     for l in d["layers"]),
        slicing=d["slicing"],
        space=Space(sp["encoder"], sp["dim"], sp["dtype"], sp["ctx"], sp["tolerance"],
                    sp.get("calibration", {}), sp.get("epoch", "")),
        license_class=d["license_class"], license_terms_sha256=d["license_terms_sha256"],
        cadence=d.get("cadence", "monthly"), cutoff=d.get("cutoff", "origin-clock-midnight"),
        grace_days=d.get("grace_days", 7),
        publication=d.get("publication", "open"),
        certifier=Certifier(cert["key"], cert["anchor"], cert.get("is_origin", False)) if cert else None,
        embedding=d.get("embedding", "open"), succession=d.get("succession", "frozen"),
        continues=d.get("continues"), from_snapshot=d.get("from_snapshot"),
        origin_reachable_until=d.get("origin_reachable_until"),
        derived_from=d.get("derived_from"), replaces=d.get("replaces"),
        defect=Defect(dfc["check"], dfc["toolchain"], tuple(dfc["witnesses"]),
                      dfc["expected"], dfc["observed"], dfc["rationale"]) if dfc else None,
    )


def loads(data: bytes | str) -> Codex:
    codex = from_dict(json.loads(data))
    validate(codex)
    return codex


def dumps(codex: Codex) -> bytes:
    """The canonical bytes. This is what is stored, shared and hashed; there
    is no pretty form on disk, so the file's hash is the collection's id."""
    validate(codex)
    return codex.canonical()
