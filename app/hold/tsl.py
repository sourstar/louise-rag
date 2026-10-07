"""Trusted lists (ETSI TS 119 612) and their own signatures (03 §3.7):
the chain of 03 §3.7 starts at "trusted list", and a list is trusted for
WHO SIGNED IT, not for its hash. Migration step 10 — until 2026-10-07 the
list was pinned by hash alone.

The EU's arrangement (ETSI TS 119 615): the List of Trusted Lists is
signed by a certificate the Official Journal announces; each of its
`OtherTSLPointer`s names a national list — its location, its territory —
and the certificates that may sign it. So:

  - a national list verifies when its XAdES signature verifies
    (app/hold/xmldsig.py) with a certificate the LOTL's pointer for its
    territory names, valid at the list's signing time;
  - the LOTL verifies when its signature verifies with a certificate the
    certifier was given from the Official Journal's announcement
    (`--lotl-signers`), valid at its signing time.

Nothing is fetched here.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timezone

from app.hold import xmldsig
from app.hold.xmldsig import XmlSigError

TL_NS = "http://uri.etsi.org/02231/v2#"
XML_MIME = "application/vnd.etsi.tsl+xml"


@dataclass(frozen=True)
class Pointer:
    location: str
    territory: str | None
    mime: str | None
    certs_der: tuple[bytes, ...]


def territory(tree) -> str | None:
    t = tree.getroot().find(f"{{{TL_NS}}}SchemeInformation/{{{TL_NS}}}SchemeTerritory")
    return t.text.strip() if t is not None and t.text else None


def pointers(lotl_tree) -> list[Pointer]:
    """Every `OtherTSLPointer` of a list of lists: where the list is, whose
    it is, and the certificates allowed to sign it."""
    out = []
    for p in lotl_tree.getroot().iter(f"{{{TL_NS}}}OtherTSLPointer"):
        loc = (p.findtext(f"{{{TL_NS}}}TSLLocation") or "").strip()
        terr = mime = None
        for info in p.iter(f"{{{TL_NS}}}OtherInformation"):
            for child in info:
                tag = child.tag.rsplit("}", 1)[-1]
                if tag == "SchemeTerritory" and child.text:
                    terr = child.text.strip()
                elif tag == "MimeType" and child.text:
                    mime = child.text.strip()
        certs = []
        for c in p.iter(f"{{{TL_NS}}}X509Certificate"):
            try:
                der = base64.b64decode("".join((c.text or "").split()), validate=True)
            except (ValueError, TypeError):
                continue
            if der and der not in certs:
                certs.append(der)
        out.append(Pointer(location=loc, territory=terr, mime=mime, certs_der=tuple(certs)))
    return out


def _at(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def verify_list(xml: bytes, allowed_der, *, what: str = "the trusted list") -> dict:
    """The list's enveloped XAdES signature verified, by a certificate in
    `allowed_der`, valid at the list's signing time. `{signer, signed_at,
    territory}`; `XmlSigError` naming what failed."""
    from cryptography import x509
    tree = xmldsig.parse(xml)
    sig = tree.getroot().find(f"{{{xmldsig.DS}}}Signature")
    if sig is None:
        raise XmlSigError(f"{what} is not signed")
    try:
        s = xmldsig.verify(sig)
    except XmlSigError as e:
        raise XmlSigError(f"{what}: {e}") from None
    allowed = set(allowed_der)
    if s.signer_der not in allowed:
        raise XmlSigError(f"{what} is signed by a certificate its pointer does not name "
                          f"({len(allowed)} named)")
    cert = x509.load_der_x509_certificate(s.signer_der)
    when = _at(s.signing_time)
    if when is None:
        raise XmlSigError(f"{what}: no signing time; the signer's validity cannot be judged")
    if not (cert.not_valid_before_utc <= when <= cert.not_valid_after_utc):
        raise XmlSigError(f"{what}: signed at {s.signing_time}, outside its signer's validity")
    return {"signer": cert.subject.rfc4514_string(), "signed_at": s.signing_time, "territory": territory(tree)}


def verify_against_lotl(tl_xml: bytes, lotl_xml: bytes) -> dict:
    """A national list verified by the certificates the LOTL's pointer for
    its territory names (every XML pointer of that territory: a list in
    transition is pointed at with both keys)."""
    terr = territory(xmldsig.parse(tl_xml))
    if not terr:
        raise XmlSigError("the trusted list names no SchemeTerritory")
    named: list[bytes] = []
    for p in pointers(xmldsig.parse(lotl_xml)):
        if p.territory == terr and (p.mime in (None, XML_MIME)):
            named.extend(p.certs_der)
    if not named:
        raise XmlSigError(f"the list of trusted lists has no pointer for territory {terr}")
    return verify_list(tl_xml, named, what=f"the trusted list of {terr}")


def verify_lotl(lotl_xml: bytes, signers_der) -> dict:
    """The LOTL verified by a certificate the Official Journal announced."""
    signers = list(signers_der)
    if not signers:
        raise XmlSigError("no LOTL signing certificate given: the list of trusted lists is trusted for who "
                          "signed it, and the Official Journal names who may")
    return verify_list(lotl_xml, signers, what="the list of trusted lists")


def load_certificates(data: bytes) -> list[bytes]:
    """Certificates as DER, from a PEM bundle or one DER certificate."""
    from cryptography import x509
    if b"-----BEGIN CERTIFICATE-----" in data:
        return [c.public_bytes(_der()) for c in x509.load_pem_x509_certificates(data)]
    try:
        return [x509.load_der_x509_certificate(data).public_bytes(_der())]
    except ValueError as e:
        raise XmlSigError(f"not a certificate (PEM or DER): {e}") from None


def _der():
    from cryptography.hazmat.primitives.serialization import Encoding
    return Encoding.DER
