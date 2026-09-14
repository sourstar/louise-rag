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
from mwparserfromhell.nodes import Text

# section titles that carry citations/navigation, not content. Their text
# still leaves the PROSE — it is citation noise in a chunk — but it is
# kept in `extra.apparatus` rather than discarded (promise 9).
_BOILERPLATE = {"references", "external links", "see also", "further reading",
                 "notes", "sources", "bibliography", "footnotes"}
_WS = re.compile(r"\n{3,}")

#: templates whose value belongs IN the sentence around them. `strip_code`
#: deletes a template whole, so "is {{convert|5|km}} long" became "is
#: long" — a sentence the origin never wrote.
_INLINE_DATA = {"convert", "cvt", "val", "nowrap", "nobr", "abbr", "frac", "sup",
                "sub", "as of", "birth date", "death date", "start date",
                "end date", "age", "formatnum", "lang", "langx", "transliteration",
                "transl", "circa", "c.", "nbsp", "snd", "spaced ndash", "ndash",
                "mdash", "ill", "interlanguage link", "ipa", "ipac-en", "ipa-en",
                "math", "mvar", "chem2"}
#: quotations: their text is the article's, and `strip_code` deleted it
#: (608 instances on 205 of 1,132 sampled enwiki articles, parser 3)
_QUOTES = {"quote", "blockquote", "cquote", "quotation", "quote box"}


def _render(name: str, positional: list[str], named: dict[str, str]) -> str:
    """What an inline template reads as in the sentence. Parser 2 joined
    every positional argument, so `{{lang|fr|texte}}` read "fr texte" and
    `{{convert|15|C|F}}` "15 C F" (the target unit is the RENDERING's)."""
    if name in ("lang", "transliteration", "transl") or name.startswith("lang-"):
        return positional[-1] if positional else ""
    if name == "langx":
        return positional[-1] if len(positional) >= 2 else "".join(positional)
    if name in ("convert", "cvt"):
        return " ".join(positional[:-1] if len(positional) == 3 else positional)
    if name in ("circa", "c."):
        return f"c. {positional[0]}" if positional else "c."
    if name == "as of":
        return "As of " + " ".join(positional)
    if name == "nbsp":
        return " "
    if name in ("snd", "spaced ndash"):
        return " – "
    if name == "ndash":
        return "–"
    if name == "mdash":
        return "—"
    if name in ("ill", "interlanguage link"):
        return positional[0] if positional else ""
    if name == "ipac-en":
        return "".join(positional)
    if name == "frac":
        return (f"{positional[0]} {positional[1]}/{positional[2]}" if len(positional) == 3
                else "/".join(positional))
    return " ".join(positional)


def _render_inline(code) -> None:
    """Replace inline data templates with their values, and quotations with
    their text as a paragraph of its own, in place."""
    for tpl in list(code.filter_templates(recursive=True)):
        name = str(tpl.name).strip().lower()
        if name not in _INLINE_DATA and name not in _QUOTES \
                and not name.startswith("lang-"):
            continue
        positional = [str(p.value).strip() for p in tpl.params if not p.showkey]
        positional = [p for p in positional if p]
        named = {str(p.name).strip().lower(): str(p.value).strip()
                 for p in tpl.params if p.showkey}
        if name in _QUOTES:
            said = named.get("text") or named.get("quote") or (positional[0] if positional else "")
            who = named.get("author") or named.get("sign") or \
                (positional[1] if len(positional) > 1 else "")
            value = f"\n\n{said}" + (f" — {who}" if who else "") + "\n\n" if said else ""
        else:
            value = _render(name, positional, named)
        try:
            code.replace(tpl, value)
        except ValueError:
            pass          # already replaced with an enclosing template


#: Image options a caption is not: the rendering's size, frame and float.
_IMG_OPTION = re.compile(
    r"^(thumb|thumbnail|frame|framed|frameless|border|left|right|center|centre|none|"
    r"upright(=[\d.]+)?|baseline|middle|sub|super|text-top|text-bottom|top|bottom|"
    r"\d*x?\d+px|(alt|link|class|lang|page|thumbtime|start|end)=.*)$", re.I)
_FILE_NS = ("file:", "image:")
#: `__TOC__`, `__NOTOC__`, `__NOEDITSECTION__`: behaviour switches, not text
_MAGIC = re.compile(r"__[A-Z]{3,}__")
#: an interlanguage link: a language code, a colon, a title — and no text
_INTERLANG = re.compile(r"^([a-z]{2,3}(?:-[a-z0-9]+)*):(.+)$")


