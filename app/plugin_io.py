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
    path, found, member = text.rpartition(sep) if sep != DEFAULT_MEMBER_SEP \
        else (text, "", "")
    if not found:
        path, member = text, ""
    if "://" in path:                       # already a URL: §9.5 admits one
        return path + (DEFAULT_MEMBER_SEP + member if member else "")
    p = Path(path)
    if base is not None:
        try:
            p = p.relative_to(Path(base))
        except ValueError:
            # Not under this peer's origin base — a hand-placed file, or a
            # corpus moved since discovery. Fall back to the name rather
            # than emitting an absolute path, which §9.5 forbids outright.
            p = Path(p.name)
    return p.as_posix() + (DEFAULT_MEMBER_SEP + member if member else "")


def metadata(plugin, row) -> dict:
    if hasattr(plugin, "read_metadata"):
        return plugin.read_metadata(Path(row["path"]), int(row["native_id"]))
    doc = canonical_doc(plugin, row)
    return {"title": doc.title, "authors": "; ".join(doc.creators),
            "lang": doc.language, "subjects": "|".join(doc.subjects),
            "bookshelves": str(doc.extra.get("bookshelves", ""))}


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
