"""Making a collection: ONE workflow (docs/notes/new-collection.md).

Operator, 2026-11-06: "A mess up in a collection embedding is the most
severe flaw we can have, this is not acceptable. I expected that a clear
workflow for the creation of a new collection had been implemented." And:
"Any new case not covered must be added to this framework. The base idea is
at some point to also make a wizard."

This module is the framework's DECLARATION: the steps, in their one order,
and the cases, each with what the operator gives, the steps it runs and one
example. The note's tables are these declarations in words, and
tests/test_creation_framework.py fails when the two disagree, or when code
writes a Codex outside the steps. The wizard (phase 5) asks a case's
`gives` and nothing else.

It also keeps a run's PROGRESS: `<LOUISE_ROOT>/state/new/<name>.json`, kept
from before the collection exists, so a run stopped at any step resumes. A
step already done is CHECKED, not redone; a different request under the
same name starts afresh, and says so.

THE STANDARD, NOT ADOPTED: a workflow engine (Luigi's targets, Airflow's
DAGs, Prefect's flows) gives resumable steps and their state. Here the steps
are a fixed sequence of nine, each a function this codebase already has,
and the state is one small JSON file; an engine is a new dependency and a
second scheduler beside the node's. It would pay when cases branch and run
in parallel, which none does.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Step:
    """One step of the workflow: what it does, and when it refuses."""
    key: str
    does: str
    refuses: str


#: the steps, in their one order (the note's "The steps")
STEPS: tuple[Step, ...] = (
    Step("resolve", "reads the recipe or the source collection; finds the endpoint, the models and the "
                    "CPU twin on the node",
         "a twin does not state a CPU run of the same weights"),
    Step("check", "checks the target before anything is measured: a collection of that name already "
                  "made is held to the recipe by space_id",
         "the platform image is unknown, or an existing collection is another space"),
    Step("calibrate", "the card against the CPU twin, for each role, on the space's own ctx, cut, "
                      "instruction and set or pairs; the CPU lane kept in the node's store",
         "an endpoint does not answer (exit 3)"),
    Step("tolerance", "takes the measured tolerance; a recipe that states none takes it",
         "the recipe states another tolerance: a tolerance is measured, never typed"),
    Step("make", "create, copy a held space, reuse a held reference, deposit, annotations, or peer fetch",
         "the plugin or recipe is refused (07 §7.3)"),
    Step("carry", "re-creation only: takes from the predecessor what is provably identical (RULE 1)", ""),
    Step("cpu-reference", "writes the collection's copy of the CPU reference from the store; nothing is "
                          "computed twice", "the twin is not reproducible"),
    Step("gate", "`hold embedder check`, for each role", "the gate fails (exit 1)"),
    Step("swap", "re-creation only: forgets the predecessor and takes its name", ""),
)
STEP_KEYS: tuple[str, ...] = tuple(s.key for s in STEPS)


@dataclass(frozen=True)
class Case:
    """A situation a collection is made in. `gives` is what only the
    operator knows; everything else is derived and checked. `built`: the
    workflow runs it today (the others are the plan's, by phase)."""
    key: str
    title: str
    when: str
    gives: tuple[str, ...]
    steps: tuple[str, ...]
    example: str
    built: bool = False
    phase: int = 1


_A = ("resolve", "check", "calibrate", "tolerance", "make", "cpu-reference", "gate")
_COPY = ("resolve", "check", "make", "cpu-reference", "gate")

#: the cases (the note's "The cases"); a new one is declared here first
CASES: tuple[Case, ...] = (
    Case("A", "A new space", "a plugin and a recipe of its own", ("plugin", "recipe", "name"), _A,
         "louise hold collection new wikidata --recipe recipes/wikidata-nemo.json --name wikidata "
         "--encoder http://node:30286 --api vega --embed-model vega-nemotron-3-embed-1b "
         "--cpu-model vega-nemotron-3-embed-1b-cpu", built=True),
    Case("B", "A new space with its own question encoder", "space.query.encoder in the recipe",
         ("plugin", "recipe", "name"), _A, "", phase=3),
    Case("C", "A pair space", "the calibration set is query-document pairs",
         ("plugin", "recipe", "name", "pair file"), _A, "pubmed (MedCPT)", phase=3),
    Case("D", "The same document side as a held collection",
         "a re-creation whose document side is unchanged",
         ("plugin", "recipe", "name", "held collection"), _COPY, "wikidata, parser 2", phase=2),
    Case("E", "A copy of a held space", "a second collection in an existing space",
         ("plugin", "recipe", "the space's collection", "name"), _COPY,
         "a second collection in wikipedia's space", phase=2),
    Case("F", "A re-creation", "a collection replaced, nothing official published from it (RULE 1)",
         ("plugin", "recipe", "name", "predecessor"), _A + ("carry", "swap"),
         "wikipedia-v8 → wikipedia", phase=3),
    Case("G", "A private collection", "LocalAGI or an OpenAI vector store; not shared", ("name",), _COPY,
         "notes", phase=2),
    Case("H", "Annotations", "claims about another collection; no encoder", ("claims file", "name"),
         ("resolve", "check", "make"), "gutenberg-book-editions", phase=3),
    Case("I", "A peer's collection", "a copy fetched from a peer", ("collection id", "name"),
         ("resolve", "make", "cpu-reference", "gate"), "Perugia's gutenberg", phase=2),
    Case("J", "Sister, daughter, corrective", "lineage (02 §2.7)", ("the mother", "what changes"), _A,
         "none yet (\"No official daughters\")", phase=4),
)


def case(key: str) -> Case:
    for c in CASES:
        if c.key == key:
            return c
    raise KeyError(key)


def ordered(c: Case) -> tuple[str, ...]:
    """A case's steps in the workflow's one order."""
    return tuple(k for k in STEP_KEYS if k in c.steps)


def progress_path(name: str) -> Path:
    """`<LOUISE_ROOT>/state/new/<name>.json`: node state, kept from before
    the collection exists (its own directory is made at `make`)."""
    from app import config
    return config.louise_root() / "state" / "new" / f"{name}.json"


def inputs_digest(inputs: dict[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(inputs, sort_keys=True, ensure_ascii=False,
                                                 default=str).encode("utf-8")).hexdigest()


class Run:
    """One run of a case for one name: numbered steps, said as they go, and
    each done step kept with its result. `inputs` is everything the run was
    given (the recipe's bytes included); a file kept for other inputs is
    another request and is not resumed."""

    def __init__(self, c: Case, name: str, inputs: dict[str, Any], *,
                 say: Callable[[str], None], path: Path | None = None):
        self.case, self.name, self.say = c, name, say
        self.steps = ordered(c)
        self.path = path or progress_path(name)
        self.digest = inputs_digest(inputs)
        self.kept: dict[str, Any] = {}
        self.restarted = False
        try:
            held = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            held = None
        if isinstance(held, dict) and held.get("inputs") == self.digest:
            self.kept = dict(held.get("steps") or {})
        elif isinstance(held, dict):
            self.restarted = True
            say(f"new: {self.path} was kept for another request under the name {name}: started afresh")

    def at(self, step: str, text: str) -> None:
        """`n/N step  text`, n the step's place in this case."""
        if step not in self.steps:
            raise KeyError(f"case {self.case.key} has no step {step!r}")
        self.say(f"{self.steps.index(step) + 1}/{len(self.steps)} {step:<13} {text}")

    def done(self, step: str) -> dict[str, Any] | None:
        """The result a step recorded on an earlier run of the same inputs."""
        got = self.kept.get(step)
        return got.get("result") if isinstance(got, dict) else None

    def record(self, step: str, result: dict[str, Any] | None = None) -> None:
        """Kept AFTER the step's own durable effect (invariant 2), written
        whole and renamed into place."""
        self.kept[step] = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "result": result or {}}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"case": self.case.key, "name": self.name, "inputs": self.digest,
                                   "steps": self.kept}, indent=1, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.path)
