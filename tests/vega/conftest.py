"""What VeGa's tests share: the receipts intake on a thread of its own, and
LocalAI's gRPC stubs generated from its backend.proto (58830f7, which is
v4.11.0), for the tests that run real gRPC.

THE gRPC TESTS NEED grpcio, protobuf and grpcio-tools, which the development
image does not carry (deploy/localai/backends/requirements-grpc.txt pins
them for the backends' venvs): without them they are skipped, and say so.
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.vega.store import RunStore
from app.vega.service import Intake, ReceiptStore, Runs

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures" / "vega"
WRAPPER = ROOT / "deploy" / "vega" / "vega_wrapper.py"


def load_wrapper():
    name = "vega_wrapper_under_test"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, WRAPPER)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module                  # its dataclasses look themselves up
        spec.loader.exec_module(module)
    return sys.modules[name]


class IntakeThread:
    """VeGa's intake on an event loop of its own, as louise-vega runs it."""

    def __init__(self, path: Path, receipts: ReceiptStore, runs: Runs) -> None:
        self.intake = Intake(path, receipts, runs, log=lambda _m: None)
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)

    def start(self) -> "IntakeThread":
        self.thread.start()
        asyncio.run_coroutine_threadsafe(self.intake.start(), self.loop).result(10)
        return self

    def stop(self) -> None:
        asyncio.run_coroutine_threadsafe(self.intake.close(), self.loop).result(10)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(10)


@pytest.fixture
def vega(tmp_path):
    """A receipts service: its stores, its intake on `tmp_path/r.sock`."""
    receipts = ReceiptStore()
    runs = Runs(RunStore(tmp_path / "vega-runs.sqlite"))
    intake = IntakeThread(tmp_path / "r.sock", receipts, runs).start()
    yield SimpleNamespace(receipts=receipts, runs=runs, intake=intake,
                          socket=tmp_path / "r.sock")
    intake.stop()


@pytest.fixture(scope="session")
def stubs(tmp_path_factory):
    pytest.importorskip("grpc", reason="grpcio is not in this image (VeGa's gRPC tests)")
    pytest.importorskip("google.protobuf", reason="protobuf is not in this image")
    protoc = pytest.importorskip("grpc_tools.protoc",
                                 reason="grpcio-tools is not in this image (stubs)")
    import grpc_tools
    out = tmp_path_factory.mktemp("stubs")
    include = Path(grpc_tools.__file__).parent / "_proto"
    rc = protoc.main(["protoc", f"-I{FIXTURES}", f"-I{include}", f"--python_out={out}",
                      f"--grpc_python_out={out}", str(FIXTURES / "backend.proto")])
    assert rc == 0, "protoc failed on LocalAI's backend.proto"
    sys.path.insert(0, str(out))
    import backend_pb2
    import backend_pb2_grpc
    return SimpleNamespace(pb=backend_pb2, rpc=backend_pb2_grpc, dir=out)
