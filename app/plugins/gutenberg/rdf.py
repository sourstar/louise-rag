"""Parse the per-book pg{id}.rdf metadata file shipped in cache/epub/{id}/."""
import re
import xml.etree.ElementTree as ET
from pathlib import Path

# MARC SUBFIELD DELIMITERS LEAK INTO PG TITLES. 4,648 of 79,286 titles on
# this node (5.86%, measured 2026-09-12) carry one, almost all of them the
# `… : $b subtitle` form that 245$b marks — and 09 §9.5 says `title` is
# "cleaned". The marker goes; the ISBD colon before a subtitle stays,
# because that is bibliographic style and not an artefact.
#
# LETTERS ONLY, NEVER DIGITS. The observed codes are `b` (4,646), `c` (2)
# and `v` (1), all genuine MARC. `$1`, `$3` and `$5` also occur and are
# MONEY in real titles — "The $100 Prize Essay on the Cultivation of the
# Potato", "The $30,000 Bequest, and Other Stories" — so a pattern that
# took `\$[a-z0-9]` would mutilate Mark Twain to tidy a delimiter.
_MARC_SUBFIELD = re.compile(r"\s*\$[a-z](?=\s)\s*")


def clean_title(raw: str) -> str:
    """§9.5's `title`: one line, no MARC subfield markers, no double
    spaces, no leading or trailing space."""
    return " ".join(_MARC_SUBFIELD.sub(" ", " ".join(raw.split())).split())

NS = {
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "dcterms": "http://purl.org/dc/terms/",
    "pgterms": "http://www.gutenberg.org/2009/pgterms/",
    #: `dcam:memberOf` names a subject's VOCABULARY, and without it LCSH
    #: headings and LCC class codes land in one flat list where "Science
    #: fiction" and "PZ" are indistinguishable (promise 9).
    "dcam": "http://purl.org/dc/dcam/",
    #: MARC relator roles — `marcrel:ill` illustrator, `trl` translator,
    #: `edt` editor, `ctb` contributor, `aui` author of introduction. The
    #: role is the ELEMENT that wraps the agent, so an agent's
    #: contribution is only knowable from its parent's tag.
    "marcrel": "http://id.loc.gov/vocabulary/relators/",
}

#: the vocabulary a `dcam:memberOf` resource names, by its last segment
_SCHEME_OF = re.compile(r"([A-Za-z]+)$")

#: MARC relator codes in readable words, for the display credit. An
#: unknown code is shown AS the code rather than guessed at or hidden —
#: a credit nobody can read is still a credit, and a wrong one is not.
_ROLE_LABEL = {
    "ill": "illustrator", "trl": "translator", "edt": "editor",
    "ctb": "contributor", "aui": "author of introduction", "com": "compiler",
    "ann": "annotator", "aft": "author of afterword", "prf": "performer",
    "nrt": "narrator", "clb": "collaborator", "art": "artist",
    "egr": "engraver", "pht": "photographer", "adp": "adapter",
    "arr": "arranger", "cmp": "composer", "lyr": "lyricist",
    "aus": "screenwriter", "dub": "dubious author", "oth": "other",
}


def _value(el: ET.Element) -> str:
    """An RDF node's text, whether it is written inline or as rdf:value."""
    v = el.find(".//rdf:value", NS)
    if v is not None and v.text:
        return v.text.strip()
    return (el.text or "").strip()


def _scheme(el: ET.Element) -> str:
    """Which vocabulary this subject belongs to: LCSH, LCC, … or ""."""
    m = el.find(".//dcam:memberOf", NS)
    if m is None:
        return ""
    res = m.get(f"{{{NS['rdf']}}}resource") or ""
    got = _SCHEME_OF.search(res.rstrip("/"))
    return got.group(1) if got else ""


