"""hold.deposit — deposit collections: created and written by a client of
this node, never shared. docs/02 §2.9, 06 §6.7, 07 §7.3; the design is
docs/notes/deposit-collections.md (migration step 16a).

    hold collection create deposit --name N   → `create`
    hold deposit add <collection> FILE        → `add`
    hold deposit erase <collection> <key>     → `erase`
    hold deposit list <collection>            → `keys`
    hold deposit show <collection> <key>      → `show`

A deposit is stored byte-exact under the collection's `origin/` and then
taken through THE WORKER'S OWN rdf and fts stages, for that document, inside
the call — so it is searchable by text when `add` returns, by exactly the
code that indexes every other document (wrap, do not rewrite). Vectors stay
the worker's: nothing here calls an encoder, because a request that waits on
the GPU is a request that times out whenever the GPU is busy (07 §7.4).
"""
from __future__ import annotations

import json
import os
import secrets
import shutil
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any

from . import codex as cx
from .codex import Codex, CodexError

PLUGIN = "deposit"

#: The node-wide deposit number's key in `state/node.sqlite`'s control
#: table (02 §2.9: unique within the plugin, later deposits sort after
#: earlier ones). Node state, beside the node's mode.
SEQ_KEY = "deposit_seq"

#: The deposit key lives in `documents.txt_name`, and `add`, `erase` and
#: `show` find a deposit by it. An index, so that finding one is a lookup
#: and never a walk of the collection (CLAUDE.md: a per-call cost scales
#: with the work, not with what has been deposited so far). Created on a
#: deposit collection's manifest only: no other plugin reads by it.
_KEY_INDEX = ("CREATE INDEX IF NOT EXISTS idx_documents_deposit_key "
              "ON documents(txt_name)")

#: Set in the manifest before an erase begins and cleared by the purge that
#: completes it: a crash in between leaves it set, and the next opening of
#: the collection runs the purge — so "no trace" survives a crash too.
PURGE_PENDING = "deposit_purge_pending"

ORIGIN_FORM = "deposit: the bytes as the client sent them"
#: 09 §9.3's closed list; a deposit's date is when it was deposited
CUTOFF_RULE = "record-date"
SUMMARY = "Deposits written by a client of this node; never shared (02 §2.9)."


class DepositError(CodexError):
    """Refused, with nothing stored."""


# --------------------------------------------------------------------------
# the collection

def _source():
    from app.plugins.deposit import source
    return source


def recipe(name: str, space: dict[str, Any]) -> dict[str, Any]:
    """The whole recipe of a deposit collection — every field the plugin
    does not default, derived and never asked for (02 §2.9).

    `origin.spec` is `{source_id, nonce}` and nothing else (09 §9.17). The
    nonce is load-bearing: `collection_id` hashes the Codex without
    `descriptive`, where the name lives, so without it two agents'
    collections made from these defaults would be ONE collection."""
    return {
        "descriptive": {"title": name, "summary": SUMMARY},
        "space": space,
        # 02 §2.9: written, not run — nothing ever measures a deposit
        # collection against it; 1, because §9.3 wants it positive
        "work": {"tokens": 1},
        "origin": {"spec": {"source_id": PLUGIN, "nonce": secrets.token_hex(16)},
                   "form": ORIGIN_FORM, "unit": "file", "clock": "UTC",
                   "cutoff_rule": CUTOFF_RULE, "jurisdiction": None},
        # 02 §2.9 / 03 §3.2: a never-share collection runs these two and
        # nothing else — nobody else holds it to prove anything to.
        # 09 §9.3 wants a shard fraction for a sampled check; there are no
        # shards, and the local timer samples chunks (step 16c).
        "validation": {"checks": ["calibration", "vectors-sample"],
                       "sample": {"vectors-sample": {"shards": "1"}},
                       "required": []},
        "layers": [_source().fixed_layer(int(space["ctx"]))],
        "update": {"cadence": "monthly", "day": 1, "grace_days": 0},
    }


