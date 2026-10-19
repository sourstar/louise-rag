"""Annotations collections — claims about another collection's records
(docs/02-collections.md §2.10; docs/09-formats.md §9.3, §9.5, §9.14; the
design: docs/notes/annotation-collections.md). Operator, 2026-10-09.

A plugin's relations are what its ORIGIN states (05 §5.5): "this law amends
that one". A link WE find — "this Latin book and that English one are the
same work", made by a matcher, by Wikidata or by a person — is not stated
by Project Gutenberg, so it cannot be a relation of the gutenberg
collection. It is a side contribution: a collection of its own whose
records are CLAIMS about the target's records, whose relations member is
those claims as typed edges (here the annotator IS the source, and the
edges are its explicit statements), and which is shared, checked and
adopted like any collection. Nothing in the target changes.

    claim  = {type, subject, object, subject_lang, object_lang,
              method, evidence, confidence, contributor,
              subject_hash, object_hash, target_snapshot}

`type` is `same-work` (two records are versions of one work in different
languages — neither is said to be the other's source) or `translation-of`
(the subject translates the object: source language = `object_lang`,
target language = `subject_lang`). `subject_hash` / `object_hash` are the
two target records' `content_hash` as the target snapshot the claim was
checked against (`target_snapshot`) holds them: `create` reads them from
that snapshot's parts and refuses a claim about a record it does not hold.

THE TIME RULE (02 §2.10). The Codex names the target snapshot the claims
hold from (`annotates.from_snapshot`); no claim is attached to an answer
read from an earlier target snapshot. Read from a later one, a claim whose
two hashes still equal the target's is `current`; one whose record has
changed since is `stale` — still shown, labelled, because the claim was
made about other text. A deterministic method re-checks by re-running and
publishing the next annotations snapshot with the new hashes; nothing on a
reading peer re-runs a method.

A claim record is a 09 §9.5 record line: `sections` is one section,
`["claim", <the claim's canonical JSON>]`, so that `content_hash` covers
the whole claim and the `records` check verifies it unchanged; `origin` is
the claims file line it came from and `origin_hash` that line's bytes.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import shutil
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

from . import codex as cx
from .codex import Codex, CodexError

PLUGIN = cx.ANNOTATIONS_PLUGIN
SAME_WORK, TRANSLATION_OF = "same-work", "translation-of"
CLAIM_TYPES = (SAME_WORK, TRANSLATION_OF)
METHOD_KINDS = ("matcher", "wikidata", "human")
#: what an annotator writes in the claims file
CLAIM_KEYS = ("type", "subject", "object", "subject_lang", "object_lang",
              "method", "evidence", "confidence", "contributor")
#: what `create` pins from the target snapshot (an annotator MAY state them;
#: then they must agree)
PINNED_KEYS = ("subject_hash", "object_hash", "target_snapshot")
SLICE = "all"
SHARD_RECORDS = 50000
PARSER = cx.Parser(PLUGIN, 1)
CLAIMS_FILE = "claims.jsonl"
FORM = ("the claims file as its annotator submitted it, one JSON claim per line: the "
        "annotator is this collection's origin, so no public origin re-produces it (02 §2.10)")
CURRENT, STALE, UNKNOWN = "current", "stale", "unknown"

_LANG_RE = re.compile(r"^([a-z]{2,3}|und)$")
_CONF_RE = re.compile(r"^(0(\.[0-9]+)?|1(\.0+)?)$")
_HEX32_RE = re.compile(r"^[0-9a-f]{32}$")
_SNAP_RE = re.compile(r"^[0-9]{4}-([0-9]{2}|W[0-9]{2})$")


class AnnotationsError(CodexError):
    exit_code = 2


def edge_type(claim_type: str) -> str:
    """09 §9.14's `<plugin>:<name>`: `annotations:same-work`."""
    return f"{PLUGIN}:{claim_type}"


def is_annotations(codex: Any) -> bool:
    return cx.is_annotations(codex)


# --------------------------------------------------------------------------
# claims

def _nfc(obj: Any) -> Any:
    if isinstance(obj, str):
        return unicodedata.normalize("NFC", obj)
    if isinstance(obj, list):
        return [_nfc(v) for v in obj]
    if isinstance(obj, dict):
        return {_nfc(k): _nfc(v) for k, v in obj.items()}
    return obj


