"""tests/hold/test_seals.py — seals (03 §3.7; 09 §9.15): a CAdES envelope
over a sealed file read and captured as an entry — the detached envelope,
the digest it commits to, the signer, the chain leaf first — validated in
archival mode with the network unplugged against the captured evidence
and a trusted list held by its hash; refused against an empty or a
foreign list, a tampered digest, a swapped chain, a moment past the
certificate's life; the member's shape; and `origin-seal` through
`hold check`: inconclusive without the member or the list, pass with
them, fail on a tampered entry or a record naming a seal not there."""
import base64
import hashlib
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
import pyhanko.sign  # noqa: F401 — registers its CMS attribute OIDs with asn1crypto before an attribute is built by name
from asn1crypto import algos, cms, core, x509 as a_x509
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import NameOID

from app.hold import check as chk, codex as cx, seals
from app.hold.codex import Certifier
from tests.hold.conftest import KEY, NOW
from tests.hold.test_check import SEED, checkable  # noqa: F401
from tests.hold.test_admit import made, space_embed  # noqa: F401

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
CONTENT = b"%PDF-1.7 the sealed issue of 12 March 2026\n"


def _cert(subject, issuer_cert, issuer_key, key, *, ca: bool, serial: int, days: int = 3650):
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, subject),
                      x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Istituto di prova")])
    b = (x509.CertificateBuilder().subject_name(name)
         .issuer_name(issuer_cert.subject if issuer_cert is not None else name)
         .public_key(key.public_key()).serial_number(serial)
         .not_valid_before(T0).not_valid_after(T0 + timedelta(days=days))
         .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
         .add_extension(x509.KeyUsage(digital_signature=not ca, content_commitment=not ca, key_encipherment=False,
                                      data_encipherment=False, key_agreement=False, key_cert_sign=ca, crl_sign=ca,
                                      encipher_only=False, decipher_only=False), critical=True)
         .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False))
    if issuer_cert is not None:
        b = b.add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(issuer_cert.public_key()),
                            critical=False)
    return b.sign(issuer_key, hashes.SHA256())


def _der(cert) -> bytes:
    return cert.public_bytes(serialization.Encoding.DER)


@pytest.fixture(scope="module")
def pki():
    ca_key = rsa.generate_private_key(65537, 2048)
    ca = _cert("Test CA", None, ca_key, ca_key, ca=True, serial=1)
    leaf_key = rsa.generate_private_key(65537, 2048)
    leaf = _cert("Test Signer", ca, ca_key, leaf_key, ca=False, serial=2, days=400)
    other_key = rsa.generate_private_key(65537, 2048)
    other = _cert("Other CA", None, other_key, other_key, ca=True, serial=3)
    return {"ca": ca, "ca_key": ca_key, "leaf": leaf, "leaf_key": leaf_key, "other": other}


def a_p7m(pki, content=CONTENT, *, when=T0 + timedelta(days=30)) -> bytes:
    """A CAdES-BES envelope with the content attached, as a .p7m carries it."""
    leaf = a_x509.Certificate.load(_der(pki["leaf"]))
    ca = a_x509.Certificate.load(_der(pki["ca"]))
    digest = hashlib.sha256(content).digest()
    attrs = cms.CMSAttributes([
        cms.CMSAttribute({"type": "content_type", "values": ["data"]}),
        cms.CMSAttribute({"type": "signing_time", "values": [cms.Time({"utc_time": core.UTCTime(when)})]}),
        cms.CMSAttribute({"type": "message_digest", "values": [digest]}),
    ])
    sig = pki["leaf_key"].sign(attrs.dump(), padding.PKCS1v15(), hashes.SHA256())
    si = cms.SignerInfo({
        "version": "v1",
        "sid": cms.SignerIdentifier({"issuer_and_serial_number": cms.IssuerAndSerialNumber({
            "issuer": leaf.issuer, "serial_number": leaf.serial_number})}),
        "digest_algorithm": algos.DigestAlgorithm({"algorithm": "sha256"}),
        "signed_attrs": attrs,
        "signature_algorithm": algos.SignedDigestAlgorithm({"algorithm": "rsassa_pkcs1v15"}),
        "signature": sig})
    sd = cms.SignedData({
        "version": "v1", "digest_algorithms": [algos.DigestAlgorithm({"algorithm": "sha256"})],
        "encap_content_info": cms.ContentInfo({"content_type": "data", "content": content}),
        "certificates": cms.CertificateSet([cms.CertificateChoices({"certificate": ca}),
                                            cms.CertificateChoices({"certificate": leaf})]),
        "signer_infos": [si]})
    return cms.ContentInfo({"content_type": "signed_data", "content": sd}).dump()


