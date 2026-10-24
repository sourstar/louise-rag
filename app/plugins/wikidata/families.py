"""Wikidata's FAMILIES — what a node chooses to hold and embed, as Wikipedia's
languages are (operator, 2026-10-24: "Go with A"; docs/notes/
wikidata-family-slices.md). A record's slice is `family=<f>/range=<n>`.

THE MAP IS PINNED AT CREATION (option A, the amendment of pack-format.md
§2): the class tree of the creation dump decides each class's family once,
and the map — every class under a family's roots, with its family — is kept
with the collection and never changes (its sha256 in the Codex's slicing
block, its bytes the SHARD member `slicing/<hex>.json`). Later edits to the
class tree move nothing; a record moves only when its OWN `instance of`
(P31) or heritage designation (P1435) changes — the edit that re-issues its
part anyway.

- A CLASS's family is that of the nearest root below which it sits (fewest
  P279 steps; a tie goes to the family first in `FAMILIES`): a municipality
  is two steps under "administrative territorial entity" and further under
  "organization", so it is a place. Multi-source breadth-first search, the
  textbook way to label a graph by its nearest source.
- An ENTITY's family is the first in `FAMILIES` among those of its
  non-deprecated P31 values; a heritage designation (P1435) makes it
  heritage; a property is `property`, a lexeme `lexeme`; nothing found,
  `other`.
- A class NOT in the map whose number is at most the map's `max_item`
  existed at creation under no root: `other`, for ever. A class created
  AFTER the creation dump (a larger number) is placed by walking the
  CURRENT dump's P279 links up to the nearest class that existed (`walk`) —
  the one place a later edit can move a record; logged per pass.

The rule and the map are pure functions of the dump, so anyone holding the
creation dump can rebuild the map and compare its sha256.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Optional

#: in PRECEDENCE order — an entity in several takes the first; the code of a
#: family is its place in the MAP's list plus one (0: not computed). This is
#: the list a NEW map is written with; every map carries its own list and is
#: read by it, so a family added here later leaves every pinned map — and the
#: collection it slices — readable as it was (wikidata-family-slices.md §7)
FAMILIES: tuple[str, ...] = ("scholarly", "internal", "heritage", "event", "person", "organization", "place",
                             "work", "taxon", "property", "lexeme", "other")
CODE: dict[str, int] = {f: i + 1 for i, f in enumerate(FAMILIES)}
OTHER = CODE["other"]
#: the classes whose subclasses (P279, any depth) make a family, by number:
#: scholarly article; Wikimedia internal item; built structure, monument,
#: archaeological site, architectural element; planned event, military
#: operation, war, calamity, massacre, explosion, geographically localized
#: event, severe weather, demonstration or protest; human; organization;
#: human settlement, administrative territorial entity, geographical feature;
#: creative work; taxon (wikidata-family-slices.md §5)
ROOTS: dict[str, tuple[int, ...]] = {
    "scholarly": (13442814,),
    "internal": (17442446,),
    "heritage": (811979, 4989906, 839954, 391414),
    "event": (1656682, 645883, 198, 1689296, 3199915, 179057, 123349687, 741507, 130260549),
    "person": (5,),
    "organization": (43229,),
    "place": (486972, 56061, 618123),
    "work": (17537576,),
    "taxon": (16521,),
}
#: a statement that makes an entity heritage, whatever it is an instance of
HERITAGE_DESIGNATION = "P1435"
#: the id range a slice holds under its family: the entity's number ÷ 10⁷
RANGE = 10_000_000
#: the families decided by an entity's kind, not by its classes: every map has them
BY_KIND = ("property", "lexeme", "other")
KIND = "louise-wikidata-families"
VERSION = 1


class FamilyError(ValueError):
    """A map that is not one this code writes."""


def canonical(obj) -> bytes:
    """09 §9.1's canonical JSON (sorted keys, no whitespace, UTF-8) — the
    same call as app/hold/codex.py `canonical_json`; the map holds only
    integers and strings."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def number(entity_id: object) -> Optional[int]:
    """`Q42` → 42; None for anything that is not an item id."""
    if isinstance(entity_id, str) and len(entity_id) > 1 and entity_id[0] == "Q" and entity_id[1:].isdigit():
        return int(entity_id[1:])
    return None


# ---- the map ---------------------------------------------------------------------------------

