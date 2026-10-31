"""space_id computation and boot-time validation.

CLAUDE.md invariant 1: a vector table whose dim/space mismatches config is a
REFUSE-TO-BOOT error, never a warning or auto-migration. See
docs/notes/architecture-v2.md §3.

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
    # WHICH chunker produced the chunks, not just at which version.
    #
    # chunk_version alone is not enough: two different chunkers can sit at
    # the same version number (content-defined chunking launched at 1, and
    # so is the fixed-width family), and then two incompatible segmentations
    # of the same text would share a space id — vectors cut one way silently
    # mixed with vectors cut the other, which is exactly what invariant 1
    # exists to prevent.
    #
    # GRANDFATHERED below so that adding this field re-pins nothing: the
    # chunkers that existed when the space format was frozen are omitted
    # from the payload, leaving every space id pinned before this change
    # byte-identical.
    chunker: str = "book"
    # Chunker LAYERS (amendment 41): the ordered (chunker, version) pairs
    # when a collection declares MORE than one layer; None for the one
    # implicit layer, so every pre-layers pin keeps its exact id. Stored
    # as a list in the pinned components JSON.
    layers: list | None = None


def resolve_embed_model_hash(recipe=None) -> tuple[str, bool]:
    """(hash, weak). weak=True means the hash could not be confirmed against
    actual model weights (non-Ollama backend, or the endpoint was
    unreachable) — boot validation treats a weak hash as unconfirmable, never
    as a mismatch, so process startup never couples to embed-server liveness.

    `recipe` is the collection's own encoder (collections.recipe_for);
    there is no default to read when none is given."""
    from . import collections as collections_mod
    if recipe is None:
        raise collections_mod.NoEncoder(
            "resolve_embed_model_hash: no encoder recipe — a collection names "
            "its own, and there is no default")
    if recipe.api == "ollama":
        try:
            with httpx.Client(timeout=10) as client:
                r = client.post(f"{recipe.url}/api/show",
                                 json={"model": recipe.model})
                r.raise_for_status()
                digest = r.json().get("digest")
                if digest:
                    return digest, False
        except Exception:
            pass
    elif recipe.api == "openai":
        # TEI (the MedCPT/BGE-M3 containers) publishes its LOADED model's
        # identity on GET /info: model_id plus model_sha, the HF revision of
        # the weights actually serving. Hash what is SERVED, not what the
        # recipe asked for — a repointed container changes the hash, which
        # is the whole point. llama-server also speaks /v1/embeddings but
        # has no /info route: the 404 falls through to the weak hash,
        # exactly as before this branch existed.
        try:
            with httpx.Client(timeout=10) as client:
                r = client.get(f"{recipe.url}/info")
                r.raise_for_status()
                info = r.json()
                model_id, model_sha = info.get("model_id"), info.get("model_sha")
                if model_id and model_sha:
                    digest = hashlib.sha256(
                        f"tei:{model_id}@{model_sha}".encode()).hexdigest()[:16]
                    return digest, False
        except Exception:
            pass
    weak_hash = hashlib.sha256(f"model-name:{recipe.model}".encode()).hexdigest()[:16]
    return weak_hash, True


def gather_components(collection=None) -> SpaceComponents:
    from . import collections as collections_mod
    recipe = collections_mod.recipe_for(collection)      # NoEncoder when it names none
    model_hash, weak = resolve_embed_model_hash(recipe)
    if recipe.query_url:
        # bi-encoder pair (MedCPT): the query side is a DIFFERENT model, so
        # its identity must move the space_id (§3's query_encoder_hash)
        query_hash = hashlib.blake2b(
            f"query-encoder:{recipe.query_url}:{recipe.model}".encode(),
            digest_size=8).hexdigest()
    else:
        query_hash = model_hash                  # symmetric embedder
    return SpaceComponents(
        embed_model_hash=model_hash,
        weak=weak,
        dtype=recipe.dtype,
        normalization="l2-client",       # embeddings.py always L2-normalizes client-side
        pooling="server-default",        # not independently observable over HTTP
        # Per CHUNKER, not one global number. It used to be
        # config.CHUNK_VERSION for everyone, which meant changing how TEXT is
        # chunked would re-pin the space of collections whose chunker never
        # changed — including the pre-embedded ones, whose vectors were
        # imported against a producer's boundaries and cannot be recomputed
        # by re-chunking at all. Fixed-width chunkers still report exactly
        # config.CHUNK_VERSION, so every existing space_id is unchanged.
        chunk_version=_chunk_version_for(collection),
        chunker=_chunker_for(collection),
        cleaning_version=config.CLEANING_VERSION,
        truncation_policy=f"ctx={recipe.ctx}",
        query_encoder_hash=query_hash,
        layers=_layers_for(collection),
    )


def _layers_for(collection) -> list | None:
    """The ordered layer chunkers when more than one is declared."""
    from . import layers as layers_mod
    try:
        payload = layers_mod.space_payload(collection)
    except Exception:   # noqa: BLE001 — a space id must always be computable
        return None
    return payload.get("layers")



def _chunker_for(collection) -> str:
    """Which chunker this collection uses, for the space identity."""
    from . import chunking
    try:
        return chunking.chunker_version_for_collection(collection)[0]
    except Exception:   # noqa: BLE001 — a space id must always be computable
        return "book"


def _chunk_version_for(collection) -> int:
    """The chunk version that belongs in this collection's space id.

    Tolerant of a collection with no plugin instance (tests build ad-hoc
    Collections): the global is the right answer for every fixed-width
    chunker, which is all of them bar a mutable source.
    """
    from . import chunking
    try:
        return chunking.chunker_version_for_collection(collection)[1]
    except Exception:   # noqa: BLE001 — a space id must always be computable
        return config.CHUNK_VERSION


# Chunkers that predate this field. Omitted from the space payload so that
# every space pinned before the chunker became part of the identity keeps
# exactly the id it already has (same reasoning as amendment 2, where
# space_id deliberately does not hash dim).
#
# "abstract" is NOT grandfathered: no space was ever pinned under it (the
# profile fell through to `book` until 2026-08-26), and wiring it is a
# real change of segmentation for PubMed — the payload must carry it so
# PubMed's space id changes and the boot check refuses until the operator
# re-pins (`space repin`), exactly as invariant 3 demands.
GRANDFATHERED_CHUNKERS = frozenset({"book", "verbatim"})


def _chunker_payload(c: SpaceComponents) -> dict:
    if c.layers:
        # several layers: the ordered chunkers ARE the segmentation identity
        # (amendment 41); a one-layer collection keeps the pre-layers payload
        return {"layers": [list(l) for l in c.layers]}
    return {} if c.chunker in GRANDFATHERED_CHUNKERS else {"chunker": c.chunker}


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
        **_chunker_payload(c),
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
        **_chunker_payload(c),
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


def known_lance_rows(lance_dir: Path) -> int | None:
    """Rows in the collection's Lance table: 0 when there is none, None when
    one exists and cannot be read (not proven empty). Metadata-only, as
    `known_lance_dim`."""
    if not lance_dir.is_dir():
        return 0
    try:
        import lancedb
        conn = lancedb.connect(str(lance_dir))
        if "chunks" not in conn.table_names():
            return 0
        return int(conn.open_table("chunks").count_rows())
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
    processing documents.

    Raising is right for ONE collection, which is how every caller that
    wants an answer about a specific collection uses it. A NODE holding
    several wants `refuse_boot` instead — see there.
    """
    validate_chunking_config()
    for collection in collections:
        con = db.connect(collection)
        try:
            _validate_one(con, collection)
        finally:
            con.close()


