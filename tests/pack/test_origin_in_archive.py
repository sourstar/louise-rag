"""06 §6.4 (main 8068727): a record whose text a credited archive supplies
is located IN that archive — its `origin` a URL at the registry's pinned
revision — and a record located in an archive the Codex does not credit
aborts the build."""
from types import SimpleNamespace

import pytest

from app.bundles.genesis import UPSTASH_WIKIPEDIA as UP
from app.pack import build

URL = f"{UP.base_url}/resolve/{UP.revision}/data/en/000.parquet#2::3"
STRADDLER = URL + "|" + URL.replace("000.parquet#2::3", "001.parquet#0::1")


def _collection(*archives):
    return SimpleNamespace(credits=[{"kind": "archive", "archive": a} for a in archives]
                           + [{"kind": "corpus", "archive": None}])


def test_a_record_in_a_credited_archive_builds():
    build._refuse_uncredited_origin(_collection(UP.archive_id), {"uid": "w:1", "origin": URL})
    build._refuse_uncredited_origin(_collection(UP.archive_id),
                                    {"uid": "w:2", "origin": STRADDLER})


def test_a_record_in_an_uncredited_archive_aborts_the_build():
    for credits in (_collection("medcpt-pubmed"), _collection(), SimpleNamespace()):
        with pytest.raises(build.BuildError, match="does not credit"):
            build._refuse_uncredited_origin(credits, {"uid": "w:1", "origin": STRADDLER})


def test_a_record_located_by_the_acquisition_spec_is_not_the_archives():
    """A dump page is relative to `origin.spec`'s base, and a URL under no
    registered archive is the origin's own: neither is refused."""
    for origin in ("enwiki/20260801/enwiki-20260801-pages-articles-multistream.xml.bz2#0::0",
                   "https://example.org/some/page"):
        build._refuse_uncredited_origin(_collection(), {"uid": "w:1", "origin": origin})