def is_deposit(codex: Codex) -> bool:
    return codex.plugin == PLUGIN


def held_file(root: Path, name: str) -> Path:
    """Where `collections/<name>.toml` is for a collection under `root` —
    the node's CONFIG_DIR for the node's own root, beside it otherwise
    (the rule `louise._write_held_file` follows)."""
    from app import collections as collections_mod
    node_root = collections_mod.louise_root() / "collections"
    if Path(root).resolve() == node_root.resolve():
        return collections_mod.config_toml_path(name)
    return Path(root) / f"{name}.toml"


def _encoder_block(root: Path, name: str) -> dict[str, Any]:
    import tomllib
    p = held_file(root, name)
    try:
        with open(p, "rb") as f:
            raw = tomllib.load(f)
    except (OSError, ValueError):
        return {}
    enc = raw.get("encoder") or {}
    return enc if isinstance(enc, dict) else {}


def encoder_of(root: Path, name: str) -> str | None:
    enc = _encoder_block(root, name)
    return str(enc.get("url")) if enc.get("url") else None


def encoder_models(encoder_url: str, *, embed_model: str | None = None,
                   query_model: str | None = None) -> tuple[str, str]:
    """(`embed_model`, `query_model`) a new deposit collection's file names
    (07 §7.8, amended 2026-10-09): `[deposit]`'s keys where it gives them,
    else the model a model-container URL names (`…/upstream/<model>`).

    A URL that names no model (LocalAI's one address for every model) and
    no keys is REFUSED — never the space's encoder label in its place: the
    label is a description ("granite … f16 (LocalAI, …)"), not a model any
    server is asked for, and a file naming it can neither embed nor search.
    The encoder is symmetric (02 §2.9), so the query side defaults to the
    document side's model."""
    from app import collections as collections_mod
    embed = embed_model or collections_mod.model_named_by(encoder_url)
    if not embed:
        raise DepositError(
            f"the deposit encoder {encoder_url} names no model (…/upstream/<model>) "
            f"and [deposit] names none: set embed_model and query_model in "
            f"platform.toml's [deposit] (07 §7.8). Nothing was created")
    return embed, query_model or embed


def space_source(root: Path, encoder_url: str | None,
                 embed_model: str | None = None) -> Codex | None:
    """A deposit collection this root already holds on the same encoder —
    endpoint AND model — whose space, calibration included, a new one copies
    whole instead of embedding the calibration set again (02 §2.9; CLAUDE.md
    RULE 1: the bytes would be identical). The oldest such, so the choice
    is stable.

    The model counts as well as the URL: on LocalAI one address serves every
    model, and a collection on another model there is another space. Where
    no model is known the URL alone decides, as it did before (a
    model-container URL names its model, so the two agree)."""
    from . import collection as col
    if not encoder_url:
        return None
    from app import collections as collections_mod
    wanted = embed_model or collections_mod.model_named_by(encoder_url)

    def same(name: str) -> bool:
        enc = _encoder_block(root, name)
        if str(enc.get("url") or "") != encoder_url:
            return False
        return wanted is None or enc.get("embed_model") in (None, wanted)

    found = [(name, c) for name, c in col.list_named(root)
             if is_deposit(c) and same(name)]
    if not found:
        return None
    found.sort(key=lambda nc: nc[0])
    return found[0][1]


