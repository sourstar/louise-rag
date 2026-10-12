"""Two trees of a backend engine, diffed file by file (docs/notes/vega.md §12).

A backend engine's `image` is the digest of its installed directory's
per-file list (09 §9.3; deploy/vega/vega_wrapper.py `tree_manifest`). On
2026-10-12 that digest moved once between two loads of DAR-legal-it's
backend with no file under it changed, and the list it had been computed from
no longer existed anywhere — VeGa kept the digest alone. Since then the
wrapper tells VeGa the list beside each run it states, VeGa keeps it under
its digest (`GET /vega/trees/<image>`), and this module says what differs
between two of them: the paths added, removed and changed, each with its
entry on either side. Nothing here is resolved by similarity: a diff is a
set difference on paths and an equality on entries.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Mapping

from .tag import DIGEST

Entry = dict[str, Any]


def diff(before: Mapping[str, Entry], after: Mapping[str, Entry]) -> dict[str, Any]:
    """`{"added": {path: entry}, "removed": {path: entry},
    "changed": {path: {"before": entry, "after": entry}}, "same": <count>}`,
    paths sorted; empty objects where nothing differs."""
    b, a = dict(before), dict(after)
    added = {p: a[p] for p in sorted(set(a) - set(b))}
    removed = {p: b[p] for p in sorted(set(b) - set(a))}
    changed = {p: {"before": b[p], "after": a[p]}
               for p in sorted(set(a) & set(b)) if a[p] != b[p]}
    same = len(set(a) & set(b)) - len(changed)
    return {"added": added, "removed": removed, "changed": changed, "same": same}


def lines(d: Mapping[str, Any]) -> list[str]:
    """The diff as one line a path, `+`/`-`/`~` first, the entry's essentials
    after — what a person reads to name the byte that moved."""
    out: list[str] = []
    for path, e in d["added"].items():
        out.append(f"+ {path}  {_brief(e)}")
    for path, e in d["removed"].items():
        out.append(f"- {path}  {_brief(e)}")
    for path, pair in d["changed"].items():
        out.append(f"~ {path}  {_brief(pair['before'])} -> {_brief(pair['after'])}")
    out.append(f"= {d['same']} unchanged")
    return out


def _brief(e: Mapping[str, Any]) -> str:
    t = e.get("type")
    if t == "file":
        return f"file {e.get('size')} B sha256:{str(e.get('sha256', ''))[:12]}…" \
               f"{' exec' if e.get('exec') else ''}"
    if t == "link":
        return f"link -> {e.get('target')}"
    return str(t)


def read(source: str, *, vega_url: str | None = None,
         get: Callable[[str], Any] | None = None) -> dict[str, Entry]:
    """A tree from a digest (read from VeGa: `<vega_url>/vega/trees/<image>`)
    or from a JSON file holding the list — the wrapper's manifest object, or
    an object with a `tree` key wrapping it."""
    if DIGEST.fullmatch(source):
        if not vega_url:
            raise ValueError(f"{source[:23]}… is a digest and no VeGa URL is set (VEGA_URL) "
                             f"to read it from")
        url = f"{vega_url.rstrip('/')}/vega/trees/{source}"
        if get is None:
            import httpx
            r = httpx.get(url, timeout=120.0)
            if r.status_code == 404:
                raise ValueError(f"VeGa was never told the tree {source[:23]}…")
            r.raise_for_status()
            data = r.json()
        else:
            data = get(url)
    else:
        data = json.loads(Path(source).read_text(encoding="utf-8"))
    if isinstance(data, dict) and isinstance(data.get("tree"), dict) and "type" not in data["tree"]:
        data = data["tree"]
    if not isinstance(data, dict) or not all(isinstance(v, dict) for v in data.values()):
        raise ValueError(f"{source}: not a tree's per-file list (an object of path: entry)")
    return data
