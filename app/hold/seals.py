"""Seals (03 §3.7; 09 §9.15): a certified collection's origin files carry
the issuer's own signature — the Gazzetta Ufficiale's CAdES `.p7m`, the
Official Journal's PAdES seal — and the certifier captures, when it
produces the snapshot, the evidence that validates them: the envelope,
the digest it commits to, the signer, the certificate chain, the
revocation responses of that moment, a timestamp, the trusted list the
chain validated against. `seals/<snapshot_id>.json` at the drag root, its
root and size in the snapshot's `files`, which the certifier signs.
Migration step 10 (operator's yes on the libraries, 2026-10-07).

Two modes of `origin-seal` (03 §3.7). ARCHIVAL, offline: the signature in
`seal` verified over `content_sha256`, the certificate path validated at
the captured moment against the captured `chain`, `revocation` and the
trusted list whose hash the entry names — a list this peer holds under
`state/trusted-lists/<sha256>.xml`; nothing fetched, so it still works
when the origin is gone. LIVE: the sealed file re-fetched, hashed to
`file_hash`, validated against the current lists — owed. A peer that
holds neither the sealed file nor the entry reports inconclusive.

What is captured as `seal` is the DETACHED envelope — the CMS SignedData
with its encapsulated content removed — so the evidence stays small
however large the issue, and archival mode verifies without the bytes:
the signed attributes carry the content's digest, and that digest is
`content_sha256`.

Libraries (requirements.txt): asn1crypto parses the CMS, cryptography
holds the keys, pyhanko verifies the signature's integrity and
pyhanko-certvalidator the path at a moment. No hand-rolled crypto.
"""
from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from xml.etree import ElementTree as ET

from app.hold import drag as drag_mod
from app.hold.codex import CodexError, canonical_json

KIND, VERSION = "louise-seals", 1
FORMATS = ("cades", "pades", "xades")
TRUSTED_LISTS = "trusted-lists"            # under the collection's state/
TL_NS = "http://uri.etsi.org/02231/v2#"
TL_GRANTED = ("http://uri.etsi.org/TrstSvc/TrstLSL/Svcstatus/granted",
              "http://uri.etsi.org/TrstSvc/TrstLSL/Svcstatus/recognisedatnationallevel")
TL_CA_TYPES = ("http://uri.etsi.org/TrstSvc/Svctype/CA/QC",
               "http://uri.etsi.org/TrstSvc/Svctype/CA/PKC")


class SealError(CodexError):
    exit_code = 2


# ---- the file ------------------------------------------------------------------------

def seals_path(drag_dir: Path, snapshot_id: str) -> Path:
    return Path(drag_dir) / drag_mod.seals_path(snapshot_id)


def file_hash(path: Path) -> str:
    """BLAKE2b-128 of the sealed file — what a record names in `extra.seal`."""
    h = hashlib.blake2b(digest_size=16)
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _unb64(text: str, what: str) -> bytes:
    try:
        return base64.b64decode(text, validate=True)
    except (ValueError, TypeError):
        raise SealError(f"{what}: not base64") from None


def validate_doc(doc: Any) -> dict[str, Any]:
    """09 §9.15's shape, refused field by field."""
    if not isinstance(doc, dict) or doc.get("kind") != KIND:
        raise SealError(f"not a {KIND} document (09 §9.15)")
    if doc.get("version") != VERSION:
        raise SealError(f"seals version {doc.get('version')!r}; this build reads {VERSION}")
    for k in ("collection_id", "snapshot_id"):
        if not isinstance(doc.get(k), str) or not doc[k]:
            raise SealError(f"seals: {k} is required")
    entries = doc.get("entries")
    if not isinstance(entries, list):
        raise SealError("seals: `entries` is a list")
    seen: list[str] = []
    for e in entries:
        validate_entry(e)
        seen.append(e["file_hash"])
    if seen != sorted(seen) or len(set(seen)) != len(seen):
        raise SealError("seals: entries are sorted by file_hash, each once")
    return doc


