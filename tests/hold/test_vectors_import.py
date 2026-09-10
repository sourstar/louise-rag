"""tests/hold/test_vectors_import.py — an admitted bundle lands.

Migration plan step 4e's second half, and 4f's importer half. `admit.py`
decides whether; `land.py` decides what is written, and `hold vectors
import` is the two of them plus this peer's own knowledge.

The end-to-end tests run the REAL command, over a bundle the real
exporter wrote, into a real Lance table. A hand-built row list would let
the exporter and the importer drift apart, and the drift would be
invisible until a stranger's bundle arrived.
"""
from pathlib import Path

import numpy as np
import pytest

from app import embeddings, louise
from app.hold import admit, collection as col, land, vectors as vec
from app.hold.land import LandError
from tests.hold.conftest import KEY
from tests.hold.test_admit import (chunk_texts, made, mine_of,  # noqa: F401
                                   reference_and_envelope, space_embed)
from tests.hold.test_vectors_export import build_drag, first_shard


def _member(root, codex, ref, env):
    """Rewrite this peer's calibration member so its reference is the one
    `space_embed` produces.

    `create` wrote the member with `conftest.fake_embed`, which is a
    different encoder: T2 compares the producer's answers to the sixteen
    known texts against THIS peer's, so leaving the two disagreeing would
    make every bundle here fail T2 for the fixture's reason rather than
    the test's.
    """
    from safetensors.numpy import save_file
    from app.hold import drag
    p = Path(root) / "gutenberg" / "drag" / \
        drag.calibration_paths(codex.space.calibration.set)[0]
    p.parent.mkdir(parents=True, exist_ok=True)
    save_file({"vectors": np.asarray(ref, dtype="float32"),
               "envelope": np.asarray(env, dtype="float32")}, str(p))
    return p


@pytest.fixture
def importable(made, monkeypatch):
    """The `made` bundle, plus everything `hold vectors import` reads."""
    monkeypatch.delenv("EMBED_URL", raising=False)
    root, snapshot, codex, dest, store, ids = made
    ref, env = reference_and_envelope(codex)
    _member(root, codex, ref, env)
    return root, snapshot, codex, dest, ids


def _import(root, dest, *extra):
    return louise.main(["hold", "vectors", "import", "gutenberg",
                        "--root", str(root), "--bundle", str(dest), *extra])


def _stored(root, codex):
    from app.hold import records as rec
    local = rec.local_collection(root, "gutenberg", codex)
    db = embeddings._conn(local)
    if embeddings.TABLE not in db.table_names():
        return {}
    return {r["id"]: r for r in
            db.open_table(embeddings.TABLE).search().limit(10_000).to_list()}


# ---- the verb ------------------------------------------------------------

def test_the_verb_is_in_the_tree():
    p = louise.build_parser()
    a = p.parse_args(["hold", "vectors", "import", "g", "--bundle", "b"])
    assert a.fn is not None and a.strict is False and a.require_t3 is False


def test_a_bundle_lands_and_the_vectors_are_the_producers(importable, capsys):
    """The whole point, end to end: the exporter's bundle goes through
    §3.5's ladder and this peer's Lance table holds the PRODUCER's
    vectors under this peer's own text."""
    root, snapshot, codex, dest, ids = importable
    assert _import(root, dest) == 0

    rows = _stored(root, codex)
    assert set(rows) == set(ids)
    texts = chunk_texts(root, snapshot, codex, first_shard(snapshot))
    for cid, r in rows.items():
        # this peer's text (it is not in the bundle at all) …
        assert r["text"] == texts[cid]
        # … and the producer's vector, to the table's f16
        want = np.asarray(space_embed([texts[cid]], dim=codex.space.dim)[0])
        assert np.allclose(np.asarray(r["vector"], dtype=np.float64), want,
                           atol=2e-3)
    out = capsys.readouterr().out
    assert "T0" in out and "landed" in out


def test_T3_is_skipped_without_an_encoder_and_the_import_still_lands(importable, capsys):
    """07 §7.8's deferral is not the rule here. T0, T1 and T2 are what
    make a bundle usable, and refusing to import for want of a GPU would
    make importing a privilege of the nodes that need it least."""
    root, _, codex, dest, ids = importable
    assert _import(root, dest) == 0
    assert "T3          SKIPPED" in capsys.readouterr().out


def test_require_t3_defers_instead(importable, capsys):
    """Exit 3, not 1: 03 §3.1's third outcome. The operator asked for the
    spot recompute, and an unreachable encoder is inconclusive about the
    bundle rather than a verdict on it."""
    root, _, _, dest, _ = importable
    assert _import(root, dest, "--require-t3") == 3
    assert "deferred" in capsys.readouterr().err


def test_importing_twice_writes_the_same_rows(importable):
    """Invariant 2: every write is delete-then-insert keyed on the chunk
    id, so a re-run is a no-op rather than a duplicate."""
    root, _, codex, dest, ids = importable
    assert _import(root, dest) == 0
    first = _stored(root, codex)
    assert _import(root, dest) == 0
    second = _stored(root, codex)
    assert set(second) == set(first) == set(ids)
    assert len(second) == len(ids), "a second import duplicated rows"


