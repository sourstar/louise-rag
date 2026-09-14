"""Wikipedia parser 3 — the operator's decisions of 2026-09-14 and what the
field map over 3,052 real pages found dropped or leaking (invariant 11).

Built pages, in a dump laid out as Wikimedia writes it: a <siteinfo>
header stream, page streams, and a separate `</mediawiki>` footer stream
that the index's last group runs into.
"""
import bz2
import json
from pathlib import Path
from xml.sax.saxutils import escape

import pytest

from app import plugin_io
from app.pack import cutoff
from app.plugins.base import DiscoverState
from app.plugins.pubmed.source import PubmedSource
from app.plugins.wikipedia import SOURCE, wikitext
from app.plugins.wikipedia import source as wsrc

STAMP = "2026-07-31T12:02:55Z"
USER = "<contributor><username>Alice</username><id>42</id></contributor>"
IP = "<contributor><ip>192.0.2.7</ip></contributor>"
HIDDEN = '<contributor deleted="deleted" />'

ARTICLE = """{{Short description|A town}}
{{DEFAULTSORT:Town, North}}
{{Infobox person
| name = Northtown
| founded = {{start date|1819|4|1}}
}}
{{Automatic taxobox|taxon=Felis}}
[[File:Spring.jpg|thumb|180px|right|Spring flowers in [[Northtown]]]]
'''Northtown''' is a town.<ref>{{cite web|url=https://x.org|title=T}}</ref> It is {{convert|15|C|F}} in spring, and the motto is {{lang|fr|la ville}}.
__TOC__
{{Quote|text=A fine place.|author=A. Visitor}}
The square measures <math>x^2 + 1</math> metres.

== History ==
{{Main|History of Northtown}}

=== Early ===
It began in 1819.<ref
name="a">Jones 2001</ref>

== Gallery ==
<gallery>
File:A.jpg|The square
</gallery>

== Notes ==
=== Citations ===
{{reflist}}
=== Works cited ===
* {{cite book|title=A History|author=Smith}}

[[Category:Towns|Northtown]]
[[Category:1819 establishments]]
[[Category:Northtown| ]]
[[de:Nordstadt]]
"""
REVERT = ("<comment>Reverted edit by [[Special:Contributions/192.0.2.9|192.0.2.9]] "
          "([[User talk:192.0.2.9|talk]]) to last version by 2001:db8::1 at 12:30."
          "</comment>")

REDIRECT = "#REDIRECT [[AA#Rivers]]\n[[Category:Redirects from short names]]"


def _page(pid: int, title: str, text: str, *, who: str = USER,
          comment: str = "<comment>fix typo</comment>", redirect: str = "") -> str:
    red = f'<redirect title="{redirect}" />' if redirect else ""
    return (f"<page><title>{title}</title><ns>0</ns><id>{pid}</id>{red}"
            f"<revision><id>{pid}00</id><parentid>{pid}99</parentid>"
            f"<timestamp>{STAMP}</timestamp>{who}<minor />{comment}"
            f"<origin>{pid}00</origin><model>wikitext</model>"
            f"<format>text/x-wiki</format>"
            f'<text bytes="{len(text.encode())}" sha1="s{pid}" xml:space="preserve">'
            f"{escape(text)}</text><sha1>s{pid}</sha1></revision></page>\n")


@pytest.fixture
def dump(tmp_path) -> dict:
    """itwiki, three page streams, a header and a footer stream."""
    wsrc._stream_cache.clear()
    root = tmp_path / "corpus"
    root.mkdir()
    header = bz2.compress(b'<mediawiki xml:lang="it"><siteinfo><dbname>itwiki'
                          b"</dbname></siteinfo>\n")
    streams = [
        [_page(1, "Northtown", ARTICLE), _page(2, "AA (river)", REDIRECT, redirect="AA")],
        [_page(3, "Anon", "Edited anonymously.", who=IP),
         _page(6, "Reverted", "A reverted page.", comment=REVERT)],
        [_page(4, "Hidden", "A suppressed editor.", who=HIDDEN,
               comment='<comment deleted="deleted" />')],
    ]
    blobs = [bz2.compress("".join(s).encode()) for s in streams]
    footer = bz2.compress(b"</mediawiki>\n")
    path = root / "itwiki-20260801-pages-articles-multistream.xml.bz2"
    path.write_bytes(header + b"".join(blobs) + footer)
    lines, offset = [], len(header)
    for s, blob in zip(streams, blobs):
        for page in s:
            pid = page.split("<id>", 1)[1].split("<", 1)[0]
            title = page.split("<title>", 1)[1].split("<", 1)[0]
            lines.append(f"{offset}:{pid}:{title}")
        offset += len(blob)
    (root / "itwiki-20260801-pages-articles-multistream-index.txt.bz2").write_bytes(
        bz2.compress(("\n".join(lines) + "\n").encode()))
    refs = {r.native_id: r for r in SOURCE.discover(DiscoverState(corpus_root=root))}
    return {"root": root, "refs": refs}


