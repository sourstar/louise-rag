"""Declarative acquisition specs — the shape a CorpusSource's origin(s)
declare, consumed by the generic engine (app/acquire/engine.py). See
docs/notes/architecture-v2.md §5.1.

No I/O here — pure data, mirroring app/plugins/base.py's own rule.
"""
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Literal

from ..plugins.base import LicenseClass


class Transport(str, Enum):
    RSYNC = "rsync"
    HTTPS = "https"
    FTP = "ftp"
    HF_HUB = "hf_hub"
    TORRENT = "torrent"   # BitTorrent v2 via libtorrent (transport_torrent.py)
    S3 = "s3"             # declared, unimplemented — not in P0.4 scope


@dataclass(frozen=True)
class RemoteFile:
    """What a transport's list_files() yields."""
    rel_path: str          # key into fetch_files; also the corpus_dir-relative destination
    size: int | None = None
    mtime: int | None = None
    checksum: str | None = None   # rare: inline checksum from the listing itself
    # classified by the engine after filtering: True = an opted-in EXTRA
    # format (local-only; see AcquisitionSpec.extras), never Louise data
    extra: bool = False
    # What to request, relative to the endpoint URL, when it is not
    # `rel_path`: an origin that serves a file by QUERY (Normattiva's
    # `collection-preconfezionata?nome=Codici&…`) still needs a plain
    # destination path on disk. None = request `rel_path` (every other spec).
    remote: str | None = None


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
    #: `zip-directory` (09 §9.17, amended 2026-10-12): a zip the origin rebuilds
    #: in place, brought up to date by its central directory and one Range per
    #: changed member (app/acquire/zipdelta.py); the whole fetch when it cannot
    mode: Literal["updatefiles-dir", "dated-dumps", "full-resync", "zip-directory"]
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
    """Declarative acquisition for one plugin (a NEW PLUGIN ships one of
    these, or none if its corpus is always hand-populated).

    LISTING CONTRACT (amendment 33): the transport's list_files() must
    enumerate the origin's COMPLETE listing for the profile — paginate to
    the end, never truncate, never sample. The engine walks it once per
    pass (the delta gate needs it) and records the totals as the
    `acquire_listing` observation; every honest denominator downstream —
    the Library's fetched counter, the Stats page's files/bytes-left and
    fetch ETA — reads that record, because fetch_files itself streams (a
    row per file as the pass processes it) and mid-pass shows done==total
    at every instant. A transport that cannot enumerate cheaply must still
    enumerate fully; a source that cannot be listed at all needs
    endpoint.static_files or snapshot_files instead.

    UNITS CONTRACT: "fetched" must be comparable with parsed/indexed —
    documents, wherever the layout makes them countable. `layout` drives
    it: "one-dir-per-doc" derives document totals from the listing (one
    distinct top directory = one document) and the Library reports
    documents with the file count in parentheses; container layouts
    ("container-files", "single-file") CANNOT know document counts before
    unpacking, so the card labels the count "compressed containers" and
    the parsed/indexed rows carry the document truth as each archive is
    unpacked. A new plugin whose origin publishes a total document count
    somewhere machine-readable should surface it (a future
    `docs_total_hint`); never present archive counts as documents."""
    source_id: str                        # ties to CorpusSource.source_id / Collection.plugin
    transport: Transport
    endpoints: tuple[Endpoint, ...]
    layout: Literal["one-dir-per-doc", "container-files", "single-file"]
    filters: tuple[str, ...] = ()          # base filters, applied under every profile
    checksums: ChecksumSource = field(default_factory=ChecksumSource)
    delta: DeltaSpec = field(default_factory=lambda: DeltaSpec(mode="full-resync"))
    etiquette: RateLimit = field(default_factory=RateLimit)
    profiles: dict[str, Filter] = field(default_factory=dict)
    #: the profile `slices = "all"` spells (07 §7.8), where it is not the one
    #: whose filter includes everything: a spec that lists files beyond a
    #: collection's whole (Normattiva's yearly exports, 2026-10-13) names it
    all_profile: str | None = None
    #: the slices this origin delivers WHOLE, each with the profile that
    #: fetches it (07 §7.3 `records fetch --slice`, amended 2026-10-25): a
    #: slice named as the SHARD's paths name it, at the depth the origin's
    #: files divide — `lang=it` is the itwiki dump, which holds every
    #: `lang=it/pages=…` slice. An origin whose every file may hold records
    #: of any slice (PubMed's update files, Gutenberg's tree) declares none,
    #: and only `--slice all` fetches from it. Local, like the profiles.
    slice_profiles: dict[str, str] = field(default_factory=dict)
    # MIRRORED sources (several endpoints): the relative path of a SMALL
    # file every structurally-correct mirror must serve — the structure
    # probe AND the benchmark unit (app/acquire/mirrors.py). A mirror that
    # cannot serve it at this exact path carries a different layout and is
    # excluded from ranking. None = single endpoint or no mirror logic.
    probe_file: str | None = None
    # EXTRA formats the origin offers beyond what Louise needs (gutenberg:
    # epub files). POLICY — exchange only the data that fulfils Louise's
    # purpose: extras are a LOCAL convenience download from the corpus
    # source, opted into per collection (TOML `extras = [...]`), and are
    # NEVER part of the peer-to-peer exchange — not published in corpus
    # torrents, not in snapshot manifests, not possession material. The
    # Library counts them separately ("+ e/E"); the Help page explains.
    extras: dict[str, Filter] = field(default_factory=dict)
    # How a source's profiles relate to one another:
    #
    #   "incremental" — demo ⊂ starter ⊂ full. Growing means a strictly
    #                   bigger corpus (gutenberg, pubmed, wikipedia).
    #   "exclusive"   — alternatives that must NEVER be combined: genome,
    #                   where each profile is an assembly and two assemblies
    #                   in one corpus collide on native_id (the record
    #                   ordinal within a FASTA). §9: one collection per
    #                   assembly.
    #   "additive"    — alternatives that MAY be combined freely: the
    #                   pre-embedded corpus, where each profile is a language
    #                   and the plugin encodes the language INTO native_id,
    #                   so any set of them coexists safely. A combined
    #                   profile is written as a comma-separated list
    #                   ("tr,it,en") and resolves to the union of its parts.
    profile_mode: Literal["incremental", "exclusive", "additive"] = "incremental"
    estimated_bytes: dict[str, int] = field(default_factory=dict)
    # Human-readable gloss per profile, for sources whose profile names are
    # opaque identifiers rather than words. "hg38" and "monDom5" are the
    # UCSC database names — correct, and unreadable to anyone who does not
    # already know them. Absent, the UI shows the bare name (which is right
    # for "demo"/"starter"/"full" and for language codes).
    profile_labels: dict[str, str] = field(default_factory=dict)
    # Sources that publish DATED, immutable snapshots declare how to name
    # their files for a given snapshot id, and how to find out which ids
    # exist. Both are plain callables rather than data because only the
    # source knows its own URL grammar — everything else about a spec stays
    # declarative.
    #
    # `snapshot_files(snapshot) -> tuple[RemoteFile, ...]` replaces
    # endpoint.static_files when a collection pins one.
    # `snapshot_index() -> tuple[str, ...]`, newest first, is what an
    # operator picks from and what "is there something newer?" reads.
    snapshot_files: Callable[[str], tuple[RemoteFile, ...]] | None = None
    snapshot_index: Callable[[], tuple[str, ...]] | None = None
    license_class: LicenseClass = LicenseClass.GREEN
    terms_text_path: Path | None = None    # required when license_class != GREEN