def check_claim(c: Any, where: str = "claim", *, target_plugin: str | None = None) -> dict[str, Any]:
    """One claim, refused with its reason or returned in its canonical form
    (strings NFC; no float anywhere, since a claim is hashed — 09 §9.1)."""
    if not isinstance(c, dict):
        raise AnnotationsError(f"{where}: a claim is a JSON object")
    missing = [k for k in CLAIM_KEYS if k not in c]
    unknown = sorted(k for k in c if k not in CLAIM_KEYS + PINNED_KEYS)
    if missing or unknown:
        raise AnnotationsError(
            f"{where}: " + "; ".join(x for x in (
                f"missing {', '.join(missing)}" if missing else "",
                f"unknown {', '.join(unknown)}" if unknown else "") if x)
            + f" (a claim is {', '.join(CLAIM_KEYS)}; 09 §9.5, amended 2026-10-09)")
    try:
        cx._reject_floats(c, where)
    except CodexError as e:
        raise AnnotationsError(f"{where}: {e} — write a ratio as a decimal string") from None
    c = _nfc(c)
    if c["type"] not in CLAIM_TYPES:
        raise AnnotationsError(f"{where}: type is one of {', '.join(CLAIM_TYPES)}, got {c['type']!r}")
    for k in ("subject", "object"):
        v = c[k]
        if not isinstance(v, str) or ":" not in v or not v.split(":", 1)[1]:
            raise AnnotationsError(f"{where}: {k} is a target record's uid <plugin>:<native_id>, got {v!r}")
        if target_plugin is not None and v.split(":", 1)[0] != target_plugin:
            raise AnnotationsError(f"{where}: {k} {v!r} is not a uid of the target, whose plugin "
                                   f"is {target_plugin!r}")
    if c["subject"] == c["object"]:
        raise AnnotationsError(f"{where}: subject and object are the same record")
    for k in ("subject_lang", "object_lang"):
        if not isinstance(c[k], str) or not _LANG_RE.match(c[k]):
            raise AnnotationsError(f"{where}: {k} is an ISO 639 code or \"und\", got {c[k]!r}")
    m = c["method"]
    if not isinstance(m, dict) or m.get("kind") not in METHOD_KINDS:
        raise AnnotationsError(f"{where}: method is {{kind: {' | '.join(METHOD_KINDS)}, …}}")
    if m["kind"] == "matcher":
        if set(m) != {"kind", "id", "version"} or not isinstance(m["id"], str) or not m["id"] \
                or not isinstance(m["version"], int) or isinstance(m["version"], bool):
            raise AnnotationsError(f"{where}: a matcher method is {{kind, id, version}}, the "
                                   f"version an integer — what a verifier re-runs")
    elif set(m) != {"kind"}:
        raise AnnotationsError(f"{where}: a {m['kind']} method is {{kind}} alone; what it saw "
                               f"goes in `evidence`")
    if not isinstance(c["evidence"], dict):
        raise AnnotationsError(f"{where}: evidence is an object — what the method matched on")
    if not isinstance(c["confidence"], str) or not _CONF_RE.match(c["confidence"]):
        raise AnnotationsError(f"{where}: confidence is a decimal string from 0 to 1, got "
                               f"{c['confidence']!r}")
    if not isinstance(c["contributor"], str) or not c["contributor"].strip():
        raise AnnotationsError(f"{where}: contributor names who makes the claim")
    for k in ("subject_hash", "object_hash"):
        if k in c and not (isinstance(c[k], str) and _HEX32_RE.match(c[k])):
            raise AnnotationsError(f"{where}: {k} is a content_hash, 32 lowercase hex digits")
    if "target_snapshot" in c and not (isinstance(c["target_snapshot"], str)
                                       and _SNAP_RE.match(c["target_snapshot"])):
        raise AnnotationsError(f"{where}: target_snapshot is a snapshot id")
    return c


@dataclass(frozen=True)
class Submitted:
    """One claim as the claims file gave it: its line number, its bytes
    (what `origin_hash` covers) and the claim."""
    line: int
    raw: bytes
    claim: dict[str, Any]


def _pair_key(c: dict[str, Any]) -> tuple:
    if c["type"] == SAME_WORK:                    # symmetric: one claim per pair
        return (c["type"],) + tuple(sorted((c["subject"], c["object"])))
    return (c["type"], c["subject"], c["object"])


