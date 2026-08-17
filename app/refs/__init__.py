"""Reference source registry + shared entity resolver + refs.db access."""
from .clinvar_mini import ClinvarMiniSource
from .gnomad import GnomadConstraintSource
from .hgnc import HgncSource
from .resolver import DictResolver
from .store import RefStore, connect, ensure_schema

REFS_REGISTRY = {
    "hgnc": HgncSource(),
    "clinvar-mini": ClinvarMiniSource(),
    "gnomad-constraint": GnomadConstraintSource(),
}

RESOLVER = DictResolver()


def get_store() -> RefStore:
    """Open the global refs.db and ensure every registered source's schema
    exists — so resolve()/expand() against an empty (never-synced) store
    just return [], which is exactly graceful degeneration."""
    con = connect()
    schemas = []
    for source in REFS_REGISTRY.values():
        schema = source.schema()
        ensure_schema(con, schema)
        schemas.append(schema)
    return RefStore(con, schemas)
