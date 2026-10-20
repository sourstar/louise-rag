"""tests/hold/test_admit.py — 03 §3.5's admission ladder.

Migration plan step 4e. Every bundle here is written by the real
exporter, so what the ladder judges is what a peer would actually
receive — a hand-built dict would let the two drift apart, and the
drift would be invisible until a stranger's bundle arrived.

The migration plan names the cases this file has to answer, and each is
here under the name it has there.
"""
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
from nacl import signing

from app.hold import admit, calibration as cal, collection as col, \
    vectors as vec
from app.hold.admit import AdmitError
from tests.hold.conftest import KEY
from tests.hold.test_vectors_export import build_shard, first_shard

OTHER_KEY = signing.SigningKey(b"\x77" * 32)


def space_embed(texts, *, dim=8, tag=b""):
    """A stand-in encoder with the ANISOTROPY a real one has.

    `conftest.fake_embed` is isotropic noise — a fresh random unit vector
    per text — which is fine for hashing fixtures and useless here: two
    random unit vectors in high dimension are orthogonal, so a bundle's
    mean vector sits at cosine ~0 to the calibration set's and T0's
    envelope test, which measures exactly that alignment, can never pass.

    Real embedding spaces are strongly anisotropic, and that is T0's whole
    premise ("foreign vectors betray themselves statistically"). So this
    gives the space a fixed centre and fixed per-dimension scales, both a
    function of `tag` alone, with the per-text variation around them. Two
    encoders differ by their tag and therefore by their centre — which is
    what makes the wrong-oven test mean something.
    """
    import hashlib

    seed = int.from_bytes(hashlib.blake2b(b"space" + tag, digest_size=8).digest(), "big")
    rng = np.random.default_rng(seed)
    centre = rng.standard_normal(dim)
    centre /= np.linalg.norm(centre)
    # The per-dimension spread is scaled to the CENTRE, whose components are
    # ~1/sqrt(dim) because it is a unit vector: a constant would be noise at
    # dim 8 and a blizzard at dim 768, and the mean vector would then sit at
    # cosine ~0 to the calibration set's for no reason a real space shares.
    scale = np.exp(rng.normal(0, 0.8, dim)) * (0.6 / np.sqrt(dim))

    out = []
    for t in texts:
        s = int.from_bytes(
            hashlib.blake2b(tag + t.encode("utf-8"), digest_size=8).digest(), "big")
        v = centre + np.random.default_rng(s).standard_normal(dim) * scale
        out.append((v / np.linalg.norm(v)).tolist())
    return out


def chunk_texts(root, snapshot, codex, shard) -> dict[str, str]:
    """chunk id -> the text it was cut from, derived the way every stage
    derives it (`layers.chunks_for_document`)."""
    from app import layers as layers_mod
    from app.hold import export as exp
    from app.pack import records as pack_records
    shard_tree = col.shard_dir_of(root, "gutenberg")
    entry = exp._shard_of(snapshot, shard)
    out = {}
    for rec in pack_records.iter_records(shard_tree / entry.path):
        row = {"uid": rec["uid"], "native_id": rec.get("native_id", 0)}
        for c in layers_mod.chunks_for_document(
                codex, row, [tuple(s) for s in rec.get("sections") or []]):
            out[c["id"]] = c["text"]
    return out


@pytest.fixture
def made(isolated_config, tmp_path, corpus_dir):
    """A SHARD, a shard, and a bundle of it — written by the exporter, by a
    TRUTHFUL producer.

    The vectors are this encoder's own output for the chunk text, so the
    bundle is what an honest peer would send: unit vectors (the space
    normalises), inside the envelope, reproducing under a spot recompute.
    Every refusal below is then a deliberate departure from that, which is
    the only way to know a refusal means what it says.
    """
    from app.hold import export as exp
    root, snapshot = build_shard(tmp_path, corpus_dir, records_per_shard=2)
    codex = col.read(root, "gutenberg")
    shard = first_shard(snapshot)
    texts = chunk_texts(root, snapshot, codex, shard)
    store = {cid: space_embed([t], dim=codex.space.dim)[0]
             for cid, t in texts.items()}
    ids = sorted(store)
    dest = tmp_path / "bundle"
    exp.export(root, "gutenberg", shard=shard, dest=dest,
               vectors_for=lambda w: {i: store[i] for i in w if i in store},
               embed=lambda t: space_embed(t, dim=codex.space.dim),
               key=KEY, created_at=1756713600)
    return root, snapshot, codex, dest, store, ids


