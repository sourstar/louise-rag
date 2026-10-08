"""Import a signed bundle — an alternative implementation of the embed stage.

GATE ORDER IS CHEAP-TO-EXPENSIVE AND IS LOAD-BEARING:

    signature -> header -> trust -> space -> content digest -> possession
    -> local chunk agreement -> admission checks -> write

Nothing attacker-controlled is acted on before the signature verifies, and the
costly gates (reading raw documents, calling the embed endpoint) only run once
the cheap ones have passed.

ALL-OR-NOTHING. Any failure imports NOTHING, records the reason in the ledger,
and returns normally — the worker is never crashed by a peer's bundle, and a
failure degrades to exactly today's behavior (the document stays un-embedded
and the local embed stage picks it up).

CRASH SAFETY WITHOUT ATOMICITY. Lance offers no cross-call transaction, so this
aims for idempotent-redo instead:

    1. ledger row 'applying', committed   <- intent durable before destruction
    2. embed_done = 0                     <- THE reordering that matters
    3. writer lock; per uid delete + add
    4. embed_done = 1, ledger 'applied'

Step 2 is what closes a hole that predates this module: if a crash lands
between Lance's delete and add, the document must not still look embedded.
With embed_done cleared first, any interruption leaves it un-embedded and
worker._next_book re-queues it.
"""
import hashlib
import time
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from .. import chunking, config, embeddings, plugin_io, spaces
from . import ledger, possession, trust
from .checks import BundleVerificationError, VerificationUnavailable, run_admission_checks
from .export import (HEADER_NAME, POSSESSION_NAME, SIGNATURE_NAME, VECTORS_NAME)
from .format import (BundleError, BundleFormatError, bundle_id_of, content_digest,
                     parse_header, parse_possession, seq_of, signed_payload,
                     sort_rows, uid_of, verify_signature)
from .writer_lock import vector_write_lock


class BundleSpaceMismatch(BundleError):
    """The bundle belongs to a different embedding space. Terminal: it is
    valid elsewhere, and only a config change (not a retry) could alter this."""


class SpaceUnconfirmedError(BundleError):
    """The local embedding space could not be CONFIRMED — the embed endpoint is
    unreachable, so resolve_embed_model_hash() fell back to hashing the model
    NAME. Two nodes running entirely different weights behind the same Ollama
    tag produce an identical weak hash, so accepting here would admit foreign
    vectors on the strength of a matching label.

    THE ONE RETRYABLE FAILURE. spaces._validate_one deliberately SOFTENS on a
    weak hash, which is right for boot (your own vectors, and startup must not
    couple to endpoint liveness) and catastrophic for import (someone else's
    vectors). Refusing is the safe direction, and T3 needs the model anyway.
    """


def _read_part(directory: Path, name: str, declared: int, limit_note: str) -> bytes:
    path = directory / name
    if not path.is_file():
        raise BundleFormatError(f"bundle is missing {name}")
    actual = path.stat().st_size
    if actual != declared:
        raise BundleFormatError(
            f"{name} is {actual} bytes but the signed header declares {declared}")
    return path.read_bytes()


def _collection_embed_fn(collection):
    """The default re-embed function for verification: the COLLECTION'S own
    recipe, not the node's env recipe — verifying MedCPT vectors with the
    Qwen3 endpoint would fail every honest bundle."""
    from ..collections import recipe_for
    recipe = recipe_for(collection)
    # stored chunk texts: `embed_chunks` feeds them in their layer's form,
    # and refuses a pair layer's, whose `fields` a text alone does not carry
    return lambda texts: embeddings.embed_chunks(collection, texts, recipe=recipe)


def _probe_fn(collection):
    """The liveness probe's embed: the collection's recipe, a fixed text,
    as text (`embeddings.embed_calibration`) — it asks whether the encoder
    answers, in any layer's form."""
    from ..collections import recipe_for
    recipe = recipe_for(collection)
    return lambda texts: embeddings.embed_calibration(recipe, texts)


def _local_space(con, collection=None) -> tuple[str, int | None, bool]:
    components = spaces.gather_components(collection)
    return spaces.compute_space_id(components), None, components.weak


