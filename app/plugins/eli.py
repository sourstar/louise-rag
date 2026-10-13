"""ELI — the European Legislation Identifier as the shape every EU legal
plugin shares (docs/notes/eli-legal-plugins.md §6).

Nothing here is Italian. A member state's plugin declares a `Profile` and
uses these helpers, so a French or Spanish collection exposes the same
identity rules and the same `extra` keys as the Italian one:

  rule 1  the record carries the jurisdiction's UNIQUE ELI (the by-id
          permalink, never a human alias that may name several acts) —
          in `extra.eli_work`, and in `keys` since app/refs/normattiva.py
          registers the `eli` and `urn` namespaces (05 §5.2: a plugin writes
          only registered ids into `keys`) — `Profile.key`;
  rule 2  the Expression is read back from what the origin returned, never
          from what was asked for — `version_of`;
  rule 3  `native_id` is DERIVED from the unique work key, hashed where the
          key is not numeric, and COLLISION-CHECKED, never trusted —
          `native_id`, `CollisionGuard`;
  rule 5  the ELI date set, type and number go into `extra` under FIXED
          keys — `EXTRA_KEYS`;
  rule 7  force status is metadata, never a tombstone — `in_force`, the
          key 09 §9.5 and 06 §6.1 name on every legal source (`IN_FORCE`).

`uid` stays `<source>:<native_id>` like every plugin's: the platform reads
the native id back out of it (discovery, the reparse), so the ELI lives in
`extra` — and in `keys` when its namespace is registered, in whatever form
the registration fixes (05 §5.2's example is the by-type alias form).
"""
import hashlib
from dataclasses import dataclass
from datetime import date, timedelta

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
    "eli_cites",              # the act's outgoing references, in reading order
)

#: 09 §9.5's `in_force`, the one force-status key every legal source writes:
#: False only on the origin's word (a repeal state, the repealed-acts
#: collection), absent where the origin does not say. Never a tombstone
#: (06 §6.1). Written without the `eli_` prefix because the ask layer and
#: every later legal plugin read ONE key (operator, 2026-10-12).
IN_FORCE = "in_force"


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
        """06 §6.5's `<namespace>:<id>`, jurisdiction-qualified: the by-id
        form, the unique one (rule 1). Written into `keys` since
        app/refs/normattiva.py registers `eli` and names this form (05 §5.2)."""
        return f"eli:{self.jurisdiction.lower()}:{by_id_path.strip('/')}"


#: Room for the hash under the date: 10**9 keeps a YYYYMMDD prefix inside a
#: THE ID'S LAYOUT (eli-legal-plugins.md §5.2, amended 2026-10-13): the day
#: in the high bits, the hash in the low —
#:
#:     (days since EPOCH) << HASH_BITS  |  blake2b(key) mod 2**HASH_BITS
#:
#: 18 bits of days reach the year 2217 from 1500-01-01 (a date outside is
#: refused; the oldest act any EU origin publishes is centuries inside),
#: 45 bits of hash make a same-day collision one pair in 3.5·10¹³, and the
#: whole is exactly 63 bits: the largest signed int64, as every book_id
#: column is. The earlier form — `YYYYMMDD * 10**9 + hash mod 10**9` — had
#: nine decimal digits of hash, and Normattiva's 2010-12-16 (the
#: taglia-leggi day: ~77,000 versions start that day) collided three times
#: on the first discovery, as the birthday arithmetic said it would
#: (n²/2·10⁹ ≈ 3); here the same day expects 8·10⁻⁵. Still checked, never
#: trusted (`CollisionGuard`).
EPOCH = date(1500, 1, 1)
DAY_BITS = 18
HASH_BITS = 45
_HASH_SPACE = 1 << HASH_BITS
_DAYS = 1 << DAY_BITS


def _day_index(date_iso: str) -> int:
    """Days since EPOCH of an ISO `YYYY-MM-DD`; anything else is refused."""
    if len(date_iso) != 10 or date_iso[4] != "-" or date_iso[7] != "-":
        raise ValueError(f"not an ISO date: {date_iso!r}")
    try:
        d = date.fromisoformat(date_iso)
    except ValueError:
        raise ValueError(f"not an ISO date: {date_iso!r}") from None
    days = (d - EPOCH).days
    if not 0 <= days < _DAYS:
        raise ValueError(f"{date_iso}: outside the id's day range ({EPOCH.isoformat()} to "
                         f"{(EPOCH + timedelta(days=_DAYS - 1)).isoformat()})")
    return days


def _compose(date_iso: str, key: bytes) -> int:
    digest = hashlib.blake2b(key, digest_size=8).digest()
    return (_day_index(date_iso) << HASH_BITS) | (int.from_bytes(digest, "big") & (_HASH_SPACE - 1))


def id_date(native_id: int) -> date:
    """The day an id sorts under — the publication date of a Work's id, the
    start of validity of an Expression's."""
    return EPOCH + timedelta(days=int(native_id) >> HASH_BITS)


def id_day(native_id: int) -> int:
    """`id_date` as the integer YYYYMMDD the validity columns use."""
    d = id_date(native_id)
    return d.year * 10000 + d.month * 100 + d.day


def native_id(date_iso: str, local_id: str) -> int:
    """eli-legal-plugins.md §5.2: the publication day in the high bits and
    blake2b(local id) in the low (the layout above). Stable (a pure function
    of the work key), date-sortable so browse order keeps its meaning, and
    NOT reversible — `keys` carry the identity, this is a join key. Two acts
    can collide only on the same date, one pair in 2**46; the caller checks
    (`CollisionGuard`), it does not trust the arithmetic."""
    return _compose(date_iso, local_id.encode("utf-8"))


def expression_native_id(valid_from_iso: str, work_key: tuple[str, ...],
                         expression: str) -> int:
    """One record per Expression (normattiva-multivigenza-review.md §3.1):
    the start of validity in the high bits, blake2b(work key ‖ expression)
    in the 46 low ones — `native_id`'s layout. Sorting by the start of
    validity is what keeps arrival monotonic (02 §2.2): a version the origin
    publishes next month starts next month, so it sorts after every id
    already in its slice. `expression` is the origin's own identifier of the
    version (`ORIGINAL`, `CONSOLIDATED/20010201`), so an original and a
    consolidation dated the same day are two ids. Collision-checked by the
    caller like `native_id`, on (work key, expression)."""
    return _compose(valid_from_iso, "".join((*work_key, expression)).encode("utf-8"))


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
