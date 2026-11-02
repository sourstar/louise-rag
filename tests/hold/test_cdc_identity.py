"""tools/measure/cdc_identity.py, the deploy gate of the compiled `cdc` loop
(docs/notes/python-hot-spots.md, "D"): a held collection's records cut both
ways, every boundary compared. Zero records, fewer than asked, or no
extension is never a success; one moved boundary is DIFFERENT, named.

The plumbing is tested with a stand-in for the extension (the Python loop
behind its interface), so it runs with the wheel or without; the real
extension, where it is installed."""
import json

import pytest

from app import chunking
from app.hold import records as rec
from app.hold import snapshot as sn
from tests.hold.test_carry import _create, quiet
from tools.measure import cdc_identity as ident

#: sizes that cut the fixture books' short sections several times each
CDC_LAYER = [{"chunker": {"id": "cdc", "version": 1,
                          "params": {"target": 60, "min": 20, "max": 90}},
              "rule": "default"}]


class StandIn:
    """The extension's interface over the Python loop; `shift` moves the
    first boundary of every answer, as a broken build would."""

    def __init__(self, shift: int = 0):
        self.shift = shift

    def boundaries(self, text, mn, target, mx):
        if not ident.answerable(text, (mn, target, mx)):
            return None
        got = list(chunking._cdc_boundaries(text, mn, target, mx))
        if self.shift and len(got) > 1:
            got[0] += self.shift
        return got


@pytest.fixture
def held(isolated_config, tmp_path, corpus_dir):
    root = tmp_path / "collections"
    _create(root, "g", over={"layers": CDC_LAYER})
    rec.build(root, "g", corpus_dir=corpus_dir, records_per_shard=2, log=quiet)
    rec.write_validation(root, "g")
    sn.create(root, "g", snapshot_id="2026-08", log=quiet)
    return root


def _run(capsys, root, *args):
    code = ident.main(["--collection", "g", "--root", str(root), "--jobs", "1", *args])
    out = capsys.readouterr().out
    return code, json.loads(out)


def test_records_cut_alike_are_identical(held, capsys, monkeypatch):
    monkeypatch.setattr(chunking, "_NATIVE", StandIn())
    code, out = _run(capsys, held, "--records", "3")
    assert (code, out["verdict"]) == (0, "IDENTICAL")
    assert out["compared"] == out["cdc_records"] == 3
    assert out["sections"] >= 3 and out["boundaries"] > out["sections"], "the books are cut, not whole"


def test_fewer_records_than_asked_is_not_a_proof(held, capsys, monkeypatch):
    monkeypatch.setattr(chunking, "_NATIVE", StandIn())
    code, out = _run(capsys, held, "--records", "4")
    assert (code, out["verdict"]) == (2, "INCOMPLETE")


def test_without_the_extension_nothing_is_proven(held, capsys, monkeypatch):
    monkeypatch.setattr(chunking, "_NATIVE", None)
    assert ident.main(["--collection", "g", "--root", str(held), "--jobs", "1"]) == 2
    assert "does not import" in capsys.readouterr().err


def test_one_moved_boundary_is_different_and_named(held, capsys, monkeypatch):
    monkeypatch.setattr(chunking, "_NATIVE", StandIn(shift=1))
    code, out = _run(capsys, held, "--records", "3")
    assert (code, out["verdict"]) == (1, "DIFFERENT")
    first = out["first_differing"][0]
    whys = {d["why"] for d in first["differs"]}
    assert whys == {"the boundaries differ", "the record's cut differs"}
    assert first["uid"].startswith("gutenberg:") and first["layer"] == "cdc"


def test_the_named_parts_are_read_instead(held, capsys, monkeypatch):
    monkeypatch.setattr(chunking, "_NATIVE", StandIn())
    parts = sorted(str(p) for p in (held / "g" / "shard").rglob("*.jsonl.zst"))
    assert parts
    code, out = _run(capsys, held, "--records", "1", *[a for p in parts[:1] for a in ("--part", p)])
    assert (code, out["verdict"], out["parts"]) == (0, "IDENTICAL", 1)


def test_the_extension_itself_on_held_records(held, capsys):
    if chunking._NATIVE is None:
        pytest.skip("louise_cdc is not installed")
    code, out = _run(capsys, held, "--records", "3")
    assert (code, out["verdict"]) == (0, "IDENTICAL")
    assert out["native_us_per_record"] and out["python_us_per_record"]