def create(root: Path, name: str, *, platform_image: str,
           encoder_url: str | None = None, embed_model: str | None = None,
           space_from: Codex | None = None, space: dict[str, Any] | None = None,
           embed=None, key=None, now: str | None = None) -> Codex:
    """`hold collection create deposit --name N` (07 §7.3).

    The space comes, in order, from `space_from` (a Codex whose space is
    copied whole, calibration and all), from a deposit collection already
    held on the same encoder endpoint (the same), or from `space` — a space
    block without its computed hashes, which `create` fills by embedding
    the calibration set with `embed`, exactly as any `create` does (06
    §6.6). The first deposit collection on an encoder pays that once; the
    others copy it.

    A name held by any collection is refused (07 §7.4: 409). Creation is
    NOT idempotent by name the way a recipe's is by hash: the nonce makes
    every call a new collection, so asking twice for one name is refused
    rather than answered with a second identity."""
    from . import collection as col, template

    col.check_name(name)
    if (Path(root) / name).exists() or held_file(root, name).exists():
        raise DepositError(f"the name {name!r} is held already; a name holds "
                           f"one collection for its life (07 §7.8)")
    source = space_from or space_source(root, encoder_url, embed_model)
    if source is not None:
        if source.modality != "text":
            raise DepositError(
                f"{source.collection_id} is a {source.modality} collection; a "
                f"deposit collection's encoder is a text encoder (02 §2.9)")
        # The hashes are spliced in as `create` would have computed them —
        # they are the source's, computed by its own `create` — and the
        # member they name is copied and checked against them below.
        space_block = source.space.to_dict()
        space_block.pop("envelope", None)
        space_block["calibration"] = {k: v for k, v in space_block["calibration"].items()
                                      if k not in ("vectors", "query_vectors")}
        r = recipe(name, space_block)
        codex = template.from_recipe(
            PLUGIN, r, platform_image=platform_image,
            calibration=(source.space.calibration.vectors, source.space.envelope,
                         source.space.calibration.query_vectors),
            space_version=source.version)
        created = col.create(root, codex, name=name)
        d = col.shard_dir_of(root, name)
        col.carry_calibration(root, created, d, source)
        _origin_of(root, name).mkdir(parents=True, exist_ok=True)
        _init_manifest(root, name, created)
        return created
    if space is None:
        raise DepositError(
            "no space to create a deposit collection in: name one with "
            "--space-from CODEX, or give the space block once with --space "
            "FILE — later deposit collections on the same encoder copy it "
            "(02 §2.9)")
    if embed is None:
        raise DepositError("the first deposit collection on an encoder embeds "
                           "the calibration set (06 §6.6): name the encoder")
    created = col.create_from_recipe(root, PLUGIN, recipe(name, dict(space)),
                                     platform_image=platform_image, embed=embed,
                                     name=name, key=key, now=now)
    _origin_of(root, name).mkdir(parents=True, exist_ok=True)
    _init_manifest(root, name, created)
    return created


def _init_manifest(root: Path, name: str, codex: Codex) -> None:
    """The manifest, created in WAL mode by the one thread that creates the
    collection, BEFORE the held file makes it discoverable.

    A fresh manifest's first `db.connect` converts it from a rollback
    journal to WAL under an exclusive lock SQLite takes without consulting
    the busy handler, so two threads opening one at the same moment — the
    deposit thread's `finish_pending`, the sources thread, the worker's
    scan — got "database is locked" out of the connect itself (the merged
    suite, 2026-10-05). Every regular collection is first opened by one
    thread, discovery; a deposit collection has three writers from birth."""
    from app import db
    from .records import local_collection
    db.connect(local_collection(root, name, codex)).close()


def _origin_of(root: Path, name: str) -> Path:
    """07 §7.8: `create` writes the empty `origin/` — the corpus directory
    of a collection that holds nothing yet, which discovery would otherwise
    report missing every round."""
    return Path(root) / name / "origin"


