"""tests/hold/test_vectors_export.py — one shard's vectors become a bundle.

Migration plan step 4d; docs/09 §9.7, docs/07 §7.3. `app/hold/vectors.py`
already owned the four files and `bundle.py` their bytes; what was
missing was the GATHERING — which records are in the shard, which chunks
they cut into, which of those this peer holds a vector for, and what the
header may therefore say.

Every test here goes through a REAL drag: a Codex, a records build, a
snapshot. The header is signed, so a gathering mistake is not a wrong
number in a log — it is a bundle every importer refuses, or worse, one
they accept that says something untrue.
"""
from pathlib import Path

import pytest
from nacl import signing

from app.hold import collection as col, export as exp, records as rec, \
    snapshot as snap, vectors as vec
from app.hold.export import ExportError
from tests.hold.conftest import KEY, NOW, PLATFORM, fake_embed
from tests.hold.test_template import recipe


def build_drag(tmp_path: Path, corpus_dir, **kw):
    """A collection with records and a snapshot — the same three commands
    an operator runs (07 §7.3), because a bundle cannot be exported from
    anything less."""
    root = tmp_path / "collections"
    col.create_from_recipe(root, "gutenberg", recipe(), platform_image=PLATFORM,
                           embed=fake_embed, name="gutenberg", key=KEY, now=NOW)
    rec.build(root, "gutenberg", corpus_dir=corpus_dir, log=lambda *_: None, **kw)
    rec.write_validation(root, "gutenberg")
    snapshot = snap.create(root, "gutenberg", log=lambda *_: None)
    return root, snapshot


def a_store(codex, root, snapshot, shard, *, hold=None):
    """This peer's vector store, as `export` sees it: chunk id -> vector.

    Built by deriving the shard's chunks the same way the exporter does,
    so the fixture cannot disagree with the code about what a chunk id
    is — if it could, a passing test would prove nothing.
    """
    from app.pack import records as pack_records
    from app import layers as layers_mod
    drag = col.drag_of(root, "gutenberg")
    entry = exp._shard_of(snapshot, shard)
    ids = []
    for r in pack_records.iter_records(drag / entry.path):
        row = {"uid": r["uid"], "native_id": r.get("native_id", 0)}
        sections = [tuple(s) for s in r.get("sections") or []]
        ids += [c["id"] for c in layers_mod.chunks_for_document(codex, row, sections)]
    keep = ids if hold is None else [i for i in ids if hold(i)]
    dim = codex.space.dim
    return {i: [float((n + 1) % 7)] * dim for n, i in enumerate(keep)}, ids


def first_shard(snapshot) -> str:
    s = snapshot.shards[0]
    return f"{s.slice}/{s.n:05d}"


@pytest.fixture
def drag(isolated_config, tmp_path, corpus_dir):
    root, snapshot = build_drag(tmp_path, corpus_dir, records_per_shard=2)
    codex = col.read(root, "gutenberg")
    return root, snapshot, codex


# ---- the happy path, end to end ------------------------------------------

def test_a_shard_round_trips_through_export_and_read(drag, tmp_path):
    """The one test that matters most: what `export` writes, §9.7's own
    reader accepts — signature, content digest, possession coverage and
    all, with nothing re-serialised in between."""
    root, snapshot, codex = drag
    shard = first_shard(snapshot)
    store, ids = a_store(codex, root, snapshot, shard)

    out = exp.export(root, "gutenberg", shard=shard, dest=tmp_path / "b",
                     vectors_for=lambda want: {i: store[i] for i in want if i in store},
                     embed=lambda texts: fake_embed(texts, dim=codex.space.dim),
                     key=KEY, created_at=1756713600)

    header, rows, possession, bundle_id = vec.read(tmp_path / "b")
    assert bundle_id == out["bundle_id"]
    assert header["shard"] == shard
    assert header["collection"] == codex.collection_id
    assert header["space_id"] == codex.space_id
    assert header["snapshot_id"] == snapshot.snapshot_id
    assert header["dim"] == codex.space.dim and header["dtype"] == codex.space.dtype
    assert header["challenge"]["n"] == exp.CHALLENGE_N
    assert header["challenge"]["set"] == codex.space.calibration.set
    # the challenge rows come first, then the chunks; `chunk_count` counts
    # only the second group
    assert len(rows) == exp.CHALLENGE_N + header["chunk_count"]
    assert header["chunk_count"] == len(ids) == out["chunks"]


