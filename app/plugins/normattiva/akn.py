"""Akoma Ntoso as Normattiva serves it → an `Act`: the act's structure, its
text, and everything its metadata asserts.

Written against the LIVE data, not the 2021 adoption profile (a census of
the 40 acts of the "Codici" collection, 2026-09-14), which differs from
the profile in ways that matter here:

  * a partition above the article is ALWAYS a flat `<chapter>` — PARTE,
    TITOLO, CAPO and SEZIONE all arrive as chapters, the level carried by
    the words in `num`/`heading` ("PARTE I", or num "-" and a heading
    "TITOLO II …"), never by nesting;
  * an article's notes sit in a `<section>` inside the `<article>`; they
    are the administration's editorial notes, not the law's text;
  * annexes are `<attachment><doc name="Allegato I">` with their own FRBR
    metadata and a `<mainBody>` of paragraphs;
  * amended text is wrapped in `((…))` in the text itself (38,109 in the
    40 codes), and an article repealed in place reads
    "((ARTICOLO ABROGATO DAL …))" (839 articles) — the rendition's
    business, so this module keeps the text verbatim;
  * TWO MEMBERS OF THE ZIP ARE BASE64 of the XML, not XML — the Codice
    civile among them. `decode_member` takes either.

Nothing is dropped here: what is not structure or text is metadata, kept
in `Act` for the record; what this module does not recognise is kept as
its XML (`meta_other`) or as text (a pseudo-article), never skipped.
"""
import base64
import binascii
import re
from dataclasses import dataclass, field
from xml.etree import ElementTree as ET

A = "{http://docs.oasis-open.org/legaldocml/ns/akn/3.0}"
NAKN = "{http://normattiva.it/akn/vocabulary}"
NRDFA = "{http://www.normattiva.it/rdfa/}"


class AknError(ValueError):
    """The member is not an Akoma Ntoso act this module can read."""


def decode_member(data: bytes) -> tuple[bytes, str]:
    """`(xml, "xml" | "base64")`. Two of the 40 "Codici" members — the Codice
    civile (R.D. 262/1942) and R.D. 318/1942 — are stored as base64 of the
    XML; read as XML they fail at line 1, column 0."""
    head = data.lstrip()[:4]
    if head.startswith(b"<") or head.startswith(b"\xef\xbb\xbf"):
        return data, "xml"
    try:
        decoded = base64.b64decode(data, validate=False)
    except (ValueError, binascii.Error) as e:
        raise AknError(f"neither XML nor base64: {e}") from e
    if decoded.lstrip()[:1] == b"<" or decoded.startswith(b"\xef\xbb\xbf"):
        return decoded, "base64"
    raise AknError("neither XML nor base64-encoded XML")


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


_SPACES = re.compile(r"[ \t\r\f\v ]+")


def clean(text: str) -> str:
    """Runs of spaces to one, every line trimmed, blank lines dropped. A
    newline INSIDE a `<p>` is the origin's own break (a second sentence
    block of one comma) and stays."""
    lines = (_SPACES.sub(" ", line).strip() for line in text.split("\n"))
    return "\n".join(line for line in lines if line)


@dataclass
class Block:
    """One unit of text in reading order. `num` is the unit's own number as
    written ("1.", "2-bis.", "a)"); `depth` 0 is a comma, 1 a lettera, 2 a
    numero. `kind` "table" rows are joined " | "."""
    num: str
    text: str
    depth: int = 0
    kind: str = "text"


@dataclass
class Article:
    eid: str
    num: str
    heading: str
    partition: str                      # the enclosing chapter's label, or ""
    blocks: list[Block] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class Attachment:
    name: str
    frbr_this: str
    blocks: list[Block] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class Act:
    act_name: str
    frbr: dict                          # work / expression / manifestation
    rdfa: dict                          # level -> [[property, value], …]
    publication: dict
    lifecycle: list
    workflow: list
    active_modifications: list
    passive_modifications: list
    references: list
    meta_other: list                    # XML of meta children not read above
    doc_type: str
    doc_date: str
    doc_date_text: str
    doc_number: str
    doc_title: str
    preface_notes: list
    preamble: list
    articles: list[Article]
    attachments: list[Attachment]
    conclusions: str
    cites: list                         # every <ref href>, reading order, once


