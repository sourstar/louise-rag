"""Snapshot manifests and deltas — the reference-plus-chain model.

A frozen snapshot of a corpus is an immutable, auditable artifact; a live
corpus is not. Everything the P2P layer wants — torrenting, signing,
comparing your holdings against a peer's, repairing damage — needs the
first and cannot be done with the second. So a mutable source is modelled
as a dated REFERENCE plus an ordered chain of DELTAS, which is exactly how
NCBI ships PubMed (annual baseline + updatefiles) and how Wikimedia ships
Wikipedia (dated dumps).

THE MANIFEST IS A FILE, NOT A TABLE. Three reasons, in order of weight: it
is the thing peers exchange and sign, so it has to have a canonical byte
form; 24M documents do not fit in memory, so every operation here streams;
and it needs no schema change to a manifest.db that six collections are
already using.

Sorted by uid, one JSON object per line. Sorting is not cosmetic — the diff
is a two-pointer merge over two sorted streams, so an unsorted manifest
would silently produce a wrong delta rather than fail. write_manifest()
sorts in SQL and read paths verify the order.

WHAT A DELTA CARRIES, and why each part is not optional:

  parent   the digest of the manifest this delta applies TO. A delta
           applied to the wrong base produces a state no other peer has,
           silently, and every later audit inherits the error. Refusing is
           the only safe behaviour, so the parent digest is checked before
           anything is applied.
  removed  tombstones. app/plugins/cursor.py is explicit that discovery
           "never prunes unseen documents" — so without tombstones a
           retracted PubMed article or a deleted Wikipedia page stays in
           the corpus forever. For a medical corpus that is a correctness
           bug, not untidiness.
  versions chunk_version and cleaning_version. A delta computed under
           different chunking describes different chunks; applying it
           across that boundary would mix two incompatible segmentations
           of the same text (invariant 3).
"""
import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable, Iterator

FORMAT = "louise-snapshot/1"

ADDED = "added"
CHANGED = "changed"
REMOVED = "removed"


class SnapshotError(RuntimeError):
    """A snapshot or delta that cannot be trusted to describe what it says."""


@dataclass(frozen=True)
class Entry:
    """One document's presence in a snapshot. `content` is the hash of the
    document's cleaned text, which is what decides whether its vectors are
    still valid — not the file's mtime, and not its size."""
    uid: str
    content: str | None
    chunks: int = 0

    def as_line(self) -> str:
        return json.dumps({"uid": self.uid, "content": self.content,
                            "chunks": self.chunks},
                           sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class Change:
    kind: str          # added | changed | removed
    uid: str
    content: str | None = None
    chunks: int = 0


@dataclass(frozen=True)
class DeltaHeader:
    """Everything needed to decide whether a delta may be applied here,
    before a single change is read."""
    format: str
    collection: str
    plugin: str
    parent_snapshot: str | None
    parent_digest: str | None
    child_snapshot: str
    child_digest: str
    chunk_version: int
    cleaning_version: int
    space_id: str | None = None
    counts: dict = field(default_factory=dict)
    created_at: int = 0

    def as_line(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))


# ---------------- writing a snapshot manifest ----------------

