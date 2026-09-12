"""Wikitext → sections of plain text, and everything else the page says.

`to_sections` strips markup, splits on top-level headings and drops the
boilerplate tails (References/External links/See also) that would pollute
retrieval with citation noise. That is still right FOR THE PROSE — but it
used to be the whole of this module, and under promise 9 (docs/01) it
cannot be: what `strip_code` removes is not packaging, it is most of what
an article asserts.

Measured on 3,000 non-redirect enwiki pages, every one of these was being
discarded:

    [[Category:…]]          93.0%      {{Authority control}}   64.9%
    <ref> citations         89.6%      {{Infobox …}}           51.4%
    {{cite …}}              89.0%      {{convert|…}} etc       35.2%
    {{Short description}}   86.1%      tables {| … |}          29.7%
    == External links ==    73.9%      {{Coord}}               13.1%

WHERE EACH GOES. `parse_all` returns the prose for `sections` and the
rest for `extra` — kept, not embedded. Promise 9 requires holding what
the origin said; it does not require chunking a bibliography or an
infobox into the vector space, where they would compete with the prose
they annotate. The one exception is an inline data template: "the river
is {{convert|5|km}} long" is a SENTENCE, and dropping the template left
"the river is  long", so those render in place.

WHAT IS NOT HERE, and why. `{{Authority control}}` looks like the perfect
source of §6.5 canonical ids (viaf:, isni:, lccn:) and carries NONE: 573
of 574 occurrences in an 800-page sample have zero parameters, because
the identifiers live in Wikidata and the template merely points at them.
Its PRESENCE is recorded and nothing more, since nothing more is there.
"""
import re

import mwparserfromhell

# section titles that carry citations/navigation, not content. Their text
# still leaves the PROSE — it is citation noise in a chunk — but it is
# kept in `extra.apparatus` rather than discarded (promise 9).
_BOILERPLATE = {"references", "external links", "see also", "further reading",
                 "notes", "sources", "bibliography", "footnotes"}
_WS = re.compile(r"\n{3,}")

#: templates whose value belongs IN the sentence around them. `strip_code`
#: deletes a template whole, so "is {{convert|5|km}} long" became "is
#: long" — a sentence the origin never wrote.
_INLINE_DATA = {"convert", "val", "nowrap", "nobr", "abbr", "frac", "sup",
                "sub", "as of", "birth date", "death date", "start date",
                "end date", "age", "formatnum", "lang"}


def _render_inline(code) -> None:
    """Replace inline data templates with their values, in place."""
    for tpl in list(code.filter_templates(recursive=True)):
        name = str(tpl.name).strip().lower()
        if name not in _INLINE_DATA:
            continue
        parts = [str(p.value).strip() for p in tpl.params if not p.showkey]
        try:
            code.replace(tpl, " ".join(p for p in parts if p))
        except ValueError:
            pass          # already replaced with an enclosing template


def _text(node) -> str:
    try:
        return str(node.strip_code(normalize=True, collapse=True)).strip()
    except AttributeError:
        return str(node).strip()


def _drop_refs(code) -> None:
    """Take `<ref>` OUT of the prose, contents and all.

    `strip_code` removes the tag but keeps what is inside it, so a
    citation was glued into the middle of the sentence it supports:
    "Northtown is a town.Smith, ''A History'', 1999. The river is…".
    That is not a sentence the origin wrote, and it went into the chunks
    and the vectors.

    Nothing is lost by removing it here — `references()` keeps every one
    of them, which is promise 9's whole shape: hold what the origin said,
    in the place where it belongs.
    """
    for tag in list(code.filter_tags(recursive=True)):
        try:
            if str(tag.tag).strip().lower() == "ref":
                code.remove(tag)
        except ValueError:
            pass          # nested inside a tag already removed


