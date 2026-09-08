"""The drag on disk — what a peer fetches and shares.

docs/02-collections.md §2.6 gives the tree; docs/09-formats.md §9.16 fixes
the member names, the roots and the torrents. Pure functions over names and
bytes: nothing here reads a manifest, a cursor or an acknowledgement, all of
which sit BESIDE the drag and never travel (07 §7.8).

A collection on disk is a drag — *distributed RAG*. The drag is the copy:
`<LOUISE_ROOT>/collections/<name>/drag/`, exactly what a peer shares.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from .codex import Codex, CodexError, canonical_json, digest

DRAG = "drag"

#: The kinds are closed (02 §2.6); `--kinds` takes these names (07 §7.3).
MEMBER_KINDS = ("records", "vectors", "buckets", "index", "relations",
                "bundle", "centres", "calibration", "terms", "seals", "origin")

#: Leaves are SHA-256 of consecutive 16 KiB blocks of the file (09 §9.5).
BLOCK = 16 * 1024

#: Every file of a member is one BitTorrent v2 torrent (09 §9.16).
PIECE_LENGTH = 16 * 1024 * 1024

_SEGMENT = r"[A-Za-z0-9=_\-][A-Za-z0-9=_.\-]*"
_SLICE_RE = re.compile(rf"^{_SEGMENT}(/{_SEGMENT})*$")
_SPACE_SHAPED = re.compile(r"^[0-9a-f]{16}$")
_SPACE_RE = _SPACE_SHAPED


class DragError(CodexError):
    """A name, a path or a member that the format does not allow."""


# --------------------------------------------------------------------------
# names

def check_slice(name: str) -> str:
    """A slice name is one or more path segments (09 §9.3).

    A segment of exactly 16 lowercase hexadecimal characters is refused: it
    is the shape of a `<space>` segment, and `index-<slice>` would then be a
    member name `index-<space>` already has (09 §9.16). It is the only
    collision the member table admits, and it is closed here rather than
    left to whoever first names a slice after a digest.
    """
    if not _SLICE_RE.match(name):
        raise DragError(f"{name!r} is not a slice name (09 §9.3)")
    for seg in name.split("/"):
        if _SPACE_SHAPED.match(seg):
            raise DragError(
                f"slice segment {seg!r} is sixteen lowercase hex characters, "
                f"which is the shape of a <space> segment; `index-{name}` "
                f"would collide with a vector index's member name (09 §9.16)")
    return name


def check_space(space: str) -> str:
    """The `<space>` segment: the first 16 hex digits of `space_id` (09 §9.3)."""
    if not _SPACE_RE.match(space):
        raise DragError(f"{space!r} is not a <space> segment: sixteen "
                        f"lowercase hexadecimal characters")
    return space


def records_member(slice_: str) -> str:
    return f"records-{check_slice(slice_)}"


def relations_member(slice_: str) -> str:
    return f"relations-{check_slice(slice_)}"


def index_member(slice_: str) -> str:
    """The text index of a shard — a cache, and no snapshot lists its root."""
    return f"index-{check_slice(slice_)}"


def vectors_member(space: str, slice_: str) -> str:
    return f"vectors-{check_space(space)}-{check_slice(slice_)}"


def buckets_member(space: str, index_version: int, group: int) -> str:
    return f"buckets-{check_space(space)}-v{index_version}-g{group:02d}"


def bundle_member(space: str, bundle_id: str) -> str:
    return f"bundle-{check_space(space)}-{_hex(bundle_id)}"


def centres_member(space: str, index_version: int) -> str:
    return f"centres-{check_space(space)}-v{index_version}"


def vector_index_member(space: str) -> str:
    """`vectors/<space>/index.json`. Distinct from `index-<slice>` because a
    slice segment may not be sixteen hex characters (`check_slice`)."""
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

def drag_dir(collection_dir: Path) -> Path:
    return collection_dir / DRAG


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
    return pieces_root(path.read_bytes())


@dataclass(frozen=True)
class MemberFile:
    path: str          # drag-relative
    root: str          # sha256:…
    bytes: int

    def to_list(self) -> list:
        return [self.path, self.root, self.bytes]


def member_root(files: Sequence[MemberFile]) -> str:
    """A member of one file has that file's pieces root as its root; a member
    of several has `H` over the canonical JSON of `[[path, root, bytes], …]`
    in path byte order (09 §9.16)."""
    if not files:
        raise DragError("a member has at least one file")
    if len(files) == 1:
        return files[0].root
    ordered = sorted(files, key=lambda f: f.path.encode("utf-8"))
    return digest(canonical_json([f.to_list() for f in ordered]))


def member_files(drag: Path, paths: Iterable[str]) -> tuple[MemberFile, ...]:
    out = []
    for p in paths:
        f = drag / p
        if not f.is_file():
            raise DragError(f"{p}: not in the drag at {drag}")
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
TREE = ("snapshots", "calibration", "seals", "records", "relations", "index",
        "vectors")


def scaffold(drag: Path) -> Path:
    """Make the drag's directories. Files arrive with the steps that write
    them; a directory that stays empty is not a member and is not served."""
    drag.mkdir(parents=True, exist_ok=True)
    for d in TREE:
        (drag / d).mkdir(exist_ok=True)
    return drag


def is_member_file(path: str, snapshot_files: Iterable[str],
                   shard_paths: Iterable[str]) -> bool:
    """A file that is neither in the snapshot's `files` nor named by a
    `shards` entry is not a member and is not served (09 §9.4)."""
    return path in set(snapshot_files) or path in set(shard_paths)
