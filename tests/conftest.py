"""Shared pytest fixtures — isolated DATA_DIR/CORPUS_DIR/CONFIG_DIR per test.

Nothing here touches the real /data or /corpus; every test gets its own
tmp_path-backed copy so tests never interfere with each other or with a real
deployment.
"""
import shutil
from pathlib import Path

import os

import pytest

# the web's background warm of the question path's models (app/warmup.py)
# is off in tests: a test that starts the app talks to no model server
os.environ["LOUISE_WARMUP"] = "0"

FIXTURE_CORPUS = Path(__file__).parent / "fixtures" / "gutenberg-mini" / "corpus"


@pytest.fixture(autouse=True)
def fresh_worker_state():
    """Module-level cursors and memos are per PROCESS, so one test's
    completed walk must not decide the next test's selection (that leak
    is exactly the bug the re-index cursor's key had in production)."""
    from app import (cpu_limit, dispatch, encoder_timeouts, language_plan, parallel, sched, sizing,
                     translate, worker)
    from app.bundles import genesis
    dispatch.reset()
    encoder_timeouts.reset()
    language_plan.reset()
    translate.reset()
    sched.scheduler().reset()
    sizing.sizer().reset()
    worker.reset_reindex_cursors()
    worker.reset_query_priority_memo()
    worker.reset_phase_memo()
    worker._prefetch_last.clear()
    worker._vindex_last.clear()
    worker._vlight_last.clear()
    genesis.reset_revisit_state()
    parallel.forget_prefetched()
    parallel.forget_users()
    parallel.reset_failures()
    # the node profile's cached limit and each collection's watch of it
    cpu_limit.reset()
    worker._cpu_watches.clear()
    # the beat reaches `_log_shares` now; its 600 s throttle must not carry
    # one test's log into the next
    worker._last_shares_log = 0.0
    yield


@pytest.fixture(autouse=True)
def no_runner_says_what_it_holds(monkeypatch):
    """app/model_load.py asks a model's runner (`GET <server>/system`) whether
    the model is loaded before a request. No test's fake encoder, reranker or
    chat server has that route, and the hosts they name do not exist: every
    test runs as against a runner that cannot say — exactly the behaviour
    before the warm existed — unless it fakes a runner itself
    (tests/test_model_load.py)."""
    from app import model_load
    model_load.reset()
    monkeypatch.setattr(model_load, "_fetch_system", lambda server: None)
    yield
    model_load.reset()


@pytest.fixture(autouse=True)
def no_served_weights_are_remembered():
    """app/encoder_resolve.py caches, per process, the weights each model on
    a server states. A test's fake server must not answer for the next."""
    from app import encoder_resolve
    encoder_resolve.reset_cache()
    yield
    encoder_resolve.reset_cache()


@pytest.fixture(autouse=True)
def no_model_is_pinned(monkeypatch):
    """app/model_pins.py pins a LocalAI model while it processes. No test's
    runner has LocalAI's pin API and its hosts do not exist, so nothing is
    pinned unless a test fakes the runner itself (tests/test_model_pins.py)."""
    from app import model_pins
    monkeypatch.setattr(model_pins, "_applies", lambda server, model: False)
    model_pins._retry_after.clear()
    yield


@pytest.fixture(autouse=True)
def no_disk_floor(monkeypatch):
    """The vector store's free-space floor (app/disk_guard.py, 32 GiB) is the
    HOST's business: a runner with less free disk deferred every vector
    import in three unrelated tests (test_accept's survivor 2026-10-19,
    test_deprecate and test_bundle_worker 2026-10-23, at 19.5 GiB free). The
    tests of the floor set their own values (tests/test_disk_guard.py)."""
    from app import config
    monkeypatch.setattr(config, "VECTORS_MIN_FREE_GB", 0.0)
    monkeypatch.setattr(config, "VECTORS_MIN_FREE_FRACTION", 0.0)


@pytest.fixture(autouse=True)
def a_fresh_torrent_session():
    """The process-wide libtorrent session (app/acquire/transport_torrent.py)
    is shut down after any test that started one, so the next starts clean.
    The single-survivor test meshes it into a private DHT with changed
    settings and others leave torrents in it; a later test inheriting that
    failed only in the full suite (2026-10-08)."""
    yield
    from app.acquire import transport_torrent as tt
    if tt._session is not None:
        tt.shutdown_session()


