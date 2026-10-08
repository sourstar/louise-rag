"""Space artifacts — where every calibrated threshold lives.

architecture-v2.md §6's tiers all compare a bundle against numbers the SPACE
AUTHOR published, never against constants in this repository. That is the
difference between a calibrated ladder and a pile of magic numbers, and it is
why the P1.2 task says "thresholds live in space artifacts".

    <space_id>.artifact/
      artifact.json      quantiles, bands, calibration, beacon — the wire bytes
      probes.f32.npy     K x dim reference vectors, little-endian
      goldens.f32.npy    G x dim golden vectors for T2
      envelope.f32.npy   2 x dim  [per-dim mean; per-dim sd]
      ARTIFACT.sig       exactly 64 raw bytes, Ed25519

WHAT THE VERIFIER SUPPLIES AND WHAT THE ARTIFACT SUPPLIES:

    artifact -> null distributions, bands, tau, f0/f1   (what "honest" looks like)
    node     -> alpha, the false-reject budget           (what risk it will accept)

alpha is deliberately NOT in the artifact. It is a rate the operator is willing
to pay, not a property of the space, and letting an artifact set it would let a
hostile artifact turn verification off by asking for alpha=1.

TRUST IS NOT TOFU HERE. A bundle PRODUCER needs no trust at all (03 §3.5;
app/bundles/trust.py only refuses a denied or burned key), which is
defensible because admission does not rest on it. An ARTIFACT defines
what honest means, so trusting one on sight hands an attacker the definition.
An unsigned or unrecognised artifact is therefore ADVISORY: it may only ever
TIGHTEN a check relative to the built-in coarse fallback, never loosen one.
"""
import hashlib
import json
import struct
import unicodedata
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from ..bundles.format import BundleFormatError
from ..bundles.keys import SIGNATURE_BYTES, verify_key_from_hex

SCHEMA = "louise-rag/space-artifact/v1"
_ARTIFACT_SIG_DOMAIN = b"louise-rag/space-artifact-sig/v1\x00"

ARTIFACT_NAME = "artifact.json"
SIGNATURE_NAME = "ARTIFACT.sig"
PROBES_NAME = "probes.f32.npy"
GOLDENS_NAME = "goldens.f32.npy"
ENVELOPE_NAME = "envelope.f32.npy"

MAX_ARTIFACT_BYTES = 4 << 20
MAX_PROBES = 4096
MAX_GOLDENS = 65536
MAX_DIM = 8192
WIRE_DTYPE = "<f4"


class ArtifactError(BundleFormatError):
    """The artifact is missing, malformed, or inconsistent with its sidecars."""


def _reject_constant(value: str):
    raise ArtifactError(f"artifact contains a non-finite constant: {value!r}")


def _reject_duplicate_keys(pairs):
    keys = [k for k, _ in pairs]
    if len(keys) != len(set(keys)):
        raise ArtifactError("artifact contains a duplicate key")
    return dict(pairs)


def _load_npy(path: Path, *, rows: int, dim: int, label: str) -> np.ndarray:
    """Load a float32 sidecar defensively.

    allow_pickle=False is load-bearing: a .npy is attacker-supplied bytes, and
    numpy's pickle path is arbitrary code execution. Shape and dtype are pinned
    and cross-checked against the declared counts BEFORE the array is used, so
    a lying header cannot make us allocate something absurd.
    """
    if not path.is_file():
        raise ArtifactError(f"artifact is missing {label} ({path.name})")
    expected_bytes = rows * dim * 4
    actual = path.stat().st_size
    if actual > expected_bytes + 4096:
        raise ArtifactError(
            f"{label} is {actual} bytes but at most {expected_bytes} were declared")
    try:
        array = np.load(path, allow_pickle=False)
    except Exception as e:
        raise ArtifactError(f"{label} could not be read ({type(e).__name__})") from None
    if array.dtype != np.dtype(WIRE_DTYPE) or array.ndim != 2:
        raise ArtifactError(f"{label} must be a 2-D little-endian float32 array")
    if array.shape != (rows, dim):
        raise ArtifactError(
            f"{label} has shape {array.shape}, expected ({rows}, {dim})")
    if not np.all(np.isfinite(array)):
        raise ArtifactError(f"{label} contains NaN or Inf")
    return np.ascontiguousarray(array)


