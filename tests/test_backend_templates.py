"""The collection backend (app/backend.py; operator, 2026-10-30): every
collection falls in a TEMPLATE, and a template's machinery is one shared
implementation. A plugin registered without a declaration — or whose
declaration does not match what its code does — fails here, before a
collection is made from it."""
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from app import backend, derived
from app import collections as collections_mod
from app.backend import Template

ROOT = Path(__file__).resolve().parent.parent
PLACED = re.compile(r"""derived\.place(?:_kept)?\(\s*["']([a-z0-9-]+)["']""")


def _package_source(plugin) -> str:
    import inspect
    pkg = Path(inspect.getsourcefile(type(plugin))).parent
    return "\n".join(p.read_text(encoding="utf-8") for p in sorted(pkg.glob("*.py")))


def test_every_registered_plugin_falls_in_a_template():
    registry = collections_mod.PLUGIN_REGISTRY
    assert len(registry) >= 7, "the registry is not what this test walks — the test is wrong"
    for name, plugin in sorted(registry.items()):
        forms = backend.forms_of(plugin)
        assert forms, name
        assert len({f.name for f in forms}) == len(forms), f"{name}: two forms of one name"


def test_a_plugin_without_a_declaration_is_refused():
    class Bare:
        pass
    with pytest.raises(TypeError, match="declares no backend template"):
        backend.forms_of(Bare())
    class Wrong:
        backend = ("container-pack",)
    with pytest.raises(TypeError, match="not a backend OriginForm"):
        backend.forms_of(Wrong())


def test_the_declared_derived_data_is_what_the_code_places_and_builds():
    """The declaration is checked against the code, not trusted: each kind a
    plugin's package places under the fast drive is declared and each
    declared kind is placed; a CONTAINER_PACK builds under
    `derived.building`, a DUMP_PACK under `derived.writing`; a MEMBER form
    derives nothing."""
    for name, plugin in sorted(collections_mod.PLUGIN_REGISTRY.items()):
        forms = backend.forms_of(plugin)
        src = _package_source(plugin)
        declared = {k for f in forms for k in f.derived}
        assert set(PLACED.findall(src)) == declared, f"{name}: placed {set(PLACED.findall(src))}, declared {declared}"
        for f in forms:
            if f.template is Template.MEMBER:
                assert not f.derived, f"{name}: a MEMBER form ({f.name}) derives nothing"
            elif f.template is Template.CONTAINER_PACK:
                assert "derived.building(" in src, f"{name}: a CONTAINER_PACK builds under derived.building"
            elif f.template is Template.DUMP_PACK:
                assert "derived.writing(" in src, f"{name}: a DUMP_PACK writes under derived.writing"


def test_an_origin_walk_never_enters_what_is_derived_from_it(tmp_path):
    """A page-props pack beside its dump was walked as an archive shard:
    "unrecognized shard path" at every scan (the 2026-10-30 inventory)."""
    from app.plugins import walk
    (tmp_path / "data" / "en").mkdir(parents=True)
    (tmp_path / "data" / "en" / "000.parquet").write_bytes(b"x")
    (tmp_path / derived.BESIDE).mkdir()
    (tmp_path / derived.BESIDE / "enwiki-page_props.sql.gz.pack.parquet").write_bytes(b"x")
    assert walk.files(tmp_path, "*.parquet") == [tmp_path / "data" / "en" / "000.parquet"]


_HOLD = """
import sys, time
from pathlib import Path
from app import derived
with derived.writing(Path(sys.argv[1]), "a test's long pass"):
    print("held", flush=True)
    time.sleep(30)
"""


