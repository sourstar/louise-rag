"""The wikitext rewrite gives the SAME BYTES as the code before it.

app/plugins/wikipedia/wikitext.py stopped calling mwparserfromhell's
`replace`/`remove` once per node (each a search of the whole tree: the
parse was quadratic in the page) and walking every template 13 times a page
(docs/notes/python-hot-spots.md, "A"). Its output is the prose the chunker
cuts and the vectors are made of: one changed byte is a new cleaning
version and 52.9 M pages re-embedded (invariant 3). So the code before is
kept VERBATIM (tools/measure/wikitext_reference.py) and compared here:

* on every string literal of the test suite — each fixture page, and
  every other string, which costs nothing and finds what a fixture misses;
* on thousands of SEEDED adversarial pages: templates nested in templates,
  links, captions, refs, tables and headings; the same fragment many times
  over (`replace` finds a node by identity, so equal-text duplicates are the
  classic trap); unclosed and stray markup; non-ASCII;
* pass by pass on the TREE, not only on the output: its shape (each
  node's depth and type, in walk order) and its text after every step
  `_prose_code` takes;

as `json.dumps(sort_keys=True)` of each result, or the same exception.
The node's held pages: tools/measure/wikitext_identity.py (≥ 100,000
before any deploy).
"""
from __future__ import annotations

import ast
import bz2
import hashlib
import json
import random
from pathlib import Path

import mwparserfromhell
import pytest
from mwparserfromhell.nodes import Text, Wikilink
from mwparserfromhell.wikicode import Wikicode

from app.plugins.wikipedia import wikitext as new
from tools.measure import wikitext_reference as old

REPO = Path(__file__).resolve().parents[1]
#: the copied file's sha256, as its header states it
REFERENCE_SHA256 = "7c9b0f62277dcde93712c062b62e319be256980e16ba98dac826306d9a00d682"
GENERATED = 3000


def outcome(fn, *args) -> str:
    """A call's result as bytes to compare — or the exception it raised."""
    try:
        return json.dumps(fn(*args), sort_keys=True, ensure_ascii=False)
    except Exception as e:  # noqa: BLE001 — the same failure is the same answer
        return f"raised {type(e).__name__}: {e}"


def shape(code) -> tuple[str, list[tuple]]:
    """The tree, as `strip_code` reads it: its text, and node by node in
    walk order each node's depth and type, and a Text node's value — a
    pre-order with depths fixes the tree's shape."""
    out: list[tuple] = []

    def visit(c, depth: int) -> None:
        for n in c.nodes:
            out.append((depth, type(n).__name__, n.value if isinstance(n, Text) else None))
            for sub in n.__children__():
                visit(sub, depth + 1)

    visit(code, 0)
    return str(code), out


def passes(mod, text: str) -> list:
    """`_prose_code`'s steps one at a time, the tree after each, and what
    each returned; then `apparatus`'s link pass on its own parse."""
    code = mwparserfromhell.parse(mod._tags(text))
    seen = []
    for step in (mod._drop_refs, mod._take_images, mod._take_links, mod._render_math,
                 mod._render_inline):
        got = step(code)
        seen.append((step.__name__, got, shape(code)))
    code = mwparserfromhell.parse(mod._PLACEHOLDER.sub("", mod._tags(text)))
    seen.append(("_take_links", mod._take_links(code), shape(code)))
    return seen


def compare(text: str, *, tree: bool = True, every: bool = True) -> list[str]:
    """`parse_all` old against new; with `tree`, the tree after each pass;
    with `every`, each public function the rewrite touched on its own (all
    are inside `parse_all`, so a page needs them once, not every time): the
    names of those that differ."""
    bad = []
    if outcome(old.parse_all, text) != outcome(new.parse_all, text):
        bad.append("parse_all")
    if tree and outcome(lambda t: [repr(x) for x in passes(old, t)], text) != \
            outcome(lambda t: [repr(x) for x in passes(new, t)], text):
        bad.append("the tree after a pass")
    if not every:
        return bad
    for name in ("to_sections", "apparatus", "redirect_info", "is_redirect"):
        if outcome(getattr(old, name), text) != outcome(getattr(new, name), text):
            bad.append(name)
    code = mwparserfromhell.parse(text)           # only read by these
    for name in ("categories", "category_sortkeys", "infoboxes", "references",
                 "external_links"):
        if outcome(getattr(old, name), code) != outcome(getattr(new, name), code):
            bad.append(name)
    for tpl in ("short description", "Coord", " main ", "convert", "Infobox person",
                "authority control"):
        if outcome(old.named_template, code, tpl) != outcome(new.named_template, code, tpl):
            bad.append(f"named_template({tpl!r})")
    return bad


