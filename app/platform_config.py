"""Tier-2 node configuration — `${CONFIG_DIR:-.}/platform.toml`.

architecture-v2.md §14 puts bandwidth policy in Tier 2 (versioned files:
diffable, backupable, "a node restores from git clone + compose up"), NOT in
env (Tier 1 is identity/infrastructure) and NOT in the DB (CLAUDE.md invariant
10: never write desired state into the DB). This module is that reader.

Conventions deliberately mirror app/collections.py, the repo's only other TOML
reader: stdlib tomllib, open(..., "rb"), ${CONFIG_DIR:-.} rooting, absent file
-> hard-coded defaults rather than an exception, no caching.

The surface is §5.2's, verbatim:

    [bandwidth]  down_max="400Mbit"  up_max="40Mbit"  reserve_interactive="20%"
                 schedule="01:00-07:00 full; else 30%"
    [transfer]   workers=8  per_endpoint=4  segments_per_file=1

(§14's example omits reserve_interactive/[transfer] and nests schedule under
[bandwidth]; §5.2 is the more complete of the two and the task specifies it, so
it wins. schedule is read from [bandwidth] either way, so both spellings load.)

With no platform.toml present, bandwidth is UNLIMITED and the schedule is
always-full — an existing deployment upgrades with zero behavior change, the
same philosophy collections.py follows for a missing collections/ directory.
"""
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .transfer import schedule as schedule_mod

DEFAULT_WORKERS = 8
DEFAULT_PER_ENDPOINT = 4
DEFAULT_RESERVE_INTERACTIVE = 0.20   # §5.2: "a reserved interactive headroom (~20%)"
# Hard ceiling on connections opened for ONE file. Not a performance limit —
# an etiquette one. Origins see per-client connection counts, not intentions.
MAX_SEGMENTS_PER_FILE = 8


@dataclass(frozen=True)
class BandwidthConfig:
    down_max_bps: int | None = None          # None = unlimited
    up_max_bps: int | None = None
    reserve_interactive: float = DEFAULT_RESERVE_INTERACTIVE
    schedule: schedule_mod.Schedule = schedule_mod.ALWAYS_FULL
    schedule_text: str = ""                  # kept verbatim for the UI ("schedule overlaid")


@dataclass(frozen=True)
class TransferConfig:
    workers: int = DEFAULT_WORKERS
    per_endpoint: int = DEFAULT_PER_ENDPOINT
    segments_per_file: int = 1


@dataclass(frozen=True)
class TrustConfig:
    """architecture-v2.md §14's `[trust]` block, the part P1.2 needs.

    `space_authors` are the pubkeys whose SIGNED space artifacts are fully
    trusted. This is deliberately Tier-2 file config and deliberately NOT
    trust-on-first-use: an artifact defines what "honest vectors" means, so
    trusting one on sight would hand an attacker the definition. An artifact
    from anyone else still loads — as ADVISORY, allowed to tighten checks but
    never to loosen a tolerance below the built-in coarse floor.
    """
    space_authors: tuple[str, ...] = ()


@dataclass(frozen=True)
class AdmissionConfig:
    """`[admission]` — 08 §8.2's aggregate limit (07 §7.8).

    What THIS peer accepts per hour from keys whose work it has not seen
    reproduced. "Nothing in the protocol reads another peer's setting,
    and no peer can tell what another's is: the limit governs what *this*
    peer accepts, so unlike a threshold that had to be agreed it cannot
    drift, cannot be negotiated, and cannot be gamed by anyone but its
    owner." Zero on either allowance accepts nothing from strangers,
    which §8.2 calls a complete configuration and not a broken one.
    """
    bytes_per_hour: int = 64 * 1024 ** 2
    objects_per_hour: int = 1000
    queue_bytes: int = 256 * 1024 ** 2


@dataclass(frozen=True)
class PlatformConfig:
    bandwidth: BandwidthConfig = BandwidthConfig()
    transfer: TransferConfig = TransferConfig()
    trust: TrustConfig = TrustConfig()
    admission: AdmissionConfig = AdmissionConfig()
    # [encoder_defaults.<plugin>] -> the raw [encoder]-shaped table. Kept
    # RAW here on purpose: app/collections.py owns EmbedRecipe and parses
    # it, and importing that module from this one would close an import
    # cycle (collections -> platform_config -> collections).
    #
    # The SPELLING is `encoder_defaults` (07 §7.8), matching the
    # `[encoder]` block a collection file carries. This code shipped it as
    # `embed_defaults`, which is the older word and reads as the MODE key
    # (`embed = "on-demand"`) sitting beside it. Both are read; the spec
    # name wins where a file declares both, and the alias is deprecated.
    embed_defaults: dict[str, dict] = field(default_factory=dict)
    # [node] codex_only — serve ONLY collections whose file names a Codex.
    # A node mid-migration holds both shapes; this is how an operator says
    # "a collection appears in Louise once it is converted, and not
    # before". Default False so nothing changes for a node that predates
    # the migration. Read by app/collections.py:codex_only().
    codex_only: bool = False
    source_path: Path | None = None          # None = defaults (no file on disk)


