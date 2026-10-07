"""tests/hold/test_xmldsig.py — XML signatures and the trusted lists' own
(app/hold/xmldsig.py, app/hold/tsl.py; 03 §3.7): the EU's published List of
Trusted Lists verified by a certificate its own pointer names, Iceland's
national list by a certificate the LOTL's pointer for IS names — real
documents, as published in 2026-09 — and every way they can be wrong
caught: a changed byte, a changed signature, a signer the pointer does not
name, a signature outside its signer's life. Then the test signer's
documents both ways (exclusive and inclusive, RSA and ECDSA), detached,
and the parser that resolves no entity."""
import gzip
import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509.oid import NameOID
from lxml import etree

from app.hold import tsl, xmldsig
from app.hold.xmldsig import XmlSigError
from tests.hold import xades_sign as xs

FIX = Path(__file__).resolve().parent.parent / "fixtures" / "trusted-lists"
T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _read(name: str) -> bytes:
    return gzip.decompress((FIX / name).read_bytes())


@pytest.fixture(scope="module")
def lotl():
    return _read("eu-lotl.xml.gz")


@pytest.fixture(scope="module")
def tl_is():
    return _read("tsl-is.xml.gz")


def _eu_signers(lotl):
    return [c for p in tsl.pointers(xmldsig.parse(lotl)) if p.territory == "EU" for c in p.certs_der]


def test_the_published_lists_verify_by_who_signed_them(lotl, tl_is):
    said = tsl.verify_lotl(lotl, _eu_signers(lotl))
    assert said["territory"] == "EU" and "EUROPEAN COMMISSION" in said["signer"]
    said = tsl.verify_against_lotl(tl_is, lotl)
    assert said["territory"] == "IS" and "Fjarskiptastofa" in said["signer"]
    ptrs = tsl.pointers(xmldsig.parse(lotl))
    assert len({p.territory for p in ptrs}) >= 30 and all(p.certs_der for p in ptrs if p.mime == tsl.XML_MIME)


def test_every_way_a_published_list_can_be_wrong_is_caught(lotl, tl_is):
    changed = tl_is.replace(b"<TSLSequenceNumber>", b"<TSLSequenceNumber>9", 1)
    with pytest.raises(XmlSigError, match="digest does not match"):
        tsl.verify_against_lotl(changed, lotl)
    head, _, rest = tl_is.partition(b"SignatureValue")
    at = rest.index(b">") + 1
    flipped = head + b"SignatureValue" + rest[:at] + (b"B" if rest[at:at + 1] == b"A" else b"A") + rest[at + 1:]
    with pytest.raises(XmlSigError, match="does not verify"):
        tsl.verify_against_lotl(flipped, lotl)
    # signed by a key the pointer does not name: Iceland's list judged by the LOTL's own signers
    with pytest.raises(XmlSigError, match="does not name"):
        tsl.verify_list(tl_is, _eu_signers(lotl))
    with pytest.raises(XmlSigError, match="no pointer for territory"):
        tsl.verify_against_lotl(tl_is.replace(b"<SchemeTerritory>IS<", b"<SchemeTerritory>XX<", 1), lotl)
    with pytest.raises(XmlSigError, match="no LOTL signing certificate"):
        tsl.verify_lotl(lotl, [])


def _self_signed(key, cn: str, *, days=3650):
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    return (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(7).not_valid_before(T0).not_valid_after(T0 + timedelta(days=days))
            .sign(key, hashes.SHA256()))


@pytest.fixture(scope="module")
def signer():
    k = rsa.generate_private_key(65537, 2048)
    return k, _self_signed(k, "List Signer")


DOC = b'<doc xmlns="urn:test" xmlns:o="urn:other"><a o:x="1">text &amp; more</a>\n  <b/></doc>'


