"""Parse the per-book pg{id}.rdf metadata file shipped in cache/epub/{id}/."""
import xml.etree.ElementTree as ET
from pathlib import Path

NS = {
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "dcterms": "http://purl.org/dc/terms/",
    "pgterms": "http://www.gutenberg.org/2009/pgterms/",
}

def parse(book_dir: Path, book_id: int) -> dict:
    path = book_dir / f"pg{book_id}.rdf"
    out = {"title": "", "authors": "", "lang": "und", "subjects": "", "bookshelves": ""}
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
        out["title"] = " ".join(t.text.split())[:500]

    authors = []
    for agent in ebook.findall(".//pgterms:agent", NS):
        name = agent.find("pgterms:name", NS)
        if name is not None and name.text:
            authors.append(name.text.strip())
    out["authors"] = "; ".join(dict.fromkeys(authors))[:500]

    langs = []
    for lang in ebook.findall("dcterms:language", NS):
        v = lang.find(".//rdf:value", NS)
        if v is not None and v.text:
            langs.append(v.text.strip().lower())
        elif lang.text and lang.text.strip():
            langs.append(lang.text.strip().lower())
    if langs:
        out["lang"] = langs[0]

    subjects = []
    for sub in ebook.findall("dcterms:subject", NS):
        v = sub.find(".//rdf:value", NS)
        if v is not None and v.text:
            subjects.append(v.text.strip())
    out["subjects"] = " | ".join(dict.fromkeys(subjects))[:2000]

    shelves = []
    for shelf in ebook.findall(".//pgterms:bookshelf", NS):
        v = shelf.find(".//rdf:value", NS)
        if v is not None and v.text:
            shelves.append(v.text.strip())
    out["bookshelves"] = " | ".join(dict.fromkeys(shelves))[:1000]
    return out
