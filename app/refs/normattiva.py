"""The Italian acts as a REFERENCE SOURCE (06 §6.5; docs/notes/normattiva-multivigenza-review.md
§3.12; gazzetta-plugin.md §6): the lookup tables a citation resolves
against, and the namespaces — `urn` (URN:NIR) and `eli` — that let a
Normattiva record write its own ids into `keys` (05 §5.2: a plugin writes
only ids a reference source registers).

Two tables, synced from the acts' own metadata in the multivigente zips
(the plugin's corpus, mounted under `<ref_root>/normattiva`): `atto`, one
row per act keyed by its URN:NIR, with its ELI work URI, type, date,
number, title and codice redazionale; `versione`, one row per version
keyed by the record's uid, with the act's URN, the start of validity and
the Expression. The link `versione.urn → atto.urn` is what `expand`
follows, so a citation pins the act and its versions as facts — never
ranked (invariant 4).

The sync reads each member's HEAD only — the first 64 KB, where the FRBR
identifiers, the publication and the preface are — so 300,000 members
cost minutes, not the hours a full parse would. What the head says is
what the plugin's own parser says of the same bytes (the `urn:nir` alias,
the `eli` alias, `docType`, `docDate`, `docNumber`, `docTitle`); a member
stored as base64 is decoded first, as the plugin decodes it.

The resolver (`CitationResolver`) turns the common Italian forms of a
citation — "l. 306/2003", "legge 31 ottobre 2003, n. 306", "d.lgs. n. 82
del 2005", "legge costituzionale 26 febbraio 1948, n. 3" — into the acts
they name, by (type, number, year) against an index memoised on the
reference data's version, as the gene resolver does. It is a pure
function of the query text and the tables' content (invariant 3). An
article number in the citation is kept on the entity's label; the entity
is the act, as the record is.
"""
from __future__ import annotations

import base64
import binascii
import re
import threading
import zipfile
from pathlib import Path
from typing import Iterator

from ..plugins.base import (Entity, RefColumn, RefLink, RefLookup, RefRecordBatch, RefSchema,
                            RefSyncState, RefTable)

SOURCE_ID = "normattiva-refs"
NAMESPACES = ("urn", "eli", "akn")

ATTO = RefTable(
    name="atto",
    columns=(
        RefColumn("urn", "TEXT"),           # urn:nir:stato:legge:2003-10-31;306 — the act's URN:NIR
        RefColumn("eli", "TEXT"),           # the ELI work URI, version-less
        RefColumn("akn", "TEXT"),           # akn:it/act/legge/stato/2003-10-31/306 — the Akoma Ntoso work path
        RefColumn("tipo", "TEXT"),          # the URN's type token: legge, decreto.legislativo, …
        RefColumn("data", "TEXT"),          # the act's own date
        RefColumn("anno", "INTEGER"),       # its year, for a citation that gives no day
        RefColumn("numero", "TEXT"),
        RefColumn("titolo", "TEXT"),
        RefColumn("denominazione", "TEXT"), # "LEGGE 31 ottobre 2003, n. 306"
        RefColumn("codice", "TEXT"),        # codice redazionale
        RefColumn("pubblicazione", "TEXT"), # the GU date
    ),
    primary_key="urn",
)

VERSIONE = RefTable(
    name="versione",
    columns=(
        RefColumn("uid", "TEXT"),           # the record: normattiva:<expression native id>
        RefColumn("urn", "TEXT"),
        RefColumn("valid_from", "TEXT"),    # YYYY-MM-DD, the start of validity (the id's prefix)
        RefColumn("expression", "TEXT"),    # ORIGINAL | CONSOLIDATED/yyyymmdd
        RefColumn("member", "TEXT"),        # <zip>#<member>, the origin unit
    ),
    primary_key="uid",
)

LINK = RefLink(name="versioni", from_table="versione", from_column="urn",
               to_table="atto", to_column="urn")

HEAD = 64 << 10
_BATCH = 2_000

_URN = re.compile(r'<FRBRalias\s+name="urn:nir"\s+value="([^"]+)"')
_AKN = re.compile(r'<FRBRWork>\s*<FRBRthis\s+value="([^"]+)"', re.S)
_ELI = re.compile(r'<FRBRalias\s+name="eli"\s+value="([^"]+)"')
_DOC_TYPE = re.compile(r"<docType>(.*?)</docType>", re.S)
_DOC_DATE = re.compile(r'<docDate\s+date="([^"]+)"[^>]*>(.*?)</docDate>', re.S)
_DOC_NUMBER = re.compile(r"<docNumber>(.*?)</docNumber>", re.S)
_DOC_TITLE = re.compile(r"<docTitle>(.*?)</docTitle>", re.S)
_PUBLICATION = re.compile(r'<publication\s+[^>]*date="([^"]+)"')
_TAGS = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def _text(fragment: str | None) -> str:
    return _WS.sub(" ", _TAGS.sub("", fragment or "")).strip()


