"""The textifier as Python source, whatever the image compiled
(docs/notes/python-hot-spots.md, "E").

The image compiles `COMPILED` with mypyc (tools/build_native.py; Dockerfile,
stage `native`): Python imports an extension module before a `.py` of the
same name, so `app.plugins.wikidata.textify` is the compiled one where the
image built it and the source everywhere else (a test run, a tree
bind-mounted over /srv). The source stays the reference: the compiled module
must give its bytes, entity for entity (tools/measure/textify_identity.py).

`textify()` is that reference, loaded from the `.py` files by path under
names of its own, so no extension can shadow it, with the `format` and
`names` it calls loaded the same way. Two kinds of caller need it: the
identity checks, and a tool that SUBCLASSES the textifier's classes (a
compiled class cannot be subclassed from Python; tools/wikidata_coverage.py).
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
import threading
from pathlib import Path
from types import ModuleType

#: the modules of this package the image compiles, in one mypyc group: the
#: textifier and the formatter it calls on every time, quantity and
#: coordinate. names.py is not: it runs only for an entity with no label
#: in the language, `mul` or the fallback, and the pack's own passes import it
COMPILED = ("textify", "format")
#: the group's shared library, beside them (`native__mypyc.<abi>.so`)
GROUP = "native"

_HERE = Path(__file__).resolve().parent
_PREFIX = "_louise_pure_wikidata_"
_loaded: dict[str, ModuleType] = {}
_lock = threading.Lock()


def is_compiled(module: ModuleType) -> bool:
    """An extension module, not Python source."""
    spec = getattr(module, "__spec__", None)
    loader = getattr(spec, "loader", None)
    if isinstance(loader, importlib.machinery.ExtensionFileLoader):
        return True
    return not str(getattr(module, "__file__", "") or "").endswith(".py")


def compiled() -> dict[str, bool]:
    """For each module of `COMPILED`, whether the one imported is compiled."""
    import importlib
    return {name: is_compiled(importlib.import_module(f"{__package__}.{name}")) for name in COMPILED}


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(_PREFIX + name, _HERE / f"{name}.py",
                                                  loader=importlib.machinery.SourceFileLoader(
                                                      _PREFIX + name, str(_HERE / f"{name}.py")))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module           # dataclasses resolve the module by name
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(spec.name, None)
        raise
    return module


def textify() -> ModuleType:
    """The textifier from its `.py`, its `fmt` and `names` from theirs: the
    source as written, never an extension. Loaded once a process."""
    with _lock:
        got = _loaded.get("textify")
        if got is None:
            fmt, names = _load("format"), _load("names")
            got = _load("textify")
            # its module-level `from app.plugins.wikidata import format as fmt,
            # names` bound the package's (maybe compiled) ones: the source's own
            # are what it calls (`fmt.time_text`, `fmt.FormatError`, `names.…`)
            got.fmt, got.names = fmt, names
            _loaded.update(textify=got, format=fmt, names=names)
        return got
