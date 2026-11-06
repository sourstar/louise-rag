"""The origin's inconsistencies, logged and flagged (06 §6.1, amended
2026-10-25, operator: "Make the parsers such that on a regular basis any
inconsistency is logged and flagged"; app/issues.py).

Each wired case produces its issue; a re-parse of a fixed origin clears it;
a parse that checks fewer codes leaves the others; the build's record lines
are the same bytes with the mechanism on; the listing, the counts and the
selection queries' plans."""
from __future__ import annotations

import ast
import json
import re
import shutil
import sqlite3
from pathlib import Path

import pytest

from app import collections as collections_mod, db, discovery, issues, plugin_io
from app.pack import build as pack_build
from app.plugins.base import Issue, RawDoc, DocRef

GUTENBERG_FIXTURE = Path(__file__).parent / "fixtures" / "gutenberg-mini" / "corpus"
PUBMED_FIXTURE = Path(__file__).parent / "fixtures" / "pubmed-mini"


def _collection(tmp_path, name, plugin, corpus) -> collections_mod.Collection:
    """The pack tests' collection: a yellow origin carries its acknowledgement."""
    from tests.pack.test_build import _collection as pack_collection
    return pack_collection(tmp_path, name, plugin, corpus)


def _build(collection, pack_dir, **kw):
    con = db.connect(collection)
    discovery.sync_collection(con, collection)
    con.close()
    return pack_build.build(collection, pack_dir, sign=False, log=lambda *_: None,
                            records_per_shard=kw.pop("records_per_shard", 2), **kw)


def _held(collection) -> dict[tuple[str, str], str]:
    con = db.connect(collection)
    try:
        return {(r["uid"], r["code"]): r["detail"] for r in issues.listing(con)}
    finally:
        con.close()


def _stamp_issued(book_dir: Path, book_id: int, date: str) -> None:
    rdf = book_dir / f"pg{book_id}.rdf"
    text = rdf.read_text(encoding="utf-8")
    rdf.write_text(text.replace("<dcterms:title>",
                                f"<dcterms:issued>{date}</dcterms:issued>\n    <dcterms:title>", 1),
                   encoding="utf-8")


# ---- the recorder ------------------------------------------------------------

CODES = {**issues.PLATFORM, "a": "one", "b": "two"}


def _con(tmp_path):
    c = _collection(tmp_path, "rec", "gutenberg", tmp_path)
    return db.connect(c)


def test_a_reparse_replaces_its_codes_and_a_fixed_origin_clears_them(tmp_path, isolated_config, capsys):
    con = _con(tmp_path)
    got = issues.record_many(con, [("x:1", [Issue("a", "first"), Issue("b", "")]), ("x:2", [])],
                             codes=CODES, now=100)
    assert got == {"new": 2, "cleared": 0, "documents": 1}
    assert issues.counts(con) == {"a": 1, "b": 1}
    out = capsys.readouterr().out
    assert "rec warn:issue #x:1] origin inconsistency a: first" in out, "logged with the collection"

    # the same issues again: nothing new is logged, first_seen is kept
    issues.record_many(con, [("x:1", [Issue("a", "first"), Issue("b", "")])], codes=CODES, now=200)
    assert "origin inconsistency" not in capsys.readouterr().out, "an issue is logged once"
    row = con.execute("SELECT first_seen, last_seen FROM issues WHERE uid='x:1' AND code='a'").fetchone()
    assert tuple(row) == (100, 200)

    # the origin was fixed for `b`: the next parse clears it, and says so once
    got = issues.record_many(con, [("x:1", [Issue("a", "first")])], codes=CODES, now=300)
    assert got["cleared"] == 1 and issues.counts(con) == {"a": 1}
    assert "origin inconsistency b cleared" in capsys.readouterr().out
    issues.record_many(con, [("x:1", [])], codes=CODES, now=400)
    assert issues.counts(con) == {}