def nearest_roots(child: Iterable[int], parent: Iterable[int],
                  roots: Mapping[str, Iterable[int]] = ROOTS,
                  families: tuple[str, ...] = FAMILIES) -> dict[int, int]:
    """Every class under a root → its family's code in `families`: the
    nearest root by P279 steps, a tie to the family first in precedence —
    level by level from all roots at once, so a cycle or a class reached
    twice is settled where it is first reached."""
    code_of = {f: i + 1 for i, f in enumerate(families)}
    subs: dict[int, list[int]] = {}
    for c, p in zip(child, parent):
        subs.setdefault(p, []).append(c)
    assigned: dict[int, int] = {}
    frontier: dict[int, int] = {}
    for fam, rs in roots.items():
        for r in rs:
            if r not in frontier or code_of[fam] < frontier[r]:
                frontier[r] = code_of[fam]
    while frontier:
        assigned.update(frontier)
        nxt: dict[int, int] = {}
        for cls, code in frontier.items():
            for sub in subs.get(cls, ()):
                if sub not in assigned and (sub not in nxt or code < nxt[sub]):
                    nxt[sub] = code
        frontier = nxt
    return assigned


def build_map(child: Iterable[int], parent: Iterable[int], *, max_item: int, dump: str,
              roots: Mapping[str, Iterable[int]] = ROOTS, families: tuple[str, ...] = FAMILIES) -> bytes:
    """The map of a creation dump, canonical bytes: its P279 edges (item
    numbers, child → parent), its largest item number, and the family list
    in precedence order it is read by."""
    _check_families(families, roots)
    codes = nearest_roots(child, parent, roots, families)
    classes: dict[str, list[int]] = {}
    for cls, code in codes.items():
        classes.setdefault(families[code - 1], []).append(cls)
    return canonical({
        "kind": KIND, "version": VERSION, "dump": dump, "max_item": int(max_item),
        "families": list(families), "roots": {f: sorted(int(r) for r in rs) for f, rs in roots.items()},
        "heritage_designation": HERITAGE_DESIGNATION, "range": RANGE,
        "classes": {f: sorted(v) for f, v in sorted(classes.items())},
    })


def _check_families(families, roots) -> None:
    if len(set(families)) != len(families) or not set(BY_KIND) <= set(families) or families[-1] != "other":
        raise FamilyError(f"a family list names each family once, ends with `other` and holds "
                          f"{', '.join(BY_KIND)}: {list(families)}")
    if not set(roots) <= set(families) - set(BY_KIND):
        raise FamilyError(f"roots for families not in the list: {sorted(set(roots) - set(families))}")


@dataclass(frozen=True)
class FamilyMap:
    """A map, read: its own family list (precedence order) and roots, class
    number → code in that list, and what identifies it."""
    raw: bytes
    sha256: str
    dump: str
    max_item: int
    families: tuple[str, ...]
    roots: dict[str, tuple[int, ...]] = field(repr=False)
    classes: dict[int, int] = field(repr=False)

    def code(self, family: str) -> int:
        return self.families.index(family) + 1

    @property
    def other(self) -> int:
        return len(self.families)                     # `other` is last, always

    def name(self, code: int) -> str:
        if not 1 <= code <= len(self.families):
            raise FamilyError(f"family code {code} is not computed")
        return self.families[code - 1]

    def code_of_class(self, q: int, walked: Mapping[int, int] | None = None) -> int:
        got = self.classes.get(q)
        if got is not None:
            return got
        if q > self.max_item and walked:
            return walked.get(q, self.other)
        return self.other


def load_map(raw: bytes) -> FamilyMap:
    """A map's bytes → `FamilyMap`, read by its OWN family list (a map
    written before a family was added reads as it was written); refused
    unless canonical and well formed."""
    try:
        d = json.loads(raw)
    except ValueError as e:
        raise FamilyError(f"the family map is not JSON: {e}") from None
    if not isinstance(d, dict) or d.get("kind") != KIND or d.get("version") != VERSION:
        raise FamilyError(f"not a {KIND} version {VERSION} map")
    if canonical(d) != bytes(raw):
        raise FamilyError("the family map is not in canonical form (09 §9.1): its hash would not be its identity")
    fams = tuple(d.get("families") or ())
    roots = {str(f): tuple(int(r) for r in rs) for f, rs in (d.get("roots") or {}).items()}
    _check_families(fams, roots)
    code_of = {f: i + 1 for i, f in enumerate(fams)}
    classes: dict[int, int] = {}
    for fam, nums in (d.get("classes") or {}).items():
        if fam not in code_of:
            raise FamilyError(f"the map lists classes for {fam!r}, which its family list does not name")
        for q in nums:
            classes[int(q)] = code_of[fam]
    return FamilyMap(raw=bytes(raw), sha256="sha256:" + hashlib.sha256(raw).hexdigest(), dump=str(d["dump"]),
                     max_item=int(d["max_item"]), families=fams, roots=roots, classes=classes)