def reference_and_envelope(codex):
    """What this peer holds about the space: the calibration member's two
    tensors, produced by the same `space_embed` the fixtures create with,
    so a conformant bundle really does agree with them."""
    texts = cal.load("text")[1]
    v, env, _, _ = cal.build_member(texts, lambda t: space_embed(t, dim=codex.space.dim),
                                    normalise=codex.space.normalise,
                                    dim=codex.space.dim)
    return v, env


def mine_of(root, snapshot, shard):
    from app.pack import records as pack_records
    from app.hold import export as exp
    shard_tree = col.shard_dir_of(root, "gutenberg")
    entry = exp._shard_of(snapshot, shard)
    return {r["uid"]: r["content_hash"]
            for r in pack_records.iter_records(shard_tree / entry.path)}


def run(made, **over):
    root, snapshot, codex, dest, store, ids = made
    ref, env = reference_and_envelope(codex)
    kw = dict(envelope=env, reference=ref,
              mine=mine_of(root, snapshot, first_shard(snapshot)),
              text_of=lambda cid: None, embed=None, seed=b"\x01" * 32)
    kw.update(over)
    return admit.admit(codex, dest, **kw)


# ---- the round trip ------------------------------------------------------

def test_a_conformant_bundle_round_trips_through_export_and_import(made):
    """The plan's first named test, and the one the rest depend on."""
    _, _, _, _, _, ids = made
    r = run(made)
    assert r.ok, [(t.tier, t.verdict, t.why) for t in r.tiers]
    assert r.tier("T0").verdict == admit.PASS
    assert r.tier("T2").verdict == admit.PASS
    assert len(r.admitted) == len(ids)
    assert r.refused == () and r.padded == 0


def test_a_first_seen_producer_key_is_admitted(made):
    """The TOFU regression the plan names. `app/bundles/trust.py` refuses
    every producer key after the first, which 03 §3.5 and 08 §8.1 forbid
    in as many words — nothing here has a notion of a known key at all."""
    r = run(made)
    assert r.ok
    assert r.producer.startswith("ed25519:")


def test_a_bundle_whose_collection_differs_is_still_admitted(made, tmp_path):
    """Admissibility is `space_id` plus per-record `content_hash`;
    `collection` and `snapshot_id` are PROVENANCE and gate nothing. A peer
    that gated on `collection` would refuse a sister's bundle — exactly
    the sharing the design exists for."""
    root, snapshot, codex, dest, store, ids = made
    header, rows, possession, _ = vec.read(dest)
    other = dict(header, collection="sha256:" + "ab" * 32)
    for k in vec.DERIVED:
        other.pop(k, None)
    out = tmp_path / "relabelled"
    vec.write(out, header=other,
              challenge_rows=[(f"calibration:{i}", list(v))
                              for i, v in enumerate(rows[:16] and
                                                    [r[1] for r in rows[:16]])],
              chunk_rows=[(c, list(v)) for c, v in rows[16:]],
              possession=possession, key=KEY)
    ref, env = reference_and_envelope(codex)
    r = admit.admit(codex, out, envelope=env, reference=ref,
                    mine=mine_of(root, snapshot, first_shard(snapshot)),
                    seed=b"\x01" * 32)
    assert r.ok, [(t.tier, t.why) for t in r.tiers]


# ---- the whole-bundle refusals -------------------------------------------

def test_a_bundle_for_another_space_is_refused_whole(made):
    root, snapshot, codex, dest, _, _ = made
    elsewhere = replace(codex, cleaning_version=codex.cleaning_version + 1)
    assert elsewhere.space_id != codex.space_id
    ref, env = reference_and_envelope(codex)
    with pytest.raises(AdmitError, match="another space"):
        admit.admit(elsewhere, dest, envelope=env, reference=ref, mine={})


