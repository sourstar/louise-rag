"""Collection = (plugin, corpus snapshot, pipeline versions, embedding space).

Each collection gets its own manifest.db / fts/ / lancedb/ paths. Config lives
in `${CONFIG_DIR:-.}/collections/*.toml` (architecture-v2.md §3, §14 Tier 2).
With no collections/ directory present, a single default Gutenberg collection
is synthesized whose paths equal today's top-level DATA_DIR/CORPUS_DIR layout
exactly — an existing deployment upgrades with zero data movement and no new
required config or volume mount.
"""
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import config
from .plugins import embedded, genome, gutenberg, packed, pubmed, wikipedia
from .plugins.base import CorpusSource

PLUGIN_REGISTRY: dict[str, CorpusSource] = {
    "embedded": embedded.SOURCE,
    "genome": genome.SOURCE,
    "gutenberg": gutenberg.SOURCE,
    "packed": packed.SOURCE,        # placeholder; a collection gets its per-pack instance
    "pubmed": pubmed.SOURCE,
    "wikipedia": wikipedia.SOURCE,
}


@dataclass(frozen=True)
class EmbedRecipe:
    """A collection's embedding recipe — architecture-v2.md §14 amendment 9.

    Declared per collection in the TOML's [embed] block; a collection
    without one uses the node's Tier-1 env recipe (fallback, not override).
    Immutability is NOT this dataclass's job: the pinned space_id and boot
    validation decide whether the vectors on disk agree with the declared
    recipe (invariant 1)."""
    url: str
    api: str                     # openai | ollama
    model: str
    dtype: str                   # f32 | f16
    ctx: int
    dim: int | None = None       # declared width; checked against the table
    query_url: str | None = None  # bi-encoder pairs (MedCPT); None = symmetric
    # Documents embedded concurrently by the worker (thread pool of whole
    # documents, each a single-text request). NOT part of the space identity
    # — it changes no vector, like EMBED_BATCH. >1 belongs to CPU encoders
    # (TEI/MedCPT); GPU llama-server stays serial — its multi-sequence
    # handling is the defect the batch gate exists for.
    concurrency: int = 1
    # Which processor this endpoint spends: "gpu" | "cpu". DESCRIPTIVE
    # ONLY — it never enters the space id, because the same model on a
    # different device computes the same function (that is precisely what
    # the live cross-backend calibration measured). It exists so the UI can
    # tell an operator whether flipping embed mode spends the scarce GPU or
    # spare CPU. Absent, it is inferred from `concurrency`, which in this
    # codebase IS the split: GPU stays serial, CPU parallelizes.
    device: str | None = None

    @property
    def spends(self) -> str:
        if self.device in ("gpu", "cpu"):
            return self.device
        return "cpu" if self.concurrency > 1 else "gpu"


def env_recipe() -> EmbedRecipe:
    """The Tier-1 fallback, built at call time so env/monkeypatched config
    changes are always honored."""
    return EmbedRecipe(url=config.EMBED_URL, api=config.EMBED_API,
                        model=config.EMBED_MODEL, dtype=config.EMBED_DTYPE,
                        ctx=config.EMBED_CTX)


def parse_recipe(raw: Any, *, where: str) -> EmbedRecipe | None:
    """An [embed]-shaped TOML table -> EmbedRecipe. None for absent/not-a-
    table, so callers can say "declared or not".

    Shared by the per-collection [embed] block and platform.toml's per-plugin
    defaults: one parser means one set of field names, defaults and error
    messages, whichever file the operator is editing."""
    if not isinstance(raw, dict):
        return None
    try:
        return EmbedRecipe(
            url=str(raw["url"]).rstrip("/"),
            api=str(raw.get("api", "openai")),
            model=str(raw["model"]),
            dtype=str(raw.get("dtype", "f16")),
            ctx=int(raw.get("ctx", 512)),
            dim=int(raw["dim"]) if raw.get("dim") is not None else None,
            query_url=(str(raw["query_url"]).rstrip("/")
                        if raw.get("query_url") else None),
            concurrency=max(1, int(raw.get("concurrency", 1))),
            device=(str(raw["device"]).lower()
                    if raw.get("device") in ("gpu", "cpu", "GPU", "CPU")
                    else None),
        )
    except KeyError as missing:
        raise ValueError(f"{where}: [embed] block requires url and model "
                          f"(missing {missing})") from None


def default_recipe_for_plugin(plugin: str) -> EmbedRecipe | None:
    """The node's default recipe for a PLUGIN, from platform.toml's
    [embed_defaults.<plugin>] — or None when the node declares none.

    Why per plugin: a genome collection embedded with the node's text
    encoder is not a worse choice, it is a wrong one — DNA is not a
    language, and the two spaces are not comparable. The endpoint URL is
    deployment-specific (someone's LAN), so it belongs in Tier-2 node
    config, never in this repo and never in a plugin's source.

    Tier-2 file, not env and not the DB: it is desired state (invariant 10)."""
    from . import platform_config
    raw = platform_config.load().embed_defaults.get(plugin)
    return parse_recipe(raw, where=f"platform.toml [embed_defaults.{plugin}]")


