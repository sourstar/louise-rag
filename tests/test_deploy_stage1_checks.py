"""The deploy's stage-1 checks hold on the tree they deploy.

deploy/16f/deploy-louise-madlad-t5.sh checks the staged tree, line by line,
for what each fix put there (a `grep -q` or a `test -s` against
"$ROOT/src.new"), under `set -e`. A check left behind by a change to the
code stops the deploy at stage 1. München, 2026-11-07: #348 replaced the
wikidata pack's `_groups = GroupCache(256 << 20)` with
`read_cache("wikidata pack")`, line 160 still looked for the old line, and
73930ab's deploy stopped three times. The stop said nothing, because
`grep -q` is quiet. Here every check runs against this checkout, which is
the tree a deploy stages, so a change that breaks one fails here, before
the merge.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "16f" / "deploy-louise-madlad-t5.sh"
STAGED = '"$ROOT/src.new'
#: a stage-1 check: one line, starting with the check itself
CHECK = re.compile(r"^(!\s+)?(grep -q|test )")


def _checks() -> list[tuple[int, str]]:
    """(line number, command) of every one-line check of the staged tree,
    its trailing comment kept (bash ignores it)."""
    out = []
    for n, line in enumerate(SCRIPT.read_text(encoding="utf-8").splitlines(), 1):
        if CHECK.match(line) and STAGED in line:
            out.append((n, line))
    return out


def test_the_script_has_stage_one_checks():
    assert len(_checks()) > 100


def test_every_stage_one_check_holds_on_this_tree():
    failed = []
    for n, line in _checks():
        cmd = line.replace(STAGED, f'"{ROOT}')
        r = subprocess.run(["bash", "-c", cmd], cwd=ROOT, capture_output=True, text=True)
        if r.returncode != 0:
            failed.append(f"line {n}: {line.strip()}")
    assert not failed, ("the deploy's stage-1 checks that this tree fails (change the check with "
                        "the code it checks):\n" + "\n".join(failed))


def test_a_failed_check_says_which_line_stopped_the_deploy():
    text = SCRIPT.read_text(encoding="utf-8")
    assert re.search(r"^trap '.*\$LINENO.*\$BASH_COMMAND.*' ERR$", text, re.MULTILINE)
