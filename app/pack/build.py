"""`pack build` — a collection's documents become record shards
(pack-format.md §2, §3, §14 step 2), resumably.

Two passes over the collection's MANIFEST (never a fresh crawl of the
origin: the manifest is the deduplicated, tombstoned, in-scope view that
discovery already maintains — a PubMed revision arriving later than its
baseline record is already resolved there):

  1. PLAN — every in-scope, error-free document gets its SLICE (the
     plugin's scheme, from a cheap hint where the locator or manifest
     says enough, else a parse) into a build database beside the pack.
  2. PACK — per slice, in native-id order, records_per_shard records
     become one shard; the shard file lands atomically and is recorded
     in the same build database AFTER the rename (invariant 2: the mark
     follows the durable write). A re-run skips recorded shards; a stop
     between shards resumes at the next one; a stop inside a shard
     discards the buffer (bounded: one shard of work).

Then the manifest: slices, shards with their hashes, layers, license,
the terms text copied in, signed with the node key when one is set.
"""
import json
import shutil
import sqlite3
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from .. import config, cpu_limit, db, layers as layers_mod, parallel, plugin_io
from ..hold import shard as shard_mod, records as hold_records
from ..plugins.base import UnindexableDocument
from . import cutoff as cutoff_mod, manifest as manifest_mod, records, slicing

BUILD_DIR = ".build"
BUILD_DB = "build.db"
RECORDS_DIR = "records"
TERMS_FILE = "TERMS.txt"
VALIDATION_FILE = "validation.json"
PLAN_PAGE = 5000
PLAN_PAGE_SQL = ("SELECT rowid, uid, native_id, path, lang, in_scope, error FROM documents "
                 "WHERE rowid>? ORDER BY rowid LIMIT ?")
#: the in-memory record's key for its parse's issues (app/issues.py) — not a
#: 09 §9.5 key, so no line could carry it even if it were left on
ISSUES_KEY = "_issues"


class BuildError(RuntimeError):
    pass


def _refuse_uncredited_origin(collection, record: dict) -> None:
    """06 §6.4 (main 8068727): a record located IN an archive — its
    `origin` a URL under a registered archive's base — must be located in
    one the Codex credits, for the reason an uncredited archive's vectors
    are refused at import (§6.6). Aborts the build like a missing
    `origin_hash`: a record standing on work its collection never declared
    cannot be repaired by the peer who receives it. A URL can hold no raw
    `|`, so a straddling record's pieces split safely there."""
    from ..bundles.genesis import ARCHIVES
    credited = set()
    for c in layers_mod._archive_credits(collection):
        credited.add(getattr(c, "archive", None)
                     or (c.get("archive") if isinstance(c, dict) else None))
    for piece in str(record.get("origin") or "").split("|"):
        if "://" not in piece:
            continue
        for a in ARCHIVES.values():
            if a.base_url and piece.startswith(a.base_url.rstrip("/") + "/") \
                    and a.archive_id not in credited:
                raise BuildError(
                    f"{record['uid']}: located in archive {a.archive_id!r}, which "
                    f"this collection does not credit (06 §6.4); it credits "
                    f"{', '.join(sorted(x for x in credited if x)) or 'no archive'}")


@dataclass
class BuildResult:
    pack_dir: Path
    slices: int
    shards: int
    records: int
    chunks: int
    skipped: int
    interrupted: bool
    manifest_path: Path | None


def _build_db(pack_dir: Path) -> sqlite3.Connection:
    d = pack_dir / BUILD_DIR
    d.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(d / BUILD_DB, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.executescript("""
    CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
    CREATE TABLE IF NOT EXISTS plan(uid TEXT PRIMARY KEY, slice TEXT NOT NULL, ord INTEGER NOT NULL);
    CREATE INDEX IF NOT EXISTS idx_plan_slice ON plan(slice, ord, uid);
    CREATE TABLE IF NOT EXISTS shards(
      slice TEXT NOT NULL, n INTEGER NOT NULL, path TEXT NOT NULL,
      records INTEGER, chunks INTEGER, bytes INTEGER, sha256 TEXT,
      first_uid TEXT, last_uid TEXT, last_ord INTEGER, id_min INTEGER, id_max INTEGER,
      layers TEXT, written_at INTEGER,
      -- 09 §9.4's shard entry. `root` is the BitTorrent v2 pieces root the
      -- transport and the `roots` check compare; `sha256` above is a plain
      -- digest of the file and is NOT it. `records_root` and `chunks_root`
      -- are what equivalence, inheritance and the `chunks` check compare,
      -- and neither depends on the compressor.
      root TEXT, records_root TEXT, chunks_root TEXT,
      first_native_id TEXT, last_native_id TEXT,
      PRIMARY KEY(slice, n));
    CREATE TABLE IF NOT EXISTS skipped(uid TEXT PRIMARY KEY, reason TEXT);
    """)
    return con


def _meta(con, key, default=None):
    row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def _set_meta(con, key, value) -> None:
    con.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)))


def _origin_plugin(collection) -> str:
    """The plugin whose scheme applies."""
    return collection.plugin


