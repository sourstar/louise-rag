"""app/acquire/transport_ftp.py — tested with a mocked ftplib.FTP (stdlib
ftplib has no lightweight way to spin up a real FTP server without a new
test dependency; mocking validates the transport's own listing-parse and
resume-offset logic, which is what's actually under test here).
"""
import ftplib
from unittest.mock import MagicMock, patch

import pytest

from app.acquire.spec import Endpoint, RemoteFile, Transport, TransportError
from app.acquire.transport_ftp import FtpTransport

_ENDPOINT = Endpoint(transport=Transport.FTP, url="ftp.example.com/pub")


def test_list_files_via_mlsd():
    fake_ftp = MagicMock()
    fake_ftp.mlsd.return_value = iter([
        ("a.txt", {"type": "file", "size": "5", "modify": "20240115102345"}),
        ("sub", {"type": "dir"}),
        ("b.txt", {"type": "file", "size": "7"}),
    ])
    with patch("app.acquire.transport_ftp._connect", return_value=fake_ftp):
        files = {f.rel_path: f for f in FtpTransport().list_files(_ENDPOINT, None)}
    assert files["a.txt"].size == 5
    assert files["a.txt"].mtime is not None
    assert "sub" not in files
    assert files["b.txt"].size == 7
    fake_ftp.quit.assert_called_once()


def test_list_files_falls_back_to_nlst_when_mlsd_unsupported():
    fake_ftp = MagicMock()
    fake_ftp.mlsd.side_effect = ftplib.error_perm("500 MLSD not understood")
    fake_ftp.nlst.return_value = ["a.txt", "b.txt"]
    with patch("app.acquire.transport_ftp._connect", return_value=fake_ftp):
        files = list(FtpTransport().list_files(_ENDPOINT, None))
    assert {f.rel_path for f in files} == {"a.txt", "b.txt"}


def test_fetch_one_resumes_with_rest_offset(tmp_path):
    dest = tmp_path / "a.txt"
    part = tmp_path / "a.txt.part"
    part.write_bytes(b"0123")   # 4 bytes already on disk

    fake_ftp = MagicMock()

    def _retrbinary(cmd, callback, rest=None):
        assert cmd == "RETR a.txt"
        assert rest == 4
        callback(b"4567")
    fake_ftp.retrbinary.side_effect = _retrbinary

    with patch("app.acquire.transport_ftp._connect", return_value=fake_ftp):
        remote = RemoteFile(rel_path="a.txt", size=8)
        size = FtpTransport().fetch_one(_ENDPOINT, remote, dest, resume_from=4)
    assert size == 8
    assert dest.read_bytes() == b"01234567"
    assert not part.exists()
    fake_ftp.quit.assert_called_once()


def test_fetch_one_fresh_download_no_resume(tmp_path):
    dest = tmp_path / "a.txt"
    fake_ftp = MagicMock()

    def _retrbinary(cmd, callback, rest=None):
        assert rest is None
        callback(b"hello")
    fake_ftp.retrbinary.side_effect = _retrbinary

    with patch("app.acquire.transport_ftp._connect", return_value=fake_ftp):
        remote = RemoteFile(rel_path="a.txt", size=5)
        size = FtpTransport().fetch_one(_ENDPOINT, remote, dest, resume_from=0)
    assert size == 5
    assert dest.read_bytes() == b"hello"


def test_fetch_one_wraps_ftp_errors(tmp_path):
    fake_ftp = MagicMock()
    fake_ftp.retrbinary.side_effect = ftplib.error_temp("421 timeout")
    with patch("app.acquire.transport_ftp._connect", return_value=fake_ftp):
        with pytest.raises(TransportError):
            FtpTransport().fetch_one(_ENDPOINT, RemoteFile(rel_path="a.txt"),
                                      tmp_path / "a.txt", resume_from=0)
