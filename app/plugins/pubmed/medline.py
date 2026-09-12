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


def _date(d: ET.Element | None) -> str | None:
    """A MEDLINE date element (Year/Month/Day) as `YYYY-MM-DD`, or the
    shortest §9.5 form the element supports."""
    if d is None:
        return None
    y = (d.findtext("Year") or "").strip()
    if not (len(y) == 4 and y.isdigit()):
        return None
    m = (d.findtext("Month") or "").strip()
    dd = (d.findtext("Day") or "").strip()
    if not m.isdigit():
        return y
    out = f"{y}-{int(m):02d}"
    return f"{out}-{int(dd):02d}" if dd.isdigit() else out


def _plain(elem: ET.Element | None, path: str) -> str:
    return _text(elem.find(path)) if elem is not None else ""


def authors_full(elem: ET.Element) -> list[dict]:
    """Every author, with everything the origin says about them.

    `creators` in §9.5 is a list of STRINGS, so it carries "Last
    Initials" and this carries the rest: an ORCID and an affiliation are
    the origin asserting who the author IS, and promise 9 admits no
    category that covers dropping them.
    """
    out = []
    for a in elem.findall("MedlineCitation/Article/AuthorList/Author"):
        ids = {}
        for ident in a.findall("Identifier"):
            src = (ident.get("Source") or "").strip().lower() or "unknown"
            if ident.text and ident.text.strip():
                ids[src] = ident.text.strip()
        affs = [_text(af.find("Affiliation")) for af in a.findall("AffiliationInfo")]
        entry = {
            "last": (a.findtext("LastName") or "").strip(),
            "fore": (a.findtext("ForeName") or "").strip(),
            "initials": (a.findtext("Initials") or "").strip(),
            "suffix": (a.findtext("Suffix") or "").strip(),
            "collective": (a.findtext("CollectiveName") or "").strip(),
            "valid": (a.get("ValidYN") or "").strip(),
            "equal_contrib": (a.get("EqualContrib") or "").strip(),
            "identifiers": ids,
            "affiliations": [x for x in affs if x],
        }
        if any(entry[k] for k in ("last", "collective")) or ids:
            out.append({k: v for k, v in entry.items() if v})
    return out


def mesh_headings(elem: ET.Element) -> tuple[list[str], list[dict]]:
    """(subjects, detail).

    SUBJECTS CARRY THE QUALIFIER. §9.5's own pubmed example writes
    "Neoplasms/genetics", and this plugin declares `subjects` a TREE
    facet with separator "/" — which had nothing to split, because only
    `DescriptorName` was read. One heading with two qualifiers is two
    subject strings, which is what the origin means by them.

    `detail` keeps the descriptor UI, the major-topic flags and the
    qualifier UIs, none of which a "/"-joined string can carry.
    """
    subjects: list[str] = []
    detail: list[dict] = []
    for mh in elem.findall("MedlineCitation/MeshHeadingList/MeshHeading"):
        d = mh.find("DescriptorName")
        if d is None or not (d.text or "").strip():
            continue
        name = d.text.strip()
        quals = [q for q in mh.findall("QualifierName") if (q.text or "").strip()]
        if quals:
            subjects.extend(f"{name}/{q.text.strip()}" for q in quals)
        else:
            subjects.append(name)
        detail.append({
            "descriptor": name,
            "ui": (d.get("UI") or "").strip(),
            "major": (d.get("MajorTopicYN") or "").strip(),
            "qualifiers": [{"name": q.text.strip(),
                            "ui": (q.get("UI") or "").strip(),
                            "major": (q.get("MajorTopicYN") or "").strip()}
                           for q in quals],
        })
    return subjects, detail


