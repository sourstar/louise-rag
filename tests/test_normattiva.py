"""Normattiva on the ELI layer: discovery over the origin's zips, the act's
record, the rendition and the article chunker — against two real acts
(tests/fixtures/normattiva: Normattiva, CC BY 4.0, testo non ufficiale) and
one built to carry the shapes the census found in the 40 codes."""
import base64
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from app import chunking, collections as collections_mod, db, discovery, plugin_io
from app.plugins import eli
from app.plugins.base import DiscoverState, Mutability
from app.plugins.normattiva import SOURCE, akn, rendition
from app.plugins.normattiva import source as nsrc

FIX = Path(__file__).parent / "fixtures" / "normattiva"
L306 = (FIX / "legge-2003-306-vigente.xml").read_bytes()
LC67 = (FIX / "legge-costituzionale-1967-1-originale.xml").read_bytes()
M306 = "LEGGE_20031031_306/2003-11-15_003G0333_VIGENZA_2025-10-02_V0.xml"
LC_V = "LEGGE COSTITUZIONALE_19670621_1/1967-07-03_067C0001_VIGENZA_2026-01-01_V0.xml"
LC_O = "LEGGE COSTITUZIONALE_19670621_1/1967-07-03_067C0001_ORIGINALE_V0.xml"
MBUILT = "DECRETO_20200102_3/2020-01-10_20G00003_VIGENZA_2025-06-01_V0.xml"

BUILT = b"""<akomaNtoso xmlns="http://docs.oasis-open.org/legaldocml/ns/akn/3.0">
<act name="multivigente"><meta><identification source=""><FRBRWork>
<FRBRthis value="/akn/it/act/decreto/stato/2020-01-02/3/!main"/>
<FRBRalias name="urn:nir" value="urn:nir:stato:decreto:2020-01-02;3"/>
<FRBRalias name="eli" value="eli/id/2020/01/10/20G00003/CONSOLIDATED/20250101"/>
</FRBRWork></identification><publication date="2020-01-10" number="7"/></meta>
<preface><p><docType>DECRETO</docType> <docDate date="2020-01-02">2 gennaio 2020</docDate>,
n. <docNumber>3</docNumber> <docTitle>Norme di prova.</docTitle></p></preface>
<body><chapter eId="chp_I"><num>-</num><heading>TITOLO II Disposizioni</heading>
<article eId="art_5-bis"><num>Art. 5-bis.</num><heading>((Rubrica nuova))</heading>
<paragraph><num>1.</num><content><p>((Testo inserito (art. 3).)) e resto.</p></content></paragraph>
<paragraph><num>2.</num><content><p>((Primo periodo;</p></content></paragraph>
<paragraph><num>3.</num><list><intro><p>secondo periodo)) con elenco:</p></intro>
<point><num>a)</num><content><p>voce;</p></content></point></list></paragraph>
</article>
<article eId="art_6"><num>Art. 6.</num><paragraph><content>
<p>((ARTICOLO ABROGATO DAL D.LGS. 1 GENNAIO 2021, N. 1))</p></content></paragraph></article>
<article eId="art_7"><num>Art. 7.</num><paragraph><num>1.</num><content><table>
<tr><th><p>A</p></th><th><p>B</p></th></tr><tr><td><p>1</p></td><td><p>2</p></td></tr>
</table></content></paragraph></article>
</chapter>
<article eId="art_8"><num>Art. 8.</num><paragraph><num>1.</num><content><p>Fuori capo((3)).</p>
</content></paragraph></article></body>
<attachments><attachment><doc name="Allegato 1"><mainBody/></doc></attachment></attachments>
</act></akomaNtoso>"""


