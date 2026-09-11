"""The three ways a layer decision could be made by the wrong thing.

All found by verification before the wikipedia collection was created,
and all of the same family: something reads a collection's declaration
from somewhere other than its Codex, and nothing detects the
disagreement because both halves are internally consistent. That is
invariant 1's failure mode, and 06 §6.3 names the defence — "the rule is
computable from the record and the Codex alone, and A PLATFORM BUILD
CANNOT MOVE A DOCUMENT BETWEEN LAYERS".
"""
from pathlib import Path

import pytest

from app import collections as collections_mod, layers
from app.hold import collection as col, records as rec
from tests.hold.conftest import KEY, NOW, PLATFORM, fake_embed
from tests.hold.test_template import recipe


@pytest.fixture
def made(tmp_path):
    root = tmp_path / "louise" / "collections"
    root.mkdir(parents=True)
    codex = col.create_from_recipe(
        root, "gutenberg", recipe(), platform_image=PLATFORM,
        embed=fake_embed, name="gutenberg", key=KEY, now=NOW)
    return root, codex


def test_the_codex_path_is_reachable_for_a_live_collection(made, tmp_path,
                                                            monkeypatch):
    """`_rule_matches` read `collection.credits`, and
    `app.collections.Collection` HAS NO SUCH FIELD — nothing in app/ sets
    one. So for every collection the node actually runs, the Codex branch
    was dead and coverage came from the platform registry instead. The
    tests passed because their fixtures set `credits` by hand.
    """
    root, codex = made
    cfg = tmp_path / "config" / "collections"
    cfg.mkdir(parents=True)
    (cfg / "gutenberg.toml").write_text(
        f'codex = "{codex.collection_id}"\n', encoding="utf-8")
    monkeypatch.setenv("LOUISE_ROOT", str(tmp_path / "louise"))
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr(collections_mod.config, "DATA_DIR", tmp_path / "louise")

    c = next(x for x in collections_mod.load_all() if x.name == "gutenberg")
    assert getattr(c, "credits", None) in (None, (), [])
    assert c.codex is not None
    # the hop that makes the Codex branch reachable at all
    assert layers._archive_credits(c) == layers._archive_credits(c.codex)


def test_the_import_collection_carries_the_codexs_layers(made):
    """`local_collection` built a Collection with no `layers` and no
    `codex`, so every stage reaching a collection through it cut documents
    with the PLUGIN's implicit chunker. Silent when they coincide — which
    they do for wikivec and pubmed — and it writes vectors under a
    `space_id` promising a different cut when they do not."""
    root, codex = made
    local = rec.local_collection(root, "gutenberg", codex)
    assert local.codex is codex
    assert [(l.chunker, l.version) for l in layers.layers_for(local)] == \
           [(l.chunker.id, l.chunker.version) for l in codex.layers]


def test_a_corpus_resident_archive_is_named_as_needing_a_corpus():
    """`hold vectors import --archive` passed corpus_dir=None, so an
    archive whose vectors live in the corpus resolved to nothing and
    imported nothing — silently. A fetched archive was unaffected, which
    is why it went unnoticed."""
    from app import louise
    assert louise.archive_is_local("upstash-wikipedia-2024-06") is True
    assert louise.archive_is_local("medcpt-pubmed") is False


def test_the_import_command_takes_a_corpus_directory():
    from app import louise
    a = louise.build_parser().parse_args(
        ["hold", "vectors", "import", "wv", "--archive",
         "upstash-wikipedia-2024-06", "--corpus", "/corpora/wikivec"])
    assert a.corpus == "/corpora/wikivec"
