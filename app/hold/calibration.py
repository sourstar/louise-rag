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

A PAIR SPACE IS THE EXCEPTION (09 §9.12 as amended 2026-10-08; operator,
docs/notes/input-forms.md). Its encoder is fed tokenizer text pairs, and a
gate over plain texts would pass a backend that flattens pairs — the very
defect the pair form exists to keep out. So its set is 512 title-and-
abstract PAIRS drawn by a fixed rule from the collection's own records
(`draw_pairs`, run by tools/build_pair_calibration_set.py as
tools/build_calibration_set.py drew the text set), and it travels INSIDE the
member as the `pairs` tensor — the bytes of its file, whose `H` is
`space.calibration.set`. Not in the platform image: those records are the
collection's, under its licence, and the member is fetched only after the
acknowledgement (04 §4.4). The four reasons above still hold where they can:
the set is fixed before `create`, two collections of the space share it,
and a newcomer gets it with the member.
"""
from __future__ import annotations

import hashlib
import json
import unicodedata
from pathlib import Path
from typing import Callable, Iterable, Iterator, Sequence

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
# a PAIR space's set (09 §9.12, amended 2026-10-08)

#: An item of a calibration set as the space's encoder is fed it: a text, or
#: in a pair space a `(first, second)` tokenizer text pair (09 §9.3).
Item = str | tuple[str, str]

#: The member's tensor that carries a pair space's set: the bytes of its
#: file, u8, `[bytes]`. `H` over them is `space.calibration.set`.
PAIRS = "pairs"

#: The one character a pair's segment may carry that a text set refuses: the
#: line feed that joins an abstract's sections (`chunking.chunk_abstract`),
#: which is what the encoder is fed and so what the set must hold.
_PAIR_ALLOWED = {"\n"}


def form(codex) -> str:
    """`text` or `pair`: the form the space's layers feed the encoder, and so
    the form its calibration set is in. A space whose layers feed both has
    no one set (`layers.form_of`) and is refused."""
    from app import layers as layers_mod
    try:
        return layers_mod.form_of(codex)
    except layers_mod.LayerError as e:
        raise CalibrationError(str(e)) from None


def query_text(item: Item) -> str:
    """What the QUERY side embeds for an item. A query is one text in every
    space (MedCPT's query encoder takes one), so a pair is given to it as the
    abstract chunker joins a record: the first segment, a blank line, the
    second. A text is itself."""
    if isinstance(item, str):
        return item
    first, second = item
    return f"{first}\n\n{second}"


def item_bytes(item: Item) -> int:
    """The UTF-8 byte length an item is ordered by (09 §9.12's fingerprint):
    a text's, or a pair's two segments together."""
    if isinstance(item, str):
        return len(item.encode("utf-8"))
    return len(item[0].encode("utf-8")) + len(item[1].encode("utf-8"))


def item_chars(item: Item) -> int:
    """Characters, for the benchmark's token estimate (03 §3.3)."""
    return len(item) if isinstance(item, str) else len(item[0]) + len(item[1])


def pair_line(i: int, first: str, second: str) -> bytes:
    """One line of a pair set: `{"i": <0…511>, "pair": [first, second]}`,
    compact, `\\n`-terminated (09 §9.12), keys in that order."""
    return (json.dumps({"i": i, "pair": [first, second]}, ensure_ascii=False,
                       separators=(",", ":")) + "\n").encode("utf-8")


def render_pairs(pairs: Sequence[tuple[str, str]]) -> bytes:
    """The whole pair set file, in index order. Its bytes are its id."""
    check_pairs(pairs)
    return b"".join(pair_line(i, a, b) for i, (a, b) in enumerate(pairs))


def parse_pairs(data: bytes) -> Iterator[tuple[str, str]]:
    """A pair set read back, refusing anything `render_pairs` would not have
    written — dense, in order, each line exactly `i` and `pair`."""
    for want, raw in enumerate(data.split(b"\n")[:-1]):
        obj = json.loads(raw)
        if set(obj) != {"i", "pair"}:
            raise CalibrationError(f"pair line {want}: keys are exactly i and pair")
        if obj["i"] != want:
            raise CalibrationError(
                f"pair line {want}: index is {obj['i']}; the set is dense and in "
                f"order (09 §9.12)")
        pair = obj["pair"]
        if (not isinstance(pair, list) or len(pair) != 2
                or not all(isinstance(s, str) for s in pair)):
            raise CalibrationError(f"pair line {want}: `pair` is two strings")
        yield pair[0], pair[1]


def _pair_fault(first, second) -> str | None:
    """Why a pair cannot be a calibration item, or None. The text set's rules
    with one difference: a segment may carry the line feed the abstract
    chunker joins sections with."""
    for name, s in (("first", first), ("second", second)):
        if not isinstance(s, str) or not s.strip():
            return f"its {name} segment is empty"
        if unicodedata.normalize("NFC", s) != s:
            return f"its {name} segment is not NFC-normalised"
        for ch in s:
            if ch not in _PAIR_ALLOWED and unicodedata.category(ch) in _FORBIDDEN:
                return (f"its {name} segment carries {unicodedata.category(ch)} "
                        f"character U+{ord(ch):04X}")
    return None


def check_pairs(pairs: Sequence[tuple[str, str]]) -> None:
    """Refuse a pair set that could not do its job: 512, both segments of
    each non-empty, NFC, no control character but the line feed, no two the
    same."""
    if len(pairs) != SIZE:
        raise CalibrationError(f"a calibration set is {SIZE} pairs, got {len(pairs)}")
    seen: dict[tuple[str, str], int] = {}
    for i, p in enumerate(pairs):
        if not isinstance(p, (tuple, list)) or len(p) != 2:
            raise CalibrationError(f"item {i} is not a pair")
        fault = _pair_fault(p[0], p[1])
        if fault:
            raise CalibrationError(f"pair {i}: {fault}")
        key = (p[0], p[1])
        if key in seen:
            raise CalibrationError(f"pairs {seen[key]} and {i} are identical")
        seen[key] = i


def pairs_of(data: bytes, expect: str | None = None) -> list[tuple[str, str]]:
    """The pairs of a pair set file, checked, and refused when its bytes do
    not hash to `expect` (the Codex's `space.calibration.set`)."""
    if expect is not None and set_id(data) != expect:
        raise CalibrationError(
            f"the pair set hashes to {set_id(data)}, the Codex names {expect}: "
            f"reference vectors over other pairs compare nothing")
    pairs = list(parse_pairs(data))
    check_pairs(pairs)
    return pairs


def read_pairs(path, *, expect: str) -> bytes:
    """The bytes of a pair space's set, from its member's `pairs` tensor,
    refused when they do not hash to `expect` (09 §9.12)."""
    import numpy as np
    from safetensors.numpy import load_file
    tensors = load_file(str(path))
    if PAIRS not in tensors:
        raise CalibrationError(
            f"{path}: no `{PAIRS}` tensor, and the space's layers feed the encoder "
            f"pairs: a pair space's set travels in its member (09 §9.12)")
    data = np.ascontiguousarray(tensors[PAIRS], dtype=np.uint8).tobytes()
    if set_id(data) != expect:
        raise CalibrationError(
            f"{path}: the `{PAIRS}` tensor hashes to {set_id(data)}, the Codex says "
            f"{expect}")
    return data


def items(codex, member=None) -> list[Item]:
    """The space's calibration set as its encoder is fed it, in set order.

    A TEXT space: the platform's file, exactly as `verify` always gave it. A
    PAIR space: the pairs its member carries (`member`, the path of
    `calibration/<set>.safetensors`), bound by `space.calibration.set`."""
    if form(codex) == "text":
        return list(verify(codex.modality, codex.space.calibration.set))
    if member is None:
        raise CalibrationError(
            "a pair space's calibration set travels in its member (09 §9.12): "
            "the member's path is needed to read it")
    want = codex.space.calibration.set
    return list(pairs_of(read_pairs(member, expect=want), want))


#: A pair candidate: `(key, first, second)` — the key is the record's uid.
Candidate = tuple[str, str, str]


def draw_pairs(candidates: Callable[[], Iterable[Candidate]],
               n: int | None = None) -> list[Candidate]:
    """09 §9.12's rule for a pair space's set: from the candidates in the
    order given (the snapshot's), those whose pair is admissible
    (`_pair_fault`), the first of any identical pairs kept; ordered by the
    UTF-8 byte length of the two segments together, ties by the key's bytes;
    and the ones at ranks ⌊k × (N − 1) / (n − 1)⌋ for k = 0 … n − 1 taken,
    in that order — the shortest, the longest and the rest spaced between,
    the whole length range up to and past any `ctx`, as the text set spans
    it. A function of the records alone, so anyone holding them draws the
    same set.

    `candidates` is called TWICE and must give the same sequence both times:
    the first pass keeps a small index of every candidate (its length, its
    key, a digest of its pair) and the second fetches only the pairs drawn —
    PubMed's shards are tens of thousands of abstracts each, and holding them
    all at once is a node's whole memory. `n` is the set's size, `SIZE`."""
    n = SIZE if n is None else int(n)
    index: list[tuple[int, bytes]] = []
    seen: set[bytes] = set()
    for key, first, second in candidates():
        if _pair_fault(first, second):
            continue
        digest = hashlib.sha256(json.dumps([first, second], ensure_ascii=False)
                                .encode("utf-8")).digest()
        if digest in seen:
            continue
        seen.add(digest)
        index.append((item_bytes((first, second)), key.encode("utf-8")))
    if len(index) < n:
        raise CalibrationError(
            f"only {len(index)} admissible pairs among the candidates; a set is "
            f"{n}. Draw from more shards")
    index.sort()
    last = len(index) - 1
    chosen = [index[(k * last) // (n - 1)][1].decode("utf-8") for k in range(n)]
    want = set(chosen)
    found: dict[str, Candidate] = {}
    for key, first, second in candidates():
        if key in want and key not in found and not _pair_fault(first, second):
            found[key] = (key, first, second)
    missing = [k for k in chosen if k not in found]
    if missing:
        raise CalibrationError(
            f"the candidates changed between the two passes ({len(missing)} drawn "
            f"keys absent the second time, first {missing[0]!r}); they must be the "
            f"same sequence both times")
    return [found[k] for k in chosen]


#: How many of a snapshot's shards a pair set is drawn from: enough for
#: every length an abstract comes in, few enough that the draw reads a few
#: hundred megabytes and holds its index in a few dozen.
PAIR_SHARDS = 16


def pair_set_from_drag(drag_dir, codex, snapshot, *,
                       shards: int = PAIR_SHARDS) -> tuple[bytes, dict]:
    """`(the pair set file, its provenance)`, drawn from a collection's own
    records (09 §9.12, amended 2026-10-08) — what
    tools/build_pair_calibration_set.py writes.

    The shards: fixed by the snapshot alone —
    `sample(<the snapshot hash's 32 bytes>, shards, <the shard names>)`,
    09 §9.1's draw, walked in snapshot order. The records: in shard order,
    each cut as the PAIR form of the collection's own layers (`abstract` and
    any other chunker a pair is admitted on, 09 §9.3) — so the set can be
    drawn from a collection that today embeds text, before the pair Codex
    that will name it exists. Then `draw_pairs`. Anyone holding the snapshot
    draws the same bytes."""
    from dataclasses import replace as _replace
    from types import SimpleNamespace

    from app import layers as layers_mod
    from app.pack import records as pack_records

    from .codex import sample
    layers = tuple(_replace(l, input=dict(layers_mod.INPUT_PAIR))
                   if l.chunker in layers_mod.PAIR_CHUNKERS else l
                   for l in layers_mod.layers_for(codex))
    if not any(l.form == "pair" for l in layers):
        raise CalibrationError(
            f"no layer of {getattr(codex, 'collection_id', 'this collection')} is cut "
            f"by a chunker a pair is admitted on "
            f"({', '.join(layers_mod.PAIR_CHUNKERS)}); its records have no pairs")
    cutter = SimpleNamespace(name=getattr(codex, "plugin", ""), layers=layers,
                             codex=codex, credits=getattr(codex, "credits", None))
    names = {f"{s.slice}/{s.n:05d}": s for s in snapshot.shards}
    seed = bytes.fromhex(snapshot.hash.split(":", 1)[1])
    drawn = sample(seed, min(int(shards), len(names)), iter(names))
    chosen = sorted((names[k] for k in drawn), key=lambda s: (s.slice.encode(), s.n))
    missing = [s.path for s in chosen if not (Path(drag_dir) / s.path).is_file()]
    if missing:
        raise CalibrationError(
            f"the draw needs {len(chosen)} shards and this copy lacks {len(missing)} "
            f"(first {missing[0]}): fetch them, or draw on a peer that holds them")

    def candidates() -> Iterator[Candidate]:
        for s in chosen:
            yield from pair_candidates(
                cutter, pack_records.iter_records(Path(drag_dir) / s.path))

    picked = draw_pairs(candidates)
    data = render_pairs([(a, b) for _, a, b in picked])
    provenance = {
        "collection_id": getattr(codex, "collection_id", None),
        "snapshot_id": snapshot.snapshot_id, "snapshot_hash": snapshot.hash,
        "shards": [f"{s.slice}/{s.n:05d}" for s in chosen],
        "rule": "09 §9.12 (amended 2026-10-08): the shards by sample(snapshot hash), "
                "each record's pair by the pair form of its layer, ordered by the bytes "
                "of both segments then uid, ranks floor(k*(N-1)/511)",
        "set": set_id(data),
        "uids": [k for k, _, _ in picked],
    }
    return data, provenance


def pair_candidates(collection, records: Iterable[dict]) -> Iterator[Candidate]:
    """`(uid, first, second)` for each record, as a pair layer of `collection`
    feeds the encoder: the chunk `layers.chunks_for_document` cuts — never a
    plugin's chunker directly (invariant 1) — and the `fields` it carries.
    A record whose layer feeds text yields nothing."""
    from app import layers as layers_mod
    for rec in records:
        uid = rec["uid"]
        row = {"uid": uid, "native_id": rec.get("native_id", 0)}
        sections = [tuple(s) for s in rec.get("sections") or []]
        for c in layers_mod.chunks_for_document(collection, row, sections):
            fields = c.get("fields")
            if fields is not None:
                yield uid, str(fields[0]), str(fields[1])


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


def build_query_member(texts, embed_query, *, instruction: str | None,
                       normalise: bool, dim: int):
    """The QUERY encoder's reference vectors and their hash (09 §9.12): "the
    query encoder's reference vectors for the same texts,
    `space.query.instruction` prefixed". An asymmetric space's query runs
    are validated against them (03 §3.3) — a bi-encoder pair such as MedCPT's
    has two functions, and gating only the document side would leave every
    query vector on this peer unchecked.

    A pair space's items reach the query side as text (`query_text`): a
    query is one text in every space."""
    asked = [query_text(t) for t in texts]
    prefixed = [f"{instruction}{t}" for t in asked] if instruction else asked
    v, _envelope, vectors_hash, _ = build_member(prefixed, embed_query,
                                                 normalise=normalise, dim=dim)
    return v, vectors_hash


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


QUERY_VECTORS = "query_vectors"


def write_member(path, vectors, envelope_, *, set_id_: str,
                 encoder_weights: str, key: str,
                 encoder_image: str | None = None,
                 encoder_engine: str | None = None,
                 query_vectors=None, pairs: bytes | None = None) -> None:
    """`calibration/<set>.safetensors` at the drag root, with 09 §9.12's
    headers. A member of every snapshot (`files`, §9.4), so its bytes
    are made deterministic before they land.

    `pairs`: a PAIR space's set, the bytes of its file, stored as the `pairs`
    tensor (09 §9.12, amended 2026-10-08) — and refused unless they hash to
    the `set` the header names. A text space passes none, and its member is
    byte for byte what it was before pairs existed.

    THE RUN THE VECTORS CAME FROM is named by `encoder_engine` — the
    `engine_id` of the Codex's `space.encoder.engine` — from Codex version 3,
    and by `encoder_image`, the engine's image digest alone, below it (the
    engine amendment, 0d85f7f). Exactly one of the two.

    `query_vectors`: the QUERY encoder's reference vectors for the same
    texts, in an asymmetric space from version 3 — what its query runs are
    validated against (03 §3.3).

    `key` is the CREATOR'S key and §9.12 marks it required, so it may not be
    empty: the member records who produced this reference, and a reference
    nobody is named for is one no peer can weigh.
    """
    if (encoder_image is None) == (encoder_engine is None):
        raise CalibrationError(
            "the calibration member names the run its vectors came from by "
            "`encoder_engine` (Codex version 3) or by `encoder_image` (below it): "
            "exactly one (09 §9.12)")
    if not key:
        raise CalibrationError(
            "the calibration member's `key` header is the creator's key and "
            "09 §9.12 marks it required; a reference with no producer named "
            "is one no peer can weigh")
    if pairs is not None and set_id(pairs) != set_id_:
        raise CalibrationError(
            f"the pair set given hashes to {set_id(pairs)} and the member names set "
            f"{set_id_}: a member carries the set its vectors were produced over")
    import tempfile
    from pathlib import Path

    from safetensors.numpy import save_file

    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=p.parent) as tmp:
        staged = Path(tmp) / p.name
        tensors = {VECTORS: vectors, ENVELOPE: envelope_}
        if query_vectors is not None:
            tensors[QUERY_VECTORS] = query_vectors
        if pairs is not None:
            import numpy as np
            tensors[PAIRS] = np.frombuffer(pairs, dtype=np.uint8).copy()
        run = ({"encoder_engine": encoder_engine} if encoder_engine is not None
               else {"encoder_image": encoder_image})
        save_file(tensors, str(staged),
                  metadata={"set": set_id_, **run,
                            "encoder_weights": encoder_weights, "key": key})
        p.write_bytes(canonical_safetensors(staged.read_bytes()))


def read_query_vectors(path, *, expect: str):
    """The query encoder's reference vectors, refused when they hash
    otherwise than `space.calibration.query_vectors` says (09 §9.12)."""
    from safetensors.numpy import load_file
    tensors = load_file(str(path))
    if QUERY_VECTORS not in tensors:
        raise CalibrationError(
            f"{path}: no `{QUERY_VECTORS}` tensor, and the Codex pins one: an "
            f"asymmetric space's query runs are validated against it (09 §9.12)")
    got = tensor_hash(tensors[QUERY_VECTORS])
    if got != expect:
        raise CalibrationError(
            f"{path}: the `{QUERY_VECTORS}` tensor hashes to {got}, the Codex says "
            f"{expect}")
    return tensors[QUERY_VECTORS]


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