def read_claims(data: bytes, *, target_plugin: str | None = None) -> list[Submitted]:
    """The claims file: JSON Lines, one claim per line, blank lines skipped.
    A claim made twice — the same pair, either way round for `same-work` —
    is refused, naming both lines."""
    out: list[Submitted] = []
    seen: dict[tuple, int] = {}
    for i, raw in enumerate(data.split(b"\n"), 1):
        raw = raw[:-1] if raw.endswith(b"\r") else raw
        if not raw.strip():
            continue
        try:
            obj = json.loads(raw)
        except ValueError as e:
            raise AnnotationsError(f"claims line {i}: not JSON ({e})") from None
        c = check_claim(obj, f"claims line {i}", target_plugin=target_plugin)
        k = _pair_key(c)
        if k in seen:
            raise AnnotationsError(f"claims line {i}: the same claim as line {seen[k]}")
        seen[k] = i
        out.append(Submitted(i, raw, c))
    if not out:
        raise AnnotationsError("the claims file holds no claim")
    return out


# --------------------------------------------------------------------------
# the target, as the snapshot the claims are checked against holds it

def _target_snapshot(shard_tree: Path, snapshot_id: str):
    from . import shard as shard_mod, snapshot as sn
    p = Path(shard_tree) / shard_mod.snapshot_path(snapshot_id)
    if not p.is_file():
        raise AnnotationsError(
            f"the target holds no snapshot {snapshot_id} ({p}): a claim is checked against "
            f"the records a snapshot this peer holds names (02 §2.10)")
    return sn.load(p)


def _part_file(shard_tree: Path, shard, current_id: str | None, snapshot_id: str) -> Path | None:
    """The file holding `shard` as `snapshot_id` lists it: the part's own
    path when that snapshot is the current one, else whichever version —
    current or superseded (09 §9.5) — has the listed root."""
    from . import shard as shard_mod
    newest = Path(shard_tree) / shard.path
    if current_id == snapshot_id:
        return newest if newest.is_file() else None
    cands = [newest] + sorted(newest.parent.glob(f"{shard.n:05d}.*.jsonl.zst"))
    for p in cands:
        if p.is_file() and shard_mod.pieces_root_of(p) == shard.root:
            return p
    return None


def target_records(shard_tree: Path, target: Codex, snapshot_id: str,
                   uids: Iterable[str]) -> dict[str, dict[str, Any]]:
    """uid -> {content_hash, lang} for every uid the target snapshot holds.
    Each part is read once and only the frames that hold a wanted uid are
    decompressed (09 §9.5's index), so the cost is the claims', never the
    target's size."""
    from app.pack import records as pack_records
    from app.share import seeder
    snap = _target_snapshot(shard_tree, snapshot_id)
    current = seeder.current_snapshot(Path(shard_tree))
    current_id = current.snapshot_id if current is not None else None
    order = (target.slicing or {}).get("order", "bytes")
    by_part: dict[tuple[str, int], tuple[Any, list[str]]] = {}
    for uid in sorted(set(uids)):
        for s in snap.shards_of(uid.split(":", 1)[1], order):
            by_part.setdefault((s.slice, s.n), (s, []))[1].append(uid)
    out: dict[str, dict[str, Any]] = {}
    for (_, _), (shard, wanted) in sorted(by_part.items()):
        path = _part_file(Path(shard_tree), shard, current_id, snapshot_id)
        if path is None:
            continue
        index = pack_records.read_index(path)
        frames: dict[int, list[tuple[str, int]]] = {}
        for uid in wanted:
            loc = index.uids.get(uid)
            if loc is not None:
                frames.setdefault(loc[0], []).append((uid, loc[1]))
        for k, items in sorted(frames.items()):
            lines = pack_records.read_frame(path, index, k)
            for uid, line in items:
                r = json.loads(lines[line])
                out[uid] = {"content_hash": r["content_hash"], "lang": r.get("lang")}
    return out


