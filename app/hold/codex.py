"""The Codex — a collection's immutable recipe, and the identities derived
from it. docs/09-formats.md §9.3 fixes the bytes; docs/02-collections.md
§2.1, §2.4, §2.7, §2.8 say why.

Everything here is a pure function of the Codex's content. No I/O, no
network, no crypto: this module decides *what* a collection is, and two
peers must reach the same answer from the same bytes.

Two hashes, not one (§9.3). `collection_id` is the identity and is taken
over the object with the key `descriptive` REMOVED — removed, not nulled,
because a null value is a different object. `codex_hash` is taken over the
file, `descriptive` included, and is what a peer compares a fetched
`codex.json` against. Two honest peers may hold one `collection_id` under
two titles, and then their `codex_hash`es differ; that is not a conflict.

Numbers: the canonical form forbids floats. A float's textual form is not
guaranteed stable across languages and versions, and a single byte of
difference is a different collection, forever. Tolerances and the like are
carried as decimal strings ("0.95") and parsed where they are used.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator

CODEX_KIND = "louise-codex"
CODEX_VERSION = 1

_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_KEY_RE = re.compile(r"^ed25519:[0-9a-f]{64}$")
_DECIMAL_RE = re.compile(r"^-?(0|[1-9][0-9]*)(\.[0-9]+)?$")
_ANCHOR_RE = re.compile(r"^https://[^\s/]+/.+$")
_DURATION_RE = re.compile(r"^P(?!$)(\d+Y)?(\d+M)?(\d+W)?(\d+D)?"
                          r"(T(?!$)(\d+H)?(\d+M)?(\d+S)?)?$")

# The checks a plan may name (03 §3.2). §9.8's table is wider: it adds the
# results a peer produces from what it finds rather than from a plan.
PLAN_CHECKS = ("roots", "records", "chunks", "records-vs-origin",
               "vectors-sample", "calibration", "centres", "labels",
               "license", "signature", "anchor", "origin-seal", "evaluation")

# The checks whose plan entry is a sample size (§9.3, the validation block).
SAMPLED_CHECKS = ("records", "chunks", "records-vs-origin", "vectors-sample")


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


def _only(d: dict[str, Any], allowed: tuple[str, ...], where: str) -> dict[str, Any]:
    """§9.18: a field the schema does not list is refused on any hashed or
    signed object. Refused, never ignored — ignoring it would let two peers
    hash the same collection differently."""
    unknown = sorted(set(d) - set(allowed))
    if unknown:
        raise CodexError(f"{where}: unknown field(s) {', '.join(unknown)}; "
                         f"a Codex carries exactly the fields of 09 §9.3")
    missing = sorted(set(allowed) - set(d))
    if missing:
        raise CodexError(f"{where}: missing field(s) {', '.join(missing)}; "
                         f"every field is present, a field not in use is null")
    return d


# --------------------------------------------------------------------------
# the blocks

@dataclass(frozen=True)
class Descriptive:
    """For people. The one key outside `collection_id` (§9.3)."""
    title: str
    summary: str

    def to_dict(self) -> dict[str, Any]:
        return {"title": self.title, "summary": self.summary}


@dataclass(frozen=True)
class Origin:
    spec: dict[str, Any]     # the identifying half of the acquisition spec (§9.17)
    form: str                # which origin form was taken and why (06 §6.4)
    unit: str                # "file" | "container-member" | "index-page"
    clock: str               # IANA zone of the origin's clock
    cutoff_rule: str         # "dated-dumps" | "update-files" | "record-date"
    jurisdiction: str | None

    def to_dict(self) -> dict[str, Any]:
        return {"spec": self.spec, "form": self.form, "unit": self.unit,
                "clock": self.clock, "cutoff_rule": self.cutoff_rule,
                "jurisdiction": self.jurisdiction}


@dataclass(frozen=True)
class Toolchain:
    """Image digests. The platform build only: the encoder image is named in
    `space.encoder.image` and nowhere else (§9.3)."""
    platform_image: str
    mother_platform_image: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"platform_image": self.platform_image,
                "mother_platform_image": self.mother_platform_image}


@dataclass(frozen=True)
class Parser:
    id: str
    version: int

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "version": self.version}


@dataclass(frozen=True)
class Chunker:
    id: str
    version: int
    params: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "version": self.version, "params": self.params}


@dataclass(frozen=True)
class Layer:
    """One chunker and the rule that decides which documents it cuts. The
    last rule is "default" (06 §6.3)."""
    chunker: Chunker
    rule: str = "default"

    def to_dict(self) -> dict[str, Any]:
        return {"chunker": self.chunker.to_dict(), "rule": self.rule}


@dataclass(frozen=True)
class Encoder:
    image: str
    weights: str
    label: str = ""          # readable, without guarantee

    def to_dict(self) -> dict[str, Any]:
        return {"image": self.image, "weights": self.weights, "label": self.label}


@dataclass(frozen=True)
class Query:
    """An asymmetric space's query side; both null in a symmetric space."""
    encoder: Encoder | None = None
    instruction: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"encoder": None if self.encoder is None
                else {"image": self.encoder.image, "weights": self.encoder.weights},
                "instruction": self.instruction}