@pytest.fixture(autouse=True)
def no_checkout_config(tmp_path_factory, monkeypatch):
    """No test reads the checkout's own config: CONFIG_DIR is an empty
    directory unless the test (or `isolated_config`) points it elsewhere.
    It used to fall back to `.`, and with the repo's pre-Codex
    `collections/gutenberg.toml` gone (2026-10-08) a test that never chose
    a config would otherwise have read whatever the working tree held."""
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path_factory.mktemp("no-config")))


#: The real data directory and node root, read ONCE at import, before any
#: fixture repoints the environment — what the guard below must watch.
_REAL_DATA = Path(os.getenv("DATA_DIR", "/data"))
_REAL_ROOT = Path(os.getenv("LOUISE_ROOT") or _REAL_DATA)


@pytest.fixture(autouse=True)
def no_real_data_dir(tmp_path_factory, monkeypatch):
    """No test resolves the real data directory: `config.DATA_DIR` is an
    empty directory unless the test (or `isolated_config`) points it
    elsewhere. Tests that build a collection from `tmp_path` alone ran fts,
    whose reference store sits beside DATA_DIR, and created the real
    /data/refs.db (2026-10-08)."""
    from app import config
    empty = tmp_path_factory.mktemp("no-data")
    monkeypatch.setattr(config, "DATA_DIR", empty)
    # AND the environment, which `config.louise_root()` reads at call time: a
    # node's container sets LOUISE_ROOT to the real /data, so every test
    # without `isolated_config` resolved the node's own state — the suite
    # run on the node wrote /data/state/.model-pins.lock (2026-10-27), and
    # with LOUISE_ROOT pointed at a stand-in, node.sqlite and trust.json too
    monkeypatch.setenv("LOUISE_ROOT", str(empty))
    monkeypatch.setenv("DATA_DIR", str(empty))


@pytest.fixture(autouse=True)
def the_real_node_store_is_never_touched():
    """app/node_state.py resolves `<LOUISE_ROOT or DATA_DIR>/state/node.sqlite`
    — on a node's volume, that node's LIVE mode. Two tests without
    `isolated_config` once wrote `worker_state=running` there (review,
    2026-09-13): run with a node's /data mounted, the suite would resume a
    paused node. Any test that creates or changes the real store fails.

    Nor anything else under the real data directory: three tests created
    `/data/refs.db` through a path frozen at import (2026-10-08). Absent
    before, it must be absent after; present (a node's volume mounted), its
    top level and the entries in it must be unchanged — one listing and a
    stat per entry, never a walk of a node's tree."""
    data = _REAL_DATA
    real = _REAL_ROOT / "state" / "node.sqlite"

    def _stamp():
        out = []
        for p in (real, real.with_name(real.name + "-wal"),
                  real.with_name("rechecked.json"), real.with_name("trust.json"),
                  real.with_name("model-pins.json"), real.with_name(".model-pins.lock")):
            try:
                st = p.stat()
                out.append((st.st_size, st.st_mtime_ns))
            except OSError:
                out.append(None)
        return out

    def _data_stamp():
        try:
            entries = sorted(data.iterdir())
        except OSError:
            return None
        out = []
        for p in [data, *entries]:
            try:
                out.append((p.name, p.stat().st_mtime_ns))
            except OSError:
                out.append((p.name, None))
        return out

    before, data_before = _stamp(), _data_stamp()
    yield
    assert _stamp() == before, \
        f"this test touched the real node store {real} — give it isolated_config"
    data_after = _data_stamp()
    assert data_after == data_before, \
        f"this test created or changed something under the real {data} " \
        f"({sorted(n for n, _ in data_after or []) if data_before is None else 'changed entries'}) " \
        f"— give it isolated_config, or resolve the path at call time"


@pytest.fixture(autouse=True)
def serial_parse_pool(monkeypatch):
    """Every test runs the in-process parse path (app/parallel.py): a
    monkeypatch cannot reach a spawned child, and the pool's own tests opt
    back in explicitly (PARSE_PROCESSES=2)."""
    from app import config
    monkeypatch.setattr(config, "PARSE_PROCESSES", 1)
    monkeypatch.setattr(config, "BUILD_WHOLE_QUOTA", False)


@pytest.fixture
def corpus_dir(tmp_path) -> Path:
    dest = tmp_path / "corpus"
    shutil.copytree(FIXTURE_CORPUS, dest)
    return dest


