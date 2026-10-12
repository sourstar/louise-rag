"""Part patches: a new version of a record part for the price of the lines
that changed (docs/notes/part-deltas.md, option D; operator, 2026-10-12).

A peer that holds an older version of a part asks a peer that holds the new
one, with a signed `pull patch` (09 §9.9): the part, the snapshot it wants,
and the SHA-256 of every line of the version it holds. The answering peer
decompresses its new part, and returns the lines the asker lacks, the order
of all lines, and the two numbers its index carries (lines per frame,
chunks). The asker rebuilds the part with the platform's writer
(`records.write_lines`) and promotes it only if it hashes to the root the
signed snapshot lists — the root stays the proof, a patch only saves bytes.
A miss (a peer that lied, or a writer built differently) costs the full part
by torrent, never a ban by itself.

The answering peer keeps each patch it computed for `[share]
patch_cache_s` seconds (07 §7.8; 0 = delete once served), keyed by the new
root and the asker's set of lines: after a new snapshot many peers holding
the same old version ask for the same patch (operator, 2026-10-12).

A frame cannot be sent as it is: inside a zstd frame a book is encoded as
references to the books before it in the same frame, so its compressed
bytes have no boundary and cannot be decoded alone; parts keep one frame
per FRAME_RECORDS lines (operator: no frame split). So the rebuild
recompresses, and lands for certain only under the producer's zstd: the
snapshot's part entry names it (`writer`, 09 §9.4), and a peer under
another one does not ask — it fetches the part.

A PLATES CONTAINER (`figures/<slice>/<NNNNN>.fig.zst`, 09 §9.5) is the
same request by plate hash (`kind: "figures"`): the asker names the plates
its container holds, the answer carries the frames of the plates it lacks
VERBATIM — a container is one independent zstd frame per plate — and the
asker lays its own frames and those out again (`figures.write_frames`).
Nothing is recompressed, so no writer condition.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Callable, Iterable, Iterator

from app.hold import figures as figures_mod
from app.hold.codex import CodexError, canonical_json
from app.pack import records as pack_records

PATCH_VERSION = 1
#: the most line hashes a request may carry (a pubmed part holds 30,000)
MAX_HAVE = 200_000
#: a part as a request names it: `<slice>/<NNNNN>`, the slice one or more `key=value`
PART_RE = re.compile(r"^[a-z_]+=[^/]+(/[a-z_]+=[^/]+)*/\d{5}$")
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
KINDS = ("records", "figures")
#: a cached patch, either kind
CACHE_SUFFIX = ".patch"


class PatchError(CodexError):
    """A patch that does not apply: a line it names is not in the old part,
    or the bytes are not a patch."""


def part_of(path: str) -> str | None:
    """`records/lang=en/00001.jsonl.zst` -> `lang=en/00001`; None for any other file."""
    if not path.startswith("records/") or not path.endswith(pack_records.SUFFIX):
        return None
    part = path[len("records/"):-len(pack_records.SUFFIX)]
    return part if PART_RE.match(part) else None


def figures_part_of(path: str) -> str | None:
    """`figures/lang=en/00001.fig.zst` -> `lang=en/00001`; None for any other file."""
    if not path.startswith("figures/") or not path.endswith(figures_mod.SUFFIX):
        return None
    part = path[len("figures/"):-len(figures_mod.SUFFIX)]
    return part if PART_RE.match(part) else None


def answer_path(kind: str, part: str) -> str:
    """The item an answer carries: the records patch keeps its first name."""
    return f"patch/{part}.json.gz" if kind == "records" else f"patch/figures/{part}.patch"


def lines_of(part: Path) -> list[bytes]:
    ix = pack_records.read_index(Path(part))
    return [line for k in range(len(ix.frames)) for line in pack_records.read_frame(Path(part), ix, k)]


def line_hash(line: bytes) -> str:
    return hashlib.sha256(line.rstrip(b"\n")).hexdigest()


def have_of(part: Path) -> list[str]:
    """What an asker sends: the hash of every line of the version it holds."""
    return [line_hash(line) for line in lines_of(part)]


def make(new_part: Path, have: Iterable[str]) -> bytes:
    """The patch from the lines `have` names to `new_part`, gzipped
    canonical JSON (deterministic: gzip without a time)."""
    ix = pack_records.read_index(Path(new_part))
    known = set(have)
    order: list = []
    new: list[str] = []
    for line in lines_of(new_part):
        h = line_hash(line)
        if h in known:
            order.append(h)
        else:
            order.append(len(new))
            new.append(line.rstrip(b"\n").decode("utf-8"))
    body = {"version": PATCH_VERSION, "records": ix.records, "chunks": ix.chunks,
            "frames": [int(f[3]) for f in ix.frames], "order": order, "new": new}
    return gzip.compress(canonical_json(body), mtime=0)


def apply(old_part: Path | None, patch: bytes, out: Path) -> Path:
    """Rebuild the new part at `out` from the old part's lines and `patch`."""
    try:
        body = json.loads(gzip.decompress(patch))
    except (OSError, ValueError) as e:
        raise PatchError(f"not a patch: {type(e).__name__}") from None
    if not isinstance(body, dict) or body.get("version") != PATCH_VERSION:
        raise PatchError("a patch of another version")
    old = {line_hash(line): line for line in lines_of(old_part)} if old_part is not None else {}
    lines: list[bytes] = []
    for o in body.get("order") or []:
        if isinstance(o, str):
            if o not in old:
                raise PatchError(f"the patch names a line the old part does not hold ({o[:12]}…)")
            lines.append(old[o])
        elif isinstance(o, int) and 0 <= o < len(body.get("new") or []):
            lines.append(body["new"][o].encode("utf-8"))
        else:
            raise PatchError("a patch entry that is neither a line hash nor a new line")
    if len(lines) != body.get("records"):
        raise PatchError(f"{len(lines)} lines rebuilt, the patch says {body.get('records')}")
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    pack_records.write_lines(out, lines, key_of=lambda line: json.loads(line)["uid"],
                             chunks=int(body.get("chunks") or 0),
                             frame_counts=[int(n) for n in body.get("frames") or []])
    return out