def _zip(path: Path, members: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in members.items():
            z.writestr(name, data)


@pytest.fixture
def corpus(tmp_path) -> Path:
    """The Codice civile is stored as base64 in the real Codici zip; 306
    stands in for it here. The constitutional law is in two collections."""
    nsrc._zip_cache.clear()
    root = tmp_path / "normattiva"
    _zip(root / "akn/V/codici.zip", {M306: base64.b64encode(L306), LC_V: LC67, MBUILT: BUILT})
    _zip(root / "akn/O/atti-normativi-abrogati-in-originale.zip", {LC_O: LC67})
    return root


def _refs(root: Path) -> list:
    return list(SOURCE.discover(DiscoverState(corpus_root=root)))


def _doc(root: Path, member: str):
    """Through `plugin_io.canonical_doc`: the worker's and the reparse's path."""
    ref = next(r for r in _refs(root) if r.locator.endswith(member))
    return plugin_io.canonical_doc(SOURCE, {"native_id": ref.native_id, "path": ref.locator})


# ---- discovery -----------------------------------------------------------

def test_one_ref_per_act_vigente_before_originale_in_native_id_order(corpus):
    refs = _refs(corpus)
    assert all(r.scope == "document" for r in refs) and len(refs) == 3
    lc = next(r for r in refs if int(r.native_id) == eli.native_id("1967-07-03", "067C0001"))
    assert nsrc.split_locator(lc.locator)[0].name == "codici.zip", \
        "the vigente copy wins over the original"
    assert [int(r.native_id) for r in refs] == sorted(int(r.native_id) for r in refs)


def test_a_collision_is_refused_loudly(corpus, monkeypatch):
    monkeypatch.setattr(nsrc.eli, "native_id", lambda date, local: 42)
    refs = _refs(corpus)
    errors = [r for r in refs if r.scope == "container"]
    assert errors and all("collides" in r.error for r in errors)
    assert len([r for r in refs if r.scope == "document"]) == 1, "the first holder keeps the id"


def test_origin_is_the_zip_and_its_member_and_reads_back(corpus):
    ref = next(r for r in _refs(corpus) if r.locator.endswith(M306))
    origin = plugin_io.origin_locator(SOURCE, ref.locator, corpus)
    assert origin == f"akn/V/codici.zip#{M306}"
    local, locator = plugin_io.local_locator(SOURCE, origin, corpus)
    assert local.is_file() and locator == ref.locator


# ---- the record ------------------------------------------------------------

def test_a_base64_member_is_read_and_recorded_as_such(corpus):
    """Two of the 40 real Codici members are base64 of the XML."""
    doc = _doc(corpus, M306)
    assert doc.extra["member_encoding"] == "base64"
    assert doc.title.startswith("LEGGE 31 ottobre 2003, n. 306 — Disposizioni per l'adempimento")


def test_the_record_carries_the_eli_set_and_the_identities(corpus):
    doc = _doc(corpus, M306)
    x = doc.extra
    assert doc.uid == f"normattiva:{eli.native_id('2003-11-15', '003G0333')}"
    assert doc.date == "2003-10-31" and doc.language == "it"
    assert doc.keys == ["eli:it:id/2003/11/15/003G0333", "urn:nir:stato:legge:2003-10-31;306"]
    assert x["eli_work"] == "http://www.normattiva.it/eli/id/2003/11/15/003G0333"
    assert x["eli_expression"] == \
        "http://www.normattiva.it/eli/id/2003/11/15/003G0333/CONSOLIDATED/20180206"
    assert (x["eli_version"], x["eli_version_date"]) == ("CONSOLIDATED", "2018-02-06")
    assert (x["eli_type_document"], x["eli_number"]) == ("LEGGE", "306")
    assert (x["eli_date_publication"], x["eli_id_local"]) == ("2003-11-15", "003G0333")
    assert x["publication"]["number"] == "266"
    assert (x["form"], x["vigenza"]) == ("vigente", "2025-10-02")
    assert {k for k in x if k.startswith("eli_")} <= set(eli.EXTRA_KEYS)
    assert "eli_in_force" not in x, "the origin does not say, so neither do we"


def test_everything_the_metadata_asserts_is_kept(corpus):
    x = _doc(corpus, M306).extra
    assert [e["source"] for e in x["lifecycle"]] == ["ro1", "rp1", "rp2"]
    assert len(x["workflow"]) == 3
    assert any(r.get("eId") == "C.3618" for r in x["references"])
    assert len(x["active_modifications"]) == 21
    assert [m["new"] for m in x["passive_modifications"]] == ["((2))", "((1))"]
    assert x["preface_notes"] == ["Entrata in vigore del provvedimento: 30-11-2003"]
    assert [p["name"] for p in x["preamble"]] == [
        "primaDiFormulaInizialePreambolo", "formulaInizialePreambolo", "formulaFinalePreambolo"]
    assert x["conclusions"].startswith("La presente legge")
    assert "Avvertenza" in x["article_notes"][0]["notes"][0]
    assert [a["name"] for a in x["attachments"]] == ["Allegato A", "Allegato B"]
    assert "/akn/it/act/legge/stato/1988-08-23/400/!main#art_14" in x["eli_cites"]
    assert x["rdfa"]["work"]["about"].startswith("na:id/2003/11/15/003G0333")
    assert x["meta_other"], "the proprietary RDF, kept as written"


def test_a_repealed_act_says_so_and_stays(tmp_path):
    """Repeal is metadata, not deletion (eli-legal-plugins.md §5.7)."""
    root = tmp_path / "n"
    _zip(root / "akn/O/atti-normativi-abrogati-in-originale.zip", {LC_O: LC67})
    doc = _doc(root, LC_O)
    assert doc.extra["eli_in_force"] is False
    assert (doc.extra["form"], doc.extra["eli_version"]) == ("originale", "ORIGINAL")
    assert doc.sections[0][1].startswith("Articolo unico")


# ---- sections and the rendition ---------------------------------------------

def test_sections_are_articles_then_annexes_with_their_structure(corpus):
    secs = _doc(corpus, M306).sections
    assert secs[0][0] == ("CAPO I — DISPOSIZIONI GENERALI SUI PROCEDIMENTI PER "
                          "L'ADEMPIMENTO DEGLI OBBLIGHI COMUNITARI › Art. 1. — (Delega al "
                          "Governo per l'attuazione di direttive comunitarie).")
    assert [label for label, _ in secs[-2:]] == ["Allegato A", "Allegato B"]
    art1 = secs[0][1].split("\n")
    assert art1[0] == "Art. 1. — (Delega al Governo per l'attuazione di direttive comunitarie)."
    assert art1[1].startswith("1. Il Governo e' delegato")
    assert "\n   a) le amministrazioni direttamente interessate" in secs[1][1]
    assert "Avvertenza" not in "\n".join(b for _, b in secs), "notes are not the law's text"


def test_no_text_of_the_body_or_the_annexes_is_lost(corpus):
    """Invariant 11: every paragraph of the act's text is in `sections`,
    only the amendment delimiters gone."""
    act = ET.fromstring(L306).find(akn.A + "act")
    flat = " ".join("\n".join(b for _, b in _doc(corpus, M306).sections).split())
    skip = set()
    for tag in ("section", "authorialNote"):
        for el in act.iter(akn.A + tag):
            skip.update(el.iter())
    missing = []
    for part in (act.find(akn.A + "body"), act.find(akn.A + "attachments")):
        for p in part.iter(akn.A + "p"):
            if p in skip:
                continue
            text = " ".join(rendition.unmark("".join(p.itertext())).split())
            if text and text not in flat:
                missing.append(text[:80])
    assert not missing


def test_the_rendition_reads_back_to_the_akn_skeleton():
    """gazzetta-plugin.md §4.4: structure preserved, checkably."""
    for data in (L306, LC67, BUILT):
        act = akn.parse(data)
        assert rendition.skeleton_matches(act, rendition.render(act, {"urn": "x"}))
    act = akn.parse(L306)
    text = rendition.render(act, {})
    broken = text.replace("\n2. I decreti legislativi", "\nI decreti legislativi", 1)
    assert broken != text and not rendition.skeleton_matches(act, broken)


def test_markers_repeal_tables_and_partitions_render_as_the_law_reads(corpus):
    doc = _doc(corpus, MBUILT)
    secs = dict(doc.sections)
    assert secs["TITOLO II Disposizioni › Art. 5-bis. — Rubrica nuova"] == (
        "Art. 5-bis. — Rubrica nuova\n1. Testo inserito (art. 3). e resto.\n"
        "2. Primo periodo;\n3. secondo periodo con elenco:\n   a) voce;"), \
        "a ((…)) span closes in a later comma; a num of '-' is no number"
    assert secs["TITOLO II Disposizioni › Art. 6. — (abrogato)"] == ""
    assert secs["TITOLO II Disposizioni › Art. 7."] == "Art. 7.\n1. A | B\n1 | 2"
    assert secs["Art. 8."] == "Art. 8.\n1. Fuori capo.", "outside the chapter, note mark gone"
    assert secs["Allegato 1"] == ""
    assert doc.extra["repealed_articles"] == [
        {"article": "art_6", "text": "((ARTICOLO ABROGATO DAL D.LGS. 1 GENNAIO 2021, N. 1))"}]
    assert doc.extra["attachments"] == [{"name": "Allegato 1", "frbr_this": "", "has_text": False}]
    assert not any("((" in b or "))" in b for b in secs.values())


# ---- the chunker -------------------------------------------------------------

def test_one_chunk_per_article_and_none_for_an_empty_one(corpus):
    doc = _doc(corpus, MBUILT)
    chunks = list(chunking.chunk_akn_article(doc.uid, 1, doc.sections, 1))
    assert [c["chapter"] for c in chunks] == [
        "TITOLO II Disposizioni › Art. 5-bis. — Rubrica nuova",
        "TITOLO II Disposizioni › Art. 7.", "Art. 8."]
    assert [c["seq"] for c in chunks] == [0, 1, 2]


def test_a_long_article_splits_between_its_commi_and_loses_nothing(corpus):
    body = _doc(corpus, M306).sections[0][1]
    pieces = chunking._akn_pieces(body, 1500)
    assert len(pieces) >= 2 and all(len(p) <= 1500 for p in pieces)
    assert all(chunking._AKN_COMMA.match(p) for p in pieces[1:]), "cut only at a comma"
    assert "\n".join(pieces) == body


def test_the_chunker_is_the_plugins_and_nobody_elses():
    from app.hold.template import default_layers
    layer = default_layers(SOURCE)[0]
    assert (layer.chunker.id, layer.chunker.version) == ("akn_article", chunking.AKN_CHUNK_VERSION)
    assert chunking.chunker_name("akn_article", Mutability.MUTABLE) == "akn_article"
    assert chunking.chunker_name(None, Mutability.MUTABLE) == "cdc", "other MUTABLE sources keep CDC"
    assert chunking.chunker_name("abstract") == "abstract"
    assert collections_mod.PLUGIN_REGISTRY["normattiva"] is SOURCE


def test_worker_stages_run_end_to_end(tmp_path, corpus, isolated_config):
    from app import worker
    collection = collections_mod.Collection(
        name="normattiva", plugin="normattiva", profile=None, embed_mode_default=None,
        corpus_dir=corpus, manifest_path=tmp_path / "manifest.db",
        fts_dir=tmp_path / "fts", lance_dir=tmp_path / "lancedb")
    con = db.connect(collection)
    discovery.sync_collection(con, collection)
    assert con.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 3
    uid = f"normattiva:{eli.native_id('2003-11-15', '003G0333')}"
    row = con.execute("SELECT * FROM documents WHERE uid=?", (uid,)).fetchone()
    worker.stage_rdf(con, row, collection)
    row = con.execute("SELECT * FROM documents WHERE uid=?", (uid,)).fetchone()
    assert row["rdf_done"] == 1 and row["title"].startswith("LEGGE 31 ottobre 2003, n. 306")
    chunks = worker._load_chunks(row, collection)
    assert len(chunks) == 27, "25 articles and 2 annexes"
    assert chunks[0]["chapter"].startswith("CAPO I") and chunks[0]["text"].startswith("Art. 1.")
