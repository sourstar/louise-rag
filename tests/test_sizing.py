"""Units sized by measured duration (app/sizing.py) — cost tests.

The defect being fixed is invisible at fixture scale and expensive at
corpus scale: `FTS_BATCH_DOCS=200` is 0.4 s of work for pubmed and 14-37 s
for gutenberg, so one setting buys two different things and neither was
chosen. A unit's duration is also the granularity at which a resource can
be handed back, so these numbers bound B2/B3 in docs/notes/scheduling.md §10.
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


def test_sizing_labels_name_the_collection_not_the_file(tmp_path):
    """Observed live, twice. Every collection's manifest is called
    `manifest.db`, so a label taken from the filename read
    `manifest.db: 250,000 rowids` five times over; taken from the parent
    directory it then read `data` for the default collection, whose
    manifest sits at the data root rather than in a directory named after
    it. The name is registered beside the key instead."""
    from app import collections as collections_mod

    def made(name, manifest):
        return collections_mod.Collection(
            name=name, plugin="gutenberg", profile=None, embed_mode_default=None,
            corpus_dir=tmp_path, manifest_path=manifest,
            fts_dir=tmp_path / "f", lance_dir=tmp_path / "l", embed=None)

    # the default collection: its manifest is NOT under a directory of its name
    default = made("gutenberg", tmp_path / "manifest.db")
    normal = made("pubmed", tmp_path / "pubmed" / "manifest.db")
    assert worker._sizing_label(worker._batch_key(default, "fts")) == "fts gutenberg"
    assert worker._sizing_label(worker._batch_key(normal, "rdf")) == "rdf pubmed"
    # and the path fallback still works for a key nobody registered
    assert worker._sizing_label("/data/wikivec/manifest.db") == "wikivec"


def test_the_worker_falls_back_to_the_configured_counts(tmp_path, isolated_config):
    from app import collections as collections_mod
    c = collections_mod.Collection(
        name="c", plugin="gutenberg", profile=None, embed_mode_default=None,
        corpus_dir=tmp_path, manifest_path=tmp_path / "m.db",
        fts_dir=tmp_path / "f", lance_dir=tmp_path / "l", embed=None)
    assert worker.batch_docs(c, "fts", config.FTS_BATCH_DOCS) == config.FTS_BATCH_DOCS
    assert worker.batch_docs(c, "rdf", config.RDF_BATCH_DOCS) == config.RDF_BATCH_DOCS


# ---------------------------------------------- the fixed cost of a batch

def _run(s, key, *, fixed, marginal, target, default, rounds=15, ceiling=12.0,
         lo=1, hi=4000):
    """Close the loop the worker closes: size a batch, pay what a batch of
    that size costs, record it, size again. Returns the sizes chosen."""
    sizes = []
    n = default
    for _ in range(rounds):
        s.record(key, n, fixed + marginal * n)
        n = s.size(key, target_s=target, default=default, lo=lo, hi=hi,
                   ceiling_s=ceiling)
        sizes.append(n)
    return sizes


def test_a_fixed_cost_per_batch_does_not_shrink_the_batch_until_it_is_all_fixed_cost(s):
    """THE DEFECT, measured on the node 2026-09-22: wikipedia's rdf batches
    cost 2.7 s before the first document (a tantivy commit, a writer, a
    transaction) plus 41 ms each, against a 3 s target. Charging all of that
    to a per-document rate reports `marginal + fixed/n`, so the next batch is
    smaller, so the rate looks worse, so it shrinks again — and it ended at
    36-234 documents where the thread could have taken thousands, paying the
    2.7 s as often as it could.

    Sized from both terms, the batch instead grows until the fixed cost is a
    minority of it, bounded by the ceiling that bounds the unit."""
    sizes = _run(s, "rdf:wikipedia", fixed=2.7, marginal=0.041, target=3.0,
                 default=200)
    settled = sizes[-1]
    assert settled > 200, f"it must grow past the default, got {sizes}"
    duration = 2.7 + 0.041 * settled
    assert duration <= 12.0 * 1.05, f"and stay inside the ceiling: {duration:.1f}s"
    assert 2.7 / duration < 0.5, "the fixed cost is now a minority of the batch"
    # and it does not oscillate: the last few rounds agree within a tenth
    assert max(sizes[-4:]) - min(sizes[-4:]) <= max(sizes[-4:]) * 0.1, sizes


def test_the_old_proportional_model_would_have_collapsed_here(s):
    """The same cost, sized the way it was before: this is the loop the fix
    breaks, pinned so nobody restores it by accident."""
    n, sizes = 200, []
    for _ in range(15):
        seconds = 2.7 + 0.041 * n
        per = seconds / n                      # the old model's whole view
        n = max(1, min(4000, int(3.0 / per)))
        sizes.append(n)
    assert sizes[-1] < 80 and sizes[-1] < sizes[0], \
        f"the proportional model shrinks toward the floor: {sizes}"
    assert 2.7 / (2.7 + 0.041 * sizes[-1]) > 0.4, \
        "and ends paying mostly fixed cost per document"


def test_a_stage_with_no_fixed_cost_still_lands_on_the_target(s):
    """The fix must not change the case it was already right about."""
    sizes = _run(s, "fts:pubmed", fixed=0.0, marginal=0.002, target=3.0,
                 default=200)
    duration = 0.002 * sizes[-1]
    assert duration == pytest.approx(3.0, rel=0.25), f"{sizes[-1]} documents"


def test_documents_that_cost_more_than_the_whole_target_are_taken_one_at_a_time(s):
    """gutenberg: 14-37 s per book. No arithmetic may talk it into a batch."""
    sizes = _run(s, "fts:gutenberg", fixed=1.0, marginal=20.0, target=3.0,
                 default=200)
    assert sizes[-1] == 1, sizes


def test_the_fit_reports_both_terms_and_refuses_to_invent_them(s):
    """Every batch the same size: the two terms cannot be told apart, and the
    sizer says so by falling back to the plain rate rather than inventing a
    split."""
    same = [(200, 3.0)] * 8
    assert sizing._affine(same) == (0.0, None)
    varied = [(100, 2.0), (200, 3.0), (400, 5.0)]        # 1.0 + 0.01n
    fixed, marginal = sizing._affine(varied)
    assert fixed == pytest.approx(1.0, abs=0.05)
    assert marginal == pytest.approx(0.01, rel=0.05)
    assert sizing._affine([(100, 5.0), (200, 1.0)])[1] is None, \
        "a fit that says a bigger batch is cheaper is noise, not a discovery"
    assert sizing._affine([(1, 1.0)]) == (0.0, None), "too few to fit"


def test_the_snapshot_shows_what_the_fit_believes(s):
    for u, sec in ((100, 2.0), (200, 3.0), (400, 5.0), (300, 4.0)):
        s.record("rdf:w", u, sec)
    shown = s.snapshot()["affine"]["rdf:w"]
    assert shown["fixed_s"] == pytest.approx(1.0, abs=0.1)
    assert shown["per_unit_s"] == pytest.approx(0.01, rel=0.1)
