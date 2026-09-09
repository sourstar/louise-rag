"""The calibration set and the calibration member. docs/09-formats.md §9.12;
docs/03-checking.md §3.3.

A calibration set is a CORPUS-INDEPENDENT file of 512 short texts shipped
with the platform, one per modality. Corpus-independent for four reasons,
each of which bites if it is not:

  * `hold embedder calibrate` runs BEFORE `collection create`, and its
    output is the recipe's `space.tolerance`. A set drawn from the corpus
    would need the collection to exist before the number it is created
    with could be computed.
  * The gate is a property of the ENCODER BUILD, not of a corpus. Two
    collections on one encoder share one calibration (03 §3.3), and the
    digest advertises one `benchmarks` entry per `space_id` (09 §9.9).
  * A newcomer holds no corpus and must still prove its encoder compliant
    in order to earn the right to embed at all.
  * A creator who chose the texts their own build does well on would set a
    tolerance nobody could fail. Sameness across peers is what makes the
    number comparable.

The set's job is NOT to be a representative corpus and NOT to measure
retrieval quality. It is to make any systematic difference between two
encoder builds show up SOMEWHERE in the 512 — so it spans scripts,
lengths, numerals, punctuation and near-duplicates, because quantisation,
tokenisation, pooling and truncation each go wrong in their own regime.
"""
from __future__ import annotations

import hashlib
import json
import unicodedata
from pathlib import Path
from typing import Iterator, Sequence

from .codex import CodexError

#: 09 §9.12 fixes the count.
SIZE = 512

#: One set per modality (09 §9.12). The file lives in the platform image,
#: not in a drag: it is not a member, and a peer that cannot find the set a
#: Codex names is running a platform build that does not carry it.
SETS_DIR = Path(__file__).resolve().parent.parent.parent / "calibration"

_FORBIDDEN = {"Cc", "Cf", "Cs", "Co", "Cn"}


class CalibrationError(CodexError):
    """The calibration set or member is not what §9.12 fixes."""


def path_for(modality: str) -> Path:
    return SETS_DIR / f"{modality}.jsonl"


def line_of(i: int, text: str) -> bytes:
    """One line of the set file: `{"i": <0…511>, "text": "…"}`, compact,
    `\\n`-terminated (09 §9.12). Key order is fixed by the document, so it
    is written explicitly rather than sorted."""
    return (json.dumps({"i": i, "text": text}, ensure_ascii=False,
                       separators=(",", ":")) + "\n").encode("utf-8")


def render(texts: Sequence[str]) -> bytes:
    """The whole file, in index order. Its bytes are what the id hashes."""
    check(texts)
    return b"".join(line_of(i, t) for i, t in enumerate(texts))


def set_id(data: bytes) -> str:
    """`H(bytes of the file)` (09 §9.12)."""
    return "sha256:" + hashlib.sha256(data).hexdigest()


def segment(set_id_: str) -> str:
    """`<set>` in a path is the id's 16-digit segment (09 §9.1, §9.12)."""
    return set_id_.split(":", 1)[1][:16]


# --------------------------------------------------------------------------
# what a set must satisfy

def check(texts: Sequence[str]) -> None:
    """Refuse a set that could not do its job, or that could not be shipped.

    Every rule here is one a bad set would pass silently: the file would
    hash, the gate would run, and the tolerance it produced would be
    measured on the wrong thing.
    """
    if len(texts) != SIZE:
        raise CalibrationError(f"a calibration set is {SIZE} texts, got {len(texts)}")

    seen: dict[str, int] = {}
    for i, t in enumerate(texts):
        if not isinstance(t, str) or not t.strip():
            raise CalibrationError(f"text {i} is empty")
        if unicodedata.normalize("NFC", t) != t:
            raise CalibrationError(
                f"text {i} is not NFC-normalised; two peers whose platforms "
                f"normalise differently would embed different bytes")
        for ch in t:
            if unicodedata.category(ch) in _FORBIDDEN:
                raise CalibrationError(
                    f"text {i} carries {unicodedata.category(ch)} character "
                    f"U+{ord(ch):04X}; a set line is one JSON string on one "
                    f"line, and a control character is not text")
        if t in seen:
            raise CalibrationError(
                f"texts {seen[t]} and {i} are identical; 512 texts means 512, "
                f"and a duplicate wastes a probe")
        seen[t] = i


