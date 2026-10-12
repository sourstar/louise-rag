"""The SHARD on disk — what a peer fetches and shares.

docs/02-collections.md §2.6 gives the tree; docs/09-formats.md §9.16 fixes
the member names, the roots and the torrents. Pure functions over names and
bytes: nothing here reads a manifest, a cursor or an acknowledgement, all of
which sit BESIDE the SHARD and never travel (07 §7.8).

A collection on disk is a SHARD — *distributed RAG*. The SHARD is the copy:
`<LOUISE_ROOT>/collections/<name>/shard/`, exactly what a peer shares.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from .codex import Codex, CodexError, canonical_json, digest

SHARD_DIR = "shard"

#: The kinds are closed (02 §2.6); `--kinds` takes these names (07 §7.3).
MEMBER_KINDS = ("records", "vectors", "buckets", "index", "relations",
                "bundle", "centres", "calibration", "terms", "seals", "origin",
                # 09 §9.5: the plates of a `record_line` 2 collection, one
                # container per slice, addressed by the record's sha256.
                "figures")

#: Leaves are SHA-256 of consecutive 16 KiB blocks of the file (09 §9.5).
BLOCK = 16 * 1024

#: Every file of a member is one BitTorrent v2 torrent (09 §9.16).
PIECE_LENGTH = 16 * 1024 * 1024

_SEGMENT = r"[A-Za-z0-9=_\-][A-Za-z0-9=_.\-]*"
_SLICE_RE = re.compile(rf"^{_SEGMENT}(/{_SEGMENT})*$")
_SPACE_SHAPED = re.compile(r"^[0-9a-f]{16}$")
_SPACE_RE = _SPACE_SHAPED


class ShardError(CodexError):
    """A name, a path or a member that the format does not allow."""


# --------------------------------------------------------------------------
# names

def check_slice(name: str) -> str:
    """A slice name is one or more path segments (09 §9.3).

    A segment of exactly 16 lowercase hexadecimal characters is refused: it
    is the shape of a `<space>` segment. It was the one collision of 09
    §9.16's table (`index-<slice>` against `index-<space>`) until the text
    index left the member list (2026-10-08); the refusal stays, since a
    slice that reads as a digest is ambiguous in every member name and no
    source needs one.
    """
    if not _SLICE_RE.match(name):
        raise ShardError(f"{name!r} is not a slice name (09 §9.3)")
    for seg in name.split("/"):
        if _SPACE_SHAPED.match(seg):
            raise ShardError(
                f"slice segment {seg!r} is sixteen lowercase hex characters, "
                f"which is the shape of a <space> segment; `index-{name}` "
                f"would collide with a vector index's member name (09 §9.16)")
    return name


def check_space(space: str) -> str:
    """The `<space>` segment: the first 16 hex digits of `space_id` (09 §9.3)."""
    if not _SPACE_RE.match(space):
        raise ShardError(f"{space!r} is not a <space> segment: sixteen "
                        f"lowercase hexadecimal characters")
    return space


def records_member(slice_: str) -> str:
    return f"records-{check_slice(slice_)}"


def relations_member(slice_: str) -> str:
    return f"relations-{check_slice(slice_)}"


def vectors_member(space: str, slice_: str) -> str:
    return f"vectors-{check_space(space)}-{check_slice(slice_)}"


def buckets_member(space: str, index_version: int, group: int) -> str:
    return f"buckets-{check_space(space)}-v{index_version}-g{group:02d}"


def bundle_member(space: str, bundle_id: str) -> str:
    return f"bundle-{check_space(space)}-{_hex(bundle_id)}"


def centres_member(space: str, index_version: int) -> str:
    return f"centres-{check_space(space)}-v{index_version}"


def vector_index_member(space: str) -> str:
    """`vectors/<space>/index.json`: the one `index-` member (09 §9.16)."""
    return f"index-{check_space(space)}"


def calibration_member(set_id: str) -> str:
    return f"calibration-{_segment(set_id)}"


def seals_member(snapshot_id: str) -> str:
    return f"seals-{snapshot_id}"


def origin_member(slice_: str) -> str:
    return f"origin-{check_slice(slice_)}"


TERMS_MEMBER = "terms"
VALIDATION_MEMBER = "validation"


def _hex(identity: str) -> str:
    return identity.split(":", 1)[1] if ":" in identity else identity


def _segment(identity: str) -> str:
    """09 §9.1: "where a digest names a directory — the `<space>` segment
    under `vectors/`, the `<set>` segment of the calibration member — the
    segment is the first 16 hexadecimal" digits. §9.12's worked example is
    `calibration/6b1a3f0e9c2d4b7f.safetensors`, which is those 16 and not
    the whole digest."""
    return _hex(identity)[:16]


# --------------------------------------------------------------------------
# paths

def shard_dir(collection_dir: Path) -> Path:
    return collection_dir / SHARD_DIR


def shard_path(slice_: str, n: int, superseded_by: str | None = None) -> str:
    """`records/<slice>/NNNNN.jsonl.zst`, and
    `records/<slice>/NNNNN.<snapshot id>.jsonl.zst` for a superseded version:
    a superseded shard keeps its old bytes beside the new ones, so every
    snapshot in the chain stays servable and checkable (02 §2.2, 09 §9.5)."""
    check_slice(slice_)
    tail = f".{superseded_by}" if superseded_by else ""
    return f"records/{slice_}/{n:05d}{tail}.jsonl.zst"


def relations_path(slice_: str, n: int) -> str:
    """One relations shard beside each record shard of the same slice and
    number (09 §9.14)."""
    check_slice(slice_)
    return f"relations/{slice_}/{n:05d}.jsonl.zst"


def figures_path(slice_: str, n: int) -> str:
    """One figures container beside each record shard of the same slice and
    number (09 §9.5). Named for the SHARD, never for a figure: a figure is
    addressed by hash INSIDE the container, so §9.1's "path segments from
    digests" rule never reaches one."""
    check_slice(slice_)
    return f"figures/{slice_}/{n:05d}.fig.zst"


