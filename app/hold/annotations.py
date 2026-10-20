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

THE GRAPH AMENDMENT (operator, 2026-10-20; 02 §2.10, 09 §9.3, §9.5, §9.14;
docs/notes/gutenberg-graph-amendments.md A-K1–A-K4). Three more claim
types, whose ends are CHUNKS and ENTITIES rather than two records:

    mention    subject = a chunk of the text target {uid, chunk_id, text_hash},
               object  = an entity's canonical id (`wikidata:Q42`),
               own keys start, end (code points in the chunk, end exclusive),
               surface (the text between them)
    relation   subject, object = entities; relation = a property's canonical
               id (`wikidata:P50`); evidence_chunk = the chunk it was read from
    community  subject = {id, level}; object null; members = sorted entity ids,
               at least two; summary

An annotations Codex may annotate SEVERAL collections (Codex version 9,
`annotates` a list): gutenberg-graph annotates gutenberg, whose chunks its
mentions are read from, and wikidata, whose records its entities are. A
claim's end belongs to the target whose plugin its uid prefix names; an
entity's canonical id becomes a record uid through the plugin's id rule
(`entity_uid`). `create` pins the RECORD at each end (`subject_hash`,
`object_hash` — both null for a community, which has no record at either
end) from the target snapshot, and CHECKS a chunk end by re-cutting its
record with the target's chunker (invariant 3: the same text cuts the same
chunks) — the chunk must exist, hash to `text_hash`, and hold the mention's
`surface` at `start:end`. A `model` method (a model whose output is not
bit-exact) names its weights and its prompt by digest and runs at
temperature 0; its claims are verified by the `model-agreement` check (03
§3.2), not by hash. The Codex stays at version 7 for a one-target
collection of the two 2026-10-09 types in JSONL parts, so nothing already
written moves.
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
MENTION, RELATION, COMMUNITY = "mention", "relation", "community"
#: the types of 2026-10-09: two records of ONE target
WORK_TYPES = (SAME_WORK, TRANSLATION_OF)
#: the types of 2026-10-20 (Codex GRAPH_VERSION): chunk and entity ends
GRAPH_TYPES = (MENTION, RELATION, COMMUNITY)
CLAIM_TYPES = WORK_TYPES + GRAPH_TYPES
#: a community's edges: each member → the community claim (09 §9.14)
MEMBER_OF = "member-of"
METHOD_KINDS = ("matcher", "wikidata", "human", "model")
#: a method whose output is not bit-exact: what a verifier re-runs (02 §2.10)
MODEL_METHOD_KEYS = ("kind", "id", "version", "model", "prompt", "temperature")
#: what an annotator writes in the claims file: the keys every claim has …
COMMON_KEYS = ("type", "subject", "object", "method", "evidence", "confidence", "contributor")
#: … and each type's own (09 §9.5)
OWN_KEYS: dict[str, tuple[str, ...]] = {
    SAME_WORK: ("subject_lang", "object_lang"),
    TRANSLATION_OF: ("subject_lang", "object_lang"),
    MENTION: ("start", "end", "surface"),
    RELATION: ("relation", "evidence_chunk"),
    COMMUNITY: ("members", "summary"),
}
#: the 2026-10-09 claim's keys, as callers knew them
CLAIM_KEYS = COMMON_KEYS[:3] + OWN_KEYS[SAME_WORK] + COMMON_KEYS[3:]
#: what `create` pins from the target snapshot (an annotator MAY state them;
#: then they must agree)
PINNED_KEYS = ("subject_hash", "object_hash", "target_snapshot")
#: a chunk end (A-K2): the record, the chunk's id and the BLAKE2b-128 of
#: its text as the target's chunker cut it (06 §6.3, `embeddings.chunk_text_hash`)
CHUNK_END_KEYS = ("uid", "chunk_id", "text_hash")
#: the kinds of end a claim has (`ends_of`)
RECORD, CHUNK, ENTITY = "record", "chunk", "entity"
SLICE = "all"
SHARD_RECORDS = 50000
#: 09 §9.3: records per part for a claims collection in Parquet
SHARD_RECORDS_PARQUET = 1_000_000
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


def _plugins(target_plugin: str | None, target_plugins: Sequence[str] | None) -> tuple[str, ...]:
    if target_plugins:
        return tuple(target_plugins)
    return (target_plugin,) if target_plugin else ()


def _prefix(uid: str) -> str:
    return uid.split(":", 1)[0]