PROFILE_SEP = ","


def profile_parts(profile: str) -> list[str]:
    return [p.strip() for p in (profile or "").split(PROFILE_SEP) if p.strip()]


def resolve_profile(spec: AcquisitionSpec, profile: str) -> Filter:
    """The Filter a profile name selects, resolving COMBINED profiles.

    An additive source accepts "tr,it,en" and yields the union of those
    profiles' include patterns — each language is an independent slice of
    the same repository, and the embedded plugin encodes the language into
    native_id, so holding several in one collection is safe by
    construction (unlike genome, where it is not)."""
    if profile in spec.profiles:
        return spec.profiles[profile]
    parts = profile_parts(profile)
    if spec.profile_mode == "additive" and len(parts) > 1:
        unknown = [p for p in parts if p not in spec.profiles]
        if unknown:
            raise ValueError(
                f"{spec.source_id}: unknown profile(s) {', '.join(unknown)} — "
                f"known: {', '.join(sorted(spec.profiles))}")
        include: tuple[str, ...] = ()
        for part in parts:
            for pattern in spec.profiles[part].include:
                if pattern not in include:
                    include += (pattern,)
        # caps are deliberately dropped: the combinable profiles are plain
        # slice selectors, and summing two sources' caps would describe
        # neither of them
        return Filter(include=include)
    raise ValueError(f"{spec.source_id}: unknown profile {profile!r}")


def estimated_bytes_for(spec: AcquisitionSpec, profile: str) -> int | None:
    """Declared size of a profile, summed across a combined one."""
    if profile in spec.estimated_bytes:
        return spec.estimated_bytes[profile]
    parts = profile_parts(profile)
    if spec.profile_mode == "additive" and len(parts) > 1:
        known = [spec.estimated_bytes[p] for p in parts
                 if p in spec.estimated_bytes]
        if known:
            return sum(known)
    return None


