"""Invariant 11 (corpus fidelity), the origin-form note: "WHERE SEVERAL
ORIGIN FORMS EXIST … choose the one that best fits our goals and RECORD
the choice with its reason beside the plugin — the form is part of what a
verifier needs in order to reproduce our records."

Beside the plugin means in the plugin's own module: every registered
plugin's source module carries, in its docstring, a section headed
"THE ORIGIN FORM, and why" (normattiva's was the first, 2026-10-12). The
Codex's `origin.form` binds (06 §6.4); this note explains it, and
docs/notes/pack-format.md's table indexes the notes. A plugin registered
without one fails here, before a collection is created from it.
"""
import ast
import inspect
import re
from pathlib import Path

from app import collections as collections_mod

ROOT = Path(__file__).resolve().parent.parent
HEADING = "THE ORIGIN FORM, and why"
#: a heading with a sentence under it is not a note; the shortest real one
#: (pubmed's) is ~600 characters
MIN_CHARS = 300


def origin_form_note(docstring: str | None) -> str | None:
    """The section under HEADING, up to the next section (a blank line then
    an upper-case lead word) or the end; None without the heading."""
    if not docstring or HEADING not in docstring:
        return None
    rest = docstring.split(HEADING, 1)[1]
    m = re.search(r"\n\s*\n(?=[A-Z][A-Z ,'-]{6,}\b)", rest)
    return rest[:m.start()] if m else rest


def _module_docstring(source) -> tuple[str, str | None]:
    path = Path(inspect.getsourcefile(type(source)))
    return path.relative_to(ROOT).as_posix(), ast.get_docstring(ast.parse(path.read_text(encoding="utf-8")))


def test_every_registered_plugin_records_its_origin_form_beside_it():
    registry = collections_mod.PLUGIN_REGISTRY
    assert len(registry) >= 5, "the registry is not what this test walks — the test is wrong"
    missing, thin = [], []
    for name, source in sorted(registry.items()):
        where, doc = _module_docstring(source)
        note = origin_form_note(doc)
        if note is None:
            missing.append(f"{name} ({where})")
        elif len(" ".join(note.split())) < MIN_CHARS:
            thin.append(f"{name} ({where}): {len(note)} characters")
    assert missing == [], (
        f"invariant 11: no '{HEADING}' section in the module docstring of {missing}: "
        "write which origin form the plugin takes and why, beside it (06 §6.4)")
    assert thin == [], f"invariant 11: an origin-form note too short to give a reason: {thin}"


def test_the_index_of_notes_names_every_registered_plugin():
    table = (ROOT / "docs" / "notes" / "pack-format.md").read_text(encoding="utf-8")
    section = table.split("#### Which origin form, and why", 1)[1].split("\n#", 1)[0]
    rows = {m.group(1) for m in re.finditer(r"^\| ([a-z]+)\b", section, re.MULTILINE)}
    absent = sorted(set(collections_mod.PLUGIN_REGISTRY) - rows)
    assert absent == [], f"docs/notes/pack-format.md's origin-form table has no row for {absent}"


def test_the_note_reader_finds_the_section_and_its_end():
    doc = "Intro.\n\nTHE ORIGIN FORM, and why — the dump, because it is checksummed.\n\nTHE VERSION KEY is x."
    assert origin_form_note(doc).strip() == "— the dump, because it is checksummed."
    assert origin_form_note("No such section.") is None
    assert origin_form_note(None) is None