def parse_full(elem: ET.Element) -> dict:
    """EVERYTHING a <PubmedArticle> asserts about its article.

    Promise 9 (01): a record holds everything the origin says about the
    entity, and the only droppable content is wrapping, boilerplate, our
    own corpus-add date, and what the provider wrapped around the entity.
    Parser 5 published 16 of the ~103 things this element carries; every
    field below was one of the other 87.

    Separate from `parse_article`, which keeps the seven paths the
    per-container PACK stores as planner columns. Leaving that function
    alone is what keeps this off PACK_VERSION and out of 1,334 pack
    rebuilds — it is a local cache, never exchanged, and its shape is
    ours.
    """
    base = parse_article(elem)
    cit = elem.find("MedlineCitation")
    art = cit.find("Article") if cit is not None else None
    pub = elem.find("PubmedData")
    subjects, mesh_detail = mesh_headings(elem)

    journal = art.find("Journal") if art is not None else None
    issue = journal.find("JournalIssue") if journal is not None else None
    issn = journal.find("ISSN") if journal is not None else None
    mji = cit.find("MedlineJournalInfo") if cit is not None else None
    pmid_el = cit.find("PMID") if cit is not None else None

    # the PubDate AS THE ORIGIN WROTE IT. `date` is normalised to §9.5's
    # three forms, which cannot express "1953 Nov-Dec" or "1998 Spring";
    # the origin's own string is kept beside it rather than rounded away.
    pub_date_raw = ""
    if issue is not None:
        pd = issue.find("PubDate")
        if pd is not None:
            pub_date_raw = " ".join(
                t for t in (pd.findtext("MedlineDate"), pd.findtext("Year"),
                            pd.findtext("Season"), pd.findtext("Month"),
                            pd.findtext("Day")) if t and t.strip()).strip()

    abstracts_other = []
    if art is not None:
        for oa in art.findall("OtherAbstract"):
            body = "\n\n".join(
                f"{at.get('Label')}: {_text(at)}" if at.get("Label") else _text(at)
                for at in oa.findall("AbstractText"))
            if body.strip():
                abstracts_other.append({"language": (oa.get("Language") or "").strip(),
                                        "type": (oa.get("Type") or "").strip(),
                                        "text": body})

    ids = {}
    for aid in (pub.findall("ArticleIdList/ArticleId") if pub is not None else []):
        kind = (aid.get("IdType") or "").strip().lower() or "unknown"
        if aid.text and aid.text.strip():
            ids.setdefault(kind, aid.text.strip())
    for el in (art.findall("ELocationID") if art is not None else []):
        kind = (el.get("EIdType") or "").strip().lower()
        if el.text and el.text.strip() and kind:
            ids.setdefault(kind, el.text.strip())

    history = []
    for d in (pub.findall("History/PubMedPubDate") if pub is not None else []):
        when = _date(d)
        if when:
            history.append({"status": (d.get("PubStatus") or "").strip(), "date": when})

    corrections = []
    for cc in (cit.findall("CommentsCorrectionsList/CommentsCorrections")
               if cit is not None else []):
        corrections.append({k: v for k, v in {
            "ref_type": (cc.get("RefType") or "").strip(),
            "source": _plain(cc, "RefSource"),
            "pmid": (cc.findtext("PMID") or "").strip(),
            "note": _plain(cc, "Note"),
        }.items() if v})

    references = []
    for ref in (pub.findall("ReferenceList//Reference") if pub is not None else []):
        cited = {k: v for k, v in {
            "citation": _plain(ref, "Citation"),
            "pmid": next((a.text.strip() for a in ref.findall("ArticleIdList/ArticleId")
                          if (a.get("IdType") or "").lower() == "pubmed"
                          and a.text and a.text.strip()), ""),
        }.items() if v}
        if cited:
            references.append(cited)

    def _values(path: str, attr: str | None = None) -> list:
        got = []
        for n in (cit.findall(path) if cit is not None else []):
            if attr is not None:
                out = {"name": _text(n), "ui": (n.get(attr) or "").strip()}
                if out["name"]:
                    got.append(out)
            elif _text(n):
                got.append(_text(n))
        return got

    extra = {
        # the citation and its version
        "status": (cit.get("Status") or "").strip() if cit is not None else "",
        "owner": (cit.get("Owner") or "").strip() if cit is not None else "",
        "version_id": (cit.get("VersionID") or "").strip() if cit is not None else "",
        "version_date": (cit.get("VersionDate") or "").strip() if cit is not None else "",
        "indexing_method": (cit.get("IndexingMethod") or "").strip() if cit is not None else "",
        "pmid_version": (pmid_el.get("Version") or "").strip() if pmid_el is not None else "",
        # the article
        "vernacular_title": _plain(art, "VernacularTitle"),
        "pub_model": (art.get("PubModel") or "").strip() if art is not None else "",
        "pagination": _plain(art, "Pagination/MedlinePgn"),
        "ids": ids,
        "publication_types": _values("Article/PublicationTypeList/PublicationType", "UI"),
        # the authors' OWN keywords, named in promise 9's text. A keyword
        # carries @MajorTopicYN and the list carries @Owner (NOTNLM when
        # the authors supplied them, NLM when a curator did) — which is
        # the origin saying who asserted the term, not packaging.
        "keywords": [{k: v for k, v in {
            "term": _text(k), "major": (k.get("MajorTopicYN") or "").strip(),
            "owner": (kl.get("Owner") or "").strip()}.items() if v}
            for kl in (cit.findall("KeywordList") if cit is not None else [])
            for k in kl.findall("Keyword") if _text(k)],
        "other_abstracts": abstracts_other,
        "copyright": _plain(art, "Abstract/CopyrightInformation"),
        "coi_statement": _plain(cit, "CoiStatement"),
        "citation_subsets": _values("CitationSubset"),
        # the journal
        "journal": base["journal"],
        "journal_title": _plain(journal, "Title"),
        "issn": _text(issn),
        "issn_type": (issn.get("IssnType") or "").strip() if issn is not None else "",
        "volume": _plain(issue, "Volume"),
        "issue": _plain(issue, "Issue"),
        "cited_medium": (issue.get("CitedMedium") or "").strip() if issue is not None else "",
        "pub_date_raw": pub_date_raw,
        "medline_ta": _plain(mji, "MedlineTA"),
        "nlm_unique_id": _plain(mji, "NlmUniqueID"),
        "issn_linking": _plain(mji, "ISSNLinking"),
        "country": _plain(mji, "Country"),
        # dates: BOTH of them, and the whole history
        "revised": _date(cit.find("DateRevised")) if cit is not None else None,
        "date_completed": _date(cit.find("DateCompleted")) if cit is not None else None,
        "article_date": _date(art.find("ArticleDate")) if art is not None else None,
        "history": history,
        "publication_status": _plain(pub, "PublicationStatus"),
        # subject machinery beyond `subjects`
        "mesh": mesh_detail,
        "chemicals": _values("ChemicalList/Chemical/NameOfSubstance", "UI"),
        "suppl_mesh": _values("SupplMeshList/SupplMeshName", "UI"),
        "gene_symbols": _values("GeneSymbolList/GeneSymbol"),
        "personal_name_subjects": [
            " ".join(x for x in ((p.findtext("LastName") or "").strip(),
                                  (p.findtext("ForeName") or "").strip()) if x)
            for p in (cit.findall("PersonalNameSubjectList/PersonalNameSubject")
                      if cit is not None else [])],
        # funding, data, and the rest
        "grants": [{k: v for k, v in {
            "id": (g.findtext("GrantID") or "").strip(),
            "acronym": (g.findtext("Acronym") or "").strip(),
            "agency": (g.findtext("Agency") or "").strip(),
            "country": (g.findtext("Country") or "").strip()}.items() if v}
            for g in (cit.findall("Article/GrantList/Grant") if cit is not None else [])],
        "databanks": [{"name": (b.findtext("DataBankName") or "").strip(),
                       "accessions": [a.text.strip() for a in
                                      b.findall("AccessionNumberList/AccessionNumber")
                                      if a.text and a.text.strip()]}
                      for b in (cit.findall("Article/DataBankList/DataBank")
                                if cit is not None else [])],
        "other_ids": [{"source": (o.get("Source") or "").strip(), "id": _text(o)}
                      for o in (cit.findall("OtherID") if cit is not None else [])
                      if _text(o)],
        "space_flight_missions": _values("SpaceFlightMission"),
        "general_notes": _values("GeneralNote"),
        "comments_corrections": corrections,
        "references": references,
        "languages": [(l.text or "").strip().lower()
                      for l in (art.findall("Language") if art is not None else [])
                      if (l.text or "").strip()],
    }
    return {**base, "subjects": subjects, "authors_full": authors_full(elem),
            "extra": {k: v for k, v in extra.items() if v not in ("", None, [], {})}}


def parse_delete_pmids(elem: ET.Element) -> list[str]:
    """elem is a <DeleteCitation> element; returns the PMIDs it retracts."""
    return [p.text.strip() for p in elem.findall("PMID") if p.text and p.text.strip()]
