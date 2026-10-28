"""Stage-level plugin access with protocol fallback.

The per-stage callers (worker, importer, contribute, summarize) were written
against Gutenberg's lazy helper surface (read_metadata/read_chapters/
read_text) and silently required it from every plugin — observed live as 30k
pubmed rdf failures. These wrappers use the lazy helpers when a plugin
offers them (they exist precisely to keep gutenberg's per-stage I/O cost
minimal) and fall back to the CorpusSource protocol (fetch → parse →
CanonicalDoc) for everyone else. The DocRef is reconstructed from the
manifest row; §2 makes the locator plugin-opaque, so whatever addressing the
plugin encoded at discovery (e.g. pubmed's container::index) round-trips.
"""
import json
from pathlib import Path

from .plugins.base import CanonicalDoc, DocRef


def canonical_doc(plugin, row) -> CanonicalDoc:
    ref = DocRef(source_id=plugin.source_id, native_id=str(row["native_id"]),
                 locator=row["path"])
    return plugin.parse(plugin.fetch(ref))


#: A plugin whose locator addresses a member of a container spells the
#: separator its own way — pubmed's is `::`. 09 §9.5 fixes ONE spelling on
#: the wire, `#`, because the record's `origin` is read by a verifier that
#: does not have this plugin. Read as an attribute rather than added to the
#: protocol: the protocol version is inside `collection_id` (06 §6.1), so
#: a method here would move every collection's identity.
DEFAULT_MEMBER_SEP = "#"


def origin_locator(plugin, locator: str, base) -> str:
    """09 §9.5's `origin` for `locator` (see `_origin_one`). A unit in
    several PIECES — an archive article whose rows straddle two shards
    (wikipedia's layer 1) — locates each piece in its own file, joined by
    the plugin's `LOCATOR_SEGMENT_SEP`."""
    seg = getattr(plugin, "LOCATOR_SEGMENT_SEP", None)
    text = str(locator)
    if seg and seg in text:
        return seg.join(_origin_one(plugin, part, base) for part in text.split(seg))
    return _origin_one(plugin, text, base)


def _origin_one(plugin, locator: str, base) -> str:
    """09 §9.5's `origin` — "the locator of this record's own origin bytes".

    "their path relative to the origin base or their URL, and where the
    document is a member of a container the path followed by `#` and the
    plugin's member index (06 §6.4); NEVER AN ABSOLUTE LOCAL PATH".

    The last clause is the whole point and is why this is a conversion
    rather than a copy. A plugin's `locator` is opaque and is an absolute
    filesystem path today, because it addresses THIS machine's copy; the
    record travels, and `/mnt/ssd4/corpora/gutenberg/1/3/4/1342` locates
    nothing on the peer that receives it. Relative to the origin base, it
    locates the same bytes for everyone who fetched from the same origin —
    which is what makes `records-vs-origin` a check a stranger can run.
    """
    sep = getattr(plugin, "LOCATOR_SEP", None) or DEFAULT_MEMBER_SEP
    text = str(locator)
    # A member of several fields — wikipedia's stream `start::end` — is
    # split off at the Nth separator from the right, not the last: the last
    # gave `…bz2::570#711904`, a path that exists nowhere.
    fields = int(getattr(plugin, "LOCATOR_MEMBER_FIELDS", 1) or 1)
    parts = text.rsplit(sep, fields) if sep != DEFAULT_MEMBER_SEP else [text]
    if len(parts) == fields + 1:
        path, member = parts[0], sep.join(parts[1:])
    else:
        path, member = text, ""
    if "://" in path:                       # already a URL: §9.5 admits one
        return path + (DEFAULT_MEMBER_SEP + member if member else "")
    p = Path(path)
    if base is not None:
        rel_p = relative_to_base(p, Path(base))
        if rel_p is None:
            # NOT UNDER THE ORIGIN BASE, by any path that reaches it. This
            # used to fall back to the file's bare NAME — "000.parquet#0::15"
            # — which locates nothing and, where every language of an
            # archive has its own 000.parquet, cannot even say which file
            # (wikipedia-v8, 2026-10-28: every record of the archive layer,
            # its manifest paths carried from the old collection under
            # /corpora/wikivec; records-vs-origin found none of 52,440).
            # 09 §9.5: the origin locates the record's own bytes, or the
            # record is not made.
            raise OriginOutsideBase(
                f"{path}: not under the origin base {base}, by its own path, "
                f"its resolved path or a link the base holds; a record must "
                f"locate its own origin bytes (09 §9.5, invariant 11)")
        p = rel_p
    rel = p.as_posix()
    # 06 §6.4: a record whose text a credited archive supplies is located
    # IN that archive — a URL, never a path under this node's copy of it.
    # Optional and read like LOCATOR_SEP, for the same reason.
    url_of = getattr(plugin, "origin_url_of", None)
    url = url_of(rel) if url_of else None
    return (url or rel) + (DEFAULT_MEMBER_SEP + member if member else "")