class _Ctx:
    """What a walk collects besides blocks: the notes of the unit being
    walked, and every reference in the act."""

    def __init__(self, cites: list, seen: set) -> None:
        self.notes: list[str] = []
        self.cites = cites
        self._seen = seen

    def cite(self, href: str | None) -> None:
        if href and href not in self._seen:
            self._seen.add(href)
            self.cites.append(href)


def _inline(el, ctx: _Ctx) -> str:
    """The text of an inline container, `<ref>` text kept (its target
    noted), an `<authorialNote>` taken out to the notes."""
    parts = [el.text or ""]
    for ch in el:
        t = _local(ch.tag)
        if t == "authorialNote":
            note = clean(_inline(ch, ctx))
            if note:
                ctx.notes.append(note)
        elif t == "table":
            parts.append("\n" + _table(ch, ctx) + "\n")
        else:
            if t == "ref":
                ctx.cite(ch.get("href"))
            parts.append(_inline(ch, ctx))
        parts.append(ch.tail or "")
    return "".join(parts)


def _table(el, ctx: _Ctx) -> str:
    rows = []
    for tr in el.iter(A + "tr"):
        cells = [clean(_inline(c, ctx)).replace("\n", " ")
                 for c in tr if _local(c.tag) in ("td", "th")]
        if any(cells):
            rows.append(" | ".join(cells))
    cap = el.find(A + "caption")
    head = [clean(_inline(cap, ctx))] if cap is not None else []
    return "\n".join(head + rows)


def _num(el) -> str:
    n = el.find(A + "num")
    return clean("".join(n.itertext())) if n is not None else ""


def _heading(el, ctx: _Ctx) -> str:
    h = el.find(A + "heading")
    return clean(_inline(h, ctx)).replace("\n", " ") if h is not None else ""


#: an element whose children are the numbered sub-units of a list
_POINTS = {"point", "item", "indent"}
#: containers of a unit's own prose
_PROSE = {"content", "intro", "wrapUp", "alinea", "wrap"}


def _walk(el, depth: int, ctx: _Ctx, out: list[Block], num: str = "") -> None:
    pending = num
    for ch in el:
        t = _local(ch.tag)
        if t in ("num", "heading", "subheading"):
            continue
        if t in _PROSE:
            for piece in _prose(ch, ctx):
                out.append(Block(pending, piece[1], depth, piece[0]))
                pending = ""
        elif t in _POINTS:
            _walk(ch, depth + 1, ctx, out, _num(ch))
        elif t in ("list", "blockList"):
            _walk(ch, depth, ctx, out, pending)
            pending = ""
        elif t == "section":            # an article's notes (census: 518)
            note = clean(_inline(ch, ctx))
            if note:
                ctx.notes.append(note)
        elif t == "table":
            out.append(Block(pending, _table(ch, ctx), depth, "table"))
            pending = ""
        elif t == "p":
            text = clean(_inline(ch, ctx))
            if text or pending:
                out.append(Block(pending, text, depth))
                pending = ""
        elif t == "authorialNote":
            note = clean(_inline(ch, ctx))
            if note:
                ctx.notes.append(note)
        elif t in ("article",):         # an article inside an annex
            head = " ".join(x for x in (_num(ch), _heading(ch, ctx)) if x)
            if head:
                out.append(Block("", head, depth))
            _walk(ch, depth, ctx, out)
        elif len(ch):                   # paragraph, subparagraph, hcontainer…
            _walk(ch, depth, ctx, out, _num(ch))
        else:
            text = clean(_inline(ch, ctx))
            if text:
                out.append(Block("", text, depth))
    if pending:
        out.append(Block(pending, "", depth))


