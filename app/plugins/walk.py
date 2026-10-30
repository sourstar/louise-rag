"""Every walk over a corpus, through the links its origin holds.

`Path.rglob` does not enter a symlinked directory — on Python 3.11, 3.12
(the node's image) and 3.13 alike (3.13 adds `recurse_symlinks`, default
False). An origin reaches parts of its data through links: wikipedia-v8's
`origin/data` is a link to the archive under /corpora. Walked by rglob, the
archive was invisible — never scanned, its 22 M rows never relocated, so every
scan re-enumerated the corpus — and the archive's coverage read EMPTY, so the
dump pass minted a second document for 21.9 M pages the archive already held
(2026-10-30; docs/notes/STATUS.md).

THE STANDARD, NOT OURS: `os.walk(followlinks=True)`, the standard library's
walk over links. A directory reached a second time by its real path is not
walked again, so a link loop ends and a directory linked twice yields its
files once. Paths are yielded AS REACHED, under the root — never resolved —
so a locator stays under the origin it names (`plugin_io.relative_to_base`
reads such a path back through the same link).
"""
from __future__ import annotations

import fnmatch
import os
from pathlib import Path


def files(root: Path | str, pattern: str) -> list[Path]:
    """The files under `root` whose NAME matches `pattern` (case-sensitive,
    as rglob is on POSIX), through linked directories, sorted."""
    root = Path(root)
    if not root.is_dir():
        return []
    out: list[Path] = []
    walked: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(root, followlinks=True):
        real = os.path.realpath(dirpath)
        if real in walked:
            dirnames[:] = []           # a loop, or a second link to one directory
            continue
        walked.add(real)
        dirnames.sort()
        for name in filenames:
            if fnmatch.fnmatchcase(name, pattern):
                p = Path(dirpath) / name
                if p.is_file():
                    out.append(p)
    return sorted(out)