def coverage(texts: Sequence[str]) -> dict[str, int]:
    """What regimes the set actually spans, counted rather than asserted.

    A set that is 512 short English sentences would pass `check` and be
    useless: it would miss a build that is wrong only on Cyrillic, or only
    past a hundred tokens. This is what a test asserts against.
    """
    import re
    out = {
        "short": 0, "medium": 0, "long": 0,
        "non_latin": 0, "latin_diacritics": 0, "ascii_only": 0,
        "numeric": 0, "punctuation_dense": 0, "has_upper_run": 0,
    }
    digits = re.compile(r"\d")
    punct = re.compile(r"[^\w\s]")
    upper_run = re.compile(r"\b[A-Z]{3,}\b")
    for t in texts:
        n = len(t.split())
        out["short" if n <= 12 else "long" if n >= 90 else "medium"] += 1
        if any(ord(c) > 0x24F for c in t):
            out["non_latin"] += 1
        elif any(ord(c) > 0x7F for c in t):
            out["latin_diacritics"] += 1
        else:
            out["ascii_only"] += 1
        if digits.search(t):
            out["numeric"] += 1
        if len(punct.findall(t)) >= 4:
            out["punctuation_dense"] += 1
        if upper_run.search(t):
            out["has_upper_run"] += 1
    return out


# --------------------------------------------------------------------------
# reading a set the platform ships

def load(modality: str) -> tuple[str, list[str]]:
    """`(set_id, texts)` for a modality this platform carries."""
    p = path_for(modality)
    if not p.is_file():
        raise CalibrationError(
            f"this platform ships no calibration set for modality "
            f"{modality!r} ({p}); a Codex naming one cannot be calibrated here")
    data = p.read_bytes()
    return set_id(data), list(parse(data))


def parse(data: bytes) -> Iterator[str]:
    """Read the file back, refusing anything the writer would not have
    produced — the index must be dense and in order, because `sample` draws
    over `{"0"…"511"}` and a hole would silently shrink the universe."""
    for want, raw in enumerate(data.split(b"\n")[:-1]):
        obj = json.loads(raw)
        if set(obj) != {"i", "text"}:
            raise CalibrationError(f"line {want}: keys are exactly i and text")
        if obj["i"] != want:
            raise CalibrationError(
                f"line {want}: index is {obj['i']}; the set is dense and in "
                f"order, and `sample` draws over {{\"0\"…\"511\"}} (09 §9.12)")
        yield obj["text"]


def verify(modality: str, expected_id: str) -> list[str]:
    """The texts of the set a Codex names, refusing a platform whose file
    hashes otherwise. The set is not a member and no snapshot lists it, so
    this hash is the only thing binding what a peer embeds to what the
    creator embedded."""
    got, texts = load(modality)
    if got != expected_id:
        raise CalibrationError(
            f"this platform's {modality} calibration set is {got}, the Codex "
            f"names {expected_id}. The reference vectors were produced over "
            f"the creator's set; embedding a different 512 texts and "
            f"comparing would compare nothing")
    return texts


# --------------------------------------------------------------------------
# the member `hold collection create` writes (09 §9.12, 06 §6.6)

VECTORS = "vectors"
ENVELOPE = "envelope"


def tensor_hash(arr) -> str:
    """`H` over the bytes of a tensor (09 §9.12).

    The bytes are the tensor's own raw buffer — C-contiguous, float32,
    row-major — which is what safetensors stores and what a verifier
    recomputes. Hashing the FILE instead would fold in the header, and two
    honest writers may order a header's keys differently.
    """
    import numpy as np
    a = np.ascontiguousarray(arr, dtype=np.float32)
    return "sha256:" + hashlib.sha256(a.tobytes()).hexdigest()


def envelope_of(vectors):
    """09 §9.12: "row 0 the per-dimension mean of `vectors`, row 1 the
    per-dimension standard deviation". It is what T0 checks a stranger's
    bundle against before anything more expensive runs (03 §3.5)."""
    import numpy as np
    v = np.ascontiguousarray(vectors, dtype=np.float32)
    return np.ascontiguousarray(
        np.stack([v.mean(axis=0), v.std(axis=0)]), dtype=np.float32)