def a_trusted_list(*certs, status="http://uri.etsi.org/TrstSvc/TrstLSL/Svcstatus/granted") -> bytes:
    svc = "".join(
        f'<tsl:TSPService><tsl:ServiceInformation>'
        f'<tsl:ServiceTypeIdentifier>http://uri.etsi.org/TrstSvc/Svctype/CA/QC</tsl:ServiceTypeIdentifier>'
        f'<tsl:ServiceName><tsl:Name xml:lang="en">CA {i}</tsl:Name></tsl:ServiceName>'
        f'<tsl:ServiceDigitalIdentity><tsl:DigitalId><tsl:X509Certificate>'
        f'{base64.b64encode(_der(c)).decode()}</tsl:X509Certificate></tsl:DigitalId></tsl:ServiceDigitalIdentity>'
        f'<tsl:ServiceStatus>{status}</tsl:ServiceStatus></tsl:ServiceInformation></tsl:TSPService>'
        for i, c in enumerate(certs))
    return (f'<?xml version="1.0" encoding="UTF-8"?><tsl:TrustServiceStatusList xmlns:tsl="{seals.TL_NS}">'
            f'<tsl:TrustServiceProviderList><tsl:TrustServiceProvider><tsl:TSPServices>{svc}</tsl:TSPServices>'
            f'</tsl:TrustServiceProvider></tsl:TrustServiceProviderList></tsl:TrustServiceStatusList>').encode()


TL = {"lotl_sha256": "sha256:" + "0" * 64, "tl_sha256": "sha256:" + "1" * 64,
      "url": "https://eidas.agid.gov.it/TL/TSL-IT.xml"}


def test_the_envelope_is_read_and_captured_detached(pki, tmp_path):
    p7m = a_p7m(pki)
    sealed = tmp_path / "GU-2026-03-12.pdf.p7m"
    sealed.write_bytes(p7m)
    parsed = seals.parse_cades(p7m)
    assert parsed["message_digest"] == hashlib.sha256(CONTENT).digest()
    assert [c.subject.native["common_name"] for c in parsed["chain"]] == ["Test Signer", "Test CA"], "leaf first"
    assert parsed["signing_time"] == T0 + timedelta(days=30) and parsed["timestamp"] is None
    assert len(parsed["detached"]) < len(p7m) and CONTENT not in parsed["detached"], "the content is not carried"
    again = seals.parse_cades(parsed["detached"])
    assert again["message_digest"] == parsed["message_digest"] and len(again["chain"]) == 2
    entry = seals.capture(sealed, trusted_list=TL, now=T0.timestamp() + 86400 * 31)
    seals.validate_entry(entry)
    assert entry["file_hash"] == seals.file_hash(sealed) == hashlib.blake2b(p7m, digest_size=16).hexdigest()
    assert entry["content_sha256"] == "sha256:" + hashlib.sha256(CONTENT).hexdigest()
    assert "Test Signer" in entry["signer"]["subject"] and "Test CA" in entry["signer"]["issuer"]
    assert entry["signer"]["serial"] == "2" and len(entry["chain"]) == 2 and entry["format"] == "cades"
    assert entry["revocation"] == [] and entry["timestamp"] is None and entry["trusted_list"] == TL
    assert entry["verified_at"] == "2026-02-01T00:00:00Z"
    with pytest.raises(seals.SealError, match="not a CMS"):
        seals.parse_cades(b"%PDF-1.7 not an envelope")
    with pytest.raises(seals.SealError, match="not a XAdES document"):
        seals.capture(sealed, format="xades", trusted_list=TL)
    with pytest.raises(seals.SealError, match="not a signed PDF"):
        seals.capture(sealed, format="pades", trusted_list=TL)


