"""`freshness` (05 §5.3; 07 §7.3's `--freshness`): on request, the top
five results of each list are checked against the origin's CURRENT
version of their record — the peer asks the origin now, re-parses what it
serves with the collection's own parser, and compares the content hash
(09 §9.5: the title and the sections as the chunker sees them). Migration
step 11.

`current`: the same hash. `newer-at-source`: the origin's version differs;
the current text is shown beside the snapshot's AS A FACT — never
re-embedded, never fed into the index, never stored — for a green or an
acknowledged yellow collection; for a red one only the hash is compared
and the origin's page is linked. `unreachable`: the origin did not answer.
`unavailable` (amendment 2026-10-07 to 05 §5.3's three): the plugin has no
per-record current form for this record — a record read from a static
archive, or a copy built before records carried a content hash — so
there is nothing honest to compare; said rather than guessed.

Off by default, per request; each fetch draws from the peer's budget
(04 §4.3) and the fetches of one request run one at a time, within any
origin's etiquette. A plugin offers the check by a `current(ref, *,
client, workdir) -> RawDoc` method; `parse` is the one it always had.
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from app.plugins.base import DocRef, NoCurrentForm

TOP = 5
CURRENT, NEWER, UNREACHABLE, UNAVAILABLE = "current", "newer-at-source", "unreachable", "unavailable"
TEXT_CHARS = 20_000           # of the current text a fact carries
TIMEOUT_S = 20.0
USER_AGENT = "louise/freshness (05 §5.3: the current version of one record, on request)"


def _draw(n: int) -> None:
    try:
        from app.transfer.bucket import Priority
        from app.transfer.manager import Direction, get_manager
        get_manager().bucket(Direction.DOWN).acquire(n, Priority.INTERACTIVE)
    except Exception:   # noqa: BLE001 — accounting after the fact never loses the answer
        pass


def _size(raw) -> int:
    if raw.data is not None:
        return len(raw.data)
    try:
        return raw.path.stat().st_size if raw.path else 0
    except OSError:
        return 0


def _text(doc, *, limit: int = TEXT_CHARS) -> tuple[str, bool]:
    out: list[str] = []
    n = 0
    for name, body in doc.sections:
        piece = (f"{name}\n\n" if name else "") + (body or "")
        if n + len(piece) > limit:
            out.append(piece[: max(0, limit - n)])
            return "\n\n".join(out).rstrip(), True
        out.append(piece)
        n += len(piece) + 2
    return "\n\n".join(out).rstrip(), False


def check(collection, hits: list[dict[str, Any]], *, con, red: bool, client=None) -> dict[str, Any]:
    """One collection's results of one list, in rank order: the first TOP
    get `freshness`; `{facts, checked, states}`."""
    import httpx
    from app.hold import records as hold_records
    plugin = collection.plugin_instance()
    current = getattr(plugin, "current", None)
    facts: list[dict[str, Any]] = []
    states: dict[str, int] = {}
    checked = 0
    c = client or httpx.Client(timeout=TIMEOUT_S, follow_redirects=True, headers={"User-Agent": USER_AGENT})
    try:
        for hit in hits[:TOP]:
            uid = hit.get("uid")
            checked += 1
            row = con.execute("SELECT content_hash, path FROM documents WHERE uid=?", (uid,)).fetchone()
            if current is None:
                hit["freshness"] = UNAVAILABLE
                hit["freshness_why"] = f"the {collection.plugin} plugin has no per-record current form"
            elif row is None or not row["content_hash"]:
                hit["freshness"] = UNAVAILABLE
                hit["freshness_why"] = "this copy carries no content hash for the record (built before records)"
            else:
                ref = DocRef(source_id=collection.plugin, native_id=str(uid).rsplit(":", 1)[-1],
                             locator=str(row["path"] or ""))
                try:
                    with tempfile.TemporaryDirectory(prefix="louise-freshness-") as tmp:
                        raw = current(ref, client=c, workdir=Path(tmp))
                        _draw(_size(raw))
                        doc = plugin.parse(raw)
                        sections = [[n or "", b or ""] for n, b in doc.sections]
                        h = hold_records.content_hash(doc.title or "", sections)
                        if h == row["content_hash"]:
                            hit["freshness"] = CURRENT
                        else:
                            hit["freshness"] = NEWER
                            fact: dict[str, Any] = {"source": "origin", "kind": "current-version", "uid": uid,
                                                    "collection": collection.name, "title": doc.title or "",
                                                    "content_hash": h, "held_content_hash": row["content_hash"]}
                            if red:
                                fact["origin_url"] = hit.get("origin_url")
                            else:
                                text, truncated = _text(doc)
                                fact.update(text=text, truncated=truncated)
                            facts.append(fact)
                except NoCurrentForm as e:
                    hit["freshness"] = UNAVAILABLE
                    hit["freshness_why"] = str(e)[:300]
                except Exception as e:   # noqa: BLE001 — the origin did not answer, or answered nonsense
                    hit["freshness"] = UNREACHABLE
                    hit["freshness_why"] = f"{type(e).__name__}: {e}"[:300]
            states[hit["freshness"]] = states.get(hit["freshness"], 0) + 1
    finally:
        if client is None:
            c.close()
    return {"facts": facts, "checked": checked, "states": states}
