"""09 §9.5's two record shapes, and what keeps them apart.

"A line has no version field of its own: its shape is its Codex's
`record_line`, which is `1` for a line without `figures` and `2` for a line
with it." So the writer is told, never asked — a record_line-2 book with no
plates still carries `"figures": []`, and a record_line-1 collection's line
has no key at all.
"""
import json

import pytest

from app.hold import figures as fig_mod
from app.pack import records as pack_records
from app.plugins.base import Figure


def _record(uid="gutenberg:1", figures=None) -> dict:
    r = {"uid": uid, "native_id": uid.rsplit(":", 1)[-1], "lang": "en",
         "title": "A Book", "creators": ["Nobody"], "date": "1900",
         "subjects": [], "keys": [], "sections": [["I", "text"]],
         "extra": {}, "layer": 0, "chunks": 1,
         "origin": "1/pg1.epub", "content_hash": "ab" * 16,
         "origin_hash": "cd" * 16}
    if figures is not None:
        r["figures"] = figures
    return r


def test_shape_1_has_no_figures_key_at_all():
    line = json.loads(pack_records.canonical_line(_record()))
    assert "figures" not in line
    assert list(line) == list(pack_records.RECORD_KEYS)
    # even when a record carries one, shape 1 does not write it: the shape
    # is the SHARD's, from the Codex, never sniffed from a record
    line = json.loads(pack_records.canonical_line(_record(figures=[{"hash": "x"}])))
    assert "figures" not in line


def test_shape_2_writes_figures_between_sections_and_extra():
    keys = list(pack_records.RECORD_KEYS_2)
    assert keys[keys.index("sections") + 1] == "figures"
    assert keys[keys.index("figures") + 1] == "extra"

    line = json.loads(
        pack_records.canonical_line(_record(figures=[]), record_line=2))
    assert list(line) == keys
    assert line["figures"] == []


def test_shape_2_never_writes_null_for_a_book_with_no_plates():
    """`[]` when it has none (§9.5). A reader distinguishing "no plates"
    from "the writer forgot" is a distinction the format does not offer."""
    line = json.loads(pack_records.canonical_line(_record(), record_line=2))
    assert line["figures"] == []


def test_a_figure_entry_is_9_5s_keys_in_9_5s_order():
    f = Figure(href="images/plate01.jpg", media_type="image/jpeg",
               sha256="sha256:" + "ab" * 32, bytes=4096,
               caption="The frontispiece", seq=0)
    assert list(f.as_record()) == ["hash", "media_type", "bytes", "seq", "carried"]
    assert f.as_record() == {"hash": "sha256:" + "ab" * 32,
                             "media_type": "image/jpeg", "bytes": 4096,
                             "seq": 0, "carried": True}
    # `href` and `caption` are ours: the first locates the bytes for the
    # container pass, the second is already in `sections`, and §9.18
    # refuses an unknown key on write
    assert "href" not in f.as_record() and "caption" not in f.as_record()


def test_a_bad_record_line_is_refused_before_any_bytes(tmp_path):
    for bad in (0, 3, None, "two"):
        with pytest.raises(pack_records.ShardFormatError, match="record_line"):
            pack_records.write_shard(tmp_path / "s.jsonl.zst", [_record()],
                                     record_line=bad)
    assert not (tmp_path / "s.jsonl.zst").exists()


def test_a_shape_2_shard_round_trips_through_the_reader(tmp_path):
    plate = {"hash": "sha256:" + "ab" * 32, "media_type": "image/png",
             "bytes": 12, "seq": 0, "carried": True}
    path = tmp_path / "00001.jsonl.zst"
    pack_records.write_shard(
        path, [_record("gutenberg:1", [plate]), _record("gutenberg:2", [])],
        record_line=2)
    got = list(pack_records.iter_records(path))
    assert [r["figures"] for r in got] == [[plate], []]


def test_the_seq_alignment_survives_a_repeat():
    """`figures` lists APPEARANCES, so `figures[N]` is the Nth
    `[Illustration…]` marker. A plate used twice is two entries with one
    hash — deduplicating would re-aim every marker after the first
    repeat."""
    same = "sha256:" + "ab" * 32
    figures = [Figure(href="a.jpg", media_type="image/jpeg", sha256=same,
                      bytes=10, caption="", seq=i).as_record()
               for i in range(3)]
    line = json.loads(
        pack_records.canonical_line(_record(figures=figures), record_line=2))
    assert [f["seq"] for f in line["figures"]] == [0, 1, 2]
    assert len({f["hash"] for f in line["figures"]}) == 1
    # and the container stores it once
    assert len({fig_mod.bare(f["hash"]) for f in line["figures"]}) == 1
