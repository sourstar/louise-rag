"""The compiled `cdc` boundary loop cuts EXACTLY where the Python loop cuts.

app/chunking.py hands its per-character `cdc` loop to native/louise_cdc
(Rust, PyO3; docs/notes/python-hot-spots.md, "D") when the wheel is
installed. One moved boundary is a different chunk under the same space id
(invariant 3: wikipedia's tens of millions of chunks), so the Python loop
`_cdc_boundaries` stays, unchanged, as the reference and the fallback, and
the two are compared here:

* the version-1 cut PINNED: digests of the Python loop's boundaries on
  seeded texts, taken before the extension existed; both paths must give
  them, so neither can drift, nor drift with the other;
* every string literal of the test suite and every fixture file under
  tests/ (as UTF-8 with its bad bytes as lone surrogates, and as Latin-1),
  and the calibration set's prose in 18 languages, at wikipedia's sizes and
  at sizes small enough to cut each many times;
* 1,000 seeded texts (tools/measure/cdc_identity.py: ASCII, controls,
  Latin-1, CJK, astral planes and emoji sequences, combining stacks, lone
  surrogates; 0 to 40,000 code points), each at three sizes. Outside the
  suite, ≥ 10,000: `python -m tools.measure.cdc_identity --generated 20000`;
* the chunkers that cut by cdc (`chunk_cdc`, `chunk_akn_article`, through
  `layers.chunks_for_document`) on the wikipedia and normattiva fixtures,
  the extension on and forced off;
* what the extension declines — a lone surrogate, a size past 64 bits, a
  `str` subclass — goes to the Python loop, and nothing raises.

Without the wheel the comparisons skip and the rest of the suite runs the
Python loop: the suite runs both ways. The node's records:
tools/measure/cdc_identity.py, ≥ 100,000 before any deploy.
"""
from __future__ import annotations

import ast
import functools
import hashlib
import json
import random
from pathlib import Path

import pytest

from app import chunking, layers
from tools.measure import cdc_identity as ident

REPO = Path(__file__).resolve().parents[1]
V1 = (chunking.CDC_MIN_CHARS, chunking.CDC_TARGET_CHARS, chunking.CDC_MAX_CHARS)
#: sizes small enough to cut the suite's short strings many times
SMALL = ((1, 2, 3), (16, 64, 128), (200, 600, 900))
GENERATED = 1_000
#: the longest generated text in the suite (the tool goes to 400,000)
LONG = 40_000


@pytest.fixture
def native():
    if chunking._NATIVE is None:
        pytest.skip("louise_cdc is not installed: the Python loop is the only path")
    return chunking._NATIVE


def _python(text, sizes) -> list[int]:
    return list(chunking._cdc_boundaries(text, *sizes))


def _same(texts, sizes_list) -> tuple[int, list]:
    """(boundaries compared, what differs) over every text at every sizes."""
    n, bad = 0, []
    for t in texts:
        for s in sizes_list:
            got = ident.compare(t, s)
            n += got["boundaries"]
            if got["differ"]:
                bad.append((t[:40], got["differ"]))
    return n, bad


# ---------------------------------------------------------------- the pin

#: emoji, a ZWJ sequence and a flag; letters under combining marks
EMOJI = "".join(map(chr, (0x1F600, 0x1F642, 0x1F680, 0x1F30D, 0x1F9D1, 0x200D, 0x1F4BB,
                           0x1F1EE, 0x1F1F9)))
MARKS = "".join(map(chr, (0x61, 0x301, 0x65, 0x300, 0x6F, 0x302)))


@functools.cache
def _golden_texts() -> tuple[str, ...]:
    """Seeded texts, written out here so that nothing else can move them:
    words in five scripts, the astral planes, combining marks, 0 to 30,000
    code points."""
    scripts = ["abcdefghijklmnopqrstuvwxyz", "àéîõüßçñøåæœ", "αβγδεζηθικλμ",
               "日本語の文章中国汉字한국어", EMOJI, MARKS]
    out = []
    for seed in range(48):
        rng = random.Random(f"cdc-v1-golden:{seed}")
        chars = "".join(rng.sample(scripts, rng.randint(1, 3)))
        vocab = ["".join(rng.choice(chars) for _ in range(rng.randint(1, 10))) for _ in range(300)]
        n, words = rng.randint(0, 30_000), []
        while sum(map(len, words)) < n:
            words.append(rng.choice(vocab) + rng.choice(" \n,."))
        out.append("".join(words))
    return tuple(out)


