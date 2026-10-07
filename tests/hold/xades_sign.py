"""A XAdES-BES enveloped signer for tests: the inverse of app/hold/xmldsig.py.
The verifier is proven against the EU's published lists (tests/hold/
test_xmldsig.py); this signer makes documents the tests can tamper with,
sealed origin files and trusted lists under a test PKI."""
import base64
import copy
import hashlib

from lxml import etree
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from app.hold import tsl, xmldsig

DS, XADES = xmldsig.DS, xmldsig.XADES
EXC = "http://www.w3.org/2001/10/xml-exc-c14n#"
INC = "http://www.w3.org/TR/2001/REC-xml-c14n-20010315"
SHA256 = "http://www.w3.org/2001/04/xmlenc#sha256"
SHA512 = "http://www.w3.org/2001/04/xmlenc#sha512"


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def _c14n(node, alg: str) -> bytes:
    return etree.tostring(node, method="c14n", exclusive=alg == EXC, with_comments=False)


def der(cert) -> bytes:
    return cert.public_bytes(serialization.Encoding.DER)


def sign(xml: bytes, key, cert, *, c14n: str = EXC, signing_time: str = "2026-02-01T00:00:00Z",
         extra_certs=(), digest: str = SHA256) -> bytes:
    root = etree.fromstring(xml)
    E = etree.SubElement
    sig = E(root, f"{{{DS}}}Signature", nsmap={"ds": DS}, Id="sig-1")
    si = E(sig, f"{{{DS}}}SignedInfo")
    E(si, f"{{{DS}}}CanonicalizationMethod", Algorithm=c14n)
    ecdsa = isinstance(key, ec.EllipticCurvePrivateKey)
    E(si, f"{{{DS}}}SignatureMethod", Algorithm="http://www.w3.org/2001/04/xmldsig-more#"
      + ("ecdsa-sha256" if ecdsa else "rsa-sha256"))
    data_ref = E(si, f"{{{DS}}}Reference", URI="")
    tr = E(data_ref, f"{{{DS}}}Transforms")
    E(tr, f"{{{DS}}}Transform", Algorithm=xmldsig.ENVELOPED)
    E(tr, f"{{{DS}}}Transform", Algorithm=c14n)
    E(data_ref, f"{{{DS}}}DigestMethod", Algorithm=digest)
    dv_data = E(data_ref, f"{{{DS}}}DigestValue")
    sp_ref = E(si, f"{{{DS}}}Reference", URI="#sp-1", Type=xmldsig.SIGNED_PROPERTIES)
    E(E(sp_ref, f"{{{DS}}}Transforms"), f"{{{DS}}}Transform", Algorithm=c14n)
    E(sp_ref, f"{{{DS}}}DigestMethod", Algorithm=SHA256)
    dv_sp = E(sp_ref, f"{{{DS}}}DigestValue")
    sv = E(sig, f"{{{DS}}}SignatureValue")
    xd = E(E(sig, f"{{{DS}}}KeyInfo"), f"{{{DS}}}X509Data")
    for c in (cert, *extra_certs):
        E(xd, f"{{{DS}}}X509Certificate").text = _b64(der(c))
    qp = E(E(sig, f"{{{DS}}}Object"), f"{{{XADES}}}QualifyingProperties", nsmap={"xades": XADES}, Target="#sig-1")
    sp = E(qp, f"{{{XADES}}}SignedProperties", Id="sp-1")
    ssp = E(sp, f"{{{XADES}}}SignedSignatureProperties")
    E(ssp, f"{{{XADES}}}SigningTime").text = signing_time
    cd = E(E(E(ssp, f"{{{XADES}}}SigningCertificateV2"), f"{{{XADES}}}Cert"), f"{{{XADES}}}CertDigest")
    E(cd, f"{{{DS}}}DigestMethod", Algorithm=SHA256)
    E(cd, f"{{{DS}}}DigestValue").text = _b64(hashlib.sha256(der(cert)).digest())
    dv_sp.text = _b64(hashlib.sha256(_c14n(sp, c14n)).digest())
    doc = copy.deepcopy(root)
    doc.remove(doc[len(doc) - 1])
    h = {SHA256: hashlib.sha256, SHA512: hashlib.sha512}[digest]
    dv_data.text = _b64(h(_c14n(doc, c14n)).digest())
    signed = _c14n(si, c14n)
    if ecdsa:
        r, s = decode_dss_signature(key.sign(signed, ec.ECDSA(hashes.SHA256())))
        n = (key.curve.key_size + 7) // 8
        value = r.to_bytes(n, "big") + s.to_bytes(n, "big")
    else:
        value = key.sign(signed, padding.PKCS1v15(), hashes.SHA256())
    sv.text = _b64(value)
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8")


def a_tl(territory: str, granted, key, cert, **kw) -> bytes:
    """A national trusted list granting `granted` CA certificates, signed."""
    svc = "".join(
        f"<TSPService><ServiceInformation>"
        f"<ServiceTypeIdentifier>http://uri.etsi.org/TrstSvc/Svctype/CA/QC</ServiceTypeIdentifier>"
        f"<ServiceDigitalIdentity><DigitalId><X509Certificate>{_b64(der(c))}</X509Certificate></DigitalId>"
        f"</ServiceDigitalIdentity>"
        f"<ServiceStatus>http://uri.etsi.org/TrstSvc/TrstLSL/Svcstatus/granted</ServiceStatus>"
        f"</ServiceInformation></TSPService>" for c in granted)
    xml = (f'<TrustServiceStatusList xmlns="{tsl.TL_NS}" Id="tsl-1"><SchemeInformation>'
           f'<TSLVersionIdentifier>6</TSLVersionIdentifier><SchemeTerritory>{territory}</SchemeTerritory>'
           f'</SchemeInformation><TrustServiceProviderList><TrustServiceProvider><TSPServices>{svc}'
           f'</TSPServices></TrustServiceProvider></TrustServiceProviderList></TrustServiceStatusList>').encode()
    return sign(xml, key, cert, **kw)


def a_lotl(pointers, key, cert, **kw) -> bytes:
    """A list of trusted lists: `pointers` = [(territory, location, [certs])], signed."""
    ptr = "".join(
        f"<OtherTSLPointer><ServiceDigitalIdentities>"
        + "".join(f"<ServiceDigitalIdentity><DigitalId><X509Certificate>{_b64(der(c))}</X509Certificate>"
                  f"</DigitalId></ServiceDigitalIdentity>" for c in certs)
        + f"</ServiceDigitalIdentities><TSLLocation>{loc}</TSLLocation><AdditionalInformation>"
          f"<OtherInformation><SchemeTerritory>{terr}</SchemeTerritory></OtherInformation>"
          f'<OtherInformation><ns3:MimeType xmlns:ns3="http://uri.etsi.org/02231/v2/additionaltypes#">'
          f"{tsl.XML_MIME}</ns3:MimeType></OtherInformation></AdditionalInformation></OtherTSLPointer>"
        for terr, loc, certs in pointers)
    xml = (f'<TrustServiceStatusList xmlns="{tsl.TL_NS}" Id="lotl-1"><SchemeInformation>'
           f'<TSLVersionIdentifier>6</TSLVersionIdentifier><SchemeTerritory>EU</SchemeTerritory>'
           f'<PointersToOtherTSL>{ptr}</PointersToOtherTSL></SchemeInformation></TrustServiceStatusList>').encode()
    return sign(xml, key, cert, **kw)
