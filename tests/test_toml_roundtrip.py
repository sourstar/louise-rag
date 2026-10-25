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
from tests.hold.conftest import fake_query_embed

from app import collections as collections_mod
from app.web import _codex_id, _write_collection_toml


def write(path, c):
    """Rewrite a collection exactly as the app's rewrite paths do."""
    _write_collection_toml(
        path, codex=_codex_id(c), plugin=c.plugin, profile=c.profile,
        acquire_enabled=c.acquire_enabled, corpus_dir=c.corpus_dir,
        manifest=c.manifest_path, fts=c.fts_dir, lancedb=c.lance_dir,
        license_ack=c.license_ack, genesis_archive=c.genesis_archive,
        embed=c.embed, embed_mode=c.embed_mode_default, layers=c.layers,
        snapshot=c.snapshot, extras=c.extras, peer_source=c.peer_source,
        title=c.title, slices=c.slices, kinds=c.kinds, share=c.share,
        reranker=c.reranker, source=c.source,
        share_slices=c.share_slices, share_kinds=c.share_kinds)


# ---- the Codex half -------------------------------------------------------
#
# Until 2026-10-08 a legacy (pre-Codex) collection was round-tripped here
# too; with that shape gone, a node's every file names a Codex, so the
# Codex collection is the only one a rewrite can meet. It was also the one
# the legacy half left untested: there `codex` was None on both sides of
# every assertion and the parametrized guard passed on it vacuously — how
# the live failure got through.

@pytest.fixture
def codex_loaded(tmp_path, isolated_config, monkeypatch):
    """A migrated collection and its 07 §7.8 file, loaded back."""
    from app.hold import collection as col
    from tests.hold.conftest import KEY, NOW, PLATFORM, fake_embed
    from tests.hold.test_template import recipe

    root = tmp_path / "louise"
    (root / "collections").mkdir(parents=True)
    cx = col.create_from_recipe(
        root / "collections", "gutenberg", recipe(), platform_image=PLATFORM,
        embed=fake_embed, embed_query=fake_query_embed, name="gutenberg", key=KEY, now=NOW)

    cfg = tmp_path / "config"
    (cfg / "collections").mkdir(parents=True, exist_ok=True)
    path = cfg / "collections" / "gutenberg.toml"
    path.write_text(
        f'codex = "{cx.collection_id}"\n'
        # 07 §7.8's own keys, each away from its default: the guards
        # compared None with None on all four until 2026-09-15
        'title = "Project Gutenberg"\n'
        'slices = "all"\n'
        'kinds = ["records"]\n'
        'share = true\n'
        'embed = "on-demand"\n'
        # no `acquire_enabled = true`: with `source = "shard"` the SHARD IS the
        # corpus and the loader refuses acquiring into it (2026-10-07: the
        # second peer's worker began listing the Gutenberg mirror).
        'corpus_dir = "/corpus"\n'
        # 07 §7.8 `source` (2026-10-07): away from its default too, so the
        # guards exercise it — a fetched collection reads its SHARD
        'source = "shard"\n'
        # 07 §7.8 share_slices / share_kinds (2026-10-07): away from None too
        'share_slices = ["lang=en"]\n'
        'share_kinds = ["records"]\n'
        '\n[encoder]\n'
        # the space's own values (tests/hold/test_template.py's recipe):
        # still accepted where they agree with the Codex, never written back
        'url = "http://enc:8080"\napi = "openai"\nembed_model = "m"\nquery_model = "m"\n'
        'dtype = "f16"\nctx = 512\ndim = 768\n'
        '\n[reranker]\nurl = "http://rr:30447/v1/rerank"\napi = "jina"\n'
        'rerank_model = "bge-reranker-v2-m3"\n', encoding="utf-8")

    monkeypatch.setenv("LOUISE_ROOT", str(root))
    monkeypatch.setenv("CONFIG_DIR", str(cfg))
    monkeypatch.setattr(collections_mod.config, "DATA_DIR", root)
    return path, collections_mod._load_one(path), cx


@pytest.fixture
def origin_loaded(codex_loaded):
    """The same collection read from its origin, so the keys `source =
    "shard"` refuses are away from their defaults too: acquisition on, an
    extra format fetched beside the text."""
    path, _, cx = codex_loaded
    path.write_text(
        f'codex = "{cx.collection_id}"\n'
        'acquire_enabled = true\n'
        'extras = ["epub"]\n'
        'corpus_dir = "/corpus"\n'
        '\n[encoder]\nurl = "http://enc:8080"\napi = "openai"\nembed_model = "m"\nquery_model = "m"\n', encoding="utf-8")
    return path, collections_mod._load_one(path), cx


def test_a_codex_rewrite_changes_nothing(codex_loaded):
    path, before, _ = codex_loaded
    write(path, before)
    assert collections_mod._load_one(path) == before


def test_the_codex_id_survives_a_rewrite(codex_loaded):
    """THE regression. An embed-mode change in the Library re-emitted the
    file without this key, which silently moved the collection back to the
    legacy load path — and took its layer params with it."""
    path, before, cx = codex_loaded
    write(path, before)
    text = path.read_text(encoding="utf-8")
    assert f'codex = "{cx.collection_id}"' in text
    after = collections_mod._load_one(path)
    assert after.codex is not None
    assert after.codex.collection_id == cx.collection_id


