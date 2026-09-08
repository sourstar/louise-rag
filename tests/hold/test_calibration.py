"""tests/hold/test_calibration.py — the calibration set is what §9.12 fixes,
and it spans the regimes it exists to span.

docs/09-formats.md §9.12; docs/03-checking.md §3.3.
"""
import hashlib
import json

import pytest

from app.hold import calibration as cal
from app.hold.calibration import CalibrationError


def a_set(n=cal.SIZE):
    return [f"Calibration text number {i}, of no particular interest." for i in range(n)]


# ---- the file's bytes -----------------------------------------------------

def test_a_line_is_index_then_text_compact_and_newline_terminated():
    """09 §9.12: `{"i": <0…511>, "text": "…"}`, compact, `\\n`-terminated."""
    line = cal.line_of(7, "a text")
    assert line == b'{"i":7,"text":"a text"}\n'
    assert list(json.loads(line)) == ["i", "text"]


def test_the_id_is_the_hash_of_the_whole_file():
    texts = a_set()
    data = cal.render(texts)
    assert cal.set_id(data) == "sha256:" + hashlib.sha256(data).hexdigest()
    assert data.count(b"\n") == cal.SIZE


def test_the_path_segment_is_the_ids_first_sixteen_digits():
    sid = "sha256:" + "ab" * 32
    assert cal.segment(sid) == "ab" * 8
    assert len(cal.segment(sid)) == 16


def test_the_file_round_trips_through_its_own_bytes():
    texts = a_set()
    assert list(cal.parse(cal.render(texts))) == texts


def test_non_ascii_is_written_as_itself_not_escaped():
    """`ensure_ascii=False`: the file is UTF-8, and \\u escapes would make
    two peers' files differ while carrying the same texts."""
    line = cal.line_of(0, "Ελληνικά και русский")
    assert "Ελληνικά" in line.decode("utf-8")
    assert b"\\u" not in line


# ---- what a set must satisfy ---------------------------------------------

def test_a_set_is_exactly_512_texts():
    with pytest.raises(CalibrationError, match="512 texts, got 511"):
        cal.check(a_set(511))
    with pytest.raises(CalibrationError, match="512 texts, got 513"):
        cal.check(a_set(513))


def test_a_duplicate_wastes_a_probe_and_is_refused():
    texts = a_set()
    texts[400] = texts[7]
    with pytest.raises(CalibrationError, match="7 and 400 are identical"):
        cal.check(texts)


def test_an_empty_text_is_refused():
    texts = a_set()
    texts[3] = "   "
    with pytest.raises(CalibrationError, match="text 3 is empty"):
        cal.check(texts)


def test_text_must_be_nfc_normalised():
    """Two peers whose platforms normalise differently would embed
    different bytes for the same-looking text."""
    texts = a_set()
    texts[9] = "café"                     # e + combining acute, NFD
    with pytest.raises(CalibrationError, match="not NFC-normalised"):
        cal.check(texts)
    texts[9] = "café"                      # the NFC form
    cal.check(texts)


def test_a_control_character_is_refused():
    for bad in ("a\tb", "a\nb", "a\rb", "a​b"):
        texts = a_set()
        texts[11] = f"text with {bad} in it"
        with pytest.raises(CalibrationError, match="character U\\+"):
            cal.check(texts)


# ---- the index is dense, because `sample` draws over it -------------------

def test_a_hole_in_the_index_is_refused():
    """09 §9.12: the sampler draws over `{"0"…"511"}`. A hole would shrink
    the universe silently, so the quorum would be drawn from fewer texts
    than the Codex believes."""
    data = cal.render(a_set())
    lines = data.split(b"\n")[:-1]
    lines[5] = b'{"i":99,"text":"out of order"}'
    with pytest.raises(CalibrationError, match="index is 99"):
        list(cal.parse(b"\n".join(lines) + b"\n"))


def test_an_extra_key_is_refused():
    data = b'{"i":0,"text":"a","note":"b"}\n'
    with pytest.raises(CalibrationError, match="exactly i and text"):
        list(cal.parse(data))


# ---- coverage: the thing a hash cannot check -----------------------------

def test_a_uniform_set_passes_check_and_fails_coverage():
    """The failure mode `check` cannot see: 512 short English sentences
    hash fine, and would miss a build that is wrong only on Cyrillic or
    only past a hundred tokens."""
    cal.check(a_set())
    c = cal.coverage(a_set())
    assert c["non_latin"] == 0
    assert c["long"] == 0
    assert c["ascii_only"] == cal.SIZE


def test_coverage_counts_the_regimes_the_set_exists_to_span():
    texts = ["short one here"] * 0 + [
        "a very short text",                                   # short, ascii
        " ".join(["word"] * 100),                              # long
        "Ελληνικά κείμενα για δοκιμή του κωδικοποιητή εδώ.",   # non-latin
        "Une phrase française avec des accents aigus ici.",     # latin diacritics
        "The reading was 12.5 °C at 3,400 m on 2001-04-17.",   # numeric
        "He said (quietly): \"it's fine\" — well, mostly...",   # punctuation dense
        "The NASA and ESA teams agreed on the ISO standard.",   # upper runs
    ]
    texts += [f"filler text number {i} for the count" for i in range(cal.SIZE - len(texts))]
    c = cal.coverage(texts)
    assert c["short"] >= 1 and c["long"] >= 1
    assert c["non_latin"] >= 1 and c["latin_diacritics"] >= 1
    assert c["numeric"] >= 1 and c["punctuation_dense"] >= 1
    assert c["has_upper_run"] >= 1
    assert c["short"] + c["medium"] + c["long"] == cal.SIZE
    assert c["ascii_only"] + c["latin_diacritics"] + c["non_latin"] == cal.SIZE


# ---- reading the set this platform ships ---------------------------------

def test_a_missing_modality_says_so_rather_than_guessing():
    with pytest.raises(CalibrationError, match="no calibration set for modality"):
        cal.load("klingon")


def test_verify_refuses_a_platform_whose_set_hashes_otherwise(monkeypatch, tmp_path):
    """The set is not a member and no snapshot lists it, so this hash is the
    only thing binding what a peer embeds to what the creator embedded."""
    d = tmp_path / "calibration"
    d.mkdir()
    (d / "text.jsonl").write_bytes(cal.render(a_set()))
    monkeypatch.setattr(cal, "SETS_DIR", d)
    real, _ = cal.load("text")
    assert cal.verify("text", real)
    with pytest.raises(CalibrationError, match="names sha256:00"):
        cal.verify("text", "sha256:" + "00" * 32)