def test_a_refused_bundle_writes_nothing(importable, capsys):
    """"Refused whole" must mean nothing was written — exit 1, and a
    table that is still empty."""
    root, snapshot, codex, dest, ids = importable
    header, rows, possession, _ = vec.read(dest)
    # a DIFFERENT encoder answering the challenge: T2's case
    from app.hold import export as exp
    out = dest.parent / "wrong-oven"
    store = {cid: space_embed([t], dim=codex.space.dim)[0]
             for cid, t in chunk_texts(root, snapshot, codex,
                                       first_shard(snapshot)).items()}
    exp.export(root, "gutenberg", shard=first_shard(snapshot), dest=out,
               vectors_for=lambda w: {i: store[i] for i in w if i in store},
               embed=lambda t: space_embed(t, dim=codex.space.dim, tag=b"other"),
               key=KEY)
    assert _import(root, out) == 1
    assert _stored(root, codex) == {}
    assert "REFUSED" in capsys.readouterr().err


def test_a_bundle_for_a_snapshot_this_peer_lacks_says_so(importable, capsys):
    """`snapshot_id` is PROVENANCE and admits nothing — but the records a
    `content_hash` is checked against live in a snapshot, so an importer
    that took no such round has nothing to check with, and the message
    says that rather than blaming the sender."""
    root, _, _, dest, _ = importable
    # Exit 2: "a rule stood in the way before anything was done" (07 §7.7).
    assert _import(root, dest, "--snapshot", "1999-01") == 2
    assert "holds no snapshot" in capsys.readouterr().err


def test_a_yellow_collection_without_an_ack_is_not_imported_into(importable,
                                                                 monkeypatch,
                                                                 capsys):
    """Invariant 9, the IMPORTER half (migration plan step 4f). The ARCHIVE
    half sits at `genesis.import_shard`; this is the other door, and the
    invariant is explicit that the gate is on USE "however the data
    arrived — hand-populated folders and peer copies are not a bypass".
    """
    from app import licensing
    monkeypatch.setattr(licensing, "license_class_for", lambda _c: "yellow")
    root, _, codex, dest, _ = importable
    assert _import(root, dest) == 2
    err = capsys.readouterr().err
    assert "requires an acknowledgement" in err
    assert _stored(root, codex) == {}


# ---- placement, on its own -----------------------------------------------

def test_a_chunk_id_this_peers_cut_does_not_produce_is_unplaceable(importable):
    """The check the module note argues for: chunking is deterministic and
    the content hashes agreed, so an id outside this peer's own cut has no
    text to be stored against, and a guess would be a stored vector nobody
    can reproduce."""
    root, snapshot, codex, dest, ids = importable
    cut = land.cut(codex, list(_records(root, snapshot, codex)))
    admitted = [(ids[0], [0.0] * codex.space.dim),
                ("gutenberg:99999:0:1", [0.0] * codex.space.dim)]
    rows, unplaceable = land.rows_to_write(codex, admitted, cut)
    assert [r["id"] for r in rows] == [ids[0]]
    assert [c for c, _ in unplaceable] == ["gutenberg:99999:0:1"]
    assert "does not produce" in unplaceable[0][1]


def test_land_refuses_to_write_a_report_the_ladder_failed(importable):
    """The second lock on `admit`'s own door: a caller that ignored `ok`
    would otherwise write zero rows and report success."""
    root, snapshot, codex, dest, ids = importable
    report = admit.Report(bundle_id="sha256:00", producer="ed25519:00",
                          tiers=(admit.Tier(admit.T0, admit.FAIL, "no"),),
                          admitted=(), refused=(), padded=0)
    with pytest.raises(LandError, match="refused this bundle at T0"):
        land.land(codex, report, records=[], write=lambda r: len(r))


def _records(root, snapshot, codex):
    from app.hold import export as exp
    from app.pack import records as pack_records
    drag = col.drag_of(root, "gutenberg")
    entry = exp._shard_of(snapshot, first_shard(snapshot))
    return pack_records.iter_records(drag / entry.path)


# ---- the chunk-id write --------------------------------------------------

def test_upsert_chunks_replaces_one_chunk_and_leaves_its_siblings(importable):
    """`upsert_book` deletes the document; this must not. A bundle carries
    the chunks its producer had embedded, which may be some of a record's
    and not all, and the rest may be this peer's own work."""
    root, snapshot, codex, dest, ids = importable
    from app.hold import records as rec
    local = rec.local_collection(root, "gutenberg", codex)
    texts = chunk_texts(root, snapshot, codex, first_shard(snapshot))

    rows = [{"id": cid, "book_id": int(cid.rsplit(":", 3)[1]), "lang": "en",
             "seq": int(cid.rsplit(":", 2)[1]), "chapter": "",
             "text": texts[cid], "vector": [0.5] * codex.space.dim}
            for cid in ids]
    assert embeddings.upsert_chunks(rows, collection=local, dtype="f32") == len(ids)

    victim = rows[0]
    again = dict(victim, text="rewritten", vector=[0.25] * codex.space.dim)
    assert embeddings.upsert_chunks([again], collection=local, dtype="f32") == 1

    got = _stored(root, codex)
    assert set(got) == set(ids), "a sibling chunk was deleted with its brother"
    assert got[victim["id"]]["text"] == "rewritten"
    assert got[victim["id"]]["vector"][0] == pytest.approx(0.25, abs=1e-3)


def test_a_chunk_id_that_is_not_9_5s_shape_never_reaches_a_filter(importable):
    """Lance filters are strings, not parameterised statements, and these
    ids come from a bundle a stranger signed."""
    root, _, codex, _, _ = importable
    from app.hold import records as rec
    local = rec.local_collection(root, "gutenberg", codex)
    bad = [{"id": "g:1:0:1' OR '1'='1", "book_id": 1, "lang": "en", "seq": 0,
            "chapter": "", "text": "x", "vector": [0.0] * codex.space.dim}]
    with pytest.raises(ValueError, match="09 §9.5"):
        embeddings.upsert_chunks(bad, collection=local, dtype="f32")
