"""tests/test_plugins_wikidata_textify.py — an entity's text (docs/notes/
wikidata-build-plan.md §2, D4 revised): Wikimedia's textifier vendored, the
labels from a table of the dump, dates/coordinates/quantities by our own
formatter; a pure function of (entity, labels, lang), so two runs give the
same bytes and no service is asked."""
import json
from pathlib import Path

import pytest

from app.plugins.wikidata import format as fmt, textify

FIXTURES = Path(__file__).parent / "fixtures" / "wikidata-mini"


def entities(name: str) -> dict[str, dict]:
    out = {}
    for line in (FIXTURES / name).read_bytes().split(b"\n"):
        line = line.strip()
        if not line or line in (b"[", b"]"):
            continue
        obj = json.loads(line[:-1] if line.endswith(b",") else line)
        out[obj["id"]] = obj
    return out


#: a label table, as the plugin's first pass over the dump would give it
LABELS = {
    ("P31", "en"): "instance of", ("P17", "en"): "country", ("P131", "en"): "located in the administrative "
    "territorial entity", ("P625", "en"): "coordinate location", ("P1619", "en"): "date of official opening",
    ("P137", "en"): "operator", ("P580", "en"): "start time", ("P582", "en"): "end time",
    ("P3417", "en"): "Quora topic ID", ("P2671", "en"): "Google Knowledge Graph ID",
    ("Q2755432", "en"): "data compression algorithm", ("Q142", "en"): "France", ("Q3351", "en"): "Lyon",
    ("Q3337", "en"): "Lyon Metropolis", ("Q2354973", "en"): "road tunnel", ("Q16665897", "en"): "Métropole de Lyon",
    ("Q2874", "en"): "Grand Lyon",
    ("P31", "fr"): "nature de l'élément", ("Q2755432", "fr"): "algorithme de compression de données",
}


def labels(eid: str, lang: str):
    return LABELS.get((eid, lang))


def test_the_text_of_a_real_entity_is_a_pure_function_and_asks_no_service():
    q = entities("dump-sample.jsonl")["Q3331"]
    text = textify.entity_text(q, "en", labels)
    assert text.startswith("tunnel de la Croix-Rousse, road tunnel in Lyon. Attributes include:\n- ")
    lines = text.split("\n- ")[1:]
    assert "instance of: road tunnel" in lines
    assert "country: France" in lines
    assert "located in the administrative territorial entity: Lyon, Lyon Metropolis" in lines
    # the date by its precision (11 = day), from our formatter, not wbformatvalue
    assert "date of official opening: 19 April 1952" in lines
    # the coordinate in DMS, the seconds rounded by the dump's precision (2.78e-06° ≈ 0.01")
    assert "coordinate location: 45°46'21.64\"N, 4°50'12.19\"E" in lines
    # a preferred statement wins over the normal one of the same property; qualifiers in brackets
    assert "operator: Métropole de Lyon (start time: 1 January 2015)" in lines
    assert not any(l.startswith("operator: Grand Lyon") for l in lines), "the normal-rank operator is left out"
    # a property with no label in the table drops its claim (upstream); a value without one is its id
    assert not any("P373" in l or "P18" in l or "P646" in l for l in lines)
    assert textify.entity_text(q, "en", labels) == text, "the same bytes, twice"
    q2 = entities("dump-sample.jsonl")["Q2711"]
    assert textify.entity_text(q2, "en", labels) == (
        "Implode, data compression algorithm. Attributes include:\n- instance of: data compression algorithm"
        "\n- Quora topic ID: Implode\n- Google Knowledge Graph ID: /g/1227d2cb")


def test_a_value_without_a_label_is_its_id_and_a_language_falls_back():
    q2 = entities("dump-sample.jsonl")["Q2711"]
    assert textify.entity_text(q2, "en", lambda eid, lang: {"P31": "instance of"}.get(eid)) == \
        "Implode, data compression algorithm. Attributes include:\n- instance of: Q2755432"
    fr = textify.entity_text(q2, "fr", labels)
    assert fr.startswith("Implode, algorithme de compression. Ses attributs incluent:\n- ")
    assert "nature de l'élément: algorithme de compression de données" in fr
    assert "Quora topic ID: Implode" in fr, "a property with no French label takes the English one (fallback)"


