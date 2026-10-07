"""Revocation evidence (09 §9.15 `revocation`; 03 §3.7): the OCSP responses
and CRLs that say, at a moment, that no certificate of a seal's chain was
revoked. Migration step 10 — until 2026-10-07 the entry's list was always
empty and validation ran soft-fail on revocation.

Two sources, both kept as 09 §9.15's `{kind, b64, time}`:
  - EMBEDDED in the envelope by its signer: a CMS SignedData's
    `crls` (CRLs, and OCSP responses as `other` revocation info), a PDF's
    Document Security Store, XAdES's `RevocationValues`;
  - FETCHED by the certifier at capture (`--fetch-revocation`) and by live
    mode: each certificate's OCSP responder (its AIA), else its CRL
    distribution point. Every byte up and down drawn from the node's
    budget (invariant 5); bounded; a responder that does not answer is
    absence of evidence, never a failure written anywhere.

An entry that carries any response is validated HARD-FAIL on revocation:
every certificate of the path that declares a responder must have an
answer among them, and a revoked one fails (app/hold/seals.py).
"""
from __future__ import annotations

import base64
from datetime import datetime, timezone
from typing import Any, Iterable

TIMEOUT_S = 20.0
MAX_OCSP = 1 << 20                       # an OCSP response is a few KiB
MAX_CRL = 32 << 20                       # a qualified CA's CRL can be tens of MiB
OCSP_RESPONSE_OID = "1.3.6.1.5.5.7.16.2"  # id-ri-ocsp-response (RFC 5940)