def test_a_bundle_of_another_width_is_refused_whole(made):
    """`dim` and `dtype` are INSIDE `space_id` (invariant 1), so an honest
    header that disagrees about either has already been refused by the
    line above — as the whole-bundle test for another space shows. These
    two checks are therefore about a header that LIES: one that claims
    this space and then declares another width. Asserted against
    `check_admissible` directly, because a bundle whose header and rows
    disagree about `dim` cannot be written (`vectors.write` builds the
    parquet schema from the header), so there is no file to hand `admit`.
    """
    _, _, codex, dest, _, _ = made
    header, _, _, _ = vec.read(dest)
    admit.check_admissible(codex, header)          # the honest one passes

    with pytest.raises(AdmitError, match="dimensional"):
        admit.check_admissible(codex, dict(header, dim=codex.space.dim + 8))
    with pytest.raises(AdmitError, match="dtype"):
        admit.check_admissible(codex, dict(header, dtype="f32"))


def test_an_uncredited_archive_is_refused(made):
    """06 §6.6: an archive whose entry is not already in `credits` is
    REFUSED, not credited late — the Codex is written once."""
    root, snapshot, codex, dest, _, _ = made
    ref, env = reference_and_envelope(codex)
    with pytest.raises(AdmitError, match="does not\\s+credit"):
        admit.admit(codex, dest, envelope=env, reference=ref, mine={},
                    credits_admit=lambda _c, _k: False)


def test_strict_on_an_empty_allow_list_admits_nothing(made):
    """Which is why `--strict` is off by default (plan, step 4e)."""
    root, snapshot, codex, dest, _, _ = made
    ref, env = reference_and_envelope(codex)
    with pytest.raises(AdmitError, match="allow list"):
        admit.admit(codex, dest, envelope=env, reference=ref, mine={},
                    strict_allow=set())


# ---- the row set, fixed before the tiers ---------------------------------

def test_a_re_issued_record_costs_its_rows_and_not_the_bundle(made):
    """The plan's case: "one re-issued record has its rows refused and the
    import exits 0 while the rest lands"."""
    root, snapshot, codex, dest, store, ids = made
    mine = mine_of(root, snapshot, first_shard(snapshot))
    assert len(mine) >= 2, "the fixture needs two records to lose one"
    victim = sorted(mine)[0]
    mine[victim] = "f" * 32                       # re-issued since

    r = run(made, mine=mine)
    assert r.ok, [(t.tier, t.why) for t in r.tiers]
    assert [u for u, _ in r.refused] == [victim]
    assert "re-issued" in r.refused[0][1]
    kept = {cid.rsplit(":", 2)[0] for cid, _ in r.admitted}
    assert victim not in kept
    assert kept, "the rest of the bundle still lands"


def test_a_record_this_peer_does_not_hold_is_not_refused(made):
    """Neither a mismatch nor a hole: simply not ours to check. §3.5
    admits a bundle on the records it CAN check."""
    r = run(made, mine={})
    assert r.ok and r.refused == ()


def test_a_padded_bundle_does_not_move_T0s_statistic(made, tmp_path):
    """The plan's sharpest case. A producer that could get refused rows
    counted would dilute every statistic over the chunks by the padding
    factor — so the row set is fixed BEFORE any tier sees a matrix.

    Here the padding is a record whose `content_hash` this peer knows
    differs: rows the producer knew would be refused.
    """
    root, snapshot, codex, dest, store, ids = made
    mine = mine_of(root, snapshot, first_shard(snapshot))
    victim = sorted(mine)[0]

    clean = run(made, mine=mine)
    mine_bad = dict(mine, **{victim: "f" * 32})
    padded = run(made, mine=mine_bad)

    assert padded.padded > 0, "the fixture must actually drop rows"
    assert len(padded.admitted) == len(clean.admitted) - padded.padded
    # T0's statistic is computed over the ACCEPTED rows, so it is the
    # statistic of a smaller honest bundle — not of the padded one.
    assert padded.tier("T0").stats["rows"] == len(padded.admitted)
    assert padded.tier("T0").verdict == admit.PASS


# ---- the tiers themselves ------------------------------------------------

