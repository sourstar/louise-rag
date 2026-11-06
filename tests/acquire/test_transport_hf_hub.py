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


def test_list_files_walks_the_tree_api_with_pagination(monkeypatch):
    """No static_files → the transport lists the repo dynamically through
    the Hub tree API, following Link-header pagination (amendment 11: shard
    counts are not worth hardcoding at ~1,450 files)."""
    import httpx

    from app.acquire import transport_hf_hub as mod

    pages = {
        "https://huggingface.co/api/datasets/u/w/tree/main?recursive=true": (
            [{"type": "file", "path": "data/tr/000.parquet", "size": 10},
             {"type": "directory", "path": "data/tr"},
             {"type": "file", "path": "README.md", "size": 5}],
            "https://huggingface.co/api/next-page"),
        "https://huggingface.co/api/next-page": (
            [{"type": "file", "path": "data/tr/001.parquet", "size": 20}],
            None),
    }

    class FakeResponse:
        def __init__(self, url):
            import json
            self._rows, nxt = pages[url]
            self.links = {"next": {"url": nxt}} if nxt else {}
            self.content = json.dumps(self._rows).encode()

        def raise_for_status(self):
            pass

        def json(self):
            return self._rows

    class FakeClient:
        def __init__(self, timeout=None, headers=None):
            self.headers = headers or {}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url):
            assert "louise-rag" in self.headers.get("User-Agent", ""), \
                "dynamic listing must identify itself (Wikimedia lesson)"
            return FakeResponse(url)

    monkeypatch.setattr(mod.httpx, "Client", FakeClient)
    drawn = []
    monkeypatch.setattr(mod.budget, "acquire_bytes", drawn.append)
    transport = HfHubTransport()
    endpoint = Endpoint(transport=Transport.HF_HUB, url="datasets/u/w")
    files = list(transport.list_files(endpoint, None))
    assert [f.rel_path for f in files] == [
        "data/tr/000.parquet", "README.md", "data/tr/001.parquet"]
    assert [f.size for f in files] == [10, 5, 20]
    # invariant 5: each listing page's bytes are drawn from the budget
    assert drawn == [len(FakeResponse(u).content) for u in pages]