@dataclass(frozen=True)
class Calibration:
    set: str                 # the calibration set's id
    vectors: str             # the reference vectors' hash (§9.12)
    n: int = 16
    min_pass: int = 12

    def to_dict(self) -> dict[str, Any]:
        return {"set": self.set, "vectors": self.vectors,
                "quorum": {"n": self.n, "min_pass": self.min_pass}}


@dataclass(frozen=True)
class Space:
    encoder: Encoder
    dim: int
    dtype: str               # "f16" | "f32"
    ctx: int
    pooling: str             # "cls" | "mean" | "last"
    normalise: bool
    query: Query
    tolerance: str           # decimal string
    calibration: Calibration
    envelope: str            # hash of the envelope tensor (§9.12)
    epoch: str = "0"         # "0" for a root; the mother's for a daughter or
                             # a sister; salted for a corrective sister
    evaluation: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"encoder": self.encoder.to_dict(), "dim": self.dim,
                "dtype": self.dtype, "ctx": self.ctx, "pooling": self.pooling,
                "normalise": self.normalise, "query": self.query.to_dict(),
                "tolerance": self.tolerance,
                "calibration": self.calibration.to_dict(),
                "envelope": self.envelope, "epoch": self.epoch,
                "evaluation": self.evaluation}


@dataclass(frozen=True)
class Work:
    """The work parameter (02 §2.4): tokens of the encoder's tokenizer per
    `per`. `per` defaults to the update cadence and is always written out."""
    tokens: int
    per: str                 # ISO 8601 duration, "P1M"

    def to_dict(self) -> dict[str, Any]:
        return {"tokens": self.tokens, "per": self.per}


@dataclass(frozen=True)
class Validation:
    """The plan (03 §3.2). `sample` is a fraction of the snapshot's SHARDS
    per sampled check; the plan never sets how many chunks a check draws."""
    checks: tuple[str, ...]
    sample: dict[str, dict[str, str]]
    required: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"checks": list(self.checks), "sample": self.sample,
                "required": list(self.required)}


@dataclass(frozen=True)
class License:
    cls: str                 # "green" | "yellow" | "red"; `class` in the file
    terms_sha256: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"class": self.cls, "terms_sha256": self.terms_sha256}


@dataclass(frozen=True)
class Update:
    """`day` is read by the cadence: 1-28 as a day of the month when monthly,
    1-7 as an ISO weekday when weekly (§9.3)."""
    cadence: str = "monthly"     # "monthly" | "weekly"
    day: int = 1
    grace_days: int = 7

    def to_dict(self) -> dict[str, Any]:
        return {"cadence": self.cadence, "day": self.day,
                "grace_days": self.grace_days}


@dataclass(frozen=True)
class Certifier:
    key: str                 # "ed25519:…"
    anchor: str              # https URL where the key fingerprint is published
    is_origin: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"key": self.key, "anchor": self.anchor,
                "is_origin": self.is_origin}


@dataclass(frozen=True)
class Defect:
    """A reproducible statement of what was wrong with a replaced collection
    (03 §3.6). `witnesses` is shard names "<slice>/<n>" or uids."""
    check: str
    toolchain: str           # image digest under which the check fails
    witnesses: tuple[str, ...]
    expected: str
    observed: str
    rationale: str

    def to_dict(self) -> dict[str, Any]:
        return {"check": self.check, "toolchain": self.toolchain,
                "witnesses": list(self.witnesses), "expected": self.expected,
                "observed": self.observed, "rationale": self.rationale}


@dataclass(frozen=True)
class Replaces:
    codex: str
    defect: Defect

    def to_dict(self) -> dict[str, Any]:
        return {"codex": self.codex, "defect": self.defect.to_dict()}


@dataclass(frozen=True)
class Facet:
    """Structure only: the values live in the records (05 §5.7)."""
    field: str
    kind: str                # "flat" | "tree" | "range"
    separator: str | None = None      # non-null exactly when kind is "tree"
    authority: dict[str, Any] | None = None   # {name}, no version

    def to_dict(self) -> dict[str, Any]:
        return {"field": self.field, "kind": self.kind,
                "separator": self.separator, "authority": self.authority}


@dataclass(frozen=True)
class Credit:
    """A pre-embedded archive whose vectors this collection carries. Derived
    at creation from the platform's registry, never composed by hand
    (02 §2.7); hashed, so nothing here can be altered by a later build.

    The archive's SIGNING KEYS are deliberately absent: they live in the
    registry (06 §6.6). A licence and an attribution are permanent facts
    about whom the collection stands on; a key is lost and reissued
    (09 §9.2), and an archive that published nothing signed may begin.
    Hashing one would fork the identity whenever it moved.

    `coverage` is not merely compliance: 06 §6.3's `archive:<id>` layer
    rule reads it to decide which layer a document falls in.
    """
    archive: str
    producer: str
    license: str
    attribution: str
    coverage: Any            # "all" or {ids_per_shard, shards}

    def to_dict(self) -> dict[str, Any]:
        return {"archive": self.archive, "producer": self.producer,
                "license": self.license, "attribution": self.attribution,
                "coverage": self.coverage}


