"""Normattiva on the ELI layer, one record per Expression
(docs/notes/normattiva-multivigenza-review.md §3): discovery over the
origin's zips, the version id, the floor, the repeal state, the act's record,
the rendition and the article chunker — against real acts
(tests/fixtures/normattiva: Normattiva, CC BY 4.0, testo non ufficiale) and
one built to carry the shapes the census found in the 40 codes."""
import base64
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from app import chunking, collections as collections_mod, db, discovery, plugin_io
from app.pack import slicing
from app.plugins import eli
from app.plugins.base import DiscoverState, Mutability
from app.plugins.normattiva import SOURCE, akn, rendition
from app.plugins.normattiva import source as nsrc

FIX = Path(__file__).parent / "fixtures" / "normattiva"
L306 = (FIX / "legge-2003-306-vigente.xml").read_bytes()
LC67 = (FIX / "legge-costituzionale-1967-1-originale.xml").read_bytes()
SARD_O = (FIX / "statuto-sardegna-1948-3-originale.xml").read_bytes()
SARD_V11 = (FIX / "statuto-sardegna-1948-3-vigenza-2001-02-01.xml").read_bytes()
SARD_V12 = (FIX / "statuto-sardegna-1948-3-vigenza-2001-02-16.xml").read_bytes()
RD206_REPEALED = (FIX / "regio-decreto-1861-206-abrogato-2025-05-09.xml").read_bytes()

# member names as the origin writes them: `<TIPO>_<YYYYMMDD>_<N>/<dataGU>_<codice>_<form>_V<n>.xml`
M306 = "LEGGE_20031031_306/2003-11-15_003G0333_VIGENZA_2025-10-02_V3.xml"
LC_O = "LEGGE COSTITUZIONALE_19670621_1/1967-07-03_067C0001_ORIGINALE_V0.xml"
LC_V = "LEGGE COSTITUZIONALE_19670621_1/1967-07-03_067C0001_VIGENZA_2026-01-01_V1.xml"
SARD = "LEGGE COSTITUZIONALE_19480226_3/1948-03-09_048C0003_"
SARD_MEMBERS = {SARD + "ORIGINALE_V0.xml": SARD_O,
                SARD + "VIGENZA_2001-02-01_V11.xml": SARD_V11,
                SARD + "VIGENZA_2001-02-16_V12.xml": SARD_V12}
RD206 = "REGIO DECRETO_18610317_206/1701-01-01_6100206R_"
RD206_O, RD206_R = RD206 + "ORIGINALE_V0.xml", RD206 + "VIGENZA_2025-05-09_V1.xml"
# an act with versions ending before and after the Republic (the floor)
RD7 = "REGIO DECRETO_19300101_7/1930-01-10_030U0007_"
RD7_O, RD7_V1, RD7_V2 = (RD7 + "ORIGINALE_V0.xml", RD7 + "VIGENZA_1935-06-01_V1.xml",
                         RD7 + "VIGENZA_1950-03-01_V2.xml")
MBUILT = "DECRETO_20200102_3/2020-01-10_20G00003_VIGENZA_2025-06-01_V0.xml"

