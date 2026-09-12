"""Pure parsing of MEDLINE/PubMed XML elements — no I/O, no manifest access.

Container files are <PubmedArticleSet> documents whose children are either
<PubmedArticle> (a citation) or <DeleteCitation> (a batch of PMIDs to
retract). See docs/notes/architecture-v2.md §2, §8.
"""
import xml.etree.ElementTree as ET


def _text(elem: ET.Element | None) -> str:
    return "".join(elem.itertext()).strip() if elem is not None else ""


# MEDLINE writes ISO 639-2/B (BIBLIOGRAPHIC) three-letter codes, and 09 §9.5
# wants "ISO 639-1 (639-3 where no two-letter code exists); "und" when
# unknown". The two are not the same alphabet and the difference is not
# cosmetic: 639-2/B is `ger`/`fre`/`chi`/`dut`/`cze`/`rum`, where 639-2/T and
# 639-3 are `deu`/`fra`/`zho`/`nld`/`ces`/`ron`. So the raw code cannot be
# passed through as a "639-3 fallback" — it would emit codes that are in
# neither alphabet.
#
# 36 distinct codes over a 360,000-article sample of this corpus
# (2026-09-12); the table is deliberately wider than that sample, because a
# code we have no entry for degrades to "und" and silently loses a real
# language. tests pin every observed code.
_LANG_2 = {
    "afr": "af", "alb": "sq", "ara": "ar", "arm": "hy", "aze": "az",
    "ben": "bn", "bos": "bs", "bul": "bg", "cat": "ca", "chi": "zh",
    "cze": "cs", "dan": "da", "dut": "nl", "eng": "en", "epo": "eo",
    "est": "et", "fin": "fi", "fre": "fr", "geo": "ka", "ger": "de",
    "glg": "gl", "gre": "el", "heb": "he", "hin": "hi", "hrv": "hr",
    "hun": "hu", "ice": "is", "ind": "id", "ita": "it", "jpn": "ja",
    "kor": "ko", "lat": "la", "lav": "lv", "lit": "lt", "mac": "mk",
    "mal": "ml", "mao": "mi", "may": "ms", "mlt": "mt", "nor": "no",
    "per": "fa", "pol": "pl", "por": "pt", "pus": "ps", "rum": "ro",
    "rus": "ru", "san": "sa", "slo": "sk", "slv": "sl", "spa": "es",
    "srp": "sr", "swe": "sv", "tha": "th", "tur": "tr", "ukr": "uk",
    "urd": "ur", "vie": "vi", "wel": "cy",
}
#: no two-letter code exists, so §9.5's 639-3 branch applies and the code
#: travels as it is: `mul` (several languages) and `und` (undetermined,
#: which MEDLINE itself writes).
_LANG_AS_IS = {"mul", "und"}


def language(elem: ET.Element) -> str:
    """§9.5's `lang` for a <PubmedArticle>: ISO 639-1, else 639-3, else
    `"und"`.

    THE FIRST <Language> WINS. 0.37% of this corpus carries more than one
    and `lang` is a single string; MEDLINE lists the primary language
    first, so the first element is the article's own language and the
    rest are what it is also available in.
    """
    for el in elem.findall("MedlineCitation/Article/Language"):
        code = (el.text or "").strip().lower()
        if not code:
            continue
        if code in _LANG_AS_IS:
            return code
        return _LANG_2.get(code, "und")
    return "und"


def authors(elem: ET.Element) -> list[str]:
    """§9.5's `creators`, IN THE ORIGIN'S ORDER — "Last Initials", or the
    collective name for a consortium byline, which is the author for a
    good part of the corpus. `[]` when the origin names none (99.0% of
    articles do name at least one)."""
    out = []
    for a in elem.findall("MedlineCitation/Article/AuthorList/Author"):
        last = (a.findtext("LastName") or "").strip()
        initials = (a.findtext("Initials") or "").strip()
        coll = (a.findtext("CollectiveName") or "").strip()
        name = f"{last} {initials}".strip() or coll
        if name:
            out.append(name)
    return out


