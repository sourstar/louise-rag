"""`hold vectors import` — where an admitted bundle's vectors land.

Migration plan step 4e, the second half. `admit.py` decides WHETHER a
bundle is admitted; this decides WHAT IS WRITTEN. They are separate
because the decision is a pure function of the bundle plus this peer's
hashes, while the write needs the corpus — and 03 §3.5's ladder has to be
runnable by a peer that is only checking.

WHY THE TEXT IS NOT IN THE BUNDLE. 09 §9.7 carries chunk ids and vectors
and nothing else, so the importer supplies its own text. That is not a
gap in the format, it is the point. Admission established that this
peer's `content_hash` for every landed record equals the producer's, and
chunking is a pure function of (cleaned text, chunker id, chunker
version) — invariant 3 — so this peer's cut of that record IS the
producer's cut, id for id and byte for byte. Re-deriving it is therefore
unambiguous, and it is also a check: a chunk id the bundle carries that
this peer's own cut does not produce is a row nobody can place, and it is
refused rather than written with a guess at its text.

THE KEY IS THE CHUNK ID, NEVER THE DOCUMENT. A bundle carries the chunks
its producer had embedded, which may be some of a record's and not all
(`export.rows_for` drops what is unembedded there), and this peer may
hold the rest from its own encoder. Delete-then-insert on the document
would throw away exactly the rows the bundle does not carry, so invariant
2's "keyed on uid / chunk id" here means the chunk id.

WHAT IT DOES NOT KNOW, like `export.py`: where vectors are stored. The
caller injects the writer, so `app/hold/` stays free of the corpus
machinery and this stays testable without a Lance table.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from .. import layers as layers_mod
from . import collection as col
from . import export as exp
from . import snapshot as snap
from .codex import CodexError


class LandError(CodexError):
    """The admitted rows cannot be placed against this peer's records."""


def snapshot_named(root, name: str, snapshot_id: str) -> snap.Snapshot:
    """The named snapshot from this peer's own drag.

    A bundle NAMES its snapshot, and `snapshot_id` is provenance (03
    §3.5): it says where the producer's records came from and gates
    nothing. So an importer that does not hold that snapshot is not
    looking at a bad bundle — it is looking at work from a round it never
    took — and the message says so rather than blaming the sender.
    """
    from . import drag as drag_mod
    path = Path(col.drag_of(Path(root), name)) / drag_mod.snapshot_path(snapshot_id)
    if not path.is_file():
        raise LandError(
            f"this peer holds no snapshot {snapshot_id!r} of {name}. The "
            f"bundle names it as the round its records come from; that is "
            f"provenance and admits nothing, but the records it points at "
            f"are what a `content_hash` is checked against")
    return snap.load(path)


def records_for(root, name: str, snapshot: snap.Snapshot, shard: str,
                uids: Iterable[str]) -> list[dict]:
    """This peer's records for the uids a bundle carries.

    Read from the shard the bundle names, because that is where the
    exporter read them and because a snapshot's roots cover shards. A uid
    the shard does not hold is simply absent from the result — it is not
    an error here, it is a record this peer does not have, and the rows
    that mention it become `unplaceable` below.
    """
    want = set(uids)
    drag = col.drag_of(Path(root), name)
    entry = exp._shard_of(snapshot, shard)
    from app.pack import records as pack_records
    return [r for r in pack_records.iter_records(drag / entry.path)
            if r["uid"] in want]


def cut(codex, records: Sequence[dict]) -> dict[str, dict]:
    """chunk id -> `{text, seq, chapter, book_id, lang}`, this peer's own.

    Through `layers.chunks_for_document` and never a plugin's chunker
    directly (invariant 1), which is also what makes a wrong layer a
    caught error rather than a reproduced one.
    """
    out: dict[str, dict] = {}
    for rec in records:
        uid = rec["uid"]
        row = {"uid": uid, "native_id": rec.get("native_id", 0)}
        sections = [tuple(s) for s in rec.get("sections") or []]
        for c in layers_mod.chunks_for_document(codex, row, sections):
            out[c["id"]] = {
                "text": c["text"], "seq": int(c["seq"]),
                "chapter": c["chapter"] or "",
                "book_id": int(c["book_id"]), "lang": rec.get("lang") or "und",
            }
    return out


def rows_to_write(codex, admitted, mine_cut: dict[str, dict],
                  ) -> tuple[list[dict], list[tuple[str, str]]]:
    """`(rows, unplaceable)` — the Lance rows for an admitted bundle.

    `rows` are ready to write: this peer's text and metadata carrying the
    producer's vector. `unplaceable` is `(chunk_id, why)` for every
    admitted row this peer's cut does not produce — see the module note;
    those are refusals, not warnings, because the alternative is a stored
    vector whose text nobody can reproduce.
    """
    rows: list[dict] = []
    unplaceable: list[tuple[str, str]] = []
    dim = codex.space.dim
    for cid, vector in admitted:
        c = mine_cut.get(cid)
        if c is None:
            unplaceable.append(
                (cid, "this peer's own cut of the record does not produce "
                      "this chunk id; chunking is deterministic and the "
                      "content hashes agreed, so there is no text to store "
                      "it against"))
            continue
        v = list(vector)
        if len(v) != dim:
            # Cannot happen past T0, which refuses the bundle whole on a
            # width mismatch. Kept because this is the last place before
            # a fixed-width Lance column, where the failure is a schema
            # error nobody can read back to a bundle.
            unplaceable.append((cid, f"{len(v)} components for a dim {dim} space"))
            continue
        rows.append({"id": cid, "book_id": c["book_id"], "lang": c["lang"],
                     "seq": c["seq"], "chapter": c["chapter"],
                     "text": c["text"], "vector": v})
    return rows, unplaceable


def land(codex, report, *, records: Sequence[dict],
         write: Callable[[list[dict]], Any]) -> dict:
    """Place an admitted report's rows and hand them to `write`.

    Refuses to write anything when the ladder failed: 03 §3.5's "refused
    whole" must mean nothing was written, and `admit` already empties
    `admitted` in that case — this is the second lock on the same door,
    because a caller that ignored `ok` would otherwise write zero rows
    silently and report success.
    """
    if not report.ok:
        failed = [t.tier for t in report.tiers if t.failed]
        raise LandError(
            f"the ladder refused this bundle at {', '.join(failed) or 'a tier'}; "
            f"nothing is written")
    rows, unplaceable = rows_to_write(codex, report.admitted, cut(codex, records))
    written = write(rows) if rows else 0
    return {"bundle_id": report.bundle_id, "producer": report.producer,
            "written": int(written or 0), "rows": len(rows),
            "unplaceable": tuple(unplaceable),
            "refused": report.refused, "padded": report.padded}
