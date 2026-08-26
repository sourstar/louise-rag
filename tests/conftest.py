"""Shared pytest fixtures — isolated DATA_DIR/CORPUS_DIR/CONFIG_DIR per test.

Nothing here touches the real /data or /corpus; every test gets its own
tmp_path-backed copy so tests never interfere with each other or with a real
deployment.
"""
import shutil
from pathlib import Path

import pytest

FIXTURE_CORPUS = Path(__file__).parent / "fixtures" / "gutenberg-mini" / "corpus"


@pytest.fixture(autouse=True)
def fresh_worker_state():
    """Module-level cursors and memos are per PROCESS, so one test's
    completed walk must not decide the next test's selection (that leak
    is exactly the bug the re-index cursor's key had in production)."""
    from app import parallel, sched, sizing, worker
    from app.bundles import genesis
    sched.scheduler().reset()
    sizing.sizer().reset()
    worker.reset_reindex_cursors()
    worker.reset_query_priority_memo()
    worker._prefetch_last.clear()
    worker._vindex_last.clear()
    genesis.reset_revisit_state()
    parallel.forget_prefetched()
    parallel.forget_users()
    parallel.reset_failures()
    yield


@pytest.fixture(autouse=True)
def serial_parse_pool(monkeypatch):
    """Every test runs the in-process parse path (app/parallel.py): a
    monkeypatch cannot reach a spawned child, and the pool's own tests opt
    back in explicitly (PARSE_PROCESSES=2)."""
    from app import config
    monkeypatch.setattr(config, "PARSE_PROCESSES", 1)


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
    # pubmed's derived packs go beside the corpus by default — tests that
    # read the committed fixture in place must not leave a .pack/ in it
    monkeypatch.setattr(config, "PUBMED_PACK_DIR", data_dir / "pubmed-pack")
    # the parse pool (app/parallel.py) is exercised by its own tests; every
    # other test runs the in-process serial path — same results, no children
    monkeypatch.setattr(config, "PARSE_PROCESSES", 1)
    monkeypatch.setenv("CONFIG_DIR", str(config_dir))
    config.ensure_dirs()
    return config
