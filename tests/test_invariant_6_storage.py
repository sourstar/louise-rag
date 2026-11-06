"""Invariant 6 (storage), mechanically.

- NOTHING VACUUMs A MANIFEST. Implicit rowids are load-bearing (the
  catalog index, app/catalog.py, is keyed on `documents.rowid`) and VACUUM
  may renumber them. No SQL string in app/, tools/ or deploy/ says VACUUM
  (`auto_vacuum` / `incremental_vacuum` move pages, not rowids, and are
  not matched).
- MANIFESTS OPEN IN WAL. A function that opens a manifest for writing
  sets `journal_mode=WAL` itself (`db.wal`), because the mode is the
  file's and the first opener of a fresh file decides it: a rollback-
  journal manifest blocks every reader while anyone writes. Read-only
  opens (`mode=ro`) are exempt — they cannot create the file.
- THE ARCHIVE AND THE AUTHORITATIVE STATE ARE NEVER UNDER LOUISE_CACHE_DIR.
  `config.CACHE_DIR` is read by app/derived.py alone (app/parallel.py
  hands the knob to its children, app/config.py defines it); everything
  else reaches the fast drive through `derived.place`, which only
  plugins' derived packs call. And by behaviour: with a cache dir set, a
  node's collection paths and its manifest land outside it.
"""
import ast
import io
import re
import sqlite3
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app"

_VACUUM = re.compile(r"\bVACUUM\b", re.IGNORECASE)


def vacuum_strings(source: str) -> list[str]:
    """Every string literal of the code that says VACUUM — strings only, so
    a comment explaining why nothing vacuums is not one."""
    return [tok.string for tok in tokenize.generate_tokens(io.StringIO(source).readline)
            if tok.type == tokenize.STRING and _VACUUM.search(tok.string)
            and not tok.string.lstrip("rbuRBU").startswith(('"""', "'''"))]


def test_nothing_vacuums_a_manifest():
    files = [*APP.rglob("*.py"), *(ROOT / "tools").rglob("*.py"),
             *(ROOT / "deploy").rglob("*.py"), *(ROOT / "deploy").rglob("*.sh"),
             *(ROOT / "tools").rglob("*.sh")]
    offenders = []
    for p in files:
        src = p.read_text(encoding="utf-8", errors="replace")
        if p.suffix == ".sh":
            hits = [line for line in src.splitlines()
                    if _VACUUM.search(line) and not line.lstrip().startswith("#")]
        else:
            hits = vacuum_strings(src)
        if hits:
            offenders.append(f"{p.relative_to(ROOT).as_posix()}: {hits}")
    assert offenders == [], (
        "invariant 6: nothing VACUUMs a manifest — VACUUM may renumber implicit rowids, "
        f"and the catalog index is keyed on documents.rowid: {offenders}")


def test_the_vacuum_guard_sees_sql_and_not_prose():
    assert vacuum_strings('con.execute("VACUUM")\n') == ['"VACUUM"']
    assert vacuum_strings("con.execute('vacuum into ?', (p,))\n") == ["'vacuum into ?'"]
    assert vacuum_strings("# nothing VACUUMs a manifest\nx = 1\n") == []
    assert vacuum_strings('def f():\n    """Nothing will VACUUM here."""\n') == []
    assert vacuum_strings('con.execute("PRAGMA auto_vacuum=NONE")\n') == []


# ---------------- manifests open in WAL ----------------

def _is_sqlite_connect(call: ast.Call) -> bool:
    f = call.func
    return isinstance(f, ast.Attribute) and f.attr == "connect" \
        and isinstance(f.value, ast.Name) and f.value.id == "sqlite3"


def _read_only(call: ast.Call) -> bool:
    return any("mode=ro" in ast.unparse(a) for a in [*call.args, *(k.value for k in call.keywords)])


class _Opens(ast.NodeVisitor):
    """For each function: does it open a manifest for writing, and does it
    set WAL. Nested functions are judged on their own."""

    def __init__(self, source: str):
        self.source = source
        self.found: list[tuple[str, int, bool]] = []    # (function, line, sets WAL)

    def _visit_fn(self, node):
        body_src = ast.get_source_segment(self.source, node) or ""
        writes = []
        for sub in self._own_nodes(node):
            if isinstance(sub, ast.Call) and _is_sqlite_connect(sub) and not _read_only(sub):
                writes.append(sub.lineno)
        if writes and "manifest" in body_src:
            sets_wal = bool(re.search(r"\bwal\(|journal_mode\s*=\s*WAL", body_src, re.IGNORECASE))
            self.found.extend((node.name, line, sets_wal) for line in writes)
        self.generic_visit(node)

    visit_FunctionDef = visit_AsyncFunctionDef = _visit_fn

    @staticmethod
    def _own_nodes(fn):
        stack = list(ast.iter_child_nodes(fn))
        while stack:
            n = stack.pop()
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue
            yield n
            stack.extend(ast.iter_child_nodes(n))


def manifest_opens(source: str) -> list[tuple[str, int, bool]]:
    v = _Opens(source)
    v.visit(ast.parse(source))
    return v.found


def test_every_write_open_of_a_manifest_sets_wal():
    seen, offenders = [], []
    for p in sorted(APP.rglob("*.py")):
        for fn, line, sets_wal in manifest_opens(p.read_text(encoding="utf-8")):
            where = f"{p.relative_to(ROOT).as_posix()}:{line} ({fn})"
            seen.append(where)
            if not sets_wal:
                offenders.append(where)
    assert any("app/db.py" in s and "(connect)" in s for s in seen), \
        f"db.connect was not seen opening a manifest — the guard is wrong: {seen}"
    assert offenders == [], (
        "invariant 6: these open a manifest for writing without setting WAL (db.wal(con)); "
        f"the first opener of a fresh file decides its journal mode: {offenders}")


