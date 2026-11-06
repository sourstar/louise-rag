"""RAGE names the RAG Engine alone (docs/notes/rage.md §5). Operator,
2026-11-01: "Rename or remove any old rage naming; rename vega where it fits,
remove any retired code, it will survive only in the github past commits".

Until 2026-11-01 the name was the model pool's — VeGa's predecessor, retired
2026-10-18 — whose code lived in an `app/rage/` of its own. None of its names
may come back into the tree: a reader would take them for the RAG Engine's.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
#: the model pool's modules, deleted with it (git history before 6a4f517)
POOL_MODULES = {"front", "admission", "placement", "store", "model", "clock", "swap",
                "swapyaml", "runs", "pairs", "procfs", "__main__"}
#: every name the model pool had: its config, deploy files, tests, tools,
#: image, apps, datasets, variables and routes
OLD_NAMES = re.compile(
    r"\brage\.toml|\brage\.d/|\brage\.example|deploy/rage\b|tests/rage\b|rage_smoke"
    r"|app/rage/(?:" + "|".join(sorted(POOL_MODULES)) + r")\b"
    r"|louise-rage(?!-settings)|sourstar/rage\b|\brage:|rage-alerts"
    r"|RAGE_(?:URL|IMAGE|RUNNER|FREQ_DIR|DIGEST)\b|rage-15c|rage-backends|/etc/rage"
    r"|rage-service|pre-rage|truenas-rage|python -m app\.rage\b"
    r"|rage-over-localai|rage-review|rage-prior-art|RAGE \[the model pool|RageConfigError")
#: data, whose bytes are hashed or vendored, is not prose
SKIP = re.compile(r"^(calibration/|app/static/vendor/)|\.(jsonl|json|parquet|safetensors|gz|zst|png|jpg|ico|woff2?)$")


def _tracked() -> list[Path]:
    try:
        out = subprocess.check_output(["git", "ls-files"], cwd=ROOT, text=True)
        return [ROOT / f for f in out.split("\n") if f and not SKIP.search(f)]
    except (OSError, subprocess.CalledProcessError):
        return [p for p in ROOT.rglob("*") if p.is_file() and ".git" not in p.parts
                and not SKIP.search(p.relative_to(ROOT).as_posix())]


def test_no_name_of_the_model_pool_is_left_in_the_tree():
    found = []
    for p in _tracked():
        if p.name == "test_rage_name.py" or not p.is_file():
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for n, line in enumerate(text.split("\n"), 1):
            if OLD_NAMES.search(line):
                found.append(f"{p.relative_to(ROOT)}:{n}: {line.strip()[:100]}")
    assert not found, "the model pool's names, which now read as RAGE's:\n" + "\n".join(found[:40])


def test_todays_package_shares_no_file_name_with_the_model_pools():
    names = {p.stem for p in (ROOT / "app" / "rage").glob("*.py")}
    assert not names & POOL_MODULES


def test_the_retired_notes_and_code_live_in_git_only():
    notes = ROOT / "docs" / "notes"
    assert not list(notes.glob("model-pool*.md")) and not list(notes.glob("rage-*.md"))
    for gone in ("attic", "app/coordinator", "app/contribute.py"):
        path = ROOT / gone
        left = [path] if path.is_file() else [p for p in path.rglob("*") if p.suffix in {".py", ".md"}]
        assert not left, gone


def test_the_vocabulary_tells_rage_from_an_engine():
    text = (ROOT / "docs" / "01-louise.md").read_text(encoding="utf-8")
    rows = {m.group(1): m.group(2) for m in re.finditer(r"^\| (RAGE|engine) \| (.*)$", text, re.M)}
    assert "RAG Engine" in rows["RAGE"] and "09 §9.3" in rows["RAGE"]
    assert "how a model is run" in rows["engine"]
