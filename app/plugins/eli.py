"""ELI — the European Legislation Identifier as the shape every EU legal
plugin shares (docs/notes/eli-legal-plugins.md §6).

Nothing here is Italian. A member state's plugin declares a `Profile` and
uses these helpers, so a French or Spanish collection exposes the same
identity rules and the same `extra` keys as the Italian one:

  rule 1  `keys` carry the jurisdiction's UNIQUE ELI (the by-id permalink,
          never a human alias that may name several acts) — `work_key`;
  rule 2  the Expression is read back from what the origin returned, never
          from what was asked for — `version_of`;
  rule 3  `native_id` is DERIVED from the unique work key, hashed where the
          key is not numeric, and COLLISION-CHECKED, never trusted —
          `native_id`, `CollisionGuard`;
  rule 5  the ELI date set, type and number go into `extra` under FIXED
          keys — `EXTRA_KEYS`;
  rule 7  force status is metadata, never a tombstone — `eli_in_force`.

`uid` stays `<source>:<native_id>` like every plugin's: the platform reads
the native id back out of it (discovery, the reparse), so the ELI lives in
`keys` — `eli:<jurisdiction>:<by-id path>`, 06 §6.5's own example form.
"""
import hashlib
from dataclasses import dataclass

ONTOLOGY = "http://data.europa.eu/eli/ontology#"

#: 05 §5.5 of the proposal: the keys every ELI plugin writes into `extra`,
#: named identically across jurisdictions. A value the origin does not give
#: is left out, never invented.
EXTRA_KEYS = (
    "eli_jurisdiction",       # ISO 3166-1 alpha-2, upper case
    "eli_work",               # the unique by-id URI of the act, version-less
    "eli_expression",         # the versioned URI the ORIGIN returned
    "eli_type_document",      # the origin's own resource-type code
    "eli_number",
    "eli_date_document",      # the act's own date (also `date`)
    "eli_date_publication",
    "eli_version",            # e.g. ORIGINAL / CONSOLIDATED
    "eli_version_date",       # the consolidation's date, as returned
    "eli_id_local",
    "eli_title",
    "eli_uri_schema",
    "eli_in_force",           # False only on the origin's word; absent = not stated
    "eli_cites",              # the act's outgoing references, in reading order
)


@dataclass(frozen=True)
class Profile:
    """06 §6.1-style declaration of one jurisdiction's ELI implementation
    (eli-legal-plugins.md §5.3)."""
    jurisdiction: str          # "IT"
    authority: str             # "stato"
    base: str                  # absolute base the origin's relative ELIs hang from
    uri_template: str          # the UNIQUE (by-id) template, verbatim from the origin
    work_key: tuple[str, ...]  # the fields that make an act unique

    def absolute(self, relative: str) -> str:
        """`eli/id/…` → `<base>eli/id/…`; an absolute URI passes through."""
        if "://" in relative:
            return relative
        return self.base.rstrip("/") + "/" + relative.lstrip("/")

    def key(self, by_id_path: str) -> str:
        """06 §6.5's `<namespace>:<id>`, jurisdiction-qualified."""
        return f"eli:{self.jurisdiction.lower()}:{by_id_path.strip('/')}"


#: Room for the hash under the date: 10**9 keeps a YYYYMMDD prefix inside a
#: signed 64-bit integer (max 9.2e18; 99991231 * 10**9 = 9.99e16).
_HASH_SPACE = 10 ** 9


def native_id(date_iso: str, local_id: str) -> int:
    """eli-legal-plugins.md §5.2: `YYYYMMDD * 10**9 + blake2b(local id) mod
    10**9`. Stable (a pure function of the work key), date-sortable in the
    high digits so range predicates and browse order keep their meaning,
    and NOT reversible — `keys` carry the identity, this is a join key.
    Two acts can collide only on the same date, one pair in 10**9; the
    caller checks (`CollisionGuard`), it does not trust the arithmetic."""
    ymd = date_iso.replace("-", "")
    if len(ymd) != 8 or not ymd.isdigit():
        raise ValueError(f"not an ISO date: {date_iso!r}")
    digest = hashlib.blake2b(local_id.encode("utf-8"), digest_size=8).digest()
    return int(ymd) * _HASH_SPACE + int.from_bytes(digest, "big") % _HASH_SPACE


class CollisionGuard:
    """Every native id seen in one scan, with the work key that made it. A
    second key under the same id is a collision: `check` returns the key
    already there, and the caller refuses the newcomer loudly."""

    def __init__(self) -> None:
        self._seen: dict[int, tuple] = {}

    def check(self, nid: int, key: tuple) -> tuple | None:
        held = self._seen.setdefault(nid, key)
        return None if held == key else held


def version_of(returned: str) -> tuple[str | None, str | None]:
    """`(version, version date)` from the versioned tail of a RETURNED ELI —
    `…/ORIGINAL`, `…/CONSOLIDATED/20230701`, `…/CONSOLIDATED` (undated: the
    whole amendment history, which pins no date). Rule 2: this is the
    answer the origin gave, and it names the Expression we hold."""
    parts = [p for p in returned.strip("/").split("/") if p]
    for i, p in enumerate(parts):
        if p in ("ORIGINAL", "CONSOLIDATED"):
            date = parts[i + 1] if i + 1 < len(parts) else ""
            if len(date) == 8 and date.isdigit():
                return p, f"{date[:4]}-{date[4:6]}-{date[6:]}"
            return p, None
    return None, None
