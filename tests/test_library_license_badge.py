"""The Library's license badge ✓ means "nothing gates use": a green
collection (no acknowledgement needed) and an acknowledged yellow one both
get it, an unacknowledged yellow one does not. It used to follow
`ack_recorded` alone, so only pubmed — the one yellow collection — showed
a ✓ and every green one looked unchecked (operator, 2026-09-15)."""
import pytest

from app import collections as collections_mod, licensing, web


@pytest.fixture
def collection(isolated_config, tmp_path):
    return collections_mod.Collection(
        name="c", plugin="gutenberg", profile=None, embed_mode_default=None,
        corpus_dir=tmp_path / "corpus", manifest_path=tmp_path / "manifest.db",
        fts_dir=tmp_path / "fts", lance_dir=tmp_path / "lancedb")


@pytest.mark.parametrize("cls, state, ok", [
    ("green", licensing.NOT_REQUIRED, True),
    ("yellow", licensing.ACKNOWLEDGED, True),
    ("red", licensing.ACKNOWLEDGED, True),
    ("yellow", licensing.MISSING, False),
    ("unknown", licensing.NOT_REQUIRED, False),
])
def test_the_check_mark_means_nothing_gates_use(collection, monkeypatch, cls, state, ok):
    monkeypatch.setattr(licensing, "license_class_for", lambda c, spec=None: cls)
    monkeypatch.setattr(licensing, "state_for", lambda c, spec=None: state)
    shown = web._license_state(collection)
    assert shown["ok"] is ok
    assert shown["ack_recorded"] is False, "a green ✓ is not an acknowledgement"


def test_the_template_draws_the_check_mark_from_ok():
    from pathlib import Path
    library = (Path(web.__file__).parent / "templates" / "library.html").read_text(encoding="utf-8")
    assert "' ✓' if c.license.ok" in library
    assert "' ✓' if c.license.ack_recorded" not in library
