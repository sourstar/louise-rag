"""The origin as its documents' files, over the network
(app/acquire/file_origin.py): what `records-vs-origin` re-fetches for a copy
that fetched the SHARD and holds no corpus (Perugia's gutenberg; operator,
2026-10-23). The end to end, through `hold check`'s wiring, is in
tests/hold/test_check.py."""
import dataclasses
import shutil
import signal
from pathlib import Path

import pytest

from app.acquire import file_origin, specs
from app.acquire.spec import Endpoint, RemoteFile, Transport, TransportError

pytestmark = pytest.mark.skipif(shutil.which("rsync") is None, reason="needs rsync")


def _tree(root: Path) -> Path:
    book = root / "1342"
    book.mkdir(parents=True)
    (book / "pg1342.txt").write_text("It is a truth universally acknowledged")
    (book / "pg1342.rdf").write_text("<rdf/>")
    (book / "pg1342.mobi").write_bytes(b"\0" * 100)       # not admitted
    return root


def _origin(tmp_path, *urls) -> file_origin.FileOrigin:
    spec = dataclasses.replace(specs.GUTENBERG_SPEC, endpoints=tuple(
        Endpoint(transport=Transport.RSYNC, url=str(u)) for u in urls))
    return file_origin.FileOrigin(spec, spec.filters)


def test_a_unit_is_its_document_s_directory_listed_on_the_first_mirror_that_has_it(tmp_path):
    mirror = _tree(tmp_path / "mirror")
    fo = _origin(tmp_path, tmp_path / "gone", mirror)
    assert fo.unit("1342/pg1342.txt") == ("1342", None), "unweighed until the bound reaches it"
    assert fo.weigh("1342") == len("It is a truth universally acknowledged") + len("<rdf/>")
    got = fo.materialise("1342/pg1342.txt")
    assert sorted(p.name for p in got.iterdir()) == ["pg1342.rdf", "pg1342.txt"]
    assert (got / "pg1342.txt").read_text().startswith("It is a truth")
    assert fo.bytes_read == fo.weigh("1342")
    fo.close()
    assert not fo.base.exists()


def test_an_origin_nobody_lists_weighs_nothing_and_is_not_found(tmp_path):
    fo = _origin(tmp_path, tmp_path / "gone", tmp_path / "also-gone")
    assert fo.weigh("1342") is None
    with pytest.raises(FileNotFoundError):
        fo.materialise("1342/pg1342.txt")


def test_a_mirror_that_lists_but_will_not_serve_falls_to_the_next(tmp_path):
    mirror = _tree(tmp_path / "mirror")
    fo = _origin(tmp_path, mirror, tmp_path / "second")
    _tree(tmp_path / "second")
    real = fo.transport.fetch_one
    calls = []

    def flaky(endpoint, remote, dest, resume_from):
        calls.append(endpoint.url)
        if endpoint.url.startswith(str(mirror)):
            raise TransportError("listed, then refused")
        return real(endpoint, remote, dest, resume_from)
    fo.transport = type("T", (), {"list_files": fo.transport.list_files, "fetch_one": staticmethod(flaky)})()
    got = fo.materialise("1342/pg1342.txt")
    assert (got / "pg1342.rdf").is_file()
    assert any(u.startswith(str(tmp_path / "second")) for u in calls)
    assert not (fo.base / ".staging" / "1342").exists(), "a unit appears whole or not at all"


def test_only_a_directory_per_document_over_a_listing_transport_gets_one(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(file_origin, "terminate_ends_the_transfer", lambda: None)
    assert isinstance(file_origin.for_codex(SimpleNamespace(plugin="gutenberg")), file_origin.FileOrigin)
    for plugin in ("pubmed", "wikipedia", "normattiva", "no-such-plugin"):
        assert file_origin.for_codex(SimpleNamespace(plugin=plugin)) is None, plugin


def test_a_stop_reaches_the_transfer_as_ctrl_c_where_nothing_else_is_bound():
    before = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGHUP)}
    try:
        for s in before:
            signal.signal(s, signal.SIG_DFL)
        file_origin.terminate_ends_the_transfer()
        assert signal.getsignal(signal.SIGTERM) is signal.default_int_handler
        bound = lambda *_a: None  # noqa: E731
        signal.signal(signal.SIGTERM, bound)
        file_origin.terminate_ends_the_transfer()
        assert signal.getsignal(signal.SIGTERM) is bound, "a handler someone set is kept"
    finally:
        for s, h in before.items():
            signal.signal(s, h)
