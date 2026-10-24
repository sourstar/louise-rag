"""Slicing — pack-format.md §2: how a corpus is split into SLICES (the
human-facing part a peer can take alone: a language, a period) and, inside
a slice, SHARDS (the transfer/lease unit, records in id order, closed once
published).

A slice key must be known from the record alone (never from a vector) —
or from the record and a map its Codex pins (2026-10-24, wikidata's
families) — stable for the life of the record, and meaningful to a person.
The scheme is declared per plugin here and recorded in the manifest, so the
split is a fact of the collection rather than a convention.
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
    dates are not in the dumps). The native id carries the language and
    the page id (wikipedia parser 4); the bin is over the page id."""
    from ..plugins.wikipedia.source import decode_native_id
    try:
        lang, page, _ = decode_native_id(int(native_id))
    except (KeyError, ValueError):
        return f"lang={doc.language or 'und'}/pages=unknown"
    return f"lang={lang}/{_pages_bin(page)}"


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


#: the Wikidata id range a part holds: 10⁷ ids (docs/notes/wikidata-build-plan.md
#: §2 "Slicing and parts"); every property in one range of its own
_WD_RANGE = 10_000_000
_WD_PROPERTY_RANGE = 100


def wikidata_slice(doc: CanonicalDoc, native_id: int) -> str:
    """`range=<native_id // 10⁷>` for an item, `range=100` for a property
    (ids.PROPERTY_BASE and above): a pure function of the native id, so
    `slice_hint` answers it without the record."""
    from ..plugins.wikidata.ids import PROPERTY_BASE
    if int(native_id) >= PROPERTY_BASE:
        return f"range={_WD_PROPERTY_RANGE}"
    return f"range={int(native_id) // _WD_RANGE}"


def single_slice(doc: CanonicalDoc, native_id: int) -> str:
    return "all"


# ---- wikidata by family (pack-format.md §2, amended 2026-10-24) ---------------------------------
#
# A collection created on or after 2026-10-24 is sliced `family=<f>/range=<n>`
# by a family MAP pinned at its creation (app/plugins/wikidata/families.py):
# its sha256 in the Codex's slicing block, its bytes the SHARD member
# `slicing/<sha256 hex>.json`. The family is the one key here that a record
# alone does not give — it needs the map — so it is read through a CONTEXT
# the build takes once from the collection (`context_for`); a Codex sliced by
# id range alone (the wikidata of 2026-10-20) keeps `wikidata_slice`.

FAMILY_RULE = "wikidata-families"


def family_block(map_sha: str, records_per_shard: int = 20_000) -> dict:
    """The Codex's slicing block for wikidata by family (09 §9.3)."""
    from ..plugins.wikidata import families
    return {"keys": ["family", "range"],
            "family": {"field": "extra.claims", "rule": FAMILY_RULE, "map": map_sha},
            "range": {"field": "id", "bin": families.RANGE},
            "unknown": "family=unknown", "shard_records": records_per_shard, "order": "numeric"}


def family_map_of(block: dict | None) -> str | None:
    """The sha256 of the family map a slicing block names, or None."""
    fam = (block or {}).get("family") if isinstance(block, dict) else None
    if isinstance(fam, dict) and fam.get("rule") == FAMILY_RULE and isinstance(fam.get("map"), str):
        return fam["map"]
    return None


def map_member(map_sha: str) -> str:
    """`slicing/<sha256 hex>.json` — the SHARD member that carries the map
    (09 §9.4, amended 2026-10-24), named by its hash."""
    algo, _, hexd = str(map_sha).partition(":")
    if algo != "sha256" or len(hexd) != 64 or any(c not in "0123456789abcdef" for c in hexd):
        raise ValueError(f"a family map is named by its sha256, not {map_sha!r}")
    return f"slicing/{hexd}.json"


