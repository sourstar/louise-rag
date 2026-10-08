"""A collection can pin the DATED snapshot of its origin that it holds.

Wikipedia was fetched from `-latest-` URLs, which is the anti-pattern
underneath several separate problems: the same URL serves different bytes
every few weeks, so the corpus cannot be torrented (an infohash describes
bytes, and these move), cannot be adopted (no stable size), cannot be
compared with a peer's, and every refresh is a 22 GB re-download because
"changed" is all the delta gate can see. Worst of all, the collection
cannot answer "which dump am I?", which is the first question both a delta
chain and any freshness feature have to ask.

Pinning is opt-in: an unpinned collection behaves exactly as before, since
switching a live 27 GB corpus to new paths would orphan every file.
"""
import pytest

from app import collections as collections_mod
from app.acquire import engine, specs
from app.acquire.specs import WIKIPEDIA_SPEC
from tests.conftest import codex_collection


def test_dated_paths_are_immutable_and_latest_is_not():
    dated = specs._wiki_files("enwiki", "20260801")
    latest = specs._wiki_files("enwiki")

    assert [f.rel_path for f in dated] == [
        "enwiki/20260801/enwiki-20260801-pages-articles-multistream.xml.bz2",
        "enwiki/20260801/enwiki-20260801-pages-articles-multistream-index.txt.bz2"]
    assert all("latest" in f.rel_path for f in latest), \
        "the unpinned default is unchanged — a live corpus must not be orphaned"
    # the two never collide on disk, so a pinned and an unpinned copy can
    # coexist during a migration
    assert not ({f.rel_path for f in dated} & {f.rel_path for f in latest})


def test_two_snapshots_do_not_collide():
    a = {f.rel_path for f in WIKIPEDIA_SPEC.snapshot_files("20260701")}
    b = {f.rel_path for f in WIKIPEDIA_SPEC.snapshot_files("20260801")}
    assert not (a & b), "each snapshot is a separate, complete corpus"
    assert len(a) == len(b) == 2 * len(specs.WIKIS)


def test_the_spec_declares_dated_dumps():
    assert WIKIPEDIA_SPEC.delta.mode == "dated-dumps"
    assert WIKIPEDIA_SPEC.snapshot_files is not None
    assert WIKIPEDIA_SPEC.snapshot_index is not None


def test_a_pinned_collection_lists_its_snapshot(isolated_config, tmp_path):
    """The behaviour that matters: acquisition asks for the pinned dump, not
    for whatever the origin currently calls latest."""
    pinned = engine._list_and_filter(WIKIPEDIA_SPEC, "all", "20260801")
    assert pinned, "a pinned collection must have files to fetch"
    assert all("20260801" in f.rel_path for f in pinned), \
        [f.rel_path for f in pinned]

    unpinned = engine._list_and_filter(WIKIPEDIA_SPEC, "all", None)
    assert all("latest" in f.rel_path for f in unpinned), \
        [f.rel_path for f in unpinned]


def test_the_profile_filter_still_applies_to_a_snapshot(isolated_config):
    """Pinning selects WHICH dump; the profile still selects which wiki."""
    demo = engine._list_and_filter(WIKIPEDIA_SPEC, "demo", "20260801")
    assert demo and all(f.rel_path.startswith("trwiki/") for f in demo)
    assert all("20260801" in f.rel_path for f in demo)


def test_a_source_without_snapshots_ignores_the_pin(isolated_config):
    """A source whose files are immutable publishes no dated dumps (genome's
    assemblies were the worked case; boxed in attic/); a pin on such a
    collection must be inert rather than an error."""
    from app.acquire.spec import (AcquisitionSpec, ChecksumSource, DeltaSpec,
                                   Endpoint, Filter, RemoteFile, Transport)
    from app.plugins.base import LicenseClass
    spec = AcquisitionSpec(
        source_id="x", transport=Transport.HTTPS,
        endpoints=(Endpoint(transport=Transport.HTTPS, url="https://x.invalid",
                            static_files=(RemoteFile(rel_path="a/a.bin", size=1),
                                          RemoteFile(rel_path="b/b.bin", size=1))),),
        layout="container-files", checksums=ChecksumSource(kind="none"),
        delta=DeltaSpec(mode="full-resync"),
        profiles={"a": Filter(include=("a/*",)), "b": Filter(include=("b/*",))},
        profile_mode="exclusive", license_class=LicenseClass.GREEN)
    assert spec.snapshot_files is None
    files = engine._list_and_filter(spec, "a", "20260801")
    assert [f.rel_path for f in files] == ["a/a.bin"]


# ---------------- the TOML field ----------------

def _pinned(data_dir, config_dir, name: str, toml_extra: str = ""):
    """A Codex collection (the test gutenberg, whose Codex pins no dump) and
    the file's `snapshot` line, if any."""
    codex_collection(name, louise_root=data_dir, config_dir=config_dir, toml_extra=toml_extra)
    return collections_mod.by_name(name)


def test_the_pin_is_tier_two_desired_state(isolated_config, data_dir, config_dir):
    assert _pinned(data_dir, config_dir, "wp", 'snapshot = "20260801"\n').snapshot == "20260801"


def test_an_unpinned_collection_reports_none(isolated_config, data_dir, config_dir):
    assert _pinned(data_dir, config_dir, "wp2").snapshot is None


def test_a_numeric_snapshot_survives_toml_typing(isolated_config, data_dir, config_dir):
    """`snapshot = 20260801` without quotes parses as an int; a date that
    silently became a number would build a nonsense URL."""
    assert _pinned(data_dir, config_dir, "wp3", "snapshot = 20260801\n").snapshot == "20260801"


# ---------------- listing what the origin offers ----------------

def test_snapshot_index_parses_dates_newest_first(monkeypatch):
    """Parsed from the directory index because there is no listing API."""
    html = ('<html><body>'
            '<a href="../">../</a>'
            '<a href="20260601/">20260601/</a>'
            '<a href="20260801/">20260801/</a>'
            '<a href="20260701/">20260701/</a>'
            '<a href="latest/">latest/</a>'
            '</body></html>')

    class _Response:
        text = html
        def raise_for_status(self): return None

    class _Client:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url, headers=None):
            assert "louise-rag" in headers["User-Agent"], \
                "Wikimedia 403s generic library user agents"
            return _Response()

    import httpx
    monkeypatch.setattr(httpx, "Client", _Client)
    assert specs.wikipedia_snapshots("enwiki") == ("20260801", "20260701", "20260601")


def test_only_eight_digit_names_are_accepted(monkeypatch):
    """No amount of unexpected HTML may turn into a fetch URL."""
    class _Response:
        text = ('<a href="latest/">x</a><a href="../../etc/">y</a>'
                '<a href="2026080/">short</a><a href="20260801/">ok</a>')
        def raise_for_status(self): return None

    class _Client:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, *a, **k): return _Response()

    import httpx
    monkeypatch.setattr(httpx, "Client", _Client)
    assert specs.wikipedia_snapshots("enwiki") == ("20260801",)
