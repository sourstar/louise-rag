"""The OFFER (04 §4.2): the collections neighbours announce that this
peer does not hold, with who serves each — and the one way to take one,
`share fetch`.

A digest names what its sender holds and serves; a `codex` event carries
the Codex itself into this peer's catalogue (app/share/catalogue.py),
whether or not it ever fetches it. The offer is the catalogue minus what
is held: each entry with the Codex's own words about itself — title,
plugin, licence class, privacy, publication — and the live neighbours
whose last digest serves it, with the snapshot and member count each
offers. A Codex another catalogue entry `continues` or `replaces` is
shown superseded and listed last, never hidden: the choice is the
operator's (operator, 2026-10-20: "a peer chooses what it fetches").

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


def served_by(con, *, now: float) -> dict[str, list[dict[str, Any]]]:
    """collection id -> the live neighbours whose last digest lists it, each
    with the snapshot it holds and how many members it serves."""
    out: dict[str, list[dict[str, Any]]] = {}
    for n in peers.neighbours(con, now=now):
        digest = peers.digest_of(con, n["key"]) or {}
        for c in digest.get("collections") or ():
            if not isinstance(c, dict) or not c.get("collection_id"):
                continue
            out.setdefault(str(c["collection_id"]), []).append(
                {"key": n["key"], "address": n["address"], "snapshot_id": c.get("snapshot_id"),
                 "members": len(c.get("members") or [])})
    return out


def cli_for(collection_id: str) -> str:
    return (f"louise share fetch {collection_id} [--slice …] [--kinds records,vectors] "
            f"[--encoder URL] [--name N]")


def offered(con, held: set[str], *, now: float) -> list[dict[str, Any]]:
    """The catalogue's entries this peer does not hold, served-first."""
    entries = catalogue.entries(con)
    codexes = {e["collection_id"]: catalogue.codex_of(con, e["collection_id"]) for e in entries}
    superseded: dict[str, str] = {}
    for cid, c in codexes.items():
        if c is None:
            continue
        for ref in (c.continues, c.replaces.codex if c.replaces else None):
            if ref:
                superseded.setdefault(ref, cid)
    serving = served_by(con, now=now)
    out: list[dict[str, Any]] = []
    for e in entries:
        cid = e["collection_id"]
        if cid in held:
            continue
        c = codexes.get(cid)
        out.append({
            "collection_id": cid,
            "lineage_id": e["lineage_id"],
            "relation": e["relation"],
            "title": e["title"] or (c.descriptive.title if c else None),
            "summary": c.descriptive.summary if c else None,
            "plugin": c.plugin if c else None,
            "license": c.license.cls if c else None,
            "privacy": c.privacy if c else None,
            "publication": c.publication if c else None,
            "modality": c.modality if c else None,
            "announced_by": e["announced_by"],
            "first_seen": messages.time_text(e["first_seen"]),
            "served_by": serving.get(cid, []),
            "superseded_by": superseded.get(cid),
            "cli": cli_for(cid),
        })
    # served by the most live peers first, then the oldest announcement;
    # a superseded Codex after every current one
    out.sort(key=lambda o: (o["superseded_by"] is not None, -len(o["served_by"]), o["first_seen"],
                            o["collection_id"]))
    return out


def offered_for_node(*, now: float | None = None) -> list[dict[str, Any]]:
    """The offer as this node sees it now: its catalogue, its neighbours,
    minus what it holds."""
    with closing(peers.connect()) as con:
        return offered(con, held_ids(), now=peers.now() if now is None else now)


def in_catalogue(collection_id: str) -> bool:
    with closing(peers.connect()) as con:
        return catalogue.codex_of(con, collection_id) is not None
