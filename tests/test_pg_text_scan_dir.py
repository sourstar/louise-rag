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

    txt, is_stub = pg_text.scan_dir(d, 1)

    assert txt.name == "pg1.txt.utf8" and is_stub is False
    assert listings == [], "the canonical hit must not list the directory"


def test_fallback_and_emptiness_share_one_listing(tmp_path, monkeypatch):
    d = _book(tmp_path, "2", ["oddname.txt", "pg2.rdf"])
    listings = []
    real = Path.iterdir
    monkeypatch.setattr(Path, "iterdir",
                        lambda self: (listings.append(self), real(self))[1])

    txt, is_stub = pg_text.scan_dir(d, 2)

    assert txt.name == "oddname.txt" and is_stub is False
    assert len(listings) == 1, "one listing answers both questions"


def test_empty_directory_is_a_stub(tmp_path):
    d = _book(tmp_path, "3", [])
    assert pg_text.scan_dir(d, 3) == (None, True)


def test_files_but_no_text_is_not_a_stub(tmp_path):
    d = _book(tmp_path, "4", ["pg4.rdf"])
    txt, is_stub = pg_text.scan_dir(d, 4)
    assert txt is None and is_stub is False


def test_readme_is_still_ignored(tmp_path):
    d = _book(tmp_path, "5", ["README.txt"])
    txt, is_stub = pg_text.scan_dir(d, 5)
    assert txt is None and is_stub is False


def test_find_txt_keeps_its_contract(tmp_path):
    """fetch() still calls find_txt; it must behave exactly as before."""
    assert pg_text.find_txt(_book(tmp_path, "6", ["pg6.txt"]), 6).name == "pg6.txt"
    assert pg_text.find_txt(_book(tmp_path, "7", []), 7) is None
