"""A tokenizer text pair through LocalAI (docs/notes/input-forms.md, RAGE).

Louise asks for a pair space's vectors with the OpenAI-style embeddings body
plus one field, `input_pairs: [[first, second], …]`, in place of `input`. The
front accepts it only for a model whose YAML declares the option `pairs:true`
— an option LocalAI sends in the load call, so it is in the run's engine
options and its `run_id` — and refuses it for any other, never flattening a
pair into one string.

LocalAI hands a backend ONE string per embedding (`Embedding`'s
`Embeddings`), so each pair travels as one marked string, MARK followed by
the JSON array of its two strings, which the pair backend decodes
(deploy/localai/backends/louise_backend.py holds the same MARK; a test holds
the two together). A plain input carrying MARK is refused: it would reach a
pair backend as a pair nobody asked for.
"""
from __future__ import annotations

import json
from typing import Any

from .wire import Refused

#: record separators around a name no text carries by accident
MARK = "\x1eLOUISE-PAIR\x1e"
#: the option a model's YAML declares to be a pair model
OPTION = "pairs:true"
FIELD = "input_pairs"


def declares_pairs(options: Any) -> bool:
    """Whether a model's `options:` (as its YAML or LocalAI's load call lists
    them) declare it a pair model."""
    return isinstance(options, list) and any(
        isinstance(o, str) and o.replace(" ", "") == OPTION for o in options)


def encode(first: str, second: str) -> str:
    return MARK + json.dumps([first, second], ensure_ascii=False)


def _texts(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [v for v in value if isinstance(v, str)]
    return []


def translate(body: dict[str, Any], model_id: str, pairs_model: bool | None,
              why_not: str = "") -> bool:
    """Rewrite an embeddings body in place for LocalAI: `input_pairs` becomes
    `input` of marked strings. True when it did. `pairs_model` is whether
    the model declares `pairs:true` (None: unknown, `why_not` says why).
    Refused (400) for a body that cannot be passed on as asked."""
    if any(MARK in t for t in _texts(body.get("input"))):
        raise Refused(400, f"an input carries the pair marker {MARK!r}, which only the "
                           f"front writes; a pair is asked for with {FIELD}")
    if FIELD not in body:
        return False
    if "input" in body:
        raise Refused(400, f"the body gives both input and {FIELD}: one or the other")
    if not pairs_model:
        reason = why_not or f"its model YAML does not declare the option {OPTION}"
        raise Refused(400, f"{model_id} does not encode text pairs: {reason}. A pair is "
                           f"never flattened into one string (docs/notes/input-forms.md)")
    pairs = body.pop(FIELD)
    if (not isinstance(pairs, list) or not pairs
            or not all(isinstance(p, list) and len(p) == 2
                       and all(isinstance(s, str) for s in p) for p in pairs)):
        raise Refused(400, f"{FIELD} is a non-empty list of [first, second] string pairs")
    if any(MARK in s for p in pairs for s in p):
        raise Refused(400, f"a pair carries the pair marker {MARK!r}")
    body["input"] = [encode(a, b) for a, b in pairs]
    return True