def test_a_second_writer_is_refused_at_once_across_processes_and_told_who(tmp_path):
    lock = tmp_path / "pack" / "writer.lock"
    env = {"PYTHONPATH": str(ROOT), "PATH": "/usr/bin:/bin"}
    holder = subprocess.Popen([sys.executable, "-c", _HOLD, str(lock)], cwd=ROOT, env=env,
                              stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        with pytest.raises(derived.Busy) as e:
            with derived.writing(lock, "a second pass"):
                pytest.fail("two writers")
        assert e.value.holder["what"] == "a test's long pass" and e.value.holder["pid"] == holder.pid
    finally:
        holder.kill()
        holder.wait()
    with derived.writing(lock, "after the holder died"):      # the kernel dropped it
        pass
    assert lock.read_bytes() == b"", "the holder's words go with the lock"


_ADOPT = """
import sys, time
from pathlib import Path
from multiprocessing import get_context

def one(args):
    src, dst, at = args
    while time.time() < at:
        time.sleep(0.001)
    from app import derived
    return derived.adopt(Path(src), Path(dst))

if __name__ == "__main__":
    at = time.time() + 2
    with get_context("spawn").Pool(4) as pool:
        print(sum(pool.map(one, [(sys.argv[1], sys.argv[2], at)] * 4)))
"""


def test_four_processes_adopt_one_pack_once_whole(tmp_path):
    """pubmed adopts a pack beside its containers into the fast drive from
    `ensure`, in every parse-pool child: two copies into one fixed temporary
    name could rename a half-written pack into place."""
    src = tmp_path / "beside" / "c.pack.parquet"
    src.parent.mkdir()
    src.write_bytes(bytes(range(256)) * 40_000)                # 10 MB
    dst = tmp_path / "cache" / "c.pack.parquet"
    script = tmp_path / "adopt.py"
    script.write_text(_ADOPT)
    p = subprocess.run([sys.executable, str(script), str(src), str(dst)], cwd=ROOT,
                       env={"PYTHONPATH": str(ROOT), "PATH": "/usr/bin:/bin"},
                       capture_output=True, text=True, timeout=120)
    assert p.returncode == 0, p.stderr[-2000:]
    assert p.stdout.strip() == "1", "one adopter copied; the others found it there"
    assert dst.read_bytes() == src.read_bytes()
    assert not [x for x in dst.parent.iterdir() if "adopting" in x.name]


def test_a_killed_adopter_s_copy_goes_with_the_next(tmp_path):
    src = tmp_path / "beside" / "c.pack.parquet"
    src.parent.mkdir()
    src.write_bytes(b"pack")
    dst = tmp_path / "cache" / "c.pack.parquet"
    dst.parent.mkdir()
    (dst.parent / ".c.pack.parquet.4242.adopting").write_bytes(b"half")
    assert derived.adopt(src, dst) is True and dst.read_bytes() == b"pack"
    assert not (dst.parent / ".c.pack.parquet.4242.adopting").exists()


def test_the_group_cache_is_budgeted_in_bytes_and_keeps_the_unit_just_read():
    c = backend.GroupCache(100)
    for i in range(5):
        c.put(("f", i), f"group {i}", 40)
    assert len(c) == 2 and c.nbytes == 80 and c.get(("f", 3)) == "group 3" and c.get(("f", 0)) is None
    c.get(("f", 3))                                   # the most recent now
    c.put(("f", 9), "big", 500)                       # over the budget alone: kept, the rest dropped
    assert len(c) == 1 and c.get(("f", 9)) == "big" and c.nbytes == 500
    c.put(("g", 1), "x", 10)
    c.forget(lambda k: k[0] == "f")
    assert c.sizes() == [10] and c.nbytes == 10
    c.clear()
    assert len(c) == 0 and c.nbytes == 0


def test_no_reader_keeps_a_cache_counted_in_items():
    """Five readers kept five LRUs, four counted in groups or frames whose
    size the data decides; every read cache is a GroupCache now."""
    hits = [str(p.relative_to(ROOT)) for p in (ROOT / "app").rglob("*.py")
            if re.search(r"_MAX_(GROUPS|FRAMES)\b", p.read_text(encoding="utf-8"))]
    assert hits == []
