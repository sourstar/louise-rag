"""Per-container Parquet PACK for PubMed — STATUS.md 5k.

A PubMed container is a gzipped <PubmedArticleSet> of ~30,000 records:
35 MB gzipped, 283 MB of XML. gzip is not seekable and the plugin parsed
the WHOLE container into a DOM to serve one record (8.5 s idle, 13–20 s
under load, hundreds of MB of tree in a one-slot cache) — so every stage
walking a container paid that, and two stages walking different
containers thrashed each other (observed live all evening).

The pack is a DERIVED file, built once per container by a single
streaming pass and rebuilt when the container's (size, mtime) or
PACK_VERSION changes: one row per direct child of the root, in document
order (the record INDEX the locators carry), holding the child's exact
serialised XML plus the fields the reader panel wants. Row groups of
ROW_GROUP rows, so record i lives in row group i // ROW_GROUP — one
small read, ~ms, no statistics needed.

Fidelity: the xml column is byte-identical to what the DOM path
returned (`ET.tostring(root[i])`, tail included — the element is
serialised only once its next sibling has started, so its tail is
complete), which keeps raw_hash identical; discovery compares raw_hash
to detect changed records, so anything else would re-process the whole
corpus. Pinned by a golden test.

Placement: a hidden `.pack/` directory beside the containers (never
published — torrent_publish skips hidden entries — never enumerated:
discovery lists files only), or config.PUBMED_PACK_DIR for faster
storage. The gz containers stay the archive: fetched, checksummed,
seeded. The pack is a cache and is never exchanged.
"""
import gzip
import json
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Iterator
from xml.etree import ElementTree as ET

import pyarrow as pa
import pyarrow.parquet as pq

from ... import config
from ...backend import GroupCache
from ...container_pack import ContainerPack, signature
from . import medline

PACK_VERSION = 1
ROW_GROUP = 1000
SUFFIX = ".pack.parquet"
_META_VERSION = b"louise.pack_version"
_META_SIZE = b"louise.source_size"
_META_MTIME = b"louise.source_mtime_ns"

SCHEMA = pa.schema([
    pa.field("index", pa.int32()),
    pa.field("tag", pa.string()),
    pa.field("pmid", pa.string()),            # PubmedArticle only
    pa.field("delete_pmids", pa.string()),    # DeleteCitation only (json list)
    pa.field("xml", pa.binary()),
    pa.field("title", pa.string()),
    pa.field("abstract", pa.string()),
    pa.field("journal", pa.string()),
    pa.field("date", pa.string()),
    pa.field("authors", pa.string()),
])

_files_lock = threading.Lock()              # guards the dict caches below
#: (pack path, row group) -> {index: xml}: 64 MiB of XML (app/backend.py);
#: it held 4 groups, ~10 MB each
_groups = GroupCache(64 << 20)


def open_container(path: Path):
    return gzip.open(path, "rb") if path.name.endswith(".gz") else open(path, "rb")


def pack_path(container: Path) -> Path:
    """PUBMED_PACK_DIR when set; else the fast drive's `pubmed-pack/<tag of
    the containers' folder>/` when the node names one (LOUISE_CACHE_DIR,
    app/derived.py — a pack already built beside the containers is carried
    there once); else `.pack/` beside the containers."""
    name = container.name + SUFFIX
    if config.PUBMED_PACK_DIR is not None:
        return Path(config.PUBMED_PACK_DIR) / name
    from app import derived
    beside = container.parent / derived.BESIDE
    cached = derived.place_kept("pubmed-pack", container.parent)
    if cached is None:
        return beside / name
    derived.adopt(beside / name, cached / name)
    return cached / name


#: the CONTAINER_PACK template's machinery (app/container_pack.py): freshness,
#: one builder across processes, the pack written whole
_PACK = ContainerPack("pubmed pack", "records", _META_VERSION)


def _signature(container: Path) -> tuple[int, int]:
    return signature(container)


def is_fresh(container: Path, path: Path) -> bool:
    return _PACK.is_fresh(container, path, PACK_VERSION)


