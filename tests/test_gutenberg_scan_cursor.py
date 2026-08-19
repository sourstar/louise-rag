"""Gutenberg's scan cursor — the last plugin that re-examined its whole
corpus on every scan.

79k book directories cost ~5 minutes on an idle pool and 25+ minutes once
three other collections were saturating the same spindles (~150 IOPS,
measured live). Every other plugin already skipped unchanged containers;
this brings the book directories in line.
"""
import os
import time

import pytest

from app.plugins import cursor
from app.plugins.base import DiscoverState
from app.plugins.gutenberg.source import GutenbergSource


def _book(root, book_id, files):
    d = root / str(book_id)
    d.mkdir(parents=True, exist_ok=True)
    for name, body in files.items():
        (d / name).write_text(body, encoding="utf-8")
    return d


def _settle(root):
    """Backdate the corpus. A real corpus is written long before it is
    scanned; a test writes it microseconds before, and mark_if_settled
    correctly refuses to trust a directory that changed during the scan
    (see test_a_directory_touched_during_the_scan_is_not_marked)."""
    past = time.time() - 60
    for p in (root, *root.iterdir()):
        os.utime(p, (past, past))


@pytest.fixture
def corpus(tmp_path):
    root = tmp_path / "corpus"
    _book(root, 1, {"pg1.txt": "one"})
    _book(root, 2, {"pg2.txt": "two"})
    _book(root, 3, {})                      # empty stub
    _settle(root)
    return root


def _discover(corpus, state_dir):
    return list(GutenbergSource().discover(
        DiscoverState(corpus_root=corpus, state_dir=state_dir)))


def test_unchanged_books_are_skipped_on_the_second_scan(corpus, tmp_path):
    first = _discover(corpus, tmp_path)
    assert {r.native_id for r in first if r.scope == "document"} == {"1", "2"}

    second = _discover(corpus, tmp_path)
    assert second == [], "an unchanged corpus must cost a stat pass, not a rescan"


def test_a_changed_book_comes_back(corpus, tmp_path):
    _discover(corpus, tmp_path)
    # a NEW file in the directory (how acquisition lands bytes: staged,
    # then renamed into place, which changes the directory's mtime)
    (corpus / "2" / "pg2.txt.utf8").write_text("two, revised", encoding="utf-8")

    again = _discover(corpus, tmp_path)
    assert [r.native_id for r in again if r.scope == "document"] == ["2"]


def test_a_new_book_is_found(corpus, tmp_path):
    _discover(corpus, tmp_path)
    _book(corpus, 9, {"pg9.txt": "nine"})
    again = _discover(corpus, tmp_path)
    assert [r.native_id for r in again if r.scope == "document"] == ["9"]


def test_stubs_are_remembered_too(corpus, tmp_path):
    """An empty directory examined once must not be re-examined every scan
    — that was 21,000 pointless listings per pass on the live node."""
    first = _discover(corpus, tmp_path)
    assert any(r.scope == "container" and "1 empty" in r.error for r in first)

    second = _discover(corpus, tmp_path)
    assert second == []

    # …until something lands in it
    (corpus / "3" / "pg3.txt").write_text("three", encoding="utf-8")
    third = _discover(corpus, tmp_path)
    assert [r.native_id for r in third if r.scope == "document"] == ["3"]


def test_clearing_the_cursor_forces_a_full_rescan(corpus, tmp_path):
    _discover(corpus, tmp_path)
    assert _discover(corpus, tmp_path) == []

    assert cursor.clear(tmp_path, "gutenberg") is True
    again = _discover(corpus, tmp_path)
    assert {r.native_id for r in again if r.scope == "document"} == {"1", "2"}
    # clearing a cursor that is not there is not an error
    assert cursor.clear(tmp_path / "nowhere", "gutenberg") is False


def test_no_state_dir_means_no_cursor(corpus):
    """Ad-hoc callers (tests, one-off tooling) still get a full scan."""
    assert len(_discover(corpus, None)) == len(_discover(corpus, None)) == 3


def test_mark_if_settled_refuses_what_changed_during_the_scan(tmp_path):
    """The timestamp race: if a book lands while the scan is running,
    marking would store a signature that ALREADY includes the change, and
    the next scan would see "unchanged" and skip it — permanently.
    Refusing to mark costs one re-examination and cannot lose an update."""
    book = tmp_path / "42"
    book.mkdir()
    started_ns = time.time_ns()
    # stamped explicitly rather than by writing a file: directory mtime
    # granularity is coarse (measured in-container: a file created 5 ms
    # after mkdir left the directory's mtime untouched), so the comparison
    # is what this test is about, not the filesystem's clock
    after = (started_ns + 5_000_000_000) / 1e9
    os.utime(book, (after, after))

    saved: dict = {}
    assert cursor.mark_if_settled(saved, book, started_ns) is False
    assert saved == {}, "a directory written during the scan is not trusted"

    past = time.time() - 60
    os.utime(book, (past, past))
    assert cursor.mark_if_settled(saved, book, started_ns) is True
    assert "42" in saved, "one settled before the scan is remembered"


def test_every_plugin_uses_the_settle_check():
    """Structural, because the failure is silent: a plugin that calls the
    plain mark() stores signatures for things that changed mid-scan and
    then skips them forever. All five container/directory plugins must go
    through mark_if_settled."""
    from pathlib import Path as P
    root = P(__file__).resolve().parent.parent / "app" / "plugins"
    offenders = [p.relative_to(root) for p in root.rglob("source.py")
                 if "cursor.mark(" in p.read_text(encoding="utf-8")]
    assert offenders == [], f"marks without the settle check: {offenders}"


def test_a_book_landing_after_the_scan_is_found_next_time(corpus, tmp_path):
    _discover(corpus, tmp_path)
    _book(corpus, 4, {"pg4.txt": "four"})
    again = [r.native_id for r in _discover(corpus, tmp_path)
             if r.scope == "document"]
    assert again == ["4"]
