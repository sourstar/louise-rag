"""The file digests behind a run, kept across loads (operator, 2026-10-21:
"VeGa rechecks at every model reload. The fix is keeping the cache between
reloads"). LocalAI starts a new wrapper for every load; with the cache in
memory alone each load read the whole backend tree and the weights again —
~75 s of louise-python on the node, past the worker's 65 s bound."""
from __future__ import annotations

import json
import os
from pathlib import Path

from app.vega import runs
from tests.vega.test_wrapper_logic import _config, vw


def _tree(root: Path) -> Path:
    for rel, data in (("run.sh", b"#!/bin/sh\n"), ("lib/libggml.so", b"x" * 5000),
                      ("python/site.py", b"print(1)\n"), ("models/w.bin", b"w" * 3000)):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(data)
    return root


def test_a_second_wrapper_reads_no_unchanged_file_and_states_the_same_digest(tmp_path):
    root, store = _tree(tmp_path / "backend"), tmp_path / "pkg" / vw.DIGEST_STORE
    store.parent.mkdir()
    first = vw.FileDigests(store=store)
    manifest = vw.tree_manifest(root, first)
    first.save()
    assert len(first.reads) == 4 and store.is_file()

    second = vw.FileDigests(store=store)                 # the next load's wrapper
    assert vw.tree_manifest(root, second) == manifest
    assert second.reads == [], "nothing read again"


def test_a_changed_file_is_read_again_and_only_it(tmp_path):
    root, store = _tree(tmp_path / "backend"), tmp_path / vw.DIGEST_STORE
    first = vw.FileDigests(store=store)
    before = vw.tree_manifest(root, first)
    first.save()
    (root / "lib" / "libggml.so").write_bytes(b"y" * 5000)   # same size, new bytes
    second = vw.FileDigests(store=store)
    after = vw.tree_manifest(root, second)
    assert second.reads == [os.path.realpath(root / "lib" / "libggml.so")]
    assert after["lib/libggml.so"] != before["lib/libggml.so"]
    assert {k: v for k, v in after.items() if k != "lib/libggml.so"} == \
        {k: v for k, v in before.items() if k != "lib/libggml.so"}


def test_an_unreadable_store_costs_only_the_reads_and_is_rewritten_whole(tmp_path):
    root, store = _tree(tmp_path / "backend"), tmp_path / vw.DIGEST_STORE
    for bad in ("not json", json.dumps({"version": 99, "files": {}}),
                json.dumps({"version": 1, "files": {str(root / "run.sh"): [1, 2, 3, "x"]}})):
        store.write_text(bad)
        files = vw.FileDigests(store=store)
        expected = vw.tree_manifest(root, vw.FileDigests())
        assert vw.tree_manifest(root, files) == expected and len(files.reads) == 4
        files.save()
        assert json.loads(store.read_text())["version"] == vw.DIGEST_STORE_VERSION
        assert vw.FileDigests(store=store)._known.keys() == files._known.keys()


def test_two_wrappers_of_one_package_keep_each_others_digests(tmp_path):
    root, store = _tree(tmp_path / "backend"), tmp_path / vw.DIGEST_STORE
    a, b = vw.FileDigests(store=store), vw.FileDigests(store=store)
    a.read(root / "run.sh")
    b.read(root / "models" / "w.bin")
    a.save()
    b.save()                                             # b never saw a's entry
    kept = vw.FileDigests(store=store)._known
    assert {os.path.realpath(root / "run.sh"), os.path.realpath(root / "models" / "w.bin")} <= set(kept)


def test_what_is_hashed_is_kept_while_hashing_so_a_given_up_load_is_not_lost(tmp_path, monkeypatch):
    root, store = _tree(tmp_path / "backend"), tmp_path / vw.DIGEST_STORE
    monkeypatch.setattr(vw, "SAVE_EVERY_S", 0.0)
    files = vw.FileDigests(store=store)
    files.read(root / "run.sh")                          # no save() called: the process "dies"
    assert os.path.realpath(root / "run.sh") in vw.FileDigests(store=store)._known


def test_weights_read_through_the_kept_digests_give_the_same_digest(tmp_path):
    root, store = _tree(tmp_path / "backend"), tmp_path / vw.DIGEST_STORE
    for weights in (root / "models", root / "models" / "w.bin"):
        files = vw.FileDigests(store=store)
        assert runs.weights_digest(weights, file_digest=files.sha256) == runs.weights_digest(weights)
        files.save()
        again = vw.FileDigests(store=store)
        assert runs.weights_digest(weights, file_digest=again.sha256) == runs.weights_digest(weights)
        assert again.reads == []


def test_a_wrapper_keeps_its_digests_beside_its_package(tmp_path):
    from tests.vega.test_wrapper_logic import _backend_dir
    backend = vw.real_backend(_backend_dir(tmp_path, name="fake-embed"))
    cfg = vw.load_config(_config(tmp_path), tmp_path / "backends")
    assert cfg.package == tmp_path.resolve()
    gate = vw.Gate(cfg, backend, child=None, receipts=None, pb=None, environ={})
    assert gate.files.store == tmp_path.resolve() / vw.DIGEST_STORE
    w = tmp_path / "w.bin"
    w.write_bytes(b"weights")
    assert gate.hash_weights(w) == runs.weights_digest(w)
    assert gate.files.reads == [os.path.realpath(w)]
