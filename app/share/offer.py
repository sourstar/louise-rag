"""The OFFER (04 §4.2): the collections neighbours announce that this
peer does not hold, with who serves each — and the one way to take one,
`share fetch`.

Every hourly DIGEST names what its sender holds and serves — collection
id, lineage, title, licence and privacy class, the latest snapshot, the
members offered (09 §9.9) — so the live neighbours' digests ARE the
offer; nobody sends a `codex` event for a collection it merely holds,
so the catalogue (app/share/catalogue.py) holds a Codex only when one
was announced as new, and the first version of this module, which read
the catalogue alone, showed an empty Library on both peers (operator,
2026-10-20: "I still don't see the list"). So: the offer is every
collection a live neighbour's digest lists, plus every Codex in the
catalogue, minus what this peer holds. The digest gives title, classes
and who serves it with which snapshot and how many members; the
catalogue's Codex, when there is one, adds the plugin, the summary and
the publication. A Codex another catalogue entry `continues` or
`replaces` is shown superseded and listed last, never hidden: the
choice is the operator's ("a peer chooses what it fetches").

`louise share offer` prints this; the Library's "Offered by peers" shows
it and queues `share fetch` for the worker (app/jobs.py SHARE_FETCH) —
one resolution, two readers (07 §7.4). Nothing here moves a byte.
"""
from __future__ import annotations

from contextlib import closing
from typing import Any

from app.share import catalogue, messages, peers


def held_ids() -> set[str]:
    """The collection ids this node holds — Codex collections and
    annotations collections alike."""
    from app import collections as collections_mod
    ids: set[str] = set()
    for c in list(collections_mod.load_all()) + list(collections_mod.load_annotations()):
        codex = getattr(c, "codex", None)
        if codex is not None:
            ids.add(codex.collection_id)
    return ids


def announced_in_digests(con, *, now: float) -> dict[str, dict[str, Any]]:
    """collection id -> what the live neighbours' digests say of it: the
    entry's own words (title, lineage, licence, privacy — the first digest
    to name it) and `served_by`, each neighbour with the snapshot it holds
    and how many members it serves (09 §9.9)."""
    out: dict[str, dict[str, Any]] = {}
    for n in peers.neighbours(con, now=now):
        digest = peers.digest_of(con, n["key"]) or {}
        for c in digest.get("collections") or ():
            if not isinstance(c, dict) or not c.get("collection_id"):
                continue
            cid = str(c["collection_id"])
            entry = out.setdefault(cid, {
                "collection_id": cid, "lineage_id": c.get("lineage_id") or cid,
                "title": c.get("title"), "license": c.get("license"), "privacy": c.get("privacy"),
                "served_by": []})
            entry["served_by"].append(
                {"key": n["key"], "address": n["address"], "snapshot_id": c.get("snapshot_id"),
                 "members": len(c.get("members") or [])})
    return out


def served_by(con, *, now: float) -> dict[str, list[dict[str, Any]]]:
    """collection id -> the live neighbours whose last digest lists it."""
    return {cid: e["served_by"] for cid, e in announced_in_digests(con, now=now).items()}


def cli_for(collection_id: str) -> str:
    return (f"louise share fetch {collection_id} [--slice …] [--kinds records,vectors] "
            f"[--encoder URL] [--name N]")


def offered(con, held: set[str], *, now: float) -> list[dict[str, Any]]:
    """What live neighbours' digests list and what the catalogue holds,
    minus what this peer holds; served-first."""
    announced = announced_in_digests(con, now=now)
    entries = {e["collection_id"]: e for e in catalogue.entries(con)}
    codexes = {cid: catalogue.codex_of(con, cid) for cid in entries}
    superseded: dict[str, str] = {}
    for cid, c in codexes.items():
        if c is None:
            continue
        for ref in (c.continues, c.replaces.codex if c.replaces else None):
            if ref:
                superseded.setdefault(ref, cid)
    out: list[dict[str, Any]] = []
    for cid in sorted(set(announced) | set(entries)):
        if cid in held:
            continue
        d, e, c = announced.get(cid), entries.get(cid), codexes.get(cid)
        out.append({
            "collection_id": cid,
            "lineage_id": (e["lineage_id"] if e else None) or (d["lineage_id"] if d else cid),
            # the catalogue knows the relation; a digest names the lineage only
            "relation": e["relation"] if e else ("root" if (d or {}).get("lineage_id") in (None, cid) else "in lineage"),
            "title": (e["title"] if e else None) or (c.descriptive.title if c else None) or (d["title"] if d else None),
            "summary": c.descriptive.summary if c else None,
            "plugin": c.plugin if c else None,
            "license": (c.license.cls if c else None) or (d["license"] if d else None),
            "privacy": (c.privacy if c else None) or (d["privacy"] if d else None),
            "publication": c.publication if c else None,
            "modality": c.modality if c else None,
            "announced_by": e["announced_by"] if e else (d["served_by"][0]["key"] if d else None),
            "first_seen": messages.time_text(e["first_seen"]) if e else None,
            "served_by": d["served_by"] if d else [],
            "superseded_by": superseded.get(cid),
            "codex_known": c is not None,         # the Codex itself is pulled by the take when not
            "cli": cli_for(cid),
        })
    # served by the most live peers first, then the oldest announcement;
    # a superseded Codex after every current one
    out.sort(key=lambda o: (o["superseded_by"] is not None, -len(o["served_by"]),
                            o["first_seen"] or "~", o["collection_id"]))
    return out


def offered_for_node(*, now: float | None = None) -> list[dict[str, Any]]:
    """The offer as this node sees it now: its catalogue, its neighbours,
    minus what it holds."""
    with closing(peers.connect()) as con:
        return offered(con, held_ids(), now=peers.now() if now is None else now)


def announced(collection_id: str, *, now: float | None = None) -> bool:
    """Is this collection in the offer at all — listed by a live neighbour's
    digest, or a Codex in the catalogue? `share fetch` finds its sources the
    same way (app/share/fetch.py `sources`)."""
    with closing(peers.connect()) as con:
        if catalogue.codex_of(con, collection_id) is not None:
            return True
        return collection_id in announced_in_digests(con, now=peers.now() if now is None else now)


def in_catalogue(collection_id: str) -> bool:
    with closing(peers.connect()) as con:
        return catalogue.codex_of(con, collection_id) is not None