def test_a_referenced_label_falls_back_to_mul_before_the_fallback_language():
    """Upstream's `get_lang_val` chain — the language, `mul`, the fallback —
    for every referenced entity, as for the entity's own label: Wikidata's
    default (`mul`) label is the name in every language; parser 1 wrote the
    id where only `mul` had one."""
    q2 = entities("dump-sample.jsonl")["Q2711"]
    table = {("P31", "en"): "instance of", ("Q2755432", "mul"): "Implode family", ("P31", "fr"): "nature"}
    asked = []

    def look(eid, lang):
        asked.append((eid, lang))
        return table.get((eid, lang))
    en = textify.entity_text(q2, "en", look)
    assert "instance of: Implode family" in en
    assert ("Q2755432", "en") in asked and asked.index(("Q2755432", "en")) < asked.index(("Q2755432", "mul"))
    assert "nature: Implode family" in textify.entity_text(q2, "fr", look), "mul before the fallback language"
    table[("Q2755432", "en")] = "data compression algorithm"
    assert "instance of: data compression algorithm" in textify.entity_text(q2, "en", look), \
        "the language's own label wins over mul"
    table[("Q2755432", "en")] = None
    del table[("Q2755432", "mul")]
    assert "instance of: Q2755432" in textify.entity_text(q2, "en", look), "no label in the chain: the id, as before"


def test_italian_has_its_own_connectives():
    q2 = entities("dump-sample.jsonl")["Q2711"]
    it = textify.entity_text(q2, "it", lambda eid, lang: {("P31", "it"): "istanza di"}.get((eid, lang)))
    assert ". I suoi attributi includono:\n- istanza di: Q2755432" in it
    assert textify.LANGUAGE_VARIABLES["it"].keys() == textify.LANGUAGE_VARIABLES["en"].keys()


def test_aliases_keep_the_dump_s_order_and_every_datavalue_type_renders():
    q42 = entities("entities.jsonl")["Q42"]
    text = textify.entity_text(q42, "en", lambda eid, lang: f"<{eid}>")
    first = text.split(". Attributes include")[0]
    aliases = q42.get("aliases", {}).get("en", [])
    if aliases:
        assert ", also known as " + ", ".join(dict.fromkeys(a["value"] for a in aliases)) in first
    assert textify.entity_text(q42, "en", lambda eid, lang: f"<{eid}>") == text


@pytest.mark.parametrize("value,lang,expected", [
    ({"time": "+1952-04-19T00:00:00Z", "precision": 11, "calendarmodel": "http://www.wikidata.org/entity/Q1985727"}, "en", "19 April 1952"),
    ({"time": "+1952-04-19T00:00:00Z", "precision": 11}, "de", "19. April 1952"),
    ({"time": "+1952-04-19T00:00:00Z", "precision": 11}, "es", "19 de abril de 1952"),
    ({"time": "+1952-04-19T00:00:00Z", "precision": 11}, "zh", "1952-04-19"),
    ({"time": "+1952-04-00T00:00:00Z", "precision": 10}, "en", "April 1952"),
    ({"time": "+1952-00-00T00:00:00Z", "precision": 9}, "en", "1952"),
    ({"time": "+1952-00-00T00:00:00Z", "precision": 8}, "en", "1950s"),
    ({"time": "+1952-00-00T00:00:00Z", "precision": 7}, "en", "20th century"),
    ({"time": "+1952-00-00T00:00:00Z", "precision": 6}, "en", "2nd millennium"),
    ({"time": "-0500-00-00T00:00:00Z", "precision": 9}, "en", "500 BCE"),
    ({"time": "-0044-03-15T00:00:00Z", "precision": 11, "calendarmodel": "http://www.wikidata.org/entity/Q1985786"}, "en", "15 March 44 BCE (Julian)"),
    ({"time": "+0065000000-00-00T00:00:00Z", "precision": 3}, "en", "65 million years"),
    ({"time": "+2001-01-01T00:00:00Z", "precision": 7}, "en", "21st century"),
    ({"time": "+2000-01-01T00:00:00Z", "precision": 7}, "en", "20th century"),
])
def test_time_by_precision_calendar_and_language(value, lang, expected):
    assert fmt.time_text(value, lang) == expected


def test_coordinates_and_quantities():
    assert fmt.coordinate_text({"latitude": 45.772677777778, "longitude": 4.8367194444444,
                                "precision": 2.7777777777778e-06, "globe": "http://www.wikidata.org/entity/Q2"}) == \
        "45°46'21.64\"N, 4°50'12.19\"E"
    assert fmt.coordinate_text({"latitude": -33.8688, "longitude": 151.2093, "precision": 0.0001}) == \
        "33°52'7.7\"S, 151°12'33.5\"E"
    assert fmt.coordinate_text({"latitude": 0.0, "longitude": -45.0, "precision": None}) == "0°0'0\"N, 45°0'0\"W"
    assert fmt.coordinate_text({"latitude": 18.65, "longitude": 226.2, "precision": 1,
                                "globe": "http://www.wikidata.org/entity/Q111"},
                               globe_label=lambda g: "Mars") == "18°39'0\"N, 226°12'0\"E (Mars)"
    assert fmt.quantity_text({"amount": "+10", "unit": "http://www.wikidata.org/entity/Q11573"},
                             unit_label=lambda u: "metre") == "10 metre"
    assert fmt.quantity_text({"amount": "-3.5", "unit": "1"}) == "-3.5"
    assert fmt.quantity_text({"amount": "+7", "unit": "http://www.wikidata.org/entity/Q999"}) == "7 Q999"
    with pytest.raises(fmt.FormatError):
        fmt.time_text({"time": "1952-04-19", "precision": 11})


