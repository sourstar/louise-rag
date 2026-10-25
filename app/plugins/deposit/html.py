"""A web page for the deposit plugin (06 §6.7, amended 2026-10-25, operator:
"Keep all"): its text, and what the page says about itself.

A URL source's deposit is the page's bytes exactly as fetched, and the
record is parsed from them here — the same conversion the sources thread
ran before the page itself was kept (`to_text`, moved here unchanged from
app/hold/sources.py), so an unchanged page cuts the same chunks.

`to_text` keeps what is searched: script, style and the head's non-content
left out (09 §9.5's (a): markup). `head` keeps what the page ASSERTS about
itself (CLAUDE.md invariant 11): `<html lang>`, `<title>`, every `<meta>`
and every `<link>` with all their attributes as written, and every JSON-LD
block. The page itself is kept whole beside the record, so nothing is
lost even where a field here has no name.

The standard library's `html.parser`, as before — not a new dependency.
The standardised alternatives, for when they pay: `extruct` (W3C
microdata, RDFa, JSON-LD, Open Graph parsed into one structure) once the
record should interpret the page's structured data rather than keep it
as written; WARC (ISO 28500, `warcio`) as the stored form once the HTTP
response's headers and redirect chain must be kept too (app/hold/sources.py).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser

#: Decoding as the sources thread always has: UTF-8, an undecodable byte
#: replaced (never refused — the page is kept byte-exact whatever it is).
ENCODING = "utf-8"
ERRORS = "replace"


def decode(data: bytes) -> str:
    return data.decode(ENCODING, ERRORS)


class _Text(HTMLParser):
    """HTML as text: script, style and the head's non-content left out,
    block elements on their own lines. The standard library, not a new
    dependency — LocalRecall's html2text has no Python twin, and the text
    is what is searched, not a rendering."""
    _SKIP = {"script", "style", "noscript", "template", "svg", "head"}
    _BLOCK = {"p", "div", "br", "li", "ul", "ol", "tr", "table", "section",
              "article", "header", "footer", "h1", "h2", "h3", "h4", "h5", "h6",
              "pre", "blockquote", "hr", "title", "dd", "dt", "main", "nav"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = 0
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self._in_title = True
        if tag in self._SKIP:
            self.skip += 1
        if tag in self._BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag in self._SKIP and self.skip:
            self.skip -= 1
        if tag in self._BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if not self.skip:
            self.parts.append(data)


def to_text(html: str) -> str:
    """The page's text — byte for byte what app/hold/sources.py's
    `html_to_text` gave before 2026-10-25 (it is that function, moved)."""
    p = _Text()
    p.feed(html)
    p.close()
    lines = [re.sub(r"[ \t\r\f\v]+", " ", line).strip()
             for line in "".join(p.parts).split("\n")]
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    title = p.title.strip()
    return (f"{title}\n\n{text}" if title and not text.startswith(title) else text)


def page_text(data: bytes) -> str:
    """The text a page's bytes give: what the deposit held as `.txt`
    before the page was kept — decoded as the sources thread decoded it,
    and a leading byte-order mark dropped as a text deposit drops it."""
    return to_text(decode(data)).removeprefix("﻿")


# --------------------------------------------------------------------------
# what the page says about itself

@dataclass
class Head:
    lang: str | None = None                 # <html lang>, as written
    title: str | None = None                # the first <title> outside <svg>
    meta: list[dict[str, str | None]] = field(default_factory=list)
    links: list[dict[str, str | None]] = field(default_factory=list)
    jsonld: list[str] = field(default_factory=list)

    def named(self, *names: str) -> str | None:
        """The first `<meta>` whose name, property or itemprop is one of
        `names` (case-insensitive), in the page's order — its content."""
        wanted = {n.lower() for n in names}
        for m in self.meta:
            for attr in ("name", "property", "itemprop"):
                v = m.get(attr)
                if v and v.strip().lower() in wanted and m.get("content") is not None:
                    return m["content"]
        return None

    def link(self, rel: str) -> str | None:
        for ln in self.links:
            if rel in (ln.get("rel") or "").lower().split():
                return ln.get("href")
        return None


class _Head(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.head = Head()
        self._svg = 0
        self._title: list[str] | None = None
        self._jsonld: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "html" and self.head.lang is None:
            self.head.lang = a.get("lang") or a.get("xml:lang")
        elif tag == "svg":
            self._svg += 1
        elif tag == "title" and not self._svg and self.head.title is None:
            self._title = []
        elif tag == "meta":
            self.head.meta.append(a)
        elif tag == "link":
            self.head.links.append(a)
        elif tag == "script" and (a.get("type") or "").strip().lower() == "application/ld+json":
            self._jsonld = []

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag == "svg" and self._svg:
            self._svg -= 1

    def handle_endtag(self, tag):
        if tag == "svg" and self._svg:
            self._svg -= 1
        elif tag == "title" and self._title is not None:
            self.head.title = "".join(self._title)
            self._title = None
        elif tag == "script" and self._jsonld is not None:
            self.head.jsonld.append("".join(self._jsonld))
            self._jsonld = None

    def handle_data(self, data):
        if self._title is not None:
            self._title.append(data)
        if self._jsonld is not None:
            self._jsonld.append(data)


def head(data: bytes) -> Head:
    p = _Head()
    p.feed(decode(data))
    p.close()
    if p._title is not None and p.head.title is None:     # an unclosed <title>
        p.head.title = "".join(p._title)
    return p.head


_DATE = re.compile(r"^\s*(\d{4})(?:-(\d{2})(?:-(\d{2}))?)?")
_LANG = re.compile(r"^[A-Za-z]{2,3}$")


def date_of(value: str | None) -> str | None:
    """09 §9.5's `date` — `YYYY`, `YYYY-MM` or `YYYY-MM-DD` — from an ISO
    8601 value, as far as it goes."""
    m = _DATE.match(value or "")
    return "-".join(g for g in m.groups() if g) if m else None


def language_of(lang: str | None) -> str:
    """09 §9.5's `lang` from a BCP 47 tag: its primary subtag where that is
    an ISO 639 code (`it-IT` → `it`), `und` otherwise. The tag as written
    stays in `extra.html_lang`."""
    primary = (lang or "").strip().split("-")[0].split("_")[0]
    return primary.lower() if _LANG.match(primary) else "und"


def clean(value: str | None) -> str | None:
    """Whitespace runs to one space, trimmed; None when nothing is left."""
    text = re.sub(r"\s+", " ", value or "").strip()
    return text or None
