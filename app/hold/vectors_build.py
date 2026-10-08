"""`hold vectors build` — this peer's own chunks, embedded here.

Migration plan step 4, the verb its sub-tasks never named and step 6 runs
at point 5. `export` turns vectors into a bundle and `import` turns a
bundle into vectors; this is where the first vectors of a collection come
from at all, and without it step 6's round trip has nothing to export.

THE REFUSALS COME FIRST, and two of them are not about this command.

  * The licence gate (invariant 9), because embedding is a USE of the
    text — a red-class corpus that may not be read may not be embedded
    either, and "the data arrived somehow" is not a bypass.
  * A PASSING CALIBRATION CHECK for this collection's space on the
    configured endpoint (07 §7.3: "`vectors build` and `share contribute`
    refuse (exit 2) without a passing check"). This is the one that
    matters, and it is what b10630 cost: an encoder build that drifted
    produced vectors nobody could tell from good ones until a sweep found
    them, months later, in a table nothing else would ever re-read. The
    statement is `state/calibration.json`, written by `hold embedder
    check --accept`, and it is checked against this Codex's `space_id`
    rather than merely existing — a statement about another space says
    nothing about this one.

THE UNIT IS THE BATCH (07 §7.7's table). Vectors are written every
`EMBED_SLICE_CHUNKS` chunks and never accumulated to the end of a shard,
so an interrupted run keeps what the GPU already produced. `upsert_book`
learned this the expensive way: a 21 MB book chunked into 5,335 pieces
took 5.3 hours in one call, and four restarts in an afternoon meant the
GPU produced nothing that survived.

WHAT IS ALREADY THERE IS NOT RE-EMBEDDED. A chunk with a stored vector is
skipped — which is what makes this resumable, and what makes it correct
to run after an import: a peer that received half a shard from a stranger
embeds the other half and no more.

WHAT IT DOES NOT DO. It does not emit bundles. 04 §4.5 says a contributor
emits one "every few hundred chunks" and 09 §9.10's worked example shows
~15,000; the choice is an open operator decision (migration plan, step
4's operator note) and it is not this command's to make quietly.
`hold vectors export` makes a bundle from a shard, which is what step 6
runs next.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from .. import layers as layers_mod
from . import codex as cx
from . import snapshot as snap
from .codex import CodexError


class BuildRefused(CodexError):
    """A rule stood in the way before anything was done (07 §7.7's exit 2)."""


@dataclass(frozen=True)
class Built:
    shards: int
    chunks_seen: int
    chunks_written: int
    tokens: int
    interrupted: bool = False

    @property
    def nothing_to_do(self) -> bool:
        return self.chunks_written == 0 and not self.interrupted


def require_statement(root, name: str, codex) -> dict:
    """07 §7.3's gate: a passing `hold embedder check` for THIS space.

    Not merely "a statement exists". 09 §9.12 puts the space id in the
    statement's body, and a collection whose encoder changed has a
    statement about the encoder it no longer runs — which is exactly the
    situation the gate exists for.
    """
    p = Path(root) / name / "state" / "calibration.json"
    if not p.is_file():
        raise BuildRefused(
            f"{name}: no calibration statement at state/calibration.json. "
            f"`vectors build` refuses without a passing check for this "
            f"collection's space on the configured endpoint (07 §7.3); run "
            f"`louise hold embedder check {name} --accept`")
    import json

    from . import embedder as emb
    try:
        body = emb.read_statement(json.loads(p.read_bytes()))
    except Exception as e:                              # noqa: BLE001
        # Through `read_statement`, which VERIFIES the envelope, so a
        # hand-edited statement is refused rather than read: the whole
        # value of the gate is that it cannot be satisfied by writing a
        # file that says pass.
        raise BuildRefused(
            f"{name}: state/calibration.json is not a valid statement "
            f"(09 §9.12): {e}") from e
    if body.get("space_id") != codex.space_id:
        raise BuildRefused(
            f"{name}: the calibration statement is for space "
            f"{str(body.get('space_id'))[:23]}… and this collection is "
            f"{codex.space_id[:23]}…. A statement about another space says "
            f"nothing about this one; re-run `hold embedder check --accept`")
    need = codex.space.calibration.min_pass
    if int(body.get("passed", 0)) < need:
        raise BuildRefused(
            f"{name}: the calibration statement records {body.get('passed')} "
            f"of the sample at tolerance {codex.space.tolerance} and the "
            f"quorum is {need}. The encoder does not reproduce this space, "
            f"and its vectors would be a drift nobody could see afterwards")
    return body


def chunks_of(codex, records: Sequence[dict]) -> list[dict]:
    """Every chunk of these records, through `layers.chunks_for_document`
    and never a plugin's chunker directly (invariant 1)."""
    out: list[dict] = []
    for rec in records:
        row = {"uid": rec["uid"], "native_id": rec.get("native_id", 0),
               "lang": rec.get("lang") or "und"}
        sections = [tuple(s) for s in rec.get("sections") or []]
        for c in layers_mod.chunks_for_document(codex, row, sections):
            out.append(dict(c, lang=row["lang"]))
    return out


def pending(codex, records: Sequence[dict],
            vectors_for: Callable[[list[str]], dict[str, list]]) -> list[dict]:
    """The chunks of these records that have no vector here yet."""
    chunks = chunks_of(codex, records)
    if not chunks:
        return []
    held = vectors_for([c["id"] for c in chunks]) or {}
    return [c for c in chunks if c["id"] not in held]


def delta_shards(previous: snap.Snapshot | None,
                 current: snap.Snapshot) -> list[snap.Shard]:
    """The shards `--delta ID` covers: those holding `delta.added ∪
    delta.changed` (07 §7.3).

    Derived from the shard ROOTS rather than by mapping the delta's uids
    back to shards, because a shard's root changes exactly when its
    records did — that is what `records_root` is for — and the mapping
    would mean reading every shard of the snapshot to find out.

    NOT `snapshot.reissued`, and the difference is a shard that is NEW.
    `reissued` answers "which shards were re-issued", so a slice's first
    shard — every shard of a growing corpus — is not one; here it is
    precisely the work to do. The first snapshot of a chain is every
    shard, because its whole content is `delta.added`.
    """
    if previous is None:
        return sorted(current.shards, key=lambda s: (s.slice, s.n))
    before = {(s.slice, s.n): s.root for s in previous.shards}
    return sorted((s for s in current.shards
                   if before.get((s.slice, s.n)) != s.root),
                  key=lambda s: (s.slice, s.n))


def build(codex, *, shards: Iterable[snap.Shard],
          records_for: Callable[[snap.Shard], Sequence[dict]],
          vectors_for: Callable[[list[str]], dict[str, list]],
          embed: Callable[[list[Any]], list[list[float]]],
          write: Callable[[list[dict]], Any],
          batch: int = 64, stop=None, log=None, dry_run: bool = False,
          before_write: Callable[[], None] | None = None) -> Built:
    """Embed what is missing, shard by shard, writing every `batch`.

    Everything about this peer arrives as a callable, as everywhere under
    `app/hold/`: the module needs no Lance table and no HTTP client to be
    tested, and one storage decision stays in one place.

    `before_write` stands between every batch's answers and its write (03
    §3.3: a vector is written only if the answer that carried it names a
    validated run — "checked on every answer, before the write"). It raises
    to stop; the batches already written stay written, this one is not.
    """
    say = log or (lambda *_a: None)
    seen = written = tokens = n_shards = 0
    interrupted = False

    for entry in shards:
        name = f"{entry.slice}/{entry.n:05d}"
        n_shards += 1
        todo = pending(codex, records_for(entry), vectors_for)
        seen += len(todo)
        if not todo:
            say(f"{name}: complete")
            continue
        say(f"{name}: {len(todo)} chunk(s) to embed")
        if dry_run:
            tokens += sum(int(c.get("tokens") or 0) for c in todo)
            continue

        for i in range(0, len(todo), max(1, batch)):
            if stop is not None and stop.is_set():
                # 07 §7.7: the unit is "the chunks in flight to the
                # encoder". What is written stays written and the next run
                # finds it stored and skips it.
                interrupted = True
                break
            part = todo[i:i + max(1, batch)]
            # each chunk as the encoder needs it: its text, or a pair layer's
            # dict with its `fields` (input forms, 09 §9.3)
            vectors = embed([layers_mod.chunk_input(c) for c in part])
            if before_write is not None:
                before_write()
            rows = [{"id": c["id"], "book_id": int(c["book_id"]),
                     "lang": c["lang"], "seq": int(c["seq"]),
                     "chapter": c["chapter"] or "", "text": c["text"],
                     "vector": list(v)}
                    for c, v in zip(part, vectors)]
            write(rows)
            written += len(rows)
            tokens += sum(int(c.get("tokens") or 0) for c in part)
        if interrupted:
            break

    return Built(n_shards, seen, written, tokens, interrupted)
