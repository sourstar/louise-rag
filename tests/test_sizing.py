"""Units sized by measured duration (app/sizing.py) — cost tests.

The defect being fixed is invisible at fixture scale and expensive at
corpus scale: `FTS_BATCH_DOCS=200` is 0.4 s of work for pubmed and 14-37 s
for gutenberg, so one setting buys two different things and neither was
chosen. A unit's duration is also the granularity at which a resource can
be handed back, so these numbers bound B2/B3 in docs/scheduling.md §10.
"""
import pytest

from app import config, sizing, worker


@pytest.fixture
def s():
    return sizing.Sizer()


# ------------------------------------------------------------------ batches

def test_the_configured_default_stands_until_something_is_measured(s):
    """It never guesses a size — it only corrects one that was observed."""
    assert s.size("fts:a", target_s=3.0, default=200) == 200
    s.record("fts:a", 200, 0.4)
    assert s.size("fts:a", target_s=3.0, default=200) == 200, "one sample is luck"


def test_a_cheap_collection_takes_more_and_a_dear_one_takes_fewer(s):
    """The same 3 s target on two corpora that differ by two orders of
    magnitude per document — which is what 'minimise switching overhead'
    has to mean when the documents are not comparable."""
    for _ in range(5):
        s.record("fts:pubmed", 200, 0.4)          # 2 ms per abstract
        s.record("fts:gutenberg", 200, 30.0)      # 150 ms per book
    cheap = s.size("fts:pubmed", target_s=3.0, default=200)
    dear = s.size("fts:gutenberg", target_s=3.0, default=200)
    assert cheap > 200 > dear
    assert 0.4 * cheap / 200 == pytest.approx(3.0, rel=0.3)
    assert 30.0 * dear / 200 == pytest.approx(3.0, rel=0.3)


def test_the_size_is_clamped_on_both_sides(s):
    """A measurement can be wrong; the clamp is what stops it being wrong
    expensively."""
    for _ in range(5):
        s.record("fts:tiny", 1000, 0.0001)        # absurdly fast
        s.record("fts:huge", 1, 3600.0)           # absurdly slow
    assert s.size("fts:tiny", target_s=3.0, default=200, hi=4000) == 4000
    assert s.size("fts:huge", target_s=3.0, default=200, lo=1) == 1


def test_it_follows_a_corpus_that_changes_shape(s):
    """Gutenberg's documents get bigger as the ids get higher: an average
    that never moves is as wrong as a constant."""
    for _ in range(20):
        s.record("fts:g", 100, 1.0)
    fast = s.size("fts:g", target_s=3.0, default=100)
    for _ in range(20):
        s.record("fts:g", 100, 10.0)
    assert s.size("fts:g", target_s=3.0, default=100) < fast / 2


def test_nonsense_input_is_ignored_not_recorded(s):
    for _ in range(5):
        s.record("k", 0, 5.0)
        s.record("k", 10, -1.0)
    assert s.size("k", target_s=3.0, default=42) == 42


# ------------------------------------------------------------------ windows

def test_an_expensive_window_shrinks_fast_and_a_cheap_one_grows_gently(s):
    """AIMD: the point of halving is to stop paying an expensive probe at
    once; the point of growing slowly is not to thrash straight back."""
    kw = dict(default=200_000, lo=1_000, hi=2_000_000)
    wide = s.note_window("w", seconds=0.5, target_s=0.1, **kw)      # 5x over
    assert wide == 100_000
    narrower = s.note_window("w", seconds=0.3, target_s=0.1, **kw)
    assert narrower == 50_000
    grown = s.note_window("w", seconds=0.01, target_s=0.1, **kw)    # cheap
    assert 50_000 < grown < 100_000, "grows by a quarter, not by doubling"


def test_a_window_converges_on_the_target_and_stays(s):
    """Feed it a cost proportional to the width and it settles, rather
    than oscillating between the clamps."""
    kw = dict(default=200_000, lo=1_000, hi=2_000_000)
    width = 200_000
    per_row = 0.1 / 40_000            # the target is hit at 40k rows
    for _ in range(40):
        width = s.note_window("w", seconds=width * per_row, target_s=0.1, **kw)
    assert 20_000 <= width <= 80_000, width


def test_the_window_never_goes_below_its_floor(s):
    """Below the floor the walk would never finish — a cursor that
    advances 100 rowids per round across 40M documents is a stall."""
    kw = dict(default=200_000, lo=1_000, hi=2_000_000)
    width = 200_000
    for _ in range(50):
        width = s.note_window("w", seconds=60.0, target_s=0.1, **kw)
    assert width == 1_000


# ------------------------------------------------------- the worker's wiring

def test_the_worker_keys_sizing_per_collection_and_stage(tmp_path):
    """Two collections can share a name and point at different corpora, so
    the key is the manifest PATH — the rule the re-index cursor learned the
    hard way."""
    from app import collections as collections_mod

    def made(name, where):
        return collections_mod.Collection(
            name=name, plugin="gutenberg", profile=None, embed_mode_default=None,
            corpus_dir=tmp_path, manifest_path=tmp_path / where / "manifest.db",
            fts_dir=tmp_path / "f", lance_dir=tmp_path / "l", embed=None)

    a, b = made("books", "one"), made("books", "two")
    assert worker._batch_key(a, "fts") != worker._batch_key(b, "fts")
    assert worker._batch_key(a, "fts") != worker._batch_key(a, "rdf")


def test_the_worker_falls_back_to_the_configured_counts(tmp_path, isolated_config):
    from app import collections as collections_mod
    c = collections_mod.Collection(
        name="c", plugin="gutenberg", profile=None, embed_mode_default=None,
        corpus_dir=tmp_path, manifest_path=tmp_path / "m.db",
        fts_dir=tmp_path / "f", lance_dir=tmp_path / "l", embed=None)
    assert worker.batch_docs(c, "fts", config.FTS_BATCH_DOCS) == config.FTS_BATCH_DOCS
    assert worker.batch_docs(c, "rdf", config.RDF_BATCH_DOCS) == config.RDF_BATCH_DOCS
