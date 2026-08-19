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


def mark_if_settled(cursor: dict, path: Path, started_ns: int) -> bool:
    """Remember `path` only if it was last modified BEFORE this scan began.

    The timestamp race this closes: something lands in a directory while
    the scan is reading it (or within the filesystem's timestamp
    granularity of it). Marking then stores a signature that already
    includes the change, so the next scan sees "unchanged" and skips it —
    permanently. Refusing to mark costs one re-examination next scan and
    cannot lose an update.

    Matters most for DIRECTORIES, whose size rarely moves, so mtime is
    doing all the work; container files usually change size too, which is
    why the plain mark() has served them.

    Best effort, not a guarantee: directory timestamp granularity is
    coarse (measured in-container, a file created 5 ms after its parent
    left the parent's mtime untouched), so a change landing inside that
    window can still look older than the scan. What this rules out is the
    much wider window of a scan that takes minutes. The Library's "rescan"
    button clears the cursor for everything else."""
    sig = signature(path)
    if sig[0] < started_ns:
        cursor[path.name] = sig
        return True
    return False


def save(state_dir: Path | None, plugin: str, cursor: dict) -> None:
    if state_dir is None:
        return
    path = state_dir / f"{plugin}_scan_cursor.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cursor), encoding="utf-8")
    tmp.replace(path)


def clear(state_dir: Path | None, plugin: str) -> bool:
    """Forget everything: the next scan re-examines the whole corpus.

    The cursor's signature is (mtime_ns, size) of the container or book
    directory, which catches every way this platform writes — acquisition
    always lands bytes with a rename into place, and a rename changes the
    directory. It cannot catch a file rewritten IN PLACE by something
    outside the platform (an external mirror running `rsync --inplace`
    over an existing file). This is the escape hatch for that case, wired
    to the Library's "rescan" button so an operator has a real forced
    full rescan rather than a documented file to delete by hand."""
    if state_dir is None:
        return False
    path = state_dir / f"{plugin}_scan_cursor.json"
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False
