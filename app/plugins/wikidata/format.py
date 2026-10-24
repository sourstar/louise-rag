"""Wikidata values as text — dates, coordinates, quantities — in code of our
own (docs/notes/wikidata-build-plan.md D4, revised 2026-10-19: "D4 Python").

Wikimedia's textifier posted every time and coordinate value to a running
Wikibase (`wbformatvalue`), which is why its stack carried a MediaWiki and
a MariaDB. Here the same values are formatted by pure functions, so a
record's text is a function of the dump and the platform image alone
(invariant 3) and a verifier needs nothing running to reproduce it. Byte
parity with Wikibase's output is not sought; determinism and legibility
are. Everything that reaches a hash is a string (09 §9.1): a coordinate's
floats are read from the dump's own JSON text, never re-serialised.

The rules, from Wikibase's own formatters (TimeFormatter, MwTimeIsoFormatter,
GlobeCoordinateFormatter in DMS), reduced to what a dump carries:

- time: `+1952-04-19T00:00:00Z` with `precision` 0–14 and a `calendarmodel`
  (Q1985727 Gregorian, Q1985786 Julian). Precision 11 and finer is the day,
  10 the month, 9 the year, 8 the decade, 7 the century, 6 the millennium,
  5–0 a span of years; BCE for years before 1; a Julian date is marked.
- coordinates: degrees, minutes, seconds with the hemisphere, the seconds
  rounded by `precision` (degrees); a globe other than Earth (Q2) named.
- quantities: the amount as the dump writes it without its leading `+`,
  the unit's label where it has one, else its id; bounds are not shown.

Month names are given for en, de, fr, es and it; every other language gets
the ISO form, which is unambiguous and still deterministic.
"""
from __future__ import annotations

import math
import re
from typing import Callable

GREGORIAN = "Q1985727"
JULIAN = "Q1985786"
EARTH = "Q2"

_MONTHS = {
    "en": ("January", "February", "March", "April", "May", "June", "July", "August", "September",
           "October", "November", "December"),
    "de": ("Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September",
           "Oktober", "November", "Dezember"),
    "fr": ("janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre",
           "octobre", "novembre", "décembre"),
    "es": ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre",
           "octubre", "noviembre", "diciembre"),
    "it": ("gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio", "agosto", "settembre",
           "ottobre", "novembre", "dicembre"),
}
#: the words a span of years takes, per language; en elsewhere
_WORDS = {
    "en": {"bce": "BCE", "century": "{n} century", "millennium": "{n} millennium", "decade": "{y}s",
           "years": "{n} years", "million": "{n} million years", "billion": "{n} billion years",
           "julian": "Julian"},
    "de": {"bce": "v. Chr.", "century": "{n}. Jahrhundert", "millennium": "{n}. Jahrtausend", "decade": "{y}er",
           "years": "{n} Jahre", "million": "{n} Millionen Jahre", "billion": "{n} Milliarden Jahre",
           "julian": "julianisch"},
    "fr": {"bce": "av. J.-C.", "century": "{n} siècle", "millennium": "{n} millénaire", "decade": "années {y}",
           "years": "{n} ans", "million": "{n} millions d'années", "billion": "{n} milliards d'années",
           "julian": "julien"},
    "es": {"bce": "a. C.", "century": "siglo {n}", "millennium": "{n} milenio", "decade": "años {y}",
           "years": "{n} años", "million": "{n} millones de años", "billion": "{n} mil millones de años",
           "julian": "juliano"},
    "it": {"bce": "a.C.", "century": "{n} secolo", "millennium": "{n} millennio", "decade": "anni {y}",
           "years": "{n} anni", "million": "{n} milioni di anni", "billion": "{n} miliardi di anni",
           "julian": "giuliano"},
}

_TIME_RE = re.compile(r"^([+-])(\d+)-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})Z$")


class FormatError(ValueError):
    """A value the dump should not hold (the parser records it raw instead)."""


def _ordinal(n: int, lang: str) -> str:
    if lang == "en":
        if 10 <= n % 100 <= 20:
            suffix = "th"
        else:
            suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
        return f"{n}{suffix}"
    if lang in ("fr",):
        return f"{n}er" if n == 1 else f"{n}e"
    if lang in ("es", "it"):
        return f"{n}º"
    return str(n)