class OriginOutsideBase(ValueError):
    """A document's origin bytes are not under the collection's origin base
    by any path that reaches it: no 09 §9.5 `origin` can be written for it."""


#: base -> [(name, resolved target)] of the base's symbolic links, read once
_base_links: dict[str, list[tuple[str, Path]]] = {}


def relative_to_base(p: Path, base: Path) -> Path | None:
    """`p` relative to the origin base, or None when nothing reaches it.

    In order: `p` as given; both resolved (a base that is itself a link);
    and through a symbolic link the base holds — the node's
    `/corpora/wikipedia/data` is a link to `/corpora/wikivec/data`, so an
    archive file found under the second is `data/<lang>/<file>` under the
    first. A path already relative is taken as relative to the base."""
    if not p.is_absolute():
        return p
    try:
        return p.relative_to(base)
    except ValueError:
        pass
    try:
        rp, rb = p.resolve(), base.resolve()
    except OSError:
        return None
    try:
        return rp.relative_to(rb)
    except ValueError:
        pass
    key = str(base)
    if key not in _base_links:
        links = []
        try:
            for child in base.iterdir():
                if child.is_symlink():
                    try:
                        links.append((child.name, child.resolve()))
                    except OSError:
                        continue
        except OSError:
            pass
        _base_links[key] = sorted(links)
    for name, target in _base_links[key]:
        try:
            return Path(name) / rp.relative_to(target)
        except ValueError:
            continue
    return None


def local_locator(plugin, origin: str, base) -> tuple[Path, str]:
    """`origin_locator` read back, piece by piece where the unit has
    several; the path returned is the first piece's file."""
    seg = getattr(plugin, "LOCATOR_SEGMENT_SEP", None)
    if seg and seg in origin:
        pieces = [_local_one(plugin, part, base) for part in origin.split(seg)]
        return pieces[0][0], seg.join(loc for _, loc in pieces)
    return _local_one(plugin, origin, base)


def _local_one(plugin, origin: str, base) -> tuple[Path, str]:
    """`(origin file under base, the plugin's locator)` — `origin_locator`
    read back: the relative path joined to `base`, and the member after
    §9.5's `#` in the plugin's own spelling again."""
    path, _, member = origin.partition(DEFAULT_MEMBER_SEP)
    sep = getattr(plugin, "LOCATOR_SEP", None) or DEFAULT_MEMBER_SEP
    if "://" in path:
        # an archive's URL (06 §6.4) back to this node's copy of that file
        rel_of = getattr(plugin, "local_rel_of", None)
        rel = rel_of(path) if rel_of else None
        if rel is not None:
            path = rel
    local = Path(base) / path
    return local, str(local) + (f"{sep}{member}" if member else "")


def origin_unit(plugin, origin: str, base) -> tuple[str, int | None]:
    """`(the origin unit a locator points into, its size in bytes)` — the FILE
    part of the locator, never the member after `#`: what has to be fetched to
    answer for the record. A unit of several pieces is all of them, and its
    size their sum. None where a piece is not under `base`: nothing to weigh.

    For `records-vs-origin`'s bound (app/hold/check.py): a check reads a
    seeded sample of these, and no more of the origin than the bound allows."""
    seg = getattr(plugin, "LOCATOR_SEGMENT_SEP", None)
    parts = origin.split(seg) if seg and seg in origin else [origin]
    names, total = [], 0
    for part in parts:
        local, _ = _local_one(plugin, part, base)
        try:
            names.append(local.relative_to(base).as_posix())
        except ValueError:
            names.append(local.as_posix())
        try:
            size = local.stat().st_size
        except OSError:
            size = None
        total = None if total is None or size is None else total + size
    return "+".join(names), total


def metadata(plugin, row) -> dict:
    """The rdf stage's fields for one document — and, under `issues`, the
    origin's inconsistencies the read met (06 §6.1, amended 2026-10-25),
    which the stage records for the codes `metadata_checks` names. A
    plugin's lazy `read_metadata` that says nothing about issues returns no
    `issues` key, and nothing is recorded for it."""
    if hasattr(plugin, "read_metadata"):
        return plugin.read_metadata(Path(row["path"]), int(row["native_id"]))
    doc = canonical_doc(plugin, row)
    meta = {"title": doc.title, "authors": "; ".join(doc.creators),
            "lang": doc.language, "subjects": "|".join(doc.subjects),
            "bookshelves": str(doc.extra.get("bookshelves", "")),
            "issues": list(doc.issues or ())}
    # one record per Expression (app/validity.py): the start of validity,
    # the Work and whether the version has text, for the rdf stage to record
    from app import validity
    v = validity.from_plugin(plugin)
    if v is not None:
        got = validity.of_doc(doc, v, plugin)
        if got is not None:
            meta["validity"] = got
    return meta


def metadata_checks(plugin) -> frozenset[str]:
    """The issue codes `metadata` examines for this plugin: every code the
    plugin declares when it falls back to the full parse, and only what a
    lazy `read_metadata` declares it reads (`METADATA_ISSUES`) otherwise —
    so the rdf stage never clears what only the full parse can see."""
    if hasattr(plugin, "read_metadata"):
        return frozenset(getattr(plugin, "METADATA_ISSUES", ()) or ())
    return frozenset(getattr(plugin, "ISSUES", None) or {})


