"""Reading the Wikidata JSON entity dump (docs/notes/wikidata-build-plan.md
§2 "The pack"): `wikidata-<date>-all.json.bz2` (or `.gz`, or the plain
file in a test) is ONE JSON array, written one entity a line —

    [
    {"type":"item","id":"Q31",…},
    {"type":"item","id":"Q8",…},
    …
    {"type":"property","id":"P6",…}
    ]

— every line but the last ending in `,`. The reader streams it and yields
each entity's exact LINE BYTES without the trailing comma and newline (what
`origin_hash` covers: this document's own bytes, 06 §6.4) beside the
decoded object and the entity's 0-based index in file order (the member
index a locator names). It stops cleanly at a truncated stream — a probe's
Range of the first megabytes, or a download cut short — reporting how far
it got, so that one bad tail costs one error and never a crash of the pass.
"""
from __future__ import annotations

import bz2
import gzip
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Iterator

#: a line this long is not an entity of any dump published so far (the
#: largest items are a few MB); a longer one is a corrupt stream, not data
MAX_LINE = 256 * 1024 * 1024


class DumpError(ValueError):
    """The bytes are not an entity dump this reader knows."""


@dataclass(frozen=True)
class Entity:
    index: int            # 0-based, file order — the member index of the locator
    raw: bytes            # the line's own bytes, no trailing comma, no newline
    obj: dict[str, Any]


@dataclass
class ReadResult:
    """What a pass over a dump reports beside the entities it yielded."""
    entities: int = 0
    complete: bool = False          # the closing `]` was read
    truncated_at: int | None = None  # bytes decoded when the stream ended early


def open_dump(path: Path) -> BinaryIO:
    """The dump decompressed as a binary stream, by its suffix: `.bz2`
    (every stream of a multi-stream file, as `bz2.open` reads them), `.gz`,
    or plain. The stream is read once, forward."""
    name = str(path)
    if name.endswith(".bz2"):
        return bz2.open(path, "rb")
    if name.endswith(".gz"):
        return gzip.open(path, "rb")
    return open(path, "rb")


def _strip_line(line: bytes) -> bytes:
    line = line.rstrip(b"\r\n")
    if line.endswith(b","):
        line = line[:-1]
    return line.rstrip()


def iter_entities(stream: BinaryIO, *, start_index: int = 0, result: ReadResult | None = None,
                  tolerate_truncation: bool = True, skip: int = 0) -> Iterator[Entity]:
    """The entities of an open dump stream, in order. `result` is filled
    as the read goes: `complete` when the array's `]` is seen,
    `truncated_at` when the stream ends before it (an EOFError of the
    decompressor, or an incomplete last line) — tolerated by default, so a
    probe's head yields what it holds; a build that must read the whole
    dump passes `tolerate_truncation=False` and gets `DumpError`. `skip`
    passes over the first entity lines WITHOUT parsing them — a resumed
    pack build pays the decompression again, never the JSON (pack.py)."""
    result = result if result is not None else ReadResult()
    reader = stream                       # bz2/gzip/plain files all read lines; no second buffer over them
    index = start_index
    opened = False
    consumed = 0
    to_skip = skip
    try:
        while True:
            line = reader.readline()
            if not line:
                break
            consumed += len(line)
            if len(line) > MAX_LINE:
                raise DumpError(f"a line of more than {MAX_LINE} bytes at entity {index}: not an entity dump")
            s = line.strip()
            if not opened:
                if not s:
                    continue
                if s == b"[":
                    opened = True
                    continue
                if s.startswith(b"["):           # `[{...},` on one line: an older dump's framing
                    opened = True
                    line = line.lstrip()[1:]
                    s = line.strip()
                    if not s:
                        continue
                else:
                    raise DumpError("the dump does not open with `[`: not a Wikidata entity dump")
            if s == b"]" or s == b"],":
                result.complete = True
                break
            if to_skip > 0 and line.endswith(b"\n") and _strip_line(line):
                to_skip -= 1
                index += 1
                continue
            if not line.endswith(b"\n"):
                # the last line of a cut stream: incomplete unless it parses
                raw = _strip_line(line)
                try:
                    obj = json.loads(raw)
                except ValueError:
                    result.truncated_at = consumed
                    break
            else:
                raw = _strip_line(line)
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except ValueError as e:
                    raise DumpError(f"entity {index}: not JSON ({e})") from None
            if not isinstance(obj, dict) or not isinstance(obj.get("id"), str):
                raise DumpError(f"entity {index}: an entity is an object with an `id`")
            yield Entity(index, raw, obj)
            index += 1
            result.entities = index - start_index
    except EOFError:
        # bz2/gzip: the compressed stream ended before its end-of-stream marker
        result.truncated_at = consumed
    if not result.complete and result.truncated_at is None and opened:
        result.truncated_at = consumed            # no `]`: the file ends inside the array
    if not result.complete and not tolerate_truncation:
        raise DumpError(f"the dump ends after {result.entities} entit(ies) without its closing `]` "
                        f"(decoded {result.truncated_at or consumed} bytes): incomplete")


def read_all(path: Path, **kw) -> tuple[list[Entity], ReadResult]:
    """Every entity of a (small) dump, for tests and probes."""
    result = ReadResult()
    with open_dump(path) as f:
        out = list(iter_entities(f, result=result, **kw))
    return out, result