def to_sections(wikitext: str) -> list[tuple[str, str]]:
    """[(heading, plain_text)] — lead section first with heading ''."""
    code = mwparserfromhell.parse(wikitext)
    _drop_refs(code)
    _render_inline(code)
    sections = []
    for section in code.get_sections(include_lead=True, flat=True):
        headings = section.filter_headings()
        title = headings[0].title.strip_code().strip() if headings else ""
        if title.lower() in _BOILERPLATE:
            continue
        # STRIPPED FIRST, and that is the fix. `strip_code` returns the
        # body with a LEADING NEWLINE before the heading text, so
        # `body.startswith(title)` was False on every section and the
        # removal below never ran — each section's text began with a
        # repeat of its own heading, in every chunk, for every article.
        # The final `.strip()` hid it by tidying the newline away after
        # the test had already failed.
        body = section.strip_code(normalize=True, collapse=True).strip()
        if headings and body.startswith(title):
            body = body[len(title):].lstrip()
        body = _WS.sub("\n\n", body).strip()
        if body:
            sections.append((title, body))
    return sections


def categories(code) -> list[str]:
    """`[[Category:X|sortkey]]` → "X". The sortkey is a collation hint for
    the category page, not a second subject."""
    out = []
    for link in code.filter_wikilinks():
        title = str(link.title).strip()
        if title.lower().startswith("category:"):
            name = title.split(":", 1)[1].split("|")[0].strip()
            if name:
                out.append(name)
    return list(dict.fromkeys(out))


def infoboxes(code) -> list[dict]:
    """Every `{{Infobox …}}` as {template, fields}. Median 18 parameters
    on enwiki, and all of them were being dropped."""
    out = []
    for tpl in code.filter_templates():
        name = str(tpl.name).strip()
        if not name.lower().startswith("infobox"):
            continue
        fields = {}
        for prm in tpl.params:
            key = str(prm.name).strip()
            val = _text(prm.value)
            if key and val:
                fields[key] = val
        if fields:
            out.append({"template": name, "fields": fields})
    return out


def references(code) -> list[str]:
    """The content of every `<ref>`, as text. A citation is what the
    article says its claim rests on."""
    out = []
    for tag in code.filter_tags():
        if str(tag.tag).strip().lower() != "ref":
            continue
        body = _text(tag.contents) if tag.contents else ""
        if body:
            out.append(body)
    return list(dict.fromkeys(out))


def external_links(code) -> list[dict]:
    out = []
    for link in code.filter_external_links():
        url = str(link.url).strip()
        if url:
            out.append({"url": url, "title": _text(link.title) if link.title else ""})
    return out


def named_template(code, name: str) -> list[dict]:
    """Every instance of one template, with its parameters — positional
    ones keyed by their index as a string."""
    out = []
    want = name.strip().lower()
    for tpl in code.filter_templates():
        if str(tpl.name).strip().lower() != want:
            continue
        fields = {}
        for i, prm in enumerate(tpl.params, start=1):
            key = str(prm.name).strip() if prm.showkey else str(i)
            val = str(prm.value).strip()
            if val:
                fields[key] = val
        out.append({"raw": str(tpl), "fields": fields})
    return out


def apparatus(wikitext: str) -> dict[str, str]:
    """The sections `to_sections` leaves out of the prose — References,
    External links, See also and their kin — as text, so that dropping
    them from the CHUNKS does not drop them from the RECORD."""
    code = mwparserfromhell.parse(wikitext)
    out = {}
    for section in code.get_sections(include_lead=False, flat=True):
        headings = section.filter_headings()
        if not headings:
            continue
        title = headings[0].title.strip_code().strip()
        if title.lower() not in _BOILERPLATE:
            continue
        body = section.strip_code(normalize=True, collapse=True).strip()
        if body.startswith(title):
            body = body[len(title):].lstrip()
        body = _WS.sub("\n\n", body).strip()
        if body:
            out[title] = body
    return out


def parse_all(wikitext: str) -> dict:
    """Sections for the chunker, and everything else for `extra`."""
    code = mwparserfromhell.parse(wikitext)
    short = named_template(code, "short description")
    return {
        "sections": to_sections(wikitext),
        "categories": categories(code),
        "infoboxes": infoboxes(code),
        "references": references(code),
        "external_links": external_links(code),
        "short_description": short[0]["fields"].get("1", "") if short else "",
        "coordinates": [c["raw"] for c in named_template(code, "coord")],
        "apparatus": apparatus(wikitext),
        #: presence only — the identifiers are in Wikidata, not here
        "has_authority_control": bool(named_template(code, "authority control")),
    }


def is_redirect(wikitext: str) -> bool:
    return wikitext.lstrip()[:9].upper() == "#REDIRECT"
