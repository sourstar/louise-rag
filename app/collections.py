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
from .plugins import gutenberg, pubmed
from .plugins.base import CorpusSource

PLUGIN_REGISTRY: dict[str, CorpusSource] = {
    "gutenberg": gutenberg.SOURCE,
    "pubmed": pubmed.SOURCE,
}


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
