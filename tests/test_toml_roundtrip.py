"""A collection's TOML must survive a rewrite unchanged.

Tier-2 TOML is DESIRED STATE (invariant 10), and half a dozen ordinary
actions rewrite it: acknowledging a licence, changing embed_mode, enabling
acquisition, creating another collection, `embed materialize`. Every one of
those passes a fixed argument list to `_write_collection_toml`, so a field
nobody remembered to pass is not preserved — it is DELETED, silently, by an
action that looks unrelated.

That is not hypothetical. `layers` was added by amendment 41 and never added
to the writer, so any of those actions would have dropped it — and layers
are part of `space_id` (invariant 1 as amended), which means the rewrite
would not merely lose a setting, it would redefine the collection's SPACE
and orphan every vector already stored under it. `peer_source` was missing
from the licence-ack path for the same reason.

The last test here is the one that matters long-term: it walks the
Collection dataclass and fails when a NEW field appears that the round-trip
does not cover, so the next amendment cannot repeat this quietly.
"""
import dataclasses

import pytest

from app import collections as collections_mod
from app.web import _write_collection_toml


def write(path, c):
    """Rewrite a collection exactly as the app's rewrite paths do."""
    _write_collection_toml(
        path, plugin=c.plugin, profile=c.profile,
        acquire_enabled=c.acquire_enabled, corpus_dir=c.corpus_dir,
        manifest=c.manifest_path, fts=c.fts_dir, lancedb=c.lance_dir,
        license_ack=c.license_ack, genesis_archive=c.genesis_archive,
        embed=c.embed, embed_mode=c.embed_mode_default, layers=c.layers,
        snapshot=c.snapshot, extras=c.extras, peer_source=c.peer_source)


@pytest.fixture
def loaded(tmp_path, isolated_config, monkeypatch):
    """Build a TOML with every field populated, then load it back."""
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    (tmp_path / "collections").mkdir(parents=True, exist_ok=True)
    path = tmp_path / "collections" / "rich.toml"
    path.write_text(
        'plugin = "wikipedia"\n'
        'profile = "all"\n'
        'embed_mode = "all"\n'
        'snapshot = "20260801"\n'
        'acquire_enabled = true\n'
        'license_ack = "2026-08-01T00:00:00Z/' + "a" * 64 + '"\n'
        'genesis_archive = "upstash-wikipedia-2024-06"\n'
        'extras = ["epub"]\n'
        'peer_source = "magnet:?xt=urn:btih:abc"\n'
        'corpus_dir = "/corpora/wikipedia"\n'
        '\n[embed]\n'
        'url = "http://enc:8080"\napi = "openai"\nmodel = "m"\n'
        'dtype = "f16"\nctx = 1024\nconcurrency = 4\n'
        '\n[[layers]]\nchunker = "verbatim"\nrule = "archive:upstash-wikipedia-2024-06"\n'
        '\n[[layers]]\nchunker = "cdc"\nrule = "default"\n'
        '\n[paths]\n'
        'manifest = "/data/w/manifest.db"\nfts = "/data/w/fts"\n'
        'lancedb = "/data/w/lancedb"\n', encoding="utf-8")
    return path, collections_mod._load_one(path)


def test_a_rewrite_changes_nothing(loaded):
    """The whole property, in one assertion."""
    path, before = loaded
    write(path, before)
    assert collections_mod._load_one(path) == before


def test_layers_survive_a_rewrite(loaded):
    """Layers are part of space_id: dropping them redefines the space and
    orphans every vector stored under it."""
    path, before = loaded
    assert before.layers is not None and len(before.layers) == 2
    write(path, before)
    after = collections_mod._load_one(path)
    assert after.layers == before.layers
    assert [l.chunker for l in after.layers] == ["verbatim", "cdc"]
    assert [l.rule for l in after.layers] == \
        ["archive:upstash-wikipedia-2024-06", "default"]


def test_the_layer_version_is_derived_not_written(loaded):
    """`version` comes from the chunker, so writing it back would let a
    stale number outlive a chunker bump."""
    path, before = loaded
    write(path, before)
    assert "version" not in path.read_text(encoding="utf-8")


def test_repeated_rewrites_are_idempotent(loaded):
    path, before = loaded
    write(path, before)
    once = path.read_text(encoding="utf-8")
    write(path, collections_mod._load_one(path))
    assert path.read_text(encoding="utf-8") == once


@pytest.mark.parametrize("field", [
    f.name for f in dataclasses.fields(collections_mod.Collection)
    if f.name not in ("name",)          # name comes from the file's stem
])
def test_every_collection_field_survives(loaded, field):
    """The guard against the NEXT amendment. A field added to Collection
    that the writer does not emit fails here, instead of being discovered
    when a licence acknowledgement silently un-declares a collection."""
    path, before = loaded
    write(path, before)
    after = collections_mod._load_one(path)
    assert getattr(after, field) == getattr(before, field), (
        f"{field!r} did not survive a TOML rewrite — add it to "
        f"_write_collection_toml and to every call site that rewrites an "
        f"existing collection")