@dataclass(frozen=True)
class FamilyContext:
    """What the family key needs beyond the record: the pinned map, the
    dump the collection reads, and the classes newer than the map walked
    over that dump's links."""
    fmap: object
    dump: object
    walked: dict


class ContextUnavailable(RuntimeError):
    """The collection's slicing needs something this node does not have yet."""


def context_for(collection, *, log=print, stop=None) -> FamilyContext | None:
    """The context a collection's slicing needs, or None (every scheme but
    wikidata by family). For wikidata by family: the map the Codex names,
    read from the SHARD member and checked against its sha256, and the
    pack's family codes under it — computed here when missing (a pass over
    the pack's lines, resumable; done at the pack's own upgrade when the
    collection was created on this dump)."""
    if getattr(collection, "plugin", None) != "wikidata":
        return None
    from pathlib import Path
    from ..hold import codex as cx
    shard = Path(collection.manifest_path).parent.parent / "shard"
    try:
        codex = cx.loads((shard / "codex.json").read_bytes())
    except OSError:
        return None                       # no Codex: the plugin's own scheme
    sha = family_map_of(codex.slicing)
    if sha is None:
        return None
    import hashlib
    from ..plugins.wikidata import families, pack, source as wd
    member = shard / map_member(sha)
    try:
        raw = member.read_bytes()
    except OSError as e:
        raise ContextUnavailable(f"{collection.name}: the Codex slices by the family map {sha} and the SHARD "
                                 f"holds no {map_member(sha)} ({e})") from None
    if "sha256:" + hashlib.sha256(raw).hexdigest() != sha:
        raise ContextUnavailable(f"{collection.name}: {map_member(sha)} does not hash to the {sha} the Codex names")
    fmap = families.load_map(raw)
    dumps = wd.find_dumps(collection.corpus_dir, getattr(collection, "snapshot", None))
    if not dumps:
        raise ContextUnavailable(f"{collection.name}: no wikidata dump under {collection.corpus_dir}")
    dump = dumps[0]
    if not pack.ensure_codes(dump, raw, log=log, stop=(stop.is_set if stop is not None else None)):
        raise ContextUnavailable(f"{collection.name}: the family codes of {dump.name} under {sha[:23]}… "
                                 f"stopped before the end; the next build resumes them")
    import sqlite3
    con = sqlite3.connect((pack.pack_dir(dump) / "labels.sqlite").resolve().as_uri() + "?mode=ro", uri=True)
    try:
        rows = con.execute("SELECT id, parent FROM subclass WHERE id > ?", (fmap.max_item,)).fetchall()
    finally:
        con.close()
    return FamilyContext(fmap, dump, families.walk((r[0] for r in rows), (r[1] for r in rows), fmap))


def describe_for(collection, scheme: "SliceScheme") -> dict:
    """What a pack manifest says of the slicing: the Codex's own family
    block where it names a map, the scheme's description otherwise."""
    from pathlib import Path
    try:
        from ..hold import codex as cx
        codex = cx.loads((Path(collection.manifest_path).parent.parent / "shard" / "codex.json").read_bytes())
    except (OSError, ValueError, AttributeError):
        return dict(scheme.describe)
    if family_map_of(codex.slicing) is None:
        return dict(scheme.describe)
    return {k: v for k, v in codex.slicing.items() if k in ("keys", "family", "range")}


def slice_of(scheme: "SliceScheme", doc: CanonicalDoc, native_id: int, context: FamilyContext | None = None) -> str:
    """The slice of a parsed record: by the context's map where there is one."""
    if isinstance(context, FamilyContext):
        from ..plugins.wikidata import families
        x = doc.extra or {}
        return families.slice_name(families.family_code(x, context.fmap, context.walked), str(x.get("id") or ""),
                                   context.fmap.families)
    return scheme.slice_of(doc, native_id)


_SLICE_TOKEN = re.compile(r"[^A-Za-z0-9_\-]+")


