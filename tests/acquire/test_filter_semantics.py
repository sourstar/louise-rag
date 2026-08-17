"""engine._apply_filters — positive patterns are alternatives, negatives
veto. Regression for the live PubMed zero-file fetch: two positive patterns
("*.xml.gz", "*.xml.gz.md5") were AND-ed and excluded everything.
"""
from app.acquire.engine import _apply_filters
from app.acquire.spec import Filter, RemoteFile


def _files(*names_sizes):
    return [RemoteFile(rel_path=n, size=s) for n, s in names_sizes]


def test_multiple_positive_patterns_are_alternatives_not_conjunction():
    files = _files(("pubmed26n0001.xml.gz", 9_000_000),
                    ("pubmed26n0001.xml.gz.md5", 60),
                    ("README.txt", 100))
    out = _apply_filters(files, ("*.xml.gz", "*.xml.gz.md5"), Filter())
    assert [f.rel_path for f in out] == ["pubmed26n0001.xml.gz",
                                          "pubmed26n0001.xml.gz.md5"]


def test_negative_patterns_still_veto():
    files = _files(("a.txt", 1), ("a.mobi", 1))
    out = _apply_filters(files, ("*", "!*.mobi"), Filter())
    assert [f.rel_path for f in out] == ["a.txt"]


def test_caps_apply_over_a_deterministic_sorted_order():
    # FTP listing order is arbitrary — hand the files over shuffled and the
    # demo caps must still take the FIRST pair, data file plus its sidecar
    files = _files(("pubmed26n0002.xml.gz", 16_000_000),
                    ("pubmed26n0001.xml.gz.md5", 60),
                    ("pubmed26n0002.xml.gz.md5", 60),
                    ("pubmed26n0001.xml.gz", 9_000_000))
    out = _apply_filters(files, ("*.xml.gz", "*.xml.gz.md5"),
                          Filter(max_files=2, max_bytes=40_000_000))
    assert [f.rel_path for f in out] == ["pubmed26n0001.xml.gz",
                                          "pubmed26n0001.xml.gz.md5"]


def test_real_pubmed_demo_profile_admits_a_real_sized_file():
    from app.acquire.specs import PUBMED_SPEC
    files = _files(("pubmed26n0001.xml.gz", 30_000_000),
                    ("pubmed26n0001.xml.gz.md5", 60))
    out = _apply_filters(files, PUBMED_SPEC.filters,
                          PUBMED_SPEC.profiles["demo"])
    assert len(out) == 2, "a 30MB baseline file must fit the demo budget"
