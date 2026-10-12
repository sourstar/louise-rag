"""tests/test_vega_stage_scripts.py — a package behind VeGa is identified by
its installed files, hashed at every load (docs/notes/vega.md §4), so its
build must leave nothing for Python to write at run time. The wrapper build
excluded its portable stdlib and venv from compileall until 2026-10-12, and
a new wrapper's engine identity moved over its first loads under the first
Codex pinned to it (vega-louise-dar-legal-it)."""
import re
from pathlib import Path

STAGE = Path(__file__).resolve().parents[1] / "deploy" / "vega" / "stage"


def _compileall(script: str) -> list[str]:
    text = (STAGE / script).read_text(encoding="utf-8")
    return [line for line in text.splitlines()
            if "compileall" in line and not line.lstrip().startswith("#")]


def test_every_package_build_compiles_its_whole_tree():
    for script in ("build_vega_package.sh", "build_louise_backend.sh"):
        lines = _compileall(script)
        assert lines, f"{script} compiles nothing"
        for line in lines:
            assert not re.search(r"\s-x\s", line), f"{script} excludes part of the tree: {line.strip()}"