def plan(collection, con_src, con_build, *, log=print, stop=None) -> int:
    """Pass 1. Resumable by rowid cursor. Returns rows planned this run."""
    plugin = collection.plugin_instance()
    origin = _origin_plugin(collection)
    scheme = slicing.scheme_for(origin)
    # what the slicing needs beyond a record — wikidata's family map, pinned
    # by the Codex (pack-format.md §2, amended 2026-10-24); None elsewhere
    try:
        context = slicing.context_for(collection, log=log, stop=stop)
    except slicing.ContextUnavailable as e:
        raise BuildError(str(e)) from None
    last = int(_meta(con_build, "plan_rowid", -1))
    planned = 0
    t0 = time.monotonic()
    while True:
        if stop is not None and stop.is_set():
            return planned
        # A pure ROWID walk: with `in_scope=1 AND error IS NULL` in the
        # WHERE clause SQLite may pick the scope index and sort every
        # matching row for ORDER BY rowid (a TEMP B-TREE per page — the
        # quadratic trap, observed as 500 rows/s on a 7M-row manifest).
        # The filter is applied here instead; the page size is in rowids.
        page = con_src.execute(PLAN_PAGE_SQL, (last, PLAN_PAGE)).fetchall()
        if not page:
            break
        rows = []
        for row in page:
            if not row["in_scope"] or row["error"] is not None:
                continue
            slice_name = slicing.slice_hint(origin, row, context)
            if slice_name is None:
                try:
                    doc = plugin_io.canonical_doc(plugin, row)
                    slice_name = slicing.slice_of(scheme, doc, int(row["native_id"]), context)
                except (UnindexableDocument, Exception) as e:   # noqa: BLE001 — recorded, never fatal
                    con_build.execute("INSERT OR REPLACE INTO skipped(uid,reason) VALUES(?,?)",
                                      (row["uid"], f"plan: {type(e).__name__}: {e}"[:200]))
                    continue
            try:
                ord_ = int(row["native_id"])
            except (TypeError, ValueError):
                ord_ = 0
            rows.append((row["uid"], slicing.check_slice_name(slice_name), ord_))
        con_build.execute("BEGIN IMMEDIATE")
        con_build.executemany("INSERT OR REPLACE INTO plan(uid,slice,ord) VALUES(?,?,?)", rows)
        last = page[-1]["rowid"]
        _set_meta(con_build, "plan_rowid", last)
        con_build.execute("COMMIT")
        planned += len(rows)
        if time.monotonic() - t0 > 30:
            log(f"[pack build] planned {planned} documents so far")
            t0 = time.monotonic()
    _set_meta(con_build, "plan_done", "1")
    return planned


@dataclass(frozen=True)
class HeldRecords:
    """RECORD LINES ALREADY BUILT, taken instead of parsing (RULE 1; 02 §2.7,
    amended 2026-10-24): a daughter that changes only the slicing — a family
    map — or a re-creation that does, reuses its mother's record lines byte
    for byte. `shard_dir` holds the parts, `paths` lists them (a snapshot's
    shards, relative to `shard_dir`). Each record is cut into chunks again
    for its part's `chunks_root` — the chunker, never the parser."""
    shard_dir: Path
    paths: tuple[str, ...]


def held_records_of(root: Path, name: str) -> HeldRecords:
    """The records a held collection's latest snapshot lists."""
    from ..hold import carry as carry_mod, collection as col
    snap = carry_mod.latest_snapshot(Path(root), name)
    return HeldRecords(col.shard_dir_of(Path(root), name), tuple(s.path for s in snap.shards))


def _held_index(con_build, held: HeldRecords, log) -> None:
    """uid → (part, frame, line) over every part `held` lists, from each
    part's own trailing index (no record decoded); resumable at a part."""
    con_build.execute("CREATE TABLE IF NOT EXISTS held(uid TEXT PRIMARY KEY, path TEXT NOT NULL, "
                      "frame INTEGER NOT NULL, line INTEGER NOT NULL) WITHOUT ROWID")
    done = int(_meta(con_build, "held_parts", 0))
    for k in range(done, len(held.paths)):
        rel = held.paths[k]
        index = records.read_index(held.shard_dir / rel)
        con_build.execute("BEGIN IMMEDIATE")
        con_build.executemany("INSERT OR REPLACE INTO held(uid,path,frame,line) VALUES(?,?,?,?)",
                              [(uid, rel, fl[0], fl[1]) for uid, fl in index.uids.items()])
        _set_meta(con_build, "held_parts", k + 1)
        con_build.execute("COMMIT")
    if done < len(held.paths):
        log(f"[pack build] the records of {len(held.paths)} held part(s) indexed for reuse")


def _held_values(collection, con_build, held: HeldRecords, srcs: list[dict]) -> list[tuple[bool, object]]:
    """`parallel.compute("record")`'s answer, from held record lines."""
    out: list[tuple[bool, object]] = []
    for src in srcs:
        at = con_build.execute("SELECT path, frame, line FROM held WHERE uid=?", (src["uid"],)).fetchone()
        if at is None:
            out.append((False, {"type": "NotHeld", "msg": "not among the held records reused"}))
            continue
        try:
            out.append((True, _record_from_line(collection, records.read_record_at(
                held.shard_dir / at["path"], int(at["frame"]), int(at["line"])))))
        except Exception as e:   # noqa: BLE001 — recorded per document by the caller
            out.append((False, {"type": type(e).__name__, "msg": str(e)[:500]}))
    return out


def _record_from_line(collection, line: dict) -> tuple[dict, int, bytes, dict[str, str]]:
    """`_record_of`'s answer for a record line already built: the line as
    it is, its layer, and its chunks cut again for the part's
    `chunks_root` (a pure function of the line's sections, invariant 3)."""
    native_id = int(line["native_id"])
    layer = layers_mod.layer_index(collection, native_id)
    chunks = 0
    chunk_blob = bytearray()
    for c in layers_mod.chunker_for_document(collection, native_id)(
            line["uid"], native_id, [tuple(s) for s in line.get("sections") or []]):
        chunks += 1
        chunk_blob += hold_records.chunk_contribution(c["id"], c["text"])
    return {**line, "layer": layer, "chunks": chunks}, layer, bytes(chunk_blob), {}


