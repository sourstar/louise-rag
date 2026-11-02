"""DIAGNOSTIC (read-only): does the COMPILED wikidata textifier give the same
bytes as its SOURCE, entity for entity?

The image compiles app/plugins/wikidata/textify.py and format.py with mypyc
(docs/notes/python-hot-spots.md, "E"; tools/build_native.py). An entity's
text is part of its record — hashed, signed, embedded — so one changed byte
is a new parser version and every wikidata record rebuilt (CLAUDE.md
invariants 3 and 11; 03 §3.4: the same parser version IS the same records).
This runs the plugin's own calls — `entity_text` and `history_text`, for
each language the collection renders, with the pack's label table and
language codes, exactly as `source.parse` makes them — through the module
Python imports (the compiled one, in an image that built it) and through
the source loaded from its `.py` by path, which no extension can shadow
(app/plugins/wikidata/pure.py), and compares every output: the text, or the
exception's type and message (the same failure is the same answer).

BEFORE ANY DEPLOY OF AN IMAGE THAT COMPILES IT, München runs it IN THAT
IMAGE on at least 100,000 of the node's own entities, and deploys only on
IDENTICAL:

  python -m tools.measure.textify_identity --collection wikidata --entities 100000
  python -m tools.measure.textify_identity --dump PATH [--lang en …] --entities 100000

Without a dump, on seeded generated entities (every value type the
textifier reads, malformed ones included; the image's build stage runs it):

  python -m tools.measure.textify_identity --generated 200000 [--seed N]

THE VERDICT. IDENTICAL needs the live modules COMPILED (an image without
the extension compares the source with itself: NOT COMPILED, exit 3, never
a success), every entity compared identical, and at least `--entities`
(`--generated`) entities compared: zero, or fewer than asked, is INCOMPLETE
(exit 2). Any difference is DIFFERENT (exit 1): the first ones are named,
and `--diff-out FILE` writes each differing entity's JSON with both outcomes
as a JSON line, ready to become a test. SIGINT/SIGTERM prints what was
compared so far as INCOMPLETE, exit 130.

WHICH ENTITIES. The pack's row groups of every dump (the collection's
pinned snapshot, or `--dump`), shuffled with `--seed`, pruned entities
skipped, as the plugin skips them. All the cores the parse pool allows
(`parallel.each_part`: PARSE_PROCESSES, or `--jobs`), each child at nice
10. It reads the pack and writes nothing but `--diff-out`. Beside the
verdict it prints both implementations' µs an entity on these entities
(measured in the children, the order alternating, under whatever else the
node runs — a speed figure, not a benchmark).
"""
from __future__ import annotations

import argparse
import json
import multiprocessing
import os
import random
import signal
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

#: row groups a pool task reads (64 entities each in a regrouped pack)
GROUPS_PER_TASK = 16
#: generated entities a pool task makes
GENERATED_PER_TASK = 2_000
#: differing entities named on the console (all of them go to --diff-out)
SHOWN = 20
#: the share of generated entities given values the dump never carries (a
#: label that is a number, a statement that is a string, …), and in those
#: the share of fields: the source renders or raises on them too, and the
#: compiled module must do the same — the rest render whole
MALFORMED_ENTITIES = 0.15
MALFORMED = 0.03

_niced = False


# ---- one entity, both implementations ---------------------------------------------------------

def outcome(fn: Callable[..., str], *args: Any, **kw: Any) -> tuple:
    try:
        return ("text", fn(*args, **kw))
    except Exception as e:  # noqa: BLE001 — the same failure is the same answer
        return ("raised", type(e).__name__, str(e))


def compare(obj: Any, langs: tuple[str, ...], labels: Callable, codes: dict | None, fallback: str | None,
            live: Any, ref: Any, flip: bool = False) -> tuple[list[dict], float, float, int]:
    """Every call `source.parse` makes for `obj`, through `live` and `ref`:
    (the differences, the seconds `live` took, the seconds `ref` took, the
    calls that raised in both)."""
    diffs: list[dict] = []
    t_live = t_ref = 0.0
    both_raised = 0
    for lang in langs:
        for name in ("entity_text", "history_text"):
            order = (ref, live) if flip else (live, ref)
            got = {}
            for mod in order:
                t = time.perf_counter()
                got[mod is live] = outcome(getattr(mod, name), obj, lang, labels, fallback=fallback, codes=codes)
                dt = time.perf_counter() - t
                if mod is live:
                    t_live += dt
                else:
                    t_ref += dt
            if got[True] != got[False]:
                diffs.append({"lang": lang, "function": name, "compiled": got[True], "source": got[False]})
            elif got[True][0] == "raised":
                both_raised += 1
    return diffs, t_live, t_ref, both_raised


