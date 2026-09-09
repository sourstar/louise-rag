"""tools/build_calibration_set.py — freeze the 512-text calibration set.

Run once; the OUTPUT is the artefact. This tool ships beside it so the set
is auditable — anyone can re-run it against the same corpus and get the
same bytes, rather than trusting 512 texts of unexplained provenance.

    python tools/build_calibration_set.py <gutenberg-root> \
        --out calibration/text.jsonl --provenance calibration/text.sources.json

WHY THE SET WAS REBUILT. Measured 2026-09-09, the first set's
longest text was 118 words — about 174 tokens — against spaces whose `ctx`
is 512 (MedCPT) and 8192 (BGE-M3). NOTHING IN IT CAME WITHIN A THIRD OF
THE CONTEXT WINDOW. That matters because of what this node has already
lived through: `approved-encoders.md` on the b10630 incident records that
the bad build "tested clean afterwards at every concurrency, with
identical and with distinct long texts — all 1.000000. Short tests do not
catch this, which is precisely why the approval trial (16 texts) and the
backend gate both pass while the corpus takes damage."

So the gate as it stood would have passed the exact build that damaged
this corpus. A calibration set that never fills a context window cannot
see the one failure this project has actually had.

THE LADDER. 512 texts, in five strata — the counts of the drawn two are
what the rule below actually produced, measured 2026-09-09:

    64    5–174 tokens   the near-duplicate PAIRS, verbatim and LAST
   192    5–174 tokens   KEPT from the rest of the existing set — tokenizer
                         edges, pooling, degenerate input, every script
    52    5–174 tokens   every non-Latin text the even spread happened to
                         miss, rescued
  ~104  175–2048 tokens  drawn — half at 2048, half spread; FILLS a 512
                         or 1024 window
  ~104 2049–8192 tokens  drawn — half at 8192, half spread; long-attention

The short strata are the existing set's whole range (its longest text is
174 tokens), spread evenly across it rather than split at a boundary it
does not have; the drawn strata pick up at 175 so the ladder is
continuous from 5 tokens to 8192. The two drawn counts are not constants:
they are whatever is left after the kept texts, split evenly, so rescuing
a script never costs the set its size.

A text longer than `ctx` is truncated, deterministically, so one long
text serves a small-`ctx` encoder as a full-window probe and a large-`ctx`
one as a genuinely long input. That is why a single set can serve every
space: it needs texts that REACH the largest window, and smaller windows
get theirs for free.

WHERE THE TEXT COMES FROM. The short stratum is kept verbatim from the
existing set — it already spans Latin, Cyrillic, Greek, CJK, Arabic,
Hebrew, Devanagari and Hangul, and short is what it is good at. The two
long strata are drawn from public-domain Project Gutenberg prose,
selected by a fixed rule recorded in `--provenance`.

A LIMIT WORTH KNOWING. Gutenberg carries long text in Latin scripts, plus
Chinese (259 books over 100 KB), Greek (206), Japanese (17) and a handful
of Russian, Bulgarian, Hebrew and Serbian. It has essentially no long
Arabic, Devanagari or Hangul. Those three therefore appear only in the
short strata, and a build that is wrong ONLY on long text in ONLY those
scripts would still slip through. That conjunction is narrow, it is
better than the status quo of no long text at all, and it is written down
here rather than discovered later.

Corpus-independent in the sense §9.12 means: the set does not depend on
the collection being calibrated. Overlap with a Gutenberg collection is
harmless — the gate compares one encoder's output against another's on
fixed input, and what §9.12 guards against is a creator choosing texts
their own build does well on.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.hold import calibration as cal          # noqa: E402

#: How many texts to KEEP from the existing set, spread across its whole
#: length range. They carry every script the set has, and the three the
#: corpus cannot supply at length — Arabic, Devanagari, Hangul — exist
#: nowhere else.
KEEP = 192

#: The existing set ENDS with 32 near-duplicate pairs — texts that differ
#: in one controlled way. They are its sharpest instrument: a build that is
#: subtly wrong collapses or exaggerates exactly those small differences,
#: where unrelated texts reveal nothing at all. They are carried over
#: verbatim, still adjacent, and still LAST, because that is where
#: `tests/hold/test_calibration.py` reads them (`texts[-64:]`) and because
#: a pair is only a probe while both halves are in the set — an even
#: spread by length would have scattered them and kept one of each.
PAIRS = 64

#: The drawn strata, as (share of what is left, low tokens, high tokens).
#: A share rather than a count because `_non_latin_missing` may add a few
#: texts above KEEP, and the total is cal.SIZE whatever it adds.
LADDER = ((1, 175, 2048), (1, 2049, 8192))

#: A token is four characters, the same rule `app/chunking.py` uses.
#:
#: MEASURED against the real thing on 2026-09-09 — every text in the frozen
#: set embedded by Qwen3-Embedding-8B-Q4_K_M, the encoder the Gutenberg
#: collection pins — the rule is right in the middle and wrong at both ends:
#:
#:     chars per REAL token   min 1.02   median 3.68   max 5.70
#:
#: So 4 is a good MEDIAN and no kind of bound. Latin prose runs 5-ish, which
#: makes the estimate pessimistic and harmless; CJK runs 1.02, which makes it
#: FOUR TIMES optimistic — a text this file calls 8192 tokens can be 32,000.
#: 67 of the 512 therefore exceed `ctx` 8192 and are truncated to fit by
#: `app/embeddings.py` before they reach the encoder (09 §9.12).
#:
#: That is not a defect in the set and it is why the numbers below are called
#: estimates. A long text that overshoots is truncated to exactly what the
#: window holds, which is a BETTER full-window probe than one built to a
#: guess: the guess falls short, the overshoot never does.
CHARS = 4

#: Languages to draw long text from, in a fixed order so the selection is
#: reproducible. Latin scripts first because there is most of them, then
#: the non-Latin ones the corpus actually carries at length.
LANGS = ("en", "fr", "de", "es", "it", "nl", "pt", "sv", "da", "fi", "la",
         "zh", "el", "ja", "ru", "bg", "he", "sr")

_LANG = re.compile(r"<dcterms:language>.*?>([a-z]{2,3})<", re.S)
_FORBIDDEN = {"Cc", "Cf", "Cs", "Co", "Cn"}
#: A blank line — the paragraph separator. Gutenberg's plain text is CRLF,
#: so this cannot be `"\n\n"`; see the note in `passage`.
_BLANK = re.compile(r"(?:\r?\n)[ \t]*(?:\r?\n)")


def clean(text: str) -> str:
    """NFC, no control characters, single spaces, one line.

    A set line is one JSON string on one line (09 §9.12), and `check()`
    refuses a control character, so the passage is flattened here rather
    than left to fail validation later.
    """
    text = unicodedata.normalize("NFC", text)
    text = "".join(ch for ch in text
                   if unicodedata.category(ch) not in _FORBIDDEN or ch in "\t\n")
    return " ".join(text.split())


def books_by_language(root: Path) -> dict[str, list[tuple[int, Path]]]:
    """`{lang: [(book_id, txt path)…]}`, in book-id order.

    Sorted, so the selection below is a function of the corpus and not of
    the filesystem's iteration order.
    """
    out: dict[str, list[tuple[int, Path]]] = {}
    for d in sorted(root.iterdir(), key=lambda p: (not p.name.isdigit(), p.name)):
        if not d.is_dir() or not d.name.isdigit():
            continue
        rdf = d / f"pg{d.name}.rdf"
        if not rdf.is_file():
            continue
        try:
            m = _LANG.search(rdf.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        if not m or m.group(1) not in LANGS:
            continue
        txt = next((p for p in sorted(d.iterdir())
                    if ".txt" in p.name and "nroff" not in p.name
                    and "readme" not in p.name.lower() and p.is_file()), None)
        if txt is None:
            continue
        out.setdefault(m.group(1), []).append((int(d.name), txt))
    return {k: sorted(v) for k, v in sorted(out.items())}


def passage(txt: Path, want_chars: int, nth: int) -> str | None:
    """The `nth` distinct passage of `want_chars` from a book's body.

    The Gutenberg wrapper is cut with the same markers `pg_text` uses, and
    the passage starts at a paragraph boundary so it begins mid-sentence
    no more often than a real chunk does.
    """
    from app.plugins.gutenberg import pg_text
    try:
        body = pg_text.read_clean(txt)
    except OSError:
        return None
    if len(body) < want_chars * (nth + 2):
        return None
    # A BLANK LINE, not `"\n\n"`. Gutenberg's plain text is CRLF, so its
    # paragraphs are separated by `\r\n\r\n`, and searching for `\n\n`
    # matches nothing at all — 40 books of up to 4.4 million characters
    # yielded not one passage before this was a regex.
    m = _BLANK.search(body, want_chars * (nth + 1) // 2)
    if m is None:
        return None
    got = clean(body[m.end():m.end() + want_chars * 2])[:want_chars]
    return got if len(got) >= want_chars * 3 // 4 else None


def build(root: Path) -> tuple[list[str], list[dict]]:
    """The 512 texts and what each long one was drawn from."""
    _keep_id, keep_texts = cal.load("text")
    # The pairs are held back from every pool below and re-attached at the
    # end, so nothing can spread them apart or select one half of one.
    pairs = keep_texts[-PAIRS:]
    rest = keep_texts[:-PAIRS]
    pool = sorted(rest, key=lambda t: (len(t), t))
    if len(pool) < KEEP:
        raise SystemExit(f"the existing set has {len(pool)} texts outside "
                         f"its pairs; the ladder keeps {KEEP}")

    texts: list[str] = []
    sources: list[dict] = []

    # Spread across the whole length range rather than split at a
    # boundary: the existing set runs 5–174 tokens and is 87 % under 64,
    # so taking one end of it would throw away the little length it has.
    texts += _spread(pool, KEEP)
    texts += _non_latin_missing(rest, texts)

    by_lang = books_by_language(root)
    if not by_lang:
        raise SystemExit(f"{root}: no books with a language tag and a .txt")
    print(f"  languages available: "
          f"{ {k: len(v) for k, v in by_lang.items()} }", file=sys.stderr)

    draw = cal.SIZE - PAIRS - len(texts)
    if draw < len(LADDER):
        raise SystemExit(f"only {draw} texts left to draw for "
                         f"{len(LADDER)} strata; lower KEEP")
    shares = sum(w for w, _, _ in LADDER)
    counts = [draw * w // shares for w, _, _ in LADDER]
    counts[-1] += draw - sum(counts)          # the remainder, to the last

    for count, (_w, lo, hi) in zip(counts, LADDER):
        langs = [l for l in LANGS if l in by_lang]
        cursor = {l: 0 for l in langs}
        nth = {l: 0 for l in langs}
        got = 0
        while got < count:
            progressed = False
            for lang in langs:
                if got >= count:
                    break
                # HALF of each stratum sits at the band's ceiling. A text
                # that FILLS the window is the probe b10630 asked for, and
                # a text one token short of it is not; an even spread over
                # eight rungs put only an eighth of the stratum there, so
                # thirteen texts in the whole set reached 8192. The other
                # half spreads across the band, because a tokeniser's
                # edges are at odd lengths too.
                want = hi if got % 2 else lo + (hi - lo) * ((got // 2) % 8) // 7
                books = by_lang[lang]
                while cursor[lang] < len(books):
                    book_id, txt = books[cursor[lang]]
                    p = passage(txt, want * CHARS, nth[lang])
                    cursor[lang] += 1
                    if p and p not in texts and p not in pairs:
                        texts.append(p)
                        sources.append({"lang": lang, "book": book_id,
                                        "nth": nth[lang], "tokens": want})
                        got += 1
                        progressed = True
                        break
            if not progressed:
                for lang in langs:                 # a second pass per book
                    cursor[lang] = 0
                    nth[lang] += 1
                if max(nth.values()) > 6:
                    raise SystemExit(
                        f"only {got} of {count} texts in {lo}–{hi} tokens; "
                        f"the corpus does not hold enough long prose")

    texts += pairs
    if len(texts) != cal.SIZE or len(set(texts)) != cal.SIZE:
        raise SystemExit(f"built {len(texts)} texts, {len(set(texts))} of them "
                         f"distinct; the set is {cal.SIZE} distinct texts")
    return texts, sources


def _non_latin_missing(all_texts: list[str], chosen: list[str]) -> list[str]:
    """Any non-Latin text the even spread happened to miss.

    Gutenberg carries no long Arabic, Devanagari or Hangul, so the
    existing set's are the ONLY ones in those scripts anywhere in the
    ladder. Losing one to an arithmetic accident would silently remove a
    script from the gate.
    """
    import unicodedata
    have = set(chosen)
    out = []
    for t in all_texts:
        if t in have:
            continue
        if any(ord(ch) > 0x24F for ch in t):
            out.append(t)
            have.add(t)
    return out


def _spread(pool: list[str], n: int) -> list[str]:
    """`n` texts spread evenly across a length-sorted pool, so the stratum
    keeps the pool's shape instead of taking one end of it."""
    if n >= len(pool):
        return list(pool)
    step = len(pool) / n
    return [pool[int(i * step)] for i in range(n)]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("root", type=Path, help="the Gutenberg corpus root")
    ap.add_argument("--out", type=Path, default=Path("calibration/text.jsonl"))
    ap.add_argument("--provenance", type=Path, default=None)
    a = ap.parse_args(argv)

    texts, sources = build(a.root)
    cal.check(texts)                      # refuse a set that could not do its job
    data = cal.render(texts)

    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_bytes(data)
    if a.provenance:
        a.provenance.write_text(json.dumps(sources, indent=1), encoding="utf-8")

    print(f"\n  {a.out}  {len(data):,} bytes")
    print(f"  set id  {cal.set_id(data)}")
    print(f"  coverage {cal.coverage(texts)}")
    lens = sorted(len(t) // CHARS for t in texts)
    print(f"  tokens  min {lens[0]}  p50 {lens[len(lens) // 2]}  "
          f"p90 {lens[int(.9 * len(lens))]}  max {lens[-1]}")
    print(f"  total   {sum(lens):,} tokens")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