def validate_entry(e: Any) -> dict[str, Any]:
    if not isinstance(e, dict):
        raise SealError("a seal entry is an object")
    fh = e.get("file_hash")
    if not isinstance(fh, str) or len(fh) != 32 or any(c not in "0123456789abcdef" for c in fh):
        raise SealError("seal entry: file_hash is BLAKE2b-128 hex")
    if e.get("format") not in FORMATS:
        raise SealError(f"seal entry {fh[:8]}…: format is one of {', '.join(FORMATS)}")
    seal = e.get("seal")
    if not isinstance(seal, dict) or not isinstance(seal.get("b64"), str):
        raise SealError(f"seal entry {fh[:8]}…: seal is {{b64: string}}")
    _unb64(seal["b64"], "seal.b64")
    cs = e.get("content_sha256")
    if not isinstance(cs, str) or not cs.startswith("sha256:") or len(cs) != 7 + 64:
        raise SealError(f"seal entry {fh[:8]}…: content_sha256 is sha256:<64 hex>")
    signer = e.get("signer")
    if not isinstance(signer, dict) or not all(isinstance(signer.get(k), str) for k in ("subject", "issuer", "serial")):
        raise SealError(f"seal entry {fh[:8]}…: signer is {{subject, issuer, serial}}")
    chain = e.get("chain")
    if not isinstance(chain, list) or not chain or not all(isinstance(c, str) for c in chain):
        raise SealError(f"seal entry {fh[:8]}…: chain is a non-empty list of DER as base64, leaf first")
    for c in chain:
        _unb64(c, "chain")
    rev = e.get("revocation")
    if not isinstance(rev, list) or not all(isinstance(r, dict) and r.get("kind") in ("ocsp", "crl")
                                           and isinstance(r.get("b64"), str) and isinstance(r.get("time"), str)
                                           for r in rev):
        raise SealError(f"seal entry {fh[:8]}…: revocation is a list of {{kind: ocsp|crl, b64, time}}")
    ts = e.get("timestamp")
    if ts is not None and not (isinstance(ts, dict) and isinstance(ts.get("b64"), str) and isinstance(ts.get("time"), str)):
        raise SealError(f"seal entry {fh[:8]}…: timestamp is {{b64, time}} or null")
    tl = e.get("trusted_list")
    if not isinstance(tl, dict) or not all(isinstance(tl.get(k), str) for k in ("lotl_sha256", "tl_sha256", "url")):
        raise SealError(f"seal entry {fh[:8]}…: trusted_list is {{lotl_sha256, tl_sha256, url}}")
    if not isinstance(e.get("verified_at"), str):
        raise SealError(f"seal entry {fh[:8]}…: verified_at is a time")
    return e


def read(drag_dir: Path, snapshot_id: str) -> dict[str, Any] | None:
    """The seals member held for the snapshot, validated; None when not held."""
    p = seals_path(drag_dir, snapshot_id)
    if not p.is_file():
        return None
    try:
        doc = json.loads(p.read_bytes())
    except ValueError as e:
        raise SealError(f"{p.name}: not JSON: {e}") from None
    return validate_doc(doc)