def _nice() -> None:
    global _niced
    if not _niced and multiprocessing.parent_process() is not None:   # a pool child
        _niced = True
        try:
            os.nice(10)
        except (AttributeError, OSError):
            pass


def _implementations() -> tuple[Any, Any, dict[str, bool]]:
    from app.plugins.wikidata import pure, textify
    return textify, pure.textify(), pure.compiled()


def _empty() -> dict:
    return {"read": 0, "compared": 0, "calls": 0, "both_raised": 0, "live_s": 0.0, "ref_s": 0.0,
            "compiled": None, "differ": []}


def _account(out: dict, eid: Any, obj: Any, diffs: list[dict], keep: bool, line: Any = None) -> None:
    if diffs:
        out["differ"].append({"entity": eid, "differences": diffs,
                              **({"json": line if line is not None else json.dumps(obj, ensure_ascii=False)}
                                 if keep else {})})


# ---- the node's entities ----------------------------------------------------------------------

def check_groups(dump: str, path: str, groups: list[int], langs: tuple[str, ...], fallback: str | None,
                 keep_json: bool) -> dict:
    """One pool task: every kept entity of these row groups, compiled
    against source. Module level, its arguments and result pickle."""
    _nice()
    import pyarrow.parquet as pq

    from app.plugins.wikidata import pack
    live, ref, compiled = _implementations()
    labels = pack.labels(Path(dump))
    codes = pack.language_codes(Path(dump))
    out = _empty()
    out["compiled"] = compiled
    pf = pq.ParquetFile(path)
    for k, g in enumerate(groups):
        t = pf.read_row_group(g, columns=["id", "pruned", "line"])
        for eid, pruned, line in zip(t.column("id").to_pylist(), t.column("pruned").to_pylist(),
                                     t.column("line").to_pylist()):
            out["read"] += 1
            if pruned:
                continue
            obj = json.loads(line)
            diffs, tl, tr, br = compare(obj, langs, labels, codes, fallback, live, ref,
                                        flip=bool((out["compared"] + k) % 2))
            out["compared"] += 1
            out["calls"] += 2 * len(langs)
            out["both_raised"] += br
            out["live_s"] += tl
            out["ref_s"] += tr
            _account(out, eid, obj, diffs, keep_json, line.decode("utf-8", "replace") if keep_json else None)
    return out


def groups_of(dump: str) -> list[tuple[str, int]]:
    """Every row group of a dump's pack, as (chunk file, group)."""
    import pyarrow.parquet as pq

    from app.plugins.wikidata import pack
    return [(str(f), g) for f in pack.chunk_files(Path(dump))
            for g in range(pq.ParquetFile(f).metadata.num_row_groups)]


# ---- generated entities -----------------------------------------------------------------------

#: languages a generated entity is rendered in: with connectives and months
#: (en it fr de es), with connectives only (ar zh), with neither (ja pt)
GEN_LANGS = ("en", "it", "fr", "de", "es", "ar", "zh", "ja", "pt")
LABEL_LANGS = (*GEN_LANGS, "mul", "ru", "nl", "ko", "he", "el", "zh-hans", "sr-el", "haw")
#: text with what tends to break a port: scripts, combining marks, case
#: folding (ſ, İ), zero-width, modifier letters, astral planes, empty, blanks
WORDS = ("Douglas Adams", "Москва", "東京", "القاهرة", "Ελλάδα", "naïve café", "Zürich", "ſtraße", "İstanbul",
         "😀 emoji 𝔘𝔫𝔦", "e\u0301", "a\u200bb", "עברית", "हिन्दी", "Hawaiʻi", " lead", "trail ", "", " ",
         "\t\n", "Ǆemal", "Les Roses de la vie", "naïve\r\nline", "50%", "{n} {y}", "'quote\"", "\\back",
         "x" * 300, "Ⅻ", "١٢٣", "½", "²", "ß", "ﬁ", "Ꭰ", "\u0000nul")