def _agents(ebook: ET.Element) -> list[dict]:
    """Every agent the RDF names, WITH ITS ROLE and everything it says
    about them — the PG agent id, birth and death years, aliases and
    webpages. Promise 9 admits no category for dropping any of it: they
    are the origin asserting who this person is.

    The role comes from the WRAPPING element (`dcterms:creator`,
    `marcrel:ill`, `marcrel:trl`, …), so it has to be read by walking the
    ebook's children rather than with a `.//pgterms:agent` sweep — which
    is what the old code did, flattening illustrators and translators
    into the same anonymous author list.
    """
    out = []
    for child in ebook:
        agent = child.find("pgterms:agent", NS)
        if agent is None:
            continue
        role = child.tag.split("}")[-1] if "}" in child.tag else child.tag
        entry = {
            "name": (agent.findtext("pgterms:name", namespaces=NS) or "").strip(),
            "role": role,
            "agent_id": (agent.get(f"{{{NS['rdf']}}}about") or "").strip(),
            "birth": (agent.findtext("pgterms:birthdate", namespaces=NS) or "").strip(),
            "death": (agent.findtext("pgterms:deathdate", namespaces=NS) or "").strip(),
            "aliases": [a.text.strip() for a in agent.findall("pgterms:alias", NS)
                        if a.text and a.text.strip()],
            "webpages": [w.get(f"{{{NS['rdf']}}}resource", "").strip()
                         for w in agent.findall("pgterms:webpage", NS)
                         if w.get(f"{{{NS['rdf']}}}resource")],
        }
        if entry["name"] or entry["agent_id"]:
            out.append({k: v for k, v in entry.items() if v})
    return out

