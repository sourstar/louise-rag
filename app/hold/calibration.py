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