def head_of(zf: zipfile.ZipFile, name: str, n: int = HEAD) -> bytes:
    """The member's first `n` bytes as XML: decoded where the origin stored
    the member as base64 (two of the forty codes)."""
    with zf.open(name) as f:
        data = f.read(n)
    if data.lstrip()[:1] == b"<" or data.startswith(b"\xef\xbb\xbf"):
        return data
    cut = data[: len(data) - len(data) % 4]
    try:
        return base64.b64decode(cut, validate=False)
    except (ValueError, binascii.Error):
        return data


def meta_of(head: bytes) -> dict | None:
    """What the head states about the act; None when it names no URN."""
    s = head.decode("utf-8", errors="replace")
    urn = _URN.search(s)
    if not urn:
        return None
    urn_value = urn.group(1).strip()
    eli = _ELI.search(s)
    date = _DOC_DATE.search(s)
    parts = urn_value.split(":")
    tipo = parts[3] if len(parts) > 4 else ""
    data = (date.group(1) if date else "") or (parts[4].split(";")[0] if len(parts) > 4 else "")
    numero = _text((_DOC_NUMBER.search(s) or [None, ""])[1] if _DOC_NUMBER.search(s) else "")
    if not numero and len(parts) > 4 and ";" in parts[4]:
        numero = parts[4].split(";", 1)[1]
    doc_type = _text(_DOC_TYPE.search(s).group(1)) if _DOC_TYPE.search(s) else ""
    date_text = _text(date.group(2)) if date else ""
    denominazione = " ".join(x for x in (doc_type, date_text) if x)
    if numero:
        denominazione = f"{denominazione}, n. {numero}" if denominazione else f"n. {numero}"
    pub = _PUBLICATION.search(s)
    return {
        "urn": urn_value,
        "eli": _eli_work(eli.group(1).strip()) if eli else "",
        "akn": (akn.group(1).split("/!", 1)[0].strip("/").replace("akn/", "akn:", 1)
                if (akn := _AKN.search(s)) and akn.group(1).lstrip("/").startswith("akn/") else ""),
        "tipo": tipo, "data": data,
        "anno": int(data[:4]) if data[:4].isdigit() else None,
        "numero": numero,
        "titolo": _text(_DOC_TITLE.search(s).group(1)) if _DOC_TITLE.search(s) else "",
        "denominazione": denominazione,
        "codice": _codice(eli.group(1)) if eli else "",
        "pubblicazione": pub.group(1) if pub else "",
    }


def _eli_work(alias: str) -> str:
    """`eli/id/2003/11/15/003G0333/CONSOLIDATED/20180206` → the work, versionless."""
    parts = alias.strip("/").split("/")
    for i, p in enumerate(parts):
        if p in ("ORIGINAL", "CONSOLIDATED"):
            parts = parts[:i]
            break
    return "http://www.normattiva.it/" + "/".join(parts)


def _codice(alias: str) -> str:
    parts = alias.strip("/").split("/")
    return parts[5] if len(parts) > 5 and parts[0] == "eli" and parts[1] == "id" else ""


