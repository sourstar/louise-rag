"""tests/hold/test_check.py — 03 §3.2's plan, run and attested.

Migration plan step 5. Every check here runs against a REAL drag built by
`hold records build` and `hold snapshot create`, because a check that
passed on a hand-built fixture would prove that the fixture agrees with
the checker and nothing about the collection.
"""
import json
from dataclasses import replace

import pytest

from app.hold import check as chk, codex as cx, collection as col, \
    drag as drag_mod, envelope as env, records as rec, snapshot as snap
from app.hold.check import CheckError
from tests.hold.conftest import KEY, NOW, PLATFORM
from tests.hold.test_admit import made, space_embed  # noqa: F401
from tests.hold.test_vectors_export import build_drag, first_shard

SEED = b"\x05" * 32


@pytest.fixture
def checkable(isolated_config, tmp_path, corpus_dir, monkeypatch):
    monkeypatch.delenv("EMBED_URL", raising=False)
    root, snapshot = build_drag(tmp_path, corpus_dir, records_per_shard=2)
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
    p = root / "gutenberg" / "drag" / drag_mod.VALIDATION_PATH
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
    (root / "gutenberg" / "drag" / drag_mod.VALIDATION_PATH).write_bytes(
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

def test_roots_passes_on_an_untouched_drag(checkable):
    root, snapshot, codex = checkable
    o = _run(root, codex, snapshot, ["roots"]).outcome("roots")
    assert o.result == chk.PASS, o.why
    assert o.detail["failed"] == []
    assert o.seed is None, "exhaustive and deterministic: §9.8 wants null"
    assert len(o.shards) == len(snapshot.shards)


def test_roots_catches_a_shard_whose_bytes_moved(checkable):
    root, snapshot, codex = checkable
    p = root / "gutenberg" / "drag" / snapshot.shards[0].path
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
    (root / "gutenberg" / "drag" / "LICENSE-TERMS").write_bytes(b"anything")
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
    p = root / "gutenberg" / "drag" / \
        drag_mod.snapshot_sig_path(snapshot.snapshot_id)
    doc = dict(json.loads(p.read_bytes()), snapshot="sha256:" + "ab" * 32)
    p.write_bytes(cx.canonical_json(doc))
    o = _run(root, codex, snapshot, ["signature"]).outcome("signature")
    assert o.result == chk.FAIL and "this snapshot is" in o.why


def test_a_forged_signature_fails(checkable):
    root, snapshot, codex = checkable
    snap.sign_snapshot(root, "gutenberg", snapshot, KEY, now=NOW)
    p = root / "gutenberg" / "drag" / \
        drag_mod.snapshot_sig_path(snapshot.snapshot_id)
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
         drag_mod._hex(codex.collection_id))
    assert len(list(d.glob("*.json"))) == 1


def test_one_attestation_per_completed_check_and_never_per_run(checkable):
    """A run that failed one check of six still proves the other five, and
    a peer that could only attest whole runs would publish nothing
    whenever one check was inconclusive."""
    root, snapshot, codex = checkable
    p = root / "gutenberg" / "drag" / "LICENSE-TERMS"
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
    assert body["kind"] == "louise-attestation" and body["version"] == 1
    assert body["collection_id"] == codex.collection_id
    assert body["snapshot_id"] == snapshot.snapshot_id
    assert body["snapshot_hash"] == snapshot.hash
    assert body["check"] == "roots" and body["result"] == "pass"
    assert body["seed"] is None, "an exact check publishes no seed"
    assert body["platform_image"] == PLATFORM
    assert body["encoder_image"] is None, "roots is not a statistical check"
    assert body["time"] == NOW
    assert set(body) == {
        "kind", "version", "collection_id", "snapshot_id", "snapshot_hash",
        "check", "seed", "shards", "result", "detail", "platform_image",
        "encoder_image", "encoder_weights", "key", "time"}


def test_a_statistical_attestation_names_the_encoder(checkable):
    root, snapshot, codex = checkable
    o = chk.Outcome("vectors-sample", chk.PASS, {"chunks": 1}, SEED, ())
    body = env.verify(env.ATTESTATION, chk.attestation(
        o, codex, snapshot, KEY, platform_image=PLATFORM, now=NOW))
    assert body["encoder_image"] == codex.space.encoder.image
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
    (root / "gutenberg" / "drag" / "LICENSE-TERMS").write_bytes(b"anything")
    assert louise.main(["hold", "check", "gutenberg", "--root", str(root),
                        "--checks", "license", "--seed", SEED.hex()]) == 1


def test_the_command_writes_one_attestation_per_completed_check(
        checkable, monkeypatch, capsys):
    from app import louise
    root, snapshot, codex = checkable
    monkeypatch.setattr("app.bundles.keys.load_signing_key", lambda *a, **k: KEY)
    assert louise.main(["hold", "check", "gutenberg", "--root", str(root),
                        "--checks", "roots,centres", "--json",
                        "--seed", SEED.hex()]) == 0
    doc = json.loads(capsys.readouterr().out)
    by = {c["check"]: c for c in doc["checks"]}
    assert by["roots"]["attestation"] and by["centres"]["attestation"] is None
    d = (root / "gutenberg" / "state" / "attestations" /
         drag_mod._hex(codex.collection_id))
    assert len(list(d.glob("*.json"))) == 1


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


# ---- records-vs-origin, and the gap it found -----------------------------

def test_records_vs_origin_is_inconclusive_because_no_record_is_locatable(
        checkable):
    """09 §9.5 makes `origin` a required record field — "the locator of
    this record's own origin bytes" — and `pack/records.py::RECORD_KEYS`
    does not carry it, so no record this platform has written says where
    its origin unit is. Half of invariant 11 is implemented.

    The check reports that rather than passing: a pass here would certify
    records nobody could ever verify against their source, which is the
    artefact the invariant calls the worst we could ship. See
    docs/notes/records-origin-gap.md.
    """
    root, snapshot, codex = checkable
    o = _run(root, codex, snapshot, ["records-vs-origin"],
             sample="1").outcome("records-vs-origin")
    assert o.result == chk.INCONCLUSIVE
    assert o.detail["origin"] == "unlocatable"
    assert o.detail["records"] > 0
    assert "09 §9.5" in o.why and "records-origin-gap" in o.why


def test_the_record_writer_still_omits_origin(checkable):
    """The gap itself, pinned where a reader will meet it. When `origin`
    is added to `RECORD_KEYS` this test fails and both it and the check
    above are what change — deliberately, so the fix cannot land while
    the check still reports the defect."""
    from app.pack import records as pack_records
    assert "origin_hash" in pack_records.RECORD_KEYS
    assert "origin" not in pack_records.RECORD_KEYS, \
        "`origin` landed: update check_records_vs_origin to re-fetch and " \
        "re-parse, and retire docs/notes/records-origin-gap.md"


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
                                                         capsys):
    from app import louise
    root, snapshot, codex = checkable
    monkeypatch.setattr("app.bundles.keys.load_signing_key", lambda *a, **k: KEY)
    assert louise.main(["hold", "records", "check", "gutenberg", "--root",
                        str(root), "--json", "--origin",
                        "--seed", SEED.hex()]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert [c["check"] for c in doc["checks"]] == \
        ["roots", "records", "records-vs-origin", "origin-seal"]
