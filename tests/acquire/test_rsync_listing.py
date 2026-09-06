"""The rsync listing parser against real rsync output shapes.

Two live incidents live here: the listing ran WITHOUT -r (a
one-dir-per-doc module listed as 79k directories and zero files), and the
size regex matched only plain digits while rsync ≥3.1 prints comma-grouped
sizes ("2,417,305") — so every file of 1,000 bytes or more was silently
dropped: a complete 2.8M-entry gutenberg listing filtered down to the five
files that happened to be under 1 KB.
"""
import subprocess

from app.acquire.spec import AcquisitionSpec, ChecksumSource, DeltaSpec, Endpoint, Filter, Transport
from app.acquire.transport_rsync import RsyncTransport
from app.plugins.base import LicenseClass

_REAL_SHAPE = """\
drwxrwsr-x             23 2025/09/02 14:00:09 .
-rw-rw-r--            209 2020/01/01 10:00:00 README
drwxrwsr-x          4,096 2025/09/02 14:00:09 1
-rw-rw-r--         18,114 2026/08/01 09:30:11 1/pg1.rdf
-rw-rw-r--      2,417,305 2026/08/01 09:30:11 1/pg1.txt.utf8
-rw-rw-r--             47 2020/01/01 10:00:00 10553/pg10553.txt.utf8.gzip
"""


def _spec():
    return AcquisitionSpec(
        source_id="g", transport=Transport.RSYNC,
        endpoints=(Endpoint(transport=Transport.RSYNC, url="host::mod"),),
        layout="one-dir-per-doc", checksums=ChecksumSource(kind="none"),
        delta=DeltaSpec(mode="full-resync"), profiles={"demo": Filter()},
        estimated_bytes={"demo": 1}, license_class=LicenseClass.GREEN)


def test_listing_is_recursive_and_parses_comma_grouped_sizes(monkeypatch):
    seen_argv = {}

    def fake_run(argv, **kw):
        seen_argv["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, stdout=_REAL_SHAPE, stderr="")
    monkeypatch.setattr(subprocess, "run", fake_run)

    files = list(RsyncTransport().list_files(_spec().endpoints[0], _spec()))
    assert "-r" in seen_argv["argv"], \
        "without -r a one-dir-per-doc module lists as directories and zero files"
    by_path = {f.rel_path: f for f in files}
    assert set(by_path) == {"README", "1/pg1.rdf", "1/pg1.txt.utf8",
                            "10553/pg10553.txt.utf8.gzip"}, by_path
    assert by_path["1/pg1.txt.utf8"].size == 2_417_305, \
        "comma-grouped sizes must parse — files over 1 KB were silently dropped"
    assert by_path["1/pg1.rdf"].size == 18_114
    assert by_path["README"].size == 209
