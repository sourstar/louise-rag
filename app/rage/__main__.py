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

    RAGE_RUNNER         `llama-swap` (the default, until migration step 15's
                        cut-over) or `localai`: LocalAI runs the models and RAGE
                        states the run of every answer (rage-over-localai.md)
    LOCALAI_MODELS_DIR  with `localai`: the models' YAML, which PUT and DELETE
                        write; the weights are mounted read-only inside it
    LOCALAI_PORT        with `localai`: LocalAI's, on 127.0.0.1 only    (8081)
    LOCALAI_BIN         with `localai`: the runner to start      (/usr/bin/local-ai)

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


#: Set on the LocalAI RAGE starts, whatever the deployment says: with them
#: off, LocalAI fills a model's unset options from the VRAM it detects at
#: each load and from its file (rage-over-localai §10), and a run changes
#: without anyone editing a model.
LOCALAI_PINNED_ENV = {"LOCALAI_DISABLE_HARDWARE_DEFAULTS": "true",
                      "LOCALAI_DISABLE_GUESSING": "true"}


def localai_child(env: dict[str, str] | os._Environ, *, state: Path, models_dir: Path,
                  port: int) -> tuple[list[str], dict[str, str]]:
    """LocalAI's argument vector and environment: on loopback with no key
    (§8.4 — it takes keys only from an environment value, which invariant 8
    rules out; RAGE's front is the door), the models directory as given,
    `HF_HOME` outside it (LocalAI points it at the models when unset, and
    they are read-only), and the §10 switches forced on."""
    child = {k: v for k, v in env.items() if k not in ("LOCALAI_API_KEY", "API_KEY")}
    child |= {"LOCALAI_ADDRESS": f"127.0.0.1:{port}", "LOCALAI_MODELS_PATH": str(models_dir),
              **LOCALAI_PINNED_ENV}
    child.setdefault("HF_HOME", str(state / "hf-home"))
    return [env.get("LOCALAI_BIN", "/usr/bin/local-ai"), "run"], child


def main_localai(env: dict[str, str] | os._Environ, image: str, token: str | None) -> int:
    from .front_localai import create_localai_app
    from .localai import LocalAI
    from .localai_config import load as load_localai_config
    from .observer import Observer

    try:
        state = Path(env.get("RAGE_STATE_DIR", "/state"))
        models_dir = Path(env["LOCALAI_MODELS_DIR"]) if env.get("LOCALAI_MODELS_DIR") else None
        if models_dir is None:
            raise RuntimeError("LOCALAI_MODELS_DIR must name LocalAI's models directory")
        cfg = load_localai_config(Path(env.get("RAGE_CONFIG", "/config/rage.toml")))
        port = int(env.get("LOCALAI_PORT", "8081"))
    except (model.RageConfigError, RuntimeError, ValueError) as e:
        print(f"[rage] refusing to start: {e}", file=sys.stderr, flush=True)
        return 2
    client = LocalAI(f"http://127.0.0.1:{port}")
    observer = Observer(cfg, client, RunStore(state / "rage.sqlite"), image=image)
    freq = env.get("RAGE_FREQ_DIR")
    clock = Clock(Path(freq) if freq else None)

    def _stop(*_: object) -> None:
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _stop)
    runner: subprocess.Popen | None = None
    try:
        argv, child_env = localai_child(env, state=state, models_dir=models_dir, port=port)
        runner = subprocess.Popen(argv, env=child_env)
        started = runner

        def _runner_died() -> None:
            code = started.wait()
            print(f"[rage] LocalAI exited with {code}; stopping", file=sys.stderr, flush=True)
            os.kill(os.getpid(), signal.SIGTERM)

        threading.Thread(target=_runner_died, daemon=True).start()
        uvicorn.run(create_localai_app(observer, client, clock, token, models_dir=models_dir),
                    host="0.0.0.0", port=int(env.get("RAGE_PORT", "8080")),
                    log_level="warning")
    finally:
        clock.close()                           # the card gets its clock back
        if runner is not None and runner.poll() is None:
            runner.terminate()                  # it stops its backends in turn
            try:
                runner.wait(timeout=45)
            except subprocess.TimeoutExpired:
                runner.kill()
    return 0


def main() -> int:
    env = os.environ
    if env.get("RAGE_RUNNER", "llama-swap") == "localai":
        try:
            image = runs.boot_image(env)
            token = read_admin_token(env)
        except (runs.BootError, RuntimeError) as e:
            print(f"[rage] refusing to start: {e}", file=sys.stderr, flush=True)
            return 2
        return main_localai(env, image, token)
    if env.get("RAGE_RUNNER", "llama-swap") != "llama-swap":
        print(f"[rage] refusing to start: RAGE_RUNNER is llama-swap or localai, got "
              f"{env['RAGE_RUNNER']!r}", file=sys.stderr, flush=True)
        return 2
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