def recipe_for(collection) -> EmbedRecipe:
    """THE way to resolve a collection's embedding recipe.

    Order: the collection's own [embed] block, then the node's per-plugin
    default, then the Tier-1 env recipe. Each step is a fallback for the
    one before it, never an override of it (invariant 10)."""
    if collection is not None and getattr(collection, "embed", None) is not None:
        return collection.embed
    if collection is not None and getattr(collection, "plugin", None):
        default = default_recipe_for_plugin(collection.plugin)
        if default is not None:
            return default
    return env_recipe()


# What the worker may SPEND on a collection's local encoder (never what
# retrieval uses — amendment 6). `embed_mode` in the collection TOML is the
# Tier-2 desired state; the Library's override is Tier 3 and lasts until the
# worker next applies the TOML (restart, or a TOML edit) — invariant 10.
EMBED_MODES = ("off", "lazy", "all")
DEFAULT_EMBED_MODE = "lazy"


@dataclass(frozen=True)
class Collection:
    name: str
    plugin: str
    profile: str | None
    embed_mode_default: str | None      # TOML `embed_mode`; None = DEFAULT_EMBED_MODE
    corpus_dir: Path
    manifest_path: Path
    fts_dir: Path
    lance_dir: Path
    # app/acquire/ — both default off/unset so an existing hand-populated
    # deployment (no collections/*.toml, or a TOML predating P0.4) sees zero
    # behavior change and no surprise outbound network access on restart.
    acquire_enabled: bool = False
    license_ack: str | None = None   # "<iso8601>/<sha256-of-terms-text>", Tier-2 only (invariant 10)
    embed: EmbedRecipe | None = None  # [embed] block; None = env fallback
    # Declarative reuse of published embeddings (§8 genesis): the worker
    # drains this archive automatically before spending GPU on backfill.
    genesis_archive: str | None = None
    # Opted-in EXTRA formats from the acquisition source (spec.extras) —
    # a LOCAL convenience (gutenberg: epubs beside the text), never part
    # of the p2p exchange. TOML: extras = ["epub"].
    extras: tuple[str, ...] = ()
    # PEER PLANE: a magnet URI or .torrent path. Set, acquisition fetches
    # this collection's corpus from a peer's torrent instead of the origin
    # — same plugin, profiles, license gate; the torrent's piece hashes
    # stand in for origin checksums (engine.effective_spec). TOML:
    # peer_source = "magnet:?xt=...&x.pe=host:port".
    peer_source: str | None = None
    # WHICH dated snapshot of the origin this collection holds, for sources
    # that publish them (Wikipedia dumps, a PubMed baseline year).
    #
    # None means "whatever the origin calls latest", which is what every
    # collection did before this existed and is deliberately still the
    # default — but it is NOT reproducible: the same URL serves different
    # bytes over time, so a corpus fetched under it cannot be torrented,
    # cached, adopted, or compared with a peer's, and the collection cannot
    # answer the one question a freshness feature has to ask, which is "how
    # old am I?". Pinning a date fixes all of that at the cost of an
    # explicit decision to move.
    snapshot: str | None = None
    # Chunker LAYERS (amendment 41): None = the one implicit layer, the
    # plugin's chunker — every collection that predates layers. TOML:
    # [[layers]] chunker = "verbatim" rule = "archive:<id>" ... last "default".
    layers: tuple | None = None
    # THE CODEX, when this collection is run from one (07 §7.8's `codex`
    # key). None for every legacy TOML, which is every collection that
    # predates step 8 — so nothing that does not name a Codex changes.
    # Where it is set it is the IDENTITY: `space_id`, layers, plugin and
    # licence all come from it, and `spaces.validate_boot` pins the vector
    # table against `codex.space_id` rather than the legacy preimage.
    codex: Any = None

    def plugin_instance(self) -> CorpusSource:
        if self.plugin == "packed":
            # a pack's identity (origin plugin, license, chunker) is in its
            # manifest, so the instance is per collection, not the registry's
            return packed.for_pack(self.corpus_dir)
        return PLUGIN_REGISTRY[self.plugin]


def _default_gutenberg_collection() -> Collection:
    return Collection(
        name="gutenberg",
        plugin="gutenberg",
        profile=None,
        embed_mode_default=None,
        corpus_dir=config.CORPUS_DIR,
        manifest_path=config.DATA_DIR / "manifest.db",
        fts_dir=config.DATA_DIR / "fts",
        lance_dir=config.DATA_DIR / "lancedb",
        acquire_enabled=False,
        license_ack=None,
    )