def test_the_wal_guard_tells_the_cases_apart():
    ok = "def f(manifest):\n    con = sqlite3.connect(manifest)\n    db.wal(con)\n"
    bad = "def f(manifest):\n    con = sqlite3.connect(manifest)\n"
    ro = "def f(manifest):\n    con = sqlite3.connect(f'file:{manifest}?mode=ro', uri=True)\n"
    other = "def f(p):\n    con = sqlite3.connect(p / 'labels.sqlite')\n"
    assert manifest_opens(ok) == [("f", 2, True)]
    assert manifest_opens(bad) == [("f", 2, False)]
    assert manifest_opens(ro) == [] and manifest_opens(other) == []


def test_a_manifest_opened_by_the_node_is_in_wal(isolated_config):
    from app import collections as collections_mod, db
    con = db.connect(collections_mod.default_collection())
    assert con.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_the_coverage_map_s_opener_makes_a_fresh_manifest_in_wal(tmp_path):
    """share/coverage.manifest() may be the FIRST to open a collection's
    manifest (the web reads the map before the worker's first beat)."""
    from app.share import coverage
    con = coverage.manifest(tmp_path, "fresh")
    con.close()
    p = tmp_path / "fresh" / "state" / "manifest.sqlite"
    with sqlite3.connect(p) as c:
        assert c.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


# ---------------- LOUISE_CACHE_DIR holds derived data only ----------------

_CACHE_READERS = {
    "app/config.py": "defines it (identity tier, 07 §7.8)",
    "app/derived.py": "the one place that resolves it: `derived.place(kind, origin)`",
    "app/parallel.py": "hands the knob to the parse pool's children unchanged",
}


def _names(path: Path) -> set[str]:
    src = path.read_text(encoding="utf-8")
    return {tok.string for tok in tokenize.generate_tokens(io.StringIO(src).readline)
            if tok.type == tokenize.NAME}


def test_only_derived_resolves_the_cache_dir():
    readers = {p.relative_to(ROOT).as_posix() for p in APP.rglob("*.py") if "CACHE_DIR" in _names(p)}
    env_readers = {p.relative_to(ROOT).as_posix() for p in APP.rglob("*.py")
                   if re.search(r"""getenv\(\s*["']LOUISE_CACHE_DIR""", p.read_text(encoding="utf-8"))}
    assert env_readers == {"app/config.py"}, f"LOUISE_CACHE_DIR read from the env by {env_readers}"
    assert readers - set(_CACHE_READERS) == set(), (
        "invariant 6: only app/derived.py resolves LOUISE_CACHE_DIR (config.CACHE_DIR); "
        f"these read it too: {sorted(readers - set(_CACHE_READERS))}")
    assert "app/derived.py" in readers, "the guard is wrong: derived.py was not seen"


#: Who puts something on the fast drive, and what: each is DERIVED,
#: re-creatable from what the node holds elsewhere, never exchanged.
_DERIVED_WRITERS = {
    "app/plugins/pubmed/pack.py": "the per-container pack, rebuilt from the gz XML",
    "app/plugins/wikipedia/page_props.py": "the page-props pack, rebuilt from the dump's SQL",
    "app/plugins/wikidata/pack.py": "the pack and label table, rebuilt from the dump",
    "app/ask/part_cache.py": "per-part search facts, rebuilt from the part on a miss",
}


def test_the_cache_dir_holds_named_derived_data_only():
    """`derived.place` is called for the derived data named above and by
    nothing that holds the archive or the authoritative state."""
    callers = {p.relative_to(ROOT).as_posix() for p in APP.rglob("*.py")
               # `place_kept` is `place`, kept per process: a caller all the same
               if p.name != "derived.py" and re.search(r"\bderived\.place(_kept)?\(",
                                                       p.read_text(encoding="utf-8"))}
    assert callers, "the guard is wrong: nothing calls derived.place"
    assert callers - set(_DERIVED_WRITERS) == set(), (
        "invariant 6: LOUISE_CACHE_DIR is for derived, re-creatable data; a new writer is "
        f"named here with what it puts there: {sorted(callers - set(_DERIVED_WRITERS))}")
    assert set(_DERIVED_WRITERS) - callers == set(), \
        f"stale entries: {sorted(set(_DERIVED_WRITERS) - callers)}"


def test_with_a_cache_dir_set_the_archive_and_state_land_outside_it(isolated_config, tmp_path,
                                                                    monkeypatch):
    from app import collections as collections_mod, config, db, derived, node_state
    cache = tmp_path / "fast-drive"
    cache.mkdir()
    monkeypatch.setattr(config, "CACHE_DIR", cache)
    collections = collections_mod.load_all()
    assert collections, "no collection loaded — the fixture is wrong"
    for c in collections:
        for p in (c.manifest_path, c.fts_dir, c.lance_dir, c.corpus_dir):
            assert cache not in Path(p).resolve().parents, f"{c.name}: {p} is under LOUISE_CACHE_DIR"
        db.connect(c).close()
    node_state.connect().close()
    assert sorted(cache.rglob("*")) == [], "something authoritative was written to the cache dir"
    # and the one thing that does go there, goes there
    assert derived.place("pack", tmp_path / "origin") .parent.parent == cache