def time_text(value: dict, lang: str = "en") -> str:
    """A Wikibase time datavalue as text. `value` is the dump's object:
    `{time, timezone, before, after, precision, calendarmodel}`."""
    raw = value.get("time")
    if not isinstance(raw, str):
        raise FormatError(f"time is not a string: {raw!r}")
    m = _TIME_RE.match(raw)
    if m is None:
        raise FormatError(f"time not in Wikibase form: {raw!r}")
    sign, year_s, month_s, day_s = m.group(1), m.group(2), m.group(3), m.group(4)
    year, month, day = int(year_s), int(month_s), int(day_s)
    precision = value.get("precision", 11)
    if not isinstance(precision, int) or not 0 <= precision <= 14:
        raise FormatError(f"precision out of range: {precision!r}")
    words = _WORDS.get(lang, _WORDS["en"])
    bce = sign == "-"
    # Wikibase writes 500 BCE as -0500 (the astronomical year is 1 - 500)
    era = f" {words['bce']}" if bce else ""
    cal = value.get("calendarmodel") or ""
    julian = f" ({words['julian']})" if cal.rsplit("/", 1)[-1] == JULIAN else ""
    if precision >= 11:
        if month == 0 or day == 0:
            text = str(year)
        else:
            months = _MONTHS.get(lang)
            if months is None:
                text = f"{year:04d}-{month:02d}-{day:02d}"
            elif lang == "en":
                text = f"{day} {months[month - 1]} {year}"
            elif lang == "de":
                text = f"{day}. {months[month - 1]} {year}"
            elif lang == "es":
                text = f"{day} de {months[month - 1]} de {year}"
            else:
                text = f"{day} {months[month - 1]} {year}"
        return text + era + julian
    if precision == 10:
        months = _MONTHS.get(lang)
        if month == 0:
            text = str(year)
        elif months is None:
            text = f"{year:04d}-{month:02d}"
        elif lang == "es":
            text = f"{months[month - 1]} de {year}"
        else:
            text = f"{months[month - 1]} {year}"
        return text + era + julian
    if precision == 9:
        return f"{year}{era}{julian}"
    if precision == 8:
        return words["decade"].format(y=f"{(year // 10) * 10}") + era
    if precision == 7:
        n = (year - 1) // 100 + 1 if year > 0 else year // 100 + 1
        return words["century"].format(n=_ordinal(n, lang)) + era
    if precision == 6:
        n = (year - 1) // 1000 + 1 if year > 0 else year // 1000 + 1
        return words["millennium"].format(n=_ordinal(n, lang)) + era
    # 5–0: spans of 10⁴ … 10⁹ years around the year given
    span = 10 ** (9 - precision)          # precision 5 → 10,000; 0 → 1,000,000,000
    rounded = (abs(year) // span) * span
    if span >= 1_000_000_000:
        text = words["billion"].format(n=f"{rounded // 1_000_000_000:,}")
    elif span >= 1_000_000:
        text = words["million"].format(n=f"{rounded // 1_000_000:,}")
    else:
        text = words["years"].format(n=f"{rounded:,}")
    return text + era


def _dms(deg: float, decimals: int) -> tuple[int, int, str]:
    d = abs(deg)
    whole = int(d)
    minutes_f = (d - whole) * 60
    minutes = int(minutes_f)
    seconds = (minutes_f - minutes) * 60
    sec = f"{seconds:.{decimals}f}"
    # carry a rounded 60.0" or 60'
    if float(sec) >= 60.0:
        sec = f"{0:.{decimals}f}"
        minutes += 1
    if minutes >= 60:
        minutes = 0
        whole += 1
    return whole, minutes, sec


def coordinate_text(value: dict, lang: str = "en", globe_label: Callable[[str], str | None] | None = None) -> str:
    """A globe-coordinate datavalue as degrees, minutes and seconds with the
    hemispheres; the seconds rounded to `precision` (degrees; absent or
    null = one arcsecond, as Wikibase assumes). A globe other than Earth is
    named after the coordinates, by its label where `globe_label` gives one,
    else its id."""
    lat, lon = value.get("latitude"), value.get("longitude")
    if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
        raise FormatError(f"coordinates without latitude/longitude: {value!r}")
    precision = value.get("precision")
    if not isinstance(precision, (int, float)) or precision <= 0:
        precision = 1 / 3600
    arcsec = precision * 3600
    decimals = 0 if arcsec >= 1 else min(4, int(math.ceil(-math.log10(arcsec))))
    la_d, la_m, la_s = _dms(lat, decimals)
    lo_d, lo_m, lo_s = _dms(lon, decimals)
    ns = "N" if lat >= 0 else "S"
    ew = "E" if lon >= 0 else "W"
    text = f"{la_d}°{la_m}'{la_s}\"{ns}, {lo_d}°{lo_m}'{lo_s}\"{ew}"
    globe = (value.get("globe") or "").rsplit("/", 1)[-1]
    if globe and globe != EARTH:
        name = globe_label(globe) if globe_label is not None else None
        text += f" ({name or globe})"
    return text


def quantity_text(value: dict, unit_label: Callable[[str], str | None] | None = None) -> str:
    """A quantity datavalue: the amount as written, without its leading `+`
    (Wikibase's own display drops it), its uncertainty as Wikibase shows it
    — `±` the larger distance to a bound, in exact decimals (parser 2: the
    first formatter dropped the bounds) — and the unit's label or id where
    the unit is not `1`."""
    from decimal import Decimal, InvalidOperation
    amount = value.get("amount")
    if not isinstance(amount, str):
        raise FormatError(f"quantity without an amount string: {value!r}")
    text = amount[1:] if amount.startswith("+") else amount
    upper, lower = value.get("upperBound"), value.get("lowerBound")
    if isinstance(upper, str) and isinstance(lower, str):
        try:
            a = Decimal(amount)
            margin = max(Decimal(upper) - a, a - Decimal(lower))
        except InvalidOperation:
            margin = Decimal(0)
        if margin > 0:
            text += "±" + format(margin, "f")
    unit = value.get("unit")
    if isinstance(unit, str) and unit not in ("", "1"):
        uid = unit.rsplit("/", 1)[-1]
        name = unit_label(uid) if unit_label is not None else None
        text += f" {name or uid}"
    return text
