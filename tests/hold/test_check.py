"""tests/hold/test_check.py — 03 §3.2's plan, run and attested.

Migration plan step 5. Every check here runs against a REAL SHARD built by
`hold records build` and `hold snapshot create`, because a check that
passed on a hand-built fixture would prove that the fixture agrees with
the checker and nothing about the collection.
"""
import json
from dataclasses import replace

import pytest

from app.hold import check as chk, codex as cx, collection as col, \
    shard as shard_mod, envelope as env, records as rec, snapshot as snap
from app.hold.check import CheckError
from tests.hold.conftest import KEY, NOW, PLATFORM
from tests.hold.test_admit import made, space_embed  # noqa: F401
from tests.hold.test_vectors_export import build_shard, first_shard

SEED = b"\x05" * 32


@pytest.fixture
def checkable(isolated_config, tmp_path, corpus_dir, monkeypatch):
    monkeypatch.delenv("EMBED_URL", raising=False)
    root, snapshot = build_shard(tmp_path, corpus_dir, records_per_shard=2)
    codex = col.read(root, "gutenberg")
    return root, snapshot, codex


def _run(root, codex, snapshot, checks, **kw):
    return chk.run(root, "gutenberg", codex, snapshot, seed=SEED,
                   checks=checks, **kw)


# ---- the plan ------------------------------------------------------------

def test_validation_json_must_agree_with_the_codex(checkable):
    """03 §3.2: "`hold check` refuses a copy where the two differ". The
    Codex is the plan; a copy that may differ is a place to publish a
    weaker plan than the identity covers."""
    root, snapshot, codex = checkable
    p = root / "gutenberg" / "shard" / shard_mod.VALIDATION_PATH
    assert p.is_file(), "records build writes it"
    assert chk.plan_of(root, "gutenberg", codex) == codex.validation.checks

    weaker = dict(codex.validation.to_dict(), checks=["roots"])
    p.write_bytes(cx.canonical_json(weaker))
    with pytest.raises(CheckError, match="differs from the Codex"):
        chk.plan_of(root, "gutenberg", codex)


def test_a_plan_naming_an_unknown_check_is_refused(checkable):
    """"A `checks` entry that names no row of this table is refused at
    `hold collection create` and at `hold check` alike\"."""
    root, snapshot, codex = checkable
    bad = replace(codex, validation=replace(
        codex.validation, checks=("roots", "vibes")))
    (root / "gutenberg" / "shard" / shard_mod.VALIDATION_PATH).write_bytes(
        cx.canonical_json(bad.validation.to_dict()))
    with pytest.raises(CheckError, match="no row of"):
        chk.plan_of(root, "gutenberg", bad)


def test_figures_is_a_plan_check(checkable):
    """It is a row of 03 §3.2's table and was missing from `PLAN_CHECKS`
    until 2026-09-10 — so a collection with plates could not name the
    check that validates them."""
    assert "figures" in cx.PLAN_CHECKS
    assert "figures" in chk.CHECKS


def test_every_row_of_the_table_has_a_check(checkable):
    """A plan naming a check nobody built must get INCONCLUSIVE with a
    reason, not a KeyError and not silence."""
    assert set(cx.PLAN_CHECKS) <= set(chk.CHECKS)


def test_an_unbuilt_check_is_inconclusive_with_its_reason(checkable):
    root, snapshot, codex = checkable
    o = _run(root, codex, snapshot, ["centres"]).outcome("centres")
    assert o.result == chk.INCONCLUSIVE
    assert "not built" in o.why and o.detail == {}


# ---- the sample ----------------------------------------------------------

def test_the_run_flag_raises_the_plans_fraction_and_cannot_lower_it(checkable):
    """07 §7.3."""
    _, _, codex = checkable
    assert chk.fraction_for(codex, "records") == "0.05"
    assert chk.fraction_for(codex, "records", "0.5") == "0.5"
    assert chk.fraction_for(codex, "records", "0.01") == "0.05"


def test_a_plan_without_an_entry_gets_3_2s_default(checkable):
    _, _, codex = checkable
    bare = replace(codex, validation=replace(codex.validation, sample={}))
    assert chk.fraction_for(bare, "records") == chk.DEFAULT_FRACTION


# ---- roots ---------------------------------------------------------------

def test_roots_passes_on_an_untouched_shard(checkable):
    root, snapshot, codex = checkable
    o = _run(root, codex, snapshot, ["roots"]).outcome("roots")
    assert o.result == chk.PASS, o.why
    assert o.detail["failed"] == []
    assert o.seed is None, "exhaustive and deterministic: §9.8 wants null"
    assert len(o.shards) == len(snapshot.shards)


def test_roots_catches_a_shard_whose_bytes_moved(checkable):
    root, snapshot, codex = checkable
    p = root / "gutenberg" / "shard" / snapshot.shards[0].path
    p.write_bytes(p.read_bytes() + b"\x00")
    o = _run(root, codex, snapshot, ["roots"]).outcome("roots")
    assert o.result == chk.FAIL
    assert o.detail["failed"] == [f"{snapshot.shards[0].slice}/"
                                  f"{snapshot.shards[0].n:05d}"]


# ---- records -------------------------------------------------------------

def test_records_passes_and_reports_what_it_covered(checkable):
    root, snapshot, codex = checkable
    o = _run(root, codex, snapshot, ["records"], sample="1").outcome("records")
    assert o.result == chk.PASS, o.why
    assert o.detail["records"] > 0 and o.detail["failed"] == []
    assert o.seed == SEED and len(o.shards) == len(snapshot.shards)
    assert all(s.startswith(codex.collection_id + "/") for s in o.shards)


# ---- chunks: the check `chunks_root` exists for --------------------------

def test_chunks_passes_on_the_codexs_own_cut(checkable):
    root, snapshot, codex = checkable
    o = _run(root, codex, snapshot, ["chunks"], sample="1").outcome("chunks")
    assert o.result == chk.PASS, o.why


