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
    from app import language_plan, parallel, sched, sizing, translate, worker
    from app.bundles import genesis
    language_plan.reset()
    translate.reset()
    sched.scheduler().reset()
    sizing.sizer().reset()
    worker.reset_reindex_cursors()
    worker.reset_query_priority_memo()
    worker.reset_phase_memo()
    worker._prefetch_last.clear()
    worker._vindex_last.clear()
    genesis.reset_revisit_state()
    parallel.forget_prefetched()
    parallel.forget_users()
    parallel.reset_failures()
    # the beat reaches `_log_shares` now; its 600 s throttle must not carry
    # one test's log into the next
    worker._last_shares_log = 0.0
    yield


@pytest.fixture(autouse=True)
def the_real_node_store_is_never_touched():
    """app/node_state.py resolves `<LOUISE_ROOT or DATA_DIR>/state/node.sqlite`
    — on a node's volume, that node's LIVE mode. Two tests without
    `isolated_config` once wrote `worker_state=running` there (review,
    2026-09-13): run with a node's /data mounted, the suite would resume a
    paused node. Any test that creates or changes the real store fails."""
    import os
    real = Path(os.getenv("LOUISE_ROOT") or os.getenv("DATA_DIR", "/data")) \
        / "state" / "node.sqlite"

    def _stamp():
        out = []
        for p in (real, real.with_name(real.name + "-wal")):
            try:
                st = p.stat()
                out.append((st.st_size, st.st_mtime_ns))
            except OSError:
                out.append(None)
        return out

    before = _stamp()
    yield
    assert _stamp() == before, \
        f"this test touched the real node store {real} — give it isolated_config"


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
def isolated_config(request, monkeypatch, corpus_dir, data_dir, config_dir):
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
    # NO DEFAULT ENCODER (operator, 2026-09-18): the synthesized legacy
    # collection names none, and a collection that names none is refused.
    # The tests that embed with it name one HERE, explicitly — the values
    # the removed env defaults had, so what those tests assert still holds.
    import dataclasses
    from app import collections as collections_mod
    if not request.node.get_closest_marker("strict_encoder"):
        synthesized = collections_mod._default_gutenberg_collection
        monkeypatch.setattr(collections_mod, "_default_gutenberg_collection",
                            lambda: dataclasses.replace(synthesized(), embed=TEST_ENCODER))
    config.ensure_dirs()
    return config


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "strict_encoder: load collection files as the node does — a file "
                   "naming no [encoder] is refused, never given the test encoder")


@pytest.fixture(autouse=True)
def test_collection_files_name_an_encoder(request, monkeypatch):
    """The suite's collection files predate "a collection names its own
    encoder, there is no default" (operator, 2026-09-18) and most name
    none: they were served the env default. Here each such file is given
    the TEST encoder, explicitly — what the removed default was — so the
    hundreds of tests about other things keep testing those things. A test
    about the rule itself is marked `strict_encoder` and loads files as the
    node does (tests/test_no_default_encoder.py)."""
    if request.node.get_closest_marker("strict_encoder"):
        return
    import dataclasses
    from app import collections as collections_mod
    real = collections_mod._load_one

    def load(path):
        c = real(path)
        if c.embed is not None:
            return c
        if c.codex is not None:
            # a Codex file's encoder carries the Codex's space, as the real one
            embed = collections_mod._codex_encoder(
                {"url": TEST_ENCODER.url, "api": TEST_ENCODER.api}, c.codex, str(path))
        else:
            embed = TEST_ENCODER
        return dataclasses.replace(c, embed=embed)
    monkeypatch.setattr(collections_mod, "_load_one", load)

    # and a Collection a test builds by hand, with no encoder, is resolved
    # to the same test encoder rather than refused
    real_recipe_for = collections_mod.recipe_for

    def recipe_for(collection):
        try:
            return real_recipe_for(collection)
        except collections_mod.NoEncoder:
            return TEST_ENCODER
    monkeypatch.setattr(collections_mod, "recipe_for", recipe_for)


def _test_encoder():
    from app.collections import EmbedRecipe
    return EmbedRecipe(url="http://host.docker.internal:11434", api="ollama",
                       model="qwen3-embedding-8k:latest", dtype="f32", ctx=8192)


TEST_ENCODER = _test_encoder()


@pytest.fixture(autouse=True)
def _catalog_writers_closed():
    """app/catalog.py keeps one writer per collection for the worker's life;
    a test's collection lives for one test."""
    yield
    from app import catalog
    catalog.close_all()