def refuse_boot(collections: list) -> list[tuple]:
    """The node's boot check: validate each collection, refuse the ones
    that fail, and keep the rest.

    Invariant 1 states this in so many words — "a vector table whose
    dim/space mismatches the Codex is a REFUSE-TO-START error FOR THAT
    COLLECTION ... the node keeps running its others, that one is not
    served, shared or searched" — and `validate_boot` did the opposite,
    raising on the first bad one and taking every other collection down
    with it. A chunking-config error still raises: that one is node-wide
    by construction and no per-collection answer exists.

    Returns [(collection, reason)] so the caller can drop them from the
    served set and NAME the reason, which the invariant also requires.
    """
    from . import collections as collections_mod
    validate_chunking_config()
    refused: list[tuple] = []
    for collection in collections:
        con = db.connect(collection)
        try:
            _validate_one(con, collection)
        except (BootValidationError, collections_mod.NoEncoder) as e:
            # a collection naming no encoder is refused like a space
            # mismatch — that one, never the node (it used to kill the boot)
            refused.append((collection, str(e)))
        finally:
            con.close()
    return refused


def report_boot_status(collections: list) -> list[dict]:
    """Non-raising per-collection boot-validation report for the System
    page's UI: same check validate_boot() runs at process startup, but
    caught and returned as pass/fail rows instead of exiting the process.

    READ-ONLY: pin_if_missing=False. A status page must never mutate space
    state — observed live: an operator reset a pin (legitimately, zero
    vectors) to switch a collection's recipe, then opened the UI, and this
    report re-pinned the half-edited recipe behind their back. The next
    boot refused. Unpinned is a reportable state, not a repair opportunity."""
    report = []
    for collection in collections:
        con = None
        try:
            con = db.connect(collection)
            pinned = _validate_one(con, collection, pin_if_missing=False)
            report.append({"collection": collection.name, "ok": True,
                            "message": "space pinned & consistent" if pinned
                            else "space unpinned — pins at next worker boot"})
        except BootValidationError as e:
            report.append({"collection": collection.name, "ok": False, "message": str(e)})
        except ValueError as e:
            # NoEncoder among them: a verdict (non-compliant), not a failure
            # to check — so it is reported as one
            if type(e).__name__ != "NoEncoder":
                raise
            report.append({"collection": collection.name, "ok": False, "message": str(e)})
        except Exception as e:   # noqa: BLE001 — one collection, never the report
            # PER COLLECTION. `db.connect` sat outside the try, so one
            # manifest that could not be opened made the whole report raise,
            # and a real refusal beside it vanished from `louise node health`
            # (verification pass, 2026-09-13)
            # `checked: False` — not a verdict of the boot check, and a
            # reader must not take it for one (`louise node health` reports an
            # unopenable manifest through its own connect, as "cannot open")
            report.append({"collection": collection.name, "ok": False, "checked": False,
                            "message": f"cannot be checked: {type(e).__name__}: {e}"})
        finally:
            if con is not None:
                con.close()
    return report