def test_a_parse_that_checks_fewer_codes_leaves_the_others(tmp_path, isolated_config):
    con = _con(tmp_path)
    issues.record_many(con, [("x:1", [Issue("a"), Issue("b")])], codes=CODES)
    # a read that checks `a` alone, and met nothing: `b` is not its to clear
    issues.record_many(con, [("x:1", [])], codes=CODES, checked={"a"})
    assert issues.counts(con) == {"b": 1}
    with pytest.raises(ValueError, match="checks only"):
        issues.record_many(con, [("x:1", [Issue("b")])], codes=CODES, checked={"a"})


def test_an_undeclared_code_is_refused_before_anything_is_written(tmp_path, isolated_config):
    con = _con(tmp_path)
    with pytest.raises(ValueError, match="not declared"):
        issues.record_many(con, [("x:1", [Issue("a")]), ("x:2", [Issue("zz")])], codes=CODES)
    assert issues.counts(con) == {}


def test_the_same_code_twice_is_one_row_with_both_details(tmp_path, isolated_config):
    con = _con(tmp_path)
    issues.record_many(con, [("x:1", [Issue("a", "one"), Issue("a", "two")])], codes=CODES)
    assert [r["detail"] for r in issues.listing(con)] == ["one; two"]


# ---- plan checks: every selection is an index walk, none sorts --------------

def _plan(con, sql, params=()) -> str:
    return " | ".join(r[3] for r in con.execute("EXPLAIN QUERY PLAN " + sql, params))


def test_the_selection_queries_are_index_walks(tmp_path, isolated_config):
    con = _con(tmp_path)
    issues.record_many(con, [(f"x:{i}", [Issue("a")]) for i in range(50)], codes=CODES)
    plans = {
        "counts": _plan(con, issues.COUNTS_SQL),
        "by_code": _plan(con, issues.LIST_BY_CODE_SQL, ("a",)),
        "all": _plan(con, issues.LIST_SQL),
        "held": _plan(con, "SELECT uid, code, detail, first_seen FROM issues WHERE uid IN (?,?,?)",
                      ("x:1", "x:2", "x:3")),
    }
    for name, plan in plans.items():
        assert "TEMP B-TREE" not in plan and "LIST SUBQUERY" not in plan, (name, plan)
    # the whole listing walks the table in its own key order (WITHOUT ROWID:
    # the table IS the primary key's tree) — what it is asked for, no sort;
    # every other selection is a search or a covering-index walk
    assert plans["all"] == "SCAN issues", plans["all"]
    for name in ("by_code", "held"):
        assert plans[name].startswith("SEARCH issues"), (name, plans[name])
    assert "COVERING INDEX idx_issues_code" in plans["counts"], plans["counts"]
    assert "idx_issues_code (code=?)" in plans["by_code"], plans["by_code"]
    assert "PRIMARY KEY (uid=?)" in plans["held"], plans["held"]


# ---- gutenberg: the catalogue entry, the epubs, the cutoff -------------------

def test_gutenberg_missing_rdf_and_dateless_records_are_flagged_and_a_fix_clears_them(
        tmp_path, isolated_config, corpus_dir):
    c = _collection(tmp_path, "g", "gutenberg", corpus_dir)
    (corpus_dir / "10002" / "pg10002.rdf").unlink()
    _build(c, tmp_path / "p1")
    held = _held(c)
    assert ("gutenberg:10002", "rdf-missing") in held
    # the fixture's catalogue entries carry no dcterms:issued: the cutoff
    # cannot place any of the three books (choice A: flagged, never silent)
    assert {u for u, code in held if code == "cutoff-undated"} == {
        "gutenberg:10001", "gutenberg:10002", "gutenberg:10003"}
    assert "extra.issued" in held[("gutenberg:10001", "cutoff-undated")]

    # the origin is fixed: the catalogue entry is back, with a release date
    shutil.copy(GUTENBERG_FIXTURE / "10002" / "pg10002.rdf", corpus_dir / "10002" / "pg10002.rdf")
    for book in (10001, 10002, 10003):
        _stamp_issued(corpus_dir / str(book), book, "2006-01-01")
    _build(c, tmp_path / "p2")
    assert _held(c) == {}


