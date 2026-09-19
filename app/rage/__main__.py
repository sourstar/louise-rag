"""`python -m app.rage` — boot the front and the process runner under it.

Identity comes from the environment (tier 1; docs/notes/rage.md §8, §12):

    RAGE_IMAGE          sha256:… — the digest the deployment pins. REQUIRED:
                        a tag, or nothing, refuses to start (09 §9.12)
    RAGE_CONFIG         rage.toml                      (/config/rage.toml)
    RAGE_STATE_DIR      the run table, the generated yaml        (/state)
    RAGE_PORT           the published port                         (8080)
    RAGE_SWAP_PORT      llama-swap's, on 127.0.0.1 only            (9090)
    LLAMA_SWAP_BIN      the process runner      (/usr/local/bin/llama-swap)
    RAGE_FREQ_DIR       the card's frequency directory; unset = no clock
    ADMIN_TOKEN_FILE    the operator's token, by path; unset = routes 401

A refusal exits 2 and says which rule stood in the way (07 §7.7's exit
codes). llama-swap dying takes the front down with it: a front that answers
while nothing can run a model would turn every request into a slow failure,
and the container's restart policy is the recovery.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
from pathlib import Path

import uvicorn

from . import model, runs
from .clock import Clock
from .front import create_app, read_admin_token
from .placement import Placement
from .store import RunStore
from .swap import Swap


def main() -> int:
    env = os.environ
    try:
        image = runs.boot_image(env)
        token = read_admin_token(env)
        state = Path(env.get("RAGE_STATE_DIR", "/state"))
        swap_port = int(env.get("RAGE_SWAP_PORT", "9090"))
        swap = Swap(f"http://127.0.0.1:{swap_port}")
        placement = Placement(config_path=Path(env.get("RAGE_CONFIG", "/config/rage.toml")),
                              yaml_path=state / "llama-swap.yaml", image=image,
                              runner=swap, store=RunStore(state / "rage.sqlite"))
    except (runs.BootError, model.RageConfigError, RuntimeError, ValueError) as e:
        print(f"[rage] refusing to start: {e}", file=sys.stderr, flush=True)
        return 2

    freq = env.get("RAGE_FREQ_DIR")
    clock = Clock(Path(freq) if freq else None)

    # As PID 1 the kernel gives SIGTERM no default action, so a stop that
    # lands before uvicorn has installed its own handlers would be ignored
    # and end in SIGKILL at the grace period's end (medcpt-rerank, each NAS
    # poweroff of 2026-09-16 and -17). uvicorn replaces this while it serves
    # and puts it back when it stops.
    def _stop(*_: object) -> None:
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _stop)

    runner: subprocess.Popen | None = None
    try:
        runner = subprocess.Popen([env.get("LLAMA_SWAP_BIN", "/usr/local/bin/llama-swap"),
                                   "-config", str(placement.yaml_path),
                                   "-listen", f"127.0.0.1:{swap_port}", "-watch-config"])
        started = runner

        def _runner_died() -> None:
            code = started.wait()
            print(f"[rage] llama-swap exited with {code}; stopping", file=sys.stderr,
                  flush=True)
            os.kill(os.getpid(), signal.SIGTERM)

        threading.Thread(target=_runner_died, daemon=True).start()
        uvicorn.run(create_app(placement, swap, clock, token), host="0.0.0.0",
                    port=int(env.get("RAGE_PORT", "8080")), log_level="warning")
    finally:
        clock.close()                           # the card gets its clock back
        if runner is not None and runner.poll() is None:
            runner.terminate()                  # it stops its children in turn
            try:
                runner.wait(timeout=45)
            except subprocess.TimeoutExpired:
                runner.kill()
    return 0


if __name__ == "__main__":
    sys.exit(main())