def _validate_codex(con, collection, *, pin_if_missing: bool = True) -> bool:
    """Invariant 1, for a collection run from its Codex (07 §7.8).

    The pin is `codex.space_id` — 09 §9.3's SHA-256 over the encoder, the
    layers, the cleaning version and the space block — and NOT
    `compute_space_id`'s BLAKE2b preimage. The two are different
    functions naming different things (step6-preflight §4 settled that
    they are not meant to agree), so a Codex collection validated against
    the legacy one would refuse to boot for a difference that means
    nothing.

    "A vector table whose dim/space mismatches the Codex is a
    REFUSE-TO-START error FOR THAT COLLECTION, never a warning or
    auto-migration" — so this raises, the node keeps its others running,
    and `hold collection show` names the reason.

    There is NO weak-hash softening here. The legacy path tolerates an
    unconfirmable embed-model hash because it probes a live endpoint; a
    Codex names its encoder BY DIGEST, so there is nothing to be unsure
    about and nothing to soften.
    """
    codex = collection.codex
    want, want_dim = codex.space_id, codex.space.dim
    observed_dim = known_lance_dim(collection.lance_dir)

    row = con.execute("SELECT space_id, dim FROM space WHERE id=1").fetchone()
    if row is None:
        if not pin_if_missing:
            return False
        con.execute(
            "INSERT INTO space(id,space_id,dim,components,pinned_at) "
            "VALUES(1,?,?,?,?)",
            (want, observed_dim or want_dim, None, int(time.time())))
        db.log_event(con, "info", "spaces",
                     f"collection '{collection.name}': pinned Codex space "
                     f"{want[:23]}… (dim {want_dim})")
        return True

    if row["space_id"] != want:
        raise BootValidationError(
            f"collection '{collection.name}': the vector table is pinned to "
            f"space {str(row['space_id'])[:23]}… and its Codex says "
            f"{want[:23]}…. No vector crosses a space boundary (02 §2.4, "
            f"invariant 1); this collection is not served, shared or "
            f"searched until the table is emptied or the name holds the "
            f"Codex it was built for")
    if observed_dim is not None and observed_dim != want_dim:
        raise BootValidationError(
            f"collection '{collection.name}': the vector table is "
            f"{observed_dim}-dimensional and its Codex's space is "
            f"{want_dim}. A width mismatch is a different encoder, not a "
            f"migration")
    return True


def _validate_one(con, collection, *, pin_if_missing: bool = True) -> bool:
    if getattr(collection, "codex", None) is not None:
        return _validate_codex(con, collection, pin_if_missing=pin_if_missing)
    components = gather_components(collection)
    computed_id = compute_space_id(components)
    computed_config_id = _config_id(components)
    observed_dim = known_lance_dim(collection.lance_dir)

    row = con.execute("SELECT space_id, dim, components FROM space WHERE id=1").fetchone()
    if row is None:
        if not pin_if_missing:
            return False
        con.execute(
            "INSERT INTO space(id,space_id,dim,components,pinned_at) VALUES(1,?,?,?,?)",
            (computed_id, observed_dim, json.dumps(asdict(components)), int(time.time())))
        db.log_event(con, "info", "spaces",
                     f"collection '{collection.name}': pinned space {computed_id}")
        return True

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
            if pinned_components.weak and not components.weak and not pin_if_missing:
                pass    # legitimate upgrade, but the read-only report never
                        # writes — the worker performs it at its next boot
            elif pinned_components.weak and not components.weak:
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
            elif pin_if_missing:
                # the boot says it; the read-only report never writes, not
                # even an event
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
        # the pin's width is filled in AT BOOT only. report_boot_status
        # promises READ-ONLY and still wrote it — from every System page
        # view and, once `louise node health` used it, from every check
        # (verification pass, 2026-09-13: dim NULL -> 768 after one check)
        if pinned_dim is None and pin_if_missing:
            con.execute("UPDATE space SET dim=? WHERE id=1", (observed_dim,))
    return True