SPECIAL_IDS = ("Q", "Qabc", "Q١٢", "Q²", "Q0", "Q-1", "Q 1", "Q1\n", "P", "P٣", "L7", "L7-F2", "L7-S1",
               "M5", "E10", "", "q42", "Q42")
UNITS = ("1", "", "http://www.wikidata.org/entity/Q11573", "http://www.wikidata.org/entity/Q4917",
         "http://www.wikidata.org/entity/Q99999999", "Q11573", "http://x/y/", "https://www.wikidata.org/wiki/Q25343")
CALENDARS = ("http://www.wikidata.org/entity/Q1985727", "http://www.wikidata.org/entity/Q1985786",
             "Q1985786", "http://www.wikidata.org/entity/Q12138", "", None)
GLOBES = ("http://www.wikidata.org/entity/Q2", "http://www.wikidata.org/entity/Q405",
          "http://www.wikidata.org/entity/Q111", "Q2", "", None, "http://www.wikidata.org/entity/Q3123")
#: what each datatype's value is made of (datavalue `type`)
DATATYPES = {"wikibase-item": "wikibase-entityid", "wikibase-property": "wikibase-entityid",
             "wikibase-lexeme": "wikibase-entityid", "wikibase-form": "wikibase-entityid",
             "wikibase-sense": "wikibase-entityid", "string": "string", "external-id": "string",
             "url": "string", "commonsMedia": "string", "math": "string", "musical-notation": "string",
             "tabular-data": "string", "geo-shape": "string", "entity-schema": "wikibase-entityid",
             "monolingualtext": "monolingualtext", "quantity": "quantity", "time": "time",
             "globe-coordinate": "globecoordinate"}
#: the properties whose values a generated entity draws on
PROPS = tuple(f"P{n}" for n in range(1, 120)) + ("P31", "P279", "P580", "P582", "P585", "P1705", "P1559",
                                                  "P1448", "P1476", "P2561", "P407", "P364", "P103", "P424")
CODES = {"Q652": "it", "Q1860": "en", "Q150": "fr", "Q188": "de", "Q1321": "es", "Q7737": "ru",
         "Q7850": "zh", "Q5287": "ja", "Q13955": "ar", "Q9288": "he", "Q36510": "haw"}
_JUNK: tuple = (5, 0, -1, 1.5, True, False, None, [], ["a"], {}, {"a": 1}, "", "x", 10 ** 30, float("nan"))


def label_table(seed: int) -> dict[tuple[str, str], str]:
    """A label table for the generated entities' references: most ids
    labelled in English, some only in `mul` or another language, some only
    by a name (`*`, names.py), some not at all — as a dump's table is."""
    rng = random.Random(f"labels:{seed}")
    table: dict[tuple[str, str], str] = {}
    ids = [*PROPS, *(f"Q{n}" for n in range(1, 400)), *CODES, *("Q11573", "Q4917", "Q99999999", "Q25343",
                                                                "Q405", "Q111", "Q3123", "Q12138", "Q5")]
    for eid in ids:
        r = rng.random()
        if r < 0.06:
            continue                                        # no label at all: the id is written
        if r < 0.12:
            table[(eid, "*")] = rng.choice(WORDS) or "name"  # a name only (names.py)
            continue
        for lang in rng.sample(LABEL_LANGS, rng.randint(1, 5)):
            word = rng.choice(WORDS)
            if word:                                         # the table holds non-empty labels
                table[(eid, lang)] = f"{word} {eid}" if rng.random() < 0.5 else word
        if r > 0.25:
            table[(eid, "en")] = f"label of {eid}"
    return table


def _bad(rng: random.Random, share: float = 1.0) -> bool:
    """Whether this field of a malformed entity is given a value the dump never carries."""
    return getattr(rng, "malformed", False) and rng.random() < MALFORMED * share


def _maybe(rng: random.Random, value: Any) -> Any:
    return rng.choice(_JUNK) if _bad(rng) else value


def _text(rng: random.Random) -> str:
    return " ".join(rng.choice(WORDS) for _ in range(rng.randint(1, 3)))


