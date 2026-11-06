"""One wiki's `page_props` table, from the same dump run as its articles
(wikipedia parser 5; docs/notes/wikipedia-qids.md).

Each run publishes `<wiki>-<date>-page_props.sql.gz` beside the articles:
MediaWiki's per-page properties, `wikibase_item` among them — the page's
Wikidata item — with the sitelink badges, the lead image and the
disambiguation flag. The QIDs come from here, from the source, and never
from another collection (operator, 2026-10-18).

THE UNIT. The table's rows are dumped in primary-key order (`pp_page`,
`pp_propname`), so one page's rows are contiguous: its unit is the bytes of
the decompressed SQL from the opening parenthesis of its first tuple to the
closing one of its last — `[start, end)`, the member its locator names. A
page whose rows straddle two INSERT statements takes the statement break
with them, as the dump wrote it.

THE PACK. gzip is not seekable, so a page is never read from the file: a
derived Parquet (`page`, `start`, `end`, `rows`), built once per file by one
streaming pass and rebuilt when the file's (size, mtime) or PACK_VERSION
moves, the way pubmed's pack is (app/plugins/pubmed/pack.py). It sits in a
hidden `.pack/` beside the file — never published, never enumerated — or, on
an origin that cannot be written (a read-only mount at check time), in the
system's temporary directory. The `.sql.gz` stays the origin.

STRICT. Every byte between two tuples of a statement must be the comma the
dump writes, and every statement must end in `);`. A row this reader does
not recognise fails the file, loudly, rather than being skipped: a skipped
row would be a property the record silently lacks.
"""
from __future__ import annotations

import base64
import gzip
import re
import tempfile
import threading
from bisect import bisect_right
from pathlib import Path

from app.backend import GroupCache
from app.container_pack import ContainerPack, signature

PACK_VERSION = 1
ROW_GROUP = 20_000
SUFFIX = "-page_props.sql.gz"
PACK_SUFFIX = ".pack.parquet"
#: the INSERT statements' head, as mysqldump and MariaDB's dump write it
_INSERT = b"INSERT INTO `page_props` VALUES "
#: one row: (pp_page, 'pp_propname', 'pp_value', pp_sortkey)
_TUPLE = re.compile(
    rb"\((\d+),'((?:[^'\\]|\\.)*)','((?:[^'\\]|\\.)*)',"
    rb"(NULL|-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)\)", re.S)
#: mysqldump's escapes; any other `\x` is `x` (MySQL's own rule)
_ESCAPES = {b"0": b"\0", b"'": b"'", b'"': b'"', b"b": b"\b", b"n": b"\n",
            b"r": b"\r", b"t": b"\t", b"Z": b"\x1a", b"\\": b"\\"}
_ESCAPED = re.compile(rb"\\(.)", re.S)
#: the property whose home is `extra.wikidata`
WIKIBASE_ITEM = "wikibase_item"

_META_VERSION = b"louise.page_props_pack_version"
_META_SIZE = b"louise.source_size"
_META_MTIME = b"louise.source_mtime_ns"


class PagePropsError(ValueError):
    """The file holds bytes this reader does not recognise as page_props rows."""


def props_path_of(dump: Path) -> Path:
    """The page_props file of the dump run `dump` belongs to: beside it, the
    run's own prefix (`enwiki-20260801`)."""
    name = dump.name
    i = name.find("-pages-articles")
    prefix = name[:i] if i > 0 else name.split(".")[0]
    return dump.with_name(prefix + SUFFIX)


def is_props_locator(segment: str) -> bool:
    return segment.rsplit("::", 2)[0].endswith(SUFFIX)


def _unescape(raw: bytes) -> bytes:
    return _ESCAPED.sub(lambda m: _ESCAPES.get(m.group(1), m.group(1)), raw)


def _text(raw: bytes):
    """A value as the record holds it: text, or `{"base64": …}` for bytes
    that are not UTF-8 (MediaWiki stores some properties compressed)."""
    b = _unescape(raw)
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        return {"base64": base64.b64encode(b).decode("ascii")}