# --------------------------------------------------------------------------
# the Codex

@dataclass(frozen=True)
class Codex:
    descriptive: Descriptive
    plugin: str
    origin: Origin
    toolchain: Toolchain
    parser: Parser
    cleaning_version: int
    protocol_version: int
    layers: tuple[Layer, ...]
    slicing: dict[str, Any]
    space: Space
    work: Work
    modality: str            # "text" | "dna"
    erasure: str             # "append-only" | "retractable" | "erasable"
    validation: Validation
    license: License
    privacy: str             # "public" | "restricted" | "never-share"
    update: Update = field(default_factory=Update)
    publication: str = "open"            # "open" | "certified"
    certifier: Certifier | None = None
    embedding: str = "open"              # "open" | "endorsed" | "certifier-only"
    succession: Any = "frozen"           # "frozen" | {"open-after": duration}
    content: str | None = None           # "manifest-only" exactly when certified
    # lineage — at most one of continues / derived_from / replaces
    continues: str | None = None                 # daughter: the mother's id
    continues_through: str | None = None         # daughter: the mother's last snapshot
    origin_reachable_until: str | None = None
    derived_from: str | None = None              # sister: the mother's id
    space_from: str | None = None                # sister: whose space was copied
    replaces: Replaces | None = None             # corrective sister
    facets: tuple[Facet, ...] = ()
    credits: tuple[Credit, ...] = ()
    profile: dict[str, Any] | None = None

    # ---- the object ---------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """The Codex as it is written to `codex.json`, `descriptive` included.
        Key order does not matter: the canonical form sorts."""
        return {
            "kind": CODEX_KIND,
            "version": CODEX_VERSION,
            "descriptive": self.descriptive.to_dict(),
            "plugin": self.plugin,
            "origin": self.origin.to_dict(),
            "toolchain": self.toolchain.to_dict(),
            "parser": self.parser.to_dict(),
            "cleaning_version": self.cleaning_version,
            "protocol_version": self.protocol_version,
            "layers": [l.to_dict() for l in self.layers],
            "slicing": self.slicing,
            "space": self.space.to_dict(),
            "work": self.work.to_dict(),
            "modality": self.modality,
            "erasure": self.erasure,
            "validation": self.validation.to_dict(),
            "license": self.license.to_dict(),
            "privacy": self.privacy,
            "update": self.update.to_dict(),
            "publication": self.publication,
            "certifier": None if self.certifier is None else self.certifier.to_dict(),
            "embedding": self.embedding,
            "succession": self.succession,
            "content": self.content,
            "continues": self.continues,
            "continues_through": self.continues_through,
            "origin_reachable_until": self.origin_reachable_until,
            "derived_from": self.derived_from,
            "space_from": self.space_from,
            "replaces": None if self.replaces is None else self.replaces.to_dict(),
            "facets": [f.to_dict() for f in self.facets],
            "credits": [c.to_dict() for c in self.credits],
            "profile": self.profile,
        }

    def identity_dict(self) -> dict[str, Any]:
        """The object with `descriptive` REMOVED — removed, not set to null:
        a null value is a different object (§9.3)."""
        d = self.to_dict()
        del d["descriptive"]
        return d

    def canonical(self) -> bytes:
        """The bytes of `codex.json`. There is no pretty form (§9.3)."""
        return canonical_json(self.to_dict())

    # ---- derived identities -------------------------------------------------

    @property
    def collection_id(self) -> str:
        """The identity. Two creators who write the same recipe under
        different titles write the same collection."""
        return digest(canonical_json(self.identity_dict()))

    @property
    def codex_hash(self) -> str:
        """The file. What a peer compares a fetched `codex.json` against, and
        what a digest advertises (§9.9). Per-peer, never an identity."""
        return digest(self.canonical())

    @property
    def space_id(self) -> str:
        """§9.3. The encoder function and what is fed to it, and nothing
        about the contract around it: not `tolerance`, not `calibration`,
        not `envelope`, not `work`."""
        return digest(canonical_json({
            "cleaning_version": self.cleaning_version,
            "encoder": {"image": self.space.encoder.image,
                        "weights": self.space.encoder.weights},
            "layers": [l.to_dict() for l in self.layers],
            "space": {"ctx": self.space.ctx, "dim": self.space.dim,
                      "dtype": self.space.dtype, "epoch": self.space.epoch,
                      "normalise": self.space.normalise,
                      "pooling": self.space.pooling,
                      "query": self.space.query.to_dict()},
        }))

    @property
    def space_segment(self) -> str:
        """The `<space>` segment of a path or a member name: the first 16
        hexadecimal digits of `space_id` (§9.3)."""
        return self.space_id.split(":", 1)[1][:16]

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
    encoder, layers and cleaning equal her mother's (02 §2.7). Her own
    sisters inherit this value; they do not mint a new one."""
    return digest(canonical_json({"replaces": replaced_codex_id}))