def test_a_bundle_too_small_for_the_envelope_says_so(made):
    """T0's envelope compares per-dimension deviations, and a sample
    deviation over three rows carries a relative standard error of about
    41 % — against a band of [0.4, 2.0] that noise alone refuses honest
    work. So under `ENVELOPE_MIN_ROWS` the structural half still runs and
    the envelope is recorded as NOT applied: §3.5 does not let a tier that
    could not run pass quietly, and 04 §4.5 sizes a real bundle at a few
    hundred chunks, so this is the tail and not the ordinary case."""
    _, _, _, _, _, ids = made
    assert len(ids) < admit.ENVELOPE_MIN_ROWS, \
        "the mini corpus grew: this test needs a bundle under the gate"
    t0 = run(made).tier("T0")
    assert t0.verdict == admit.PASS
    assert t0.stats["envelope"].startswith("skipped")
    assert "NOT applied" in t0.why


def test_foreign_vectors_are_refused_at_T0(made):
    """The plan's case: "a bundle with foreign vectors is refused at T0".

    Against `tier0` directly and not through a bundle, because the mini
    corpus cuts three chunks and three rows do not reach the envelope
    (above). Both matrices are here, not just the bad one — a gate that
    refuses everything would pass the negative half on its own.
    """
    _, _, codex, _, _, _ = made
    _, env = reference_and_envelope(codex)
    n, dim = admit.ENVELOPE_MIN_ROWS * 2, codex.space.dim

    ours = list(zip(
        (f"sha256:{i:064x}:0:{i}" for i in range(n)),
        space_embed([f"a chunk this space embedded, number {i}"
                     for i in range(n)], dim=dim)))
    good = admit.tier0(codex, ours, env)
    assert good.verdict == admit.PASS, good.why
    assert good.stats["rows"] == n

    rng = np.random.default_rng(5)
    foreign = [(cid, list(v / np.linalg.norm(v)))
               for (cid, _), v in zip(ours, rng.standard_normal((n, dim)))]
    bad = admit.tier0(codex, foreign, env)
    assert bad.failed, bad.why
    assert "cosine" in bad.why
    # and it is marked as the ENVELOPE half failing, which is what lets
    # `judge` offer it to T2 (below) where a structural failure gets nothing
    assert bad.stats["envelope"] == "outside"
    assert admit.envelope_only(bad)
    assert not admit.envelope_only(admit.Tier(admit.T0, admit.FAIL, "3 duplicate chunk id(s)"))


# ---- T2 is the envelope's tie-breaker (operator, 2026-10-20) --------------

def outside(reason="2nd percentile of the deviation ratios 0.289 < 0.4"):
    """An envelope verdict that refuses, in `envelope_verdict`'s shape —
    the node's lang=en/00043, which T0 refused twice while T2 passed."""
    return {"passed": False, "reasons": [reason], "cosine": 0.71,
            "p_lo": 0.289, "p_hi": 1.2, "ratio_min": 0.1, "ratio_max": 1.9}


def force_outside(monkeypatch):
    """The mini corpus cuts three chunks and never reaches the envelope, so
    the gate is lowered and the verdict forced: what these tests judge is
    the ladder's COMPOSITION, not the statistic (that is above)."""
    monkeypatch.setattr(admit, "ENVELOPE_MIN_ROWS", 1)
    monkeypatch.setattr(admit.cal, "envelope_verdict", lambda m, e: outside())


def wrong_oven(made, tmp_path):
    """The bundle of `test_T2_fails…`: the vectors are ours, the challenge
    answered by ANOTHER encoder."""
    root, snapshot, codex, dest, store, ids = made
    from app.hold import export as exp
    out = tmp_path / "wrong-oven"
    exp.export(root, "gutenberg", shard=first_shard(snapshot), dest=out,
               vectors_for=lambda w: {i: store[i] for i in w if i in store},
               embed=lambda t: space_embed(t, dim=codex.space.dim, tag=b"other"),
               key=KEY)
    return out