def test_a_dated_build_excludes_a_dateless_record_and_says_so(tmp_path, isolated_config, corpus_dir):
    c = _collection(tmp_path, "g", "gutenberg", corpus_dir)
    _stamp_issued(corpus_dir / "10001", 10001, "2006-01-01")
    _stamp_issued(corpus_dir / "10002", 10002, "2006-01-01")
    lines: list[str] = []
    con = db.connect(c)
    discovery.sync_collection(con, c)
    con.close()
    r = pack_build.build(c, tmp_path / "p", until="2026-06-30", records_per_shard=1, sign=False,
                         log=lines.append)
    assert r.records == 2 and r.skipped == 1
    assert any("1 record(s) EXCLUDED" in ln and "cutoff-undated" in ln for ln in lines), lines
    assert "excluded from the snapshot cut at 2026-06-30" in _held(c)[("gutenberg:10003", "cutoff-undated")]


def _epub_book(tmp_path, book_id: int, *, missing_image=False, missing_doc=False,
               zero_byte_images=False) -> Path:
    from tests.test_plugins_gutenberg_epub import build, page
    from tests.test_plugins_gutenberg_source import _rdf
    book = tmp_path / "corpus" / str(book_id)
    book.mkdir(parents=True)
    body = '<p>Text.</p><img src="a.jpg" alt="A"/>'
    if missing_image:
        body += '<img src="gone.jpg" alt="B"/><img src="data:image/png;base64,AAAA" alt="C"/>'
    docs = [("c1.xhtml", page(body))]
    target = book / (f"pg{book_id}.epub" if zero_byte_images else f"pg{book_id}-images.epub")
    build(target, docs, images=[("a.jpg", b"\xff\xd8\xff\xe0jpg")],
          spine=["c1.xhtml"])
    if missing_doc:
        import zipfile
        with zipfile.ZipFile(target, "a") as z:      # the spine names a document the zip lacks
            opf = z.read("OEBPS/content.opf").decode()
        rebuilt = book / "tmp.epub"
        with zipfile.ZipFile(target) as src, zipfile.ZipFile(rebuilt, "w") as dst:
            for info in src.infolist():
                data = src.read(info.filename)
                if info.filename == "OEBPS/content.opf":
                    data = opf.replace("</manifest>", '<item id="dx" href="lost.xhtml" '
                                       'media-type="application/xhtml+xml"/></manifest>').replace(
                        "</spine>", '<itemref idref="dx"/></spine>').encode()
                dst.writestr(info, data)
        rebuilt.replace(target)
    if zero_byte_images:
        (book / f"pg{book_id}-images.epub").write_bytes(b"")
    _rdf(book, book_id)
    return book


def _parse_book(book: Path, book_id: int):
    from app.plugins.gutenberg import SOURCE
    return plugin_io.canonical_doc(SOURCE, {"native_id": str(book_id), "path": str(book)})


def test_an_epubs_broken_references_are_issues_and_its_text_is_unchanged(tmp_path):
    clean = _parse_book(_epub_book(tmp_path / "a", 7), 7)
    broken = _parse_book(_epub_book(tmp_path / "b", 7, missing_image=True, missing_doc=True), 7)
    assert [i.code for i in clean.issues] == []
    by = {i.code: i.detail for i in broken.issues}
    assert set(by) == {"epub-image-missing", "epub-document-missing"}
    assert "OEBPS/gone.jpg" in by["epub-image-missing"] and "data:" not in by["epub-image-missing"]
    assert "lost.xhtml" in by["epub-document-missing"]
    # dropped as before: the same figures, the same text
    assert [f.sha256 for f in broken.figures] == [f.sha256 for f in clean.figures]
    assert broken.sections == clean.sections


def test_a_zero_byte_epub_the_rule_skips_is_flagged(tmp_path):
    doc = _parse_book(_epub_book(tmp_path, 9, zero_byte_images=True), 9)
    by = {i.code: i.detail for i in doc.issues}
    assert set(by) == {"epub-unreadable"}
    assert "pg9-images.epub (0 bytes)" in by["epub-unreadable"] and "read pg9.epub" in by["epub-unreadable"]


@pytest.mark.parametrize("damage,code", [("", "rdf-unreadable"),
                                         ("<rdf:RDF xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'/>",
                                          "rdf-no-ebook")])
