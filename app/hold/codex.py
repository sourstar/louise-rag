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
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Iterable, Iterator

CODEX_KIND = "louise-codex"

#: What a NEW Codex is written at. 09 §9.3: "`version` tracks what wrote
#: the Codex, `record_line` tracks what is in the shards, and a figureless
#: collection created under a later format writes `version` 3 with
#: `record_line` 1" — 4 since the options amendment (OPTIONS_VERSION), and 5
#: since the input-forms amendment (INPUT_VERSION).
CODEX_VERSION = 5

#: What this build can READ. "A Codex already written stays at its version
#: and cannot be moved, because `version` is inside the canonical bytes and
#: so inside `collection_id` — changing it does not upgrade a collection, it
#: names a different one" (§9.3). So 1 and 2 are read forever and never
#: rewritten.
CODEX_VERSIONS = (1, 2, 3, 4, 5)

#: THE ENGINE AMENDMENT (0d85f7f; 02 §2.4, 09 §9.3). From this version a
#: space's encoder is `{weights, engine, label}`: the ENGINE the reference
#: vectors were computed with, written out whole so anyone can run it again,
#: inside `collection_id` and OUTSIDE `space_id` — a space is the model and
#: its settings, and whether another engine computes the same function is
#: measured by the gate (03 §3.3), never declared. Below it the encoder is
#: `{image, weights, label}`, the engine's image digest alone, and that
#: image IS inside `space_id`. A version-2 Codex keeps that for ever.
ENGINE_VERSION = 3

#: THE OPTIONS AMENDMENT (operator, 2026-10-03; 09 §9.3, migration step 15a).
#: From this version an engine MAY carry `options`: what a model runner sent
#: the engine when it loaded the weights over its own protocol (a LocalAI
#: backend's `LoadModel`), which no argument vector shows. ABSENT, never
#: null, for an engine started by `args` alone — so such an engine keeps the
#: bytes and the `engine_id` it had at version 3, and a version-3 `space` is
#: a valid version-4 one. Below this version `options` is an unknown key.
OPTIONS_VERSION = 4

#: THE INPUT-FORMS AMENDMENT (operator, 2026-10-08; 02 §2.4, 09 §9.3,
#: docs/notes/input-forms.md). From this version every layer carries
#: `input`: the FORM its chunks reach the encoder in, the model's documented
#: input format — `{"form": "text"}` or `{"form": "pair", "split":
#: "first-section"}`. `layers` is hashed whole into `space_id`, so the form
#: is inside it. Below this version `input` is an unknown key and a layer
#: reads as text — a version-4 Codex keeps its bytes and both its ids.
INPUT_VERSION = 5
#: The two forms (09 §9.3), as written into a layer.
INPUT_TEXT: dict[str, str] = {"form": "text"}
INPUT_PAIR: dict[str, str] = {"form": "pair", "split": "first-section"}

#: The engine's fields (§9.3). `options` is the one that may be absent.
ENGINE_FIELDS = ("name", "build", "variant", "image", "args", "env", "device")

#: The placeholders an engine's `options` carries for the host's paths
#: (09 §9.3): the weights file, and the runner's models directory.
OPTION_PLACEHOLDERS = ("${WEIGHTS}", "${MODELS}")

#: An absolute path where an option value can start one: at its start, or
#: after a separator (`name:/path`, `--flag=/path`, a list inside a string).
_ABSOLUTE_PATH_RE = re.compile(r"""(?:^|[\s:=,;'"(\[])/""")

#: 09 §9.5's two record shapes: 1 without a `figures` key, 2 with it. Only
#: 1 can be CREATED here — `record_line` 2 needs the figures containers,
#: the member kind and the plugin side of the amendment, none of which is
#: built (docs/notes/figures-amendment-plan.md). Refused at create with
#: that reason rather than accepted and quietly written without plates.
RECORD_LINES = (1, 2)
#: The DEFAULT, and it stays 1: "a figureless collection created under a
#: later format writes `version` 2 with `record_line` 1" (§9.3). A
#: collection gets plates by asking for them, never by being created after
#: the amendment landed.
RECORD_LINE_BUILT = 1
#: What the code can WRITE, which is the separate question. 2 since
#: 2026-09-11: `base.Figure` and `CanonicalDoc.figures`,
#: `drag.MEMBER_KINDS`'s `figures`, `app/hold/figures.py`'s containers, the
#: `figures` key in `pack/build._record_of`, and a plugin that yields
#: plates (`gutenberg-epub`).
RECORD_LINES_WRITABLE = (1, 2)

_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_KEY_RE = re.compile(r"^ed25519:[0-9a-f]{64}$")
_DECIMAL_RE = re.compile(r"^-?(0|[1-9][0-9]*)(\.[0-9]+)?$")
_ANCHOR_RE = re.compile(r"^https://[^\s/]+/.+$")
_DURATION_RE = re.compile(r"^P(?!$)(\d+Y)?(\d+M)?(\d+W)?(\d+D)?"
                          r"(T(?!$)(\d+H)?(\d+M)?(\d+S)?)?$")

# The checks a plan may name (03 §3.2). §9.8's table is wider: it adds the
# results a peer produces from what it finds rather than from a plan.
# `figures` was missing here until 2026-09-10, and its absence was a real
# refusal: §3.2 says "a `checks` entry that names no row of this table is
# refused at `hold collection create` and at `hold check` alike", so a
# collection with plates could not name the check that validates them.
# The figures amendment added the row to §3.2 and this list was not
# followed. Adding a name moves no existing `collection_id` — `validation`
# is hashed, but this list only decides what is ACCEPTED into it.
PLAN_CHECKS = ("roots", "records", "chunks", "records-vs-origin",
               "vectors-sample", "calibration", "centres", "labels",
               "figures", "license", "signature", "anchor", "origin-seal",
               "evaluation")

