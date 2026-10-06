"""`ask lookup <id>` / `louise_lookup(id)` (05 §5.4; §5.2) — a fact by key
from the reference source that owns the id's namespace, never ranked.
Migration step 11.

A canonical id is `<namespace>:<id>`; the namespace is the reference
source's id (06 §6.5) and the id the origin's own, verbatim. The fact is
the row of the table that holds it — every column — with the source and
its `ref_version`; an id that resolves to no row is listed as unresolved,
and never produces an empty fact (05 §5.2). `louise_lookup` takes no
`as_of`: a reference source has no snapshots.
"""
from __future__ import annotations

from typing import Any

from app.hold.codex import CodexError


class LookupError_(CodexError):
    exit_code = 2


def split(canonical: str) -> tuple[str, str]:
    if not isinstance(canonical, str) or ":" not in canonical:
        raise LookupError_(f"a canonical id is <namespace>:<id> (05 §5.2), got {canonical!r}")
    ns, _, native = canonical.partition(":")
    ns = ns.strip().lower()
    if not ns or not native:
        raise LookupError_(f"a canonical id is <namespace>:<id> (05 §5.2), got {canonical!r}")
    return ns, native


def lookup(canonical: str, *, store=None) -> dict[str, Any]:
    """`{facts: [{entity_id, kind, source_id, ref_version, label, fields}], unresolved: [id]}`."""
    from app import refs
    ns, native = split(canonical)
    store = store or refs.get_store()
    facts: list[dict[str, Any]] = []
    for table in store.tables():
        try:
            if store.owner_of(table) != ns:
                continue
            row = store.get(table, native)
        except Exception:   # noqa: BLE001 — a table that cannot be read holds no fact
            continue
        if row:
            facts.append({"entity_id": f"{ns}:{native}", "kind": table, "source_id": ns,
                          "ref_version": _version(store, ns),
                          "label": row.get("significance") or row.get("name") or row.get("symbol"),
                          "fields": dict(row)})
    return {"facts": facts, "unresolved": [] if facts else [f"{ns}:{native}"]}


def _version(store, source_id: str) -> str | None:
    """The source's `ref_version` (06 §6.5), where the store records one."""
    try:
        row = store.con.execute("SELECT ref_version FROM ref_meta WHERE source_id=?",
                                (source_id,)).fetchone()
        return str(row[0]) if row and row[0] is not None else None
    except Exception:   # noqa: BLE001 — never synced: no version to name
        return None
