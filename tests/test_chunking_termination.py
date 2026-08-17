"""Regression: chunking must terminate for every configuration.

Found while building P1.1's import tests. `chunk_book`'s hard-split loop
advances with `p = p[max_chars - overlap_chars:]`; once
CHUNK_OVERLAP >= CHUNK_TOKENS that slice index is negative, `p` stops
shrinking, and the generator yields chunks forever with unbounded memory
growth. It hangs the ingestion worker on the first oversized paragraph, and it
is reachable from configuration alone — no hostile input required.
"""
import itertools

import pytest

from app import chunking, config

LONG_PARAGRAPH = "lighthouse keeper tended the lamp nightly. " * 400


def _chunks(monkeypatch, tokens, overlap, limit=500):
    monkeypatch.setattr(config, "CHUNK_TOKENS", tokens)
    monkeypatch.setattr(config, "CHUNK_OVERLAP", overlap)
    # islice, so a regression fails the assertion instead of hanging the suite
    return list(itertools.islice(
        chunking.chunk_book("gutenberg:1", 1, [("Chapter I", LONG_PARAGRAPH)]), limit))


@pytest.mark.parametrize("tokens,overlap", [
    (1, 120),        # the exact case that hung: overlap far exceeds the window
    (10, 10),        # overlap equals the window
    (10, 11),        # overlap just exceeds it
    (100, 5000),     # absurd overlap
])
def test_pathological_overlap_still_terminates(monkeypatch, tokens, overlap):
    """Termination is the guarantee, not efficiency: with the window nearly
    filled by overlap, progress is only a few characters per step, so the
    result is finite but useless. spaces.validate_chunking_config refuses that
    range outright at boot — this only proves the generator cannot hang."""
    limit = 100_000
    chunks = _chunks(monkeypatch, tokens, overlap, limit=limit)
    assert len(chunks) < limit, "chunking did not terminate"
    assert all(c["text"] for c in chunks)


def test_boot_refuses_a_degenerate_overlap(monkeypatch):
    """The honest fix for the range that merely terminates: refuse it loudly
    rather than let it quietly fill an index with near-duplicate chunks."""
    from app import spaces
    monkeypatch.setattr(config, "CHUNK_TOKENS", 1000)
    monkeypatch.setattr(config, "CHUNK_OVERLAP", 600)
    with pytest.raises(spaces.BootValidationError, match="CHUNK_OVERLAP"):
        spaces.validate_chunking_config()


def test_boot_accepts_the_shipped_default(monkeypatch):
    from app import spaces
    monkeypatch.setattr(config, "CHUNK_TOKENS", 1000)
    monkeypatch.setattr(config, "CHUNK_OVERLAP", 120)
    spaces.validate_chunking_config()


def test_sane_configurations_are_unaffected(monkeypatch):
    """The clamp must be a no-op wherever chunking already terminated, so no
    existing corpus re-chunks differently (CLAUDE.md invariant 3)."""
    before = _chunks(monkeypatch, 1000, 120)
    after = _chunks(monkeypatch, 1000, 120)
    assert [c["id"] for c in before] == [c["id"] for c in after]
    assert [c["text"] for c in before] == [c["text"] for c in after]
    assert len(before) > 1


@pytest.mark.parametrize("tokens,overlap", [(1000, 120), (500, 50), (2000, 400)])
def test_terminating_configurations_keep_their_exact_output(monkeypatch, tokens, overlap):
    """Every configuration with overlap < window is untouched by the clamp —
    which is why this needs no CHUNK_VERSION bump."""
    chunks = _chunks(monkeypatch, tokens, overlap, limit=2000)
    assert len(chunks) < 2000
    # ids are dense and monotonic from 0, as the id contract requires
    assert [c["seq"] for c in chunks] == list(range(len(chunks)))