def default_per(cadence: str) -> str:
    """`work.per` defaults to the update cadence when the recipe omits it
    (§9.3), and is always written out."""
    return {"monthly": "P1M", "weekly": "P1W"}[cadence]


# --------------------------------------------------------------------------
# validation

def validate(codex: Codex, mother: Codex | None = None) -> None:
    """Refuse a Codex that breaks a rule. `mother` is the Codex named by
    `continues` / `derived_from` / `replaces.codex`, when the caller has it;
    the rules that need it are skipped otherwise and re-checked on adoption.
    """
    if not codex.plugin:
        raise CodexError("plugin is required")
    if not codex.descriptive.title.strip():
        raise CodexError("descriptive.title may not be empty")
    if "\n" in codex.descriptive.title:
        raise CodexError("descriptive.title is one line")
    if not codex.descriptive.summary.strip():
        raise CodexError("descriptive.summary may not be empty")

    if not _DIGEST_RE.match(codex.toolchain.platform_image):
        raise CodexError("toolchain.platform_image must be a sha256: digest, "
                         f"got {codex.toolchain.platform_image!r}")
    mpi = codex.toolchain.mother_platform_image
    if mpi is not None and not _DIGEST_RE.match(mpi):
        raise CodexError("toolchain.mother_platform_image must be a sha256: digest or null")
    if mpi is not None and codex.continues is None:
        raise CodexError("toolchain.mother_platform_image is null except on a daughter")

    if codex.origin.unit not in ("file", "container-member", "index-page"):
        raise CodexError("origin.unit must be file|container-member|index-page, "
                         f"got {codex.origin.unit!r}")
    if codex.origin.cutoff_rule not in ("dated-dumps", "update-files", "record-date"):
        raise CodexError("origin.cutoff_rule must be dated-dumps|update-files|record-date, "
                         f"got {codex.origin.cutoff_rule!r}")
    if not codex.origin.form.strip():
        raise CodexError("origin.form records which origin form was taken and why "
                         "(06 §6.4); it is not optional")

    _validate_layers(codex)
    _validate_space(codex)

    if codex.modality not in ("text", "dna"):
        raise CodexError(f"modality must be text|dna, got {codex.modality!r}")
    if codex.erasure not in ("append-only", "retractable", "erasable"):
        raise CodexError("erasure must be append-only|retractable|erasable, "
                         f"got {codex.erasure!r}")
    if codex.privacy not in ("public", "restricted", "never-share"):
        raise CodexError(f"privacy must be public|restricted|never-share, "
                         f"got {codex.privacy!r}")
    if codex.erasure == "erasable" and codex.privacy != "never-share":
        raise CodexError('erasure "erasable" requires privacy "never-share" (04 §4.4)')

    _validate_license(codex)
    _validate_update(codex)
    _validate_work(codex)
    _validate_validation(codex)
    _validate_publication(codex)
    _validate_facets(codex)
    _validate_credits(codex)
    _validate_lineage(codex)

    if mother is not None:
        _validate_against_mother(codex, mother)


def _validate_layers(codex: Codex) -> None:
    if not codex.layers:
        raise CodexError("at least one layer is required")
    if codex.layers[-1].rule != "default":
        raise CodexError('the last layer\'s rule is "default" (06 §6.3): every '
                         "document falls in exactly one layer")
    for i, l in enumerate(codex.layers[:-1]):
        if l.rule == "default":
            raise CodexError(f"layers[{i}]: only the last rule is \"default\"")
    seen = [l.chunker.id for l in codex.layers]
    if len(set(seen)) != len(seen):
        raise CodexError("one chunker per layer: two layers name the same chunker")


def _validate_space(codex: Codex) -> None:
    sp = codex.space
    for name, val in (("encoder.image", sp.encoder.image),
                      ("encoder.weights", sp.encoder.weights),
                      ("calibration.set", sp.calibration.set),
                      ("calibration.vectors", sp.calibration.vectors),
                      ("envelope", sp.envelope)):
        if not _DIGEST_RE.match(val):
            raise CodexError(f"space.{name} must be a sha256: digest, got {val!r}")
    if sp.dtype not in ("f16", "f32"):
        raise CodexError(f"space.dtype must be f16|f32, got {sp.dtype!r}")
    if sp.pooling not in ("cls", "mean", "last"):
        raise CodexError(f"space.pooling must be cls|mean|last, got {sp.pooling!r}")
    if sp.dim <= 0 or sp.ctx <= 0:
        raise CodexError("space.dim and space.ctx are positive")
    if not _DECIMAL_RE.match(sp.tolerance):
        raise CodexError(f"space.tolerance must be a decimal string, got {sp.tolerance!r}")
    if not 0 < sp.calibration.min_pass <= sp.calibration.n:
        raise CodexError("space.calibration.quorum: 0 < min_pass <= n")
    if sp.query.encoder is not None:
        for name, val in (("image", sp.query.encoder.image),
                          ("weights", sp.query.encoder.weights)):
            if not _DIGEST_RE.match(val):
                raise CodexError(f"space.query.encoder.{name} must be a sha256: digest")