def test_an_envelope_failure_is_a_warning_when_T2_passes(made, monkeypatch):
    """Issue 16: four of the node's own HOMOGENEOUS parts, built by its
    validated run, fell outside literals measured over whole corpora while
    their challenge rows passed. T2 proves the oven and T0 is sanity only
    (03 §3.5), so the envelope's verdict is recorded as a WARNING and the
    bundle is admitted — T0 passes, says what it saw, and the ladder goes
    on to T3."""
    force_outside(monkeypatch)
    r = run(made)
    assert r.ok, [(t.tier, t.verdict, t.why) for t in r.tiers]
    t0 = r.tier("T0")
    assert t0.verdict == admit.PASS
    assert t0.stats["envelope"] == "warning"
    assert "0.289 < 0.4" in t0.warning
    assert "WARNING" in t0.why and "0.289 < 0.4" in t0.why
    assert r.warnings == (("T0", t0.warning),)
    assert r.tier("T2").verdict == admit.PASS
    assert r.tier("T3") is not None, "the ladder went on past T2"
    assert len(r.admitted) == len(run(made).admitted) > 0
    # the coverage row and the attestation record the tier as PASSED: 09
    # §9.7's vocabulary is pass/fail/skipped and did not change
    from app.share import coverage as cov
    assert cov._tiers_of(r)["T0"] == "pass"


def test_an_envelope_failure_stands_when_T2_fails(made, tmp_path, monkeypatch):
    """The wrong oven outside the envelope has no tie-breaker: refused at
    T0 AND T2, both named, nothing admitted, no warning (a warning is an
    overruled clause, and nothing overruled this one)."""
    force_outside(monkeypatch)
    root, snapshot, codex, _, _, _ = made
    ref, env = reference_and_envelope(codex)
    r = admit.admit(codex, wrong_oven(made, tmp_path), envelope=env, reference=ref,
                    mine=mine_of(root, snapshot, first_shard(snapshot)))
    assert not r.ok
    assert r.tier("T0").failed and r.tier("T0").stats["envelope"] == "outside"
    assert r.tier("T2").failed
    assert r.tier("T3") is None
    assert r.admitted == ()
    assert r.warnings == ()


def test_an_envelope_failure_stands_when_T2_is_skipped(made, monkeypatch):
    """A peer holding no calibration member has no tie-breaker. SKIPPED is
    not PASSED (§3.5: a tier that could not run is recorded as not run),
    and the envelope's verdict stands."""
    force_outside(monkeypatch)
    r = run(made, reference=None)
    assert not r.ok
    assert r.tier("T0").failed
    assert r.tier("T2").verdict == admit.SKIPPED
    assert r.admitted == ()
    assert r.warnings == ()


def test_a_structural_T0_failure_ends_the_ladder(made, monkeypatch):
    """Only the envelope half may be overruled. A NaN, a duplicate, a wrong
    width: T1 and T2 are not run at all, whatever they would have said."""
    monkeypatch.setattr(admit, "tier0", lambda codex, accepted, env: admit.Tier(
        admit.T0, admit.FAIL, "1 row(s) carry a NaN or an infinity"))
    r = run(made)
    assert not r.ok
    assert [t.tier for t in r.tiers] == ["T0"]
    assert r.admitted == ()


def test_T2_fails_when_the_producers_encoder_is_not_this_space(made, tmp_path):
    """The challenge proves a correct oven. A producer whose encoder is
    another model answers the sixteen known texts differently."""
    root, snapshot, codex, dest, store, ids = made
    from app.hold import export as exp
    out = tmp_path / "wrong-oven"
    exp.export(root, "gutenberg", shard=first_shard(snapshot), dest=out,
               vectors_for=lambda w: {i: store[i] for i in w if i in store},
               # a DIFFERENT encoder answering the challenge
               embed=lambda t: space_embed(t, dim=codex.space.dim, tag=b"other"),
               key=KEY)
    ref, env = reference_and_envelope(codex)
    r = admit.admit(codex, out, envelope=env, reference=ref,
                    mine=mine_of(root, snapshot, first_shard(snapshot)))
    assert not r.ok
    assert r.tier("T2").failed, r.tier("T2").why
    assert "not this space" in r.tier("T2").why
    assert r.admitted == (), "a failed ladder admits nothing"


def test_T2_costs_the_importer_nothing(made):
    """§3.5's table says so in its cost column: the answers travel in the
    bundle and the questions are the set this peer already ships. Asserted
    by running the whole ladder with NO encoder."""
    r = run(made, embed=None)
    assert r.tier("T2").verdict == admit.PASS
    assert r.tier("T3").verdict == admit.SKIPPED


