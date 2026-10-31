"""A collection's vector table rewritten with Lance's STABLE ROW IDS
(operator, 2026-10-31: "Rewrite wikipedia now").

A table created before `embeddings.NEW_TABLE_STORAGE` re-maps its whole
vector index into new files at every compaction that moves indexed rows:
wikipedia's twins repair wrote eight 17.8 GiB copies in four hours. The
feature cannot be switched on in place, so the table is copied, with every
row and every byte unchanged — RULE 1, nothing re-embedded — into a new one
that has it, indexed there, verified, and swapped in.

THE STAGES, each recorded in `rewrite-stable.json` beside the table before
the next begins (invariant 2: resumable, and safe to run again):

1. **copy** — the source, checked out at the version the rewrite began
   from, a group of fragments per append; the state records the fragments
   and rows done after each commit. A run stopped between a commit and its
   record finds the extra rows on resume and counts that group done. A
   stop (SIGTERM) finishes the group in flight and exits 130.
2. **verify** — the same rows, the same schema, and a sample of rows whose
   id, text and vector bytes are compared with the source's.
3. **index** — the vector index, the `book_id` and `lang` indexes built on the copy
   by the collection's own rule (`vector_index.build_table`), BEFORE the
   swap: searches never fall back to a scan of 146 M rows.
4. **swap** — under the vector write lock: refused when the source moved
   since the rewrite began (a write landed: the copy lacks it), else the
   source renamed aside and the copy renamed in. The old table is KEPT
   until `drop_old`, once every process has reopened the new one.

Only the vector table is touched: a records build may run beside it. The
collection is expected PAUSED (no embed, no maintainer pass); the version
check at the swap is what makes that safe rather than assumed.

Measure on the node (CLAUDE.md, multi-threading): the copy is Lance's own
reader and writer, which use their own threads; if `docker top` shows one
core at 100 %, the groups go to a thread pool of `write_fragments` calls
committed together.
"""
from __future__ import annotations

import json
import os
import random
import shutil
from pathlib import Path
from typing import Any, Callable

from . import embeddings, vector_index

TMP = f"{embeddings.TABLE}__stable"
OLD = f"{embeddings.TABLE}__prestable"
STATE = "rewrite-stable.json"
#: source fragments per append: wikipedia's 325 in ~41 commits
GROUP = 8
#: rows compared byte for byte with the source before the swap
SAMPLE = 2_000
#: free space asked beyond the source's data before the copy starts
HEADROOM = 1.10


class RewriteError(RuntimeError):
    """The rewrite cannot go on as asked; nothing it has not finished is
    swapped in."""


def _paths(collection) -> tuple[Path, Path, Path, Path]:
    d = Path(collection.lance_dir)
    return (d / f"{embeddings.TABLE}.lance", d / f"{TMP}.lance", d / f"{OLD}.lance", d / STATE)


def _bytes(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file() and not f.is_symlink())


