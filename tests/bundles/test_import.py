"""Import: the acceptance criteria.

Round-trip; a one-byte tamper in each of header / parquet / sig / possession
rejected with a DISTINCT error class; all-or-nothing; and the crash/race
behavior around Lance's non-atomic delete-then-add.
"""
import json
import threading

import numpy as np
import pytest

from app import db, embeddings
from app.bundles import checks, export as exp, importer, ledger, trust
from app.bundles.format import BundleFormatError, BundleSignatureError
from tests.bundles.helpers import DIM, seed_document, unit_vectors


@pytest.fixture(autouse=True)
def no_collection_files(tmp_path, monkeypatch):
    """The hand-built `collection` is the only one: CONFIG_DIR names an
    empty folder, so nothing outside the test (the checkout's own
    collections/, a pre-Codex file now refused) becomes the default."""
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path / "no-config"))


UID = "gutenberg:1"


@pytest.fixture
def bundle(con, collection, producer_key, tmp_path):
    """A valid bundle plus the local state needed to import it."""
    rows = seed_document(con, collection, UID, 1, n_chunks=12, seed=1)
    con.execute("UPDATE documents SET embed_done=0 WHERE uid=?", (UID,))
    result = exp.export_bundle(con, collection, [UID], tmp_path / "bundle",
                               signing_key=producer_key)
    return tmp_path / "bundle", result, rows


def _local_embedder(rows):
    """Stand-in for this node's embed endpoint: reproduces the bundle's own
    vectors, i.e. the same model."""
    by_text = {r["text"]: np.asarray(r["vector"], dtype=np.float32) for r in rows}
    dim = len(next(iter(by_text.values()))) if by_text else 4
    # a real encoder answers ANY text (the importer's liveness probe among
    # them); unknown texts get a fixed unit vector so only the bundle's own
    # chunks reproduce exactly
    probe = np.zeros(dim, dtype=np.float32); probe[0] = 1.0
    return lambda batch: [by_text.get(t, probe) for t in batch]


def _import(con, collection, path, rows, **kw):
    kw.setdefault("embed_fn", _local_embedder(rows))
    return importer.import_bundle(con, collection, path, **kw)


def _tamper(path, offset=-40):
    data = bytearray(path.read_bytes())
    data[offset] ^= 0x01
    path.write_bytes(bytes(data))


# ─────────────────────────── round trip ───────────────────────────

def test_valid_bundle_imports(bundle, con, collection):
    path, exported, rows = bundle
    result = _import(con, collection, path, rows)
    assert result["ok"], result.get("error")
    assert result["chunks"] == len(rows)
    assert result["bundle_id"] == exported["bundle_id"]

    doc = con.execute("SELECT embed_done FROM documents WHERE uid=?", (UID,)).fetchone()
    assert doc["embed_done"] == 1
    assert ledger.already_applied(con, exported["bundle_id"])


def test_imported_vectors_land_in_lance_exactly_once(bundle, con, collection):
    path, _, rows = bundle
    _import(con, collection, path, rows)
    table = embeddings._conn(collection).open_table(embeddings.TABLE)
    stored = table.search().where("book_id IN (1)").limit(1000).to_arrow()
    ids = stored.column("id").to_pylist()
    assert len(ids) == len(rows)
    assert len(set(ids)) == len(rows)


def test_import_restores_text_locally(bundle, con, collection):
    """The bundle carries no text; the importer rebuilds it from the local raw
    document, which is what makes possession structural."""
    path, _, rows = bundle
    _import(con, collection, path, rows)
    table = embeddings._conn(collection).open_table(embeddings.TABLE)
    stored = table.search().where("book_id IN (1)").limit(1000).to_arrow()
    assert all(t for t in stored.column("text").to_pylist())


