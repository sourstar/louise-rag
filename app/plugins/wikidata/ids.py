"""Wikidata's ids as Louise's (docs/notes/wikidata-build-plan.md §2 "Ids").

The entity dump holds items `Q<n>` and properties `P<n>`, one number space
per letter. A `native_id` is a string the platform parses only under
`slicing.order: numeric`, and then as an integer (06 §6.1, 02 §2.2), so the
KIND is encoded in the number, as the Wikipedia plugin encodes the language
in its id: an item's native id is its number, a property's is
`PROPERTY_BASE + n`. `uid = wikidata:<native_id>`; the entity's own id is
the canonical id `wikidata:Q42` / `wikidata:P31` in `keys` (the namespace
`wikidata-refs` registers) and in `extra.id`. A pure function of the
origin's key, both ways, refused where it does not hold.
"""
from __future__ import annotations

#: a property's native id is this plus its number; QIDs are below 1.5 × 10⁸
#: and grow by about 10⁷ a year (2026-10-19), so the two never meet
PROPERTY_BASE = 1_000_000_000


class IdError(ValueError):
    """Not a Wikidata entity id this plugin numbers (items and properties)."""


def is_entity_id(s: object) -> bool:
    return isinstance(s, str) and len(s) > 1 and s[0] in "QP" and s[1:].isdigit() and s[1] != "0"


def native_id_of(entity_id: str) -> str:
    """`Q42` → `"42"`, `P31` → `"1000000031"`; a lexeme or a form is refused
    (they are another dump)."""
    if not is_entity_id(entity_id):
        raise IdError(f"{entity_id!r}: not an item (Q<n>) or property (P<n>) id")
    n = int(entity_id[1:])
    if entity_id[0] == "P":
        if n >= PROPERTY_BASE:
            raise IdError(f"{entity_id}: a property number at or above {PROPERTY_BASE} cannot be encoded")
        return str(PROPERTY_BASE + n)
    if n >= PROPERTY_BASE:
        raise IdError(f"{entity_id}: an item number at or above {PROPERTY_BASE} would read as a property")
    return str(n)


def entity_id_of(native_id: str | int) -> str:
    """The inverse: `"42"` → `Q42`, `"1000000031"` → `P31`."""
    n = int(native_id)
    if n <= 0:
        raise IdError(f"{native_id!r}: a native id is a positive integer")
    if n >= PROPERTY_BASE:
        return f"P{n - PROPERTY_BASE}"
    return f"Q{n}"


def kind_of(entity_id: str) -> str:
    return "property" if entity_id[0] == "P" else "item"


def canonical(entity_id: str) -> str:
    """`wikidata:Q42` — the canonical id (06 §6.5) of the namespace the
    reference source `wikidata-refs` registers (app/refs/wikidata.py)."""
    if not is_entity_id(entity_id):
        raise IdError(f"{entity_id!r}: not an item or property id")
    return f"wikidata:{entity_id}"