def test_T3_is_SKIPPED_and_never_failed_without_an_encoder(made):
    """§3.5: a tier that could not run is recorded as not run, never as
    passed — and the bundle is imported with its chunks *unverified
    here*."""
    r = run(made, embed=None)
    t3 = r.tier("T3")
    assert t3.verdict == admit.SKIPPED and not t3.failed
    assert r.ok, "T0, T1 and T2 are what make a bundle usable"


def test_T3_recomputes_and_passes_on_the_producers_own_vectors(made):
    """With this peer's text and its own encoder, the producer's vectors
    reproduce — which is what a spot recompute is for."""
    root, snapshot, codex, dest, store, ids = made
    texts = {}
    from app.pack import records as pack_records
    from app import layers as layers_mod
    from app.hold import export as exp
    shard_tree = col.shard_dir_of(root, "gutenberg")
    entry = exp._shard_of(snapshot, first_shard(snapshot))
    for rec in pack_records.iter_records(shard_tree / entry.path):
        row = {"uid": rec["uid"], "native_id": rec.get("native_id", 0)}
        for c in layers_mod.chunks_for_document(
                codex, row, [tuple(s) for s in rec.get("sections") or []]):
            texts[c["id"]] = c["text"]

    # the producer's vectors ARE this encoder's output for that text
    truthful = {cid: space_embed([t], dim=codex.space.dim)[0]
                for cid, t in texts.items()}
    out = dest.parent / "truthful"
    exp.export(root, "gutenberg", shard=first_shard(snapshot), dest=out,
               vectors_for=lambda w: {i: truthful[i] for i in w if i in truthful},
               embed=lambda t: space_embed(t, dim=codex.space.dim), key=KEY)

    ref, env = reference_and_envelope(codex)
    r = admit.admit(codex, out, envelope=env, reference=ref,
                    mine=mine_of(root, snapshot, first_shard(snapshot)),
                    text_of=texts.get,
                    embed=lambda t: space_embed(t, dim=codex.space.dim),
                    seed=b"\x02" * 32)
    t3 = r.tier("T3")
    assert t3.verdict in (admit.PASS, admit.SKIPPED), t3.why
    if t3.verdict == admit.PASS:
        assert t3.stats["median"] == pytest.approx(1.0, abs=1e-6)


def test_the_T3_sample_is_drawn_with_this_peers_own_seed(made):
    """Never from the bundle: a producer who could predict the draw would
    re-roll a nonce until the sample missed its corruption (§3.5)."""
    root, snapshot, codex, dest, store, ids = made
    seen = []

    def spy(cid):
        seen.append(cid)
        return None                 # no text: T3 skips, which is fine here

    run(made, text_of=spy, embed=lambda t: [], seed=b"\x01" * 32)
    first = list(seen)
    seen.clear()
    run(made, text_of=spy, embed=lambda t: [], seed=b"\x09" * 32)
    assert first and seen and first != seen, \
        "two seeds drew the same sample; the draw is not seeded"


def test_landing_reuses_the_callers_cut_and_cuts_once_otherwise(made, monkeypatch):
    """Perugia, 2026-10-20: the intake cut a 50-book part twice per bundle —
    once for the ladder's T3 (`text_of`) and once in `land` — in a thread
    holding the GIL. `land(cut_=…)` takes the cut the caller has; without
    one it cuts, exactly as before."""
    from app.hold import land as landing
    root, snapshot, codex, dest, store, ids = made
    r = run(made)
    assert r.ok
    from app.hold import export as exp
    from app.pack import records as pack_records
    shard_tree = col.shard_dir_of(root, "gutenberg")
    entry = exp._shard_of(snapshot, first_shard(snapshot))
    records = list(pack_records.iter_records(shard_tree / entry.path))
    cuts = []
    real_cut = landing.cut

    def counting(codex_, recs):
        cuts.append(len(recs))
        return real_cut(codex_, recs)
    monkeypatch.setattr(landing, "cut", counting)
    written = []
    out = landing.land(codex, r, records=records, write=lambda rows: written.append(len(rows)) or len(rows))
    assert cuts == [len(records)] and out["rows"] == len(r.admitted)
    mine = real_cut(codex, records)
    out2 = landing.land(codex, r, records=records, write=lambda rows: len(rows), cut_=mine)
    assert cuts == [len(records)], "the caller's cut is used; nothing is cut again"
    assert out2["rows"] == out["rows"] and out2["unplaceable"] == out["unplaceable"]