def pin(submitted: Sequence[Submitted], held: dict[str, dict[str, Any]],
        snapshot_id: str) -> list[dict[str, Any]]:
    """Each claim with the target records' hashes and the snapshot it was
    checked against. A record the snapshot does not hold, or a stated hash
    that is not the record's there, refuses the file — a claim about text
    we do not hold, or about other text, is not one we publish."""
    out, missing = [], []
    for s in submitted:
        c = dict(s.claim)
        for k in ("subject", "object"):
            got = held.get(c[k])
            if got is None:
                missing.append(f"line {s.line}: {c[k]}")
                continue
            stated = c.get(f"{k}_hash")
            if stated is not None and stated != got["content_hash"]:
                raise AnnotationsError(
                    f"claims line {s.line}: {k}_hash {stated} is not {c[k]}'s content_hash in "
                    f"snapshot {snapshot_id} ({got['content_hash']}): the claim was checked "
                    f"against other text")
            c[f"{k}_hash"] = got["content_hash"]
        if c.get("target_snapshot", snapshot_id) != snapshot_id:
            raise AnnotationsError(f"claims line {s.line}: checked against {c['target_snapshot']}, "
                                   f"and these are pinned to {snapshot_id}")
        c["target_snapshot"] = snapshot_id
        out.append(c)
    if missing:
        raise AnnotationsError(
            f"{len(missing)} claim end(s) name a record the target snapshot {snapshot_id} does "
            f"not hold: {'; '.join(missing[:8])}{' …' if len(missing) > 8 else ''}")
    return out


# --------------------------------------------------------------------------
# records and edges (09 §9.5, §9.14)

def blake128(data: bytes) -> str:
    return hashlib.blake2b(data, digest_size=16).hexdigest()


def claim_text(claim: dict[str, Any]) -> str:
    """The claim's one section: its canonical JSON (09 §9.1)."""
    return cx.canonical_json(claim).decode("utf-8")


def claim_of(record: dict[str, Any]) -> dict[str, Any]:
    """The claim a record line carries."""
    return json.loads(record["sections"][0][1])


def records_of(claims: Sequence[dict[str, Any]], submitted: Sequence[Submitted]) -> list[dict[str, Any]]:
    """The claim records, numbered from 1 in (subject, object, type) byte
    order — a rule, so two annotators building the same file cut the same
    records — each with its §9.5 hashes."""
    from . import records as rec
    pairs = sorted(zip(claims, submitted),
                   key=lambda cs: (cs[0]["subject"].encode(), cs[0]["object"].encode(),
                                   cs[0]["type"].encode()))
    out = []
    for n, (c, s) in enumerate(pairs, 1):
        sections = [["claim", claim_text(c)]]
        out.append({"uid": f"{PLUGIN}:{n}", "native_id": str(n), "lang": "und", "title": "",
                    "creators": [], "date": None, "subjects": [], "keys": [],
                    "sections": sections, "extra": {}, "origin": f"{CLAIMS_FILE}#{s.line}",
                    "content_hash": rec.content_hash("", [tuple(x) for x in sections]),
                    "origin_hash": blake128(s.raw)})
    return out


def edge_lines(records: Iterable[dict[str, Any]], snapshot_id: str) -> list[bytes]:
    """The relations of a part: each claim as its edge subject → object,
    typed `annotations:<type>`, sorted by (from, type, to) bytes (09 §9.14)."""
    from . import records as rec
    edges = []
    for r in records:
        c = claim_of(r)
        edges.append((c["subject"], edge_type(c["type"]), c["object"]))
    edges.sort(key=lambda e: (e[0].encode(), e[1].encode(), e[2].encode()))
    return [rec.edge_line(f, t, ty, snapshot_id) for f, ty, t in edges]


# --------------------------------------------------------------------------
# the Codex

def build_codex(target: Codex, from_snapshot: str, *, title: str, summary: str,
                annotator: str, platform_image: str, nonce: str | None = None,
                attribution: str | None = None, erasure: str = "retractable") -> Codex:
    """02 §2.10's Codex: every field present, the space and work null,
    no layers, the target and the snapshot the claims hold from."""
    codex = Codex(
        descriptive=cx.Descriptive(title, summary),
        plugin=PLUGIN,
        origin=cx.Origin({"source_id": PLUGIN, "nonce": nonce or secrets.token_hex(16)},
                         FORM, "file", "UTC", "record-date", None),
        toolchain=cx.Toolchain(platform_image, None),
        parser=PARSER,
        cleaning_version=1,
        protocol_version=1,
        layers=(),
        slicing={"keys": [], "unknown": SLICE, "shard_records": SHARD_RECORDS, "order": "numeric"},
        space=None,
        work=None,
        modality="text",
        erasure=erasure,
        validation=cx.Validation(cx.ANNOTATIONS_CHECKS, {"records": {"shards": "1"}}, ()),
        license=cx.License("green", None),
        privacy="public",
        update=cx.Update("monthly", 1, 7),
        publication="open",
        embedding="open",
        credits=(cx.Credit(cx.CORPUS, None, annotator, None,
                           attribution or f"Annotations by {annotator}", "all"),),
        facets=(),
        version=cx.ANNOTATIONS_VERSION,
        record_line=1,
        annotates=cx.Annotates(target.collection_id, from_snapshot),
    )
    cx.validate(codex)
    return codex