def _year(rng: random.Random) -> str:
    sign = rng.choice("+++-")
    y = rng.choice((0, 1, 9, 10, 99, 100, 101, 999, 1000, 1001, 1582, 1900, 1952, 2000, 2026, 9999, 10000, 12345,
                    13_798_000_000, 4_540_000_000, 10 ** 22, rng.randint(1, 3000), rng.randint(1, 10 ** 9)))
    width = rng.choice((4, 4, 4, 11, 1, 16))
    digits = str(y).rjust(width, "0")
    if rng.random() < 0.01:
        digits = "".join("٠١٢٣٤٥٦٧٨٩"[int(c)] for c in digits)   # Unicode digits: `\d` matches them
    return sign + digits


def _time(rng: random.Random) -> dict:
    month = rng.choice(("00", "01", "02", "06", "09", "10", "12", f"{rng.randint(1, 12):02d}"))
    day = rng.choice(("00", "01", "15", "28", "29", "31", f"{rng.randint(1, 28):02d}"))
    t = f"{_year(rng)}-{month}-{day}T00:00:00Z"
    if _bad(rng):
        t = f"{_year(rng)}-{rng.choice(('13', '99'))}-{rng.choice(('32', '01'))}T00:00:00Z"   # IndexError, both
    if _bad(rng):
        t = rng.choice(("1952-04-19", "+1952-04-19", "+1952-04-19T25:61:61Z", "+1952-4-19T00:00:00Z",
                        "+1952-04-19T00:00:00", "", "now", "+1952-04-19T00:00:00Z ", "-0000-00-00T00:00:00Z"))
    v = {"time": _maybe(rng, t), "timezone": rng.choice((0, 0, 60, -300)), "before": 0, "after": 0,
         "precision": rng.choice(tuple(range(15)) + (11, 11, 11, 9, 9, 10)),
         "calendarmodel": rng.choice(CALENDARS)}
    if _bad(rng):
        v["precision"] = rng.choice((-1, 15, "11", True, False, None, 11.0, 2 ** 70))
    if rng.random() < 0.02:
        del v[rng.choice(("precision", "calendarmodel"))]
    return v


def _decimal(rng: random.Random) -> str:
    whole = rng.choice(("0", "1", "12", "1000", str(rng.randint(0, 10 ** 6)), "123456789012345678901234567890"))
    frac = rng.choice(("", "", ".5", ".25", ".000001", "." + str(rng.randint(0, 10 ** 6)).rjust(6, "0")))
    return rng.choice("+-") + whole + frac


def _quantity(rng: random.Random) -> dict:
    v: dict[str, Any] = {"amount": _maybe(rng, _decimal(rng)), "unit": rng.choice(UNITS)}
    r = rng.random()
    if r < 0.4:
        v["upperBound"], v["lowerBound"] = _decimal(rng), _decimal(rng)
    elif r < 0.55:
        v["upperBound"] = v["lowerBound"] = v["amount"] if isinstance(v["amount"], str) else "+1"
    elif r < 0.6:
        v[rng.choice(("upperBound", "lowerBound"))] = _decimal(rng)
    elif r < 0.63:
        v["upperBound"], v["lowerBound"] = rng.choice(("NaN", "Infinity", "-Infinity", "abc", "+1e5", "1E-3")), "+0"
    if _bad(rng):
        v["unit"] = rng.choice(_JUNK)
    return v


def _coordinate(rng: random.Random) -> dict:
    def deg(limit: int) -> Any:
        return rng.choice((round(rng.uniform(-limit, limit), rng.randint(0, 12)), rng.randint(-limit, limit),
                           59.99999999, -0.0, 0, limit, -limit, 45.772677777778, 0.5 / 3600, 1e-12))
    v: dict[str, Any] = {"latitude": deg(90), "longitude": deg(180), "altitude": None,
                         "precision": rng.choice((1, 0.1, 0.01, 0.001, 0.0001, 1 / 3600, 0.000277778, 1e-05,
                                                  1e-09, 10, None, 0, -1, 2.7777777777778e-05)),
                         "globe": rng.choice(GLOBES)}
    if _bad(rng):
        v[rng.choice(("latitude", "longitude", "precision", "globe"))] = rng.choice(
            (*_JUNK, float("inf"), -float("inf"), 10 ** 20 + 1, 2 ** 53 + 1, 1e308, 5e-324))
    if _bad(rng):
        del v[rng.choice(("latitude", "longitude"))]
    if rng.random() < 0.02:
        del v[rng.choice(("precision", "globe"))]
    return v