def _prose(el, ctx: _Ctx) -> list[tuple[str, str]]:
    """`[(kind, text)]` of a content/intro: one entry per `<p>` or table."""
    out = []
    loose = clean(el.text or "")
    if loose:
        out.append(("text", loose))
    for ch in el:
        t = _local(ch.tag)
        if t == "table":
            out.append(("table", _table(ch, ctx)))
        elif t == "authorialNote":
            note = clean(_inline(ch, ctx))
            if note:
                ctx.notes.append(note)
        else:
            text = clean(_inline(ch, ctx))
            if text:
                out.append(("text", text))
        tail = clean(ch.tail or "")
        if tail:
            out.append(("text", tail))
    return out


def _partition_label(ch, ctx: _Ctx) -> str:
    """"PARTE I — AMBITO DI APPLICAZIONE…"; a num of "-" (the census: the
    level word moved into the heading) is no number."""
    num = _num(ch)
    head = _heading(ch, ctx)
    num = "" if num.strip("- ") == "" else num
    return " — ".join(x for x in (num, head) if x)


def _article(el, partition: str, ctx: _Ctx) -> Article:
    ctx.notes = []
    art = Article(eid=el.get("eId") or "", num=_num(el), heading=_heading(el, ctx),
                  partition=partition)
    _walk(el, 0, ctx, art.blocks)
    art.notes = ctx.notes
    return art


def _body(body, ctx: _Ctx, path: str = "") -> list[Article]:
    out: list[Article] = []
    loose: list = []

    def flush_loose() -> None:
        if loose:
            ctx.notes = []
            art = Article(eid="", num="", heading="", partition=path)
            for el in loose:
                _walk_one(el, ctx, art.blocks)
            art.notes = ctx.notes
            if art.blocks or art.notes:
                out.append(art)
            loose.clear()

    for ch in body:
        t = _local(ch.tag)
        if t == "article":
            flush_loose()
            out.append(_article(ch, path, ctx))
        elif t in ("chapter", "book", "part", "title", "subtitle", "section",
                   "subsection", "division", "tome", "hcontainer"):
            flush_loose()
            label = _partition_label(ch, ctx)
            inner = " › ".join(x for x in (path, label) if x)
            out.extend(_body(ch, ctx, inner))
        elif t in ("num", "heading", "subheading"):
            continue
        else:
            loose.append(ch)
    flush_loose()
    return out


def _walk_one(el, ctx: _Ctx, out: list[Block]) -> None:
    holder = ET.Element("holder")
    holder.append(el)
    _walk(holder, 0, ctx, out)


def _frbr(ident) -> tuple[dict, dict]:
    frbr, rdfa = {}, {}
    if ident is None:
        return frbr, rdfa
    for level, key in (("FRBRWork", "work"), ("FRBRExpression", "expression"),
                       ("FRBRManifestation", "manifestation")):
        el = ident.find(A + level)
        if el is None:
            continue
        d: dict = {"aliases": {}}
        for ch in el:
            t = _local(ch.tag)
            if t == "FRBRalias":
                d["aliases"][ch.get("name") or ""] = ch.get("value") or ""
            elif t == "FRBRthis":
                d["this"] = ch.get("value") or ""
            elif t == "FRBRuri":
                d["uri"] = ch.get("value") or ""
            elif t == "FRBRdate":
                d["date"] = ch.get("date") or ""
                if ch.get("name"):
                    d["date_name"] = ch.get("name")
            elif t == "FRBRauthor":
                d["author"] = {k: v for k, v in ch.attrib.items() if v}
            elif t == "FRBRcountry":
                d["country"] = ch.get("value") or ""
            elif t == "FRBRlanguage":
                d["language"] = ch.get("language") or ""
            elif t == "preservation":
                for eli in ch:
                    spans = [[s.get("property") or "", s.get("content") if s.get("content")
                              is not None else (s.get("resource") or "")]
                             for s in eli.iter(NRDFA + "span")]
                    rdfa[key] = {"about": eli.get("about") or "", "spans": spans}
            else:
                d.setdefault("other", []).append(ET.tostring(ch, encoding="unicode"))
        if not d["aliases"]:
            del d["aliases"]
        frbr[key] = d
    return frbr, rdfa