BUILT = b"""<akomaNtoso xmlns="http://docs.oasis-open.org/legaldocml/ns/akn/3.0">
<act name="multivigente"><meta><identification source=""><FRBRWork>
<FRBRthis value="/akn/it/act/decreto/stato/2020-01-02/3/!main"/>
<FRBRalias name="urn:nir" value="urn:nir:stato:decreto:2020-01-02;3"/>
<FRBRalias name="eli" value="eli/id/2020/01/10/20G00003/CONSOLIDATED/20250101"/>
</FRBRWork></identification><publication date="2020-01-10" number="7"/></meta>
<preface><p><docType>DECRETO</docType> <docDate date="2020-01-02">2 gennaio 2020</docDate>,
n. <docNumber>3</docNumber> <docTitle>Norme di prova.</docTitle></p>
<p><authorialNote eId="atn1" placement="bottom"><p>Entrata in vigore del provvedimento: 25/01/2020</p></authorialNote></p></preface>
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
    """The acquisition spec's layout: the M zips, and the repealed acts' O
    zip. The Codice civile is stored as base64 in the real Codici zip; 306
    stands in for it here. The 1967 constitutional law is in two zips, as
    79,922 royal decrees are; R.D. 206/1861 has its original in the
    repealed-acts zip and its repeal state in Regi decreti's."""
    nsrc._zip_cache.clear()
    root = tmp_path / "normattiva"
    _zip(root / "akn/M/leggi-costituzionali.zip",
         {**SARD_MEMBERS, LC_O: LC67, LC_V: LC67})
    _zip(root / "akn/M/codici.zip", {M306: base64.b64encode(L306), MBUILT: BUILT})
    _zip(root / "akn/M/regi-decreti.zip",
         {RD206_R: RD206_REPEALED, RD7_O: BUILT, RD7_V1: BUILT, RD7_V2: BUILT})
    _zip(root / "akn/O/atti-normativi-abrogati-in-originale.zip",
         {LC_O: LC67, RD206_O: BUILT})
    return root


def _refs(root: Path) -> list:
    return list(SOURCE.discover(DiscoverState(corpus_root=root)))


def _doc(root: Path, member: str):
    """Through `plugin_io.canonical_doc`: the worker's and the reparse's path."""
    ref = next(r for r in _refs(root) if r.locator.endswith(member))
    return plugin_io.canonical_doc(SOURCE, {"native_id": ref.native_id, "path": ref.locator})


def _nid(gu: str, codice: str, valid_from: str, expression: str) -> int:
    return eli.expression_native_id(valid_from, (gu, codice), expression)


# ---- discovery -----------------------------------------------------------