def test_import_onto_a_node_with_no_vector_table(con, collection, producer_key,
                                                  tmp_path, monkeypatch):
    """A consumer that has never embedded anything has no Lance table at all —
    and is exactly the node most likely to be importing. Found by the
    end-to-end smoke test; every other test here seeds a document first, which
    creates the table and hides this.
    """
    from app import db as db_mod
    # producer side, in its own collection
    from tests.bundles.helpers import make_collection
    producer_col = make_collection(tmp_path / "prod", name="gutenberg")
    pcon = db_mod.connect(producer_col)
    from app import spaces
    pcon.execute("INSERT OR REPLACE INTO space(id,space_id,dim,components,pinned_at) "
                 "VALUES(1,?,?,'{}',0)",
                 (spaces.compute_space_id(spaces.gather_components()), DIM))
    rows = seed_document(pcon, producer_col, UID, 1, n_chunks=4, seed=1)
    exp.export_bundle(pcon, producer_col, [UID], tmp_path / "b",
                      signing_key=producer_key)

    # consumer side: same raw document, NO vectors, NO table
    import shutil
    shutil.copytree(producer_col.corpus_dir / "1", collection.corpus_dir / "1")
    con.execute(
        "INSERT INTO documents(uid,source,native_id,path,lang,rdf_done,fts_done,"
        "embed_done,updated_at) VALUES(?,?,?,?,?,1,1,0,0)",
        (UID, "gutenberg", "1", str(collection.corpus_dir / "1"), "en"))
    assert embeddings.TABLE not in embeddings._conn(collection).table_names()

    result = _import(con, collection, tmp_path / "b", rows)
    assert result["ok"], result.get("error")
    assert result["chunks"] == len(rows)
    assert embeddings.stats(collection)["vectors"] == len(rows)


def test_reimport_is_a_no_op(bundle, con, collection):
    path, _, rows = bundle
    _import(con, collection, path, rows)
    second = _import(con, collection, path, rows)
    assert second["ok"] and second.get("skipped")
    assert con.execute("SELECT COUNT(*) c FROM bundles").fetchone()["c"] == 1


def test_provenance_is_recorded_per_chunk(bundle, con, collection, producer_key):
    path, _, rows = bundle
    _import(con, collection, path, rows)
    states = ledger.verification_states(con, [f"{UID}:0:1"])
    assert states[f"{UID}:0:1"]["state"] == "imported"
    assert states[f"{UID}:0:1"]["producer"] == bytes(producer_key.verify_key).hex()


# ────────────────── acceptance: distinct class per tamper ──────────────────

def test_tampered_header_is_rejected(bundle, con, collection):
    path, _, rows = bundle
    _tamper(path / exp.HEADER_NAME)
    result = _import(con, collection, path, rows)
    assert not result["ok"]
    assert result["error_class"] in ("BundleSignatureError", "BundleFormatError")


def test_tampered_parquet_is_rejected_as_a_signature_failure(bundle, con, collection):
    path, _, rows = bundle
    _tamper(path / exp.VECTORS_NAME)
    result = _import(con, collection, path, rows)
    assert not result["ok"]
    assert result["error_class"] == "BundleSignatureError"


def test_tampered_signature_is_rejected(bundle, con, collection):
    path, _, rows = bundle
    _tamper(path / exp.SIGNATURE_NAME, offset=10)
    result = _import(con, collection, path, rows)
    assert not result["ok"]
    assert result["error_class"] == "BundleSignatureError"


def test_tampered_possession_sidecar_is_rejected(bundle, con, collection):
    path, _, rows = bundle
    _tamper(path / exp.POSSESSION_NAME, offset=-5)
    result = _import(con, collection, path, rows)
    assert not result["ok"]
    assert result["error_class"] == "BundleSignatureError"


def test_truncated_part_is_caught_before_it_is_read(bundle, con, collection):
    """The header declares each part's size, checked before opening it."""
    path, _, rows = bundle
    (path / exp.VECTORS_NAME).write_bytes(b"short")
    result = _import(con, collection, path, rows)
    assert not result["ok"] and result["error_class"] == "BundleFormatError"


@pytest.mark.parametrize("missing", [exp.HEADER_NAME, exp.VECTORS_NAME,
                                      exp.POSSESSION_NAME, exp.SIGNATURE_NAME])
def test_missing_part_is_rejected(bundle, con, collection, missing):
    path, _, rows = bundle
    (path / missing).unlink()
    result = _import(con, collection, path, rows)
    assert not result["ok"]


# ─────────────────────────── all-or-nothing ───────────────────────────

def test_a_rejected_bundle_writes_no_vectors(bundle, con, collection):
    path, _, rows = bundle
    _tamper(path / exp.VECTORS_NAME)
    _import(con, collection, path, rows)

    conn = embeddings._conn(collection)
    stored = conn.open_table(embeddings.TABLE).search().where(
        "book_id IN (1)").limit(1000).to_arrow()
    # only the rows the fixture seeded directly; nothing from the bundle
    assert len(stored.column("id").to_pylist()) == len(rows)
    assert not ledger.recent(con)[0]["uids"]


