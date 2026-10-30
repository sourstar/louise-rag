"""Reference source registry + shared entity resolver + refs.db access."""
import threading

from .. import config
from .clinvar_mini import ClinvarMiniSource
from .gnomad import GnomadConstraintSource
from .hgnc import HgncSource
from .homologene import HomologeneSource
from .normattiva import CitationResolver, NormattivaRefSource
from .resolver import CompositeResolver, DictResolver
from .store import RefStore, connect, ensure_schema
from .wikidata import WikidataRefSource

REFS_REGISTRY = {
    "hgnc": HgncSource(),
    "clinvar-mini": ClinvarMiniSource(),
    "gnomad-constraint": GnomadConstraintSource(),
    "homologene": HomologeneSource(),
    # the Italian acts: `urn` and `eli` registered (05 §5.2), citations
    # resolved by key (app/refs/normattiva.py; review §3.12)
    "normattiva-refs": NormattivaRefSource(),
    # Wikidata's entity ids: the namespace `wikidata` (QIDs, PIDs) for the
    # wikidata collection's keys and gutenberg-graph's mentions
    # (docs/notes/wikidata-build-plan.md §2)
    "wikidata-refs": WikidataRefSource(),
}

#: every registered resolver in turn; `expand` follows the declared links
#: whichever source owns the entity (app/refs/resolver.py)
RESOLVER = CompositeResolver((DictResolver(), CitationResolver()))


#: thread -> (the refs.db path it was opened on, the store). SQLite
#: connections are thread-affine, so this cannot be a plain module cache; and
#: the path is the key, never a name, because a test's refs.db comes and goes
#: with its DATA_DIR while the thread stays (CLAUDE.md: physical identity).
_local = threading.local()


def get_store() -> RefStore:
    """The global refs.db, with every registered source's schema ensured — so
    resolve()/expand() against an empty (never-synced) store just return [],
    which is exactly graceful degeneration.

    KEPT PER THREAD. This used to open a connection and run four
    `ensure_schema` passes on EVERY fts batch, inside the manifest's
    `BEGIN IMMEDIATE` — so every other writer of that manifest queued behind
    a connection being set up for a database that is read-mostly and usually
    empty. Measured on the node, 2026-09-22: 4.5 s of fixed cost per fts
    batch, of which this is a part (app/sizing.py fits the rest)."""
    path = config.refs_db_path()
    got = getattr(_local, "store", None)
    if got is not None and got[0] == str(path):
        return got[1]
    con = connect(path)
    schemas = []
    for source in REFS_REGISTRY.values():
        schema = source.schema()
        ensure_schema(con, schema)
        schemas.append(schema)
    store = RefStore(con, schemas)
    _local.store = (str(path), store)
    return store


_read_local = threading.local()


def read_store() -> RefStore:
    """refs.db READ-ONLY, for the parse pool's children: tagging a document
    only reads it, and eleven children each running `ensure_schema` would
    queue writes on a read-mostly database. The worker ensures the schema
    (`get_store`) before it sends a batch. Kept per thread, keyed on the
    path, as `get_store` is; a refs.db not there yet is `get_store`'s to make."""
    import sqlite3
    path = config.refs_db_path()
    got = getattr(_read_local, "store", None)
    if got is not None and got[0] == str(path):
        return got[1]
    if not path.is_file():
        return get_store()
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30, isolation_level=None,
                          check_same_thread=True)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=15000")
    store = RefStore(con, [source.schema() for source in REFS_REGISTRY.values()])
    _read_local.store = (str(path), store)
    return store