def _check_space(con, collection, header, embed_fn=None) -> bool:
    """Compare the header's space_id against the LOCALLY computed one, and
    check dim separately.

    dim is checked on its own because compute_space_id's payload does NOT
    include it (§3 says it should; the implementation omits it), so a matching
    space_id is not evidence of a matching width.

    A WEAK model hash (the backend publishes no weight identity — every
    llama-server does not; or the endpoint was unreachable when resolved)
    is not, by itself, grounds to refuse: the local encoder is the proof
    that matters. So on a weak hash the encoder must be LIVE right now (a
    probe embed through the collection's own recipe), and the caller then
    REQUIRES the sampled re-embed to run and pass — foreign vectors are
    admitted on reproduction under the local function, never on a matching
    label. Unreachable endpoint -> SpaceUnconfirmedError, the one retryable
    refusal. (Found live on the first two-node exchange: both nodes pin the
    same space via the same weak hash by construction, and the old rule
    refused every bundle between them forever.)
    """
    local_space_id, _, weak = _local_space(con, collection)
    if weak:
        # a liveness probe is no chunk: one fixed text, as text
        probe = embed_fn if embed_fn is not None else _probe_fn(collection)
        try:
            probe(["louise-rag: embedding-space liveness probe"])
        except Exception as e:   # noqa: BLE001 — any failure means "not live"
            raise SpaceUnconfirmedError(
                "the local embedding space could not be confirmed: the backend "
                "publishes no weight identity (model hash is the model NAME) and "
                f"the embed endpoint did not answer a probe ({type(e).__name__}), "
                "so the vectors cannot be sample-verified against the local "
                "encoder. Refusing; retry when the endpoint is up.") from None
    if header.space_id != local_space_id:
        raise BundleSpaceMismatch(
            f"bundle is for space {header.space_id[:12]}… but this collection is "
            f"{local_space_id[:12]}… — cosine distance across embedding spaces is "
            f"meaningless (CLAUDE.md invariant 1)")

    pinned = con.execute("SELECT dim FROM space WHERE id=1").fetchone()
    pinned_dim = pinned["dim"] if pinned else None
    observed = spaces.known_lance_dim(collection.lance_dir)
    for label, value in (("pinned", pinned_dim), ("local vector table", observed)):
        if value is not None and int(value) != header.dim:
            raise BundleSpaceMismatch(
                f"bundle dim {header.dim} != {label} dim {value}")
    return weak


def _read_parquet(path: Path, header) -> tuple[list[str], list[np.ndarray]]:
    table = pq.read_table(path)
    if set(table.column_names) != {"chunk_id", "vector"}:
        raise BundleFormatError(
            f"vectors.parquet must contain exactly chunk_id and vector, "
            f"found {sorted(table.column_names)}")
    chunk_ids = table.column("chunk_id").to_pylist()
    if len(chunk_ids) != header.chunk_count:
        raise BundleFormatError(
            f"parquet holds {len(chunk_ids)} rows but the signed header "
            f"declares {header.chunk_count}")
    np_dtype = np.float16 if header.dtype == "f16" else np.float32
    vectors = [np.asarray(v, dtype=np_dtype) for v in table.column("vector").to_pylist()]
    for chunk_id in chunk_ids:
        if not isinstance(chunk_id, str):
            raise BundleFormatError("parquet chunk_id column contains a non-string")
    return chunk_ids, vectors


def _local_chunks(collection, row) -> dict[str, str]:
    """Re-derive this document's chunks locally: chunk_id -> text.

    Required because the bundle carries no text and Lance's schema has a
    non-null text column. It is also what makes possession structural — you
    cannot import vectors for a document whose bytes you do not hold.
    """
    from .. import layers as layers_mod
    plugin = collection.plugin_instance()
    chapters = plugin_io.sections(plugin, row)
    return {c["id"]: c["text"]
            for c in layers_mod.chunks_for_document(collection, row, chapters)}


def _document_rows(con, uids) -> dict[str, dict]:
    out = {}
    for uid in uids:
        row = con.execute(
            "SELECT uid, source, native_id, path, lang FROM documents WHERE uid=?",
            (uid,)).fetchone()
        if row is None:
            raise possession.PossessionError(
                f"{uid} is not in this node's manifest — a bundle can only be "
                f"imported for documents this node already holds")
        out[uid] = dict(row)
    return out


def inbox_dir(collection) -> Path:
    """Where bundles wait to be imported. Per collection, beside its manifest.

    P1.3's coordinator will deliver here; for now an operator drops a bundle
    directory in by hand.
    """
    return collection.manifest_path.parent / "bundles" / "inbox"


def _artifact_search_dirs() -> list[Path]:
    import os
    configured = os.getenv("SPACE_ARTIFACT_DIR")
    if configured:
        return [Path(configured)]
    return [Path(os.getenv("CONFIG_DIR", ".")) / "spaces"]


