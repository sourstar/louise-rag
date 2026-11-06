"""The collection creation framework's guard (docs/notes/new-collection.md,
"Adding a case"; operator, 2026-11-06: "Any new case not covered must be
added to this framework").

- The steps and the cases app/hold/workflow.py declares are the note's
  tables, both ways: a case declared and not documented fails, and so does
  one documented and not declared.
- No code writes a Codex outside the framework's steps. Every call of a
  primitive that makes one is in a step, or on the allow-list below naming
  the path not yet moved and the phase that moves it. An entry whose path
  is gone fails too, so the list only shrinks; phase 4 empties it.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

from app.hold import workflow as wf

ROOT = Path(__file__).resolve().parents[1]
NOTE = ROOT / "docs" / "notes" / "new-collection.md"

#: what makes a Codex: app/hold/collection.py's own, and louise's two
#: wrappers every route calls (their bodies ARE the primitive)
COLLECTION_PRIMITIVES = {"create", "create_from_recipe", "derive", "continue_", "replace", "write"}
LOUISE_PRIMITIVES = {"create_from", "create_deposit"}
DEFINITIONS = {"app/louise.py:create_from", "app/louise.py:create_deposit"}

#: the framework's steps that make a Codex
FRAMEWORK = {"app/louise.py:cmd_collection_new": "case A, make"}

#: paths not yet moved into the framework, each with the phase that moves it
NOT_YET_MOVED = {
    "app/hold/deposit.py:create": "G, a private collection's first space (phase 2)",
    "app/jobs.py:_deposit": "G, the LocalAGI and OpenAI routes (phase 2)",
    "app/louise.py:cmd_create_deposit": "G, `create deposit` (phase 2)",
    "app/share/fetch.py:hold": "I, a peer's collection (phase 2)",
    "app/hold/annotations.py:create": "H, annotations (phase 3)",
    "app/louise.py:cmd_create": "`create` (phase 4)",
    "app/jobs.py:_create": "the Library's create (phase 4)",
    "app/louise.py:cmd_derive": "J, sister and daughter (phase 4)",
    "app/louise.py:cmd_continue": "J, continuation (phase 4)",
    "app/louise.py:cmd_replace": "J, corrective (phase 4)",
}


def _calls(node: ast.AST, fn: str | None, rel: str, aliases: set[str], found: set[str]) -> None:
    """Every primitive called under `node`, as `rel:outermost function`."""
    for c in ast.iter_child_nodes(node):
        f = c.name if fn is None and isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef)) else fn
        if isinstance(c, ast.Call):
            fu = c.func
            if isinstance(fu, ast.Attribute) and isinstance(fu.value, ast.Name) and (
                    (fu.value.id in aliases and fu.attr in COLLECTION_PRIMITIVES)
                    or (fu.value.id == "louise" and fu.attr in LOUISE_PRIMITIVES)):
                found.add(f"{rel}:{f}")
            if isinstance(fu, ast.Name) and fu.id in LOUISE_PRIMITIVES and rel == "app/louise.py":
                found.add(f"{rel}:{f}")
        _calls(c, f, rel, aliases, found)


def _codex_writers() -> set[str]:
    """`path:outermost function` of every call of a primitive in app/ and
    tools/, outside app/hold/collection.py, which defines them."""
    found: set[str] = set()
    for p in sorted([*ROOT.joinpath("app").rglob("*.py"), *ROOT.joinpath("tools").rglob("*.py")]):
        rel = p.relative_to(ROOT).as_posix()
        if rel == "app/hold/collection.py":
            continue
        tree = ast.parse(p.read_text(encoding="utf-8"))
        aliases = {a.asname or a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)
                   for a in n.names if a.name == "collection"
                   and (n.module in (None, "app.hold") or (n.module or "").endswith("hold"))}
        _calls(tree, None, rel, aliases, found)
    return found - DEFINITIONS


def test_no_code_writes_a_codex_outside_the_frameworks_steps():
    writers = _codex_writers()
    outside = writers - set(FRAMEWORK) - set(NOT_YET_MOVED)
    assert not outside, (f"{sorted(outside)} make a Codex outside the workflow: make it a step of a case "
                         f"(app/hold/workflow.py, docs/notes/new-collection.md), never a new chain")
    gone = (set(FRAMEWORK) | set(NOT_YET_MOVED)) - writers
    assert not gone, f"{sorted(gone)} make no Codex any more: take them off this test's lists"


def _table(heading: str) -> list[list[str]]:
    text = NOTE.read_text(encoding="utf-8")
    body = text.split(heading, 1)[1].split("\n## ", 1)[0]
    rows = [[c.strip() for c in line.strip().strip("|").split("|")]
            for line in body.splitlines() if line.startswith("|")]
    return [r for r in rows[2:] if r]


def test_the_steps_are_the_notes_in_its_order():
    rows = _table("## The steps")
    assert [(int(r[0]), r[1]) for r in rows] == [(i + 1, s.key) for i, s in enumerate(wf.STEPS)]


def test_every_case_declared_is_documented_and_every_case_documented_is_declared():
    rows = _table("## The cases")
    documented = {}
    for r in rows:
        m = re.fullmatch(r"\*\*([A-Z])\. (.+)\*\*", r[0])
        assert m, r[0]
        documented[m.group(1)] = m.group(2)
    assert documented == {c.key: c.title for c in wf.CASES}
    status = NOTE.read_text(encoding="utf-8").split("## Status", 1)[1]
    for c in wf.CASES:
        if c.built:
            assert re.search(rf"^\| {c.key} \| Built", status, re.MULTILINE), f"case {c.key} is built: say so in Status"


def test_each_case_runs_the_workflows_steps_in_its_one_order():
    for c in wf.CASES:
        assert set(c.steps) <= set(wf.STEP_KEYS), c.key
        assert c.gives, f"case {c.key}: what the operator gives"
        assert "make" in c.steps, c.key
    a = wf.case("A")
    assert wf.ordered(a) == ("resolve", "check", "calibrate", "tolerance", "make", "cpu-reference", "gate")