def write_held_file(root: Path, codex: Codex, encoder_url: str | None, *,
                    api: str = "openai", embed_model: str | None = None,
                    query_model: str | None = None) -> Path:
    """07 §7.8's collection file for a deposit collection: every slice (it
    has none), records and vectors, EVERY deposit embedded (a deposit is
    small and is asked for as soon as it lands), never shared — and the
    `[encoder]` `[deposit]` describes: url, api and both sides' models
    (amended 2026-10-09; `encoder_models` says where the models come from)."""
    from . import collection as col
    name = col.name_of(root, codex.collection_id)
    models = (encoder_models(encoder_url, embed_model=embed_model, query_model=query_model)
              if encoder_url else None)
    path = held_file(root, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".toml.tmp")
    # json.dumps: a TOML basic string, quotes and backslashes escaped
    tmp.write_text(f'codex = "{codex.collection_id}"\n'
                   'slices = "all"\n'
                   'kinds = ["records", "vectors"]\n'
                   'embed = "all"\n'
                   "share = false\n"
                   + (f'\n[encoder]\nurl = {json.dumps(encoder_url)}\napi = {json.dumps(api)}\n'
                      f'embed_model = {json.dumps(models[0])}\n'
                      f'query_model = {json.dumps(models[1])}\n'
                      if models else ""),
                   encoding="utf-8")
    tmp.replace(path)
    return path


# --------------------------------------------------------------------------
# the node-wide deposit number

def next_native_id() -> int:
    """02 §2.9: node-wide and monotonic — unique within the plugin, and a
    later deposit sorts after every earlier one, in every deposit
    collection. Taken under `BEGIN IMMEDIATE` (a writer that reads first),
    so two concurrent deposits never draw the same number."""
    from app import node_state
    with closing(node_state.connect()) as con:
        con.execute("BEGIN IMMEDIATE")
        try:
            row = con.execute("SELECT value FROM control WHERE key=?",
                              (SEQ_KEY,)).fetchone()
            n = int(row[0]) + 1 if row else 1
            con.execute("INSERT INTO control(key,value) VALUES(?,?) ON CONFLICT(key) "
                        "DO UPDATE SET value=excluded.value", (SEQ_KEY, str(n)))
            con.execute("COMMIT")
        except BaseException:
            con.execute("ROLLBACK")
            raise
    return n


# --------------------------------------------------------------------------
# deposits

def _collection(name: str):
    from app import collections as collections_mod
    c = collections_mod.by_name(name)
    if c is None:
        raise DepositError(f"no collection {name!r} is held")
    if c.codex is None or not is_deposit(c.codex):
        raise DepositError(f"{name!r} is not a deposit collection; only a deposit "
                           f"collection is written by a client (02 §2.9)")
    return c


def _connect(collection, *, write: bool = True) -> sqlite3.Connection:
    """The collection's manifest. A READ (`write=False`: list, show) writes
    nothing — it may run in the web process, which never writes a deposit
    collection (app/jobs.py DEPOSIT)."""
    from app import db
    con = db.connect(collection)
    if not write:
        return con
    # 02 §2.9: freed pages are zeroed. The platform's SQLite is built with
    # it on; said here as well, so no build and no other code path decides.
    con.execute("PRAGMA secure_delete=ON")
    con.execute(_KEY_INDEX)
    if db.get_control(con, PURGE_PENDING):
        purge(collection, con)          # an erase that did not finish did
    return con


def _row(con, key: str):
    return con.execute("SELECT * FROM documents WHERE source=? AND txt_name=?",
                       (PLUGIN, key)).fetchone()


def _fsync_write(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".part")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(path)


_write_locks: dict[str, "threading.Lock"] = {}
_write_locks_guard = __import__("threading").Lock()


def _writing(name: str):
    """One writer per deposit collection in this process — the deposit
    thread and the sources thread both write, and a purge rewrites whole
    indexes: an add landing between its read and its rewrite would be
    lost. Keyed on the collection's directory, its physical identity."""
    from app import collections as collections_mod
    key = str(collections_mod.louise_root() / "collections" / name)
    with _write_locks_guard:
        lock = _write_locks.get(key)
        if lock is None:
            lock = _write_locks[key] = __import__("threading").Lock()
    return lock