# The checks whose plan entry is a sample size (§9.3, the validation block).
SAMPLED_CHECKS = ("records", "chunks", "records-vs-origin", "vectors-sample",
                  # 09 §9.8's table says `sampled`, for the same reason as
                  # `records`: re-hashing 51 GB of plates on every check is
                  # not what a fraction of the shards is for.
                  "figures")


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
    last rule is "default" (06 §6.3).

    `input` (Codex version 5, INPUT_VERSION): the form the layer's chunks
    reach the encoder in. `None` means ABSENT from the object — a layer of a
    Codex below version 5, which reads as `{"form": "text"}` and hashes as
    it always did. `Codex.__post_init__` sets it from the version, so no
    construction has to spell it."""
    chunker: Chunker
    rule: str = "default"
    input: dict[str, str] | None = None

    @property
    def form(self) -> str:
        """`text` or `pair`: what `embeddings.embed_chunks` builds."""
        return (self.input or INPUT_TEXT)["form"]

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"chunker": self.chunker.to_dict(), "rule": self.rule}
        if self.input is not None:
            out["input"] = dict(self.input)
        return out


@dataclass(frozen=True)
class Engine:
    """09 §9.3, *the engine*: how the reference vectors were computed,
    written out whole so that anyone can compute them again (02 §2.4).

    `image`, `args` and `env` are what a peer runs to reproduce the
    reference; `name`, `build`, `variant` and `device` are readable, bound
    by `image` and by the reference vectors and by no check. `args` carries
    the weights as `${WEIGHTS}` and the port as `${PORT}` and nothing else
    that names the host; `env` is only what was set for the engine beyond
    the image's own, and never a credential — the Codex is public."""
    name: str
    build: str
    variant: str | None
    image: str
    args: tuple[str, ...]
    env: dict[str, str]
    device: str
    #: From Codex version 4 (OPTIONS_VERSION): the load options a model
    #: runner sent the engine over its own protocol. `None` means ABSENT
    #: from the object — never written as null — so an engine started by
    #: `args` alone hashes exactly as it did at version 3.
    options: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        out = {"name": self.name, "build": self.build, "variant": self.variant,
               "image": self.image, "args": list(self.args), "env": dict(self.env),
               "device": self.device}
        if self.options is not None:
            out["options"] = self.options
        return out

    @property
    def engine_id(self) -> str:
        """`H(canonical JSON of the engine object)`. It names an engine
        wherever a producer's engine is recorded: the bundle header (§9.7),
        attestations (§9.8), benchmarks (§9.9), calibration objects (§9.12)."""
        return digest(canonical_json(self.to_dict()))


@dataclass(frozen=True)
class Encoder:
    """A space's encoder, in one of two shapes (ENGINE_VERSION):
    `{weights, engine, label}` from Codex version 3, and `{image, weights,
    label}` below it. The shape is the object's own — an `engine` or an
    `image`, never both — and `validate` ties it to the Codex's version.

    `image` stays FIRST, where every positional construction in the code
    and the tests already puts it; a version-3 encoder passes `None` there."""
    image: str | None
    weights: str
    label: str = ""          # readable, without guarantee
    engine: Engine | None = None

    def to_dict(self) -> dict[str, Any]:
        if self.engine is not None:
            return {"weights": self.weights, "engine": self.engine.to_dict(),
                    "label": self.label}
        return {"image": self.image, "weights": self.weights, "label": self.label}


@dataclass(frozen=True)
class Query:
    """An asymmetric space's query side; both null in a symmetric space."""
    encoder: Encoder | None = None
    instruction: str | None = None

    def to_dict(self) -> dict[str, Any]:
        e = self.encoder
        if e is None:
            side = None
        elif e.engine is not None:
            side = {"weights": e.weights, "engine": e.engine.to_dict()}
        else:
            side = {"image": e.image, "weights": e.weights}
        return {"encoder": side, "instruction": self.instruction}