def parse(book_dir: Path, book_id: int) -> dict:
    path = book_dir / f"pg{book_id}.rdf"
    # EVERY key on every return path. `parse` returns early three ways —
    # no file, unparseable XML, no <pgterms:ebook> — and a caller reading
    # a key that only the happy path sets gets a KeyError on exactly the
    # books whose metadata is already broken.
    out = {"title": "", "authors": "", "lang": "und", "subjects": "", "bookshelves": "",
           "issued": None, "creators_detail": [], "author_names": [], "languages": [],
           "subjects_by_scheme": {}, "alt_titles": [], "description": "",
           "toc": "", "dcmi_type": "", "rights": "", "reading_ease": "", "marc": {}}
    if not path.is_file():
        return out
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError:
        return out

    ebook = root.find("pgterms:ebook", NS)
    if ebook is None:
        return out

    t = ebook.find("dcterms:title", NS)
    if t is not None and t.text:
        out["title"] = clean_title(t.text)

    # the PG release date ("YYYY-MM-DD"): what a dated snapshot of the
    # catalogue cuts on (pack build --until) — never the text's own date
    issued = ebook.find("dcterms:issued", NS)
    if issued is not None and issued.text and issued.text.strip():
        out["issued"] = issued.text.strip()[:10]

    # THE CAPS ARE GONE. `[:500]` on title and authors, `[:2000]` on
    # subjects, `[:1000]` on bookshelves: measured on the live manifest,
    # 25 titles and 7 author lists sat exactly at 500, so their tails
    # were cut and nothing said so. A silent truncation is the shape
    # promise 9 forbids.
    agents = _agents(ebook)
    out["creators_detail"] = agents
    # AUTHORS ARE THE ONLY AUTHORS, and everyone else is credited AS WHAT
    # THEY DID (operator, 2026-09-12). PG names illustrators, translators
    # and editors as agents beside the author, and the old sweep put them
    # all in one list, so Huckleberry Finn read "Twain, Mark; Kemble, E.
    # W." as though Kemble wrote it. Dropping him would be worse — he is
    # a credit — so he is named with his role instead.
    #
    # `out["authors"]` is the DISPLAY string the worker writes into
    # `documents.authors`; the record's `creators` is built from
    # `creators_detail` and holds authors ALONE (see source.parse).
    out["author_names"] = [a["name"] for a in agents
                           if a.get("name") and a.get("role") in ("creator", "")]
    others = [f"{a['name']} ({_ROLE_LABEL.get(a['role'], a['role'])})"
              for a in agents
              if a.get("name") and a.get("role") not in ("creator", "")]
    out["authors"] = "; ".join(dict.fromkeys(out["author_names"] + others))

    langs = [_value(lang).lower() for lang in ebook.findall("dcterms:language", NS)]
    langs = [x for x in dict.fromkeys(langs) if x]
    if langs:
        out["lang"] = langs[0]
    #: EVERY language, in the origin's order. §9.5's `lang` is one string,
    #: and a bilingual edition says so in the RDF.
    out["languages"] = langs

    # SUBJECTS CARRY THEIR VOCABULARY. Each dcterms:subject names its
    # scheme in dcam:memberOf — this corpus uses LCSH and LCC — and
    # joining all the values into one string made "Science fiction" (a
    # heading) and "PZ" (a class code) the same kind of thing.
    by_scheme: dict[str, list[str]] = {}
    for sub in ebook.findall("dcterms:subject", NS):
        val = _value(sub)
        if val:
            by_scheme.setdefault(_scheme(sub) or "unknown", []).append(val)
    out["subjects_by_scheme"] = {k: list(dict.fromkeys(v))
                                 for k, v in by_scheme.items()}
    #: the display string keeps every subject, whatever its scheme, so
    #: nothing disappears from the Library card; the record gets them
    #: separated.
    out["subjects"] = " | ".join(dict.fromkeys(
        v for vals in by_scheme.values() for v in vals))

    shelves = [_value(s) for s in ebook.findall(".//pgterms:bookshelf", NS)]
    out["bookshelves"] = " | ".join(dict.fromkeys(x for x in shelves if x))

    out["alt_titles"] = [_value(a) for a in ebook.findall("dcterms:alternative", NS)
                         if _value(a)]
    out["description"] = "\n\n".join(
        _value(d) for d in ebook.findall("dcterms:description", NS) if _value(d))
    out["toc"] = "\n".join(_value(t) for t in ebook.findall("dcterms:tableOfContents", NS)
                           if _value(t))
    out["dcmi_type"] = _value(ebook.find("dcterms:type", NS)) \
        if ebook.find("dcterms:type", NS) is not None else ""
    #: PG's rights statement. Kept rather than assumed: the collection's
    #: licence block says what WE may do, this says what the origin
    #: declared about this text, and they are not the same assertion.
    out["rights"] = (ebook.findtext("dcterms:rights", namespaces=NS) or "").strip()
    #: marc908, the reading-ease score PG computes per text. Also in
    #: `marc` below; kept by name because the Library card shows it.
    out["reading_ease"] = (ebook.findtext("pgterms:marc908", namespaces=NS) or "").strip()

    # EVERY pgterms:marcNNN, BY NUMBER, whatever it is. The audit could
    # not enumerate these from one machine — a twelve-book sample shows
    # 508, 520, 901 and 908, and the full 79,320-book corpus certainly
    # carries more — so enumerating them would guarantee losing the ones
    # nobody sampled. Capturing the datafield NUMBER as the key keeps a
    # field this code has never heard of.
    #
    # 508 is the production credit ("Produced by David Widger and Pat
    # Castevens"). The operator settled it, 2026-09-12: "keep the PG
    # production credits, project louise respects the credits". 520 is a
    # summary PG GENERATED — kept too, because promise 9's question is
    # whether the origin said it, not whether we trust it; a consumer can
    # tell from the number which is which.
    marc: dict[str, list[str]] = {}
    for child in ebook:
        tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
        if tag.startswith("marc") and tag[4:].isdigit():
            val = _value(child) or (child.get(f"{{{NS['rdf']}}}resource") or "").strip()
            if val:
                marc.setdefault(tag[4:], []).append(val)
    out["marc"] = marc
    return out
