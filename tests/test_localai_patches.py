"""The LocalAI patches RAGE carries, held to the one list of LocalAI action
items (deploy/localai/patches/, docs/notes/localai-bugs.md; migration-plan
step 15).

Operator, 2026-10-03: every LocalAI patch is noted in the same file as its
defects, so that the LocalAI team's action items are in one place. A patch
that is in the series but not in that file is one the operator never files;
a patch named there but missing here is one the image never applies.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATCHES = ROOT / "deploy" / "localai" / "patches"
ACTIONS = (ROOT / "docs" / "notes" / "localai-bugs.md").read_text(encoding="utf-8")


def _series() -> tuple[str, list[str]]:
    lines = [line.strip() for line in (PATCHES / "series").read_text(encoding="utf-8").splitlines()
             if line.strip() and not line.lstrip().startswith("#")]
    head, *names = lines
    kind, _, base = head.partition(" ")
    assert kind == "base" and re.fullmatch(r"[0-9a-f]{40}", base), \
        "the series starts with the full commit the patches apply to"
    return base, names


def _carried_section() -> str:
    start = ACTIONS.index("## Patches we carry")
    end = ACTIONS.index("\n## ", start + 1)
    return ACTIONS[start:end]


def test_the_series_and_the_directory_hold_the_same_patches():
    _, names = _series()
    assert names, "an empty series is a deleted directory"
    assert len(set(names)) == len(names)
    assert set(names) == {p.name for p in PATCHES.glob("*.patch")}, \
        "a patch in the directory and not in the series is never applied"
    for i, name in enumerate(names, 1):
        assert name.startswith(f"{i:04d}-"), f"{name}: numbered in the order applied"


def test_every_patch_is_a_mail_git_am_applies():
    for name in _series()[1]:
        text = (PATCHES / name).read_text(encoding="utf-8")
        assert text.startswith("From 0000000000000000000000000000000000000000 "), name
        assert re.search(r"^Subject: \[PATCH\] \S", text, re.M), name
        assert re.search(r"^diff --git ", text, re.M), name


def test_no_patch_signs_off_for_a_human_or_credits_a_model_as_co_author():
    """LocalAI's policy for AI-assisted work (.agents/ai-coding-assistants.md):
    the DCO is certified by the human who submits, never by an agent."""
    for name in _series()[1]:
        text = (PATCHES / name).read_text(encoding="utf-8")
        message = text[:text.index("\n---\n")]
        assert not re.search(r"^Signed-off-by:", message, re.M | re.I), name
        assert not re.search(r"^Co-Authored-By:", message, re.M | re.I), name
        assert re.search(r"^Assisted-by: ", message, re.M), name


def test_every_patch_is_an_action_item_with_its_base_and_its_file():
    base, names = _series()
    section = _carried_section()
    assert base in section, "the base commit the patches apply to is named beside them"
    for i, name in enumerate(names, 1):
        assert f"### P{i}. " in section, f"P{i} has its own entry"
        assert name in ACTIONS, f"{name} is named in the action items"
        row = re.search(rf"^\| \*\*P{i}\*\* \|.*$", ACTIONS, re.M)
        assert row and name in row.group(0), f"P{i} is in the index table with its file"
    assert f"### P{len(names) + 1}. " not in section, "an entry whose patch is not carried"