def test_archival_validation_with_the_network_unplugged(pki, tmp_path):
    sealed = tmp_path / "issue.p7m"
    sealed.write_bytes(a_p7m(pki))
    entry = seals.capture(sealed, trusted_list=TL, now=(T0 + timedelta(days=31)).timestamp())
    ca_der = _der(pki["ca"])
    got = seals.validate(entry, anchors=[ca_der])
    assert got["ok"], got["why"]
    assert not seals.validate(entry, anchors=[])["ok"], "an empty trusted list anchors nothing"
    foreign = seals.validate(entry, anchors=[_der(pki["other"])])
    assert not foreign["ok"] and "path does not validate" in foreign["why"]
    late = seals.validate(entry, anchors=[ca_der], moment=T0 + timedelta(days=3000))
    assert not late["ok"], "the signer's certificate had lapsed by then"
    tampered = dict(entry, content_sha256="sha256:" + "ab" * 32)
    assert "message digest" in seals.validate(tampered, anchors=[ca_der])["why"]
    swapped = dict(entry, chain=list(reversed(entry["chain"])))
    assert "does not start with the envelope's signer" in seals.validate(swapped, anchors=[ca_der])["why"]
    # a signature made by another key over the same attributes does not verify
    forged_p7m = a_p7m({**pki, "leaf_key": rsa.generate_private_key(65537, 2048)})
    forged = dict(entry, seal={"b64": base64.b64encode(seals.parse_cades(forged_p7m)["detached"]).decode()})
    assert "does not verify" in seals.validate(forged, anchors=[ca_der])["why"]


def test_the_trusted_list_is_parsed_and_held_by_its_hash(pki, tmp_path):
    xml = a_trusted_list(pki["ca"], pki["other"])
    assert seals.anchors_from_trusted_list(xml) == [_der(pki["ca"]), _der(pki["other"])]
    withdrawn = a_trusted_list(pki["ca"], status="http://uri.etsi.org/TrstSvc/TrstLSL/Svcstatus/withdrawn")
    assert seals.anchors_from_trusted_list(withdrawn) == []
    with pytest.raises(seals.SealError, match="not XML"):
        seals.anchors_from_trusted_list(b"<broken")
    root = tmp_path / "collections"
    digest, p = seals.keep_trusted_list(root, "gazzetta", xml)
    assert digest == "sha256:" + hashlib.sha256(xml).hexdigest() and p.read_bytes() == xml
    assert seals.held_trusted_list(root, "gazzetta", digest) == xml
    assert seals.held_trusted_list(root, "gazzetta", "sha256:" + "f" * 64) is None
    p.write_bytes(b"<tampered/>")
    assert seals.held_trusted_list(root, "gazzetta", digest) is None, "a file that is not what its name says"


def test_the_member_s_shape(pki, tmp_path):
    from tests.hold.conftest import pubmed
    sealed = tmp_path / "a.p7m"
    sealed.write_bytes(a_p7m(pki))
    e1 = seals.capture(sealed, trusted_list=TL, now=T0.timestamp())
    e2 = dict(e1, file_hash="0" * 32)
    codex = pubmed()
    d = tmp_path / "shard"
    p = seals.write(d, codex, "2026-08", [e1, e2])
    doc = json.loads(p.read_bytes())
    assert [e["file_hash"] for e in doc["entries"]] == sorted([e1["file_hash"], "0" * 32]) and doc["kind"] == "louise-seals"
    assert seals.read(d, "2026-08")["snapshot_id"] == "2026-08" and seals.read(d, "2026-09") is None
    for bad in ({**doc, "kind": "x"}, {**doc, "version": 2}, {**doc, "entries": [e1, e2]},
                {**doc, "entries": [dict(e1, format="pgp")]}, {**doc, "entries": [dict(e1, chain=[])]},
                {**doc, "entries": [dict(e1, content_sha256="abc")]}):
        with pytest.raises(seals.SealError):
            seals.validate_doc(bad)


def test_origin_seal_through_hold_check(checkable, pki):  # noqa: F811
    root, snapshot, codex = checkable
    o = chk.run(root, "gutenberg", codex, snapshot, seed=SEED, checks=["origin-seal"]).outcome("origin-seal")
    assert o.result == chk.INCONCLUSIVE and "open collection" in o.why
    certified = replace(codex, publication="certified", content="manifest-only",
                        certifier=Certifier("ed25519:" + "5c" * 32, "https://x.example/.well-known/louise-certifier.json"))
    run = lambda: chk.run(root, "gutenberg", certified, snapshot, seed=SEED, checks=["origin-seal"]).outcome("origin-seal")  # noqa: E731
    o = run()
    assert o.result == chk.INCONCLUSIVE and "no seals member" in o.why
    sealed = root / "issue.p7m"
    sealed.write_bytes(a_p7m(pki))
    xml = a_trusted_list(pki["ca"])
    tl = {**TL, "tl_sha256": "sha256:" + hashlib.sha256(xml).hexdigest()}
    entry = seals.capture(sealed, trusted_list=tl, now=(T0 + timedelta(days=31)).timestamp())
    shard_tree = root / "gutenberg" / "shard"
    seals.write(shard_tree, certified, snapshot.snapshot_id, [entry])
    o = run()
    assert o.result == chk.INCONCLUSIVE and "trusted list" in o.why and "not held" in o.why
    seals.keep_trusted_list(root, "gutenberg", xml)
    o = run()
    assert o.result == chk.PASS, o.why
    assert (o.detail["mode"], o.detail["entries"], o.detail["failed"]) == ("archival", 1, []) and o.shards
    assert "LOTL is not held" in o.detail["trusted_list_signature"][tl["tl_sha256"]]
    # a tampered entry fails, naming it
    seals.write(shard_tree, certified, snapshot.snapshot_id, [dict(entry, content_sha256="sha256:" + "ab" * 32)])
    o = run()
    assert o.result == chk.FAIL and o.detail["failed"] == [entry["file_hash"]] and "message digest" in o.why
    # a member that does not read fails too
    seals.seals_path(shard_tree, snapshot.snapshot_id).write_bytes(b"{")
    assert run().result == chk.FAIL


