"""tests/test_layers_archive_rule.py — where an archive's coverage comes from.

06 §6.3: the documents a pre-embedded archive covers are "read from that
archive's `coverage` in the Codex's `credits` (02 §2.7), so the rule is
computable from the record and the Codex alone, and A PLATFORM BUILD
CANNOT MOVE A DOCUMENT BETWEEN LAYERS".

That clause is load-bearing. `layers` is inside `space_id`, so two peers
agree on the space; if they then read coverage from their own platform
builds they can put one document in different layers, cut it with
different chunkers, and produce different vectors under one space id —
undetectably, because both computed the id the document prescribes.

The bug was DORMANT until now: `layer_index` short-circuits at one layer,
and every collection on this node has one. Wikipedia as one collection
with two layers (operator decision, 2026-09-09) is the first to reach it.
"""
from dataclasses import dataclass, field

import pytest

from app import layers as layers_mod
from app.layers import Layer


@dataclass
class Credit:
    archive: str
    coverage: object


@dataclass
class Coll:
    """Enough of a collection for the rule: what it declares, and nothing
    from the platform."""
    layers: tuple = ()
    credits: tuple = ()
    plugin: str = "wikipedia"


WIKI = "upstash-wikipedia-2024-06"

TWO_LAYERS = (Layer("verbatim", 1, f"archive:{WIKI}"),
              Layer("cdc", 1, "default", {"target": 4000, "min": 1000, "max": 8000}))


def test_the_codex_wins_over_the_platform_registry():
    """The strongest form of 06 §6.3's clause, and the reason it exists.

    This archive IS in the platform registry, so both sources can answer.
    The Codex declares a DIFFERENT coverage, and the Codex must win — if
    the platform's answer could override it, then a peer on another build
    would put the same article in another layer, and the whole two-layer
    design would produce different vectors under one `space_id`.
    """
    from app.bundles.genesis import ARCHIVES
    assert WIKI in ARCHIVES, "the fixture is only meaningful if both can answer"

    c = Coll(layers=TWO_LAYERS,
             credits=(Credit(WIKI, {"ids_per_shard": 1_000_000, "shards": 37}),))
    assert layers_mod.layer_of(c, 5).chunker == "verbatim", "inside coverage"
    assert layers_mod.layer_of(c, 36_999_999).chunker == "verbatim", "last covered"
    assert layers_mod.layer_of(c, 37_000_000).chunker == "cdc", "past the archive"


def test_a_narrower_codex_coverage_is_honoured_against_a_wider_platform_one():
    """The same point made so it cannot pass by coincidence: the Codex says
    two shards of a thousand, and ids beyond that fall to `default` no
    matter what the platform's registry holds for this archive."""
    c = Coll(layers=TWO_LAYERS,
             credits=(Credit(WIKI, {"ids_per_shard": 1000, "shards": 2}),))
    assert layers_mod.layer_of(c, 1999).chunker == "verbatim"
    assert layers_mod.layer_of(c, 2000).chunker == "cdc"
    assert layers_mod.layer_of(c, 5_000_000).chunker == "cdc"


def test_coverage_all_puts_every_document_in_the_archive_layer():
    c = Coll(layers=TWO_LAYERS, credits=(Credit(WIKI, "all"),))
    for nid in (0, 1, 10**9):
        assert layers_mod.layer_of(c, nid).chunker == "verbatim"


def test_a_document_past_the_coverage_falls_to_the_default_layer():
    """A post-snapshot article the archive never saw is chunked normally and
    embedded here — which is the entire point of two layers."""
    c = Coll(layers=TWO_LAYERS,
             credits=(Credit(WIKI, {"ids_per_shard": 1000, "shards": 2}),))
    assert layers_mod.layer_of(c, 1999).chunker == "verbatim"
    assert layers_mod.layer_of(c, 2000).chunker == "cdc"


def test_an_archive_the_codex_does_not_credit_matches_nothing():
    """06 §6.6 refuses an uncredited archive at creation; if one reaches the
    rule anyway it must not silently cover everything."""
    c = Coll(layers=TWO_LAYERS, credits=(Credit("some-other-archive", "all"),))
    assert layers_mod.layer_of(c, 5).chunker == "cdc"


def test_the_platform_registry_is_the_fallback_for_a_credit_less_collection():
    """Every collection on this node predates `credits`. They keep working
    exactly as before — the grandfathering is the same shape as `book`
    beside `fixed`."""
    from app.bundles.genesis import ARCHIVES
    known = next(iter(ARCHIVES), None)
    if known is None:
        pytest.skip("no archives registered in this build")
    c = Coll(layers=(Layer("verbatim", 1, f"archive:{known}"),
                     Layer("cdc", 1, "default")))
    assert c.credits == ()
    layers_mod.layer_of(c, 1)          # resolves through ARCHIVES, does not raise


def test_one_layer_never_consults_coverage_at_all():
    """Why the bug was dormant: `layer_index` short-circuits, so no
    single-layer collection has ever reached this code."""
    c = Coll(layers=(Layer("book", 1, "default"),))
    assert layers_mod.layer_of(c, 12345).chunker == "book"
