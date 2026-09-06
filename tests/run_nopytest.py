"""Run tests/hold and tests/share without pytest. A stand-in for environments
where pytest cannot be installed; the test files are ordinary pytest tests.

    python -m tests.run_nopytest
"""
from __future__ import annotations

import contextlib
import importlib
import inspect
import re
import sys
import tempfile
import traceback
import types
from pathlib import Path


# ---- a tiny pytest shim ------------------------------------------------------

class _Raises:
    def __init__(self, exc, match=None):
        self.exc, self.match = exc, match
    def __enter__(self):
        return self
    def __exit__(self, et, ev, tb):
        if et is None:
            raise AssertionError(f"did not raise {self.exc.__name__}")
        if not issubclass(et, self.exc):
            return False
        if self.match and not re.search(self.match, str(ev)):
            raise AssertionError(f"message {str(ev)!r} does not match {self.match!r}")
        return True


class _Approx:
    def __init__(self, v, abs=1e-12):
        self.v, self.abs = v, abs
    def __eq__(self, other):
        return abs(other - self.v) <= self.abs
    def __repr__(self):
        return f"approx({self.v} ± {self.abs})"


def _parametrize(names, values):
    def deco(fn):
        fn._params = (names, values)
        return fn
    return deco


pytest_shim = types.ModuleType("pytest")
pytest_shim.raises = _Raises
pytest_shim.approx = _Approx
pytest_shim.mark = types.SimpleNamespace(parametrize=_parametrize)
sys.modules.setdefault("pytest", pytest_shim)


# ---- discovery and run -------------------------------------------------------

def _cases(fn):
    if hasattr(fn, "_params"):
        names, values = fn._params
        names = [n.strip() for n in names.split(",")]
        for i, v in enumerate(values):
            v = v if isinstance(v, tuple) else (v,)
            yield f"{fn.__name__}[{i}]", dict(zip(names, v))
    else:
        yield fn.__name__, {}


def run(modules: list[str]) -> int:
    passed = failed = 0
    for modname in modules:
        mod = importlib.import_module(modname)
        for name, fn in sorted(vars(mod).items()):
            if not (name.startswith("test_") and callable(fn)):
                continue
            for case, kwargs in _cases(fn):
                sig = inspect.signature(fn)
                with contextlib.ExitStack() as stack:
                    if "tmp_path" in sig.parameters:
                        kwargs["tmp_path"] = Path(stack.enter_context(tempfile.TemporaryDirectory()))
                    try:
                        fn(**kwargs)
                        passed += 1
                        print(f"  ok   {modname}::{case}")
                    except Exception:
                        failed += 1
                        print(f"  FAIL {modname}::{case}")
                        traceback.print_exc()
    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(run([
        "tests.hold.test_codex",
        "tests.hold.test_snapshot_and_collection",
        "tests.share.test_rendezvous",
    ]))
