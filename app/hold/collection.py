"""hold.collection — create a Codex and read it back. docs/07-commands.md §7.3.

    hold collection create   → a root Codex
    hold collection derive   → a sister (the mother's corpus, a new space)
    hold collection continue → a daughter (a new toolchain, the same space)
    hold collection replace  → a corrective sister (with its defect)
    hold collection show     → what a Codex says, and the ids derived from it

On disk a collection is a directory named by the 64 hexadecimal digits of
its `collection_id` (09 §9.3); `codex.json` inside is the canonical bytes
and nothing else. A file whose inner hash does not equal the id its
directory claims is refused.
"""
from __future__ import annotations

import json
import re
from dataclasses import replace as _replace
from pathlib import Path
from typing import Any, Iterable

from . import codex as cx
from .codex import (Codex, CodexError, Credit, Defect, Descriptive, Layer,
                    License, Origin, Parser, Replaces, Space, Update, Work,
                    encoder_field)


#: A local name: the collection's directory under
#: `<LOUISE_ROOT>/collections/` (07 §7.7's third form, §7.8's tree). It is
#: this peer's own word for the collection and is NOT derived from the id
#: — a name holds one collection for its life, and another collection is
#: another name.
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

CODEX_FILE = "codex.json"
SHARD_DIR = "shard"


def check_name(name: str) -> str:
    if not _NAME_RE.match(name or ""):
        raise CodexError(f"{name!r} is not a collection name: letters, digits, "
                         f"then any of . _ - (07 §7.8)")
    return name


def dir_of(root: Path, name: str) -> Path:
    """`<LOUISE_ROOT>/collections/<name>/` — the collection's whole
    directory: the SHARD, the state, the index and the vector table."""
    return root / check_name(name)


def shard_dir_of(root: Path, name: str) -> Path:
    """`<name>/shard/` — the copy, exactly the tree of 02 §2.6, which is
    what `share` serves and nothing else (07 §7.8). `codex.json` is the
    first line of that tree, so it lives HERE and not a level up: it is a
    member, and one of the three fetchable before an acknowledgement."""
    return dir_of(root, name) / SHARD_DIR


def default_name(root: Path, codex: Codex) -> str:
    """07 §7.8: the plugin's name, or the plugin's name and the first 12 hex
    characters of the id when that is taken."""
    if not (root / codex.plugin).exists():
        return codex.plugin
    return f"{codex.plugin}-{codex.collection_id.split(':', 1)[1][:12]}"


def write(root: Path, codex: Codex, mother: Codex | None = None, *,
          name: str | None = None) -> Path:
    cx.validate(codex, mother)
    root.mkdir(parents=True, exist_ok=True)
    # Idempotence is by IDENTITY, not by name: writing a Codex this root
    # already holds is nothing to do, whatever it is called here. Only a
    # genuinely new collection reaches the naming rule, so re-running
    # `create` with the same recipe never makes a second directory.
    for held_name, held in _held(root):
        if held.collection_id == codex.collection_id:
            if name and name != held_name:
                raise CodexError(
                    f"{codex.collection_id} is already held here as "
                    f"{held_name!r}; a collection has one local name")
            return shard_dir_of(root, held_name)
    name = check_name(name) if name else default_name(root, codex)
    d = shard_dir_of(root, name)
    p = d / CODEX_FILE
    if p.exists():
        # Idempotent, but only for the SAME collection: a name holds one
        # collection for its life (07 §7.8), so re-using it for another is
        # refused rather than silently rebinding what every local reference
        # already points at.
        held = cx.loads(p.read_bytes())
        if held.collection_id != codex.collection_id:
            raise CodexError(
                f"the name {name!r} already holds {held.collection_id}; a name "
                f"holds one collection for its life, and another collection is "
                f"another name (07 §7.8)")
        return d
    if not link_shared_roots(root, name):
        d.mkdir(parents=True, exist_ok=False)
    p.write_bytes(codex.canonical())
    return d


def link_shared_roots(root: Path, name: str) -> bool:
    """ONE MOUNT PER KIND, A FOLDER PER COLLECTION (operator, 2026-10-12).
    With `SHARDS_ROOT` set, `<name>/shard` is a symlink to
    `<SHARDS_ROOT>/<name>`, made here when the collection is first written;
    with `ORIGINS_ROOT` set, `<name>/origin` likewise points at
    `<ORIGINS_ROOT>/<name>` (left alone when it already exists: a custom
    origin, gutenberg's, is linked by hand). Every path a manifest stores
    stays `<root>/<name>/…`, so nothing is rescanned or relocated, and a new
    collection needs no new mount. Only for the node's own root
    (`<LOUISE_ROOT>/collections`): a `--root` elsewhere is untouched.
    Returns whether the SHARD directory was linked (and so exists)."""
    import os
    from app import config
    if Path(root).resolve() != (config.louise_root() / "collections").resolve():
        return False
    base = dir_of(root, name)
    linked = False
    shards = os.getenv("SHARDS_ROOT", "").strip()
    if shards:
        target = Path(shards) / name
        if target.exists() and any(target.iterdir()):
            raise CodexError(f"{target} already holds files: another collection's SHARD under "
                             f"this name? Nothing is linked over it")
        target.mkdir(parents=True, exist_ok=True)
        base.mkdir(parents=True, exist_ok=True)
        (base / SHARD_DIR).symlink_to(target, target_is_directory=True)
        linked = True
    origins = os.getenv("ORIGINS_ROOT", "").strip()
    link = base / "origin"
    if origins and not link.exists() and not link.is_symlink():
        target = Path(origins) / name
        target.mkdir(parents=True, exist_ok=True)
        base.mkdir(parents=True, exist_ok=True)
        link.symlink_to(target, target_is_directory=True)
    return linked


def _normalise(cid: str) -> str:
    return cid if cid.startswith("sha256:") else "sha256:" + cid


