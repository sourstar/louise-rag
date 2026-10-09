"""tools/archive_backend.py — keep a backend's installed files for the Codex's
life (02 §2.4, 09 §9.3 amended 2026-10-08; docs/notes/v5-migration.md G6).

    python -m tools.archive_backend <dir> <dest-dir>

A backend engine's `image` is the tree digest of the backend's installed
directory (deploy/vega/vega_wrapper.py `tree_digest`), and the model runner's
gallery drops old versions — so the node that creates a Codex keeps those
files. This writes `<dest-dir>/<tree hex>.tar.zst`, then unpacks it into a
temporary directory and recomputes `tree_digest` there: the archive is kept
only when the digest it unpacks to IS the directory's. Any directory works the
same way (an OpenVINO IR, a model folder): the digest is of the bytes.

What the tar keeps is what the digest reads — every regular file with its
execute bits, every symbolic link as a link (never followed), every
directory, empty ones included — plus the files' times, which a Python tree
needs (a `.pyc` checks its source's mtime; zeroed, the backend would rewrite
its own caches at the first load and become another engine). Owners are not
kept.

zstd: the `zstandard` module where the image has it, else pyarrow's bundled
codec (the one the record parts are written with, already a dependency),
else the `zstd` program. No new dependency.

Re-running with the archive already there verifies it and prints the same.
Prints `{"digest": …, "path": …}`; exit 1 when the unpacked tree is another.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "deploy" / "vega" / "vega_wrapper.py"


def tree_digest(directory: Path) -> str:
    """vega_wrapper's own function, loaded from its file: ONE definition."""
    name = "vega_wrapper_for_archive"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, WRAPPER)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name].tree_digest(Path(directory))


class NoZstd(RuntimeError):
    """Nothing in this image writes zstd."""


def _codec() -> str:
    """Which zstd this image has, in order of preference."""
    for module in ("zstandard", "pyarrow"):
        if importlib.util.find_spec(module) is not None:
            return module
    if shutil.which("zstd") is not None:
        return "zstd"
    raise NoZstd("no zstd here: neither the zstandard module, pyarrow, nor the zstd program")


@contextmanager
def _zstd_writer(path: Path):
    codec = _codec()
    if codec == "zstandard":
        import zstandard
        with open(path, "wb") as raw:
            with zstandard.ZstdCompressor(level=19).stream_writer(raw, closefd=False) as w:
                yield w
    elif codec == "pyarrow":
        import pyarrow as pa
        w = pa.CompressedOutputStream(str(path), "zstd")
        try:
            yield w
        finally:
            w.close()
    else:
        with open(path, "wb") as out:
            p = subprocess.Popen(["zstd", "-19", "-q", "-c"], stdin=subprocess.PIPE, stdout=out)
            try:
                yield p.stdin
            finally:
                p.stdin.close()
                if p.wait() != 0:
                    raise RuntimeError(f"zstd exited {p.returncode}")


@contextmanager
def _zstd_reader(path: Path):
    codec = _codec()
    if codec == "zstandard":
        import zstandard
        with open(path, "rb") as raw:
            with zstandard.ZstdDecompressor().stream_reader(raw) as r:
                yield r
    elif codec == "pyarrow":
        import pyarrow as pa
        r = pa.CompressedInputStream(str(path), "zstd")
        try:
            yield r
        finally:
            r.close()
    else:
        p = subprocess.Popen(["zstd", "-d", "-q", "-c", str(path)], stdout=subprocess.PIPE)
        try:
            yield p.stdout
        finally:
            p.stdout.close()
            p.wait()


def _anonymous(info: tarfile.TarInfo) -> tarfile.TarInfo:
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    return info


def write_archive(directory: Path, out: Path) -> None:
    """`directory`'s tree as a stream tar in zstd, its entries relative to it
    and in name order (tarfile sorts a directory's entries)."""
    with _zstd_writer(out) as w, tarfile.open(fileobj=w, mode="w|",
                                              format=tarfile.PAX_FORMAT) as tar:
        for entry in sorted(os.listdir(directory)):
            tar.add(directory / entry, arcname=entry, recursive=True, filter=_anonymous)


def unpack(archive: Path, into: Path) -> None:
    with _zstd_reader(archive) as r, tarfile.open(fileobj=r, mode="r|") as tar:
        # "tar": links kept as they are (a venv's absolute interpreter link is
        # the backend's own), paths kept inside `into`, setuid bits dropped
        tar.extractall(into, filter="tar")


def verify(archive: Path, want: str, scratch: Path) -> str:
    tmp = Path(tempfile.mkdtemp(prefix=".unpack-", dir=scratch))
    try:
        unpack(archive, tmp)
        return tree_digest(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def archive_backend(directory: Path, dest: Path) -> dict:
    directory, dest = Path(directory), Path(dest)
    if not directory.is_dir():
        raise SystemExit(f"{directory} is not a directory")
    want = tree_digest(directory)
    dest.mkdir(parents=True, exist_ok=True)
    final = dest / f"{want.split(':', 1)[1]}.tar.zst"
    if final.is_file():
        got = verify(final, want, dest)
        return {"digest": want, "path": str(final), "verified": got == want, "unpacked": got}
    tmp = dest / f".{final.name}.part"
    tmp.unlink(missing_ok=True)
    try:
        write_archive(directory, tmp)
        got = verify(tmp, want, dest)
        if got != want:
            return {"digest": want, "path": None, "verified": False, "unpacked": got}
        os.replace(tmp, final)
    finally:
        tmp.unlink(missing_ok=True)
    return {"digest": want, "path": str(final), "verified": True, "unpacked": got}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="archive_backend", description=__doc__.split("\n\n")[0])
    p.add_argument("directory", type=Path, help="the backend's installed directory (or any)")
    p.add_argument("dest", type=Path, help="where <tree hex>.tar.zst is written")
    a = p.parse_args(argv)
    try:
        out = archive_backend(a.directory, a.dest)
    except NoZstd as e:
        print(f"archive_backend: {e}", file=sys.stderr)
        return 2
    print(json.dumps(out, indent=2, sort_keys=True))
    if not out["verified"]:
        print(f"archive_backend: the archive unpacks to {out['unpacked']}, not "
              f"{out['digest']}; nothing kept", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
