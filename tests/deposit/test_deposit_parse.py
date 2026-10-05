"""tests/deposit/test_deposit_parse.py — what a deposit becomes (06 §6.7;
migration step 16b): text and Markdown as UTF-8, PDF through PDFium, the
same bytes always giving the same record and the same chunks, a PDF's own
metadata kept, and a scan or unreadable bytes refused and never stored."""
import pytest

from app import collections as collections_mod, fts, layers
from app.hold import deposit
from app.plugins.deposit import pdf, source
from app.plugins.base import DocRef, RawDoc
from tests.deposit.pdfs import make_pdf
from tests.deposit.test_deposit_identity import make, node  # noqa: F401

INFO = {"Title": "Relazione annuale", "Author": "Ada Lovelace",
        "Subject": "Bilancio", "Keywords": "bilancio; relazione, 2024",
        "Creator": "Writer", "Producer": "LibreOffice",
        "CreationDate": "D:20240102030405Z", "ModDate": "D:20240203"}
PDF = make_pdf(["Il bilancio del primo trimestre.", None,
                "Conclusioni sulla gestione."], INFO)


def parse(key, data, native_id="7", meta=None):
    """The plugin's parse over bytes, as `fetch` would hand them over."""
    ref = DocRef(source_id="deposit", native_id=native_id, locator=f"/nowhere/{key}")
    raw = RawDoc(ref=ref, raw_hash=source.origin_hash(data), data=data)
    orig = source.read_sidecar
    source.read_sidecar = lambda d: {"key": key, "deposited_at": "2026-09-30T00:00:00Z",
                                     **(meta or {})}
    try:
        return source.DepositSource().parse(raw)
    finally:
        source.read_sidecar = orig


# ---- text and Markdown -----------------------------------------------------

def test_markdown_is_cut_at_its_headings():
    doc = parse("n.md", b"intro line\n\n# One\n\nbody one\n\n## Two ##\nbody two\n")
    assert doc.sections == [("", "intro line"), ("One", "body one"),
                            ("Two", "body two")]


def test_a_text_without_headings_is_one_untitled_section():
    assert parse("n.txt", b"just words\n").sections == [("", "just words")]


def test_a_byte_order_mark_is_not_text():
    assert parse("n.txt", "﻿ciao".encode("utf-8")).sections == [("", "ciao")]


# ---- PDF ---------------------------------------------------------------------

def test_a_pdf_is_one_section_per_page_with_text():
    doc = parse("r.pdf", PDF)
    assert doc.sections == [("page 1", "Il bilancio del primo trimestre."),
                            ("page 3", "Conclusioni sulla gestione.")]


def test_what_a_pdf_says_about_itself_is_kept():
    """Promise 9: nothing the origin says about the document is dropped."""
    doc = parse("r.pdf", PDF)
    assert doc.title == "Relazione annuale"
    assert doc.creators == ["Ada Lovelace"]
    assert doc.date == "2024-01-02"
    assert doc.subjects == ["bilancio", "relazione", "2024"]
    e = doc.extra
    assert (e["pdf_subject"], e["pdf_creator"], e["pdf_producer"]) == \
        ("Bilancio", "Writer", "LibreOffice")
    assert (e["pdf_creation_date"], e["pdf_mod_date"], e["pdf_pages"]) == \
        ("D:20240102030405Z", "D:20240203", 3)


def test_every_deposit_has_the_same_extra_keys():
    """09 §9.5: a collection's `extra` keys are fixed for its life."""
    assert set(parse("a.txt", b"x").extra) == set(parse("r.pdf", PDF).extra) \
        == set(source.EXTRA_KEYS)


def test_a_pdf_without_metadata_is_titled_by_its_key():
    doc = parse("plain.pdf", make_pdf(["words"]))
    assert (doc.title, doc.creators, doc.date, doc.subjects) == ("plain.pdf", [], None, [])


def test_the_same_bytes_give_the_same_record_and_the_same_chunks(node):
    """Invariant 3: a re-parse, a re-embed, cuts exactly what the first did."""
    make(node, "alice")
    c = collections_mod.by_name("alice")
    a, b = parse("r.pdf", PDF), parse("r.pdf", PDF)
    assert a == b
    row = {"uid": "deposit:7", "native_id": "7"}
    ca = list(layers.chunks_for_document(c, row, a.sections))
    cb = list(layers.chunks_for_document(c, row, b.sections))
    assert ca == cb and ca


def test_a_pdf_deposit_is_searchable_and_shown_as_its_text(node):
    make(node, "alice")
    got = deposit.add("alice", "relazione.pdf", PDF)
    shown = deposit.show("alice", "relazione.pdf")
    assert "primo trimestre" in shown["content"]
    assert shown["chunk_count"] == got["chunks"] >= 1
    c = collections_mod.by_name("alice")
    assert any(int(h["book_id"]) == got["native_id"]
               for h in fts.search("other", "trimestre", collection=c))


@pytest.mark.parametrize("key,data,why", [
    ("scan.pdf", make_pdf([None, None]), "no text"),
    ("broken.pdf", b"%PDF-1.4 this is not a pdf", "PDFium can read"),
    ("renamed.pdf", b"plain text pretending", "PDFium can read"),
])
def test_a_pdf_that_yields_no_text_is_refused_and_never_stored(node, key, data, why):
    make(node, "alice")
    with pytest.raises(deposit.DepositError, match=why):
        deposit.add("alice", key, data)
    c = collections_mod.by_name("alice")
    assert deposit.keys("alice") == []
    assert not c.corpus_dir.exists() or not any(c.corpus_dir.iterdir())


def test_extraction_is_bounded_in_time(monkeypatch):
    """CLAUDE.md: nothing waits without a bound. A PDF that keeps PDFium
    busy past the deadline is refused, and the child is killed."""
    monkeypatch.setattr(pdf, "TIMEOUT_S", 0.001)
    with pytest.raises(pdf.PdfError, match="no text within"):
        pdf.extract(PDF)


def test_extraction_works_from_any_working_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert pdf.extract(PDF)["pages"][0] == "Il bilancio del primo trimestre."
