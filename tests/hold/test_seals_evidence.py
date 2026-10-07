"""tests/hold/test_seals_evidence.py — the rest of 03 §3.7's evidence
(migration step 10): a XAdES seal captured detached and validated offline;
revocation responses embedded, given and fetched on the budget, kept only
for the chain they are about, and validated HARD-FAIL once an entry
carries any; the trusted list's own signature verified in archival mode
against the LOTL held beside it; and LIVE mode — the sealed bytes hashed,
their digest recomputed, the path validated now against the current lists
and fresh responses — with every outside answer given by a mock transport."""
import base64
import hashlib
from dataclasses import replace
from datetime import timedelta

import httpx
import pytest
from asn1crypto import cms, core, ocsp, x509 as ax
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import AuthorityInformationAccessOID, NameOID

from app.hold import check as chk, revocation as rev, seals, seals_live
from app.hold.codex import Certifier
from tests.hold import xades_sign as xs
from tests.hold.test_check import SEED, checkable  # noqa: F401
from tests.hold.test_seals import CONTENT, T0, _der, a_p7m, a_signed_pdf, pki, signed_lists  # noqa: F401

OCSP_URL, CRL_URL = "http://ocsp.test/ocsp", "http://crl.test/ca.crl"
CAPTURED = T0 + timedelta(days=31)
NOW = T0 + timedelta(days=40)


def a_leaf(pki, *, serial: int, aia: bool = True, cdp: bool = False):
    key = rsa.generate_private_key(65537, 2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"Signer {serial}")])
    b = (x509.CertificateBuilder().subject_name(name).issuer_name(pki["ca"].subject).public_key(key.public_key())
         .serial_number(serial).not_valid_before(T0).not_valid_after(T0 + timedelta(days=400))
         .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
         .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=True, key_encipherment=False,
                                      data_encipherment=False, key_agreement=False, key_cert_sign=False,
                                      crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
         .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(pki["ca"].public_key()), critical=False))
    if aia:
        b = b.add_extension(x509.AuthorityInformationAccess([x509.AccessDescription(
            AuthorityInformationAccessOID.OCSP, x509.UniformResourceIdentifier(OCSP_URL))]), critical=False)
    if cdp:
        b = b.add_extension(x509.CRLDistributionPoints([x509.DistributionPoint(
            full_name=[x509.UniformResourceIdentifier(CRL_URL)], relative_name=None, reasons=None,
            crl_issuer=None)]), critical=False)
    return {**pki, "leaf": b.sign(pki["ca_key"], hashes.SHA256()), "leaf_key": key}


def an_ocsp(pki, cert, *, when, revoked: bool = False, serial: int | None = None) -> bytes:
    issuer = ax.Certificate.load(_der(pki["ca"]))
    c = ax.Certificate.load(_der(cert))
    cert_id = ocsp.CertId({"hash_algorithm": {"algorithm": "sha1"}, "issuer_name_hash": issuer.subject.sha1,
                           "issuer_key_hash": issuer.public_key.sha1,
                           "serial_number": serial if serial is not None else c.serial_number})
    status = (ocsp.CertStatus(name="revoked", value=ocsp.RevokedInfo({"revocation_time": T0,
                                                                     "revocation_reason": "key_compromise"}))
              if revoked else ocsp.CertStatus(name="good", value=core.Null()))
    tbs = ocsp.ResponseData({"responder_id": ocsp.ResponderId(name="by_key", value=issuer.public_key.sha1),
                             "produced_at": when,
                             "responses": [ocsp.SingleResponse({"cert_id": cert_id, "cert_status": status,
                                                                "this_update": when - timedelta(hours=1),
                                                                "next_update": when + timedelta(days=7)})]})
    sig = pki["ca_key"].sign(tbs.dump(), padding.PKCS1v15(), hashes.SHA256())
    basic = ocsp.BasicOCSPResponse({"tbs_response_data": tbs, "signature_algorithm": {"algorithm": "sha256_rsa"},
                                    "signature": sig})
    return ocsp.OCSPResponse({"response_status": "successful",
                              "response_bytes": {"response_type": "basic_ocsp_response", "response": basic}}).dump()


