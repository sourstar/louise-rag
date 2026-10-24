"""An entity's text (docs/notes/wikidata-build-plan.md §2 "Why textification
is the PARSER's"): Wikimedia's textifier, vendored and adapted — see
VENDORED.md for the notice and the changes. `entity_text(obj, lang, labels)`
is the one entry point the parser uses per language of the Codex's list.

Adapted from WikidataTextifier (wmde, commit 0befd7c), BSD 3-Clause,
Copyright (c) 2025, Wikimedia Deutschland e. V.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from app.plugins.wikidata import format as fmt, names

#: upstream's `language_variables.json`, inlined; en for every other language
LANGUAGE_VARIABLES: dict[str, dict[str, str]] = {
    "en": {"also known as": "also known as", "Attributes include": "Attributes include", ", ": ", ",
           "has": "has", "Examples": "Examples"},
    "fr": {"also known as": "aussi connu sous le nom de", "Attributes include": "Ses attributs incluent", ", ": ", ",
           "has": "possède", "Examples": "Exemples"},
    "ar": {"also known as": "المعروف أيضًا باسم", "Attributes include": "السمات تتضمن", ", ": "، ",
           "has": "لديها", "Examples": "أمثلة"},
    "de": {"also known as": "auch bekannt als", "Attributes include": "Attribute sind", ", ": ", ",
           "has": "hat", "Examples": "Beispiele"},
    "es": {"also known as": "también conocido como", "Attributes include": "Sus atributos incluyen", ", ": ", ",
           "has": "tiene", "Examples": "Ejemplos"},
    "zh": {"also known as": "也称为", "Attributes include": "属性包括", ", ": "，", "has": "有", "Examples": "示例"},
    # OURS (parser 2, 2026-10-24), offered upstream: Italian, built as the
    # French and Spanish entries are (docs/notes/upstream-reports/wmde-wikidata-textifier.md §3)
    "it": {"also known as": "noto anche come", "Attributes include": "I suoi attributi includono", ", ": ", ",
           "has": "ha", "Examples": "Esempi"},
}
#: OURS (parser 2): Wikibase's own wording for the two special values
#: (`wikibase-snakview-variations-novalue-label` / `-somevalue-label`); a
#: language without them takes the English. Upstream rendered both as
#: "has <property>", which for `novalue` says the opposite of the statement
SPECIAL_VALUES: dict[str, dict[str, str]] = {
    "en": {"novalue": "no value", "somevalue": "unknown value"},
    "de": {"novalue": "kein Wert", "somevalue": "unbekannter Wert"},
    "fr": {"novalue": "aucune valeur", "somevalue": "valeur inconnue"},
    "es": {"novalue": "sin valor", "somevalue": "valor desconocido"},
    "it": {"novalue": "nessun valore", "somevalue": "valore sconosciuto"},
}

#: `labels(entity_id, lang) -> label or None`: the dump's label table
Labels = Callable[[str, str], Optional[str]]
#: rendered first: what the entity is an instance of — upstream's
#: `sticky_property_ids`, which every chunk of a long text repeats
STICKY_PROPERTIES = ("P31",)


# ---- the data classes (upstream's, `to_text` as theirs) -----------------------------------

@dataclass(slots=True)
class Text:
    text: Optional[str] = None

    def __str__(self) -> str:
        return self.text or ""

    def __bool__(self) -> bool:
        return bool(self.text)


@dataclass(slots=True)
class MonolingualText:
    text: Optional[str] = None
    lang: Optional[str] = None

    def __str__(self) -> str:
        return self.lang + ":" + self.text if self.lang and self.text else self.text or ""

    def __bool__(self) -> bool:
        return bool(self.text)


@dataclass(slots=True)
class Special:
    """OURS: a statement whose value is `novalue` ("no value") or `somevalue`
    ("unknown value") — information, rendered as Wikibase displays it."""
    kind: str

    def to_text(self, lang: str = "en") -> str:
        return SPECIAL_VALUES.get(lang, SPECIAL_VALUES["en"]).get(self.kind) or SPECIAL_VALUES["en"][self.kind]

    def __str__(self) -> str:
        return self.to_text("en")

    def __bool__(self) -> bool:
        return True


@dataclass(slots=True)
class Formatted:
    """A time, coordinate or quantity already rendered by format.py."""
    text: str

    def __str__(self) -> str:
        return self.text

    def __bool__(self) -> bool:
        return bool(self.text)


@dataclass(slots=True)
class Entity:
    id: str
    label: Optional[str] = None
    description: Optional[str] = None
    aliases: list[str] = field(default_factory=list)
    claims: list["Claim"] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.id) and self.label is not None and str(self.label) != ""

    def to_text(self, lang: str = "en") -> str:
        lang_var = LANGUAGE_VARIABLES.get(lang, LANGUAGE_VARIABLES["en"])
        # OURS: a referenced entity without a label in this language is its
        # id, not "<no label>" — the id is searchable, the placeholder is not
        label_str = str(self.label) if self.label else self.id
        string = label_str
        if self.description:
            string += f"{lang_var[', ']}{self.description}"
        if self.aliases:
            string += f"{lang_var[', ']}{lang_var['also known as']}"
            string += f" {lang_var[', '].join(map(str, self.aliases))}"
        attributes = [c.to_text(lang) for c in self.claims]
        attributes = [a for a in attributes if a]
        if attributes:
            string += f". {lang_var['Attributes include']}:\n- " + "\n- ".join(attributes)
        elif string != label_str:
            string += "."
        return string


@dataclass(slots=True)
class Claim:
    subject: Entity
    property: Entity
    values: list["ClaimValue"] = field(default_factory=list)
    datatype: str = "string"

    def __bool__(self) -> bool:
        return bool(self.property) and any(bool(v) for v in self.values)

    def to_text(self, lang: str = "en") -> str:
        lang_var = LANGUAGE_VARIABLES.get(lang, LANGUAGE_VARIABLES["en"])
        # upstream: a claim whose PROPERTY has no label is left out of the text
        if not bool(self.property):
            return ""
        if any(bool(v) for v in self.values):
            values = lang_var[", "].join(v.to_text(lang) for v in self.values if v)
            return f"{self.property.label}: {values}"
        return f"{lang_var['has']} {self.property.label}"


@dataclass(slots=True)
class ClaimValue:
    claim: Claim
    value: Any = None
    qualifiers: list[Claim] = field(default_factory=list)
    rank: Optional[str] = None

    def __bool__(self) -> bool:
        # OURS: a referenced entity is a value even without a label in this
        # language — it renders as its id (upstream dropped it with the claim)
        if isinstance(self.value, Entity):
            return bool(self.value.id)
        return bool(self.value)

    def to_text(self, lang: str = "en") -> str:
        lang_var = LANGUAGE_VARIABLES.get(lang, LANGUAGE_VARIABLES["en"])
        if not self:
            return ""
        s = self.value.to_text(lang) if isinstance(self.value, (Entity, Special)) else str(self.value)
        if self.rank == "deprecated":
            s += " [deprecated]"
        qs = [q.to_text(lang) for q in self.qualifiers]
        qs = [q for q in qs if q]
        if qs:
            s += f" ({lang_var[', '].join(qs)})"
        return s


# ---- the normaliser (upstream's shape; the dump's labels, our formatter) -------------------

def _lang_val(d: Any, lang: str, fallback: str | None) -> str:
    """Upstream `get_lang_val`: the language, else `mul`, else the fallback."""
    if not isinstance(d, dict):
        return ""
    v = d.get(lang, d.get("mul", {}))
    if fallback and not v:
        v = d.get(fallback, {})
    if isinstance(v, str):
        return v
    return (v or {}).get("value", "") if isinstance(v, dict) else ""


def _filter_by_rank(statements: list[dict]) -> list[dict]:
    """Upstream: where a property has a preferred statement, only the
    preferred ones; else the normal ones; deprecated never (all_ranks False)."""
    has_preferred = any(isinstance(s, dict) and s.get("rank") == "preferred" for s in statements)
    kept = []
    for s in statements:
        if not isinstance(s, dict):
            continue
        r = s.get("rank")
        if r is None or (has_preferred and r == "preferred") or (not has_preferred and r == "normal"):
            kept.append(s)
    return kept


def _datatype(statements: list[dict]) -> str:
    for st in statements:
        if isinstance(st, dict):
            ms = st.get("mainsnak", st)
            if isinstance(ms, dict) and isinstance(ms.get("datatype"), str) and ms["datatype"]:
                return ms["datatype"]
    return "string"


class Normalizer:
    """Entity JSON (the dump's line, parsed) → the `Entity` tree, for one
    language. `labels` answers every referenced id from the dump's label
    table; `fallback` is the language tried when `lang` has none."""

    def __init__(self, *, lang: str = "en", fallback: str | None = "en", labels: Labels,
                 external_ids: bool = True, qualifiers: bool = True, codes: dict[str, str] | None = None):
        self.lang, self.fallback, self.labels = lang, fallback, labels
        self.codes = codes                     # language item → code, for names.py (the pack's table)
        self.external_ids, self.qualifiers = external_ids, qualifiers
        # upstream's `get_lang_val` chain, the one `_lang_val` applies to the
        # entity's own label: the language, else `mul`, else the fallback.
        # Parser 1 skipped `mul` here, so a referenced entity labelled only in
        # `mul` (Wikidata's default label since 2024) rendered as its id
        self._chain = tuple(dict.fromkeys(x for x in (lang, "mul", fallback) if x))

    def _label(self, entity_id: str) -> Optional[str]:
        for lang in self._chain:
            got = self.labels(entity_id, lang)
            if got:
                return got
        # OURS (parser 2, operator 2026-10-24: "showing an id is meaningless
        # for humans"): the entity's name by names.py's order, which the label
        # table keeps for every entity with no English and no mul label
        return self.labels(entity_id, names.NAME) or None

    def normalize(self, e: dict) -> Entity:
        label = _lang_val(e.get("labels"), self.lang, self.fallback)
        if not label:
            # OURS (parser 2): a name by the operator's order, never the id
            # while the entity has any label or name statement (names.py)
            got = names.display_name(e, self.codes)
            label = got[1] if got else ""
        description = _lang_val(e.get("descriptions"), self.lang, self.fallback)
        aliases_in = e.get("aliases") or {}
        raw = (aliases_in.get(self.lang) or []) + (aliases_in.get("mul") or [])
        # OURS: the dump's order, deduplicated — upstream's `list({...})` was unordered
        aliases = list(dict.fromkeys(a.get("value") if isinstance(a, dict) else str(a) for a in raw if a))
        entity = Entity(id=str(e.get("id") or ""), label=label or None, description=description or None,
                        aliases=[a for a in aliases if a])
        claims_out: list[Claim] = []
        for pid, statements in (e.get("claims") or {}).items():
            if not (isinstance(pid, str) and pid.startswith("P") and isinstance(statements, list)):
                continue
            claim = self._claim(entity, pid, statements)
            if claim is not None and claim.values:
                claims_out.append(claim)
        # OURS: `instance of` first, the rest in the dump's order — the line the
        # chunker keeps in every window (the STICKY claim of Wikimedia's
        # chunk_item_text), found by its place rather than by a label
        claims_out.sort(key=lambda c: 0 if c.property.id in STICKY_PROPERTIES else 1)
        entity.claims = claims_out
        return entity

    def _claim(self, subject: Entity, pid: str, statements: list[dict]) -> Optional[Claim]:
        datatype = _datatype(statements)
        if not self.external_ids and datatype == "external-id":
            return None
        claim = Claim(subject=subject, property=Entity(id=pid, label=self._label(pid)), datatype=datatype)
        values = []
        for st in _filter_by_rank(statements):
            cv = self._claim_value(claim, st, datatype)
            if cv is not None:
                values.append(cv)
        claim.values = values
        return claim

    def _claim_value(self, claim: Claim, st: dict, datatype: str) -> Optional[ClaimValue]:
        mainsnak = st.get("mainsnak", st)
        if not isinstance(mainsnak, dict):
            return None
        snaktype = mainsnak.get("snaktype", "value")
        if snaktype in ("somevalue", "novalue"):
            # OURS: the special value as Wikibase shows it, its qualifiers kept
            # (earliest/latest date bound an unknown date); upstream wrote
            # "has <property>" and dropped the qualifiers
            value: Any = Special(snaktype)
        elif snaktype != "value":
            return None
        else:
            value = self._value(datatype, mainsnak.get("datavalue"))
        quals: list[Claim] = []
        if self.qualifiers:
            for qpid, snaks in (st.get("qualifiers") or {}).items():
                if isinstance(qpid, str) and qpid.startswith("P") and isinstance(snaks, list):
                    quals.append(self._snak_claim(qpid, snaks))
        return ClaimValue(claim=claim, value=value, qualifiers=quals, rank=st.get("rank"))

    def _snak_claim(self, pid: str, snaks: list[dict]) -> Claim:
        datatype = next((s["datatype"] for s in snaks
                         if isinstance(s, dict) and isinstance(s.get("datatype"), str) and s["datatype"]), "string")
        claim = Claim(subject=Entity(id="<qualifier>"), property=Entity(id=pid, label=self._label(pid)),
                      datatype=datatype)
        vals = []
        for snak in snaks:
            if not isinstance(snak, dict):
                continue
            snaktype = snak.get("snaktype", "value")
            if snaktype != "value":
                vals.append(ClaimValue(claim=claim, value=Special(snaktype) if snaktype in ("somevalue", "novalue")
                                       else None))
                continue
            vals.append(ClaimValue(claim=claim, value=self._value(datatype, snak.get("datavalue"))))
        claim.values = vals
        return claim

    def _value(self, datatype: str, dv: Any) -> Any:
        if not isinstance(dv, dict):
            return None
        t, v = dv.get("type"), dv.get("value")
        if t == "wikibase-entityid" or datatype in ("wikibase-item", "wikibase-property"):
            eid = v.get("id") if isinstance(v, dict) else None
            if not isinstance(eid, str) and isinstance(v, dict) and isinstance(v.get("numeric-id"), int):
                eid = {"item": "Q", "property": "P", "lexeme": "L"}.get(v.get("entity-type"), "") + str(v["numeric-id"])
            if isinstance(eid, str) and eid[:1] in ("Q", "P") and eid[1:].isdigit():
                return Entity(id=eid, label=self._label(eid))
            # OURS: a lexeme, form or sense (L7, L7-F2) is its id; upstream
            # wrote the Python repr of the datavalue into the text
            return Text(text=eid) if isinstance(eid, str) and eid else None
        if t == "time" or datatype == "time":
            if not isinstance(v, dict):
                return None
            try:
                return Formatted(fmt.time_text(v, self.lang))
            except fmt.FormatError:
                return Text(text=str(v.get("time") or ""))
        if t == "quantity" or datatype == "quantity":
            if not isinstance(v, dict):
                return None
            try:
                return Formatted(fmt.quantity_text(v, unit_label=self._label))
            except fmt.FormatError:
                return None
        if t == "globecoordinate" or datatype == "globe-coordinate":
            if not isinstance(v, dict):
                return None
            try:
                return Formatted(fmt.coordinate_text(v, self.lang, globe_label=self._label))
            except fmt.FormatError:
                return None
        if t == "monolingualtext" or datatype == "monolingualtext":
            if not isinstance(v, dict):
                return None
            return MonolingualText(text=str(v.get("text") or ""), lang=v.get("language"))
        return Text(text=str(v) if v is not None else None)


def entity_text(obj: dict, lang: str, labels: Labels, *, fallback: str | None = "en",
                codes: dict[str, str] | None = None) -> str:
    """The text of one dump entity in `lang`: upstream's `format=text`, with
    the dump's labels and our formatting — a pure function of (entity,
    label table, language codes, lang)."""
    return Normalizer(lang=lang, fallback=fallback, labels=labels, codes=codes).normalize(obj).to_text(lang)