def _add(name: str, key: str, data: bytes, *, url: str | None = None,
         now: float | None = None) -> dict[str, Any]:
    """`hold deposit add` — store `data` as the deposit `key`, replacing a
    deposit already under that key (02 §2.9: replaced whole, never edited),
    and index it for text search before returning.

    Refused before anything is written: a key 02 §2.9 does not allow, a
    form 06 §6.7 does not take, text that is not UTF-8."""
    src = _source()
    try:
        src.check_content(key, data)
    except src.DepositError as e:
        raise DepositError(str(e)) from e

    collection = _collection(name)
    with closing(_connect(collection)) as con:
        if _row(con, key) is not None:
            _erase(collection, con, key)     # replaced whole: the old one
            purge(collection, con)           # leaves no trace either
        nid = next_native_id()
        d = Path(collection.corpus_dir) / str(nid)
        d.mkdir(parents=True, exist_ok=False)
        f = d / key
        _fsync_write(f, data)
        stamp = time.gmtime(now if now is not None else time.time())
        meta = {"key": key, "deposited_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", stamp),
                "url": url}
        # The sidecar LAST: `discover` lists a deposit only once it exists,
        # so a deposit half-written by a crash is never taken for one.
        _fsync_write(d / src.SIDECAR,
                     json.dumps(meta, sort_keys=True).encode("utf-8"))
        st = f.stat()
        uid = f"{PLUGIN}:{nid}"
        con.execute("BEGIN IMMEDIATE")
        try:
            con.execute(
                "INSERT INTO documents(uid,source,native_id,path,txt_name,lang,"
                "mtime,size,txt_hash,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (uid, PLUGIN, str(nid), str(f), key, "und", int(st.st_mtime),
                 st.st_size, src.origin_hash(data), int(time.time())))
            con.execute("COMMIT")
        except BaseException:
            con.execute("ROLLBACK")
            raise
        _index(collection, con, uid)
        row = con.execute("SELECT chunks FROM documents WHERE uid=?", (uid,)).fetchone()
    return {"collection": name, "key": key, "uid": uid, "native_id": nid,
            "chunks": int(row["chunks"] or 0)}


def add(name: str, key: str, data: bytes, *, url: str | None = None,
        now: float | None = None) -> dict[str, Any]:
    """`hold deposit add` — see `_add`; under the collection's write lock."""
    with _writing(name):
        return _add(name, key, data, url=url, now=now)


def erase(name: str, key: str) -> list[str]:
    """`hold deposit erase` — see `_erase_one`; under the collection's write lock."""
    with _writing(name):
        return _erase_one(name, key)


def reset(name: str) -> int:
    """Every deposit erased, one purge — see `_reset`; under the write lock."""
    with _writing(name):
        return _reset(name)


def _index(collection, con, uid: str) -> None:
    """The worker's rdf and fts stages, for this document, now. Each stage
    opens its own `BEGIN IMMEDIATE` once its long part is done and leaves
    the commit to its caller (app/worker.py) — this is that caller."""
    from app import worker
    for stage in (worker.stage_rdf, worker.stage_fts):
        row = con.execute("SELECT * FROM documents WHERE uid=?", (uid,)).fetchone()
        try:
            stage(con, row, collection)
            if con.in_transaction:
                con.execute("COMMIT")
        except BaseException:
            if con.in_transaction:
                con.execute("ROLLBACK")
            raise


def _erase_one(name: str, key: str) -> list[str]:
    """`hold deposit erase` — remove the deposit `key`, leaving no trace
    (02 §2.9); the remaining keys."""
    collection = _collection(name)
    with closing(_connect(collection)) as con:
        if _row(con, key) is None:
            raise DepositError(f"{name!r} holds no deposit {key!r}")
        _erase(collection, con, key)
        purge(collection, con)
        return _keys(con)


def _reset(name: str) -> int:
    """Every deposit erased, and ONE purge after them rather than one each
    — the reset of 07 §7.4. Returns how many were erased."""
    collection = _collection(name)
    with closing(_connect(collection)) as con:
        keys = _keys(con)
        for key in keys:
            _erase(collection, con, key)
        purge(collection, con)
        return len(keys)