def _validate_license(codex: Codex) -> None:
    if codex.license.cls not in ("green", "yellow", "red"):
        raise CodexError(f"license.class must be green|yellow|red, "
                         f"got {codex.license.cls!r}")
    terms = codex.license.terms_sha256
    if codex.license.cls == "green":
        if terms is not None:
            raise CodexError("license.terms_sha256 is null on green and only on green")
    else:
        if terms is None or not _DIGEST_RE.match(terms):
            raise CodexError("a yellow or red collection names its terms by hash")


def _validate_update(codex: Codex) -> None:
    up = codex.update
    if up.cadence not in ("monthly", "weekly"):
        raise CodexError(f"update.cadence must be monthly|weekly, got {up.cadence!r}")
    limit = 28 if up.cadence == "monthly" else 7
    if not 1 <= up.day <= limit:
        raise CodexError(f"update.day is 1-{limit} on a {up.cadence} cadence, "
                         f"got {up.day}")
    if up.grace_days < 0:
        raise CodexError("update.grace_days must be >= 0")


def _validate_work(codex: Codex) -> None:
    if codex.work.tokens <= 0:
        raise CodexError("work.tokens is positive: a collection that promises "
                         "no work promises nothing")
    if not _DURATION_RE.match(codex.work.per):
        raise CodexError(f"work.per must be an ISO 8601 duration, "
                         f"got {codex.work.per!r}")


def _validate_validation(codex: Codex) -> None:
    v = codex.validation
    if not v.checks:
        raise CodexError("validation.checks names at least one check")
    for c in v.checks:
        if c not in PLAN_CHECKS:
            raise CodexError(f"validation.checks: {c!r} is not a plan check "
                             f"(03 §3.2); the results a peer produces from what "
                             f"it finds are not planned")
    if len(set(v.checks)) != len(v.checks):
        raise CodexError("validation.checks lists each check once")
    for name, entry in v.sample.items():
        if name not in SAMPLED_CHECKS:
            raise CodexError(f"validation.sample: {name!r} is not a sampled check")
        if set(entry) != {"shards"}:
            raise CodexError(f"validation.sample.{name} carries a shard fraction "
                             f"and nothing else: the plan never sets how many "
                             f"chunks a check draws (09 §9.3)")
        if not _DECIMAL_RE.match(entry["shards"]):
            raise CodexError(f"validation.sample.{name}.shards must be a decimal string")
    for name in v.checks:
        if name in SAMPLED_CHECKS and name not in v.sample:
            raise CodexError(f"validation.sample has no size for planned check {name!r}")
    for c in v.required:
        if c not in v.checks:
            raise CodexError(f"validation.required: {c!r} is not in the plan")
    if codex.publication != "certified" and v.required:
        raise CodexError("validation.required is [] on an open collection")


def _validate_publication(codex: Codex) -> None:
    if codex.publication not in ("open", "certified"):
        raise CodexError(f"publication must be open|certified, got {codex.publication!r}")
    if codex.embedding not in ("open", "endorsed", "certifier-only"):
        raise CodexError("embedding must be open|endorsed|certifier-only, "
                         f"got {codex.embedding!r}")
    succ = codex.succession
    if not (succ == "frozen" or (isinstance(succ, dict) and set(succ) == {"open-after"}
                                 and _DURATION_RE.match(str(succ["open-after"])))):
        raise CodexError('succession must be "frozen" or {"open-after": <duration>}')

    if codex.publication == "certified":
        if codex.certifier is None:
            raise CodexError("a certified collection names its certifier")
        if not _KEY_RE.match(codex.certifier.key):
            raise CodexError("certifier.key must be ed25519: and 64 hex characters")
        if not _ANCHOR_RE.match(codex.certifier.anchor):
            raise CodexError("certifier.anchor must be an https URL")
        if codex.content != "manifest-only":
            raise CodexError('a certified collection\'s content is "manifest-only"')
    else:
        if codex.certifier is not None:
            raise CodexError("only a certified collection names a certifier")
        if codex.embedding != "open":
            raise CodexError("an open collection's embedding is open")
        if codex.succession != "frozen":
            raise CodexError("an open collection's succession is frozen")
        if codex.content is not None:
            raise CodexError('content is "manifest-only" exactly when certified')


def _validate_facets(codex: Codex) -> None:
    fields = [f.field for f in codex.facets]
    if fields != sorted(fields):
        raise CodexError("facets are sorted by field")
    if len(set(fields)) != len(fields):
        raise CodexError("facets: one entry per field")
    for f in codex.facets:
        if f.kind not in ("flat", "tree", "range"):
            raise CodexError(f"facets[{f.field}].kind must be flat|tree|range, "
                             f"got {f.kind!r}")
        if (f.separator is not None) != (f.kind == "tree"):
            raise CodexError(f"facets[{f.field}]: separator is non-null exactly "
                             f"when kind is \"tree\"")
        if f.authority is not None and set(f.authority) != {"name"}:
            raise CodexError(f"facets[{f.field}].authority is {{name}} and carries "
                             f"no version: a vocabulary that gains a term is the "
                             f"same vocabulary (02 §2.1)")