def _doc(dump, pid: str):
    """Through `plugin_io.canonical_doc` — the worker's, build's and
    reparse's path, which rebuilds the DocRef WITHOUT `extra`."""
    ref = dump["refs"][pid]
    return plugin_io.canonical_doc(SOURCE, {"native_id": pid, "path": ref.locator})


def _prose(doc) -> str:
    return "\n".join(b for _, b in doc.sections)


# ---- the operator's decisions -------------------------------------------

def test_date_is_the_revision_date_and_the_instant_is_kept_whole(dump):
    doc = _doc(dump, "1")
    assert doc.date == "2026-07-31"
    assert doc.extra["revised"] == STAMP, "parser 2 cut it to [:10]"


def test_a_disclosed_contributor_is_kept(dump):
    assert _doc(dump, "1").extra["last_editor"] == {"username": "Alice", "id": "42"}


def test_an_anonymous_contributor_is_not_available_and_the_ip_is_nowhere(dump):
    doc = _doc(dump, "3")
    assert doc.extra["last_editor"] == "not available"
    held = json.dumps([doc.title, doc.sections, doc.extra, doc.subjects], default=str)
    assert "192.0.2.7" not in held


def test_a_suppressed_contributor_and_comment_are_not_available(dump):
    doc = _doc(dump, "4")
    assert doc.extra["last_editor"] == "not available"
    assert doc.extra["comment"] == "not available"


def test_an_ip_named_in_the_edit_summary_is_discarded(dump):
    """A revert's summary names the anonymous editor it went back to."""
    comment = _doc(dump, "6").extra["comment"]
    assert "192.0.2.9" not in comment and "2001:db8::1" not in comment
    assert comment == ("Reverted edit by [[Special:Contributions/not available|"
                       "not available]] ([[User talk:not available|talk]]) to last "
                       "version by not available at 12:30.")


def test_the_rest_of_the_revision_is_kept(dump):
    x = _doc(dump, "1").extra
    assert x["revision"] == "100" and x["parent_revision"] == "199"
    assert x["revision_origin"] == "100" and x["minor_edit"] is True
    assert x["comment"] == "fix typo" and x["revision_sha1"] == "s1"
    assert x["text_bytes"] == str(len(ARTICLE.encode())) and x["namespace"] == "0"
    assert x["wikitext"] == ARTICLE, "the page as the origin wrote it"


# ---- defects -------------------------------------------------------------

def test_language_comes_from_the_dump_through_the_pipelines_path(dump):
    """Parser 2 read `ref.extra`, which canonical_doc does not rebuild: an
    itwiki page parsed as "en" in the worker, build and reparse."""
    doc = _doc(dump, "1")
    assert doc.language == "it" and doc.extra["wiki"] == "itwiki"


def test_the_last_stream_parses_despite_the_footer_stream(dump):
    """The index's last group runs to EOF and so takes `</mediawiki>`."""
    assert dump["refs"]["4"].locator.endswith("::0")
    assert _doc(dump, "4").title == "Hidden"


def test_origin_is_the_dump_and_its_two_field_member_and_reads_back(dump):
    ref = dump["refs"]["3"]
    origin = plugin_io.origin_locator(SOURCE, ref.locator, dump["root"])
    _, start, end = wsrc.split_locator(ref.locator)
    assert origin == f"itwiki-20260801-pages-articles-multistream.xml.bz2#{start}::{end}"
    local, locator = plugin_io.local_locator(SOURCE, origin, dump["root"])
    assert local.is_file() and locator == ref.locator


def test_a_one_field_member_is_unchanged():
    assert plugin_io.origin_locator(PubmedSource(), "/base/a/b.xml.gz::5", "/base") \
        == "a/b.xml.gz#5"


def test_the_full_timestamp_changes_no_cutoff():
    """Wikipedia's `--until` is the dump date; nothing reads `revised`."""
    assert cutoff.excluded("wikipedia", {"extra": {"revised": STAMP}}, "2020-01-01") is None
    assert cutoff.normalise(STAMP) == "2026-07-31"


