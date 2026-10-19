"""tools/medcpt_text_compare.py: NCBI's embedded texts against ours, by a
fingerprint of what the uncased tokenizer reads alike."""
import json

import pytest

from tools import medcpt_text_compare as mtc


def test_one_input_one_fingerprint_whatever_the_case_accents_and_spacing():
    assert mtc.fingerprint("Café  au LAIT", "a\nb") == mtc.fingerprint("cafe au lait", "a b")
    assert mtc.fingerprint("Japan", "") != mtc.fingerprint("Japan", "This news article …")
    assert mtc.fingerprint("A", "B") != mtc.fingerprint("A B", ""), "the split is the input too"


def test_our_text_is_title_a_blank_line_and_the_abstract():
    assert mtc.ours("Title.\n\nPart one.\n\nPart two.") == ("Title.", "Part one.\n\nPart two.")
    assert mtc.ours("Editorial.") == ("Editorial.", "")


@pytest.mark.parametrize("block", [3, 7, 64, 1 << 20])
def test_the_object_is_read_a_block_at_a_time_and_whole(tmp_path, block):
    d = {"12320358": {"t": "Japan's TFR plunges", "a": "This news article — résumé"},
         "5158766": {"t": "Professional team opinion", "a": "", "m": ["x", "y"]},
         "35335946": {"t": "中文 title", "a": "ü" * 50}}
    path = tmp_path / "pubmed_chunk_0.json"
    path.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    got = dict(mtc.entries(path, block=block))
    assert got == {int(k): v for k, v in d.items()}
