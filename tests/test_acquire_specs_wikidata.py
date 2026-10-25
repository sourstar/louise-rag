"""tests/test_acquire_specs_wikidata.py — the Wikidata acquisition spec
(docs/notes/wikidata-build-plan.md D1): dated dumps only, the full JSON
dump with the run's two checksum files, two connections; the snapshot
index keeps the dated directories that hold a full dump (not every one
does)."""
from app.acquire import spec as acquire_spec, specs
from app.acquire.specs import WIKIDATA_SPEC


def test_the_spec_names_the_dated_dump_and_its_checksum_files():
    files = WIKIDATA_SPEC.snapshot_files("20261012")
    assert [f.rel_path for f in files] == [
        "wikidatawiki/entities/20261012/wikidata-20261012-all.json.bz2",
        "wikidatawiki/entities/20261012/wikidata-20261012-md5sums.txt",
        "wikidatawiki/entities/20261012/wikidata-20261012-sha1sums.txt"]
    assert WIKIDATA_SPEC.endpoints[0].max_conns == 2 and WIKIDATA_SPEC.endpoints[0].static_files == ()
    assert WIKIDATA_SPEC.delta.mode == "dated-dumps" and WIKIDATA_SPEC.layout == "single-file"
    assert acquire_spec.resolve_profile(WIKIDATA_SPEC, "all") == acquire_spec.Filter()
    assert acquire_spec.everything_profile(WIKIDATA_SPEC) == "all"
    assert WIKIDATA_SPEC.estimated_bytes["all"] > 100_000_000_000
    assert WIKIDATA_SPEC.license_class.value == "green" and WIKIDATA_SPEC.terms_text_path is None
    assert specs.ACQUIRE_REGISTRY["wikidata"] is WIKIDATA_SPEC


def test_the_snapshot_index_keeps_only_directories_with_a_full_dump(monkeypatch):
    """München's listing of 2026-10-19: 20261016 held lexemes only, 20261014
    lexemes and truthy RDF, 20261012 the full dump."""
    pages = {
        "https://dumps.wikimedia.org/wikidatawiki/entities/":
            '<a href="20261007/">20261007/</a> <a href="20261009/">20261009/</a> <a href="20261012/">20261012/</a> '
            '<a href="20261014/">20261014/</a> <a href="20261016/">20261016/</a> <a href="latest/">latest/</a>',
        "https://dumps.wikimedia.org/wikidatawiki/entities/20261016/": 'wikidata-20261016-lexemes-BETA.nt.bz2',
        "https://dumps.wikimedia.org/wikidatawiki/entities/20261014/": 'wikidata-20261014-lexemes.json.bz2 wikidata-20261014-truthy-BETA.nt.bz2',
        "https://dumps.wikimedia.org/wikidatawiki/entities/20261012/": 'wikidata-20261012-all.json.bz2 wikidata-20261012-all.json.gz wikidata-20261012-md5sums.txt',
        "https://dumps.wikimedia.org/wikidatawiki/entities/20261009/": 'wikidata-20261009-lexemes.json.bz2',
        "https://dumps.wikimedia.org/wikidatawiki/entities/20261007/": 'wikidata-20261007-all.json.bz2',
    }

    class R:
        def __init__(self, text, code=200):
            self.text, self.status_code = text, code
            self.content = text.encode()

        def raise_for_status(self):
            pass

    class Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def get(self, url, headers=None):
            return R(pages[url]) if url in pages else R("", 404)
    import httpx

    from app.acquire import budget
    monkeypatch.setattr(httpx, "Client", Client)
    drawn = []
    monkeypatch.setattr(budget, "acquire_bytes", drawn.append)
    assert specs.wikidata_snapshots() == ("20261012", "20261007")
    # invariant 5: the top listing and each of the five dated listings, drawn
    assert len(drawn) == len(pages) and sum(drawn) == sum(len(t.encode()) for t in pages.values())
    assert WIKIDATA_SPEC.snapshot_index is specs.wikidata_snapshots