def _snak(pid, snaktype="value", datavalue=None, datatype="wikibase-item"):
    out = {"snaktype": snaktype, "property": pid, "datatype": datatype}
    if datavalue is not None:
        out["datavalue"] = datavalue
    return out


def test_unknown_and_no_value_read_as_wikibase_shows_them_with_their_qualifiers():
    """Upstream wrote "has <property>" for both special values — for
    `novalue` the opposite of the statement — and dropped their qualifiers,
    which is where an unknown date keeps its bounds."""
    e = {"id": "Q900", "labels": {"en": {"language": "en", "value": "Old tower"}}, "claims": {
        "P84": [{"mainsnak": _snak("P84", "novalue"), "rank": "normal"}],
        "P570": [{"mainsnak": _snak("P570", "somevalue", datatype="time"), "rank": "normal",
                  "qualifiers": {"P1319": [_snak("P1319", datavalue={"type": "time", "value": {
                      "time": "+1450-00-00T00:00:00Z", "precision": 9, "timezone": 0, "before": 0, "after": 0,
                      "calendarmodel": "http://www.wikidata.org/entity/Q1985727"}}, datatype="time")],
                      "P582": [_snak("P582", "novalue", datatype="time")]}}],
    }}
    table = {("P84", "en"): "architect", ("P570", "en"): "date of death", ("P1319", "en"): "earliest date",
             ("P582", "en"): "end time", ("P84", "it"): "architetto"}
    text = textify.entity_text(e, "en", lambda eid, lang: table.get((eid, lang)))
    assert "- architect: no value" in text and "has architect" not in text
    assert "- date of death: unknown value (earliest date: 1450, end time: no value)" in text
    assert "architetto: nessun valore" in textify.entity_text(e, "it", lambda eid, lang: table.get((eid, lang)))


def test_a_lexeme_value_is_its_id_and_a_quantity_keeps_its_uncertainty():
    e = {"id": "Q901", "labels": {"en": {"language": "en", "value": "Tower"}}, "claims": {
        "P5137": [{"mainsnak": _snak("P5137", datavalue={"type": "wikibase-entityid", "value": {
            "entity-type": "lexeme", "numeric-id": 7, "id": "L7"}}, datatype="wikibase-lexeme"), "rank": "normal"}],
        "P2048": [{"mainsnak": _snak("P2048", datavalue={"type": "quantity", "value": {
            "amount": "+52", "unit": "http://www.wikidata.org/entity/Q11573", "upperBound": "+53",
            "lowerBound": "+50.5"}}, datatype="quantity"), "rank": "normal"}],
    }}
    table = {("P5137", "en"): "item for this sense", ("P2048", "en"): "height", ("Q11573", "en"): "metre"}
    text = textify.entity_text(e, "en", lambda eid, lang: table.get((eid, lang)))
    assert "- item for this sense: L7" in text and "entity-type" not in text, "never the datavalue's repr"
    assert "- height: 52±1.5 metre" in text, "± the larger distance to a bound, as Wikibase shows it"
    assert fmt.quantity_text({"amount": "+52", "unit": "1"}) == "52", "no bounds: the amount alone"
    assert fmt.quantity_text({"amount": "+52.00", "unit": "1", "upperBound": "+52.50",
                              "lowerBound": "+51.75"}) == "52.00±0.50"
    assert fmt.quantity_text({"amount": "+3", "unit": "1", "upperBound": "+3", "lowerBound": "+3"}) == "3"


# ---- the history section (parser 2, operator 2026-10-24: "Go with family slices and the history
# ---- section now"; wikidata-completeness-report.md §4) ------------------------------------------

def _time(t: str, precision: int = 11) -> dict:
    return {"type": "time", "value": {"time": t, "precision": precision, "timezone": 0, "before": 0, "after": 0,
                                      "calendarmodel": "http://www.wikidata.org/entity/Q1985727"}}


def _item(n: int) -> dict:
    return {"type": "wikibase-entityid", "value": {"entity-type": "item", "numeric-id": n, "id": f"Q{n}"}}


def _st(pid, value, rank="normal", datatype="wikibase-item", **qualifiers):
    out = {"mainsnak": _snak(pid, datavalue=value, datatype=datatype), "rank": rank}
    if qualifiers:
        out["qualifiers"] = {q: [_snak(q, datavalue=_time(t), datatype="time")] for q, t in qualifiers.items()}
    return out