def _entity_id_value(rng: random.Random, datatype: str) -> dict:
    kind = {"wikibase-property": "property", "wikibase-lexeme": "lexeme", "wikibase-form": "form",
            "wikibase-sense": "sense"}.get(datatype, "item")
    n = rng.choice((rng.randint(1, 420), 5, 42, 11573, rng.randint(1, 10 ** 8)))
    letter = {"property": "P", "lexeme": "L", "form": "L", "sense": "L"}.get(kind, "Q")
    eid = f"{letter}{n}" + {"form": "-F2", "sense": "-S1"}.get(kind, "")
    v: dict[str, Any] = {"entity-type": kind, "numeric-id": n, "id": eid}
    r = rng.random()
    if r < 0.1:
        del v["id"]                                  # an older dump: the numeric id alone
    elif r < 0.13:
        del v["numeric-id"]
    elif r < 0.17:
        v["id"] = rng.choice(SPECIAL_IDS)
    if _bad(rng):
        v[rng.choice(("entity-type", "numeric-id", "id"))] = rng.choice(_JUNK)
    if rng.random() < 0.02:
        v.pop("entity-type", None)
    return v


def _value(rng: random.Random, datatype: str) -> Any:
    if DATATYPES.get(datatype) == "wikibase-entityid":
        return _entity_id_value(rng, datatype)
    if datatype == "monolingualtext":
        v = {"text": _maybe(rng, _text(rng)), "language": _maybe(rng, rng.choice(LABEL_LANGS))}
        if rng.random() < 0.03:
            del v[rng.choice(("text", "language"))]
        return v
    if datatype == "quantity":
        return _quantity(rng)
    if datatype == "time":
        return _time(rng)
    if datatype == "globe-coordinate":
        return _coordinate(rng)
    return _maybe(rng, _text(rng) if rng.random() < 0.9 else rng.choice(("", "x", "https://example.org/ü")))


def _snak(rng: random.Random, pid: str, datatype: str) -> Any:
    if _bad(rng, 1 / 3):
        return rng.choice(_JUNK)
    r = rng.random()
    snaktype = "value" if r < 0.89 else "somevalue" if r < 0.95 else "novalue" if r < 0.995 else \
        rng.choice(("bogus", ""))
    if _bad(rng):
        snaktype = rng.choice((None, 5, []))
    snak: dict[str, Any] = {"snaktype": snaktype, "property": pid, "hash": f"{rng.getrandbits(40):x}"}
    dt = datatype
    if rng.random() < 0.03:
        dt = rng.choice((*DATATYPES, "", "unknown-type", None))   # a datatype that is not the value's
    if dt is not None or rng.random() < 0.5:
        snak["datatype"] = _maybe(rng, dt)
    if snaktype == "value":
        vtype = DATATYPES.get(datatype, "string")
        if rng.random() < 0.02:
            vtype = rng.choice((*set(DATATYPES.values()), "unknown"))
        dv: Any = {"value": _value(rng, datatype), "type": vtype}
        if _bad(rng):
            dv = rng.choice(_JUNK + ({"type": vtype},))
        if rng.random() > 0.01:
            snak["datavalue"] = dv
    if rng.random() < 0.01:
        snak.pop("snaktype")
    return snak


def _statement(rng: random.Random, pid: str, datatype: str, rank: str | None = None) -> Any:
    if _bad(rng, 1 / 3):
        return rng.choice(_JUNK)
    st: dict[str, Any] = {"mainsnak": _snak(rng, pid, datatype), "type": "statement",
                          "id": f"Q1${rng.getrandbits(64):x}"}
    if rank is None:
        r = rng.random()
        rank = "normal" if r < 0.7 else "preferred" if r < 0.82 else "deprecated" if r < 0.97 else \
            rng.choice((None, "PREFERRED", ""))
        if _bad(rng):
            rank = rng.choice((5, [], True))
    if rank is not None or rng.random() < 0.5:
        st["rank"] = rank
    if rng.random() < 0.45:
        quals: dict[str, Any] = {}
        for _ in range(rng.randint(1, 4)):
            qpid = rng.choice(("P580", "P582", "P585", "P580", *rng.sample(PROPS, 2)))
            qdt = "time" if qpid in ("P580", "P582", "P585") and rng.random() < 0.9 else \
                rng.choice(tuple(DATATYPES))
            quals[qpid] = [_snak(rng, qpid, qdt) for _ in range(rng.choice((1, 1, 1, 2, 3)))]
            if _bad(rng, 1 / 2):
                quals[qpid] = rng.choice(_JUNK)
        st["qualifiers"] = _maybe(rng, quals)
        st["qualifiers-order"] = list(quals)
    if rng.random() < 0.5:
        st["references"] = [{"hash": f"{rng.getrandbits(40):x}",
                             "snaks": {"P248": [_snak(rng, "P248", "wikibase-item")],
                                       "P813": [_snak(rng, "P813", "time")]},
                             "snaks-order": ["P248", "P813"]}]
    if rng.random() < 0.01:
        st = st["mainsnak"] if isinstance(st["mainsnak"], dict) else st   # a bare snak: `st.get("mainsnak", st)`
    return st