def test_every_version_is_a_record_in_validity_order(corpus):
    """review §3.1: one record per Expression, ids by the start of validity."""
    refs = _refs(corpus)
    assert all(r.scope == "document" and r.error is None for r in refs)
    ids = [int(r.native_id) for r in refs]
    assert ids == sorted(ids)
    sard = [r for r in refs if "048C0003" in r.locator]
    assert [int(r.native_id) // 10**9 for r in sard] == [19480309, 20010201, 20010216]
    assert sard[0].native_id == str(_nid("1948-03-09", "048C0003", "1948-03-09", "ORIGINAL"))
    assert sard[1].native_id == str(_nid("1948-03-09", "048C0003", "2001-02-01", "CONSOLIDATED/20010201"))
    # 3 Sardegna + 2 of the 1967 law + 306 + built + 2 of R.D. 206 + 2 of R.D. 7
    assert len(refs) == 11


def test_a_version_in_two_zips_is_held_once_from_the_multivigente_zip(corpus):
    refs = _refs(corpus)
    lc = [r for r in refs if "067C0001_ORIGINALE" in r.locator]
    assert len(lc) == 1 and nsrc.split_locator(lc[0].locator)[0].name == "leggi-costituzionali.zip"
    rd = [r for r in refs if "6100206R_ORIGINALE" in r.locator]
    assert len(rd) == 1 and "abrogati" in nsrc.split_locator(rd[0].locator)[0].name, \
        "the original only the repealed-acts zip holds comes from there"


def test_the_floor_drops_a_version_that_ended_before_the_republic(corpus):
    """review §3.8: the original of R.D. 7/1930 ended 1935-06-01; the 1935
    version ended 1950-03-01 and stays; the 1950 version is open and stays."""
    members = {nsrc.split_locator(r.locator)[1] for r in _refs(corpus)}
    assert RD7_O not in members and RD7_V1 in members and RD7_V2 in members
    assert RD206_O in members, "an original whose repeal came in 2025 is in force after 1948"
    assert nsrc.VALIDITY_FLOOR == "1948-01-01"


def test_a_collision_is_refused_loudly(corpus, monkeypatch):
    monkeypatch.setattr(nsrc.eli, "expression_native_id", lambda *a, **k: 42)
    refs = _refs(corpus)
    errors = [r for r in refs if r.scope == "container"]
    assert errors and all("collides" in r.error for r in errors)
    assert len([r for r in refs if r.scope == "document"]) == 1, "the first holder keeps the id"


def test_origin_is_the_zip_and_its_member_and_reads_back(corpus):
    ref = next(r for r in _refs(corpus) if r.locator.endswith(M306))
    origin = plugin_io.origin_locator(SOURCE, ref.locator, corpus)
    assert origin == f"akn/M/codici.zip#{M306}"
    local, locator = plugin_io.local_locator(SOURCE, origin, corpus)
    assert local.is_file() and locator == ref.locator


def test_the_version_key_is_the_member_name_not_its_sequence_number():
    assert nsrc.version_key("ORIGINALE", None) == "ORIGINAL"
    assert nsrc.version_key("VIGENZA", "2001-02-01") == "CONSOLIDATED/20010201"
    assert nsrc.member_dir_type("LEGGE COSTITUZIONALE_19480226_3") == "LEGGE COSTITUZIONALE"
    assert nsrc.member_dir_type("REGIO DECRETO_18610317_206") == "REGIO DECRETO"


# ---- the record ------------------------------------------------------------

def test_a_base64_member_is_read_and_recorded_as_such(corpus):
    """Two of the 40 real Codici members are base64 of the XML."""
    doc = _doc(corpus, M306)
    assert doc.extra["member_encoding"] == "base64"
    assert doc.title.startswith("LEGGE 31 ottobre 2003, n. 306 — Disposizioni per l'adempimento")


def test_the_record_carries_the_eli_set_and_the_identities(corpus):
    doc = _doc(corpus, M306)
    x = doc.extra
    assert doc.uid == f"normattiva:{_nid('2003-11-15', '003G0333', '2025-10-02', 'CONSOLIDATED/20251002')}"
    assert doc.date == "2003-10-31" and doc.language == "it"
    assert doc.keys == [], "05 §5.2: only registered ids, and nothing registers eli or urn"
    assert x["urn_nir"] == "urn:nir:stato:legge:2003-10-31;306"
    assert x["codice_redazionale"] == "003G0333"
    assert x["eli_work"] == "http://www.normattiva.it/eli/id/2003/11/15/003G0333"
    assert x["eli_expression"] == \
        "http://www.normattiva.it/eli/id/2003/11/15/003G0333/CONSOLIDATED/20180206"
    assert (x["eli_version"], x["eli_version_date"]) == ("CONSOLIDATED", "2018-02-06")
    assert (x["eli_type_document"], x["eli_number"]) == ("LEGGE", "306")
    assert (x["eli_date_publication"], x["eli_id_local"]) == ("2003-11-15", "003G0333")
    assert x["publication"]["number"] == "266"
    assert (x["form"], x["vigenza"], x["vigenza_from"]) == ("vigente", "2025-10-02", "2025-10-02")
    assert x["version_key"] == "CONSOLIDATED/20251002" and x["member_dir"] == "LEGGE_20031031_306"
    assert x["repealed_in_full"] is False and "repealed_by_text" not in x
    assert x["entry_into_force"] == "2003-11-30"
    assert {k for k in x if k.startswith("eli_")} <= set(eli.EXTRA_KEYS)
    assert "eli_in_force" not in x and "in_force" not in x, "the origin does not say, so neither do we"


def test_the_three_versions_of_one_act_are_three_records(corpus):
    """review §1 and §3.1: the ELI alias agrees with the member name and is
    the version key; the FRBR expression date can be stale (V11 says
    1993-10-10) and is kept as the origin wrote it, never used as the key."""
    o, v11, v12 = (_doc(corpus, m) for m in SARD_MEMBERS)
    assert {d.extra["eli_work"] for d in (o, v11, v12)} == \
        {"http://www.normattiva.it/eli/id/1948/03/09/048C0003"}
    assert [d.extra["version_key"] for d in (o, v11, v12)] == \
        ["ORIGINAL", "CONSOLIDATED/20010201", "CONSOLIDATED/20010216"]
    assert [d.extra["eli_expression"].rsplit("/048C0003/", 1)[1] for d in (o, v11, v12)] == \
        ["ORIGINAL", "CONSOLIDATED/20010201", "CONSOLIDATED/20010216"]
    assert [d.extra["vigenza_from"] for d in (o, v11, v12)] == \
        ["1948-03-09", "2001-02-01", "2001-02-16"]
    assert v11.extra["frbr"]["expression"]["date"] == "1993-10-10", "stale at the origin, kept"
    assert len({d.uid for d in (o, v11, v12)}) == 3
    assert o.date == v11.date == "1948-02-26", "`date` is the act's own, on every version"
    assert len(v11.extra["lifecycle"]) == 11 and len(v12.extra["lifecycle"]) == 11, \
        "a member lists the events up to its own version: the end of validity is not in its bytes"
    assert [b for _, b in o.sections] != [b for _, b in v12.sections], "the texts differ"
    assert len({d.raw_hash for d in (o, v11, v12)}) == 3, "three origin units, three origin hashes"


def test_a_repeal_state_has_no_text_and_says_so(corpus):
    """review §3.2 case 1: the origin's last word on a repealed act — every
    article a repeal statement, dated the day the repeal took effect."""
    doc = _doc(corpus, RD206_R)
    x = doc.extra
    assert x["repealed_in_full"] is True and x[eli.IN_FORCE] is False
    assert x["repealed_by_text"] == "PROVVEDIMENTO ABROGATO DALLA L. 7 APRILE 2025, N. 56"
    assert (x["vigenza_from"], x["version_key"]) == ("2025-05-09", "CONSOLIDATED/20250509")
    assert all(body == "" for _, body in doc.sections) and len(doc.sections) == 10
    assert list(chunking.chunk_akn_article(doc.uid, 1, doc.sections, 1)) == []
    assert x["entry_into_force"] == "1861-03-17"
    assert len(x["repealed_articles"]) == 10
    assert x["eli_date_publication"] == "1701-01-01", "the origin's placeholder date, kept as given"


def test_a_repealed_act_says_so_and_stays(corpus):
    """Repeal is metadata, not deletion (eli-legal-plugins.md §5.7): the
    repealed-acts zip is the origin's word, and the text is indexed."""
    doc = _doc(corpus, RD206_O)
    assert doc.extra[eli.IN_FORCE] is False and doc.extra["repealed_in_full"] is False
    assert (doc.extra["form"], doc.extra["eli_version"]) == ("originale", "CONSOLIDATED")
    assert any(body for _, body in doc.sections)


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


# ---- slicing ----------------------------------------------------------------

def test_the_slice_is_the_acts_type_and_the_year_the_version_took_force(corpus):
    """review §3.7; the hint from the manifest row agrees with the scheme."""
    scheme = slicing.scheme_for("normattiva")
    assert scheme.keys == ("type", "vig") and scheme.records_per_shard == 50
    v11 = _doc(corpus, SARD + "VIGENZA_2001-02-01_V11.xml")
    assert scheme.slice_of(v11, int(v11.uid.split(":")[1])) == "type=LEGGE_COSTITUZIONALE/vig=2001"
    ref = next(r for r in _refs(corpus) if r.locator.endswith(SARD + "VIGENZA_2001-02-01_V11.xml"))
    assert slicing.slice_hint("normattiva", {"native_id": ref.native_id, "path": ref.locator}) == \
        "type=LEGGE_COSTITUZIONALE/vig=2001"
    rd = _doc(corpus, RD206_R)
    assert scheme.slice_of(rd, 0) == "type=REGIO_DECRETO/vig=2025"
    from app.hold.template import default_slicing
    block = default_slicing(SOURCE)
    assert block["keys"] == ["type", "vig"] and block["shard_records"] == 50
    assert block["unknown"] == "type=unknown" and block["order"] == "numeric"
    slicing.check_slice_name(scheme.slice_of(v11, 0))


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
    for data in (L306, LC67, BUILT, SARD_O, SARD_V12):
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
    assert doc.extra["repealed_in_full"] is False, "one repealed article is not a repeal state"
    assert doc.extra["entry_into_force"] == "2020-01-25"
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
    assert con.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 11
    uid = f"normattiva:{_nid('2003-11-15', '003G0333', '2025-10-02', 'CONSOLIDATED/20251002')}"
    row = con.execute("SELECT * FROM documents WHERE uid=?", (uid,)).fetchone()
    worker.stage_rdf(con, row, collection)
    row = con.execute("SELECT * FROM documents WHERE uid=?", (uid,)).fetchone()
    assert row["rdf_done"] == 1 and row["title"].startswith("LEGGE 31 ottobre 2003, n. 306")
    chunks = worker._load_chunks(row, collection)
    assert len(chunks) == 27, "25 articles and 2 annexes"
    assert chunks[0]["chapter"].startswith("CAPO I") and chunks[0]["text"].startswith("Art. 1.")
    # the repeal state is a record with no chunk, never an error
    rid = f"normattiva:{_nid('1701-01-01', '6100206R', '2025-05-09', 'CONSOLIDATED/20250509')}"
    row = con.execute("SELECT * FROM documents WHERE uid=?", (rid,)).fetchone()
    worker.stage_rdf(con, row, collection)
    row = con.execute("SELECT * FROM documents WHERE uid=?", (rid,)).fetchone()
    assert row["rdf_done"] == 1 and row["error"] is None
    assert worker._load_chunks(row, collection) == []


def test_origin_coverage(corpus):
    """tests/origin_coverage.py: an act is parsed from the member fetch
    hashed, and a changed zip is rediscovered."""
    from tests.origin_coverage import assert_covered
    refs = [r for r in _refs(corpus) if r.scope == "document" and r.error is None]
    assert refs
    nsrc._zip_cache.clear()           # discovery opened the zips: fetch must read them
    read = set()
    for ref in refs:
        read |= assert_covered(SOURCE, ref, corpus)
    assert read, "fetch read the zips"


# ---- relations ----------------------------------------------------------------

def test_the_acts_modifications_and_references_are_eli_edges(corpus):
    """06 §6.1 `relations()`, eli-legal-plugins.md §5.4: the statements the
    origin makes about other acts, as typed edges between acts, left
    unresolved (05 §5.5); written by the build as 09 §9.14's lines."""
    from app.hold import records as hold_records
    from app.plugins.base import Edge
    doc = _doc(corpus, M306)
    edges = list(SOURCE.relations(doc))
    assert Edge(to="urn:nir:stato:decreto.legislativo:2000-05-26;187", type="eli:amends") in edges
    assert Edge(to="akn:it/act/legge/stato/1988-08-23/400", type="eli:cites") in edges
    assert all(e.type in ("eli:amends", "eli:repeals", "eli:cites") for e in edges)
    assert all("#" not in e.to and not e.to.endswith("!main") for e in edges), "between acts, never articles"
    assert len(edges) == len({(e.type, e.to) for e in edges}), "one edge per (type, act)"
    amends = {e.to for e in edges if e.type == "eli:amends"}
    assert 1 <= len(amends) <= len(doc.extra["active_modifications"])
    lines = hold_records.edges_of(SOURCE, doc, from_uid=doc.uid, snapshot_id="2026-10")
    assert len(lines) == len(edges) and all(line.endswith(b"\n") for line in lines)
    assert b'"type":"eli:amends"' in lines[0]
    repealed = _doc(corpus, RD206_R)
    assert list(SOURCE.relations(repealed)) == [], "a repeal state states nothing about other acts"
