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


def _app_imports(path: Path, root: Path) -> set[str]:
    """The app/ modules `path` imports, and theirs, as repo-relative paths."""
    import ast
    seen: set[str] = set()
    todo = [path]
    while todo:
        tree = ast.parse(todo.pop().read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("app"):
                names = [f"{node.module}.{a.name}" for a in node.names] + [node.module]
            elif isinstance(node, ast.Import):
                names = [a.name for a in node.names if a.name.startswith("app")]
            for name in names:
                for f in (root / (name.replace(".", "/") + ".py"),
                          root / name.replace(".", "/") / "__init__.py"):
                    rel = str(f.relative_to(root))
                    if f.is_file() and rel not in seen:
                        seen.add(rel)
                        todo.append(f)
    return seen


def test_every_app_module_the_wrapper_imports_is_staged_and_packaged():
    """The NVIDIA reader (app/vega/nvidia.py, 2026-10-21) is imported by the
    wrapper at start: a package without it is a backend LocalAI cannot start."""
    root = STAGE.parents[2]
    wanted = _app_imports(root / "deploy" / "vega" / "vega_wrapper.py", root)
    assert "app/vega/nvidia.py" in wanted and "app/vega/dxg.py" in wanted
    for script in ("stage_src.sh", "build_vega_package.sh"):
        listed = set(re.findall(r"app/[\w/]+\.py", (STAGE / script).read_text(encoding="utf-8")))
        assert not wanted - listed, f"{script} leaves out {sorted(wanted - listed)}"


def test_a_rebuild_keeps_the_digests_the_wrapper_kept():
    """A rebuild of the wrapper changes no file of the real backend: its first
    load must not read the whole tree again (the 65 s bound, 2026-10-21)."""
    text = (STAGE / "build_vega_package.sh").read_text(encoding="utf-8")
    clean = next(line for line in text.splitlines() if "-exec rm -rf" in line and "mindepth 1" in line)
    assert "! -name file-digests.json" in clean
    from tests.vega.conftest import load_wrapper
    assert load_wrapper().DIGEST_STORE == "file-digests.json"