_MONTHS = {m: f"{i:02d}" for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"), start=1)}


def _ymd(year: str, month: str, day: str) -> str | None:
    """`YYYY`, `YYYY-MM` or `YYYY-MM-DD` — 09 §9.5's three admitted forms
    and nothing else. A month may arrive as "Nov" or as "11"; a range
    like "Nov-Dec" or a season like "Spring" resolves to the year alone
    rather than to an invented month."""
    year = (year or "").strip()
    if not (len(year) == 4 and year.isdigit()):
        return None
    month = (month or "").strip().lower()
    # `isalpha`, not `month[:3]`: "nov-dec" starts with "nov" and a range is
    # NOT November. Inventing a month from a range would put a precision in
    # the record that the origin never claimed.
    if month.isdigit():
        mm = f"{int(month):02d}" if 1 <= int(month) <= 12 else None
    elif month.isalpha():
        mm = _MONTHS.get(month[:3])
    else:
        mm = None
    if mm is None:
        return year
    day = (day or "").strip()
    if day.isdigit() and 1 <= int(day) <= 31:
        return f"{year}-{mm}-{int(day):02d}"
    return f"{year}-{mm}"


def publication_date(elem: ET.Element) -> str | None:
    """§9.5's `date` for a <PubmedArticle>, or None when the origin gives
    no usable year.

    The JOURNAL's publication date (`Journal/JournalIssue/PubDate`) is the
    document's date and is what a reader is shown; `ArticleDate` — the
    electronic-publication date, present on 80.8% of this corpus — is the
    fallback, and `MedlineDate` ("1953 Nov-Dec", "1998 Spring") the last
    resort, from which only the year is trustworthy.

    Distinct from `parse_article`'s `date`, which is the PACK's planner
    column and keeps its own "1953-Nov" shape on purpose: that column is
    internal, its format is not published, and changing it would bump
    PACK_VERSION and rebuild all 1,334 packs for no gain.
    """
    pub = elem.find("MedlineCitation/Article/Journal/JournalIssue/PubDate")
    if pub is not None:
        got = _ymd(pub.findtext("Year") or "", pub.findtext("Month") or "",
                   pub.findtext("Day") or "")
        if got:
            return got
        medline_date = (pub.findtext("MedlineDate") or "").strip()
        if medline_date[:4].isdigit():
            return medline_date[:4]
    art = elem.find("MedlineCitation/Article/ArticleDate")
    if art is not None:
        return _ymd(art.findtext("Year") or "", art.findtext("Month") or "",
                    art.findtext("Day") or "")
    return None


def parse_article(elem: ET.Element) -> dict:
    """elem is a <PubmedArticle> element. Returns pmid, title, abstract,
    mesh[], journal, date."""
    citation = elem.find("MedlineCitation")
    pmid_el = citation.find("PMID") if citation is not None else None
    pmid = pmid_el.text.strip() if pmid_el is not None and pmid_el.text else ""

    article = citation.find("Article") if citation is not None else None
    title = _text(article.find("ArticleTitle")) if article is not None else ""

    abstract_parts = []
    if article is not None:
        abstract_el = article.find("Abstract")
        if abstract_el is not None:
            for at in abstract_el.findall("AbstractText"):
                label = at.get("Label")
                text = _text(at)
                abstract_parts.append(f"{label}: {text}" if label else text)
    abstract = "\n\n".join(p for p in abstract_parts if p)

    mesh = []
    if citation is not None:
        for desc in citation.findall("MeshHeadingList/MeshHeading/DescriptorName"):
            if desc.text and desc.text.strip():
                mesh.append(desc.text.strip())

    journal, year, month = "", None, None
    if article is not None:
        journal_el = article.find("Journal")
        if journal_el is not None:
            journal = _text(journal_el.find("ISOAbbreviation"))
            pub_date = journal_el.find("JournalIssue/PubDate")
            if pub_date is not None:
                y = pub_date.find("Year")
                m = pub_date.find("Month")
                year = y.text.strip() if y is not None and y.text else None
                month = m.text.strip() if m is not None and m.text else None
    date = f"{year}-{month}" if year and month else year

    # DateRevised (fallback DateCompleted): WHEN the record last entered or
    # changed in PubMed — the key a dated snapshot cuts on (pack build
    # --until). The publication date above is what the reader shows.
    revised = None
    if citation is not None:
        for tag in ("DateRevised", "DateCompleted"):
            d = citation.find(tag)
            if d is not None:
                y = (d.findtext("Year") or "").strip()
                m = (d.findtext("Month") or "").strip()
                dd = (d.findtext("Day") or "").strip()
                if y:
                    revised = f"{y}-{m.zfill(2) if m else '01'}-{dd.zfill(2) if dd else '01'}"
                    break

    return {"pmid": pmid, "title": title, "abstract": abstract, "mesh": mesh,
            "journal": journal, "date": date, "revised": revised}


def parse_delete_pmids(elem: ET.Element) -> list[str]:
    """elem is a <DeleteCitation> element; returns the PMIDs it retracts."""
    return [p.text.strip() for p in elem.findall("PMID") if p.text and p.text.strip()]
