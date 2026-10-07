"""XML signatures (W3C XMLDSig) with XAdES's qualifying properties (ETSI EN
319 132): the envelope of the EU's trusted lists (ETSI TS 119 612) and of
an origin file sealed in XAdES (09 §9.15's `"xades"`). Migration step 10.

Verified here, and nothing guessed around:
  - every `Reference`: dereferenced (`""`, the document; `#id`, the one
    element carrying that Id), its transforms applied — the enveloped
    signature removed, then a canonicalisation — and its digest compared;
  - the `SignatureValue` over the canonical `SignedInfo`, with the key of
    the signing certificate;
  - XAdES: the `SignedProperties` are one of the references, and the
    `SigningCertificate(V2)` digest they carry is the digest of the
    certificate that verified the signature.

Only the algorithms the EU lists and the qualified seals use are admitted
— RSA (PKCS#1 v1.5 and PSS) and ECDSA over SHA-256/384/512, the inclusive
and exclusive canonicalisations; SHA-1, XPath and XSLT transforms are
refused. The parser resolves no entity and fetches nothing.

lxml canonicalises (it arrives with pyhanko, approved 2026-10-07);
`cryptography` verifies. Checked against the published EU list of trusted
lists and four national lists (tests/hold/test_xmldsig.py).
"""
from __future__ import annotations

import base64
import copy
import hashlib
from dataclasses import dataclass, field

from app.hold.codex import CodexError

DS = "http://www.w3.org/2000/09/xmldsig#"
XADES = "http://uri.etsi.org/01903/v1.3.2#"
XADES141 = "http://uri.etsi.org/01903/v1.4.1#"
SIGNED_PROPERTIES = "http://uri.etsi.org/01903#SignedProperties"
ENVELOPED = "http://www.w3.org/2000/09/xmldsig#enveloped-signature"
EXC_NS = "http://www.w3.org/2001/10/xml-exc-c14n#"

#: algorithm -> (exclusive, with comments, 1.1)
C14N = {
    "http://www.w3.org/TR/2001/REC-xml-c14n-20010315": (False, False, False),
    "http://www.w3.org/TR/2001/REC-xml-c14n-20010315#WithComments": (False, True, False),
    "http://www.w3.org/2001/10/xml-exc-c14n#": (True, False, False),
    "http://www.w3.org/2001/10/xml-exc-c14n#WithComments": (True, True, False),
    "http://www.w3.org/2006/12/xml-c14n11": (False, False, True),
    "http://www.w3.org/2006/12/xml-c14n11#WithComments": (False, True, True),
}
DIGESTS = {
    "http://www.w3.org/2001/04/xmlenc#sha256": "sha256",
    "http://www.w3.org/2001/04/xmldsig-more#sha384": "sha384",
    "http://www.w3.org/2001/04/xmlenc#sha512": "sha512",
}
#: algorithm -> (key kind, hash name)
SIGNATURES = {
    "http://www.w3.org/2001/04/xmldsig-more#rsa-sha256": ("rsa", "sha256"),
    "http://www.w3.org/2001/04/xmldsig-more#rsa-sha384": ("rsa", "sha384"),
    "http://www.w3.org/2001/04/xmldsig-more#rsa-sha512": ("rsa", "sha512"),
    "http://www.w3.org/2007/05/xmldsig-more#sha256-rsa-MGF1": ("pss", "sha256"),
    "http://www.w3.org/2007/05/xmldsig-more#sha384-rsa-MGF1": ("pss", "sha384"),
    "http://www.w3.org/2007/05/xmldsig-more#sha512-rsa-MGF1": ("pss", "sha512"),
    "http://www.w3.org/2001/04/xmldsig-more#ecdsa-sha256": ("ecdsa", "sha256"),
    "http://www.w3.org/2001/04/xmldsig-more#ecdsa-sha384": ("ecdsa", "sha384"),
    "http://www.w3.org/2001/04/xmldsig-more#ecdsa-sha512": ("ecdsa", "sha512"),
}


class XmlSigError(CodexError):
    exit_code = 2


@dataclass
class Reference:
    uri: str
    type: str | None
    digest_alg: str                    # hash name: sha256 · sha384 · sha512
    digest: bytes                      # the DigestValue the SignedInfo commits to