def _text(obj, key, parent=""):
    value = obj.get(key)
    if not isinstance(value, str):
        raise ArtifactError(f"artifact field {parent}{key!r} must be a string")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ArtifactError(f"artifact field {parent}{key!r} has unpaired surrogates") from None
    if not unicodedata.is_normalized("NFC", value):
        raise ArtifactError(f"artifact field {parent}{key!r} is not NFC-normalized")
    return value


def _int(obj, key, low, high, parent=""):
    value = obj.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ArtifactError(f"artifact field {parent}{key!r} must be an integer")
    if not low <= value <= high:
        raise ArtifactError(f"artifact field {parent}{key!r} out of range: {value}")
    return value


@dataclass
class SpaceArtifact:
    """A parsed, size-validated space artifact."""
    raw: dict
    path: Path
    artifact_id: str
    space_id: str
    dim: int
    trusted: bool                     # signed by a configured space author
    probes: np.ndarray | None = None
    goldens: np.ndarray | None = None
    envelope: np.ndarray | None = None   # 2 x dim: [mean; sd]

    # ---- accessors, each naming which tier reads it ----

    @property
    def version(self) -> int:
        return int(self.raw.get("artifact_version", 0))

    @property
    def bootstrap_unit(self) -> str:
        return self.raw.get("reference_corpus", {}).get("bootstrap_unit", "")

    @property
    def bootstrap_replicates(self) -> int:
        return int(self.raw.get("reference_corpus", {}).get("bootstrap_replicates", 0))

    def envelope_block(self) -> dict:
        return self.raw.get("envelope") or {}

    def probes_block(self) -> dict:
        return self.raw.get("probes") or {}

    def overlap_block(self) -> dict:
        return self.raw.get("overlap") or {}

    def calibration(self) -> dict:
        return self.raw.get("calibration") or {}

    def goldens_block(self) -> dict:
        return self.raw.get("goldens") or {}

    def bucket(self, buckets: list[dict], n_docs: int, n_chunks: int) -> dict | None:
        """Pick the null band for a bundle of this size.

        ROUNDS DOWN — the widest band whose size requirements the bundle
        actually meets. Interpolating into a tighter band than the bundle
        supports is how a small honest bundle gets rejected.
        """
        eligible = [b for b in buckets
                    if n_docs >= int(b.get("min_docs", 0))
                    and n_chunks >= int(b.get("min_chunks", b.get("min_pairs", 0)))]
        if not eligible:
            return None
        return max(eligible, key=lambda b: (int(b.get("min_docs", 0)),
                                             int(b.get("min_chunks",
                                                       b.get("min_pairs", 0)))))

    def resolvable_alpha(self, alpha: float) -> bool:
        """An empirical quantile from B bootstrap replicates cannot resolve a
        p-value below 1/(B+1). Asking it to is how a ladder silently clips its
        own strictness."""
        return self.bootstrap_replicates >= 1.0 / max(alpha, 1e-12)


def signed_payload(artifact_bytes: bytes, digests: list[bytes]) -> bytes:
    """Same construction as app/bundles/format.signed_payload, with a distinct
    domain prefix so an artifact signature can never be replayed as a bundle
    signature (or vice versa)."""
    parts = [_ARTIFACT_SIG_DOMAIN, struct.pack(">I", len(artifact_bytes)), artifact_bytes]
    for digest in digests:
        if len(digest) != 32:
            raise ArtifactError("sidecar digests must be 32 bytes")
        parts.append(struct.pack(">I", 32))
        parts.append(digest)
    return b"".join(parts)


def _sidecar_digests(directory: Path) -> list[bytes]:
    out = []
    for name in (PROBES_NAME, GOLDENS_NAME, ENVELOPE_NAME):
        path = directory / name
        out.append(hashlib.sha256(path.read_bytes()).digest() if path.is_file()
                   else hashlib.sha256(b"").digest())
    return out