class NormattivaRefSource:
    source_id = SOURCE_ID
    ref_version = 2            # 2: the `akn` column (2026-10-12)
    namespaces = NAMESPACES

    def schema(self) -> RefSchema:
        return RefSchema(source_id=self.source_id, tables=(ATTO, VERSIONE), links=(LINK,))

    def sync(self, state: RefSyncState) -> Iterator[RefRecordBatch]:
        """The zips under `<ref_root>/normattiva` — the plugin's corpus, as
        acquisition lays it out — read member by member, heads only."""
        from ..plugins import eli as eli_mod
        from ..plugins.normattiva.source import _MEMBER, version_key
        root = Path(state.ref_root) / "normattiva"
        if not root.is_dir():
            return
        acts: dict[str, dict] = {}
        versions: list[dict] = []
        for zpath in sorted(root.rglob("*.zip")):
            try:
                zf = zipfile.ZipFile(zpath)
            except (zipfile.BadZipFile, OSError):
                continue
            with zf:
                for info in zf.infolist():
                    m = _MEMBER.match(info.filename)
                    if m is None:
                        continue
                    try:
                        meta = meta_of(head_of(zf, info.filename))
                    except (zipfile.BadZipFile, OSError):
                        continue
                    if meta is None:
                        continue
                    gu, codice, form, vig = m["gu"], m["codice"], m["form"], m["vig"]
                    valid_from = vig or gu
                    expr = version_key(form, vig)
                    nid = eli_mod.expression_native_id(valid_from, (gu, codice), expr)
                    if not meta["codice"]:
                        meta["codice"] = codice
                    if not meta["pubblicazione"]:
                        meta["pubblicazione"] = gu
                    acts.setdefault(meta["urn"], meta)
                    versions.append({"uid": f"normattiva:{nid}", "urn": meta["urn"],
                                     "valid_from": valid_from, "expression": expr,
                                     "member": f"{zpath.relative_to(root).as_posix()}#{info.filename}"})
                    if len(versions) >= _BATCH:
                        yield RefRecordBatch(table=VERSIONE.name, rows=tuple(versions))
                        versions = []
            if len(acts) >= _BATCH:
                yield RefRecordBatch(table=ATTO.name, rows=tuple(acts.values()))
                acts = {}
        if acts:
            yield RefRecordBatch(table=ATTO.name, rows=tuple(acts.values()))
        if versions:
            yield RefRecordBatch(table=VERSIONE.name, rows=tuple(versions))


# ---- the citation resolver ----------------------------------------------------------

#: how a citation names an act's type → the URN:NIR type token
_TYPES: tuple[tuple[str, str], ...] = (
    (r"legge\s+costituzionale|l\.\s*cost\.", "legge.costituzionale"),
    (r"decreto\s+legislativo|d\.\s*lgs\.?|dlgs|d\.\s*lg\.", "decreto.legislativo"),
    (r"decreto[-\s]legge|d\.\s*l\.|\bdl\b", "decreto.legge"),
    (r"decreto\s+del\s+presidente\s+della\s+repubblica|d\.\s*p\.\s*r\.|\bdpr\b",
     "decreto.del.presidente.della.repubblica"),
    (r"decreto\s+del\s+presidente\s+del\s+consiglio\s+dei\s+ministri|d\.\s*p\.\s*c\.\s*m\.|\bdpcm\b",
     "decreto.del.presidente.del.consiglio.dei.ministri"),
    (r"regio\s+decreto[-\s]legge|r\.\s*d\.\s*l\.", "regio.decreto.legge"),
    (r"regio\s+decreto|r\.\s*d\.", "regio.decreto"),
    # a consolidated text named by what it is, not by its legal form: the
    # form is the origin's to say ("testo unico … 13 novembre 1919, n. 2205"
    # is a regio decreto-legge), found by date and number (`resolve`)
    (r"testo\s+unico|t\.\s*u\.", "*"),
    (r"legge|\bl\.", "legge"),
)
_TYPE_RE = "|".join(f"(?:{p})" for p, _ in _TYPES)
_MONTHS = {m: i + 1 for i, m in enumerate(
    ("gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio", "agosto",
     "settembre", "ottobre", "novembre", "dicembre"))}
_MONTH_RE = "|".join(_MONTHS)
_NUM = r"(?P<num>\d+(?:[-\s]?(?:bis|ter|quater|quinquies))?)"
#: "art. 5, comma 3, della" before an act: the article and comma kept for
#: the label, the preposition not
_ART = r"(?:(?P<art>art(?:icolo|\.)?\s*\d+(?:-\w+)?(?:\s*,?\s*comma\s*\d+)?)\s*,?\s*(?:del(?:la)?\s+)?)?"
#: "legge 31 ottobre 2003, n. 306" — the full form; a few words of title may
#: stand between the form and the date ("legge sull'emigrazione testo unico
#: 13 novembre 1919, n. 2205", operator 2026-10-13: the citation went
#: unrecognised and the act was answered by ranking alone)
_CITE_DATED = re.compile(
    rf"{_ART}(?P<type>{_TYPE_RE})(?:\s+[^\s\d,;]+){{0,8}}?\s*,?\s+(?P<day>\d{{1,2}})[°]?\s+(?P<month>{_MONTH_RE})\s+(?P<year>\d{{4}})"
    rf"\s*,?\s*n\.?\s*{_NUM}", re.I)
#: "l. 306/2003", "d.lgs. n. 82 del 2005", "d.lgs. 82 del 7 marzo 2005"
_CITE_SHORT = re.compile(
    rf"{_ART}(?P<type>{_TYPE_RE})\s*n?\.?\s*{_NUM}\s*(?:/|del\s+(?:\d{{1,2}}\s+(?:{_MONTH_RE})\s+)?|,\s*)"
    rf"(?P<year>\d{{4}})", re.I)


