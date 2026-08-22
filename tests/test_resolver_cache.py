"""The symbol index is memoised on the reference data's version stamp.

DictResolver.resolve rebuilt `{symbol: row}` from hgnc_gene on EVERY call —
so once per document, for every document of every corpus. Measured live:
45,040 rows loaded and hashed in 61.7 ms to do 0.029 ms of matching, a
2,149x waste and 64% of the whole fts stage.

Caching must not cost determinism (invariant 3). The key is the reference
content's own version — ref_meta's (ref_version, synced_at) per source, which
sync_source writes whenever a source is reprocessed — so resolve() remains a
pure function of the reference tables' current content.
"""
import pytest

from app.plugins.base import Entity
from app.refs import resolver as R


class FakeRefs:
    """Minimal RefLookup: counts how often the gene table is read."""

    def __init__(self, genes, meta=None):
        self._genes = genes
        self._meta = meta if meta is not None else [
            {"source_id": "hgnc", "ref_version": 1, "synced_at": 1000}]
        self.gene_reads = 0

    def rows(self, table, where=None):
        if table == "ref_meta":
            return list(self._meta)
        if table == "hgnc_gene":
            self.gene_reads += 1
            return list(self._genes)
        return []

    def get(self, table, pk_value):
        return None

    def links_for(self, table):
        return []

    def pk_of(self, table):
        return "id"

    def owner_of(self, table):
        return "hgnc"


GENES = [{"symbol": "BRCA2", "hgnc_id": "1101"}, {"symbol": "TP53", "hgnc_id": "11998"}]


@pytest.fixture(autouse=True)
def _clean():
    R.invalidate_cache()
    yield
    R.invalidate_cache()


def test_resolution_is_unchanged():
    refs = FakeRefs(GENES)
    got = R.DictResolver().resolve("BRCA2 and TP53 and NOTAGENE", refs)
    assert {e.entity_id for e in got} == {"hgnc:1101", "hgnc:11998"}
    assert all(isinstance(e, Entity) for e in got)


def test_gene_table_is_read_once_across_many_documents():
    refs = FakeRefs(GENES)
    res = R.DictResolver()
    for _ in range(50):
        res.resolve("BRCA2 appears here", refs)
    assert refs.gene_reads == 1, \
        f"symbol index rebuilt {refs.gene_reads} times for 50 documents"


def test_a_reference_sync_invalidates_the_cache():
    """The cache must not outlive the data it was built from."""
    refs = FakeRefs(GENES)
    res = R.DictResolver()
    assert {e.entity_id for e in res.resolve("BRCA2", refs)} == {"hgnc:1101"}

    # a sync adds a gene and bumps the stamp, exactly as sync_source does
    refs._genes = GENES + [{"symbol": "EGFR", "hgnc_id": "3236"}]
    refs._meta = [{"source_id": "hgnc", "ref_version": 1, "synced_at": 2000}]

    got = {e.entity_id for e in res.resolve("EGFR", refs)}
    assert got == {"hgnc:3236"}, "a stale cache survived a reference sync"


def test_an_unchanged_stamp_keeps_the_cache():
    """Complement of the above: identical stamp must NOT rebuild, or the
    memoisation is worthless."""
    refs = FakeRefs(GENES)
    res = R.DictResolver()
    res.resolve("BRCA2", refs)
    refs._genes = GENES + [{"symbol": "EGFR", "hgnc_id": "3236"}]  # no stamp bump
    assert res.resolve("EGFR", refs) == []
    assert refs.gene_reads == 1


def test_a_reflookup_without_ref_meta_still_resolves():
    """A hand-rolled double with no ref_meta gets the old uncached behaviour
    rather than a wrong answer."""

    class NoMeta(FakeRefs):
        def rows(self, table, where=None):
            if table == "ref_meta":
                raise RuntimeError("no such table")
            return super().rows(table, where)

    refs = NoMeta(GENES)
    got = R.DictResolver().resolve("TP53", refs)
    assert {e.entity_id for e in got} == {"hgnc:11998"}


def test_cache_is_shared_across_resolver_instances():
    """stage_fts builds a store per batch and pipeline threads run in
    parallel; the index must be process-wide, not per-object."""
    refs = FakeRefs(GENES)
    R.DictResolver().resolve("BRCA2", refs)
    R.DictResolver().resolve("TP53", refs)
    assert refs.gene_reads == 1
