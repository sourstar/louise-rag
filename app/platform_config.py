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
import re
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
class ShareConfig:
    """`[share]` (07 §7.8). `patch_cache_s`: how long this peer keeps a part
    patch it computed for a peer (app/share/patch.py), so the many peers
    asking for the same patch after a new snapshot get it without a
    recompute; 0 deletes each patch once served (operator, 2026-10-12)."""
    patch_cache_s: int = 7 * 86400


@dataclass(frozen=True)
class TranslateConfig:
    """`[translate]` (07 §7.8). `reading`: {source language: model} — the
    Ask page's translation of what is READ (results, the reader) for a text
    in that language goes to that model instead of TRANSLATE_MODEL
    (app/translate.py `reading_model`). Searches never use it: a query is
    translated by TRANSLATE_MODEL. Node-wide, in no Codex (operator,
    2026-10-12; docs/notes/translator-choice.md: Index-Translate-9B for
    Latin and Ancient Greek)."""
    reading: dict[str, str] = field(default_factory=dict)


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
class CertifierConfig:
    """`[certifier]` (07 §7.8; 09 §9.11): what this node says of ITS OWN
    key when it serves the anchor document at
    `/.well-known/louise-certifier.json` — from when the key certifies,
    and when it was revoked, if ever. Both 09 §9.1 times. Only a node
    that fronts itself with TLS on a domain the operator controls is an
    anchor (03 §3.7); the document is served regardless."""
    valid_from: str | None = None
    revoked_at: str | None = None


@dataclass(frozen=True)
class AskConfig:
    """`[ask]` (07 §7.8): the reranker's latency bound (05 §5.6). A list
    the reranker cannot order inside it keeps its fused order and says
    `reranked: false`. 8000 by the operator's decision (2026-09-15): at
    the spec's former 400 every list fell back, and the GPU reranker takes
    0.3–0.7 s on pubmed and wikipedia and 3–6 s on gutenberg's long chunks.

    `translate_share` (added 2026-10-10, 05 §5.2): a translated query goes
    out only into a collection's MAIN languages — its text index groups,
    largest first, until they hold this share of its documents. 0.90 is
    gutenberg's en, fr, fi, de (92 % of the books); measured offline,
    translating into those alone beat all nineteen (descriptive MRR 0.231
    → 0.245, R@50 0.605 → 0.630; docs/notes/one-language-per-work.md).
    1.0 is every language with an index of its own, as before."""
    latency_budget_ms: int = 8000
    translate_share: float = 0.90


@dataclass(frozen=True)
class ModelsConfig:
    """`[models]` (07 §7.8, added 2026-10-09): `load_timeout_s` bounds the
    LOAD of a model a runner reports as not loaded — the warm request
    app/model_load.py sends before the real one, so that no request's
    budget holds a cold start (operator, 2026-10-09). A model measured on
    the node has its own bound (app/model_loads.json, from
    docs/notes/model-profile.md); this is the bound of every other. 120 s:
    the slowest load measured is LocalAGI's 2B chat model at 44–46 s
    (docs/notes/vega.md §11), and a load can be slowed by the card's other
    work."""
    load_timeout_s: int = 120


@dataclass(frozen=True)
class DepositConfig:
    """`[deposit]` (07 §7.8; 02 §2.9): what a deposit collection is created
    with when the creator names nothing — a client's create carries no
    model. `encoder` is the endpoint; `space` a file holding the space
    block for it, needed once, by the first deposit collection on that
    encoder (later ones copy its space); `space_from` a Codex to copy the
    space from instead. `[deposit.collections.<name>] space_from` does the
    same for one name."""
    encoder: str | None = None
    space: Path | None = None
    space_from: str | None = None
    by_name: dict[str, str] = field(default_factory=dict)
    # 07 §7.8 (amended 2026-10-09): the rest of the `[encoder]` block a new
    # deposit collection's file is written with. `encoder` is the SERVER;
    # on LocalAI (api = "vega") one address serves every model, so the URL
    # names none and the model keys must. Absent, the model is the one a
    # model-container URL names (`…/upstream/<model>`), else create refuses.
    api: str = "openai"
    embed_model: str | None = None
    query_model: str | None = None


