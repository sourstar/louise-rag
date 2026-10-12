"""The rendition: an act as the canonical text the encoder sees
(docs/notes/gazzetta-plugin.md §4; format approved by the operator,
2026-09-14).

Three constraints, in this order: STRUCTURE PRESERVED — every partition,
article number (5-bis included), comma, lettera and annex survives, and
survives recoverably (`skeleton_matches` reads it back out of the text and
the tests hold it to the AKN's); READABLE in a plain editor; and BETTER FOR
THE ENCODER — prose, no markup, no attribute soup.

What leaves the embedded text, and where it goes instead:

  * the `((…))` amendment delimiters — markup of the consolidation
    (invariant 11 (a)); the words stay, and where each insertion sits is in
    the record's `passive_modifications`. They are removed over a whole
    article at once: a span opens in one comma and closes in another (the
    census found the `))` orphaned on a line of its own otherwise);
  * `((3))`-style pointers to an update note, for the same reason;
  * an article repealed in place ("((ARTICOLO ABROGATO DAL …))", 839 in
    the 40 codes) keeps its heading and loses its body, so it produces NO
    chunk — its repeal statement is in the record's `repealed_articles`;
    the same for a REPEAL STATE, a whole act whose every article reads
    "((PROVVEDIMENTO ABROGATO DALLA …))" (87,798 members of the archive,
    normattiva-multivigenza-review.md §1): no chunk at all, and the record
    says so (`repealed_in_full`, `in_force`);
  * the administration's notes to an article, the preface's notes, the
    preamble and the conclusions — the act's apparatus, kept in the record
    and never embedded (in the file they read as `> nota:`).

A change to anything here changes `sections`, so it bumps the plugin's
PARSER_VERSION (§4.3 folded the rendition into the chunker's version; on
this platform the parser owns `sections` and the chunker only cuts them).
"""
import json
import re

from . import akn

_NOTE_MARK = re.compile(r"\(\(\s*\d+\s*\)\)")
_REPEALED = re.compile(r"^\s*\(\(\s*(ARTICOLO\s+|PROVVEDIMENTO\s+|ATTO\s+)?(ABROGAT|SOPPRESS)", re.I)
#: a comma's number at the margin: "1. ", "2-bis. ", "10-quinquies."
_COMMA = re.compile(r"^(\d+(?:-[a-z]+)?\.)(?:\s|$)")


def unmark(text: str) -> str:
    """`((…))` delimiters out, the text inside kept. A scanner, not a regex:
    the law's own parentheses nest inside the markers ("((la legge (n.
    5)))"), and "lettera a))" — a lettera closing a parenthesis — must not
    be read as a marker's end."""
    text = _NOTE_MARK.sub("", text)
    out: list[str] = []
    stack: list[str] = []
    i, n = 0, len(text)
    while i < n:
        if text.startswith("((", i):
            stack.append("M")
            i += 2
            continue
        c = text[i]
        if c == "(":
            stack.append("P")
        elif c == ")":
            if stack and stack[-1] == "P":
                stack.pop()
            elif stack and stack[-1] == "M" and text.startswith("))", i):
                stack.pop()
                i += 2
                continue
        out.append(c)
        i += 1
    return "".join(out)


def _tidy(text: str) -> str:
    """Per line: indentation kept, inner runs of spaces to one, trailing
    space off; lines left empty (a `))` that stood alone) dropped."""
    out = []
    for line in text.split("\n"):
        body = line.lstrip(" ")
        body = re.sub(r"[ \t]{2,}", " ", body).rstrip()
        if body:
            out.append(line[: len(line) - len(line.lstrip(" "))] + body)
    return "\n".join(out)


def plain(text: str) -> str:
    """A label or heading, unmarked and on one line."""
    return _tidy(unmark(text)).replace("\n", " ")


def is_repealed(article: akn.Article) -> bool:
    texts = [b.text for b in article.blocks if b.text.strip()]
    return bool(texts) and all(_REPEALED.match(t) for t in texts)


def article_line(article: akn.Article) -> str:
    """"Art. 1. — (Delega al Governo …)." — number and rubrica as written."""
    return " — ".join(x for x in (article.num, plain(article.heading)) if x)


def _raw_lines(blocks: list[akn.Block]) -> list[str]:
    lines: list[str] = []
    for b in blocks:
        indent = "   " * b.depth
        parts = b.text.split("\n") if b.text else [""]
        first = f"{b.num} {parts[0]}".strip() if b.num else parts[0]
        lines.append(indent + first)
        lines.extend(indent + p for p in parts[1:])
    return lines


def blocks_text(blocks: list[akn.Block]) -> str:
    """Commi at the margin, lettere indented three spaces, numeri six."""
    return _tidy(unmark("\n".join(_raw_lines(blocks))))