# ---- the prose: what leaked in, and what fell out ----------------------

def test_image_options_leave_the_prose_and_the_caption_stays(dump):
    doc = _doc(dump, "1")
    prose = _prose(doc)
    assert "thumb" not in prose and "180px" not in prose
    assert "Spring flowers in Northtown" in prose
    assert {"file": "Spring.jpg", "caption": "Spring flowers in Northtown"} in doc.extra["images"]
    assert {"file": "A.jpg", "caption": "The square", "gallery": True} in doc.extra["images"]


def test_category_and_interlanguage_links_and_magic_words_leave_the_prose(dump):
    doc = _doc(dump, "1")
    prose = _prose(doc)
    assert "Category:" not in prose and "Nordstadt" not in prose and "__TOC__" not in prose
    assert doc.subjects == ["Towns", "1819 establishments", "Northtown"]
    assert doc.extra["interlanguage"] == ["de:Nordstadt"]


def test_quotations_math_and_inline_templates_read_as_written(dump):
    prose = _prose(_doc(dump, "1")).replace("\n", " ")
    assert "A fine place. — A. Visitor" in prose
    assert "x^2 + 1 metres" in prose
    assert "It is 15 C in spring" in prose, "the target unit is the rendering's"
    assert "the motto is la ville" in prose, "not 'fr la ville'"


def test_heading_levels_and_empty_sections_are_kept(dump):
    doc = _doc(dump, "1")
    assert [h for h, _ in doc.sections] == ["", "History", "Early", "Gallery"]
    assert doc.extra["section_levels"] == [1, 2, 3, 2]
    assert dict(doc.sections)["History"] == "", "its prose is all in its subsection"


def test_an_apparatus_subsection_leaves_the_prose_with_its_parent(dump):
    doc = _doc(dump, "1")
    assert "Citations" not in [h for h, _ in doc.sections]
    notes = doc.extra["apparatus"]["Notes"]
    assert "{{cite book|title=A History|author=Smith}}" in notes, \
        "a section of cite templates stripped to '' and was dropped"
    assert "reflist" not in notes, "a placeholder says nothing"


def test_citation_templates_and_template_valued_boxes_are_kept(dump):
    x = _doc(dump, "1").extra
    assert "{{cite web|url=https://x.org|title=T}}" in x["references"]
    boxes = {b["template"]: b["fields"] for b in x["infoboxes"]}
    assert boxes["Infobox person"]["founded"] == "{{start date|1819|4|1}}"
    assert boxes["Automatic taxobox"] == {"taxon": "Felis"}


def test_sort_keys_and_hatnotes_are_kept(dump):
    x = _doc(dump, "1").extra
    assert x["category_sortkeys"] == {"Towns": "Northtown", "Northtown": " "}, \
        "a space is a sort key"
    assert x["defaultsort"] == "Town, North"
    assert {"template": "main", "fields": {"1": "History of Northtown"}} in x["hatnotes"]


def test_a_redirect_keeps_its_fragment_and_its_own_categories(dump):
    doc = _doc(dump, "2")
    assert doc.sections == []
    assert doc.extra["redirect_to"] == "AA"
    assert doc.extra["redirect_target"] == "AA#Rivers"
    assert doc.subjects == ["Redirects from short names"]


def test_a_ref_tag_broken_over_a_line_is_still_a_citation(dump):
    doc = _doc(dump, "1")
    assert dict(doc.sections)["Early"] == "It began in 1819."
    assert "Jones 2001" in doc.extra["references"]


def test_a_table_the_parser_could_not_read_keeps_its_cells_not_its_markup():
    body = ('Before.\n{| class="wikitable" style="font-size:smaller;"\n|+ Some words\n'
            '|-\n! A !! B\n|-\n| style="x" | 1 || [[Link|2]]\n|}\nAfter.')
    assert wikitext._untable(body) == "Before.\nSome words\nA | B\n1 | [[Link|2]]\nAfter."


def test_inline_renderings():
    secs = wikitext.to_sections(
        "Born {{circa|1500}}{{snd}}died {{as of|2020}}; {{frac|2|1|2}} miles, "
        "{{ill|Foo|de|Bar}} and {{langx|fr|texte}}.")
    assert secs[0][1] == "Born c. 1500 – died As of 2020; 2 1/2 miles, Foo and texte."
