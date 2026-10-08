"""The bundles ledger and the trust store.

The load-bearing properties: a rejection never touches documents.error, an
interrupted import is visible on restart, quarantine can name exactly which
rows to delete, and re-import is a no-op.
"""
import json

import pytest

from app.bundles import ledger, trust


@pytest.fixture(autouse=True)
def no_collection_files(tmp_path, monkeypatch):
    """The hand-built `collection` is the only one: CONFIG_DIR names an
    empty folder, so nothing outside the test (the checkout's own
    collections/, a pre-Codex file now refused) becomes the default."""
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path / "no-config"))


KEY_A = "a" * 64
KEY_B = "b" * 64


def _applying(con, bundle_id="bundle1", producer=KEY_A, **kw):
    args = dict(bundle_id=bundle_id, producer_pubkey=producer, space_id="s" * 32,
                collection="gutenberg", chunk_count=3, signature_hex="f" * 128,
                parquet_sha256="1" * 64, content_digest="2" * 64,
                created_at=1_700_000_000, space_weak=False)
    args.update(kw)
    ledger.record_applying(con, **args)


# ─────────────────────────── ledger lifecycle ───────────────────────────

def test_applying_then_applied(con):
    _applying(con)
    assert ledger.get(con, "bundle1")["state"] == ledger.APPLYING
    assert not ledger.already_applied(con, "bundle1")

    ledger.record_applied(con, "bundle1", {"gutenberg:1": 3}, "t0=ok; isotropy=ok")
    row = ledger.get(con, "bundle1")
    assert row["state"] == ledger.APPLIED
    assert json.loads(row["uids"]) == {"gutenberg:1": 3}
    assert row["chunk_count"] == 3
    assert ledger.already_applied(con, "bundle1")


def test_reimport_is_a_no_op_by_primary_key(con):
    """Invariant 2: re-running an import must not duplicate anything."""
    _applying(con)
    ledger.record_applied(con, "bundle1", {"gutenberg:1": 3}, "")
    _applying(con)
    ledger.record_applied(con, "bundle1", {"gutenberg:1": 3}, "")
    count = con.execute("SELECT COUNT(*) c FROM bundles").fetchone()["c"]
    assert count == 1


def test_interrupted_import_is_swept_on_restart(con):
    _applying(con, bundle_id="crashed")
    _applying(con, bundle_id="fine")
    ledger.record_applied(con, "fine", {"gutenberg:1": 1}, "")

    swept = ledger.sweep_stale(con)
    assert swept == ["crashed"]
    assert ledger.get(con, "crashed")["state"] == ledger.ABORTED
    assert ledger.get(con, "fine")["state"] == ledger.APPLIED
    assert ledger.sweep_stale(con) == []          # idempotent


def test_rejection_is_recorded_without_a_bundle_id(con):
    """A bundle can fail before its id is even computable — a malformed header
    has no verifiable identity."""
    ledger.record_rejected(con, bundle_id=None, producer_pubkey=None,
                           reason="header is not valid UTF-8 JSON")
    rows = ledger.recent(con)
    assert len(rows) == 1
    assert rows[0]["state"] == ledger.REJECTED
    assert "not valid UTF-8" in rows[0]["error"]


def test_rejection_never_writes_to_documents_error(con, collection):
    """worker._next_book filters `error IS NULL` for the rdf and fts stages
    too, so an import failure recorded on the document would remove it from
    the ENTIRE pipeline, permanently. This is the regression guard."""
    con.execute(
        "INSERT INTO documents(uid,source,native_id,path,lang,updated_at) "
        "VALUES('gutenberg:1','gutenberg','1','/x','en',0)")
    ledger.record_rejected(con, bundle_id="bad", producer_pubkey=KEY_A,
                           reason="signature does not verify")
    row = con.execute("SELECT error FROM documents WHERE uid='gutenberg:1'").fetchone()
    assert row["error"] is None


def test_space_weak_flag_is_retained(con):
    """So a later phase can re-sweep bundles admitted while the local model
    hash was unconfirmed."""
    _applying(con, space_weak=True)
    assert ledger.get(con, "bundle1")["space_weak"] == 1


def test_forgery_evidence_is_retained(con):
    """Without the signature and digests, '§6 proof of forgery' is only an
    accusation."""
    _applying(con)
    row = ledger.get(con, "bundle1")
    assert row["signature"] == "f" * 128
    assert row["parquet_sha256"] == "1" * 64
    assert row["content_digest"] == "2" * 64


# ─────────────────────────── provider stats ───────────────────────────

def test_provider_counters_track_outcomes(con):
    _applying(con, bundle_id="b1")
    ledger.record_applied(con, "b1", {"gutenberg:1": 2}, "")
    ledger.record_rejected(con, bundle_id="b2", producer_pubkey=KEY_A, reason="bad")
    provider = trust.get_provider(con, KEY_A)
    assert provider["bundles_accepted"] == 1
    assert provider["bundles_rejected"] == 1


def test_uids_written_by_producer_aggregates_applied_bundles_only(con):
    _applying(con, bundle_id="b1")
    ledger.record_applied(con, "b1", {"gutenberg:1": 2, "gutenberg:2": 3}, "")
    _applying(con, bundle_id="b2")
    ledger.record_applied(con, "b2", {"gutenberg:2": 1}, "")
    _applying(con, bundle_id="b3")      # left applying, never applied
    assert ledger.uids_written_by(con, KEY_A) == {"gutenberg:1": 2, "gutenberg:2": 4}


