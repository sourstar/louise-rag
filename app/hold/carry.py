"""hold.carry — a collection's local state carried into a new collection with
the same records. `louise dev carry <from> <to> [--copy] [--keep producer:<a>]`
(07 §7.3; docs/notes/v5-migration.md G1, G2; amendment A1 of 02 §2.4/§2.7 and
03 §3.5).

RULE 1. A version-5/6 Codex is a new collection, and a by-the-book one would
re-derive its manifest, its text index and its vector table from records that
are byte for byte the old collection's. A1 lets a vector table cross into a
collection with the same records whose space differs ONLY by engine, or by a
declared input form equal to the one its vectors were produced in — once
`vectors-sample` under a validated run passes at the target's tolerance, before
use. So this moves (or with `--copy` copies) the three local things —
`state/manifest.sqlite` (after a TRUNCATE checkpoint), `index/`, `lance/` —
re-pins the manifest's `space` row to the target's Codex, and clears the
source's coverage rows (they name the source's collection and space).

What it refuses (nothing is written): a record-deciding difference
(hold/seed.py's agreement); a space that differs in anything but the engine
and the layers' input forms (weights, dim, dtype, ctx, pooling, normalise, the
query side, the epoch); a target whose latest snapshot's parts are not the
source's; a source table pinned to another space or of another width; a
non-empty target. The caller holds BOTH collections' write locks.

`keep_producer` (G2) then drops every vector that is not an archive's: rows
whose chunk has a `chunk_verification` row of that producer in state
`imported` or `verified` stay; every other row is deleted through
`embeddings.delete_chunks` and its document's `embed_done` reset, a window of
documents at a time, each window in three steps that can each be repeated —
the reset committed first (the safe direction: a document marked not done
whose vectors still exist is re-embedded, never lost), the rows deleted under
the vector write lock, the cursor committed last. Never one transaction over
the corpus (db.py's apply_reset says what that did on pubmed).
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from .codex import TOKEN_CUT, Codex, CodexError
from .seed import latest_snapshot, refuse_unless_same_records

MARKER = "carry.json"
MANIFEST = "manifest.sqlite"
#: SQLite's companions of a database file, moved with it.
_COMPANIONS = ("-wal", "-shm")
#: chunk_verification states that are an archive's vectors (ledger.py).
KEPT_STATES = ("imported", "verified")
KEEP_KEY = "carry_keep:"
WINDOW = 500


class CarryMismatch(CodexError):
    """Carried, and the vector table does not hold what the keep says it should."""
    exit_code = 1


def records_twin_of(state_dir) -> str | None:
    """The collection whose records this one holds byte for byte BY A CARRY:
    the source's collection_id from this collection's own marker, when the
    carry finished and was not `--rebuilt` — such a carry refuses unless the
    records and the latest snapshot's parts are the source's (A1). None
    otherwise. Local state, what this peer proved here: an annotations
    collection about the source applies to its records twin (gutenberg's
    A1f twin, 2026-10-20: the editions' claims named the old Codex)."""
    try:
        held = json.loads((Path(state_dir) / MARKER).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if held.get("done") and not held.get("rebuilt") and held.get("from"):
        return str(held["from"])
    return None


# ---- what must agree -------------------------------------------------------

def space_differences(a: Codex, b: Codex) -> list[str]:
    """A1: the space may differ by ENGINE and by the layers' input forms, and
    by nothing else that makes the function. `[]` when it is so."""
    def fn(c: Codex) -> dict[str, Any]:
        s = c.space
        q = s.query
        return {"encoder.weights": s.encoder.weights, "dim": s.dim, "dtype": s.dtype,
                "ctx": s.ctx, "pooling": s.pooling, "normalise": s.normalise,
                "query.encoder.weights": q.encoder.weights if q.encoder else None,
                "query.instruction": q.instruction, "epoch": s.epoch,
                "cleaning_version": c.cleaning_version,
                # the cut is the function's (Codex version 8): crossed by --recut alone
                "cut": s.cut}
    fa, fb = fn(a), fn(b)
    return [k for k in fa if fa[k] != fb[k]]


def _part_entries(snap) -> dict[str, list]:
    return {"shards": sorted((s.slice, s.n, s.root, s.records_root, s.chunks_root, s.records,
                              s.chunks, s.bytes, s.first_id, s.last_id) for s in snap.shards),
            "relations": sorted((r.slice, r.n, r.root, r.edges, r.bytes) for r in snap.relations),
            "figures": sorted((f.slice, f.n, f.root, f.figures, f.bytes) for f in snap.figures)}


# ---- the files -------------------------------------------------------------

def _paths(root: Path, name: str) -> dict[str, Path]:
    base = Path(root) / name
    return {"manifest": base / "state" / MANIFEST, "index": base / "index",
            "lance": base / "lance", "state": base / "state"}


def _empty_dir(p: Path) -> bool:
    return not p.exists() or (p.is_dir() and not any(p.iterdir()))


#: A1f (2026-10-20): what a carry into a space that ADMITS the source's
#: (`Codex.admitted_spaces`: the same document vectors) also moves — the
#: bundles, filed under their own space segment, and the seeder's state
#: (torrents, the bundle roots cache, the coverage union)
SHARED_STATE = ("bundles", "share")


def _move_state(src: Path, dst: Path) -> str:
    """`src` (a directory, or the link to `BUNDLES_ROOT/<name>` it may be)
    renamed to `dst`, the link as a link. An empty `dst` — a directory, or
    a link to an empty one that `create` made — goes first; a non-empty one
    is refused. Repeatable: `src` gone and `dst` there is an earlier run."""
    if not src.exists() and not src.is_symlink():
        return "present" if dst.exists() or dst.is_symlink() else "absent"
    if dst.is_symlink():
        target = dst.resolve()
        if target.is_dir() and any(target.iterdir()):
            raise CodexError(f"carry: {dst} links to {target}, which is not empty; a carry never overwrites")
        dst.unlink()
        if target.is_dir():
            target.rmdir()
    elif dst.is_dir():
        if any(dst.iterdir()):
            raise CodexError(f"carry: {dst} is not empty; a carry never overwrites")
        dst.rmdir()
    dst.parent.mkdir(parents=True, exist_ok=True)
    os.replace(src, dst)
    return "moved"


def _rekey_union(share: Path, old_prefix: str, new_prefix: str) -> int:
    """The coverage union's cache keyed on the new collection's shard ids —
    the same parts and bundles, so nothing is read again (RULE 1)."""
    from app.share import counting
    p = share / counting.CACHE
    try:
        got = json.loads(p.read_bytes())
    except (OSError, ValueError):
        return 0
    if not isinstance(got, dict):
        return 0
    out = {(new_prefix + k[len(old_prefix):] if k.startswith(old_prefix) else k): v for k, v in got.items()}
    tmp = p.with_suffix(".json.tmp")
    tmp.write_bytes(json.dumps(out, sort_keys=True).encode())
    os.replace(tmp, p)
    return sum(1 for k in got if k.startswith(old_prefix))


# ---- the predecessor's rows: provenance, not coverage ------------------------
#
# gutenberg's parser-10 carry (2026-10-22): the SAME space, a rebuilt target.
# Clearing the source's coverage rows deleted the only record of which rows
# the node had LANDED from another producer's bundles, and its held path then
# bundled everything it held as its own. Re-keying them would be worse for a
# rebuilt target: its parts are re-cut (222 of 1,639 identical), a bundle may
# span two new parts, and its signed header names the old collection — offered
# as the new one's coverage, every ladder would refuse it. So the rows stay
# UNKEYED, under their own collection's shard ids: never coverage of the new
# collection, never announced, read by the held path (contribute.claimed_uids)
# as provenance — which uids another producer's accepted bundle already holds.

def _link_tree(src: Path, dst: Path) -> dict[str, int]:
    """Every file under `src` HARD-LINKED at the same relative path under
    `dst` — the bundles' bytes held once (RULE 1), each bundle's
    possession.json readable at the path its row names. An existing file is
    left as it is; across filesystems a link cannot be made and the file is
    copied."""
    out = {"linked": 0, "copied": 0, "present": 0}
    src = src.resolve() if src.exists() else src
    if not src.is_dir():
        return out
    for base, _dirs, files in os.walk(src):
        rel = Path(base).relative_to(src)
        for f in files:
            s, d = Path(base) / f, dst / rel / f
            if d.exists():
                out["present"] += 1
                continue
            d.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.link(s, d)
                out["linked"] += 1
            except OSError:
                shutil.copy2(s, d)
                out["copied"] += 1
    return out


def _predecessors_of(state: Path, src_id: str) -> list[str]:
    """`src_id` and the collections IT was carried from in the same space, as
    its own carry marker records them — so provenance survives a chain of
    re-mints."""
    try:
        held = json.loads((state / MARKER).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        held = {}
    chain = list(held.get("predecessors") or []) if held.get("same_space") else []
    return [src_id, *[c for c in chain if c != src_id]]


def keep_predecessor_rows(root, from_ref: str, to_ref: str, *, apply: bool = False,
                          log: Callable[[str], None] = print) -> dict[str, Any]:
    """For a same-space carry that ran before provenance was kept (gutenberg
    p9 → p10, 2026-10-22): the source's coverage rows copied UNKEYED into the
    target's manifest, its bundle tree hard-linked into the target's, and the
    carry marker naming the predecessors. Repeatable."""
    from . import collection as col
    root = Path(root)
    from_name, src = col.resolve(root, from_ref)
    to_name, to = col.resolve(root, to_ref)
    if src.space_id != to.space_id:
        raise CodexError(f"{from_name} and {to_name} are not one space: their rows are another function's")
    sp, tp = _paths(root, from_name), _paths(root, to_name)
    marker = tp["state"] / MARKER
    held = json.loads(marker.read_text(encoding="utf-8")) if marker.is_file() else {}
    if not (held.get("done") and held.get("from") == src.collection_id and held.get("to") == to.collection_id):
        raise CodexError(f"{to_name} records no finished carry from {from_name} ({marker})")
    prefix = cov_prefix(src)
    like = prefix.replace("%", r"\%").replace("_", r"\_") + "%"
    scon = sqlite3.connect(f"file:{sp['manifest']}?mode=ro", uri=True)
    try:
        rows = scon.execute("SELECT * FROM coverage WHERE shard LIKE ? ESCAPE '\\'", (like,)).fetchall()
        cols = [d[0] for d in scon.execute("SELECT * FROM coverage LIMIT 0").description]
    finally:
        scon.close()
    plan = {"from": from_name, "to": to_name, "rows": len(rows), "apply": apply}
    if not apply:
        log(f"[carry] would keep {len(rows)} coverage row(s) of {from_name} in {to_name}, unkeyed "
            f"(provenance, not coverage), and link its bundle tree (--yes)")
        return plan
    tcon = sqlite3.connect(tp["manifest"], timeout=60, isolation_level=None)
    try:
        from app import db
        db.wal(tcon)                                    # invariant 6: a manifest is WAL
        tcon.execute("BEGIN IMMEDIATE")
        before = tcon.total_changes
        tcon.executemany(f"INSERT OR IGNORE INTO coverage({','.join(cols)}) "
                         f"VALUES({','.join('?' * len(cols))})", rows)
        plan["rows_written"] = tcon.total_changes - before
        tcon.execute("COMMIT")
    except BaseException:
        if tcon.in_transaction:
            tcon.execute("ROLLBACK")
        raise
    finally:
        tcon.close()
    plan["bundles"] = _link_tree(sp["state"] / "bundles", tp["state"] / "bundles")
    marker.write_text(json.dumps({**held, "same_space": True,
                                  "predecessors": _predecessors_of(sp["state"], src.collection_id)},
                                 sort_keys=True), encoding="utf-8")
    log(f"[carry] {to_name}: {plan['rows_written']} of {len(rows)} coverage row(s) of {from_name} kept "
        f"unkeyed; bundle tree {plan['bundles']}")
    return plan


def _documents(manifest: Path) -> int | None:
    if not manifest.is_file():
        return None
    con = sqlite3.connect(f"file:{manifest.as_posix()}?mode=ro", uri=True)
    try:
        return int(con.execute("SELECT COUNT(*) FROM documents").fetchone()[0])
    except sqlite3.OperationalError:
        return 0                                    # no documents table: nothing held
    finally:
        con.close()


def _pin(manifest: Path) -> tuple[str | None, int | None]:
    con = sqlite3.connect(f"file:{manifest.as_posix()}?mode=ro", uri=True)
    try:
        row = con.execute("SELECT space_id, dim FROM space WHERE id=1").fetchone()
    except sqlite3.OperationalError:
        row = None
    finally:
        con.close()
    return (row[0], row[1]) if row else (None, None)


def _copy_tree(src: Path, dst: Path) -> None:
    """A reflink where the filesystem has one (`cp --reflink=auto`), else a
    copy; `dst` must not exist."""
    cp = shutil.which("cp")
    if cp:
        r = subprocess.run([cp, "-a", "--reflink=auto", str(src), str(dst)],
                           capture_output=True, text=True)
        if r.returncode == 0:
            return
        if dst.exists():
            shutil.rmtree(dst) if dst.is_dir() else dst.unlink()
    if src.is_dir():
        shutil.copytree(src, dst, symlinks=True)
    else:
        shutil.copy2(src, dst)


def _transfer(src: Path, dst: Path, *, copy: bool) -> str:
    """`src` to `dst`, whole or not at all: a rename where it can, else a copy
    to a hidden name renamed into place (and, moving, the source removed
    after). Repeatable: a `dst` already there is what an earlier run put."""
    if dst.exists() and not _empty_dir(dst):
        if not copy and src.exists():
            # a move that crossed devices was interrupted removing its source
            shutil.rmtree(src) if src.is_dir() else src.unlink()
        return "present"
    if not src.exists():
        return "present" if dst.exists() else "absent"
    if dst.is_dir():
        dst.rmdir()                                   # empty: db.connect makes them
    dst.parent.mkdir(parents=True, exist_ok=True)
    if not copy:
        try:
            os.replace(src, dst)
            return "moved"
        except OSError:
            pass                                       # another device: copy, then remove
    tmp = dst.with_name(f".{dst.name}.carry")
    if tmp.exists():
        shutil.rmtree(tmp) if tmp.is_dir() else tmp.unlink()
    _copy_tree(src, tmp)
    os.replace(tmp, dst)
    if copy:
        return "copied"
    shutil.rmtree(src) if src.is_dir() else src.unlink()
    return "moved"


def _checkpoint(manifest: Path) -> None:
    """Everything the WAL holds into the main file, the WAL truncated — the
    manifest is then one file, and the move cannot leave half of it behind."""
    con = sqlite3.connect(manifest, timeout=60)
    try:
        from app import db
        db.wal(con)                                     # invariant 6: a manifest is WAL
        busy, _log, _done = con.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    finally:
        con.close()
    if busy:
        raise CodexError(f"{manifest}: the checkpoint could not complete (a reader or "
                         f"writer holds it); stop whatever uses the collection and retry")


# ---- G1 --------------------------------------------------------------------

#: A1b (RULE 1, 2026-10-10, pubmed): with `--keep producer:<archive>` the rows
#: that stay were made by the ARCHIVE'S producer (NCBI's MedCPT), by neither
#: Codex's encoder file, so the two Codexes' encoder weights may differ — the
#: old space named the GGUF conversion, the new the OpenVINO one, of the same
#: model. What proves the kept rows is `vectors-sample` at the target's
#: tolerance before use (03 §3.5), as for any carry. Query vectors are never
#: stored, so the query side's weights do not reach the table either.
WEIGHTS_WITH_KEEP = frozenset({"encoder.weights", "query.encoder.weights"})
#: A1h: what a carry from a collection holding no vector may change — the
#: encoder's side of the space (the engine is outside `space_differences`)
ENCODER_SIDE = frozenset({"encoder.weights", "dim", "dtype", "ctx", "cut", "pooling", "normalise",
                          "query.encoder.weights", "query.instruction", "epoch"})


#: what may differ under `--rebuilt` (A1c): the parser and the image that runs
#: it. Cleaning, layers, chunkers, slicing and the rest still agree, so a
#: document's chunks are a function of its title and sections alone
#: (invariant 3) — `content_hash` decides, per document, at `reconcile`.
RECORDS_REBUILT = frozenset({"parser", "toolchain.platform_image"})


def origin_only_adds(a: Codex, b: Codex) -> bool:
    """A1d: `b`'s origin is `a`'s with files added — every file `a`'s records
    were read from still declared exactly as before, the filters only widened
    — and nothing else changed but `form`, the sentence saying which form was
    taken (wikipedia parser 5: each run's page_props beside its dump)."""
    oa, ob = a.origin.to_dict(), b.origin.to_dict()
    sa, sb = dict(oa.pop("spec") or {}), dict(ob.pop("spec") or {})
    oa.pop("form", None)
    ob.pop("form", None)
    if oa != ob:
        return False
    fa, fb = sa.pop("files", None) or [], sb.pop("files", None) or []
    la, lb = sa.pop("filters", None) or [], sb.pop("filters", None) or []
    return (sa == sb and all(f in fb for f in fa) and all(x in lb for x in la)
            and len(fb) > len(fa))


def unembedded_why_not(lance: Path, manifest: Path) -> str | None:
    """Why the collection at `lance`/`manifest` holds vectors, or None when it
    holds none (A1h): no row in its vector table, no document marked
    embedded. Read-only; the table is opened only when it exists."""
    from app import spaces
    rows = spaces.known_lance_rows(lance)
    if rows is None:
        return "its vector table cannot be read: not proven empty"
    if rows:
        return f"its vector table holds {rows} row(s)"
    con = sqlite3.connect(f"file:{manifest.as_posix()}?mode=ro", uri=True)
    try:
        if con.execute("SELECT 1 FROM documents WHERE embed_done > 0 LIMIT 1").fetchone():
            return "a document is marked embedded"
    finally:
        con.close()
    return None


def carry(root, from_ref: str, to_ref: str, *, copy: bool = False, apply: bool = False,
          keep: str | None = None, rebuilt: bool = False, recut: bool = False,
          resliced: bool = False, unembedded: bool = False,
          log: Callable[[str], None] = print) -> dict[str, Any]:
    """Carry `from_ref`'s manifest, text index and vector table into `to_ref`
    (G1). `apply=False` checks everything and writes nothing.

    `unembedded` (A1h, 2026-10-31; wikidata from jina-v3 to bge-m3, nothing
    embedded or published): the source holds NO vector, so the target's space
    may differ on the ENCODER'S side (`ENCODER_SIDE`: weights, dim, dtype,
    ctx, cut, pooling, normalisation, the query side, the epoch) — no vector
    depends on it. What cuts the chunks the text index and the manifest hold —
    the layers whole, their input forms, the cleaning — may not differ, and the
    records must be the same records; the manifest
    (its marks, the declined ones included) and the text index are functions
    of the records alone and move; the vector table does not (the target
    makes its own, of its own width). Its parts come from `hold records build
    <to> --records-from <from>`, so no snapshot is compared here. By the
    book, the target would discover, rdf and index 75.9 M documents again
    (~23 h on the node) to the same bytes.

    `resliced` (A1g, 2026-10-24; operator: a daughter with a new family map
    reuses "all the vectors and corpus of the mother"): the target differs
    only by its SLICING and cuts its parts by content (02 §2.2) — the same
    records, the same chunks, the same space, other parts. Its parts come
    from `hold records build <to> --records-from <from>` (the source's
    record lines, none parsed), so no snapshot is compared here; the
    coverage rows name the source's parts and are kept unkeyed.

    `rebuilt` (A1c, 2026-10-11): the target's records are BUILT, not seeded —
    its parser moved without moving cleaning or chunking (gutenberg's parser
    9: `origin_hash` over the catalogue entry too). The parser and the
    platform image may differ, no snapshot is compared here, and the carried
    state is unusable until `reconcile` has compared every document's
    `content_hash` in the two snapshots."""
    from . import collection as col
    from .seed import record_differences
    from app import spaces

    root = Path(root)
    from_name, src = col.resolve(root, from_ref)
    to_name, to = col.resolve(root, to_ref)
    if unembedded and (resliced or rebuilt or keep or recut):
        raise CodexError("carry --unembedded carries no vector: no --resliced, --rebuilt, --keep "
                         "or --recut with it")
    if resliced and (rebuilt or keep or recut):
        raise CodexError("carry --resliced moves the same records' vectors whole: no --rebuilt, "
                         "--keep or --recut with it")
    if resliced:
        from app.pack import slicing as sl
        if src.collection_id == to.collection_id:
            raise CodexError("carry: the source and the target are one collection")
        other = [d for d in record_differences(src, to) if d != "slicing"]
        if other or src.slicing == to.slicing:
            raise CodexError(
                f"carry --resliced: {to.collection_id} and {src.collection_id} differ in "
                f"{', '.join(other) or 'nothing'} — only the slicing may differ (A1g); anything else "
                f"moves records or chunks, and nothing is carried")
        if sl.parts_rule_of(to.slicing) is None:
            raise CodexError(f"carry --resliced: {to_name} cuts its parts by count; a re-slice "
                             f"needs parts cut by content (02 §2.2) to equal a build from scratch")
    elif rebuilt:
        if src.collection_id == to.collection_id:
            raise CodexError("carry: the source and the target are one collection")
        other = [d for d in record_differences(src, to) if d not in RECORDS_REBUILT
                 and not (d == "origin" and origin_only_adds(src, to))]
        if other:
            raise CodexError(
                f"carry --rebuilt: {to.collection_id} and {src.collection_id} differ in "
                f"{', '.join(other)} — only the parser and its image may differ (A1c), and "
                f"the origin only by files added beside the ones read (A1d); anything else "
                f"moves chunks, and nothing is carried")
    else:
        refuse_unless_same_records(src, to, "carry --unembedded" if unembedded else "carry")
    diff = space_differences(src, to)
    if unembedded:
        # the text index is keyed on chunk ids and the manifest counts chunks:
        # what cuts them — the layers whole, their input forms, the cleaning —
        # must be the same; only the ENCODER's side may differ
        layer_side = [d for d in diff if d not in ENCODER_SIDE]
        if [(l.chunker.to_dict(), l.rule, l.form) for l in src.layers] != \
                [(l.chunker.to_dict(), l.rule, l.form) for l in to.layers]:
            layer_side.append("layers")
        if layer_side:
            raise CodexError(
                f"carry --unembedded: {to.collection_id} and {src.collection_id} differ in "
                f"{', '.join(layer_side)}, which cut the chunks the text index and the manifest "
                f"hold (A1h admits the encoder's side only: {', '.join(sorted(ENCODER_SIDE))})")
        if diff:
            log(f"[carry] {from_name} → {to_name}: the spaces differ in {', '.join(diff)}; "
                f"{from_name} holds no vector, so nothing carried depends on them (A1h)")
        diff = []
    if keep:
        diff = [d for d in diff if d not in WEIGHTS_WITH_KEEP]
    if "query.instruction" in diff:
        # A1f (RULE 1, 2026-10-20): the instruction is prepended to QUERIES only
        # (embeddings._asked), so no row of the table depends on it; the target's
        # own calibration holds its query_vectors under the new instruction, and
        # its query runs are validated against those before the leg answers
        log(f"[carry] {from_name} → {to_name}: the query instruction differs "
            f"({src.space.query.instruction!r} → {to.space.query.instruction!r}); no stored "
            f"vector depends on it (A1f)")
        diff = [d for d in diff if d != "query.instruction"]
    if "cut" in diff:
        if not recut:
            raise CodexError(
                f"carry: {to_name} cuts by {to.space.cut or 'characters'} and {from_name} by "
                f"{src.space.cut or 'characters'}: the cut is part of the function (09 "
                f"§9.3). Only `--recut` (A1e) crosses it, keeping the rows both cuts make "
                f"alike and dropping the rest to be embedded again")
        if src.space.cut is not None or to.space.cut != TOKEN_CUT:
            raise CodexError(
                "carry --recut: from a space cut by characters into one cut by tokens, "
                "never the other way: a character cut is not reproducible (A1e)")
        diff = [d for d in diff if d != "cut"]
    elif recut:
        raise CodexError(f"carry --recut: {from_name} and {to_name} cut alike; there is "
                         f"nothing to recut")
    if diff:
        raise CodexError(
            f"carry: the spaces differ in {', '.join(diff)}. Amendment A1 lets a vector "
            f"table cross into a space that differs only by ENGINE or by a declared input "
            f"form (02 §2.4, 03 §3.5); any other difference is another function, and its "
            f"vectors are embedded again, never carried")
    if not rebuilt and not resliced and not unembedded:
        a, b = latest_snapshot(root, from_name), latest_snapshot(root, to_name)
        if _part_entries(a) != _part_entries(b):
            raise CodexError(
                f"carry: {to_name}'s latest snapshot ({b.snapshot_id}) does not name the parts "
                f"{from_name}'s ({a.snapshot_id}) names — seed it first "
                f"(`hold snapshot create {to_name} --shards-from {from_name}`)")

    # A1f: the target admits the source's space (only the query instruction
    # was added) — its bundles, coverage rows and seeder state are the
    # target's too, by the book, and travel with the table
    shares = src.space_id != to.space_id and src.space_id in (getattr(to, "admitted_spaces", ()) or ())
    # the SAME space, not A1f: the source's rows are provenance, kept unkeyed (2026-10-22)
    same = src.space_id == to.space_id
    sp, tp = _paths(root, from_name), _paths(root, to_name)
    marker = tp["state"] / MARKER
    claim = {"from": src.collection_id, "to": to.collection_id,
             "mode": "copy" if copy else "move"}
    if rebuilt:
        claim["rebuilt"] = True
    if resliced:
        claim["resliced"] = True
    if unembedded:
        claim["unembedded"] = True
    held = json.loads(marker.read_text(encoding="utf-8")) if marker.is_file() else None
    if held is not None and {k: held.get(k) for k in claim} != claim:
        raise CodexError(f"{marker} records another carry ({held}); this one is {claim}")

    plan: dict[str, Any] = {**claim, "from_name": from_name, "to_name": to_name,
                            "space_id": to.space_id, "apply": apply}
    if held is None:
        if not sp["manifest"].is_file():
            raise CodexError(f"carry: {from_name} has no manifest at {sp['manifest']}")
        pinned, _dim = _pin(sp["manifest"])
        lance_dim = spaces.known_lance_dim(sp["lance"])
        if pinned not in (None, src.space_id) or (pinned is None and lance_dim is not None):
            raise CodexError(
                f"carry: {from_name}'s vector table is pinned to {str(pinned)[:23]}…, not its "
                f"Codex's space {src.space_id[:23]}…; nothing says what its vectors are")
        if lance_dim not in (None, src.space.dim):
            raise CodexError(f"carry: {from_name}'s vector table is {lance_dim}-dimensional "
                             f"and its space is {src.space.dim}")
        if unembedded:
            why = unembedded_why_not(sp["lance"], sp["manifest"])
            if why:
                raise CodexError(
                    f"carry --unembedded: {from_name} holds vectors ({why}); they are its "
                    f"space's, and a carry across spaces never moves one (A1h). Without "
                    f"vectors, nothing carried depends on the space")
        docs = _documents(tp["manifest"])
        busy = [k for k in ("index", "lance") if not _empty_dir(tp[k])]
        if docs or busy:
            raise CodexError(
                f"carry: {to_name} is not empty ({'its manifest holds documents' if docs else ''}"
                f"{', ' if docs and busy else ''}{', '.join(busy)}); a carry never overwrites")
        plan["documents"] = _documents(sp["manifest"])
        if not apply:
            log(f"[carry] {from_name} → {to_name}: would {claim['mode']} the manifest "
                f"({plan['documents']} documents), index/"
                + (" (no vector: the target makes its own table, A1h)" if unembedded else " and lance/")
                + (", state/bundles and state/share — the bundles, coverage rows and torrents "
                   "too (A1f: the same document vectors)" if shares and not copy else "")
                + f", and re-pin to {to.space_id[:23]}…; nothing written (--yes)")
            return plan
        _checkpoint(sp["manifest"])
        for suffix in ("", *_COMPANIONS):            # an empty target's own manifest goes
            Path(f"{tp['manifest']}{suffix}").unlink(missing_ok=True)
        tp["state"].mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps(claim, sort_keys=True), encoding="utf-8")
    elif not apply:
        log(f"[carry] {from_name} → {to_name}: begun earlier; --yes finishes it")
        return {**plan, "begun": True}

    done: dict[str, str] = {}
    # A1h: no vector crosses spaces; the target's table is its own, of its width
    for k in (("index",) if unembedded else ("lance", "index")):
        done[k] = _transfer(sp[k], tp[k], copy=copy)
    # Moving: the companions before the main file — the main file in place is
    # what says the manifest arrived — so none is left beside a source that no
    # longer has its database. After the checkpoint the WAL is empty, so a copy
    # takes the main file alone (a copied `-shm` would describe another file).
    if not copy:
        for suffix in _COMPANIONS:
            _transfer(Path(f"{sp['manifest']}{suffix}"), Path(f"{tp['manifest']}{suffix}"),
                      copy=False)
    # A MANIFEST AT THE TARGET IS NOT PROOF OF AN EARLIER COPY (2026-10-13):
    # the worker created an empty one in normattiva-1861 two seconds after
    # this carry removed it, `_transfer` called it "present", and the copy
    # never happened — 318,349 documents' marks lost; moving, the SOURCE's
    # manifest would have been deleted as "an interrupted move". A target
    # manifest holding fewer documents than the source's is a stray, removed
    # with its companions, and the source is transferred.
    src_docs = _documents(sp["manifest"]) if sp["manifest"].is_file() else None
    # A MOVED SOURCE'S STRAY (2026-10-19, pubmed → pubmed-v8): after the move a
    # restart opened the source collection, whose file still names it, and
    # db.connect made it an empty manifest. Resumed, the move had happened —
    # the target holds the documents — and `_transfer` below removes the empty
    # file, moving: there is nothing to compare the target with.
    if (not copy and held is not None and src_docs == 0 and tp["manifest"].is_file()
            and (_documents(tp["manifest"]) or 0) > 0):
        log(f"[carry] {from_name}: an empty manifest appeared after its move (another "
            f"opener?); removed, the move stands")
        src_docs = None
    if src_docs and tp["manifest"].is_file() and (_documents(tp["manifest"]) or 0) < src_docs:
        log(f"[carry] {to_name}: a manifest of {_documents(tp['manifest']) or 0} document(s) "
            f"appeared at the target meanwhile (another writer?); replaced by {from_name}'s")
        for suffix in ("", *_COMPANIONS):
            Path(f"{tp['manifest']}{suffix}").unlink(missing_ok=True)
    done["manifest"] = _transfer(sp["manifest"], tp["manifest"], copy=copy)
    if not tp["manifest"].is_file():
        raise CodexError(f"carry: {tp['manifest']} is missing after the transfer")
    if src_docs is not None and _documents(tp["manifest"]) != src_docs:
        raise CarryMismatch(
            f"carry: {tp['manifest']} holds {_documents(tp['manifest'])} document(s) and "
            f"{from_name}'s manifest {src_docs}: the copy is not the source's. Nothing is "
            f"marked done; pause both collections and run the carry again")

    if shares and not copy:
        for k in SHARED_STATE:
            done[k] = _move_state(sp["state"] / k, tp["state"] / k)
        _rekey_union(tp["state"] / "share", cov_prefix(src), cov_prefix(to))
    elif shares:
        log(f"[carry] {from_name} → {to_name}: --copy leaves state/bundles and state/share with "
            f"{from_name}; the coverage rows are carried")
    elif same:
        sb, tb = sp["state"] / "bundles", tp["state"] / "bundles"
        done["bundles"] = (_move_state(sb, tb) if not copy
                           else "linked " + json.dumps(_link_tree(sb, tb), sort_keys=True))
    cleared = _repin(tp["manifest"], src, to, keep_coverage=shares, keep_unkeyed=same and not shares)
    plan.update(done=done, coverage_rows_cleared=cleared, documents=_documents(tp["manifest"]))
    final = {**claim, "done": True}
    if same and not shares:
        final.update(same_space=True, predecessors=_predecessors_of(sp["state"], src.collection_id))
    marker.write_text(json.dumps(final, sort_keys=True), encoding="utf-8")
    log(f"[carry] {from_name} → {to_name}: manifest {done['manifest']}, index/ "
        f"{done['index']}, lance/ {done.get('lance', 'not carried (A1h)')}"
        + (f", state/bundles {done.get('bundles')}, state/share {done.get('share')}" if shares and not copy else "")
        + f"; space re-pinned to {to.space_id[:23]}…; {cleared} coverage row(s) of {from_name} "
        + ("re-keyed to it (A1f)" if shares else "kept unkeyed (provenance, never its coverage)"
           if same else "cleared") + ". Before "
        + (f"use: `hold records build {to_name}`, `hold snapshot create {to_name}`, then "
           f"`dev carry {from_name} {to_name} --reconcile` (A1c) — or, where {from_name} "
           f"holds no records, `dev carry {from_name} {to_name} --resign` (A1d)" if rebuilt else
           f"use: `hold records build {to_name} --records-from {from_name}` (its parts, from "
           f"{from_name}'s record lines), `hold snapshot create {to_name}` (A1g)" if resliced else
           f"use: `hold records build {to_name} --records-from {from_name}`, `hold snapshot "
           f"create {to_name}`; its documents are embedded in its own space (A1h)" if unembedded else
           f"use: `hold check {to_name} --checks vectors-sample` (amendment A1)"))
    return plan


# ---- A1c: reconcile a rebuilt target --------------------------------------

RECONCILE_KEY = "carry_reconcile"


def _content_of(root: Path, name: str) -> dict[str, tuple[str, str | None]]:
    """uid → (content_hash, origin_hash) over the latest snapshot's records.
    One entry per record: 79k for gutenberg, a few MB."""
    from app.pack import records as pack_records
    from . import collection as col
    snap = latest_snapshot(root, name)
    d = col.shard_dir_of(root, name)
    out: dict[str, tuple[str, str | None]] = {}
    for s in snap.shards:
        for r in pack_records.iter_records(d / s.path):
            out[r["uid"]] = (r.get("content_hash"), r.get("origin_hash"))
    return out


def _cursor(con, key: str, pair: list[str], fresh: dict[str, Any]) -> dict[str, Any]:
    """A resumable pass's cursor in the manifest's `control` table, valid only
    for the collections it was written for: a manifest is COPIED by a carry,
    and gutenberg's parser-10 reconcile (2026-10-22) read the parser-9 one's
    finished cursor from the copy and did nothing — "done", with the old
    pass's counts. A cursor naming another pair, or none, starts over."""
    from app import db
    state = json.loads(db.get_control(con, key) or "{}")
    if state.get("pair") != pair:
        state = {**fresh, "pair": pair}
    return state


def reconcile(root, from_ref: str, to_ref: str, *, window: int = WINDOW,
              log: Callable[[str], None] = print) -> dict[str, Any]:
    """After `carry --rebuilt` and the target's own build and snapshot: every
    document whose `content_hash` is the source snapshot's keeps its vectors
    (its chunks are the same chunks, invariant 3); every other one — revised
    upstream, new, or gone from the target — loses them and is marked to embed
    again. The manifest's `txt_hash` becomes the target record's
    `origin_hash`, so the next scan compares like with like. A window of
    documents at a time, each in keep_producer's three repeatable steps;
    resumable from its cursor."""
    from . import collection as col, records as rec
    from app import db, embeddings

    root = Path(root)
    from_name, src = col.resolve(root, from_ref)
    to_name, to = col.resolve(root, to_ref)
    marker = _paths(root, to_name)["state"] / MARKER
    held = json.loads(marker.read_text(encoding="utf-8")) if marker.is_file() else {}
    if not (held.get("rebuilt") and held.get("done") and held.get("from") == src.collection_id
            and held.get("to") == to.collection_id):
        raise CodexError(f"reconcile: {to_name} records no finished `carry --rebuilt` from "
                         f"{from_name} ({marker})")
    before, after = _content_of(root, from_name), _content_of(root, to_name)
    coll = rec.local_collection(root, to_name, codex=to)
    con = db.connect(coll)
    try:
        state = _cursor(con, RECONCILE_KEY, [src.collection_id, to.collection_id],
                        {"rowid": 0, "kept": 0, "reset": 0, "rehashed": 0, "done": False})
        pair = state["pair"]
        while not state.get("done"):
            rows = con.execute("SELECT rowid, uid, native_id, txt_hash FROM documents "
                               "WHERE rowid > ? ORDER BY rowid LIMIT ?",
                               (state["rowid"], window)).fetchall()
            if not rows:
                state["done"] = True
                con.execute("BEGIN IMMEDIATE")
                db.set_control(con, RECONCILE_KEY, json.dumps(state, sort_keys=True))
                con.execute("COMMIT")
                break
            last = int(rows[-1]["rowid"])
            same = {r["uid"] for r in rows
                    if r["uid"] in after and r["uid"] in before
                    and after[r["uid"]][0] == before[r["uid"]][0]}
            stale = [r for r in rows if r["uid"] not in same]
            rehash = [(after[r["uid"]][1], r["uid"]) for r in rows
                      if r["uid"] in after and after[r["uid"]][1]
                      and r["txt_hash"] != after[r["uid"]][1]]
            pending = state.get("pending") or {}
            if pending.get("rowid") != last:
                # step 1: marked not done (and rehashed) FIRST — a document
                # marked whose vectors still exist is embedded again, never lost
                con.execute("BEGIN IMMEDIATE")
                for i in range(0, len(stale), 500):
                    part = [r["uid"] for r in stale[i:i + 500]]
                    marks = ",".join("?" * len(part))
                    con.execute(f"UPDATE documents SET embed_done=0 WHERE uid IN ({marks})", part)
                    con.execute(f"DELETE FROM chunk_verification WHERE uid IN ({marks})", part)
                con.executemany("UPDATE documents SET txt_hash=? WHERE uid=?", rehash)
                pending = {"rowid": last, "reset": len(stale), "rehashed": len(rehash)}
                db.set_control(con, RECONCILE_KEY,
                               json.dumps({**state, "pending": pending}, sort_keys=True))
                con.execute("COMMIT")
            # step 2: their rows, under the vector write lock
            if stale:
                books = sorted({int(r["native_id"]) for r in stale})
                gone = [c for c in embeddings.chunk_ids_of_books(books, collection=coll)
                        if c.rsplit(":", 2)[0] in {r["uid"] for r in stale}]
                embeddings.delete_chunks(gone, collection=coll, lock_con=con)
            # step 3: the cursor
            state = {"rowid": last, "kept": state["kept"] + len(same),
                     "reset": state["reset"] + pending["reset"],
                     "rehashed": state["rehashed"] + pending["rehashed"], "done": False,
                     "pair": pair}
            con.execute("BEGIN IMMEDIATE")
            db.set_control(con, RECONCILE_KEY, json.dumps(state, sort_keys=True))
            con.execute("COMMIT")
    finally:
        con.close()
    out = {"collection": to_name, "kept": state["kept"], "reset": state["reset"],
           "rehashed": state["rehashed"], "records_before": len(before),
           "records_after": len(after)}
    log(f"[carry] {to_name}: {state['kept']} document(s) keep their vectors (same "
        f"content_hash as {from_name}), {state['reset']} to embed again, "
        f"{state['rehashed']} txt_hash(es) set to their record's origin_hash. Before use: "
        f"`hold check {to_name} --checks vectors-sample` (A1c)")
    return out


# ---- A1d: re-sign a rebuilt target whose source holds no records -----------

RESIGN_KEY = "carry_resign"
#: documents re-chunked to prove the text did not move
RESIGN_SAMPLE = 2000


def _scanned(root: Path, name: str, codex, corpus_dir=None):
    """The collection as the WORKER discovers it — its own resolution
    (`collections.by_name`: corpus, pinned snapshot, credited archive) — or,
    where this root is not the node's configuration, the local side with the
    corpus `records build` would read and the snapshot the Codex pins."""
    from dataclasses import replace as _replace
    from . import records as rec
    from app import collections as collections_mod
    from app.acquire import spec as acquire_spec
    from app.acquire.specs import ACQUIRE_REGISTRY
    local = rec.local_collection(root, name, codex,
                                 corpus_dir=corpus_dir or (Path(root) / name / "origin"))
    try:
        worker = collections_mod.by_name(name)
    except Exception:   # noqa: BLE001 — no node configuration here
        worker = None
    if worker is not None and Path(worker.manifest_path) == Path(local.manifest_path) \
            and corpus_dir is None:
        return worker
    spec = ACQUIRE_REGISTRY.get(codex.plugin)
    pin = acquire_spec.pinned_snapshot(spec, codex.origin.spec) if spec is not None else None
    return _replace(local, snapshot=pin)


def resign(root, from_ref: str, to_ref: str, *, sample: int = RESIGN_SAMPLE, corpus_dir=None,
           log: Callable[[str], None] = print) -> dict[str, Any]:
    """After `carry --rebuilt`, where the source holds no records to reconcile
    against (02 §2.4 A1d; wikipedia parser 5, 2026-10-18): the target's own
    discovery, once, in `adopt` mode — every document takes the locator and
    signature the new parser gives it, `txt_hash` unset as a fresh scan leaves
    it, every stage mark and vector kept — and then the proof: a seeded sample
    of embedded documents, cut again by the target's parser and layers,
    holds byte for byte the chunk texts the vector table holds. A mismatch
    raises and the collection is not to be used. Repeatable: the scan
    rewrites nothing the second time."""
    from . import collection as col, records as rec
    from app import db, discovery

    root = Path(root)
    from_name, src = col.resolve(root, from_ref)
    to_name, to = col.resolve(root, to_ref)
    marker = _paths(root, to_name)["state"] / MARKER
    held = json.loads(marker.read_text(encoding="utf-8")) if marker.is_file() else {}
    if not (held.get("rebuilt") and held.get("done") and held.get("from") == src.collection_id
            and held.get("to") == to.collection_id):
        raise CodexError(f"resign: {to_name} records no finished `carry --rebuilt` from "
                         f"{from_name} ({marker})")
    try:
        latest_snapshot(root, from_name)
    except CodexError:
        pass
    else:
        raise CodexError(f"resign: {from_name} holds records — `--reconcile` compares every "
                         f"document's content_hash (A1c), which is the stronger proof")
    coll = _scanned(root, to_name, to, corpus_dir)
    con = db.connect(coll)
    try:
        scan = discovery.sync_collection(con, coll, adopt=True)
        if scan.get("error") or scan.get("interrupted"):
            raise CodexError(f"resign: {to_name}'s scan did not finish ({scan}); run it again")
        if scan.get("container_errors"):
            # a container the scan could not read (wikipedia: a run whose
            # page_props is not on disk yet) left its documents unsigned, and
            # the worker's next scan would read them as CHANGED and reset them
            raise CodexError(
                f"resign: {scan['container_errors']} container(s) of {to_name} could not be "
                f"enumerated (its event log names them); their documents are not re-signed. "
                f"Keep {to_name} paused, put them right and run it again")
        proof = _resign_proof(coll, con, seed=to.collection_id, n=sample)
        out = {"collection": to_name, "scan": scan, **proof}
        con.execute("BEGIN IMMEDIATE")
        db.set_control(con, RESIGN_KEY, json.dumps(
            {**out, "done": not proof["mismatched"]}, sort_keys=True))
        con.execute("COMMIT")
    finally:
        con.close()
    if proof["mismatched"]:
        raise CarryMismatch(
            f"resign: {len(proof['mismatched'])} of {proof['compared']} sampled document(s) of "
            f"{to_name} cut to chunks the vector table does not hold "
            f"({', '.join(proof['mismatched'][:5])}): the text moved, A1d does not apply. "
            f"Do not use {to_name}")
    log(f"[carry] {to_name}: {scan.get('resigned', 0)} document(s) re-signed, "
        f"{scan.get('added', 0)} new; {proof['compared']} sampled document(s) cut to exactly "
        f"the chunks the vector table holds ({proof['chunks']} chunks; "
        f"{proof['without_vectors']} sampled without vectors). A1d holds")
    return out


def _resign_proof(coll, con, *, seed: str, n: int) -> dict[str, Any]:
    """`n` embedded documents drawn by rowid under `seed`, each cut by the
    collection's parser and layers and compared, id and text, with the chunks
    the vector table holds for it."""
    import random
    from app import embeddings, layers, plugin_io
    plugin = coll.plugin_instance()
    top = con.execute("SELECT max(rowid) FROM documents").fetchone()[0] or 0
    rng = random.Random(seed)
    rows: dict[int, dict] = {}
    for _ in range(max(1, n) * 20):
        if len(rows) >= n or not top:
            break
        r = con.execute("SELECT rowid, uid, native_id, path FROM documents WHERE rowid=? "
                        "AND embed_done=1 AND error IS NULL", (rng.randint(1, top),)).fetchone()
        if r is not None:
            rows[int(r["rowid"])] = dict(r)
    picked = [rows[k] for k in sorted(rows)]
    stored = embeddings.held_chunks(coll, [int(r["native_id"]) for r in picked])
    compared, chunks, without, mismatched = 0, 0, 0, []
    for row in picked:
        have = {cid: v["text"] for cid, v in stored.of(row["native_id"]).items()}
        if not have:
            without += 1
            continue
        cut = {c["id"]: c["text"] for c in layers.chunks_for_document(
            coll, row, plugin_io.sections(plugin, row))}
        compared += 1
        chunks += len(cut)
        if cut != have:
            mismatched.append(row["uid"])
    return {"sampled": len(picked), "compared": compared, "chunks": chunks,
            "without_vectors": without, "mismatched": mismatched}


def cov_prefix(codex: Codex) -> str:
    """`<collection_id>/<space>/`: the prefix of every shard id of a collection."""
    from app.share import coverage as cov
    return cov.shard_id(codex, "")


def _repin(manifest: Path, src: Codex, to: Codex, *, keep_coverage: bool = False,
           keep_unkeyed: bool = False) -> int:
    """The `space` row to `to`'s Codex, and `src`'s coverage rows gone, in
    one transaction — or, `keep_coverage` (A1f: `to` admits `src`'s space,
    the same document vectors), re-keyed to `to`'s shard ids, so the parts
    the network covered stay covered and nothing is embedded or bundled
    again; or, `keep_unkeyed` (the same space), left under `src`'s shard ids
    as provenance (see `keep_predecessor_rows`), and counted. Repeatable: a
    second run finds nothing to clear or re-key."""
    from app.share import coverage as cov
    con = sqlite3.connect(manifest, timeout=60, isolation_level=None)
    try:
        from app import db
        db.wal(con)                                     # invariant 6: a manifest is WAL
        con.execute("BEGIN IMMEDIATE")
        con.execute("INSERT INTO space(id, space_id, dim, components, pinned_at) "
                    "VALUES(1, ?, ?, NULL, ?) ON CONFLICT(id) DO UPDATE SET "
                    "space_id=excluded.space_id, dim=excluded.dim, "
                    "components=NULL, pinned_at=excluded.pinned_at",
                    (to.space_id, to.space.dim, int(time.time())))
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        cleared = 0
        prefix = cov.shard_id(src, "")                # "<collection_id>/<space>/"
        like = prefix.replace("%", r"\%").replace("_", r"\_") + "%"
        for t in ("coverage", "coverage_shards"):
            if t not in tables:
                continue
            if keep_unkeyed:
                cleared += con.execute(f"SELECT COUNT(*) FROM {t} WHERE shard LIKE ? ESCAPE '\\'",
                                       (like,)).fetchone()[0]
            elif keep_coverage:
                cleared += con.execute(f"UPDATE {t} SET shard = ? || substr(shard, ?) "
                                       f"WHERE shard LIKE ? ESCAPE '\\'",
                                       (cov.shard_id(to, ""), len(prefix) + 1, like)).rowcount
            else:
                cleared += con.execute(f"DELETE FROM {t} WHERE shard LIKE ? ESCAPE '\\'",
                                       (like,)).rowcount
        con.execute("COMMIT")
        return cleared
    except BaseException:
        if con.in_transaction:
            con.execute("ROLLBACK")
        raise
    finally:
        con.close()


# ---- G2 --------------------------------------------------------------------

def producer_of(keep: str) -> str:
    """`producer:<archive>` → the producer an archive's import records in
    `chunk_verification` (`genesis:<archive>`, app/bundles/genesis.py); a value
    already carrying its kind (`genesis:…`, `ed25519:…`) is taken as it is."""
    kind, sep, value = keep.partition(":")
    if kind != "producer" or not sep or not value:
        raise CodexError(f"--keep is producer:<archive>, got {keep!r}")
    return value if ":" in value else f"genesis:{value}"


def keep_producer(root, to_ref: str, producer: str, *, window: int = WINDOW,
                  skip_marked: bool = False,
                  log: Callable[[str], None] = print) -> dict[str, Any]:
    """Keep only the vectors `producer` produced (G2). Resumable from its
    cursor in the manifest's `control` table."""
    from . import collection as col, records as rec
    from app import db, embeddings

    root = Path(root)
    name, codex = col.resolve(root, to_ref)
    coll = rec.local_collection(root, name, codex)
    key = KEEP_KEY + producer
    con = db.connect(coll)
    try:
        state = _cursor(con, key, [codex.collection_id],
                        {"rowid": 0, "kept": 0, "dropped": 0, "documents_reset": 0, "done": False})
        pair = state["pair"]
        while not state.get("done"):
            rows = con.execute("SELECT rowid, uid, native_id FROM documents WHERE rowid > ? "
                               "ORDER BY rowid LIMIT ?", (state["rowid"], window)).fetchall()
            if not rows:
                state["done"] = True
                con.execute("BEGIN IMMEDIATE")
                db.set_control(con, key, json.dumps(state, sort_keys=True))
                con.execute("COMMIT")
                break
            last = int(rows[-1]["rowid"])
            try:
                books = sorted({int(r["native_id"]) for r in rows})
            except ValueError:
                raise CodexError(
                    f"{name}: a native_id that is not an integer in rowids "
                    f"{rows[0]['rowid']}–{last}; the vector table keys rows on numeric "
                    f"book ids (embeddings.delete_chunks), so this collection's rows "
                    f"cannot be dropped by chunk here") from None
            uids = [r["uid"] for r in rows]
            marks = ",".join("?" * len(uids))
            kept = {r[0] for r in con.execute(
                f"SELECT chunk_id FROM chunk_verification WHERE uid IN ({marks}) "
                f"AND producer=? AND state IN ({','.join('?' * len(KEPT_STATES))})",
                (*uids, producer, *KEPT_STATES))}
            # `skip_marked`: ONLY THE DOCUMENTS THAT CAN HOLD A ROW TO DROP are read from the
            # vector table: one whose every chunk carries the archive's mark has
            # nothing the keep removes. Live 2026-10-10, pubmed (40M documents):
            # reading every window's books from the table cost ~17 s per 10,000
            # documents — ~16 h — for windows that dropped nothing. A stray
            # duplicate row of a kept chunk is still caught: the end requires
            # the table to hold exactly the kept count (CarryMismatch).
            marked: dict[str, int] = {}
            for c in kept:
                u = c.rsplit(":", 2)[0]
                marked[u] = marked.get(u, 0) + 1
            chunks_of = {r[0]: r[1] for r in con.execute(
                f"SELECT uid, chunks FROM documents WHERE uid IN ({marks})", uids)}
            short = ({u for u in uids
                      if chunks_of.get(u) is None or marked.get(u, 0) < int(chunks_of[u] or 0)}
                     if skip_marked else set(uids))
            if short:
                short_books = sorted({int(r["native_id"]) for r in rows if r["uid"] in short})
                present_short = set(embeddings.chunk_ids_of_books(short_books, collection=coll))
            else:
                present_short = set()
            present = present_short | {c for c in kept if c.rsplit(":", 2)[0] not in short}
            drop = sorted(present_short - kept)
            pending = state.get("pending") or {}
            if pending.get("rowid") != last:
                # step 1: the documents marked not done FIRST, with the count
                drop_uids = sorted({c.rsplit(":", 2)[0] for c in drop})
                con.execute("BEGIN IMMEDIATE")
                for i in range(0, len(drop_uids), 500):
                    part = drop_uids[i:i + 500]
                    con.execute(f"UPDATE documents SET embed_done=0 WHERE uid IN "
                                f"({','.join('?' * len(part))})", part)
                for i in range(0, len(drop), 500):
                    part = drop[i:i + 500]
                    con.execute(f"DELETE FROM chunk_verification WHERE chunk_id IN "
                                f"({','.join('?' * len(part))})", part)
                pending = {"rowid": last, "dropped": len(drop),
                           "documents_reset": len(drop_uids)}
                db.set_control(con, key, json.dumps({**state, "pending": pending},
                                                     sort_keys=True))
                con.execute("COMMIT")
            # step 2: the rows, under the vector write lock
            embeddings.delete_chunks(drop, collection=coll, lock_con=con)
            # step 3: the cursor
            state = {"rowid": last, "kept": state["kept"] + len(present & kept),
                     "dropped": state["dropped"] + pending["dropped"],
                     "documents_reset": state["documents_reset"] + pending["documents_reset"],
                     "done": False, "pair": pair}
            con.execute("BEGIN IMMEDIATE")
            db.set_control(con, key, json.dumps(state, sort_keys=True))
            con.execute("COMMIT")
    finally:
        con.close()
    rows_now = embeddings.stats(coll)["vectors"]
    out = {"collection": name, "producer": producer, "kept": state["kept"],
           "dropped": state["dropped"], "documents_reset": state["documents_reset"],
           "vectors": rows_now, "verified": rows_now == state["kept"]}
    log(f"[carry] {name}: kept {state['kept']} vector(s) of {producer}, dropped "
        f"{state['dropped']}, {state['documents_reset']} document(s) to embed again; the "
        f"table holds {rows_now}")
    if not out["verified"]:
        raise CarryMismatch(
            f"{name}: the vector table holds {rows_now} rows and {state['kept']} were kept — "
            f"rows whose book_id is no document of the manifest were never visited")
    return out


#: values in one delete predicate at most: few passes over the table, a bounded string
DELETE_BATCH = 50_000
#: A Lance delete's filter costs memory per value PER FRAGMENT, not per row:
#: measured on wikipedia-v8 (3,247 fragments, 146 M rows, 2026-10-22) with
#: `count_rows(filter=book_id IN (...))`, 0.85 GB at 1,000 values and 3.5 GB
#: at 5,000 — ~0.22 kB a value a fragment — and the keep's first 50,000-value
#: delete was OOM-killed at 33.8 GB. So a batch is sized from the table's
#: fragment count to this budget (`_delete_batch`): 1,231 values there.
DELETE_MEM_KB = 1_000_000
DELETE_KB_PER_VALUE_FRAGMENT = 0.25


def _delete_batch(fragments: int) -> int:
    """Values per delete predicate on a table of `fragments`, within DELETE_MEM_KB."""
    return max(100, min(DELETE_BATCH, int(DELETE_MEM_KB / (DELETE_KB_PER_VALUE_FRAGMENT * max(1, fragments)))))
#: ids sorted in memory per run file of the keep's merge (~150 MB of strings)
SORT_RUN = 2_000_000


def _sorted_runs(rows, workdir: Path, prefix: str, *, run: int) -> list[Path]:
    """`rows` (lines without a newline) as sorted run files in `workdir`."""
    paths: list[Path] = []
    buf: list[str] = []

    def flush() -> None:
        if not buf:
            return
        buf.sort()
        p = workdir / f"{prefix}-{len(paths):05d}"
        with p.open("w", encoding="utf-8", newline="\n") as f:
            f.writelines(line + "\n" for line in buf)
        paths.append(p)
        buf.clear()
    for line in rows:
        buf.append(line)
        if len(buf) >= run:
            flush()
    flush()
    return paths


def _merged(paths: list[Path]):
    """The lines of sorted run files, in one sorted stream."""
    import heapq
    files = [p.open(encoding="utf-8") for p in paths]
    try:
        yield from (line.rstrip("\n") for line in heapq.merge(*files))
    finally:
        for f in files:
            f.close()


def keep_producer_scan(root, to_ref: str, producer: str, *, run: int = SORT_RUN,
                       log: Callable[[str], None] = print) -> dict[str, Any]:
    """`keep_producer` by SORT-MERGE, for a table too large to visit by
    document (2026-10-21, wikipedia's parser-5 carry: 146 M rows, 53 M
    documents; the windowed walk read each window's books from the table, ~4
    GB a window, 0.17 % done in 3 h — ~1,800 h; then a per-id lookup among
    the marks cost ~66 ms a lookup, the 60 GB manifest's chunk_id index read
    at random — months, measured 2026-10-22). Every read is SEQUENTIAL: the
    producer's marks (`chunk_verification`, in table order) and the vector
    table's `id` and `book_id` are each written as sorted run files beside
    the manifest, and one merge of the two streams finds every row not the
    producer's. The order is invariant 2's: the documents that lose a row
    are marked not embedded FIRST, then the rows go — a book whose rows are
    all dropped by `book_id`, the rest by `id`, in few large predicates —
    and the end requires the table to hold exactly the producer's rows
    (CarryMismatch). A producer with no marks at all is refused: it would
    drop the whole table. Interrupted, it is run again: the scan recomputes
    what is left."""
    import shutil
    from . import collection as col, records as rec
    from app import db, embeddings

    root = Path(root)
    name, codex = col.resolve(root, to_ref)
    coll = rec.local_collection(root, name, codex)
    con = db.connect(coll)
    workdir = Path(coll.manifest_path).parent / "carry-keep-runs"
    shutil.rmtree(workdir, ignore_errors=True)
    workdir.mkdir(parents=True)
    try:
        tbl = embeddings._conn(coll).open_table(embeddings.TABLE)
        states = ",".join("?" * len(KEPT_STATES))
        marks = _sorted_runs((r[0] for r in con.execute(
            f"SELECT chunk_id FROM chunk_verification NOT INDEXED WHERE producer=? "
            f"AND state IN ({states})", (producer, *KEPT_STATES))), workdir, "marks", run=run)
        if not marks:
            raise CarryMismatch(f"{name}: no chunk of {producer} is marked in chunk_verification — "
                                f"keeping its rows would drop the whole table (is the producer right?)")
        log(f"[carry] {name}: {producer}'s marks read in {len(marks)} sorted run(s)")

        def rows():
            for batch in embeddings.scan_columns(coll, ["id", embeddings.BOOK_COLUMN_NAME],
                                                 batch_size=SCAN_ROWS):
                yield from (f"{i}\t{b}" for i, b in zip(batch.column(0).to_pylist(),
                                                        batch.column(1).to_pylist()))
        table = _sorted_runs(rows(), workdir, "rows", run=run)
        log(f"[carry] {name}: the table's ids read in {len(table)} sorted run(s)")
        kept = 0
        seen = 0
        drop_ids: dict[int, list[str]] = {}      # book -> its unmarked ids
        mark_it = _merged(marks)
        mark = next(mark_it, None)
        for line in _merged(table):
            cid, _, b = line.partition("\t")
            while mark is not None and mark < cid:
                mark = next(mark_it, None)
            if mark == cid:
                kept += 1
            else:
                drop_ids.setdefault(int(b), []).append(cid)
            seen += 1
            if seen % (SCAN_ROWS * 160) == 0:
                log(f"[carry] {name}: merged {seen} rows, {kept} the producer's, "
                    f"{sum(len(v) for v in drop_ids.values())} to drop")
        shutil.rmtree(workdir, ignore_errors=True)
        # a book that loses rows and holds none of the producer's goes whole, by
        # book_id; one that holds both loses its unmarked ids alone
        whole, partial = [], []
        for b, v in drop_ids.items():
            uid = v[0].rsplit(":", 2)[0]
            n = con.execute(f"SELECT COUNT(*) FROM chunk_verification WHERE uid=? AND producer=? "
                            f"AND state IN ({states})", (uid, producer, *KEPT_STATES)).fetchone()[0]
            (partial if n else whole).append(b)
        whole.sort()
        partial.sort()
        dropped = sum(len(v) for v in drop_ids.values())
        log(f"[carry] {name}: {seen} rows read, {kept} kept, {dropped} to drop — "
            f"{len(whole)} document(s) whole, {len(partial)} in part")

        # step 1: the documents marked not done FIRST, a dropped chunk's
        # provenance going with it (as `keep_producer`)
        uids = sorted({c.rsplit(":", 2)[0] for v in drop_ids.values() for c in v})
        for i in range(0, len(uids), 10_000):
            part = uids[i:i + 10_000]
            con.execute("BEGIN IMMEDIATE")
            for j in range(0, len(part), 500):
                p = part[j:j + 500]
                con.execute(f"UPDATE documents SET embed_done=0 WHERE uid IN ({','.join('?' * len(p))})", p)
            con.execute("COMMIT")
        gone = [c for v in drop_ids.values() for c in v]
        for i in range(0, len(gone), 10_000):
            part = gone[i:i + 10_000]
            con.execute("BEGIN IMMEDIATE")
            for j in range(0, len(part), 500):
                p = part[j:j + 500]
                con.execute(f"DELETE FROM chunk_verification WHERE chunk_id IN ({','.join('?' * len(p))})", p)
            con.execute("COMMIT")
        # step 2: the rows, under the vector write lock, in few predicates
        with embeddings._write_lock(con):
            frags = embeddings.fragment_count(coll)
            batch = _delete_batch(frags)
            log(f"[carry] {name}: {batch} value(s) a delete ({frags} fragments, "
                f"{DELETE_MEM_KB // 1000} MB budget)")
            for i in range(0, len(whole), batch):
                part = whole[i:i + batch]
                tbl.delete(f"{embeddings.BOOK_COLUMN_NAME} IN ({','.join(str(b) for b in part)})")
                if (i // batch) % 20 == 0 or i + len(part) == len(whole):
                    log(f"[carry] {name}: dropped the rows of {i + len(part)} of {len(whole)} whole document(s)")
            ids_part = [c for b in partial for c in drop_ids[b]]
            for i in range(0, len(ids_part), batch):
                part = ids_part[i:i + batch]
                quoted = ",".join("'" + c.replace("'", "''") + "'" for c in part)
                tbl.delete(f"id IN ({quoted})")
    finally:
        con.close()
        shutil.rmtree(workdir, ignore_errors=True)
    rows_now = embeddings.stats(coll)["vectors"]
    out = {"collection": name, "producer": producer, "kept": kept, "dropped": dropped,
           "documents_reset": len(uids), "vectors": rows_now, "verified": rows_now == kept}
    log(f"[carry] {name}: kept {kept} vector(s) of {producer}, dropped {dropped}, "
        f"{len(uids)} document(s) to embed again; the table holds {rows_now}")
    if not out["verified"]:
        raise CarryMismatch(f"{name}: the vector table holds {rows_now} rows and {kept} were kept")
    return out


# ---- A1e: the rows the character cut shortened --------------------------------

#: An archive's rows (02 §2.4 A1b): made by its producer at the token cut the
#: archive declares, never through this platform's character cut.
ARCHIVE_PREFIX = "genesis:"
#: Rows read from the vector table at a time, a few MB of text each.
SCAN_ROWS = 65_536


def _dense_pattern() -> str:
    """embeddings._dense_script as an RE2 class: a batch holding one such
    character was cut to a budget below `ctx` x 4 that nothing recorded."""
    from app import embeddings
    return "[" + "".join(rf"\x{{{lo:04X}}}-\x{{{hi:04X}}}"
                         for lo, hi in embeddings.DENSE_RANGES) + "]"


def embedded_alone(codex: Codex) -> bool:
    """Was every input of this collection embedded in a request of its own?
    A pair layer is admitted only on a chunker that makes one chunk per record
    (09 §9.3), and the worker embeds one document at a time, so each pair was
    its own batch: no neighbour's characters set its budget."""
    from .codex import INPUT_PAIR
    return bool(codex.layers) and all(l.input == INPUT_PAIR for l in codex.layers)


def recut(root, to_ref: str, *, budget: int, apply: bool = False, window: int = WINDOW,
          log: Callable[[str], None] = print) -> dict[str, Any]:
    """A1e (RULE 1, 2026-10-18): after a carry from a space cut by characters
    into one cut by tokens, drop the rows the two cuts make unalike, and mark
    their documents to be embedded again. A row is the same vector under both
    when the character cut left its input whole: an input within `budget`
    (the source's `ctx` x 4) went to the encoder whole, and the encoder cut it
    at `ctx` itself — so a row longer than the budget is dropped, unless an
    ARCHIVE made it (a `chunk_verification` row of a `genesis:` producer, A1b:
    the archive cut by tokens). A row holding a dense-script character is the
    case nothing decides — its batch's budget was lowered by an amount not
    recorded, its neighbours' too — so finding one refuses the recut, nothing
    written; EXCEPT where every input was embedded alone (`embedded_alone`:
    a pair layer, one chunk per record), where there were no neighbours and
    such a row is dropped too, its own cut uncertain and such rows few
    (pubmed, 2026-10-18: 102 of 4.58 M).

    One read of the table (id, text), the drops then a window of documents at
    a time: the reset committed first (a document marked not done whose rows
    still exist is embedded again, never lost), the rows deleted under the
    vector write lock. A second run recomputes what is left, so nothing needs
    a cursor. The caller holds the target's write lock."""
    import pyarrow.compute as pc

    from . import collection as col, records as rec
    from app import db, embeddings

    root = Path(root)
    name, codex = col.resolve(root, to_ref)
    if codex.space is None or codex.space.cut != TOKEN_CUT:
        raise CodexError(f"recut: {name} does not cut by tokens; there is nothing to recut")
    coll = rec.local_collection(root, name, codex)
    dense_re = _dense_pattern()
    con = db.connect(coll)
    try:
        before = embeddings.stats(coll)["vectors"]
        drop: list[str] = []
        long_rows = archive_rows = dense_rows = 0
        for part in embeddings.scan_columns(coll, ("id", "text"), batch_size=SCAN_ROWS):
            text = part.column("text")
            dense = pc.match_substring_regex(text, dense_re)
            hit = pc.or_(pc.greater(pc.utf8_length(text), budget), dense)
            ids = pc.filter(part.column("id"), hit).to_pylist()
            if not ids:
                continue
            dense_ids = set(pc.filter(part.column("id"), dense).to_pylist())
            archive: set[str] = set()
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                archive.update(r[0] for r in con.execute(
                    f"SELECT chunk_id FROM chunk_verification WHERE chunk_id IN "
                    f"({','.join('?' * len(chunk))}) AND producer LIKE ?",
                    (*chunk, ARCHIVE_PREFIX + "%")))
            long_rows += len(ids)
            archive_rows += len(archive)
            for c in ids:
                if c in archive:
                    continue
                dense_rows += c in dense_ids
                drop.append(c)
        alone = embedded_alone(codex)
        out: dict[str, Any] = {
            "collection": name, "budget_chars": budget, "vectors": before,
            "over_budget_or_dense": long_rows, "kept_archive": archive_rows,
            "dense_not_archive": dense_rows, "embedded_alone": alone, "to_drop": len(drop)}
        if dense_rows and not alone:
            raise CodexError(
                f"recut: {name} holds {dense_rows} row(s) not an archive's with dense-script "
                f"characters (CJK, kana, hangul): the character cut lowered their batch's "
                f"budget by an amount nothing recorded, so which of their neighbours it "
                f"shortened is unknown. Nothing was written ({out})")
        if not apply:
            log(f"[carry] {name}: --recut would drop {len(drop)} row(s) the character cut "
                f"shortened (over {budget} characters, not an archive's) and embed their "
                f"documents again; {archive_rows} archive row(s) over it stay; nothing "
                f"written (--yes)")
            return {**out, "apply": False}
        reset = _drop_and_reset(con, coll, drop, window=window,
                                log=lambda m: log(f"[carry] {name}: recut {m}"))
    finally:
        con.close()
    rows_now = embeddings.stats(coll)["vectors"]
    out.update(apply=True, dropped=len(drop), documents_reset=reset, vectors_now=rows_now,
               verified=rows_now == before - len(drop))
    log(f"[carry] {name}: dropped {len(drop)} row(s) the character cut shortened, {reset} "
        f"document(s) to embed again; the table holds {rows_now} (was {before}). Before use: "
        f"`hold check {name} --checks vectors-sample` (A1e)")
    if not out["verified"]:
        raise CarryMismatch(
            f"{name}: the table holds {rows_now} rows, {before} - {len(drop)} expected; run "
            f"the recut again (it recomputes what is left)")
    return out


def _drop_and_reset(con, coll, ids: list[str], *, window: int,
                    log: Callable[[str], None]) -> int:
    """`ids`' rows dropped and their documents marked to embed again, a window
    of rows at a time, each in two steps that can be repeated: the reset
    committed FIRST (a document marked not done whose rows still exist is
    embedded again, never lost), with a bundle's provenance of the rows; then
    the rows, under the vector write lock. Returns the documents reset."""
    from app import embeddings
    ids = sorted(ids)
    reset = 0
    for i in range(0, len(ids), window):
        part_ids = ids[i:i + window]
        uids = sorted({c.rsplit(":", 2)[0] for c in part_ids})
        con.execute("BEGIN IMMEDIATE")
        con.execute(f"UPDATE documents SET embed_done=0 WHERE uid IN "
                    f"({','.join('?' * len(uids))})", uids)
        con.execute(f"DELETE FROM chunk_verification WHERE chunk_id IN "
                    f"({','.join('?' * len(part_ids))})", part_ids)
        con.execute("COMMIT")
        reset += len(uids)
        embeddings.delete_chunks(part_ids, collection=coll, lock_con=con)
        if (i // window) % 200 == 0:
            log(f"{i + len(part_ids)}/{len(ids)} row(s)")
    return reset


def drop_rows(root, ref: str, ids: list[str], *, window: int = WINDOW,
              log: Callable[[str], None] = print) -> dict[str, Any]:
    """02 §2.4's remedy for rows that are not this space's function — an
    archive's vector of an input other than the one its record cuts to — with
    their documents embedded again, which `vectors check --drop` (an empty
    row, no re-embed) does not do. `louise dev drop-rows`. The caller holds
    the collection's write lock. Rows not in the table are no work."""
    from . import collection as col, records as rec
    from app import db, embeddings

    name, codex = col.resolve(Path(root), ref)
    coll = rec.local_collection(Path(root), name, codex)
    want = set(ids)
    present = want & set(embeddings.chunk_ids_of_books(
        {int(c.rsplit(":", 3)[1]) for c in want}, collection=coll))
    before = embeddings.stats(coll)["vectors"]
    con = db.connect(coll)
    try:
        reset = _drop_and_reset(con, coll, sorted(present), window=window,
                                log=lambda m: log(f"[drop] {name}: {m}"))
    finally:
        con.close()
    now = embeddings.stats(coll)["vectors"]
    out = {"collection": name, "asked": len(set(ids)), "dropped": len(present),
           "documents_reset": reset, "vectors": now, "verified": now == before - len(present)}
    log(f"[drop] {name}: dropped {len(present)} row(s), {reset} document(s) to embed again; "
        f"the table holds {now} (was {before})")
    if not out["verified"]:
        raise CarryMismatch(f"{name}: the table holds {now} rows, {before} - {len(present)} "
                            f"expected; run it again")
    return out