@dataclass(frozen=True)
class PeersConfig:
    """`[peers]`, `[dht]` and the node's own address (07 §7.8; 04 §4.2):
    the bootstrap addresses `share peers add` writes, the keys it pins to
    an address (`[peers.pins]`: a digest from that address under another
    key is discarded), whether the DHT is on, and where THIS node answers
    pulls — `[node] address`, the reachable one
    (`http://192.168.178.10:30042` on the reference node), not the
    container's port; `[node] name` labels it."""
    addresses: tuple[str, ...] = ()
    dht: bool = True
    address: str | None = None
    name: str | None = None
    pins: dict[str, str] = field(default_factory=dict)
    peer_port: int | None = None      # `[node] peer_port`: the swarm's port, as peers reach it


_KEY_TEXT = re.compile(r"ed25519:[0-9a-f]{64}")


def normalise_address(text: Any) -> str:
    """A peer address as the messages carry it (09 §9.9): `http(s)://host[:port]`,
    no path, no trailing slash. A bare `host:port` is http. Refused when it
    names no host or carries a path — one spelling per peer, so the list and
    the pins hold each address once."""
    from urllib.parse import urlsplit
    if not isinstance(text, str) or not text.strip():
        raise schedule_mod.ScheduleError(f"a peer address is http(s)://host[:port], got {text!r}")
    s = text.strip()
    if "://" not in s:
        s = "http://" + s
    parts = urlsplit(s)
    try:
        port = parts.port
    except ValueError:
        port = -1
    if parts.scheme not in ("http", "https") or parts.path.strip("/") or parts.query \
            or parts.fragment or port == -1 \
            or not re.fullmatch(r"([A-Za-z0-9.\-]+|\[[0-9A-Fa-f:.]+\])(:\d{1,5})?", parts.netloc):
        raise schedule_mod.ScheduleError(f"a peer address is http(s)://host[:port], got {text!r}")
    return f"{parts.scheme}://{parts.netloc}"


@dataclass(frozen=True)
class PlatformConfig:
    bandwidth: BandwidthConfig = BandwidthConfig()
    transfer: TransferConfig = TransferConfig()
    share: ShareConfig = ShareConfig()
    translate: TranslateConfig = TranslateConfig()
    trust: TrustConfig = TrustConfig()
    admission: AdmissionConfig = AdmissionConfig()
    ask: AskConfig = AskConfig()
    models: ModelsConfig = ModelsConfig()
    certifier: CertifierConfig = CertifierConfig()
    deposit: DepositConfig = DepositConfig()
    peers: PeersConfig = PeersConfig()
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
    # [node] codex_only — OBSOLETE since 2026-10-08: a node serves only Codex
    # collections, always (the pre-Codex shape is gone). Still accepted, so
    # a platform.toml that sets it loads; read by nothing.
    # Was: serve ONLY collections whose file names a Codex.
    # A node mid-migration holds both shapes; this is how an operator says
    # "a collection appears in Louise once it is converted, and not
    # before". Default False so nothing changes for a node that predates
    # the migration. Read by app/collections.py:codex_only().
    codex_only: bool = False
    # [node] quarantine — "report" or "enforce" (03 §3.6; migration plan, step
    # 14). A copy's state is always computed and shown; only "enforce"
    # WITHHOLDS a copy in quarantine. Default "report", and that is the first
    # of two deploys: the day the states exist no Codex collection on a node
    # has its run statement or its own origin check yet, so enforcing at once
    # would take every one of them out of search together. The operator
    # switches it once `hold collection show` reads in-use for each collection
    # meant to be. Removed at step 12. Read by app/copy_state.py:enforced().
    quarantine: str = "report"
    # [node] quarantine_in_use_before — the copies this peer ALREADY USED the day
    # it began to enforce (operator, 2026-09-20: "assume that the current
    # collections have already passed it; the next collection to be created
    # will be our guinea pig"). By COLLECTION ID, never a name: a name can be
    # given to another collection. It stands in for the two tests a copy made
    # before the tests existed cannot run yet — roots-and-chunks, the origin —
    # and for nothing else: never the licence, never the gate, never a test
    # that FAILED. Read by app/copy_state.py:in_use_before().
    quarantine_in_use_before: tuple[str, ...] = ()
    # [schedule] / [profiles] / [gpu.levels] — the node's quiet, day and night
    # profiles (app/node_profile.py); None when the file declares no schedule
    profiles: Any = None
    source_path: Path | None = None          # None = defaults (no file on disk)


