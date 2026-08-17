"""Checksum verification shared by the acquisition engine and CorpusSource
plugins with sidecar checksums (e.g. PubMed's NLM-style .md5 files). Origin
sidecar when declared (ChecksumSource.kind == "sidecar"); blake2 always,
matching the blake2b(digest_size=16) convention used throughout this
codebase (GutenbergSource.fetch, PubmedSource.fetch, discovery.py).

Everything here streams in 1 MiB blocks and, where both digests are needed,
computes them in a SINGLE pass. verify_sidecar previously did
`path.read_bytes()` — the entire file into memory — which a 55 GB MedCPT dump
would not survive, and which P0.7's parallel workers would multiply by the
worker count. One pass also halves the IO for the common case, since the
engine needs md5 (sidecar) and blake2 (always) for the same bytes.
"""
import hashlib
from pathlib import Path

from .spec import ChecksumSource, TransportError

_BLOCK = 1 << 20


def _parse_sidecar(text: str) -> str:
    text = text.strip()
    # NLM's format: "MD5(filename)= <hex>". Also accept plain md5sum output
    # ("<hex>  filename") for hand-authored fixtures.
    if "=" in text:
        return text.rsplit("=", 1)[1].strip().lower()
    return text.split()[0].lower()


def _sidecar_expectation(path: Path, checksums: ChecksumSource) -> str | None:
    """The expected md5 for `path`, or None when no sidecar applies.

    Callers that must not silently skip verification should check
    sidecar_path(...).is_file() themselves — a sidecar whose own download
    FAILED is indistinguishable here from an origin that publishes none.
    """
    if checksums.kind != "sidecar":
        return None
    if checksums.sidecar_algo != "md5":
        raise ValueError(f"unsupported sidecar algo: {checksums.sidecar_algo!r}")
    sidecar = sidecar_path(path, checksums)
    if not sidecar.is_file():
        return None
    return _parse_sidecar(sidecar.read_text())


def sidecar_path(path: Path, checksums: ChecksumSource) -> Path:
    return Path(str(path) + checksums.sidecar_suffix)


def digest_and_verify(path: Path, checksums: ChecksumSource) -> str:
    """Single streaming pass: verifies the origin sidecar (when one exists)
    and returns the blake2 digest. Raises TransportError on mismatch."""
    expected = _sidecar_expectation(path, checksums)
    blake = hashlib.blake2b(digest_size=16)
    md5 = hashlib.md5() if expected is not None else None
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(_BLOCK), b""):
            blake.update(block)
            if md5 is not None:
                md5.update(block)
    if md5 is not None:
        actual = md5.hexdigest()
        if actual != expected:
            raise TransportError(
                f"{path}: {checksums.sidecar_algo} sidecar mismatch "
                f"(expected {expected}, got {actual})")
    return blake.hexdigest()


def verify_sidecar(path: Path, checksums: ChecksumSource) -> None:
    """No-op if checksums.kind != 'sidecar' or the sidecar file is absent —
    verification is best-effort when the origin doesn't publish a sidecar
    for a given file. Retained for callers that only want verification;
    digest_and_verify() is preferred where the blake2 digest is also needed."""
    expected = _sidecar_expectation(path, checksums)
    if expected is None:
        return
    md5 = hashlib.md5()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(_BLOCK), b""):
            md5.update(block)
    actual = md5.hexdigest()
    if actual != expected:
        raise TransportError(
            f"{path}: {checksums.sidecar_algo} sidecar mismatch "
            f"(expected {expected}, got {actual})")


def blake2_of(path: Path) -> str:
    h = hashlib.blake2b(digest_size=16)
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(_BLOCK), b""):
            h.update(block)
    return h.hexdigest()