def erase_all(root: Path, name: str, codex: Codex) -> int:
    """`reset` for `hold collection forget` (02 §2.9: every deposit erased
    leaving no trace, THEN the directory): the collection is no longer held
    by then, so its local side is built the one way every stage builds it
    (records.local_collection) rather than looked up by name. Under the
    collection's write lock, like `reset`. Returns how many were erased."""
    from .records import local_collection
    collection = local_collection(root, name, codex)
    with _writing(name):
        with closing(_connect(collection)) as con:
            keys = _keys(con)
            for key in keys:
                _erase(collection, con, key)
            purge(collection, con)
            return len(keys)


def _erase(collection, con, key: str) -> None:
    """The deposit out of search, the manifest and the origin. What makes
    that physical is `purge`, which every caller runs after it.

    The ORDER is what makes a crash harmless: the sidecar goes first, so
    no rescan can take the deposit back in; the derived copies next; the
    rows after them, so a re-run still finds the deposit by its key; the
    directory last."""
    from app import embeddings, fts
    from app.plugins.deposit import source as src

    from app import db
    row = _row(con, key)
    db.set_control(con, PURGE_PENDING, "1")
    d = Path(row["path"]).parent
    (d / src.SIDECAR).unlink(missing_ok=True)
    book_id = int(row["native_id"])
    fts.delete_book(row["lang"] or "und", book_id, collection)
    embeddings.delete_book(book_id, collection, lock_con=con)
    con.execute("BEGIN IMMEDIATE")
    try:
        for table, column in _keyed_tables(con):
            con.execute(f'DELETE FROM "{table}" WHERE "{column}"=?', (row["uid"],))
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise
    shutil.rmtree(d, ignore_errors=True)


def _keyed_tables(con) -> list[tuple[str, str]]:
    """Every manifest table that names a document, and the column it names
    it by — all of them, not a list someone must remember to extend: a
    table added later that keys on the uid is erased from too."""
    out = []
    for (table,) in con.execute("SELECT name FROM sqlite_master WHERE type='table' "
                                "AND name NOT LIKE 'sqlite_%'").fetchall():
        cols = {r[1] for r in con.execute(f'PRAGMA table_info("{table}")')}
        for column in ("uid", "doc_uid"):
            if column in cols:
                out.append((table, column))
    return out


def purge(collection, con) -> dict[str, Any]:
    """What makes an erase leave NO TRACE (02 §2.9), on each store that
    keeps deleted data by design:

    - the text index and the catalog index, rewritten from their live
      documents — tantivy keeps a deleted document's bytes in any segment
      that still holds a live one (`fts.rewrite_without`);
    - the vector table: deletions materialised, old versions dropped, a
      trained index trained again (`vector_index.purge_deleted`);
    - the manifest: freed pages zeroed (`secure_delete`, set on every
      connection here) and the WAL checkpointed and truncated, since the
      WAL holds the pages as they were before the delete.

    Every store is rewritten whole — O(the collection), which a deposit
    collection affords and a curated one could not; `reset` pays it once
    for all its deposits."""
    from app import catalog, fts, vector_index
    t0 = time.monotonic()
    kept = {g: fts.rewrite_without(g, collection) for g in fts.existing_groups(collection)}
    rows = con.execute("SELECT rowid, title, authors, subjects, lang FROM documents "
                       "WHERE rdf_done=1").fetchall()
    catalog.rewrite(collection, [tuple(r) for r in rows])
    vectors = vector_index.purge_deleted(collection, lock_con=con)
    con.execute("DELETE FROM control WHERE key=?", (PURGE_PENDING,))
    _checkpoint(con)
    return {"text_index": kept, "catalog": len(rows), "vectors": vectors,
            "seconds": round(time.monotonic() - t0, 3)}