def test_a_damaged_catalogue_entry_is_flagged(tmp_path, damage, code):
    book = _epub_book(tmp_path, 11)
    (book / "pg11.rdf").write_text(damage, encoding="utf-8")
    assert [i.code for i in _parse_book(book, 11).issues] == [code]


def test_the_rdf_stage_records_the_catalogues_codes_and_leaves_the_epubs(
        tmp_path, isolated_config, corpus_dir):
    """The worker's metadata stage reads the catalogue entry alone
    (`METADATA_ISSUES`): it records `rdf-missing` and never clears what only
    the build's full parse can see."""
    from app import worker
    c = _collection(tmp_path, "g", "gutenberg", corpus_dir)
    (corpus_dir / "10001" / "pg10001.rdf").unlink()
    con = db.connect(c)
    discovery.sync_collection(con, c)
    issues.record_many(con, [("gutenberg:10001", [Issue("epub-image-missing", "held from a build")])],
                       codes=issues.declared(c.plugin_instance()))
    row = con.execute("SELECT * FROM documents WHERE uid='gutenberg:10001'").fetchone()
    worker.stage_rdf(con, row, c)
    con.execute("COMMIT")
    assert issues.counts(con) == {"epub-image-missing": 1, "rdf-missing": 1}


# ---- pubmed -----------------------------------------------------------------

def _citation(*, date="<Year>1999</Year>", title="A title.", abstract="<Abstract><AbstractText>A.</AbstractText></Abstract>",
              other="", revised="") -> bytes:
    return (f"<PubmedArticle><MedlineCitation><PMID>42</PMID>{revised}{other}<Article>"
            f"<Journal><JournalIssue><PubDate>{date}</PubDate></JournalIssue></Journal>"
            f"<ArticleTitle>{title}</ArticleTitle>{abstract}</Article></MedlineCitation>"
            f"<PubmedData/></PubmedArticle>").encode()


def _pubmed(data: bytes):
    from app.plugins.pubmed import SOURCE
    ref = DocRef(source_id="pubmed", native_id="42", locator="x.xml::0")
    return SOURCE.parse(RawDoc(ref=ref, raw_hash="0" * 32, data=data))


def test_pubmed_a_citation_with_no_date_of_any_kind_is_flagged():
    assert _pubmed(_citation()).issues == []
    medline = _pubmed(_citation(date="<MedlineDate>1998 Spring</MedlineDate>"))
    assert medline.date == "1998" and medline.issues == [], "the MedlineDate year is a date"
    none = _pubmed(_citation(date="<MedlineDate>Spring</MedlineDate>"))
    assert none.date is None
    assert [i.code for i in none.issues] == ["no-publication-date"]
    assert "'Spring'" in none.issues[0].detail


def test_pubmed_an_empty_title_and_an_other_abstract_alone_are_flagged():
    doc = _pubmed(_citation(title="", abstract="", other=(
        '<OtherAbstract Type="PIP" Language="eng"><AbstractText>Popline.</AbstractText></OtherAbstract>')))
    assert {i.code: i.detail for i in doc.issues} == {
        "no-title": "ArticleTitle and VernacularTitle are both empty or absent",
        "other-abstract-only": "PIP/eng"}


def test_pubmed_a_record_the_cutoff_cannot_place_is_flagged_by_the_build(tmp_path, isolated_config):
    from app.pack import cutoff
    assert cutoff.undated("pubmed", {"date": None, "extra": {}}) is not None
    assert cutoff.undated("pubmed", {"date": "1999", "extra": {}}) is None
    assert cutoff.undated("wikipedia", {"date": None, "extra": {}}) is None, "no per-record rule"


# ---- normattiva ---------------------------------------------------------------

