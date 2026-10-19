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