# ---------------------------------------------------------------- the yardstick

def test_the_reference_is_the_old_file_byte_for_byte():
    body = (REPO / "tools/measure/wikitext_reference.py").read_text(encoding="utf-8")
    body = body.split("# " + "-" * 75 + "\n", 1)[1]
    assert hashlib.sha256(body.encode("utf-8")).hexdigest() == REFERENCE_SHA256


def test_nothing_in_app_imports_the_reference():
    hits = []
    for p in (REPO / "app").rglob("*.py"):
        for node in ast.walk(ast.parse(p.read_text(encoding="utf-8-sig"))):
            names = [a.name for a in node.names] if isinstance(node, (ast.Import, ast.ImportFrom)) else []
            if isinstance(node, ast.ImportFrom):
                names.append(node.module or "")
            if any("wikitext_reference" in n for n in names):
                hits.append(str(p))
    assert hits == []


# ---------------------------------------------------------------- fixtures

def _literals() -> list[str]:
    out: dict[str, None] = {}
    for p in sorted((REPO / "tests").rglob("*.py")):
        tree = ast.parse(p.read_text(encoding="utf-8-sig"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                out.setdefault(node.value, None)
    return list(out)


def _scaled(target: int) -> str:
    """docs/notes/python-hot-spots.md's SYNTHETIC page: the parser3 article
    repeated under numbered headings to `target` characters."""
    from tests.test_wikipedia_parser3 import ARTICLE
    body, tail = ARTICLE.split("[[Category:Towns|Northtown]]", 1)
    parts, n = [], 0
    while sum(len(p) for p in parts) < target:
        n += 1
        parts.append(body.replace("== History ==", f"== History {n} ==")
                     .replace("=== Early ===", f"=== Early {n} ===")
                     .replace("== Gallery ==", f"== Gallery {n} ==")
                     .replace("== Notes ==", f"== Notes {n} =="))
    return "".join(parts) + "[[Category:Towns|Northtown]]" + tail


def test_every_string_of_the_test_suite_parses_the_same():
    texts = _literals()
    assert len(texts) > 10_000
    wikiish = [t for t in texts if any(m in t for m in ("[[", "{{", "<", "==", "{|", "''", "__"))]
    assert len(wikiish) > 300
    differ = {}
    for t in texts:
        if outcome(old.parse_all, t) != outcome(new.parse_all, t):
            differ[t[:60]] = ["parse_all"]
    for t in wikiish:
        bad = compare(t)
        if bad:
            differ[t[:60]] = bad
    assert differ == {}


@pytest.mark.parametrize("chars", [2_000, 20_000])
def test_the_scaled_page_parses_the_same(chars):
    assert compare(_scaled(chars)) == []


# ---------------------------------------------------------------- adversarial pages

_NAMES = sorted(old._INLINE_DATA | old._QUOTES) + [
    "lang-fr", "Lang-de", "LANG", " convert ", "CONVERT", "Infobox person", "infobox settlement",
    "Automatic taxobox", "Weather box", "Short description", "short description ", "Coord",
    "coord", "Main", "See also", "Further", "About", "For", "Distinguish", "Redirect",
    "Other uses", "Authority control", "reflist", "Reflist|2", "notelist", "refbegin", "refend",
    "References", "cite web", "Cite book", "Webarchive", "Portal", "DEFAULTSORT:Town, North",
    "DEFAULTSORT: Ünïcode", "DEFAULTSORTKEY:x", "foo", "Foo bar", "subst:x", "#if:a", "!"]
_WORDS = ["Northtown", "river", "is", "a", "the", "1819", "5", "km", "é", "Straße", "日本語",
          "Ελλάδα", "مرحبا", "🙂", "ﬁ", "x^2", "|", "||", "=", ":", "!", "!!", "&nbsp;", "&amp;",
          "&#91;&#91;", "&lt;ref&gt;", "&#x3a3;", "&bogus;", " ", "  ", "\n", "\n\n", "\n\n\n\n",
          "\t", "''", "'''", "*", "# ", ": ", "; ", "----", "__TOC__", "__NOTOC__", "__FOO__",
          "{", "}", "[", "]", "<", ">", "<br>", "<br/>", "~~~~", "{{", "}}", "[[", "]]", "{|", "|}",
          "|-", "{{{", "}}}", "<!--", "-->", "</ref>", "</math>"]
_OPTIONS = ["thumb", "thumbnail", "frameless", "180px", "x200px", "upright", "upright=1.5",
            "left", "right", "center", "none", "alt=An image", "link=", "border", "baseline",
            "class=x", "page=2"]
_HEADINGS = ["History", "Early", "References", "references", "External links", "See also",
             "Further reading", "Notes", "Sources", "Bibliography", "Footnotes", "Citations",
             "Gallery", "Geography", " Notes ", "NOTES", "Zürich"]


class _Page:
    """A seeded generator of wikitext that leans on the rewrite's edges."""

    def __init__(self, seed: int):
        self.r = random.Random(seed)
        self.made: list[str] = []          # fragments already written, to repeat

    def pick(self, xs):
        return xs[self.r.randrange(len(xs))]

    def keep(self, s: str) -> str:
        if self.r.random() < 0.4:
            self.made.append(s)
        return s

    def inline(self, d: int) -> str:
        out = []
        for _ in range(self.r.randint(0, 5 if d < 3 else 2)):
            k = self.r.random()
            if self.made and k < 0.10:
                out.append(self.pick(self.made))             # an equal-text duplicate
            elif d >= 4 or k < 0.45:
                out.append(self.pick(_WORDS))
            elif k < 0.62:
                out.append(self.template(d + 1))
            elif k < 0.76:
                out.append(self.link(d + 1))
            elif k < 0.88:
                out.append(self.tag(d + 1))
            elif k < 0.92:
                out.append(self.pick(["[http://x.org title]", "[https://例え.jp/é ''it'']",
                                      "http://bare.example/a_(b)", "[//proto.example]",
                                      "[http://x.org " + self.inline(d + 1) + "]"]))
            elif k < 0.95:
                out.append(self.pick(["<!-- c -->", "<!-- {{convert|1|m}} [[Category:Hidden]] -->",
                                      "<!-- unclosed", "{{{1|default}}}", "{{{name}}}"]))
            else:
                q = self.pick(["''", "'''", "'''''"])
                out.append(q + self.inline(d + 1) + q)
        return "".join(out)

    def template(self, d: int) -> str:
        name = self.pick(_NAMES)
        params = []
        for i in range(self.r.randint(0, 4)):
            k = self.r.random()
            if k < 0.5:
                params.append(self.inline(d + 1))
            elif k < 0.85:
                key = self.pick(["text", "quote", "author", "sign", "name", "1", "2", "url",
                                 "title", "taxon", " Text ", "AUTHOR", "founded", ""])
                params.append(f"{key}={self.inline(d + 1)}")
            else:
                params.append(self.pick(["", " ", "\n", "5", "km", "fr", "C|F"]))
        sep = self.pick(["|", "|", " | ", "\n|"])
        body = name + "".join(sep + p for p in params)
        return self.keep("{{" + body + self.pick(["}}", "}}", "}}", "\n}}", "}"]))

    def link(self, d: int) -> str:
        k = self.r.random()
        if k < 0.25:
            t = "[[" + self.pick(["Earth", "Paris", "Wikipedia:Foo", ":Category:Shown", "a|b"])
            s = t + ("|" + self.inline(d + 1) if self.r.random() < 0.5 else "") + "]]"
        elif k < 0.45:
            s = "[[" + self.pick(["Category:", "category:", "CATEGORY:"]) + \
                self.pick(["Towns", "Ünïcode", "X|", "Y| ", "Z|Sort, Key", ""]) + \
                self.pick(["]]", "|" + self.inline(d + 1) + "]]"])
        elif k < 0.60:
            s = "[[" + self.pick(["de:Logdatei", "fr-ca:Bar", "zh-min-nan:X", "en:Y",
                                  "de:Z|with text", "xx:", "file:no"]) + "]]"
        else:
            ns = self.pick(["File:", "Image:", "file:", "IMAGE:"])
            opts = [self.pick(_OPTIONS) for _ in range(self.r.randint(0, 3))]
            cap = [self.inline(d + 1)] if self.r.random() < 0.8 else []
            s = "[[" + ns + self.pick(["A b.jpg", "x.png", "Ü.svg", ""]) + \
                "".join("|" + o for o in opts + cap) + self.pick(["]]", "]]", "]"])
        return self.keep(s)

    def tag(self, d: int) -> str:
        inner = self.inline(d + 1)
        s = self.pick([
            f"<ref>{inner}</ref>", '<ref name="a" />', f"<ref name=a>{inner}</ref>",
            f'<ref\nname="b">{inner}</ref>', f"<REF group=n>{inner}</REF>", f"<ref>{inner}",
            f"<math>x^2 + {inner}</math>", f"<math>{inner}", f"<chem>H2O {inner}</chem>",
            f"<ce>{inner}</ce>", f"<pre>  code {inner}  </pre>",
            f'<syntaxhighlight lang="py">\n{inner}\n</syntaxhighlight>', f"<source>{inner}</source>",
            f"<gallery>\nFile:a.jpg|cap {inner}\nb.png|{{{{lang|fr|t}}}}\n\nImage:c.gif\n|x\n</gallery>",
            "<references/>", "<references />", f'<div class="x">{inner}</div>',
            f"<small>{inner}</small>", f"<span>{inner}", f"<blockquote>{inner}</blockquote>",
            f"<nowiki>{inner}</nowiki>", f"<sup>{inner}</sup>", "<math></math>", "<ref></ref>"])
        return self.keep(s)

    def table(self, d: int) -> str:
        rows = []
        for _ in range(self.r.randint(0, 3)):
            cells = [self.pick(["", 'style="x" | ', "a=b|"]) + self.inline(d + 1)
                     for _ in range(self.r.randint(1, 3))]
            rows.append(self.pick(["|-\n", "|- class=r\n", ""]) +
                        self.pick(["| ", "! "]) + self.pick([" || ", " !! "]).join(cells))
        cap = self.pick(["", f"|+ {self.inline(d + 1)}\n"])
        return "{|" + self.pick(["", ' class="wikitable"']) + "\n" + cap + "\n".join(rows) + \
            self.pick(["\n|}", "\n|}", ""])

    def page(self) -> str:
        out = [self.inline(0)]
        long = self.r.random() < 0.015               # a long article, now and then
        for _ in range(self.r.randint(20, 50) if long else self.r.randint(0, 6)):
            k = self.r.random()
            if k < 0.35:
                eq = "=" * self.r.randint(1, 5)
                title = self.pick(_HEADINGS) if self.r.random() < 0.7 else self.inline(2)
                close = eq if self.r.random() < 0.9 else eq[:-1]
                out.append(f"\n{eq} {title} {close}\n")
            elif k < 0.45:
                out.append("\n" + self.table(1) + "\n")
            elif k < 0.55 and self.made:
                out.append(self.pick(self.made) * self.r.randint(2, 4))
            elif k < 0.62:
                out.append(self.pick(["\n* ", "\n# ", "\n: ", "\n; a : ", "\n----\n", "\n\n"]))
            out.append(self.inline(0))
        if self.r.random() < 0.5:
            out.append("\n" + "".join(self.link(3) for _ in range(self.r.randint(0, 4))))
        if self.r.random() < 0.15:
            out.insert(0, self.pick(["#REDIRECT [[Elsewhere]]", "#redirect [[A#b]]\n"]))
        return "".join(out)


def test_seeded_adversarial_pages_parse_the_same():
    differ: dict[int, list[str]] = {}
    sizes = []
    for seed in range(GENERATED):
        text = _Page(seed).page()
        sizes.append(len(text))
        # parse_all on every page; the tree on a third, the rest on a tenth
        # (the old code is the slow half: ~9 ms a page, quadratic on the long)
        bad = compare(text, tree=seed % 3 == 0, every=seed % 10 == 0)
        if bad:
            differ[seed] = bad
    assert differ == {}, f"seeds whose output differs (reproduce with _Page(seed).page()): {differ}"
    assert max(sizes) > 5_000 and sum(sizes) / len(sizes) > 300


def test_the_generator_reaches_the_edges():
    """Not a vacuous pass: the generated pages hold nested and duplicated
    replaceable nodes, which is where `replace` could have behaved apart."""
    nested = duplicated = files_in_captions = 0
    for seed in range(300):
        code = mwparserfromhell.parse(_Page(seed).page())
        tpls = code.filter_templates()
        if any(code.get_ancestors(t) and str(t.name).strip().lower() in old._INLINE_DATA
               for t in tpls):
            nested += 1
        texts = [str(t) for t in tpls if str(t.name).strip().lower() in old._INLINE_DATA]
        if len(texts) != len(set(texts)):
            duplicated += 1
        links = code.filter_wikilinks()
        if any(str(lk.title).strip().lower().startswith(("file:", "image:")) and
               any(isinstance(a, Wikilink) for a in code.get_ancestors(lk)) for lk in links):
            files_in_captions += 1
    assert nested > 30 and duplicated > 30 and files_in_captions > 5


# ---------------------------------------------------------------- the machinery

def test_no_tree_search_and_one_template_walk(monkeypatch):
    """The two costs this replaced do not come back: no per-node search of
    the tree (`_do_strong_search`, the quadratic one) and no walk of the
    page's templates per `named_template` lookup."""
    calls = {"search": 0, "filter": 0}
    real_search, real_filter = Wikicode._do_strong_search, Wikicode.filter

    def search(self, *a, **k):
        calls["search"] += 1
        return real_search(self, *a, **k)

    def filt(self, *a, **k):
        calls["filter"] += 1
        return real_filter(self, *a, **k)

    monkeypatch.setattr(Wikicode, "_do_strong_search", search)
    monkeypatch.setattr(Wikicode, "filter", filt)
    page = _scaled(20_000)
    new.parse_all(page)
    assert calls["search"] == 0
    sections = len(new.to_sections(page)) + 1
    calls["filter"] = 0
    new.parse_all(page)
    # what is left is the library's own per-section heading lookup
    # (`filter_headings`, in `_structured` and `apparatus`)
    assert calls["filter"] <= 3 * sections


def test_a_node_reachable_twice_takes_the_library_s_own_loop():
    """The parser never builds a tree that holds one node twice, but if one
    did, `_rewrite` must not splice it twice: it falls back to the loop it
    replaced, whose search decides — and gives what the old code gives."""
    def tree():
        code = mwparserfromhell.parse("a {{convert|5|km}} b [[Category:X]] c")
        tpl = code.filter_templates()[0]
        code.nodes.append(Text(" d "))
        code.nodes.append(tpl)                    # the same node, twice
        return code

    t = tree()
    assert t.filter_templates()[0] is t.filter_templates()[1]
    a, b = tree(), tree()
    old._render_inline(a)
    searched = []
    real = Wikicode.replace

    def replace(self, *args, **kw):
        searched.append(args[0])
        return real(self, *args, **kw)

    Wikicode.replace = replace
    try:
        new._render_inline(b)
    finally:
        Wikicode.replace = real
    assert searched                               # the fallback ran
    assert shape(a) == shape(b)
    a, b = tree(), tree()
    assert old._take_links(a) == new._take_links(b)
    assert str(a) == str(b)


#: the edges a reviewer asks about first, by name
EDGES = {
    "an inline template inside one": "a {{convert|{{val|5}}|km}} b",
    "a file link in a file's caption": "[[File:a.jpg|thumb|cap [[File:b.jpg|thumb|inner]] end]] t",
    "equal-text duplicates": "{{lang|fr|[[Paris]]}} x {{lang|fr|[[Paris]]}} x {{lang|fr|[[Paris]]}}",
    "a template in a ref": "a<ref>{{convert|1|m}}</ref> b",
    "a template in a category's sort key": "[[Category:X|{{convert|1|m}}]] t",
    "markup inside math, re-parsed": "<math>{{convert|1|m}} [[Category:Y]]</math> t",
    "escaped markup in a caption": "[[File:a.jpg|thumb|&lt;ref&gt;x&lt;/ref&gt; &#91;&#91;Category:Z]]",
    "templates 150 deep": "{{lang|fr|" * 150 + "x" + "}}" * 150,
    "links 150 deep": "[[a|" * 150 + "x" + "]]" * 150,
    "tags 150 deep": "<div>" * 150 + "{{convert|1|m}}" + "</div>" * 150,
    "mixed 60 deep": "{{lang|fr|[[a|<span>" * 60 + "{{nbsp}}" + "</span>]]}}" * 60,
    "a quote in a quote": "{{quote|text={{quote|text=inner|author=B}}|author=A}}",
    "a gallery in a caption": "[[File:a.jpg|thumb|<gallery>\nFile:g.jpg|gcap\n</gallery>]]",
    "the same image three times": "[[File:a.jpg|thumb|same]] " * 3,
    "interlanguage links, one in a caption": "[[File:a.jpg|thumb|c [[de:Foo]]]] [[de:Foo]] [[de:Foo]]",
    "a ref in a ref, twice": "<ref>a<ref>b</ref>c</ref> <ref>a<ref>b</ref>c</ref>",
    "templates in headings": ("== {{convert|1|m}} ==\ntext\n== References ==\n{{cite web|url=x}}\n"
                              "=== Sub {{lang|fr|t}} ===\nmore"),
}


@pytest.mark.parametrize("name", sorted(EDGES))
def test_an_edge_parses_the_same(name):
    assert compare(EDGES[name]) == []


# ---------------------------------------------------------------- one parse for two

def _whole_page_parses(monkeypatch, mod, page: str) -> int:
    """How many times `mod.parse_all(page)` parses the page itself (or the
    page without its reference lists, `apparatus`'s string)."""
    wholes = {old._tags(page), old._PLACEHOLDER.sub("", old._tags(page))}
    seen = []
    real = mwparserfromhell.parse
    monkeypatch.setattr(mod.mwparserfromhell, "parse",
                        lambda text, *a, **k: (seen.append(text in wholes), real(text, *a, **k))[1])
    try:
        mod.parse_all(page)
    finally:
        monkeypatch.setattr(mod.mwparserfromhell, "parse", real)
    return sum(seen)


def test_parse_all_parses_the_page_twice_where_it_parsed_it_three_times(monkeypatch):
    page = _scaled(5_000)
    assert _whole_page_parses(monkeypatch, old, page) == 3
    assert _whole_page_parses(monkeypatch, new, page) == 2


def _shared_tree_pages() -> list[str]:
    return [_scaled(2_000), *EDGES.values(), *(_Page(seed).page() for seed in range(400))]


def test_every_read_of_the_tree_is_done_before_the_prose_changes_it(monkeypatch):
    """The prose passes change the tree `parse_all` read the page's facts
    from. Every read happens first, on a tree whose shape is a fresh
    parse's; the passes then leave it, step for step, as they leave a fresh
    parse (the old code's own `_prose_code`)."""
    events: list[str] = []
    for name in ("_categories", "_category_sortkeys", "_infoboxes", "_references",
                 "_external_links", "_named", "_by_name"):
        real = getattr(new, name)
        monkeypatch.setattr(new, name, lambda *a, _n=name, _r=real: (events.append(_n), _r(*a))[1])
    real_prose = new._prose
    checked = []

    def prose(code):
        events.append("prose")
        checked.append(shape(code) == shape(mwparserfromhell.parse(old._tags(page))))
        got = real_prose(code)
        expect = old._prose_code(old._tags(page))
        checked.append((shape(got[0]), got[1], got[2]) == (shape(expect[0]), expect[1], expect[2]))
        return got

    monkeypatch.setattr(new, "_prose", prose)
    for page in _shared_tree_pages():
        events.clear()
        if new.is_redirect(page):
            continue
        assert outcome(new.parse_all, page) == outcome(old.parse_all, page)
        assert events.count("prose") == 1 and events[-1] == "prose", events
        assert len(events) > 1
    assert checked and all(checked)


def test_a_string_tags_would_still_change_gets_its_own_parse(monkeypatch):
    """`_tags` is idempotent, so the page `parse_all` parsed is the page
    `_structured` would parse. Should it ever not be, the prose takes its
    own parse of its own string, as the code before did."""
    def tags(text):
        return text + "\n"                   # not idempotent
    page = _scaled(2_000)
    monkeypatch.setattr(old, "_tags", tags)
    monkeypatch.setattr(new, "_tags", tags)
    parsed = []
    real = mwparserfromhell.parse
    monkeypatch.setattr(new.mwparserfromhell, "parse",
                        lambda text, *a, **k: (parsed.append(text), real(text, *a, **k))[1])
    got = outcome(new.parse_all, page)
    monkeypatch.setattr(new.mwparserfromhell, "parse", real)
    assert parsed.count(page + "\n") == 1 and parsed.count(page + "\n\n") == 1
    assert got == outcome(old.parse_all, page)
    for page in EDGES.values():
        assert outcome(new.parse_all, page) == outcome(old.parse_all, page)


# ---------------------------------------------------------------- the node's checker

def _dump(tmp_path: Path, streams: list[list[tuple[int, str, str]]]) -> Path:
    """A multistream dump as Wikimedia writes it — header, page streams,
    footer — and its index; pages as (id, namespace, text)."""
    from xml.sax.saxutils import escape
    header = bz2.compress(b'<mediawiki xml:lang="en"><siteinfo><dbname>enwiki</dbname></siteinfo>\n')
    blobs = []
    for s in streams:
        xml = "".join(f"<page><title>P{pid}</title><ns>{ns}</ns><id>{pid}</id><revision><id>1</id>"
                      f'<text xml:space="preserve">{escape(text)}</text></revision></page>\n'
                      for pid, ns, text in s)
        blobs.append(bz2.compress(xml.encode()))
    d = tmp_path / "enwiki" / "20261001"
    d.mkdir(parents=True)
    path = d / "enwiki-20261001-pages-articles-multistream.xml.bz2"
    path.write_bytes(header + b"".join(blobs) + bz2.compress(b"</mediawiki>\n"))
    lines, off = [], len(header)
    for s, blob in zip(streams, blobs):
        lines += [f"{off}:{pid}:P{pid}" for pid, _, _ in s]
        off += len(blob)
    (d / "enwiki-20261001-pages-articles-multistream-index.txt.bz2").write_bytes(
        bz2.compress(("\n".join(lines) + "\n").encode()))
    return path


def _streams() -> list[list[tuple[int, str, str]]]:
    pages = [(i + 1, "0", _Page(i).page()) for i in range(30)]
    pages[3] = (4, "0", "#REDIRECT [[Elsewhere#There]]\n[[Category:Redirects]]")
    pages[5] = (6, "4", "{{Project page}} not an article")
    return [pages[i:i + 10] for i in range(0, 30, 10)]


def _run(capsys, *argv) -> tuple[int, dict]:
    from tools.measure import wikitext_identity as tool
    code = tool.main(list(argv))
    return code, json.loads(capsys.readouterr().out)


def test_the_checker_proves_identity_on_a_dump(tmp_path, capsys):
    path = _dump(tmp_path, _streams())
    code, got = _run(capsys, "--dump", str(path), "--pages", "29")
    assert code == 0 and got["verdict"] == "IDENTICAL"
    assert (got["read"], got["compared"], got["differ"]) == (30, 29, 0)
    assert got["redirect_info"] >= 1 and got["parse_all"] + got["redirect_info"] == 29
    assert got["reference_us_per_page"] > 0 and got["live_us_per_page"] > 0


def test_the_checker_never_calls_too_few_pages_a_success(tmp_path, capsys):
    path = _dump(tmp_path, _streams())
    code, got = _run(capsys, "--dump", str(path), "--pages", "1000")
    assert code == 2 and got["verdict"] == "INCOMPLETE" and got["compared"] == 29
    only_project = _dump(tmp_path / "b", [[(1, "4", "a"), (2, "4", "b")]])
    code, got = _run(capsys, "--dump", str(only_project), "--pages", "0")
    assert code == 2 and got["verdict"] == "INCOMPLETE" and got["compared"] == 0


def test_the_checker_names_a_differing_page(tmp_path, capsys, monkeypatch):
    path = _dump(tmp_path, _streams())
    real = new.parse_all

    def off_by_one(text):
        out = real(text)
        if text == _Page(7).page():
            out["sections"] = out["sections"] + [("", "x")]
        return out

    monkeypatch.setattr(new, "parse_all", off_by_one)
    out = tmp_path / "diff.jsonl"
    code, got = _run(capsys, "--dump", str(path), "--pages", "29", "--diff-out", str(out))
    assert code == 1 and got["verdict"] == "DIFFERENT" and got["differ"] == 1
    assert got["first_differing"] == [{"dump": path.name, "page_id": "8", "title": "P8",
                                       "function": "parse_all"}]
    assert json.loads(out.read_text())["wikitext"] == _Page(7).page()