@dataclass
class Signature:
    """What a `ds:Signature` carries, read (and, from `verify`, verified)."""
    element: object
    references: list[Reference]
    signer_der: bytes                  # the certificate whose key verified the signature
    certs_der: list[bytes]             # KeyInfo's and XAdES CertificateValues', signer first
    signing_time: str | None
    timestamp_der: bytes | None        # XAdES SignatureTimeStamp's EncapsulatedTimeStamp
    ocsp_der: list[bytes] = field(default_factory=list)
    crl_der: list[bytes] = field(default_factory=list)
    xades: bool = False

    @property
    def data_references(self) -> list[Reference]:
        return [r for r in self.references if r.type != SIGNED_PROPERTIES]


def _parser():
    from lxml import etree
    return etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False,
                           remove_blank_text=False)


def parse(xml: bytes):
    """The document, read by a parser that expands no entity and fetches
    nothing: a trusted list is input, and input is not trusted."""
    from lxml import etree
    try:
        return etree.ElementTree(etree.fromstring(xml, _parser()))
    except etree.XMLSyntaxError as e:
        raise XmlSigError(f"not XML: {e}") from None


def signature_of(tree):
    """The document's one `ds:Signature`. Several: which is the seal is not
    guessed."""
    found = tree.getroot().iter(f"{{{DS}}}Signature")
    sigs = list(found)
    if not sigs:
        raise XmlSigError("no ds:Signature in the document")
    if len(sigs) > 1:
        raise XmlSigError(f"{len(sigs)} ds:Signature elements; which one is the seal is not guessed")
    return sigs[0]


def _b64(text: str | None, what: str) -> bytes:
    try:
        return base64.b64decode("".join((text or "").split()), validate=True)
    except (ValueError, TypeError):
        raise XmlSigError(f"{what}: not base64") from None


def _one(parent, tag: str, what: str):
    el = parent.find(tag)
    if el is None:
        raise XmlSigError(f"no {what}")
    return el


def _c14n(node, algorithm: str, *, prefixes: list[str] | None = None, comments: bool | None = None) -> bytes:
    from lxml import etree
    if algorithm not in C14N:
        raise XmlSigError(f"canonicalisation {algorithm} is not admitted")
    exclusive, with_comments, v11 = C14N[algorithm]
    if v11:
        el = node.getroot() if hasattr(node, "getroot") else node
        for anc in el.iterancestors():
            if any(k.startswith("{http://www.w3.org/XML/1998/namespace}") and k.rsplit("}", 1)[1] in ("base", "id")
                   for k in anc.attrib):
                raise XmlSigError("C14N 1.1 over a subset under xml:base or xml:id: it differs from 1.0 there, "
                                  "and only 1.0 is implemented")
    return etree.tostring(node, method="c14n", exclusive=exclusive,
                          with_comments=with_comments if comments is None else comments,
                          inclusive_ns_prefixes=(prefixes or None) if exclusive else None)


def _prefixes(method_el) -> list[str]:
    inc = method_el.find(f"{{{EXC_NS}}}InclusiveNamespaces")
    return (inc.get("PrefixList") or "").split() if inc is not None else []


def _by_id(tree, ident: str):
    hits = tree.xpath("//*[@Id=$v or @ID=$v or @id=$v]", v=ident)
    if len(hits) != 1:
        raise XmlSigError(f"#{ident}: {len(hits)} elements carry that Id; a reference names exactly one")
    return hits[0]


def _steps(el) -> list[int]:
    """The child indices from the root down to `el`: a path that survives
    a deep copy, whatever the prefixes."""
    out = []
    while el.getparent() is not None:
        out.append(el.getparent().index(el))
        el = el.getparent()
    return out[::-1]


def _follow(root, steps: list[int]):
    for i in steps:
        root = root[i]
    return root


def _remove_keeping_tail(el) -> None:
    parent = el.getparent()
    if el.tail:
        prev = el.getprevious()
        if prev is not None:
            prev.tail = (prev.tail or "") + el.tail
        else:
            parent.text = (parent.text or "") + el.tail
    parent.remove(el)


def _dereference(sig, ref_el) -> bytes:
    """The octets a reference's digest is over: its node-set (comments
    excluded, as a same-document URI excludes them), the enveloped
    signature removed, canonicalised by its last transform (inclusive
    1.0 when it names none)."""
    tree = sig.getroottree()
    uri = ref_el.get("URI")
    if uri is None or (uri and not uri.startswith("#")):
        raise XmlSigError(f"reference URI {uri!r}: only same-document references are admitted")
    transforms = ref_el.find(f"{{{DS}}}Transforms")
    steps = list(transforms) if transforms is not None else []
    enveloped = False
    c14n_alg, prefixes = "http://www.w3.org/TR/2001/REC-xml-c14n-20010315", []
    for i, t in enumerate(steps):
        alg = t.get("Algorithm")
        if alg == ENVELOPED:
            enveloped = True
        elif alg in C14N and i == len(steps) - 1:
            c14n_alg, prefixes = alg, _prefixes(t)
        else:
            raise XmlSigError(f"transform {alg} is not admitted here")
    if enveloped:
        doc = copy.deepcopy(tree)
        sig_copy = _follow(doc.getroot(), _steps(sig))
        target = doc if uri == "" else _follow(doc.getroot(), _steps(_by_id(tree, uri[1:])))
        _remove_keeping_tail(sig_copy)
    else:
        target = tree if uri == "" else _by_id(tree, uri[1:])
    return _c14n(target, c14n_alg, prefixes=prefixes, comments=False)