def load(directory: Path, *, trusted_authors: list[str] | None = None) -> SpaceArtifact:
    """Parse and validate an artifact directory.

    Signature verification decides TRUSTED vs ADVISORY, never load vs refuse: an
    advisory artifact is still useful because it may only tighten checks.
    """
    directory = Path(directory)
    artifact_path = directory / ARTIFACT_NAME
    if not artifact_path.is_file():
        raise ArtifactError(f"no {ARTIFACT_NAME} in {directory}")
    raw_bytes = artifact_path.read_bytes()
    if len(raw_bytes) > MAX_ARTIFACT_BYTES:
        raise ArtifactError(f"artifact exceeds {MAX_ARTIFACT_BYTES} bytes")

    try:
        obj = json.loads(raw_bytes.decode("utf-8"),
                         object_pairs_hook=_reject_duplicate_keys,
                         parse_constant=_reject_constant)
    except ArtifactError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ArtifactError(f"artifact is not valid UTF-8 JSON ({type(e).__name__})") from None
    if not isinstance(obj, dict):
        raise ArtifactError("artifact must be a JSON object")
    # NOTE, deliberately different from bundle headers: floats ARE allowed here.
    # An artifact is nothing but calibrated floats, and float round-tripping is
    # irrelevant to integrity because the signature covers the wire bytes.
    if _text(obj, "schema") != SCHEMA:
        raise ArtifactError(f"unsupported artifact schema (expected {SCHEMA})")

    space = obj.get("space") or {}
    dim = _int(space, "dim", 1, MAX_DIM, parent="space.")
    space_id = _text(obj, "space_id")

    corpus = obj.get("reference_corpus") or {}
    unit = corpus.get("bootstrap_unit")
    if unit != "document":
        # A null built by resampling CHUNKS is far too tight: it destroys the
        # within-document correlation every honest bundle has, and honest small
        # bundles then fail en masse.
        raise ArtifactError(
            f"artifact declares bootstrap_unit={unit!r}; only 'document' produces "
            f"a null a real bundle can be compared against")

    digests = _sidecar_digests(directory)
    payload = signed_payload(raw_bytes, digests)
    artifact_id = hashlib.sha256(payload).hexdigest()

    trusted = False
    signature_path = directory / SIGNATURE_NAME
    author = obj.get("author_pubkey")
    if signature_path.is_file() and isinstance(author, str) and trusted_authors:
        sig = signature_path.read_bytes()
        if len(sig) == SIGNATURE_BYTES and author in set(trusted_authors):
            try:
                verify_key_from_hex(author).verify(payload, sig)
                trusted = True
            except Exception:
                trusted = False

    artifact = SpaceArtifact(raw=obj, path=directory, artifact_id=artifact_id,
                             space_id=space_id, dim=dim, trusted=trusted)

    envelope_block = artifact.envelope_block()
    if envelope_block.get("per_dim_ref"):
        artifact.envelope = _load_npy(directory / ENVELOPE_NAME, rows=2, dim=dim,
                                      label="envelope")
    probes_block = artifact.probes_block()
    if probes_block.get("count"):
        count = _int(probes_block, "count", 1, MAX_PROBES, parent="probes.")
        artifact.probes = _load_npy(directory / PROBES_NAME, rows=count, dim=dim,
                                    label="probes")
    goldens_block = artifact.goldens_block()
    if goldens_block.get("bank_size"):
        bank = _int(goldens_block, "bank_size", 1, MAX_GOLDENS, parent="goldens.")
        artifact.goldens = _load_npy(directory / GOLDENS_NAME, rows=bank, dim=dim,
                                     label="goldens")
    return artifact


def find(space_id: str, search_dirs: list[Path],
         trusted_authors: list[str] | None = None) -> SpaceArtifact | None:
    """Locate an artifact by space_id. No network service: the filename IS the
    content-derived space id, so lookup is O(1) with no index to corrupt.

    Deliberately NOT supported: an artifact carried inside the bundle.
    Producer-supplied thresholds are producer-chosen thresholds, which is the
    whole attack.
    """
    for base in search_dirs:
        candidate = Path(base) / f"{space_id}.artifact"
        if (candidate / ARTIFACT_NAME).is_file():
            return load(candidate, trusted_authors=trusted_authors)
    return None