def _split_top(text: str) -> list[str]:
    """Split a link's text at its OWN pipes, not at those inside a nested
    [[link|text]] or {{template|…}} in a caption."""
    parts, depth, cur, i = [], 0, [], 0
    while i < len(text):
        two = text[i:i + 2]
        if two in ("[[", "{{"):
            depth += 1
            cur.append(two)
            i += 2
            continue
        if two in ("]]", "}}"):
            depth = max(0, depth - 1)
            cur.append(two)
            i += 2
            continue
        if text[i] == "|" and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(text[i])
        i += 1
    parts.append("".join(cur))
    return parts


def _take_images(code) -> list[dict]:
    """Every `[[File:…]]` and `<gallery>` entry: the file and its caption.
    The file name was dropped, and the link's OPTIONS leaked into the prose
    ("thumb|180px|right|Spring flowers…" on 797 of 1,132 sampled enwiki
    articles, parser 3). The caption stays in the prose as a paragraph —
    it is the article's text — and the options go."""
    out = []
    for link in list(code.filter_wikilinks(recursive=True)):
        title = str(link.title).strip()
        if not title.lower().startswith(_FILE_NS):
            continue
        parts = _split_top(str(link.text)) if link.text is not None else []
        caption = next((p.strip() for p in reversed(parts)
                        if p.strip() and not _IMG_OPTION.match(p.strip())), "")
        cap = ""
        if caption:
            cc = mwparserfromhell.parse(caption)
            _render_inline(cc)
            cap = _text(cc)
        out.append({"file": title.split(":", 1)[1].strip(), "caption": cap})
        try:
            code.replace(link, f"\n\n{cap}\n\n" if cap else "")
        except ValueError:
            pass
    for tag in list(code.filter_tags(recursive=True)):
        if str(tag.tag).strip().lower() != "gallery" or tag.contents is None:
            continue
        for line in str(tag.contents).splitlines():
            name, _, cap = line.strip().partition("|")
            if name:
                name = name.split(":", 1)[1] if name.lower().startswith(_FILE_NS) else name
                out.append({"file": name.strip(), "caption": _text(mwparserfromhell.parse(cap)),
                            "gallery": True})
    return out


def _take_links(code) -> list[str]:
    """Category and interlanguage links OUT of the prose. `strip_code` left
    "Category:X" (or its sort key) and "de:Logdatei" in the text (44/469 and
    1/4 of sampled pages); both are kept elsewhere — categories as
    `subjects`, interlanguage links returned here."""
    inter = []
    for link in list(code.filter_wikilinks(recursive=True)):
        title = str(link.title).strip()
        low = title.lower()
        if low.startswith("category:"):
            drop = True
        elif link.text is None and _INTERLANG.match(title) and not low.startswith(_FILE_NS):
            inter.append(title)
            drop = True
        else:
            drop = False
        if drop:
            try:
                code.remove(link)
            except ValueError:
                pass
    return list(dict.fromkeys(inter))


def _render_math(code) -> None:
    """`<math>`/`<chem>` content IN the sentence, as the TeX it is, and
    `<pre>`/`<syntaxhighlight>` as the block it is; `strip_code` treats
    all of them as invisible and parser 2 dropped them (2,242 formulas on
    71 sampled pages)."""
    for tag in list(code.filter_tags(recursive=True)):
        name = str(tag.tag).strip().lower()
        if tag.contents is None:
            continue
        if name in ("math", "chem", "ce"):
            value = " ".join(str(tag.contents).split())
        elif name in ("pre", "syntaxhighlight", "source"):
            value = f"\n\n{str(tag.contents).strip()}\n\n"
        else:
            continue
        try:
            code.replace(tag, value)
        except ValueError:
            pass


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


#: `<ref\nname="a">`: MediaWiki reads it as a ref, mwparserfromhell as text,
#: and the citation sat in the prose, tag and all (simplewiki sample)
_REF_OPEN = re.compile(r"<ref\s+(?=[^<>]*>)", re.I)


def _tags(wikitext: str) -> str:
    return _REF_OPEN.sub("<ref ", wikitext)


def _cell(cell: str) -> str:
    """`style="x" | text` → "text": the part before a lone `|` is the
    cell's attributes."""
    before, bar, after = cell.partition("|")
    if bar and "=" in before and "[[" not in before:
        cell = after
    return cell.strip()


def _untable(body: str) -> str:
    """A table mwparserfromhell could not parse leaves its markup in the
    prose — `{| class="wikitable" style=…`, `|-`, `||` — on 26 of 2,198
    sampled articles. The cells are the article's text; the rest is
    markup (invariant 11 (a)). A row becomes one line, cells " | "."""
    if "{|" not in body:
        return body
    out: list[str] = []
    row: list[str] = []
    depth = 0

    def flush() -> None:
        if row:
            out.append(" | ".join(c for c in row if c))
            row.clear()

    for line in body.split("\n"):
        s = line.strip()
        if s.startswith("{|"):
            flush()
            depth += 1
        elif not depth:
            out.append(line)
        elif s.startswith("|}"):
            flush()
            depth -= 1
        elif s.startswith("|-"):
            flush()
        elif s.startswith("|+"):
            flush()
            out.append(_cell(s[2:]))
        elif s.startswith(("!", "|")):
            row.extend(_cell(c) for c in re.split(r"!!|\|\|", s[1:]))
        elif row:
            row[-1] = f"{row[-1]} {s}".strip()
        elif s:
            out.append(s)
    flush()
    return "\n".join(out)