def test_the_history_section_holds_what_the_rank_filter_drops_and_the_text_stays_as_it_was():
    q = entities("dump-sample.jsonl")["Q3331"]
    assert textify.history_text(q, "en", labels) == (
        "tunnel de la Croix-Rousse, road tunnel in Lyon. Other statements include:\n"
        "- instance of: road tunnel\n"
        "- operator: Grand Lyon (end time: 1 January 2015)"), \
        "the head names it, its `instance of` first (every window repeats it), the dropped statement dated"
    text = textify.entity_text(q, "en", labels)
    assert "Grand Lyon" not in text and "Other statements" not in text, "the main text is upstream's, unchanged"
    assert textify.history_text(q, "en", labels) == textify.history_text(q, "en", labels), "a pure function"


def test_history_keeps_ten_a_property_the_most_recent_first_and_leaves_series_and_ids_in_the_record():
    mayors = [_st("P6", _item(100 + i), P582=f"+{1900 + i:04d}-00-00T00:00:00Z") for i in (3, 11, 0, 7, 1, 9, 2, 10,
                                                                                         5, 4, 8, 6)]
    e = {"id": "Q950", "labels": {"en": {"language": "en", "value": "Bologna"}},
         "aliases": {"en": [{"language": "en", "value": "Bononia"}]},
         "claims": {
             "P31": [_st("P31", _item(515))],
             "P6": [_st("P6", _item(99), rank="preferred", P580="+2021-10-11T00:00:00Z"),
                    _st("P6", _item(98)), *mayors,
                    {**_st("P6", _item(97), P582="+2999-00-00T00:00:00Z"), "rank": "deprecated"}],
             "P1082": [_st("P1082", {"type": "quantity", "value": {"amount": "+390000", "unit": "1"}},
                           rank="preferred", datatype="quantity"),
                       _st("P1082", {"type": "quantity", "value": {"amount": "+370000", "unit": "1"}},
                           datatype="quantity", P585="+2001-00-00T00:00:00Z")],
             "P214": [_st("P214", "123", rank="preferred", datatype="external-id"),
                      _st("P214", "456", datatype="external-id")],
             "P17": [_st("P17", _item(38))],
         }}
    table = {("P31", "en"): "instance of", ("P6", "en"): "head of government", ("P582", "en"): "end time",
             ("P580", "en"): "start time", ("P1082", "en"): "population", ("P214", "en"): "VIAF ID",
             ("P17", "en"): "country", ("Q515", "en"): "city", ("Q38", "en"): "Italy",
             **{(f"Q{n}", "en"): f"Mayor {n}" for n in range(97, 113)}}
    history = textify.history_text(e, "en", lambda eid, lang: table.get((eid, lang)))
    head, lines = history.split(":\n- ")[0], history.split(":\n- ", 1)[1].split("\n- ")
    assert head == "Bologna. Other statements include", "the aliases are in the main text, not repeated"
    assert lines[0] == "instance of: city"
    assert lines[1] == ("head of government: " + ", ".join(
        f"Mayor {100 + i} (end time: {1900 + i})" for i in (11, 10, 9, 8, 7, 6, 5, 4, 3, 2))), \
        "ten, the latest end time first; the eleventh and twelfth and the undated Mayor 98 wait"
    assert len(lines) == 2, "no population series, no withdrawn VIAF id, no property without a preferred one"
    assert "Mayor 97" not in history, "deprecated never"
    assert "Mayor 99" not in history, "the preferred value is the main text's"


def test_no_history_without_a_preferred_statement_and_dates_order_by_end_then_start_then_point():
    plain = {"id": "Q951", "labels": {"en": {"language": "en", "value": "Village"}},
             "claims": {"P17": [_st("P17", _item(38)), _st("P17", _item(39))]}}
    assert textify.history_text(plain, "en", labels) == "", "two normal values are both the text's"
    sts = [_st("P1", _item(1)),                                            # undated: last, in the dump's order
           _st("P1", _item(2), P585="+1500-00-00T00:00:00Z"),
           _st("P1", _item(3), P580="+1600-05-00T00:00:00Z"),
           _st("P1", _item(4), P582="-0044-03-15T00:00:00Z", P580="+1700-00-00T00:00:00Z"),   # end time wins
           _st("P1", _item(5)),
           _st("P1", _item(6), P580="+1600-05-02T00:00:00Z")]
    order = [s["mainsnak"]["datavalue"]["value"]["id"] for s in textify.most_recent_first(sts)]
    assert order == ["Q6", "Q3", "Q2", "Q4", "Q1", "Q5"]
