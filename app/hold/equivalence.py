"""Which platform images may build, snapshot and flag a collection (03 §3.4).

The Codex freezes a REFERENCE toolchain by digest,
`toolchain.platform_image`. Another image is EQUIVALENT for one collection
once this peer's own `records-vs-origin` AND `chunks` have both passed under
it: "a deterministic proof that the new image is equivalent for this
collection; the proof travels as an attestation whose `platform_image` is
the image run".

Two consequences, both enforced from here:

  - A `records-vs-origin` failure under any other image "is an equivalence
    failure of that image for this collection, not a defect in the
    collection ... it flags nothing" (`app/copy_state.py` asks
    `reference_images`).
  - "A peer whose platform image is neither ... does not build its records,
    create its snapshots or contribute vectors to it until its own
    `records-vs-origin` and `chunks` pass under the image it runs"
    (`require_reference`, called by `hold records build` and `hold snapshot
    create`).

Found live 2026-09-21. `hold check` wrote the CODEX's image into every
attestation whatever image ran it, so a check run after a deploy that had
moved pubmed's parser from 4 to 8 reported the collection's own records as
a failure against its own toolchain and quarantined it. The same day a
records build ran for gutenberg under an image its Codex does not name —
the Codex's own had been pruned — and nothing refused it.
"""
from __future__ import annotations

import os

from .codex import Codex, CodexError, digest

#: The SHA-256 of empty bytes: a placeholder that names no build (07 §7.3).
_PLACEHOLDER = digest(b"")


class NotEquivalent(CodexError):
    """This process's image may not do this to this collection (03 §3.4)."""


def running_image() -> str | None:
    """The image this process runs in, as the deploy states it.

    A container cannot see its own image id, so the deploy hands it over as
    LOUISE_PLATFORM_IMAGE and checks it against the running image (07 §7.3).
    None when it is unstated, or the placeholder: a process that cannot name
    its image can neither attest (09 §9.8 requires "the image the verifier
    ran") nor claim to be the reference."""
    value = (os.environ.get("LOUISE_PLATFORM_IMAGE") or "").strip()
    return value if value and value != _PLACEHOLDER else None


def reference_images(bodies, key: str | None, codex: Codex) -> set[str]:
    """The images whose verdicts count for this collection on this peer: the
    Codex's own, and each image under which this peer's own
    `records-vs-origin` and `chunks` have both passed. `bodies` is
    `copy_state.attestations(...)`; `key` this peer's."""
    images = {codex.toolchain.platform_image}
    if key is None:
        return images
    origin = bodies.by_image(key, "records-vs-origin")
    chunks = bodies.by_image(key, "chunks")
    # or `image-equivalence` in place of `records-vs-origin` (amended
    # 2026-10-07): the same lines as the reference image re-parses from the
    # same origin bytes — what an origin that moved since the build leaves
    # as the only proof (app/hold/check.py `check_image_equivalence`)
    same_parse = bodies.by_image(key, "image-equivalence")
    return images | {image for image, (passed, _) in list(origin.items()) + list(same_parse.items())
                     if passed and chunks.get(image, (False, None))[0]}


def require_reference(root, name: str, codex: Codex, *, use: str) -> str:
    """The image this process runs in, when it may `use` this collection's
    records; `NotEquivalent` otherwise, naming what would clear it."""
    from app import collections as collections_mod, copy_state

    image = running_image()
    if image is None:
        raise NotEquivalent(
            f"{name}: this process does not know the image it runs in "
            f"(LOUISE_PLATFORM_IMAGE, which the deploy sets), so it cannot "
            f"show that it is {codex.toolchain.platform_image[:19]}…, the "
            f"reference its Codex names, or an image equivalent to it (03 §3.4). "
            f"Nothing to {use}")
    key = copy_state.own_key()
    allowed = reference_images(copy_state.attestations(root, name, codex), key, codex)
    if image in allowed:
        return image
    plugin = collections_mod.PLUGIN_REGISTRY.get(codex.plugin)
    running_parser = getattr(plugin, "PARSER_VERSION", None)
    parser_note = (f" The Codex names {codex.parser.id} parser "
                   f"{codex.parser.version} and this image runs parser "
                   f"{running_parser}, so it cannot become equivalent: build under "
                   f"the Codex's own image, or create a new collection."
                   if running_parser is not None and running_parser != codex.parser.version
                   else f" Run `louise hold check {name} --checks "
                        f"records-vs-origin,chunks --origin DIR` under this image; "
                        f"passing both makes it equivalent for {name}. Where the origin "
                        f"moved since the build, `--checks image-equivalence,chunks "
                        f"--against FILE`, FILE written by tools/reparse_lines.py under "
                        f"the Codex's own image.")
    raise NotEquivalent(
        f"{name}: this image ({image[:19]}…) is neither the reference its Codex "
        f"names ({codex.toolchain.platform_image[:19]}…) nor proven equivalent "
        f"to it on this peer, and 03 §3.4 says such a peer \"does not build its "
        f"records, create its snapshots or contribute vectors to it\". Not "
        f"allowed: {use}.{parser_note}")