def test_normattiva_a_stale_expression_date_and_one_before_publication(tmp_path):
    from tests.test_normattiva import SARD, SARD_O, SARD_V11, SARD_V12, _zip
    from app.plugins.normattiva import SOURCE, source as nsrc
    nsrc._zip_cache.clear()
    late = "LEGGE COSTITUZIONALE_19480226_3/1948-03-20_048C0003_ORIGINALE_V0.xml"
    z = tmp_path / "akn" / "M" / "lc.zip"
    _zip(z, {SARD + "VIGENZA_2001-02-01_V11.xml": SARD_V11,
             SARD + "VIGENZA_2001-02-16_V12.xml": SARD_V12, late: SARD_O})

    def parse(member):
        data = nsrc._read_member(z, member)
        ref = DocRef(source_id="normattiva", native_id="1", locator=f"{z}::{member}")
        return SOURCE.parse(RawDoc(ref=ref, raw_hash="0" * 32, data=data))

    v11 = parse(SARD + "VIGENZA_2001-02-01_V11.xml")
    assert [(i.code, i.detail) for i in v11.issues] == [(
        "frbr-expression-stale",
        "FRBRExpression date 1993-10-10, the member name's version date 2001-02-01; "
        "the version is read from the name")]
    assert parse(SARD + "VIGENZA_2001-02-16_V12.xml").issues == []
    early = parse(late)
    assert [i.code for i in early.issues] == ["expression-before-publication"]
    assert "1948-03-10 precedes the publication 1948-03-20" in early.issues[0].detail


# ---- every code a plugin can report, it declares -----------------------------

def test_every_issue_code_in_the_plugins_is_declared():
    """A literal `Issue("<code>", …)` anywhere under app/plugins/<source>/
    names a code that source declares (`ISSUES`): an undeclared code is
    refused when recorded, and that must be caught here, not on a node."""
    from app.collections import PLUGIN_REGISTRY
    plugins = Path(__file__).resolve().parents[1] / "app" / "plugins"
    seen = 0
    for name, plugin in PLUGIN_REGISTRY.items():
        folder = plugins / name
        if not folder.is_dir():
            continue
        declared = set(issues.declared(plugin))
        for py in folder.glob("*.py"):
            for node in ast.walk(ast.parse(py.read_text(encoding="utf-8"))):
                if (isinstance(node, ast.Call) and getattr(node.func, "id", None) == "Issue"
                        and node.args and isinstance(node.args[0], ast.Constant)):
                    seen += 1
                    assert node.args[0].value in declared, (py, node.args[0].value)
    assert seen >= 10
    for name, plugin in PLUGIN_REGISTRY.items():
        for code, meaning in (getattr(plugin, "ISSUES", None) or {}).items():
            assert re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", code) and meaning, (name, code)


# ---- the record lines are the same bytes with the mechanism on ---------------

def _parts(pack_dir: Path) -> dict[str, bytes]:
    return {p.relative_to(pack_dir).as_posix(): p.read_bytes()
            for p in sorted((pack_dir / "records").rglob("*")) if p.is_file()}


@pytest.mark.parametrize("plugin,fixture", [("gutenberg", GUTENBERG_FIXTURE), ("pubmed", PUBMED_FIXTURE)])
def test_the_build_writes_byte_identical_parts_with_the_mechanism_on(
        tmp_path, isolated_config, monkeypatch, plugin, fixture):
    corpus = tmp_path / "src"
    shutil.copytree(fixture, corpus)
    if plugin == "pubmed":
        # one more update file, holding a citation with no date and no title:
        # the committed fixture's citations are all consistent
        import hashlib
        xml = ('<?xml version="1.0" encoding="UTF-8"?>\n<PubmedArticleSet>'
               + _citation(title="", date="<MedlineDate>Spring</MedlineDate>").decode()
               .replace("<PMID>42</PMID>", '<PMID Version="1">30000099</PMID>')
               + "</PubmedArticleSet>\n").encode()
        (corpus / "pubmed25n0004.xml").write_bytes(xml)
        (corpus / "pubmed25n0004.xml.md5").write_text(
            f"MD5(pubmed25n0004.xml)= {hashlib.md5(xml).hexdigest()}", encoding="utf-8")
    on = _collection(tmp_path, "on", plugin, corpus)
    _build(on, tmp_path / "on-pack")
    assert _held(on), "the mechanism ran: the fixtures' records carry issues"

    real = pack_build._record_of

    def without(collection, plugin_, row):
        record, layer, blob, plates = real(collection, plugin_, row)
        record.pop(pack_build.ISSUES_KEY)
        return record, layer, blob, plates
    monkeypatch.setattr(pack_build, "_record_of", without)
    off = _collection(tmp_path, "off", plugin, corpus)
    _build(off, tmp_path / "off-pack")
    assert _held(off) == {}, "nothing recorded without it"
    a, b = _parts(tmp_path / "on-pack"), _parts(tmp_path / "off-pack")
    assert a and a == b
    # and no line carries the in-memory key
    from app.pack import records
    for rel in a:
        for line in records.iter_records(tmp_path / "on-pack" / rel):
            assert pack_build.ISSUES_KEY not in line and "issues" not in line


