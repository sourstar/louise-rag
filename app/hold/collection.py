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
                    License, Origin, Parser, Replaces, Space, Update, Work)


#: A local name: the collection's directory under
#: `<LOUISE_ROOT>/collections/` (07 §7.7's third form, §7.8's tree). It is
#: this peer's own word for the collection and is NOT derived from the id
#: — a name holds one collection for its life, and another collection is
#: another name.
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

CODEX_FILE = "codex.json"
DRAG = "drag"


def check_name(name: str) -> str:
    if not _NAME_RE.match(name or ""):
        raise CodexError(f"{name!r} is not a collection name: letters, digits, "
                         f"then any of . _ - (07 §7.8)")
    return name


def dir_of(root: Path, name: str) -> Path:
    """`<LOUISE_ROOT>/collections/<name>/` — the collection's whole
    directory: the drag, the state, the index and the vector table."""
    return root / check_name(name)


def drag_of(root: Path, name: str) -> Path:
    """`<name>/drag/` — the copy, exactly the tree of 02 §2.6, which is
    what `share` serves and nothing else (07 §7.8). `codex.json` is the
    first line of that tree, so it lives HERE and not a level up: it is a
    member, and one of the three fetchable before an acknowledgement."""
    return dir_of(root, name) / DRAG


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
            return drag_of(root, held_name)
    name = check_name(name) if name else default_name(root, codex)
    d = drag_of(root, name)
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
    d.mkdir(parents=True, exist_ok=False)
    p.write_bytes(codex.canonical())
    return d


def _normalise(cid: str) -> str:
    return cid if cid.startswith("sha256:") else "sha256:" + cid


def _held(root: Path) -> list[tuple[str, Codex]]:
    out = []
    for d in sorted(root.iterdir()) if root.exists() else []:
        p = d / DRAG / CODEX_FILE
        if p.is_file():
            out.append((d.name, cx.loads(p.read_bytes())))
    return out


def resolve(root: Path, ref: str) -> tuple[str, Codex]:
    """07 §7.7's four forms, in this order: a full collection id with or
    without its prefix; a unique prefix of at least 12 hex characters; a
    local name; a lineage id, which resolves to the leading daughter held
    here. Ambiguity is exit 2, naming the candidates.

    Returns `(name, codex)` — the caller almost always needs both, since
    the name is where the drag and the state directory live.
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


# ---- the four ways to make one -------------------------------------------

def create(root: Path, codex: Codex, *, name: str | None = None) -> Codex:
    """A root: no lineage fields, and `space.epoch` is "0"."""
    if codex.relation != "root":
        raise CodexError("create makes a root; use derive/continue/replace "
                         "for the others")
    write(root, codex, name=name)
    return codex


def create_from_recipe(root: Path, plugin: str, recipe: dict[str, Any], *,
                       platform_image: str, credits: tuple = (),
                       name: str | None = None) -> Codex:
    """`hold collection create <plugin> --recipe FILE` (06 §6.6, 07 §7.3).

    Re-running with the same recipe prints the same id and exits 0: the hash
    is the identity, and writing it twice is nothing.
    """
    from . import template
    return create(root, template.from_recipe(plugin, recipe,
                                             platform_image=platform_image,
                                             credits=credits), name=name)


def derive(root: Path, mother_id: str, *,
           space: Space | None = None,
           space_from: Codex | None = None,
           layers: Iterable[Layer] | None = None,
           update: Update | None = None,
           work: Work | None = None,
           credits: tuple[Credit, ...] | None = None,
           descriptive: Descriptive | None = None) -> Codex:
    """A sister: her mother's corpus, and at most `space`, `layers`, `update`
    and `work` changed (02 §2.7).

    `cleaning_version` is never copied and never changed — changing it would
    re-derive the corpus she is defined to inherit. `space_from` copies
    another Codex's `space` and `layers` byte for byte and is refused when
    that Codex's `cleaning_version` differs from the mother's, because the
    resulting `space_id` could not then equal the source's.

    Credits follow the space (02 §2.7): her mother's when `space_id` matches,
    the `space_from` Codex's when that supplied the space, and otherwise
    whatever `credits` names — `()` when it names nothing. The caller
    expands archive ids into `Credit`s from the platform's registry, exactly
    as `create` does; nothing here composes one by hand.
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

    sister = _replace(
        mother,
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

    same_space = sister.space_id == mother.space_id
    if credits is not None:
        if same_space:
            raise CodexError("derive: a sister in her mother's space carries "
                             "her mother's credits; --archives is refused here")
        if space_from is not None:
            raise CodexError("derive: --space-from brings that Codex's credits; "
                             "--archives is refused here")
        sister = _replace(sister, credits=tuple(credits))
    elif space_from is not None:
        sister = _replace(sister, credits=space_from.credits)
    elif not same_space:
        sister = _replace(sister, credits=())

    write(root, sister, mother)
    return sister


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
    write(root, daughter, mother)
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
        descriptive=descriptive if descriptive is not None else mother.descriptive,
        toolchain=tc,
        space=_replace(base, epoch=cx.corrective_epoch(mother.collection_id)),
        replaces=Replaces(mother.collection_id, defect),
        derived_from=None, space_from=None, continues=None,
        continues_through=None, origin_reachable_until=None,
        credits=(),
    )
    write(root, corrective, mother)
    return corrective


def show(root: Path, ref: str) -> dict[str, Any]:
    """What `hold collection show --json` prints (09 §9.3). The parts that
    need the drag on disk — snapshots, holdings, trust_level — arrive with
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
        "credits": [c.archive for c in codex.credits],
        "refused": None,
        "codex": json.loads(codex.canonical()),
    }
