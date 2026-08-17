"""Container scan cursor — incremental discovery for container plugins.

DiscoverState.state_dir (reserved by base.py's own docstring for exactly
this: "a PubMed baseline+updatefiles cursor") lets a plugin remember which
container files it has already enumerated, keyed by (mtime_ns, size). An
unchanged container is skipped WITHOUT opening it — at full-PubMed scale
that turns an hour of re-parsing ~1,270 × 16 MB XML files per scan into a
stat pass, and keeps discovery's in-memory ref buffer proportional to what
actually changed instead of to the whole corpus.

Safe by construction: the orchestrator (app/discovery.py) only upserts what
discover() yields and never prunes unseen documents, so skipping yields
nothing to un-know. The cursor lives beside the manifest, so wiping a
collection's data directory wipes its cursor with it.
"""
import json
from pathlib import Path


def load(state_dir: Path | None, plugin: str) -> dict:
    if state_dir is None:
        return {}
    path = state_dir / f"{plugin}_scan_cursor.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def signature(path: Path) -> list:
    st = path.stat()
    return [st.st_mtime_ns, st.st_size]


def unchanged(cursor: dict, path: Path) -> bool:
    return cursor.get(path.name) == signature(path)


def mark(cursor: dict, path: Path) -> None:
    cursor[path.name] = signature(path)


def save(state_dir: Path | None, plugin: str, cursor: dict) -> None:
    if state_dir is None:
        return
    path = state_dir / f"{plugin}_scan_cursor.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cursor), encoding="utf-8")
    tmp.replace(path)
