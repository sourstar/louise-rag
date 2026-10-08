"""Desired state surviving every file rewrite.

A pinned `snapshot` and `embed` are Tier-2 desired state in the
collection's file, and every endpoint that rewrites it must carry them
forward — dropping either would silently move the collection at the next
restart, the same trap the [encoder] block already guards against.

(The install-time wikipedia companion this file also tested was created
by the old `/admin/library/new/create`, whose files named no Codex; the
route is gone since 2026-10-08.)
"""
import tomllib

import pytest
from fastapi.testclient import TestClient

from app import collections as collections_mod, config, spaces, web
from app.api import app


@pytest.fixture
def client(isolated_config, monkeypatch, tmp_path):
    monkeypatch.setattr(spaces, "resolve_embed_model_hash", lambda *a: ("abc123", False))
    monkeypatch.setattr(web, "_ping", lambda url: True)
    monkeypatch.setattr(config, "ADMIN_TOKEN", "sekrit")
    with TestClient(app) as c:
        c.headers["X-Admin-Token"] = "sekrit"
        yield c


def test_snapshot_pin_survives_ui_rewrites(client):
    """A pinned `snapshot` is desired state like the [encoder] block: any
    file rewrite that drops it silently moves the collection back to
    'whatever the origin calls latest', which is exactly the
    irreproducibility the pin exists to prevent."""
    toml_path = collections_mod.config_toml_path("gutenberg")
    toml_path.write_text(toml_path.read_text(encoding="utf-8") + 'snapshot = "20260801"\n',
                         encoding="utf-8")

    assert client.post("/admin/library/gutenberg/embed-mode",
                       json={"mode": "off"}).status_code == 200
    raw = tomllib.loads(toml_path.read_text(encoding="utf-8"))
    assert raw["snapshot"] == "20260801", "the pin was dropped by a rewrite"
    assert raw["embed"] == "off"
    assert raw["codex"], "a rewrite carries the Codex through"
    assert collections_mod.by_name("gutenberg").snapshot == "20260801"