def everything_profile(spec: AcquisitionSpec) -> str | None:
    """The profile that selects every slice — what 07 §7.8's `slices =
    "all"` fetches. Profiles are local (09 §9.17), so the collection file
    names the selection and this finds the spec's spelling of it: the one
    the spec DECLARES (`all_profile`) where it declares one — Normattiva's
    `full` is the 23 pre-packaged collections, the live collection's whole,
    while `archive` adds the yearly exports for another collection
    (2026-10-13) — else the profile whose filter includes everything."""
    if spec.all_profile:
        return spec.all_profile
    for name, flt in spec.profiles.items():
        if flt == Filter():
            return name
    return None


def delivered_slice(spec: AcquisitionSpec, slice_name: str) -> str | None:
    """The slice the origin delivers whole that holds `slice_name` — itself,
    or the declared slice it lies under (`lang=it/pages=3M` → `lang=it`) —
    or None when the origin's files do not divide by it."""
    for key in spec.slice_profiles:
        if slice_name == key or slice_name.startswith(key + "/"):
            return key
    return None


def profile_for_slices(spec: AcquisitionSpec, slices) -> str | None:
    """The profile that fetches these slices: the everything profile for
    `"all"`, else the declared profiles of the slices the origin delivers,
    combined on an additive source. None when one is not delivered whole,
    or when two need profiles that do not combine."""
    if slices == "all":
        return everything_profile(spec)
    parts: list[str] = []
    for s in slices or ():
        key = delivered_slice(spec, s)
        if key is None:
            return None
        if spec.slice_profiles[key] not in parts:
            parts.append(spec.slice_profiles[key])
    if not parts or (len(parts) > 1 and spec.profile_mode != "additive"):
        return None
    return PROFILE_SEP.join(parts)


def slices_for_profile(spec: AcquisitionSpec, profile: str | None):
    """The inverse, for a file that still names a `profile`: `"all"` for the
    everything profile, else the delivered slices whose profile selects what
    each part of it selects (wikipedia's `demo` is trwiki: `lang=tr`). None
    when a part selects something no declared slice is."""
    if not profile:
        return None
    if profile == everything_profile(spec):
        return "all"
    out: list[str] = []
    for part in profile_parts(profile):
        try:
            want = resolve_profile(spec, part)
        except ValueError:
            return None
        hits = [k for k, p in spec.slice_profiles.items()
                if p == part or spec.profiles.get(p) == want]
        if not hits:
            return None
        out += [k for k in hits if k not in out]
    return out


def admitted(rel_path: str, filters) -> bool:
    """`rel_path` against a spec's base filters: a `!` pattern vetoes, any
    other one admits (engine._apply_filters' reading, without a profile)."""
    import fnmatch
    if any(p.startswith("!") and fnmatch.fnmatch(rel_path, p[1:]) for p in filters):
        return False
    positive = [p for p in filters if not p.startswith("!")]
    return not positive or any(fnmatch.fnmatch(rel_path, p) for p in positive)


def pinned_snapshot(spec: AcquisitionSpec, origin_spec: dict | None) -> str | None:
    """The dated dump a Codex pins, read back from its `origin.spec.files`
    (09 §9.17: "for a dated-dump origin the dump pinned"), so that a
    collection file never states it a second time. None when the source
    publishes no dated dumps or the Codex pins none.

    THE SNAPSHOT AS THE CODEX'S OWN FILTERS SELECT IT. A plugin's spec may
    list more of a dump run than an older Codex pins — wikipedia's parser 5
    added each run's page_props, and the live Codex pins the dumps and their
    indexes alone — and that Codex still pins that date. A Codex pinning
    PART of what its filters select is refused, as before."""
    files = (origin_spec or {}).get("files")
    if spec.snapshot_files is None or not files:
        return None
    pinned = {f["rel_path"] for f in files}
    filters = (origin_spec or {}).get("filters") or spec.filters
    dates = sorted({seg for p in pinned for seg in p.split("/")
                    if len(seg) == 8 and seg.isdigit()})
    for date in dates:
        if {f.rel_path for f in spec.snapshot_files(date)
                if admitted(f.rel_path, filters)} == pinned:
            return date
    raise ValueError(f"{spec.source_id}: the Codex pins {len(pinned)} origin "
                     f"files that no dated snapshot of this spec lists")


@dataclass(frozen=True)
class AcquisitionPlan:
    """--dry-run / status output."""
    files_total: int
    files_pending: int
    bytes_total: int
    bytes_pending: int
    eta_seconds: float
    license_state: Literal["not_required", "acknowledged", "missing"]
    # Of files_pending, how many are already on disk at the right size and
    # will be ADOPTED (verified and recorded) rather than downloaded. Their
    # bytes are excluded from bytes_pending, so the ETA and the disk
    # preflight describe the bytes that will actually move.
    files_adoptable: int = 0
    bytes_adoptable: int = 0


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