def parse_rows(unit: bytes) -> list[tuple[str, object]]:
    """`[(pp_propname, pp_value), …]` of one page's unit, in the dump's order.
    `pp_page` is the page itself; `pp_sortkey` is MediaWiki's float copy of a
    numeric value, kept for its index, and adds nothing the value does not
    say (docs/notes/wikipedia-qids.md §2)."""
    out = []
    for m in _TUPLE.finditer(unit):
        name = _unescape(m.group(2)).decode("utf-8", errors="strict")
        out.append((name, _text(m.group(3))))
    return out


def fields_of(unit: bytes | None) -> dict:
    """What a record holds from its page's unit: `wikidata` (the QID) and
    `page_props` (every other property, by name). `{}` for no unit."""
    if not unit:
        return {}
    rows = parse_rows(unit)
    qid = next((v for k, v in rows if k == WIKIBASE_ITEM), None)
    rest = {k: v for k, v in rows if k != WIKIBASE_ITEM}
    return {k: v for k, v in {"wikidata": qid, "page_props": rest}.items() if v}


def iter_units(f):
    """`(page, start, end, unit bytes)` per page, streaming the decompressed
    SQL `f` (a binary file object) line by line. Pages strictly ascending, or
    PagePropsError."""
    pos = 0
    cur = None            # [page, start, parts, line, line_from, end_in_line, line_pos]
    last_page = -1
    for line in f:
        if line.startswith(_INSERT):
            at = len(_INSERT)
            for m in _TUPLE.finditer(line, at):
                if m.start() != at:
                    raise PagePropsError(f"unparsed bytes at offset {pos + at}: "
                                         f"{line[at:at + 60]!r}")
                page = int(m.group(1))
                if cur is not None and cur[0] == page:
                    if cur[3] is not line:
                        cur[2].append(cur[3][cur[4]:])
                        cur[3], cur[4], cur[6] = line, 0, pos
                    cur[5] = m.end()
                else:
                    if cur is not None:
                        yield _close(cur)
                    if page <= last_page:
                        raise PagePropsError(f"page {page} after {last_page}: the rows are "
                                             f"not in primary-key order")
                    last_page = page
                    cur = [page, pos + m.start(), [], line, m.start(), m.end(), pos]
                at = m.end()
                if line[at:at + 1] == b",":
                    at += 1
            tail = line[at:].rstrip(b"\r\n")
            if tail != b";" or line[at - 1:at] != b")":
                raise PagePropsError(f"statement at offset {pos} ends in {line[at - 1:at + 20]!r}")
        pos += len(line)
    if cur is not None:
        yield _close(cur)


def _close(cur) -> tuple[int, int, int, bytes]:
    page, start, parts, line, line_from, end_in_line, line_pos = cur
    unit = b"".join(parts) + line[line_from:end_in_line]
    return page, start, line_pos + end_in_line, unit


# ---- the pack ---------------------------------------------------------------

#: the CONTAINER_PACK template's machinery (app/container_pack.py): freshness,
#: one builder across processes, the pack written whole
_PACK = ContainerPack("wikipedia page_props", "pages", _META_VERSION)


def _signature(path: Path) -> tuple[int, int]:
    return signature(path)


def _pack_dirs(props: Path) -> list[Path]:
    """The fast drive's `wikipedia-page-props/<tag of the props' folder>/`
    when the node names one (LOUISE_CACHE_DIR, app/derived.py), else
    `.pack/` beside the props; then the temporary directory."""
    from app import derived
    first = derived.place("wikipedia-page-props", props.parent) or props.parent / derived.BESIDE
    return [first, Path(tempfile.gettempdir()) / "louise-page-props"]


def _candidates(props: Path) -> list[Path]:
    # the temporary one is keyed by the origin's full path: two corpora can
    # hold a file of the same name (caches key on physical identity)
    from app import derived
    tag = derived.tag(props) + "-"
    first, second = _pack_dirs(props)
    beside = props.parent / derived.BESIDE / (props.name + PACK_SUFFIX)
    if first != beside.parent:
        derived.adopt(beside, first / (props.name + PACK_SUFFIX))   # a pack built beside the props, carried once
    return [first / (props.name + PACK_SUFFIX), second / (tag + props.name + PACK_SUFFIX)]