def signed_lists(pki, tmp_path) -> dict:
    """A national list (IT) granting the test CA, signed by a key the LOTL's
    pointer names; the LOTL signed by a key "the Official Journal"
    announces; and the lists a certifier must refuse."""
    from tests.hold import xades_sign as xs
    tl_key = rsa.generate_private_key(65537, 2048)
    tl_cert = _cert("TL Signer", None, tl_key, tl_key, ca=False, serial=11)
    lotl_key = rsa.generate_private_key(65537, 2048)
    lotl_cert = _cert("LOTL Signer", None, lotl_key, lotl_key, ca=False, serial=12)
    rogue_key = rsa.generate_private_key(65537, 2048)
    rogue_cert = _cert("Rogue", None, rogue_key, rogue_key, ca=False, serial=13)
    out = {}
    for name, data in (("tl", xs.a_tl("IT", [pki["ca"]], tl_key, tl_cert)),
                       ("foreign_tl", xs.a_tl("IT", [pki["other"]], tl_key, tl_cert)),
                       ("rogue_tl", xs.a_tl("IT", [pki["ca"]], rogue_key, rogue_cert)),
                       ("lotl", xs.a_lotl([("EU", "https://lotl.test/eu-lotl.xml", [lotl_cert]),
                                           ("IT", "https://tl.test/TSL-IT.xml", [tl_cert])], lotl_key, lotl_cert)),
                       ("signers", lotl_cert.public_bytes(serialization.Encoding.PEM)),
                       ("tl_cert", tl_cert.public_bytes(serialization.Encoding.PEM))):
        p = tmp_path / f"{name}.xml"
        p.write_bytes(data)
        out[name] = p
    return out


def test_records_seal_captures_validates_and_writes_the_member(pki, tmp_path, monkeypatch, capsys):
    from app import collections as collections_mod, louise
    from app.hold import collection as col
    from tests.hold.conftest import a_root, pubmed
    root = tmp_path / "louise" / "collections"
    monkeypatch.setattr(collections_mod, "louise_root", lambda: tmp_path / "louise")
    codex = pubmed(publication="certified", content="manifest-only",
                   certifier=Certifier("ed25519:" + "5c" * 32, "https://x.example/.well-known/louise-certifier.json"))
    a_root(root, codex)
    name = col.name_of(root, codex.collection_id)
    files = tmp_path / "issues"
    files.mkdir()
    (files / "a.p7m").write_bytes(a_p7m(pki))
    (files / "b.p7m").write_bytes(a_p7m(pki, CONTENT + b" second issue"))
    lists = signed_lists(pki, tmp_path)
    tl, lotl, signers = lists["tl"], lists["lotl"], lists["signers"]
    flags = ["--lotl", str(lotl), "--lotl-signers", str(signers)]
    assert louise.main(["hold", "records", "seal", name, "2026-08", "--root", str(root), "--files", str(files),
                        "--trusted-list", str(tl), "--url", "https://eidas.agid.gov.it/TL/TSL-IT.xml"] + flags) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["entries"] == 2 and out["snapshot_id"] == "2026-08"
    assert "CN=TL Signer" in out["trusted_list_signed_by"] and "CN=LOTL Signer" in out["lotl_signed_by"]
    doc = seals.read(col.shard_dir_of(root, name), "2026-08")
    assert len(doc["entries"]) == 2 and doc["entries"][0]["trusted_list"]["url"].endswith("TSL-IT.xml")
    tl_digest = doc["entries"][0]["trusted_list"]["tl_sha256"]
    lotl_digest = doc["entries"][0]["trusted_list"]["lotl_sha256"]
    assert seals.held_trusted_list(root, name, tl_digest) == tl.read_bytes()
    assert seals.held_trusted_list(root, name, lotl_digest) == lotl.read_bytes(), "both lists kept by their hash"
    # a list trusted for its signer: the LOTL by the Official Journal's, the national list by its pointer's
    for bad_flags, why in (([str(lists["tl"]), "--lotl", str(lists["lotl"]), "--lotl-signers", str(lists["tl_cert"])],
                            "list of trusted lists is signed by a certificate"),
                           ([str(lists["rogue_tl"])] + flags, "trusted list of IT is signed by a certificate")):
        assert louise.main(["hold", "records", "seal", name, "2026-10", "--root", str(root), "--files", str(files),
                            "--trusted-list"] + bad_flags) == 2
        assert why in capsys.readouterr().err
    assert seals.read(col.shard_dir_of(root, name), "2026-10") is None
    # a seal the list does not anchor is refused, and nothing is written for that snapshot
    foreign = lists["foreign_tl"]
    assert louise.main(["hold", "records", "seal", name, "2026-09", "--root", str(root), "--files", str(files),
                        "--trusted-list", str(foreign)] + flags) == 2
    assert "not written" in capsys.readouterr().err
    assert seals.read(col.shard_dir_of(root, name), "2026-09") is None
    # an open collection carries none
    open_codex = pubmed()
    a_root(root, open_codex)
    assert louise.main(["hold", "records", "seal", col.name_of(root, open_codex.collection_id), "2026-08", "--root",
                        str(root), "--files", str(files), "--trusted-list", str(tl)] + flags) == 2
    p = louise.build_parser()
    a = p.parse_args(["hold", "records", "check", name, "--seal"])
    assert a.seal is True and a.fn is louise.cmd_records_check