def _validate_credits(codex: Codex) -> None:
    archives = [c.archive for c in codex.credits]
    if archives != sorted(archives):
        raise CodexError("credits are sorted by archive")
    if len(set(archives)) != len(archives):
        raise CodexError("credits: one entry per archive")
    for c in codex.credits:
        if not c.archive:
            raise CodexError("credits[].archive names the archive; it is what "
                             "03 §3.5's refusal resolves a producer key to")
        if not c.license or not c.attribution:
            raise CodexError(f"credits[{c.archive}]: an archive is credited under "
                             f"a licence and with the attribution that licence "
                             f"requires; both are hashed so neither can be "
                             f"altered by a later build (02 §2.7)")
        if c.coverage != "all" and set(c.coverage or {}) != {"ids_per_shard", "shards"}:
            raise CodexError(f"credits[{c.archive}].coverage is \"all\" or "
                             f"{{ids_per_shard, shards}}")


def _validate_lineage(codex: Codex) -> None:
    named = [r for r in (codex.continues, codex.derived_from, codex.replaces)
             if r is not None]
    if len(named) > 1:
        raise CodexError("a Codex names at most one of continues, derived_from, "
                         "replaces")

    if codex.continues is not None:
        if codex.continues_through is None:
            raise CodexError("a daughter names her mother's last snapshot "
                             "(continues_through) — the boundary is named from "
                             "the side that already exists (02 §2.7)")
    elif codex.continues_through is not None or codex.origin_reachable_until is not None:
        raise CodexError("continues_through and origin_reachable_until belong "
                         "to a daughter")

    if codex.space_from is not None and codex.derived_from is None:
        raise CodexError("space_from belongs to a sister")

    if codex.replaces is not None:
        d = codex.replaces.defect
        if not d.witnesses:
            raise CodexError("replaces.defect.witnesses names at least one shard "
                             "or record: until a peer has reproduced the defect, "
                             "the sister has no right to mark anything superseded")
        if not _DIGEST_RE.match(d.toolchain):
            raise CodexError("replaces.defect.toolchain must be an image digest")
        if d.check not in PLAN_CHECKS:
            raise CodexError(f"replaces.defect.check must be an existing check, "
                             f"got {d.check!r}")
        if codex.space.epoch != corrective_epoch(codex.replaces.codex):
            raise CodexError("a corrective sister's space.epoch is salted with the "
                             "replaced Codex id (corrective_epoch)")
    elif codex.relation == "root" and codex.space.epoch != "0":
        raise CodexError('a root\'s space.epoch is "0"')


def _validate_against_mother(codex: Codex, mother: Codex) -> None:
    mid = mother.collection_id
    if codex.relation == "daughter":
        if codex.continues != mid:
            raise CodexError("continues does not name the given mother")
        if codex.space_id != mother.space_id:
            raise CodexError("a daughter keeps her mother's space — same encoder, "
                             "layers, cleaning and epoch; this is a sister in disguise")
        if codex.plugin != mother.plugin:
            raise CodexError("a daughter continues the same plugin")
        if codex.publication != mother.publication:
            raise CodexError("a daughter keeps her mother's publication mode")
        if codex.toolchain.mother_platform_image != mother.toolchain.platform_image:
            raise CodexError("a daughter's toolchain.mother_platform_image is her "
                             "mother's platform_image")
    elif codex.relation == "sister":
        if codex.derived_from != mid:
            raise CodexError("derived_from does not name the given mother")
        if codex.origin != mother.origin:
            raise CodexError("a sister inherits her mother's corpus: same origin")
        if codex.cleaning_version != mother.cleaning_version:
            raise CodexError("a sister keeps her mother's cleaning_version: "
                             "changing it would re-derive the corpus she is "
                             "defined to inherit (02 §2.7)")
        if codex.toolchain.platform_image != mother.toolchain.platform_image:
            raise CodexError("a sister shares her mother's platform image")
        if codex.parser != mother.parser:
            raise CodexError("a sister keeps her mother's parser")
        if codex.slicing != mother.slicing:
            raise CodexError("a sister keeps her mother's slicing: the shards are "
                             "the same shards")
        changed = _sister_changes(codex, mother)
        if not changed:
            raise CodexError("a sister changes something: same everything is the "
                             "same collection")
        if codex.space_id == mother.space_id and codex.credits != mother.credits:
            raise CodexError("a sister in her mother's space carries her mother's "
                             "credits (02 §2.7)")
        if codex.space.epoch != mother.space.epoch and codex.space_from is None:
            raise CodexError("a sister keeps her mother's space.epoch unless she "
                             "copied a space from another Codex")
    elif codex.relation == "corrective-sister":
        if codex.replaces.codex != mid:
            raise CodexError("replaces.codex does not name the given mother")
        if codex.origin != mother.origin:
            raise CodexError("a corrective sister re-derives the same origin")


def _sister_changes(codex: Codex, mother: Codex) -> list[str]:
    """What a sister may change, and nothing else: `space`, `layers`,
    `update` and `work` (02 §2.7). `work` because the rate is denominated in
    the declared encoder's tokenizer, so a sister on another encoder must
    measure her own."""
    allowed = {"space": (codex.space, mother.space),
               "layers": (codex.layers, mother.layers),
               "update": (codex.update, mother.update),
               "work": (codex.work, mother.work)}
    return [k for k, (a, b) in allowed.items() if a != b]