#: {name: reason} for every held directory whose Codex would not load,
#: filled by the last `_held` call. Module-level for the same reason
#: `collections._REFUSALS` is: the enumeration returns what it CAN serve,
#: and the caller that wants to report the rest asks afterwards.
_UNREADABLE: dict[str, str] = {}


def _held(root: Path) -> list[tuple[str, Codex]]:
    """Every Codex held here that can be READ.

    ONE UNREADABLE CODEX REFUSES ITSELF, NOT EVERY COMMAND — invariant
    1's doctrine ("the node keeps running its others"), which this
    function did not follow. It called `cx.loads` on every
    `<name>/shard/codex.json` under the root and let the exception out, so
    a single bad file took down `list`, `show`, `resolve` and `create`
    alike.

    Found 2026-09-13, and not hypothetically: the live node holds four
    Codexes — `gutenberg`, `pubmed`, and the retired
    `gutenberg.parser4-2026-09-12` and `gutenberg.retired-2026-09-12` —
    and NONE of them carries §9.3's `derivation`. Migrating the two the
    worker serves would have left every `hold` verb still raising on a
    retired directory that nothing reads and nobody would think to look
    at.

    What is skipped is not hidden: `unreadable()` returns it, and `list`
    prints it. The one caller that loses something real is `write`'s
    idempotence check, which cannot match an id it cannot read — and
    refusing every create because one retired directory is stale is the
    worse failure of the two, with the name collision below still caught.
    """
    out = []
    _UNREADABLE.clear()
    for d in sorted(root.iterdir()) if root.exists() else []:
        p = d / SHARD_DIR / CODEX_FILE
        if not p.is_file():
            continue
        try:
            out.append((d.name, cx.loads(p.read_bytes())))
        except Exception as e:          # noqa: BLE001 — its own refusal
            _UNREADABLE[d.name] = f"{type(e).__name__}: {e}"
    return out


def unreadable() -> dict[str, str]:
    """What the last `_held` could not load, {name: reason}."""
    return dict(_UNREADABLE)


def resolve(root: Path, ref: str) -> tuple[str, Codex]:
    """07 §7.7's four forms, in this order: a full collection id with or
    without its prefix; a unique prefix of at least 12 hex characters; a
    local name; a lineage id, which resolves to the leading daughter held
    here. Ambiguity is exit 2, naming the candidates.

    Returns `(name, codex)` — the caller almost always needs both, since
    the name is where the SHARD and the state directory live.
    """
    held = _held(root)
    if not held:
        raise CodexError(f"no collections under {root}")

    exact = [(n, c) for n, c in held if c.collection_id == _normalise(ref)]
    if exact:
        return exact[0]

    bare = ref[7:] if ref.startswith("sha256:") else ref
    if len(bare) >= 12 and all(ch in "0123456789abcdef" for ch in bare):
        hits = [(n, c) for n, c in held
                if c.collection_id.split(":", 1)[1].startswith(bare)]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            raise CodexError(f"{ref!r} is ambiguous: "
                             f"{', '.join(c.collection_id for _, c in hits)}")

    by_name = [(n, c) for n, c in held if n == ref]
    if by_name:
        return by_name[0]

    res = resolver(root)
    lineage = [(n, c) for n, c in held
               if cx.lineage_id(c, res) == _normalise(ref)]
    if len(lineage) == 1:
        return lineage[0]
    if len(lineage) > 1:
        # 04 §4.6's leading daughter: the longest `continues` chain held.
        def depth(c: Codex) -> int:
            return sum(1 for _ in cx.walk_lineage(c, res))
        best = max(lineage, key=lambda nc: depth(nc[1]))
        return best

    raise CodexError(f"no collection {ref!r} under {root}; it is not an id, a "
                     f"12-character prefix, a name held here, or a lineage")


def read(root: Path, ref: str) -> Codex:
    return resolve(root, ref)[1]


def name_of(root: Path, ref: str) -> str:
    return resolve(root, ref)[0]


def resolver(root: Path):
    def _resolve(cid: str) -> Codex | None:
        want = _normalise(cid)
        for _, c in _held(root):
            if c.collection_id == want:
                return c
        return None
    return _resolve


def list_all(root: Path) -> list[Codex]:
    return [c for _, c in _held(root)]


def list_named(root: Path) -> list[tuple[str, Codex]]:
    return _held(root)


def _merge_credits(kept: Iterable[Credit],
                   archives: Iterable[Credit]) -> tuple[Credit, ...]:
    """The corpus and community entries a relative keeps, plus whichever
    archive entries the space gives her, in 09 §9.3's order.

    Sorting here rather than trusting the caller is the point: `credits` is
    hashed into `collection_id`, so two peers deriving the same sister must
    produce the same list in the same order or they have made two
    collections out of one (02 §2.7).
    """
    return tuple(sorted(tuple(kept) + tuple(archives), key=cx.credit_order))


# ---- the four ways to make one -------------------------------------------

def create(root: Path, codex: Codex, *, name: str | None = None) -> Codex:
    """A root: no lineage fields, and `space.epoch` is "0"."""
    if codex.relation != "root":
        raise CodexError("create makes a root; use derive/continue/replace "
                         "for the others")
    write(root, codex, name=name)
    return codex


