"""pg_text.scan_dir answers "where is the text" and "is this an empty stub"
in at most ONE directory listing.

The first version of the stub check called iterdir() before find_txt, which
made the COMMON case worse: a canonically-named book previously cost a
single stat and no listing at all, and suddenly paid a full listing — 79k
of them per cold scan, on a network-backed mount.
"""
from pathlib import Path

import pytest

from app.plugins.gutenberg import pg_text


def _book(tmp_path, name, files):
    d = tmp_path / name
    d.mkdir(parents=True, exist_ok=True)
    for f in files:
        (d / f).write_text("x", encoding="utf-8")
    return d


def test_canonical_name_costs_no_listing(tmp_path, monkeypatch):
    d = _book(tmp_path, "1", ["pg1.txt.utf8", "pg1.rdf"])
    listings = []
    real = Path.iterdir
    monkeypatch.setattr(Path, "iterdir",
                        lambda self: (listings.append(self), real(self))[1])

    txt, is_stub, _ = pg_text.scan_dir(d, 1)

    assert txt.name == "pg1.txt.utf8" and is_stub is False
    assert listings == [], "the canonical hit must not list the directory"


def test_fallback_and_emptiness_share_one_listing(tmp_path, monkeypatch):
    d = _book(tmp_path, "2", ["oddname.txt", "pg2.rdf"])
    listings = []
    real = Path.iterdir
    monkeypatch.setattr(Path, "iterdir",
                        lambda self: (listings.append(self), real(self))[1])

    txt, is_stub, _ = pg_text.scan_dir(d, 2)

    assert txt.name == "oddname.txt" and is_stub is False
    assert len(listings) == 1, "one listing answers both questions"


def test_empty_directory_is_a_stub(tmp_path):
    d = _book(tmp_path, "3", [])
    assert pg_text.scan_dir(d, 3) == (None, True, None)


def test_files_but_no_text_is_not_a_stub(tmp_path):
    d = _book(tmp_path, "4", ["pg4.rdf"])
    txt, is_stub, _ = pg_text.scan_dir(d, 4)
    assert txt is None and is_stub is False


def test_readme_is_still_ignored(tmp_path):
    d = _book(tmp_path, "5", ["README.txt"])
    txt, is_stub, _ = pg_text.scan_dir(d, 5)
    assert txt is None and is_stub is False


def test_find_txt_keeps_its_contract(tmp_path):
    """fetch() still calls find_txt; it must behave exactly as before."""
    assert pg_text.find_txt(_book(tmp_path, "6", ["pg6.txt"]), 6).name == "pg6.txt"
    assert pg_text.find_txt(_book(tmp_path, "7", []), 7) is None


def test_it_names_the_form_when_there_is_no_txt(tmp_path):
    """"the source has no usable content for them" was true of all 1,454 of
    gutenberg's unindexable titles and MISLEADING about 422 of them — 255
    carry an .epub, 159 a .mobi, 8 an .html. Those books are not
    unreadable; they are not readable AS .txt, which is this collection's
    origin form. The listing is already in hand, so naming the form costs
    nothing."""
    d = tmp_path / "6"
    d.mkdir()
    (d / "pg6.rdf").write_text("x")
    (d / "pg6-images.epub").write_bytes(b"PK")
    txt, is_stub, other = pg_text.scan_dir(d, 6)
    assert txt is None and is_stub is False
    assert other == "epub"


def test_a_title_with_no_text_at_all_still_says_so(tmp_path):
    """The other ~1,199: an .rdf and a cover, sometimes only a converter
    log. Book 1767's directory is a converter log and two qrcodes — that is
    Gutenberg's own build having failed in 2020, and no form recovers it."""
    d = tmp_path / "7"
    d.mkdir()
    (d / "pg7.rdf").write_text("x")
    (d / "pg7.qrcode.png").write_bytes(b"\x89PNG")
    txt, is_stub, other = pg_text.scan_dir(d, 7)
    assert txt is None and is_stub is False and other is None