# --------------------------------------------------------------------------
# lineage

def lineage_id(codex: Codex, resolve: Callable[[str], Codex | None]) -> str:
    """The root Codex's id along `continues` (02 §2.7). `resolve` returns the
    Codex for an id the peer holds, or None; an unresolvable mother stops the
    walk at the last known link, which is honest — the peer cannot claim a
    root it has never seen."""
    seen: set[str] = set()
    cur = codex
    while cur.continues is not None:
        if cur.collection_id in seen:
            raise CodexError("lineage cycle")
        seen.add(cur.collection_id)
        mother = resolve(cur.continues)
        if mother is None:
            return cur.continues
        cur = mother
    return cur.collection_id


def walk_lineage(codex: Codex, resolve: Callable[[str], Codex | None]) -> Iterator[Codex]:
    yield codex
    cur = codex
    while cur.continues is not None:
        mother = resolve(cur.continues)
        if mother is None:
            return
        yield mother
        cur = mother


# --------------------------------------------------------------------------
# (de)serialisation

_TOP = ("kind", "version", "descriptive", "plugin", "origin", "toolchain",
        "parser", "cleaning_version", "protocol_version", "layers", "slicing",
        "space", "work", "modality", "erasure", "validation", "license",
        "privacy", "update", "publication", "certifier", "embedding",
        "succession", "content", "continues", "continues_through",
        "origin_reachable_until", "derived_from", "space_from", "replaces",
        "facets", "credits", "profile")


def from_dict(d: dict[str, Any]) -> Codex:
    if d.get("kind") != CODEX_KIND:
        raise CodexError(f"not a Codex: kind={d.get('kind')!r}")
    if d.get("version") != CODEX_VERSION:
        raise CodexError(f"unsupported Codex version {d.get('version')!r}; "
                         f"this build implements {CODEX_VERSION}")
    _only(d, _TOP, "codex")

    de = _only(d["descriptive"], ("title", "summary"), "descriptive")
    og = _only(d["origin"], ("spec", "form", "unit", "clock", "cutoff_rule",
                             "jurisdiction"), "origin")
    tc = _only(d["toolchain"], ("platform_image", "mother_platform_image"), "toolchain")
    pa = _only(d["parser"], ("id", "version"), "parser")
    sp = _only(d["space"], ("encoder", "dim", "dtype", "ctx", "pooling",
                            "normalise", "query", "tolerance", "calibration",
                            "envelope", "epoch", "evaluation"), "space")
    wk = _only(d["work"], ("tokens", "per"), "work")
    va = _only(d["validation"], ("checks", "sample", "required"), "validation")
    li = _only(d["license"], ("class", "terms_sha256"), "license")
    up = _only(d["update"], ("cadence", "day", "grace_days"), "update")

    return Codex(
        descriptive=Descriptive(de["title"], de["summary"]),
        plugin=d["plugin"],
        origin=Origin(og["spec"], og["form"], og["unit"], og["clock"],
                      og["cutoff_rule"], og["jurisdiction"]),
        toolchain=Toolchain(tc["platform_image"], tc["mother_platform_image"]),
        parser=Parser(pa["id"], pa["version"]),
        cleaning_version=d["cleaning_version"],
        protocol_version=d["protocol_version"],
        layers=tuple(_layer(l, i) for i, l in enumerate(d["layers"])),
        slicing=d["slicing"],
        space=_space(sp),
        work=Work(wk["tokens"], wk["per"]),
        modality=d["modality"],
        erasure=d["erasure"],
        validation=Validation(tuple(va["checks"]), va["sample"],
                              tuple(va["required"])),
        license=License(li["class"], li["terms_sha256"]),
        privacy=d["privacy"],
        update=Update(up["cadence"], up["day"], up["grace_days"]),
        publication=d["publication"],
        certifier=_certifier(d["certifier"]),
        embedding=d["embedding"],
        succession=d["succession"],
        content=d["content"],
        continues=d["continues"],
        continues_through=d["continues_through"],
        origin_reachable_until=d["origin_reachable_until"],
        derived_from=d["derived_from"],
        space_from=d["space_from"],
        replaces=_replaces(d["replaces"]),
        facets=tuple(_facet(f, i) for i, f in enumerate(d["facets"])),
        credits=tuple(_credit(c, i) for i, c in enumerate(d["credits"])),
        profile=d["profile"],
    )


def _layer(l: dict[str, Any], i: int) -> Layer:
    l = _only(l, ("chunker", "rule"), f"layers[{i}]")
    ch = _only(l["chunker"], ("id", "version", "params"), f"layers[{i}].chunker")
    return Layer(Chunker(ch["id"], ch["version"], ch["params"]), l["rule"])


