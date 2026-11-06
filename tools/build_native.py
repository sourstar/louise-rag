"""tools/build_native.py — compile the modules Louise ships compiled, with
mypyc, beside their sources (docs/notes/python-hot-spots.md, "E").

Today: wikidata's textifier and its formatter (`app.plugins.wikidata.pure.
COMPILED`), in one mypyc group whose shared library sits in the same
package (`native__mypyc.<abi>.so`), so the compiled modules import wherever
`app` imports — never a top-level module that depends on the working
directory. Python imports an extension before the `.py` of the same name;
the `.py` stays the source, and the reference the compiled bytes are proven
against (tools/measure/textify_identity.py).

The image runs it in its build stage `native` (Dockerfile), with the
compiler of requirements-build.txt, and copies out what `--out` collects;
mypy never reaches the runtime image. By hand, in a tree:

  pip install -r requirements-build.txt     # and a C compiler
  python -m tools.build_native              # the .so files beside the sources
  python -m tools.build_native --clean      # remove them: the .py runs again

It builds for the interpreter that runs it (its ABI tag is in the file
names), removes that interpreter's earlier build of these modules first, so
a failed build leaves the sources running, never a stale extension, and
writes nothing else into the tree: the C sources, objects and mypy's cache
go to a temporary directory. Built files are gitignored and kept out of the
image's build context (.dockerignore): only the stage builds them.

The mypy options: `--follow-imports=skip`, so the compiled code depends on
these modules' own annotations alone — what another module of the app
declares cannot add a type check the Python never makes. mypy must find no
error in them: an error is a build failure, never ignored here.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.machinery
import importlib.util
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "app" / "plugins" / "wikidata"
#: -O3 as mypyc's default; no debug information (smaller files, the same code)
OPT_LEVEL, DEBUG_LEVEL = "3", "0"


def _plan() -> tuple[tuple[str, ...], str]:
    """`COMPILED` and `GROUP` from app/plugins/wikidata/pure.py, read by path:
    importing the package would import the very extensions being replaced."""
    spec = importlib.util.spec_from_file_location("_louise_native_plan", PACKAGE / "pure.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return tuple(module.COMPILED), str(module.GROUP)


def outputs(suffix: str | None = None) -> list[Path]:
    """This interpreter's (or `suffix`'s) extension files of the plan."""
    modules, group = _plan()
    suffix = suffix or importlib.machinery.EXTENSION_SUFFIXES[0]
    return [PACKAGE / f"{name}{suffix}" for name in (*modules, f"{group}__mypyc")]


def clean() -> list[Path]:
    """Remove this interpreter's built files: unlinking, never truncating, so
    a process that has one mapped keeps its pages."""
    gone = [p for p in outputs() if p.exists()]
    for p in gone:
        p.unlink()
    return gone


def build(out: Path | None = None) -> list[Path]:
    """Compile the plan in a temporary directory, put each extension beside
    its source (and under `out`, at its path from the repository root);
    returns the files placed in the tree."""
    from mypyc.build import mypycify
    from setuptools import setup

    modules, group = _plan()
    clean()
    sources = [str((PACKAGE / f"{name}.py").relative_to(ROOT)) for name in modules]
    with tempfile.TemporaryDirectory(prefix="louise-native-") as tmp:
        t = Path(tmp)
        cwd = Path.cwd()
        os.chdir(ROOT)                      # mypyc names modules by their path from here
        try:
            extensions = mypycify(
                ["--follow-imports=skip", "--cache-dir", str(t / "mypy-cache"), *sources],
                opt_level=OPT_LEVEL, debug_level=DEBUG_LEVEL, target_dir=str(t / "c"),
                separate=[(sources, f"app.plugins.wikidata.{group}")])
            setup(name="louise-native", ext_modules=extensions,
                  script_args=["--quiet", "build_ext", "--build-lib", str(t / "lib"),
                               "--build-temp", str(t / "obj")])
        finally:
            os.chdir(cwd)
        built = sorted((t / "lib").rglob("*.so"))
        placed = []
        for f in built:
            rel = f.relative_to(t / "lib")
            target = ROOT / rel
            if target.parent != PACKAGE:
                raise SystemExit(f"mypyc wrote {rel} outside {PACKAGE.relative_to(ROOT)}: not placed")
            shutil.copyfile(f, target)
            placed.append(target)
            if out is not None:
                (out / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(f, out / rel)
    missing = [p for p in outputs() if p not in placed]
    if missing:
        raise SystemExit(f"the build made no {', '.join(p.name for p in missing)}")
    return placed


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="build_native", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", type=Path, default=None,
                   help="also copy each built file here, at its path from the repository root")
    p.add_argument("--clean", action="store_true", help="remove this interpreter's built files and stop")
    a = p.parse_args(argv)
    if a.clean:
        for f in clean():
            print(f"removed {f.relative_to(ROOT)}")
        return 0
    for f in build(a.out):
        print(f"{hashlib.sha256(f.read_bytes()).hexdigest()}  {f.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
