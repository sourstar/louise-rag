"""Pure parsing of MEDLINE/PubMed XML elements — no I/O, no manifest access.

Container files are <PubmedArticleSet> documents whose children are either
<PubmedArticle> (a citation) or <DeleteCitation> (a batch of PMIDs to
retract). See docs/architecture-v2.md §2, §8.
"""
import xml.etree.ElementTree as ET


def _text(elem: ET.Element | None) -> str:
    return "".join(elem.itertext()).strip() if elem is not None else ""


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