def test_a_chunker_that_moves_boundaries_without_changing_the_count_is_CAUGHT(
        checkable, monkeypatch):
    """The test that justifies `chunks_root` (migration plan, step 5).

    A count alone cannot see a chunker that cut the same NUMBER of pieces
    at different places — and the vectors would then be of different text
    under the same ids, which is the drift nothing else notices.
    """
    root, snapshot, codex = checkable
    from app import layers as layers_mod
    real = layers_mod.chunks_for_document

    def shifted(collection, row, sections):
        out = real(collection, row, sections)
        # same ids, same count, one character of text moved
        return [dict(c, text=(c["text"][1:] + c["text"][:1])) for c in out]

    monkeypatch.setattr(layers_mod, "chunks_for_document", shifted)
    o = _run(root, codex, snapshot, ["chunks"], sample="1").outcome("chunks")
    assert o.result == chk.FAIL
    assert any("chunks_root" in f for f in o.detail["failed"])
    assert not any("chunks=" in f for f in o.detail["failed"]), \
        "the count was unchanged, which is the point"


# ---- license and signature ----------------------------------------------

def test_license_passes_for_a_green_collection_with_no_terms(checkable):
    root, snapshot, codex = checkable
    o = _run(root, codex, snapshot, ["license"]).outcome("license")
    assert o.result == chk.PASS and "green" in o.why


def test_a_terms_file_nothing_pins_is_a_failure(checkable):
    """One of the two is wrong, and a terms text nothing pins is a terms
    text anyone may replace."""
    root, snapshot, codex = checkable
    (root / "gutenberg" / "shard" / "LICENSE-TERMS").write_bytes(b"anything")
    o = _run(root, codex, snapshot, ["license"]).outcome("license")
    assert o.result == chk.FAIL and "records no terms hash" in o.why


def test_an_unsigned_snapshot_is_inconclusive_not_a_failure(checkable):
    """09 §9.4 holds an unsigned snapshot rather than refusing it, and 03
    §3.1 has a word for "nothing was proved": inconclusive."""
    root, snapshot, codex = checkable
    o = _run(root, codex, snapshot, ["signature"]).outcome("signature")
    assert o.result == chk.INCONCLUSIVE and "unsigned" in o.why


def test_signature_verifies_the_sidecar_over_this_snapshots_hash(checkable):
    root, snapshot, codex = checkable
    snap.sign_snapshot(root, "gutenberg", snapshot, KEY, now=NOW)
    o = _run(root, codex, snapshot, ["signature"]).outcome("signature")
    assert o.result == chk.PASS, o.why
    assert o.detail["keys"] == [env.key_text(KEY.verify_key)]


def test_a_sidecar_covering_another_snapshot_is_dropped_not_failed_silently(
        checkable):
    """"a signature whose `snapshot` does not equal the file's hash is
    dropped" (09 §9.4) — it is a signature about another snapshot that
    landed in the wrong file, and calling it a forgery would be a verdict
    the bytes do not support. The CHECK still fails, because this copy's
    snapshot then carries none."""
    root, snapshot, codex = checkable
    snap.sign_snapshot(root, "gutenberg", snapshot, KEY, now=NOW)
    p = root / "gutenberg" / "shard" / \
        shard_mod.snapshot_sig_path(snapshot.snapshot_id)
    doc = dict(json.loads(p.read_bytes()), snapshot="sha256:" + "ab" * 32)
    p.write_bytes(cx.canonical_json(doc))
    o = _run(root, codex, snapshot, ["signature"]).outcome("signature")
    assert o.result == chk.FAIL and "this snapshot is" in o.why


def test_a_forged_signature_fails(checkable):
    root, snapshot, codex = checkable
    snap.sign_snapshot(root, "gutenberg", snapshot, KEY, now=NOW)
    p = root / "gutenberg" / "shard" / \
        shard_mod.snapshot_sig_path(snapshot.snapshot_id)
    doc = json.loads(p.read_bytes())
    doc["signatures"][0]["sig"] = "ab" * 64
    p.write_bytes(cx.canonical_json(doc))
    o = _run(root, codex, snapshot, ["signature"]).outcome("signature")
    assert o.result == chk.FAIL


def test_a_sidecar_is_a_set_two_peers_merge_by_union(checkable):
    """§9.4: "Two peers merge sidecars by union". A body signed whole
    could not gain a signature without invalidating the ones already
    there, which is why this is the one signed object that is not §9.2's
    envelope."""
    from nacl import signing as nacl_signing
    root, snapshot, codex = checkable
    other = nacl_signing.SigningKey(b"\x42" * 32)
    snap.sign_snapshot(root, "gutenberg", snapshot, KEY, now=NOW)
    doc = snap.sign_snapshot(root, "gutenberg", snapshot, other, now=NOW)
    assert len(doc["signatures"]) == 2
    keys = [s["key"] for s in doc["signatures"]]
    assert keys == sorted(keys), "§9.4 sorts by (key, time)"
    ok, bad = snap.sidecar_keys(doc, snapshot.hash)
    assert sorted(ok) == sorted(keys) and bad == []

    # and signing again with the same key at the same instant adds nothing
    again = snap.sign_snapshot(root, "gutenberg", snapshot, KEY, now=NOW)
    assert len(again["signatures"]) == 2


def test_the_sidecar_signature_is_over_the_raw_hash_bytes(checkable):
    """§9.4: `sig` is over `"louise/snapshot/v1" ‖ 0x00 ‖ <the 32 raw
    bytes of snapshot>` — NOT §9.2's canonical-JSON envelope construction,
    and a signature made the other way verifies against neither."""
    _, snapshot, _ = checkable
    msg = snap.sig_message(snapshot.hash)
    assert msg == env.SNAPSHOT.encode() + b"\x00" + \
        bytes.fromhex(snapshot.hash.split(":", 1)[1])
    assert len(msg) - len(env.SNAPSHOT) - 1 == 32


# ---- the encoder-dependent checks ---------------------------------------