def _config_dir() -> Path:
    return Path(os.getenv("CONFIG_DIR", "."))


def louise_root() -> Path:
    """`<LOUISE_ROOT>` — the one directory of 07 §7.8, whose
    `collections/<name>/` holds a Codex collection's drag, state, index
    and vector table. Defaults to `DATA_DIR` so a node that never sets it
    behaves exactly as before."""
    import os
    value = os.getenv("LOUISE_ROOT")
    return Path(value) if value else config.DATA_DIR


def _codex_collection(path: Path, raw: dict[str, Any], name: str) -> Collection:
    """A collection the worker runs FROM ITS CODEX (07 §7.8).

    The bridge between the `hold` tree and the running node, and the one
    piece that stood between "migrated" and "in use": step 8 produced
    complete, verified Codex collections under
    `<LOUISE_ROOT>/collections/<name>/` that nothing in the worker could
    see, because `Collection` was built entirely from a legacy TOML's
    `[paths]` block.

    A TOML that names `codex` gets §7.8's tree instead — `state/`,
    `index/`, `lance/` beside the drag — and its plugin, layers and
    corpus come from the Codex rather than from the file, because the
    Codex is the identity and a file that could disagree with it would be
    a second answer to the same question.

    THE HASH IS CHECKED, not trusted. §7.8: a collection whose
    `drag/codex.json` has an inner hash other than its file's `codex` "is
    not served, not shared and not searched". That is a per-collection
    refusal, so it is raised here and the caller keeps its other
    collections.
    """
    from .hold import codex as cx

    want = str(raw["codex"])
    want = want if want.startswith("sha256:") else "sha256:" + want
    d = louise_root() / "collections" / name
    p = d / "drag" / "codex.json"
    if not p.is_file():
        raise ValueError(
            f"{path}: names codex {want[:23]}… and there is no {p}. A "
            f"collection's Codex is the first line of its drag (07 §7.8); "
            f"without it there is nothing to run")
    codex = cx.loads(p.read_bytes())
    if codex.collection_id != want:
        raise ValueError(
            f"{path}: names codex {want[:23]}… and {p} is "
            f"{codex.collection_id[:23]}…. A name holds one collection for "
            f"its life (07 §7.8); this one is not served, shared or searched "
            f"until they agree")

    corpus = Path(raw["corpus_dir"]) if raw.get("corpus_dir") else config.CORPUS_DIR
    embed_mode = _embed_mode(raw.get("embed", raw.get("embed_mode")), path)
    from . import layers as layers_mod
    return Collection(
        name=name,
        # FROM THE CODEX, not the file: plugin and layers are inside
        # `collection_id`, so a TOML that set them could name a collection
        # and then chunk it differently — invariant 1's failure mode, and
        # undetectable, because both peers computed the id honestly.
        plugin=codex.plugin,
        profile=raw.get("profile"),
        embed_mode_default=embed_mode,
        corpus_dir=corpus,
        manifest_path=d / "state" / "manifest.sqlite",
        fts_dir=d / "index",
        lance_dir=d / "lance",
        acquire_enabled=bool(raw.get("acquire_enabled", False)),
        license_ack=_codex_ack(codex, d / "state"),
        embed=parse_recipe(raw.get("embed_recipe") or raw.get("encoder"),
                           where=str(path)),
        genesis_archive=raw.get("genesis_archive"),
        extras=tuple(raw.get("extras") or ()),
        snapshot=(str(raw["snapshot"]) if raw.get("snapshot") else None),
        layers=layers_mod.layers_for(codex),
        codex=codex,
    )


#: 07 §7.8 spells the middle mode `on-demand`; the code has always called
#: it `lazy`, and `EMBED_MODES` is stored in the runtime-override table and
#: read by the worker and the UI. The documents fix the vocabulary
#: (CLAUDE.md), so a Codex TOML is written in THEIR word and canonicalised
#: here rather than the rename being done under a running node mid-
#: migration. Retiring `lazy` belongs with the other old names, step 12.
_EMBED_MODE_ALIASES = {"on-demand": "lazy"}


def _embed_mode(value, path: Path) -> str | None:
    if value is None:
        return None
    mode = _EMBED_MODE_ALIASES.get(str(value), str(value))
    if mode not in EMBED_MODES:
        raise ValueError(
            f"{path}: embed must be one of off | on-demand | all "
            f"(07 §7.8), got {value!r}")
    return mode