def _type_token(text: str) -> str | None:
    for pattern, token in _TYPES:
        if re.fullmatch(pattern, text.strip(), re.I):
            return token
    return None


def citations(query: str) -> list[dict]:
    """The acts a text names: `{tipo, numero, anno, data?, art?}` each, in
    order of appearance, once — the dated form first where both name the
    same act, since it carries the date."""
    found: list[tuple[int, dict]] = []
    seen: set[tuple] = set()
    for pattern in (_CITE_DATED, _CITE_SHORT):
        for m in pattern.finditer(query):
            tipo = _type_token(m.group("type"))
            if tipo is None:
                continue
            data = None
            if pattern is _CITE_DATED:
                month = _MONTHS[m.group("month").lower()]
                data = f"{int(m.group('year')):04d}-{month:02d}-{int(m.group('day')):02d}"
            num = _WS.sub("", m.group("num").replace(" ", "")).lower()
            key = (tipo, num, int(m.group("year")))
            if key in seen:
                continue
            seen.add(key)
            found.append((m.start(), {
                "tipo": tipo, "numero": num, "anno": int(m.group("year")), "data": data,
                "art": _text(m.group("art")).rstrip(" ,") if m.group("art") else None}))
    return [c for _, c in sorted(found, key=lambda t: t[0])]


_index_lock = threading.Lock()
_index_cache: tuple | None = None       # (stamp, {(tipo, numero, anno): [rows…]})


def _stamp(refs: RefLookup):
    return tuple(sorted((r["source_id"], r["ref_version"], r["synced_at"]) for r in refs.rows("ref_meta")))


def _acts_index(refs: RefLookup) -> dict:
    """`atto` keyed by (tipo, numero, anno), built once per reference-data
    version — the gene resolver's reasoning (`_symbol_index`)."""
    global _index_cache
    try:
        stamp = _stamp(refs)
    except Exception:   # noqa: BLE001 — no ref_meta: correctness over speed
        stamp = None
    with _index_lock:
        cached = _index_cache
    if stamp is not None and cached is not None and cached[0] == stamp:
        return cached[1]
    index: dict = {}
    try:
        rows = refs.rows("atto")
    except Exception:   # noqa: BLE001 — a store without the table (never synced): no acts
        rows = []
    for r in rows:
        key = (r["tipo"], str(r["numero"] or "").lower(), r["anno"])
        index.setdefault(key, []).append(r)
        # every act also by number and year alone, whatever its form: a dated
        # citation whose form is not the origin's finds it by its date
        index.setdefault(("*",) + key[1:], []).append(r)
    if stamp is not None:
        with _index_lock:
            _index_cache = (stamp, index)
    return index


def invalidate_cache() -> None:
    global _index_cache
    with _index_lock:
        _index_cache = None


class CitationResolver:
    """Citation text → the acts named (`atto` rows), as entities whose id
    is the act's URN:NIR (`urn:nir:stato:legge:2003-10-31;306` — namespace
    `urn`); `expand` follows the declared links like any resolver (the
    composite in app/refs delegates it)."""

    def resolve(self, query: str, refs: RefLookup) -> list[Entity]:
        found = citations(query)
        if not found:
            return []
        index = _acts_index(refs)
        out: list[Entity] = []
        seen: set[str] = set()
        for c in found:
            rows = index.get((c["tipo"], c["numero"], c["anno"]), []) if c["tipo"] != "*" else []
            if c["data"]:
                dated = [r for r in rows if r.get("data") == c["data"]]
                rows = dated or rows
                if not rows:
                    # the form the citation gives is not the origin's (a
                    # "legge" that is a regio decreto-legge, a "testo unico"):
                    # date and number name one act whatever its form
                    rows = [r for r in index.get(("*", c["numero"], c["anno"]), [])
                            if r.get("data") == c["data"]]
            for r in rows:
                # the URN verbatim IS the canonical id (05 §5.2): namespace
                # `urn`, the origin's identifier after it; the table keys on
                # the whole of it (resolver.row_of)
                eid = r["urn"]
                if eid in seen:
                    continue
                seen.add(eid)
                label = r.get("denominazione") or r["urn"]
                if c["art"]:
                    label = f"{label} — {c['art']}"
                out.append(Entity(entity_id=eid, kind=ATTO.name, source_id=SOURCE_ID, label=label))
        return out