def work_identity(meta: dict) -> str:
    """`documents.work` for one document: what work it is an edition of,
    as JSON, or "" when its plugin does not say.

    A plugin that knows (gutenberg, from the MARC relator roles) returns
    `work_author`, `work_subjects` and `translated` in its metadata; every
    other plugin returns none of them and gets "", which never groups and
    so never suppresses anything (app/work_dedup.py). One place, so a new
    plugin opts in by naming the three keys and nothing else changes.
    """
    author = str(meta.get("work_author") or "")
    subjects = [str(s) for s in (meta.get("work_subjects") or ()) if s]
    if not author or not subjects:
        return ""
    return json.dumps({"author": author, "subjects": subjects,
                       "translated": bool(meta.get("translated"))})


def sections(plugin, row) -> list[tuple[str, str]]:
    if hasattr(plugin, "read_chapters"):
        return plugin.read_chapters(Path(row["path"]), int(row["native_id"]))
    return canonical_doc(plugin, row).sections


def full_text(plugin, row) -> str:
    if hasattr(plugin, "read_text"):
        return plugin.read_text(Path(row["path"]), int(row["native_id"]))
    return "\n\n".join(body for _, body in sections(plugin, row))


def prepare(plugin, row) -> str | None:
    """Build whatever derived cache this document's CONTAINER needs, ahead
    of the stage that will read it (app/parallel.py's prefetch). Optional:
    a plugin without the hook has nothing to prepare. Returns the key
    prepared, for the log."""
    if not hasattr(plugin, "prepare"):
        return None
    plugin.prepare(row)
    return prepare_key(plugin, row)


def prepare_key(plugin, row) -> str | None:
    """What `prepare` would work on — the CONTAINER, not the document, so a
    prefetch is submitted once per container instead of once per row.
    Plugins with no derived cache return None and are never submitted."""
    if not hasattr(plugin, "prepare"):
        return None
    if hasattr(plugin, "prepare_key"):
        return plugin.prepare_key(row)
    return str(row.get("path") or "") or None


def work_span(plugin, row) -> tuple[str, int] | None:
    """`(shared-unit key, its cost in bytes)` for one row, or None when the
    plugin has no such structure — what the PARSE POOL cuts a batch along.

    A document is often not the unit of work. wikipedia's pages arrive in
    multistream bz2 chunks of ~100 pages: whoever touches one page of a
    chunk decompresses the whole ~1 MB chunk, and `source._stream_pages`
    caches exactly one of them PER PROCESS. So two facts follow, and both
    are about the pool:

      LOCALITY. A chunk split across two children is decompressed twice.
      Cutting a batch into 8 parts per child instead of 2 put a third of a
      chunk in each part and cost 4.6x on the node (2026-09-23); the cost
      is in this function's absence, not in the number of parts.

      BALANCE. Chunks differ in size, so parts of equal COUNT are not parts
      of equal WORK, and the batch ends with ten children idle waiting for
      one. Measured the same night: 3.7 of 11 children running on average,
      with bursts to 11 and troughs at 0.

    The key says which rows share work; the cost says how much that share
    is, and it is charged ONCE per unit, not per row. Plugins whose
    documents are independent files (gutenberg, pubmed) return None and
    keep the plain equal-count cut, which for them is equal work.
    """
    if hasattr(plugin, "work_span"):
        return plugin.work_span(row)
    return None


class FiguresUnsupported(RuntimeError):
    """A plugin yielded figures and cannot hand over their bytes.

    Not a skip and not a warning. A record line naming a plate the
    container does not hold is a record that fails `hold check`'s `figures`
    row on every peer that receives it, and the peer cannot repair it — so
    the build stops here, where the plugin author can be named.
    """


def figure_bytes(plugin, row, hrefs: dict[str, str]) -> dict[str, bytes]:
    """`{hash: href}` for ONE record -> `{hash: bytes}`, as the origin
    ships them.

    PLURAL, and that is the point: the origin unit is opened once for all
    of a record's plates. Gutenberg's corpus holds ~700,000 images over
    ~48,000 books, so a per-plate call would re-open the same zip once per
    picture — a per-item cost scaling with the corpus.

    An `href` is whatever the plugin put in `Figure.href` — the path inside
    the epub for gutenberg, and opaque to everyone else, exactly as the
    locator is (06 §6.1). The record line does not carry it: a verifier
    checks a plate by its hash against the container and never goes back
    to the origin unit for it.
    """
    if hasattr(plugin, "read_figures"):
        return plugin.read_figures(Path(row["path"]), int(row["native_id"]),
                                   dict(hrefs))
    raise FiguresUnsupported(
        f"the {plugin.source_id!r} plugin yields figures but implements no "
        f"read_figures(book_dir, native_id, {{hash: href}}) — a record "
        f"cannot name a plate this node is unable to carry (09 §9.5)")