#: 03 §3.5's T0 envelope literals, measured 2026-09-09 (three runs: two
#: encoders, two corpora, dims 2560 and 4096) and defended against seven
#: deliberately broken bundles. `tools/measure_t0_envelope.py` reproduces
#: the table and the controls.
ENVELOPE_COS_MIN = 0.5
ENVELOPE_BAND = (0.4, 2.0)

#: THE PERCENTILE IS THE POINT, not the band. Stated as "every dimension
#: within the band", this test is a multiple-comparison problem: an honest
#: bundle gets one chance to fail per dimension, so the false-alarm rate
#: grows with the space. Measured: the SAME honest vectors passed at 2560
#: dimensions and put 16 of 4096 outside at 4096, because `min` over more
#: samples is smaller for no reason but the count. A percentile does not
#: drift with sample size, so the rule holds at any dimension and the band
#: barely had to move (it was [0.5, 2.0] before, applied to the extremes).
ENVELOPE_QUANTILE = 2


def envelope_verdict(vectors, envelope_) -> dict:
    """T0's envelope test (03 §3.5): does this bundle look like it came
    out of this space?

    Returns the two statistics and a verdict rather than a bare bool,
    because a refusal that cannot say which number failed is a refusal
    nobody can act on — and because §3.5 keeps the per-dimension numbers
    as EVIDENCE even when they are not the decision.

    What it does not see, measured rather than assumed: a bundle 10 % or
    25 % random passes both statistics, its mean vector indistinguishable
    from clean. The extremum form did catch those — with the very
    statistic that refuses honest work — so no version of this test sees
    partial contamination AND admits real bundles. That is T3's and T4's,
    and §3.5 says so.
    """
    import numpy as np
    v = np.asarray(vectors, dtype=np.float64)
    env = np.asarray(envelope_, dtype=np.float64)
    if env.ndim != 2 or env.shape[0] != 2:
        raise CalibrationError(
            f"an envelope is [2, dim] — row 0 the mean, row 1 the standard "
            f"deviation (09 §9.12); got {env.shape}")
    if v.ndim != 2 or v.shape[1] != env.shape[1]:
        raise CalibrationError(
            f"the bundle is {v.shape} and the envelope is for dim "
            f"{env.shape[1]}")

    mean, sd = env[0], env[1]
    m = v.mean(axis=0)
    denom = np.linalg.norm(m) * np.linalg.norm(mean)
    cos = float(m @ mean / denom) if denom else 0.0

    # A dead dimension — zero deviation across the whole set — would make
    # the ratio a division by zero. None was found on any measured space
    # (the smallest was 2.3e-06), but a space that had one must not turn
    # into a NaN that compares false against every bound.
    live = sd > 0
    ratio = np.full(sd.shape, np.nan)
    ratio[live] = v.std(axis=0)[live] / sd[live]
    finite = ratio[np.isfinite(ratio)]
    if finite.size == 0:
        raise CalibrationError("the envelope has no dimension that varies")

    lo = float(np.percentile(finite, ENVELOPE_QUANTILE))
    hi = float(np.percentile(finite, 100 - ENVELOPE_QUANTILE))
    band_lo, band_hi = ENVELOPE_BAND
    reasons = []
    if cos < ENVELOPE_COS_MIN:
        reasons.append(f"mean-vector cosine {cos:.4f} < {ENVELOPE_COS_MIN}")
    if lo < band_lo:
        reasons.append(f"p{ENVELOPE_QUANTILE} of the deviation ratios "
                       f"{lo:.3f} < {band_lo}")
    if hi > band_hi:
        reasons.append(f"p{100 - ENVELOPE_QUANTILE} of the deviation ratios "
                       f"{hi:.3f} > {band_hi}")
    return {
        "passed": not reasons,
        "reasons": reasons,
        "cosine": cos,
        "p_lo": lo,
        "p_hi": hi,
        # Evidence, never the decision (03 §3.5).
        "ratio_min": float(finite.min()),
        "ratio_max": float(finite.max()),
        "dead_dims": int((~live).sum()),
    }