def create_from_recipe(root: Path, plugin: str, recipe: dict[str, Any], *,
                       platform_image: str, embed, credits: tuple = (),
                       name: str | None = None,
                       key: Any | None = None,
                       now: str | None = None,
                       embed_query=None,
                       space_version: int | None = None,
                       pairs: bytes | None = None) -> Codex:
    """`hold collection create <plugin> --recipe FILE` (06 §6.6, 07 §7.3).

    `pairs`: the bytes of a PAIR space's calibration set — 512 title-and-
    abstract pairs drawn from the collection's records by
    tools/build_pair_calibration_set.py, whose `H` the recipe names as
    `space.calibration.set` (09 §9.12, amended 2026-10-08). Required exactly
    when the recipe's layers feed the encoder pairs; the member carries it.

    Re-running with the same recipe prints the same id and exits 0: the hash
    is the identity, and writing it twice is nothing.

    THIS NEEDS AN ENCODER. 06 §6.6 step 2: "the calibration comes first and
    needs no record: the calibration set is a corpus-independent file
    shipped with the platform, `create` embeds it with the named encoder,
    writes the reference vectors' hash into `space.calibration` and their
    envelope into `space.envelope`, and stores the vectors as the SHARD
    member `calibration/<set>.safetensors`". So `embed` is required and has
    no offline default: those two hashes are what every peer's gate and
    every T0 admission are decided against, and a collection created with
    placeholders for them is one nobody else can ever join.

    It runs BEFORE the Codex is built, because the hashes are Codex fields —
    an encoder that cannot answer means no collection, not a collection
    with a hole in it.
    """
    from . import calibration as cal, shard as shard_mod, envelope as env, template

    space = recipe.get("space")
    if not isinstance(space, dict):
        raise CodexError("recipe: a space block is required (06 §6.6)")
    set_id = (space.get("calibration") or {}).get("set")
    if not set_id:
        raise CodexError(
            "recipe: space.calibration.set names the 512-text set the "
            "reference vectors are produced over (09 §9.12); without it "
            "there is nothing to embed")
    modality = recipe.get("modality") or template.source_for(plugin).modality.value
    # `verify` and not `load`: the recipe names a set by hash, and a
    # platform whose file for this modality is a different 512 texts would
    # produce a reference that measures something else entirely. A pair
    # space's set is the one given, bound by the same hash.
    texts, pairs = _calibration_items(modality, set_id,
                                      _form_of_recipe(plugin, recipe), pairs)
    vectors, envelope, vectors_hash, envelope_hash = cal.build_member(
        texts, embed, normalise=bool(space.get("normalise")),
        dim=int(space["dim"]))

    # AN ASYMMETRIC SPACE HAS TWO FUNCTIONS, and the Codex pins a reference
    # for each (09 §9.12, `query_vectors`): a bi-encoder pair such as MedCPT's
    # embeds a query with another model, and a gate that compared only the
    # document side would leave every query vector on a peer unchecked.
    query = space.get("query") if isinstance(space.get("query"), dict) else {}
    query_vectors = query_hash = None
    # a version-2 space copied whole (`space_version`) pins no query reference:
    # its calibration is {set, vectors, quorum} (09 §9.12 before the engine
    # amendment), and a member no field names would be a stray SHARD file
    image_shaped = (space_version is not None and space_version < cx.ENGINE_VERSION
                    and "image" in (space.get("encoder") or {}))
    if query.get("encoder") and not image_shaped:
        if embed_query is None:
            raise CodexError(
                "recipe: this space names a query encoder (space.query.encoder), "
                "so its reference vectors are part of the Codex too "
                "(space.calibration.query_vectors, 09 §9.12). Name the query "
                "encoder's endpoint: `--query-encoder URL`")
        query_vectors, query_hash = cal.build_query_member(
            texts, embed_query, instruction=query.get("instruction") or None,
            normalise=bool(space.get("normalise")), dim=int(space["dim"]))

    codex = template.from_recipe(plugin, recipe, platform_image=platform_image,
                                 calibration=(vectors_hash, envelope_hash, query_hash),
                                 credits=credits, space_version=space_version)
    created = create(root, codex, name=name)
    d = shard_dir_of(root, name_of(root, created.collection_id))
    write_calibration(d, codex, vectors, envelope, key=key, now=now,
                      query_vectors=query_vectors, pairs=pairs)
    write_terms(d, codex, plugin,
                terms_path=(recipe.get("license") or {}).get("terms_path"))
    return created


def _form_of_recipe(plugin: str, recipe: dict[str, Any]) -> str:
    """The form the recipe's layers feed the encoder (09 §9.3), read before
    the Codex exists — its calibration hashes are what it is built with."""
    from types import SimpleNamespace

    from . import calibration as cal, template
    if "layers" in recipe:
        layers = tuple(template._layer(d, i) for i, d in enumerate(recipe["layers"]))
    else:
        layers = template.default_layers(template.source_for(plugin))
    return cal.form(SimpleNamespace(layers=layers, name=plugin))


def _calibration_items(modality: str, set_id: str, form: str,
                       pairs: bytes | None) -> tuple[list, bytes | None]:
    """`(items, pairs)`: the set a new reference is embedded over, in the
    space's form (09 §9.12, amended 2026-10-08). A TEXT space's is the
    platform's file, exactly as ever, and a pair set given for it is
    refused. A PAIR space's is the pair set given — drawn from the
    collection's records, never shipped with the platform — bound by
    `set_id`; without one there is nothing to embed."""
    from . import calibration as cal
    if form == "pair":
        if pairs is None:
            raise CodexError(
                "this space's layers feed the encoder text PAIRS, so its calibration "
                "set is 512 title-and-abstract pairs drawn from the collection's own "
                "records (09 §9.12) — draw them with "
                "tools/build_pair_calibration_set.py and pass the file "
                "(`--calibration-set FILE`); the recipe names its sha256 as "
                "space.calibration.set")
        return cal.pairs_of(pairs, set_id), pairs
    if pairs is not None:
        raise CodexError(
            "a pair calibration set was given, and this space's layers feed the "
            "encoder text: a text space's set is the one the platform ships "
            "(09 §9.12)")
    return cal.verify(modality, set_id), None