def gutenberg_books(dest: Path, n: int, first: int = 20000) -> Path:
    """A gutenberg corpus of `n` books at `dest`, cycled from the mini
    fixture's three under new ids (~2.5 KB each): enough rows for a build of
    several pages (a page is at least 50 records, app/pack/build.py)."""
    templates = sorted(p for p in FIXTURE_CORPUS.iterdir() if p.is_dir())
    for k in range(n):
        src = templates[k % len(templates)]
        old, new = src.name, str(first + k)
        (dest / new).mkdir(parents=True)
        for f in src.iterdir():
            (dest / new / f.name.replace(old, new)).write_bytes(
                f.read_bytes().replace(old.encode(), new.encode()))
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
    # an EXPORT_DIR in the environment running the suite must not send the
    # tests' exports to it; the tests that want one set their own
    monkeypatch.setattr(config, "EXPORT_DIR", None)
    # the parse pool (app/parallel.py) is exercised by its own tests; every
    # other test runs the in-process serial path — same results, no children
    monkeypatch.setattr(config, "PARSE_PROCESSES", 1)
    monkeypatch.setenv("CONFIG_DIR", str(config_dir))
    # THE DEFAULT COLLECTION IS A CODEX COLLECTION (2026-10-08: the pre-Codex
    # shape and its synthesized default are gone): gutenberg from the test
    # recipe over this test's corpus, its encoder the Codex's own.
    _codex_default(request, monkeypatch, corpus_dir, data_dir, config_dir)
    config.ensure_dirs()
    return config


_CODEX_TEMPLATE: dict = {}


def _codex_template():
    """gutenberg from the test recipe, created ONCE per session (a Codex,
    its calibration member, its SHARD) and copied wherever a test wants a
    Codex collection: the tree is content-addressed, so every copy is the
    same collection."""
    import tempfile
    from app.hold import collection as col
    from tests.hold.conftest import KEY, NOW, PLATFORM, fake_embed, fake_query_embed
    from tests.hold.test_template import recipe
    if "root" not in _CODEX_TEMPLATE:
        tmpl = Path(tempfile.mkdtemp(prefix="codex-default-"))
        (tmpl / "collections").mkdir()
        codex = col.create_from_recipe(tmpl / "collections", "gutenberg", recipe(), platform_image=PLATFORM,
                                       embed=fake_embed, embed_query=fake_query_embed, name="gutenberg",
                                       key=KEY, now=NOW)
        _CODEX_TEMPLATE.update(root=tmpl, id=codex.collection_id)
    return _CODEX_TEMPLATE


def codex_collection(name: str = "gutenberg", *, louise_root: Path, config_dir: Path, corpus_dir: Path | None = None,
                     toml_extra: str = "") -> str:
    """A Codex collection named `name` (gutenberg's test Codex) on this
    test's node: its tree under `<louise_root>/collections/<name>/`, its
    file `<config_dir>/collections/<name>.toml` naming the Codex, the
    corpus when given, and `toml_extra` (more file keys, as TOML text).
    Returns the collection_id. THE way a test makes a collection since the
    pre-Codex shape left (2026-10-08)."""
    t = _codex_template()
    shutil.copytree(t["root"] / "collections" / "gutenberg", Path(louise_root) / "collections" / name,
                    dirs_exist_ok=True)
    (Path(config_dir) / "collections").mkdir(parents=True, exist_ok=True)
    body = f'codex = "{t["id"]}"\n'
    if corpus_dir is not None:
        body += f'corpus_dir = "{Path(corpus_dir).as_posix()}"\n'
    (Path(config_dir) / "collections" / f"{name}.toml").write_text(body + toml_extra, encoding="utf-8")
    return t["id"]


def _codex_default(request, monkeypatch, corpus_dir, data_dir, config_dir):
    """The default collection: `codex_collection("gutenberg")` over this
    test's corpus, LOUISE_ROOT pointed at the test's data dir."""
    codex_collection("gutenberg", louise_root=data_dir, config_dir=config_dir, corpus_dir=corpus_dir)
    monkeypatch.setenv("LOUISE_ROOT", str(data_dir))

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
                {"url": TEST_ENCODER.url, "api": TEST_ENCODER.api,
                 "embed_model": TEST_ENCODER.model, "query_model": TEST_ENCODER.model},
                c.codex, str(path))
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
    from app import catalog, fts
    catalog.close_all()
    fts.close_all()       # the same: one writer per index, kept for the worker's life