#: sha256 of json.dumps of the boundaries ([[…] per sizes] per text), from
#: `_cdc_boundaries` as b1d6bc1 holds it (its app/chunking.py, the commit
#: before the extension): the version-1 cut, 5,777 boundaries over 611,204
#: code points
GOLDEN_SIZES = (V1, (200, 600, 900), (16, 64, 128))
GOLDEN_DIGEST = "8edc4e440ce4b54abc9e3bb490857a1bf348b4007c3bcc60555b35bad27d4eb8"
#: the gear table itself (blake2b of each byte value, person b"louise-cdc")
GEAR_DIGEST = "3742dd185d3e2dda528adf3846cf506f1a46ec0eb0c8becf7d477d900bf42899"


def _digest(cut) -> str:
    got = [[cut(t, s) for s in GOLDEN_SIZES] for t in _golden_texts()]
    return hashlib.sha256(json.dumps(got).encode()).hexdigest()


def test_the_version_1_cut_is_pinned_on_the_python_loop():
    assert hashlib.sha256(json.dumps(chunking._GEAR).encode()).hexdigest() == GEAR_DIGEST
    assert (chunking._MASK_STRICT, chunking._MASK_LOOSE) == ((1 << 14) - 1, (1 << 10) - 1)
    assert _digest(_python) == GOLDEN_DIGEST


def test_the_version_1_cut_is_pinned_on_the_extension(native):
    assert _digest(lambda t, s: native.boundaries(t, *s)) == GOLDEN_DIGEST


def test_the_extension_is_handed_this_modules_table(native):
    assert native.table == list(chunking._GEAR)
    assert native.masks == (chunking._MASK_STRICT, chunking._MASK_LOOSE)


# ---------------------------------------------------------------- the suite's own text

def _literals() -> list[str]:
    out: dict[str, None] = {}
    for p in sorted((REPO / "tests").rglob("*.py")):
        for node in ast.walk(ast.parse(p.read_text(encoding="utf-8-sig"))):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                out.setdefault(node.value, None)
    return list(out)


def _fixtures() -> list[str]:
    """Every fixture file, as UTF-8 (a byte that is not one becomes a lone
    surrogate: a text with no UTF-8 form) and as Latin-1 (every byte a code
    point below 256)."""
    out = []
    for p in sorted((REPO / "tests" / "fixtures").rglob("*")):
        if p.is_file():
            raw = p.read_bytes()
            out += [raw.decode("utf-8", "surrogateescape"), raw.decode("latin-1")]
    return out


def test_every_string_of_the_test_suite_cuts_the_same(native):
    texts = _literals()
    assert len(texts) > 10_000
    n, bad = _same(texts, (V1, *SMALL))
    assert bad == []
    assert n > 400_000


def test_every_fixture_file_cuts_the_same(native):
    texts = _fixtures()
    assert len(texts) >= 60
    assert any(not ident.answerable(t, V1) for t in texts), "no fixture took the fallback"
    n, bad = _same(texts, (V1, (200, 600, 900)))
    assert bad == []
    assert n > 10_000