def _labels_block(rng: random.Random, langs: tuple[str, ...]) -> dict:
    out: dict[str, Any] = {}
    for lang in rng.sample(langs, rng.randint(0, min(6, len(langs)))):
        out[lang] = {"language": lang, "value": _maybe(rng, _text(rng))}
        if rng.random() < 0.01:
            out[lang] = rng.choice((_text(rng), {"language": lang}))            # a bare string, no value
        if _bad(rng):
            out[lang] = rng.choice((None, {"value": None}, 5, ["x"]))
    return out


def entity(rng: random.Random) -> dict:
    """One generated entity: a dump line's shape with every value type the
    textifier reads, history material (a property's preferred and normal
    statements, dated by qualifiers), the names.py path (no label in the
    language), and, at `MALFORMED`, values a dump never carries."""
    rng.malformed = rng.random() < MALFORMED_ENTITIES          # type: ignore[attr-defined]
    is_prop = rng.random() < 0.08
    eid = f"{'P' if is_prop else 'Q'}{rng.randint(1, 10 ** 8)}"
    obj: dict[str, Any] = {"type": "property" if is_prop else "item", "id": _maybe(rng, eid)}
    if is_prop:
        obj["datatype"] = rng.choice(tuple(DATATYPES))
    lang_pool = LABEL_LANGS if rng.random() < 0.7 else tuple(x for x in LABEL_LANGS if x not in ("en", "mul"))
    obj["labels"] = _labels_block(rng, lang_pool)
    obj["descriptions"] = _labels_block(rng, LABEL_LANGS)
    if rng.random() < 0.02:
        del obj[rng.choice(("labels", "descriptions"))]            # a missing block: `_lang_val` of None
    aliases: dict[str, Any] = {}
    for lang in rng.sample(LABEL_LANGS, rng.randint(0, 3)):
        words = [_text(rng) for _ in range(rng.randint(1, 4))]
        if rng.random() < 0.3:
            words.append(words[0])                                  # a duplicate: kept once
        aliases[lang] = [{"language": lang, "value": _maybe(rng, w)} for w in words]
        if rng.random() < 0.02:
            aliases[lang].append("bare string")
        if _bad(rng):
            aliases[lang].append(rng.choice(_JUNK))
    obj["aliases"] = aliases
    claims: dict[str, Any] = {}
    for _ in range(rng.choice((0, 1, 2, 4, 6, 8, 12, 20))):
        pid = rng.choice(PROPS)
        dt = rng.choice(tuple(DATATYPES))
        n = rng.choice((1, 1, 1, 2, 3, 5, 12))
        if rng.random() < 0.25:                                   # history: preferred + dated normals
            sts = [_statement(rng, pid, dt, "preferred")] + [_statement(rng, pid, dt, "normal") for _ in range(n)]
            rng.shuffle(sts)
        else:
            sts = [_statement(rng, pid, dt) for _ in range(n)]
        claims[pid] = _maybe(rng, sts)
    if rng.random() < 0.6:
        claims["P31"] = [_statement(rng, "P31", "wikibase-item") for _ in range(rng.choice((1, 1, 2)))]
    if rng.random() < 0.2:                                        # names.py: what the entity calls itself
        npid = rng.choice(("P1705", "P1559", "P1448", "P1476", "P2561"))
        claims[npid] = [_statement(rng, npid, "monolingualtext")]
    if rng.random() < 0.2:
        lpid = rng.choice(("P407", "P364", "P103"))
        claims[lpid] = [{"mainsnak": {"snaktype": "value", "property": lpid, "datatype": "wikibase-item",
                                      "datavalue": {"type": "wikibase-entityid",
                                                    "value": {"entity-type": "item", "id": rng.choice(tuple(CODES))}}},
                         "type": "statement", "rank": rng.choice(("normal", "preferred", "deprecated"))}]
    if rng.random() < 0.02:
        claims[rng.choice(("Q5", "p31", "", "L1"))] = [_statement(rng, "P1", "string")]
    obj["claims"] = rng.choice(_JUNK) if _bad(rng, 1 / 3) else claims
    obj["sitelinks"] = {site: {"site": site, "title": _text(rng), "badges": []}
                        for site in rng.sample(("enwiki", "frwiki", "itwiki", "zh_min_nanwiki", "dewikivoyage",
                                                "commonswiki", "wikidatawiki", "ruwikiquote", "hawwiki",
                                                "wikimania"),
                                               rng.randint(0, 4))}
    obj["lastrevid"], obj["modified"] = rng.randint(1, 2 * 10 ** 9), "2026-10-12T00:00:00Z"
    return obj


