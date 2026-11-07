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
Prints `{"digest": …, "path": …, "sidecar": …}`; exit 1 when the unpacked tree
is another.

THE SIDECAR (München, 2026-11-07). The node's host, where
deploy/vega/retired_backends.py runs as root, has no zstd at all (no
zstandard, no pyarrow, no `zstd` program), so it cannot unpack an archive to
check it. So every archive that verifies here gets `<tree hex>.tar.zst.verified`
beside it: the tree digest the unpack proved, the sha256 of the .tar.zst
file's bytes (what `sha256sum` prints), their size, when, and this tool's
version; the host checks the bytes against it with hashlib alone. Written only
once the unpacked tree IS the digest, rewritten each time an archive verifies
again, removed when it does not. For an archive made before the sidecar:

    python -m tools.archive_backend --sidecar <archive>…

verifies each against the tree digest its name states and writes its sidecar
(in the worker image, which has pyarrow; approved-encoders.md, "Retiring an
engine after a swap"). Exit 1 when one unpacks to another tree.

The standard way: a checksum file beside the artefact, as `.sha256` and
SHA256SUMS files sit beside a download for `sha256sum -c`. JSON rather than
their `<hex>  <name>` line, because the sidecar also says WHICH tree the
unpack proved. The standard alternative, written down (CLAUDE.md, do not
reinvent the wheel): SHA256SUMS with a detached signature (minisign, or the
node key's Ed25519) — it pays once archives travel between nodes and a reader
must know who verified them; on one node root writes both files, and a
signature would add nothing.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "deploy" / "vega" / "vega_wrapper.py"
#: This tool's version, written into the sidecar: 1 kept the archive alone
#: (2026-10-10, v5-migration.md G6); 2 also writes its `.verified` (2026-11-07).
VERSION = 2
TOOL = "tools/archive_backend.py"
SIDECAR = ".verified"
#: An archive's name: the hex of the tree digest it unpacks to.
ARCHIVE_NAME = re.compile(r"([0-9a-f]{64})\.tar\.zst")


def wrapper():
    """deploy/vega/vega_wrapper.py, loaded from its file once: ONE definition
    of the tree digest (and of the engine's whole image, `engine_manifest`)."""
    name = "vega_wrapper_for_archive"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, WRAPPER)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


def tree_digest(directory: Path) -> str:
    """vega_wrapper's own function, loaded from its file: ONE definition."""
    return wrapper().tree_digest(Path(directory))


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


# ---- the sidecar: what the host checks without zstd ------------------------------------------

def sidecar_of(archive: Path) -> Path:
    """`<tree hex>.tar.zst.verified`, beside the archive."""
    archive = Path(archive)
    return archive.with_name(archive.name + SIDECAR)


def file_sha256(path: Path) -> str:
    """The hex sha256 of a file's bytes, as `sha256sum` prints it (hashlib)."""
    with open(path, "rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def _identity(st: os.stat_result) -> tuple[int, int, int, int]:
    return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns


def _verify_recording(archive: Path, want: str, scratch: Path) -> tuple[str, dict | None]:
    """`verify`, and the sidecar's fields when the archive unpacks to `want`:
    the sha256 of the bytes that were unpacked, the file the same from before
    the hash to after the unpack."""
    before = os.stat(archive)
    sha = file_sha256(archive)
    got = verify(archive, want, scratch)
    if got != want:
        return got, None
    if _identity(os.stat(archive)) != _identity(before):
        raise RuntimeError(f"{archive} changed while it was verified; verify it again")
    return got, {"tree": want, "sha256": sha, "bytes": before.st_size,
                 "verified_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                 "tool": TOOL, "tool_version": VERSION}


def write_sidecar(archive: Path, record: dict) -> Path:
    path = sidecar_of(archive)
    tmp = path.with_name(f".{path.name}.part")
    tmp.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


def check_sidecar(archive: Path, tree: str) -> str | None:
    """Why the sidecar does not vouch for `archive` unpacking to `tree`, or
    None when it does: it names `tree`, and the archive's bytes hash to the
    sha256 recorded when they verified. No zstd needed: hashlib alone."""
    archive = Path(archive)
    path = sidecar_of(archive)
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return (f"there is no sidecar {path} (`python -m tools.archive_backend --sidecar "
                f"{archive}`, where zstd is, writes it)")
    except (OSError, ValueError) as e:
        return f"its sidecar {path} cannot be read ({type(e).__name__}: {e})"
    if not isinstance(doc, dict) or doc.get("tree") != tree:
        named = doc.get("tree") if isinstance(doc, dict) else None
        return f"its sidecar {path} names the tree {named}, not {tree}"
    size = archive.stat().st_size
    if doc.get("bytes") != size:
        return (f"the archive holds {size:,} bytes and its sidecar {path} records "
                f"{doc.get('bytes')}: not the bytes that verified")
    sha = file_sha256(archive)
    if doc.get("sha256") != sha:
        return (f"the archive's sha256 is {sha} and its sidecar {path} records "
                f"{doc.get('sha256')}: not the bytes that verified")
    return None


# ---- making and verifying archives ------------------------------------------------------------

def _recorded(archive: Path, want: str, got: str, record: dict | None) -> dict:
    """The sidecar follows the verdict: written when the archive verified,
    removed when it did not — a sidecar never outlives its proof."""
    if record is None:
        sidecar_of(archive).unlink(missing_ok=True)
        return {"digest": want, "path": None, "verified": False, "unpacked": got, "sidecar": None}
    return {"digest": want, "path": str(archive), "verified": True, "unpacked": got,
            "sidecar": str(write_sidecar(archive, record))}


def archive_backend(directory: Path, dest: Path) -> dict:
    directory, dest = Path(directory), Path(dest)
    if not directory.is_dir():
        raise SystemExit(f"{directory} is not a directory")
    want = tree_digest(directory)
    dest.mkdir(parents=True, exist_ok=True)
    final = dest / f"{want.split(':', 1)[1]}.tar.zst"
    if final.is_file():
        got, record = _verify_recording(final, want, dest)
        return {**_recorded(final, want, got, record), "path": str(final)}
    tmp = dest / f".{final.name}.part"
    tmp.unlink(missing_ok=True)
    try:
        write_archive(directory, tmp)
        got, record = _verify_recording(tmp, want, dest)
        if record is None:
            return _recorded(final, want, got, None)
        os.replace(tmp, final)
    finally:
        tmp.unlink(missing_ok=True)
    return _recorded(final, want, got, record)


def sidecar(archive: Path) -> dict:
    """An archive already kept, verified against the tree digest its name
    states (`<tree hex>.tar.zst`), and its sidecar written: the one-off for
    the archives made before the sidecar existed."""
    archive = Path(archive)
    m = ARCHIVE_NAME.fullmatch(archive.name)
    if m is None:
        raise SystemExit(f"{archive} is not named <tree hex>.tar.zst: no tree digest to verify")
    if not archive.is_file():
        raise SystemExit(f"{archive} is not a file")
    want = f"sha256:{m.group(1)}"
    got, record = _verify_recording(archive, want, archive.parent)
    return {**_recorded(archive, want, got, record), "path": str(archive)}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="archive_backend", description=__doc__.split("\n\n")[0])
    p.add_argument("directory", type=Path, nargs="?",
                   help="the backend's installed directory (or any)")
    p.add_argument("dest", type=Path, nargs="?", help="where <tree hex>.tar.zst is written")
    p.add_argument("--sidecar", type=Path, nargs="+", metavar="ARCHIVE",
                   help="verify archives already kept and write their .verified sidecars")
    a = p.parse_args(argv)
    if a.sidecar and a.directory is not None:
        p.error("--sidecar takes archives, not a directory and a destination")
    if not a.sidecar and a.dest is None:
        p.error("the backend's directory and the destination are required")
    try:
        if not a.sidecar:
            out = archive_backend(a.directory, a.dest)
            print(json.dumps(out, indent=2, sort_keys=True))
            if not out["verified"]:
                print(f"archive_backend: the archive unpacks to {out['unpacked']}, not "
                      f"{out['digest']}; nothing kept", file=sys.stderr)
                return 1
            return 0
        failed = 0
        for archive in a.sidecar:
            out = sidecar(archive)
            print(json.dumps(out, sort_keys=True), flush=True)
            if not out["verified"]:
                print(f"archive_backend: {archive} unpacks to {out['unpacked']}, not "
                      f"{out['digest']}; no sidecar", file=sys.stderr)
                failed += 1
        return 1 if failed else 0
    except NoZstd as e:
        print(f"archive_backend: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