def entity_uid(canonical: str) -> str:
    """The record uid of an entity end: `wikidata:Q42` → `wikidata:42`
    through the plugin's id rule (app/plugins/wikidata/ids.py); any other
    namespace's canonical id is its record's uid as written. The namespace
    of an entity end is the plugin of the target that holds it: the
    reference source registering the namespace is that plugin's (06 §6.5)."""
    ns, _, ident = canonical.partition(":")
    if ns == "wikidata":
        from app.plugins.wikidata import ids
        return f"{ns}:{ids.native_id_of(ident)}"
    return canonical


def _entity_ok(ns: str, ident: str, *, property_: bool = False) -> bool:
    if ns == "wikidata":
        from app.plugins.wikidata import ids
        return ids.is_entity_id(ident) and (ids.kind_of(ident) == "property") == property_
    return bool(ident)


def _check_uid(v: Any, where: str, plugins: tuple[str, ...]) -> str:
    if not isinstance(v, str) or ":" not in v or not v.split(":", 1)[1]:
        raise AnnotationsError(f"{where} is a target record's uid <plugin>:<native_id>, got {v!r}")
    if plugins and _prefix(v) not in plugins:
        raise AnnotationsError(f"{where} {v!r} is not a uid of the target, whose plugin "
                               f"is {' or '.join(repr(p) for p in plugins)}")
    return v


def _check_entity(v: Any, where: str, plugins: tuple[str, ...], *, property_: bool = False) -> str:
    what = "a property's" if property_ else "an entity's"
    if not isinstance(v, str) or ":" not in v:
        raise AnnotationsError(f"{where} is {what} canonical id <namespace>:<id> (09 §9.14), got {v!r}")
    ns, _, ident = v.partition(":")
    if plugins and ns not in plugins:
        raise AnnotationsError(f"{where} {v!r} is not in a target's namespace, which is "
                               f"{' or '.join(repr(p) for p in plugins)}")
    if not _entity_ok(ns, ident, property_=property_):
        raise AnnotationsError(f"{where} {v!r} is not {what} id in the {ns!r} namespace")
    return v


def _check_chunk_end(v: Any, where: str, plugins: tuple[str, ...]) -> dict[str, Any]:
    if not isinstance(v, dict) or tuple(sorted(v)) != tuple(sorted(CHUNK_END_KEYS)):
        raise AnnotationsError(f"{where} is a chunk of the text target, {{{', '.join(CHUNK_END_KEYS)}}} "
                               f"(09 §9.5, amended 2026-10-20), got {v!r}")
    _check_uid(v["uid"], f"{where}.uid", plugins)
    if not isinstance(v["chunk_id"], str) or not v["chunk_id"]:
        raise AnnotationsError(f"{where}.chunk_id is the chunk's id as the target's chunker names it")
    if not isinstance(v["text_hash"], str) or not _HEX32_RE.match(v["text_hash"]):
        raise AnnotationsError(f"{where}.text_hash is the BLAKE2b-128 of the chunk's text, 32 hex digits")
    return v


def _check_int(v: Any, where: str, *, minimum: int = 0) -> int:
    if not isinstance(v, int) or isinstance(v, bool) or v < minimum:
        raise AnnotationsError(f"{where} is an integer of at least {minimum}, got {v!r}")
    return v


