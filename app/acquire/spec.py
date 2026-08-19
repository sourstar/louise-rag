"""Declarative acquisition specs — the shape a CorpusSource's origin(s)
declare, consumed by the generic engine (app/acquire/engine.py). See
docs/architecture-v2.md §5.1.

No I/O here — pure data, mirroring app/plugins/base.py's own rule.
"""
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Literal

from ..plugins.base import LicenseClass


class Transport(str, Enum):
    RSYNC = "rsync"
    HTTPS = "https"
    FTP = "ftp"
    HF_HUB = "hf_hub"
    TORRENT = "torrent"   # stub interface only — Phase 2
    S3 = "s3"             # declared, unimplemented — not in P0.4 scope


@dataclass(frozen=True)
class RemoteFile:
    """What a transport's list_files() yields."""
    rel_path: str          # key into fetch_files; also the corpus_dir-relative destination
    size: int | None = None
    mtime: int | None = None
    checksum: str | None = None   # rare: inline checksum from the listing itself


@dataclass(frozen=True)
class Endpoint:
    """One source of bytes. AcquisitionSpec.endpoints is ordered: mirrors
    first, origin last."""
    transport: Transport
    url: str                              # base URL / rsync target / hf repo id
    region: str | None = None
    max_conns: int = 4                    # etiquette; read-only today, P0.7 enforces
    ssh_key_path: Path | None = None      # rsync-over-ssh key — path only (invariant 8)
    auth_token_path: Path | None = None   # bearer token file — path only (invariant 8)
    # Declared file list for transports with no generic directory-listing
    # protocol (https, hf_hub — unlike rsync's --list-only or FTP's
    # MLSD/NLST, plain HTTPS has nothing standard to crawl). Real per-source
    # specs name the files a profile needs explicitly; rsync/ftp transports
    # ignore this and list dynamically instead.
    static_files: tuple[RemoteFile, ...] = ()


@dataclass(frozen=True)
class ChecksumSource:
    kind: Literal["sidecar", "manifest", "none"] = "none"
    sidecar_suffix: str = ".md5"
    sidecar_algo: Literal["md5"] = "md5"


@dataclass(frozen=True)
class DeltaSpec:
    mode: Literal["updatefiles-dir", "dated-dumps", "full-resync"]
    updatefiles_subdir: str | None = None


@dataclass(frozen=True)
class RateLimit:
    """Declared etiquette. P0.4 only records/reports this; P0.7's transfer
    manager is what actually enforces it (CLAUDE.md invariant 5 — no per-tool
    rate limits as a substitute for the central budget)."""
    max_conns: int = 4
    bandwidth_cap_bps: int | None = None
    windows: str | None = None            # cron-like string, same shape as §5.2's schedule


@dataclass(frozen=True)
class Filter:
    """One profile (demo | starter | recent | full)."""
    include: tuple[str, ...] = ("*",)
    exclude: tuple[str, ...] = ()
    max_files: int | None = None
    max_bytes: int | None = None


@dataclass(frozen=True)
class AcquisitionSpec:
    source_id: str                        # ties to CorpusSource.source_id / Collection.plugin
    transport: Transport
    endpoints: tuple[Endpoint, ...]
    layout: Literal["one-dir-per-doc", "container-files", "single-file"]
    filters: tuple[str, ...] = ()          # base filters, applied under every profile
    checksums: ChecksumSource = field(default_factory=ChecksumSource)
    delta: DeltaSpec = field(default_factory=lambda: DeltaSpec(mode="full-resync"))
    etiquette: RateLimit = field(default_factory=RateLimit)
    profiles: dict[str, Filter] = field(default_factory=dict)
    # Profiles are INCREMENTAL by default (demo ⊂ starter ⊂ full), which is
    # what makes "grow" meaningful: acquisition only ever adds files.
    # exclusive_profiles=True marks a source whose profiles are ALTERNATIVES
    # — genome, where each profile is a different assembly (§9: one
    # collection per assembly). Growing across them is not a bigger corpus,
    # it is a second corpus in the same collection, and for genome that
    # silently collides: native_id is the record ordinal WITHIN a FASTA, so
    # every assembly mints the same document ids (observed live — a `full`
    # grow left yeast titles on human locators with yeast vectors attached).
    exclusive_profiles: bool = False
    estimated_bytes: dict[str, int] = field(default_factory=dict)
    license_class: LicenseClass = LicenseClass.GREEN
    terms_text_path: Path | None = None    # required when license_class != GREEN


@dataclass(frozen=True)
class AcquisitionPlan:
    """--dry-run / status output."""
    files_total: int
    files_pending: int
    bytes_total: int
    bytes_pending: int
    eta_seconds: float
    license_state: Literal["not_required", "acknowledged", "missing"]


class LicenseAckRequiredError(RuntimeError):
    """A yellow+ source has no valid (current) license acknowledgement.
    Raised before any transport call — CLAUDE.md invariant 9."""


class DiskPreflightError(RuntimeError):
    """Not enough free disk space for the chosen profile. The whole run is
    refused before any byte moves — never half-fill."""


class TransportError(RuntimeError):
    """A transport-level failure (network, subprocess, protocol)."""


class TransferAborted(TransportError):
    """A stop event (SIGTERM) fired mid-transfer.

    Subclasses TransportError deliberately: engine.run_acquisition catches
    (TransportError, OSError) per file, so a bare Exception here would escape
    the loop entirely and leave a .part file on disk with a stale fetch_files
    row — a direct CLAUDE.md invariant-2 break.

    But this is NOT a failure and must never reach _upsert_failed: a clean
    shutdown that incremented `retries` would permanently blacklist a healthy
    file after MAX_RETRIES restarts, and invariant 2 requires SIGTERM to exit
    0. The engine records it via _upsert_aborted instead, which leaves
    `retries` untouched.
    """