def _load(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save(path: Path, state: dict[str, Any]) -> None:
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def plan(collection) -> dict[str, Any]:
    """What a rewrite would do, read-only."""
    import lance
    src, tmp, old, state_path = _paths(collection)
    if not src.exists():
        return {"table": None}
    ds = lance.dataset(str(src))
    data = _bytes(src / "data")
    return {"table": str(src), "rows": ds.count_rows(), "version": ds.version,
            "fragments": len(ds.get_fragments()), "stable_row_ids": bool(ds.has_stable_row_ids),
            "data_gib": round(data / 2**30, 1),
            "free_gib": round(shutil.disk_usage(src.parent).free / 2**30, 1),
            "needs_gib": round(data * HEADROOM / 2**30, 1),
            "state": _load(state_path) or None, "old_kept": old.exists()}


def run(collection, lock_con=None, *, stop=None, log: Callable[[str], None] | None = None,
        group: int = GROUP, restart: bool = False) -> dict[str, Any]:
    """The four stages, from wherever the state says. Returns what it did;
    `{"interrupted": True, …}` when `stop` was set between two units."""
    import lance
    say = log or (lambda m: None)
    src, tmp, old, state_path = _paths(collection)
    if not src.exists():
        raise RewriteError(f"{collection.name} has no vector table")
    live = lance.dataset(str(src))
    state = _load(state_path)
    if live.has_stable_row_ids and state.get("stage") != "swapped":
        return {"unchanged": True, "rows": live.count_rows(),
                "reason": "the table already has stable row ids"}
    if old.exists() and state.get("stage") != "swapped":
        raise RewriteError(f"{old} is there from an earlier rewrite: check it and remove it "
                           f"(`dev rewrite-vectors {collection.name} --drop-old`) first")
    if restart or not state or (state.get("stage") != "swapped"
                                and state.get("version") != live.version):
        # a first run, or the table moved since the copy began: from the start
        if tmp.exists():
            shutil.rmtree(tmp)
        state = {"version": live.version, "fragments": 0, "rows": 0, "stage": "copy",
                 "source_rows": live.count_rows()}
        _save(state_path, state)
    if state["stage"] == "copy":
        src_ds = lance.dataset(str(src), version=state["version"])
        frags = src_ds.get_fragments()
        need = _bytes(src / "data") * HEADROOM - _bytes(tmp)
        free = shutil.disk_usage(src.parent).free
        if free < need:
            raise RewriteError(f"the copy needs {need / 2**30:.0f} GiB more and {free / 2**30:.0f} GiB "
                               f"are free on {src.parent}; nothing written")
        done = lance.dataset(str(tmp)).count_rows() if tmp.exists() else 0
        if done != state["rows"]:
            k = state["fragments"]
            nxt = frags[k:k + group]
            if nxt and done == state["rows"] + src_ds.scanner(fragments=nxt).count_rows():
                state.update(fragments=k + len(nxt), rows=done)     # committed, not recorded
                _save(state_path, state)
            else:
                raise RewriteError(f"the copy holds {done:,} rows where {state['rows']:,} were "
                                   f"recorded: run again with --restart")
        while state["fragments"] < len(frags):
            if stop is not None and stop.is_set():
                return {"interrupted": True, **state}
            k = state["fragments"]
            part = frags[k:k + group]
            reader = src_ds.scanner(fragments=part).to_reader()
            if tmp.exists():
                lance.write_dataset(reader, str(tmp), mode="append")
            else:
                lance.write_dataset(reader, str(tmp), mode="create", enable_stable_row_ids=True,
                                    data_storage_version=src_ds.data_storage_version)
            state.update(fragments=k + len(part), rows=lance.dataset(str(tmp)).count_rows())
            _save(state_path, state)
            say(f"copied {state['fragments']}/{len(frags)} fragments, {state['rows']:,} rows")
        state["stage"] = "verify"
        _save(state_path, state)
    if state["stage"] == "verify":
        verify(lance.dataset(str(src), version=state["version"]), lance.dataset(str(tmp)), log=say)
        state["stage"] = "index"
        _save(state_path, state)
    if state["stage"] == "index":
        if stop is not None and stop.is_set():
            return {"interrupted": True, **state}
        import lancedb
        tbl = lancedb.connect(str(src.parent)).open_table(TMP)
        state["btree"] = vector_index.build_scalar_table(tbl, log=say)
        if vector_index.lang_indexed(tbl) is False:
            state["bitmap"] = vector_index.build_lang_table(tbl, log=say)
        state["index"] = vector_index.build_table(tbl, log=say)
        state["stage"] = "swap"
        _save(state_path, state)
    if state["stage"] == "swap":
        with embeddings._write_lock(lock_con, collection):
            now = lance.dataset(str(src)).version
            if now != state["version"]:
                raise RewriteError(f"the table was written during the rewrite (version "
                                   f"{state['version']} → {now}): the copy lacks those rows, "
                                   f"nothing swapped; run again with --restart")
            os.replace(src, old)
            os.replace(tmp, src)
            embeddings.forget(collection)
            vector_index.forget(collection)
        state["stage"] = "swapped"
        _save(state_path, state)
        say(f"swapped: {src} has stable row ids; the old table kept at {old}")
    new = lance.dataset(str(src))
    return {"stage": state["stage"], "rows": new.count_rows(),
            "stable_row_ids": bool(new.has_stable_row_ids),
            "indices": sorted(i["name"] for i in new.list_indices()),
            "old_kept": str(old) if old.exists() else None}


def verify(source, copy, *, sample: int = SAMPLE, seed: int | None = None,
           log: Callable[[str], None] | None = None) -> dict[str, Any]:
    """The copy is the source: the same rows, the same schema, and `sample`
    rows whose id, text and vector are byte-identical. Raises RewriteError."""
    n = source.count_rows()
    if copy.count_rows() != n:
        raise RewriteError(f"the copy holds {copy.count_rows():,} rows, the source {n:,}")
    if copy.schema != source.schema:
        raise RewriteError(f"the copy's schema differs from the source's:\n{copy.schema}\n"
                           f"against\n{source.schema}")
    rng = random.Random(seed)
    picks = sorted(rng.sample(range(n), min(sample, n)))
    want = source.take(picks, columns=["id", "text", "vector"]).to_pylist()
    by_id = {r["id"]: r for r in want}
    got: dict[str, dict] = {}
    ids = sorted(by_id)
    for i in range(0, len(ids), 500):
        quoted = ",".join("'" + x.replace("'", "''") + "'" for x in ids[i:i + 500])
        for r in copy.to_table(columns=["id", "text", "vector"],
                               filter=f"id IN ({quoted})").to_pylist():
            got[r["id"]] = r
    for uid, r in by_id.items():
        c = got.get(uid)
        if c is None or c["text"] != r["text"] or c["vector"] != r["vector"]:
            raise RewriteError(f"row {uid} differs between the copy and the source")
    if log:
        log(f"verified: {n:,} rows, the schema, and {len(by_id):,} sampled rows byte for byte")
    return {"rows": n, "sampled": len(by_id)}


def drop_old(collection, *, log: Callable[[str], None] | None = None) -> dict[str, Any]:
    """The table the swap set aside, removed once every process reads the
    new one (the worker and the web restarted, a search answered)."""
    src, _tmp, old, state_path = _paths(collection)
    if not old.exists():
        return {"dropped": False, "reason": "no old table"}
    import lance
    if not lance.dataset(str(src)).has_stable_row_ids:
        raise RewriteError(f"{src} has no stable row ids: the old table is not dropped")
    size = _bytes(old)
    shutil.rmtree(old)
    if log:
        log(f"dropped {old}: {size / 2**30:.1f} GiB")
    state = _load(state_path)
    if state:
        _save(state_path, {**state, "old_dropped_gib": round(size / 2**30, 1)})
    return {"dropped": True, "gib": round(size / 2**30, 1)}