def write(drag_dir: Path, codex, snapshot_id: str, entries: Iterable[dict[str, Any]]) -> Path:
    doc = {"kind": KIND, "version": VERSION, "collection_id": codex.collection_id, "snapshot_id": snapshot_id,
           "entries": sorted(entries, key=lambda e: e["file_hash"])}
    validate_doc(doc)
    p = seals_path(drag_dir, snapshot_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.tmp")
    tmp.write_bytes(canonical_json(doc))
    tmp.replace(p)
    return p


# ---- the trusted list ----------------------------------------------------------------

def trusted_list_dir(root: Path, name: str) -> Path:
    return Path(root) / name / "state" / TRUSTED_LISTS


def keep_trusted_list(root: Path, name: str, data: bytes) -> tuple[str, Path]:
    """The list held by its own hash — what an entry's `trusted_list`
    names and archival mode reads back. `(sha256:…, path)`."""
    digest = "sha256:" + hashlib.sha256(data).hexdigest()
    d = trusted_list_dir(root, name)
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{digest.split(':', 1)[1]}.xml"
    if not p.is_file():
        p.write_bytes(data)
    return digest, p


def held_trusted_list(root: Path, name: str, digest: str) -> bytes | None:
    p = trusted_list_dir(root, name) / f"{digest.split(':', 1)[-1]}.xml"
    if not p.is_file():
        return None
    data = p.read_bytes()
    if "sha256:" + hashlib.sha256(data).hexdigest() != digest:
        return None                       # a file that is not what its name says holds no anchors
    return data


def anchors_from_trusted_list(xml: bytes) -> list[bytes]:
    """The CA certificates (DER) a trusted list (ETSI TS 119 612) grants:
    every TSP service of a CA type whose status is granted, by the
    X509Certificate of its digital identity."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as e:
        raise SealError(f"trusted list: not XML: {e}") from None
    out: list[bytes] = []
    seen: set[bytes] = set()
    for svc in root.iter(f"{{{TL_NS}}}TSPService"):
        info = svc.find(f"{{{TL_NS}}}ServiceInformation")
        if info is None:
            continue
        kind = (info.findtext(f"{{{TL_NS}}}ServiceTypeIdentifier") or "").strip()
        status = (info.findtext(f"{{{TL_NS}}}ServiceStatus") or "").strip()
        if kind not in TL_CA_TYPES or status not in TL_GRANTED:
            continue
        for cert in info.iter(f"{{{TL_NS}}}X509Certificate"):
            try:
                der = base64.b64decode("".join((cert.text or "").split()), validate=True)
            except (ValueError, TypeError):
                continue
            if der and der not in seen:
                seen.add(der)
                out.append(der)
    return out


# ---- CAdES: the envelope -------------------------------------------------------------

def _name(x509_name) -> str:
    return x509_name.human_friendly


def parse_cades(der: bytes) -> dict[str, Any]:
    """A CMS SignedData (RFC 5652; CAdES, ETSI EN 319 122) read: the signer
    info, the message digest the signed attributes carry, the signer's
    certificate, every certificate the envelope carries (leaf first), the
    timestamp token when one is embedded, and the DETACHED envelope."""
    from asn1crypto import cms, tsp
    try:
        ci = cms.ContentInfo.load(der)
    except Exception as e:   # noqa: BLE001 — asn1crypto raises its own kinds
        raise SealError(f"not a CMS envelope: {e}") from None
    if ci["content_type"].native != "signed_data":
        raise SealError(f"not CMS SignedData: {ci['content_type'].native}")
    sd = ci["content"]
    infos = list(sd["signer_infos"])
    if not infos:
        raise SealError("CMS SignedData with no signer")
    si = infos[0]
    digest_alg = si["digest_algorithm"]["algorithm"].native
    if digest_alg != "sha256":
        raise SealError(f"the signer's digest is {digest_alg}; 09 §9.15 names content_sha256 — another digest "
                        f"needs the format amended, not guessed around")
    attrs = si["signed_attrs"]
    if attrs is None or attrs.native is None:
        raise SealError("CAdES: no signed attributes (the message digest lives there)")
    message_digest = None
    signing_time = None
    for a in attrs:
        t = a["type"].native
        if t == "message_digest":
            message_digest = a["values"][0].native
        elif t == "signing_time":
            signing_time = a["values"][0].native
    if not isinstance(message_digest, bytes):
        raise SealError("CAdES: no message-digest attribute")
    certs: list[bytes] = []
    parsed = []
    for c in (sd["certificates"] or []):
        if c.name != "certificate":
            continue
        parsed.append(c.chosen)
        certs.append(c.chosen.dump())
    sid = si["sid"]
    signer_cert = None
    if sid.name == "issuer_and_serial_number":
        issuer, serial = sid.chosen["issuer"], sid.chosen["serial_number"].native
        for c in parsed:
            if c.issuer == issuer and c.serial_number == serial:
                signer_cert = c
                break
    elif sid.name == "subject_key_identifier":
        ski = sid.chosen.native
        for c in parsed:
            if c.key_identifier == ski:
                signer_cert = c
                break
    if signer_cert is None:
        raise SealError("CAdES: the envelope does not carry the signer's certificate")
    chain = _order_chain(signer_cert, parsed)
    timestamp = None
    for a in (si["unsigned_attrs"] or []):
        if a["type"].native == "signature_time_stamp_token":
            token = a["values"][0]
            try:
                tst = tsp.TimeStampToken.load(token.dump()) if not isinstance(token, cms.ContentInfo) else token
                gen = tst["content"]["encap_content_info"]["content"].parsed["gen_time"].native
                timestamp = {"b64": _b64(token.dump()), "time": _time_text(gen)}
            except Exception:   # noqa: BLE001 — a token that does not read is not evidence
                timestamp = None
            break
    detached = _detach(ci)
    return {"signer_info": si, "signer_cert": signer_cert, "chain": chain, "chain_der": [c.dump() for c in chain],
            "message_digest": message_digest, "signing_time": signing_time, "timestamp": timestamp,
            "detached": detached, "content_type": sd["encap_content_info"]["content_type"].native}


def _order_chain(leaf, certs: list) -> list:
    """Leaf first, each followed by its issuer where the envelope carries it."""
    out = [leaf]
    rest = [c for c in certs if c.dump() != leaf.dump()]
    current = leaf
    while rest:
        nxt = next((c for c in rest if c.subject == current.issuer), None)
        if nxt is None:
            break
        out.append(nxt)
        rest = [c for c in rest if c.dump() != nxt.dump()]
        current = nxt
    return out


def _detach(ci) -> bytes:
    """The same envelope without its encapsulated content."""
    from asn1crypto import cms
    sd = ci["content"]
    # asn1crypto types a SignedData's encapsulated content as ContentInfo
    # (CMS v1's shape); without `content` it is the detached form
    eci = cms.ContentInfo({"content_type": sd["encap_content_info"]["content_type"]})
    fields: dict[str, Any] = {"version": sd["version"], "digest_algorithms": sd["digest_algorithms"],
                              "encap_content_info": eci, "signer_infos": sd["signer_infos"]}
    for optional in ("certificates", "crls"):
        if sd[optional] is not None and sd[optional].native is not None:
            fields[optional] = sd[optional]
    return cms.ContentInfo({"content_type": "signed_data", "content": cms.SignedData(fields)}).dump()


def _time_text(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---- PAdES: the envelope inside a signed PDF -----------------------------------------

def pades_envelope(pdf: bytes) -> bytes:
    """The CMS of a PDF's first signature field (PAdES, ETSI EN 319 142):
    a detached SignedData over the document's byte range, read with
    pyhanko's reader. The message digest its signed attributes carry is
    over that byte range, which is what `content_sha256` then names."""
    from io import BytesIO
    try:
        from pyhanko.pdf_utils.reader import PdfFileReader
        reader = PdfFileReader(BytesIO(pdf))
        sigs = list(reader.embedded_signatures)
    except Exception as e:   # noqa: BLE001 — not a PDF, or one pyhanko cannot read
        raise SealError(f"not a signed PDF: {type(e).__name__}: {e}") from None
    if not sigs:
        raise SealError("the PDF carries no signature")
    sig = sigs[0]
    try:
        return bytes(sig.sig_object["/Contents"]).rstrip(b"\x00")
    except Exception as e:   # noqa: BLE001
        raise SealError(f"the PDF's signature has no /Contents: {e}") from None


def parse_pades(pdf: bytes) -> dict[str, Any]:
    return parse_cades(pades_envelope(pdf))


# ---- capture ------------------------------------------------------------------------

def capture(sealed: Path, *, format: str = "cades", trusted_list: dict[str, str],
            revocation: Iterable[dict[str, str]] = (), now: float | None = None) -> dict[str, Any]:
    """One 09 §9.15 entry for a sealed origin file, from the file's own
    envelope: nothing is validated here — the certifier validates with
    `validate` before writing, and a verifier validates again."""
    import time as _time
    if format not in ("cades", "pades"):
        raise SealError(f"{format}: only CAdES and PAdES envelopes are captured in this build; XAdES is owed")
    data = Path(sealed).read_bytes()
    parsed = parse_pades(data) if format == "pades" else parse_cades(data)
    cert = parsed["signer_cert"]
    moment = datetime.fromtimestamp(_time.time() if now is None else now, tz=timezone.utc)
    return {
        "file_hash": file_hash(sealed),
        "format": format,
        "seal": {"b64": _b64(parsed["detached"])},
        "content_sha256": "sha256:" + parsed["message_digest"].hex(),
        "signer": {"subject": _name(cert.subject), "issuer": _name(cert.issuer),
                   "serial": str(cert.serial_number)},
        "chain": [_b64(c) for c in parsed["chain_der"]],
        "revocation": [dict(r) for r in revocation],
        "timestamp": parsed["timestamp"],
        "trusted_list": dict(trusted_list),
        "verified_at": _time_text(moment),
    }


# ---- validation ---------------------------------------------------------------------

def validate(entry: dict[str, Any], *, anchors: Iterable[bytes], moment: datetime | None = None,
             require_revocation: bool = False) -> dict[str, Any]:
    """Archival mode: the signature in `seal` verified over
    `content_sha256`, the path validated at `moment` — the timestamp's
    time, else `verified_at` — against the captured chain, revocation
    responses and the anchors given. `{ok, why}`; never raises on a bad
    seal, only on a malformed entry."""
    from asn1crypto import crl as crl_mod, ocsp as ocsp_mod, x509
    validate_entry(entry)
    if entry["format"] not in ("cades", "pades"):
        return {"ok": False, "why": f"{entry['format']}: validation of this format is owed; archival mode cannot "
                                    f"judge it in this build"}
    # a PAdES seal is a detached CMS over the PDF's byte range: once captured
    # it validates exactly as a CAdES one — the envelope over its digest
    try:
        parsed = parse_cades(_unb64(entry["seal"]["b64"], "seal.b64"))
    except SealError as e:
        return {"ok": False, "why": f"the envelope does not read: {e}"}
    want = entry["content_sha256"].split(":", 1)[1]
    if parsed["message_digest"].hex() != want:
        return {"ok": False, "why": "the envelope's message digest is not the entry's content_sha256"}
    chain = [x509.Certificate.load(_unb64(c, "chain")) for c in entry["chain"]]
    leaf = chain[0]
    if leaf.dump() != parsed["signer_cert"].dump():
        return {"ok": False, "why": "the entry's chain does not start with the envelope's signer"}
    intact, valid = _signature(parsed, leaf)
    if not (intact and valid):
        return {"ok": False, "why": "the signature does not verify over the signed attributes"}
    when = moment
    if when is None:
        text = (entry.get("timestamp") or {}).get("time") or entry["verified_at"]
        when = datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    ocsps, crls = [], []
    for r in entry["revocation"]:
        raw = _unb64(r["b64"], "revocation")
        try:
            if r["kind"] == "ocsp":
                ocsps.append(ocsp_mod.OCSPResponse.load(raw))
            else:
                crls.append(crl_mod.CertificateList.load(raw))
        except Exception:   # noqa: BLE001 — a response that does not read is not evidence
            continue
    roots = [x509.Certificate.load(a) for a in anchors]
    if not roots:
        return {"ok": False, "why": "no trust anchor: the trusted list grants no CA this path could end in"}
    why = _path(leaf, chain[1:], roots, when, ocsps, crls, require_revocation)
    if why:
        return {"ok": False, "why": why}
    return {"ok": True, "why": f"signature intact; path valid at {_time_text(when)} against {len(roots)} anchor(s)"}


def _signature(parsed: dict[str, Any], leaf) -> tuple[bool, bool]:
    """pyhanko's integrity check: the signed attributes' digest of the
    content is what we have (the entry's content_sha256 equals it), and
    the signature over the attributes verifies with the leaf's key."""
    from pyhanko.sign.validation import generic_cms
    try:
        status = generic_cms.validate_sig_integrity(
            parsed["signer_info"], leaf, expected_content_type=parsed["content_type"],
            actual_digest=parsed["message_digest"])
    except Exception:   # noqa: BLE001 — a key or algorithm pyhanko refuses
        return False, False
    if isinstance(status, tuple):
        return bool(status[0]), bool(status[1])
    return bool(getattr(status, "intact", False)), bool(getattr(status, "valid", False))


def _path(leaf, intermediates, roots, when, ocsps, crls, require_revocation: bool) -> str | None:
    import asyncio
    from pyhanko_certvalidator import CertificateValidator, ValidationContext
    from pyhanko_certvalidator.errors import PathValidationError
    try:
        ctx = ValidationContext(trust_roots=roots, other_certs=intermediates, moment=when, allow_fetching=False,
                                ocsps=ocsps or None, crls=crls or None,
                                revocation_mode="hard-fail" if require_revocation else "soft-fail")
        validator = CertificateValidator(leaf, intermediate_certs=intermediates, validation_context=ctx)
        asyncio.run(validator.async_validate_usage(set()))
    except PathValidationError as e:
        return f"the certificate path does not validate at {_time_text(when)}: {e}"
    except Exception as e:   # noqa: BLE001 — certvalidator's other refusals
        return f"the certificate path does not validate: {type(e).__name__}: {e}"
    return None