def test_calibration_without_an_encoder_is_inconclusive(checkable):
    root, snapshot, codex = checkable
    o = _run(root, codex, snapshot, ["calibration"]).outcome("calibration")
    assert o.result == chk.INCONCLUSIVE and "encoder" in o.why


def test_vectors_sample_over_an_empty_table_is_inconclusive(checkable):
    root, snapshot, codex = checkable
    o = _run(root, codex, snapshot, ["vectors-sample"],
             vectors_for=lambda _i: {},
             embed=lambda t: space_embed(t, dim=codex.space.dim)
             ).outcome("vectors-sample")
    assert o.result == chk.INCONCLUSIVE


# ---- attestations --------------------------------------------------------

def test_an_inconclusive_check_writes_no_attestation(checkable):
    """09 §9.8 in as many words."""
    root, snapshot, codex = checkable
    report = _run(root, codex, snapshot, ["roots", "centres"])
    written = chk.write_attestations(root, "gutenberg", report, codex,
                                     snapshot, KEY, platform_image=PLATFORM,
                                     now=NOW)
    assert set(written) == {"roots"}
    d = (root / "gutenberg" / "state" / "attestations" /
         shard_mod._hex(codex.collection_id))
    assert len(list(d.glob("*.json"))) == 1


def test_one_attestation_per_completed_check_and_never_per_run(checkable):
    """A run that failed one check of six still proves the other five, and
    a peer that could only attest whole runs would publish nothing
    whenever one check was inconclusive."""
    root, snapshot, codex = checkable
    p = root / "gutenberg" / "shard" / "LICENSE-TERMS"
    p.write_bytes(b"anything")                       # makes `license` FAIL
    report = _run(root, codex, snapshot, ["roots", "license", "centres"])
    assert not report.ok
    written = chk.write_attestations(root, "gutenberg", report, codex,
                                     snapshot, KEY, platform_image=PLATFORM,
                                     now=NOW)
    assert set(written) == {"roots", "license"}


def test_the_attestation_is_9_8s_body_and_verifies(checkable):
    root, snapshot, codex = checkable
    report = _run(root, codex, snapshot, ["roots"])
    doc = chk.attestation(report.outcomes[0], codex, snapshot, KEY,
                          platform_image=PLATFORM, now=NOW)
    body = env.verify(env.ATTESTATION, doc)
    # version 3: "version 2 is this body with `encoder_image` … in place of
    # `encoder_engine`" (09 §9.8, the engine amendment)
    assert body["kind"] == "louise-attestation" and body["version"] == 3
    assert body["collection_id"] == codex.collection_id
    assert body["snapshot_id"] == snapshot.snapshot_id
    assert body["snapshot_hash"] == snapshot.hash
    assert body["check"] == "roots" and body["result"] == "pass"
    assert body["seed"] is None, "an exact check publishes no seed"
    assert body["platform_image"] == PLATFORM
    assert body["encoder_engine"] is None, "roots is not a statistical check"
    assert body["time"] == NOW
    assert set(body) == {
        "kind", "version", "collection_id", "snapshot_id", "snapshot_hash",
        "check", "seed", "shards", "result", "detail", "platform_image",
        "encoder_engine", "encoder_weights", "key", "time"}


def test_a_statistical_attestation_names_the_encoder(checkable):
    root, snapshot, codex = checkable
    o = chk.Outcome("vectors-sample", chk.PASS, {"chunks": 1}, SEED, ())
    body = env.verify(env.ATTESTATION, chk.attestation(
        o, codex, snapshot, KEY, platform_image=PLATFORM, now=NOW))
    assert body["encoder_engine"] == codex.space.encoder.engine.engine_id
    assert body["encoder_weights"] == codex.space.encoder.weights
    assert body["seed"] == SEED.hex()


# ---- the report ----------------------------------------------------------

def test_the_json_report_is_9_8s_object(checkable):
    root, snapshot, codex = checkable
    doc = _run(root, codex, snapshot, ["roots", "centres"]).to_json()
    assert set(doc) == {"collection_id", "snapshot_id", "snapshot_hash",
                        "checks"}
    assert [c["check"] for c in doc["checks"]] == ["roots", "centres"]
    assert doc["checks"][1]["result"] == "inconclusive"
    assert doc["checks"][1]["attestation"] is None


def test_an_inconclusive_check_does_not_fail_the_run(checkable):
    """03 §3.1: inconclusive is "neither a failure nor a pass"."""
    root, snapshot, codex = checkable
    assert _run(root, codex, snapshot, ["roots", "centres", "labels"]).ok


# ---- the command ---------------------------------------------------------

def test_the_verb_is_in_the_tree():
    from app import louise
    a = louise.build_parser().parse_args(["hold", "check", "g", "--json"])
    assert a.fn is not None and a.json is True


def test_the_command_runs_the_plan_and_exits_0(checkable, capsys, monkeypatch,
                                               tmp_path):
    from app import louise
    root, snapshot, codex = checkable
    monkeypatch.setattr("app.bundles.keys.load_signing_key", lambda *a, **k: KEY)
    assert louise.main(["hold", "check", "gutenberg", "--root", str(root),
                        "--seed", SEED.hex()]) == 0
    out = capsys.readouterr().out
    assert "PASS" in out
    for c in codex.validation.checks:
        assert c in out


def test_the_command_exits_1_when_a_check_failed(checkable, capsys, monkeypatch):
    from app import louise
    root, snapshot, codex = checkable
    monkeypatch.setattr("app.bundles.keys.load_signing_key", lambda *a, **k: KEY)
    (root / "gutenberg" / "shard" / "LICENSE-TERMS").write_bytes(b"anything")
    assert louise.main(["hold", "check", "gutenberg", "--root", str(root),
                        "--checks", "license", "--seed", SEED.hex()]) == 1


