"""hold.collection — create a Codex and read it back. docs/07-commands.md §7.3.

    hold collection create   → a root Codex
    hold collection derive   → a sister (same corpus, new space)
    hold collection continue → a daughter (new toolchain, same space)
    hold collection replace  → a corrective sister (with its defect)
    hold collection show     → what a Codex says, and the ids derived from it

On disk a collection is a directory named by its id; `codex.json` inside is
the canonical bytes and nothing else, so `sha256(codex.json)` is the
directory's name. There is no pretty form: the file *is* the identity.
"""
from __future__ import annotations

import json
from dataclasses import replace as _replace
from pathlib import Path
from typing import Any

from . import codex as cx
from .codex import Codex, CodexError, Defect, Space


def _dir(root: Path, collection_id: str) -> Path:
    return root / collection_id.replace("sha256:", "")


def write(root: Path, codex: Codex, parent: Codex | None = None) -> Path:
    cx.validate(codex, parent)
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
    data = p.read_bytes()
    codex = cx.loads(data)
    if codex.collection_id != _normalise(collection_id):
        raise CodexError(f"{p}: bytes hash to {codex.collection_id}, directory says {collection_id}")
    return codex


def _normalise(cid: str) -> str:
    return cid if cid.startswith("sha256:") else "sha256:" + cid


def resolver(root: Path):
    def _resolve(cid: str) -> Codex | None:
        try:
            return read(root, cid)
        except CodexError:
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
    """A root: no lineage fields."""
    if codex.relation != "root":
        raise CodexError("create makes a root; use derive/continue/replace for the others")
    write(root, codex)
    return codex


def derive(root: Path, parent_id: str, *, encoder_image: str | None = None,
           layers=None, cleaning_version: int | None = None, space: Space | None = None) -> Codex:
    """A sister: the parent's corpus, a new space. At least one of the space
    inputs must change, or it is the same collection."""
    parent = read(root, parent_id)
    tc = parent.toolchain if encoder_image is None else _replace(parent.toolchain, encoder_image=encoder_image)
    sister = _replace(parent,
                      toolchain=tc,
                      layers=parent.layers if layers is None else tuple(layers),
                      cleaning_version=parent.cleaning_version if cleaning_version is None else cleaning_version,
                      space=(space or parent.space),
                      derived_from=parent.collection_id,
                      continues=None, from_snapshot=None, origin_reachable_until=None,
                      replaces=None, defect=None)
    if sister.space_id == parent.space_id:
        raise CodexError("derive: nothing about the space changed — this is the same collection")
    write(root, sister, parent)
    return sister


def continue_(root: Path, parent_id: str, *, platform_image: str, from_snapshot: str,
              origin: dict[str, Any] | None = None, parser_version: int | None = None,
              license_terms_sha256: str | None = None,
              origin_reachable_until: str | None = None) -> Codex:
    """A daughter: the outside world changed; the toolchain adapts; the space
    stays. The encoder image is inherited and cannot be given here."""
    parent = read(root, parent_id)
    daughter = _replace(parent,
                        toolchain=_replace(parent.toolchain, platform_image=platform_image),
                        origin=parent.origin if origin is None else origin,
                        parser_version=parent.parser_version if parser_version is None else parser_version,
                        license_terms_sha256=parent.license_terms_sha256 if license_terms_sha256 is None
                        else license_terms_sha256,
                        continues=parent.collection_id, from_snapshot=from_snapshot,
                        origin_reachable_until=origin_reachable_until,
                        derived_from=None, replaces=None, defect=None)
    write(root, daughter, parent)
    return daughter


def replace(root: Path, parent_id: str, *, platform_image: str | None, encoder_image: str | None,
            defect: Defect) -> Codex:
    """A corrective sister: the parent's origin, a corrected toolchain, and a
    fresh space by construction. The defect travels with it."""
    parent = read(root, parent_id)
    tc = parent.toolchain
    if platform_image:
        tc = _replace(tc, platform_image=platform_image)
    if encoder_image:
        tc = _replace(tc, encoder_image=encoder_image)
    corrective = _replace(parent,
                          toolchain=tc,
                          space=_replace(parent.space, epoch=cx.corrective_epoch(parent.collection_id)),
                          replaces=parent.collection_id, defect=defect,
                          derived_from=None, continues=None, from_snapshot=None,
                          origin_reachable_until=None)
    write(root, corrective, parent)
    return corrective


def show(root: Path, collection_id: str) -> dict[str, Any]:
    codex = read(root, collection_id)
    return {
        "collection_id": codex.collection_id,
        "lineage_id": cx.lineage_id(codex, resolver(root)),
        "relation": codex.relation,
        "space_id": codex.space_id,
        "publication": codex.publication,
        "codex": json.loads(codex.canonical()),
    }