def write_terms(shard_dir_path: Path, codex: Codex, plugin: str,
                terms_path: Any = None) -> None:
    """`LICENSE-TERMS` at the SHARD root — the origin's terms text, byte
    for byte (09 §9.13).

    A yellow or red collection's Codex PINS `license.terms_sha256`, and
    everything downstream needs the text those bytes hash to: `hold
    collection ack` cannot record an acknowledgement without it, `check`'s
    `license` compares against it, and 04 §4.4 makes it one of the three
    members a peer may fetch BEFORE acknowledging anything. Nothing wrote
    it until 2026-09-10, so `create` produced a yellow collection that
    could never be acknowledged and therefore never built — found taking
    `pubmed` through step 8.

    Copied from the plugin's own file and then CHECKED against the hash
    the Codex pins, because "byte-exact and never converted" is the whole
    property: a CRLF checkout that changed a line ending would silently
    invalidate every ack ever recorded against it.
    """
    import hashlib

    want = codex.license.terms_sha256
    if not want:
        return                       # green: no member, and none expected
    # The recipe's own file first, then the plugin's. A collection may
    # TIGHTEN its class (04 §4.4) — green to yellow — and its plugin then
    # ships no terms text at all, so the creator has to supply the one
    # they are tightening to. Without this the tightening is declarable
    # and unusable.
    src = terms_path
    if not src:
        from app.acquire.specs import ACQUIRE_REGISTRY
        spec = ACQUIRE_REGISTRY.get(plugin)
        src = getattr(spec, "terms_text_path", None) if spec else None
    if not src or not Path(src).is_file():
        raise CodexError(
            f"{codex.license.cls}-class: the Codex pins terms {want[:23]}… "
            f"and there is no terms text to write as LICENSE-TERMS (09 "
            f"§9.13) — plugin {plugin!r} ships none and the recipe names no "
            f"`license.terms_path`. The collection would be one nobody could "
            f"ever acknowledge")
    body = Path(src).read_bytes()
    got = "sha256:" + hashlib.sha256(body).hexdigest()
    if got != want:
        raise CodexError(
            f"the recipe pins license.terms_sha256 {want[:23]}… and "
            f"{Path(src).name} hashes to {got[:23]}…. Terms are byte-exact "
            f"and never converted (09 §9.13); nothing is written")
    (shard_dir_path / "LICENSE-TERMS").write_bytes(body)


def require_signer(key, now: str | None) -> None:
    """What signing a calibration reference needs, refused before any work."""
    if key is None:
        raise CodexError(
            "creating a collection needs the node's signing key: 09 §9.12 "
            "makes the calibration member's `key` header required and §9.4 "
            "lists its signature among a snapshot's files without allowing "
            "null. The reference records WHO produced it (invariant 8: the "
            "key is referenced by path, NODE_KEY_PATH)")
    if not now:
        raise CodexError(
            "signing the calibration reference needs the time to sign it "
            "at; nothing under `hold` reads the clock (the snapshots do the "
            "same), so a caller supplies it")