def _mods(el) -> list[dict]:
    out = []
    for tm in el:
        d = {"eId": tm.get("eId") or "", "type": tm.get("type") or ""}
        for part in tm:
            t = _local(part.tag)
            if t in ("source", "destination"):
                d[t] = part.get("href") or ""
                if part.get("pos"):
                    d[t + "_pos"] = part.get("pos")
            elif t in ("new", "old"):
                d[t] = clean(" ".join(part.itertext())) or (part.get("href") or "")
            else:
                d.setdefault("other", []).append(ET.tostring(part, encoding="unicode"))
        out.append(d)
    return out


def parse(xml: bytes) -> Act:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as e:
        raise AknError(f"not well-formed XML: {e}") from e
    act = root.find(A + "act")
    if act is None:
        raise AknError(f"no <act> under <{_local(root.tag)}>")
    cites: list = []
    ctx = _Ctx(cites, set())

    meta = act.find(A + "meta")
    frbr, rdfa = _frbr(meta.find(A + "identification") if meta is not None else None)
    publication: dict = {}
    lifecycle, workflow, references, meta_other = [], [], [], []
    active, passive = [], []
    for ch in (meta if meta is not None else []):
        t = _local(ch.tag)
        if t == "identification":
            continue
        if t == "publication":
            publication = dict(ch.attrib)
        elif t == "lifecycle":
            lifecycle = [dict(e.attrib) for e in ch]
        elif t == "workflow":
            workflow = [dict(e.attrib) for e in ch]
        elif t == "references":
            references = [{"kind": _local(e.tag), **e.attrib} for e in ch]
        elif t == "analysis":
            for part in ch:
                pt = _local(part.tag)
                if pt == "activeModifications":
                    active = _mods(part)
                elif pt == "passiveModifications":
                    passive = _mods(part)
                else:
                    meta_other.append(ET.tostring(part, encoding="unicode"))
        else:
            meta_other.append(ET.tostring(ch, encoding="unicode"))

    doc_type = doc_date = doc_date_text = doc_number = doc_title = ""
    preface_notes: list = []
    preface = act.find(A + "preface")
    if preface is not None:
        ctx.notes = []
        for el in preface.iter():
            t = _local(el.tag)
            if t == "docType":
                doc_type = clean("".join(el.itertext()))
            elif t == "docDate":
                doc_date = el.get("date") or ""
                doc_date_text = clean("".join(el.itertext()))
            elif t == "docNumber":
                doc_number = clean("".join(el.itertext()))
            elif t == "docTitle":
                doc_title = clean(_inline(el, ctx)).replace("\n", " ")
            elif t == "authorialNote":
                note = clean(_inline(el, _Ctx(cites, set(cites))))
                if note:
                    preface_notes.append(note)

    preamble = []
    pre = act.find(A + "preamble")
    if pre is not None:
        for ch in pre:
            text = clean(_inline(ch, ctx))
            if text:
                preamble.append({"kind": _local(ch.tag), "name": ch.get("name") or "",
                                 "text": text})

    body = act.find(A + "body")
    articles = _body(body, ctx) if body is not None else []

    attachments = []
    for att in act.iter(A + "attachment"):
        doc = att.find(A + "doc")
        if doc is None:
            continue
        this = ""
        for fw in doc.iter(A + "FRBRWork"):
            t = fw.find(A + "FRBRthis")
            this = (t.get("value") or "") if t is not None else ""
            break
        ctx.notes = []
        item = Attachment(name=doc.get("name") or "", frbr_this=this)
        main = doc.find(A + "mainBody")
        if main is not None:
            _walk(main, 0, ctx, item.blocks)
        item.notes = ctx.notes
        attachments.append(item)

    concl = act.find(A + "conclusions")
    conclusions = clean(_inline(concl, ctx)) if concl is not None else ""

    return Act(
        act_name=act.get("name") or "", frbr=frbr, rdfa=rdfa, publication=publication,
        lifecycle=lifecycle, workflow=workflow, active_modifications=active,
        passive_modifications=passive, references=references, meta_other=meta_other,
        doc_type=doc_type, doc_date=doc_date, doc_date_text=doc_date_text,
        doc_number=doc_number, doc_title=doc_title, preface_notes=preface_notes,
        preamble=preamble, articles=articles, attachments=attachments,
        conclusions=conclusions, cites=cites)
