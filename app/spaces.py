"""space_id computation and boot-time validation.

CLAUDE.md invariant 1: a vector table whose dim/space mismatches config is a
REFUSE-TO-BOOT error, never a warning or auto-migration. See
docs/architecture-v2.md §3.

The pinned space_id covers the embedding *recipe* (model hash, dtype,
normalization, pooling, chunk/cleaning versions, truncation policy, query-
encoder hash) and is compared on every boot. The vector table's physical
`dim` is tracked and compared separately (it's only observable once vectors
actually exist), matching invariant 1's own phrasing of "dim/space" as two
related but distinct checks.
"""
import hashlib
import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import httpx

from . import config, db


class BootValidationError(RuntimeError):
    """A collection's pinned embedding space is confirmed to mismatch current
    config or on-disk vectors. Never auto-migrate, never just warn."""


@dataclass(frozen=True)
class SpaceComponents:
    embed_model_hash: str
    weak: bool             # True = hash unconfirmed against real weights
    dtype: str
    normalization: str
    pooling: str
    chunk_version: int
    cleaning_version: int
    truncation_policy: str
    query_encoder_hash: str


def resolve_embed_model_hash() -> tuple[str, bool]:
    """(hash, weak). weak=True means the hash could not be confirmed against
    actual model weights (non-Ollama backend, or the endpoint was
    unreachable) — boot validation treats a weak hash as unconfirmable, never
    as a mismatch, so process startup never couples to embed-server liveness."""
    if config.EMBED_API == "ollama":
        try:
            with httpx.Client(timeout=10) as client:
                r = client.post(f"{config.EMBED_URL}/api/show",
                                 json={"model": config.EMBED_MODEL})
                r.raise_for_status()
                digest = r.json().get("digest")
                if digest:
                    return digest, False
        except Exception:
            pass
    weak_hash = hashlib.sha256(f"model-name:{config.EMBED_MODEL}".encode()).hexdigest()[:16]
    return weak_hash, True


def gather_components() -> SpaceComponents:
    model_hash, weak = resolve_embed_model_hash()
    return SpaceComponents(
        embed_model_hash=model_hash,
        weak=weak,
        dtype=config.EMBED_DTYPE,
        normalization="l2-client",       # embeddings.py always L2-normalizes client-side
        pooling="server-default",        # not independently observable over HTTP
        chunk_version=config.CHUNK_VERSION,
        cleaning_version=config.CLEANING_VERSION,
        truncation_policy=f"ctx={config.EMBED_CTX}",
        query_encoder_hash=model_hash,   # symmetric embedder for Gutenberg
    )