@dataclass(frozen=True)
class Calibration:
    set: str                 # the calibration set's id
    vectors: str             # the reference vectors' hash (§9.12)
    n: int = 16
    min_pass: int = 12
    #: From version 3: the hash of the QUERY encoder's reference vectors in
    #: an asymmetric space — what its query runs are validated against
    #: (03 §3.3) — and `null` in a symmetric one. Absent below version 3.
    query_vectors: str | None = None

    def to_dict(self, with_query_vectors: bool = False) -> dict[str, Any]:
        out = {"set": self.set, "vectors": self.vectors,
               "quorum": {"n": self.n, "min_pass": self.min_pass}}
        if with_query_vectors:
            out["query_vectors"] = self.query_vectors
        return out


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

    @property
    def has_engine(self) -> bool:
        """Is this the version-3 shape — an engine, not an image alone."""
        return self.encoder.engine is not None

    @property
    def has_options(self) -> bool:
        """Does either engine carry load options — which only a Codex of
        OPTIONS_VERSION or later can hold (09 §9.3)."""
        return any(e is not None and e.engine is not None and e.engine.options is not None
                   for e in (self.encoder, self.query.encoder))

    def to_dict(self) -> dict[str, Any]:
        return {"encoder": self.encoder.to_dict(), "dim": self.dim,
                "dtype": self.dtype, "ctx": self.ctx, "pooling": self.pooling,
                "normalise": self.normalise, "query": self.query.to_dict(),
                "tolerance": self.tolerance,
                "calibration": self.calibration.to_dict(self.has_engine),
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


#: §9.3's two admitted derivations. Both are pure functions of another
#: space's vectors, which is what lets two creators produce the same
#: numbers from the same source.
TRUNCATE, QUANTISE = "truncate", "quantise"
DERIVATION_KINDS = (TRUNCATE, QUANTISE)


@dataclass(frozen=True)
class Derivation:
    """§9.3's `derivation`: a ROUTE TO a space's vectors, never a property
    of them (02 §2.4). `from_` is a `space_id`; naming a `collection_id`
    there is refused, because a collection is not a function."""
    from_: str
    kind: str                         # "truncate" | "quantise"
    params: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        # `from` on the wire — it is a Python keyword, hence `from_` here
        return {"from": self.from_, "kind": self.kind, "params": self.params}


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


#: 09 §9.3's three kinds of debt, in the order they sort.
CORPUS, ARCHIVE, COMMUNITY = "corpus", "archive", "community"
CREDIT_KINDS = (ARCHIVE, COMMUNITY, CORPUS)          # sorted, as the list is


@dataclass(frozen=True)
class Credit:
    """Work this collection stands on: its text, its archives, its peers.

    One closed six-key shape for all three kinds, `kind` saying which keys
    mean anything (09 §9.3) — and, more importantly, by which rule the
    entry descends to a relative:

      corpus     whom the TEXT is from. Exactly one, on every collection
                 including green ones. Declared by the plugin (06 §6.1),
                 copied by `create`, never writable by a recipe. Descends
                 to every relative without exception: a new encoder does
                 not unwrite whose text it read.
      archive    a pre-embedded archive whose vectors this collection
                 carries. Expanded from the platform's registry by the
                 recipe's `archives`. Follows the SPACE — a sister in a
                 new space has her own, a corrective sister has none.
      community  the peers who embed, as ONE collective and never a
                 roster: a roster is unbounded and changing and belongs
                 in the coverage map (02 §2.5), where it cannot fix at
                 creation a membership nobody knows then. Derived from
                 `embedding`: present when "open" or "endorsed".

    Never composed by hand, and all of it hashed, so nothing here can be
    altered or dropped by a later build.

    The archive's SIGNING KEYS are deliberately absent: they live in the
    registry (06 §6.6). A licence and an attribution are permanent facts
    about whom the collection stands on; a key is lost and reissued
    (09 §9.2), and an archive that published nothing signed may begin.
    Hashing one would fork the identity whenever it moved.

    `coverage` is not merely compliance: 06 §6.3's `archive:<id>` layer
    rule reads it to decide which layer a document falls in — and reads
    ONLY the archive entries, the other two being `"all"` because a
    corpus provider and the community are owed for everything.
    """
    kind: str                # corpus | archive | community
    archive: str | None      # non-null on `archive` only
    producer: str
    license: str | None      # null on `community`; on `corpus` only where none
    attribution: str
    coverage: Any            # "all" or {ids_per_shard, shards}

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "archive": self.archive,
                "producer": self.producer,
                "license": self.license, "attribution": self.attribution,
                "coverage": self.coverage}


def credit_order(c: Credit) -> tuple[str, str, str]:
    """09 §9.3's sort: `(kind, archive, producer)`. Two creators building
    the same collection derive the same order and so the same
    `collection_id` — which is the whole reason `credits` is derived."""
    return (c.kind, c.archive or "", c.producer)