def _record_of(collection, plugin, row) -> tuple[dict, int, bytes, dict[str, str]]:
    doc = plugin_io.canonical_doc(plugin, row)
    native_id = int(row["native_id"])
    sections = [[name or "", body or ""] for name, body in doc.sections]
    layer = layers_mod.layer_index(collection, native_id)
    # The chunks are counted AND folded into this document's contribution to
    # the shard's `chunks_root` (09 §9.4) in one pass. It is done here, in
    # the parse pool's child, because parse + chunk + hash per document is
    # the whole cost of a pack pass (amendment 47) and re-chunking in the
    # parent to compute the root would double it. The parent concatenates
    # these blobs in record order and hashes once.
    chunks = 0
    chunk_blob = bytearray()
    for c in layers_mod.chunker_for_document(collection, native_id)(
            doc.uid, native_id, doc.sections):
        chunks += 1
        chunk_blob += hold_records.chunk_contribution(c["id"], c["text"])
    record = {
        "uid": doc.uid, "native_id": str(row["native_id"]), "lang": doc.language or "und",
        "title": doc.title or "", "creators": list(doc.creators or []),
        "date": doc.date, "subjects": list(doc.subjects or []), "keys": list(doc.keys or []),
        "sections": sections, "extra": dict(doc.extra or {}),
        "layer": layer, "chunks": chunks,
        # 09 §9.5's `origin`, relative to the origin base and never an
        # absolute local path: the record travels, and this machine's
        # `/mnt/…` locates nothing on the peer that receives it. Half of
        # invariant 11 — `origin_hash` below is the hash of the bytes this
        # locates — and what makes `records-vs-origin` a check a stranger
        # can run at all.
        "origin": plugin_io.origin_locator(plugin, row["path"],
                                           collection.corpus_dir),
        "content_hash": records.content_hash(doc.title or "", sections),
        "origin_hash": doc.raw_hash,
    }
    # 09 §9.5's `figures`, iff the Codex says `record_line` 2. Written from
    # the key's PRESENCE, never from emptiness: `record_line` 1 is defined
    # as the line WITHOUT the key, so a figureless book in a plates
    # collection carries `"figures": []` and a book in a text collection
    # carries no key at all. `record_line` is inside `collection_id`, so
    # the two shapes are two different collections and a line that guessed
    # would put one collection's records under the other's id.
    #
    # After `content_hash`, and that is the whole design: a figure is not
    # text, its caption already lives in `sections`, so gaining plates
    # changes no content hash and re-uses every text vector (02 §2.3).
    plates: dict[str, str] = {}
    if getattr(getattr(collection, "codex", None), "record_line", 1) == 2:
        record["figures"] = [f.as_record() for f in (doc.figures or [])]
        # WHERE THE BYTES ARE, for the container pass — {hash: href}, and
        # never the bytes themselves. The child has the epub open and could
        # hand them over for free, but the corpus holds ~51 GB of plates
        # and every one of them would be pickled back through the pool.
        # An href is a few dozen bytes and the parent re-opens the book,
        # which is still in the page cache.
        #
        # DISTINCT plates, where `figures` above lists APPEARANCES: the
        # container stores each once (09 §9.5), and a book repeating one
        # plate 703 times would otherwise read it 703 times. Every plate
        # is here whatever its type or size — SVG, GIF, over a cap — since
        # 08 §8.7's amendment of 2026-10-25 ("Every plate"): the container
        # stores opaque bytes and only display applies the closed list. A
        # `carried` False entry (a record from before it) has no bytes to
        # fetch, and the container must not hold any.
        plates = {f.sha256: f.href for f in (doc.figures or []) if f.carried}
    # THE ORIGIN'S INCONSISTENCIES this parse met (06 §6.1, amended
    # 2026-10-25), beside the record and never in its line: `canonical_line`
    # writes 09 §9.5's keys alone, and `pack_slices` pops this one before the
    # record is buffered. A list (possibly empty) means "parsed"; a record
    # taken from a held line has none, and records nothing.
    record[ISSUES_KEY] = [(i.code, i.detail) for i in (doc.issues or [])]
    # NOTE: no validation here. This runs in a parse-pool child, where an
    # exception becomes one skipped row rather than a failed build — so a
    # fidelity defect raised here would quietly DROP documents from the
    # pack, which is worse than what it was meant to prevent. The check
    # belongs in pack_slices, in the process that can abort.
    return record, layer, bytes(chunk_blob), plates


def _refuse_too_many_figures(record: dict) -> None:
    """08 §8.7's cap on what a record may name: over it the BUILD aborts, as
    for a missing `origin_hash` — dropping figures would re-aim every
    `[Illustration…]` marker after them (09 §9.5)."""
    from ..hold import figures as figures_mod
    n = len(record.get("figures") or [])
    if n > figures_mod.MAX_PER_RECORD:
        raise ValueError(f"{record['uid']}: names {n:,} figures, over 08 §8.7's "
                         f"{figures_mod.MAX_PER_RECORD:,} per record; not packed")