def with_crls(p7m: bytes, *ocsp_der: bytes) -> bytes:
    """The envelope with OCSP responses embedded in its `crls` field, as a
    CAdES-X-L signer puts them; the signature is over the signed attributes,
    so it still verifies."""
    ci = cms.ContentInfo.load(p7m)
    sd = ci["content"]
    choices = [cms.RevocationInfoChoice(name="other", value=cms.OtherRevocationInfoFormat({
        "other_rev_info_format": "ocsp_response", "other_rev_info": ocsp.OCSPResponse.load(d)})) for d in ocsp_der]
    fields = {k: sd[k] for k in ("version", "digest_algorithms", "encap_content_info", "certificates",
                                 "signer_infos")}
    fields["crls"] = cms.RevocationInfoChoices(choices)
    return cms.ContentInfo({"content_type": "signed_data", "content": cms.SignedData(fields)}).dump()


def a_sealed_xml(pki, **kw) -> bytes:
    act = b'<atto xmlns="urn:gazzetta:test"><titolo>Legge 1/2026</titolo><testo>Art. 1. Il testo.</testo></atto>'
    return xs.sign(act, pki["leaf_key"], pki["leaf"], extra_certs=[pki["ca"]], **kw)


TL = {"lotl_sha256": "sha256:" + "0" * 64, "tl_sha256": "sha256:" + "1" * 64, "url": "https://tl.test/TSL-IT.xml"}


def test_a_xades_seal_is_captured_detached_and_validates_offline(pki, tmp_path):
    sealed = tmp_path / "atto.xml"
    data = a_sealed_xml(pki)
    sealed.write_bytes(data)
    e = seals.capture(sealed, format="xades", trusted_list=TL, now=CAPTURED.timestamp())
    seals.validate_entry(e)
    seal = base64.b64decode(e["seal"]["b64"])
    assert seal.startswith(b"<ds:Signature") and b"Il testo" not in seal, "the signature alone, detached"
    assert e["content_sha256"] == "sha256:" + seals.content_digest("xades", data).hex()
    assert "Test Signer" in e["signer"]["subject"] and len(e["chain"]) == 2
    ca = _der(pki["ca"])
    got = seals.validate(e, anchors=[ca])
    assert got["ok"] and "soft-fail" in got["why"], got["why"]
    assert not seals.validate(e, anchors=[_der(pki["other"])])["ok"]
    assert "content_sha256" in seals.validate(dict(e, content_sha256="sha256:" + "ab" * 32), anchors=[ca])["why"]
    tampered = base64.b64encode(seal.replace(b"2026-02-01", b"2026-02-02")).decode()
    assert "does not verify" in seals.validate(dict(e, seal={"b64": tampered}), anchors=[ca])["why"]
    other = tmp_path / "sha512.xml"
    other.write_bytes(a_sealed_xml(pki, digest=xs.SHA512))
    with pytest.raises(seals.SealError, match="content_sha256"):
        seals.capture(other, format="xades", trusted_list=TL)


def test_revocation_embedded_kept_for_its_chain_and_hard_fail(pki, tmp_path):
    signer = a_leaf(pki, serial=41)                 # declares its responder, as a qualified certificate does
    good = an_ocsp(pki, signer["leaf"], when=CAPTURED)
    stranger = an_ocsp(pki, signer["leaf"], when=CAPTURED, serial=999)
    sealed = tmp_path / "issue.p7m"
    sealed.write_bytes(with_crls(a_p7m(signer), good))
    e = seals.capture(sealed, trusted_list=TL, now=CAPTURED.timestamp())
    assert [r["kind"] for r in e["revocation"]] == ["ocsp"] and e["revocation"][0]["time"] == "2026-02-01T00:00:00Z"
    ca = _der(pki["ca"])
    got = seals.validate(e, anchors=[ca])
    assert got["ok"] and "revocation checked" in got["why"], got["why"]
    revoked = dict(e, revocation=[rev.entry("ocsp", an_ocsp(pki, signer["leaf"], when=CAPTURED, revoked=True))])
    assert not seals.validate(revoked, anchors=[ca])["ok"], "revoked: refused"
    chain = [base64.b64decode(c) for c in e["chain"]]
    assert rev.relevant([rev.entry("ocsp", stranger), rev.entry("ocsp", good)], chain) == [rev.entry("ocsp", good)]
    unrelated = dict(e, revocation=[rev.entry("ocsp", stranger)])
    assert not seals.validate(unrelated, anchors=[ca])["ok"], "carries responses, none for the leaf: hard-fail"
    # a certificate that declares no responder has nothing to be asked: PKIX checks what is declared
    quiet = tmp_path / "quiet.p7m"
    quiet.write_bytes(a_p7m(pki))
    said = seals.validate(seals.capture(quiet, trusted_list=TL, now=CAPTURED.timestamp()), anchors=[ca],
                          require_revocation=True)
    assert said["ok"], said["why"]