# ─────────────────────────── trust on first use ───────────────────────────

def test_first_key_seen_is_trusted_and_recorded(con):
    assert trust.check_admission(con, KEY_A) == "first-use"
    provider = trust.get_provider(con, KEY_A)
    assert provider["trusted"] == 1
    assert "first use" in provider["note"]
    assert trust.check_admission(con, KEY_A) == "trusted"    # sticky


def test_a_second_conflicting_key_is_refused(con):
    trust.check_admission(con, KEY_A)
    with pytest.raises(trust.BundleUntrusted, match="not trusted"):
        trust.check_admission(con, KEY_B)


def test_an_explicitly_trusted_second_key_is_accepted(con):
    trust.check_admission(con, KEY_A)
    trust.trust_key(con, KEY_B, note="operator added")
    assert trust.check_admission(con, KEY_B) == "trusted"
    assert set(trust.trusted_keys(con)) == {KEY_A, KEY_B}


def test_first_use_can_be_disabled(con):
    with pytest.raises(trust.BundleUntrusted, match="first-use trust is disabled"):
        trust.check_admission(con, KEY_A, allow_first_use=False)
    assert trust.get_provider(con, KEY_A) is None    # nothing recorded on refusal


# ─────────────────────────── quarantine ───────────────────────────

def test_quarantine_refuses_and_names_the_rows_to_delete(con):
    trust.check_admission(con, KEY_A)
    _applying(con, bundle_id="b1")
    ledger.record_applied(con, "b1", {"gutenberg:1": 2, "gutenberg:9": 5}, "")

    affected = trust.quarantine_key(con, KEY_A, "recomputed vectors did not match")
    assert affected == {"gutenberg:1": 2, "gutenberg:9": 5}
    with pytest.raises(trust.BundleUntrusted, match="quarantined"):
        trust.check_admission(con, KEY_A)


def test_quarantine_beats_trust_and_beats_first_use(con):
    """Quarantine is checked before every other branch, so no path can bypass
    it — including the first-use path on a node that trusts nothing else."""
    trust.quarantine_key(con, KEY_A, "bad")
    with pytest.raises(trust.BundleUntrusted, match="quarantined"):
        trust.check_admission(con, KEY_A)
    assert trust.trusted_keys(con) == []


def test_quarantine_is_reversible_only_by_naming_the_key(con):
    trust.quarantine_key(con, KEY_A, "mistake")
    trust.trust_key(con, KEY_A, note="cleared after review")
    assert trust.check_admission(con, KEY_A) == "trusted"
    assert trust.get_provider(con, KEY_A)["quarantined"] == 0


# ─────────────────────────── per-chunk verification ───────────────────────────

def test_imported_chunks_are_marked_and_local_ones_are_not(con):
    """Absence means locally embedded — that is what keeps this table
    proportional to imported content rather than to the whole corpus."""
    ledger.mark_imported(con, ["gutenberg:1:0:1", "gutenberg:1:1:1"],
                          "gutenberg:1", KEY_A, "b1")
    states = ledger.verification_states(
        con, ["gutenberg:1:0:1", "gutenberg:1:1:1", "gutenberg:2:0:1"])
    assert states["gutenberg:1:0:1"]["state"] == "imported"
    assert "gutenberg:2:0:1" not in states       # never imported -> local


def test_verification_outcome_is_recorded(con):
    ledger.mark_imported(con, ["gutenberg:1:0:1"], "gutenberg:1", KEY_A, "b1")
    ledger.set_verified(con, "gutenberg:1:0:1", 0.9997, passed=True)
    row = ledger.verification_states(con, ["gutenberg:1:0:1"])["gutenberg:1:0:1"]
    assert row["state"] == "verified" and row["cosine"] == pytest.approx(0.9997)

    ledger.set_verified(con, "gutenberg:1:0:1", 0.42, passed=False)
    row = ledger.verification_states(con, ["gutenberg:1:0:1"])["gutenberg:1:0:1"]
    assert row["state"] == "failed"


def test_local_reembed_clears_stale_import_provenance(con):
    """Otherwise a re-embedded document would keep claiming it came from a
    producer it no longer has anything to do with."""
    ledger.mark_imported(con, ["gutenberg:1:0:1"], "gutenberg:1", KEY_A, "b1")
    ledger.clear_verification(con, "gutenberg:1")
    assert ledger.verification_states(con, ["gutenberg:1:0:1"]) == {}


def test_marking_imported_twice_updates_rather_than_duplicates(con):
    ledger.mark_imported(con, ["gutenberg:1:0:1"], "gutenberg:1", KEY_A, "b1")
    ledger.set_verified(con, "gutenberg:1:0:1", 0.99, passed=True)
    ledger.mark_imported(con, ["gutenberg:1:0:1"], "gutenberg:1", KEY_B, "b2")
    row = ledger.verification_states(con, ["gutenberg:1:0:1"])["gutenberg:1:0:1"]
    assert row["state"] == "imported"       # re-import resets the verdict
    assert row["producer"] == KEY_B
    assert row["cosine"] is None
