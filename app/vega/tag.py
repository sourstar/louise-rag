"""The tag, and the two digests a receipt binds (docs/notes/vega.md §2).

ONE MODULE ON BOTH SIDES. Louise tags with it (app/vega/client.py) and VeGa's
wrapper parses with it (deploy/vega/vega_wrapper.py, which carries `app/vega/`
and `app/hold/codex.py` into its package):
there is one definition of the bytes, never two kept equal.

THE TAG. Every input to a `vega-` model is one string:

    MARK + canonical JSON (09 §9.1) of
        {"form": "text", "id": <one-time id>, "text": <the text>}
     or {"form": "pair", "id": <one-time id>, "pair": [<first>, <second>]}

and, where the input is to be cut to the space's window, `"window": <tokens>`
beside them (docs/notes/char-cut-truncation.md; operator, 2026-10-18): the
encoder embeds at most that many tokens of the input, cut by the model's own
tokenizer as 09 §9.3 cuts a chunk to `ctx`. The platform holds no tokenizer,
so it no longer cuts by characters; a tag without a window is embedded whole.

`MARK` is two record separators around a name and a version, which no text
carries by accident and which canonical JSON never contains raw (it escapes
control characters). The id is 128 random bits as 32 lowercase hexadecimal
digits, fresh for every input. A tag that is not exactly this — another key, a
float, a duplicate key, any byte the canonical form would write otherwise — is
refused: the wrapper never guesses what was meant.

THE INPUT DIGEST is `H(canonical JSON of the tag's object without its id)`:
what was asked, independent of who asked — the window included, so a receipt
binds the text and the cut it was embedded under. THE VECTOR DIGEST is SHA-256 over
the vector as little-endian float32, concatenated — the bytes the backend
produced, written `sha256:<hex>` like every identity (09 §9.1).

WIRE FORMAT PENDING 09 §9.12's amendment (vega.md §4): approved in principle
by the operator, the bytes get their own review.
"""
from __future__ import annotations

import json
import re
import secrets
import struct
from typing import Any, Sequence

from ..hold.codex import CodexError, canonical_json, digest

MARK = "\x1eVEGA/1\x1e"
TEXT, PAIR = "text", "pair"
FORMS = (TEXT, PAIR)
#: the optional key of either form: the tokens the encoder embeds at most
WINDOW = "window"
#: how VeGa's wrapper begins a refusal of the INPUT itself (it cannot be cut at
#: its window exactly): Louise reads it as the input's error, not a deferral
INPUT_MARK = "VeGa: this input"
_ID = re.compile(r"[0-9a-f]{32}")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")

#: The phase-3 form of a pair, what `louise-medcpt-article-ov` decodes
#: (docs/notes/model-pool-over-localai.md §13). The wrapper forwards a tagged pair
#: in it; deploy/localai/backends/louise_backend.py holds the same marker,
#: and a test holds the two equal.
PAIR_MARK = "\x1eLOUISE-PAIR\x1e"

#: THE ONLY TWO float32 VALUES whose JSON, as LocalAI writes it (Go's
#: shortest form), Python reads back as another float32: the decimal lands on
#: the double that is exactly halfway between two floats, and narrowing rounds
#: it to the even one. Exhaustive over every finite float32
#: (tools/vega_f32_json.go, 2026-10-08, go1.26.8): these two and no other.
#: Keyed by the double Python reads; the value is the float32 Go wrote.
_HALFWAY = {float("7.038531e-26"): 0x15AE43FD, float("-7.038531e-26"): 0x95AE43FD}

Item = str | tuple[str, str]


class TagError(ValueError):
    """An input is not a valid tag, or content cannot be tagged. The message
    says which rule, never the content."""


def new_id() -> str:
    return secrets.token_hex(16)


def _key(form: str) -> str:
    if form not in FORMS:
        raise TagError(f"the form is one of {', '.join(FORMS)}, not {form!r}")
    return TEXT if form == TEXT else PAIR


def _content(form: str, content: Any) -> str | list[str]:
    if form == TEXT:
        if not isinstance(content, str):
            raise TagError("a text input is a string")
        return content
    if (not isinstance(content, (tuple, list)) or len(content) != 2
            or not all(isinstance(s, str) for s in content)):
        raise TagError("a pair input is two strings, [first, second]")
    return [content[0], content[1]]


def _canonical(obj: dict[str, Any]) -> bytes:
    try:
        return canonical_json(obj)
    except (CodexError, UnicodeEncodeError, ValueError) as e:
        # an unpaired surrogate cannot be written as UTF-8 (09 §9.1 refuses it)
        raise TagError(f"the content cannot be written as canonical JSON: "
                       f"{type(e).__name__}") from None


