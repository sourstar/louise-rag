"""tests/test_chunker_fixed.py — `fixed` is `book` with its window declared.

docs/06-plugins.md §6.3 registers `fixed` and says, immediately above its
table, that **a chunker reads no node configuration**. `chunk_book` reads
`CHUNK_TOKENS` and `CHUNK_OVERLAP` from the environment, so two peers could
agree on a `space_id` (09 §9.3 hashes the layer's params) and disagree on
chunks — invariant 1's failure mode, and undetectable, because both peers
computed the id the document prescribes and both are honest.

`fixed` closes that. It is the same windows with the two numbers carried in
the layer, and these tests pin the two things that makes true: that it cuts
identically, and that it refuses to guess.
"""
import pytest

from app import chunking, config
from app.chunking import ChunkerError

BOOK = [
    ("Chapter 1", "\n\n".join(f"Paragraph {i} of the first chapter, with "
                              f"enough words in it to be worth cutting and "
                              f"re-cutting several times over." for i in range(60))),
    ("Chapter 2", "One short paragraph."),
    ("Chapter 3", "x" * 30_000),          # forces the hard-split path
]


def ids(gen):
    return [(c["id"], c["seq"], c["chapter"], c["text"]) for c in gen]


# ---- the identity that lets legacy vectors carry over ---------------------

def test_fixed_at_the_config_defaults_cuts_exactly_like_book():
    """The load-bearing claim. `book` and `fixed` share `_fixed_windows`, so
    this is true by construction — and pinned here because everything else
    rests on it: if the two cut identically, a Codex collection declaring
    `fixed` has the chunk ids a legacy pack already holds, and its vectors
    carry over instead of being re-embedded."""
    book = ids(chunking.chunk_book("gutenberg:1342", 1342, BOOK, 1))
    fixed = ids(chunking.chunk_fixed(
        "gutenberg:1342", 1342, BOOK, 1,
        {"tokens": config.CHUNK_TOKENS, "overlap": config.CHUNK_OVERLAP}))
    assert book == fixed
    assert len(book) > 3, "the fixture must actually exercise the windowing"


def test_the_chunk_ids_are_the_same_string():
    """A chunk id is `{uid}:{seq}:{version}` (09 §9.5), so the VERSION has to
    match too — not just the boundaries. `FIXED_CHUNK_VERSION` is 1 because
    `config.CHUNK_VERSION` is, which is what makes the ids equal rather than
    merely the texts."""
    assert chunking.chunk_version_for(chunking.FIXED) == \
        chunking.chunk_version_for(chunking.BOOK) == config.CHUNK_VERSION

    v = chunking.chunk_version_for(chunking.FIXED)
    book = [c["id"] for c in chunking.chunk_book("g:1", 1, BOOK, v)]
    fixed = [c["id"] for c in chunking.chunk_fixed(
        "g:1", 1, BOOK, v,
        {"tokens": config.CHUNK_TOKENS, "overlap": config.CHUNK_OVERLAP})]
    assert book == fixed


def test_a_different_window_cuts_differently():
    """Otherwise the test above would pass for a chunker that ignored its
    parameters entirely, which is the bug being fixed."""
    a = ids(chunking.chunk_fixed("g:1", 1, BOOK, 1, {"tokens": 1000, "overlap": 120}))
    b = ids(chunking.chunk_fixed("g:1", 1, BOOK, 1, {"tokens": 300, "overlap": 30}))
    assert a != b
    assert len(b) > len(a)


# ---- it refuses to guess --------------------------------------------------

def test_fixed_refuses_a_layer_that_does_not_say():
    """No defaults, and no reading the environment. A chunker that guessed
    would reintroduce exactly the divergence it exists to remove."""
    for params in (None, {}, {"tokens": 1000}, {"overlap": 120}):
        with pytest.raises(ChunkerError, match="no defaults"):
            list(chunking.chunk_fixed("g:1", 1, BOOK, 1, params))


def test_fixed_does_not_read_the_environment(monkeypatch):
    """The property the whole change is for: move the node's configuration
    and `fixed` does not move with it."""
    p = {"tokens": 1000, "overlap": 120}
    before = ids(chunking.chunk_fixed("g:1", 1, BOOK, 1, p))
    monkeypatch.setattr(config, "CHUNK_TOKENS", 250)
    monkeypatch.setattr(config, "CHUNK_OVERLAP", 25)
    assert ids(chunking.chunk_fixed("g:1", 1, BOOK, 1, p)) == before

    # ...and `book` DOES, which is why it stays grandfathered and why a new
    # collection uses `fixed` instead.
    assert ids(chunking.chunk_book("g:1", 1, BOOK, 1)) != before