def read(sig) -> Signature:
    """What the signature carries, without verifying any of it."""
    si = _one(sig, f"{{{DS}}}SignedInfo", "ds:SignedInfo")
    refs = []
    for r in si.findall(f"{{{DS}}}Reference"):
        alg = _one(r, f"{{{DS}}}DigestMethod", "ds:DigestMethod").get("Algorithm")
        if alg not in DIGESTS:
            raise XmlSigError(f"digest {alg} is not admitted")
        refs.append(Reference(uri=r.get("URI") or "", type=r.get("Type"), digest_alg=DIGESTS[alg],
                              digest=_b64(r.findtext(f"{{{DS}}}DigestValue"), "ds:DigestValue")))
    if not refs:
        raise XmlSigError("ds:SignedInfo with no reference")
    certs = [_b64(c.text, "ds:X509Certificate") for c in sig.iterfind(f".//{{{DS}}}KeyInfo//{{{DS}}}X509Certificate")]
    qp = sig.find(f"{{{DS}}}Object/{{{XADES}}}QualifyingProperties")
    signing_time = timestamp = None
    ocsp: list[bytes] = []
    crls: list[bytes] = []
    if qp is not None:
        st = qp.find(f".//{{{XADES}}}SignedSignatureProperties/{{{XADES}}}SigningTime")
        signing_time = st.text.strip() if st is not None and st.text else None
        unsigned = qp.find(f"{{{XADES}}}UnsignedProperties/{{{XADES}}}UnsignedSignatureProperties")
        if unsigned is not None:
            ts = unsigned.find(f"{{{XADES}}}SignatureTimeStamp/{{{XADES}}}EncapsulatedTimeStamp")
            timestamp = _b64(ts.text, "EncapsulatedTimeStamp") if ts is not None else None
            for c in unsigned.iterfind(f"{{{XADES}}}CertificateValues/{{{XADES}}}EncapsulatedX509Certificate"):
                der = _b64(c.text, "EncapsulatedX509Certificate")
                if der not in certs:
                    certs.append(der)
            for o in unsigned.iterfind(f".//{{{XADES}}}OCSPValues/{{{XADES}}}EncapsulatedOCSPValue"):
                ocsp.append(_b64(o.text, "EncapsulatedOCSPValue"))
            for c in unsigned.iterfind(f".//{{{XADES}}}CRLValues/{{{XADES}}}EncapsulatedCRLValue"):
                crls.append(_b64(c.text, "EncapsulatedCRLValue"))
    if qp is not None:
        signer = _signing_certificate(qp, certs)
        if signer is None:
            raise XmlSigError("XAdES: the SigningCertificate names none of the certificates the signature carries")
    elif certs:
        signer = certs[0]
    else:
        raise XmlSigError("the signature carries no certificate to verify it with")
    certs = [signer] + [c for c in certs if c != signer]
    return Signature(element=sig, references=refs, signer_der=signer, certs_der=certs, signing_time=signing_time,
                     timestamp_der=timestamp, ocsp_der=ocsp, crl_der=crls, xades=qp is not None)


def _signing_certificate(qp, certs: list[bytes]) -> bytes | None:
    """XAdES's SigningCertificateV2 (or the older SigningCertificate): the
    carried certificate whose digest it names. None when it names none
    of them — a signature whose signer is not identified."""
    ssp = qp.find(f"{{{XADES}}}SignedProperties/{{{XADES}}}SignedSignatureProperties")
    if ssp is None:
        raise XmlSigError("XAdES: no SignedSignatureProperties")
    sc = ssp.find(f"{{{XADES}}}SigningCertificateV2")
    if sc is None:
        sc = ssp.find(f"{{{XADES}}}SigningCertificate")
    if sc is None:
        raise XmlSigError("XAdES: no SigningCertificate(V2): the signer is not identified")
    for cert in sc.iterfind(f"{{{XADES}}}Cert"):
        dm = cert.find(f"{{{XADES}}}CertDigest/{{{DS}}}DigestMethod")
        dv = cert.find(f"{{{XADES}}}CertDigest/{{{DS}}}DigestValue")
        if dm is None or dv is None or dm.get("Algorithm") not in DIGESTS:
            continue
        want = _b64(dv.text, "CertDigest")
        for der in certs:
            if hashlib.new(DIGESTS[dm.get("Algorithm")], der).digest() == want:
                return der
    return None