# --------------------------------------------------------------------------
# the SHARD

def write_parts(shard_tree: Path, records: Sequence[dict[str, Any]], snapshot_id: str):
    """Record parts and their relations parts, `SHARD_RECORDS` per part,
    and the snapshot entries for both."""
    from app.pack import records as pack_records
    from . import shard as shard_mod, records as rec, snapshot as sn
    shards, relations = [], []
    for n, start in enumerate(range(0, len(records), SHARD_RECORDS), 1):
        part = records[start:start + SHARD_RECORDS]
        p = Path(shard_tree) / shard_mod.shard_path(SLICE, n)
        pack_records.write_shard(p, part, record_line=1)
        raw = p.read_bytes()
        shards.append(sn.Shard(
            slice=SLICE, n=n, root=shard_mod.pieces_root(raw),
            records_root=rec.records_root(r["content_hash"] for r in part),
            first_id=part[0]["native_id"], last_id=part[-1]["native_id"],
            records=len(part), chunks=0, chunks_root=rec.chunks_root([]), tokens=0,
            bytes=len(raw), toolchain="own"))
        lines = edge_lines(part, snapshot_id)
        rp = Path(shard_tree) / shard_mod.relations_path(SLICE, n)
        pack_records.write_lines(rp, lines, key_of=lambda line: json.loads(line)["from"])
        rraw = rp.read_bytes()
        relations.append(sn.RelationShard(slice=SLICE, n=n, root=shard_mod.pieces_root(rraw),
                                          edges=len(lines), bytes=len(rraw)))
    return tuple(shards), tuple(relations)


def files_of(shard_tree: Path, snapshot_id: str) -> dict[str, Any]:
    """09 §9.4's `files` for an annotations collection: `validation.json`,
    `LICENSE-TERMS` (null: green) and the seals file (null: open). It has
    no calibration member — no space (09 §9.4, amended 2026-10-09)."""
    from . import shard as shard_mod
    out: dict[str, Any] = {}
    for name, required in ((shard_mod.VALIDATION_PATH, True), (shard_mod.TERMS_PATH, False),
                           (shard_mod.seals_path(snapshot_id), False)):
        p = Path(shard_tree) / name
        if p.is_file():
            raw = p.read_bytes()
            out[name] = {"root": shard_mod.pieces_root(raw), "bytes": len(raw)}
        elif required:
            raise AnnotationsError(f"{name} is missing from the SHARD")
        else:
            out[name] = None
    return out


def write_snapshot(shard_tree: Path, staging: Path, codex: Codex,
                   records: Sequence[dict[str, Any]], snapshot_id: str):
    """The parts, `validation.json` and the FIRST snapshot.

    The parts are cut in `staging` and promoted into the SHARD only when the
    snapshot is new, so nothing a snapshot already lists is ever
    overwritten: run again with the same claims it computes the same bytes
    and is a no-op; a snapshot that exists with other bytes is refused, as
    `hold snapshot create` refuses (09 §9.4). A later snapshot — the claims
    re-checked, re-issuing parts under 09 §9.5's superseded names — is not
    built here yet, and is refused rather than half-done."""
    import os
    from . import shard as shard_mod, snapshot as sn
    shard_tree, staging = Path(shard_tree), Path(staging)
    out = shard_tree / shard_mod.snapshot_path(snapshot_id)
    previous, _ = sn._previous(shard_tree, snapshot_id)
    if previous is not None:
        raise AnnotationsError(
            f"this collection already has snapshot {previous.snapshot_id}: a later snapshot "
            f"re-issues its parts (09 §9.5), which `create` does not do — it makes the first")
    (shard_tree / shard_mod.VALIDATION_PATH).write_bytes(cx.canonical_json(codex.validation.to_dict()))
    if staging.exists():
        shutil.rmtree(staging)
    shards, relations = write_parts(staging, records, snapshot_id)
    snap = sn.Snapshot(
        collection_id=codex.collection_id, snapshot_id=snapshot_id, previous=None,
        shards=shards,
        delta=sn.compute_delta(None, {r["uid"]: r["content_hash"] for r in records}),
        relations=relations, figures=(), files=files_of(shard_tree, snapshot_id))
    if out.is_file():
        shutil.rmtree(staging)
        if out.read_bytes() == snap.canonical():
            return snap
        raise AnnotationsError(f"snapshot {snapshot_id} exists with different bytes; a snapshot "
                               f"is written once (09 §9.4)")
    for entry in list(shards) + list(relations):
        dst = shard_tree / entry.path
        dst.parent.mkdir(parents=True, exist_ok=True)
        # the SHARD lives on other storage than `state/` since 2026-10-12
        # (shard -> /shards/<name>, tank): a rename cannot cross, and the first
        # create after the move failed with EXDEV (2026-10-19). `promote` copies
        # beside the target and renames there, whole or not at all
        shard_mod.promote(staging / entry.path, dst)
    shutil.rmtree(staging)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(snap.canonical())
    return snap


