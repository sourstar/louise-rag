"""The COMPILED textifier gives the bytes of its SOURCE (docs/notes/
python-hot-spots.md, "E").

The image compiles app/plugins/wikidata/textify.py and format.py with mypyc
(tools/build_native.py; Dockerfile, stage `native`). An entity's text is in
its record — hashed, signed, embedded — so the compiled module must answer
as the `.py` does, byte for byte, and fail where it fails. mypyc's edges
are where a difference would hide: it CHECKS the annotations at run time
(a `str` that is a number, a `list[dict]` holding a string, a `float` that
is an integer past 2**53 — the annotations of what the dump gives as it is
say `Any`: VENDORED.md), it compiles dataclasses, `__bool__` and `__str__`
natively, and it reads some generator expressions eagerly. So, compiled
against the source loaded from its `.py` by path (app/plugins/wikidata/
pure.py), the text or the same exception, on:

* every entity of tests/fixtures/wikidata-mini, in nine languages, three
  fallbacks and three label tables — and every text any test of the suite
  asks for (tests/conftest.py wraps the compiled functions while they are
  compiled: the plugin's tests and the pack's builds are checks too);
* `GENERATED` seeded entities with every value type the textifier reads
  (tools/measure/textify_identity.py), a share of them malformed;
* the edges, named: values of the wrong JSON type, integers past 2**53,
  NaN and infinities, Unicode digits in ids, years and months, the classes
  and switches no entity text reaches.

Where the extension is not built, the source is compared with itself:
these pass and prove nothing, which `test_the_extension_is_imported_where_
it_was_built` states. The node's own entities: tools/measure/
textify_identity.py (≥ 100,000 before any deploy).
"""
from __future__ import annotations

import importlib.machinery
import json
import random
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.plugins.wikidata import format as fmt
from app.plugins.wikidata import pure, textify
from tools.measure import textify_identity as tid

FIXTURES = Path(__file__).parent / "fixtures" / "wikidata-mini"
GENERATED = 3000
LANGS = ("en", "it", "fr", "de", "es", "ar", "zh", "ja", "pt")
SOURCE = pure.textify()


def fixture_entities() -> list[dict]:
    out = []
    for f in sorted(FIXTURES.glob("*.jsonl")):
        for line in f.read_bytes().split(b"\n"):
            line = line.strip()
            if line and line not in (b"[", b"]"):
                out.append(json.loads(line[:-1] if line.endswith(b",") else line))
    return out


def same(what, obj, lang, labels, **kw) -> None:
    for name in ("entity_text", "history_text"):
        got = tid.outcome(getattr(textify, name), obj, lang, labels, **kw)
        want = tid.outcome(getattr(SOURCE, name), obj, lang, labels, **kw)
        assert got == want, (what, lang, name, kw)


def test_the_source_is_loaded_from_the_py_and_never_an_extension():
    assert SOURCE.__file__.endswith("textify.py") and not pure.is_compiled(SOURCE)
    assert SOURCE.fmt.__file__.endswith("format.py") and not pure.is_compiled(SOURCE.fmt)
    assert SOURCE.names.__file__.endswith("names.py")
    assert SOURCE is pure.textify(), "loaded once a process"
    assert SOURCE.Entity is not textify.Entity or not pure.is_compiled(textify)


def test_the_extension_is_imported_where_it_was_built():
    """Python takes an extension before the `.py` of the same name: built for
    this interpreter, the live modules are the compiled ones."""
    here = Path(textify.__spec__.origin).parent
    suffix = importlib.machinery.EXTENSION_SUFFIXES[0]
    built = {name: (here / f"{name}{suffix}").exists() for name in pure.COMPILED}
    assert pure.compiled() == built, (pure.compiled(), built)


def test_the_suite_checks_every_text_against_the_source_where_compiled():
    wrapped = getattr(textify.entity_text, "__qualname__", "").startswith("compiled_textify_answers_as_its_source")
    assert wrapped == pure.compiled()["textify"]


def test_every_fixture_entity_in_every_language_fallback_and_label_table():
    entities = fixture_entities()
    assert len(entities) >= 6
    own = {(e["id"], lang): v["value"] for e in entities for lang, v in (e.get("labels") or {}).items()}
    tables = (lambda eid, lang: own.get((eid, lang)), lambda eid, lang: None, lambda eid, lang: f"<{eid}>")
    for obj in entities:
        for lang in LANGS:
            for fallback in ("en", None, "fr"):
                for labels in tables:
                    for codes in (None, tid.CODES):
                        same(obj["id"], obj, lang, labels, fallback=fallback, codes=codes)


