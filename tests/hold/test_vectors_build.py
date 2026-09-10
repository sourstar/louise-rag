"""tests/hold/test_vectors_build.py — where a collection's first vectors
come from.

The verb step 4's sub-tasks never named and step 6 runs at point 5:
without it the proof-of-concept round trip has nothing to export.
"""
import json
from dataclasses import replace

import pytest

from app import embeddings, louise
from app.hold import codex as cx, collection as col, embedder as emb, land, \
    vectors_build as vb
from app.hold.vectors_build import BuildRefused
from tests.hold.conftest import KEY, NOW
from tests.hold.test_admit import made, space_embed  # noqa: F401
from tests.hold.test_vectors_export import build_drag, first_shard
from tests.hold.test_vectors_import import _stored  # noqa: F401

SEED = b"\x03" * 32


def _all_chunks(codex, snapshot, root) -> int:
    """Every chunk of every shard, cut the way every stage cuts."""
    n = 0
    for s in snapshot.shards:
        n += len(land.cut(codex, land.records_for(
            root, "gutenberg", snapshot, f"{s.slice}/{s.n:05d}", None)))
    return n


def _statement(root, codex, *, passed=None, space_id=None):
    """What `hold embedder check --accept` leaves in `state/`."""
    n = codex.space.calibration.n
    tol = float(codex.space.tolerance)
    ok = codex.space.calibration.min_pass if passed is None else passed
    run = emb.Run(
        space_id=space_id or codex.space_id, collection_id=codex.collection_id,
        set_id=codex.space.calibration.set, seed=SEED,
        sampled=tuple(range(n)),
        cosines=tuple([0.999] * ok + [tol - 0.1] * (n - ok)),
        all_cosines=tuple([0.999] * 512), tolerance=tol,
        min_pass=codex.space.calibration.min_pass,
        max_input_tokens=8192, tokens_per_s=1000)
    out = root / "gutenberg" / "state" / "calibration.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(cx.canonical_json(emb.statement(run, codex, KEY, now=NOW)))
    return out


@pytest.fixture
def buildable(isolated_config, tmp_path, corpus_dir, monkeypatch):
    """A drag with records and a snapshot, and NO vectors."""
    monkeypatch.delenv("EMBED_URL", raising=False)
    root, snapshot = build_drag(tmp_path, corpus_dir, records_per_shard=2)
    codex = col.read(root, "gutenberg")
    _statement(root, codex)
    return root, snapshot, codex


def _run(codex, snapshot, root, *, embed, shards=None, dry_run=False, stop=None):
    from app.hold import records as rec
    local = rec.local_collection(root, "gutenberg", codex)
    return vb.build(
        codex, shards=shards if shards is not None else snapshot.shards,
        records_for=lambda s: land.records_for(
            root, "gutenberg", snapshot, f"{s.slice}/{s.n:05d}", None),
        vectors_for=lambda i: embeddings.vectors_by_chunk_id(i, collection=local),
        embed=embed,
        write=lambda rows: embeddings.upsert_chunks(
            rows, collection=local, dtype=codex.space.dtype),
        batch=2, stop=stop, dry_run=dry_run)


# ---- the refusals, which come first --------------------------------------

def test_no_statement_refuses_before_anything_is_done(buildable):
    """07 §7.3: "`vectors build` … refuse (exit 2) without a passing check
    for this collection's space on the configured endpoint"."""
    root, _, codex = buildable
    (root / "gutenberg" / "state" / "calibration.json").unlink()
    with pytest.raises(BuildRefused, match="no calibration statement"):
        vb.require_statement(root, "gutenberg", codex)


def test_a_statement_about_another_space_says_nothing_about_this_one(buildable):
    """A collection whose encoder changed has a statement about the
    encoder it no longer runs — which is the situation the gate is for."""
    root, _, codex = buildable
    _statement(root, codex, space_id="sha256:" + "ab" * 32)
    with pytest.raises(BuildRefused, match="another space"):
        vb.require_statement(root, "gutenberg", codex)


def test_a_failing_statement_is_refused(buildable):
    root, _, codex = buildable
    _statement(root, codex, passed=1)
    with pytest.raises(BuildRefused, match="quorum"):
        vb.require_statement(root, "gutenberg", codex)


def test_a_hand_edited_statement_is_refused(buildable):
    """The whole value of the gate is that it cannot be satisfied by
    writing a file that says pass, so the envelope is VERIFIED."""
    root, _, codex = buildable
    p = root / "gutenberg" / "state" / "calibration.json"
    doc = json.loads(p.read_bytes())
    doc["body"]["passed"] = 16
    p.write_bytes(cx.canonical_json(doc))
    with pytest.raises(BuildRefused, match="not a valid statement"):
        vb.require_statement(root, "gutenberg", codex)


def test_a_passing_statement_for_this_space_is_accepted(buildable):
    root, _, codex = buildable
    body = vb.require_statement(root, "gutenberg", codex)
    assert body["space_id"] == codex.space_id


# ---- the build itself ----------------------------------------------------

def test_it_embeds_every_chunk_of_every_shard(buildable):
    root, snapshot, codex = buildable
    out = _run(codex, snapshot, root,
               embed=lambda t: space_embed(t, dim=codex.space.dim))
    assert out.chunks_written == out.chunks_seen > 0
    assert out.shards == len(snapshot.shards)

    stored = _stored(root, codex)
    texts = {}
    for s in snapshot.shards:
        texts.update(land.cut(codex, land.records_for(
            root, "gutenberg", snapshot, f"{s.slice}/{s.n:05d}", None)))
    assert set(stored) == set(texts)
    for cid, row in stored.items():
        assert row["text"] == texts[cid]["text"]