def _config_dir() -> Path:
    return Path(os.getenv("CONFIG_DIR", "."))


def platform_path() -> Path:
    return _config_dir() / "platform.toml"


def _parse_fraction(value: Any, key: str) -> float:
    """'20%' | 0.2 | 20 -> 0.2. A bare number > 1 is read as a percentage,
    since '20' plainly means 20% and 0.2 plainly means 20% — both spellings
    appear in the wild and neither reading is ever ambiguous in range."""
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return DEFAULT_RESERVE_INTERACTIVE
        if text.endswith("%"):
            try:
                pct = float(text[:-1])
            except ValueError:
                raise schedule_mod.ScheduleError(
                    f"{key}: cannot parse percentage {value!r}") from None
        else:
            try:
                pct = float(text)
            except ValueError:
                raise schedule_mod.ScheduleError(
                    f"{key}: cannot parse {value!r} (try '20%')") from None
            if pct <= 1.0:
                pct *= 100.0
    else:
        pct = float(value)
        if pct <= 1.0:
            pct *= 100.0
    if not 0.0 <= pct < 100.0:
        raise schedule_mod.ScheduleError(
            f"{key}: {value!r} out of range — expected 0% <= x < 100%")
    return pct / 100.0


_SIZE_UNITS = {"": 1, "b": 1, "kb": 1000, "mb": 1000 ** 2, "gb": 1000 ** 3,
               "kib": 1024, "mib": 1024 ** 2, "gib": 1024 ** 3}


def _parse_bytes(value: Any, key: str, default: int) -> int:
    """`"64MB"` | `67108864` | `0` -> bytes. `0` is meaningful (08 §8.2:
    a peer that accepts nothing from strangers), so it is not a falsy
    stand-in for "unset" — absence is."""
    if value is None:
        return default
    if isinstance(value, (int, float)):
        n = int(value)
    else:
        text = str(value).strip().lower()
        i = 0
        while i < len(text) and (text[i].isdigit() or text[i] in "._"):
            i += 1
        unit = text[i:].strip()
        if unit not in _SIZE_UNITS:
            raise schedule_mod.ScheduleError(
                f"{key}: unknown size unit in {value!r} — use B, KB, MB, GB "
                f"(powers of 1000) or KiB, MiB, GiB (powers of 1024)")
        try:
            n = int(float(text[:i].replace("_", "")) * _SIZE_UNITS[unit])
        except ValueError:
            raise schedule_mod.ScheduleError(
                f"{key}: cannot parse a size from {value!r}") from None
    if n < 0:
        raise schedule_mod.ScheduleError(f"{key}: must be >= 0, got {n}")
    return n


def _positive_int(raw: dict, key: str, default: int) -> int:
    value = raw.get(key, default)
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise schedule_mod.ScheduleError(f"{key}: expected an integer, got {value!r}") from None
    if n < 1:
        raise schedule_mod.ScheduleError(f"{key}: must be >= 1, got {n}")
    return n