def verify(sig, *, data: bool = True, require_xades: bool = True) -> Signature:
    """The signature verified, or `XmlSigError` naming what failed.

    `data=False` leaves the data references' digests un-recomputed: the
    signed bytes are not held (09 §9.15's archival mode, which compares
    that digest with `content_sha256` instead). The SignedProperties,
    inside the signature, are always recomputed."""
    from cryptography import x509
    s = read(sig)
    if require_xades and not s.xades:
        raise XmlSigError("not XAdES: no QualifyingProperties")
    si = sig.find(f"{{{DS}}}SignedInfo")
    ref_els = si.findall(f"{{{DS}}}Reference")
    if s.xades:
        props = [r for r in ref_els if r.get("Type") == SIGNED_PROPERTIES]
        if len(props) != 1:
            raise XmlSigError(f"XAdES: {len(props)} references to the SignedProperties; there must be one")
        target = _by_id(sig.getroottree(), (props[0].get("URI") or "#")[1:])
        if target.tag != f"{{{XADES}}}SignedProperties" or sig not in target.iterancestors():
            raise XmlSigError("XAdES: the SignedProperties reference names another element")
    for el, ref in zip(ref_els, s.references):
        if not data and ref.type != SIGNED_PROPERTIES:
            continue
        got = hashlib.new(ref.digest_alg, _dereference(sig, el)).digest()
        if got != ref.digest:
            raise XmlSigError(f"reference {ref.uri or '(the document)'}: the digest does not match — "
                              f"the signed content changed")
    method = _one(si, f"{{{DS}}}CanonicalizationMethod", "ds:CanonicalizationMethod")
    signed = _c14n(si, method.get("Algorithm"), prefixes=_prefixes(method))
    alg = _one(si, f"{{{DS}}}SignatureMethod", "ds:SignatureMethod").get("Algorithm")
    if alg not in SIGNATURES:
        raise XmlSigError(f"signature algorithm {alg} is not admitted")
    value = _b64(sig.findtext(f"{{{DS}}}SignatureValue"), "ds:SignatureValue")
    try:
        cert = x509.load_der_x509_certificate(s.signer_der)
    except ValueError as e:
        raise XmlSigError(f"the signing certificate does not read: {e}") from None
    if not _signature_valid(cert.public_key(), *SIGNATURES[alg], signed, value):
        raise XmlSigError("the signature value does not verify over the SignedInfo with the signer's key")
    return s


def _signature_valid(key, kind: str, hash_name: str, signed: bytes, value: bytes) -> bool:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
    from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
    h = {"sha256": hashes.SHA256, "sha384": hashes.SHA384, "sha512": hashes.SHA512}[hash_name]()
    try:
        if kind in ("rsa", "pss"):
            if not isinstance(key, rsa.RSAPublicKey):
                return False
            pad = padding.PKCS1v15() if kind == "rsa" else padding.PSS(mgf=padding.MGF1(h), salt_length=h.digest_size)
            key.verify(value, signed, pad, h)
        else:
            if not isinstance(key, ec.EllipticCurvePublicKey):
                return False
            n = (key.curve.key_size + 7) // 8
            if len(value) != 2 * n:
                return False
            der = encode_dss_signature(int.from_bytes(value[:n], "big"), int.from_bytes(value[n:], "big"))
            key.verify(der, signed, ec.ECDSA(h))
    except InvalidSignature:
        return False
    return True


def detached(sig) -> bytes:
    """The `ds:Signature` alone, with every `ds:Object` that is not the
    XAdES properties removed — an enveloping signature's content goes, as
    09 §9.15's DETACHED asks; SignedInfo is untouched, so it still
    verifies. Every namespace in scope where it stood is declared on it,
    so an inclusive canonicalisation renders the same declarations
    detached as in place."""
    from lxml import etree
    out = etree.Element(sig.tag, attrib=dict(sig.attrib), nsmap=sig.nsmap)
    out.text = sig.text
    for child in sig:
        if child.tag == f"{{{DS}}}Object" and child.find(f"{{{XADES}}}QualifyingProperties") is None:
            continue
        out.append(copy.deepcopy(child))
    return etree.tostring(out)