def _normattiva_type(member_dir: str) -> str:
    """`LEGGE COSTITUZIONALE_19480226_3` → `LEGGE_COSTITUZIONALE`: the act
    type the origin files the member under, as a safe slice segment."""
    from ..plugins.normattiva.source import member_dir_type
    tok = _SLICE_TOKEN.sub("_", member_dir_type(member_dir)).strip("_")
    return tok or "unknown"


def normattiva_slice(doc: CanonicalDoc, native_id: int) -> str:
    """normattiva-multivigenza-review.md §3.7: the act type the origin files
    the member under, and the year the version came into force — the id's
    high digits (02 §2.2's monotonic prefix), so `slice_hint` answers from
    the manifest row alone and agrees with this by construction."""
    x = doc.extra or {}
    year = str(x.get("vigenza_from") or "")[:4]
    return (f"type={_normattiva_type(str(x.get('member_dir') or ''))}"
            f"/vig={year if year.isdigit() else 'unknown'}")


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
    "wikidata": SliceScheme(
        keys=("range",), records_per_shard=20_000,
        describe={"keys": ["range"], "range": {"field": "native_id", "bin": _WD_RANGE,
                                                "properties": _WD_PROPERTY_RANGE}},
        slice_of=wikidata_slice),
    "gutenberg": SliceScheme(
        keys=("lang",), records_per_shard=50,
        describe={"keys": ["lang"]},
        slice_of=gutenberg_slice),
    # one record per Expression, parts of about 50 versions: the codes run
    # to 400 articles a version and a royal decree to four, so the 7,000-chunk
    # target of 02 §2.3 is met in the middle and missed at both ends — one
    # `shard_records` per collection is the format's (review §3.7, §7)
    "normattiva": SliceScheme(
        keys=("type", "vig"), records_per_shard=50,
        describe={"keys": ["type", "vig"],
                  "type": {"field": "extra.member_dir", "rule": "the act type the origin files the member under"},
                  "vig": {"field": "extra.vigenza_from", "bin": "year"}},
        slice_of=normattiva_slice),
}

DEFAULT_SCHEME = SliceScheme(keys=(), records_per_shard=10_000,
                             describe={"keys": []}, slice_of=single_slice)


def slice_hint(plugin_name: str, row, context: FamilyContext | None = None) -> str | None:
    """The slice of a manifest row WITHOUT parsing the document, where the
    plugin's locator or the manifest already says enough — the planning
    pass over 36M PubMed rows or 6M Wikipedia pages must not cost a full
    parse each. None = parse the document and use the scheme."""
    try:
        if isinstance(context, FamilyContext):
            # wikidata by family: the byte the pack keeps for the entity
            from ..plugins.wikidata import families, ids, pack
            from ..plugins.wikidata.source import split_locator
            dump, index = split_locator(row["path"])
            code = pack.family_code_at(dump, context.fmap.sha256, index)
            if code is None:
                return None
            return families.slice_name(code, ids.entity_id_of(int(row["native_id"])), context.fmap.families)
        if plugin_name == "wikipedia":
            from ..plugins.wikipedia.source import decode_native_id
            lang, page, _ = decode_native_id(int(row["native_id"]))
            return f"lang={lang}/{_pages_bin(page)}"
        if plugin_name == "embedded":
            from ..plugins.embedded.source import decode_native_id
            lang, article = decode_native_id(int(row["native_id"]))
            return f"lang={lang}/{_pages_bin(article)}"
        if plugin_name == "gutenberg":
            lang = row["lang"] if "lang" in row.keys() else None
            return f"lang={lang}" if lang else None
        if plugin_name == "wikidata":
            return wikidata_slice(None, int(row["native_id"]))
        if plugin_name == "normattiva":
            from ..plugins import eli
            from ..plugins.normattiva.source import split_locator
            _, member = split_locator(row["path"])
            year = eli.id_day(int(row["native_id"])) // 10000
            return f"type={_normattiva_type(member.split('/', 1)[0])}/vig={year:04d}"
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
