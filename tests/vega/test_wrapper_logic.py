"""deploy/vega/vega_wrapper.py without gRPC: its configuration, the real
backend's identity, the run it states, and its receipts client against the
real intake. The gRPC path is test_wrapper_grpc.py."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from app.rage import loadopts, runs as rage_runs
from app.rage import pairs

from .conftest import ROOT, load_wrapper

vw = load_wrapper()
DIGEST = "sha256:" + "cd" * 32


def _backend_dir(tmp_path, meta=None, name="fake-embed"):
    d = tmp_path / "backends" / name
    d.mkdir(parents=True)
    (d / "run.sh").write_text("#!/bin/sh\n")
    meta = {"name": name, "version": "v4.11.0", "uri": "quay.io/x:y", "digest": DIGEST} \
        if meta is None else meta
    (d / "metadata.json").write_text(json.dumps(meta))
    return d


def _config(tmp_path, **kw):
    doc = {"backend": "fake-embed", "receipts_socket": "/vega/r.sock"} | kw
    p = tmp_path / "vega.json"
    p.write_text(json.dumps(doc))
    return p


def test_the_config_names_the_backend_beside_the_wrapper(tmp_path):
    cfg = vw.load_config(_config(tmp_path, cards={"0000:03:00.0": "Arc"}), tmp_path / "backends")
    assert cfg.backend == tmp_path / "backends" / "fake-embed"
    assert cfg.socket == Path("/vega/r.sock") and cfg.cards == {"0000:03:00.0": "Arc"}
    assert cfg.runtime_packages == vw.RUNTIME_PACKAGES


@pytest.mark.parametrize("doc, why", [
    ({"receipts_socket": "/r"}, "`backend`"),
    ({"backend": "b", "receipts_socket": "relative"}, "absolute"),
    ({"backend": "b", "receipts_socket": "/r", "cards": {"3:0": "Arc"}}, "PCI address"),
    ({"backend": "b", "receipts_socket": "/r", "colour": 1}, "unknown key"),
])
def test_a_config_that_is_not_valid_is_refused_with_its_rule(tmp_path, doc, why):
    p = tmp_path / "vega.json"
    p.write_text(json.dumps(doc))
    with pytest.raises(vw.ConfigError, match=why):
        vw.load_config(p, tmp_path)


def test_the_real_backend_is_named_by_the_digest_its_gallery_wrote(tmp_path):
    b = vw.real_backend(_backend_dir(tmp_path))
    assert (b.name, b.build, b.image) == ("fake-embed", "v4.11.0", DIGEST)
    assert b.run_sh.is_absolute() and b.run_sh.name == "run.sh"


@pytest.mark.parametrize("meta, why", [
    ({"name": "x", "version": "v1"}, "no image digest"),
    ({"name": "x", "version": "v1", "digest": "abc"}, "no image digest"),
    ({"name": "x", "digest": DIGEST}, "neither a version nor a uri"),
])
def test_a_backend_installed_by_hand_states_no_run(tmp_path, meta, why):
    with pytest.raises(loadopts.Unstatable, match=why):
        vw.real_backend(_backend_dir(tmp_path, meta))


def test_the_runtime_is_read_from_dpkgs_status_without_dpkg(tmp_path):
    status = tmp_path / "status"
    status.write_text("Package: libze1\nStatus: install ok installed\nVersion: 1.21.9\n"
                      "Description: x\n continued\n\n"
                      "Package: intel-opencl-icd\nStatus: deinstall ok config-files\n"
                      "Version: 25.0\n\n"
                      "Package: libze-intel-gpu1\nStatus: install ok installed\n"
                      "Version: 25.35.35096.9-1\n")
    assert vw.runtime_lines(vw.RUNTIME_PACKAGES, status) == [
        "libze-intel-gpu1 25.35.35096.9-1", "libze1 1.21.9"]
    assert vw.runtime_lines(vw.RUNTIME_PACKAGES, tmp_path / "absent") == []


def test_the_family_is_the_process_and_its_descendants(tmp_path):
    for pid, kids in {10: "11 12", 11: "13", 12: "", 13: ""}.items():
        task = tmp_path / str(pid) / "task" / str(pid)
        task.mkdir(parents=True)
        (task / "children").write_text(kids)
    assert sorted(vw.family(10, tmp_path)) == [10, 11, 12, 13]


class FakeProc:
    def __init__(self, held):
        self.held = held

    def card_mib_by_device(self, pid):
        return self.held.get(pid)


def test_the_device_is_read_from_the_cards_the_family_holds():
    cards = {"0000:03:00.0": "Intel(R) Arc(TM) Pro B70 Graphics"}
    assert vw.device_of([1], cards, FakeProc({1: {}})) == ("cpu", None)
    assert vw.device_of([1, 2], cards, FakeProc({1: {}, 2: {"0000:03:00.0": 900}})) == \
        ("Intel(R) Arc(TM) Pro B70 Graphics", "0000:03:00.0")
    assert vw.device_of([1], cards, FakeProc({1: {None: 5}})) == \
        ("Intel(R) Arc(TM) Pro B70 Graphics", "0000:03:00.0")
    with pytest.raises(loadopts.Unstatable, match="names no device"):
        vw.device_of([1], cards, FakeProc({1: {"0000:04:00.0": 5}}))
    with pytest.raises(loadopts.Unstatable, match="several cards"):
        vw.device_of([1], cards | {"0000:04:00.0": "B"},
                     FakeProc({1: {"0000:03:00.0": 5, "0000:04:00.0": 5}}))
    with pytest.raises(loadopts.Unstatable, match="cannot be read"):
        vw.device_of([1], cards, FakeProc({}))


#: ModelOptions as protojson with proto names, unset fields omitted: P1's form
LOAD = json.dumps({"Model": "bge-m3.gguf", "ModelFile": "/models/bge-m3.gguf",
                   "ModelPath": "/models", "ContextSize": 8192, "Embeddings": True,
                   "RopeFreqBase": 10000.5, "Options": ["pairs:true"],
                   "EnvVars": {"NEO_CACHE_DIR": "/state/neo-cache"}})


def test_the_options_are_canonical_as_rage_writes_them():
    canon = vw.canon_from_protojson(LOAD)
    assert canon.options == {"Model": "bge-m3.gguf", "ModelFile": "${WEIGHTS}",
                             "ModelPath": "${MODELS}", "ContextSize": 8192,
                             "Embeddings": True, "RopeFreqBase": "10000.5",
                             "Options": ["pairs:true"]}
    assert canon.env == {"NEO_CACHE_DIR": "/state/neo-cache"}
    assert canon.weights_path == "/models/bge-m3.gguf"


def test_the_run_is_what_the_wrapper_launched(tmp_path):
    backend = vw.real_backend(_backend_dir(tmp_path))
    canon = vw.canon_from_protojson(LOAD)
    rid, run = vw.state_run(backend=backend, canon=canon,
                            environ={"NEO_CACHE_DIR": "/state/neo-cache"}, device="cpu",
                            pci=None, weights="sha256:" + "11" * 32, runtime=[])
    assert rid == rage_runs.run_id(run)
    assert run["driver"] == "cpu" and run["weights"] == "sha256:" + "11" * 32
    assert run["engine"] == {
        "name": "localai/fake-embed", "build": "v4.11.0", "variant": None, "image": DIGEST,
        "args": [str(backend.run_sh), "--addr", "127.0.0.1:${PORT}"],
        "env": {"NEO_CACHE_DIR": "/state/neo-cache"}, "device": "cpu",
        "options": canon.options}
    with pytest.raises(loadopts.Unstatable, match="NEO_CACHE_DIR"):
        vw.state_run(backend=backend, canon=canon, environ={}, device="cpu", pci=None,
                     weights="sha256:" + "11" * 32, runtime=[])


def test_pairs_are_declared_as_rage_reads_them():
    for options in (["pairs:true"], ["device:xpu", "pairs : true"], ["pairs:false"], [], None):
        assert vw.declares_pairs(options) == pairs.declares_pairs(options)


def test_the_wrapper_venv_pins_the_backends_grpc():
    def pins(path):
        return sorted(line for line in path.read_text().splitlines()
                      if line and not line.startswith("#"))
    assert pins(ROOT / "deploy/vega/requirements.txt") == \
        pins(ROOT / "deploy/localai/backends/requirements-grpc.txt")


# ---- the receipts client against the real intake -------------------------------------

RUN = {"driver": "cpu", "weights": "sha256:" + "11" * 32,
       "engine": {"name": "localai/fake", "build": "v1", "variant": None, "image": DIGEST,
                  "args": [], "env": {}, "device": "cpu", "options": {"M": 1}}}
RUN_ID = rage_runs.run_id(RUN)


def _receipt(i):
    return {"id": f"{i:032x}", "run_id": RUN_ID, "input_digest": "sha256:" + "33" * 32,
            "vector_digest": "sha256:" + "44" * 32}


def test_a_receipt_is_delivered_with_its_run_told_first(vega):
    c = vw.ReceiptClient(vega.socket, timeout_s=2)
    c.deliver(_receipt(1), RUN_ID, RUN)
    c.deliver(_receipt(2), RUN_ID, RUN)
    assert vega.runs.get(RUN_ID) == RUN
    assert len(vega.receipts.get([_receipt(1)["id"], _receipt(2)["id"]])) == 2
    c.close()


def test_a_service_that_forgot_the_run_is_told_again(vega):
    c = vw.ReceiptClient(vega.socket, timeout_s=2)
    c.publish(RUN_ID, RUN)
    from app.rage.store import RunStore
    vega.runs._known.clear()                 # a service with a fresh state directory
    vega.runs.store = RunStore(vega.socket.parent / "fresh.sqlite")
    c.deliver(_receipt(5), RUN_ID, RUN)
    assert vega.receipts.get([_receipt(5)["id"]]) and vega.runs.known(RUN_ID)


def test_no_service_is_unavailable_within_the_bound(tmp_path):
    c = vw.ReceiptClient(tmp_path / "nobody.sock", timeout_s=0.3)
    t0 = time.monotonic()
    with pytest.raises(vw.Unavailable, match="no acknowledgement within 0.3 s"):
        c.deliver(_receipt(1), RUN_ID, RUN)
    assert time.monotonic() - t0 < 1.5


def test_a_conflicting_receipt_is_refused_not_retried(vega):
    c = vw.ReceiptClient(vega.socket, timeout_s=2)
    c.deliver(_receipt(1), RUN_ID, RUN)
    with pytest.raises(vw.ReceiptRefused, match="one-time"):
        c.deliver(_receipt(1) | {"vector_digest": "sha256:" + "55" * 32}, RUN_ID, RUN)


def test_a_service_that_hangs_is_unavailable_within_the_bound(tmp_path):
    import socket
    path = tmp_path / "hang.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    server.listen(4)
    held = []
    threading.Thread(target=lambda: held.append(server.accept()), daemon=True).start()
    c = vw.ReceiptClient(path, timeout_s=0.4)
    t0 = time.monotonic()
    with pytest.raises(vw.Unavailable):
        c.deliver(_receipt(1), RUN_ID, RUN)
    assert time.monotonic() - t0 < 1.5
    server.close()