def generated(seed: int, start: int, count: int) -> list[tuple[str, dict, tuple[str, ...], str | None]]:
    """Entities `start .. start + count` of `seed`, each with the languages
    it is rendered in and the fallback: (name, entity, languages, fallback)."""
    out = []
    for i in range(start, start + count):
        rng = random.Random(f"entity:{seed}:{i}")
        obj = entity(rng)
        langs = ("en", *rng.sample(GEN_LANGS[1:], rng.choice((0, 1, 2))))
        fallback = rng.choice(("en", "en", "en", None, "fr", "mul"))
        out.append((f"gen:{seed}:{i}", obj, langs, fallback))
    return out


_tables: dict[int, Callable] = {}


def labels_for(seed: int) -> Callable[[str, str], str | None]:
    table = _tables.get(seed)
    if table is None:
        t = label_table(seed)
        table = _tables[seed] = lambda eid, lang: t.get((eid, lang))
    return table


def check_generated(seed: int, start: int, count: int, keep_json: bool) -> dict:
    """One pool task: generated entities `start .. start + count`."""
    _nice()
    live, ref, compiled = _implementations()
    labels = labels_for(seed)
    out = _empty()
    out["compiled"] = compiled
    for k, (name, obj, langs, fallback) in enumerate(generated(seed, start, count)):
        codes = CODES if k % 5 else None
        out["read"] += 1
        diffs, tl, tr, br = compare(obj, langs, labels, codes, fallback, live, ref, flip=bool(k % 2))
        out["compared"] += 1
        out["calls"] += 2 * len(langs)
        out["both_raised"] += br
        out["live_s"] += tl
        out["ref_s"] += tr
        _account(out, name, obj, diffs, keep_json)
    return out


# ---- the pass ---------------------------------------------------------------------------------