def _write_figures(plugin, pack_dir: Path, slice_name: str, shard_n: int,
                   plates: dict, log) -> None:
    """The shard's figures container (09 §9.5), read back from the origin.

    THE SECOND PASS the format asks for. Every plate the shard's records
    name is read from the book it came from and stored once, ordered by
    hash; `figures.write_container` verifies each against the name the
    record gave it before anything lands.

    A plate that CANNOT be read aborts the build, like `origin_hash` above
    and for the same reason: a record line naming a plate the container
    does not hold fails `hold check`'s `figures` row on every peer that
    receives it, and no peer can repair it. Dropping the entry instead is
    not available either — `figures[N]` is the Nth `[Illustration…]`
    marker in the text, so removing one silently re-aims every marker
    after it at the wrong picture.
    """
    from ..hold import figures as figures_mod

    # ONE OPEN PER BOOK, not one per plate. The plates are grouped by the
    # record that names them before anything is read: gutenberg's corpus
    # holds ~700,000 images over ~48,000 illustrated books, and a book with
    # 100 plates read one at a time is 100 re-opens of the same zip plus
    # 100 re-applications of the reading rule. That is a per-item cost
    # scaling with the corpus, which is a bug however fast it looks on a
    # three-book fixture.
    by_record: dict[str, tuple[dict, dict[str, str]]] = {}
    for figure_hash, (row, href) in plates.items():
        by_record.setdefault(row["uid"], (row, {}))[1][figure_hash] = href

    data: dict[str, bytes] = {}
    for uid, (row, wanted) in by_record.items():
        try:
            data.update(plugin_io.figure_bytes(plugin, row, wanted))
        except Exception as e:
            raise ValueError(
                f"{uid}: the record names {len(wanted)} plate(s) and they "
                f"could not be read ({type(e).__name__}: {e}). A record line "
                f"may not name a plate its container cannot carry (09 §9.5); "
                f"fix the plugin, do not pack."
            ) from e
    missing = sorted(set(plates) - set(data))
    if missing:
        # The plugin answered without raising and came back short. Named
        # here rather than discovered by `write_container`, whose error
        # would be about a hash it was never given.
        raise ValueError(
            f"{plates[missing[0]][0]['uid']}: the {plugin.source_id!r} plugin "
            f"returned no bytes for {len(missing)} named plate(s), first "
            f"{missing[0][:19]}… (09 §9.5)")
    path = figures_mod.path_for(pack_dir, slice_name, shard_n)
    info = figures_mod.write_container(path, data)
    log(f"[pack build] {slice_name}: shard {shard_n} carries {info.figures} "
        f"plate(s), {info.bytes / 1e6:.1f} MB")


