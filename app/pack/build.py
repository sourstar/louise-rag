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
from dataclasses import dataclass
from pathlib import Path

from .. import config, db, layers as layers_mod, parallel, plugin_io
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


class BuildError(RuntimeError):
    pass


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
      layers TEXT, written_at INTEGER, PRIMARY KEY(slice, n));
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
    """The plugin whose scheme applies: a packed corpus keeps its origin's."""
    if collection.plugin == "packed":
        try:
            return manifest_mod.load(collection.corpus_dir)["plugin"]
        except manifest_mod.ManifestError:
            return "packed"
    return collection.plugin


def plan(collection, con_src, con_build, *, log=print, stop=None) -> int:
    """Pass 1. Resumable by rowid cursor. Returns rows planned this run."""
    plugin = collection.plugin_instance()
    origin = _origin_plugin(collection)
    scheme = slicing.scheme_for(origin)
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
            slice_name = slicing.slice_hint(origin, row)
            if slice_name is None:
                try:
                    doc = plugin_io.canonical_doc(plugin, row)
                    slice_name = scheme.slice_of(doc, int(row["native_id"]))
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


def _record_of(collection, plugin, row) -> tuple[dict, int]:
    doc = plugin_io.canonical_doc(plugin, row)
    native_id = int(row["native_id"])
    sections = [[name or "", body or ""] for name, body in doc.sections]
    layer = layers_mod.layer_index(collection, native_id)
    chunks = sum(1 for _ in layers_mod.chunker_for_document(collection, native_id)(
        doc.uid, native_id, doc.sections))
    record = {
        "uid": doc.uid, "native_id": str(row["native_id"]), "lang": doc.language or "und",
        "title": doc.title or "", "creators": list(doc.creators or []),
        "date": doc.date, "subjects": list(doc.subjects or []), "keys": list(doc.keys or []),
        "sections": sections, "extra": dict(doc.extra or {}),
        "layer": layer, "chunks": chunks,
        "content_hash": records.content_hash(doc.title or "", sections),
        "origin_hash": doc.raw_hash,
    }
    # NOTE: no validation here. This runs in a parse-pool child, where an
    # exception becomes one skipped row rather than a failed build — so a
    # fidelity defect raised here would quietly DROP documents from the
    # pack, which is worse than what it was meant to prevent. The check
    # belongs in pack_slices, in the process that can abort.
    return record, layer