def dumps_for(a) -> tuple[list[Path], tuple[str, ...], str | None]:
    from app.plugins.wikidata import source as wsrc
    if a.dump:
        plugin = wsrc.WikidataSource()
        return [Path(d) for d in a.dump], tuple(a.lang or plugin.languages), plugin.fallback_language
    from app import collections as collections_mod
    c = collections_mod.by_name(a.collection)
    if c is None:
        raise SystemExit(f"no collection named {a.collection!r} on this node (--dump names files)")
    plugin = c.plugin_instance()
    return (wsrc.find_dumps(Path(c.corpus_dir), getattr(c, "snapshot", None))[:1],
            tuple(a.lang or plugin.languages), plugin.fallback_language)


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="textify_identity", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--collection", default="wikidata", help="whose dump (its pinned snapshot)")
    p.add_argument("--dump", action="append", help="a wikidata JSON dump whose pack is built, instead")
    p.add_argument("--lang", action="append", help="a language to render (default: the collection's)")
    p.add_argument("--entities", type=int, default=100_000, help="entities to compare (default 100000)")
    p.add_argument("--generated", type=int, default=None, metavar="N",
                   help="compare N seeded generated entities instead of a dump's")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--jobs", type=int, default=None, help="processes (default PARSE_PROCESSES)")
    p.add_argument("--diff-out", type=Path, default=None, help="differing entities, JSON lines")
    a = p.parse_args(argv)
    from app import config, parallel
    if a.jobs:
        config.PARSE_PROCESSES = a.jobs
    keep = a.diff_out is not None
    if a.generated is not None:
        asked = a.generated
        tasks: list[tuple] = [(a.seed, s, min(GENERATED_PER_TASK, asked - s), keep)
                              for s in range(0, asked, GENERATED_PER_TASK)]
        fn: Callable = check_generated
        where: dict = {"generated": asked, "seed": a.seed, "malformed_entities": MALFORMED_ENTITIES}
    else:
        asked = a.entities
        dumps, langs, fallback = dumps_for(a)
        if not dumps:
            print("INCOMPLETE: no dump found — nothing compared, nothing proven", file=sys.stderr)
            return 2
        print(f"{len(dumps)} dump(s); listing their packs' row groups…", file=sys.stderr, flush=True)
        groups = [(str(d), f, g) for d in dumps for f, g in groups_of(str(d))]
        random.Random(a.seed).shuffle(groups)
        tasks = []
        for i in range(0, len(groups), GROUPS_PER_TASK):
            batch = groups[i:i + GROUPS_PER_TASK]
            by_file: dict[tuple[str, str], list[int]] = {}
            for d, f, g in batch:
                by_file.setdefault((d, f), []).append(g)
            tasks.extend((d, f, gs, langs, fallback, keep) for (d, f), gs in by_file.items())
        fn = check_groups
        where = {"dumps": [str(d) for d in dumps], "languages": list(langs), "fallback": fallback,
                 "seed": a.seed}
    total = {"read": 0, "compared": 0, "calls": 0, "both_raised": 0, "live_s": 0.0, "ref_s": 0.0}
    compiled: dict[str, bool] = {}
    differ: list[dict] = []
    stopped = False

    def stop(signum, frame):
        raise KeyboardInterrupt

    before = signal.signal(signal.SIGTERM, stop)
    t0 = last = time.monotonic()
    try:
        for got in parallel.each_part(fn, tasks):
            for k in total:
                total[k] += got[k]
            for name, is_c in (got["compiled"] or {}).items():
                compiled[name] = compiled.get(name, True) and is_c
            differ.extend(got["differ"])
            if time.monotonic() - last > 30:
                last = time.monotonic()
                print(f"{total['compared']:,} compared, {len(differ)} differ, "
                      f"{time.monotonic() - t0:.0f} s", file=sys.stderr, flush=True)
            if total["compared"] >= asked:
                break
    except KeyboardInterrupt:
        stopped = True
    finally:
        signal.signal(signal.SIGTERM, before)
    n = total["compared"]
    if differ:
        verdict, code = "DIFFERENT", 1
    elif stopped or n < max(1, asked):
        verdict, code = "INCOMPLETE", 130 if stopped else 2
    elif not compiled or not all(compiled.values()):
        verdict, code = "NOT COMPILED", 3
    else:
        verdict, code = "IDENTICAL", 0
    if keep and differ:
        with a.diff_out.open("w", encoding="utf-8") as f:
            for d in differ:
                f.write(json.dumps(d, ensure_ascii=False) + "\n")
    summary = {
        "verdict": verdict, "asked": asked, "compiled": compiled,
        **{k: v for k, v in total.items() if not k.endswith("_s")}, "differ": len(differ),
        "first_differing": [{k: v for k, v in d.items() if k != "json"} for d in differ[:SHOWN]],
        "compiled_us_per_entity": round(total["live_s"] / n * 1e6, 1) if n else None,
        "source_us_per_entity": round(total["ref_s"] / n * 1e6, 1) if n else None,
        "speedup": round(total["ref_s"] / total["live_s"], 2) if total["live_s"] else None,
        **where, "wall_s": round(time.monotonic() - t0, 1)}
    print(json.dumps(summary, ensure_ascii=False, indent=1, default=str))
    why = {"IDENTICAL": f"all {n:,} entities compared give the same bytes",
           "DIFFERENT": f"{len(differ):,} of {n:,} entities differ — do not deploy",
           "NOT COMPILED": "the live textifier is the .py: the source compared with itself proves nothing",
           "INCOMPLETE": f"{n:,} of {asked:,} entities compared — not a proof"}[verdict]
    print(f"{verdict}: {why}", file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