def test_a_second_run_embeds_nothing(buildable):
    """What is already there is not re-embedded — which is what makes this
    resumable, and correct to run after an import."""
    root, snapshot, codex = buildable
    calls = []

    def embed(texts):
        calls.append(len(texts))
        return space_embed(texts, dim=codex.space.dim)

    first = _run(codex, snapshot, root, embed=embed)
    assert first.chunks_written > 0
    calls.clear()
    second = _run(codex, snapshot, root, embed=embed)
    assert second.nothing_to_do and second.chunks_seen == 0
    assert calls == [], "it re-embedded chunks that already had vectors"


def test_a_peer_that_received_half_a_shard_embeds_the_other_half(buildable):
    """The import case: the encoder is spent only on what is missing."""
    root, snapshot, codex = buildable
    from app.hold import records as rec
    local = rec.local_collection(root, "gutenberg", codex)
    cut = land.cut(codex, land.records_for(
        root, "gutenberg", snapshot, first_shard(snapshot), None))
    already = sorted(cut)[0]
    embeddings.upsert_chunks([{
        "id": already, "book_id": cut[already]["book_id"], "lang": "en",
        "seq": cut[already]["seq"], "chapter": "", "text": cut[already]["text"],
        "vector": space_embed([cut[already]["text"]], dim=codex.space.dim)[0],
    }], collection=local, dtype=codex.space.dtype)

    asked = []

    def embed(texts):
        asked.extend(texts)
        return space_embed(texts, dim=codex.space.dim)

    out = _run(codex, snapshot, root, embed=embed)
    assert cut[already]["text"] not in asked
    assert out.chunks_written == _all_chunks(codex, snapshot, root) - 1


def test_dry_run_writes_no_byte(buildable):
    """07 §7.7: "--dry-run prints the plan … and exits 0 without writing a
    byte or moving one\"."""
    root, snapshot, codex = buildable
    out = _run(codex, snapshot, root, embed=None, dry_run=True)
    assert out.chunks_seen > 0 and out.chunks_written == 0
    assert out.tokens > 0
    assert _stored(root, codex) == {}


def test_a_signal_stops_at_a_batch_and_keeps_what_was_written(buildable):
    """07 §7.7's table: the unit is "the chunks in flight to the encoder".
    `upsert_book` learned this the expensive way — a 5.3-hour document
    written once at the end produced nothing across four restarts."""
    import threading
    root, snapshot, codex = buildable
    stop = threading.Event()
    n = [0]

    def embed(texts):
        n[0] += 1
        if n[0] >= 1:
            stop.set()                      # after the first batch lands
        return space_embed(texts, dim=codex.space.dim)

    total = _all_chunks(codex, snapshot, root)
    out = _run(codex, snapshot, root, embed=embed, stop=stop)
    assert out.interrupted
    assert 0 < out.chunks_written < total
    assert len(_stored(root, codex)) == out.chunks_written

    # a re-run continues rather than restarting: between them the two runs
    # write every chunk exactly once.
    again = _run(codex, snapshot, root,
                 embed=lambda t: space_embed(t, dim=codex.space.dim))
    assert out.chunks_written + again.chunks_written == total
    assert len(_stored(root, codex)) == total


# ---- --delta -------------------------------------------------------------

def test_the_first_snapshots_delta_is_every_shard(buildable):
    """Its whole content is `delta.added`; saying "nothing changed" there
    would embed nothing at all on a fresh chain."""
    _, snapshot, _ = buildable
    assert len(vb.delta_shards(None, snapshot)) == len(snapshot.shards)


def test_a_NEW_shard_is_in_the_delta(buildable):
    """The difference from `snapshot.reissued`, which answers "which
    shards were RE-issued" — so a growing corpus's new shards are not
    among them, and here they are precisely the work to do."""
    _, snapshot, _ = buildable
    older = replace(snapshot, shards=snapshot.shards[:1])
    got = vb.delta_shards(older, snapshot)
    assert len(got) == len(snapshot.shards) - 1
    assert snapshot.shards[0] not in got


def test_an_unchanged_shard_is_not(buildable):
    _, snapshot, _ = buildable
    assert vb.delta_shards(snapshot, snapshot) == []


# ---- the verb ------------------------------------------------------------

def test_the_verb_is_in_the_tree():
    p = louise.build_parser()
    a = p.parse_args(["hold", "vectors", "build", "g", "--dry-run"])
    assert a.fn is not None and a.dry_run is True
    assert p.parse_args(["hold", "vectors", "build", "g",
                         "--delta", "2026-08"]).delta == "2026-08"


def test_the_command_defers_without_an_encoder(buildable, capsys):
    """07 §7.8: `EMBED_URL` unset means `vectors build` defers (exit 3)."""
    root, _, _ = buildable
    assert louise.main(["hold", "vectors", "build", "gutenberg",
                        "--root", str(root)]) == 3
    assert "deferred" in capsys.readouterr().err


def test_the_command_dry_runs_without_an_encoder(buildable, capsys):
    """A plan is not a use of the encoder, so it is not a deferral."""
    root, _, codex = buildable
    assert louise.main(["hold", "vectors", "build", "gutenberg",
                        "--root", str(root), "--dry-run"]) == 0
    assert "would embed" in capsys.readouterr().out
    assert _stored(root, codex) == {}


def test_the_command_refuses_without_a_statement(buildable, capsys):
    root, _, _ = buildable
    (root / "gutenberg" / "state" / "calibration.json").unlink()
    assert louise.main(["hold", "vectors", "build", "gutenberg",
                        "--root", str(root), "--dry-run"]) == 2
    assert "calibration statement" in capsys.readouterr().err