def _checkpoint(con, attempts: int = 20) -> None:
    """`wal_checkpoint(TRUNCATE)` until it succeeds: a reader in the middle
    of a transaction makes it answer busy, and the WAL keeps the old pages
    until nobody reads them. Bounded: a node whose manifest is never idle
    raises rather than reporting an erase it has not finished."""
    for _ in range(attempts):
        busy, _log, _done = con.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if not busy:
            return
        time.sleep(0.25)
    raise DepositError("the manifest stayed busy; the erase is not complete "
                       "until its write-ahead log is truncated — run it again")


def _keys(con) -> list[str]:
    return [r[0] for r in con.execute(
        "SELECT txt_name FROM documents WHERE source=? AND txt_name IS NOT NULL "
        "ORDER BY CAST(native_id AS INTEGER)", (PLUGIN,))]


def keys(name: str) -> list[str]:
    """`hold deposit list` — the collection's keys, oldest deposit first."""
    collection = _collection(name)
    with closing(_connect(collection, write=False)) as con:
        return _keys(con)


def show(name: str, key: str) -> dict[str, Any]:
    """`hold deposit show` — the deposit's whole text and its chunk count,
    which is what LocalAGI's `GetEntryContent` reads (07 §7.4)."""
    from app import plugin_io
    collection = _collection(name)
    with closing(_connect(collection, write=False)) as con:
        row = _row(con, key)
        if row is None:
            raise DepositError(f"{name!r} holds no deposit {key!r}")
        src = _source()
        data = Path(row["path"]).read_bytes()
        if Path(key).suffix.lower() in src.TEXT_SUFFIXES:
            text = src.decode_text(key, data)     # the file, as deposited
        else:
            doc = plugin_io.canonical_doc(collection.plugin_instance(), row)
            text = "\n\n".join(b for _, b in doc.sections)
        return {"collection": name, "key": key, "uid": row["uid"],
                "content": text, "chunk_count": int(row["chunks"] or 0)}


def stage(name: str, data: bytes) -> str:
    """An upload's bytes, waiting for the worker in the collection's own
    `state/incoming/` — never a directory another collection or the node
    shares — and deleted by the job whatever its outcome (app/jobs.py)."""
    import uuid
    from app import collections as collections_mod
    d = collections_mod.louise_root() / "collections" / name / "state" / "incoming"
    d.mkdir(parents=True, exist_ok=True)
    final = d / uuid.uuid4().hex
    _fsync_write(final, data)
    return str(final)


def via_worker(op: str, name: str, *, timeout: float = 300.0, **args) -> dict:
    """A deposit write queued for the worker and waited for (app/jobs.py
    DEPOSIT) — how anything but the worker writes a deposit collection on
    a running node, because tantivy allows one writer per index and the
    worker keeps its own. The job's row is forgotten once read."""
    from app import jobs
    job_id = jobs.submit(jobs.DEPOSIT, {"op": op, "collection": name, **args})
    deadline = time.monotonic() + timeout
    while True:
        job = jobs.get(job_id)
        if job is not None and job.state in (jobs.DONE, jobs.FAILED):
            jobs.forget(job_id)
            if job.state == jobs.DONE:
                return job.result or {}
            raise DepositError((job.error or "").split(": ", 1)[-1])
        if time.monotonic() > deadline:
            raise DepositError("the worker has not done it yet; it stays queued — "
                               "is the node's worker running? (--here runs it in "
                               "this process, when no worker is)")
        time.sleep(0.05)


def finish_pending() -> list[str]:
    """Every deposit collection whose erase a crash cut short, purged now —
    what the worker's deposit thread does when it starts. A read never
    does it: it may run in the web process, which writes nothing."""
    from app import collections as collections_mod
    done = []
    for c in collections_mod.load_all():
        if c.codex is None or not is_deposit(c.codex):
            continue
        with closing(_connect(c)) as con:    # a WRITE opening: runs the purge
            done.append(c.name)
    return done


def is_named_deposit(root: Path, name: str) -> bool:
    from . import collection as col
    try:
        return is_deposit(col.read(root, name))
    except CodexError:
        return False