def build_member(texts, embed, *, normalise: bool, dim: int):
    """Embed the set and produce the member's two tensors and their hashes.

    06 §6.6: "the calibration comes first and needs no record … `create`
    embeds it with the named encoder, writes the reference vectors' hash
    into `space.calibration` and their envelope into `space.envelope`, and
    stores the vectors as the drag member". So these two hashes are OUTPUTS
    of `create`, never inputs a recipe supplies — a recipe carrying them
    would be asserting the result of a computation it did not do.
    """
    import numpy as np
    got = embed(list(texts))
    if len(got) != SIZE:
        raise CalibrationError(f"the encoder returned {len(got)} vectors for "
                               f"{SIZE} calibration texts")
    v = np.asarray(got, dtype=np.float32)
    if v.shape != (SIZE, dim):
        raise CalibrationError(f"the encoder returned {v.shape} vectors, the "
                               f"space is ({SIZE}, {dim})")
    if normalise:
        # 09 §9.12: "unit-normalised when `space.normalise`". Done here and
        # not left to the endpoint, because `normalise` is inside `space_id`
        # and so is a property of the space rather than of a deployment.
        norms = np.linalg.norm(v, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        v = np.ascontiguousarray(v / norms, dtype=np.float32)
    env = envelope_of(v)
    return v, env, tensor_hash(v), tensor_hash(env)


def canonical_safetensors(raw: bytes) -> bytes:
    """The same file with its header's keys in sorted order.

    MEASURED, not assumed: six identical `save_file` calls produce six
    different files. safetensors serialises the header from a Rust hash map,
    so `__metadata__`'s key order varies from write to write. The tensors
    and their offsets are identical; only the header's byte order moves.

    That is fatal for a drag member. A member's transport root is a BitTorrent
    v2 pieces root over the FILE (09 §9.4, §9.16), so a header that reorders
    itself gives the same collection a different root on every write — two
    honest nodes with the same encoder would publish snapshots that disagree,
    and re-running `create` would appear to change the collection.

    The data buffer is untouched and `data_offsets` are relative to its
    start, so re-serialising the header is safe; the header is padded to a
    multiple of 8 so the data stays aligned as `save_file` leaves it.
    """
    n = int.from_bytes(raw[:8], "little")
    header = json.loads(raw[8:8 + n])
    text = json.dumps(header, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")
    text += b" " * (-len(text) % 8)
    return len(text).to_bytes(8, "little") + text + raw[8 + n:]


def write_member(path, vectors, envelope_, *, set_id_: str, encoder_image: str,
                 encoder_weights: str, key: str) -> None:
    """`calibration/<set>.safetensors` at the drag root, with 09 §9.12's
    four headers. A member of every snapshot (`files`, §9.4), so its bytes
    are made deterministic before they land.

    `key` is the CREATOR'S key and §9.12 marks it required, so it may not be
    empty: the member records who produced this reference, and a reference
    nobody is named for is one no peer can weigh.
    """
    if not key:
        raise CalibrationError(
            "the calibration member's `key` header is the creator's key and "
            "09 §9.12 marks it required; a reference with no producer named "
            "is one no peer can weigh")
    import tempfile
    from pathlib import Path

    from safetensors.numpy import save_file

    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=p.parent) as tmp:
        staged = Path(tmp) / p.name
        save_file({VECTORS: vectors, ENVELOPE: envelope_}, str(staged),
                  metadata={"set": set_id_, "encoder_image": encoder_image,
                            "encoder_weights": encoder_weights, "key": key})
        p.write_bytes(canonical_safetensors(staged.read_bytes()))


def read_member(path, *, expect_vectors: str, expect_envelope: str):
    """The two tensors, refusing a member whose tensors hash otherwise.

    09 §9.12: "a member whose tensors hash otherwise is refused" — and note
    what binds it, which is not the signature beside it: "the Codex's
    hashes, not the signature, are what bind it".
    """
    from safetensors.numpy import load_file
    tensors = load_file(str(path))
    for name in (VECTORS, ENVELOPE):
        if name not in tensors:
            raise CalibrationError(f"{path}: no `{name}` tensor (09 §9.12)")
    got_v, got_e = tensor_hash(tensors[VECTORS]), tensor_hash(tensors[ENVELOPE])
    if got_v != expect_vectors:
        raise CalibrationError(
            f"{path}: the `vectors` tensor hashes to {got_v}, the Codex says "
            f"{expect_vectors}. The reference is what every peer's encoder is "
            f"compared against, so a member that is not the one the Codex "
            f"names compares nothing")
    if got_e != expect_envelope:
        raise CalibrationError(
            f"{path}: the `envelope` tensor hashes to {got_e}, the Codex says "
            f"{expect_envelope}")
    return tensors[VECTORS], tensors[ENVELOPE]