@pytest.mark.parametrize("c14n", [xs.EXC, xs.INC])
def test_the_test_signer_s_documents_verify_and_tampering_fails(signer, c14n):
    key, cert = signer
    signed = xs.sign(DOC, key, cert, c14n=c14n)
    s = xmldsig.verify(xmldsig.signature_of(xmldsig.parse(signed)))
    assert s.xades and s.signer_der == xs.der(cert) and s.signing_time == "2026-02-01T00:00:00Z"
    assert len(s.data_references) == 1 and s.data_references[0].digest_alg == "sha256"
    with pytest.raises(XmlSigError, match="digest does not match"):
        xmldsig.verify(xmldsig.signature_of(xmldsig.parse(signed.replace(b"text", b"test"))))
    with pytest.raises(XmlSigError, match="digest does not match"):
        xmldsig.verify(xmldsig.signature_of(xmldsig.parse(signed.replace(b"2026-02-01", b"2026-02-02"))))
    # detached: the SignedInfo and the properties verify without the document
    alone = xmldsig.parse(xmldsig.detached(s.element)).getroot()
    assert xmldsig.verify(alone, data=False).data_references[0].digest == s.data_references[0].digest


def test_ecdsa_and_the_signer_the_properties_name(signer):
    k = ec.generate_private_key(ec.SECP256R1())
    cert = _self_signed(k, "EC Signer")
    signed = xs.sign(DOC, k, cert)
    assert xmldsig.verify(xmldsig.signature_of(xmldsig.parse(signed))).signer_der == xs.der(cert)
    # the properties name another certificate than the one carried: refused
    mine = xs._b64(hashlib.sha256(xs.der(cert)).digest()).encode()
    theirs = xs._b64(hashlib.sha256(xs.der(signer[1])).digest()).encode()
    with pytest.raises(XmlSigError, match="names none of the certificates"):
        xmldsig.verify(xmldsig.signature_of(xmldsig.parse(signed.replace(mine, theirs))))


def test_what_is_not_admitted_is_refused_not_guessed(signer):
    key, cert = signer
    signed = xs.sign(DOC, key, cert)
    with pytest.raises(XmlSigError, match="is not admitted"):
        xmldsig.verify(xmldsig.signature_of(xmldsig.parse(signed.replace(
            xs.SHA256.encode(), b"http://www.w3.org/2000/09/xmldsig#sha1", 1))))
    with pytest.raises(XmlSigError, match="only same-document"):
        xmldsig.verify(xmldsig.signature_of(xmldsig.parse(signed.replace(b'URI=""', b'URI="http://x/"', 1))))
    with pytest.raises(XmlSigError, match="2 ds:Signature"):
        xmldsig.signature_of(xmldsig.parse(xs.sign(signed, key, cert)))
    bare = xmldsig.parse(signed)
    sig = xmldsig.signature_of(bare)
    sig.remove(sig.find(f"{{{xmldsig.DS}}}Object"))
    with pytest.raises(XmlSigError, match="not XAdES"):
        xmldsig.verify(sig)


def test_the_parser_resolves_no_entity(tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("KEY MATERIAL")
    xml = (f'<?xml version="1.0"?><!DOCTYPE d [<!ENTITY x SYSTEM "file://{secret.as_posix()}">]>'
           f'<d>&x;</d>').encode()
    assert b"KEY MATERIAL" not in etree.tostring(xmldsig.parse(xml))


def test_a_list_signed_outside_its_signer_s_life_is_refused(signer):
    key, cert = signer
    late = xs.a_tl("IT", [], key, cert, signing_time="2040-01-01T00:00:00Z")
    with pytest.raises(XmlSigError, match="outside its signer's validity"):
        tsl.verify_list(late, [xs.der(cert)])
    assert tsl.verify_list(xs.a_tl("IT", [], key, cert), [xs.der(cert)])["territory"] == "IT"
    pem = cert.public_bytes(serialization.Encoding.PEM)
    assert tsl.load_certificates(pem) == [xs.der(cert)] == tsl.load_certificates(xs.der(cert))