def build(container: Path, dest: Path) -> int:
    """One streaming pass over the container into `dest`, written whole
    (app/container_pack.py). Raises what the XML/gzip layer raises on a
    corrupt container — the caller records ONE container error, exactly as
    before."""
    return _PACK.write(container, dest, SCHEMA, _tables(container), PACK_VERSION)


def _tables(container: Path) -> Iterator[pa.Table]:
    """The container's children as `SCHEMA` tables of ROW_GROUP rows."""
    rows: list[dict] = []
    n = 0

    def emit(elem: ET.Element, index: int) -> None:
        # serialised WITH its tail — identical to ET.tostring(root[i])
        xml = ET.tostring(elem, encoding="unicode").encode("utf-8")
        row = {"index": index, "tag": elem.tag, "pmid": None, "delete_pmids": None,
               "xml": xml, "title": None, "abstract": None, "journal": None,
               "date": None, "authors": None}
        if elem.tag == "PubmedArticle":
            meta = medline.parse_article(elem)
            row.update(pmid=meta["pmid"] or None, title=meta["title"],
                       abstract=meta["abstract"], journal=meta["journal"],
                       date=meta["date"], authors=authors_of(elem))
        elif elem.tag == "DeleteCitation":
            row["delete_pmids"] = json.dumps(medline.parse_delete_pmids(elem))
        rows.append(row)

    depth = 0
    root: ET.Element | None = None
    pending: ET.Element | None = None
    with open_container(container) as f:
        for event, elem in ET.iterparse(f, events=("start", "end")):
            if event == "start":
                depth += 1
                if depth == 1:
                    root = elem
                elif depth == 2 and pending is not None:
                    # the previous child's tail is complete now
                    emit(pending, n)
                    n += 1
                    pending.clear()
                    if root is not None:
                        root.remove(pending)
                    pending = None
            else:
                depth -= 1
                if depth == 1:
                    pending = elem
                elif depth == 0 and pending is not None:
                    emit(pending, n)
                    n += 1
                    pending = None
            if len(rows) >= ROW_GROUP:
                yield pa.Table.from_pylist(rows, schema=SCHEMA)
                rows = []
    if rows:
        yield pa.Table.from_pylist(rows, schema=SCHEMA)


def authors_of(elem: ET.Element) -> str:
    """"Last Initials" per author, "; "-joined, collective names kept.
    Public because `source.read_metadata` needs exactly the string the
    pack stores, so the manifest and the pack can never disagree."""
    out = []
    for a in elem.findall("MedlineCitation/Article/AuthorList/Author"):
        last = (a.findtext("LastName") or "").strip()
        initials = (a.findtext("Initials") or "").strip()
        coll = (a.findtext("CollectiveName") or "").strip()
        name = f"{last} {initials}".strip() or coll
        if name:
            out.append(name)
    return "; ".join(out)


def ensure(container: Path) -> Path:
    """The pack for `container`, built or rebuilt if missing or stale —
    one builder across threads and processes, the others wait and reuse
    (app/container_pack.py)."""
    from app import derived
    return _PACK.ensure(container, lambda: [pack_path(container)], version=PACK_VERSION,
                        build=lambda c, d: build(c, d), ready=_forget,
                        where=(config.PUBMED_PACK_DIR, derived.place_kept("pubmed-pack", container.parent)))


#: (container, where) -> (signature, version, pack path) of the pack found fresh
_fresh = _PACK.fresh


def _forget(path: Path) -> None:
    with _files_lock:
        _groups.forget(lambda k: k[0] == str(path))


def _open(path: Path) -> pq.ParquetFile:
    """A ParquetFile of the caller's own, to be closed after the read —
    never one shared between threads. A ParquetFile is not safe for
    concurrent use: with pre_buffer (pyarrow 25's default) every read
    replaces the reader's ReadRangeCache while another thread's read is
    still using it. They were shared (one cached per pack) until
    2026-09-18, when pubmed's fts stage and its four embed workers read the
    same packs at once on the node: `ArrowInvalid: ReadRangeCache did not
    find matching cache entry` recorded as an error on a healthy document,
    glibc aborting on a double free, and two segfaults at the same
    libarrow instruction — three worker restarts in three hours. Opening
    costs one footer parse, paid once per ROW_GROUP records."""
    return pq.ParquetFile(path)