def check_claim(c: Any, where: str = "claim", *, target_plugin: str | None = None,
                target_plugins: Sequence[str] | None = None) -> dict[str, Any]:
    """One claim, refused with its reason or returned in its canonical form
    (strings NFC; no float anywhere, since a claim is hashed — 09 §9.1).
    `target_plugins` are the plugins of the collections annotated, in which
    every end must be; `target_plugin` is the one-target spelling."""
    plugins = _plugins(target_plugin, target_plugins)
    if not isinstance(c, dict):
        raise AnnotationsError(f"{where}: a claim is a JSON object")
    t = c.get("type")
    if t not in CLAIM_TYPES:
        raise AnnotationsError(f"{where}: type is one of {', '.join(CLAIM_TYPES)}, got {t!r}")
    keys = COMMON_KEYS + OWN_KEYS[t]
    missing = [k for k in keys if k not in c]
    unknown = sorted(k for k in c if k not in keys + PINNED_KEYS)
    if missing or unknown:
        raise AnnotationsError(
            f"{where}: " + "; ".join(x for x in (
                f"missing {', '.join(missing)}" if missing else "",
                f"unknown {', '.join(unknown)}" if unknown else "") if x)
            + f" (a {t} claim is {', '.join(keys)}; 09 §9.5, amended "
              f"{'2026-10-09' if t in WORK_TYPES else '2026-10-20'})")
    try:
        cx._reject_floats(c, where)
    except CodexError as e:
        raise AnnotationsError(f"{where}: {e} — write a ratio as a decimal string") from None
    c = _nfc(c)
    if t in WORK_TYPES:
        for k in ("subject", "object"):
            _check_uid(c[k], f"{where}: {k}", plugins)
        if c["subject"] == c["object"]:
            raise AnnotationsError(f"{where}: subject and object are the same record")
        if _prefix(c["subject"]) != _prefix(c["object"]):
            raise AnnotationsError(f"{where}: a {t} claim joins two records of ONE target; "
                                   f"{c['subject']} and {c['object']} are in different ones")
        for k in ("subject_lang", "object_lang"):
            if not isinstance(c[k], str) or not _LANG_RE.match(c[k]):
                raise AnnotationsError(f"{where}: {k} is an ISO 639 code or \"und\", got {c[k]!r}")
    elif t == MENTION:
        _check_chunk_end(c["subject"], f"{where}: subject", plugins)
        _check_entity(c["object"], f"{where}: object", plugins)
        start, end = _check_int(c["start"], f"{where}: start"), _check_int(c["end"], f"{where}: end")
        if end <= start:
            raise AnnotationsError(f"{where}: end is after start (code points in the chunk's text, end "
                                   f"exclusive), got {start}..{end}")
        if not isinstance(c["surface"], str) or not c["surface"]:
            raise AnnotationsError(f"{where}: surface is the text between start and end")
    elif t == RELATION:
        _check_entity(c["subject"], f"{where}: subject", plugins)
        _check_entity(c["object"], f"{where}: object", plugins)
        if c["subject"] == c["object"]:
            raise AnnotationsError(f"{where}: subject and object are the same entity")
        _check_entity(c["relation"], f"{where}: relation", plugins, property_=True)
        _check_chunk_end(c["evidence_chunk"], f"{where}: evidence_chunk", plugins)
    else:                                                   # COMMUNITY
        sub = c["subject"]
        if not isinstance(sub, dict) or set(sub) != {"id", "level"} or not isinstance(sub["id"], str) \
                or not sub["id"]:
            raise AnnotationsError(f"{where}: a community's subject is {{id, level}}, the id the method "
                                   f"mints, got {sub!r}")
        _check_int(sub["level"], f"{where}: subject.level")
        if c["object"] is not None:
            raise AnnotationsError(f"{where}: a community has no object; it is null")
        members = c["members"]
        if not isinstance(members, list) or len(members) < 2:
            raise AnnotationsError(f"{where}: members is a list of at least two entities")
        for i, m in enumerate(members):
            _check_entity(m, f"{where}: members[{i}]", plugins)
        if members != sorted(members, key=lambda m: m.encode("utf-8")) or len(set(members)) != len(members):
            raise AnnotationsError(f"{where}: members are sorted by bytes, no entity twice")
        if not isinstance(c["summary"], str):
            raise AnnotationsError(f"{where}: summary is the method's text about the members")
    m = c["method"]
    if not isinstance(m, dict) or m.get("kind") not in METHOD_KINDS:
        raise AnnotationsError(f"{where}: method is {{kind: {' | '.join(METHOD_KINDS)}, …}}")
    if m["kind"] == "matcher":
        if set(m) != {"kind", "id", "version"} or not isinstance(m["id"], str) or not m["id"] \
                or not isinstance(m["version"], int) or isinstance(m["version"], bool):
            raise AnnotationsError(f"{where}: a matcher method is {{kind, id, version}}, the "
                                   f"version an integer — what a verifier re-runs")
    elif m["kind"] == "model":
        if (set(m) != set(MODEL_METHOD_KEYS) or not isinstance(m["id"], str) or not m["id"]
                or not isinstance(m["version"], int) or isinstance(m["version"], bool)
                or not all(isinstance(m[k], str) and cx._DIGEST_RE.match(m[k]) for k in ("model", "prompt"))
                or m["temperature"] != "0"):
            raise AnnotationsError(
                f"{where}: a model method is {{{', '.join(MODEL_METHOD_KEYS)}}}: the version an integer, "
                f"model and prompt sha256: digests of the weights and the prompt text, temperature \"0\" "
                f"— what the model-agreement check re-runs (02 §2.10, 03 §3.2)")
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
        if k in c and c[k] is None and t == COMMUNITY:
            continue                        # a community pins no record (09 §9.5)
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


def identity_key(c: dict[str, Any]) -> tuple:
    """What makes two claims the same claim (09 §9.5): the pair and the type
    for the 2026-10-09 types — either way round for `same-work` — and the
    type with its ends and own identifying keys for the rest."""
    t = c["type"]
    if t == SAME_WORK:                    # symmetric: one claim per pair
        return (t,) + tuple(sorted((c["subject"], c["object"])))
    if t == TRANSLATION_OF:
        return (t, c["subject"], c["object"])
    if t == MENTION:
        return (t, cx.canonical_json(c["subject"]), c["object"], c["start"], c["end"])
    if t == RELATION:
        return (t, c["subject"], c["object"], c["relation"])
    return (t, c["subject"]["id"], c["subject"]["level"])