def _load_one(path: Path) -> PlatformConfig:
    with open(path, "rb") as f:
        raw: dict[str, Any] = tomllib.load(f)
    bw_raw = raw.get("bandwidth", {}) or {}
    tr_raw = raw.get("transfer", {}) or {}
    trust_raw = raw.get("trust", {}) or {}

    authors_raw = trust_raw.get("space_authors", [])
    if not isinstance(authors_raw, list):
        raise schedule_mod.ScheduleError("trust.space_authors must be a list of pubkeys")
    authors = []
    for value in authors_raw:
        if not isinstance(value, str) or len(value) != 64:
            raise schedule_mod.ScheduleError(
                f"trust.space_authors entries must be 64-hex pubkeys, got {value!r}")
        try:
            bytes.fromhex(value)
        except ValueError:
            raise schedule_mod.ScheduleError(
                f"trust.space_authors entry is not hex: {value!r}") from None
        authors.append(value.lower())

    schedule_text = (bw_raw.get("schedule") or "").strip()
    bandwidth = BandwidthConfig(
        down_max_bps=schedule_mod.parse_rate(bw_raw.get("down_max")),
        up_max_bps=schedule_mod.parse_rate(bw_raw.get("up_max")),
        reserve_interactive=(
            _parse_fraction(bw_raw["reserve_interactive"], "reserve_interactive")
            if "reserve_interactive" in bw_raw else DEFAULT_RESERVE_INTERACTIVE),
        schedule=schedule_mod.parse_schedule(schedule_text),
        schedule_text=schedule_text,
    )

    segments = _positive_int(tr_raw, "segments_per_file", 1)
    if segments > MAX_SEGMENTS_PER_FILE:
        # §5.2 calls multi-segment downloads "default-off against origins".
        # They stay opt-in, and the ceiling is etiquette: past a handful of
        # connections one client stops being a polite peer and starts being
        # a small denial of service, with no throughput to show for it.
        raise schedule_mod.ScheduleError(
            f"segments_per_file={segments} exceeds the {MAX_SEGMENTS_PER_FILE} "
            f"connection ceiling for a single file (§5.2: default-off against "
            f"origins). Raise per_endpoint if you want more parallelism ACROSS "
            f"files — that is the axis origins mind less.")
    transfer = TransferConfig(
        workers=_positive_int(tr_raw, "workers", DEFAULT_WORKERS),
        per_endpoint=_positive_int(tr_raw, "per_endpoint", DEFAULT_PER_ENDPOINT),
        segments_per_file=segments,
    )
    # [encoder_defaults.<plugin>] — validated as "a table per plugin" only;
    # the field-level parse belongs to collections.parse_recipe, which owns
    # EmbedRecipe and reports errors in the same words for both files.
    # `embed_defaults` is the deprecated alias: read, merged UNDER the spec
    # name, and named by its own word in any error so the operator is told
    # which line of which file is wrong.
    embed_raw: dict = {}
    for key in ("embed_defaults", "encoder_defaults"):
        table_raw = raw.get(key, {}) or {}
        if not isinstance(table_raw, dict):
            raise schedule_mod.ScheduleError(
                f"{key} must be a table of per-plugin [encoder] blocks, "
                f"e.g. [{key}.pubmed]")
        for plugin, table in table_raw.items():
            if not isinstance(table, dict):
                raise schedule_mod.ScheduleError(
                    f"{key}.{plugin} must be a table, got {type(table).__name__}")
        embed_raw.update({str(k): dict(v) for k, v in table_raw.items()})

    adm_raw = raw.get("admission", {}) or {}
    if not isinstance(adm_raw, dict):
        raise schedule_mod.ScheduleError("admission must be a table")
    defaults = AdmissionConfig()
    objects = adm_raw.get("objects_per_hour", defaults.objects_per_hour)
    try:
        objects = int(objects)
    except (TypeError, ValueError):
        raise schedule_mod.ScheduleError(
            f"admission.objects_per_hour: expected an integer, got {objects!r}"
        ) from None
    if objects < 0:
        raise schedule_mod.ScheduleError(
            f"admission.objects_per_hour: must be >= 0, got {objects}")
    admission = AdmissionConfig(
        bytes_per_hour=_parse_bytes(adm_raw.get("bytes_per_hour"),
                                    "admission.bytes_per_hour",
                                    defaults.bytes_per_hour),
        objects_per_hour=objects,
        queue_bytes=_parse_bytes(adm_raw.get("queue"), "admission.queue",
                                 defaults.queue_bytes),
    )

    node_raw = raw.get("node", {}) or {}
    codex_only_raw = node_raw.get("codex_only", False)
    if not isinstance(codex_only_raw, bool):
        # Refused rather than coerced: `codex_only = "false"` read as truthy
        # would hide every unconverted collection on the node, which looks
        # exactly like data loss to whoever opens the Library.
        raise schedule_mod.ScheduleError(
            f"node.codex_only must be true or false, got {codex_only_raw!r}")

    return PlatformConfig(bandwidth=bandwidth, transfer=transfer,
                          trust=TrustConfig(space_authors=tuple(authors)),
                          admission=admission,
                          embed_defaults=embed_raw,
                          codex_only=codex_only_raw,
                          source_path=path)


def load() -> PlatformConfig:
    """Re-reads on every call, exactly like collections.load_all() — config is
    small, and not caching means an operator edit needs no process restart to
    be visible in the System page's effective-config view."""
    path = platform_path()
    if not path.is_file():
        return PlatformConfig()
    return _load_one(path)
