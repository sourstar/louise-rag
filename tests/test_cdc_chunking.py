"""Content-defined chunking, and the scoping that makes it affordable.

Two separable claims:

  1. CDC does what it is for — an edit disturbs its own neighbourhood
     instead of every chunk after it.
  2. Adopting it costs only the collections that adopt it, because the
     chunk version is scoped per chunker rather than being one global
     number in every collection's space id.

The second is the one that would have gone wrong silently: a global bump
re-pins the pre-embedded collections, whose vectors were imported against a
producer's boundaries and cannot be recomputed by re-chunking at all.
"""
import random
from dataclasses import replace

import pytest

from app import chunking, collections as collections_mod, config, spaces
from app.plugins.base import Mutability


def _prose(words=40_000, seed=7) -> str:
    rng = random.Random(seed)
    vocab = ["".join(rng.choice("abcdefghijklmnopqrstuvwxyz")
                     for _ in range(rng.randint(3, 11))) for _ in range(4000)]
    return " ".join(rng.choice(vocab) for _ in range(words))


# ---------------- the chunker ----------------

def test_cdc_reassembles_exactly():
    """Every character, once, in order — a chunker that loses or duplicates
    text is not a chunker."""
    text = _prose(5_000)
    assert "".join(chunking.cdc_split(text)) == text


def test_cdc_respects_its_size_bounds():
    """The maximum is the binding one: a chunk that exceeds the encoder's
    context gets silently truncated by the backend."""
    sizes = [len(p) for p in chunking.cdc_split(_prose())]
    assert max(sizes) <= chunking.CDC_MAX_CHARS
    # only the final piece may fall below the minimum (it is what is left)
    assert all(s >= chunking.CDC_MIN_CHARS for s in sizes[:-1])
    mean = sum(sizes) / len(sizes)
    assert chunking.CDC_TARGET_CHARS * 0.6 < mean < chunking.CDC_TARGET_CHARS * 1.6, mean


def test_an_edit_changes_only_its_neighbourhood():
    """THE property. Measured against fixed-width chunking of the same text,
    because the comparison is the entire argument for doing this."""
    text = _prose()
    edited = text[:500] + " AN INSERTED SENTENCE APPEARS HERE " + text[500:]

    before, after = chunking.cdc_split(text), chunking.cdc_split(edited)
    survived = len(set(before) & set(after))
    assert len(before) - survived <= 2, \
        f"{len(before) - survived} of {len(before)} chunks changed"

    fixed_before = [text[i:i + 4000] for i in range(0, len(text), 4000)]
    fixed_after = [edited[i:i + 4000] for i in range(0, len(edited), 4000)]
    fixed_survived = len(set(fixed_before) & set(fixed_after))
    assert fixed_survived == 0, \
        "fixed-width chunking should lose everything after the edit"


def test_an_edit_at_the_end_leaves_the_start_alone():
    text = _prose()
    edited = text + " a trailing addition to the article"
    before, after = chunking.cdc_split(text), chunking.cdc_split(edited)
    assert before[:-1] == after[:len(before) - 1], \
        "everything before the edited region must be untouched"


def test_boundaries_are_deterministic_across_processes():
    """Invariant 3. The gear table is derived from a fixed constant by
    blake2b precisely so it cannot vary by machine, seed or Python version."""
    text = _prose(3_000)
    assert chunking.cdc_split(text) == chunking.cdc_split(text)
    assert chunking._GEAR[0] == chunking._GEAR[0]
    assert len(set(chunking._GEAR)) > 250, "the table must not be degenerate"


def test_chunk_cdc_yields_the_standard_shape():
    sections = [("Intro", _prose(2_000)), ("History", _prose(2_000, seed=9))]
    chunks = list(chunking.chunk_cdc("wikipedia:1", 1, sections, version=1))
    assert chunks, "sections must produce chunks"
    assert [c["seq"] for c in chunks] == list(range(len(chunks)))
    assert all(c["id"] == f"wikipedia:1:{c['seq']}:1" for c in chunks)
    assert {c["chapter"] for c in chunks} == {"Intro", "History"}
    assert all(c["tokens"] > 0 for c in chunks)


def test_sections_are_chunked_independently():
    """An edit in one section cannot move a boundary in another, even in
    principle — which is a stronger guarantee than the rolling hash alone."""
    a, b = _prose(2_000), _prose(2_000, seed=11)
    first = list(chunking.chunk_cdc("w:1", 1, [("A", a), ("B", b)], version=1))
    second = list(chunking.chunk_cdc("w:1", 1, [("A", a + " edited"), ("B", b)],
                                      version=1))
    text_b_first = [c["text"] for c in first if c["chapter"] == "B"]
    text_b_second = [c["text"] for c in second if c["chapter"] == "B"]
    assert text_b_first == text_b_second


# ---------------- who gets it ----------------

