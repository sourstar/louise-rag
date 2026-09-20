"""Start one of the platform's own model servers from an argument vector.

    python -m servers.launch <server> --weights PATH --device DEVICE \
                                      --host HOST --port PORT

WHY ARGUMENTS. RAGE states a run from the engine's `args` (docs/09-formats.md
§9.3): the argument vector, with the weights written `${WEIGHTS}` and the
port `${PORT}` and nothing else that names the host, and an `env` that
carries no placeholder — llama-swap expands `${PORT}` in a command and not
in an environment. The three servers under deploy/ read their settings from
the environment, which is right for an app of their own and wrong here. So
this launcher turns the arguments into the variables each server already
reads, and runs the server IN THIS PROCESS: `/proc/<pid>/cmdline` is then
the template RAGE checks the process against.

    dnabert2   deploy/dnabert2/server.py        FastAPI, under uvicorn
    madlad     deploy/madlad-translate/server.py   its own `main()`
    medcpt     deploy/medcpt-rerank/server.py      its own `main()`
    dar_legal_it  deploy/dar-legal-it/server.py     its own `main()`

NOTHING IS FETCHED (CLAUDE.md invariant 5; docs/notes/rage.md §10). The
weights are a path that must exist, and the Hugging Face hub is switched off
before any library is imported: a model that is not on disk is an error
that names the path, never a download outside the transfer budget.

This module imports no model library; the servers do, after it has set
their environment.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Mapping, Sequence

#: server -> the variable that names its model
SERVERS: Mapping[str, str] = {"dnabert2": "MODEL_ID", "madlad": "MODEL_ID",
                              "dar_legal_it": "MODEL_DIR",
                              "medcpt": "MODEL_DIR"}
#: what each server calls a device, by RAGE's name for it
DEVICE_WORDS: Mapping[str, Mapping[str, str]] = {
    "dnabert2": {"xpu": "xpu", "cpu": "cpu"},
    "madlad": {"xpu": "xpu", "cpu": "cpu"},
    "dar_legal_it": {"xpu": "xpu", "cpu": "cpu"},
    "medcpt": {"xpu": "GPU", "cpu": "CPU"},          # OpenVINO's own names
}


class LaunchError(SystemExit):
    def __init__(self, message: str) -> None:
        print(f"servers.launch: {message}", file=sys.stderr, flush=True)
        super().__init__(2)


def parse(argv: Sequence[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m servers.launch", allow_abbrev=False)
    p.add_argument("server", choices=sorted(SERVERS))
    p.add_argument("--weights", required=True,
                   help="the model on disk: a file or a directory, never a hub id")
    p.add_argument("--device", required=True, choices=["xpu", "cpu"])
    p.add_argument("--host", required=True)
    p.add_argument("--port", required=True, type=int)
    return p.parse_args(list(argv))


def environment(args: argparse.Namespace) -> dict[str, str]:
    """The variables the chosen server reads, from the arguments alone."""
    if not Path(args.weights).exists():
        raise LaunchError(f"the weights are absent: {args.weights}. Nothing is fetched "
                          f"here; place them and start again")
    return {SERVERS[args.server]: args.weights,
            "DEVICE": DEVICE_WORDS[args.server][args.device],
            "HOST": args.host, "PORT": str(args.port),
            "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"}


def main(argv: Sequence[str] | None = None) -> None:
    args = parse(sys.argv[1:] if argv is None else argv)
    os.environ.update(environment(args))
    if args.server == "dnabert2":
        import uvicorn
        uvicorn.run("servers.dnabert2:app", host=args.host, port=args.port,
                    log_level="warning")
        return
    import importlib
    importlib.import_module(f"servers.{args.server}").main()


if __name__ == "__main__":
    main()
