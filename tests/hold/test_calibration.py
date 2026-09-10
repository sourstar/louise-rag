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


# ---- the set this platform actually ships --------------------------------

def test_the_shipped_text_set_is_512_valid_texts():
    sid, texts = cal.load("text")
    assert len(texts) == cal.SIZE
    cal.check(texts)
    assert sid == cal.set_id(cal.path_for("text").read_bytes())


def test_the_shipped_set_spans_the_regimes_it_exists_to_span():
    """The failure a hash cannot see. These floors are deliberately loose —
    they exist to catch a set that lost a whole regime, not to pin an exact
    composition. Tightening them would make an honest re-author fail."""
    _, texts = cal.load("text")
    c = cal.coverage(texts)
    assert c["short"] >= 40, "no short texts: pooling differences hide"
    assert c["long"] >= 40, "no long texts: truncation differences hide"
    assert c["non_latin"] >= 50, "no non-Latin script: a whole tokeniser regime untested"
    assert c["latin_diacritics"] >= 40, "no accented Latin"
    assert c["numeric"] >= 40, "no numerals: a tokeniser's worst regime"
    assert c["punctuation_dense"] >= 40
    assert c["short"] + c["medium"] + c["long"] == cal.SIZE
    assert c["ascii_only"] + c["latin_diacritics"] + c["non_latin"] == cal.SIZE


def test_the_shipped_set_carries_adjacent_near_duplicate_pairs():
    """32 pairs at the end, each differing in one controlled way. A build
    that is subtly wrong tends to collapse or exaggerate exactly these
    small differences, which unrelated texts would never reveal."""
    _, texts = cal.load("text")
    tail = texts[-64:]
    close = 0
    for a, b in zip(tail[0::2], tail[1::2]):
        sa, sb = set(a.split()), set(b.split())
        overlap = len(sa & sb) / max(len(sa | sb), 1)
        if overlap >= 0.7:
            close += 1
    assert close >= 24, f"only {close}/32 tail pairs are near-duplicates"


def test_the_shipped_set_reaches_the_largest_context_window():
    """WHY THE SET WAS REBUILT, 2026-09-09, as an assertion.

    The first set's longest text was 174 tokens, against spaces whose `ctx`
    is 512 (MedCPT) and 8192 (BGE-M3) — nothing in it came within a third
    of the smaller window. `approved-encoders.md` records that the b10630
    build "tested clean afterwards at every concurrency, with identical and
    with distinct long texts — all 1.000000. Short tests do not catch
    this". So the gate would have passed the one build that has actually
    damaged this corpus.

    `coverage`'s `long` counts anything over 128 tokens, which the first
    set satisfied while failing at exactly this. Length is therefore
    asserted here in tokens, at the windows real encoders declare.

    THE UNIT IS AN ESTIMATE, and the estimate was measured on 2026-09-09
    against Qwen3-Embedding-8B, the `gutenberg-pack` collection's encoder
    (`gutenberg` itself runs the 4B, at dim 2560):
    chars per real token ran 1.02 (CJK) to 5.70 (Latin prose), median 3.68.
    So `//4` is a good median and no bound in either direction, and 67 of
    the 512 texts really exceed an 8192 window — they are truncated to fit
    by `app/embeddings.py` before they reach an encoder (09 §9.12), which
    is what makes an overshooting text a better full-window probe than one
    built to a guess. Nothing here can assert real tokens: the platform
    holds no tokenizer (invariant 7), and the estimate is the only unit a
    test has.
    """
    _, texts = cal.load("text")
    tok = sorted(len(t) // 4 for t in texts)          # chunking.py's rule
    assert tok[-1] >= 8192, f"longest text is {tok[-1]} tokens; nothing fills an 8192 window"
    for window in (512, 1024, 2048, 4096, 8192):
        n = sum(1 for t in tok if t >= window)
        assert n >= 16, f"only {n} texts reach a {window}-token window"
    assert sum(1 for t in tok if t <= 16) >= 8, "the short end went with it"


def test_every_drawn_text_has_a_recorded_source():
    """09 §9.12 wants a set nobody can tune to their own build. The short
    texts are authored and the long ones are drawn from Project Gutenberg,
    so the drawn ones — and only those — carry a book id in the sidecar. A
    long text with no source, or a source with no text, means the set and
    `tools/build_calibration_set.py` have drifted apart and the set is no
    longer reproducible from the corpus.
    """
    import json
    _, texts = cal.load("text")
    sidecar = cal.path_for("text").with_name("text.sources.json")
    assert sidecar.is_file(), f"{sidecar} is missing: the set's provenance"
    sources = json.loads(sidecar.read_text(encoding="utf-8"))
    drawn = [t for t in texts if len(t) // 4 >= 175]
    assert len(sources) == len(drawn), (
        f"{len(sources)} recorded sources for {len(drawn)} drawn texts")
    for s in sources:
        assert set(s) == {"lang", "book", "nth", "tokens"}, s
        assert isinstance(s["book"], int) and s["book"] > 0, s


def test_the_shipped_sets_id_is_stable():
    """The id is the file's hash, and every Codex naming it needs this exact
    file forever. If this test fails, the set was edited — which is a NEW
    set and a new id, not a correction to this one.

    It has been rebuilt once, on 2026-09-09, for the reason the test above
    asserts; `tools/build_calibration_set.py` reproduces these bytes and
    `calibration/text.sources.json` says where each drawn text came from.
    The first set's id was sha256:6a1bac6e…f1ad6e19 — recorded because a
    Codex written before that date names it, and nothing rewrites a Codex.
    """
    sid, _ = cal.load("text")
    assert sid == ("sha256:7d06a2f6a4a773da01a517cb30571cac"
                   "421753df3e30b20fca4716e5441fbf0f")
