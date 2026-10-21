"""ONE LocalAI backend for the platform's Python models (operator, 2026-10-17:
at most one backend per engine; docs/notes/backend-consolidation.md). It
replaces louise-dar-legal-it, louise-madlad, louise-medcpt-article-ov and
louise-medcpt-rerank, which needed four venvs for one set of libraries: the
four run in one, with the same results (measured, the note's table).

THE MODEL'S YAML SAYS WHICH MODEL IT IS, with one more option:

    task:dar-legal-it     Embedding               torch, sentence-transformers
    task:madlad           Predict (a JSON batch)  torch, transformers
    task:medcpt-article   Embedding (text pairs)  OpenVINO
    task:medcpt-rerank    Rerank                  OpenVINO
    task:jina-v3          Embedding               torch (wikidata's encoder)

Each task IS the backend it replaces: its backend.py, copied beside this file
under the module name TASKS gives (the Dockerfile), unchanged. Every other
option goes to it as before, so a model's settings mean what they meant.
LocalAI starts one backend process per model, so a process serves one task;
asking it to load another is refused, never a second model on the card.
"""
from __future__ import annotations

import importlib
import os
from typing import Any, Callable, Mapping

import louise_backend as lb

#: the task's name -> the module holding its Servicer
TASKS = {"dar-legal-it": "task_dar_legal_it",
         "madlad": "task_madlad",
         "medcpt-article": "task_medcpt_article",
         "medcpt-rerank": "task_medcpt_rerank",
         "jina-v3": "task_jina_v3"}
TASK = "task"
#: the calls LocalAI may make of a model; a task answers those it defines
CALLS = ("Embedding", "Predict", "Rerank")

#: CLAUDE.md invariant 12 FAIL-CLOSED (operator, 2026-10-20: "a backend that
#: does not produce bit exact results upon reprocessing the same data cannot be
#: used under any circumstances"). A torch task loads only with torch's
#: deterministic switch in the model's env — each task's model code reads it
#: (torch.use_deterministic_algorithms, oneDNN's deterministic primitives:
#: DAR-legal-it was 1/4 bit-identical against itself without it, jina-v3 went
#: from 204/512 to 512/512 with it) — and an OpenVINO task, which offers no
#: deterministic mode, does not load at all. The rule of VeGa's wrapper
#: (deploy/vega/vega_wrapper.py `determinism_lacks`), here too for a model the
#: wrapper is not in front of.
DETERMINISM_ENV = "LOUISE_TORCH_DETERMINISTIC"
TORCH_TASKS = frozenset({"dar-legal-it", "madlad", "jina-v3"})
#: an OpenVINO task loads only MEASURED bit-exact on this very tree: the model's
#: env names the tree (its directory) the measurement ran on — VeGa's
#: wrapper's MEASURED_ENV, the same rule here for a model it is not in front of
MEASURED_ENV = "LOUISE_BITEXACT_MEASURED"
MEASURED_TASKS = frozenset({"medcpt-article", "medcpt-rerank"})
TREE = os.path.basename(os.path.dirname(os.path.abspath(__file__)))


def determinism_refusal(task: str, environ: Mapping[str, str], tree: str = TREE) -> str | None:
    """Why `task` may not load under `environ` from the tree `tree` — None when it may."""
    if task in MEASURED_TASKS and environ.get(MEASURED_ENV, "").strip() == tree:
        return None
    if task not in TORCH_TASKS:
        return (f"{TASK}:{task} runs on OpenVINO, which offers no deterministic mode (CLAUDE.md "
                f"invariant 12), and is not measured bit-exact on this tree ({tree}): a backend "
                f"whose answers are not shown to come back bit for bit is not loaded; measure it, "
                f"then set {MEASURED_ENV}={tree} in the model's env")
    if environ.get(DETERMINISM_ENV, "").strip() != "1":
        return (f"{TASK}:{task} loads only with {DETERMINISM_ENV}=1 in the model's env (CLAUDE.md "
                f"invariant 12): torch's defaults do not give back their own bytes")
    return None


def split_task(options: list[str]) -> tuple[str, list[str]]:
    """(the task, the options left for it). Exactly one `task:`, a known one."""
    named = [i for i, o in enumerate(options) if o.partition(":")[0].strip() == TASK]
    if len(named) != 1:
        raise lb.LoadError(f"the model's options name {'no' if not named else 'several'} "
                           f"{TASK}: one of {', '.join(f'{TASK}:{t}' for t in TASKS)}")
    (at,) = named
    task = options[at].partition(":")[2].strip()
    if task not in TASKS:
        raise lb.LoadError(f"option {options[at]!r}: this backend runs "
                           f"{', '.join(sorted(TASKS))}")
    return task, options[:at] + options[at + 1:]


class _Load:
    """The load call as the task sees it: the same, less its `task:`."""

    def __init__(self, request: Any, options: list[str]) -> None:
        self._request, self.Options = request, options

    def __getattr__(self, name: str) -> Any:
        return getattr(self._request, name)


class Servicer:
    def __init__(self, pb: Any, status: Any, *,
                 load_module: Callable[[str], Any] = importlib.import_module,
                 environ: Mapping[str, str] | None = None,
                 check_determinism: bool = True) -> None:
        self.pb, self.status = pb, status
        self._import = load_module
        self.environ = os.environ if environ is None else environ
        # False only where a test exercises the routing of a task the rule refuses
        self.check_determinism = check_determinism
        self.task: str | None = None
        self.inner: Any = None

    def Health(self, request, context):  # noqa: N802 — gRPC's names
        return self.pb.Reply(message=b"OK")

    def LoadModel(self, request, context):  # noqa: N802
        try:
            task, rest = split_task(list(request.Options))
            why = determinism_refusal(task, self.environ) if self.check_determinism else None
            if why:
                raise lb.LoadError(why)
            if self.task is not None and task != self.task:
                raise lb.LoadError(f"this process serves {TASK}:{self.task}; a model of "
                                   f"{TASK}:{task} is another process")
            inner = self.inner or self._import(TASKS[task]).Servicer(self.pb, self.status)
        except Exception as e:                          # noqa: BLE001 — said to LocalAI
            return lb.refused(self.pb, e)
        result = inner.LoadModel(_Load(request, rest), context)
        if getattr(result, "success", False):
            self.task, self.inner = task, inner
        return result

    def _call(self, name: str, request, context):
        if self.inner is None:
            context.abort(self.status.FAILED_PRECONDITION, "no model is loaded")
        method = getattr(self.inner, name, None)
        if method is None:
            context.abort(self.status.UNIMPLEMENTED,
                          f"{TASK}:{self.task} answers no {name}")
        return method(request, context)

    def Embedding(self, request, context):  # noqa: N802
        return self._call("Embedding", request, context)

    def Predict(self, request, context):  # noqa: N802
        return self._call("Predict", request, context)

    def Rerank(self, request, context):  # noqa: N802
        return self._call("Rerank", request, context)


if __name__ == "__main__":
    lb.main(Servicer)
