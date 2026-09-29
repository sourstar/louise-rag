"""DepositSource — the plugin of every deposit collection. docs/06 §6.7,
docs/02 §2.9; the design is docs/notes/deposit-collections.md.

Its origin is the client. What arrives over HTTP (07 §7.4) or from `hold
deposit add` is written byte-exact under the collection's `origin/`, and
that directory is the corpus `discover` lists and `fetch` reads:

    origin/<native_id>/<key>            the deposited bytes, exactly
    origin/<native_id>/.deposit.json    {key, deposited_at, url}

One directory per deposit, named by its node-wide deposit number, so the
file inside keeps the name the client gave it — the key LocalAGI lists and
deletes by — and a key can never collide with the sidecar, because a key
never starts with a dot (02 §2.9).

Nothing here writes. `app/hold/deposit.py` places the bytes and runs the
worker's own stages over them; this module only reads what is there, like
any plugin reads its corpus.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from app import chunking

from ..base import (PROTOCOL_VERSION, CanonicalDoc, DiscoverState, DocRef,
                    Erasure, LicenseClass, Modality, Mutability, PrivacyClass,
                    RawDoc, UnindexableDocument)

SIDECAR = ".deposit.json"

#: 02 §2.9: 1–255 bytes of UTF-8, no `/`, no NUL, no leading dot.
KEY_MAX_BYTES = 255

#: 06 §6.7's accepted forms, by the key's extension. PDF joins in step 15b.
TEXT_SUFFIXES = (".txt", ".md")

_HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")


class DepositError(ValueError):
    """A deposit that is refused when it arrives, and never stored."""


def check_key(key: str) -> str:
    if not isinstance(key, str) or not key:
        raise DepositError("a deposit key is 1–255 bytes of UTF-8 (02 §2.9)")
    if len(key.encode("utf-8")) > KEY_MAX_BYTES:
        raise DepositError(f"a deposit key is at most {KEY_MAX_BYTES} bytes "
                           f"of UTF-8 (02 §2.9)")
    if "/" in key or "\x00" in key or key.startswith("."):
        raise DepositError(f"{key!r}: a deposit key has no `/`, no NUL and no "
                           f"leading dot (02 §2.9)")
    return key


def origin_hash(data: bytes) -> str:
    """09 §9.5's `origin_hash`: BLAKE2b-128 of the bytes as deposited."""
    return hashlib.blake2b(data, digest_size=16).hexdigest()


def sections_of_text(text: str) -> list[tuple[str, str]]:
    """Markdown headings cut sections; text before the first heading, or a
    text with none, is one untitled section (06 §6.7). A heading's own line
    is its section's title and not part of its body."""
    out: list[tuple[str, list[str]]] = [("", [])]
    for line in text.splitlines():
        m = _HEADING.match(line)
        if m:
            out.append((m.group(2).strip(), []))
        else:
            out[-1][1].append(line)
    sections = [(title, "\n".join(body).strip()) for title, body in out]
    return [(t, b) for t, b in sections if t or b]


def decode_text(key: str, data: bytes) -> str:
    """Text and Markdown are UTF-8, and nothing is guessed: bytes that are
    not are refused (06 §6.7). A leading byte-order mark is not text."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as e:
        raise DepositError(f"{key}: not valid UTF-8 at byte {e.start}; a text "
                           f"deposit is UTF-8 and is never guessed at") from e
    return text.removeprefix("﻿")


def accepted(key: str) -> bool:
    return Path(key).suffix.lower() in TEXT_SUFFIXES


def fixed_layer(ctx: int) -> dict:
    """06 §6.7: one `fixed` layer, its window sized to the encoder's
    context. A pure function of `ctx`, because the layer's params are
    inside `space_id` (09 §9.3): two nodes creating a deposit collection on
    the same encoder must cut the same chunks."""
    tokens = max(64, (int(ctx) * 7) // 8)
    return {"chunker": {"id": chunking.FIXED,
                        "version": chunking.chunk_version_for(chunking.FIXED),
                        "params": {"tokens": tokens, "overlap": tokens // 8}},
            "rule": "default"}


class DepositSource:
    source_id = "deposit"
    protocol_version = PROTOCOL_VERSION
    PARSER_VERSION = 1
    # Nothing is taken from a third party by the plugin itself; a URL
    # source is 04 §4.4's and has its own rule (step 15e).
    license_class = LicenseClass.GREEN
    corpus_provider = "the depositor"
    corpus_attribution = "Deposited by a client of this node."
    corpus_license = None
    # 02 §2.9: fixed. A recipe cannot loosen privacy (template's ladder),
    # and `hold collection create deposit` writes no recipe at all.
    privacy_class = PrivacyClass.NEVER_SHARE
    erasure = Erasure.ERASABLE
    modality = Modality.TEXT
    chunking_profile = chunking.FIXED
    mutability = Mutability.IMMUTABLE      # replaced whole, never edited
    native_id_order = "numeric"            # the node-wide deposit number
    # The window depends on the encoder's context, so `create` writes the
    # layer (`fixed_layer`); this is the shape with the default's context.
    default_layers: list = [fixed_layer(512)]
    # Nothing is ever cut into shards (02 §2.9: no drag, no snapshot), but
    # the Codex carries every field (09 §9.3).
    default_slicing: dict = {"keys": [], "unknown": "all",
                             "shard_records": 30000, "order": "numeric"}
    default_facets: list = []

    # -- the corpus ----------------------------------------------------------

    def discover(self, state: DiscoverState):
        root = Path(state.corpus_root)
        if not root.is_dir():
            return
        dirs = sorted((d for d in root.iterdir()
                       if d.is_dir() and d.name.isdigit()), key=lambda d: int(d.name))
        for d in dirs:
            meta = read_sidecar(d)
            if meta is None:
                continue                   # a deposit being written, or erased
            f = d / meta["key"]
            if not f.is_file():
                continue
            st = f.stat()
            yield DocRef(source_id=self.source_id, native_id=d.name,
                         locator=str(f), mtime=int(st.st_mtime), size=st.st_size)

    def fetch(self, ref: DocRef) -> RawDoc:
        p = Path(ref.locator)
        try:
            data = p.read_bytes()
        except OSError as e:
            raise UnindexableDocument(f"{p.name}: {type(e).__name__}: {e}") from e
        return RawDoc(ref=ref, raw_hash=origin_hash(data), path=p, data=data)

    def parse(self, raw: RawDoc) -> CanonicalDoc:
        p = Path(raw.ref.locator)
        meta = read_sidecar(p.parent) or {}
        key = meta.get("key") or p.name
        data = raw.data if raw.data is not None else p.read_bytes()
        if not accepted(key):
            raise UnindexableDocument(f"{key}: not a form a deposit takes (06 §6.7)")
        try:
            sections = sections_of_text(decode_text(key, data))
        except DepositError as e:
            raise UnindexableDocument(str(e)) from e
        extra = {"key": key, "deposited_at": meta.get("deposited_at")}
        if meta.get("url"):
            extra["url"] = meta["url"]
        return CanonicalDoc(
            uid=f"{self.source_id}:{raw.ref.native_id}", title=key, creators=[],
            language="und", date=None, subjects=[], keys=[], extra=extra,
            sections=sections, raw_hash=raw.raw_hash)


def read_sidecar(d: Path) -> dict | None:
    try:
        meta = json.loads((Path(d) / SIDECAR).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return meta if isinstance(meta, dict) and meta.get("key") else None