def test_a_rejected_bundle_never_touches_documents_error(bundle, con, collection):
    """One malicious bundle must not remove a document from the rdf/fts stages
    too — worker._next_book filters `error IS NULL` for those as well."""
    path, _, rows = bundle
    _tamper(path / exp.SIGNATURE_NAME, offset=10)
    _import(con, collection, path, rows)
    doc = con.execute("SELECT error, retries FROM documents WHERE uid=?",
                       (UID,)).fetchone()
    assert doc["error"] is None and doc["retries"] == 0


def test_failure_is_recorded_in_the_ledger(bundle, con, collection):
    path, _, rows = bundle
    _tamper(path / exp.SIGNATURE_NAME, offset=10)
    _import(con, collection, path, rows)
    entry = ledger.recent(con)[0]
    assert entry["state"] == ledger.REJECTED
    assert "Signature" in entry["error"] or "signature" in entry["error"]


def test_import_never_raises_to_the_caller(bundle, con, collection):
    """'never crashes the worker' — asserted against a deliberately hostile
    directory rather than trusted to the docstring."""
    path, _, rows = bundle
    (path / exp.HEADER_NAME).write_bytes(b"\xff\xfe not json")
    result = _import(con, collection, path, rows)
    assert not result["ok"]


# ─────────────────────────── trust: the peer-wide list ───────────────────────────

def test_a_second_key_is_admitted_no_first_use_pin(bundle, con, collection, other_key,
                                                   tmp_path):
    """03 §3.5 / 08 §8.1: admission is the checks, not who came first."""
    path, _, rows = bundle
    assert _import(con, collection, path, rows)["ok"]

    other = tmp_path / "other"
    exp.export_bundle(con, collection, [UID], other, signing_key=other_key)
    result = _import(con, collection, other, rows)
    assert result["ok"] and result["admission"] == "admitted", result


def test_a_key_denied_by_hold_key_trust_is_refused(bundle, con, collection, producer_key):
    from app.hold import node_key
    path, _, rows = bundle
    key = "ed25519:" + bytes(producer_key.verify_key).hex()
    node_key.trust(key, deny=True)          # the peer-wide file the inbox reads
    result = _import(con, collection, path, rows)
    assert not result["ok"] and result["error_class"] == "BundleUntrusted"
    assert "--remove" in result["error"]
    node_key.trust(key, remove=True)
    assert _import(con, collection, path, rows)["ok"]


def test_a_burned_key_is_refused(bundle, con, collection, producer_key):
    path, _, rows = bundle
    trust.quarantine_key(con, bytes(producer_key.verify_key).hex())
    result = _import(con, collection, path, rows)
    assert not result["ok"] and result["error_class"] == "BundleUntrusted"
    assert "--restore" in result["error"]


def test_strict_refuses_a_key_off_the_allow_list(bundle, con, collection):
    path, _, rows = bundle
    result = _import(con, collection, path, rows, strict=True)
    assert not result["ok"] and result["error_class"] == "BundleUntrusted"


# ─────────────────────────── space ───────────────────────────

def test_wrong_space_is_refused(bundle, con, collection, monkeypatch):
    """The comparison is against the LOCALLY COMPUTED space_id, never against
    components the producer supplied — otherwise the producer would get to
    choose which recipe it is judged by. So the drift has to be introduced in
    the local computation, not in the space table."""
    from app import spaces
    path, _, rows = bundle
    monkeypatch.setattr(spaces, "resolve_embed_model_hash", lambda *a: ("d1ffe4e17", False))
    result = _import(con, collection, path, rows)
    assert not result["ok"] and result["error_class"] == "BundleSpaceMismatch"


def test_weak_model_hash_with_a_dead_encoder_is_refused_but_retryable(
        bundle, con, collection, monkeypatch):
    """A weak hash (the backend publishes no weight identity, or was
    unreachable) means the LABEL proves nothing: two nodes with different
    weights behind one tag hash identically. The proof that matters is the
    local encoder reproducing the vectors — and with the encoder dead that
    proof cannot be produced, so refuse, retryably."""
    from app import spaces
    monkeypatch.setattr(spaces, "resolve_embed_model_hash", lambda *a, **k: ("x" * 16, True))
    path, _, rows = bundle

    def dead(texts):
        raise ConnectionError("encoder down")
    result = _import(con, collection, path, rows, embed_fn=dead)
    assert not result["ok"]
    assert result["error_class"] == "SpaceUnconfirmedError"
    assert result["retryable"] is True


