"""`python -m app.vega` — the receipts service on its own (docs/notes/vega.md).

Identity from the environment (tier 1):

    VEGA_SOCKET     the intake's unix socket, on the dataset LocalAI's
                    container mounts too. REQUIRED
    VEGA_STATE_DIR  where the run descriptions are kept          (/state)
    VEGA_PORT       the read-only HTTP API                        (8090)

On the node it runs inside the louise-rage app (operator, 2026-10-08), which
mounts `service.router` and starts a `service.Intake`; this entry point is the
same service for a container of its own and for the tests.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import uvicorn

from .store import RunStore
from .service import Intake, ReceiptStore, Runs, create_app


def main() -> int:
    env = os.environ
    socket_path = env.get("VEGA_SOCKET", "")
    if not socket_path:
        print("[vega] refusing to start: VEGA_SOCKET must name the intake's unix socket",
              file=sys.stderr, flush=True)
        return 2
    try:
        port = int(env.get("VEGA_PORT", "8090"))
    except ValueError:
        print("[vega] refusing to start: VEGA_PORT is a port number", file=sys.stderr,
              flush=True)
        return 2
    receipts = ReceiptStore()
    runs = Runs(RunStore(Path(env.get("VEGA_STATE_DIR", "/state")) / "vega-runs.sqlite"))
    app = create_app(receipts, runs, Intake(Path(socket_path), receipts, runs))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
