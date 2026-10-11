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

#: 06 §6.7's accepted forms, by the key's extension.
TEXT_SUFFIXES = (".txt", ".md")
PDF_SUFFIX = ".pdf"

#: `extra`'s keys, the SAME for every deposit whatever its form: a record's
#: `extra` keys are fixed for a collection's life (09 §9.5), and a text
#: deposit beside a PDF one must not be two shapes. `url` is a source's
#: (06 §6.7); the `pdf_*` keys are what a PDF says about itself (promise
#: 9), null for a text deposit.
EXTRA_KEYS = ("key", "deposited_at", "url", "pdf_title", "pdf_author",
              "pdf_subject", "pdf_keywords", "pdf_creator", "pdf_producer",
              "pdf_creation_date", "pdf_mod_date", "pdf_pages")
_PDF_META = {"Title": "pdf_title", "Author": "pdf_author", "Subject": "pdf_subject",
             "Keywords": "pdf_keywords", "Creator": "pdf_creator",
             "Producer": "pdf_producer", "CreationDate": "pdf_creation_date",
             "ModDate": "pdf_mod_date"}
_PDF_DATE = re.compile(r"^D:(\d{4})(\d{2})?(\d{2})?")

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
    return Path(key).suffix.lower() in (*TEXT_SUFFIXES, PDF_SUFFIX)


def is_pdf(key: str) -> bool:
    return Path(key).suffix.lower() == PDF_SUFFIX


def pdf_content(key: str, data: bytes) -> tuple[list[tuple[str, str]], dict]:
    """A PDF's sections — one per page that has text, titled by its page
    number — and what it says about itself. A PDF with no text at all (a
    scan: there is no OCR) is refused (06 §6.7)."""
    from . import pdf
    try:
        got = pdf.extract(data)
    except pdf.PdfError as e:
        raise DepositError(f"{key}: {e}") from e
    sections = []
    for n, raw in enumerate(got["pages"], 1):
        text = "\n".join(line.rstrip() for line in
                         raw.replace("\r\n", "\n").replace("\r", "\n").split("\n")).strip()
        if text:
            sections.append((f"page {n}", text))
    if not sections:
        raise DepositError(f"{key}: the PDF has no text — a scan, perhaps; there "
                           f"is no OCR, so it is refused rather than stored "
                           f"unsearchable (06 §6.7)")
    meta = {_PDF_META[k]: (v or None) for k, v in got["meta"].items() if k in _PDF_META}
    meta["pdf_pages"] = len(got["pages"])
    return sections, meta


def check_content(key: str, data: bytes) -> None:
    """What `add` checks before storing anything: the form is taken and the
    bytes are what it says they are."""
    check_key(key)
    if not accepted(key):
        raise DepositError(f"{key}: a deposit is .txt, .md or .pdf (06 §6.7)")
    if is_pdf(key):
        pdf_content(key, data)
    else:
        decode_text(key, data)


def _pdf_date(value: str | None) -> str | None:
    """`D:YYYYMMDD…` as far as it goes: `YYYY`, `YYYY-MM` or `YYYY-MM-DD`."""
    m = _PDF_DATE.match(value or "")
    return "-".join(g for g in m.groups() if g) if m else None


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
    #: 2: PDF (step 16b), and `extra` carries every key of EXTRA_KEYS.
    PARSER_VERSION = 2
    # Nothing is taken from a third party by the plugin itself; a URL
    # source is 04 §4.4's and has its own rule (step 16e).
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
    # Nothing is ever cut into shards (02 §2.9: no SHARD, no snapshot), but
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
        extra = dict.fromkeys(EXTRA_KEYS)
        extra.update(key=key, deposited_at=meta.get("deposited_at"),
                     url=meta.get("url") or None)
        title, creators, date, subjects = key, [], None, []
        try:
            if is_pdf(key):
                sections, pdf_meta = pdf_content(key, data)
                extra.update(pdf_meta)
                title = extra["pdf_title"] or key
                creators = [extra["pdf_author"]] if extra["pdf_author"] else []
                date = _pdf_date(extra["pdf_creation_date"])
                subjects = [k.strip() for k in re.split(r"[;,]", extra["pdf_keywords"] or "")
                            if k.strip()]
            else:
                sections = sections_of_text(decode_text(key, data))
        except DepositError as e:
            raise UnindexableDocument(str(e)) from e
        return CanonicalDoc(
            uid=f"{self.source_id}:{raw.ref.native_id}", title=title,
            creators=creators, language="und", date=date, subjects=subjects,
            keys=[], extra=extra, sections=sections, raw_hash=raw.raw_hash)


def read_sidecar(d: Path) -> dict | None:
    try:
        meta = json.loads((Path(d) / SIDECAR).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return meta if isinstance(meta, dict) and meta.get("key") else None
