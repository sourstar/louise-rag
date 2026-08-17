"""Wikitext → sections of plain text, via mwparserfromhell.

Kept deliberately small: strip markup, split on top-level headings, drop the
boilerplate tails (References/External links/See also) that would pollute
retrieval with citation noise. Anything fancier (infobox extraction, table
flattening) is a later, versioned cleaning change — CLEANING_VERSION exists
precisely so this module can evolve without silently corrupting chunk ids.
"""
import re

import mwparserfromhell

# section titles that carry citations/navigation, not content
_BOILERPLATE = {"references", "external links", "see also", "further reading",
                 "notes", "sources", "bibliography", "footnotes"}
_WS = re.compile(r"\n{3,}")


def to_sections(wikitext: str) -> list[tuple[str, str]]:
    """[(heading, plain_text)] — lead section first with heading ''."""
    code = mwparserfromhell.parse(wikitext)
    sections = []
    for section in code.get_sections(include_lead=True, flat=True):
        headings = section.filter_headings()
        title = headings[0].title.strip_code().strip() if headings else ""
        if title.lower() in _BOILERPLATE:
            continue
        body = section.strip_code(normalize=True, collapse=True)
        if headings:
            # strip_code keeps the heading text at the top of the body
            body = body[len(title):].lstrip() if body.startswith(title) else body
        body = _WS.sub("\n\n", body).strip()
        if body:
            sections.append((title, body))
    return sections


def is_redirect(wikitext: str) -> bool:
    return wikitext.lstrip()[:9].upper() == "#REDIRECT"
