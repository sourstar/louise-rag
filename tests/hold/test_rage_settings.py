"""Recommended RAGE settings, the signed file (app/hold/rage_settings.py,
09 §9.19; operator 2026-11-01: "A recommended, non binding, setting shall be
part of a collection", choice B)."""
from __future__ import annotations

import json

import pytest
from nacl import signing

from app.hold import rage_settings as es, envelope as env
from app.hold.codex import CodexError

CID = "sha256:" + "c" * 64
SID = "sha256:" + "5" * 64
ENGINE = "sha256:" + "e" * 64
B70 = es.Card("xe", "8086", "e223")
RX = es.Card("amdgpu", "1002", "7590")
ME = signing.SigningKey(b"\x01" * 32)
PEER = signing.SigningKey(b"\x02" * 32)


def _entry(card=B70, *, batch=16, concurrency=4, time="2026-11-01T20:00:00Z", **kw):
    return es.entry(card, engine_id=ENGINE, batch=batch, concurrency=concurrency,
                    busy="0.82", chunks_per_s="42.0", window_s=600, time=time, **kw)


def _record(shard, key=ME, entry=None, *, cid=CID, sid=SID, time="2026-11-01T20:00:00Z"):
    return es.record(shard, key, collection_id=cid, space_id=sid,
                     new=entry or _entry(), time=time)


def test_a_signers_file_is_named_for_its_key_and_reads_back_verified(tmp_path):
    p = _record(tmp_path)
    assert p == tmp_path / "rage-settings" / (bytes(ME.verify_key).hex() + ".json")
    body = es.read(p)
    assert body["key"] == env.key_text(ME.verify_key)
    assert body["entries"] == [_entry()]
    assert set(body) == es.BODY_KEYS


def test_a_re_measurement_replaces_only_the_signers_own_entry_for_that_card(tmp_path):
    _record(tmp_path)
    _record(tmp_path, entry=_entry(RX, batch=8, concurrency=2))
    _record(tmp_path, entry=_entry(batch=32, concurrency=2, time="2026-11-02T08:00:00Z"),
            time="2026-11-02T08:00:00Z")
    body = es.read(es.path_of(tmp_path, env.key_text(ME.verify_key)))
    by_card = {e["card"]["driver"]: e["settings"] for e in body["entries"]}
    assert by_card == {"xe": {"batch": 32, "concurrency": 2},
                       "amdgpu": {"batch": 8, "concurrency": 2}}
    assert [e["card"]["driver"] for e in body["entries"]] == ["amdgpu", "xe"], "ordered"


def test_two_signers_never_write_one_file(tmp_path):
    a, b = _record(tmp_path, ME), _record(tmp_path, PEER)
    assert a != b
    bodies, refused = es.read_all(tmp_path, collection_id=CID, space_id=SID)
    assert len(bodies) == 2 and refused == []


def test_a_tampered_file_is_set_aside_with_the_reason(tmp_path):
    p = _record(tmp_path)
    envelope = json.loads(p.read_bytes())
    envelope["body"]["entries"][0]["settings"]["batch"] = 64
    p.write_text(json.dumps(envelope))
    bodies, refused = es.read_all(tmp_path, collection_id=CID, space_id=SID)
    assert bodies == [] and "does not verify" in refused[0]


def test_a_file_under_another_signers_name_is_set_aside(tmp_path):
    p = _record(tmp_path)
    p.rename(es.path_of(tmp_path, env.key_text(PEER.verify_key)))
    bodies, refused = es.read_all(tmp_path, collection_id=CID, space_id=SID)
    assert bodies == [] and "another signer" in refused[0]


def test_an_unknown_key_is_refused_even_when_signed(tmp_path):
    """09 §9.18: a field a later version adds must not be strippable."""
    body = {"kind": es.KIND, "version": 1, "key": env.key_text(ME.verify_key),
            "time": "2026-11-01T20:00:00Z", "collection_id": CID, "space_id": SID,
            "entries": [_entry()], "expires": "2027-01-01T00:00:00Z"}
    p = es.path_of(tmp_path, body["key"])
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps(env.sign(es.DOMAIN, body, ME)))
    with pytest.raises(CodexError, match="exactly"):
        es.read(p)


def test_another_collections_or_spaces_file_is_set_aside(tmp_path):
    _record(tmp_path, cid="sha256:" + "d" * 64)
    bodies, refused = es.read_all(tmp_path, collection_id=CID, space_id=SID)
    assert bodies == [] and "another collection or space" in refused[0]


def test_a_signature_under_another_domain_does_not_verify(tmp_path):
    body = {"kind": es.KIND, "version": 1, "key": env.key_text(ME.verify_key),
            "time": "2026-11-01T20:00:00Z", "collection_id": CID, "space_id": SID,
            "entries": [_entry()]}
    p = es.path_of(tmp_path, body["key"])
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps(env.sign(env.CALIBRATION, body, ME)))
    with pytest.raises(CodexError, match="does not verify"):
        es.read(p)


@pytest.mark.parametrize("change, why", [
    (dict(batch=0), "batch"), (dict(batch=2048), "batch"), (dict(concurrency=65), "concurrency"),
    (dict(batch=None, concurrency=None), "settings"),
])
def test_settings_out_of_bounds_are_refused(change, why):
    with pytest.raises(CodexError, match=why):
        _entry(**change)


@pytest.mark.parametrize("card", [es.Card("xe", "0x8086", "e223"), es.Card("xe", "8086", "E223"),
                                  es.Card("", "8086", "e223")])
def test_a_card_type_is_a_driver_and_four_lowercase_hex_digits_each(card):
    with pytest.raises(CodexError, match="card"):
        _entry(card)


def test_measurements_are_decimal_strings():
    with pytest.raises(CodexError, match="decimal string"):
        es.entry(B70, engine_id=ENGINE, batch=16, busy=0.82, time="t")
    with pytest.raises(CodexError, match="engine_id"):
        es.entry(B70, engine_id="e" * 64, batch=16, time="t")


# ---------------------------------------------------------------- choosing

def _bodies(tmp_path):
    _record(tmp_path, ME, _entry(batch=16, time="2026-11-01T10:00:00Z"))
    _record(tmp_path, PEER, _entry(batch=32, time="2026-11-01T12:00:00Z"))
    return es.read_all(tmp_path, collection_id=CID, space_id=SID)[0]


def test_this_nodes_own_measurement_comes_first(tmp_path):
    rec = es.recommend(_bodies(tmp_path), [B70], own=env.key_text(ME.verify_key))
    assert (rec.batch, rec.signer) == (16, env.key_text(ME.verify_key))


def test_else_the_most_recently_measured(tmp_path):
    rec = es.recommend(_bodies(tmp_path), [B70], own=None)
    assert (rec.batch, rec.signer) == (32, env.key_text(PEER.verify_key))
    assert rec.measured["busy"] == "0.82" and rec.engine_id == ENGINE


def test_another_cards_numbers_are_not_this_cards(tmp_path):
    assert es.recommend(_bodies(tmp_path), [RX], own=None) is None
    assert es.recommend(_bodies(tmp_path), [], own=None) is None
