"""app/verify/backend_gate.py — the behavioral backend gate.

The scenario it exists for happened in production: a weak (name-derived)
model hash cannot see a backend swap behind EMBED_URL, so the gate records
probe vectors on first use and refuses to write new vectors when the live
endpoint stops reproducing them.
"""
import hashlib
import json

import pytest

from app import collections as collections_mod, config, db, worker
from app.verify import backend_gate


def _collection(tmp_path):
    d = tmp_path / "corpus"
    d.mkdir(exist_ok=True)
    return collections_mod.Collection(
        name="test", plugin="gutenberg", profile=None, embed_mode_default=None,
        corpus_dir=d, manifest_path=tmp_path / "manifest.db",
        fts_dir=tmp_path / "fts", lance_dir=tmp_path / "lancedb",
    )


def _make_embed_fn(seed: str, dim: int = 32):
    """Deterministic fake backend: vector is a pure function of (seed, text).
    Different seeds = different models — near-orthogonal outputs."""
    def embed(texts):
        out = []
        for t in texts:
            h = hashlib.blake2b(f"{seed}:{t}".encode(), digest_size=dim * 2).digest()
            vec = [b - 127.5 for b in h[:dim]]
            norm = sum(x * x for x in vec) ** 0.5
            out.append([x / norm for x in vec])
        return out
    return embed


@pytest.fixture(autouse=True)
def _fresh_cache():
    backend_gate.reset_cache()
    yield
    backend_gate.reset_cache()


def test_first_use_records_baseline_and_passes(tmp_path):
    collection = _collection(tmp_path)
    result = backend_gate.check(collection, _make_embed_fn("model-A"))
    assert result["ok"] and result["recorded"]
    baseline = json.loads(backend_gate.baseline_path(collection).read_text())
    assert set(baseline["probes"]) == set(backend_gate.PROBES)
    assert baseline["dim"] == 32


def test_same_backend_passes_against_recorded_baseline(tmp_path):
    collection = _collection(tmp_path)
    backend_gate.check(collection, _make_embed_fn("model-A"))
    result = backend_gate.check(collection, _make_embed_fn("model-A"))
    assert result["ok"] and not result["recorded"]
    assert min(result["cosines"].values()) > 0.999


def test_changed_backend_is_refused_and_baseline_untouched(tmp_path):
    collection = _collection(tmp_path)
    backend_gate.check(collection, _make_embed_fn("model-A"))
    before = backend_gate.baseline_path(collection).read_bytes()
    with pytest.raises(backend_gate.BackendGateError) as e:
        backend_gate.check(collection, _make_embed_fn("model-B"))
    assert "rebaseline" in str(e.value)
    assert backend_gate.baseline_path(collection).read_bytes() == before


def test_changed_dim_is_refused_as_different_model(tmp_path):
    collection = _collection(tmp_path)
    backend_gate.check(collection, _make_embed_fn("model-A", dim=32))
    with pytest.raises(backend_gate.BackendGateError) as e:
        backend_gate.check(collection, _make_embed_fn("model-A", dim=16))
    assert "dim" in str(e.value)


def test_rebaseline_accepts_the_new_backend(tmp_path):
    collection = _collection(tmp_path)
    backend_gate.check(collection, _make_embed_fn("model-A"))
    backend_gate.record_baseline(collection, _make_embed_fn("model-B"))
    result = backend_gate.check(collection, _make_embed_fn("model-B"))
    assert result["ok"]


def test_endpoint_down_is_unavailable_not_mismatch(tmp_path):
    collection = _collection(tmp_path)
    backend_gate.check(collection, _make_embed_fn("model-A"))

    def broken(texts):
        raise ConnectionError("boom")
    with pytest.raises(backend_gate.BackendUnavailable):
        backend_gate.check(collection, broken)


def test_batch_corruption_is_detected(tmp_path, monkeypatch):
    """The production defect: multi-text requests return corrupted vectors
    while single-text requests are fine. Only checked when config asks for
    batches — at EMBED_BATCH=1 the batched code path never runs."""
    collection = _collection(tmp_path)
    good = _make_embed_fn("model-A")
    bad = _make_embed_fn("corrupted")

    def batch_corrupting(texts):
        return good(texts) if len(texts) == 1 else bad(texts)

    monkeypatch.setattr(config, "EMBED_BATCH", 4)
    with pytest.raises(backend_gate.BackendGateError) as e:
        backend_gate.check(collection, batch_corrupting)
    assert "EMBED_BATCH=1" in str(e.value)

    monkeypatch.setattr(config, "EMBED_BATCH", 1)
    backend_gate.reset_cache()
    assert backend_gate.check(collection, batch_corrupting)["ok"]


def test_embed_allowed_caches_and_logs_transitions(tmp_path):
    collection = _collection(tmp_path)
    con = db.connect(collection)
    calls = []
    fn = _make_embed_fn("model-A")

    def counting(texts):
        calls.append(len(texts))
        return fn(texts)

    assert backend_gate.embed_allowed(con, collection, counting)
    first = len(calls)
    assert backend_gate.embed_allowed(con, collection, counting)
    assert len(calls) == first          # cached — no second probe round

    events = con.execute(
        "SELECT message FROM events WHERE stage='backend-gate'").fetchall()
    assert any("enabled" in e["message"] for e in events)


def test_embed_allowed_blocks_on_mismatch_without_erroring_documents(tmp_path):
    collection = _collection(tmp_path)
    con = db.connect(collection)
    backend_gate.check(collection, _make_embed_fn("model-A"))
    assert not backend_gate.embed_allowed(con, collection, _make_embed_fn("model-B"))
    events = con.execute(
        "SELECT message FROM events WHERE stage='backend-gate'").fetchall()
    assert any("PAUSED" in e["message"] for e in events)
    # nothing wrote to documents.error — that column poisons the pipeline
    assert con.execute("SELECT COUNT(*) FROM documents WHERE error IS NOT NULL") \
        .fetchone()[0] == 0


def test_gate_off_bypasses_without_probing(tmp_path, monkeypatch):
    collection = _collection(tmp_path)
    con = db.connect(collection)
    monkeypatch.setattr(config, "BACKEND_GATE", "off")

    def must_not_run(texts):
        raise AssertionError("gate probed despite BACKEND_GATE=off")
    assert backend_gate.embed_allowed(con, collection, must_not_run)


def test_next_book_skips_embed_when_gate_blocks(tmp_path):
    """With the gate blocking, _next_book must fall through to fts/rdf work
    instead of returning embed items forever (which would starve the rest of
    the pipeline while the backend is wrong)."""
    collection = _collection(tmp_path)
    con = db.connect(collection)
    con.execute(
        "INSERT INTO documents(uid, source, native_id, path, in_scope, rdf_done, "
        "fts_done, embed_done) VALUES('gutenberg:1','gutenberg','1','/x',1,1,1,0)")
    con.execute(
        "INSERT INTO documents(uid, source, native_id, path, in_scope, rdf_done, "
        "fts_done, embed_done) VALUES('gutenberg:2','gutenberg','2','/y',1,0,0,0)")
    # the lazy-priority queue outranks everything, so it is the strongest
    # case: even a user-requested embed must yield while the backend is wrong
    con.execute("INSERT INTO embed_queue(uid, priority, ts) VALUES('gutenberg:1', 1, 1)")
    scope = {"embed_mode": "all"}

    stage, row = worker._next_book(con, scope)
    assert stage == "embed" and row["uid"] == "gutenberg:1"

    stage, row = worker._next_book(con, scope, embed_allowed=False)
    assert stage == "rdf" and row["uid"] == "gutenberg:2"