def pack_slices(collection, con_src, con_build, pack_dir: Path, *, records_per_shard: int,
                until: str | None = None, log=print, stop=None,
                closed: dict[str, dict[int, tuple[str, str]]] | None = None,
                held: HeldRecords | None = None
                ) -> tuple[int, int, int, int, bool]:
    """Pass 2. Returns (shards written, records, chunks, skipped, interrupted).
    `until`: the snapshot cutoff (cutoff.py) — records outside it are
    skipped here, where the document is parsed anyway, so shards stay
    full and the result is a pure function of (manifest, cutoff).

    `closed`: {slice: {n: (first_id, last_id)}}, the CLOSED parts of the
    previous snapshot (02 §2.2: "a closed part never changes again except to
    be re-issued … fewer afterwards if a tombstoned record is dropped from
    it, never more, and never refilled"). Part n is then cut where the
    previous part n ended, not after `records_per_shard` records: a record
    dropped from it leaves it one shorter and moves nothing after it; a
    part whose every record went is a number left empty. Only past the
    closed parts are records counted into `records_per_shard`. Without it
    (a slice's first build, the legacy `pack build`) every part is counted.
    Until 2026-10-12 a fresh build re-cut every slice by count, so one book
    gone from the origin re-issued every later part of its slice.

    A Codex whose slicing cuts parts BY CONTENT (`slicing.parts`, 02 §2.2
    amended 2026-10-24) keeps no closed parts: a record ends its part by its
    own uid's fingerprint (slicing.ends_part), so the layout is a pure
    function of each slice's records — a from-scratch build, a resumed one
    and a daughter's give the same parts byte for byte."""
    content_rule = slicing.parts_rule_of(getattr(getattr(collection, "codex", None), "slicing", None))
    closed = {} if content_rule is not None else (closed or {})
    plugin = collection.plugin_instance()
    origin = _origin_plugin(collection)
    # THE SHARD'S SHAPE, from the Codex and never from a record (09 §9.5):
    # `record_line` 2 writes the `figures` key on every line, `[]` included,
    # and it is inside `collection_id`, so it cannot vary within a build.
    record_line = int(getattr(getattr(collection, "codex", None),
                              "record_line", 1) or 1)
    from app.hold.codex import container_of
    from app.hold.shard import PART_SUFFIX
    part_suffix = PART_SUFFIX[container_of(getattr(collection, "codex", None))]
    # How this source's ids sort within a slice (09 §9.3's `slicing.order`).
    # A plugin that has not declared it yet sorts by bytes, which is what
    # `slicing.SCHEMES` has always done for a slice with no numeric key.
    order = getattr(plugin, "native_id_order", "bytes")
    slices = [r["slice"] for r in con_build.execute("SELECT DISTINCT slice FROM plan ORDER BY slice")]
    written = n_records = n_chunks = n_skipped = 0
    # THE ORIGIN'S INCONSISTENCIES (06 §6.1, amended 2026-10-25): what each
    # parse met, recorded in the manifest a page at a time (app/issues.py —
    # one keyed read per page, a write only where a document has or had
    # one), logged once each, and summed here for the build's own line
    from .. import issues as issues_mod
    codes = issues_mod.declared(plugin)
    page_issues: list[tuple[str, list]] = []
    met: dict[str, int] = {}
    undated_out = 0

    def flush_issues() -> None:
        if page_issues:
            issues_mod.record_many(con_src, page_issues, codes=codes)
            page_issues.clear()

    def say_issues() -> None:
        if met:
            log(f"[pack build] {collection.name}: origin inconsistencies met — "
                + ", ".join(f"{c} {n:,}" for c, n in sorted(met.items()))
                + f" (`louise hold issues list {collection.name}`)")
        if undated_out:
            # 06 §6.2 excludes them; the operator's choice A forbids doing it
            # silently (STATUS, 2026-10-25)
            log(f"[pack build] {collection.name}: {undated_out:,} record(s) EXCLUDED from the "
                f"snapshot cut at {until}: no cutoff date — a defect to fix, flagged as "
                f"`cutoff-undated`")

    for slice_name in slices:
        done = con_build.execute(
            "SELECT n, last_ord, last_uid FROM shards WHERE slice=? ORDER BY n DESC LIMIT 1",
            (slice_name,)).fetchone()
        # 09 §9.4: "the shard number within the slice, FROM 1"; 02 §2.2:
        # "shards are numbered 00001… per slice in the order they are
        # created". This numbered from 0 until 2026-09-08. It matters more
        # than a filename: the shard path is in the snapshot's shard entry,
        # in the member name (09 §9.16) and in a bundle header's `shard`
        # field (§9.7), so two peers that number differently disagree about
        # which shard a bundle covers. Fixed before any Codex exists, which
        # is the last moment it is free.
        #
        # A build RESUMED across this change keeps its own numbering — the
        # `+ 1` continues from what is there — so a pack half-built under
        # the old rule stays contiguous rather than growing a hole.
        next_n = (done["n"] + 1) if done else 1
        bounds = closed.get(slice_name, {})
        params: list = [slice_name]
        after = ""
        if done:
            after = " AND (ord > ? OR (ord = ? AND uid > ?))"
            params += [done["last_ord"], done["last_ord"], done["last_uid"]]
        rows = con_build.execute(
            f"SELECT uid, ord FROM plan WHERE slice=?{after} ORDER BY ord, uid", params).fetchall()
        if not rows:
            continue
        buf: list[dict] = []
        layer_counts: dict[int, int] = {}
        chunk_blobs: list[bytes] = []
        # {hash: (manifest row, href)} for the shard being filled — the
        # plates its records name, each listed once however often it
        # appears. The row travels with the href because reading a plate
        # means re-opening the book it came from.
        shard_plates: dict[str, tuple[dict, str]] = {}
        t0 = time.monotonic()

        def close_shard(last_row) -> None:
            nonlocal buf, layer_counts, chunk_blobs, shard_plates
            nonlocal written, n_records, n_chunks, next_n
            if not buf:
                return
            # the part's container (09 §9.5 "The file"): the Codex's, version 9
            rel = f"{RECORDS_DIR}/{slice_name}/{slicing.shard_name(next_n)}{part_suffix}"
            info = records.write_shard(pack_dir / rel, buf, record_line=record_line)
            # 09 §9.5's figures container, `figures/<slice>/<NNNNN>.fig.zst`,
            # BEFORE the shard is recorded: the two are re-issued together
            # because a plate arriving rewrites a record line and so moves
            # the shard's own root, and a shard recorded without its
            # container would be served naming plates nothing holds.
            if record_line == 2 and shard_plates:
                _write_figures(plugin, pack_dir, slice_name, next_n, shard_plates, log)
            # 09 §9.4's shard entry. Three roots, and each answers a
            # different question: `root` is what the transport and the
            # `roots` check compare (it depends on the compressor, which the
            # platform image pins); `records_root` is what equivalence
            # (03 §3.4), inheritance (02 §2.7) and `records-vs-origin`
            # compare, and does not; `chunks_root` is what the `chunks`
            # check reproduces, and without it that check could compare only
            # a count (09 §9.5).
            root = shard_mod.pieces_root_of(pack_dir / rel)
            records_root = hold_records.records_root(r["content_hash"] for r in buf)
            chunks_root = hold_records.chunks_root_of(b"".join(chunk_blobs))
            con_build.execute("BEGIN IMMEDIATE")
            con_build.execute(
                "INSERT OR REPLACE INTO shards(slice,n,path,records,chunks,bytes,sha256,first_uid,"
                "last_uid,last_ord,id_min,id_max,layers,written_at,"
                "root,records_root,chunks_root,first_native_id,last_native_id) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (slice_name, next_n, rel, info.records, info.chunks, info.bytes, info.sha256,
                 info.first_uid, info.last_uid, int(last_row["ord"]), info.id_min, info.id_max,
                 json.dumps(layer_counts, sort_keys=True), int(time.time()),
                 root, records_root, chunks_root,
                 buf[0]["native_id"], buf[-1]["native_id"]))
            con_build.execute("COMMIT")

            # 09 §9.5's `content_hash`, onto the manifest, for the shard
            # that just closed. It is computed above either way; storing it
            # is what lets an arriving bundle's `possession.json` (§9.7) be
            # checked with ONE indexed lookup over its uids instead of
            # re-reading and re-hashing shards per bundle — a per-bundle
            # cost that would scale with the corpus.
            #
            # A separate database, so not the shard's transaction: the
            # shards live in the staging build db and `documents` in the
            # manifest, and SQLite has no transaction across the two. It is
            # safe because it is idempotent and re-runnable (invariant 2) —
            # a crash between the two leaves NULLs, and the next build over
            # that shard writes them again.
            con_src.execute("BEGIN IMMEDIATE")
            con_src.executemany(
                "UPDATE documents SET content_hash=? WHERE uid=?",
                [(r["content_hash"], r["uid"]) for r in buf])
            con_src.execute("COMMIT")

            written += 1
            n_records += info.records
            n_chunks += info.chunks
            next_n += 1
            buf, layer_counts, chunk_blobs, shard_plates = [], {}, [], {}

        last_row = None
        done_rows = 0
        # records are computed by the PARSE POOL (app/parallel.py) a page
        # at a time — parse + chunk + hash per document is the whole cost
        # of a pack pass, and it is pure Python (amendment 47)
        page_size = max(50, min(records_per_shard, 500))
        # THE PAGES STREAM (parallel.compute_pages): the pool computes the
        # next pages while this thread writes the current one, instead of a
        # barrier per page — München's profile, 2026-10-24: one page at a
        # time kept ~1.3 of 4 cpus busy on wikipedia-v8, 47.8 % of the
        # children's samples idle behind each page's longest article
        pending: deque = deque()

        def pages_of():
            for page_start in range(0, len(rows), page_size):
                page = rows[page_start:page_start + page_size]
                srcs = []
                for prow in page:
                    src = con_src.execute(
                        "SELECT uid, native_id, path, lang FROM documents WHERE uid=?",
                        (prow["uid"],)).fetchone()
                    srcs.append(dict(src) if src is not None else None)
                pending.append((page, srcs))
                yield [s for s in srcs if s is not None]
        # THE NODE PROFILE'S CORES (app/cpu_limit.py, operator 2026-10-27):
        # the pool keeps at most that many parts in flight, re-read at each
        # page; 0 pauses at a page boundary — the pages in flight are taken
        # and the parts they complete committed, the open part's records stay
        # buffered — until a profile allows cores again. A stop meanwhile is
        # this loop's stop at a page boundary. Bytes never depend on it.
        say = lambda m: log(f"[pack build] {collection.name}: {m}")   # noqa: E731
        halted: list[bool] = []

        def held_pages():
            watch = cpu_limit.Watch(say)
            for wanted in pages_of():
                if watch.limit().cores == 0 and not cpu_limit.wait_while_paused(stop, watch):
                    raise parallel.Interrupted("stopped while the node profile paused the build")
                yield _held_values(collection, con_build, held, wanted)

        def guarded(raw):
            try:
                yield from raw
            except parallel.Interrupted:
                halted.append(True)
        stream = guarded(held_pages() if held is not None else
                         parallel.compute_pages("record", collection, pages_of(), stop=stop, say=say))
        for results in stream:
            page, srcs = pending.popleft()
            if stop is not None and stop.is_set():
                stream.close()                 # what the pool still holds is cancelled
                say_issues()
                log(f"[pack build] stop requested — {slice_name}: {len(buf)} buffered records "
                    f"discarded, resumes at shard {next_n}")
                return written, n_records, n_chunks, n_skipped, True
            computed = iter(results)
            for prow, src in zip(page, srcs):
                done_rows += 1
                if src is None:
                    n_skipped += 1
                    continue
                ok, value = next(computed)
                if not ok and value.get("type") == "OriginOutsideBase":
                    # A record that cannot locate its origin bytes is not made,
                    # and the build stops, as for a missing `origin_hash`: a
                    # skipped record is a quietly incomplete SHARD (invariant 11)
                    raise BuildError(f"{prow['uid']}: {value.get('msg')}")
                if not ok:
                    n_skipped += 1
                    con_build.execute("INSERT OR REPLACE INTO skipped(uid,reason) VALUES(?,?)",
                                      (prow["uid"], f"pack: {value.get('type')}: {value.get('msg')}"[:200]))
                    continue
                record, layer, chunk_blob, plates = value
                found = record.pop(ISSUES_KEY, None)
                if found is not None:
                    found = list(found)
                    why = cutoff_mod.undated(origin, record)
                    if why is not None:
                        found.append(("cutoff-undated", why + (
                            f"; excluded from the snapshot cut at {until}" if until else "")))
                    page_issues.append((src["uid"], found))
                    for code, _ in found:
                        met[code] = met.get(code, 0) + 1
                if not record.get("origin_hash"):
                    # CORPUS FIDELITY (invariant 11). `origin_hash` is the
                    # only link between a packed record and the origin bytes
                    # it was parsed from, and it is what `records-vs-origin`
                    # checks against. A pack written with nulls here looks
                    # entirely valid — every content_hash verifies, every
                    # shard hashes, the manifest signs — while being
                    # permanently unverifiable against its source.
                    #
                    # So this aborts the BUILD. Skipping the record instead
                    # would ship a quietly incomplete pack, and a pack is
                    # published once and adopted by peers who cannot repair
                    # either fault.
                    raise ValueError(
                        f"{record['uid']}: the {collection.plugin!r} plugin "
                        f"returned no raw_hash, so this record could never be "
                        f"verified against its origin (CLAUDE.md invariant 11). "
                        f"Fix the plugin's canonical_doc; do not pack.")
                _refuse_uncredited_origin(collection, record)
                _refuse_too_many_figures(record)
                if record["uid"] != src["uid"]:
                    n_skipped += 1
                    con_build.execute("INSERT OR REPLACE INTO skipped(uid,reason) VALUES(?,?)",
                                      (prow["uid"], f"pack: parsed uid {record['uid']} != manifest uid"))
                    continue
                outside = cutoff_mod.excluded(origin, record, until)
                if outside is not None:
                    n_skipped += 1
                    undated_out += outside.startswith("no cutoff date")
                    con_build.execute("INSERT OR REPLACE INTO skipped(uid,reason) VALUES(?,?)",
                                      (prow["uid"], f"cutoff: {outside}"[:200]))
                    continue
                # 02 §2.2: a record whose `native_id` sorts before the OPEN
                # shard's first id is REFUSED, naming the record and the
                # shard, rather than placed out of order. That is what makes
                # shard ranges a partition of the id space, which 05 §5.5
                # needs to resolve a uid to a shard from a snapshot alone,
                # and what stops two honest peers cutting one cutoff into
                # different shards. Like the origin_hash refusal above it
                # aborts the build rather than skipping the record: a shard
                # cut two ways is not repairable by the peer who receives it.
                # A CLOSED PART ENDS WHERE IT ENDED (02 §2.2): past its last
                # id the part is cut, however few records it kept; a part
                # left with none is a number skipped, never refilled
                key = hold_records.order_key(record["native_id"], order)
                while next_n in bounds and key > hold_records.order_key(bounds[next_n][1], order):
                    if buf:
                        close_shard(last_row)
                    else:
                        next_n += 1
                if next_n in bounds and key < hold_records.order_key(bounds[next_n][0], order):
                    raise hold_records.RecordsError(
                        f"{record['native_id']!r} sorts between closed parts, before "
                        f"{bounds[next_n][0]!r}, the first id of {slice_name}/{next_n:05d}. A closed "
                        f"part is never refilled (02 §2.2), so this record is refused rather "
                        f"than placed in one")
                hold_records.refuse_out_of_order(
                    record["native_id"], buf[0]["native_id"] if buf else None,
                    order, slice_=slice_name, n=next_n)
                buf.append(record)
                chunk_blobs.append(chunk_blob)
                for figure_hash, href in plates.items():
                    shard_plates.setdefault(figure_hash, (src, href))
                layer_counts[layer] = layer_counts.get(layer, 0) + 1
                last_row = prow
                if (slicing.ends_part(record["uid"], len(buf), content_rule) if content_rule is not None
                        else next_n not in bounds and len(buf) >= records_per_shard):
                    close_shard(last_row)
                    if time.monotonic() - t0 > 30:
                        log(f"[pack build] {slice_name}: shard {next_n - 1} written "
                            f"({done_rows}/{len(rows)} records of the slice)")
                        t0 = time.monotonic()
                    if stop is not None and stop.is_set():
                        # the unit is a shard: what the page still holds is
                        # recomputed on resume
                        stream.close()
                        flush_issues()
                        say_issues()
                        log(f"[pack build] stop requested — {slice_name}: resumes at shard {next_n}")
                        return written, n_records, n_chunks, n_skipped, True
            flush_issues()
        if halted:
            say_issues()
            log(f"[pack build] stop requested while the node profile paused the build — "
                f"{slice_name}: {len(buf)} buffered records discarded, resumes at shard {next_n}")
            return written, n_records, n_chunks, n_skipped, True
        close_shard(last_row)
        log(f"[pack build] {slice_name}: {next_n - 1} shard(s)")   # next_n is the NEXT index: one past the last written
    say_issues()
    return written, n_records, n_chunks, n_skipped, False