def _trusted_space_authors() -> list[str]:
    """platform.toml's [trust] space_authors — the pubkeys whose signed
    artifacts are fully trusted. Everyone else's artifacts load as ADVISORY,
    and t2/t3's tighten-only rule keeps them from loosening any tolerance.
    Read per call, matching platform_config.load()'s no-caching convention."""
    try:
        from .. import platform_config
        return list(platform_config.load().trust.space_authors)
    except Exception:
        return []


def find_space_artifact(space_id: str):
    """The one place bundle code resolves an artifact, so trust plumbing
    cannot be forgotten at a call site."""
    from ..verify import artifact as artifact_mod
    return artifact_mod.find(space_id, _artifact_search_dirs(),
                              trusted_authors=_trusted_space_authors())


def _run_ladder_if_possible(collection, header, chunk_ids, vectors, texts, embed_fn):
    """Run the calibrated ladder when this node has an artifact for the space.

    Returns None when no artifact is available — which is a DEGRADED state, not
    an approval: the caller keeps the coarse gates in that case, and the ledger
    records which tier was actually reached.

    Never raises: a missing or malformed artifact must not be able to stop an
    import that the coarse gates would otherwise accept, because an attacker
    who can plant a bad artifact could otherwise deny service.
    """
    try:
        from .. import verify as ladder
        space_artifact = find_space_artifact(header.space_id)
        if space_artifact is None:
            return None
        return ladder.run_ladder(
            chunk_ids, vectors, dim=header.dim, dtype=header.dtype,
            artifact=space_artifact, header=header, texts=texts,
            embed_fn=embed_fn if embed_fn is not None else _collection_embed_fn(collection),
            uid_of=uid_of, seq_of=seq_of,
            corpus_tag=space_artifact.raw.get("reference_corpus", {}).get("tag"),
            chunking_profile="book")
    except Exception:
        return None


def _merge_summaries(coarse, ladder_verdict):
    """Fold the ladder's tier summary into the coarse verdict's summary so the
    ledger records which tier was reached, not just that checks passed."""
    tier = ladder_verdict.tier_reached
    label = f"ladder[{ladder_verdict.summary()}]"
    if tier is not None:
        label += f" tier_reached={tier.name}"

    class _Merged:
        passed = coarse.passed
        results = coarse.results

        @staticmethod
        def summary():
            return f"{coarse.summary()}; {label}"

        @staticmethod
        def raise_if_failed():
            coarse.raise_if_failed()

    return _Merged


def _write_rows(collection, native_id: int, lance_rows: list[dict], header) -> None:
    """Delete-then-insert this document's vectors, creating the table if this
    node has none yet.

    The create path is NOT hypothetical: a consumer that has never embedded
    anything locally has no `chunks` table at all, and that is precisely the
    node most likely to be importing a bundle. Schema mirrors
    embeddings.upsert_book's so both writers produce identical tables; width
    and value type come from the header, which the space check has already
    reconciled against the pinned dim.
    """
    conn = embeddings._conn(collection)
    if embeddings.TABLE in conn.table_names():
        table = conn.open_table(embeddings.TABLE)
        table.delete(embeddings._book_filter(native_id))
        table.add(lance_rows)
        return
    value_type = pa.float16() if header.dtype == "f16" else pa.float32()
    schema = pa.schema([
        pa.field("id", pa.string()), pa.field("book_id", pa.int64()),
        pa.field("lang", pa.string()), pa.field("seq", pa.int64()),
        pa.field("chapter", pa.string()), pa.field("text", pa.string()),
        pa.field("vector", pa.list_(value_type, header.dim)),
    ])
    conn.create_table(embeddings.TABLE, data=lance_rows, schema=schema)