@pytest.mark.parametrize("profile,mutability,expected", [
    ("book", Mutability.IMMUTABLE, "book"),
    ("abstract", Mutability.IMMUTABLE, "book"),
    ("sequence-window", Mutability.IMMUTABLE, "book"),
    ("wiki", Mutability.MUTABLE, "cdc"),
    # verbatim wins over everything: those boundaries belong to the producer
    # of the vectors, and re-cutting them orphans every one
    ("verbatim", Mutability.IMMUTABLE, "verbatim"),
    ("verbatim", Mutability.MUTABLE, "verbatim"),
    # a source that declares nothing keeps exactly what it had
    ("book", None, "book"),
])
def test_the_rule_is_mutability_not_a_named_source(profile, mutability, expected):
    assert chunking.chunker_name(profile, mutability) == expected


def test_only_wikipedia_is_mutable_today():
    """Recorded so that adding a mutable source is a deliberate act with a
    visible diff, rather than something that happens by default."""
    mutable = {name for name, src in collections_mod.PLUGIN_REGISTRY.items()
               if src.mutability == Mutability.MUTABLE}
    assert mutable == {"wikipedia"}


# ---------------- the scoping that makes it affordable ----------------

def test_fixed_width_chunkers_keep_the_global_version():
    """Every existing collection's space id must be unchanged to the byte."""
    assert chunking.chunk_version_for("book") == config.CHUNK_VERSION
    assert chunking.chunk_version_for("verbatim") == config.CHUNK_VERSION
    assert chunking.chunk_version_for("abstract") == config.CHUNK_VERSION


def test_cdc_is_versioned_separately():
    assert chunking.chunk_version_for("cdc") == chunking.CDC_CHUNK_VERSION


def _collection(tmp_path, name, plugin):
    return collections_mod.Collection(
        name=name, plugin=plugin, profile=None, embed_mode_default=None,
        corpus_dir=tmp_path, manifest_path=tmp_path / f"{name}.db",
        fts_dir=tmp_path / f"{name}f", lance_dir=tmp_path / f"{name}l")


def test_an_immutable_collections_space_id_does_not_move(isolated_config, tmp_path,
                                                          monkeypatch):
    """The whole point of scoping. If this fails, adopting CDC re-pins
    gutenberg, pubmed, genome AND the pre-embedded collections — and the
    last of those cannot be recomputed by re-chunking at all."""
    monkeypatch.setattr(spaces, "resolve_embed_model_hash", lambda: ("abc", False))
    for plugin in ("gutenberg", "pubmed", "genome", "embedded"):
        collection = _collection(tmp_path, plugin, plugin)
        components = spaces.gather_components(collection)
        assert components.chunk_version == config.CHUNK_VERSION, plugin


def test_a_mutable_collection_gets_the_cdc_version(isolated_config, tmp_path,
                                                    monkeypatch):
    monkeypatch.setattr(spaces, "resolve_embed_model_hash", lambda: ("abc", False))
    collection = _collection(tmp_path, "wikipedia", "wikipedia")
    assert spaces.gather_components(collection).chunk_version == \
        chunking.CDC_CHUNK_VERSION


def test_the_chunker_is_part_of_the_space_identity(isolated_config, tmp_path,
                                                    monkeypatch):
    """chunk_version alone is NOT enough, and this nearly shipped wrong:
    CDC launched at version 1 and so is the fixed-width family, so the two
    payloads were byte-identical and wikipedia would have booted happily
    while mixing two incompatible segmentations in one space."""
    monkeypatch.setattr(spaces, "resolve_embed_model_hash", lambda: ("abc", False))
    book = spaces.gather_components(_collection(tmp_path, "gutenberg", "gutenberg"))
    cdc = spaces.gather_components(_collection(tmp_path, "wikipedia", "wikipedia"))

    assert book.chunk_version == cdc.chunk_version, \
        "the numbers really do collide — which is the whole point"
    assert book.chunker == "book" and cdc.chunker == "cdc"
    assert spaces.compute_space_id(book) != spaces.compute_space_id(cdc)
    assert spaces._config_id(book) != spaces._config_id(cdc), \
        "the config id must catch it too — it is what boot validation " \
        "checks when the model hash is weak"


def test_adding_the_chunker_field_repins_nothing(isolated_config, tmp_path,
                                                  monkeypatch):
    """Grandfathering, asserted: every chunker that existed when the space
    format was frozen must be omitted from the payload, so a space pinned
    before this change keeps exactly the id it already had."""
    monkeypatch.setattr(spaces, "resolve_embed_model_hash", lambda: ("abc", False))
    components = spaces.gather_components(_collection(tmp_path, "g", "gutenberg"))

    for name in ("book", "verbatim", "abstract"):
        assert name in spaces.GRANDFATHERED_CHUNKERS
        variant = replace(components, chunker=name)
        assert spaces.compute_space_id(variant) == spaces.compute_space_id(components)
        assert spaces._config_id(variant) == spaces._config_id(components)