def index_columns(container: Path) -> list[tuple[int, str, str | None, list[str]]]:
    """(index, tag, pmid, delete_pmids) for every child, in order — what
    discovery enumerates. Three small columns, never the XML."""
    with _open(ensure(container)) as pf:
        table = pf.read(columns=["index", "tag", "pmid", "delete_pmids"])
    out = []
    for idx, tag, pmid, dels in zip(table.column("index").to_pylist(),
                                     table.column("tag").to_pylist(),
                                     table.column("pmid").to_pylist(),
                                     table.column("delete_pmids").to_pylist()):
        out.append((int(idx), tag, pmid, json.loads(dels) if dels else []))
    return out


def record_xml(container: Path, index: int) -> bytes:
    """The serialised XML of child `index` — one row-group read, cached
    for the neighbours the pipeline is about to ask for."""
    path = ensure(container)
    rg = index // ROW_GROUP
    key = (str(path), rg)
    group = _groups.get(key)
    if group is None:
        with _open(path) as pf:
            if rg >= pf.num_row_groups:
                raise IndexError(f"{container.name}: record {index} beyond the pack")
            table = pf.read_row_group(rg, columns=["index", "xml"])
        group = _groups.put(key, dict(zip(table.column("index").to_pylist(), table.column("xml").to_pylist())),
                            table.column("xml").nbytes)
    try:
        return group[index]
    except KeyError:
        raise IndexError(f"{container.name}: record {index} not in the pack") from None


def record_fields(container: Path, index: int) -> dict:
    """The reader's view of one record (no XML): pmid, title, abstract,
    journal, date, authors."""
    rg = index // ROW_GROUP
    with _open(ensure(container)) as pf:
        table = pf.read_row_group(rg, columns=["index", "pmid", "title", "abstract",
                                               "journal", "date", "authors"])
    rows = table.to_pylist()
    for row in rows:
        if row["index"] == index:
            row.pop("index")
            return row
    raise IndexError(f"{container.name}: record {index} not in the pack")


_dates: OrderedDict = OrderedDict()          # pack path -> {index: date}
_MAX_DATES = 2


def record_dates(container: Path) -> dict[int, str | None]:
    """index -> publication date for EVERY record of the container, two
    small columns read once and cached (one slot per container): what the
    pack planner needs per record, without the row group's abstracts.
    record_fields() per record read a 1000-row group each time — 20
    records/s on the 33M-row pubmed manifest."""
    path = ensure(container)
    key = str(path)
    with _files_lock:
        hit = _dates.get(key)
        if hit is not None:
            _dates.move_to_end(key)
            return hit
    with _open(path) as pf:
        table = pf.read(columns=["index", "date"])
    out = dict(zip(table.column("index").to_pylist(), table.column("date").to_pylist()))
    with _files_lock:
        _dates[key] = out
        _dates.move_to_end(key)
        while len(_dates) > _MAX_DATES:
            _dates.popitem(last=False)
    return out


def record_authors(container: Path) -> dict[str, str]:
    """pmid -> author list for every ARTICLE of the container, as two
    small columns read once.

    Keyed on pmid, not on the record index, because that is what the
    manifest can look up: `documents` has a UNIQUE index on
    (source, native_id) and none at all on `path`, so a backfill that
    matched on the `<container>::<index>` locator would scan 40M rows per
    record. Deletions carry no pmid and are skipped, as are articles the
    origin gives no author (editorials, errata, much of the pre-1975
    baseline) — a blank is not written over, so a caller can tell "no
    authors recorded upstream" from "not looked at yet".

    Not cached: the one caller walks 1,334 containers once and never
    comes back, and a 30k-entry dict per container is not worth a slot
    that `record_dates` needs for the pack planner.
    """
    with _open(ensure(container)) as pf:
        table = pf.read(columns=["pmid", "authors"])
    out: dict[str, str] = {}
    for pmid, authors in zip(table.column("pmid").to_pylist(),
                             table.column("authors").to_pylist()):
        if pmid and authors:
            out[str(pmid)] = authors
    return out


def reset_caches() -> None:
    with _files_lock:
        _groups.clear()
        _fresh.clear()
        _dates.clear()