# ---- plates containers ---------------------------------------------------------------

def plates_of(container: Path) -> list[str]:
    """What an asker sends for a container: the plate hashes it holds."""
    return sorted(figures_mod.read_index(Path(container)))


def _frames(container: Path) -> Iterator[tuple[str, int, bytes]]:
    index = sorted(figures_mod.read_index(Path(container)).items())
    with open(container, "rb") as f:
        for h, e in index:
            f.seek(e.offset)
            yield h, e.bytes, f.read(e.frame_bytes)


def make_figures(new_container: Path, have: Iterable[str]) -> bytes:
    """The patch to `new_container` for a peer holding the plates `have`
    names: one canonical JSON line — every plate in hash order as
    `[hash, length, frame bytes or 0 when held]` — then the frames of the
    plates it lacks, verbatim, in that order."""
    known = set(have)
    plates, frames = [], []
    for h, length, frame in _frames(new_container):
        if h in known:
            plates.append([h, length, 0])
        else:
            plates.append([h, length, len(frame)])
            frames.append(frame)
    head = canonical_json({"version": PATCH_VERSION, "kind": "figures", "plates": plates})
    return head + b"\n" + b"".join(frames)


def apply_figures(old_container: Path, patch: bytes, out: Path) -> Path:
    """Rebuild the new container at `out` from the old one's frames and `patch`."""
    head, sep, rest = patch.partition(b"\n")
    try:
        body = json.loads(head)
    except ValueError:
        raise PatchError("not a plates patch") from None
    if not sep or not isinstance(body, dict) or body.get("version") != PATCH_VERSION \
            or body.get("kind") != "figures" or not isinstance(body.get("plates"), list):
        raise PatchError("a plates patch of another version")
    old = {h: frame for h, _, frame in _frames(old_container)}
    view = memoryview(rest)

    def frames() -> Iterator[tuple[str, int, bytes]]:
        at = 0
        for p in body["plates"]:
            if not (isinstance(p, list) and len(p) == 3 and isinstance(p[0], str)
                    and HASH_RE.match(p[0]) and all(isinstance(x, int) and x >= 0 for x in p[1:])):
                raise PatchError("a plates patch entry is [hash, length, frame bytes]")
            h, length, n = p
            if n == 0:
                if h not in old:
                    raise PatchError(f"the patch names a plate the old container does not hold ({h[:12]}…)")
                yield h, length, old[h]
            else:
                if at + n > len(view):
                    raise PatchError("a plates patch shorter than the frames it declares")
                yield h, length, bytes(view[at:at + n])
                at += n
        if at != len(view):
            raise PatchError("a plates patch longer than the frames it declares")
    out = Path(out)
    try:
        figures_mod.write_frames(out, frames())
    except (figures_mod.FigureError, PatchError) as e:
        out.with_name(out.name + ".tmp").unlink(missing_ok=True)
        raise PatchError(str(e)) from None
    return out