def _license_of(origin_plugin: str) -> tuple[str, Path | None]:
    from ..acquire import specs
    spec = specs.ACQUIRE_REGISTRY.get(origin_plugin)
    if spec is None:
        from ..collections import PLUGIN_REGISTRY
        plugin = PLUGIN_REGISTRY.get(origin_plugin)
        cls = getattr(getattr(plugin, "license_class", None), "value", "yellow")
        return cls, None
    return spec.license_class.value, spec.terms_text_path


def write_manifest(collection, con_build, pack_dir: Path, *, snapshot: str, name: str,
                   records_per_shard: int, until: str | None = None, sign: bool = True,
                   log=print) -> Path:
    import hashlib
    origin = _origin_plugin(collection)
    scheme = slicing.scheme_for(origin)
    license_class, terms_path = _license_of(origin)
    terms_sha = None
    if terms_path is not None and terms_path.is_file():
        data = terms_path.read_bytes()             # byte-exact: never re-encoded (invariant 9)
        (pack_dir / TERMS_FILE).write_bytes(data)
        terms_sha = hashlib.sha256(data).hexdigest()
    publisher = None
    key = None
    if sign and config.NODE_KEY_PATH:
        from ..bundles import keys as node_keys
        key = node_keys.load_signing_key()
        publisher = node_keys.public_key_hex(key)
    m = manifest_mod.new_manifest(
        plugin=origin, name=name, snapshot=snapshot, license_class=license_class,
        terms_sha256=terms_sha, slicing={**slicing.describe_for(collection, scheme),
                                         "records_per_shard": records_per_shard},
        layers=[l.as_manifest() for l in layers_mod.layers_for(collection)],
        cleaning_version=config.CLEANING_VERSION,
        origin={"plugin": origin, "corpus_snapshot": getattr(collection, "snapshot", None),
                "source_collection": collection.name,
                "cutoff": {"until": until, "rule": cutoff_mod.rule_for(origin)} if until else None},
        publisher_hex=publisher)
    m["closed"] = bool(until)      # a dated snapshot: nothing later belongs to it
    slices: dict[str, dict] = {}
    shards = []
    for r in con_build.execute("SELECT * FROM shards ORDER BY slice, n"):
        s = slices.setdefault(r["slice"], {"shards": 0, "records": 0, "chunks": 0, "bytes": 0})
        s["shards"] += 1
        s["records"] += r["records"] or 0
        s["chunks"] += r["chunks"] or 0
        s["bytes"] += r["bytes"] or 0
        shards.append({"slice": r["slice"], "n": r["n"], "path": r["path"], "sha256": r["sha256"],
                       "bytes": r["bytes"], "records": r["records"], "chunks": r["chunks"],
                       "ids": [r["first_uid"], r["last_uid"]],
                       "id_min": r["id_min"], "id_max": r["id_max"],
                       "layers": json.loads(r["layers"] or "{}"), "index_min": None})
    m["slices"] = slices
    m["shards"] = shards
    m["records"] = sum(s["records"] for s in slices.values())
    m["chunks"] = sum(s["chunks"] for s in slices.values())
    m["skipped"] = int(con_build.execute("SELECT COUNT(*) FROM skipped").fetchone()[0])
    if key is not None:
        m = manifest_mod.sign(m, key)
    (pack_dir / VALIDATION_FILE).write_text(json.dumps(
        {"checks": ["signature", "roots", "records", "chunks", "license"], "sample": 0.05}, indent=1) + "\n",
        encoding="utf-8")
    path = manifest_mod.write(pack_dir, m)
    log(f"[pack build] manifest: {len(slices)} slice(s), {len(shards)} shard(s), "
        f"{m['records']} records, {m['chunks']} chunks"
        + (", signed" if key is not None
           # `hold records build` asks for no signature: its manifest is staging,
           # and the snapshot is what is signed — the old wording named a
           # missing NODE_KEY_PATH on a worker that had one (2026-10-07)
           else ", unsigned (the snapshot carries the signature)" if not sign
           else ", UNSIGNED (no NODE_KEY_PATH)"))
    return path


