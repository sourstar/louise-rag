"""`origin-seal` in LIVE mode (03 §3.7; 09 §9.15): the sealed files in hand —
re-fetched from the origin, or the optional `origin-<slice>` member —
each hashed to its entry's `file_hash`, the digest its envelope commits to
recomputed from its bytes, and its path validated NOW against the CURRENT
trusted list and fresh revocation responses. Migration step 10.

The current lists are fetched on the budget and trusted for who signed
them: the current LOTL by a certificate the LOTL held (the one the
certifier verified at capture) names for itself — the pivot the EU's
arrangement provides — and the current national list by a certificate the
current LOTL's pointer names. A list that cannot be had or judged makes
the check inconclusive, never a failure: deferral is not failure. A seal
that does not validate against them fails.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from app.hold import revocation as rev, seals, tsl, xmldsig

MAX_LIST = 64 << 20                    # Italy's list is 2.8 MiB; a list is never this large


class Undecided(Exception):
    """The current lists could not be had or judged: inconclusive."""


def _current_anchors(root: Path, name: str, trusted_list: dict[str, str], *, client, stop) -> list[bytes]:
    held_lotl = seals.held_trusted_list(root, name, trusted_list["lotl_sha256"])
    held_tl = seals.held_trusted_list(root, name, trusted_list["tl_sha256"])
    if held_lotl is None or held_tl is None:
        raise Undecided("the LOTL and the list the entry names are not held: the current lists' signers "
                        "cannot be judged")
    own = [p for p in tsl.pointers(xmldsig.parse(held_lotl)) if p.territory == "EU"]
    if not own or not own[0].location:
        raise Undecided("the LOTL held names no location or signer for itself")
    current_lotl = rev.get(own[0].location, limit=MAX_LIST, client=client, stop=stop)
    if current_lotl is None:
        raise Undecided(f"{own[0].location}: no answer")
    try:
        tsl.verify_list(current_lotl, [c for p in own for c in p.certs_der],
                        what="the current list of trusted lists")
    except xmldsig.XmlSigError as e:
        raise Undecided(f"{e} — a LOTL newer than the one held may name its new signer") from None
    terr = tsl.territory(xmldsig.parse(held_tl))
    ptr = [p for p in tsl.pointers(xmldsig.parse(current_lotl))
           if p.territory == terr and p.mime in (None, tsl.XML_MIME) and p.location]
    url = ptr[0].location if ptr else trusted_list.get("url")
    if not url:
        raise Undecided(f"the current LOTL points at no list for territory {terr}")
    current_tl = rev.get(url, limit=MAX_LIST, client=client, stop=stop)
    if current_tl is None:
        raise Undecided(f"{url}: no answer")
    try:
        tsl.verify_against_lotl(current_tl, current_lotl)
    except xmldsig.XmlSigError as e:
        raise Undecided(str(e)) from None
    return seals.anchors_from_trusted_list(current_tl)


def check(root: Path, name: str, entries: dict[str, dict[str, Any]], live_dir: Path, *, client=None,
          now: datetime | None = None, stop=None) -> tuple[str, dict[str, Any], str]:
    """`(status, detail, why)`: status `pass` · `fail` · `inconclusive`."""
    files = {seals.file_hash(p): p for p in sorted(Path(live_dir).rglob("*")) if p.is_file()}
    targets = [(fh, e) for fh, e in sorted(entries.items()) if fh in files]
    detail: dict[str, Any] = {"mode": "live", "entries": len(entries), "checked": 0, "failed": [],
                              "inconclusive": []}
    if not targets:
        return "inconclusive", detail, (f"none of the files under {live_dir} is a sealed file the member lists; "
                                        f"live mode re-validates the files in hand")
    own = client is None
    c = client or rev._client()
    why: list[str] = []
    anchors_by: dict[tuple, list[bytes] | str] = {}
    try:
        for fh, e in targets:
            key = (e["trusted_list"]["lotl_sha256"], e["trusted_list"]["tl_sha256"])
            if key not in anchors_by:
                try:
                    anchors_by[key] = _current_anchors(root, name, e["trusted_list"], client=c, stop=stop)
                except Undecided as u:
                    anchors_by[key] = str(u)
            anchors = anchors_by[key]
            if isinstance(anchors, str):
                detail["inconclusive"].append(fh)
                why.append(f"{fh[:12]}…: {anchors}")
                continue
            chain = [seals._unb64(x, "chain") for x in e["chain"]]
            fresh, unanswered = rev.fetch(chain, anchors, client=c, stop=stop)
            got = seals.validate_live(e, files[fh].read_bytes(), anchors=anchors, revocation=fresh, now=now)
            detail["checked"] += 1
            if got["ok"] is True:
                continue
            (detail["failed"] if got["ok"] is False else detail["inconclusive"]).append(fh)
            why.append(f"{fh[:12]}…: {got['why']}" + (f" (no answer for {', '.join(unanswered)[:200]})"
                                                     if unanswered and got["ok"] is None else ""))
    finally:
        if own:
            c.close()
    if detail["failed"]:
        return "fail", detail, "; ".join(why)
    if detail["inconclusive"]:
        return "inconclusive", detail, "; ".join(why)
    return "pass", detail, (f"{detail['checked']} sealed file(s) re-validated now against the current trusted "
                            f"list and fresh revocation responses")