# ---- the answering peer's cache --------------------------------------------------------

def cache_key(new_root: str, have: Iterable[str]) -> str:
    return hashlib.sha256((new_root + "\n" + "\n".join(sorted(set(have)))).encode()).hexdigest()


def expire(cache_dir: Path, ttl_s: int, now: float | None = None) -> int:
    """Delete cached patches older than `ttl_s`; how many."""
    now = time.time() if now is None else now
    gone = 0
    for p in Path(cache_dir).glob("*" + CACHE_SUFFIX) if Path(cache_dir).is_dir() else []:
        try:
            if now - p.stat().st_mtime >= ttl_s:
                p.unlink()
                gone += 1
        except OSError:
            pass
    return gone


def cached(cache_dir: Path, new_root: str, have: list[str], build: Callable[[], bytes], *,
           ttl_s: int, now: float | None = None) -> bytes:
    """The patch for (`new_root`, `have`): from the cache when another peer
    asked the same, else built and kept for `ttl_s` (0: not kept — the
    patch is deleted once served)."""
    cache_dir = Path(cache_dir)
    expire(cache_dir, ttl_s, now)
    p = cache_dir / f"{cache_key(new_root, have)}{CACHE_SUFFIX}"
    if p.is_file():
        return p.read_bytes()
    data = build()
    if ttl_s > 0:
        cache_dir.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + f".{os.getpid()}.tmp")
        tmp.write_bytes(data)
        os.replace(tmp, p)
    return data


# ---- the asking peer -------------------------------------------------------------------

def ask(puller, sources, collection_id: str, snapshot_id: str, part: str, old_part: Path,
        out: Path, *, kind: str = "records", tries: int = 3,
        log: Callable[[str], None] | None = None) -> Path | None:
    """Ask up to `tries` peers for the patch from `old_part` to `part` as
    `snapshot_id` lists it, and rebuild the new part at `out`. None when
    nobody answered with a patch that applies: the caller fetches the whole
    part. The rebuilt file is unverified here — its root decides at
    promotion, as a downloaded file's does."""
    have = have_of(old_part) if kind == "records" else plates_of(old_part)
    rebuild = apply if kind == "records" else apply_figures
    for source in list(sources)[:tries]:
        try:
            items = puller.pull(source, "patch", collection_id, snapshot_id=snapshot_id,
                                part=part, have=have, kind=kind)
        except Exception as e:   # noqa: BLE001 — a peer away or a refusal: the next one
            if log:
                log(f"patch {part}: {source.address}: {type(e).__name__}"[:200])
            continue
        data = next((d for path, d in items if path == answer_path(kind, part)), None)
        if data is None:
            continue
        try:
            return rebuild(old_part, data, out)
        except (PatchError, OSError, ValueError, KeyError) as e:
            if log:
                log(f"patch {part}: {source.address}: does not apply ({e})"[:300])
            Path(out).unlink(missing_ok=True)
    return None
