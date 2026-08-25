"""Slicing — pack-format.md §2: how a corpus is split into SLICES (the
human-facing part a peer can take alone: a language, a period) and, inside
a slice, SHARDS (the transfer/lease unit, records in id order, closed once
published).

A slice key must be known from the record alone (never from a vector),
stable for the life of the record, and meaningful to a person. The scheme
is declared per plugin here and recorded in the manifest, so the split is
a fact of the collection rather than a convention.
"""
import re
from dataclasses import dataclass
from typing import Callable

from ..plugins.base import CanonicalDoc


@dataclass(frozen=True)
class SliceScheme:
    keys: tuple[str, ...]
    records_per_shard: int
    describe: dict                      # what goes in the manifest's `slicing`
    slice_of: Callable[[CanonicalDoc, int], str]


_YEAR = re.compile(r"(\d{4})")


def _year_of(doc: CanonicalDoc) -> int | None:
    for candidate in (doc.date, (doc.extra or {}).get("date")):
        if candidate:
            m = _YEAR.search(str(candidate))
            if m:
                return int(m.group(1))
    return None


def pubmed_slice(doc: CanonicalDoc, native_id: int) -> str:
    """Publication year: 5-year bins before 2000, one year after, one bin
    for everything before 1950 (sparse) and one for records with no date."""
    year = _year_of(doc)
    if year is None:
        return "year=unknown"
    if year < 1950:
        return "year=pre1950"
    if year < 2000:
        lo = year - (year - 1950) % 5
        return f"year={lo}-{lo + 4}"
    return f"year={year}"


_PAGE_BIN = 1_000_000


def _pages_bin(page_id: int) -> str:
    k = page_id // _PAGE_BIN
    return f"pages={k}M"


def wikipedia_slice(doc: CanonicalDoc, native_id: int) -> str:
    """Language and a page-id range: page ids are stable and monotonic in
    creation time, the honest proxy for "created before ~2010" (creation
    dates are not in the dumps)."""
    return f"lang={doc.language or 'und'}/{_pages_bin(native_id)}"


def embedded_slice(doc: CanonicalDoc, native_id: int) -> str:
    """The pre-embedded Wikipedia: native ids encode (language ordinal,
    article id); the article id is the page id."""
    from ..plugins.embedded.source import decode_native_id
    try:
        lang, article = decode_native_id(int(native_id))
    except (KeyError, ValueError):
        return f"lang={doc.language or 'und'}/pages=unknown"
    return f"lang={lang}/{_pages_bin(article)}"


def gutenberg_slice(doc: CanonicalDoc, native_id: int) -> str:
    return f"lang={doc.language or 'und'}"


def single_slice(doc: CanonicalDoc, native_id: int) -> str:
    return "all"


SCHEMES: dict[str, SliceScheme] = {
    "pubmed": SliceScheme(
        keys=("year",), records_per_shard=30_000,
        describe={"keys": ["year"], "year": {"field": "date", "bins": ["pre1950", "5y<2000", "1y"]}},
        slice_of=pubmed_slice),
    "wikipedia": SliceScheme(
        keys=("lang", "pages"), records_per_shard=2_000,
        describe={"keys": ["lang", "pages"], "pages": {"field": "page_id", "bin": _PAGE_BIN}},
        slice_of=wikipedia_slice),
    "embedded": SliceScheme(
        keys=("lang", "pages"), records_per_shard=2_000,
        describe={"keys": ["lang", "pages"], "pages": {"field": "article_id", "bin": _PAGE_BIN}},
        slice_of=embedded_slice),
    "gutenberg": SliceScheme(
        keys=("lang",), records_per_shard=50,
        describe={"keys": ["lang"]},
        slice_of=gutenberg_slice),
}

DEFAULT_SCHEME = SliceScheme(keys=(), records_per_shard=10_000,
                             describe={"keys": []}, slice_of=single_slice)


def slice_hint(plugin_name: str, row) -> str | None:
    """The slice of a manifest row WITHOUT parsing the document, where the
    plugin's locator or the manifest already says enough — the planning
    pass over 36M PubMed rows or 6M Wikipedia pages must not cost a full
    parse each. None = parse the document and use the scheme."""
    try:
        if plugin_name == "wikipedia":
            from ..plugins.wikipedia.source import _lang_of, split_locator
            dump, _s, _e = split_locator(row["path"])
            return f"lang={_lang_of(dump.name)}/{_pages_bin(int(row['native_id']))}"
        if plugin_name == "embedded":
            from ..plugins.embedded.source import decode_native_id
            lang, article = decode_native_id(int(row["native_id"]))
            return f"lang={lang}/{_pages_bin(article)}"
        if plugin_name == "gutenberg":
            lang = row["lang"] if "lang" in row.keys() else None
            return f"lang={lang}" if lang else None
        if plugin_name == "pubmed":
            from ..plugins.pubmed import pack as pubmed_pack
            from ..plugins.pubmed.source import split_locator
            container, index = split_locator(row["path"])
            if index is None:
                return None
            date = pubmed_pack.record_dates(container).get(index)
            m = _YEAR.search(str(date or ""))
            if not m:
                return "year=unknown"
            year = int(m.group(1))
            if year < 1950:
                return "year=pre1950"
            if year < 2000:
                lo = year - (year - 1950) % 5
                return f"year={lo}-{lo + 4}"
            return f"year={year}"
    except Exception:   # noqa: BLE001 — a hint is an optimisation, never a verdict
        return None
    return None

_SEGMENT = r"[A-Za-z0-9=_\-][A-Za-z0-9=_.\-]*"      # no leading dot: never "." / ".." / hidden
_SAFE = re.compile(rf"^{_SEGMENT}(/{_SEGMENT})*$")


def scheme_for(plugin: str) -> SliceScheme:
    return SCHEMES.get(plugin, DEFAULT_SCHEME)


def check_slice_name(name: str) -> str:
    """A slice is a relative POSIX path of safe segments (it becomes a
    directory under records/)."""
    if not _SAFE.match(name):
        raise ValueError(f"unsafe slice name {name!r}")
    return name


def shard_name(n: int) -> str:
    return f"{n:05d}"