def _is_fresh(props: Path, pack: Path) -> bool:
    return _PACK.is_fresh(props, pack, PACK_VERSION)


_SCHEMA = None


def _schema():
    import pyarrow as pa
    global _SCHEMA
    if _SCHEMA is None:
        _SCHEMA = pa.schema([pa.field("page", pa.int64()), pa.field("start", pa.int64()),
                             pa.field("end", pa.int64()), pa.field("rows", pa.binary())])
    return _SCHEMA


def build(props: Path, dest: Path) -> int:
    """One streaming pass over `props` into `dest`, written whole
    (app/container_pack.py)."""
    return _PACK.write(props, dest, _schema(), _tables(props), PACK_VERSION)


def _tables(props: Path):
    """The props' units as tables of ROW_GROUP rows."""
    import pyarrow as pa
    cols: dict[str, list] = {"page": [], "start": [], "end": [], "rows": []}
    with gzip.open(props, "rb") as f:
        for page, start, end, unit in iter_units(f):
            cols["page"].append(page)
            cols["start"].append(start)
            cols["end"].append(end)
            cols["rows"].append(unit)
            if len(cols["page"]) >= ROW_GROUP:
                yield pa.table(cols, schema=_schema())
                cols = {k: [] for k in cols}
    if cols["page"]:
        yield pa.table(cols, schema=_schema())


_lock = threading.Lock()
#: (props, where) -> (signature, version, pack path) of the pack found fresh
_fresh = _PACK.fresh
_bounds: dict[str, list[int]] = {}             # pack path -> first page of each row group
#: (pack path, group) -> {page: (start, end, rows)}: 64 MiB (app/backend.py); it held 4 groups
_groups = GroupCache(64 << 20)


def ensure(props: Path) -> Path:
    """The pack of `props`, built if missing or stale; one builder across
    threads and processes (app/container_pack.py)."""
    from app import derived
    return _PACK.ensure(props, lambda: _candidates(props), version=PACK_VERSION,
                        build=lambda o, d: build(o, d), ready=_ready,
                        where=derived.place_kept("wikipedia-page-props", props.parent))


def _ready(pack: Path) -> None:
    """A pack newly confirmed or built: what was cached from an older one goes."""
    with _lock:
        _bounds.pop(str(pack), None)
    _groups.forget(lambda k: k[0] == str(pack))


def unit_of(props: Path, page: int) -> tuple[int, int, bytes] | None:
    """`(start, end, unit)` of `page` in `props`, or None when the table has
    no row for it."""
    return _lookup(ensure(props), page)


def reader(props: Path):
    """`unit_of` over one file for a whole scan: the pack confirmed once,
    not stat-ed per page."""
    pack = ensure(props)
    return lambda page: _lookup(pack, page)


def _lookup(pack: Path, page: int) -> tuple[int, int, bytes] | None:
    import pyarrow.parquet as pq
    pk = str(pack)
    with _lock:
        bounds = _bounds.get(pk)
    if bounds is None:
        md = pq.read_metadata(pack)
        bounds = []
        for g in range(md.num_row_groups):
            st = md.row_group(g).column(0).statistics
            bounds.append(int(st.min))
        with _lock:
            _bounds[pk] = bounds
    g = bisect_right(bounds, page) - 1
    if g < 0:
        return None
    group = _groups.get((pk, g))
    if group is None:
        with pq.ParquetFile(pack) as pf:
            t = pf.read_row_group(g)
        group = _groups.put((pk, g), {p: (s, e, r) for p, s, e, r in zip(
            t.column("page").to_pylist(), t.column("start").to_pylist(),
            t.column("end").to_pylist(), t.column("rows").to_pylist())}, t.nbytes)
    return group.get(page)