def test_revocation_is_fetched_on_the_budget_from_each_certificate_s_responder(pki, monkeypatch):
    drawn = []
    monkeypatch.setattr(rev, "_draw", lambda direction, n, stop=None: drawn.append((direction, n)))
    signer = a_leaf(pki, serial=21, cdp=True)
    asked = []

    def answer(request: httpx.Request) -> httpx.Response:
        asked.append((request.method, str(request.url)))
        if request.method == "POST" and str(request.url) == OCSP_URL:
            req = ocsp.OCSPRequest.load(request.content)
            assert req["tbs_request"]["request_list"][0]["req_cert"]["serial_number"].native == 21
            return httpx.Response(200, content=an_ocsp(pki, signer["leaf"], when=CAPTURED))
        return httpx.Response(503)
    chain = [_der(signer["leaf"]), _der(pki["ca"])]
    got, unanswered = rev.fetch(chain, [_der(pki["ca"])], client=httpx.Client(transport=httpx.MockTransport(answer)))
    assert [g["kind"] for g in got] == ["ocsp"] and unanswered == [], "the CA is the anchor: nothing to ask for it"
    assert ("UP", len(asked) and drawn[0][1]) == drawn[0] and drawn[1][0] == "DOWN", "both ways on the budget"
    # the responder down: its CRL point; nobody: said, not failed
    crl = (x509.CertificateRevocationListBuilder().issuer_name(pki["ca"].subject)
           .last_update(CAPTURED - timedelta(hours=1)).next_update(CAPTURED + timedelta(days=7))
           .sign(pki["ca_key"], hashes.SHA256())).public_bytes(serialization.Encoding.DER)
    by_crl = httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, content=crl) if str(r.url) == CRL_URL else httpx.Response(503)))
    got, unanswered = rev.fetch(chain, [_der(pki["ca"])], client=by_crl)
    assert [g["kind"] for g in got] == ["crl"] and unanswered == []
    nobody = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503)))
    got, unanswered = rev.fetch(chain, [_der(pki["ca"])], client=nobody)
    assert got == [] and unanswered == ["Common Name: Signer 21"]


def test_live_the_sealed_bytes_are_hashed_and_their_digest_recomputed(pki, tmp_path):
    ca = [_der(pki["ca"])]
    signer = a_leaf(pki, serial=51)
    fresh = [rev.entry("ocsp", an_ocsp(pki, signer["leaf"], when=NOW))]
    revoked = [rev.entry("ocsp", an_ocsp(pki, signer["leaf"], when=NOW, revoked=True))]
    for fmt, data in (("cades", a_p7m(signer)), ("pades", a_signed_pdf(signer)), ("xades", a_sealed_xml(signer))):
        sealed = tmp_path / f"sealed.{fmt}"
        sealed.write_bytes(data)
        e = seals.capture(sealed, format=fmt, trusted_list=TL, now=CAPTURED.timestamp())
        got = seals.validate_live(e, data, anchors=ca, revocation=fresh, now=NOW)
        assert got["ok"] is True, (fmt, got["why"])
        assert "does not hash" in seals.validate_live(e, data + b" ", anchors=ca, revocation=fresh, now=NOW)["why"]
        assert seals.validate_live(e, data, anchors=ca, revocation=[], now=NOW)["ok"] is None, \
            "no responder answered: inconclusive, not a pass"
        assert seals.validate_live(e, data, anchors=ca, revocation=revoked, now=NOW)["ok"] is False, fmt
    # the file's bytes are the issuer's, its envelope says otherwise: the digest is recomputed, not trusted
    e = seals.capture(tmp_path / "sealed.cades", trusted_list=TL, now=CAPTURED.timestamp())
    forged = dict(e, content_sha256="sha256:" + "ab" * 32)
    assert "commits to" in seals.validate_live(forged, (tmp_path / "sealed.cades").read_bytes(), anchors=ca,
                                               revocation=fresh, now=NOW)["why"]


def _certified(codex):
    return replace(codex, publication="certified", content="manifest-only",
                   certifier=Certifier("ed25519:" + "5c" * 32, "https://x.example/.well-known/louise-certifier.json"))


