"""09 §9.5's figures containers — the plate bytes, beside the shards.

A figure is named in the record by `sha256` and carried here; its CAPTION
stays in `sections` and stays indexed. So `content_hash` never covers a
figure, and a collection that gains plates keeps every text vector it had
(02 §2.3).

ADDRESSED BY HASH, stored once. A plate used twice is two entries in the
record and one file here — 39% of Gutenberg's illustrated books repeat a
plate, one of them 703 times over 66 distinct images, so storing per
appearance would multiply the bytes for nothing.

RESUMABLE BY CONSTRUCTION (invariant 2). The file name IS the content
hash, so a plate already written is skipped by a stat, and an interrupted
pass costs the re-read of one book. Nothing is marked done anywhere;
presence on disk is the record.

WRITTEN IN A SECOND PASS, not in the parse pool. `_record_of` runs in a
child that has the epub open and could write them for free, but it
receives neither the pack directory nor the slice, and threading both
through the pool's contract to save one re-read of a file that is already
in the page cache is the wrong trade — the pool's contract is the thing
that makes a failed child one skipped row instead of a failed build.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

FIGURES_DIR = "figures"


class FigureError(ValueError):
    """A plate that cannot be carried as the record says it is."""


def container_dir(pack_dir: Path, slice_name: str) -> Path:
    return Path(pack_dir) / FIGURES_DIR / slice_name


def bare(sha256: str) -> str:
    """The hex, with or without 09 §9.2's `sha256:` prefix. The record line
    carries the prefixed form and a file name cannot, so one function knows
    about the difference instead of every call site."""
    h = sha256[len("sha256:"):] if sha256.startswith("sha256:") else sha256
    if len(h) != 64 or not all(c in "0123456789abcdef" for c in h):
        raise FigureError(f"a figure is addressed by its sha256, got {sha256!r}")
    return h


def path_for(pack_dir: Path, slice_name: str, sha256: str) -> Path:
    """`figures/<slice>/<sha256>` — no extension: the record's `media_type`
    says what the bytes are, and a name that also said it could disagree
    with them. 08 §8.7 checks the type against the bytes, never the name."""
    return container_dir(pack_dir, slice_name) / bare(sha256)


def write(pack_dir: Path, slice_name: str, sha256: str, data: bytes) -> bool:
    """Store one plate. True when it was written, False when already held.

    The bytes are VERIFIED against the name before landing: this is the
    one place the two can disagree, and a container whose file does not
    hash to its own name would fail every check downstream with nothing
    pointing back here.
    """
    want = bare(sha256)
    actual = hashlib.sha256(data).hexdigest()
    if actual != want:
        raise FigureError(
            f"figure bytes hash to {actual[:12]}… but the record names "
            f"{want[:12]}… — the plate and the record line disagree")
    dest = path_for(pack_dir, slice_name, sha256)
    if dest.is_file() and dest.stat().st_size == len(data):
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    tmp.write_bytes(data)
    tmp.replace(dest)           # atomic: a reader never sees a half plate
    return True


def held(pack_dir: Path, slice_name: str) -> set[str]:
    """The hashes this container already holds."""
    d = container_dir(pack_dir, slice_name)
    if not d.is_dir():
        return set()
    return {p.name for p in d.iterdir() if p.is_file() and len(p.name) == 64}
