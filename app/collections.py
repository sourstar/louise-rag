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
from .plugins import genome, gutenberg, pubmed, wikipedia
from .plugins.base import CorpusSource

PLUGIN_REGISTRY: dict[str, CorpusSource] = {
    "genome": genome.SOURCE,
    "gutenberg": gutenberg.SOURCE,
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


def env_recipe() -> EmbedRecipe:
    """The Tier-1 fallback, built at call time so env/monkeypatched config
    changes are always honored."""
    return EmbedRecipe(url=config.EMBED_URL, api=config.EMBED_API,
                        model=config.EMBED_MODEL, dtype=config.EMBED_DTYPE,
                        ctx=config.EMBED_CTX)


def recipe_for(collection) -> EmbedRecipe:
    """THE way to resolve a collection's embedding recipe. None (or a
    collection without an [embed] block) falls back to the env recipe."""
    if collection is not None and getattr(collection, "embed", None) is not None:
        return collection.embed
    return env_recipe()


@dataclass(frozen=True)
class Collection:
    name: str
    plugin: str
    profile: str | None
    embed_mode_default: str | None
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

    def plugin_instance(self) -> CorpusSource:
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


def _load_one(path: Path) -> Collection:
    with open(path, "rb") as f:
        raw: dict[str, Any] = tomllib.load(f)
    name = raw.get("name") or path.stem
    plugin = raw.get("plugin", name)
    paths = raw.get("paths", {})

    def _resolve(key: str, default: Path) -> Path:
        value = paths.get(key)
        return Path(value) if value else default

    corpus_dir = Path(raw["corpus_dir"]) if raw.get("corpus_dir") else config.CORPUS_DIR

    embed = None
    if isinstance(raw.get("embed"), dict):
        e = raw["embed"]
        try:
            embed = EmbedRecipe(
                url=str(e["url"]).rstrip("/"),
                api=str(e.get("api", "openai")),
                model=str(e["model"]),
                dtype=str(e.get("dtype", "f16")),
                ctx=int(e.get("ctx", 512)),
                dim=int(e["dim"]) if e.get("dim") is not None else None,
                query_url=(str(e["query_url"]).rstrip("/")
                            if e.get("query_url") else None),
                concurrency=max(1, int(e.get("concurrency", 1))),
            )
        except KeyError as missing:
            raise ValueError(
                f"{path}: [embed] block requires url and model "
                f"(missing {missing})") from None

    return Collection(
        name=name,
        plugin=plugin,
        profile=raw.get("profile"),
        embed_mode_default=raw.get("embed_mode"),
        corpus_dir=corpus_dir,
        manifest_path=_resolve("manifest", config.DATA_DIR / "manifest.db"),
        fts_dir=_resolve("fts", config.DATA_DIR / "fts"),
        lance_dir=_resolve("lancedb", config.DATA_DIR / "lancedb"),
        acquire_enabled=bool(raw.get("acquire_enabled", False)),
        license_ack=raw.get("license_ack"),
        embed=embed,
        genesis_archive=raw.get("genesis_archive"),
    )


def config_toml_path(name: str) -> Path:
    """Where a collection's Tier-2 definition file lives (whether or not it
    exists yet). The file existing IS the collection existing — creation and
    removal act on this path, never on a database row."""
    return _config_dir() / "collections" / f"{name}.toml"


def load_all() -> list[Collection]:
    cfg_dir = _config_dir() / "collections"
    tomls = sorted(cfg_dir.glob("*.toml")) if cfg_dir.is_dir() else []
    if not tomls:
        return [_default_gutenberg_collection()]
    return [_load_one(p) for p in tomls]


def default_collection() -> Collection:
    """The first collection load_all() returns — used wherever no explicit
    collection is given (single-collection call sites predating P0.5's
    federation)."""
    return load_all()[0]


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