def _time(dt: datetime | None) -> str:
    dt = dt or datetime.now(timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def entry(kind: str, der: bytes) -> dict[str, str] | None:
    """09 §9.15's revocation item, its `time` the response's own (an OCSP
    response's producedAt, a CRL's thisUpdate). None for bytes that are
    not one."""
    from asn1crypto import crl as crl_mod, ocsp as ocsp_mod
    try:
        if kind == "ocsp":
            r = ocsp_mod.OCSPResponse.load(der)
            if r["response_status"].native != "successful":
                return None
            when = r.basic_ocsp_response["tbs_response_data"]["produced_at"].native
        elif kind == "crl":
            when = crl_mod.CertificateList.load(der)["tbs_cert_list"]["this_update"].native
        else:
            return None
    except Exception:   # noqa: BLE001 — bytes that do not read are not evidence
        return None
    return {"kind": kind, "b64": base64.b64encode(der).decode("ascii"), "time": _time(when)}


def _basic_to_response(basic_der: bytes) -> bytes:
    """A BasicOCSPResponse (as XAdES and CAdES embed it) wrapped as the
    OCSPResponse 09 §9.15 keeps and the validator reads."""
    from asn1crypto import ocsp as ocsp_mod
    basic = ocsp_mod.BasicOCSPResponse.load(basic_der)
    return ocsp_mod.OCSPResponse({"response_status": "successful",
                                  "response_bytes": {"response_type": "basic_ocsp_response",
                                                     "response": basic}}).dump()


def from_cms(signed_data) -> list[dict[str, str]]:
    """What a CMS SignedData's `crls` field carries."""
    out = []
    for choice in (signed_data["crls"] or []):
        if choice.name == "crl":
            e = entry("crl", choice.chosen.dump())
        elif choice.name == "other" and choice.chosen["other_rev_info_format"].dotted == OCSP_RESPONSE_OID:
            e = entry("ocsp", choice.chosen["other_rev_info"].dump())
        else:
            e = None
        if e:
            out.append(e)
    return out


def from_xades(ocsp_der: Iterable[bytes], crl_der: Iterable[bytes]) -> list[dict[str, str]]:
    """XAdES's RevocationValues: BasicOCSPResponses and CRLs."""
    out = []
    for der in ocsp_der:
        try:
            e = entry("ocsp", _basic_to_response(der))
        except Exception:   # noqa: BLE001
            e = None
        if e:
            out.append(e)
    for der in crl_der:
        e = entry("crl", der)
        if e:
            out.append(e)
    return out


def from_pdf(pdf: bytes) -> list[dict[str, str]]:
    """A PDF's Document Security Store (PAdES-LT)."""
    from io import BytesIO
    try:
        from pyhanko.pdf_utils.reader import PdfFileReader
        from pyhanko.sign.validation.dss import DocumentSecurityStore
        dss = DocumentSecurityStore.read_dss(PdfFileReader(BytesIO(pdf)))
    except Exception:   # noqa: BLE001 — no DSS, or one that does not read
        return []
    out = []
    for kind, items in (("ocsp", getattr(dss, "ocsps", []) or []), ("crl", getattr(dss, "crls", []) or [])):
        for item in items:
            der = item.dump() if hasattr(item, "dump") else bytes(item)
            e = entry(kind, der)
            if e:
                out.append(e)
    return out


def relevant(entries: Iterable[dict[str, str]], chain_der: list[bytes]) -> list[dict[str, str]]:
    """The responses about THIS chain: an OCSP response naming one of its
    serials, a CRL issued by one of its issuers. An entry carries its own
    evidence, not every response the certifier happened to hold — a CA's
    CRL is tens of MiB, and an issue a day would carry it thousands of
    times."""
    from asn1crypto import crl as crl_mod, ocsp as ocsp_mod, x509
    certs = [x509.Certificate.load(d) for d in chain_der]
    serials = {c.serial_number for c in certs}
    issuers = [c.issuer for c in certs]
    out = []
    for e in entries:
        try:
            raw = base64.b64decode(e["b64"], validate=True)
            if e["kind"] == "ocsp":
                rs = ocsp_mod.OCSPResponse.load(raw).basic_ocsp_response["tbs_response_data"]["responses"]
                keep = any(r["cert_id"]["serial_number"].native in serials for r in rs)
            else:
                issuer = crl_mod.CertificateList.load(raw)["tbs_cert_list"]["issuer"]
                keep = any(issuer == i for i in issuers)
        except Exception:   # noqa: BLE001
            keep = False
        if keep:
            out.append(e)
    return out


def merge(*lists: Iterable[dict[str, str]]) -> list[dict[str, str]]:
    """One list, each response once, in a stable order."""
    seen: dict[str, dict[str, str]] = {}
    for lst in lists:
        for e in lst:
            seen.setdefault(e["b64"], dict(e))
    return sorted(seen.values(), key=lambda e: (e["kind"], e["time"], e["b64"]))


# ---- fetched -------------------------------------------------------------------------

def _draw(direction_name: str, n: int, stop=None) -> None:
    from app.transfer.bucket import Priority
    from app.transfer.manager import Direction, get_manager
    try:
        get_manager().bucket(Direction[direction_name]).acquire(n, Priority.DELTA, stop=stop)
    except Exception:   # noqa: BLE001 — accounting never loses the evidence
        pass


def _client():
    import httpx
    return httpx.Client(timeout=TIMEOUT_S, follow_redirects=True)


def get(url: str, *, limit: int, client=None, stop=None) -> bytes | None:
    """A GET on the budget, bounded: the body, or None for no answer."""
    own = client is None
    c = client or _client()
    try:
        return _read(c, "GET", url, limit=limit, stop=stop)
    finally:
        if own:
            c.close()


def _read(client, method: str, url: str, *, limit: int, stop=None, **kw) -> bytes | None:
    import httpx
    if not url.lower().startswith(("http://", "https://")):
        return None                       # ldap:// and the like: not ours to speak
    body = kw.get("content") or b""
    if body:
        _draw("UP", len(body), stop)
    try:
        with client.stream(method, url, **kw) as r:
            data = bytearray()
            for chunk in r.iter_bytes():
                data.extend(chunk)
                if len(data) > limit:
                    _draw("DOWN", len(data), stop)
                    return None
            _draw("DOWN", len(data), stop)
            return bytes(data) if r.status_code == 200 else None
    except httpx.HTTPError:
        return None


def _ocsp_request(cert, issuer) -> bytes:
    from asn1crypto import ocsp as ocsp_mod
    cert_id = ocsp_mod.CertId({
        "hash_algorithm": {"algorithm": "sha1"},     # RFC 6960's CertID default: an identifier, not a seal
        "issuer_name_hash": issuer.subject.sha1,
        "issuer_key_hash": issuer.public_key.sha1,
        "serial_number": cert.serial_number,
    })
    return ocsp_mod.OCSPRequest({"tbs_request": {"request_list": [{"req_cert": cert_id}]}}).dump()


def fetch(chain_der: list[bytes], anchors_der: Iterable[bytes] = (), *, client=None,
          stop=None) -> tuple[list[dict[str, str]], list[str]]:
    """For every certificate of the chain but a trust anchor: its OCSP
    responder's answer, else its CRL. `(entries, unanswered)` — the
    subjects nobody answered for, so the caller can say so."""
    from asn1crypto import x509
    certs = [x509.Certificate.load(d) for d in chain_der]
    pool = certs + [x509.Certificate.load(d) for d in anchors_der]
    anchors = {bytes(d) for d in anchors_der}
    own = client is None
    c = client or _client()
    out: list[dict[str, str]] = []
    unanswered: list[str] = []
    try:
        for cert in certs:
            if cert.dump() in anchors or cert.self_signed == "maybe" or cert.self_signed == "yes":
                continue
            issuer = next((i for i in pool if i.subject == cert.issuer and i.dump() != cert.dump()), None)
            got = None
            if issuer is not None:
                for url in cert.ocsp_urls:
                    raw = _read(c, "POST", url, limit=MAX_OCSP, stop=stop, content=_ocsp_request(cert, issuer),
                                headers={"Content-Type": "application/ocsp-request"})
                    got = entry("ocsp", raw) if raw else None
                    if got:
                        break
            if got is None:
                for dp in cert.crl_distribution_points:
                    for url in (dp.url,) if dp.url else ():
                        raw = _read(c, "GET", url, limit=MAX_CRL, stop=stop)
                        got = entry("crl", raw) if raw else None
                        if got:
                            break
                    if got:
                        break
            if got is None:
                unanswered.append(cert.subject.human_friendly)
            else:
                out.append(got)
    finally:
        if own:
            c.close()
    return out, unanswered


def parsed(entries: Iterable[dict[str, Any]]):
    """`(ocsps, crls)` as the validator takes them; unreadable items skipped."""
    from asn1crypto import crl as crl_mod, ocsp as ocsp_mod
    ocsps, crls = [], []
    for r in entries:
        try:
            raw = base64.b64decode(r["b64"], validate=True)
            if r["kind"] == "ocsp":
                ocsps.append(ocsp_mod.OCSPResponse.load(raw))
            else:
                crls.append(crl_mod.CertificateList.load(raw))
        except Exception:   # noqa: BLE001
            continue
    return ocsps, crls
