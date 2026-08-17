"""app/acquire/transport_hf_hub.py — URL construction, delegating the
actual fetch to HttpsTransport (no huggingface_hub dependency)."""
from app.acquire.spec import Endpoint, RemoteFile, Transport
from app.acquire.transport_hf_hub import HfHubTransport


def test_fetch_one_builds_resolve_url_default_revision(tmp_path, monkeypatch):
    transport = HfHubTransport()
    captured = {}

    def fake_fetch_one(endpoint, remote, dest, resume_from):
        captured["url"] = endpoint.url
        return 4
    monkeypatch.setattr(transport._https, "fetch_one", fake_fetch_one)

    endpoint = Endpoint(transport=Transport.HF_HUB,
                         url="datasets/Upstash/wikipedia-2024-06-bge-m3")
    size = transport.fetch_one(endpoint, RemoteFile(rel_path="file.parquet"),
                                tmp_path / "file.parquet", resume_from=0)
    assert size == 4
    assert captured["url"] == \
        "https://huggingface.co/datasets/Upstash/wikipedia-2024-06-bge-m3/resolve/main"


def test_fetch_one_respects_explicit_revision(tmp_path, monkeypatch):
    transport = HfHubTransport()
    captured = {}

    def fake_fetch_one(endpoint, remote, dest, resume_from):
        captured["url"] = endpoint.url
        return 0
    monkeypatch.setattr(transport._https, "fetch_one", fake_fetch_one)

    endpoint = Endpoint(transport=Transport.HF_HUB, url="datasets/foo/bar@v2")
    transport.fetch_one(endpoint, RemoteFile(rel_path="f"), tmp_path / "f", 0)
    assert captured["url"] == "https://huggingface.co/datasets/foo/bar/resolve/v2"


def test_list_files_returns_static_files():
    transport = HfHubTransport()
    files = (RemoteFile(rel_path="a"), RemoteFile(rel_path="b"))
    endpoint = Endpoint(transport=Transport.HF_HUB, url="datasets/x/y", static_files=files)
    assert list(transport.list_files(endpoint, None)) == list(files)