def test_archival_verifies_the_list_s_signature_against_the_lotl_held(checkable, pki, tmp_path):  # noqa: F811
    root, snapshot, codex = checkable
    certified = _certified(codex)
    lists = signed_lists(pki, tmp_path)
    tl_digest, _ = seals.keep_trusted_list(root, "gutenberg", lists["tl"].read_bytes())
    lotl_digest, _ = seals.keep_trusted_list(root, "gutenberg", lists["lotl"].read_bytes())
    sealed = tmp_path / "issue.p7m"
    sealed.write_bytes(a_p7m(pki))
    e = seals.capture(sealed, trusted_list={"lotl_sha256": lotl_digest, "tl_sha256": tl_digest, "url": ""},
                      now=CAPTURED.timestamp())
    drag = root / "gutenberg" / "drag"
    seals.write(drag, certified, snapshot.snapshot_id, [e])
    o = chk.run(root, "gutenberg", certified, snapshot, seed=SEED, checks=["origin-seal"]).outcome("origin-seal")
    assert o.result == chk.PASS, o.why
    assert "TL Signer" in o.detail["trusted_list_signature"][tl_digest]
    # a list held by its hash but signed by a key the LOTL does not name fails, naming why
    rogue_digest, _ = seals.keep_trusted_list(root, "gutenberg", lists["rogue_tl"].read_bytes())
    e2 = dict(e, trusted_list={"lotl_sha256": lotl_digest, "tl_sha256": rogue_digest, "url": ""})
    seals.write(drag, certified, snapshot.snapshot_id, [e2])
    o = chk.run(root, "gutenberg", certified, snapshot, seed=SEED, checks=["origin-seal"]).outcome("origin-seal")
    assert o.result == chk.FAIL and "signed by a certificate its pointer does not name" in o.why


def test_live_mode_revalidates_against_the_current_lists(pki, tmp_path):
    signer = a_leaf(pki, serial=31)
    lists = signed_lists(pki, tmp_path)
    root, name = tmp_path / "collections", "gazzetta"
    tl_digest, _ = seals.keep_trusted_list(root, name, lists["tl"].read_bytes())
    lotl_digest, _ = seals.keep_trusted_list(root, name, lists["lotl"].read_bytes())
    live = tmp_path / "live"
    live.mkdir()
    data = a_p7m(signer)
    (live / "issue.p7m").write_bytes(data)
    e = seals.capture(live / "issue.p7m", now=CAPTURED.timestamp(),
                      trusted_list={"lotl_sha256": lotl_digest, "tl_sha256": tl_digest, "url": ""})
    entries = {e["file_hash"]: e}
    state = {"tl": lists["tl"].read_bytes(), "revoked": False}

    def answer(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == "https://lotl.test/eu-lotl.xml":
            return httpx.Response(200, content=lists["lotl"].read_bytes())
        if url == "https://tl.test/TSL-IT.xml":
            return httpx.Response(200, content=state["tl"]) if state["tl"] else httpx.Response(404)
        if url == OCSP_URL:
            return httpx.Response(200, content=an_ocsp(signer, signer["leaf"], when=NOW, revoked=state["revoked"]))
        return httpx.Response(404)
    client = httpx.Client(transport=httpx.MockTransport(answer))
    status, detail, why = seals_live.check(root, name, entries, live, client=client, now=NOW)
    assert status == "pass" and detail["checked"] == 1, why
    state["revoked"] = True
    status, detail, why = seals_live.check(root, name, entries, live, client=client, now=NOW)
    assert status == "fail" and detail["failed"] == [e["file_hash"]], why
    state.update(revoked=False, tl=lists["rogue_tl"].read_bytes())
    status, detail, why = seals_live.check(root, name, entries, live, client=client, now=NOW)
    assert status == "inconclusive" and "does not name" in why, "a current list nobody named: not judged"
    state["tl"] = None
    status, _, why = seals_live.check(root, name, entries, live, client=client, now=NOW)
    assert status == "inconclusive" and "no answer" in why, "unreachable: deferral, never failure"
    (live / "issue.p7m").write_bytes(data + b"x")
    status, _, why = seals_live.check(root, name, entries, live, client=client, now=NOW)
    assert status == "inconclusive" and "none of the files" in why


def test_the_commands_take_live_and_fetch_revocation():
    from app import louise
    p = louise.build_parser()
    assert p.parse_args(["hold", "check", "x", "--live", "D"]).live == "D"
    a = p.parse_args(["hold", "records", "check", "x", "--seal", "--live", "D"])
    assert a.live == "D" and a.seal is True
    a = p.parse_args(["hold", "records", "seal", "x", "s", "--files", "F", "--trusted-list", "T", "--lotl", "L",
                      "--lotl-signers", "S", "--format", "xades", "--revocation", "r1", "--revocation", "r2",
                      "--fetch-revocation"])
    assert (a.format, a.revocation, a.fetch_revocation) == ("xades", ["r1", "r2"], True)
