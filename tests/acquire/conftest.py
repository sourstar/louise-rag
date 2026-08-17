import pytest

from tests.acquire.mock_http_server import MockServer


@pytest.fixture
def mock_server(tmp_path):
    root = tmp_path / "origin"
    root.mkdir()
    server = MockServer(root)
    yield server
    server.shutdown()