_pair_key = identity_key                    # the 2026-10-09 name


def identity_tuple(c: dict[str, Any]) -> list:
    """What a claim is NUMBERED by at Codex GRAPH_VERSION (09 §9.5): the
    canonical JSON of this list, in byte order — (subject, object, type) for
    the two 2026-10-09 types, as before them."""
    t = c["type"]
    if t in WORK_TYPES:
        return [c["subject"], c["object"], t]
    if t == MENTION:
        return [t, c["subject"], c["object"], c["start"], c["end"]]
    if t == RELATION:
        return [t, c["subject"], c["object"], c["relation"]]
    return [t, c["subject"]["id"], c["subject"]["level"]]


def ends_of(c: dict[str, Any]) -> list[tuple[str, Any]]:
    """Every end of a claim with its kind — `(RECORD, uid)`, `(CHUNK, {uid,
    chunk_id, text_hash})`, `(ENTITY, canonical id)` — the first two being
    what `subject_hash` and `object_hash` pin (a relation's evidence chunk
    is third: checked, not pinned)."""
    t = c["type"]
    if t in WORK_TYPES:
        return [(RECORD, c["subject"]), (RECORD, c["object"])]
    if t == MENTION:
        return [(CHUNK, c["subject"]), (ENTITY, c["object"])]
    if t == RELATION:
        return [(ENTITY, c["subject"]), (ENTITY, c["object"]), (CHUNK, c["evidence_chunk"])]
    return [(ENTITY, m) for m in c["members"]]


def end_uid(kind: str, value: Any) -> str:
    """The RECORD uid an end names, in the target that holds it."""
    if kind == RECORD:
        return value
    if kind == CHUNK:
        return value["uid"]
    return entity_uid(value)


def record_ends(c: dict[str, Any]) -> list[str]:
    """The record uids a claim names, each once, in end order."""
    out: list[str] = []
    for kind, value in ends_of(c):
        uid = end_uid(kind, value)
        if uid not in out:
            out.append(uid)
    return out


def read_claims(data: bytes, *, target_plugin: str | None = None,
                target_plugins: Sequence[str] | None = None) -> list[Submitted]:
    """The claims file: JSON Lines, one claim per line, blank lines skipped.
    A claim made twice — the same pair, either way round for `same-work`;
    the same identifying keys for the other types — is refused, naming both
    lines."""
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
        c = check_claim(obj, f"claims line {i}", target_plugin=target_plugin, target_plugins=target_plugins)
        k = identity_key(c)
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
                   uids: Iterable[str], *, text_for: Iterable[str] = ()) -> dict[str, dict[str, Any]]:
    """uid -> {content_hash, lang} for every uid the target snapshot holds —
    with `uid`, `native_id`, `title` and `sections` for the uids in
    `text_for`, the records a chunk end names, which `pin` re-cuts. Each
    part is read once and only the frames that hold a wanted uid are
    decompressed (09 §9.5's index), so the cost is the claims', never the
    target's size."""
    text_for = set(text_for)
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
                if uid in text_for:
                    out[uid].update(uid=r["uid"], native_id=r["native_id"], title=r.get("title"),
                                    sections=r.get("sections"))
    return out


def chunks_of(codex: Codex, record: dict[str, Any]) -> dict[str, str]:
    """chunk id -> text, as the target's chunker cuts the record — the one
    way a stage cuts a document (`layers.chunks_for_document`, invariant 1),
    a pure function of the text (invariant 3), so a chunk end is checked
    against the text this peer holds and never against a vectors table."""
    from app import layers
    row = {"uid": record["uid"], "native_id": record["native_id"]}
    sections = [tuple(x) for x in record.get("sections") or []]
    return {c["id"]: c["text"] for c in layers.chunks_for_document(codex, row, sections)}


def _check_chunk(end: dict[str, Any], record: dict[str, Any], codices: dict[str, Codex] | None,
                 line: int) -> str:
    """The chunk end against the record this peer holds: re-cut with the
    target's chunker, the chunk exists and its text hashes to `text_hash`.
    The chunk's text, for the mention's `surface`."""
    uid = end["uid"]
    codex = (codices or {}).get(_prefix(uid))
    if codex is None or record.get("sections") is None:
        raise AnnotationsError(
            f"claims line {line}: the chunk {end['chunk_id']} cannot be checked — this peer holds no "
            f"text of {uid} to re-cut (a chunk end is checked against the target's text, 02 §2.10)")
    text = chunks_of(codex, record).get(end["chunk_id"])
    if text is None:
        raise AnnotationsError(
            f"claims line {line}: {uid} has no chunk {end['chunk_id']} under the target's chunker "
            f"(the same text cuts the same chunks, invariant 3): the claim was made about other text "
            f"or another cut")
    if blake128(text.encode("utf-8")) != end["text_hash"]:
        raise AnnotationsError(
            f"claims line {line}: text_hash {end['text_hash']} is not chunk {end['chunk_id']}'s "
            f"({blake128(text.encode('utf-8'))}): the claim was made about other text")
    return text