def import_bundle(con, collection, bundle_dir, *, embed_fn=None,
                  strict: bool = False, stop=None) -> dict:
    """Verify and apply a bundle. Never raises a BundleError to the caller.

    Returns a summary dict with `ok`, and on failure `error` plus the class
    name so the caller can distinguish terminal from retryable.
    """
    bundle_dir = Path(bundle_dir)
    header = None
    bundle_id = None
    producer = None
    # invariant 9, "not imported": the backstop for every caller. The doors
    # — the worker's inbox, `bundle import` — refuse first, in their own
    # words; this makes a door that forgets impossible (audit, 2026-09-13)
    from .. import licensing
    licensing.require_ack(collection, use="vectors are imported into it")
    try:
        return _import_checked(con, collection, bundle_dir, embed_fn=embed_fn,
                               strict=strict, stop=stop)
    except (SpaceUnconfirmedError, VerificationUnavailable) as e:
        # TRANSIENT. Recorded but NOT counted as a producer rejection, and the
        # caller must not burn a retry: the endpoint being down says nothing
        # about the bundle.
        return {"ok": False, "retryable": True, "error": str(e),
                "error_class": type(e).__name__}
    except BundleError as e:
        try:
            ledger.record_rejected(
                con, bundle_id=bundle_id, producer_pubkey=producer,
                reason=f"{type(e).__name__}: {e}",
                collection=collection.name)
        except Exception:
            pass
        return {"ok": False, "retryable": False, "error": str(e),
                "error_class": type(e).__name__}


