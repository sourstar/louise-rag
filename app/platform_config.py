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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .transfer import schedule as schedule_mod

DEFAULT_WORKERS = 8
DEFAULT_PER_ENDPOINT = 4
DEFAULT_RESERVE_INTERACTIVE = 0.20   # §5.2: "a reserved interactive headroom (~20%)"


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
class PlatformConfig:
    bandwidth: BandwidthConfig = BandwidthConfig()
    transfer: TransferConfig = TransferConfig()
    trust: TrustConfig = TrustConfig()
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
    if segments != 1:
        # §5.2 defaults this to 1 and calls multi-segment downloads "default-off
        # against origins". Refusing loudly beats silently ignoring the knob.
        raise schedule_mod.ScheduleError(
            f"segments_per_file={segments} is not implemented in P0.7 "
            f"(single-file multi-connection downloads are a later task); "
            f"only segments_per_file=1 is supported")
    transfer = TransferConfig(
        workers=_positive_int(tr_raw, "workers", DEFAULT_WORKERS),
        per_endpoint=_positive_int(tr_raw, "per_endpoint", DEFAULT_PER_ENDPOINT),
        segments_per_file=segments,
    )
    return PlatformConfig(bandwidth=bandwidth, transfer=transfer,
                          trust=TrustConfig(space_authors=tuple(authors)),
                          source_path=path)


def load() -> PlatformConfig:
    """Re-reads on every call, exactly like collections.load_all() — config is
    small, and not caching means an operator edit needs no process restart to
    be visible in the System page's effective-config view."""
    path = platform_path()
    if not path.is_file():
        return PlatformConfig()
    return _load_one(path)