def test_the_bundle_is_a_directory_of_the_four_files(drag, tmp_path):
    """07 §7.3 said `--out FILE` and 09 §9.7 defines a directory; the
    document was amended and this is what it now means."""
    root, snapshot, codex = drag
    shard = first_shard(snapshot)
    store, _ = a_store(codex, root, snapshot, shard)
    dest = tmp_path / "bundle"
    exp.export(root, "gutenberg", shard=shard, dest=dest,
               vectors_for=lambda w: {i: store[i] for i in w if i in store},
               embed=lambda t: fake_embed(t, dim=codex.space.dim), key=KEY)
    assert dest.is_dir()
    assert sorted(p.name for p in dest.iterdir()) == sorted(vec.bd.FILES)


# ---- what `uids` and `possession` are, which is the easy thing to get wrong

def test_uids_are_what_is_CARRIED_not_what_is_in_the_shard(drag, tmp_path):
    """A bundle is what this peer PRODUCED. A record whose chunks are all
    unembedded belongs in neither `uids` nor `possession` — and §9.7's
    reader requires those two to name exactly the same set, so getting it
    wrong is a bundle nobody can import."""
    root, snapshot, codex = drag
    shard = first_shard(snapshot)
    all_store, ids = a_store(codex, root, snapshot, shard)
    # hold vectors for the FIRST record's chunks only
    first_uid = sorted({i.rsplit(":", 2)[0] for i in ids})[0]
    partial = {i: v for i, v in all_store.items() if i.startswith(first_uid + ":")}
    assert 0 < len(partial) < len(all_store), "the fixture must be partial"

    out = exp.export(root, "gutenberg", shard=shard, dest=tmp_path / "b",
                     vectors_for=lambda w: {i: partial[i] for i in w if i in partial},
                     embed=lambda t: fake_embed(t, dim=codex.space.dim), key=KEY)

    header, _, possession, _ = vec.read(tmp_path / "b")
    assert header["uids"] == [first_uid]
    assert set(possession) == {first_uid}
    assert out["uids"] == 1 and out["records_in_shard"] >= 2


def test_possession_carries_the_records_own_content_hash(drag, tmp_path):
    """03 §3.5 admits a bundle on `space_id` plus per-record
    `content_hash`, so this value is the whole of what binds these vectors
    to a text. It is READ from the shard, never recomputed — the shard is
    what the snapshot's roots cover."""
    from app.pack import records as pack_records
    root, snapshot, codex = drag
    shard = first_shard(snapshot)
    store, _ = a_store(codex, root, snapshot, shard)
    exp.export(root, "gutenberg", shard=shard, dest=tmp_path / "b",
               vectors_for=lambda w: {i: store[i] for i in w if i in store},
               embed=lambda t: fake_embed(t, dim=codex.space.dim), key=KEY)

    _, _, possession, _ = vec.read(tmp_path / "b")
    drag_dir = col.drag_of(root, "gutenberg")
    entry = exp._shard_of(snapshot, shard)
    on_disk = {r["uid"]: r["content_hash"]
               for r in pack_records.iter_records(drag_dir / entry.path)}
    assert possession == {u: on_disk[u] for u in possession}
    assert all(len(h) == 32 for h in possession.values()), "blake2b-128 hex"


# ---- the refusals --------------------------------------------------------