def _config_id(c: SpaceComponents) -> str:
    """Hash of the components that are always fully known from local config —
    never network-dependent, so a mismatch here is NEVER softened by a weak
    embed-model hash. This is what actually catches a CHUNK_VERSION/
    CLEANING_VERSION/dtype bump, independent of whether the embed endpoint
    happens to be reachable this boot."""
    payload = {
        "dtype": c.dtype,
        "normalization": c.normalization,
        "pooling": c.pooling,
        "chunk_version": c.chunk_version,
        "cleaning_version": c.cleaning_version,
        "truncation_policy": c.truncation_policy,
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.blake2b(blob, digest_size=16).hexdigest()


def compute_space_id(c: SpaceComponents) -> str:
    """Full space identity (display/pin value) — includes the embed model
    hash. Boot validation does NOT compare this as one blob; see
    _validate_one, which checks _config_id and the model hash separately so
    a weak model hash can never mask a real config-driven mismatch."""
    payload = {
        "embed_model_hash": c.embed_model_hash,
        "dtype": c.dtype,
        "normalization": c.normalization,
        "pooling": c.pooling,
        "chunk_version": c.chunk_version,
        "cleaning_version": c.cleaning_version,
        "truncation_policy": c.truncation_policy,
        "query_encoder_hash": c.query_encoder_hash,
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.blake2b(blob, digest_size=16).hexdigest()


def known_lance_dim(lance_dir: Path) -> int | None:
    """Vector width of the collection's Lance table, if one exists. Metadata-
    only (schema inspection) — no embedding calls, no GPU/CUDA import
    (invariant 7)."""
    if not lance_dir.is_dir():
        return None
    try:
        import lancedb
        conn = lancedb.connect(str(lance_dir))
        if "chunks" not in conn.table_names():
            return None
        field = conn.open_table("chunks").schema.field("vector")
        list_size = getattr(field.type, "list_size", None)
        return int(list_size) if list_size and list_size > 0 else None
    except Exception:
        return None


def validate_chunking_config() -> None:
    """Refuse a chunking configuration that cannot do useful work.

    chunk_book's hard-split loop advances by (max_chars - overlap_chars) per
    step. At CHUNK_OVERLAP >= CHUNK_TOKENS it advanced by nothing at all and
    looped forever, yielding chunks until memory ran out — a hang reachable
    from configuration alone. chunking.py now clamps to guarantee termination,
    but a near-equal overlap still degenerates into thousands of
    near-duplicate chunks per document, so it is refused here rather than
    allowed to quietly ruin an index.
    """
    if config.CHUNK_OVERLAP * 2 >= config.CHUNK_TOKENS:
        raise BootValidationError(
            f"CHUNK_OVERLAP ({config.CHUNK_OVERLAP}) must be less than half of "
            f"CHUNK_TOKENS ({config.CHUNK_TOKENS}). At or above that ratio each "
            f"chunk is mostly a copy of its predecessor, and the hard-split path "
            f"degenerates into near-duplicate chunks. The shipped default is "
            f"120/1000.")


def validate_boot(collections: list) -> None:
    """Raises BootValidationError on a confirmed space or dim mismatch for
    any collection. Call once at process startup, before serving traffic or
    processing documents."""
    validate_chunking_config()
    for collection in collections:
        con = db.connect(collection)
        try:
            _validate_one(con, collection)
        finally:
            con.close()


def report_boot_status(collections: list) -> list[dict]:
    """Non-raising per-collection boot-validation report for the System
    page's UI: same check validate_boot() runs at process startup, but
    caught and returned as pass/fail rows instead of exiting the process."""
    report = []
    for collection in collections:
        con = db.connect(collection)
        try:
            _validate_one(con, collection)
            report.append({"collection": collection.name, "ok": True,
                            "message": "space pinned & consistent"})
        except BootValidationError as e:
            report.append({"collection": collection.name, "ok": False, "message": str(e)})
        finally:
            con.close()
    return report


def _validate_one(con, collection) -> None:
    components = gather_components()
    computed_id = compute_space_id(components)
    computed_config_id = _config_id(components)
    observed_dim = known_lance_dim(collection.lance_dir)

    row = con.execute("SELECT space_id, dim, components FROM space WHERE id=1").fetchone()
    if row is None:
        con.execute(
            "INSERT INTO space(id,space_id,dim,components,pinned_at) VALUES(1,?,?,?,?)",
            (computed_id, observed_dim, json.dumps(asdict(components)), int(time.time())))
        db.log_event(con, "info", "spaces",
                     f"collection '{collection.name}': pinned space {computed_id}")
        return

    pinned_dim = row["dim"]
    pinned_components = SpaceComponents(**json.loads(row["components"])) if row["components"] else None

    if pinned_components is not None:
        # Fully local/deterministic components: NEVER softened by a weak embed
        # hash — a chunk/cleaning-version bump must refuse to boot regardless
        # of whether the embed endpoint happens to be reachable this run.
        if _config_id(pinned_components) != computed_config_id:
            raise BootValidationError(
                f"collection '{collection.name}': pinned embedding recipe "
                f"(dtype/normalization/pooling/chunk_version/cleaning_version/"
                f"truncation_policy) does not match current config. Embedding "
                f"spaces must never be mixed silently (CLAUDE.md invariant 1). "
                f"Either restore the previous config, or wipe {collection.lance_dir} "
                f"and re-embed to adopt the new space.")

        if pinned_components.embed_model_hash != components.embed_model_hash:
            if pinned_components.weak and not components.weak:
                # F1-D4, the boot-order trap: a node whose FIRST boot ran while
                # the embed endpoint was down pinned a weak, name-derived hash.
                # When the endpoint came up, the confirmed hash differed from
                # the pin and this branch refused to boot — permanently, with a
                # message telling the operator to wipe their vectors. But the
                # weak pin was never a confirmed identity: any vector embedded
                # since that boot came from whatever server answers EMBED_URL,
                # which is exactly the model now being confirmed. Upgrading the
                # unconfirmed pin to the confirmed hash protects the same
                # vectors the refusal claimed to protect — without bricking an
                # innocent startup ordering. A later CONFIRMED change still
                # refuses below, unchanged.
                con.execute(
                    "UPDATE space SET space_id=?, components=?, pinned_at=? "
                    "WHERE id=1",
                    (computed_id, json.dumps(asdict(components)), int(time.time())))
                db.log_event(con, "info", "spaces",
                             f"collection '{collection.name}': upgraded an "
                             f"unconfirmed (weak) space pin to the confirmed "
                             f"model hash {components.embed_model_hash}")
            elif not components.weak:
                raise BootValidationError(
                    f"collection '{collection.name}': pinned embed model hash "
                    f"{pinned_components.embed_model_hash} does not match current "
                    f"({components.embed_model_hash}). Embedding spaces must never "
                    f"be mixed silently (CLAUDE.md invariant 1). Either restore the "
                    f"previous EMBED_MODEL, or wipe {collection.lance_dir} and "
                    f"re-embed to adopt the new space.")
            else:
                db.log_event(con, "warn", "spaces",
                         f"collection '{collection.name}': embed model hash appears "
                         f"changed but could not be confirmed (weak hash — e.g. embed "
                         f"endpoint unreachable this boot) — proceeding without a "
                         f"hard refuse")

    if observed_dim is not None:
        if pinned_dim is not None and observed_dim != pinned_dim:
            raise BootValidationError(
                f"collection '{collection.name}': vector table dim {observed_dim} != "
                f"pinned dim {pinned_dim}. REFUSE TO BOOT (CLAUDE.md invariant 1).")
        if pinned_dim is None:
            con.execute("UPDATE space SET dim=? WHERE id=1", (observed_dim,))
