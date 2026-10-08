"""Reference source registry + shared entity resolver + refs.db access."""
import threading

from .. import config
from .clinvar_mini import ClinvarMiniSource
from .gnomad import GnomadConstraintSource
from .hgnc import HgncSource
from .homologene import HomologeneSource
from .resolver import DictResolver
from .store import RefStore, connect, ensure_schema

REFS_REGISTRY = {
    "hgnc": HgncSource(),
    "clinvar-mini": ClinvarMiniSource(),
    "gnomad-constraint": GnomadConstraintSource(),
    "homologene": HomologeneSource(),
}

RESOLVER = DictResolver()


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
