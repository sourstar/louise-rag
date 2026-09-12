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
import os
import sys
import threading
import time
from collections import OrderedDict
from pathlib import Path
from xml.etree import ElementTree as ET

import pyarrow as pa
import pyarrow.parquet as pq

from ... import config
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

_build_locks: dict[str, threading.Lock] = {}
_build_locks_guard = threading.Lock()
_files_lock = threading.Lock()
_files: OrderedDict = OrderedDict()          # pack path -> (mtime_ns, ParquetFile)
_groups: OrderedDict = OrderedDict()         # (pack path, row group) -> {index: xml}
_MAX_FILES = 8
_MAX_GROUPS = 4                              # ~10 MB of XML each


def open_container(path: Path):
    return gzip.open(path, "rb") if path.name.endswith(".gz") else open(path, "rb")


def pack_path(container: Path) -> Path:
    base = config.PUBMED_PACK_DIR if config.PUBMED_PACK_DIR is not None \
        else container.parent / ".pack"
    return Path(base) / (container.name + SUFFIX)


def _signature(container: Path) -> tuple[int, int]:
    st = container.stat()
    return st.st_size, st.st_mtime_ns


def is_fresh(container: Path, path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        meta = pq.read_metadata(path).metadata or {}
    except Exception:   # noqa: BLE001 — a truncated pack is simply stale
        return False
    size, mtime = _signature(container)
    return (meta.get(_META_VERSION) == str(PACK_VERSION).encode()
            and meta.get(_META_SIZE) == str(size).encode()
            and meta.get(_META_MTIME) == str(mtime).encode())


def build(container: Path, dest: Path) -> int:
    """One streaming pass over the container into `dest` (atomic: tmp +
    rename). Raises what the XML/gzip layer raises on a corrupt container
    — the caller records ONE container error, exactly as before."""
    size, mtime = _signature(container)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    rows: list[dict] = []
    n = 0
    # file-level key/value metadata rides on the schema (pyarrow 25)
    schema = SCHEMA.with_metadata({_META_VERSION: str(PACK_VERSION),
                                   _META_SIZE: str(size), _META_MTIME: str(mtime)})
    writer = pq.ParquetWriter(tmp, schema, compression="zstd", compression_level=3)
    try:
        def flush():
            nonlocal rows
            if rows:
                writer.write_table(pa.Table.from_pylist(rows, schema=schema))
                rows = []

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
            if len(rows) >= ROW_GROUP:
                flush()

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
        flush()
        writer.close()
        os.replace(tmp, dest)
    except BaseException:
        try:
            writer.close()
        except Exception:   # noqa: BLE001
            pass
        tmp.unlink(missing_ok=True)
        raise
    return n


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
    """The pack for `container`, built or rebuilt if missing or stale.
    One builder per container at a time; the others wait and reuse.
    Freshness is memoised per (container, size, mtime): reading the
    Parquet footer per record cost ~3 ms, a stat costs microseconds."""
    path = pack_path(container)
    sig = _signature(container)
    with _files_lock:
        if _fresh.get(str(container)) == (sig, PACK_VERSION, str(path)):
            return path
    if is_fresh(container, path):
        with _files_lock:
            _fresh[str(container)] = (sig, PACK_VERSION, str(path))
        return path
    with _build_locks_guard:
        lock = _build_locks.setdefault(str(container), threading.Lock())
    with lock:
        if is_fresh(container, path):
            with _files_lock:
                _fresh[str(container)] = (sig, PACK_VERSION, str(path))
            return path
        t0 = time.monotonic()
        n = build(container, path)
        print(f"[pubmed pack] built {path.name} ({n} records, "
              f"{path.stat().st_size >> 20} MiB) in {time.monotonic() - t0:.1f}s",
              file=sys.stderr, flush=True)
        _forget(path)
        with _files_lock:
            _fresh[str(container)] = (_signature(container), PACK_VERSION, str(path))
        return path


_fresh: dict[str, tuple[tuple[int, int], int, str]] = {}   # container -> (signature, version, pack path)


def _forget(path: Path) -> None:
    with _files_lock:
        _files.pop(str(path), None)
        for key in [k for k in _groups if k[0] == str(path)]:
            _groups.pop(key, None)


def _open(path: Path) -> pq.ParquetFile:
    key = str(path)
    mtime = path.stat().st_mtime_ns
    with _files_lock:
        hit = _files.get(key)
        if hit is not None and hit[0] == mtime:
            _files.move_to_end(key)
            return hit[1]
    pf = pq.ParquetFile(path)
    with _files_lock:
        _files[key] = (mtime, pf)
        _files.move_to_end(key)
        while len(_files) > _MAX_FILES:
            _files.popitem(last=False)
    return pf


def index_columns(container: Path) -> list[tuple[int, str, str | None, list[str]]]:
    """(index, tag, pmid, delete_pmids) for every child, in order — what
    discovery enumerates. Three small columns, never the XML."""
    pf = _open(ensure(container))
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
    with _files_lock:
        group = _groups.get(key)
        if group is not None:
            _groups.move_to_end(key)
    if group is None:
        pf = _open(path)
        if rg >= pf.num_row_groups:
            raise IndexError(f"{container.name}: record {index} beyond the pack")
        table = pf.read_row_group(rg, columns=["index", "xml"])
        group = dict(zip(table.column("index").to_pylist(), table.column("xml").to_pylist()))
        with _files_lock:
            _groups[key] = group
            _groups.move_to_end(key)
            while len(_groups) > _MAX_GROUPS:
                _groups.popitem(last=False)
    try:
        return group[index]
    except KeyError:
        raise IndexError(f"{container.name}: record {index} not in the pack") from None


def record_fields(container: Path, index: int) -> dict:
    """The reader's view of one record (no XML): pmid, title, abstract,
    journal, date, authors."""
    pf = _open(ensure(container))
    rg = index // ROW_GROUP
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
    table = _open(path).read(columns=["index", "date"])
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
    table = _open(ensure(container)).read(columns=["pmid", "authors"])
    out: dict[str, str] = {}
    for pmid, authors in zip(table.column("pmid").to_pylist(),
                             table.column("authors").to_pylist()):
        if pmid and authors:
            out[str(pmid)] = authors
    return out


def reset_caches() -> None:
    with _files_lock:
        _files.clear()
        _groups.clear()
        _fresh.clear()
        _dates.clear()
