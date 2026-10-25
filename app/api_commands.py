"""Every command is one route, named by the tree (07 §7.4; operator,
2026-10-25: "Http api, I choose A").

    GET  /api/<surface>/<noun>/<verb>   `show` and `list`, answered at once
    POST /api/<surface>/<noun>/<verb>   everything else, queued for the worker

The route is built from `louise`'s own argparse tree, so a command added
there is a route without a line here. The body's keys (a GET's query
parameters) are the command's flags without their dashes, and its
positional arguments by name; the response is the command's `--json`
output. The exit code is the status — 0 → 200, 1 → 500, 2 → 409, 3 → 503
— and the body carries `exit` and the message.

A POST is queued in `state/node.sqlite` (app/jobs.py, COMMAND) and run by
the worker, which holds the node's key and the write locks: the web
process signs nothing (07 §7.8), and a command may outlive any request.
It answers 202; `GET /api/jobs/<id>` follows it, and its result carries
the same `exit`, `status`, `output` and `message`. An explicit route for
the same path (`/api/share/fetch`, `/api/hold/collection/create`, …) is
registered before this one and answers instead.

Not routed, each for its reason: `node start` (the daemon itself), `ask`
(its own routes answer at once under the user's token, 05 §5.4), `user`
(prints a token once, which a job row would keep — invariant 8; the
`/admin/api/users` routes), `dev` (operator instruments stay on the
command line, CLAUDE.md RULE 2).

The command runs as `python -m app.louise …` in a child process — the CLI
exactly as typed, never a re-implementation of it; argv is a list, never a
shell line. Standard alternative, written down: an OpenAPI schema
generated per command (FastAPI models from the parser) would document each
route's body; it pays once a client wants typed bodies, and costs a model
per command kept in step with the tree.
"""
from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from . import auth

STATUS = {0: 200, 1: 500, 2: 409, 3: 503}
READ_VERBS = ("show", "list")
NOT_ROUTED = {("node", "start"): "the daemon itself is started on the node",
              ("ask",): "`ask` has its own routes (05 §5.4)",
              ("user",): "users are managed at /admin/api/users (07 §7.4)",
              ("dev",): "operator instruments stay on the command line (RULE 2)"}
#: A read answered at once still has a bound (CLAUDE.md: no unbounded wait).
READ_TIMEOUT_S = 120.0
#: A command sent SIGTERM finishes its unit (07 §7.7) — bounded in time
#: (invariant 2) — or is killed after this.
KILL_AFTER_S = 60.0
#: What a job row keeps of a command's output.
MAX_OUTPUT_CHARS = 1 << 20
ROOT = Path(__file__).resolve().parent.parent

router = APIRouter(dependencies=[Depends(auth.require_admin)])


class Refused(ValueError):
    """The request names no command, or flags the command does not take."""
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _raise(message: str):
    raise Refused(400, message)


def _parser() -> argparse.ArgumentParser:
    """`louise`'s tree, every level made to raise instead of exiting."""
    from . import louise
    top = louise.build_parser()
    stack = [top]
    while stack:
        p = stack.pop()
        p.error = _raise                                  # type: ignore[method-assign]
        for a in p._actions:
            if isinstance(a, argparse._SubParsersAction):
                stack.extend(a.choices.values())
    return top


def resolve(words: list[str], top: argparse.ArgumentParser | None = None
            ) -> tuple[tuple[str, ...], argparse.ArgumentParser]:
    """The command `words` name, and its parser; Refused(404) otherwise."""
    p = top or _parser()
    path: list[str] = []
    for w in words:
        subs = next((a for a in p._actions if isinstance(a, argparse._SubParsersAction)), None)
        if subs is None or w not in subs.choices:
            raise Refused(404, f"no command `louise {' '.join(path + [w])}` (07 §7.3)")
        path.append(w)
        p = subs.choices[w]
    if any(isinstance(a, argparse._SubParsersAction) for a in p._actions) or not path:
        raise Refused(404, f"`louise {' '.join(path)}` is not a whole command (07 §7.3)")
    for prefix, why in NOT_ROUTED.items():
        if tuple(path[:len(prefix)]) == prefix:
            raise Refused(404, f"`louise {' '.join(path)}` is not routed: {why}")
    return tuple(path), p


def _names(a: argparse.Action) -> set[str]:
    return {a.dest} | {o.lstrip("-").replace("-", "_") for o in a.option_strings}


def _scalar(key: str, v: Any) -> str:
    if isinstance(v, bool) or not isinstance(v, (str, int, float)):
        raise Refused(400, f"{key}: a string or a number, not {type(v).__name__}")
    s = str(v)
    if s.startswith("-"):
        raise Refused(400, f"{key}: a value may not begin with '-'")
    return s