def test_generated_entities_of_every_value_type():
    labels = tid.labels_for(0)
    # the compiled functions themselves, not the suite's checked wrappers (conftest.py)
    live = SimpleNamespace(**{name: getattr(getattr(textify, name), "__wrapped__", getattr(textify, name))
                              for name in ("entity_text", "history_text")})
    texts = raised = 0
    for k, (name, obj, langs, fallback) in enumerate(tid.generated(0, 0, GENERATED)):
        diffs, _, _, both = tid.compare(obj, langs, labels, tid.CODES if k % 5 else None, fallback,
                                        live, SOURCE, flip=bool(k % 2))
        assert not diffs, (name, diffs)
        raised += both
        texts += 2 * len(langs) - both
    # most texts render whole; the malformed share raises in both, as it must
    assert texts > 8 * raised > 0


def test_the_generated_set_is_seeded():
    a = json.dumps([o for _, o, _, _ in tid.generated(7, 100, 20)], sort_keys=True, default=str)
    b = json.dumps([o for _, o, _, _ in tid.generated(7, 100, 20)], sort_keys=True, default=str)
    assert a == b


def _st(snak, rank="normal", **qualifiers):
    st = {"mainsnak": snak, "type": "statement", "rank": rank}
    if qualifiers:
        st["qualifiers"] = qualifiers
    return st


def _snak(dt, value, vtype, snaktype="value", pid="P1"):
    s = {"snaktype": snaktype, "property": pid, "datatype": dt}
    if snaktype == "value":
        s["datavalue"] = {"value": value, "type": vtype}
    return s


def _entity(claims=None, **kw):
    e = {"type": "item", "id": "Q1", "labels": {"en": {"language": "en", "value": "X"}}, "claims": claims or {}}
    e.update(kw)
    return e


JUNK = (5, 0, -1, 1.5, True, None, [], ["a"], {}, {"a": 1}, "", 10 ** 30, float("nan"))
BIG = (2 ** 53 + 1, 12345678901234567891, 10 ** 400, -(10 ** 20) - 1, float("inf"), -float("inf"),
       float("nan"), -0.0, 1e308, 5e-324, True, 45, 59.99999999)