def test_an_overlap_at_or_above_the_window_is_refused():
    """`book` clamps this because it is reachable from configuration alone;
    `fixed` refuses it, because a layer is a declaration and a declaration
    that cannot terminate is a mistake to report, not to repair."""
    with pytest.raises(ChunkerError, match="yields for ever"):
        list(chunking.chunk_fixed("g:1", 1, BOOK, 1,
                                  {"tokens": 100, "overlap": 100}))
    with pytest.raises(ChunkerError, match="at least 1"):
        list(chunking.chunk_fixed("g:1", 1, BOOK, 1,
                                  {"tokens": 0, "overlap": 0}))


# ---- what must NOT have moved --------------------------------------------

def test_fixed_is_registered_and_book_is_untouched():
    assert chunking._CHUNKERS[chunking.FIXED] is chunking.chunk_fixed
    assert chunking._CHUNKERS[chunking.BOOK] is chunking.chunk_book
    assert set(chunking._CHUNKERS) == {"book", "fixed", "verbatim",
                                       "abstract", "cdc", "akn_article"}


def test_every_chunker_takes_the_same_shape():
    """The dispatch passes `(uid, nid, sections, version, params)` blind, so
    a chunker with another signature is a TypeError at cut time — on a
    corpus, in a worker, hours in."""
    import inspect
    for name, fn in chunking._CHUNKERS.items():
        args = list(inspect.signature(fn).parameters)
        assert args[:5] == ["uid", "native_id", args[2], "version", "params"], \
            f"{name} has signature {args}"


# ---- the two places an identity could have moved, and did not ------------

def test_params_do_not_enter_the_legacy_space_id():
    """`layers.space_payload` is what `app/spaces.py` folds into the LEGACY
    space id, and the vectors already on this node are keyed to it. It
    returns `{}` for a one-layer collection and `[[chunker, version]]`
    otherwise — params in NEITHER. Checked rather than assumed, because if
    it were otherwise every existing collection would refuse to start
    (invariant 1) the moment a layer gained a parameter."""
    from app import layers as layers_mod
    import inspect
    src = inspect.getsource(layers_mod.space_payload)
    assert "params" not in src, \
        "space_payload must not carry params, or every pinned space id moves"


def test_a_manifest_is_unchanged_when_a_layer_has_no_params():
    """Every collection on this node predates params. A manifest that gained
    an empty key would differ from the bytes already on disk for no reason."""
    from app.layers import Layer
    assert Layer("book", 1, "default").as_manifest() == \
        {"chunker": {"id": "book", "version": 1}, "rule": "default"}


def test_a_manifest_carries_params_when_a_layer_has_them():
    from app.layers import Layer
    m = Layer("fixed", 1, "default", {"tokens": 1000, "overlap": 120}).as_manifest()
    assert m["chunker"]["params"] == {"tokens": 1000, "overlap": 120}


def test_the_layer_parser_reads_params_and_refuses_a_non_table():
    from app import layers as layers_mod
    from app.layers import LayerError
    got, = layers_mod.parse_layers(
        [{"chunker": "fixed", "params": {"tokens": 800, "overlap": 80}}],
        where="test")
    assert got.chunker == "fixed" and got.params == {"tokens": 800, "overlap": 80}
    assert layers_mod.parse_layers([{"chunker": "book"}], where="t")[0].params == {}

    with pytest.raises(LayerError, match="params is a table"):
        layers_mod.parse_layers([{"chunker": "fixed", "params": 7}], where="t")


def test_a_bundle_refuses_the_legacy_bare_hex_space_id():
    """Item 3 needs no code change — the two `space_id` functions name
    different things and never meet — but this is the assertion that keeps
    it true. `app/spaces.py` produces a bare 32-hex BLAKE2b digest; 09 §9.3
    fixes `sha256:` and 64 lowercase hex, and a conformant bundle therefore
    CANNOT carry the legacy value."""
    from app.hold import bundle as bd
    from app.hold.bundle import BundleError
    from tests.hold.test_bundle import header

    with pytest.raises(BundleError, match="sha256:"):
        bd.check(header(space_id="41" * 16))     # the legacy form: bare, 32 hex
    bd.check(header())                            # the Codex's form passes