def _prose_code(wikitext: str):
    """`(code, images, interlanguage)` — the wikitext with everything that
    is not prose taken out of it, and the two things taken that need a
    home in the record."""
    code = mwparserfromhell.parse(wikitext)
    _drop_refs(code)            # first: a caption's citation is not caption
    images = _take_images(code)
    inter = _take_links(code)
    _render_math(code)
    _render_inline(code)
    return code, images, inter


def _structured(wikitext: str) -> tuple[list[tuple[str, str]], list[int], list[dict], list[str]]:
    """`(sections, their heading levels, images, interlanguage links)`.

    PARSER 3. The level of every heading is kept (parser 2 flattened
    `==`/`===`/`====`, 872 of 1,132 sampled pages). A section with no body
    of its own — "History", whose prose is all in its subsections — is
    KEPT, with an empty body: it is the structure the origin asserts, and
    the chunker cuts nothing from an empty body. A boilerplate heading
    (References, Notes…) now takes its SUBSECTIONS with it: `===Citations`
    under `==Notes` used to land in the prose (167 of 1,132 pages).
    """
    code, images, inter = _prose_code(_tags(wikitext))
    sections: list[tuple[str, str]] = []
    levels: list[int] = []
    skip_below: int | None = None
    for section in code.get_sections(include_lead=True, flat=True):
        headings = section.filter_headings()
        level = headings[0].level if headings else 1
        title = headings[0].title.strip_code().strip() if headings else ""
        if skip_below is not None:
            if headings and level <= skip_below:
                skip_below = None
            else:
                continue
        if title.lower() in _BOILERPLATE:
            skip_below = level
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
        body = _WS.sub("\n\n", _MAGIC.sub("", _untable(body))).strip()
        if not headings and not body:
            continue                      # an empty lead is not a section
        sections.append((title, body))
        levels.append(level)
    return sections, levels, images, inter


def to_sections(wikitext: str) -> list[tuple[str, str]]:
    """[(heading, plain_text)] — lead section first with heading ''."""
    return _structured(wikitext)[0]


def categories(code) -> list[str]:
    """`[[Category:X|sortkey]]` → "X". The sortkey is a collation hint for
    the category page, not a second subject — kept apart, below."""
    out = []
    for link in code.filter_wikilinks():
        title = str(link.title).strip()
        if title.lower().startswith("category:"):
            name = title.split(":", 1)[1].split("|")[0].strip()
            if name:
                out.append(name)
    return list(dict.fromkeys(out))


def category_sortkeys(code) -> dict[str, str]:
    """`{category: sort key}` where the page gives one — often the
    entity's filing name ("Einstein, Albert"), found nowhere else
    (parser 3; "unsure" in the audit, kept by the operator's rule)."""
    out: dict[str, str] = {}
    for link in code.filter_wikilinks():
        title = str(link.title).strip()
        if title.lower().startswith("category:") and link.text is not None:
            # `[[Category:X| ]]` — a SPACE is a sort key (it files the page
            # first); stripped, it vanished on most sampled pages
            key = str(link.text)
            key = key.strip() or key
            if key:
                out.setdefault(title.split(":", 1)[1].strip(), key)
    return out


_DEFAULTSORT = re.compile(r"\{\{\s*DEFAULTSORT(?:KEY)?\s*:\s*([^}|]+)\}\}", re.I)

#: The named boxes that carry an article's structured facts. Parser 2 read
#: only names starting "infobox" and dropped the rest whole — automatic
#: taxobox (29), weather box (27), speciesbox, chembox, succession box….
_BOXES = ("infobox", "automatic taxobox", "taxobox", "speciesbox", "chembox",
          "drugbox", "weather box", "succession box", "geobox")


def infoboxes(code) -> list[dict]:
    """Every infobox-like template as {template, fields}. Median 18
    parameters on enwiki. A value that is ALL template — `{{birth date and
    age|1974|6|1}}` — stripped to "" and was dropped (2,152 of 13,285
    sampled parameters): it is kept as its wikitext instead."""
    out = []
    for tpl in code.filter_templates():
        name = str(tpl.name).strip()
        if not name.lower().startswith(_BOXES):
            continue
        fields = {}
        for prm in tpl.params:
            key = str(prm.name).strip()
            val = _text(prm.value) or str(prm.value).strip()
            if key and val:
                fields[key] = val
        if fields:
            out.append({"template": name, "fields": fields})
    return out


