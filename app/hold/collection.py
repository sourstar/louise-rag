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
from dataclasses import replace as _replace
from pathlib import Path
from typing import Any, Iterable

from . import codex as cx
from .codex import (Codex, CodexError, Credit, Defect, Descriptive, Layer,
                    License, Origin, Parser, Replaces, Space, Update, Work)


def _dir(root: Path, collection_id: str) -> Path:
    return root / collection_id.replace("sha256:", "")


def write(root: Path, codex: Codex, mother: Codex | None = None) -> Path:
    cx.validate(codex, mother)
    d = _dir(root, codex.collection_id)
    if (d / "codex.json").exists():
        return d                      # idempotent: same bytes, same id, nothing to do
    d.mkdir(parents=True, exist_ok=False)
    (d / "codex.json").write_bytes(codex.canonical())
    return d


def read(root: Path, collection_id: str) -> Codex:
    p = _dir(root, collection_id) / "codex.json"
    if not p.exists():
        raise CodexError(f"no collection {collection_id} under {root}")
    codex = cx.loads(p.read_bytes())
    if codex.collection_id != _normalise(collection_id):
        raise CodexError(f"{p}: bytes hash to {codex.collection_id}, "
                         f"directory says {collection_id}")
    return codex


def _normalise(cid: str) -> str:
    return cid if cid.startswith("sha256:") else "sha256:" + cid


def resolver(root: Path):
    def _resolve(cid: str) -> Codex | None:
        try:
            return read(root, cid)
        except (CodexError, ValueError):
            return None
    return _resolve


def list_all(root: Path) -> list[Codex]:
    out = []
    for d in sorted(root.iterdir()) if root.exists() else []:
        if (d / "codex.json").exists():
            out.append(read(root, d.name))
    return out


# ---- the four ways to make one -------------------------------------------

def create(root: Path, codex: Codex) -> Codex:
    """A root: no lineage fields, and `space.epoch` is "0"."""
    if codex.relation != "root":
        raise CodexError("create makes a root; use derive/continue/replace "
                         "for the others")
    write(root, codex)
    return codex


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


def show(root: Path, collection_id: str) -> dict[str, Any]:
    """What `hold collection show --json` prints (09 §9.3). The parts that
    need the drag on disk — snapshots, holdings, trust_level — arrive with
    the members they read; `refused` is the reason the collection is not
    served when refuse-to-start applies to it (07 §7.8)."""
    codex = read(root, collection_id)
    return {
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