def test_the_command_writes_one_attestation_per_completed_check(
        checkable, monkeypatch, capsys):
    from app import louise
    root, snapshot, codex = checkable
    monkeypatch.setattr("app.bundles.keys.load_signing_key", lambda *a, **k: KEY)
    monkeypatch.setenv("LOUISE_PLATFORM_IMAGE", codex.toolchain.platform_image)
    assert louise.main(["hold", "check", "gutenberg", "--root", str(root),
                        "--checks", "roots,centres", "--json",
                        "--seed", SEED.hex()]) == 0
    doc = json.loads(capsys.readouterr().out)
    by = {c["check"]: c for c in doc["checks"]}
    assert by["roots"]["attestation"] and by["centres"]["attestation"] is None
    d = (root / "gutenberg" / "state" / "attestations" /
         shard_mod._hex(codex.collection_id))
    assert len(list(d.glob("*.json"))) == 1


def test_an_attestation_names_the_image_that_ran_it(checkable, monkeypatch, capsys):
    """09 §9.8: `platform_image` is "the image the verifier ran". It was the
    Codex's, whatever ran — so on 2026-09-21 a new image's equivalence
    failure read as the collection failing its own toolchain (03 §3.4)."""
    from app import louise
    root, snapshot, codex = checkable
    monkeypatch.setattr("app.bundles.keys.load_signing_key", lambda *a, **k: KEY)
    ran = "sha256:" + "2e" * 32
    monkeypatch.setenv("LOUISE_PLATFORM_IMAGE", ran)
    assert louise.main(["hold", "check", "gutenberg", "--root", str(root),
                        "--checks", "roots", "--seed", SEED.hex()]) == 0
    d = (root / "gutenberg" / "state" / "attestations" /
         shard_mod._hex(codex.collection_id))
    body, = [json.loads(p.read_bytes()) for p in d.glob("*.json")]
    body = body.get("body", body)
    assert body["platform_image"] == ran != codex.toolchain.platform_image


def test_a_process_that_cannot_name_its_image_attests_nothing(checkable, monkeypatch, capsys):
    from app import louise
    root, snapshot, codex = checkable
    monkeypatch.setattr("app.bundles.keys.load_signing_key", lambda *a, **k: KEY)
    monkeypatch.delenv("LOUISE_PLATFORM_IMAGE", raising=False)
    assert louise.main(["hold", "check", "gutenberg", "--root", str(root),
                        "--checks", "roots", "--seed", SEED.hex()]) == 0
    assert "unattested" in capsys.readouterr().err
    d = (root / "gutenberg" / "state" / "attestations" /
         shard_mod._hex(codex.collection_id))
    assert not list(d.glob("*.json")) if d.exists() else True


def test_a_peer_with_no_key_still_checks(checkable, monkeypatch, capsys):
    """07 §7.8: "`NODE_KEY_PATH` unset means this peer cannot sign … import,
    check and search work without it"; the report counts on this peer
    alone."""
    from app import louise

    def no_key(*_a, **_k):
        raise FileNotFoundError("NODE_KEY_PATH is unset")

    monkeypatch.setattr("app.bundles.keys.load_signing_key", no_key)
    root, snapshot, codex = checkable
    assert louise.main(["hold", "check", "gutenberg", "--root", str(root),
                        "--checks", "roots", "--seed", SEED.hex()]) == 0
    assert "unsigned" in capsys.readouterr().err


# ---- records-vs-origin ---------------------------------------------------

def test_every_record_now_carries_its_origin_locator(checkable):
    """09 §9.5's `origin`, added 2026-09-10 by operator decision. It is
    half of invariant 11 — `origin_hash` is the hash of the origin unit
    and this locates that unit — and without it a peer that RECEIVED a
    shard could never re-fetch its source.

    Relative and posix, never an absolute local path: the record travels,
    and this machine's `/mnt/…` locates nothing on the peer that gets it.
    """
    from app.pack import records as pack_records
    root, snapshot, codex = checkable
    assert "origin" in pack_records.RECORD_KEYS
    for r in pack_records.iter_records(
            root / "gutenberg" / "shard" / snapshot.shards[0].path):
        o = r["origin"]
        assert o and not o.startswith("/") and ":\\" not in o
        assert "\\" not in o, "09 §9.5's locator is a posix path"
        assert r["origin_hash"], "the other half of invariant 11"


def test_records_vs_origin_without_an_origin_is_inconclusive_never_a_pass(
        checkable):
    """03 §3.2's `needs` column is "the origin, the platform image". A
    check that passed for want of an origin would certify the copy
    against itself."""
    root, snapshot, codex = checkable
    o = _run(root, codex, snapshot, ["records-vs-origin"],
             sample="1").outcome("records-vs-origin")
    assert o.result == chk.INCONCLUSIVE
    assert o.detail["origin"] == "not given"
    assert o.detail["records"] > 0


def test_records_vs_origin_reparses_the_origin_byte_for_byte(checkable,
                                                             corpus_dir):
    """"reproduces every record line of the shard byte for byte — the
    decompressed lines in order, not merely the same `records_root`".

    Comparing `records_root` would not do it: that root is over the
    records' `content_hash` values, and `content_hash` is over title and
    sections alone — so `creators`, `date`, `subjects`, `keys`, `extra`
    and `origin` could all differ from what the origin says while the root
    still matched.
    """
    from app import louise
    root, snapshot, codex = checkable
    o = _run(root, codex, snapshot, ["records-vs-origin"], sample="1",
             reparse=louise._reparse_from(codex, "gutenberg", corpus_dir)
             ).outcome("records-vs-origin")
    assert o.result == chk.PASS, o.why
    assert o.detail["origin"] == "reachable" and o.detail["failed"] == []
    assert o.detail["records"] > 0


