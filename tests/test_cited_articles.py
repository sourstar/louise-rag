"""A cited article pinned by key (app/ask/articles.py; operator 2026-10-13:
"art. 12 della legge sull'emigrazione testo unico 13 novembre 1919, n.
2205" found the act and never read the article). The act by its URN, the
version in force on the date asked or the shown one, the article's chunk by
its label; a repealed article also with its last text in force. Facts,
never hits (invariant 4)."""
from app import retrieval
from app.ask import articles
from tests.test_validity import O_ID, V12_ID, _run, versioned  # noqa: F401

Q = "art. 8 della legge costituzionale 26 febbraio 1948, n. 3"


def test_the_cited_article_of_the_version_in_force_is_pinned(versioned):
    c, con, _ = versioned
    _run(con, c, "rdf", "fts")
    got = articles.pin(Q, [c])
    assert len(got) == 1
    f = got[0]
    assert f["kind"] == "articolo" and f["entity_id"] == "urn:nir:stato:legge.costituzionale:1948-02-26;3~art8"
    assert f["fields"]["versione"] == f"normattiva:{V12_ID}", "no date asked: the shown version"
    assert f["fields"]["testo"].startswith("Art. 8.") and "a) dai set" in f["fields"]["testo"]
    assert f["fields"]["fino al"] == "oggi" and "art. 8" in f["label"]


def test_a_date_picks_the_version_in_force_then(versioned):
    c, con, _ = versioned
    _run(con, c, "rdf", "fts")
    f = articles.pin(Q, [c], as_of=19990101)[0]
    assert f["fields"]["versione"] == f"normattiva:{O_ID}"
    assert "dai nove" in f["fields"]["testo"], "the original's text, not 2001's"
    assert (f["fields"]["in vigore dal"], f["fields"]["fino al"]) == ("1948-03-10", "2001-02-01")
    assert articles.pin(Q, [c], as_of=19400101) == [], "before the act: nothing in force"


def test_a_repealed_article_brings_its_last_text_in_force(versioned, monkeypatch):
    c, con, _ = versioned
    _run(con, c, "rdf", "fts")
    real = articles._article

    def shown_repealed(collection, native_id, tok):
        ch = real(collection, native_id, tok)
        if int(native_id) == V12_ID and ch is not None:
            return {**ch, "text": "Art. 8.\nARTICOLO ABROGATO DALLA L. 24 LUGLIO 2001, N. 9"}
        return ch
    monkeypatch.setattr(articles, "_article", shown_repealed)
    got = articles.pin(Q, [c])
    assert [g["label"].rsplit("(", 1)[1] for g in got] == ["testo vigente)", "ultimo testo in vigore)"]
    assert "ABROGATO" in got[0]["fields"]["testo"]
    assert "a) dai set" in got[1]["fields"]["testo"] and got[1]["fields"]["fino al"] == "2001-02-16"


def test_no_article_cited_or_no_validity_pins_nothing(versioned):
    c, con, _ = versioned
    assert articles.pin("legge costituzionale 26 febbraio 1948, n. 3", [c]) == []
    assert articles.pin("art. 99 della legge costituzionale 26 febbraio 1948, n. 3", [c]) == []
    assert articles.token("articolo 5-bis") == "5-bis" and articles.token("art.12") == "12"
    assert articles.label_token("TITOLO I — X › Art. 1.") == "1"
    assert articles.label_token("Legge sull'emigrazione-art. 12") == "12"
    assert articles.label_token("Allegato A") is None


def test_the_search_answer_carries_the_article_as_a_fact_never_a_hit(versioned):
    c, con, _ = versioned
    _run(con, c, "rdf", "fts")
    result = retrieval.search(con, Q, k=5, mode="fts", translate=False, collection=c)
    pinned = [f for f in result["facts"] if f["kind"] == "articolo"]
    assert len(pinned) == 1 and "a) dai set" in pinned[0]["fields"]["testo"]
    assert all("~art" not in str(h.get("chunk_id", "")) for h in result["hits"])
    msg = retrieval.build_answer_messages(Q, result)[1]["content"]
    assert "[reference]" in msg and "a) dai set" in msg


def test_a_long_article_is_cut_and_the_cut_said(monkeypatch):
    monkeypatch.setattr(articles, "MAX_CHARS", 20)
    out = articles._capped("Art. 1.\n" + "x" * 100)
    assert out.startswith("Art. 1.\nxxxxxxxxxxxx") and "troncato a 20 di 108 caratteri" in out
    assert articles._capped("short") == "short"


def test_a_citation_in_another_form_finds_the_act_by_date_and_number_without_references(versioned):
    """2026-10-14 on the node: "legge … 13 novembre 1919, n. 2205" names a
    regio decreto-legge, and the reference source had never been synced, so
    nothing resolved. The collection's own keys answer: the URN under each
    form, by primary key."""
    c, con, _ = versioned
    _run(con, c, "rdf", "fts")
    got = articles.pin("art. 8 del decreto-legge 26 febbraio 1948, n. 3", [c])
    assert len(got) == 1 and got[0]["entity_id"].startswith("urn:nir:stato:legge.costituzionale:1948-02-26;3")
    got = articles.pin("art. 8 del testo unico 26 febbraio 1948, n. 3", [c])
    assert len(got) == 1
    assert articles.pin("art. 8 della legge 27 febbraio 1948, n. 3", [c]) == [], "another date: no act"