def write_manifest(con, path: Path, *, in_scope_only: bool = True) -> str:
    """Stream this collection's current holdings to `path`; return its digest.

    Ordered by uid in SQL so the diff can be a merge rather than a load. Only
    in-scope documents by default: a document filtered out by the collection's
    scope is not part of what this node claims to hold.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    where = "WHERE in_scope=1" if in_scope_only else ""
    digest = hashlib.blake2b(digest_size=32)
    count = 0
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        for row in con.execute(
                f"SELECT uid, txt_hash, chunks FROM documents {where} ORDER BY uid"):
            line = Entry(uid=row["uid"], content=row["txt_hash"],
                          chunks=row["chunks"] or 0).as_line()
            f.write(line + "\n")
            digest.update(line.encode("utf-8") + b"\n")
            count += 1
    return digest.hexdigest()


def manifest_digest(path: Path) -> str:
    """The digest of a manifest AS WRITTEN.

    Computed over the file's bytes rather than over a re-parse, so a
    manifest that has been reformatted — even harmlessly — reads as a
    different manifest. Two peers comparing digests are asking "do we hold
    the identical artifact?", and the answer must not depend on whose JSON
    writer produced it.
    """
    digest = hashlib.blake2b(digest_size=32)
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def read_manifest(path: Path) -> Iterator[Entry]:
    """Stream a manifest, verifying it is sorted as it goes.

    Checked rather than assumed: the diff below is a merge, so an unsorted
    manifest would produce a plausible-looking WRONG delta instead of an
    error, and nothing downstream could tell.
    """
    previous = None
    with open(path, encoding="utf-8") as f:
        for number, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
                entry = Entry(uid=raw["uid"], content=raw.get("content"),
                               chunks=int(raw.get("chunks") or 0))
            except (ValueError, TypeError, KeyError) as e:
                raise SnapshotError(f"{path}:{number}: malformed entry ({e})") from None
            if previous is not None and entry.uid <= previous:
                raise SnapshotError(
                    f"{path}:{number}: manifest is not sorted by uid "
                    f"({entry.uid!r} after {previous!r}) — the diff is a merge "
                    f"and would silently produce a wrong delta")
            previous = entry.uid
            yield entry


# ---------------- diffing two snapshots ----------------

def diff(parent: Path, child: Path) -> Iterator[Change]:
    """Stream the changes that turn `parent` into `child`.

    A two-pointer merge over two sorted streams: constant memory whatever
    the corpus size, which is the only thing that works at 24M documents.
    """
    left = _peekable(read_manifest(parent))
    right = _peekable(read_manifest(child))
    while True:
        a, b = left.peek(), right.peek()
        if a is None and b is None:
            return
        if b is None or (a is not None and a.uid < b.uid):
            # present in the parent, gone from the child
            left.next()
            yield Change(REMOVED, a.uid)
        elif a is None or b.uid < a.uid:
            right.next()
            yield Change(ADDED, b.uid, b.content, b.chunks)
        else:
            left.next(); right.next()
            if a.content != b.content:
                yield Change(CHANGED, b.uid, b.content, b.chunks)


class _peekable:
    def __init__(self, it):
        self._it = iter(it)
        self._head = next(self._it, None)

    def peek(self):
        return self._head

    def next(self):
        head = self._head
        self._head = next(self._it, None)
        return head


# ---------------- writing and reading a delta ----------------

def write_delta(path: Path, header: DeltaHeader, changes: Iterable[Change]) -> dict:
    """Header line, then one line per change. Returns the counts, which the
    caller usually wants to fold back into the header."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    counts = {ADDED: 0, CHANGED: 0, REMOVED: 0}
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(header.as_line() + "\n")
        for change in changes:
            counts[change.kind] = counts.get(change.kind, 0) + 1
            f.write(json.dumps(
                {"k": change.kind, "uid": change.uid,
                 "content": change.content, "chunks": change.chunks},
                sort_keys=True, separators=(",", ":")) + "\n")
    return counts


def read_delta(path: Path) -> tuple[DeltaHeader, Iterator[Change]]:
    """(header, streaming changes). The header is read eagerly so a caller
    can refuse the delta before consuming any of it — which is the whole
    point of putting the parent digest in a header."""
    f = open(path, encoding="utf-8")
    first = f.readline()
    if not first.strip():
        f.close()
        raise SnapshotError(f"{path}: empty delta")
    try:
        raw = json.loads(first)
        header = DeltaHeader(**raw)
    except (ValueError, TypeError) as e:
        f.close()
        raise SnapshotError(f"{path}: malformed delta header ({e})") from None
    if header.format != FORMAT:
        f.close()
        raise SnapshotError(
            f"{path}: unknown delta format {header.format!r} (this node "
            f"speaks {FORMAT!r})")

    def _changes():
        try:
            for number, line in enumerate(f, start=2):
                line = line.strip()
                if not line:
                    continue
                try:
                    raw_change = json.loads(line)
                    yield Change(kind=raw_change["k"], uid=raw_change["uid"],
                                  content=raw_change.get("content"),
                                  chunks=int(raw_change.get("chunks") or 0))
                except (ValueError, TypeError, KeyError) as e:
                    raise SnapshotError(
                        f"{path}:{number}: malformed change ({e})") from None
        finally:
            f.close()

    return header, _changes()


def build_delta(parent_manifest: Path, child_manifest: Path, out: Path, *,
                collection: str, plugin: str,
                parent_snapshot: str | None, child_snapshot: str,
                chunk_version: int, cleaning_version: int,
                space_id: str | None = None,
                created_at: int | None = None) -> DeltaHeader:
    """The whole operation: diff two manifests and write the delta between
    them, with a header that binds it to its parent."""
    header = DeltaHeader(
        format=FORMAT, collection=collection, plugin=plugin,
        parent_snapshot=parent_snapshot,
        parent_digest=manifest_digest(parent_manifest),
        child_snapshot=child_snapshot,
        child_digest=manifest_digest(child_manifest),
        chunk_version=chunk_version, cleaning_version=cleaning_version,
        space_id=space_id,
        created_at=int(created_at if created_at is not None else time.time()))
    counts = write_delta(out, header, diff(parent_manifest, child_manifest))
    # rewrite with the counts folded in: they are what an operator reads
    # before deciding to apply a delta, so they belong in the header rather
    # than only being discoverable by consuming the whole file
    final = DeltaHeader(**{**asdict(header), "counts": counts})
    _rewrite_header(out, final)
    return final


def _rewrite_header(path: Path, header: DeltaHeader) -> None:
    path = Path(path)
    body = path.read_text(encoding="utf-8").split("\n", 1)[1]
    path.write_text(header.as_line() + "\n" + body, encoding="utf-8", newline="\n")


# ---------------- admission ----------------