def _calibration() -> list[str]:
    lines = (REPO / "calibration" / "text.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line)["text"] for line in lines]


def test_the_calibration_prose_cuts_the_same(native):
    """Real prose: Gutenberg books in 18 languages, up to 32,768 characters."""
    texts = _calibration()
    assert len(texts) == 512
    n, bad = _same(texts, (V1, (200, 600, 900)))
    assert bad == []
    assert n > 4_000


# ---------------------------------------------------------------- generated texts

def test_generated_texts_cut_the_same(native):
    kinds, declined, boundaries, chars, bad = set(), 0, 0, 0, []
    for i in range(GENERATED):
        text, sizes = ident.generated(0, i, long=LONG)
        chars += len(text)
        for s in sizes:
            got = ident.compare(text, s)
            boundaries += got["boundaries"]
            declined += got["declined"]
            if got["differ"]:
                bad.append((i, got["differ"]))
        kinds.update(n for n, rs in ident.ALPHABETS.items()
                     if any(lo <= ord(c) <= hi for lo, hi in rs for c in text[:200]))
    assert bad == []
    assert kinds == set(ident.ALPHABETS), "every alphabet drawn"
    assert declined > 30, "texts with lone surrogates went to the Python loop"
    assert chars > 5_000_000 and boundaries > 200_000


# ---------------------------------------------------------------- through the chunkers

def _both(fn):
    """`fn()` with the extension, then with the Python loop forced."""
    got = fn()
    with ident.python_loop():
        assert chunking._NATIVE is None
        want = fn()
    return got, want


def _wikipedia_sections():
    from app.plugins.wikipedia import wikitext
    from tests.test_wikipedia_parser3 import ARTICLE
    from tests.test_wikitext_identity import _scaled
    out = []
    for page in (ARTICLE, _scaled(2_000), _scaled(20_000), _scaled(60_000)):
        out.append(wikitext.parse_all(page)["sections"])
    # sections of real prose, as a long page's would be
    prose = _calibration()
    out.append([(f"S{i}", t) for i, t in enumerate(prose[:64])])
    out.append([("", "\n\n".join(prose[64:96]))])
    return out


def test_wikipedias_cut_is_the_same_both_ways(native):
    from app.hold import template
    from app.plugins.wikipedia import SOURCE as WIKIPEDIA
    layer = template.default_layers(WIKIPEDIA)[-1]
    assert layer.chunker.id == chunking.CDC
    pages = _wikipedia_sections()
    for params in (None, {}, layer.chunker.params, {"target": 600, "min": 200, "max": 900}):
        got, want = _both(lambda params=params: [
            list(chunking.chunk_cdc("wikipedia:1", 1, s, 1, params)) for s in pages])
        assert got == want
        assert sum(map(len, got)) > len(pages)
    for s in pages:
        for _, body in s:
            got, want = _both(lambda body=body: chunking.cdc_split((body or "").strip()))
            assert got == want


def test_a_layered_collections_cut_is_the_same_both_ways(native):
    """`layers.chunks_for_document`, the one path every stage cuts by."""
    class Codexish:
        layers = (layers.Layer(chunker=chunking.CDC, version=1, rule="default",
                               params={"target": 4000, "min": 1000, "max": 8000}),)
    sections = [s for page in _wikipedia_sections() for s in page]
    row = {"uid": "wikipedia:7", "native_id": 7}
    got, want = _both(lambda: layers.chunks_for_document(Codexish(), row, sections))
    assert got == want and len(got) > 50


class _Counting:
    """The extension, counting the texts it is asked to cut."""

    def __init__(self, inner):
        self.inner, self.calls = inner, 0

    def boundaries(self, *args):
        self.calls += 1
        return self.inner.boundaries(*args)


def test_normattivas_long_commas_are_cut_the_same_both_ways(native, monkeypatch):
    from app.plugins.normattiva import akn, rendition
    counting = _Counting(native)
    monkeypatch.setattr(chunking, "_NATIVE", counting)
    acts = [rendition.sections(akn.parse(p.read_bytes()))
            for p in sorted((REPO / "tests/fixtures/normattiva").glob("*.xml"))]
    # whole acts as one section, so that a comma past `max_chars` meets cdc
    acts.append([("", "\n".join(b for a in acts for _, b in a))])
    for params in (None, {"max_chars": 300}, {"max_chars": 50}):
        got, want = _both(lambda params=params: [
            list(chunking.chunk_akn_article("n:1", 1, a, 1, params)) for a in acts])
        assert got == want
    assert counting.calls > 0, "a comma long enough for cdc to cut"


# ---------------------------------------------------------------- what it declines

@pytest.mark.parametrize("text", [
    "a\ud800b" * 3_000,                       # a lone high surrogate
    "x" * 2_000 + "\udfff" + "y" * 9_000,     # a lone low one past the minimum
    (chr(0xD83D) + chr(0xDE00)) * 5_000,      # a pair, as two code points: no UTF-8 form
    "é" * 1_500 + "\udc80",                   # what surrogateescape makes of a bad byte
])
def test_a_text_with_no_utf8_form_takes_the_python_loop(native, text):
    assert native.boundaries(text, *V1) is None
    want = _python(text, V1)
    assert list(chunking._cdc_ends(text, *V1)) == want
    assert chunking.cdc_split(text) == [text[a:b] for a, b in zip([0, *want], want)]
    assert "".join(chunking.cdc_split(text)) == text


@pytest.mark.parametrize("sizes", [(1, 2, 1 << 64), (1 << 64, 1 << 65, 1 << 66), (-1, 4000, 8000),
                                   (1.0, 4000, 8000), ("1", 4000, 8000)])
def test_a_size_it_cannot_hold_is_declined(native, sizes):
    assert native.boundaries("x" * 10_000, *sizes) is None


def test_a_str_subclass_or_not_a_str_is_declined(native):
    class Text(str):
        pass
    assert native.boundaries(Text("x" * 10_000), *V1) is None
    assert native.boundaries(b"x" * 10_000, *V1) is None
    assert native.boundaries(None, *V1) is None


def test_the_python_loops_endless_case_is_declined(native):
    """`max = 0` with text past `min`: the Python loop yields `start` for
    ever; the extension does not try. Short of `min` it ends, both ways."""
    assert native.boundaries("x" * 100, 10, 0, 0) is None
    for text in ("", "x", "x" * 10):
        assert native.boundaries(text, 10, 0, 0) == _python(text, (10, 0, 0))


@pytest.mark.parametrize("text", ["", "x", "x" * 1000, "x" * 1001, "€" * 1001, "𝄞" * 9000])
def test_the_edges_of_the_minimum(native, text):
    for s in (V1, (1, 2, 2), (999, 1000, 1001), (1000, 1001, 1001)):
        assert native.boundaries(text, *s) == _python(text, s)