def _window(window: Any) -> int | None:
    if window is None:
        return None
    if isinstance(window, bool) or not isinstance(window, int) or window < 1:
        raise TagError("the window is a whole number of tokens, at least 1")
    return window


def _object(form: str, content: Item | list[str], window: int | None) -> dict[str, Any]:
    obj: dict[str, Any] = {"form": form, _key(form): _content(form, content)}
    if _window(window) is not None:
        obj[WINDOW] = window
    return obj


def tag(form: str, content: Item | list[str], *, id: str | None = None,
        window: int | None = None) -> tuple[str, str]:
    """`(id, the tagged string)` for one input, cut to `window` tokens by the
    encoder when one is given. `id` is for tests; Louise always takes a fresh
    one."""
    tid = new_id() if id is None else id
    if not _ID.fullmatch(tid):
        raise TagError("the id is 32 lowercase hexadecimal digits")
    body = _canonical({**_object(form, content, window), "id": tid})
    return tid, MARK + body.decode("utf-8")


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in pairs:
        if k in out:
            raise TagError(f"the tag repeats the key {k!r}")
        out[k] = v
    return out


def _no_float(text: str) -> Any:
    raise TagError("the tag carries a number that is not an integer")


def parse(s: str) -> tuple[str, str, Item]:
    """`(id, form, content)` of a tagged input — a pair as a tuple — or a
    TagError naming the rule it breaks. The window, if any: `parse_window`."""
    tid, form, content, _window_ = parse_window(s)
    return tid, form, content


def parse_window(s: str) -> tuple[str, str, Item, int | None]:
    """`(id, form, content, window or None)` of a tagged input."""
    if not isinstance(s, str):
        raise TagError("an input is a string")
    if not s.startswith(MARK):
        if MARK in s:
            raise TagError("the VeGa marker is inside the input, not at its start")
        raise TagError("an input without a VeGa tag: every input to a vega- model is "
                       "tagged (docs/notes/vega.md)")
    body = s[len(MARK):]
    try:
        obj = json.loads(body, object_pairs_hook=_no_duplicates, parse_float=_no_float,
                         parse_constant=_no_float)
    except TagError:
        raise
    except ValueError as e:
        raise TagError(f"the tag's JSON does not parse: {e.msg} at {e.pos}") from None
    if not isinstance(obj, dict):
        raise TagError("the tag's JSON is an object")
    form = obj.get("form")
    if form not in FORMS:
        raise TagError(f"the tag's form is one of {', '.join(FORMS)}")
    key = _key(form)
    if set(obj) - {WINDOW} != {"form", "id", key}:
        raise TagError(f"a {form} tag has exactly the keys form, id and {key}, and "
                       f"{WINDOW} where it is cut")
    tid = obj["id"]
    if not isinstance(tid, str) or not _ID.fullmatch(tid):
        raise TagError("the tag's id is 32 lowercase hexadecimal digits")
    content = _content(form, obj[key])
    window = _window(obj.get(WINDOW))
    if _canonical(obj).decode("utf-8") != body:
        raise TagError("the tag is not in canonical form (09 §9.1)")
    return tid, form, (content if form == TEXT else (content[0], content[1])), window


def input_digest(form: str, content: Item | list[str], window: int | None = None) -> str:
    return digest(_canonical(_object(form, content, window)))


def f32_bytes(values: Sequence[float] | bytes) -> bytes:
    """The vector as little-endian float32, concatenated. Bytes pass as they
    are (LocalAI's `encoding_format: base64`); numbers are narrowed to float32,
    with the two values Python reads off by one float repaired (`_HALFWAY`).
    A number outside float32's range is a ValueError."""
    if isinstance(values, (bytes, bytearray, memoryview)):
        raw = bytes(values)
        if len(raw) % 4:
            raise ValueError(f"{len(raw)} bytes are not a whole number of float32")
        return raw
    try:
        raw = struct.pack(f"<{len(values)}f", *values)
    except (OverflowError, struct.error) as e:
        raise ValueError(f"a vector component is not a float32: {e}") from None
    if _HALFWAY.keys().isdisjoint(values):
        return raw
    out = bytearray(raw)
    for i, v in enumerate(values):
        bits = _HALFWAY.get(v)
        if bits is not None:
            out[4 * i:4 * i + 4] = struct.pack("<I", bits)
    return bytes(out)


def vector_digest(values: Sequence[float] | bytes) -> str:
    return digest(f32_bytes(values))


def backend_pair(first: str, second: str) -> str:
    """A pair as `louise-medcpt-article-ov` reads it (louise_backend.decode_pair)."""
    return PAIR_MARK + json.dumps([first, second], ensure_ascii=False)