def test_weak_model_hash_with_a_live_encoder_is_admitted_on_reproduction(
        con, collection, producer_key, tmp_path, monkeypatch):
    """Found live on the first two-node exchange: llama-server has no
    identity route, so BOTH nodes pin the same space through the same weak
    hash by construction — and the old rule refused every bundle between
    them forever. With the encoder live, the sampled re-embed IS the
    verification (the same test that approved the Vulkan backend): admit
    on reproduction, never on the label. The producer and the importer
    share the weak hash here, exactly as the two nodes do."""
    from app import spaces
    monkeypatch.setattr(spaces, "resolve_embed_model_hash", lambda *a, **k: ("x" * 16, True))
    # the pin was taken before the patch; on a real node pin and live agree
    con.execute("UPDATE space SET space_id=? WHERE id=1",
                (importer._local_space(con, collection)[0],))
    rows = seed_document(con, collection, UID, 1, n_chunks=12, seed=1)
    con.execute("UPDATE documents SET embed_done=0 WHERE uid=?", (UID,))
    exp.export_bundle(con, collection, [UID], tmp_path / "bundle", signing_key=producer_key)
    result = _import(con, collection, tmp_path / "bundle", rows)   # reproduces -> admitted
    assert result["ok"], result


def test_dim_mismatch_is_refused(bundle, con, collection):
    """space_id does not cover dim, so it is checked separately."""
    path, _, rows = bundle
    con.execute("UPDATE space SET dim=? WHERE id=1", (DIM + 1,))
    result = _import(con, collection, path, rows)
    assert not result["ok"] and result["error_class"] == "BundleSpaceMismatch"


# ─────────────────────────── possession ───────────────────────────

def test_changed_local_text_is_refused(bundle, con, collection):
    """The producer's vectors were computed from a different revision."""
    path, _, rows = bundle
    con.execute("UPDATE documents SET txt_hash=? WHERE uid=?", ("0" * 32, UID))
    result = _import(con, collection, path, rows)
    assert not result["ok"] and result["error_class"] == "PossessionError"


def test_document_absent_from_the_manifest_is_refused(bundle, con, collection):
    path, _, rows = bundle
    con.execute("DELETE FROM documents WHERE uid=?", (UID,))
    result = _import(con, collection, path, rows)
    assert not result["ok"] and result["error_class"] == "PossessionError"


# ─────────────────────────── admission checks ───────────────────────────

def test_a_different_model_is_refused(bundle, con, collection):
    """The bundle is perfectly formed and correctly signed; only re-embedding
    reveals the vectors came from elsewhere."""
    path, _, rows = bundle
    foreign = [{"text": r["text"], "vector": v}
               for r, v in zip(rows, unit_vectors(len(rows), seed=4242))]
    result = _import(con, collection, path, rows,
                     embed_fn=_local_embedder(foreign))
    assert not result["ok"]
    assert result["error_class"] == "BundleVerificationError"
    assert "DIFFERENT MODEL" in result["error"]


def test_unreachable_embed_endpoint_is_retryable_not_a_rejection(bundle, con, collection):
    path, _, rows = bundle

    def boom(_batch):
        raise ConnectionError("endpoint down")
    result = _import(con, collection, path, rows, embed_fn=boom)
    assert not result["ok"] and result["retryable"] is True
    assert result["error_class"] == "VerificationUnavailable"
    # a transient failure must not be counted against the producer
    assert con.execute("SELECT COUNT(*) c FROM bundles").fetchone()["c"] == 0


# ─────────────────────────── chunking drift ───────────────────────────

def test_different_chunking_is_refused(bundle, con, collection, monkeypatch):
    """CHUNK_TOKENS and CHUNK_OVERLAP are NOT part of space_id, so a fully
    honest producer at different settings would otherwise attach its vectors to
    different text."""
    from app import config
    path, _, rows = bundle
    # Realistic drift (a peer configured at a different window), not a
    # pathological value — CHUNK_OVERLAP >= CHUNK_TOKENS used to make chunking
    # loop forever, see tests/test_chunking_termination.py.
    monkeypatch.setattr(config, "CHUNK_TOKENS", 200)
    result = _import(con, collection, path, rows)
    assert not result["ok"] and result["error_class"] == "BundleFormatError"
    assert "chunked the text differently" in result["error"]