def check_applicable(header: DeltaHeader, *, local_digest: str | None,
                     collection: str, chunk_version: int,
                     cleaning_version: int) -> None:
    """Refuse a delta that does not describe this node's situation.

    Every one of these is silent corruption if skipped. A delta applied to
    the wrong parent yields a state no peer shares and no audit can detect
    afterwards; a delta from another collection applies plausible-looking
    changes to the wrong uids; a delta computed under different chunking
    mixes two incompatible segmentations of the same text.
    """
    if header.collection != collection:
        raise SnapshotError(
            f"delta is for collection {header.collection!r}, not {collection!r}")
    if header.chunk_version != chunk_version:
        raise SnapshotError(
            f"delta was computed at CHUNK_VERSION {header.chunk_version}, this "
            f"node is at {chunk_version} — the chunks it describes are not the "
            f"chunks this node would produce")
    if header.cleaning_version != cleaning_version:
        raise SnapshotError(
            f"delta was computed at cleaning version {header.cleaning_version}, "
            f"this node is at {cleaning_version}")
    if header.parent_digest is not None and local_digest is not None \
            and header.parent_digest != local_digest:
        raise SnapshotError(
            f"delta applies to snapshot {header.parent_snapshot!r} "
            f"(digest {header.parent_digest[:16]}…) but this node holds "
            f"{local_digest[:16]}… — applying it would produce a state no "
            f"other peer has, undetectably")


def chain(headers: Iterable[DeltaHeader]) -> list[DeltaHeader]:
    """Order deltas into an applicable chain, or refuse.

    Returns them parent-to-child. A gap means a missing delta, and applying
    what is left would skip changes without anything noticing — so a broken
    chain is an error rather than a best effort.
    """
    remaining = list(headers)
    if not remaining:
        return []
    by_parent: dict[str | None, DeltaHeader] = {}
    for h in remaining:
        if h.parent_digest in by_parent:
            raise SnapshotError(
                f"two deltas claim the same parent {h.parent_digest} — the "
                f"chain forks and there is no way to choose")
        by_parent[h.parent_digest] = h

    children = {h.child_digest for h in remaining}
    roots = [h for h in remaining if h.parent_digest not in children]
    if len(roots) != 1:
        raise SnapshotError(
            f"expected exactly one chain start, found {len(roots)} — the "
            f"deltas do not form a single line")

    ordered = [roots[0]]
    while (nxt := by_parent.get(ordered[-1].child_digest)) is not None:
        ordered.append(nxt)
    if len(ordered) != len(remaining):
        raise SnapshotError(
            f"the chain covers {len(ordered)} of {len(remaining)} deltas — "
            f"one is missing, and skipping it would lose changes silently")
    return ordered


# ---------------- where snapshots live on this node ----------------

SNAPSHOT_DIRNAME = "snapshots"


def snapshot_dir(collection) -> Path:
    """Beside the manifest, so wiping a collection's data wipes its recorded
    snapshots with it — they describe that manifest and nothing else."""
    return Path(collection.manifest_path).parent / SNAPSHOT_DIRNAME


def manifest_path_for(collection, snapshot: str) -> Path:
    return snapshot_dir(collection) / f"{snapshot}.jsonl"


def record(con, collection, snapshot: str) -> tuple[Path, str]:
    """Freeze what this collection currently holds as snapshot `snapshot`.

    Deliberately explicit rather than automatic on every scan: a manifest is
    a claim about a moment, and writing one during an in-progress ingest
    would freeze a half-applied state that no other node will ever match.
    """
    path = manifest_path_for(collection, snapshot)
    digest = write_manifest(con, path)
    return path, digest


def recorded(collection) -> list[tuple[str, Path]]:
    """(snapshot, path) for every manifest this node has recorded, oldest
    first. Snapshot ids are dump dates, so lexical order is chronological."""
    directory = snapshot_dir(collection)
    if not directory.is_dir():
        return []
    return sorted((p.stem, p) for p in directory.glob("*.jsonl"))


def vector_uids(delta_path: Path) -> list[str]:
    """The uids a VECTOR delta must cover: added and changed, never removed.

    A deleted document has no vectors to ship — it has vectors to delete,
    which is the tombstone's job on the consumer side, not a bundle's.
    """
    _, changes = read_delta(delta_path)
    return sorted({c.uid for c in changes if c.kind in (ADDED, CHANGED)})


# Applying the two planes in the wrong order is the most likely operational
# mistake, and it fails in a way that looks like a bad bundle rather than a
# sequencing error, so it is named here once:
#
#   1. corpus delta   — the node's text becomes the new revision
#   2. vector delta   — the bundle's per-uid raw hashes are checked against
#                       that text (bundles/possession.py::verify_map) and
#                       admitted only if they agree
#
# Reversed, the possession check compares new vectors against OLD text,
# correctly refuses every document, and reports "these vectors were computed
# from a different revision of the text" — which is true, and reads like the
# producer's fault.
APPLY_ORDER = ("corpus delta", "vector delta")