def walk(child: Iterable[int], parent: Iterable[int], fmap: FamilyMap) -> dict[int, int]:
    """Every class created after the map's dump (number > `max_item`) that
    has P279 links in THIS dump → the family of the nearest class the map
    knows — one it lists, or one that existed at creation (fewest steps up;
    a tie to the first family in precedence; none reached: `other`). A pure
    function of the dump and the map, so two passes over one dump agree."""
    ups: dict[int, list[int]] = {}
    for c, p in zip(child, parent):
        if c > fmap.max_item:
            ups.setdefault(c, []).append(p)
    out: dict[int, int] = {}
    for start in ups:
        seen, level, found = {start}, [start], fmap.other
        while level and found == fmap.other:
            nxt = []
            old = []
            for cls in level:
                for up in ups.get(cls, ()):
                    if up in seen:
                        continue
                    seen.add(up)
                    (old if up <= fmap.max_item or up in fmap.classes else nxt).append(up)
            if old:
                found = min(fmap.code_of_class(q) for q in old)
                break
            level = nxt
        out[start] = found
    return out


# ---- an entity's family, and its slice -------------------------------------------------------

def item_values(statements) -> Iterable[int]:
    """The item numbers a property's non-deprecated statements state."""
    for st in statements or ():
        if not isinstance(st, dict) or st.get("rank") == "deprecated":
            continue
        snak = st.get("mainsnak") or {}
        dv = (snak.get("datavalue") or {}).get("value") if isinstance(snak, dict) else None
        q = number(dv.get("id")) if isinstance(dv, dict) else None
        if q is None and isinstance(dv, dict) and dv.get("entity-type") == "item" \
                and isinstance(dv.get("numeric-id"), int):
            q = int(dv["numeric-id"])
        if q is not None:
            yield q


def _designated(statements) -> bool:
    """A non-deprecated heritage designation whose value is stated or
    unknown — "no value" says it has none."""
    return any(isinstance(st, dict) and st.get("rank") != "deprecated"
               and ((st.get("mainsnak") or {}).get("snaktype") or "value") != "novalue"
               for st in statements or ())


def family_and_class(obj: dict, fmap: FamilyMap,
                     walked: Mapping[int, int] | None = None) -> tuple[int, Optional[int]]:
    """The entity's family code, from its own statements and the map, and
    the class that decided it (None: its kind, its designation or nothing)."""
    eid = str(obj.get("id") or "")
    if eid[:1] == "P":
        return fmap.code("property"), None
    if eid[:1] == "L":
        return fmap.code("lexeme"), None
    claims = obj.get("claims") if isinstance(obj.get("claims"), dict) else {}
    best, by = fmap.other, None
    for q in item_values(claims.get("P31")):
        code = fmap.code_of_class(q, walked)
        if code < best:
            best, by = code, q
    if "heritage" in fmap.families and best > fmap.code("heritage") \
            and _designated(claims.get(HERITAGE_DESIGNATION)):
        return fmap.code("heritage"), None
    return best, by


def family_code(obj: dict, fmap: FamilyMap, walked: Mapping[int, int] | None = None) -> int:
    """The entity's family code (`family_and_class`)."""
    return family_and_class(obj, fmap, walked)[0]


def slice_name(code: int, entity_id: str, families: tuple[str, ...] = FAMILIES) -> str:
    """`family=heritage/range=3` — the family (by the MAP's list: pass
    `fmap.families`), then the entity's number in its kind ÷ 10⁷ (Q42 → 0,
    P31 → 0, L7 → 0), so parts stay even under a family."""
    if not 1 <= code <= len(families):
        raise FamilyError(f"family code {code} is not computed")
    n = int(entity_id[1:]) if len(entity_id) > 1 and entity_id[1:].isdigit() else 0
    return f"family={families[code - 1]}/range={n // RANGE}"