def _codex_ack(codex, state_dir: Path) -> str | None:
    """The acknowledgement `hold collection ack` recorded (09 §9.13),
    in the shape `licensing.state_for` compares — read, never decided
    here; `app/licensing.py` is THE resolution."""
    if codex.license.cls == "green":
        return None
    import json
    p = state_dir / "ack.json"
    if not p.is_file():
        return None
    try:
        ack = json.loads(p.read_text(encoding="utf-8"))
        return f"{ack['time']}/{ack['terms_sha256'].replace('sha256:', '')}"
    except (OSError, ValueError, KeyError):
        return None


def _load_one(path: Path) -> Collection:
    with open(path, "rb") as f:
        raw: dict[str, Any] = tomllib.load(f)
    name = raw.get("name") or path.stem
    if raw.get("codex"):
        return _codex_collection(path, raw, name)
    plugin = raw.get("plugin", name)
    paths = raw.get("paths", {})

    def _resolve(key: str, default: Path) -> Path:
        value = paths.get(key)
        return Path(value) if value else default

    corpus_dir = Path(raw["corpus_dir"]) if raw.get("corpus_dir") else config.CORPUS_DIR

    embed = parse_recipe(raw.get("embed"), where=str(path))
    from . import layers as layers_mod
    layers = layers_mod.parse_layers(raw.get("layers"), where=str(path))
    embed_mode = raw.get("embed_mode")
    if embed_mode is not None and embed_mode not in EMBED_MODES:
        raise ValueError(f"{path}: embed_mode must be one of "
                         f"{' | '.join(EMBED_MODES)}, got {embed_mode!r}")

    return Collection(
        name=name,
        plugin=plugin,
        profile=raw.get("profile"),
        embed_mode_default=embed_mode,
        corpus_dir=corpus_dir,
        manifest_path=_resolve("manifest", config.DATA_DIR / "manifest.db"),
        fts_dir=_resolve("fts", config.DATA_DIR / "fts"),
        lance_dir=_resolve("lancedb", config.DATA_DIR / "lancedb"),
        acquire_enabled=bool(raw.get("acquire_enabled", False)),
        license_ack=raw.get("license_ack"),
        embed=embed,
        genesis_archive=raw.get("genesis_archive"),
        extras=tuple(raw.get("extras") or ()),
        # one magnet/.torrent, or several (a TOML array, or one per line):
        # the per-language torrents of a pre-embedded corpus (STATUS 5h)
        peer_source=(("\n".join(str(x) for x in raw["peer_source"])
                      if isinstance(raw.get("peer_source"), list)
                      else str(raw["peer_source"])) if raw.get("peer_source") else None),
        snapshot=(str(raw["snapshot"]) if raw.get("snapshot") else None),
        layers=layers,
    )


def config_toml_path(name: str) -> Path:
    """Where a collection's Tier-2 definition file lives (whether or not it
    exists yet). The file existing IS the collection existing — creation and
    removal act on this path, never on a database row."""
    return _config_dir() / "collections" / f"{name}.toml"


def collections_dir() -> Path:
    return _config_dir() / "collections"


def load_all() -> list[Collection]:
    """The collections this node has. TWO modes, decided by whether the
    `collections/` directory EXISTS:

      absent   — the legacy single-collection deployment: one synthesized
                 default (gutenberg over CORPUS_DIR/DATA_DIR), no TOML.
      present  — TOML mode: exactly the TOMLs in it, possibly NONE. An
                 empty directory is a node with zero collections, not a
                 legacy node: the synthesized default used to reappear the
                 moment the last TOML was removed (Louise2, 2026-08-26 —
                 "cannot delete all collections") and could not be removed
                 again from the UI, because it has no file to retire.
    """
    cfg_dir = collections_dir()
    if not cfg_dir.is_dir():
        return [_default_gutenberg_collection()]
    return [_load_one(p) for p in sorted(cfg_dir.glob("*.toml"))]


def is_synthesized(collection) -> bool:
    """True for the legacy default: no TOML, no collections/ directory."""
    return not collections_dir().is_dir() and not config_toml_path(collection.name).exists()


def default_collection() -> Collection:
    """The first collection load_all() returns — used wherever no explicit
    collection is given (single-collection call sites predating P0.5's
    federation), and as the CONTROL manifest (worker state, seeding table).
    With zero collections the control manifest falls back to the legacy
    default's path (DATA_DIR/manifest.db) so the node keeps running; that
    collection is NOT listed anywhere and no pipeline runs on it."""
    collections = load_all()
    return collections[0] if collections else _default_gutenberg_collection()


def by_name(name: str) -> Collection | None:
    """A collection is available for query federation simply by existing in
    load_all() — there's no separate "enabled" flag (unlike acquire_enabled,
    which guards a real side effect: outbound network, disk, license gates).
    Querying an already-ingested collection has no equivalent hazard, so an
    operator opts a collection out of federation the same way they opt it
    out of existing at all today: by not shipping its collections/*.toml."""
    for c in load_all():
        if c.name == name:
            return c
    return None