def references(code) -> list[str]:
    """The content of every `<ref>`, as text — or as its wikitext when it
    is only a citation template: `{{cite web|…}}` stripped to "" and 53,969
    of 66,690 sampled citations were dropped (parser 3). A citation is what
    the article says its claim rests on."""
    out = []
    for tag in code.filter_tags():
        if str(tag.tag).strip().lower() != "ref":
            continue
        body = (_text(tag.contents) or str(tag.contents).strip()) if tag.contents else ""
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


#: where the software renders the citations: says nothing itself — the
#: <ref>s it renders are in `references` already
_PLACEHOLDER = re.compile(
    r"<references\s*/>|\{\{\s*(?:reflist|notelist|references|refbegin|refend)\b[^{}]*\}\}",
    re.I)


def apparatus(wikitext: str) -> dict[str, str]:
    """The sections `to_sections` leaves out of the prose — References,
    External links, See also and their kin — as text, so that dropping
    them from the CHUNKS does not drop them from the RECORD. With their
    subsections, which follow them out of the prose (parser 3); and with
    every template kept AS ITS WIKITEXT — `{{cite book|…}}`, `{{Portal|…}}`,
    `{{Webarchive|…}}` — which `strip_code` deleted, dropping a section of
    citations whole (105 References, 108 Further reading of 1,132 pages).
    The page-end category and interlanguage links are not the last
    section's text, and `{{reflist}}` is where the software renders the
    <ref>s `references` keeps."""
    code = mwparserfromhell.parse(_PLACEHOLDER.sub("", wikitext))
    _take_links(code)
    for tpl in list(code.filter_templates(recursive=True)):
        try:
            code.replace(tpl, Text(str(tpl)))
        except ValueError:
            pass          # inside a template already kept whole
    out: dict[str, str] = {}
    current: tuple[str, int] | None = None
    for section in code.get_sections(include_lead=False, flat=True):
        headings = section.filter_headings()
        if not headings:
            continue
        level = headings[0].level
        title = headings[0].title.strip_code().strip()
        if title.lower() in _BOILERPLATE:
            current, heading_of_block = (title, level), title
        elif current is not None and level > current[1]:
            heading_of_block = current[0]
        else:
            current = None
            continue
        body = section.strip_code(normalize=True, collapse=True).strip()
        if body.startswith(title):
            body = body[len(title):].lstrip()
        body = _WS.sub("\n\n", _MAGIC.sub("", body)).strip()
        if not body:
            continue
        if heading_of_block != title:
            body = f"{title}\n{body}"
        out[heading_of_block] = f"{out[heading_of_block]}\n\n{body}" \
            if heading_of_block in out else body
    return out


#: navigation the article asserts about itself: "Main article: X",
#: "See also: Y", "For the film, see Z" (489, 322, 174, 185… sampled pages)
_HATNOTES = ("main", "see also", "further", "about", "for", "distinguish",
             "redirect", "other uses")


def parse_all(wikitext: str) -> dict:
    """Sections for the chunker, and everything else for `extra`."""
    wikitext = _tags(wikitext)
    code = mwparserfromhell.parse(wikitext)
    short = named_template(code, "short description")
    sections, levels, images, inter = _structured(wikitext)
    default_sort = _DEFAULTSORT.search(wikitext)
    return {
        "sections": sections,
        "section_levels": levels,
        "categories": categories(code),
        "category_sortkeys": category_sortkeys(code),
        "defaultsort": default_sort.group(1).strip() if default_sort else "",
        "infoboxes": infoboxes(code),
        "references": references(code),
        "external_links": external_links(code),
        "short_description": short[0]["fields"].get("1", "") if short else "",
        "coordinates": [c["raw"] for c in named_template(code, "coord")],
        "apparatus": apparatus(wikitext),
        "images": images,
        "interlanguage": inter,
        "hatnotes": [{"template": n, "fields": t["fields"]}
                     for n in _HATNOTES for t in named_template(code, n)],
        #: presence only — the identifiers are in Wikidata, not here
        "has_authority_control": bool(named_template(code, "authority control")),
    }


def redirect_info(wikitext: str) -> dict:
    """What a redirect page says: its target WITH the `#fragment` that the
    dump's `<redirect title>` drops (28 of 417 sampled redirects), and its
    own categories — they classify the redirect, not its target."""
    m = re.search(r"\[\[([^\]|]+)", wikitext)
    return {"redirect_target": m.group(1).strip() if m else "",
            "categories": categories(mwparserfromhell.parse(wikitext))}


def is_redirect(wikitext: str) -> bool:
    return wikitext.lstrip()[:9].upper() == "#REDIRECT"