def edge_entities() -> dict[str, dict]:
    """mypyc's edges, each a named entity: what the dump never carries but
    the source still answers (a text or an exception), and what it does."""
    out: dict[str, dict] = {}
    for v in JUNK:
        out[f"label value {v!r}"] = _entity(labels={"en": {"language": "en", "value": v}})
        out[f"bare label {v!r}"] = _entity(labels={"en": v})
        out[f"description {v!r}"] = _entity(descriptions={"en": {"value": v}})
        out[f"alias {v!r}"] = _entity(aliases={"en": [{"value": v}, v]})
        out[f"monolingual language {v!r}"] = _entity(
            {"P1": [_st(_snak("monolingualtext", {"text": "t", "language": v}, "monolingualtext"))]})
        out[f"monolingual text {v!r}"] = _entity(
            {"P1": [_st(_snak("monolingualtext", {"text": v, "language": "fr"}, "monolingualtext"))]})
        out[f"entity-type {v!r}"] = _entity(
            {"P1": [_st(_snak("wikibase-item", {"numeric-id": 5, "entity-type": v}, "wikibase-entityid"))]})
        out[f"numeric-id {v!r}"] = _entity(
            {"P1": [_st(_snak("wikibase-item", {"numeric-id": v, "entity-type": "item"}, "wikibase-entityid"))]})
        out[f"entity id {v!r}"] = _entity({"P1": [_st(_snak("wikibase-item", {"id": v}, "wikibase-entityid"))]})
        out[f"rank {v!r}"] = _entity({"P1": [_st(_snak("string", "s", "string"), rank=v)]})
        out[f"datatype {v!r}"] = _entity({"P1": [_st(_snak(v, "s", "string"))]})
        out[f"snaktype {v!r}"] = _entity({"P1": [_st(_snak("string", "s", "string", snaktype=v))]})
        out[f"string {v!r}"] = _entity({"P1": [_st(_snak("string", v, "string"))]})
        out[f"time {v!r}"] = _entity({"P1": [_st(_snak("time", {"time": v, "precision": 11}, "time"))]})
        out[f"precision {v!r}"] = _entity(
            {"P1": [_st(_snak("time", {"time": "+2001-02-03T00:00:00Z", "precision": v}, "time"))]})
        out[f"calendar {v!r}"] = _entity({"P1": [_st(_snak(
            "time", {"time": "+2001-02-03T00:00:00Z", "precision": 11, "calendarmodel": v}, "time"))]})
        out[f"amount {v!r}"] = _entity({"P1": [_st(_snak("quantity", {"amount": v, "unit": "1"}, "quantity"))]})
        out[f"unit {v!r}"] = _entity({"P1": [_st(_snak("quantity", {"amount": "+5", "unit": v}, "quantity"))]})
        out[f"bound {v!r}"] = _entity({"P1": [_st(_snak(
            "quantity", {"amount": "+5", "upperBound": v, "lowerBound": "+4"}, "quantity"))]})
        out[f"globe {v!r}"] = _entity({"P1": [_st(_snak(
            "globe-coordinate", {"latitude": 1.5, "longitude": 3.5, "globe": v}, "globecoordinate"))]})
        out[f"qualifiers {v!r}"] = _entity({"P1": [_st(_snak("string", "s", "string"))]})
        out[f"qualifiers {v!r}"]["claims"]["P1"][0]["qualifiers"] = v
        out[f"qualifier snaks {v!r}"] = _entity({"P1": [_st(_snak("string", "s", "string"), P2=v)]})
        out[f"qualifier snak {v!r}"] = _entity({"P1": [_st(_snak("string", "s", "string"), P2=[v])]})
        out[f"claims {v!r}"] = _entity(v)
        out[f"statements {v!r}"] = _entity({"P1": v})
        out[f"statement {v!r}"] = _entity({"P1": [v], "P2": [_st(_snak("string", "s", "string", pid="P2"),
                                                                   rank="preferred"), v]})
        out[f"mainsnak {v!r}"] = _entity({"P1": [{"mainsnak": v, "rank": "normal"}]})
        out[f"datavalue {v!r}"] = _entity({"P1": [_st({"snaktype": "value", "datatype": "string",
                                                       "datavalue": v})]})
        out[f"id {v!r}"] = _entity(id=v)
    for v in BIG:
        out[f"latitude {v!r}"] = _entity({"P625": [_st(_snak(
            "globe-coordinate", {"latitude": v, "longitude": v, "precision": 0.0001}, "globecoordinate"))]})
        out[f"coordinate precision {v!r}"] = _entity({"P625": [_st(_snak(
            "globe-coordinate", {"latitude": 12.3456, "longitude": -7.1, "precision": v}, "globecoordinate"))]})
        out[f"numeric-id {v!r} big"] = _entity({"P1": [_st(_snak(
            "wikibase-item", {"numeric-id": v, "entity-type": "item"}, "wikibase-entityid"))]})
    for year in ("+0", "-0", "+1", "-1", "+99999999999999999999999", "-13800000000", "+00000002001", "+١٢٣",
                 "+0000000000000000000000000000000000000001"):
        for precision in range(-1, 16):
            out[f"time {year} precision {precision}"] = _entity({"P585": [_st(_snak(
                "time", {"time": f"{year}-01-02T00:00:00Z", "precision": precision,
                         "calendarmodel": "http://www.wikidata.org/entity/Q1985786"}, "time"))]})
    for month in ("00", "١٢", "13", "99"):
        for precision in (9, 10, 11):
            out[f"month {month} precision {precision}"] = _entity({"P585": [_st(_snak(
                "time", {"time": f"+2001-{month}-02T00:00:00Z", "precision": precision}, "time"))]})
    for amount, upper, lower in (("+5", "+6", "+4"), ("+5", "NaN", "+4"), ("-0.5", "+1e3", "-1e-3"),
                                 ("+1e999999", "+1", "+1"), ("abc", "+1", "+0"), ("+5", "+5", "+5"),
                                 ("+123456789012345678901234567890", "+123456789012345678901234567891", "+0")):
        out[f"quantity {amount} {upper} {lower}"] = _entity({"P2048": [_st(_snak("quantity", {
            "amount": amount, "upperBound": upper, "lowerBound": lower,
            "unit": "http://www.wikidata.org/entity/Q11573"}, "quantity"))]})
    for eid in ("Q١٢", "Q²", "Q①", "Q", "Q0", "Q-1", "Q 1", "Q1\n", "P٣", "L7-F2", "Ⅻ"):
        out[f"value id {eid!r}"] = _entity({"P1": [_st(_snak("wikibase-item", {"id": eid}, "wikibase-entityid"))]})
    out["no labels at all"] = _entity(labels={}, sitelinks={"frwiki": {}, "zh_min_nanwiki": {}, "wikimania": {}})
    out["names by the original language"] = _entity(
        {"P407": [_st(_snak("wikibase-item", {"id": "Q7737"}, "wikibase-entityid", pid="P407"))]},
        labels={"ru": {"value": "Москва"}, "fr": {"value": "Moscou"}, "ja": {"value": "モスクワ"}})
    out["history"] = _entity({
        "P1": [_st(_snak("string", "a", "string"), rank="preferred")]
        + [_st(_snak("string", f"b{i}", "string"), P580=[_snak("time", {
            "time": f"+{2000 + i % 3}-0{1 + i % 9}-02T00:00:00Z", "precision": 11 - i % 3}, "time", pid="P580")])
           for i in range(14)],
        "P31": [_st(_snak("wikibase-item", {"id": "Q5"}, "wikibase-entityid", pid="P31"))],
        "P2048": [_st(_snak("quantity", {"amount": "+1"}, "quantity"), rank="preferred"),
                  _st(_snak("quantity", {"amount": "+2"}, "quantity"))]})
    return out