def write_held_file(root: Path, codex: Codex, name: str) -> Path:
    """07 §7.8's collection file: the Codex, every slice, records and
    relations, nothing embedded (there is nothing to embed), not shared
    until the operator says so. No `[encoder]`: an annotations collection
    has no space (02 §2.10)."""
    from .deposit import held_file
    path = held_file(root, name)
    if path.exists():
        import tomllib
        with open(path, "rb") as f:
            held = str(tomllib.load(f).get("codex") or "")
        if held.removeprefix("sha256:") == codex.collection_id.removeprefix("sha256:"):
            return path
        raise AnnotationsError(f"{path} holds {held or 'no codex'}; a name holds one collection "
                               f"for its life (07 §7.8)")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".toml.tmp")
    tmp.write_text(f'codex = "{codex.collection_id}"\n'
                   'slices = "all"\n'
                   'kinds = ["records", "relations"]\n'
                   'embed = "off"\n'
                   "share = false\n", encoding="utf-8")
    tmp.replace(path)
    return path


def create(root: Path, name: str, *, target_ref: str, from_snapshot: str, claims: bytes,
           platform_image: str, title: str | None = None, summary: str | None = None,
           annotator: str | None = None, snapshot_id: str | None = None,
           key=None, now: str | None = None, nonce: str | None = None,
           log=print) -> tuple[Codex, Any]:
    """`hold collection create annotations` (07 §7.3): the Codex, the
    claims kept as this collection's origin, the record and relations
    parts, `validation.json`, the first snapshot (signed when this peer has
    a key) and the collection file.

    The claims are checked first and the target read before anything is
    written, so a refused file leaves nothing behind. A name already
    holding an annotations collection of the same target and snapshot is
    RESUMED (its Codex kept, every part rewritten byte for byte); any other
    holder of the name is refused."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from . import collection as col, snapshot as sn

    root = Path(root)
    col.check_name(name)
    tname, target = col.resolve(root, target_ref)
    if is_annotations(target):
        raise AnnotationsError(f"{tname} is itself an annotations collection; annotations are "
                               f"about a collection's records (02 §2.10)")
    submitted = read_claims(claims, target_plugin=target.plugin)
    tshard = col.shard_dir_of(root, tname)
    held = target_records(tshard, target, from_snapshot,
                          {u for s in submitted for u in (s.claim["subject"], s.claim["object"])})
    pinned = pin(submitted, held, from_snapshot)
    contributors = sorted({c["contributor"] for c in pinned})
    annotator = annotator or (contributors[0] if len(contributors) == 1 else None)
    if not annotator:
        raise AnnotationsError(f"the claims name {len(contributors)} contributors; name the "
                               f"annotator the Codex credits with --annotator")

    existing = None
    p = col.shard_dir_of(root, name) / col.CODEX_FILE
    if p.is_file():
        existing = cx.loads(p.read_bytes())
        if not (is_annotations(existing) and existing.annotates
                == cx.Annotates(target.collection_id, from_snapshot)):
            raise AnnotationsError(f"the name {name!r} already holds {existing.collection_id}; a "
                                   f"name holds one collection for its life (07 §7.8)")
        codex = existing
        log(f"[annotations] {name}: resuming {codex.collection_id[:23]}…")
    else:
        tt = (target.descriptive.title or tname).strip()
        codex = build_codex(
            target, from_snapshot,
            title=title or f"Annotations on {tt}",
            summary=summary or (f"Claims about the records of {tt} ({target.collection_id[:19]}…) "
                                f"from its snapshot {from_snapshot} on, made by {annotator}; "
                                f"attached to its search results as facts, never ranked."),
            annotator=annotator, platform_image=platform_image, nonce=nonce)
        col.create(root, codex, name=name)
    shard_tree = col.shard_dir_of(root, name)
    if snapshot_id is None:
        snapshot_id = sn.snapshot_id_for(datetime.now(ZoneInfo(codex.origin.clock)),
                                         codex.update.cadence)
    records = records_of(pinned, submitted)
    snap = write_snapshot(shard_tree, root / name / "state" / "annotations-build", codex, records,
                          snapshot_id)
    # the claims as submitted: this collection's origin (02 §2.10), what each
    # record's `origin` locates and `origin_hash` covers. Beside the SHARD,
    # never in it — an origin is not a member (07 §7.8)
    origin = root / name / "origin"
    origin.mkdir(parents=True, exist_ok=True)
    (origin / CLAIMS_FILE).write_bytes(claims)
    if key is not None:
        sn.sign_snapshot(root, name, snap, key,
                         now=now or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    held_path = write_held_file(root, codex, name)
    log(f"[annotations] {name}: {len(records)} claim(s) about {tname} from {from_snapshot}, "
        f"snapshot {snapshot_id} {snap.hash[:23]}…; held: {held_path}")
    return codex, snap


# --------------------------------------------------------------------------
# reading: the claims a held copy makes, by target record

@dataclass(frozen=True)
class Claim:
    uid: str                     # the claim record's uid
    claim: dict[str, Any]


class ClaimIndex:
    """target uid -> the claims naming it, from a snapshot's parts. Small by
    nature — a claim per linked pair — and rebuilt only when the snapshot
    changes."""

    def __init__(self, snapshot_id: str | None, claims: Sequence[Claim]) -> None:
        self.snapshot_id = snapshot_id
        self.claims = tuple(claims)
        self.by_target: dict[str, list[Claim]] = {}
        for c in self.claims:
            for k in ("subject", "object"):
                self.by_target.setdefault(c.claim[k], []).append(c)


#: SHARD path -> (snapshot hash, its index). Keyed on the PATH (CLAUDE.md).
_indexes: dict[str, tuple[str, ClaimIndex]] = {}


def index_of(shard_tree: Path) -> ClaimIndex:
    """The claims of the held copy's current snapshot; a tombstoned claim
    is not among them (02 §2.2)."""
    from app.pack import records as pack_records
    from app.share import seeder
    snap = seeder.current_snapshot(Path(shard_tree))
    if snap is None:
        return ClaimIndex(None, ())
    hit = _indexes.get(str(shard_tree))
    if hit is not None and hit[0] == snap.hash:
        return hit[1]
    gone = {t.uid for t in snap.delta.tombstoned}
    claims = []
    for s in snap.shards:
        p = Path(shard_tree) / s.path
        if not p.is_file():
            continue                     # a part this peer does not hold: its claims are not here
        for r in pack_records.iter_records(p):
            if r["uid"] not in gone:
                claims.append(Claim(r["uid"], claim_of(r)))
    idx = ClaimIndex(snap.snapshot_id, claims)
    _indexes[str(shard_tree)] = (snap.hash, idx)
    return idx


def applies_from(from_snapshot: str, read_snapshot: str | None) -> bool:
    """Is an answer read from `read_snapshot` of the target one these
    annotations fit? From `from_snapshot` onwards (both ids are the
    target's, one cadence, and sort as they run — 09 §9.4)."""
    return read_snapshot is not None and read_snapshot >= from_snapshot


def state_of(claim: dict[str, Any], now: dict[str, str | None]) -> str:
    """`current` when both records' content hashes are still the ones the
    claim was checked against, `stale` when one is not, `unknown` when this
    peer's manifest carries no hash to compare."""
    a, b = now.get(claim["subject"]), now.get(claim["object"])
    if a is None or b is None:
        return UNKNOWN
    return CURRENT if (a, b) == (claim["subject_hash"], claim["object_hash"]) else STALE