def pack_slices(collection, con_src, con_build, pack_dir: Path, *, records_per_shard: int,
                until: str | None = None, log=print, stop=None) -> tuple[int, int, int, int, bool]:
    """Pass 2. Returns (shards written, records, chunks, skipped, interrupted).
    `until`: the snapshot cutoff (cutoff.py) — records outside it are
    skipped here, where the document is parsed anyway, so shards stay
    full and the result is a pure function of (manifest, cutoff)."""
    plugin = collection.plugin_instance()
    origin = _origin_plugin(collection)
    slices = [r["slice"] for r in con_build.execute("SELECT DISTINCT slice FROM plan ORDER BY slice")]
    written = n_records = n_chunks = n_skipped = 0
    for slice_name in slices:
        done = con_build.execute(
            "SELECT n, last_ord, last_uid FROM shards WHERE slice=? ORDER BY n DESC LIMIT 1",
            (slice_name,)).fetchone()
        next_n = (done["n"] + 1) if done else 0
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
        t0 = time.monotonic()

        def close_shard(last_row) -> None:
            nonlocal buf, layer_counts, written, n_records, n_chunks, next_n
            if not buf:
                return
            rel = f"{RECORDS_DIR}/{slice_name}/{slicing.shard_name(next_n)}{records.SUFFIX}"
            info = records.write_shard(pack_dir / rel, buf)
            con_build.execute("BEGIN IMMEDIATE")
            con_build.execute(
                "INSERT OR REPLACE INTO shards(slice,n,path,records,chunks,bytes,sha256,first_uid,"
                "last_uid,last_ord,id_min,id_max,layers,written_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (slice_name, next_n, rel, info.records, info.chunks, info.bytes, info.sha256,
                 info.first_uid, info.last_uid, int(last_row["ord"]), info.id_min, info.id_max,
                 json.dumps(layer_counts, sort_keys=True), int(time.time())))
            con_build.execute("COMMIT")
            written += 1
            n_records += info.records
            n_chunks += info.chunks
            next_n += 1
            buf, layer_counts = [], {}

        last_row = None
        done_rows = 0
        # records are computed by the PARSE POOL (app/parallel.py) a page
        # at a time — parse + chunk + hash per document is the whole cost
        # of a pack pass, and it is pure Python (amendment 47)
        page_size = max(50, min(records_per_shard, 500))
        for page_start in range(0, len(rows), page_size):
            if stop is not None and stop.is_set():
                log(f"[pack build] stop requested — {slice_name}: {len(buf)} buffered records "
                    f"discarded, resumes at shard {next_n}")
                return written, n_records, n_chunks, n_skipped, True
            page = rows[page_start:page_start + page_size]
            srcs = []
            for prow in page:
                src = con_src.execute(
                    "SELECT uid, native_id, path, lang FROM documents WHERE uid=?",
                    (prow["uid"],)).fetchone()
                srcs.append(dict(src) if src is not None else None)
            computed = iter(parallel.compute("record", collection, [s for s in srcs if s is not None]))
            for prow, src in zip(page, srcs):
                done_rows += 1
                if src is None:
                    n_skipped += 1
                    continue
                ok, value = next(computed)
                if not ok:
                    n_skipped += 1
                    con_build.execute("INSERT OR REPLACE INTO skipped(uid,reason) VALUES(?,?)",
                                      (prow["uid"], f"pack: {value.get('type')}: {value.get('msg')}"[:200]))
                    continue
                record, layer = value
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
                if record["uid"] != src["uid"]:
                    n_skipped += 1
                    con_build.execute("INSERT OR REPLACE INTO skipped(uid,reason) VALUES(?,?)",
                                      (prow["uid"], f"pack: parsed uid {record['uid']} != manifest uid"))
                    continue
                outside = cutoff_mod.excluded(origin, record, until)
                if outside is not None:
                    n_skipped += 1
                    con_build.execute("INSERT OR REPLACE INTO skipped(uid,reason) VALUES(?,?)",
                                      (prow["uid"], f"cutoff: {outside}"[:200]))
                    continue
                buf.append(record)
                layer_counts[layer] = layer_counts.get(layer, 0) + 1
                last_row = prow
                if len(buf) >= records_per_shard:
                    close_shard(last_row)
                    if time.monotonic() - t0 > 30:
                        log(f"[pack build] {slice_name}: shard {next_n - 1} written "
                            f"({done_rows}/{len(rows)} records of the slice)")
                        t0 = time.monotonic()
                    if stop is not None and stop.is_set():
                        # the unit is a shard: what the page still holds is
                        # recomputed on resume
                        log(f"[pack build] stop requested — {slice_name}: resumes at shard {next_n}")
                        return written, n_records, n_chunks, n_skipped, True
        close_shard(last_row)
        log(f"[pack build] {slice_name}: {next_n} shard(s)")
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
        terms_sha256=terms_sha, slicing={**scheme.describe, "records_per_shard": records_per_shard},
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
        + (", signed" if key is not None else ", UNSIGNED (no NODE_KEY_PATH)"))
    return path


def build(collection, pack_dir: Path, *, snapshot: str | None = None, name: str | None = None,
          records_per_shard: int | None = None, until: str | None = None, sign: bool = True,
          log=print, stop=None) -> BuildResult:
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
        written, n_rec, n_chunks, n_skip, interrupted = pack_slices(
            collection, con_src, con_build, pack_dir, records_per_shard=rps, until=until,
            log=log, stop=stop)
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