def _space(sp: dict[str, Any]) -> Space:
    en = _only(sp["encoder"], ("image", "weights", "label"), "space.encoder")
    qu = _only(sp["query"], ("encoder", "instruction"), "space.query")
    qe = qu["encoder"]
    if qe is not None:
        qe = _only(qe, ("image", "weights"), "space.query.encoder")
    ca = _only(sp["calibration"], ("set", "vectors", "quorum"), "space.calibration")
    qm = _only(ca["quorum"], ("n", "min_pass"), "space.calibration.quorum")
    return Space(
        encoder=Encoder(en["image"], en["weights"], en["label"]),
        dim=sp["dim"], dtype=sp["dtype"], ctx=sp["ctx"], pooling=sp["pooling"],
        normalise=sp["normalise"],
        query=Query(None if qe is None else Encoder(qe["image"], qe["weights"], ""),
                    qu["instruction"]),
        tolerance=sp["tolerance"],
        calibration=Calibration(ca["set"], ca["vectors"], qm["n"], qm["min_pass"]),
        envelope=sp["envelope"], epoch=sp["epoch"], evaluation=sp["evaluation"],
    )


def _certifier(c: dict[str, Any] | None) -> Certifier | None:
    if c is None:
        return None
    c = _only(c, ("key", "anchor", "is_origin"), "certifier")
    return Certifier(c["key"], c["anchor"], c["is_origin"])


def _replaces(r: dict[str, Any] | None) -> Replaces | None:
    if r is None:
        return None
    r = _only(r, ("codex", "defect"), "replaces")
    d = _only(r["defect"], ("check", "toolchain", "witnesses", "expected",
                            "observed", "rationale"), "replaces.defect")
    return Replaces(r["codex"], Defect(d["check"], d["toolchain"],
                                       tuple(d["witnesses"]), d["expected"],
                                       d["observed"], d["rationale"]))


def _facet(f: dict[str, Any], i: int) -> Facet:
    f = _only(f, ("field", "kind", "separator", "authority"), f"facets[{i}]")
    return Facet(f["field"], f["kind"], f["separator"], f["authority"])


def _credit(c: dict[str, Any], i: int) -> Credit:
    c = _only(c, ("archive", "producer", "license", "attribution",
                  "coverage"), f"credits[{i}]")
    return Credit(c["archive"], c["producer"], c["license"],
                  c["attribution"], c["coverage"])


def loads(data: bytes | str) -> Codex:
    """Read a `codex.json`. Its bytes are the canonical JSON of the object
    and nothing else (§9.3), so bytes that are not canonical are refused —
    a peer that re-serialised would otherwise change `codex_hash` and be
    dropped later, further from the cause."""
    raw = data.encode("utf-8") if isinstance(data, str) else data
    codex = from_dict(json.loads(raw))
    validate(codex)
    if codex.canonical() != raw:
        raise CodexError("codex.json is not canonical JSON; its bytes are the "
                         "canonical form of the object and nothing else (09 §9.3)")
    return codex


def dumps(codex: Codex) -> bytes:
    """The canonical bytes. This is what is stored, shared and hashed; there
    is no pretty form on disk."""
    validate(codex)
    return codex.canonical()


# Public names for the block readers the CLI needs; the leading-underscore
# forms stay private to `from_dict`, which is the only place a whole Codex
# is read.
space_from_dict = _space
layer_from_dict = _layer


# --------------------------------------------------------------------------
# the seeded sample (09 §9.1)

def sample(seed: bytes, n: int, universe: Iterable[str]) -> list[str]:
    """09 §9.1's ONE sampling function, used by every sampled check.

    "sort the universe of ids by their UTF-8 bytes; score each id by
    HMAC-SHA256 with key `seed` over the id's UTF-8 bytes, reading the first
    8 bytes of the tag as a big-endian unsigned integer; take the `n` ids
    with the lowest scores, ties by the id's bytes."

    The seed is published in the attestation (§9.8), so a sample is
    reproducible by anyone once published and predictable by no one before.
    That is the whole design: a verifier cannot be steered toward the shards
    a producer prepared, and cannot be accused of choosing them either.
    """
    import hmac
    ids = sorted(universe, key=lambda s: s.encode("utf-8"))
    if len(seed) != 32:
        raise CodexError(f"a sample seed is 32 bytes, got {len(seed)}")
    if n < 0:
        raise CodexError("a sample size is not negative")

    def score(i: str) -> tuple[int, bytes]:
        tag = hmac.new(seed, i.encode("utf-8"), hashlib.sha256).digest()
        return int.from_bytes(tag[:8], "big"), i.encode("utf-8")

    return sorted(ids, key=score)[:n]


def sample_size(fraction: str, universe_size: int) -> int:
    """"A fraction `f` of a universe of size `N` is `n = max(1, ⌈f × N⌉)`"
    (09 §9.1). `fraction` is a decimal STRING, because the plan carries it
    in canonical JSON where floats are refused (§9.1)."""
    from decimal import Decimal
    if not _DECIMAL_RE.match(fraction):
        raise CodexError(f"a sample fraction is a decimal string, got {fraction!r}")
    f = Decimal(fraction)
    if not 0 < f <= 1:
        raise CodexError(f"a sample fraction is in (0, 1], got {fraction!r}")
    from math import ceil
    return max(1, ceil(f * universe_size))