def write_calibration(shard_dir_path: Path, codex: Codex, vectors, envelope,
                      *, key, now: str | None, query_vectors=None,
                      pairs: bytes | None = None) -> None:
    """The member and the signature beside it (09 §9.12).

    `pairs`: a PAIR space's set, which its member carries (amended
    2026-10-08) — given exactly when the space's layers feed pairs. Its
    signature is then body version 3; a text space's member and body are
    byte for byte what they were.

    BOTH, ALWAYS — which is why `key` is required. §9.12 marks the member's
    `key` header required ("the creator's key"), and 09 §9.4's `files` lists
    `calibration/<set>.sig.json` without the "or null" it gives LICENSE-TERMS
    and seals. So a collection whose creator signed nothing has a member
    with an empty required header and a snapshot that cannot list what §9.4
    says it lists — and 07 §7.8 then refuses the whole SHARD, since `share`
    refuses a SHARD holding a file no snapshot names.
    """
    from . import calibration as cal, shard as shard_mod, envelope as env

    require_signer(key, now)

    set_id = codex.space.calibration.set
    member, sig_path = shard_mod.calibration_paths(set_id)
    key_text = env.key_text(key.verify_key)
    # The run the vectors came from: the engine, by its id, from Codex
    # version 3; the engine's image alone below it (09 §9.12).
    engine_key, engine = encoder_field(codex)
    pinned = codex.space.calibration.query_vectors
    if (pinned is None) != (query_vectors is None):
        raise CodexError(
            f"{codex.collection_id}: "
            + ("the Codex pins the query encoder's reference vectors "
               "(space.calibration.query_vectors) and none were given: an "
               "asymmetric space's member carries them (09 §9.12)" if pinned
               else "query reference vectors were given for a space whose Codex "
                    "pins none"))
    paired = cal.form(codex) == "pair"
    if paired != (pairs is not None):
        raise CodexError(
            f"{codex.collection_id}: "
            + ("the space's layers feed the encoder pairs, and its member carries "
               "the pair set its vectors were produced over (09 §9.12); none was "
               "given" if paired
               else "a pair set was given for a space whose layers feed text"))
    cal.write_member(shard_dir_path / member, vectors, envelope, set_id_=set_id,
                     encoder_weights=codex.space.encoder.weights, key=key_text,
                     query_vectors=query_vectors, pairs=pairs, **{engine_key: engine})

    # 09 §9.12's body, field for field, under ITS OWN domain. Not the
    # attestation domain: §9.2's whole point is that a signature made under
    # one domain verifies under no other, so signing this as an attestation
    # would produce a file no conformant peer can check. Version 2 names the
    # engine; "version 1 of that body carried `encoder_image`". Version 3 is
    # the body of a member that carries `pairs` (amended 2026-10-08), and
    # only of one: a text space's stays 2, so no text space's signature moves.
    body = {"kind": "louise-calibration",
            "version": (3 if paired else 2) if engine_key == "encoder_engine" else 1,
            "set": set_id,
            "space_id": codex.space_id,
            "vectors": codex.space.calibration.vectors,
            "envelope": codex.space.envelope,
            engine_key: engine,
            "encoder_weights": codex.space.encoder.weights,
            "key": key_text, "time": now}
    (shard_dir_path / sig_path).write_text(
        json.dumps(env.sign(env.CALIBRATION, body, key),
                   ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def derive(root: Path, mother_id: str, *,
           space: Space | None = None,
           space_from: Codex | None = None,
           layers: Iterable[Layer] | None = None,
           update: Update | None = None,
           work: Work | None = None,
           credits: tuple[Credit, ...] | None = None,
           descriptive: Descriptive | None = None,
           embed=None, embed_query=None, key=None,
           now: str | None = None, pairs: bytes | None = None,
           name: str | None = None) -> Codex:
    """A sister: her mother's corpus, and at most `space`, `layers`, `update`
    and `work` changed (02 §2.7).

    `pairs`: when she embeds a reference of her own in a PAIR space, the
    bytes of its calibration set (09 §9.12, amended 2026-10-08), as for
    `create_from_recipe`; a reference she inherits carries its set already.

    HER CODEX VERSION FOLLOWS HER SPACE. In her mother's space she keeps her
    mother's version: a version-2 mother's space_id has the engine's image in
    it (09 §9.3), so a version-3 sister would land in another space and
    could not share the vectors she is defined to inherit. In a NEW space
    she is written as new Codices are — CODEX_VERSION, naming the engine her
    reference vectors are computed with. An engine carrying load options
    needs version 4 (09 §9.3) wherever she is: the engine is outside
    `space_id`, so a sister in her mother's space may name a new one. A
    BACKEND engine makes her version 6, and only it (09 §9.3, amended
    2026-10-08): a `--space-from` copy of a version-6 space is 6, of a
    version-5 one 5 (02 §2.7), and a sister of a version-6 mother naming an
    argv engine is 5.

    `embed` IS REQUIRED WHEN THE SISTER'S ENCODER DIFFERS, and ignored when
    it does not. A new build needs its own reference vectors — the mother's
    were produced by another build, so comparing against them would compare
    nothing — and 06 §6.6 puts that embedding at collection creation. A
    sister who changes only her cadence, her layers or her work inherits
    the mother's member and needs no endpoint. `--space-from` inherits the
    SOURCE's, since her space was copied from it byte for byte.

    `cleaning_version` is never copied and never changed — changing it would
    re-derive the corpus she is defined to inherit. `space_from` copies
    another Codex's `space` and `layers` byte for byte and is refused when
    that Codex's `cleaning_version` differs from the mother's, because the
    resulting `space_id` could not then equal the source's.

    The ARCHIVE credits follow the space (02 §2.7): her mother's when
    `space_id` matches, the `space_from` Codex's when that supplied the
    space, and otherwise whatever `credits` names — none when it names
    nothing. The caller expands archive ids into `Credit`s from the
    platform's registry, exactly as `create` does; nothing here composes one
    by hand.

    The CORPUS and COMMUNITY credits do not follow the space and are kept
    on every path: a sister shares the corpus by definition, and a new
    encoder does not unwrite whose text it read.
    """
    mother = read(root, mother_id)

    if space_from is not None:
        if space is not None or layers is not None:
            raise CodexError("derive: --space-from supplies the space and the "
                             "layers; do not also give them")
        if space_from.cleaning_version != mother.cleaning_version:
            raise CodexError(
                f"derive: --space-from names a Codex whose cleaning_version is "
                f"{space_from.cleaning_version} and the mother's is "
                f"{mother.cleaning_version}. A sister keeps her mother's, so "
                f"the resulting space_id could not equal the source's — which "
                f"is the whole of what the flag promises")
        space, layers = space_from.space, space_from.layers

    new_layers = tuple(layers) if layers is not None else mother.layers
    if space_from is not None:
        new_space = space          # the source's space, epoch included
    elif space is not None:
        # A sister keeps her mother's epoch: only a corrective sister mints
        # one, and forcing "0" here would put the sister of a corrected
        # collection in a different space from her mother for no reason.
        new_space = _replace(space, epoch=mother.space.epoch)
    else:
        new_space = mother.space

    if new_space.has_engine:
        # In her mother's space she keeps her mother's version; in a new one
        # she is written as new Codices are. An engine carrying `options`
        # needs version 4 either way (09 §9.3): in her mother's space too,
        # since the engine is outside `space_id` and a sister may name a new
        # one there.
        in_mothers_space = (mother.version >= cx.ENGINE_VERSION
                            and _replace(mother, space=new_space,
                                         layers=new_layers).space_id == mother.space_id)
        version = mother.version if in_mothers_space else cx.CODEX_VERSION
        if (not in_mothers_space and space_from is not None
                and space_from.version < cx.INPUT_VERSION):
            # the source's layers carry no `input`; at version 5 they would,
            # and her space_id could not equal the source's (09 §9.3,
            # amended 2026-10-08) — the last version without it
            version = cx.INPUT_VERSION - 1
        if new_space.has_options:
            version = max(version, cx.OPTIONS_VERSION)
        if (new_space.has_backend and in_mothers_space
                and mother.version < cx.INPUT_VERSION):
            # A backend engine makes her version 6 (09 §9.3, amended
            # 2026-10-08), whose layers carry `input` — which her mother's,
            # below version 5, do not. So she could not keep her mother's
            # space_id, which is what staying in it promises.
            raise CodexError(
                f"derive: a backend engine is admitted from Codex version "
                f"{cx.BACKEND_VERSION}, whose layers carry `input`, and her mother is "
                f"version {mother.version}, whose layers do not — so a sister naming "
                f"one cannot stay in her mother's space (09 §9.3). An engine joins a "
                f"space by passing the gate, not by a new Codex (03 §3.3): validate "
                f"the run, or derive her in a new space")
        # version 6 for a space carrying a backend engine, and for it alone
        # (09 §9.3, amended 2026-10-08; 02 §2.7 for `--space-from`)
        version = cx.written_version(new_space, version)
    elif mother.version >= cx.ENGINE_VERSION:
        raise CodexError(
            "derive: a space is written with the ENGINE its reference vectors are "
            "computed with — {weights, engine, label} — not with an image alone "
            "(09 §9.3, the engine amendment)")
    else:
        version = mother.version

    sister = _replace(
        mother,
        version=version,
        descriptive=descriptive if descriptive is not None else mother.descriptive,
        space=new_space,
        layers=new_layers,
        update=update if update is not None else mother.update,
        work=work if work is not None else mother.work,
        derived_from=mother.collection_id,
        space_from=None if space_from is None else space_from.collection_id,
        continues=None, continues_through=None, origin_reachable_until=None,
        replaces=None,
        toolchain=_replace(mother.toolchain, mother_platform_image=None),
    )

    # Only the archive entries are in play. `kept` is what descends on every
    # path — the corpus entry (hers because the corpus is hers) and the
    # community entry (hers because her `embedding` is her mother's).
    kept = tuple(c for c in mother.credits if c.kind != cx.ARCHIVE)
    same_space = sister.space_id == mother.space_id
    if credits is not None:
        if same_space:
            raise CodexError("derive: a sister in her mother's space carries "
                             "her mother's archive credits; --archives is "
                             "refused here")
        if space_from is not None:
            raise CodexError("derive: --space-from brings that Codex's archive "
                             "credits; --archives is refused here")
        sister = _replace(sister, credits=_merge_credits(kept, credits))
    elif space_from is not None:
        sister = _replace(sister, credits=_merge_credits(
            kept, cx.archive_credits(space_from)))
    elif not same_space:
        sister = _replace(sister, credits=_merge_credits(kept, ()))

    sources = (mother,) if space_from is None else (mother, space_from)
    own = None
    if embed is not None and not any(_same_reference(sister, s) for s in sources):
        # A new build: embed ONCE, as `create` does, and let the new hashes
        # into the Codex BEFORE it is written, since they are Codex fields;
        # the member is written from these very vectors (2026-10-09: it
        # embedded the set a second time and required the same bytes back,
        # which an engine whose runs differ in the last bit could never give)
        from . import calibration as cal
        require_signer(key, now)          # before the embedding, before the Codex
        items, pairs = _calibration_items(mother.modality, sister.space.calibration.set,
                                          cal.form(sister), pairs)
        vectors, envelope, got_v, got_e = cal.build_member(
            items, embed, normalise=sister.space.normalise, dim=sister.space.dim)
        query_vectors, got_q = _query_member(sister.space, items, embed_query)
        sister = _replace(sister, space=_replace(
            sister.space, envelope=got_e,
            calibration=_replace(sister.space.calibration, vectors=got_v,
                                 query_vectors=got_q)))
        own = (vectors, envelope, query_vectors)
    shard_dir_path = write(root, sister, mother, name=name)
    if any(_same_reference(sister, s) for s in sources):
        carry_calibration(root, sister, shard_dir_path, *sources)
    elif own is not None:
        write_calibration(shard_dir_path, sister, own[0], own[1], key=key, now=now,
                          query_vectors=own[2], pairs=pairs)
    else:
        _write_own_calibration(sister, shard_dir_path, embed, mother.modality,
                               key, now, embed_query, pairs=pairs)
    return sister


def _names_encoder(codex: Codex) -> str:
    """How a message names a Codex's encoder: the engine, by its id, from
    version 3; the image below it."""
    key, value = encoder_field(codex)
    return f"{'engine' if key == 'encoder_engine' else 'encoder'} {value}"


def _query_member(space: Space, texts, embed_query):
    """The query side's reference vectors for an asymmetric version-3 space,
    `(vectors, hash)`; `(None, None)` where the space pins none."""
    from . import calibration as cal

    if not (space.has_engine and space.query.encoder is not None):
        return None, None
    if embed_query is None:
        raise CodexError(
            "this space has a query encoder of its own (space.query.encoder), and "
            "its reference vectors are pinned in the Codex too "
            "(space.calibration.query_vectors, 09 §9.12): an asymmetric space's "
            "query runs are validated against them. Name the query encoder's "
            "endpoint — pass `embed_query`")
    return cal.build_query_member(texts, embed_query,
                                  instruction=space.query.instruction,
                                  normalise=space.normalise, dim=space.dim)


def _write_own_calibration(codex: Codex, shard_dir_path: Path, embed,
                           modality: str, key, now: str | None,
                           embed_query=None, pairs: bytes | None = None) -> None:
    """The member and signature for a collection whose reference is its own
    and not a source's. Re-embeds rather than keeping the vectors from
    `recalibrate`, because the two hashes are already in the Codex and this
    must produce the file they name — the check below catches a
    disagreement, which is the point of doing it this way round."""
    from . import calibration as cal

    if embed is None:
        raise CodexError(
            f"{codex.collection_id} names {_names_encoder(codex)}, "
            f"which no source it derives from holds a reference for. A new "
            f"build needs its own reference vectors, embedded at an endpoint "
            f"(06 §6.6) — pass `embed`")
    texts, pairs = _calibration_items(modality, codex.space.calibration.set,
                                      cal.form(codex), pairs)
    vectors, envelope, got_v, got_e = cal.build_member(
        texts, embed, normalise=codex.space.normalise, dim=codex.space.dim)
    if (got_v, got_e) != (codex.space.calibration.vectors, codex.space.envelope):
        raise CodexError(
            f"the encoder produced reference vectors {got_v} and the Codex "
            f"names {codex.space.calibration.vectors}; the same build over "
            f"the same set must produce the same bytes twice")
    query_vectors, got_q = _query_member(codex.space, texts, embed_query)
    if got_q != codex.space.calibration.query_vectors:
        raise CodexError(
            f"the query encoder produced reference vectors {got_q} and the Codex "
            f"names {codex.space.calibration.query_vectors}")
    write_calibration(shard_dir_path, codex, vectors, envelope, key=key, now=now,
                      query_vectors=query_vectors, pairs=pairs)


def _same_reference(a: Codex, b: Codex) -> bool:
    """Whether two Codices name the same calibration reference — the same
    encoder build over the same set, with the same resulting hashes. Then
    one's member IS the other's and copying it is exact. The build is the
    engine, by its id, from version 3, and the image below it; a version-2
    Codex and a version-3 one never share a member, whose header names the
    run differently."""
    def reference(c: Codex):
        return (encoder_field(c), c.space.encoder.weights,
                c.space.calibration.set, c.space.calibration.vectors,
                c.space.calibration.query_vectors, c.space.envelope)
    return reference(a) == reference(b)


def carry_calibration(root: Path, child: Codex, shard_dir_path: Path,
                      *sources: Codex) -> None:
    """Copy a derived collection's calibration member from whichever source
    holds the same reference.

    The member is a member of EVERY snapshot (09 §9.4's `files`), and T0's
    envelope, T1's probe set, T2's goldens and the gate's reference are all
    read from it — so a daughter or sister without one cannot be checked,
    shared, or admitted from. Only `create` and a new-encoder `derive`
    embed; everything else INHERITS, because the reference is a property of
    the encoder build and re-embedding the same set with the same build to
    get bit-identical vectors is not something float arithmetic promises.

    `sources` is tried in order: the mother, and for `--space-from` the
    Codex that supplied the space — whose member is the one the sister's
    hashes name, since her space was copied from it byte for byte.
    """
    from . import calibration as cal, shard as shard_mod

    src = next((s for s in sources if _same_reference(child, s)), None)
    if src is None:
        raise CodexError(
            f"{child.collection_id} names {_names_encoder(child)} "
            f"over calibration set {child.space.calibration.set}, and no "
            f"source it derives from holds that reference. A different build "
            f"needs its own reference vectors, which means embedding the set "
            f"at an endpoint (06 §6.6) — pass `embed`")

    src_shard = shard_dir_of(root, name_of(root, src.collection_id))
    for name in shard_mod.calibration_paths(src.space.calibration.set):
        origin = src_shard / name
        if not origin.is_file():
            if name.endswith(".sig.json"):
                continue                # optional: the hashes bind, not it
            raise CodexError(
                f"{src.collection_id} has no calibration member at {name}. "
                f"It is written by `hold collection create` (09 §9.12); a "
                f"collection derived from one that lacks it would inherit a "
                f"SHARD nothing can check")
        dst = shard_dir_path / name
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(origin.read_bytes())

    # The Codex's hashes bind the member (09 §9.12), so what was copied is
    # checked against the CHILD's Codex and not assumed from the source's.
    member, _ = shard_mod.calibration_paths(child.space.calibration.set)
    cal.read_member(shard_dir_path / member,
                    expect_vectors=child.space.calibration.vectors,
                    expect_envelope=child.space.envelope)
    if child.space.calibration.query_vectors is not None:
        cal.read_query_vectors(shard_dir_path / member,
                               expect=child.space.calibration.query_vectors)
    if cal.form(child) == "pair":
        # a pair space's member carries its set (09 §9.12, amended
        # 2026-10-08): a text space's member, however equal its hashes, is
        # not one a pair child can be gated with
        cal.read_pairs(shard_dir_path / member, expect=child.space.calibration.set)


def recalibrate(space: Space, modality: str, embed, embed_query=None,
                items=None) -> Space:
    """`space` with the reference vectors THIS encoder produces.

    A sister in a new space has a new encoder build, and a new build needs
    its own reference: the mother's vectors were produced by another build
    and comparing against them would compare nothing. So `derive` embeds
    exactly as `create` does (06 §6.6), and the two hashes it writes are
    outputs here too — whatever the caller's `space` carried in those two
    fields is replaced, never trusted.

    Neither hash is inside `space_id` (02 §2.4), so this does not move the
    sister's space; it makes her SHARD checkable.

    `items`: the set in the space's form (`_calibration_items`); the
    platform's texts when not given.
    """
    from . import calibration as cal

    texts = items if items is not None else cal.verify(modality, space.calibration.set)
    _, _, vectors, envelope = cal.build_member(
        texts, embed, normalise=space.normalise, dim=space.dim)
    _, query_vectors = _query_member(space, texts, embed_query)
    return _replace(space, envelope=envelope,
                    calibration=_replace(space.calibration, vectors=vectors,
                                         query_vectors=query_vectors))


def continue_(root: Path, mother_id: str, *, platform_image: str,
              continues_through: str, origin: Origin | None = None,
              parser: Parser | None = None, license: License | None = None,
              origin_reachable_until: str | None = None,
              descriptive: Descriptive | None = None) -> Codex:
    """A daughter: the outside world changed, the toolchain adapts, the space
    stays. The encoder is inherited and cannot be given here — an encoder
    change is a sister.

    The boundary is named from the mother's side: `continues_through` is her
    LAST snapshot, which exists and is content-addressed, rather than the
    daughter's first, which does not exist when her Codex is written.
    """
    mother = read(root, mother_id)
    daughter = _replace(
        mother,
        descriptive=descriptive if descriptive is not None else mother.descriptive,
        toolchain=_replace(mother.toolchain, platform_image=platform_image,
                           mother_platform_image=mother.toolchain.platform_image),
        origin=origin if origin is not None else mother.origin,
        parser=parser if parser is not None else mother.parser,
        license=license if license is not None else mother.license,
        continues=mother.collection_id,
        continues_through=continues_through,
        origin_reachable_until=origin_reachable_until,
        derived_from=None, space_from=None, replaces=None,
    )
    carry_calibration(root, daughter, write(root, daughter, mother), mother)
    return daughter


def replace(root: Path, mother_id: str, *, defect: Defect,
            platform_image: str | None = None, space: Space | None = None,
            descriptive: Descriptive | None = None) -> Codex:
    """A corrective sister: the mother's origin, a corrected toolchain, and a
    fresh space by construction — `space.epoch` is salted with the replaced
    Codex's id, so no vector of the mother's is admissible to her.

    The defect travels with her, and until a peer has reproduced it she has
    no right to mark anything superseded (02 §2.7, 03 §3.6).
    """
    mother = read(root, mother_id)
    tc = mother.toolchain
    if platform_image:
        tc = _replace(tc, platform_image=platform_image)
    tc = _replace(tc, mother_platform_image=None)
    base = space if space is not None else mother.space
    corrective = _replace(
        mother,
        # her space is fresh by construction (the salted epoch), so her
        # version is free to follow her engine: 6 with a backend engine and
        # only with one (09 §9.3, amended 2026-10-08)
        version=cx.written_version(base, mother.version),
        descriptive=descriptive if descriptive is not None else mother.descriptive,
        toolchain=tc,
        space=_replace(base, epoch=cx.corrective_epoch(mother.collection_id)),
        replaces=Replaces(mother.collection_id, defect),
        derived_from=None, space_from=None, continues=None,
        continues_through=None, origin_reachable_until=None,
        # No archive credit: a correction starts from zero and carries none
        # of the mother's vectors. It keeps the corpus and community entries,
        # which were never about the vectors it discarded — the text is the
        # same text, re-derived from the same origin (02 §2.7).
        credits=_merge_credits(
            (c for c in mother.credits if c.kind != cx.ARCHIVE), ()),
    )
    carry_calibration(root, corrective, write(root, corrective, mother), mother)
    return corrective


def show(root: Path, ref: str) -> dict[str, Any]:
    """What `hold collection show --json` prints (09 §9.3). The parts that
    need the SHARD on disk — snapshots, holdings, trust_level — arrive with
    the members they read; `refused` is the reason the collection is not
    served when refuse-to-start applies to it (07 §7.8)."""
    name, codex = resolve(root, ref)
    return {
        "name": name,
        "collection_id": codex.collection_id,
        "codex_hash": codex.codex_hash,
        "lineage_id": cx.lineage_id(codex, resolver(root)),
        "relation": codex.relation,
        "space_id": codex.space_id,
        "space": codex.space_segment,
        "publication": codex.publication,
        "license": {"class": codex.license.cls},
        # Every credit, not only the archives: a reader asking whom this
        # collection stands on is owed the corpus provider and the community
        # too, which is the entire reason those entries exist (02 §2.7).
        "credits": [{"kind": c.kind, "archive": c.archive,
                     "producer": c.producer, "attribution": c.attribution}
                    for c in codex.credits],
        "refused": None,
        # 02 §2.10: what an annotations collection makes claims about, and the
        # target snapshot they hold from; null on every other collection
        "annotates": (None if codex.annotates is None else
                      {**codex.annotates.to_dict(), "target": _target_name(root, codex)}),
        "codex": json.loads(codex.canonical()),
    }


def _target_name(root: Path, codex: Codex) -> str | None:
    """The local name of the collection an annotations Codex annotates, when
    this peer holds it."""
    for name, held in _held(root):
        if held.collection_id == codex.annotates.collection_id:
            return name
    return None


def migrate(root: Path, name: str, *, apply: bool = False) -> dict:
    """Bring a Codex written before a §9.3 field existed up to the schema.

    Adds every nullable §9.3 field the file predates (today:
    `derivation`), rewrites `codex.json` with the canonical bytes, and
    reports the identity change. `apply=False` is a dry run and writes
    nothing.

    The caller must then update the collection's TOML `codex = "…"` line,
    which is the `collection_id` and is refused on mismatch
    (`collections.py`). This does NOT do it, because the TOML is the
    operator's desired state (invariant 10) and a tool that edits it is a
    tool that edits config behind their back.

    Untouched: `space_id` (§2.4 excludes `derivation`), so every vector
    already computed stays admissible; records, chunks and snapshots,
    which are keyed on their own hashes and not on the collection's.
    """
    path = shard_dir_of(root, name) / CODEX_FILE
    raw = json.loads(path.read_bytes())
    before = cx.digest(cx.canonical_json(
        {k: v for k, v in raw.items() if k != "descriptive"}))
    migrated, added = cx.migrate_missing_nulls(raw)
    if not added:
        return {"name": name, "added": [], "before": before, "after": before,
                "written": False}
    codex = cx.from_dict(migrated)          # refuses if it is still not valid
    cx.validate(codex)
    result = {"name": name, "added": added, "before": before,
              "after": codex.collection_id, "written": False,
              "path": str(path), "carries_old_id": _carries_old_id(path.parent)}
    if apply:
        path.write_bytes(codex.canonical())
        result["written"] = True
    return result


def _carries_old_id(shard_tree: Path) -> list[str]:
    """SHARD members that name the collection by its CURRENT id, and so
    will refer to the old one after a migration.

    Not a reason to refuse — the identity change is the point, and the
    §9.3 field is missing whatever these say — but an operator who is
    told "collection_id moves" deserves to know WHAT ELSE says it.

    Two on the live pubmed SHARD:

      - `snapshots/*.json` carry `collection_id`. The chain check
        (`snapshot.verify_chain`) compares snapshot to snapshot, so one
        snapshot is fine; the NEXT one, written with the new id, would
        raise "collection changes mid-chain".
      - the signed attestations of `hold check` name their witnesses
        `"<collection_id>/<slice>/<n>"` (09 §9.8), so a fresh run
        produces different witness names than the ones already signed.

    Neither blocks a deploy and neither loses anything: `hold check`
    re-runs, and a snapshot chain that begins again is what a new
    identity means.
    """
    out: list[str] = []
    snaps = sorted((shard_tree / "snapshots").glob("*.json")) if shard_tree.is_dir() else []
    real = [p for p in snaps if not p.name.endswith(".sig.json")]
    if real:
        out.append(f"{len(real)} snapshot(s) — the chain restarts, because "
                   f"`verify_chain` refuses a collection that changes mid-chain")
    if (shard_tree / "validation.json").is_file():
        out.append("signed attestations name witnesses "
                   "`<collection_id>/<slice>/<n>` — re-run `hold check`")
    return out
