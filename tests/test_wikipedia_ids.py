"""Wikipedia's native ids (parser 4): the collection holds wikivec's
languages and two layers — the pre-embedded archive's articles and the
dumps' — and the id is what keeps them apart and lined up
(docs/notes/wikipedia-two-layers.md)."""
import pytest

from app import layers
from app.acquire.specs import WIKIPEDIA_SPEC, _WIKI_DUMP_BYTES
from app.plugins.embedded import source as wikivec
from app.plugins.wikipedia import source as wikipedia


def test_the_language_table_is_wikivecs_own():
    """A document the archive covers must keep the id the archive's rows
    are keyed by, or layer 1's vectors attach to nothing."""
    assert wikipedia.LANG_ORDINAL == wikivec.LANG_ORDINAL
    for lang in wikivec.LANG_ORDINAL:
        assert wikipedia.native_id_of(lang, 12, archive=True) == \
            wikivec.encode_native_id(lang, 12)


@pytest.mark.parametrize("lang", sorted(wikipedia.LANG_ORDINAL))
def test_an_id_decodes_to_its_language_page_and_source(lang):
    for archive in (True, False):
        nid = wikipedia.native_id_of(lang, 736, archive=archive)
        assert wikipedia.decode_native_id(nid) == (lang, 736, archive)


def test_the_layer_is_a_function_of_the_id():
    """06 §6.3's `archive:` rule reads the archive's coverage from the
    Codex; {ids_per_shard: 10^10, shards: 11} is every archive id and no
    dump id, in any language."""
    coverage = {"ids_per_shard": 10 ** 10, "shards": len(wikipedia.LANG_ORDINAL)}

    class Col:
        credits = [{"kind": "archive", "archive": "upstash-wikipedia-2024-06",
                    "coverage": coverage}]

    rule = "archive:upstash-wikipedia-2024-06"
    for lang in wikipedia.LANG_ORDINAL:
        assert layers._rule_matches(rule, Col, wikipedia.native_id_of(lang, 9_999_999, archive=True))
        assert not layers._rule_matches(rule, Col, wikipedia.native_id_of(lang, 1))


def test_the_spec_offers_every_language_combinable():
    assert WIKIPEDIA_SPEC.profile_mode == "additive"
    assert set(_WIKI_DUMP_BYTES) == set(wikipedia.LANG_ORDINAL)
    for lang in wikipedia.LANG_ORDINAL:
        assert WIKIPEDIA_SPEC.profiles[lang].include == (f"{lang}wiki/*",)
    from app.acquire.specs import _WIKI_PROPS_BYTES
    assert set(_WIKI_PROPS_BYTES) == set(_WIKI_DUMP_BYTES)
    assert WIKIPEDIA_SPEC.estimated_bytes["all"] == \
        sum(_WIKI_DUMP_BYTES.values()) + sum(_WIKI_PROPS_BYTES.values())
    names = {f.rel_path.split("/")[0] for f in WIKIPEDIA_SPEC.endpoints[0].static_files}
    assert names == {f"{lang}wiki" for lang in wikipedia.LANG_ORDINAL}, "no simplewiki"


def test_a_dump_run_is_its_articles_their_index_and_its_page_props():
    """Parser 5 reads each page's rows of its run's `page_props` (the QIDs, from
    the source): a pinned run lists the table beside the dump, and the filter
    admits it."""
    import fnmatch
    from app.acquire.specs import _wikipedia_snapshot_files
    files = [f.rel_path for f in _wikipedia_snapshot_files("20260801") if f.rel_path.startswith("dewiki/")]
    assert files == ["dewiki/20260801/dewiki-20260801-pages-articles-multistream.xml.bz2",
                     "dewiki/20260801/dewiki-20260801-pages-articles-multistream-index.txt.bz2",
                     "dewiki/20260801/dewiki-20260801-page_props.sql.gz"]
    assert all(any(fnmatch.fnmatch(f, pat) for pat in WIKIPEDIA_SPEC.filters) for f in files)


def test_a_slice_bins_the_page_id_not_the_native_id():
    from app.pack import slicing
    row = {"native_id": str(wikipedia.native_id_of("de", 2_500_000)), "path": "x"}
    assert slicing.slice_hint("wikipedia", row) == "lang=de/pages=2M"