def argv_for(path: tuple[str, ...], parser: argparse.ArgumentParser,
             values: dict[str, Any]) -> list[str]:
    """The command line `values` describe, checked by the command's own
    parser; Refused(400) for a key it does not take or a value it refuses."""
    given = {k.replace("-", "_"): v for k, v in values.items()}
    actions = [a for a in parser._actions if not isinstance(a, argparse._HelpAction)]
    known = set().union(*(_names(a) for a in actions)) if actions else set()
    unknown = sorted(set(given) - known)
    if unknown:
        raise Refused(400, f"`louise {' '.join(path)}` takes no {', '.join(unknown)}; "
                           f"it takes {', '.join(sorted(a.dest for a in actions))}")
    options: list[str] = []
    positionals: list[str] = []
    for a in actions:
        key = next((n for n in _names(a) if n in given), None)
        if key is None:
            continue
        v = given[key]
        if not a.option_strings:
            vals = v if isinstance(v, list) else [v]
            positionals += [_scalar(key, x) for x in vals]
            continue
        flag = next(o for o in a.option_strings if o.startswith("--"))
        if isinstance(a, (argparse._StoreTrueAction, argparse._StoreFalseAction)):
            if not isinstance(v, bool):
                raise Refused(400, f"{key}: true or false")
            if v:
                options.append(flag)
            continue
        vals = v if isinstance(v, list) else [v]
        if isinstance(a, argparse._AppendAction):
            for x in vals:
                options += [flag, _scalar(key, x)]
        elif a.nargs in ("*", "+") or isinstance(a.nargs, int):
            options += [flag, *(_scalar(key, x) for x in vals)]
        else:
            if isinstance(v, list):
                raise Refused(400, f"{key}: one value")
            options += [flag, _scalar(key, v)]
    takes_json = any("--json" in a.option_strings for a in actions)
    if takes_json and "--json" not in options:
        options.append("--json")
    argv = [*path, *options, *positionals]
    parser.parse_args(argv[len(path):])           # the command's own refusal, as 400
    return argv


def run(argv: list[str], timeout: float | None = None, stop=None) -> dict[str, Any]:
    """`louise <argv>` in a child process: `exit`, `status`, `output` (the
    `--json` object where the command printed one) and `message` (stderr's
    tail). With `stop`, polled: the child is sent SIGTERM — it finishes
    its unit and exits 130 (07 §7.7) — and `interrupted` is set."""
    import time
    proc = subprocess.Popen([sys.executable, "-m", "app.louise", *argv], cwd=ROOT,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    deadline = None if timeout is None else time.monotonic() + timeout
    interrupted = False
    while True:
        try:
            out, err = proc.communicate(timeout=0.5)
            break
        except subprocess.TimeoutExpired:
            if stop is not None and stop.is_set() and not interrupted:
                proc.terminate()
                interrupted = True
                deadline = time.monotonic() + KILL_AFTER_S
                timeout = KILL_AFTER_S
            if deadline is not None and time.monotonic() > deadline:
                proc.kill()
                out, err = proc.communicate()
                return {"exit": 3, "status": 503, "output": None,
                        "message": f"no answer in {timeout:.0f} s",
                        **({"interrupted": True} if interrupted else {})}
    return {"exit": proc.returncode, "status": STATUS.get(proc.returncode, 500),
            "output": _output(out), "message": err.strip()[-4000:],
            **({"interrupted": True} if interrupted else {})}


def _output(out: str) -> Any:
    text = out.strip()
    if len(text) > MAX_OUTPUT_CHARS:
        return {"truncated": True, "text": text[:MAX_OUTPUT_CHARS]}
    try:
        return json.loads(text) if text else None
    except ValueError:
        lines = [ln for ln in text.splitlines() if ln.strip()]
        try:
            return [json.loads(ln) for ln in lines]
        except ValueError:
            return text


@router.api_route("/api/{words:path}", methods=["GET", "POST"])
async def command(words: str, request: Request):
    try:
        path, parser = resolve([w for w in words.split("/") if w])
        method = "GET" if path[-1] in READ_VERBS else "POST"
        if request.method != method:
            return JSONResponse({"detail": f"`louise {' '.join(path)}` is {method} (07 §7.4)"},
                                status_code=405, headers={"Allow": method})
        if method == "GET":
            q = request.query_params
            values: dict[str, Any] = {k: (q.getlist(k) if len(q.getlist(k)) > 1 else q[k])
                                      for k in q.keys()}
            values = {k: (True if v == "true" else False if v == "false" else v)
                      for k, v in values.items()}
        else:
            raw = await request.body()
            values = json.loads(raw) if raw.strip() else {}
            if not isinstance(values, dict):
                raise Refused(400, "the body is a JSON object of the command's flags")
        argv = argv_for(path, parser, values)
    except Refused as e:
        return JSONResponse({"detail": str(e)}, status_code=e.status)
    except ValueError as e:
        return JSONResponse({"detail": f"the body is not JSON: {e}"}, status_code=400)
    cli = shlex.join(["louise", *argv])
    if method == "GET":
        from starlette.concurrency import run_in_threadpool
        got = await run_in_threadpool(run, argv, READ_TIMEOUT_S)
        return JSONResponse({**got, "cli": cli}, status_code=got["status"])
    from . import jobs
    job_id = jobs.submit(jobs.COMMAND, {"argv": argv, "command": " ".join(path)})
    return JSONResponse({"id": job_id, "job": f"/api/jobs/{job_id}", "cli": cli},
                        status_code=202)
