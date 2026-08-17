"""Shared pytest fixtures — isolated DATA_DIR/CORPUS_DIR/CONFIG_DIR per test.

Nothing here touches the real /data or /corpus; every test gets its own
tmp_path-backed copy so tests never interfere with each other or with a real
deployment.
"""
import shutil
from pathlib import Path

import pytest

FIXTURE_CORPUS = Path(__file__).parent / "fixtures" / "gutenberg-mini" / "corpus"


@pytest.fixture
def corpus_dir(tmp_path) -> Path:
    dest = tmp_path / "corpus"
    shutil.copytree(FIXTURE_CORPUS, dest)
    return dest


@pytest.fixture
def data_dir(tmp_path) -> Path:
    d = tmp_path / "data"
    d.mkdir()
    return d


@pytest.fixture
def config_dir(tmp_path) -> Path:
    d = tmp_path / "config"
    d.mkdir()
    return d


@pytest.fixture
def isolated_config(monkeypatch, corpus_dir, data_dir, config_dir):
    """Point app.config at temp dirs for the duration of one test."""
    from app import config

    monkeypatch.setattr(config, "CORPUS_DIR", corpus_dir)
    monkeypatch.setattr(config, "DATA_DIR", data_dir)
    monkeypatch.setattr(config, "DB_PATH", data_dir / "manifest.db")
    monkeypatch.setattr(config, "FTS_DIR", data_dir / "fts")
    monkeypatch.setattr(config, "LANCE_DIR", data_dir / "lancedb")
    monkeypatch.setenv("CONFIG_DIR", str(config_dir))
    config.ensure_dirs()
    return config
