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


class _ListProc:
    """A finished `rsync --list-only`: the listing reads its stdout through
    communicate(), as the real Popen loop does."""
    returncode = 0

    def communicate(self, timeout=None):
        return _REAL_SHAPE, ""


def _spec():
    return AcquisitionSpec(
        source_id="g", transport=Transport.RSYNC,
        endpoints=(Endpoint(transport=Transport.RSYNC, url="host::mod"),),
        layout="one-dir-per-doc", checksums=ChecksumSource(kind="none"),
        delta=DeltaSpec(mode="full-resync"), profiles={"demo": Filter()},
        estimated_bytes={"demo": 1}, license_class=LicenseClass.GREEN)


def test_listing_is_recursive_and_parses_comma_grouped_sizes(monkeypatch):
    seen_argv = {}

    def fake_popen(argv, **kw):
        seen_argv["argv"] = argv
        return _ListProc()
    monkeypatch.setattr(subprocess, "Popen", fake_popen)

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


def test_every_rsync_invocation_bounds_its_own_idleness(monkeypatch, tmp_path):
    """A transfer that stalls SILENTLY is the failure that matters, and
    `_DEFAULT_TIMEOUT` does not catch it: that bounds a transfer's total
    duration, which a dead connection happily sits inside.

    Live on gutenberg 2026-09-12: ten rsync processes against
    ftp.ibiblio.org, all ESTABLISHED with empty queues both ways, every one
    frozen at exactly 786,432 bytes. The mirror had accepted the
    connection, sent 768 KiB and gone quiet — a fresh connection made by
    hand at the same moment pulled a 1.2 MB file instantly. Each dead
    session held a worker slot for the full 300 s, and the fetch ran at
    SIXTY FILES AN HOUR: 273 days for the corpus.

    rsync's own `--timeout` exits 30 after N seconds of no I/O, which the
    caller already treats as a retryable TransportError against the other
    mirror. It measures inactivity, not rate, so a slow-but-moving
    transfer is never killed.
    """
    from app.acquire import transport_rsync as tr
    seen = []

    class _Proc:
        returncode = 0

        def communicate(self, timeout=None):
            return "", ""

    def fake_popen(argv, **kw):
        seen.append(argv)
        return _Proc()

    monkeypatch.setattr(tr.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(tr.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(
                            a[0] if a else [], 0, _REAL_SHAPE, ""))

    from app.acquire.spec import Endpoint, RemoteFile, Transport
    endpoint = Endpoint(transport=Transport.RSYNC, url="host::mod")
    dest = tmp_path / "1" / "pg1.txt"
    dest.parent.mkdir(parents=True)
    (dest.parent / "pg1.txt.part").write_bytes(b"x")
    tr.RsyncTransport().fetch_one(endpoint, RemoteFile(rel_path="1/pg1.txt"), dest, 0)

    argv = seen[-1]
    assert any(a.startswith("--timeout=") for a in argv), argv
    assert any(a.startswith("--contimeout=") for a in argv), argv
    io = int(next(a for a in argv if a.startswith("--timeout=")).split("=")[1])
    assert 0 < io < tr._DEFAULT_TIMEOUT, \
        "an idleness bound looser than the total-duration bound catches nothing"

    # the LISTING too: one that stalls holds the whole pass, not one slot
    listed = []
    monkeypatch.setattr(tr.subprocess, "Popen",
                        lambda argv, **k: listed.append(argv) or _ListProc())
    list(tr.RsyncTransport().list_files(endpoint, _spec()))
    assert any(a.startswith("--timeout=") for a in listed[-1]), listed[-1]
    assert any(a.startswith("--contimeout=") for a in listed[-1]), listed[-1]