def _import_checked(con, collection, bundle_dir: Path, *, embed_fn, strict,
                    stop) -> dict:
    # ── 1. signature, over the WIRE BYTES ──────────────────────────────
    header_path = bundle_dir / HEADER_NAME
    if not header_path.is_file():
        raise BundleFormatError(f"bundle is missing {HEADER_NAME}")
    header_bytes = header_path.read_bytes()
    sig = (bundle_dir / SIGNATURE_NAME).read_bytes() \
        if (bundle_dir / SIGNATURE_NAME).is_file() else b""

    header = parse_header(header_bytes)
    parquet_bytes = _read_part(bundle_dir, VECTORS_NAME, header.parquet_bytes, "parquet")
    possession_bytes = _read_part(bundle_dir, POSSESSION_NAME, header.possession_bytes,
                                  "possession")
    payload = signed_payload(header_bytes,
                             hashlib.sha256(parquet_bytes).digest(),
                             hashlib.sha256(possession_bytes).digest())
    verify_signature(payload, sig, header.producer_pubkey)
    bundle_id = bundle_id_of(payload)

    if ledger.already_applied(con, bundle_id):
        return {"ok": True, "bundle_id": bundle_id, "skipped": "already applied",
                "uids": 0, "chunks": 0}

    # ── 2. trust: the peer-wide list (app/bundles/trust.py) ────────────
    admission = trust.check_admission(header.producer_pubkey, strict=strict)

    # ── 3. space ───────────────────────────────────────────────────────
    weak = _check_space(con, collection, header, embed_fn=embed_fn)

    # ── 4. content ─────────────────────────────────────────────────────
    chunk_ids, vectors = _read_parquet(bundle_dir / VECTORS_NAME, header)
    rows = sort_rows([(cid, seq_of(cid), vec) for cid, vec in zip(chunk_ids, vectors)])
    if content_digest(rows, header.dim, header.dtype) != header.content_digest:
        raise BundleFormatError(
            "vectors do not match the content digest in the signed header")

    covered = sorted({uid_of(cid) for cid in chunk_ids})
    if covered != sorted(header.uids):
        raise BundleFormatError(
            "the uids in vectors.parquet do not match the signed header")

    # ── 5. possession ──────────────────────────────────────────────────
    claimed = parse_possession(possession_bytes)
    documents = _document_rows(con, covered)
    possession.verify_map(con, collection, claimed, covered)

    # ── 6. local chunk agreement ───────────────────────────────────────
    # space_id pins CHUNK_VERSION but NOT CHUNK_TOKENS/CHUNK_OVERLAP, so two
    # nodes at the same CHUNK_VERSION can chunk differently and a fully HONEST
    # producer's vectors would attach to different text. Set equality catches
    # that, and cleaning drift with it, for free.
    texts: dict[str, str] = {}
    by_uid: dict[str, list[str]] = {}
    for uid in covered:
        local = _local_chunks(collection, documents[uid])
        bundle_side = {cid for cid in chunk_ids if uid_of(cid) == uid}
        if bundle_side != set(local):
            raise BundleFormatError(
                f"{uid}: the bundle's chunk ids do not match this node's "
                f"({len(bundle_side)} vs {len(local)}) — the producer chunked the "
                f"text differently (CHUNK_TOKENS/CHUNK_OVERLAP differ, or the "
                f"cleaning version does)")
        texts.update(local)
        by_uid[uid] = sorted(bundle_side, key=seq_of)

    # ── 7. verification ladder, then the coarse admission gates ────────
    # The calibrated ladder (P1.2, app/verify/) runs when a space artifact is
    # available. The coarse gates ALWAYS run as well: they need no artifact, so
    # they are the floor a node keeps even with no calibration published, and
    # an artifact can only ever tighten the outcome, never loosen it.
    chunk_id_list = [r[0] for r in rows]
    vector_list = [r[2] for r in rows]
    ladder_verdict = _run_ladder_if_possible(
        collection, header, chunk_id_list, vector_list, texts, embed_fn)
    if ladder_verdict is not None and ladder_verdict.failed:
        raise BundleVerificationError(
            f"verification ladder failed — "
            + "; ".join(f"{r.tier.name}: {r.detail}" for r in ladder_verdict.failures))

    verdict = run_admission_checks(
        chunk_id_list, vector_list, header.dim, header.dtype,
        uid_of=uid_of, seq_of=seq_of, texts=texts,
        embed_fn=embed_fn if embed_fn is not None else _collection_embed_fn(collection))
    verdict.raise_if_failed()
    if weak and not any(r.name == "sample_reembed" and r.passed
                        for r in verdict.results):
        # a weak model hash was admitted past _check_space only on the
        # promise that the local encoder would PROVE reproduction; if the
        # sample check could not run (no local texts to re-embed), that
        # proof is missing and the label alone is not enough
        raise SpaceUnconfirmedError(
            "the model hash is weak (backend publishes no weight identity) and "
            "the sampled re-embed could not run for this bundle — no locally held "
            "text to verify against; retry once the documents are indexed here.")
    if ladder_verdict is not None:
        verdict = _merge_summaries(verdict, ladder_verdict)

    # ── 8. apply ───────────────────────────────────────────────────────
    ledger.record_applying(
        con, bundle_id=bundle_id, producer_pubkey=header.producer_pubkey,
        space_id=header.space_id, collection=collection.name,
        chunk_count=len(rows), signature_hex=sig.hex(),
        parquet_sha256=hashlib.sha256(parquet_bytes).hexdigest(),
        content_digest=header.content_digest, created_at=header.created_at,
        space_weak=weak)

    vector_by_id = {cid: vec for cid, _, vec in rows}
    written: dict[str, int] = {}
    for uid in covered:
        if stop is not None and stop.is_set():
            break
        document = documents[uid]
        native_id = int(document["native_id"])
        ids = by_uid[uid]

        # Clear the flag BEFORE touching Lance: a crash from here on leaves the
        # document looking un-embedded, so _next_book re-queues it rather than
        # leaving it flagged done with zero vectors.
        con.execute("UPDATE documents SET embed_done=0, updated_at=? WHERE uid=?",
                    (int(time.time()), uid))

        lance_rows = [{
            "id": cid, "book_id": native_id, "lang": document["lang"] or "und",
            "seq": seq_of(cid), "chapter": "", "text": texts[cid],
            "vector": np.asarray(vector_by_id[cid],
                                  dtype=np.float16 if header.dtype == "f16"
                                  else np.float32),
        } for cid in ids]

        with vector_write_lock(con):
            _write_rows(collection, native_id, lance_rows, header)

        con.execute(
            "UPDATE documents SET embed_done=1, embed_version=?, updated_at=? "
            "WHERE uid=?", (config.EMBED_VERSION, int(time.time()), uid))
        # F1-D5 (minor): the import path satisfies demand, so it must also
        # CLEAR it. The worker's own embed stage removes the queue row, but
        # this alternative implementation of that stage didn't — so an
        # imported document's lazy-queue entry lingered and kept being
        # re-published as demand the network had already met.
        con.execute("DELETE FROM embed_queue WHERE uid=?", (uid,))
        ledger.mark_imported(con, ids, uid, header.producer_pubkey, bundle_id)
        written[uid] = len(ids)

    ladder_tier = (int(ladder_verdict.tier_reached)
                   if ladder_verdict is not None and ladder_verdict.tier_reached is not None
                   else None)
    ledger.record_applied(con, bundle_id, written, verdict.summary(),
                           tier_reached=ladder_tier)
    if ladder_verdict is not None:
        # The FULL evidence, untruncated, into its own blob — the summary
        # column alone cannot prove anything to a third party.
        ledger.record_evidence(con, bundle_id, ladder_verdict.evidence())
    return {"ok": True, "bundle_id": bundle_id, "producer": header.producer_pubkey,
            "admission": admission, "uids": len(written),
            "chunks": sum(written.values()), "verdict": verdict.summary()}