def _drifted(real, victim_uid):
    """A re-parse that differs from the record while the origin bytes are
    the ones it was parsed from (`origin_hash` unchanged): what a parser
    that no longer reproduces its own records looks like."""
    import json as _json

    def reparse(uid, locator):
        line = real(uid, locator)
        if uid != victim_uid:
            return line
        r = _json.loads(line)
        r["title"] = (r.get("title") or "") + " (drifted)"
        return _json.dumps(r, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return reparse


def test_a_record_its_own_origin_bytes_no_longer_reproduce_FAILS(checkable, corpus_dir):
    """And it names the record, which is what §9.8's detail asks for. The
    origin unit is the very bytes the record was parsed from (same
    `origin_hash`), so the difference is the record's or the parser's."""
    from app import louise
    root, snapshot, codex = checkable
    victim = next(iter(_records_of(root, snapshot)))
    real = louise._reparse_from(codex, "gutenberg", corpus_dir)
    o = _run(root, codex, snapshot, ["records-vs-origin"], sample="1",
             reparse=_drifted(real, victim["uid"])).outcome("records-vs-origin")
    assert o.result == chk.FAIL, o.why
    assert victim["uid"] in o.detail["failed"] and o.detail["changed"] == []


def test_a_record_whose_origin_was_revised_is_changed_at_the_origin_not_failed(
        checkable, corpus_dir):
    """03 §3.2, amended 2026-10-11 (operator's yes): the origin's text was
    revised after the build, so the record cannot be reproduced from it and
    is no evidence against the record either (gutenberg: 17 of 4,055 sampled
    records, which failed the check under every image, the Codex's own
    included). Named as changed; a large share says nothing (below)."""
    from app import louise
    root, snapshot, codex = checkable
    records = _all_records(root, snapshot)
    victim = records[0]
    txt = next((corpus_dir / str(victim["native_id"])).glob("*.txt*"))
    _change_the_text(txt)
    o = _run(root, codex, snapshot, ["records-vs-origin"], sample="1",
             reparse=louise._reparse_from(codex, "gutenberg", corpus_dir)
             ).outcome("records-vs-origin")
    assert victim["uid"] in o.detail["changed"] and o.detail["failed"] == [], o.why
    one_in = len({r["uid"] for r in records})
    if 1 > chk.CHANGED_AT_ORIGIN_SHARE * one_in:
        assert o.result == chk.INCONCLUSIVE and "proves nothing" in o.why
    else:
        assert o.result == chk.PASS and "1 changed at the origin" in o.why


def test_too_many_records_changed_at_the_origin_is_inconclusive(checkable, corpus_dir, monkeypatch):
    """A record whose `origin_hash` no longer matches is either revised
    upstream or written with a hash that never matched; past a share of the
    sample the check cannot tell, so it passes nothing — and a few pass."""
    import json as _json
    from app import louise
    root, snapshot, codex = checkable
    real = louise._reparse_from(codex, "gutenberg", corpus_dir)
    uids = sorted({r["uid"] for r in _all_records(root, snapshot)})

    def revised(chosen):
        def reparse(uid, locator):
            line = real(uid, locator)
            if uid not in chosen:
                return line
            r = _json.loads(line)
            r["origin_hash"], r["title"] = "ee" * 16, (r.get("title") or "") + " (rev.)"
            return _json.dumps(r, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        return reparse
    o = _run(root, codex, snapshot, ["records-vs-origin"], sample="1",
             reparse=revised(set(uids))).outcome("records-vs-origin")
    assert o.result == chk.INCONCLUSIVE and o.detail["origin"] == "changed", o.why
    monkeypatch.setattr(chk, "CHANGED_AT_ORIGIN_SHARE", 1.0)
    o = _run(root, codex, snapshot, ["records-vs-origin"], sample="1",
             reparse=revised({uids[0]})).outcome("records-vs-origin")
    assert o.result == chk.PASS and o.detail["changed"] == [uids[0]], o.why


def test_an_origin_that_is_not_there_is_inconclusive(checkable, tmp_path):
    """"nothing is wrong and nothing was done" — an unreachable origin is
    03 §3.1's third outcome, not a verdict about the records."""
    from app import louise
    root, snapshot, codex = checkable
    empty = tmp_path / "no-origin"
    empty.mkdir()
    o = _run(root, codex, snapshot, ["records-vs-origin"], sample="1",
             reparse=louise._reparse_from(codex, "gutenberg", empty)
             ).outcome("records-vs-origin")
    assert o.result == chk.INCONCLUSIVE
    assert o.detail["origin"] == "unreachable"


def _records_of(root, snapshot):
    from app.pack import records as pack_records
    return list(pack_records.iter_records(
        root / "gutenberg" / "shard" / snapshot.shards[0].path))



def test_records_check_includes_records_and_nothing_else(checkable, monkeypatch,
                                                         capsys):
    """07 §7.3: "`records check` is `hold check --checks roots,records[,
    records-vs-origin][,origin-seal]` and nothing else — `records` … is
    the only local check that proves what is inside a shard rather than
    that the file matches the snapshot, and leaving it out made `--sample
    F` on this command affect nothing unless `--origin` was given."
    """
    from app import louise
    root, snapshot, codex = checkable
    monkeypatch.setattr("app.bundles.keys.load_signing_key", lambda *a, **k: KEY)
    assert louise.main(["hold", "records", "check", "gutenberg", "--root",
                        str(root), "--json", "--seed", SEED.hex(),
                        "--sample", "1"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert [c["check"] for c in doc["checks"]] == ["roots", "records"]
    assert doc["checks"][1]["detail"]["records"] > 0


def test_records_check_origin_adds_the_two_origin_checks(checkable, monkeypatch,
                                                         capsys, corpus_dir):
    from app import louise
    root, snapshot, codex = checkable
    monkeypatch.setattr("app.bundles.keys.load_signing_key", lambda *a, **k: KEY)
    assert louise.main(["hold", "records", "check", "gutenberg", "--root",
                        str(root), "--json", "--origin", str(corpus_dir), "--seal",
                        "--sample", "1", "--seed", SEED.hex()]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert [c["check"] for c in doc["checks"]] == \
        ["roots", "records", "records-vs-origin", "origin-seal"], \
        "--origin adds the re-parse and --seal the seal check, each its own flag (2026-10-07)"
    by = {c["check"]: c for c in doc["checks"]}
    assert by["records-vs-origin"]["result"] == "pass", \
        "the origin was given, so the re-parse ran"
    assert by["origin-seal"]["result"] == "inconclusive", "an open collection carries no seals"
    assert louise.main(["hold", "records", "check", "gutenberg", "--root", str(root), "--json",
                        "--origin", str(corpus_dir), "--sample", "1", "--seed", SEED.hex()]) == 0
    assert [c["check"] for c in json.loads(capsys.readouterr().out)["checks"]] == \
        ["roots", "records", "records-vs-origin"]


def test_license_passes_when_the_terms_are_the_codexs(checkable):
    """THE YELLOW PATH, which nothing covered — and which could not pass.

    The Codex stores 09 §9.2's id form, `"sha256:" + hexdigest`, and that
    is what `collection.write_terms` compares against when it writes the
    member. This check computed a BARE digest, so `got != want` for every
    yellow and red collection there has ever been: invariant 9's check,
    structurally unable to succeed.

    Live on pubmed 2026-09-12, with the two halves of one identical hash
    side by side in its own failure message: "LICENSE-TERMS hashes to
    77d605f1c9080f8b… and the Codex pins sha256:77d605f1c…".
    """
    import hashlib
    from dataclasses import replace
    root, snapshot, codex = checkable
    body = b"The terms of the origin, byte for byte.\n"
    (root / "gutenberg" / "shard" / "LICENSE-TERMS").write_bytes(body)
    yellow = replace(codex, license=replace(
        codex.license, cls="yellow",
        terms_sha256="sha256:" + hashlib.sha256(body).hexdigest()))

    o = _run(root, yellow, snapshot, ["license"]).outcome("license")
    assert o.result == chk.PASS, o.why

    # and a byte that moved is still caught
    (root / "gutenberg" / "shard" / "LICENSE-TERMS").write_bytes(body + b" ")
    o = _run(root, yellow, snapshot, ["license"]).outcome("license")
    assert o.result == chk.FAIL and "byte-exact" in o.why


def test_an_unreadable_origin_is_inconclusive_not_a_fidelity_failure(checkable):
    """"Differs from its origin" and "could not be re-parsed" are not the
    same finding, and conflating them is the most damaging wrong answer
    this check can give.

    Live 2026-09-12: a run with the origin mounted READ-ONLY reported
    "2,028,614 record(s) re-parsed from the origin, 1,640,709 differing".
    Not one of them differed — the pubmed plugin writes a parse cache
    beside the origin, the mount refused it with errno 30, and every
    OSError went onto the `failed` list. An operator reading that had every
    reason to believe 1.6M records had drifted from PubMed, and the
    records were perfect.
    """
    root, snapshot, codex = checkable

    def read_only(uid, locator):
        raise OSError(30, "Read-only file system")

    o = _run(root, codex, snapshot, ["records-vs-origin"],
             reparse=read_only).outcome("records-vs-origin")
    assert o.result == chk.INCONCLUSIVE, o.why
    assert o.detail["failed"] == [] and o.detail["errored"] > 0
    assert "nothing was proved" in o.why and "Read-only" in o.why


def test_a_real_difference_still_fails_even_beside_unreadable_records(checkable):
    """The distinction cuts one way only: an unreadable record must not
    mask a record that genuinely drifted."""
    root, snapshot, codex = checkable
    calls = {"n": 0}

    def one_bad(uid, locator):
        calls["n"] += 1
        if calls["n"] == 1:
            return b'{"uid":"not the line that was written"}\n'
        raise OSError(30, "Read-only file system")

    o = _run(root, codex, snapshot, ["records-vs-origin"],
             reparse=one_bad).outcome("records-vs-origin")
    assert o.result == chk.FAIL, o.why
    assert len(o.detail["failed"]) == 1
    assert o.detail["errored"] > 0
    assert "unreadable" in o.why


def test_a_crashing_check_is_inconclusive_and_the_others_still_run(checkable,
                                                                    monkeypatch):
    """A checker that crashed proved NOTHING, so it is inconclusive with
    its reason (03 §3.1) — never FAIL, because a defect in the checker is
    not a defect in the collection, and this is the report an operator
    decides to replace a collection on.

    And never the whole run: an attestation is per check (09 §9.8), so one
    unreadable file must not deny the other eight results. That is the
    same mistake the worker's round loop made with a malformed TOML.
    """
    root, snapshot, codex = checkable

    def boom(_ctx):
        raise OSError(5, "Input/output error")

    monkeypatch.setitem(chk.CHECKS, "roots", boom)
    report = _run(root, codex, snapshot, ["roots", "license"])
    assert report.outcome("roots").result == chk.INCONCLUSIVE
    assert "the check itself failed" in report.outcome("roots").why
    assert "Input/output error" in report.outcome("roots").why
    assert report.outcome("license").result == chk.PASS, \
        "the rest of the plan still ran"


def test_a_broken_plan_is_still_raised_not_reported_as_a_check(checkable):
    """The distinction: a plan that names an unknown check is the
    operator's error about the plan, not one check's verdict."""
    root, snapshot, codex = checkable
    with pytest.raises(CheckError, match="no such check"):
        _run(root, codex, snapshot, ["vibes"])


# ---- what one origin check may read of the origin --------------------------------------

def _all_records(root, snapshot) -> list[dict]:
    from app.pack import records as pack_records
    return [r for s in snapshot.shards for r in pack_records.iter_records(
        root / "gutenberg" / "shard" / s.path)]


def _unit_sizes(size: int):
    """Every record of the fixture is its own origin file (a Gutenberg book):
    the unit is the locator's file part, and each weighs `size` bytes."""
    return lambda locator: (locator.partition("#")[0], size)


def test_an_origin_check_reads_no_more_of_the_origin_than_the_bound(checkable, corpus_dir):
    """Operator, 2026-09-20: "10.5 GiB are too much, limit the test to some
    hundred MB". 5 % of PubMed's shards, sliced by year, reach into about 249
    of its baseline files — 10.5 GiB to fetch for every check on every peer.
    The shard sample stays what the plan says; within it the origin FILES are
    ranked by the same seeded rule and read up to the bound, and every sampled
    record of a file that is read is still compared byte for byte."""
    from app import louise
    from app.hold import codex as cx
    root, snapshot, codex = checkable
    reparse = louise._reparse_from(codex, "gutenberg", corpus_dir)
    asked: list[str] = []

    def counted(uid, locator):
        asked.append(locator.partition("#")[0])
        return reparse(uid, locator)

    records = _all_records(root, snapshot)
    units = sorted({r["origin"].partition("#")[0] for r in records})
    assert len(units) >= 3, "the fixture has several origin files"
    each = 100 << 20                                 # 100 MiB a file: two fit in 256 MiB
    o = _run(root, codex, snapshot, ["records-vs-origin"], sample="1", reparse=counted,
             origin_unit=_unit_sizes(each)).outcome("records-vs-origin")
    assert o.result == chk.PASS, o.why
    ranked = cx.sample(SEED, len(units), iter(units))
    assert sorted(set(asked)) == sorted(ranked[:2]), \
        "the first two by the seeded rule, and nothing else was read"
    assert o.detail["origin_units"] == 2 and o.detail["origin_units_sampled"] == len(units)
    assert o.detail["origin_bytes"] == 2 * each
    assert o.detail["origin_bytes_bound"] == chk.ORIGIN_BYTES == 256 << 20
    assert o.detail["compared"] == len(asked) < o.detail["records"]
    assert "2 of the" in o.why and "256 MiB" in o.why


def test_the_first_origin_file_is_always_read_whatever_it_weighs(checkable, corpus_dir):
    """A check that read nothing would prove nothing: one file bigger than the
    bound is still one file, and the report says what it weighed."""
    from app import louise
    root, snapshot, codex = checkable
    o = _run(root, codex, snapshot, ["records-vs-origin"], sample="1",
             reparse=louise._reparse_from(codex, "gutenberg", corpus_dir),
             origin_unit=_unit_sizes(10 << 30)).outcome("records-vs-origin")
    assert o.result == chk.PASS, o.why
    assert o.detail["origin_units"] == 1 and o.detail["origin_bytes"] == 10 << 30


def test_a_run_may_raise_the_bound_and_never_lower_it(checkable, corpus_dir):
    from app import louise
    root, snapshot, codex = checkable
    reparse = louise._reparse_from(codex, "gutenberg", corpus_dir)
    each = 100 << 20
    lowered = _run(root, codex, snapshot, ["records-vs-origin"], sample="1", reparse=reparse,
                   origin_unit=_unit_sizes(each), origin_bytes=1).outcome("records-vs-origin")
    assert lowered.detail["origin_bytes_bound"] == 256 << 20 and lowered.detail["origin_units"] == 2
    raised = _run(root, codex, snapshot, ["records-vs-origin"], sample="1", reparse=reparse,
                  origin_unit=_unit_sizes(each),
                  origin_bytes=1 << 40).outcome("records-vs-origin")
    assert raised.detail["origin_units"] == raised.detail["origin_units_sampled"]
    assert raised.detail["compared"] == raised.detail["records"]


def test_a_difference_in_a_file_that_is_read_still_fails(checkable, corpus_dir):
    from app import louise
    from app.hold import codex as cx
    root, snapshot, codex = checkable
    records = _all_records(root, snapshot)
    units = sorted({r["origin"].partition("#")[0] for r in records})
    first = cx.sample(SEED, 1, iter(units))[0]
    victim = next(r for r in records if r["origin"].partition("#")[0] == first)
    real = louise._reparse_from(codex, "gutenberg", corpus_dir)
    o = _run(root, codex, snapshot, ["records-vs-origin"], sample="1",
             reparse=_drifted(real, victim["uid"]),
             origin_unit=_unit_sizes(200 << 20)).outcome("records-vs-origin")
    assert o.result == chk.FAIL and victim["uid"] in o.detail["failed"], o.why


def test_the_command_weighs_the_origin_files_it_is_given(checkable, corpus_dir):
    """`--origin DIR`: a file's size is the file's; one that is not there has
    none, and its records are reported as not there, as before."""
    from app import louise
    root, snapshot, codex = checkable
    unit_of = louise._origin_unit_from(codex, "gutenberg", corpus_dir)
    record = _records_of(root, snapshot)[0]
    unit, size = unit_of(record["origin"])
    assert not unit.startswith("/") and "#" not in unit
    assert size == (corpus_dir / unit).stat().st_size > 0
    assert unit_of("no/such/file.txt#1")[1] is None
    assert louise._origin_unit_from(codex, "gutenberg", None) is None

    o = _run(root, codex, snapshot, ["records-vs-origin"], sample="1",
             reparse=louise._reparse_from(codex, "gutenberg", corpus_dir),
             origin_unit=unit_of).outcome("records-vs-origin")
    assert o.result == chk.PASS, o.why
    assert o.detail["origin_units"] == o.detail["origin_units_sampled"], \
        "a few small books are far inside the bound: everything is compared"
    assert o.detail["compared"] == o.detail["records"]


def test_an_origin_file_is_weighed_once_however_many_records_point_into_it(
        checkable, corpus_dir, monkeypatch):
    """A COST test. Measured on the node (pubmed, 2026-09-20): 1,998,732
    sampled records point into 651 origin files, and weighing the file again
    for every record took 51 s — longer than reading the shards (34 s)."""
    from app import louise, plugin_io
    root, snapshot, codex = checkable
    weighed: list[str] = []
    real = plugin_io.origin_unit

    def counting(plugin, origin, base):
        weighed.append(origin)
        return real(plugin, origin, base)
    monkeypatch.setattr(plugin_io, "origin_unit", counting)
    unit_of = louise._origin_unit_from(codex, "gutenberg", corpus_dir)
    record = _records_of(root, snapshot)[0]
    file_part = record["origin"].partition("#")[0]
    answers = {unit_of(f"{file_part}#{member}") for member in range(500)}
    assert len(weighed) == 1 and len(answers) == 1, "500 records of one file: one stat"


# ---- anchor (03 §3.7; step 10) ---------------------------------------------

def test_anchor_is_judged_from_the_document_last_fetched(checkable):
    """An open collection has none; a certified one is inconclusive until
    `hold key anchor` fetched, then anchored or stale pass and a mismatch
    fails — the check itself fetches nothing."""
    from app.hold import anchor
    from app.hold.codex import Certifier
    root, snapshot, codex = checkable
    o = _run(root, codex, snapshot, ["anchor"]).outcome("anchor")
    assert o.result == chk.INCONCLUSIVE and "open collection" in o.why
    certified = replace(codex, publication="certified", content="manifest-only",
                        certifier=Certifier(env.key_text(KEY.verify_key), "https://x.example/.well-known/louise-certifier.json"))
    o = _run(root, certified, snapshot, ["anchor"]).outcome("anchor")
    assert o.result == chk.INCONCLUSIVE and "hold key anchor" in o.why
    doc = {"kind": "louise-certifier", "version": 1, "collections": [certified.collection_id],
           "keys": [{"key": env.key_text(KEY.verify_key), "valid_from": "2026-01-01T00:00:00Z", "revoked_at": None}]}
    anchor.record(root, "gutenberg", certified, document=anchor.parse(doc), now=1_790_000_000.0)
    o = _run(root, certified, snapshot, ["anchor"]).outcome("anchor")
    assert o.result == chk.PASS and o.detail == {"state": "anchored"}, o.why
    anchor.record(root, "gutenberg", certified, document=anchor.parse({**doc, "collections": []}), now=1_790_000_000.0)
    o = _run(root, certified, snapshot, ["anchor"]).outcome("anchor")
    assert o.result == chk.FAIL and o.detail == {"state": "mismatch"} and "this collection" in o.why


def test_a_record_whose_origin_unit_moved_and_parses_the_same_is_named_moved_not_failed(checkable, corpus_dir):
    """03 §3.2, amended 2026-10-07 (operator's yes): the mirror rewrote half
    of a sample of gutenberg's book folders on 2026-10-05, every parsed
    record unchanged, and every one of them failed — so the new image could
    not be proven equivalent, though it reproduced each record's content.
    A line identical but for `origin_hash` is MOVED: named, not failed. A
    line that differs anywhere else as well is CHANGED AT THE ORIGIN (amended
    2026-10-11), never moved."""
    import json as _json
    from app import louise
    root, snapshot, codex = checkable
    real = louise._reparse_from(codex, "gutenberg", corpus_dir)
    victim = next(iter(_records_of(root, snapshot)))["uid"]

    def rewritten(uid, locator):
        line = real(uid, locator)
        if uid != victim:
            return line
        r = _json.loads(line)
        r["origin_hash"] = "ff" * 16
        return _json.dumps(r, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    o = _run(root, codex, snapshot, ["records-vs-origin"], sample="1", reparse=rewritten).outcome("records-vs-origin")
    assert o.result == chk.PASS, o.why
    assert o.detail["moved"] == [victim] and o.detail["failed"] == []
    assert "1 moved" in o.why

    def altered(uid, locator):
        line = real(uid, locator)
        if uid != victim:
            return line
        r = _json.loads(line)
        r["origin_hash"], r["title"] = "ff" * 16, (r.get("title") or "") + " (rev.)"
        return _json.dumps(r, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    o = _run(root, codex, snapshot, ["records-vs-origin"], sample="1", reparse=altered).outcome("records-vs-origin")
    assert o.detail["changed"] == [victim] and o.detail["moved"] == [] and o.detail["failed"] == []
    assert o.result != chk.FAIL, o.why


def _change_the_text(txt):
    """A paragraph INSIDE the book. Appended after the end marker, as these
    tests did until 2026-10-07, it is boilerplate the cleaner drops (invariant
    11 (b)): the record did not change, and the tests failed it only on
    `origin_hash` — which is now the MOVED case, not a failure."""
    data = txt.read_bytes()
    i = data.index(b"*** END OF")
    txt.write_bytes(data[:i] + b"An inserted paragraph, inside the book.\n\n" + data[i:])


def test_a_claim_is_reproduced_on_the_named_shard_alone(checkable, corpus_dir):
    """03 §3.6 / migration step 9: a received deterministic claim names a
    shard; `--shards` runs the check on it alone, in place of the sample."""
    from app import louise
    root, snapshot, codex = checkable
    names = sorted(f"{s.slice}/{s.n:05d}" for s in snapshot.shards)
    one = names[0]
    report = chk.run(root, "gutenberg", codex, snapshot, seed=SEED, checks=["chunks", "records-vs-origin"],
                     reparse=louise._reparse_from(codex, "gutenberg", corpus_dir), only_shards=[one])
    for check in ("chunks", "records-vs-origin"):
        o = report.outcome(check)
        assert o.result == chk.PASS and [s.split("/", 1)[1] for s in o.shards] == [one], (check, o.shards, o.why)


def test_the_origin_check_serialises_only_the_records_it_compares(checkable, corpus_dir, monkeypatch):
    """A COST test (2026-10-08): the one-pass check held every sampled line
    until the bound had picked its files — pubmed's 2 million, 13 GB, to
    compare a few thousand. Now nothing is kept between the passes but the
    origin units, and a line is made only for a record that is compared."""
    from app import louise
    from app.pack import records as pack_records
    root, snapshot, codex = checkable
    made = []
    real = pack_records.canonical_line
    monkeypatch.setattr(pack_records, "canonical_line", lambda r, **kw: made.append(r["uid"]) or real(r, **kw))
    o = _run(root, codex, snapshot, ["records-vs-origin"], sample="1",
             reparse=louise._reparse_from(codex, "gutenberg", corpus_dir),
             origin_unit=_unit_sizes(100 << 20)).outcome("records-vs-origin")
    assert o.result == chk.PASS, o.why
    # each compared record is serialised twice — the copy's line and the
    # re-parse's — and a record of a file not read, never
    assert len(made) == 2 * o.detail["compared"] and o.detail["compared"] < o.detail["records"]