# ─────────────────────────── crash safety and the race ───────────────────────────

def test_crash_between_delete_and_add_leaves_the_document_unembedded(
        bundle, con, collection, monkeypatch):
    """The reordering that closes a hole predating this module: embed_done is
    cleared BEFORE Lance is touched, so an interruption cannot leave a document
    flagged embedded with zero vectors."""
    path, _, rows = bundle
    con.execute("UPDATE documents SET embed_done=1 WHERE uid=?", (UID,))

    class Boom(RuntimeError):
        pass

    # Injected at _book_filter, which the importer calls inside the write lock
    # immediately before Lance's delete — after embed_done has been cleared.
    # Deliberately NOT by patching the LanceDB table class: mutating a shared
    # library class leaks across tests and left the engine in a state that hung
    # the whole run.
    def explode(_book_id):
        raise Boom("killed between delete and add")

    monkeypatch.setattr(embeddings, "_book_filter", explode)
    with pytest.raises(Boom):
        importer._import_checked(con, collection, path,
                                 embed_fn=_local_embedder(rows),
                                 strict=False, stop=None)

    doc = con.execute("SELECT embed_done FROM documents WHERE uid=?", (UID,)).fetchone()
    assert doc["embed_done"] == 0, "an interrupted import must leave work to redo"
    assert ledger.get(con, ledger.recent(con)[0]["bundle_id"])["state"] == ledger.APPLYING
    assert ledger.sweep_stale(con)          # visible on restart


def test_stop_event_halts_between_documents(con, collection, producer_key, tmp_path):
    """SIGTERM mid-bundle stops cleanly between documents rather than leaving a
    partially applied bundle."""
    rows = seed_document(con, collection, "gutenberg:1", 1, n_chunks=4, seed=1)
    rows += seed_document(con, collection, "gutenberg:2", 2, n_chunks=4, seed=2)
    exp.export_bundle(con, collection, ["gutenberg:1", "gutenberg:2"],
                      tmp_path / "b", signing_key=producer_key)
    con.execute("UPDATE documents SET embed_done=0")

    stop = threading.Event()
    stop.set()
    result = importer.import_bundle(con, collection, tmp_path / "b",
                                     embed_fn=_local_embedder(rows), stop=stop)
    assert result["ok"], result.get("error")
    assert result["uids"] == 0, "no document may be written once stop is set"
    embedded = con.execute(
        "SELECT COUNT(*) c FROM documents WHERE embed_done=1").fetchone()["c"]
    assert embedded == 0


def test_concurrent_import_and_local_embed_leave_one_consistent_copy(
        bundle, con, collection, monkeypatch):
    """Lance's delete-then-add is two calls with no transaction; without the
    shared writer lock, interleaving duplicates every row."""
    path, _, rows = bundle
    barrier = threading.Barrier(2, timeout=20)
    errors = []

    def do_import():
        connection = db.connect(collection)
        try:
            barrier.wait()
            importer.import_bundle(connection, collection, path,
                                    embed_fn=_local_embedder(rows))
        except Exception as e:      # noqa: BLE001
            errors.append(e)

    def do_local_embed():
        connection = db.connect(collection)
        try:
            chunks = [{"id": r["id"], "seq": r["seq"], "chapter": "",
                        "text": r["text"]} for r in rows]
            monkeypatch_free_vectors = [np.asarray(r["vector"], dtype=np.float32)
                                         for r in rows]
            barrier.wait()
            embeddings.upsert_book.__wrapped__ if False else None
            from app import embeddings as em
            original = em._embed_texts
            em._embed_texts = lambda texts, **kw: monkeypatch_free_vectors[:len(texts)]
            try:
                em.upsert_book(1, "en", chunks, collection, lock_con=connection)
            finally:
                em._embed_texts = original
        except Exception as e:      # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=do_import), threading.Thread(target=do_local_embed)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
        assert not t.is_alive(), "writer deadlocked"

    table = embeddings._conn(collection).open_table(embeddings.TABLE)
    stored = table.search().where("book_id IN (1)").limit(10_000).to_arrow()
    ids = stored.column("id").to_pylist()
    assert len(ids) == len(set(ids)), f"duplicate vectors: {len(ids)} rows, {len(set(ids))} ids"
    assert len(ids) == len(rows)