@pytest.mark.parametrize("lang", ("en", "it", "ja"))
def test_the_named_edges(lang):
    labels = {"P1": "one", "P2": "two", "P31": "instance of", "P580": "start time", "P585": "point in time",
              "P625": "coordinates", "P2048": "height", "Q5": "human", "Q11573": "metre", "Q7737": "Russian"}
    for name, obj in edge_entities().items():
        for codes in (None, {"Q7737": "ru"}):
            for fallback in ("en", None):
                same(name, obj, lang, lambda eid, _lang: labels.get(eid), fallback=fallback, codes=codes)


def test_a_str_or_dict_subclass_reads_as_the_dump_s_own():
    class S(str):
        pass

    class D(dict):
        pass
    obj = D(_entity({S("P1"): [D(_st(D(_snak("string", S("s"), "string"))))]},
                    labels=D({S("en"): D({"value": S("lab")})})))
    same("subclasses", obj, S("en"), lambda eid, lang: S(f"<{eid}>"), fallback=S("en"))


def test_what_no_entity_text_reaches_the_classes_and_the_switches():
    """`Normalizer`'s switches (`external_ids`, `qualifiers`), a deprecated
    value, `Special.__str__`, a falsy value's text: no entity text asks for
    them, a caller may."""
    labels = (lambda eid, lang: {"P1": "one", "P2": "two", "P3": "ext"}.get(eid))
    obj = _entity({"P1": [_st(_snak("string", "s", "string"), P2=[_snak("string", "q", "string", pid="P2")])],
                   "P3": [_st(_snak("external-id", "X1", "string", pid="P3"))]})
    for ext in (True, False):
        for quals in (True, False):
            texts = [m.Normalizer(lang="en", labels=labels, external_ids=ext, qualifiers=quals).normalize(obj)
                     .to_text("en") for m in (textify, SOURCE)]
            assert texts[0] == texts[1]
    for m in (textify, SOURCE):
        assert str(m.Special("novalue")) == "no value" and m.Special("somevalue").to_text("it") == "valore sconosciuto"
    out = []
    for m in (textify, SOURCE):
        claim = m.Claim(subject=m.Entity(id="Q1"), property=m.Entity(id="P1", label="one"))
        claim.values = [m.ClaimValue(claim=claim, value=m.Text(text="v"), rank="deprecated"),
                        m.ClaimValue(claim=claim, value=m.Text(text=None))]
        out.append((claim.to_text("en"), claim.values[1].to_text("en"), str(m.MonolingualText("t", "fr")),
                    bool(m.Formatted("")), bool(m.Entity(id="Q1", label="")), str(m.Text())))
    assert out[0] == out[1] == ("one: v [deprecated]", "", "fr:t", False, False, "")