def index_path(slice_: str, n: int) -> str:
    check_slice(slice_)
    return f"index/{slice_}/{n:05d}"


def snapshot_path(snapshot_id: str) -> str:
    return f"snapshots/{snapshot_id}.json"


def snapshot_sig_path(snapshot_id: str) -> str:
    return f"snapshots/{snapshot_id}.sig.json"


def centres_path(space: str, index_version: int) -> str:
    return f"vectors/{check_space(space)}/centres/v{index_version}.safetensors"


def vector_index_path(space: str) -> str:
    return f"vectors/{check_space(space)}/index.json"


def calibration_paths(set_id: str) -> tuple[str, str]:
    h = _segment(set_id)
    return f"calibration/{h}.safetensors", f"calibration/{h}.sig.json"


def seals_path(snapshot_id: str) -> str:
    return f"seals/{snapshot_id}.json"


def trusted_list_path(digest: str) -> str:
    """`seals/lists/<sha256 hex>.xml`: a trusted list the seals name, by its
    hash, travelling in the seals member (09 §9.16, amended 2026-10-08)."""
    return f"seals/lists/{_hex(digest)}.xml"


CODEX_PATH = "codex.json"
TERMS_PATH = "LICENSE-TERMS"
VALIDATION_PATH = "validation.json"


# --------------------------------------------------------------------------
# what may be fetched before the acknowledgement

#: Exactly these, for a yellow- or red-class collection, until the
#: acknowledgement of 04 §4.4 is recorded (02 §2.6, 07 §7.3, 09 §9.13).
#: `validation.json` is NOT among them: it is the Codex's own `validation`
#: block written out, so a peer holding `codex.json` already has its bytes.
PRE_ACK_PATHS = (CODEX_PATH, "snapshots/*", TERMS_PATH)


def fetchable_before_ack(codex: Codex) -> tuple[str, ...]:
    """A green collection has no terms to acknowledge and nothing is
    withheld; every other class is held to `PRE_ACK_PATHS` until the ack
    exists (invariant 9: however the data arrived)."""
    if codex.license.cls == "green":
        return ()          # nothing is gated
    return PRE_ACK_PATHS


def gated_before_ack(codex: Codex, path: str) -> bool:
    if codex.license.cls == "green":
        return False
    if path == CODEX_PATH or path == TERMS_PATH:
        return False
    return not path.startswith("snapshots/")


# --------------------------------------------------------------------------
# roots

