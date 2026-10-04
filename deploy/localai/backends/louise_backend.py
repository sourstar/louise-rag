"""What the platform's LocalAI backends share (migration step 15e-2;
docs/notes/rage-over-localai.md §7): the gRPC server LocalAI talks to, and the
reading of a model's load call.

    louise-dar-legal-it   Embedding   deploy/dar-legal-it/server.py
    louise-medcpt-rerank  Rerank      deploy/medcpt-rerank/server.py
    louise-madlad         Predict     deploy/madlad-translate/server.py

Each one wraps its server's model code, copied beside it in the image under the
server's name, and rewrites none of it: one source per model, whether it runs
as an app of its own or under LocalAI.

LocalAI starts a backend as `run.sh --addr 127.0.0.1:<port>` and names the
model in `LoadModel`: `ModelFile` is the weights (a directory, for these),
`ContextSize` and `NBatch` are LocalAI's own fields, used for what they mean,
and `Options` carries `key:value` strings for what LocalAI has no field for.
All of it is in the run RAGE states (09 §9.3), so a backend takes its settings
from the load call, and from an environment variable only where the model's
`env:` sets it — which is in the run too, as the engine's `env`.

NOTHING IS FETCHED (CLAUDE.md invariant 5): the Hugging Face hub is switched
off before any model library is imported, so weights that are not on disk are
an error naming the path, never a download outside the transfer budget.

This module imports no model library and no gRPC until `main` runs, so the
backends' logic is tested with stand-in messages (tests/test_localai_backends.py).
"""
from __future__ import annotations

import argparse
import os
import signal
import sys
import threading
from concurrent import futures
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

#: set before any model library is imported
OFFLINE = {"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"}
#: RAGE's words for a device; each backend maps them to its library's
DEVICES = ("xpu", "cpu")
#: as LocalAI's own Python backends
MAX_MESSAGE = 50 * 1024 * 1024
#: a request in progress when LocalAI stops the backend finishes within this
GRACE_S = 30.0


class LoadError(ValueError):
    """The load call cannot be honoured; LocalAI is told `success=False`."""


def device(value: str) -> str:
    value = value.strip()
    if value not in DEVICES:
        raise ValueError(f"one of {', '.join(DEVICES)}")
    return value


def positive(value: str) -> int:
    n = int(value.strip())
    if n <= 0:
        raise ValueError("a positive integer")
    return n


def options(given: Iterable[str], known: Mapping[str, Callable[[str], Any]],
            defaults: Mapping[str, Any]) -> dict[str, Any]:
    """The model's `options:` strings, typed. An unknown key, a key given
    twice or a value its type refuses is a LoadError: a misspelt option would
    otherwise load with a default while the run names the misspelling."""
    out, seen = dict(defaults), set()
    for item in given:
        key, sep, value = item.partition(":")
        key = key.strip()
        if not sep or key not in known:
            raise LoadError(f"option {item!r}: this backend reads "
                            f"{', '.join(f'{k}:<value>' for k in sorted(known))}")
        if key in seen:
            raise LoadError(f"option {key!r} is given twice")
        seen.add(key)
        try:
            out[key] = known[key](value)
        except ValueError as e:
            raise LoadError(f"option {item!r}: {e}") from None
    return out


def weights(model_file: str) -> Path:
    """The weights LocalAI names, which must be on disk (invariant 5)."""
    if not model_file:
        raise LoadError("the load call names no weights (ModelFile)")
    path = Path(model_file)
    if not path.exists():
        raise LoadError(f"the weights are absent: {path}. Nothing is fetched here; "
                        f"place them and load again")
    return path


def loaded(pb: Any, message: str) -> Any:
    return pb.Result(success=True, message=message)


def refused(pb: Any, error: BaseException) -> Any:
    """A load that failed, as LocalAI shows it: the reason, never a traceback."""
    text = str(error) if isinstance(error, LoadError) else f"{type(error).__name__}: {error}"
    print(f"load refused: {text}", file=sys.stderr, flush=True)
    return pb.Result(success=False, message=text)


def serve(servicer: type, address: str, workers: int = 4) -> None:
    """`servicer(pb, status)` behind LocalAI's gRPC service, until SIGTERM or
    SIGINT; the request in progress then finishes (GRACE_S) and it exits 0.

    LocalAI's own interceptors are installed (`grpc_auth`, copied from the
    LocalAI commit the image is built from): the model-identity check, its
    bearer token when LOCALAI_GRPC_AUTH_TOKEN is set, and the watcher that
    ends a backend whose LocalAI died without stopping it — a process left
    holding the card."""
    import grpc
    import backend_pb2
    import backend_pb2_grpc
    from grpc_auth import get_auth_interceptors

    bound = type("BackendServicer", (servicer, backend_pb2_grpc.BackendServicer), {})
    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=workers),
        options=[("grpc.max_message_length", MAX_MESSAGE),
                 ("grpc.max_send_message_length", MAX_MESSAGE),
                 ("grpc.max_receive_message_length", MAX_MESSAGE)],
        interceptors=get_auth_interceptors())
    backend_pb2_grpc.add_BackendServicer_to_server(bound(backend_pb2, grpc.StatusCode), server)
    server.add_insecure_port(address)
    server.start()
    print(f"{servicer.__module__}: listening on {address}", file=sys.stderr, flush=True)
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    stop.wait()
    server.stop(GRACE_S).wait()


def main(servicer: type, argv: list[str] | None = None) -> None:
    os.environ.update(OFFLINE)
    p = argparse.ArgumentParser(allow_abbrev=False)
    p.add_argument("--addr", required=True, help="where LocalAI reaches it: host:port")
    serve(servicer, p.parse_args(sys.argv[1:] if argv is None else argv).addr)