def test_a_rewrite_never_writes_a_second_answer(codex_loaded):
    """07 §7.8: plugin, layers and the tree come from the Codex. A file
    that also stated them could disagree with the id it carries, and both
    halves would still be internally consistent — invariant 1's failure
    mode, which is undetectable from the outside."""
    path, before, _ = codex_loaded
    write(path, before)
    text = path.read_text(encoding="utf-8")
    for forbidden in ("[[layers]]", "[paths]", "plugin =", "[embed]",
                      "dtype =", "ctx =", "dim ="):
        assert forbidden not in text, \
            f"{forbidden!r} restates what the Codex already fixes"


def test_the_layer_window_still_comes_from_the_codex(codex_loaded):
    """What the 10,212 errors were: a params-less `fixed` layer, which
    `chunk_fixed` refuses rather than guessing a window."""
    path, before, _ = codex_loaded
    write(path, before)
    after = collections_mod._load_one(path)
    assert after.layers, "the rewrite left the collection with no layers"
    for layer in after.layers:
        if layer.chunker == "fixed":
            assert layer.params.get("tokens") and layer.params.get("overlap"), \
                "a `fixed` layer without params errors every document"


def test_the_mode_is_written_in_the_documents_word(codex_loaded):
    """07 §7.8 spells it `on-demand`; the stored value is `lazy`. The file
    is the document's word, and it must load back to the stored one."""
    path, before, _ = codex_loaded
    assert before.embed_mode_default == "lazy"
    write(path, before)
    assert 'embed = "on-demand"' in path.read_text(encoding="utf-8")
    assert collections_mod._load_one(path).embed_mode_default == "lazy"


def test_the_reranker_window_and_the_sources_survive_a_rewrite(codex_loaded):
    """Both were dropped by every rewrite until 2026-10-25: the `[reranker]`
    `window` (05 §5.6, amended 2026-10-21) came back as the default, and a
    deposit collection's `[[sources]]` (06 §6.7) vanished — the guards above
    compare `Collection` fields, and the subscriptions are not one."""
    import tomllib
    path, _, _ = codex_loaded
    path.write_text(path.read_text(encoding="utf-8") + "window = 1024\n"
                    '\n[[sources]]\nurl = "https://example.org/a\\\\b.xml"\ninterval = 60\n',
                    encoding="utf-8")
    before = collections_mod._load_one(path)
    assert before.reranker.window == 1024
    write(path, before)
    assert collections_mod._load_one(path) == before
    assert tomllib.loads(path.read_text(encoding="utf-8"))["sources"] == [
        {"url": "https://example.org/a\\b.xml", "interval": 60}]


def test_codex_rewrites_are_idempotent(codex_loaded):
    path, before, _ = codex_loaded
    write(path, before)
    once = path.read_text(encoding="utf-8")
    write(path, collections_mod._load_one(path))
    assert path.read_text(encoding="utf-8") == once


@pytest.mark.parametrize("field", [
    f.name for f in dataclasses.fields(collections_mod.Collection)
    if f.name not in ("name",)
])
def test_every_field_survives_a_codex_rewrite(codex_loaded, field):
    """The guard again, this time against a collection that HAS a Codex."""
    path, before, _ = codex_loaded
    write(path, before)
    after = collections_mod._load_one(path)
    assert getattr(after, field) == getattr(before, field), (
        f"{field!r} did not survive a Codex TOML rewrite")


@pytest.mark.parametrize("field", [
    f.name for f in dataclasses.fields(collections_mod.Collection)
    if f.name not in ("name",)
])
def test_every_field_survives_an_origin_rewrite(origin_loaded, field):
    path, before, _ = origin_loaded
    write(path, before)
    after = collections_mod._load_one(path)
    assert getattr(after, field) == getattr(before, field), (
        f"{field!r} did not survive a Codex TOML rewrite")


# ---- the guards themselves ------------------------------------------------
#
# The parametrized guards compare a field before and after a rewrite, so a
# field both fixtures leave at its default passes them however the writer
# treats it. `reranker` did exactly that from the day it was added until
# 2026-09-15, when an ordinary rewrite dropped pubmed's.

# fields no fixture can populate, each with the reason
_UNPOPULATED: dict[str, str] = {
    "license_ack": "the test Codex is green, and a green collection records no "
                   "acknowledgement (`_codex_ack`); an ack is state/ack.json, not the file",
    "genesis_archive": "the test Codex credits no archive, and the loader refuses one "
                       "it does not credit (03 §3.5)",
    "snapshot": "the test Codex's origin pins no dump (09 §9.17), and the loader "
                "refuses a snapshot the Codex does not pin",
    "peer_source": "`_codex_collection` never reads it: 07 §7.8's file has no "
                   "such key, and since 2026-10-08 every file is a Codex file",
    "embed_mode_note": "derived at load — `embed = \"p2p\"` with `share = false` runs as "
                       "`all` and this says so (07 §7.8, 2026-10-19); never a file key",
}


def test_the_guards_are_not_vacuous(codex_loaded, origin_loaded):
    _, codexed, _ = codex_loaded
    _, origin, _ = origin_loaded
    vacuous = []
    for f in dataclasses.fields(collections_mod.Collection):
        if f.default is not dataclasses.MISSING:
            default = f.default
        elif f.default_factory is not dataclasses.MISSING:
            default = f.default_factory()
        else:
            continue                    # required: always populated
        if getattr(codexed, f.name) == default and getattr(origin, f.name) == default                 and f.name not in _UNPOPULATED:
            vacuous.append(f.name)
    assert not vacuous, (
        f"{vacuous} sit at their defaults in both fixtures, so the round-trip "
        f"guards pass on them whatever the writer does — declare them in a "
        f"fixture, or list them in _UNPOPULATED with the reason")
