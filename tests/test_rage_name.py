"""RAGE names the RAG Engine alone (operator, 2026-11-01: "be sure there will
be no misunderstandings in the files"; docs/notes/rage.md §5). Until then the
name was the model pool's, whose code lived in an `app/rage/` of its own."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
#: the model pool's modules, deleted with it (git history before 2026-10-18)
POOL_MODULES = {"front", "admission", "placement", "store", "model", "clock", "swap",
                "swapyaml", "runs", "pairs", "procfs", "__main__"}


def test_todays_package_shares_no_file_name_with_the_model_pools():
    """Its paths are still cited by the notes and by commits: a module of the
    same name in today's `app/rage/` would be read as that one."""
    names = {p.stem for p in (ROOT / "app" / "rage").glob("*.py")}
    assert not names & POOL_MODULES


def test_the_model_pools_notes_say_whose_they_are():
    for p in (ROOT / "docs" / "notes").glob("model-pool*.md"):
        head = p.read_text(encoding="utf-8")[:1200]
        assert "This note is about **the model pool**" in head, p.name
    assert not list((ROOT / "docs" / "notes").glob("rage-*.md")), "the pool's notes are model-pool-*"


def test_the_vocabulary_tells_rage_from_an_engine():
    text = (ROOT / "docs" / "01-louise.md").read_text(encoding="utf-8")
    rows = {m.group(1): m.group(2) for m in re.finditer(r"^\| (RAGE|engine) \| (.*)$", text, re.M)}
    assert "RAG Engine" in rows["RAGE"] and "09 §9.3" in rows["RAGE"]
    assert "how a model is run" in rows["engine"]


def test_nothing_reads_the_model_pools_address():
    """`RAGE_URL` was the model pool's (retired 2026-10-18): a variable of that
    name would now read as the RAG Engine's, which has no address."""
    for d in ("app", "deploy/docker"):
        for p in (ROOT / d).rglob("*"):
            if p.is_file() and p.suffix in {".py", ".yaml", ".yml", ".example", ".env"}:
                assert "RAGE_URL" not in p.read_text(encoding="utf-8", errors="replace"), p