def test_a_shard_this_peer_has_no_vectors_for_is_refused(drag, tmp_path):
    """An empty bundle is not a smaller bundle; it is an absent one, and
    a header describing zero chunks would still be signed and shared."""
    root, snapshot, codex = drag
    with pytest.raises(ExportError, match="nothing to export"):
        exp.export(root, "gutenberg", shard=first_shard(snapshot),
                   dest=tmp_path / "b", vectors_for=lambda w: {},
                   embed=lambda t: fake_embed(t, dim=codex.space.dim), key=KEY)


def test_an_unknown_shard_names_the_ones_there_are(drag, tmp_path):
    root, snapshot, codex = drag
    with pytest.raises(ExportError, match="no shard 'lang=xx/00099'"):
        exp.export(root, "gutenberg", shard="lang=xx/00099", dest=tmp_path / "b",
                   vectors_for=lambda w: {}, embed=lambda t: [], key=KEY)


def test_a_named_snapshot_that_is_not_held_is_refused(drag, tmp_path):
    root, snapshot, codex = drag
    with pytest.raises(ExportError, match="no snapshot '1999-01'"):
        exp.export(root, "gutenberg", shard=first_shard(snapshot),
                   dest=tmp_path / "b", snapshot_id="1999-01",
                   vectors_for=lambda w: {}, embed=lambda t: [], key=KEY)


def test_an_encoder_that_returns_the_wrong_count_is_refused(drag, tmp_path):
    """The challenge is sixteen rows by §9.7 and the header says so; a
    short answer would make `chunk_count` disagree with the parquet."""
    root, snapshot, codex = drag
    shard = first_shard(snapshot)
    store, _ = a_store(codex, root, snapshot, shard)
    with pytest.raises(ExportError, match="returned 3 vectors for 16"):
        exp.export(root, "gutenberg", shard=shard, dest=tmp_path / "b",
                   vectors_for=lambda w: {i: store[i] for i in w if i in store},
                   embed=lambda t: fake_embed(t[:3], dim=codex.space.dim), key=KEY)


# ---- the shape of the thing ----------------------------------------------

def test_the_challenge_rows_come_first_and_in_index_order(drag, tmp_path):
    """§9.7 fixes the row order: the challenge rows first, in index order,
    then the chunk rows sorted by (uid, seq)."""
    root, snapshot, codex = drag
    shard = first_shard(snapshot)
    store, _ = a_store(codex, root, snapshot, shard)
    exp.export(root, "gutenberg", shard=shard, dest=tmp_path / "b",
               vectors_for=lambda w: {i: store[i] for i in w if i in store},
               embed=lambda t: fake_embed(t, dim=codex.space.dim), key=KEY)
    _, rows, _, _ = vec.read(tmp_path / "b")
    head = [r[0] for r in rows[:exp.CHALLENGE_N]]
    assert head == [f"calibration:{i}" for i in range(exp.CHALLENGE_N)]
    assert not any(r[0].startswith("calibration:") for r in rows[exp.CHALLENGE_N:])


def test_two_exports_of_one_shard_agree_on_everything_signed(drag, tmp_path):
    """Parquet bytes are not stable across writers and are not meant to
    be (`vectors.py`), but the LOGICAL content is: same header, same
    `content_digest`, same bundle id, for the same inputs."""
    root, snapshot, codex = drag
    shard = first_shard(snapshot)
    store, _ = a_store(codex, root, snapshot, shard)
    kw = dict(shard=shard,
              vectors_for=lambda w: {i: store[i] for i in w if i in store},
              embed=lambda t: fake_embed(t, dim=codex.space.dim),
              key=KEY, created_at=1756713600)
    a = exp.export(root, "gutenberg", dest=tmp_path / "a", **kw)
    b = exp.export(root, "gutenberg", dest=tmp_path / "b", **kw)
    assert a["bundle_id"] == b["bundle_id"]
    ha, _, _, _ = vec.read(tmp_path / "a")
    hb, _, _, _ = vec.read(tmp_path / "b")
    assert ha == hb