def test_the_formatter_alone():
    rng = random.Random(1)
    for _ in range(3000):
        for value, fn in ((tid._time(rng), "time_text"), (tid._quantity(rng), "quantity_text"),
                          (tid._coordinate(rng), "coordinate_text")):
            for lang in ("en", "fr", "ja"):
                args = (value, lang) if fn != "quantity_text" else (value,)
                assert tid.outcome(getattr(fmt, fn), *args) == tid.outcome(getattr(SOURCE.fmt, fn), *args)
    for deg in (2 ** 53 + 1, 12345678901234567891, 10 ** 30, 59.99999999, -0.0, 7):
        assert fmt._dms(deg, 2) == SOURCE.fmt._dms(deg, 2)


def test_the_checker_claims_nothing_on_nothing_or_on_the_source(capsys):
    """Zero entities is INCOMPLETE; the source against itself is NOT
    COMPILED; a difference is DIFFERENT — never IDENTICAL."""
    assert tid.main(["--generated", "0"]) == 2
    code = tid.main(["--generated", "40", "--seed", "3"])
    assert code == (0 if all(pure.compiled().values()) else 3)
    assert '"compared": 40' in capsys.readouterr().out


def test_the_checker_names_a_difference(monkeypatch, tmp_path, capsys):
    class Off:
        @staticmethod
        def entity_text(obj, lang, labels, **kw):
            return SOURCE.entity_text(obj, lang, labels, **kw) + "."

        history_text = staticmethod(SOURCE.history_text)
    monkeypatch.setattr(tid, "_implementations", lambda: (Off, SOURCE, {"textify": True, "format": True}))
    out = tmp_path / "diff.jsonl"
    assert tid.main(["--generated", "10", "--diff-out", str(out)]) == 1
    rows = [json.loads(line) for line in out.read_text().splitlines()]
    assert len(rows) == 10 and rows[0]["differences"][0]["function"] == "entity_text" and "json" in rows[0]
    assert "DIFFERENT" in capsys.readouterr().err


def test_the_checker_reads_a_pack_as_the_plugin_does(tmp_path, capsys):
    """München's mode: the dump's pack, row group by row group, pruned
    entities skipped, every language asked, the pack's labels and codes."""
    from app.plugins.wikidata import pack
    from tests.test_plugins_wikidata_source import a_dump
    pack.reset_caches()
    dump = a_dump(tmp_path)
    assert pack.build(dump, languages=("en", "fr")).get("complete")
    try:
        code = tid.main(["--dump", str(dump), "--lang", "en", "--lang", "fr", "--entities", "5"])
        summary = json.loads(capsys.readouterr().out)
    finally:
        pack.reset_caches()
    assert summary["read"] == 6 and summary["compared"] == 5, "the scholarly article is pruned, as discover skips it"
    assert summary["calls"] == 5 * 2 * 2 and summary["differ"] == 0 and summary["languages"] == ["en", "fr"]
    assert code == (0 if all(pure.compiled().values()) else 3)
    assert summary["verdict"] == ("IDENTICAL" if code == 0 else "NOT COMPILED")
    assert tid.main(["--dump", str(dump), "--entities", "6"]) == 2, "fewer than asked: INCOMPLETE"


def test_the_build_compiles_what_the_package_says():
    from tools import build_native
    modules, group = build_native._plan()
    assert modules == pure.COMPILED and group == pure.GROUP
    suffix = importlib.machinery.EXTENSION_SUFFIXES[0]
    assert [p.name for p in build_native.outputs()] == [f"{m}{suffix}" for m in (*modules, f"{group}__mypyc")]
    root = Path(__file__).resolve().parents[1]
    assert "*.so" in (root / ".gitignore").read_text().split()
    assert "**/*.so" in (root / ".dockerignore").read_text().split()


def test_a_tool_that_subclasses_the_textifier_subclasses_its_source():
    """A compiled class cannot be subclassed from Python: the coverage tool's
    lookups subclass the source's `Normalizer` (its `_label` override would
    otherwise fail, and be counted as an entity the parse fails on)."""
    from tools import wikidata_coverage as wc
    assert issubclass(wc._Lookups, SOURCE.Normalizer) and issubclass(wc._Chain, SOURCE.Normalizer)
    look = wc._Lookups()
    look.normalize(_entity({"P31": [_st(_snak("wikibase-item", {"id": "Q5"}, "wikibase-entityid", pid="P31"))]}))
    assert look.asked == ["P31", "Q5"]