def _snapshot_for(uid: str, snapshot_id: str | None, snapshots: dict[str, str] | None) -> str:
    if snapshots:
        try:
            return snapshots[_prefix(uid)]
        except KeyError:
            raise AnnotationsError(f"{uid} is in no target's plugin ({', '.join(sorted(snapshots))})") from None
    if snapshot_id is None:
        raise AnnotationsError("pin: a target snapshot is needed")
    return snapshot_id


def pin(submitted: Sequence[Submitted], held: dict[str, dict[str, Any]],
        snapshot_id: str | None = None, *, snapshots: dict[str, str] | None = None,
        codices: dict[str, Codex] | None = None) -> list[dict[str, Any]]:
    """Each claim with the target records' hashes and the snapshot it was
    checked against — the snapshot of the target its SUBJECT belongs to
    (09 §9.5): `snapshot_id` with one target, `snapshots` (plugin -> target
    snapshot) with several; `codices` (plugin -> Codex) re-cuts a chunk end's
    record. A record the snapshot does not hold, a stated hash that is not
    the record's there, a chunk that is not the record's — each refuses the
    file: a claim about text we do not hold, or about other text, is not one
    we publish. A community pins no record: both hashes null."""
    out, missing = [], []
    for s in submitted:
        c = dict(s.claim)
        t = c["type"]
        ends = ends_of(c)
        snap = _snapshot_for(end_uid(*ends[0]), snapshot_id, snapshots)
        hashes: list[str] = []
        lost = False
        for kind, value in ends:
            uid = end_uid(kind, value)
            got = held.get(uid)
            if got is None:
                missing.append(f"line {s.line}: {uid}" + (f" ({value})" if kind == ENTITY and value != uid else ""))
                lost = True
                continue
            if kind == CHUNK:
                text = _check_chunk(value, got, codices, s.line)
                if t == MENTION:
                    if c["end"] > len(text) or text[c["start"]:c["end"]] != c["surface"]:
                        raise AnnotationsError(
                            f"claims line {s.line}: surface {c['surface']!r} is not the chunk's text at "
                            f"{c['start']}..{c['end']} ({text[c['start']:c['end']]!r})")
            hashes.append(got["content_hash"])
        if lost:
            continue
        if t == COMMUNITY:
            for k in ("subject_hash", "object_hash"):
                if c.get(k) is not None:
                    raise AnnotationsError(f"claims line {s.line}: a community pins no {k} — it has no "
                                           f"record at either end (09 §9.5)")
                c[k] = None
        else:
            for k, h in zip(("subject", "object"), hashes):
                stated = c.get(f"{k}_hash")
                if stated is not None and stated != h:
                    raise AnnotationsError(
                        f"claims line {s.line}: {k}_hash {stated} is not {end_uid(*ends[0 if k == 'subject' else 1])}'s "
                        f"content_hash in snapshot {snap} ({h}): the claim was checked against other text")
                c[f"{k}_hash"] = h
        if c.get("target_snapshot", snap) != snap:
            raise AnnotationsError(f"claims line {s.line}: checked against {c['target_snapshot']}, "
                                   f"and these are pinned to {snap}")
        c["target_snapshot"] = snap
        out.append(c)
    if missing:
        raise AnnotationsError(
            f"{len(missing)} claim end(s) name a record the target snapshot does "
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


def records_of(claims: Sequence[dict[str, Any]], submitted: Sequence[Submitted], *,
               version: int = cx.ANNOTATIONS_VERSION) -> list[dict[str, Any]]:
    """The claim records, numbered from 1 in (subject, object, type) byte
    order — a rule, so two annotators building the same file cut the same
    records — each with its §9.5 hashes. At GRAPH_VERSION the order is the
    byte order of each claim's `identity_tuple` as canonical JSON (09 §9.5,
    amended 2026-10-20), which is the same order for the 2026-10-09 types
    whose uids carry no byte below `"`; the version-7 rule is kept as it was
    so no part already written moves."""
    from . import records as rec
    if version >= cx.GRAPH_VERSION:
        pairs = sorted(zip(claims, submitted), key=lambda cs: cx.canonical_json(identity_tuple(cs[0])))
    else:
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
    typed `annotations:<type>`, sorted by (from, type, to) bytes (09 §9.14).
    Amended 2026-10-20: a mention runs from the chunk's RECORD to the
    entity's canonical id; a relation from the subject entity's record uid
    to the object's canonical id (the property stays in the claim); a
    community has no edge of its own — each member's record points at the
    community claim, `annotations:member-of`."""
    from . import records as rec
    edges = []
    for r in records:
        c = claim_of(r)
        t = c["type"]
        if t in WORK_TYPES:
            edges.append((c["subject"], edge_type(t), c["object"]))
        elif t == MENTION:
            edges.append((c["subject"]["uid"], edge_type(t), c["object"]))
        elif t == RELATION:
            edges.append((entity_uid(c["subject"]), edge_type(t), c["object"]))
        else:
            edges.extend((entity_uid(m), edge_type(MEMBER_OF), r["uid"]) for m in c["members"])
    edges.sort(key=lambda e: (e[0].encode(), e[1].encode(), e[2].encode()))
    return [rec.edge_line(f, t, ty, snapshot_id) for f, ty, t in edges]


# --------------------------------------------------------------------------
# the Codex

def build_codex(target: Codex | None, from_snapshot: str | None, *, title: str, summary: str,
                annotator: str, platform_image: str, nonce: str | None = None,
                attribution: str | None = None, erasure: str = "retractable",
                targets: Sequence[tuple[Codex, str]] | None = None, container: str = cx.JSONL,
                graph: bool = False) -> Codex:
    """02 §2.10's Codex: every field present, the space and work null,
    no layers, the target(s) and the snapshot the claims hold from. Version
    7 for one target, JSONL parts and the two 2026-10-09 claim types; the
    GRAPH_VERSION list where there are several targets, a Parquet container
    or a graph claim type (`graph`) — 09 §9.3's version rule, so the
    collections already written keep their bytes."""
    pairs = tuple(targets) if targets else ((target, from_snapshot),)
    cx.refuse_unbuilt_container(container)
    version = (cx.GRAPH_VERSION if len(pairs) > 1 or container != cx.JSONL or graph
               else cx.ANNOTATIONS_VERSION)
    slicing: dict[str, Any] = {"keys": [], "unknown": SLICE, "shard_records": SHARD_RECORDS,
                               "order": "numeric"}
    if version >= cx.GRAPH_VERSION:
        slicing["container"] = container
        if container == cx.PARQUET:
            slicing["shard_records"] = SHARD_RECORDS_PARQUET
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
        slicing=slicing,
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
        version=version,
        record_line=1,
        annotates=tuple(cx.Annotates(t.collection_id, snap) for t, snap in pairs),
    )
    cx.validate(codex)
    return codex


# --------------------------------------------------------------------------
# the SHARD

def write_parts(shard_tree: Path, records: Sequence[dict[str, Any]], snapshot_id: str, *,
                shard_records: int = SHARD_RECORDS):
    """Record parts and their relations parts, `shard_records` (the Codex's
    `slicing.shard_records`) per part, and the snapshot entries for both."""
    from app.pack import records as pack_records
    from . import shard as shard_mod, records as rec, snapshot as sn
    shards, relations = [], []
    for n, start in enumerate(range(0, len(records), shard_records), 1):
        part = records[start:start + shard_records]
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
    shards, relations = write_parts(staging, records, snapshot_id,
                                    shard_records=int(codex.slicing.get("shard_records") or SHARD_RECORDS))
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


@dataclass(frozen=True)
class Target:
    """One collection an annotations collection is about, as this peer
    holds it: its local name, its Codex, the snapshot the claims hold from
    and its SHARD."""
    name: str
    codex: Codex
    from_snapshot: str
    shard_tree: Path

    @property
    def plugin(self) -> str:
        return self.codex.plugin


@dataclass(frozen=True)
class Checked:
    """What `create` checked before writing anything."""
    targets: tuple[Target, ...]
    submitted: tuple[Submitted, ...]
    pinned: tuple[dict[str, Any], ...]
    annotator: str

    @property
    def graph(self) -> bool:
        """Does any claim take a 2026-10-20 type (GRAPH_VERSION)?"""
        return any(c["type"] in GRAPH_TYPES for c in self.pinned)


def _targets(root: Path, refs: Sequence[tuple[str, str]]) -> tuple[Target, ...]:
    """The targets named, resolved and refused where 02 §2.10 refuses: an
    annotations collection, one plugin twice (a claim's end names its target
    by the uid's plugin prefix, 09 §9.3), one collection twice."""
    from . import collection as col
    out: list[Target] = []
    for ref, from_snapshot in refs:
        if not from_snapshot:
            raise AnnotationsError(f"{ref}: every target names the snapshot the claims hold from "
                                   f"(--from-snapshot once per --annotates)")
        tname, target = col.resolve(Path(root), ref)
        if is_annotations(target):
            raise AnnotationsError(f"{tname} is itself an annotations collection; annotations are "
                                   f"about a collection's records (02 §2.10)")
        for t in out:
            if t.codex.collection_id == target.collection_id:
                raise AnnotationsError(f"{tname} is named twice among the targets")
            if t.plugin == target.plugin:
                raise AnnotationsError(f"{t.name} and {tname} are both {target.plugin!r} collections: a "
                                       f"claim's end names its target by its uid's plugin prefix, so "
                                       f"one target per plugin (09 §9.3, amended 2026-10-20)")
        out.append(Target(tname, target, from_snapshot, col.shard_dir_of(Path(root), tname)))
    return tuple(out)


def _refs(target_ref: str | None, from_snapshot: str | None,
          targets: Sequence[tuple[str, str]] | None) -> tuple[tuple[str, str], ...]:
    if targets:
        return tuple((str(r), str(s)) for r, s in targets)
    if not target_ref:
        raise AnnotationsError("create annotations names the collection(s) it annotates")
    return ((target_ref, from_snapshot or ""),)


def _checked(root: Path, target_ref: str | None = None, from_snapshot: str | None = None,
             claims: bytes = b"", *, annotator: str | None,
             targets: Sequence[tuple[str, str]] | None = None) -> Checked:
    """What `create` checks before it writes anything: the targets, the
    claims file, every claim's ends in the target snapshots — each chunk
    end re-cut — and the one annotator."""
    ts = _targets(Path(root), _refs(target_ref, from_snapshot, targets))
    by_plugin = {t.plugin: t for t in ts}
    submitted = read_claims(claims, target_plugins=tuple(by_plugin))
    wanted: dict[str, set[str]] = {}
    text_for: dict[str, set[str]] = {}
    for s_ in submitted:
        for kind, value in ends_of(s_.claim):
            uid = end_uid(kind, value)
            wanted.setdefault(_prefix(uid), set()).add(uid)
            if kind == CHUNK:
                text_for.setdefault(_prefix(uid), set()).add(uid)
    held: dict[str, dict[str, Any]] = {}
    for plugin, uids in wanted.items():
        t = by_plugin[plugin]              # read_claims saw to it that every prefix is a target's
        held.update(target_records(t.shard_tree, t.codex, t.from_snapshot, uids,
                                   text_for=text_for.get(plugin, ())))
    pinned = pin(submitted, held, snapshots={p: t.from_snapshot for p, t in by_plugin.items()},
                 codices={p: t.codex for p, t in by_plugin.items()})
    contributors = sorted({c["contributor"] for c in pinned})
    annotator = annotator or (contributors[0] if len(contributors) == 1 else None)
    if not annotator:
        raise AnnotationsError(f"the claims name {len(contributors)} contributors; name the "
                               f"annotator the Codex credits with --annotator")
    return Checked(ts, tuple(submitted), tuple(pinned), annotator)


def _method_name(m: dict[str, Any]) -> str:
    return m["kind"] + (f":{m['id']}" if m["kind"] in ("matcher", "model") else "")


def preview(root: Path, *, target_ref: str | None = None, from_snapshot: str | None = None, claims: bytes,
            annotator: str | None = None, targets: Sequence[tuple[str, str]] | None = None,
            container: str = cx.JSONL) -> dict[str, Any]:
    """`create annotations --dry-run`: the claims checked against the target
    snapshot(s) exactly as `create` checks them, counted, nothing written —
    for a file of thousands of claims about a live collection, the measure
    before the write (operator, 2026-10-19: the gutenberg graph collection).
    `target`, `target_id` and `from_snapshot` are the first target's;
    `targets` lists them all."""
    from collections import Counter
    cx.refuse_unbuilt_container(container)
    ch = _checked(root, target_ref, from_snapshot, claims, annotator=annotator, targets=targets)
    first = ch.targets[0]
    records = {u for c in ch.pinned for u in record_ends(c)}
    version = (cx.GRAPH_VERSION if len(ch.targets) > 1 or container != cx.JSONL or ch.graph
               else cx.ANNOTATIONS_VERSION)
    per_part = SHARD_RECORDS_PARQUET if container == cx.PARQUET else SHARD_RECORDS
    return {"target": first.name, "target_id": first.codex.collection_id, "from_snapshot": first.from_snapshot,
            "targets": [{"target": t.name, "collection_id": t.codex.collection_id,
                         "from_snapshot": t.from_snapshot} for t in ch.targets],
            "version": version, "container": container,
            "claims": len(ch.pinned), "target_records_named": len(records), "annotator": ch.annotator,
            "by_type": dict(sorted(Counter(c["type"] for c in ch.pinned).items())),
            "by_method": dict(sorted(Counter(_method_name(c["method"]) for c in ch.pinned).items())),
            "parts": -(-len(ch.pinned) // per_part),
            "written": False}


def create(root: Path, name: str, *, target_ref: str | None = None, from_snapshot: str | None = None,
           claims: bytes, platform_image: str, title: str | None = None, summary: str | None = None,
           annotator: str | None = None, snapshot_id: str | None = None,
           key=None, now: str | None = None, nonce: str | None = None,
           log=print, targets: Sequence[tuple[str, str]] | None = None,
           container: str = cx.JSONL) -> tuple[Codex, Any]:
    """`hold collection create annotations` (07 §7.3): the Codex, the
    claims kept as this collection's origin, the record and relations
    parts, `validation.json`, the first snapshot (signed when this peer has
    a key) and the collection file. `targets` — (collection, from_snapshot)
    pairs, `--annotates` and `--from-snapshot` repeated — or the one
    `target_ref` / `from_snapshot`.

    The claims are checked first and the targets read before anything is
    written, so a refused file leaves nothing behind. A name already
    holding an annotations collection of the same targets, container and
    version is RESUMED (its Codex kept, every part rewritten byte for
    byte); any other holder of the name is refused."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from . import collection as col, snapshot as sn

    root = Path(root)
    col.check_name(name)
    cx.refuse_unbuilt_container(container)
    ch = _checked(root, target_ref, from_snapshot, claims, annotator=annotator, targets=targets)
    first = ch.targets[0]
    tt = (first.codex.descriptive.title or first.name).strip()
    names = ", ".join(t.name for t in ch.targets)
    wanted = build_codex(
        None, None, targets=[(t.codex, t.from_snapshot) for t in ch.targets], container=container,
        graph=ch.graph,
        title=title or f"Annotations on {tt}",
        summary=summary or (f"Claims about the records of {names} "
                            f"({first.codex.collection_id[:19]}…) from snapshot "
                            f"{first.from_snapshot} on, made by {ch.annotator}; "
                            f"attached to its search results as facts, never ranked."),
        annotator=ch.annotator, platform_image=platform_image, nonce=nonce)

    p = col.shard_dir_of(root, name) / col.CODEX_FILE
    if p.is_file():
        existing = cx.loads(p.read_bytes())
        if not (is_annotations(existing) and cx.targets_of(existing) == wanted.annotates
                and cx.container_of(existing) == container and existing.version == wanted.version):
            raise AnnotationsError(f"the name {name!r} already holds {existing.collection_id}; a "
                                   f"name holds one collection for its life (07 §7.8)")
        codex = existing
        log(f"[annotations] {name}: resuming {codex.collection_id[:23]}…")
    else:
        codex = wanted
        col.create(root, codex, name=name)
    shard_tree = col.shard_dir_of(root, name)
    if snapshot_id is None:
        snapshot_id = sn.snapshot_id_for(datetime.now(ZoneInfo(codex.origin.clock)),
                                         codex.update.cadence)
    records = records_of(ch.pinned, ch.submitted, version=codex.version)
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
    log(f"[annotations] {name}: {len(records)} claim(s) about {names} from "
        f"{', '.join(t.from_snapshot for t in ch.targets)}, "
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
        #: target record uid -> the WORK claims (2026-10-09) naming it, what
        #: the Ask side attaches today (app/ask/annotations.py, app/ask/works.py)
        self.by_target: dict[str, list[Claim]] = {}
        #: target record uid -> the GRAPH claims (2026-10-20) naming it — a
        #: mention by its chunk's record and its entity, a relation by its
        #: entities, a community by its members; attached by step 3 of
        #: docs/notes/gutenberg-graph-amendments.md
        self.graph: dict[str, list[Claim]] = {}
        for c in self.claims:
            into = self.by_target if c.claim["type"] in WORK_TYPES else self.graph
            for uid in record_ends(c.claim):
                into.setdefault(uid, []).append(c)


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
    """`current` when both pinned records' content hashes are still the ones
    the claim was checked against, `stale` when one is not, `unknown` when
    this peer's manifest carries no hash to compare — or when the claim pins
    no record (a community, 09 §9.5). `now` is keyed by record uid (a
    mention's chunk record, an entity's record uid)."""
    if claim["type"] == COMMUNITY:
        return UNKNOWN
    ends = ends_of(claim)
    a, b = now.get(end_uid(*ends[0])), now.get(end_uid(*ends[1]))
    if a is None or b is None:
        return UNKNOWN
    return CURRENT if (a, b) == (claim["subject_hash"], claim["object_hash"]) else STALE