def build(collection, pack_dir: Path, *, snapshot: str | None = None, name: str | None = None,
          records_per_shard: int | None = None, until: str | None = None, sign: bool = True,
          log=print, stop=None, closed: dict | None = None, held: HeldRecords | None = None) -> BuildResult:
    """`held`: take each record's line from records already built (a mother's
    parts) instead of parsing it — RULE 1, for a daughter or re-creation
    whose records are the same records (`HeldRecords`)."""
    from .. import licensing
    if licensing.state_for(collection) == licensing.MISSING:
        # a pack carries the data onward: it must not carry data whose
        # terms were never acknowledged here (invariant 9)
        raise BuildError(
            f"{collection.name}: license_class={licensing.license_class_for(collection)} "
            f"requires an acknowledgement of the terms before its data is packed")
    pack_dir = Path(pack_dir)
    pack_dir.mkdir(parents=True, exist_ok=True)
    origin = _origin_plugin(collection)
    scheme = slicing.scheme_for(origin)
    rps = int(records_per_shard or scheme.records_per_shard)
    until = cutoff_mod.check_until(until)
    snapshot = snapshot or until or time.strftime("%Y-%m-%d", time.gmtime())
    name = name or collection.name
    con_src = db.connect(collection)
    con_build = _build_db(pack_dir)
    try:
        prior = _meta(con_build, "plugin")
        if prior and prior != origin:
            raise BuildError(f"{pack_dir} was started for plugin {prior}, not {origin}")
        prior_until = _meta(con_build, "until")
        if prior_until is not None and prior_until != (until or ""):
            # the cutoff decides which records exist: resuming under another
            # one would mix two snapshots in one pack
            raise BuildError(f"{pack_dir} was started with --until {prior_until or 'none'}; "
                             f"a different cutoff needs a new pack directory")
        _set_meta(con_build, "until", until or "")
        _set_meta(con_build, "plugin", origin)
        _set_meta(con_build, "records_per_shard", rps)
        if _meta(con_build, "plan_done") != "1":
            n = plan(collection, con_src, con_build, log=log, stop=stop)
            if stop is not None and stop.is_set() and _meta(con_build, "plan_done") != "1":
                log(f"[pack build] stop requested during planning ({n} planned this run); resumes")
                return BuildResult(pack_dir, 0, 0, 0, 0, 0, True, None)
            total = con_build.execute("SELECT COUNT(*) FROM plan").fetchone()[0]
            log(f"[pack build] plan: {total} documents in "
                f"{con_build.execute('SELECT COUNT(DISTINCT slice) FROM plan').fetchone()[0]} slice(s)")
        if held is not None:
            if int(getattr(getattr(collection, "codex", None), "record_line", 1) or 1) != 1:
                raise BuildError(f"{collection.name}: records with figures are not reused from held "
                                 f"parts (their figure containers would have to travel too)")
            _held_index(con_build, held, log)
        written, n_rec, n_chunks, n_skip, interrupted = pack_slices(
            collection, con_src, con_build, pack_dir, records_per_shard=rps, until=until,
            log=log, stop=stop, closed=closed, held=held)
        if interrupted:
            return BuildResult(pack_dir, 0, written, n_rec, n_chunks, n_skip, True, None)
        manifest_path = write_manifest(collection, con_build, pack_dir, snapshot=snapshot,
                                       name=name, records_per_shard=rps, until=until, sign=sign,
                                       log=log)
        totals = con_build.execute(
            "SELECT COUNT(*) n, COUNT(DISTINCT slice) s, SUM(records) r, SUM(chunks) c FROM shards").fetchone()
        skipped = con_build.execute("SELECT COUNT(*) FROM skipped").fetchone()[0]
        return BuildResult(pack_dir, totals["s"] or 0, totals["n"] or 0, totals["r"] or 0,
                           totals["c"] or 0, skipped, False, manifest_path)
    finally:
        con_build.close()
        con_src.close()


def clean_build_state(pack_dir: Path) -> None:
    shutil.rmtree(Path(pack_dir) / BUILD_DIR, ignore_errors=True)