def article_body(article: akn.Article) -> str:
    """The text one article contributes: its line, then its commi. Empty
    when it is repealed in place — nothing to embed, and the chunker cuts
    nothing from an empty body."""
    if is_repealed(article):
        return ""
    return "\n".join(x for x in (article_line(article), blocks_text(article.blocks)) if x)


def label(article: akn.Article) -> str:
    """The section's heading: the partition, then the article —
    "PARTE I — AMBITO … › Art. 1. — (Oggetto)". An article repealed in
    place reads "(abrogato)" after its number."""
    head = f"{article.num} — (abrogato)" if is_repealed(article) else article_line(article)
    return " › ".join(x for x in (plain(article.partition), head.strip()) if x) or "Testo"


def sections(act: akn.Act) -> list[tuple[str, str]]:
    """`[(label, text)]`: one per article in reading order, then one per
    annex. An annex with no text in the XML (the origin keeps many as PDF
    or image) is a section with an empty body — present, not chunked. The
    Codice civile arrives this way round: two articles of the decree, then
    its 3,280 articles as annexes named "CODICE CIVILE-art. 906"."""
    out = [(label(a), article_body(a)) for a in act.articles]
    out += [(plain(att.name) or "Allegato", blocks_text(att.blocks))
            for att in act.attachments]
    return out


def denomination(act: akn.Act) -> str:
    """"LEGGE 31 ottobre 2003, n. 306" — how the act is cited."""
    head = " ".join(x for x in (act.doc_type, act.doc_date_text) if x)
    return f"{head}, n. {act.doc_number}" if act.doc_number else head


def render(act: akn.Act, front: dict) -> str:
    """The whole act as one readable file: JSON-valued front matter (valid
    YAML), the denomination and title, then partitions (`## `; a bare `##`
    closes one), articles (`### `), commi at the margin and lettere
    indented, notes as `> nota:`, annexes after `## Allegati`."""
    lines = ["---"] + [f"{k}: {json.dumps(v, ensure_ascii=False)}"
                       for k, v in front.items()] + ["---", "", f"# {denomination(act)}"]
    if act.doc_title:
        lines.append(act.doc_title)
    current = ""
    for a in act.articles:
        part = plain(a.partition)
        if part != current:
            current = part
            lines += ["", f"## {current}" if current else "##"]
        lines += ["", "### " + (f"{a.num} — (abrogato)" if is_repealed(a) else article_line(a))]
        if not is_repealed(a):
            body = blocks_text(a.blocks)
            if body:
                lines.append(body)
        for note in a.notes:
            lines += [f"> nota: {line}" for line in note.split("\n")]
    if act.attachments:
        lines += ["", "## Allegati"]
        for att in act.attachments:
            lines += ["", f"### {plain(att.name) or 'Allegato'}"]
            body = blocks_text(att.blocks)
            if body:
                lines.append(body)
    return "\n".join(lines).rstrip() + "\n"


def skeleton_of_act(act: akn.Act) -> list[tuple[str, str, tuple[str, ...]]]:
    """`[(partition, article number, (comma numbers the AKN marks up…))]`."""
    return [(plain(a.partition), a.num, () if is_repealed(a) else tuple(
        b.num for b in a.blocks if b.depth == 0 and _COMMA.match(b.num + " ")))
        for a in act.articles]


def skeleton_of_text(text: str) -> list[tuple[str, str, tuple[str, ...]]]:
    """`[(partition, article number, (every comma number at the margin…))]`
    read back out of `render`'s text alone. The text shows MORE commi than
    the markup where the origin wrote a comma's number in its prose
    without marking it up — the census found both."""
    body = text.split("\n---\n", 1)[-1]
    out: list = []
    partition, cur, num, commi = "", "", None, []
    for line in body.split("\n"):
        if line == "## Allegati":
            break
        if line == "##":
            partition = ""
        elif line.startswith("## "):
            partition = line[3:]
        elif line.startswith("### "):
            if num is not None:
                out.append((cur, num, tuple(commi)))
            cur, num, commi = partition, line[4:].split(" — ", 1)[0], []
        elif num is not None and not line.startswith((" ", ">")):
            m = _COMMA.match(line)
            if m:
                commi.append(m.group(1))
    if num is not None:
        out.append((cur, num, tuple(commi)))
    return out


def skeleton_matches(act: akn.Act, text: str) -> bool:
    """§4.4's test: the same partitions and articles in the same order, and
    every comma the AKN marks up present in the text, in order."""
    want, got = skeleton_of_act(act), skeleton_of_text(text)
    if [(p, n) for p, n, _ in want] != [(p, n) for p, n, _ in got]:
        return False
    for (_, _, marked), (_, _, seen) in zip(want, got):
        it = iter(seen)
        if not all(c in it for c in marked):      # a subsequence, in order
            return False
    return True