_COLLECTION_ID = re.compile(r"sha256:[0-9a-f]{64}")


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
    # 07 §7.8 names the table `[budget]`; `[bandwidth]` is this module's
    # older spelling and still loads. Only the older one was read, so a file
    # written as the spec says had its ceilings silently ignored (2026-10-19)
    if raw.get("budget") is not None and raw.get("bandwidth") is not None:
        raise schedule_mod.ScheduleError("[budget] and [bandwidth] are one table (07 §7.8): keep [budget]")
    bw_raw = raw.get("budget", raw.get("bandwidth", {})) or {}
    tr_raw = raw.get("transfer", {}) or {}
    trust_raw = raw.get("trust", {}) or {}
    share_raw = raw.get("share", {}) or {}
    cache_s = share_raw.get("patch_cache_s", ShareConfig.patch_cache_s)
    if not isinstance(cache_s, int) or isinstance(cache_s, bool) or cache_s < 0:
        raise schedule_mod.ScheduleError(
            f"[share] patch_cache_s is a number of seconds, 0 or more (0 = delete each "
            f"patch once served), got {cache_s!r}")
    reading_raw = (raw.get("translate", {}) or {}).get("reading", {}) or {}
    if not isinstance(reading_raw, dict) or not all(
            isinstance(k, str) and re.fullmatch(r"[a-z]{2,3}", k) and isinstance(v, str) and v.strip()
            for k, v in reading_raw.items()):
        raise schedule_mod.ScheduleError(
            "[translate] reading is {source language code: model name}, "
            "e.g. { la = \"index-translate-9b\" } (07 §7.8)")

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

    ask_raw = raw.get("ask", {}) or {}
    if not isinstance(ask_raw, dict):
        raise schedule_mod.ScheduleError("ask must be a table")
    share = ask_raw.get("translate_share", AskConfig().translate_share)
    if isinstance(share, bool) or not isinstance(share, (int, float)) or not 0 < share <= 1:
        raise schedule_mod.ScheduleError(
            f"ask.translate_share: a share of documents, 0 < share <= 1, got {share!r} (07 §7.8)")
    ask = AskConfig(latency_budget_ms=_positive_int(
        ask_raw, "latency_budget_ms", AskConfig().latency_budget_ms),
        translate_share=float(share))

    models_raw = raw.get("models", {}) or {}
    if not isinstance(models_raw, dict):
        raise schedule_mod.ScheduleError("models must be a table (07 §7.8)")
    models = ModelsConfig(load_timeout_s=_positive_int(
        models_raw, "load_timeout_s", ModelsConfig().load_timeout_s))

    cert_raw = raw.get("certifier", {}) or {}
    if not isinstance(cert_raw, dict):
        raise schedule_mod.ScheduleError("certifier must be a table (07 §7.8)")
    for key in ("valid_from", "revoked_at"):
        v = cert_raw.get(key)
        if v is not None and not (isinstance(v, str) and re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", v)):
            raise schedule_mod.ScheduleError(f"[certifier] {key} is a time YYYY-MM-DDTHH:MM:SSZ (09 §9.1)")
    certifier = CertifierConfig(valid_from=cert_raw.get("valid_from"), revoked_at=cert_raw.get("revoked_at"))

    node_raw = raw.get("node", {}) or {}
    codex_only_raw = node_raw.get("codex_only", False)
    if not isinstance(codex_only_raw, bool):
        # Refused rather than coerced: `codex_only = "false"` read as truthy
        # would hide every unconverted collection on the node, which looks
        # exactly like data loss to whoever opens the Library.
        raise schedule_mod.ScheduleError(
            f"node.codex_only must be true or false, got {codex_only_raw!r}")
    quarantine_raw = node_raw.get("quarantine", "report")
    if quarantine_raw not in ("report", "enforce"):
        # refused, not coerced: a typo must not decide whether copies are
        # withheld — in either direction
        raise schedule_mod.ScheduleError(
            f"node.quarantine must be \"report\" or \"enforce\", got {quarantine_raw!r}")
    before_raw = node_raw.get("quarantine_in_use_before", [])
    if not isinstance(before_raw, list) or not all(
            isinstance(c, str) and _COLLECTION_ID.fullmatch(c) for c in before_raw):
        # refused, not skipped: an entry that names nothing would leave the copy
        # it was meant for withheld, and the operator reading "in the list"
        raise schedule_mod.ScheduleError(
            "node.quarantine_in_use_before must be a list of collection ids, each "
            f"\"sha256:\" and 64 hex digits (`louise hold collection show`), got {before_raw!r}")

    deposit = _deposit(raw.get("deposit") or {}, path)
    peers_raw = raw.get("peers") or {}
    addresses_raw = peers_raw.get("addresses", []) if isinstance(peers_raw, dict) else None
    if not isinstance(addresses_raw, list) or not all(isinstance(a, str) and a for a in addresses_raw):
        raise schedule_mod.ScheduleError("[peers] addresses is a list of strings (07 §7.8)")
    addresses: list[str] = []
    for a in addresses_raw:
        a = normalise_address(a)
        if a not in addresses:
            addresses.append(a)
    pins_raw = peers_raw.get("pins", {}) if isinstance(peers_raw, dict) else {}
    if not isinstance(pins_raw, dict) or not all(
            isinstance(k, str) and _KEY_TEXT.fullmatch(k) for k in pins_raw.values()):
        raise schedule_mod.ScheduleError(
            "[peers.pins] maps an address to the key it is pinned to, \"ed25519:\" and 64 hex "
            "(07 §7.8; 04 §4.2)")
    pins = {normalise_address(a): k for a, k in pins_raw.items()}
    dht_raw = raw.get("dht") or {}
    dht_enabled = dht_raw.get("enabled", True) if isinstance(dht_raw, dict) else True
    if not isinstance(dht_enabled, bool):
        raise schedule_mod.ScheduleError("[dht] enabled is true or false (07 §7.8)")
    node_address = node_raw.get("address")
    if node_address is not None and not (isinstance(node_address, str) and node_address.startswith("http")):
        raise schedule_mod.ScheduleError("[node] address is this node's reachable URL, http(s)://host:port")
    peer_port = node_raw.get("peer_port")
    if peer_port is not None and (not isinstance(peer_port, int) or isinstance(peer_port, bool)
                                  or not 1 <= peer_port <= 65535):
        raise schedule_mod.ScheduleError("[node] peer_port is the port this node's swarm listens "
                                         "on as peers reach it, 1-65535 (07 §7.8)")
    peers = PeersConfig(addresses=tuple(addresses), dht=dht_enabled, address=node_address,
                        name=node_raw.get("name") if isinstance(node_raw.get("name"), str) else None,
                        pins=pins, peer_port=peer_port)
    return PlatformConfig(bandwidth=bandwidth, transfer=transfer,
                          share=ShareConfig(patch_cache_s=cache_s),
                          translate=TranslateConfig(reading={k: v.strip() for k, v in reading_raw.items()}),
                          deposit=deposit,
                          peers=peers,
                          trust=TrustConfig(space_authors=tuple(authors)),
                          admission=admission,
                          ask=ask,
                          models=models,
                          certifier=certifier,
                          embed_defaults=embed_raw,
                          codex_only=codex_only_raw,
                          quarantine=quarantine_raw,
                          quarantine_in_use_before=tuple(before_raw),
                          profiles=_profiles(raw),
                          source_path=path)


def _profiles(raw: dict[str, Any]):
    """[schedule] / [profiles] / [gpu.levels], parsed and checked at load so a
    typo is refused at boot, not at the next quiet period."""
    from . import node_profile
    return node_profile.parse(raw)


def _deposit(raw: Any, path: Path) -> DepositConfig:
    if not isinstance(raw, dict):
        raise schedule_mod.ScheduleError("[deposit] is a table (07 §7.8)")
    known = {"encoder", "api", "embed_model", "query_model", "space", "space_from",
             "collections"}
    unknown = sorted(set(raw) - known)
    if unknown:
        # refused, not ignored: a misspelt key would silently leave every
        # client-created collection on another encoder than the one meant
        raise schedule_mod.ScheduleError(
            f"[deposit]: unknown key(s) {', '.join(unknown)}; it takes "
            f"encoder, api, embed_model, query_model, space, space_from and "
            f"[deposit.collections.<name>]")
    for k in ("encoder", "api", "embed_model", "query_model", "space", "space_from"):
        if k in raw and not isinstance(raw[k], str):
            raise schedule_mod.ScheduleError(f"deposit.{k} is a string, got {raw[k]!r}")
    api, embed_model, query_model = _deposit_encoder(raw)
    by_name = {}
    for name, table in (raw.get("collections") or {}).items():
        if not isinstance(table, dict) or set(table) != {"space_from"} \
                or not isinstance(table["space_from"], str):
            raise schedule_mod.ScheduleError(
                f"[deposit.collections.{name}] carries space_from and nothing else")
        by_name[name] = table["space_from"]
    space = raw.get("space")
    return DepositConfig(
        encoder=raw.get("encoder") or None,
        space=((path.parent / space) if space else None),
        space_from=raw.get("space_from") or None,
        by_name=by_name,
        api=api, embed_model=embed_model, query_model=query_model)


def _deposit_encoder(raw: dict) -> tuple[str, str | None, str | None]:
    """`[deposit]`'s api and model keys (07 §7.8, amended 2026-10-09), checked
    as a collection file's `[encoder]` would check them — at load, so a
    wrong value is the operator's to see at once, not a client's create
    refused later, or worse a file written that cannot embed."""
    # one set of wire formats for every encoder block (app/hold/calibrate.py)
    from app.collections import model_named_by
    from app.hold.calibrate import APIS
    api = raw.get("api") or "openai"
    if api not in APIS:
        raise schedule_mod.ScheduleError(
            f"deposit.api is one of {', '.join(APIS)} (07 §7.8), got {api!r}")
    embed_model = raw.get("embed_model") or None
    query_model = raw.get("query_model") or None
    encoder = raw.get("encoder") or None
    if (embed_model or query_model or "api" in raw) and not encoder:
        raise schedule_mod.ScheduleError(
            "[deposit]: api, embed_model and query_model describe deposit.encoder, "
            "which is not set")
    if api == "vega" and not (embed_model and query_model):
        # LocalAI serves every model at one address: the URL names none, and
        # without the keys the file would be written with no model to ask for
        raise schedule_mod.ScheduleError(
            "[deposit]: api = \"vega\" requires embed_model and query_model — "
            "LocalAI's one address names no model, so the keys must (07 §7.8)")
    named = model_named_by(encoder) if encoder else None
    for key, value in (("embed_model", embed_model), ("query_model", query_model)):
        if named is not None and value is not None and value != named:
            raise schedule_mod.ScheduleError(
                f"deposit.{key} = {value!r} and deposit.encoder {encoder} names "
                f"{named!r}; on the model container the URL says which model, and "
                f"the key must agree with it (07 §7.8)")
    return api, embed_model, query_model


def load() -> PlatformConfig:
    """Re-reads on every call, exactly like collections.load_all() — config is
    small, and not caching means an operator edit needs no process restart to
    be visible in the System page's effective-config view."""
    path = platform_path()
    if not path.is_file():
        return PlatformConfig()
    return _load_one(path)