# ---- flagged: the listing, show, health, the Library -------------------------

def _louise(*argv) -> tuple[int, str, str]:
    import contextlib
    import io
    from app import louise
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = louise.main(list(argv))
    return code, out.getvalue(), err.getvalue()


@pytest.fixture
def held_gutenberg(tmp_path, isolated_config, corpus_dir):
    """A Codex collection under a LOUISE_ROOT-shaped tree, built once, its
    pg10002.rdf gone: three `cutoff-undated`, one `rdf-missing`."""
    from tests.hold.test_build_and_snapshot import _collection as hold_collection, _build as hold_build
    (corpus_dir / "10002" / "pg10002.rdf").unlink()
    root = hold_collection(tmp_path)
    hold_build(root, corpus_dir, records_per_shard=2)
    return root


def test_issues_list_lists_and_counts(held_gutenberg):
    root = str(held_gutenberg)
    code, out, err = _louise("hold", "issues", "list", "--root", root, "gutenberg")
    assert code == 0
    assert "gutenberg:10002\trdf-missing\tno pg10002.rdf in the book's directory" in out.splitlines()
    assert "cutoff-undated\t3\t" in err and "rdf-missing\t1\t" in err
    code, out, _ = _louise("hold", "issues", "list", "--root", root, "gutenberg",
                           "--code", "rdf-missing", "--json")
    got = json.loads(out)["issues"]
    assert code == 0 and [i["uid"] for i in got["items"]] == ["gutenberg:10002"]
    assert got["total"] == 4 and got["by_code"]["cutoff-undated"]["documents"] == 3
    assert got["codes"]["epub-image-missing"], "every code with its meaning"
    code, out, _ = _louise("hold", "issues", "list", "--root", root, "gutenberg", "--limit", "1")
    assert code == 0 and len(out.splitlines()) == 1
    code, _, err = _louise("hold", "issues", "list", "--root", root, "gutenberg", "--code", "nope")
    assert code == 2 and "no issue code" in err


def test_collection_show_counts_them(held_gutenberg):
    code, out, _ = _louise("hold", "collection", "show", "--root", str(held_gutenberg), "gutenberg")
    assert code == 0
    got = json.loads(out)["issues"]
    assert got["total"] == 4
    assert got["by_code"]["rdf-missing"] == {"documents": 1, "meaning": issues.declared(
        collections_mod.PLUGIN_REGISTRY["gutenberg"])["rdf-missing"]}


def test_node_health_and_the_library_card_flag_them(tmp_path, isolated_config, corpus_dir):
    from app import health, web
    c = _collection(tmp_path, "g", "gutenberg", corpus_dir)
    _build(c, tmp_path / "p")
    reports, blocking = health.check([c], mode="running")
    assert reports[0].issues == {"cutoff-undated": 3}
    text = health.render(reports)
    assert "origin inconsistencies: cutoff-undated 3 — `louise hold issues list g`" in text
    assert not [b for b in blocking if "issue" in b], "flagged, never blocking"
    con = db.connect(c)
    assert web._issue_counts(con) == {"cutoff-undated": 3}


def test_an_old_manifest_gains_the_table_on_open_and_a_reader_never_creates_it(tmp_path):
    path = tmp_path / "old.sqlite"
    sqlite3.connect(path).execute("CREATE TABLE documents (uid TEXT PRIMARY KEY)").connection.commit()
    ro = issues.open_read(path)
    assert issues.counts(ro) == {} and list(issues.listing(ro)) == []
    ro.close()
    assert not sqlite3.connect(path).execute(
        "SELECT 1 FROM sqlite_master WHERE name='issues'").fetchone(), "a reader created nothing"