def pieces_root(data: bytes) -> str:
    """The BitTorrent v2 pieces root of a file (BEP 52; 09 §9.5).

    Leaves are SHA-256 of consecutive 16 KiB blocks, the last block as long
    as it is; the leaf count is padded to the next power of two with leaves
    of 32 zero bytes; each parent is SHA-256 of its two children
    concatenated; a file of at most one block has its single leaf as root.

    Implemented here rather than taken from libtorrent because `roots` is a
    check (03 §3.2): a peer must be able to say what a file's root should be
    without a torrent session, and without trusting one.
    """
    leaves = [hashlib.sha256(data[i:i + BLOCK]).digest()
              for i in range(0, len(data), BLOCK)] or [hashlib.sha256(b"").digest()]
    if len(leaves) == 1:
        return "sha256:" + leaves[0].hex()
    width = 1
    while width < len(leaves):
        width *= 2
    leaves += [bytes(32)] * (width - len(leaves))
    while len(leaves) > 1:
        leaves = [hashlib.sha256(leaves[i] + leaves[i + 1]).digest()
                  for i in range(0, len(leaves), 2)]
    return "sha256:" + leaves[0].hex()


def pieces_root_of(path: Path) -> str:
    """`pieces_root` of a file, read a block at a time: the same root, in
    memory bounded by the tree's height rather than by the file — a torrent
    file is checked on disk before it is moved into place (2026-10-08), and
    those run to tens of GB. Leaves are folded as they come (a stack of
    complete subtrees); the padding to the next power of two is folded
    from precomputed all-zero subtrees, never leaf by leaf."""
    size = Path(path).stat().st_size
    if size <= BLOCK:
        return pieces_root(Path(path).read_bytes())
    leaves = -(-size // BLOCK)
    height = (leaves - 1).bit_length()          # width = 2 ** height
    zero = [bytes(32)]
    for _ in range(height):
        zero.append(hashlib.sha256(zero[-1] + zero[-1]).digest())
    stack: list[tuple[int, bytes]] = []

    def push(level: int, h: bytes) -> None:
        while stack and stack[-1][0] == level:
            h = hashlib.sha256(stack.pop()[1] + h).digest()
            level += 1
        stack.append((level, h))

    with open(path, "rb") as f:
        for _ in range(leaves):
            push(0, hashlib.sha256(f.read(BLOCK)).digest())
    while not (len(stack) == 1 and stack[0][0] == height):
        push(stack[-1][0], zero[stack[-1][0]])
    return "sha256:" + stack[0][1].hex()


@dataclass(frozen=True)
class MemberFile:
    path: str          # SHARD-relative
    root: str          # sha256:…
    bytes: int

    def to_list(self) -> list:
        return [self.path, self.root, self.bytes]


def member_root(files: Sequence[MemberFile]) -> str:
    """A member of one file has that file's pieces root as its root; a member
    of several has `H` over the canonical JSON of `[[path, root, bytes], …]`
    in path byte order (09 §9.16)."""
    if not files:
        raise ShardError("a member has at least one file")
    if len(files) == 1:
        return files[0].root
    ordered = sorted(files, key=lambda f: f.path.encode("utf-8"))
    return digest(canonical_json([f.to_list() for f in ordered]))


def member_files(shard_tree: Path, paths: Iterable[str]) -> tuple[MemberFile, ...]:
    out = []
    for p in paths:
        f = shard_tree / p
        if not f.is_file():
            raise ShardError(f"{p}: not in the SHARD at {shard_tree}")
        b = f.read_bytes()
        out.append(MemberFile(p, pieces_root(b), len(b)))
    return tuple(out)


# --------------------------------------------------------------------------
# torrents

def info_dict(member: str, file: MemberFile) -> dict:
    """The info dictionary of one file's torrent (09 §9.16), as a mapping
    ready to bencode. It contains exactly these keys, so the infohash is a
    function of the file's path, size and root alone and two honest
    publishers produce one infohash: no `private`, no `creation date`, no
    `created by`, no trackers inside `info`.
    """
    segments = file.path.split("/")
    tree: dict = {"": {"length": file.bytes,
                       "pieces root": bytes.fromhex(_hex(file.root))}}
    for seg in reversed(segments):
        tree = {seg: tree}
    return {"meta version": 2, "name": member,
            "piece length": PIECE_LENGTH, "file tree": tree}


# --------------------------------------------------------------------------
# the tree

#: Every directory the format defines, in the order 02 §2.6 lists them.
TREE = ("snapshots", "calibration", "seals", "records", "relations",
        "vectors")


def scaffold(shard_tree: Path) -> Path:
    """Make the SHARD's directories. Files arrive with the steps that write
    them; a directory that stays empty is not a member and is not served."""
    shard_tree.mkdir(parents=True, exist_ok=True)
    for d in TREE:
        (shard_tree / d).mkdir(exist_ok=True)
    return shard_tree


def superseded_name(target: Path, snapshot_id: str) -> Path:
    """`NNNNN.<snapshot id>.jsonl.zst` (or `.fig.zst`) beside `target`:
    02 §2.3's name for a version a re-issue replaced."""
    stem, dot, rest = target.name.partition(".")
    return target.with_name(f"{stem}.{snapshot_id}{dot}{rest}")


def last_listing(shard_dir: Path, rel: str) -> tuple[str, str, int] | None:
    """(snapshot id, root, bytes) of the newest snapshot held in `shard_dir`
    that lists `rel` — the version the SHARD holds at that path — or None."""
    from . import snapshot as sn
    d = Path(shard_dir) / "snapshots"
    ids = sorted((p.stem for p in d.glob("*.json") if not p.name.endswith(".sig.json")),
                 reverse=True) if d.is_dir() else []
    for sid in ids:
        try:
            snap = sn.load(d / f"{sid}.json")
        except Exception:                                   # noqa: BLE001 — skip one unreadable file
            continue
        for e in (*snap.shards, *snap.relations, *snap.figures):
            if e.path == rel:
                return sid, e.root, int(e.bytes)
    return None


def keep_superseded(target: Path, shard_dir: Path | None, new_root: str) -> Path | None:
    """Before a new version (`new_root`) replaces `target`: when the newest
    held snapshot listing that path names other bytes, and the file there is
    that version (its size), rename it to its superseded name and keep it
    (02 §2.3: "the version it supersedes is renamed with the id of the last
    snapshot that listed it and kept, so an old snapshot can still be served
    and checked"). Bytes no snapshot lists are nobody's version and are
    replaced. The old version's root comes from the snapshot, never from
    hashing the file: a monthly rebuild promotes every part and plate
    container, and re-reading them all (gutenberg: ~95 GB) per promotion
    would be the cost of the rule. The moved path, or None.

    Until 2026-10-12 nothing did this: the name existed
    (`shard_path(..., superseded_by=)`) and every promotion overwrote, on
    the producer and on every peer that fetched."""
    if shard_dir is None or not target.is_file():
        return None
    listed = last_listing(shard_dir, target.relative_to(shard_dir).as_posix())
    if listed is None:
        return None
    sid, old_root, old_bytes = listed
    if old_root == new_root or target.stat().st_size != old_bytes:
        return None                       # unchanged, or not the snapshotted version
    dest = superseded_name(target, sid)
    if dest.exists():
        return dest
    target.replace(dest)
    return dest


def promote(src: Path, target: Path, *, shard_dir: Path | None = None,
            new_root: str | None = None) -> None:
    """Move one finished file into the SHARD; `target` only ever appears
    whole. With `shard_dir`, a different version already at `target` that a
    held snapshot lists is kept under its superseded name first
    (`keep_superseded`).

    A rename where `src` and `target` share a filesystem. Where they do not
    — the SHARD's bytes on other storage, mounted at `shard/` as `origin/`'s
    are (07 §7.8), while a build stages under `state/` — a rename cannot
    cross, so the bytes are copied to a hidden name beside `target`, synced,
    and renamed over it: the same all-or-nothing, as one sequential write.
    A failed copy removes its temporary and leaves `src` where it was."""
    import errno
    import os
    import shutil
    if shard_dir is not None and Path(target).is_file():
        keep_superseded(Path(target), Path(shard_dir),
                        new_root if new_root is not None else pieces_root_of(Path(src)))
    try:
        os.replace(src, target)
        return
    except OSError as e:
        if e.errno != errno.EXDEV:
            raise
    tmp = target.with_name(f".{target.name}.promote")
    try:
        with open(src, "rb") as r, open(tmp, "wb") as w:
            shutil.copyfileobj(r, w, 16 << 20)
            w.flush()
            os.fsync(w.fileno())
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    Path(src).unlink()


def is_member_file(path: str, snapshot_files: Iterable[str],
                   shard_paths: Iterable[str]) -> bool:
    """A file that is neither in the snapshot's `files` nor named by a
    `shards` entry is not a member and is not served (09 §9.4)."""
    return path in set(snapshot_files) or path in set(shard_paths)