def archive_credits(codex) -> tuple[Credit, ...]:
    """The `kind: "archive"` entries — what 03 §3.5's key refusal and
    06 §6.3's layer rule read, and the only kind that follows the space."""
    return tuple(c for c in codex.credits if c.kind == ARCHIVE)


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
    #: §9.3: "how this space's vectors may be obtained WITHOUT an encoder,
    #: when it is a pure function of another space's:
    #: `{from: <space_id>, kind: "truncate" | "quantise", params: {…}}`".
    #: `from` is a `space_id` and NEVER a `collection_id`.
    #:
    #: Deliberately NOT in `space_id`, and 02 §2.4 calls that load-bearing:
    #: two creators applying the same function to the same source produce
    #: the same numbers, so if the derivation entered `space_id` they would
    #: land in different spaces and could not share a bundle — which is the
    #: whole point of allowing it. It IS in `collection_id`.
    derivation: Derivation | None = None         # a derived space's route
    replaces: Replaces | None = None             # corrective sister
    facets: tuple[Facet, ...] = ()
    credits: tuple[Credit, ...] = ()
    profile: dict[str, Any] | None = None
    #: 09 §9.3's `version` and `record_line`. Last in the field list and
    #: not first, where they read, because the canonical form sorts and
    #: putting them where they belong would renumber every positional
    #: construction in the codebase for no change of bytes.
    version: int = CODEX_VERSION
    #: `None` exactly when `version` is 1 — §9.3: "Version 1 is this
    #: object without `record_line`".
    record_line: int | None = None

    def __post_init__(self) -> None:
        """`record_line` follows `version`, so no construction has to spell
        both. 09 §9.3: version 1 is the object WITHOUT the key, and a
        figureless collection at a later format writes 2 with
        `record_line` 1. Normalised here rather than at each call site,
        because the two fields are one fact and a Codex that disagreed
        with itself about it would be a shape nobody else hashes."""
        if self.version == 1:
            if self.record_line is not None:
                raise CodexError(
                    "a version 1 Codex has no `record_line` (09 §9.3); "
                    "adding it does not upgrade a collection, it names a "
                    "different one")
        elif self.record_line is None:
            object.__setattr__(self, "record_line", RECORD_LINE_BUILT)
        elif self.record_line not in RECORD_LINES:
            raise CodexError(f"record_line is 1 or 2 (09 §9.3, §9.5), got "
                             f"{self.record_line!r}")
        if self.version not in CODEX_VERSIONS:
            raise CodexError(f"unsupported Codex version {self.version!r}; "
                             f"this build reads "
                             f"{', '.join(str(v) for v in CODEX_VERSIONS)}")
        object.__setattr__(self, "layers", _layers_at(self.layers, self.version))

    # ---- the object ---------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """The Codex as it is written to `codex.json`, `descriptive` included.
        Key order does not matter: the canonical form sorts."""
        out = {
            "kind": CODEX_KIND,
            "version": self.version,
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
            "derivation": None if self.derivation is None else self.derivation.to_dict(),
            "replaces": None if self.replaces is None else self.replaces.to_dict(),
            "facets": [f.to_dict() for f in self.facets],
            "credits": [c.to_dict() for c in self.credits],
            "profile": self.profile,
        }
        if self.version >= 2:
            # ABSENT at version 1, not null: §9.3 says "Version 1 is this
            # object without `record_line`", and 09 §9.18 refuses an
            # unknown key — so a version-1 Codex carrying it, even as
            # null, is a different object that no reader accepts.
            out["record_line"] = self.record_line
        return out

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
        not `envelope`, not `work`.

        FROM VERSION 3, NOT THE ENGINE EITHER (02 §2.4): the space is the
        weights and the settings. A newer build that runs faster, another
        engine, another device, a quantised variant each JOINS the space by
        passing the gate and never makes a new one — so two creators on
        different engines land in one space. The query side enters "as the
        Codex writes it, except that a non-null `query.encoder` is reduced
        to `{weights}`".

        BELOW VERSION 3 the engine's image digest is inside: those bytes are
        what version-2 collections were identified by, and a Codex already
        written cannot be moved."""
        sp = self.space
        if self.version >= ENGINE_VERSION:
            encoder: dict[str, Any] = {"weights": sp.encoder.weights}
            query = sp.query.to_dict()
            if sp.query.encoder is not None:
                query["encoder"] = {"weights": sp.query.encoder.weights}
        else:
            encoder = {"image": sp.encoder.image, "weights": sp.encoder.weights}
            query = sp.query.to_dict()
        return digest(canonical_json({
            "cleaning_version": self.cleaning_version,
            "encoder": encoder,
            "layers": [l.to_dict() for l in self.layers],
            "space": {"ctx": sp.ctx, "dim": sp.dim, "dtype": sp.dtype,
                      "epoch": sp.epoch, "normalise": sp.normalise,
                      "pooling": sp.pooling, "query": query},
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


def _layers_at(layers: Iterable[Layer], version: int) -> tuple[Layer, ...]:
    """`input` follows `version`, as `record_line` does: one fact.

    From INPUT_VERSION every layer carries it — `{"form": "text"}` where a
    construction named none. Below it the key is ABSENT: a text form is
    dropped (that is what the layer reads as), and any other form is
    refused, because a version-4 Codex cannot say it — writing it would be
    a shape nobody else hashes, and dropping it would put pair vectors under
    a text space's id."""
    out = []
    for i, l in enumerate(layers):
        if version >= INPUT_VERSION:
            if l.input is None:
                l = replace(l, input=dict(INPUT_TEXT))
        elif l.input is not None:
            if l.input != INPUT_TEXT:
                raise CodexError(
                    f"layers[{i}]: a version {version} Codex has no `input` (09 §9.3) "
                    f"and its layers are text; the form {l.input!r} needs version "
                    f"{INPUT_VERSION}")
            l = replace(l, input=None)
        out.append(l)
    return tuple(out)


def encoder_field(codex: "Codex", *, query: bool = False) -> tuple[str, str | None]:
    """How an artefact written FOR this collection names the Codex's own
    engine, beside the weights: `("encoder_engine", <engine_id>)` from Codex
    version 3, `("encoder_image", <image digest>)` below it.

    The engine amendment moved every artefact that named an image — the
    bundle header (09 §9.7), the attestation (§9.8), the digest's benchmarks
    (§9.9), both calibration objects (§9.12) — to the engine's id, each by
    one version. An artefact of a version-2 collection keeps the image and
    its old version: those bytes are what its peers already hold."""
    enc = codex.space.query.encoder if query else codex.space.encoder
    if enc is None:
        return ("encoder_engine" if codex.version >= ENGINE_VERSION
                else "encoder_image"), None
    if codex.version >= ENGINE_VERSION:
        return "encoder_engine", enc.engine.engine_id
    return "encoder_image", enc.image


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
    # THE RULE'S FORM, not just its position. A Codex carrying a rule this
    # platform does not recognise used to validate, hash, and then put
    # every document in the LAST layer — `layers._rule_matches` returns
    # False for anything unknown and `layer_index` falls through. The layer
    # list is inside `space_id`, so that silence produces vectors under an
    # id promising a different cut, which is invariant 1's failure mode and
    # undetectable from outside.
    from ..layers import LayerError, check_input, check_rule
    for i, l in enumerate(codex.layers):
        try:
            check_rule(l.rule, where=f"layers[{i}]")
            # the form, as strictly as the rule and for the same reason: it
            # is inside `space_id` (09 §9.3, amended 2026-10-08)
            if codex.version >= INPUT_VERSION:
                check_input(l.input, chunker=l.chunker.id, where=f"layers[{i}]")
        except LayerError as e:
            raise CodexError(str(e)) from None
    seen = [l.chunker.id for l in codex.layers]
    if len(set(seen)) != len(seen):
        raise CodexError("one chunker per layer: two layers name the same chunker")


def _validate_engine(engine: Engine, where: str) -> None:
    """09 §9.3, *the engine*. What binds is `image`, `args` and `env`; the
    rest is readable. `args` names the weights and the port by placeholder
    and nothing else of the host."""
    for name in ("name", "build", "device"):
        value = getattr(engine, name)
        if not isinstance(value, str) or not value:
            raise CodexError(f"{where}.{name} must be a non-empty string, got {value!r}")
    if engine.variant is not None and (not isinstance(engine.variant, str)
                                       or not engine.variant):
        raise CodexError(f"{where}.variant is a non-empty string or null")
    if not isinstance(engine.image, str) or not _DIGEST_RE.match(engine.image):
        raise CodexError(f"{where}.image must be the sha256: digest of the OCI image that "
                         f"was run — a tag is not an image — got {engine.image!r}")
    if not engine.args or not all(isinstance(a, str) and a for a in engine.args):
        raise CodexError(f"{where}.args must be the argument vector, program first: a "
                         f"non-empty list of non-empty strings")
    for placeholder in ("${WEIGHTS}", "${PORT}"):
        if sum(1 for a in engine.args if placeholder in a) > 1:
            raise CodexError(f"{where}.args carries {placeholder} more than once")
    for key, value in engine.env.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise CodexError(f"{where}.env maps names to strings, got {key!r}: {value!r}")
    if engine.options is not None:
        validate_options(engine.options, f"{where}.options")


def validate_options(options: Any, where: str = "options") -> None:
    """09 §9.3's `options`: the runner's record of the load call, with the
    host's paths as placeholders and every non-integer number as its decimal
    string. ABSENT when there are none — so an empty object, a third way of
    saying "none" that would hash differently from the absence, is refused.

    AN OPTION NAMING ANY OTHER ABSOLUTE PATH IS REFUSED: the weights digest
    covers `${WEIGHTS}` and nothing else, so a run that loads a second file —
    a projector, a LoRA, a draft model — would carry a file no digest names
    (09 §9.3: such a run "cannot be stated yet")."""
    if not isinstance(options, dict) or not options:
        raise CodexError(
            f"{where} is a non-empty object of the options the engine was loaded with, "
            f"and ABSENT for an engine started by `args` alone — never null, never "
            f"empty (09 §9.3)")

    def walk(value: Any, path: str) -> None:
        if value is None or isinstance(value, (bool, int)):
            return
        if isinstance(value, float):
            raise CodexError(f"{path}: a number that is not an integer is written as its "
                             f"decimal string (09 §9.1, §9.3), got {value!r}")
        if isinstance(value, str):
            if _ABSOLUTE_PATH_RE.search(value):
                raise CodexError(
                    f"{path} names an absolute path ({value!r}). The weights file is "
                    f"written {OPTION_PLACEHOLDERS[0]} and the runner's models directory "
                    f"{OPTION_PLACEHOLDERS[1]}; any other file is one no weights digest "
                    f"covers, so a run that loads it cannot be stated yet (09 §9.3)")
            return
        if isinstance(value, dict):
            for k, v in value.items():
                if not isinstance(k, str) or not k:
                    raise CodexError(f"{path}: option names are non-empty strings, "
                                     f"got {k!r}")
                walk(v, f"{path}.{k}")
            return
        if isinstance(value, (list, tuple)):
            for i, v in enumerate(value):
                walk(v, f"{path}[{i}]")
            return
        raise CodexError(f"{path}: not a JSON value: {value!r}")

    walk(options, where)


def _validate_encoder_shape(codex: Codex) -> None:
    """The shape follows the version, both ways (ENGINE_VERSION): an engine
    from version 3, an image alone below it. A version-2 Codex given an
    engine — or a version-3 one left with an image — would be a third shape
    that nobody else computes the same `collection_id` for."""
    sp = codex.space
    sides = [("space.encoder", sp.encoder)]
    if sp.query.encoder is not None:
        sides.append(("space.query.encoder", sp.query.encoder))
    for where, enc in sides:
        if codex.version >= ENGINE_VERSION:
            if enc.engine is None or enc.image is not None:
                raise CodexError(
                    f"{where}: a version {codex.version} Codex names the ENGINE the "
                    f"reference vectors were computed with — {{weights, engine, …}} — "
                    f"not an image alone (09 §9.3; 02 §2.4)")
            _validate_engine(enc.engine, f"{where}.engine")
            if enc.engine.options is not None and codex.version < OPTIONS_VERSION:
                raise CodexError(
                    f"{where}.engine.options: an engine's load options are a version "
                    f"{OPTIONS_VERSION} field, unknown to a version {codex.version} Codex "
                    f"(09 §9.3, §9.18). A Codex already written stays at its version: "
                    f"`version` is inside `collection_id`")
        else:
            if enc.engine is not None:
                raise CodexError(
                    f"{where}: a version {codex.version} Codex names its encoder's IMAGE; "
                    f"the engine is version {ENGINE_VERSION}'s. A Codex already written "
                    f"cannot be moved: `version` is inside `collection_id`")
            if not isinstance(enc.image, str) or not _DIGEST_RE.match(enc.image):
                raise CodexError(f"{where}.image must be a sha256: digest, got {enc.image!r}")
        if not _DIGEST_RE.match(enc.weights):
            raise CodexError(f"{where}.weights must be a sha256: digest, got "
                             f"{enc.weights!r}")
    qv = sp.calibration.query_vectors
    if codex.version >= ENGINE_VERSION and sp.query.encoder is not None:
        if not isinstance(qv, str) or not _DIGEST_RE.match(qv):
            raise CodexError(
                "space.calibration.query_vectors must be the sha256: hash of the query "
                "encoder's reference vectors: an asymmetric space's query runs are "
                "validated against them (03 §3.3; 09 §9.12)")
    elif qv is not None:
        raise CodexError(
            "space.calibration.query_vectors is null in a symmetric space, and absent "
            f"below Codex version {ENGINE_VERSION} (09 §9.3)")


def _validate_space(codex: Codex) -> None:
    sp = codex.space
    _validate_encoder_shape(codex)
    for name, val in (("calibration.set", sp.calibration.set),
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
    keys = [credit_order(c) for c in codex.credits]
    if keys != sorted(keys):
        raise CodexError("credits are sorted by (kind, archive, producer)")
    if len(set(keys)) != len(keys):
        raise CodexError("credits: one entry per (kind, archive, producer)")

    for c in codex.credits:
        if c.kind not in CREDIT_KINDS:
            raise CodexError(f"credits[].kind is one of "
                             f"{', '.join(CREDIT_KINDS)}, not {c.kind!r}")
        if not c.producer or not c.attribution:
            raise CodexError(
                f"credits[{c.kind}]: every credit names a producer and carries "
                f"the attribution line for them; both are hashed so neither "
                f"can be altered or dropped by a later build (02 §2.7)")

    # Exactly one corpus entry, always. `credits` is therefore never empty:
    # every collection's text came from somewhere, green ones included —
    # a provider that asks nothing is still the provider.
    corpus = [c for c in codex.credits if c.kind == CORPUS]
    if len(corpus) != 1:
        raise CodexError(
            f"credits carries exactly one kind=\"corpus\" entry, not "
            f"{len(corpus)}: whom the TEXT is from, declared by the plugin "
            f"(06 §6.1) and on every collection including green ones (02 §2.7)")

    # At most one community entry, and it is DERIVED from `embedding` rather
    # than declared — so a collection that accepts no contributed vectors
    # credits nobody for them, and one that accepts them cannot omit it.
    community = [c for c in codex.credits if c.kind == COMMUNITY]
    wanted = codex.embedding in ("open", "endorsed")
    if len(community) > 1:
        raise CodexError("credits carries at most one kind=\"community\" entry: "
                         "the peers who embed are ONE collective, never a "
                         "roster — a roster belongs in the coverage map (02 §2.5)")
    if bool(community) != wanted:
        raise CodexError(
            f"embedding is {codex.embedding!r}, so credits "
            f"{'carries' if wanted else 'carries no'} kind=\"community\" entry "
            f"(02 §2.7): it is present exactly when contributed vectors are "
            f"admissible, and is derived from `embedding`, never declared")

    for c in codex.credits:
        if c.kind == ARCHIVE:
            if not c.archive:
                raise CodexError(
                    "credits[archive].archive names the archive; it is what "
                    "03 §3.5's refusal resolves a producer key to")
            if not c.license:
                raise CodexError(
                    f"credits[{c.archive}]: an archive is credited under a "
                    f"licence (02 §2.7)")
        elif c.archive is not None:
            raise CodexError(f"credits[{c.kind}].archive is null: only an "
                             f"archive entry names an archive")
        if c.kind == COMMUNITY and c.license is not None:
            raise CodexError("credits[community].license is null: contributed "
                             "vectors carry the collection's own terms and "
                             "there is no second licence to record (02 §2.7)")
        if c.kind != ARCHIVE and c.coverage != "all":
            raise CodexError(
                f"credits[{c.kind}].coverage is \"all\": a corpus provider and "
                f"the community are owed for every document. Only an archive "
                f"entry declares a range, and only those are read for layers "
                f"(06 §6.3)")
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

    if codex.derivation is not None:
        if not _DIGEST_RE.match(codex.derivation.from_ or ""):
            raise CodexError("derivation.from is a space_id (09 §9.3), "
                             "sha256:<64 hex>")
        # "`from` being a `space_id` and never a `collection_id`" — the two
        # are the same shape, so no check can tell that a digest is some
        # OTHER collection's id. What IS checkable is self-reference: a
        # space that is a pure function of itself is not a derivation.
        #
        # Against `space_id` only, NOT `collection_id`: `derivation` is
        # inside the identity, so by the time this object exists its own
        # `collection_id` already accounts for the derivation and could
        # never equal the `from` it contains. `space_id` excludes
        # `derivation` (02 §2.4), which is exactly what makes it stable
        # enough to compare against.
        if codex.derivation.from_ == codex.space_id:
            raise CodexError("derivation.from names this collection's own "
                             "space_id; it is the OTHER space's space_id that "
                             "the vectors are a function of (09 §9.3)")

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
        # Only the ARCHIVE entries follow the space. Her corpus entry is her
        # mother's on every relation — she shares the corpus by definition —
        # and her community entry follows her own `embedding`, which is hers
        # to change: a sister may open a certifier-only mother to
        # contribution, or close an open one, and her credits say which.
        if (codex.space_id == mother.space_id
                and archive_credits(codex) != archive_credits(mother)):
            raise CodexError("a sister in her mother's space carries her mother's "
                             "archive credits (02 §2.7)")
        mine = [c for c in codex.credits if c.kind == CORPUS]
        hers = [c for c in mother.credits if c.kind == CORPUS]
        if mine != hers:
            raise CodexError("a sister keeps her mother's corpus credit: she "
                             "shares the corpus, and a new encoder does not "
                             "unwrite whose text it read (02 §2.7)")
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
        "parser", "cleaning_version", "protocol_version", "record_line",
        "layers", "slicing",
        "space", "work", "modality", "erasure", "validation", "license",
        "privacy", "update", "publication", "certifier", "embedding",
        "succession", "content", "continues", "continues_through",
        "origin_reachable_until", "derived_from", "space_from", "derivation",
        "replaces", "facets", "credits", "profile")

#: 09 §9.3: "Version 1 is this object without `record_line`."
_TOP_V1 = tuple(k for k in _TOP if k != "record_line")


def from_dict(d: dict[str, Any]) -> Codex:
    if d.get("kind") != CODEX_KIND:
        raise CodexError(f"not a Codex: kind={d.get('kind')!r}")
    version = d.get("version")
    if version not in CODEX_VERSIONS:
        raise CodexError(f"unsupported Codex version {version!r}; this build "
                         f"reads {', '.join(str(v) for v in CODEX_VERSIONS)}")
    # 09 §9.3: "Version 1 is this object without `record_line`". Absent at
    # 1 and present at 2, both enforced by the schema itself, because
    # `version` and `record_line` are inside `collection_id` — a Codex
    # that carried the key at version 1, or omitted it at 2, would be a
    # third shape nobody else computes the same id for.
    if version == 1 and "record_line" in d:
        raise CodexError(
            "a version 1 Codex is this object WITHOUT `record_line` (09 "
            "§9.3). A Codex already written stays 1 and cannot be moved: "
            "`version` is inside `collection_id`, so adding the key does not "
            "upgrade a collection, it names a different one")
    _only(d, _TOP if version >= 2 else _TOP_V1, "codex")

    record_line = d.get("record_line")
    if version >= 2 and record_line not in RECORD_LINES:
        raise CodexError(
            f"record_line is 1 or 2 (09 §9.3, §9.5) and this Codex says "
            f"{record_line!r}; a version {version} Codex carries it")

    de = _only(d["descriptive"], ("title", "summary"), "descriptive")
    og = _only(d["origin"], ("spec", "form", "unit", "clock", "cutoff_rule",
                             "jurisdiction"), "origin")
    tc = _only(d["toolchain"], ("platform_image", "mother_platform_image"), "toolchain")
    pa = _only(d["parser"], ("id", "version"), "parser")
    sp = _only(d["space"], ("encoder", "dim", "dtype", "ctx", "pooling",
                            "normalise", "query", "tolerance", "calibration",
                            "envelope", "epoch", "evaluation"), "space")
    # THE SHAPE FOLLOWS THE VERSION, checked where the file is read and not
    # only in `validate`: 09 §9.3 defines version 2 as "this object with
    # `space.encoder` … as `{image, weights, label}`", so a version-2 file
    # that carries an engine is not a version-2 Codex at all.
    has_engine = isinstance(sp["encoder"], dict) and "engine" in sp["encoder"]
    if has_engine != (version >= ENGINE_VERSION):
        raise CodexError(
            f"a version {version} Codex names its encoder by "
            f"{'its ENGINE — {weights, engine, label}' if version >= ENGINE_VERSION else 'its IMAGE — {image, weights, label}'}"
            f" (09 §9.3); this one carries the other shape")
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
        layers=tuple(_layer(l, i, version) for i, l in enumerate(d["layers"])),
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
        derivation=_derivation(d["derivation"]),
        replaces=_replaces(d["replaces"]),
        facets=tuple(_facet(f, i) for i, f in enumerate(d["facets"])),
        credits=tuple(_credit(c, i) for i, c in enumerate(d["credits"])),
        profile=d["profile"],
        version=version,
        record_line=record_line,
    )


def _layer(l: dict[str, Any], i: int, version: int = CODEX_VERSION) -> Layer:
    """A layer as written. `input` is REQUIRED from INPUT_VERSION and an
    unknown key below it (09 §9.3): the key's presence is part of the bytes
    the version names, so a version-4 file carrying it, or a version-5 one
    without it, is not that version's Codex. The form's VALUE is
    `validate`'s to check, with the chunker it rides on."""
    keys = ("chunker", "rule", "input") if version >= INPUT_VERSION else ("chunker", "rule")
    l = _only(l, keys, f"layers[{i}]")
    ch = _only(l["chunker"], ("id", "version", "params"), f"layers[{i}].chunker")
    form = l.get("input")
    if form is not None and not isinstance(form, dict):
        raise CodexError(f"layers[{i}].input must be an object (09 §9.3), got {form!r}")
    return Layer(Chunker(ch["id"], ch["version"], ch["params"]), l["rule"],
                 None if form is None else dict(form))


def engine_from_dict(e: Any, where: str = "engine") -> Engine:
    """An engine object (09 §9.3) as written, with `options` when it carries
    them. Whether a Codex's version admits `options` is `validate`'s to
    check, against that version."""
    if not isinstance(e, dict):
        raise CodexError(f"{where} must be an object (09 §9.3, the engine)")
    has_options = "options" in e
    if has_options and e["options"] is None:
        raise CodexError(f"{where}.options is ABSENT for an engine started by `args` "
                         f"alone, never null (09 §9.3)")
    fields = _only({k: v for k, v in e.items() if k != "options"}, ENGINE_FIELDS, where)
    if not isinstance(fields["args"], list) or not isinstance(fields["env"], dict):
        raise CodexError(f"{where}: args is a list of strings and env an object")
    return Engine(fields["name"], fields["build"], fields["variant"], fields["image"],
                  tuple(fields["args"]), dict(fields["env"]), fields["device"],
                  e["options"] if has_options else None)


_engine = engine_from_dict


def _space(sp: dict[str, Any]) -> Space:
    """A space block, in the shape it is written in. The encoder says which:
    an `engine` (Codex version 3) or an `image` (below it). `_only` refuses
    a block that mixes the two, and `validate` ties the shape to the Codex's
    own `version`."""
    raw = sp["encoder"]
    engine_shape = isinstance(raw, dict) and "engine" in raw
    if engine_shape:
        en = _only(raw, ("weights", "engine", "label"), "space.encoder")
        encoder = Encoder(None, en["weights"], en["label"],
                          _engine(en["engine"], "space.encoder.engine"))
    else:
        en = _only(raw, ("image", "weights", "label"), "space.encoder")
        encoder = Encoder(en["image"], en["weights"], en["label"])
    qu = _only(sp["query"], ("encoder", "instruction"), "space.query")
    qe = qu["encoder"]
    if qe is None:
        query_encoder = None
    elif engine_shape:
        qe = _only(qe, ("weights", "engine"), "space.query.encoder")
        query_encoder = Encoder(None, qe["weights"], "",
                                _engine(qe["engine"], "space.query.encoder.engine"))
    else:
        qe = _only(qe, ("image", "weights"), "space.query.encoder")
        query_encoder = Encoder(qe["image"], qe["weights"], "")
    ca = _only(sp["calibration"],
               ("set", "vectors", "query_vectors", "quorum") if engine_shape
               else ("set", "vectors", "quorum"), "space.calibration")
    qm = _only(ca["quorum"], ("n", "min_pass"), "space.calibration.quorum")
    return Space(
        encoder=encoder,
        dim=sp["dim"], dtype=sp["dtype"], ctx=sp["ctx"], pooling=sp["pooling"],
        normalise=sp["normalise"],
        query=Query(query_encoder, qu["instruction"]),
        tolerance=sp["tolerance"],
        calibration=Calibration(ca["set"], ca["vectors"], qm["n"], qm["min_pass"],
                                ca.get("query_vectors")),
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


def _derivation(v: dict[str, Any] | None) -> Derivation | None:
    if v is None:
        return None
    v = _only(v, ("from", "kind", "params"), "derivation")
    if v["kind"] not in DERIVATION_KINDS:
        raise CodexError(f"derivation.kind is one of "
                         f"{', '.join(DERIVATION_KINDS)} (09 §9.3), "
                         f"got {v['kind']!r}")
    if not isinstance(v["params"], dict):
        raise CodexError("derivation.params is an object")
    return Derivation(v["from"], v["kind"], v["params"])


def _facet(f: dict[str, Any], i: int) -> Facet:
    f = _only(f, ("field", "kind", "separator", "authority"), f"facets[{i}]")
    return Facet(f["field"], f["kind"], f["separator"], f["authority"])


def _credit(c: dict[str, Any], i: int) -> Credit:
    c = _only(c, ("kind", "archive", "producer", "license", "attribution",
                  "coverage"), f"credits[{i}]")
    return Credit(c["kind"], c["archive"], c["producer"], c["license"],
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


def layer_from_dict(l: dict[str, Any], i: int = 0) -> Layer:
    """A layer given on the command line (`derive --layers`). `input` MAY be
    omitted there: the Codex it lands in sets it from its version, which
    `derive` decides from the space."""
    return _layer(l, i, INPUT_VERSION if "input" in l else INPUT_VERSION - 1)


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

    `universe` MAY BE A GENERATOR and should be one when it is large. This
    draws with a heap of `n` and never materialises the universe, because
    the centres rule draws over every chunk id in the collection — 36M for
    PubMed. Measured at N = 1,000,000 from a generator: sorting peaks at
    232 MB and this peaks at 5 KB, so the sorted form needs about 8 GB at
    PubMed's size and does not run on a normal node. It is also 2x faster
    (3.32 s → 1.63 s at N = 1,000,000).

    §9.1 opens with "sort the universe of ids by their UTF-8 bytes", and
    that initial sort is NOT performed here. It is operationally redundant:
    the draw's own order is `(score, id bytes)`, a total order on distinct
    ids, so the input order cannot reach the output. Dropping it is a
    change of cost and not of result — `test_the_initial_sort_is_redundant`
    pins the two against each other, because "obviously equivalent" is how
    a sampling function silently stops being reproducible.
    """
    import heapq
    import hmac

    # Before touching the universe: at 36M ids the old order of these
    # checks meant a bad seed sorted the whole corpus and then raised.
    if len(seed) != 32:
        raise CodexError(f"a sample seed is 32 bytes, got {len(seed)}")
    if n < 0:
        raise CodexError("a sample size is not negative")

    def score(i: str) -> tuple[int, bytes]:
        raw = i.encode("utf-8")
        tag = hmac.new(seed, raw, hashlib.sha256).digest()
        return int.from_bytes(tag[:8], "big"), raw

    return heapq.nsmallest(n, universe, key=score)


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


#: §9.3 fields whose spec value may be `null`, so an older Codex that
#: predates one can be brought up to the schema WITHOUT inventing a
#: claim: absent and "not in use" mean the same thing for exactly these.
#: A field not here (`space`, `credits`, `facets`…) carries real content
#: and a Codex missing one is not migratable — it is broken.
NULLABLE_TOP = ("certifier", "content", "continues", "continues_through",
                "origin_reachable_until", "derived_from", "space_from",
                "derivation", "replaces", "profile")


def migrate_missing_nulls(raw: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Add the §9.3 fields this Codex predates, each as `null`.

    WHY THIS EXISTS. `derivation` was added to §9.3 after the node's
    collections were minted, and `_only` refuses a MISSING field as
    firmly as an unknown one — deliberately, because a peer that ignored
    one would hash the same collection differently (§9.18). So every
    Codex written before the field fails to load outright: measured
    2026-09-13, the live node's gutenberg and pubmed both refused with
    "codex: missing field(s) derivation", which is zero collections and
    a `default_collection()` that raises.

    ONLY NULLABLE FIELDS, and that is the whole safety of it. Setting
    `derivation` to null asserts what was already true — this space is
    not a derivation of another — so the migrated Codex says exactly
    what the original meant and nothing more. A missing `space` or
    `credits` is not a schema gap, it is a broken Codex, and this
    refuses to guess at one.

    THE IDENTITY MOVES, and it must. `collection_id` is the digest of
    the object without `descriptive`, so a field added to the object is
    a field added to the id; §9.3 says as much about `version` and the
    reasoning is the same. What does NOT move is `space_id`, which
    excludes `derivation` by §2.4 — so every vector already computed
    stays admissible (a bundle is admitted on `space_id` plus per-record
    `content_hash`, and `collection` is provenance that gates nothing).
    Records, chunks and snapshots are untouched.

    Returns the migrated object and the field names added.
    """
    out = dict(raw)
    version = out.get("version")
    allowed = _TOP if isinstance(version, int) and version >= 2 else _TOP_V1
    added = [k for k in allowed if k not in out and k in NULLABLE_TOP]
    for key in added:
        out[key] = None
    missing = sorted(set(allowed) - set(out))
    if missing:
        raise CodexError(
            f"this Codex is missing {', '.join(missing)}, which §9.3 does not "
            f"admit as null. That is not a schema gap this can close — a "
            f"value would have to be invented, and it would be hashed into "
            f"`collection_id` as though the creator had written it")
    return out, added
