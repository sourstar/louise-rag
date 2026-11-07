"""deploy/vega/retired_backends.py — the backends and wrappers in LocalAI's
backends path that nothing names any more, listed with their sizes, and
removed with `--delete`.

The operator, 2026-11-07: "It is important to delete the retired backends as
a practice, to avoid reusing a wrong one" — a card measurement had run on
louise-sycl-f16-llama-cpp-16acc89, an engine retired by a swap and still
installed beside the deployed louise-sycl-f16-llama-cpp-ea2417d91dcc
(approved-encoders.md, "Retiring an engine after a swap").

    python3 -I deploy/vega/retired_backends.py                 # list
    python3 -I deploy/vega/retired_backends.py --delete        # remove them
    python3 -I deploy/vega/retired_backends.py --engine [WRAPPER]
        # the engine the deployed wrapper runs (default vega-louise-llama-cpp),
        # read from its vega.json: what a measurement names (measuring.md §6)

WHAT NAMES A BACKEND. A model file in LocalAI's models path names one by its
`backend` line. A wrapper (a directory holding `vega.json`, vega_wrapper.py's
package) names its engine by `backend`, and the directory that holds VeGa's
socket by `receipts_socket`. LocalAI's install record (`metadata.json`) makes a
backend answer for its `name` and `alias`, and a meta backend name its concrete
one (`meta_backend_for`). LIVE is what the model files name, closed over those
links: a YAML names a wrapper, the wrapper's vega.json names an engine. Every
other top-level directory is RETIRED, so a wrapper and the engine only it names
go together. What the tool cannot read it keeps, and says why:
- a model file is any regular file of the models path whose name holds `.yaml`
  or `.yml`, backups (`x.yaml.bak-20261017`) included — a backup is a backout,
  and counting one only keeps a backend; full-line `#` comments do not count;
- a model file that names no backend lets LocalAI choose one, so every
  upstream backend (not named `louise-…` or `vega-…`, the operator's rule of
  2026-11-03) is kept while one exists;
- an unreadable vega.json or metadata.json keeps its directory;
- dot-named entries are LocalAI's (`.downloading`, localai-bugs.md B15; the
  install tars) and plain files are no backend: neither is examined.

IN USE IS REFUSED. Before anything is removed every process is read from
/proc — its `cwd`, `exe`, open files (`fd/`) and mapped files (`maps`) — and a
retired directory any of them uses is refused. A process in LocalAI's container
sees the directory under another path (`/backends`, `--container-backends`), so
a file is matched by its device and inode first and by either path second (a
mapping of a file already unlinked keeps only its path). If /proc cannot be
read — not listed, no process in it, a process's files not readable — the
deletion is refused whole: no guess.

THE STANDARD ALTERNATIVES, and why not (CLAUDE.md, do not reinvent the wheel):
- LocalAI's own `POST /backends/delete/<name>` (louise-python-rebuild.md §2)
  removes one installed backend by name. It knows no vega.json, no directory
  that is not a backend (backends-retired-20261102 was one), and checks no
  process; it would pay as the removal step once LocalAI reports which model
  files name each backend.
- `lsof +D DIR` does the in-use read (the same /proc files, by device and
  inode). It is not on every host, and its exit status is 1 both when nothing
  uses DIR and when it could not read a process, so "unreadable: refuse"
  cannot be told from "unused: remove"; it would pay with a reliable status.

Standard library only, Python 3.11 or later: it runs on the node's host.
Exit 0: listed, or every retired entry removed; 1: one was refused (in use) or
could not be removed; 2: nothing decided (an unreadable models path or /proc,
a wrapper without vega.json).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

#: The node's paths (node-runbook.md §3: LocalAI's models and backends on
#: ssd5). Argument defaults only: nothing below reads them.
NODE_BACKENDS = Path("/mnt/ssd5/apps/localai/backends")
NODE_MODELS = Path("/mnt/ssd5/apps/localai/models")
#: The same directory as LocalAI's container, and every backend it starts,
#: sees it (rollout-p2.sh: `ocifile:///backends/…`, the wrappers' SOCKET).
NODE_CONTAINER_BACKENDS = "/backends"
PROC = Path("/proc")
#: The one wrapper of every llama.cpp model on the card (approved-encoders.md,
#: "Names", operator 2026-11-03).
DEFAULT_WRAPPER = "vega-louise-llama-cpp"
WRAPPER_CONFIG = "vega.json"
INSTALL_RECORD = "metadata.json"
#: Our backends' names (operator, 2026-11-03): `vega-louise-…` wrapped,
#: `louise-…` not; `vega-…` the older wrappers (vega.md §1).
OURS = ("louise-", "vega-")
#: A `backend` key in a model file, YAML or JSON, at any depth; its value.
_BACKEND_KEY = re.compile(r"""(?<![\w-])["']?backend["']?\s*:\s*["']?([^\s"',#}\]]+)""")


class Undecidable(RuntimeError):
    """What the tool must read to decide could not be read: it decides nothing."""


@dataclass
class Entry:
    """A top-level directory of the backends path."""
    name: str
    path: Path
    kind: str                                         # wrapper | backend | directory
    answers_for: set[str] = field(default_factory=set)            # metadata name, alias
    links: list[tuple[str, str]] = field(default_factory=list)    # (raw value, what names it)
    named: list[tuple[str, str]] = field(default_factory=list)    # (entry, what names it)
    unreadable: str | None = None


@dataclass
class Inventory:
    backends: Path
    model_files: int
    live: dict[str, list[str]]                        # entry -> what names it, or why kept
    retired: list[Entry]
    retired_by: dict[str, list[str]]                  # retired entry -> retired entries naming it
    not_examined: list[str]


@dataclass
class Retired:
    entry: Entry
    bytes: int
    inodes: set[tuple[int, int]]
    unreadable: int                                   # directories the size walk could not read


# ---- what names what ------------------------------------------------------------------------

def model_references(models: Path) -> tuple[dict[str, list[str]], list[str], int]:
    """{backend: [model files naming it]}, the model files naming none, and how
    many model files there are. An unreadable models path or file decides nothing."""
    try:
        names = sorted(e.name for e in os.scandir(models)
                       if e.is_file() and (".yaml" in e.name or ".yml" in e.name))
    except OSError as e:
        raise Undecidable(f"cannot list the models path {models}: {e.strerror or e}") from None
    refs: dict[str, list[str]] = {}
    unnamed: list[str] = []
    for name in names:
        try:
            text = (models / name).read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            raise Undecidable(f"cannot read the model file {models / name}: "
                              f"{e.strerror or e}") from None
        found = {m.group(1) for line in text.splitlines()
                 if not line.lstrip().startswith("#") for m in _BACKEND_KEY.finditer(line)}
        for backend in found:
            refs.setdefault(backend, []).append(name)
        if not found:
            unnamed.append(name)
    return refs, unnamed, len(names)


def _read_json(path: Path) -> dict:
    doc = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(doc, dict):
        raise ValueError("not a JSON object")
    return doc


def read_entries(backends: Path) -> tuple[dict[str, Entry], list[str]]:
    """The top-level directories (or links to directories) of the backends
    path, with what each names; and the names not examined."""
    entries: dict[str, Entry] = {}
    skipped: list[str] = []
    try:
        children = sorted(os.scandir(backends), key=lambda e: e.name)
    except OSError as e:
        raise Undecidable(f"cannot list the backends path {backends}: {e.strerror or e}") from None
    for child in children:
        if child.name.startswith(".") or not child.is_dir():
            skipped.append(child.name)
            continue
        path = Path(child.path)
        config, record = path / WRAPPER_CONFIG, path / INSTALL_RECORD
        kind = ("wrapper" if config.is_file() else
                "backend" if record.is_file() or (path / "run.sh").exists() else "directory")
        e = Entry(child.name, path, kind)
        if child.is_symlink():
            e.links.append((os.path.realpath(path), "link"))
        try:
            if kind == "wrapper":
                doc = _read_json(config)
                for key in ("backend", "receipts_socket"):
                    if isinstance(doc.get(key), str) and doc[key]:
                        e.links.append((doc[key], f"{WRAPPER_CONFIG} {key}"))
            if record.is_file():
                doc = _read_json(record)
                e.answers_for |= {doc[k] for k in ("name", "alias")
                                  if isinstance(doc.get(k), str) and doc[k]}
                if isinstance(doc.get("meta_backend_for"), str) and doc["meta_backend_for"]:
                    e.links.append((doc["meta_backend_for"], f"{INSTALL_RECORD} meta_backend_for"))
        except (OSError, ValueError) as err:
            e.unreadable = f"{type(err).__name__}: {err}"
        entries[e.name] = e
    return entries, skipped


def _prefixes(backends: Path, container_backends: str) -> tuple[str, ...]:
    """The backends path as this host and as LocalAI's container name it."""
    return tuple(dict.fromkeys((os.path.realpath(backends), str(backends), container_backends)))


def _entries_of(value: str, names: set[str], prefixes: tuple[str, ...]) -> list[str]:
    """The entries a path or name in a config names: a relative value by its
    first component; an absolute one under a prefix by the component after it,
    and otherwise by any component that is an entry's name (a path this tool
    cannot place still keeps what it might name)."""
    p = PurePosixPath(value)
    if not p.is_absolute():
        return [p.parts[0]] if p.parts else []
    for prefix in prefixes:
        try:
            rel = p.relative_to(prefix)
        except ValueError:
            continue
        return [rel.parts[0]] if rel.parts else []
    return [part for part in p.parts[1:] if part in names]


def inventory(backends: Path, models: Path, container_backends: str) -> Inventory:
    entries, skipped = read_entries(backends)
    refs, unnamed, n_models = model_references(models)
    names = set(entries)
    prefixes = _prefixes(backends, container_backends)
    for e in entries.values():
        for value, how in e.links:
            e.named += [(n, f"{e.name}/{how}") for n in _entries_of(value, names, prefixes)
                        if n in names and n != e.name]
    answering: dict[str, list[str]] = {}
    for e in entries.values():
        for alias in {e.name} | e.answers_for:
            answering.setdefault(alias, []).append(e.name)

    live: dict[str, list[str]] = {}
    todo = [(backend, f) for backend, files in refs.items() for f in files]
    for e in entries.values():
        if e.unreadable:
            todo.append((e.name, f"its own config, unreadable ({e.unreadable})"))
        elif unnamed and e.kind != "directory" and not e.name.startswith(OURS):
            todo.append((e.name, f"LocalAI chooses the backend of {_namers(unnamed, 1)}, "
                                 f"which names none"))
    while todo:
        ref, by = todo.pop()
        for name in answering.get(ref, []):
            first = name not in live
            live.setdefault(name, []).append(by)
            if first:
                todo += entries[name].named
    retired = [e for e in entries.values() if e.name not in live]
    retired_by = {e.name: sorted({by.split("/")[0] for r in retired for n, by in r.named
                                  if n == e.name}) for e in retired}
    return Inventory(backends, n_models, {k: sorted(set(v)) for k, v in live.items()},
                     retired, retired_by, skipped)


def deployed_engine(backends: Path, wrapper: str = DEFAULT_WRAPPER) -> str:
    """The engine the deployed wrapper runs: its vega.json's `backend`, the
    directory's name under the backends path. What a measurement names and
    loads (measuring.md §6) — never a name from a note or a message."""
    config = backends / wrapper / WRAPPER_CONFIG
    try:
        backend = _read_json(config).get("backend")
    except (OSError, ValueError) as e:
        raise Undecidable(f"cannot read {config}: {e}") from None
    if not isinstance(backend, str) or not backend:
        raise Undecidable(f"{config} names no `backend`")
    return PurePosixPath(backend).name if backend.startswith("/") else PurePosixPath(backend).parts[0]


# ---- sizes and the processes that use a path ------------------------------------------------

def measure(entry: Entry) -> Retired:
    """Bytes on disk (each inode once) and every (device, inode) of the tree,
    links never followed; a link entry is the link alone."""
    st = os.lstat(entry.path)
    seen = {(st.st_dev, st.st_ino)}
    total, bad = st.st_blocks * 512, 0
    stack = [] if entry.path.is_symlink() else [entry.path]
    while stack:
        try:
            with os.scandir(stack.pop()) as it:
                for de in it:
                    s = de.stat(follow_symlinks=False)
                    if (s.st_dev, s.st_ino) not in seen:
                        seen.add((s.st_dev, s.st_ino))
                        total += s.st_blocks * 512
                    if de.is_dir(follow_symlinks=False):
                        stack.append(Path(de.path))
        except OSError:
            bad += 1
    return Retired(entry, total, seen, bad)


def _under(path: str, prefixes: tuple[str, ...]) -> str | None:
    p = PurePosixPath(path.removesuffix(" (deleted)"))
    for prefix in prefixes:
        try:
            rel = p.relative_to(prefix)
        except ValueError:
            continue
        return rel.parts[0] if rel.parts else None
    return None


def users(proc: Path, items: list[Retired], prefixes: tuple[str, ...]) -> dict[str, list[str]]:
    """{retired entry: ["pid N cwd", …]} for every process using a file of it.
    Raises Undecidable when /proc cannot be read."""
    by_inode = {key: r.entry.name for r in items for key in r.inodes}
    wanted = {r.entry.name for r in items}
    try:
        pids = sorted(int(n) for n in os.listdir(proc) if n.isdigit())
    except OSError as e:
        raise Undecidable(f"cannot list {proc}: {e.strerror or e}") from None
    if not pids:
        raise Undecidable(f"{proc} lists no process: it is not the process table")
    found: dict[str, set[str]] = {}

    def hit(key: tuple[int, int] | None, path: str | None, pid: int, how: str) -> None:
        name = by_inode.get(key) if key else None
        if name is None and path:
            under = _under(path, prefixes)
            name = under if under in wanted else None
        if name:
            found.setdefault(name, set()).add(f"pid {pid} {how}")

    for pid in pids:
        base = proc / str(pid)
        try:
            links = [(base / how, how) for how in ("cwd", "exe")]
            links += [(base / "fd" / fd, "fd") for fd in os.listdir(base / "fd")]
            for link, how in links:
                try:
                    st = os.stat(link)
                    target = os.readlink(link)
                except (FileNotFoundError, ProcessLookupError):
                    continue                    # a kernel thread's exe, a closed fd
                hit((st.st_dev, st.st_ino), target, pid, how)
            with open(base / "maps", encoding="utf-8", errors="replace") as maps:
                for line in maps:
                    parts = line.split(None, 5)
                    if len(parts) < 5 or parts[4] == "0":
                        continue
                    major, _, minor = parts[3].partition(":")
                    key = (os.makedev(int(major, 16), int(minor, 16)), int(parts[4]))
                    hit(key, parts[5].strip() if len(parts) == 6 else None, pid, "maps")
        except (FileNotFoundError, ProcessLookupError):
            continue                            # it ended while being read
        except PermissionError as e:
            raise Undecidable(f"cannot read {e.filename or base}: {e.strerror}; "
                              f"the in-use check reads every process (root)") from None
    return {name: sorted(v, key=lambda s: int(s.split()[1])) for name, v in found.items()}


# ---- the report ------------------------------------------------------------------------------

def _gb(n: int) -> str:
    return f"{n:,} B ({n / 1e9:.2f} GB)"


def _namers(why: list[str], most: int = 3) -> str:
    return ", ".join(why[:most]) + (f" (+{len(why) - most})" if len(why) > most else "")


def _free(path: Path) -> int:
    st = os.statvfs(path)
    return st.f_bavail * st.f_frsize


def run(backends: Path, models: Path, container_backends: str, proc: Path,
        delete: bool, out=sys.stdout) -> int:
    inv = inventory(backends, models, container_backends)

    def say(line: str) -> None:
        print(line, file=out, flush=True)

    say(f"backends {backends}; {inv.model_files} model files in {models}")
    width = max([len(n) for n in inv.live] + [len(e.name) for e in inv.retired] + [8])
    kinds = {e.name: e.kind for e in inv.retired}
    for name in sorted(inv.live):
        say(f"live     {name:<{width}}  named by {_namers(inv.live[name])}")
    items = [measure(e) for e in inv.retired]
    for r in items:
        by = inv.retired_by[r.entry.name]
        why = f"named only by {', '.join(by)} (retired)" if by else "named by nothing"
        partial = f", {r.unreadable} directories unreadable" if r.unreadable else ""
        say(f"retired  {r.entry.name:<{width}}  {kinds[r.entry.name]}, {_gb(r.bytes)}{partial}; {why}")
    if inv.not_examined:
        say(f"not examined (dot-named or not a directory): {', '.join(inv.not_examined)}")
    total = sum(r.bytes for r in items)
    prefixes = _prefixes(backends, container_backends)
    if not delete:
        try:
            busy = users(proc, items, prefixes) if items else {}
        except Undecidable as e:
            say(f"in use: not read ({e}); --delete would refuse")
            busy = {}
        for name, who in busy.items():
            say(f"in use   {name}: {', '.join(who)}")
        say(f"retired: {len(items)}, {_gb(total)}"
            + ("; --delete removes them" if items else ""))
        return 0
    if not items:
        say("retired: none; nothing removed")
        return 0
    busy = users(proc, items, prefixes)                 # Undecidable: refused whole
    before = _free(backends)
    removed, failed = [], 0
    for r in items:
        if r.entry.name in busy:
            say(f"refused  {r.entry.name}: in use by {', '.join(busy[r.entry.name])}")
            failed += 1
            continue
        try:
            if r.entry.path.is_symlink():
                r.entry.path.unlink()
            else:
                shutil.rmtree(r.entry.path)
        except OSError as e:
            say(f"FAILED   {r.entry.name}: {e}")
            failed += 1
            continue
        removed.append(r)
        say(f"removed  {r.entry.name}  {_gb(r.bytes)}")
    after = _free(backends)
    freed = sum(r.bytes for r in removed)
    say(f"removed {len(removed)} of {len(items)}: {_gb(freed)} counted; free space on "
        f"{backends} {before / 1e9:.2f} -> {after / 1e9:.2f} GB ({(after - before) / 1e9:+.2f} GB)")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="retired_backends", allow_abbrev=False,
                                description=__doc__.split("\n\n")[0])
    p.add_argument("--backends", type=Path, default=NODE_BACKENDS,
                   help=f"LocalAI's backends path, as this host sees it ({NODE_BACKENDS})")
    p.add_argument("--models", type=Path, default=NODE_MODELS,
                   help=f"LocalAI's models path ({NODE_MODELS})")
    p.add_argument("--container-backends", default=NODE_CONTAINER_BACKENDS,
                   help="the backends path as LocalAI's container sees it "
                        f"({NODE_CONTAINER_BACKENDS})")
    p.add_argument("--proc", type=Path, default=PROC, help="the process table (/proc)")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--delete", action="store_true",
                      help="remove every retired entry no process uses")
    mode.add_argument("--engine", nargs="?", const=DEFAULT_WRAPPER, metavar="WRAPPER",
                      help=f"print the engine WRAPPER's vega.json names ({DEFAULT_WRAPPER})")
    a = p.parse_args(argv)
    try:
        if a.engine:
            engine = deployed_engine(a.backends, a.engine)
            if not (a.backends / engine).is_dir():
                print(f"retired_backends: {a.engine}/{WRAPPER_CONFIG} names {engine}, which "
                      f"is not installed in {a.backends}", file=sys.stderr)
                return 2
            print(engine)
            return 0
        return run(a.backends, a.models, a.container_backends, a.proc, a.delete)
    except Undecidable as e:
        print(f"retired_backends: {e}; nothing removed", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