def a_signed_pdf(pki) -> bytes:
    """A one-page PDF with a PAdES signature by the test signer (pyhanko)."""
    from io import BytesIO
    from asn1crypto import keys as a_keys, x509 as a_x509
    from pyhanko.pdf_utils import generic, writer
    from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter
    from pyhanko.sign import signers
    from pyhanko_certvalidator.registry import SimpleCertificateStore
    w = writer.PdfFileWriter()
    w.insert_page(generic.DictionaryObject({
        generic.pdf_name("/Type"): generic.pdf_name("/Page"),
        generic.pdf_name("/MediaBox"): generic.ArrayObject([generic.NumberObject(n) for n in (0, 0, 200, 200)])}))
    buf = BytesIO()
    w.write(buf)
    signer = signers.SimpleSigner(
        signing_cert=a_x509.Certificate.load(_der(pki["leaf"])),
        signing_key=a_keys.PrivateKeyInfo.load(pki["leaf_key"].private_bytes(
            serialization.Encoding.DER, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())),
        cert_registry=SimpleCertificateStore.from_certs([a_x509.Certificate.load(_der(pki["ca"]))]))
    out = signers.sign_pdf(IncrementalPdfFileWriter(BytesIO(buf.getvalue())),
                           signers.PdfSignatureMetadata(field_name="Seal", md_algorithm="sha256"), signer=signer)
    return out.getvalue()


def test_a_pades_seal_is_captured_from_the_pdf_and_validates_like_a_cades_one(pki, tmp_path):
    pdf = a_signed_pdf(pki)
    sealed = tmp_path / "OJ-L-2026-001.pdf"
    sealed.write_bytes(pdf)
    envelope = seals.pades_envelope(pdf)
    parsed = seals.parse_cades(envelope)
    assert [c.subject.native["common_name"] for c in parsed["chain"]] == ["Test Signer", "Test CA"]
    entry = seals.capture(sealed, format="pades", trusted_list=TL, now=(T0 + timedelta(days=31)).timestamp())
    assert entry["format"] == "pades" and entry["file_hash"] == seals.file_hash(sealed)
    assert entry["content_sha256"] == "sha256:" + parsed["message_digest"].hex(), \
        "the digest over the PDF's byte range, as the signed attributes carry it"
    got = seals.validate(entry, anchors=[_der(pki["ca"])])
    assert got["ok"], got["why"]
    assert not seals.validate(entry, anchors=[_der(pki["other"])])["ok"]
    with pytest.raises(seals.SealError, match="not a signed PDF|no signature"):
        seals.pades_envelope(b"%PDF-1.7\n1 0 obj << /Type /Catalog >> endobj\ntrailer << /Root 1 0 R >>\n%%EOF\n")
    with pytest.raises(seals.SealError, match="not a signed PDF"):
        seals.capture(tmp_path / "x.p7m", format="pades", trusted_list=TL) if (tmp_path / "x.p7m").write_bytes(
            a_p7m(pki)) else None
