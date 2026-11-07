"""Which chat model should answer Ask — measured on Ask's own loop (operator,
2026-11-03: "Consider replacing the chat model with nemo equivalent";
"Evaluate both A and B" — A Nemotron-3.5-Lightning-30B-A3B, B
Nemotron-3.5-Bolt-E4B; the incumbent Qwen3-8B is the reference row).

    python -m tools.measure.chat_eval run    --out DIR --name lightning --model <chat yaml name> \\
        [--queries tools/measure/one_language_queries.tsv] [--per-lang 10] [--collections gutenberg]
    python -m tools.measure.chat_eval score  --out DIR
    python -m tools.measure.chat_eval rescore --out DIR --runs a,b --search-cache FILE
    python -m tools.measure.chat_eval sample --out DIR --runs qwen3-8b,lightning,bolt-e4b [--n 30]

WHAT RUNS. Ask's tool loop exactly as the node serves it: `app.ask.chat.ask`,
whose model is `llm.chat_tools` at its production settings, with
`config.CHAT_MODEL` set to the candidate for the run and the node's own MCP
tools (`louise_search`, `louise_lookup`, `louise_traverse`). Run it inside the
node's image, at night (operator), one model at a time.

A PILOT BEFORE EVERY WHOLE RUN (operator, 2026-11-03: "Check the first 3
question/answer per test category one by one before launching a whole test
that may produce garbage"; measuring.md §12). `run --pilot 3` asks the
first 3 questions of every language and writes them, one by one, to
`runs/<name>/pilot-<tag>.md`. They are read, then `pilot-ok --name --tag
--by --note` records the reading. A whole `run` refuses without one; it
resumes after the pilot's answers.

THE SEARCHES ONCE (`--search-cache FILE`, SearchCache): Ask searches the
question before any model speaks, the same for every model, so the 50
searches are recorded once (`run --direct --search-cache FILE`, reranker
included, as a reader gets them) and replayed for every model. A row's
`seconds` is then the model's part; `replayed_s` is what its searches
took when they ran.

THE QUESTIONS are the 451 labelled queries of one-language-per-work.md, in 18
languages. THE OPERATOR'S FIVE decide (2026-11-03: "As long as it answers
correctly for english, italian, german, french and spanish I am good with it.
Other users can use another chat model to fit their needs"): en it de fr es;
the others show what happens outside them. `--per-lang` takes the first N of
each language, `--langs` keeps only those named: a deterministic subset.

WHAT IS SCORED, DETERMINISTICALLY (no LLM judge, as the legal pilot):
- tool use: calls per answer, calls that failed (unknown tool, refused
  arguments), loops that hit the round bound;
- the answer's language: `app.query_language.guess` (the node's own
  deterministic guess) sentence by sentence, titles and quoted spans out,
  the majority by words (`language_mix`), set against the query's label.
  "No guess" is counted apart, never as right; an answer more than a
  quarter in other languages is counted "mixed" beside it;
- time per answer;
- PRECISION AND TOOLS (operator, 2026-11-03: "fast, not too big, precise in
  articulating the answer and able to use tools"): the sources an answer
  cites in brackets ([gutenberg:2701]), the share of them found in what its
  tools returned ("grounded"; a cited uid no tool returned is invented), and
  the answers given without any tool call.
- THE OPERATOR'S THREE (2026-11-03: "how effectively the model conveys the
  query to Louise, how effectively it summarizes the results, and the amount
  of citations from the results"). Every question is labelled with its work
  (a Wikidata QID); `works.json` (one_language_works.py) lists the work's
  gutenberg books. With `score --works`:
  - conveys: did the model's OWN searches return the work (any edition), and
    at which rank at best (MRR over its searches)? Set against the question
    searched as typed: `run --direct` asks no model, it runs louise_search on
    the question verbatim, so its row is what Louise finds without the model;
  - summarises: does the answer cite the work it was asked about, and how
    often did its searches find the work and the answer leave it out;
  - citations: cited per answer, grounded, citing nothing though results came
    back (above).
- ADDS OR DISTORTS (operator, the same day: "is it adding some additional
  information or distorting an information from the collection, overlapping
  basically its own knowledge?"): entity-level factual consistency (Nan et
  al., EACL 2021, "Entity-level Factual Consistency of Abstractive Text
  Summarization": the precision of the summary's entities against its
  source). Every name and number the answer states is looked up in what its
  tools showed the model (chat.compact, the text it read) and in the
  question; one found in neither came from the model's own knowledge or is
  a distortion. Names are capitalised words not opening a sentence, numbers
  any run of digits — a spaCy NER would be the full method, and a name the
  answer translates (Marie-Antoinette, Maria Antonietta) counts against it,
  so the blind sample reads the flagged ones.
- SAYS WHAT ITS SOURCES SAY (the walk of 2026-11-03: Qwen3-8B put the
  question, pasted as a title, in a source's mouth): every sentence that
  cites, its names, numbers and quoted spans looked up in the sources it
  cites, the question left out — ALCE's citation precision (Gao et al.,
  EMNLP 2023), at the level of entities. The blind sample lists each such
  sentence under its answer.
Quality beyond that is read, not scored: `sample` writes the same questions
with each model's answer under a blind letter, and the key in a separate
file.

THE BENCHMARK OF THE OPTIMIZATIONS (operator, 2026-11-03: "Use the chat
comparison benchmark to work on the optimizations, suspend the test anytime
to implement improvements"). Every answer records where its time went: the
model's rounds (`model_s`), the tool calls (`tools_s`), and inside the
searches the node's own timings — rerank, the language plan, the dense and
word legs. A run is stopped at any time (each answer is written as it ends
and a run resumes where it stopped), the improvement deployed, and the run
resumed with another `--tag`: `score` gives each tag its own rows, so the
same questions before and after are read side by side. The tag is not a
setting: one run's answers may carry several.

SAMPLING ARMS (operator, 2026-11-06: "let's try to optimize the results by
changing their parameters"; docs/notes/nemotron-embed.md, "Sampling, Q8
first"). `run --sampling temperature=0.1,presence_penalty=1.5` sends those
values with every request of the run, over the model file's; the run is
the arm, its name says which (`qwen3-8b-q8.t01`), and its settings.json
holds them, so a run never mixes two. What LocalAI does with them decides
what can be sent (measured from its code, 2026-11-06):
- a request's presence, frequency and repeat penalties of 0 read as UNSET
  (plain floats, `!= 0` in the request merge): the model file's value
  applies, silently. 0 is refused here; the bench's model files hold the
  neutral values and an arm sends the penalty it tests;
- llama.cpp applies the penalties over the last `repeat_last_n` tokens
  (prompt tail included), and 0 there DISABLES them; LocalAI sends 0 when
  the file names none. The bench's files state 64, llama.cpp's default;
- an unset temperature, top_p or top_k gets LocalAI's generic 0.9, 0.95 and
  40, not the card's: the files state every value.
A PROBE BEFORE ANY ARM (operator, 2026-11-06: "Be absolutely sure that
localai is applying your settings otherwise the test is void"): `probe
--model M` checks every value by its effect on the node
(sampling_probe.py); `run` refuses a model whose probe has not passed.
LOOPS are counted (`looped`): a span of words repeated back to back three
times, the degenerate repetition near-greedy sampling can fall into (the
35B pilot, 2026-11-03: "Rouge-Profond ? Non, …" to the token bound), or a
sentence of eight words or more said twice anywhere in the answer.
"""
from __future__ import annotations

import argparse
import json
import random
import re
import statistics
import sys
import time
import unicodedata
from collections import defaultdict
from collections.abc import Callable
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from tools.measure import sampling_probe

DEFAULT_QUERIES = Path(__file__).with_name("one_language_queries.tsv")
#: a citation by uid or canonical id in brackets ([pubmed:37000021]); Ask's own
#: answers cite [n] since 2026-11-03 and say what each cites (`cited`)
CITATION = re.compile(r"\[([A-Za-z][\w.-]*:[^\]\s,;]+)\]")


def returned_uids(value: Any) -> set[str]:
    """Every record uid a tool result carries (its hits' and facts' `uid`
    keys, at any depth): what an answer could have cited."""
    out: set[str] = set()
    if isinstance(value, dict):
        for k, v in value.items():
            if k == "uid" and isinstance(v, str):
                out.add(v)
            else:
                out |= returned_uids(v)
    elif isinstance(value, list):
        for v in value:
            out |= returned_uids(v)
    return out


#: a name: a capitalised word (any script with case) not opening a sentence;
#: a number: a run of digits
_NAME = re.compile(r"(?<![.!?:\n]\s)(?<!^)\b([A-ZÀ-ÖØ-ÞĀ-ſ][\w'’-]{2,})", re.UNICODE)
_NUMBER = re.compile(r"\b\d[\d.,]*\d\b|\b\d\b")


#: the checker's version, written on every row it scored (`rescore` rewrites
#: a run's rows under the current one): 2 = labels and markup out, German
#: names not counted, a name held in another spelling (operator, 2026-11-06);
#: 3 = a loop is also a sentence said twice (`repeated_sentence`), and the
#: answer's language is its sentences' majority by words, titles and quotes
#: out, with "mixed" beside it (`language_mix`; 2026-11-07)
CHECKER = 3

#: a list item's label — "Auteur:", "* **Personnages principaux**:" — a
#: heading the answer gives its item, not a fact it states; at most three words
_LABEL = re.compile(r"(?m)^[\s>*#-]*(?:\*\*)?(?:[^\W\d_][\w'’-]*\s?){1,3}(?:\*\*)?\s*:(?:\*\*)?")
#: markdown a line opens with (bullets, headings, bold): what follows opens the sentence
_MARKUP = re.compile(r"(?m)^[\s>*#-]+|\*\*|__")


def stated_facts(answer: str, lang: str | None = None) -> tuple[list[str], list[str]]:
    """(names, numbers) an answer states, citations taken out. Numbers hold
    in every language. A name is a capitalised word not opening a sentence,
    a list item's label taken out first — and none in German, which
    capitalises every noun: the walk of 2026-11-06 (München) found the
    measure's German "names" were Quelle ×18, Werk, Konzerte, Städten.

    THE STANDARD, NOT ADOPTED: a multilingual named-entity recogniser
    (spaCy's xx_ent_wiki_sm, or a common-word list such as wordfreq's)
    would find German names too; it is a new dependency (CLAUDE.md: ask
    first), and pays when German answers' names must be checked."""
    from app.ask.chat import CITE_N
    # both citation forms out: Ask's [n] would otherwise count as numbers stated
    text = CITE_N.sub(" ", CITATION.sub(" ", answer or ""))
    text = _MARKUP.sub("", _LABEL.sub("", text))
    names = [] if lang == "de" else sorted({m.group(1) for m in _NAME.finditer(text)})
    numbers = sorted({m.group(0).rstrip(".,") for m in _NUMBER.finditer(text)} - {""})
    return names, numbers


def fold(text: str) -> str:
    """Casefolded, accents off: "Calibán" is the sources' "Caliban"."""
    return "".join(c for c in unicodedata.normalize("NFKD", text.casefold())
                   if not unicodedata.combining(c))


_WORD = re.compile(r"\w+")


def held(name: str, hay: str, words: set[str] | None = None) -> bool:
    """Is a name in `hay` (folded), in its spelling or another of it: the
    answer's "Principe", "Nápoles" for the sources' "Prince", "Naples" — the
    same first letter, at most two letters apart, difflib's ratio 0.8 (the
    Ratcliff/Obershelp similarity). A NAME ONLY: a quoted span and a number
    are looked up as written."""
    f = fold(name)
    if f in hay:
        return True
    for w in words if words is not None else set(_WORD.findall(hay)):
        if w[:1] == f[:1] and abs(len(w) - len(f)) <= 2 and SequenceMatcher(None, f, w).ratio() >= 0.8:
            return True
    return False


def unsupported(answer: str, question: str, turns: list[dict[str, Any]],
                lang: str | None = None) -> dict[str, Any]:
    """The names and numbers stated, and those found neither in what the
    tools showed the model nor in the question: what it added or distorted."""
    from app.ask.chat import compact
    seen = " ".join(json.dumps(compact(t.get("tool", ""), t.get("result")), ensure_ascii=False,
                               default=str) for t in turns if t.get("result") is not None)
    hay = fold(seen + " " + (question or ""))
    words = set(_WORD.findall(hay))
    names, numbers = stated_facts(answer, lang)
    return {"names": len(names), "numbers": len(numbers),
            "added_names": [f for f in names if not held(f, hay, words)][:20],
            "added_numbers": [f for f in numbers if fold(f) not in hay][:20]}


#: a quoted span: what an answer gives as a title or as a source's words
_QUOTED = re.compile(r"[«\"“„]([^«»\"“”„\n]{3,160})[»\"”“]")
_SENTENCES = re.compile(r"(?<=[.!?])\s+|\n+")


_POSSESSIVE = re.compile(r"[’']s$")


def citation_support(got: dict[str, Any], turns: list[dict[str, Any]],
                     question: str = "", lang: str | None = None) -> dict[str, Any] | None:
    """Does each sentence say only what its sources say? ALCE's citation
    precision (Gao et al., EMNLP 2023), at the level of entities as
    `unsupported` (Nan et al.):
    - a sentence that CITES is supported when every quoted span it states
      is in a source it cites, and every name and number is there or in the
      question. A quoted span must be the source's own: the walk of
      2026-11-03 (München) had the question pasted as a title and put in
      source [1]'s mouth. A name may be the question's, in the reader's
      language (the walk's "Luigi XVI" for the sources' "Louis XVI");
    - a sentence that cites NOTHING is a claim when it states a quoted span,
      name or number found in no source shown and not in the question: the
      walk's invented title was in such a sentence, out of every check.
    A possessive ("Dumas's") is looked up as its name. None for an answer
    that does not say what its [n] are."""
    from app.ask.chat import CITE_ID, CITE_N, _trim
    sources = got.get("sources")
    if sources is None:
        return None
    seen: dict[str, str] = {}
    for t in turns:
        if t.get("tool") != "louise_search" or not isinstance(t.get("result"), dict):
            continue
        for lst in t["result"].get("lists", []):
            for h in lst.get("hits", []):
                if isinstance(h.get("uid"), str):
                    parts = [h.get("title"), h.get("authors"), _trim(h.get("text"))]
                    parts += [e.get("title") for e in h.get("editions") or []]
                    seen[h["uid"]] = seen.get(h["uid"], "") + " " + " ".join(str(x) for x in parts if x)
    by_n = {e.get("n"): e.get("uid") for e in sources}
    everything = fold(" ".join(seen.values()))
    every_word = set(_WORD.findall(everything))
    asked = fold(question or "")
    out: dict[str, Any] = {"sentences": 0, "supported": 0, "quotes": 0, "quotes_found": 0, "unsupported": [],
                           "uncited_claims": 0, "uncited": []}
    for sentence in _SENTENCES.split(got.get("answer") or ""):
        ns = [int(x) for m in CITE_N.finditer(sentence) for x in re.findall(r"\d+", m.group(1))]
        ids = CITE_ID.findall(sentence)
        said = re.sub(r"\s+([.,;:!?])", r"\1", " ".join(CITE_ID.sub(" ", CITE_N.sub(" ", sentence)).split()))
        names, numbers = stated_facts(said, lang)
        quotes = [q.strip() for q in _QUOTED.findall(said)]
        # a name inside a quoted span is judged with the span, not again alone
        inside = fold(" ".join(quotes))
        terms = [_POSSESSIVE.sub("", e) for e in names + numbers if fold(e) not in inside]
        named = {_POSSESSIVE.sub("", e) for e in names}

        def found(e: str, hay: str, words: set[str], named: set[str] = named) -> bool:
            return held(e, hay, words) if e in named else fold(e) in hay
        if not ns and not ids:
            missing = ([q for q in quotes if fold(q) not in everything and fold(q) not in asked]
                       + [e for e in terms if not found(e, everything, every_word) and fold(e) not in asked])
            if missing:
                out["uncited_claims"] += 1
                out["uncited"].append({"sentence": said[:300], "missing": missing[:8]})
            continue
        hay = " ".join(seen.get(by_n.get(n) or "", "") for n in ns)
        hay = fold(hay + " " + " ".join(seen.get(i, "") for i in ids))
        words = set(_WORD.findall(hay))
        missing = ([q for q in quotes if fold(q) not in hay]
                   + [e for e in terms if not found(e, hay, words) and fold(e) not in asked])
        out["sentences"] += 1
        out["supported"] += not missing
        out["quotes"] += len(quotes)
        out["quotes_found"] += sum(1 for q in quotes if fold(q) in hay)
        if missing:
            out["unsupported"].append({"sentence": said[:300], "missing": missing[:8]})
    out["unsupported"] = out["unsupported"][:10]
    out["uncited"] = out["uncited"][:10]
    return out


def support_summary(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    rows = [r for r in rows if isinstance(r.get("support"), dict) and not r.get("error")]
    if not rows:
        return None
    total = {k: sum(r["support"].get(k, 0) for r in rows)
             for k in ("sentences", "supported", "quotes", "quotes_found", "uncited_claims")}
    return {"n": len(rows), **total,
            "answers_misattributing": sum(1 for r in rows if r["support"]["unsupported"]),
            "answers_claiming_uncited": sum(1 for r in rows if r["support"].get("uncited"))}


def search_hits(turns: list[dict[str, Any]]) -> list[list[str]]:
    """Each louise_search call's hits in rank order, as uids: what the
    searches brought back from Louise (`searched_by` says whose each was)."""
    out = []
    for t in turns:
        if t.get("tool") != "louise_search" or not isinstance(t.get("result"), dict):
            continue
        uids: list[str] = []
        for lst in t["result"].get("lists", []):
            uids += [h["uid"] for h in lst.get("hits", []) if isinstance(h.get("uid"), str)]
        out.append(uids)
    return out


def searched_by(turns: list[dict[str, Any]]) -> list[str]:
    """Whose each louise_search was, in `search_hits`' order: "louise" (the
    question as typed), "louise:rewrite" (a search the model named before
    answering), "model" (one it called); "model" for rows from before
    Louise searched first (2026-11-03)."""
    return [t.get("by") or "model" for t in turns
            if t.get("tool") == "louise_search" and isinstance(t.get("result"), dict)]


def load_works(path: Path) -> dict[str, set[str]]:
    """work id -> the uids of its books (one_language_works.py's works.json)."""
    return {w["work"]: set(w["books"]) for w in json.loads(path.read_text(encoding="utf-8"))}


def gold_of(row: dict[str, Any], works: dict[str, set[str]]) -> set[str]:
    return works.get(str(row.get("qid", "")).split("#")[0], set())


def conveyed(rows: list[dict[str, Any]], works: dict[str, set[str]]) -> dict[str, Any]:
    """The operator's three for rows whose work is known: found by the model's
    searches, MRR of the work's best rank over them, the right work cited,
    found but left out of the answer."""
    rows = [r for r in rows if "hits" in r and gold_of(r, works)]
    n = len(rows)
    found = cited = left_out = typed = 0
    rr = 0.0
    for r in rows:
        gold = gold_of(r, works)
        by = r.get("hits_by") or ["model"] * len(r["hits"])
        typed += any(u in gold for hits, b in zip(r["hits"], by) if b == "louise" for u in hits)
        ranks = [i + 1 for hits in r["hits"] for i, u in enumerate(hits) if u in gold]
        hit = bool(ranks)
        found += hit
        rr += 1.0 / min(ranks) if ranks else 0.0
        right = bool(gold & set(r.get("cited_uids", [])))
        cited += right
        left_out += hit and not right
    return {"n": n, "found": found, "mrr": rr / n if n else 0.0, "cited_right": cited,
            "left_out": left_out, "found_typed": typed}


def faithfulness(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Over the rows that recorded it: the facts stated, the share found in
    the results or the question, and the answers stating at least one fact
    found in neither. None for rows asked before it existed."""
    rows = [r for r in rows if isinstance(r.get("facts"), dict) and not r.get("error")]
    if not rows:
        return None
    out: dict[str, Any] = {"n": len(rows)}
    for kind in ("numbers", "names"):
        stated = sum(r["facts"][kind] for r in rows)
        added = sum(len(r["facts"]["added_" + kind]) for r in rows)
        out[kind] = stated
        out[kind + "_found"] = (stated - added) / stated if stated else 1.0
        out[kind + "_adding"] = sum(1 for r in rows if r["facts"]["added_" + kind])
    return out


class SearchWithheld(RuntimeError):
    """A search that withheld a leg or its reranker: not the search a reader
    of a settled node gets. Never recorded, never a row: the run stops, and
    resumes at that question once the leg is back (CLAUDE.md, "Deferral is
    not failure")."""


def withheld_legs(result: Any) -> list[str]:
    """`collection (what): reason` for each leg or reranker a search
    withheld (05 §5.3's `legs_withheld`, `rerank_withheld`)."""
    out: list[str] = []
    if isinstance(result, dict):
        for k in ("legs_withheld", "rerank_withheld"):
            for w in result.get(k) or []:
                if isinstance(w, dict):
                    out.append(f"{w.get('collection') or w.get('collection_id')} "
                               f"({w.get('leg') or k}): {w.get('reason')}")
                else:
                    out.append(f"{k}: {w}")
    return out


def settle(collections: list[str] | None) -> dict[str, bool]:
    """THE VECTOR LEG, SETTLED IN THIS PROCESS before a run (2026-11-06: 314
    of the contest's 320 recorded searches were text only). The worker
    re-checks a run's kept statement only while the node's mode embeds
    (app/worker.py, the search settle: paused or seed-only, the encoder is
    not loaded for it), and every deploy restarts it; a search answered
    meanwhile says "Searched on its text only". The very check the worker
    makes, `run_check.embed_allowed` (sixteen texts re-embedded, a whole set
    for a run new to this peer), made here: the searches of this process are
    then answered with every leg. {collection: settled}."""
    from app import collections as collections_mod
    from app import db, run_check
    wanted = set(collections) if collections else None
    out: dict[str, bool] = {}
    for c in collections_mod.load_all():
        if (wanted is not None and c.name not in wanted) or not run_check.applies(c):
            continue
        con = db.connect(c)
        try:
            out[c.name] = bool(run_check.embed_allowed(con, c))
        finally:
            con.close()
    return out


class SearchCache:
    """THE QUESTION'S OWN SEARCH, ONCE (operator, 2026-11-03: "Do we need the
    reranker for this kind of test?"). Since #290 Ask searches the question
    as typed before any model speaks, with the same arguments for every
    model; a search is a pure function of its arguments and the data held
    (invariant 4 (5)). So it runs once, reranker included, as a reader gets
    it, and is replayed for every model (CLAUDE.md RULE 1: reuse what is
    provably identical). A search a model makes itself is replayed only if
    an earlier run made the very same one. Only louise_search is kept.

    The file's first line names the data it was read from (`data_version`:
    the snapshot of every collection searched); another snapshot refuses it.
    A change to the search code is not seen: record a new file after one.

    A SEARCH THAT WITHHELD A LEG IS NEVER KEPT (2026-11-06: 314 of 320 kept
    searches were text only, the node paused and its worker restarted by
    each deploy, so the whole contest measured the text leg): it raises
    `SearchWithheld`, and a file holding one is refused whole."""

    def __init__(self, path: Path, version: dict[str, Any],
                 live: Callable[[str, dict[str, Any]], Any] | None = None) -> None:
        self.path, self.version, self.hits, self.replayed_s = path, version, 0, 0.0
        self._live = live
        self._kept: dict[str, tuple[Any, float]] = {}
        if path.exists():
            lines = path.read_text(encoding="utf-8").splitlines()
            head = json.loads(lines[0]) if lines else {}
            if head.get("data") != version:
                raise SystemExit(f"{path}: recorded on {head.get('data')}, the node holds {version}: "
                                 f"record a new file")
            withheld = []
            for line in lines[1:]:
                row = json.loads(line)
                self._kept[row["key"]] = (row["result"], float(row.get("seconds") or 0.0))
                if withheld_legs(row["result"]):
                    withheld.append(json.loads(row["key"]).get("query"))
            if withheld:
                raise SystemExit(f"{path}: {len(withheld)} of {len(lines) - 1} recorded searches withheld a "
                                 f"leg (first: {withheld[0]!r}), and a run replaying them measures a search no "
                                 f"settled node gives: record a new file (each run settles the vector leg "
                                 f"first)")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"data": version}, sort_keys=True) + "\n", encoding="utf-8")

    def __call__(self, name: str, args: dict[str, Any]) -> Any:
        live = self._live
        if live is None:
            from app.ask import mcp
            live = mcp._call_tool
        if name != "louise_search":
            return live(name, args)
        key = json.dumps(args, sort_keys=True, ensure_ascii=False)
        if key in self._kept:
            result, seconds = self._kept[key]
            self.hits += 1
            self.replayed_s += seconds
            return json.loads(json.dumps(result))       # a copy: the caller may write into it
        t0 = time.monotonic()
        result = live(name, args)
        seconds = round(time.monotonic() - t0, 3)
        legs = withheld_legs(result)
        if legs:
            raise SearchWithheld(f"the search {args.get('query')!r} withheld {'; '.join(legs)}: not kept, "
                                 f"and this question is not answered; run again once the leg is back")
        kept = json.loads(json.dumps(result, ensure_ascii=False, default=str))
        self._kept[key] = (kept, seconds)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"key": key, "result": kept, "seconds": seconds}, ensure_ascii=False) + "\n")
        return result


def data_version(collections: list[str] | None) -> dict[str, Any]:
    """The snapshot each collection searched is read from (every one held
    when none is named): what a recorded search is valid for."""
    from app.ask import results
    from app.hold import collection as col
    from app.share import seeder
    root = results._root()
    names = ([col.resolve(root, c)[0] for c in collections] if collections
             else sorted(n for n, _ in col.list_named(root)))
    out: dict[str, Any] = {}
    for n in names:
        snap = seeder.cached_snapshot(col.shard_dir_of(root, n))
        out[n] = getattr(snap, "snapshot_id", None)
    return out


def ask_direct(collections: list[str] | None = None,
               call: Callable[[str, dict[str, Any]], Any] | None = None) -> Callable[..., dict[str, Any]]:
    """No model: the question searched as typed, as the Ask tab searches it
    (translation on), through the same tool the model calls — the reference
    row for "conveys"."""
    from app.ask import mcp

    def ask(question: str, collections: list[str] | None = collections) -> dict[str, Any]:
        # as the Ask tab searches it: translation on (web._ask_result)
        args: dict[str, Any] = {"query": question, "translate": True}
        if collections:
            args["collections"] = list(collections)
        result = (call or mcp._call_tool)("louise_search", args)
        return {"answer": "", "rounds": 0, "exhausted": False,
                "turns": [{"tool": "louise_search", "arguments": args, "by": "louise", "result": result,
                           "error": None, "seconds": 0.0}]}
    return ask


def cited_of(got: dict[str, Any]) -> tuple[int, int, list[str]]:
    """(cited, grounded, the uids cited): from the answer's own `cited` and
    `cited_unknown` where it says them (Ask's [n], 2026-11-03) — a number or
    id it names that no search showed is cited and not grounded — else from
    the bracketed ids in its text (`citations`)."""
    if "cited" in got:
        named = [c.get("uid") or c.get("id") for c in got["cited"]]
        unknown = got.get("cited_unknown") or []
        return len(named) + len(unknown), len(named), sorted({u for u in named if u})
    cited, grounded = citations(got.get("answer") or "", got.get("turns", []))
    return cited, grounded, sorted(set(CITATION.findall(got.get("answer") or "")))


def citations(answer: str, turns: list[dict[str, Any]]) -> tuple[int, int]:
    """(cited, grounded): the distinct ids the answer cites, and how many of
    them occur in what its tools returned."""
    cited = set(CITATION.findall(answer or ""))
    seen = " ".join(json.dumps(t.get("result"), ensure_ascii=False, default=str) for t in turns)
    return len(cited), sum(1 for c in cited if c in seen)
OPERATOR_LANGS = frozenset({"en", "it", "de", "fr", "es"})


def read_queries(path: Path, per_lang: int | None = None,
                 langs: frozenset[str] | None = None) -> list[dict[str, str]]:
    rows, seen = [], defaultdict(int)
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split("\t")
        if len(parts) < 3 or not parts[2].strip():
            continue
        qid, lang, text = parts[0], parts[1], parts[2].strip()
        if langs is not None and lang not in langs:
            continue
        if per_lang is not None and seen[lang] >= per_lang:
            continue
        seen[lang] += 1
        rows.append({"key": f"{qid}:{lang}", "qid": qid, "lang": lang, "query": text})
    return rows


#: penalties LocalAI cannot set to 0 from a request: 0 is its "unset", and the
#: model file's value would apply without a word
UNSETTABLE_ZERO = frozenset({"presence_penalty", "frequency_penalty", "repeat_penalty"})
_INT_SAMPLING = frozenset({"top_k", "seed"})


def parse_sampling(text: str) -> dict[str, float | int]:
    """`temperature=0.1,top_p=0.8` as the values an arm sends; a name the
    chat client does not send, a value that is not a number, or a penalty
    of 0 (UNSETTABLE_ZERO) is refused with the reason."""
    from app.llm import SAMPLING_KEYS
    out: dict[str, float | int] = {}
    for part in (p.strip() for p in text.split(",")):
        if not part:
            continue
        key, sep, value = part.partition("=")
        key = key.strip()
        if not sep or key not in SAMPLING_KEYS:
            raise SystemExit(f"--sampling: {part!r}: not one of {', '.join(sorted(SAMPLING_KEYS))}=<number>")
        try:
            number: float | int = int(value) if key in _INT_SAMPLING else float(value)
        except ValueError:
            raise SystemExit(f"--sampling: {key}={value!r} is not a number") from None
        if key in UNSETTABLE_ZERO and number == 0:
            raise SystemExit(f"--sampling: {key}=0 reads as unset in LocalAI (the model file's value "
                             f"would apply); put the neutral value in the model file instead")
        out[key] = number
    return out


def arm_model(sampling: dict[str, float | int] | None):
    """Ask's model for an arm: `llm.chat_tools` sending `sampling` with every
    round; None (the model file's own sampling) without one."""
    if not sampling:
        return None
    from app import llm
    return lambda messages, tools: llm.chat_tools(messages, tools, sampling=sampling)


#: the shortest repeated span, in words, and how many times back to back
LOOP_MIN_WORDS, LOOP_TIMES = 8, 3
#: the shortest sentence, in words, whose second saying is a loop: the same
#: bound as a span's. What a list's items share is shorter — a label
#: ("Autore: Alexandre Dumas", 3), a note ("Non è disponibile in italiano", 5)
REPEAT_MIN_WORDS = LOOP_MIN_WORDS
_LOOP_CITE = re.compile(r"\[[^\]]*\]")
#: a list item's or a heading's marker at a line's start: "- ", "* ", "1. ", "> ", "## "
_LIST_MARK = re.compile(r"(?m)^[ \t]*(?:[-*+>#]+|\d{1,3}[.)])[ \t]+")


def looped(answer: str, *, max_span: int = 80) -> bool:
    """The answer repeats itself, by either of two triggers:
    - `back_to_back`: a span repeated LOOP_TIMES times in a row, the
      degenerate loop of near-greedy decoding (Holtzman et al. 2020);
    - `repeated_sentence`: a sentence said twice anywhere in it, the same
      claim restated with other text between (Welleck et al. 2019's
      seq-rep, the sentence as its unit).
    Both feed the one "looped" column: the same defect, a model repeating
    what it has written; the pilot's line says which trigger fired."""
    return back_to_back(answer, max_span=max_span) or repeated_sentence(answer) is not None


def repeated_sentence(answer: str) -> str | None:
    """The first sentence the answer says a second time, or None.

    THE STANDARD: the repetition rate of Welleck et al. (ICLR 2020, "Neural
    Text Generation with Unlikelihood Training"), seq-rep-n = the share of a
    text's n-grams that are duplicates, taken with the SENTENCE as its unit
    and flagged above 0. The loop is sentence-level and feeds itself: once a
    sentence is repeated, repeating it again grows likelier (Xu et al.,
    NeurIPS 2022, "Learning to Break the Loop").

    WHY THE SENTENCE, NOT seq-rep-4: a list whose items share a frame
    ("Dumas wrote X in 1844 [3]. Dumas wrote Y in 1845 [4].") repeats
    4-grams by construction, and is a good answer. Its items differ in a
    word; a sentence repeated WHOLE does not happen by chance.

    THE THRESHOLDS: a sentence of REPEAT_MIN_WORDS (8) words or more — what
    a list's items legitimately share verbatim is a label or a short note,
    under 8 — and twice: one restatement is what `back_to_back`, needing
    three in a row, misses. Compared on its words: case, whitespace, punctuation,
    markdown, list markers and citations ([n], `_LOOP_CITE`) aside, so a
    restatement citing another source is still one."""
    seen: set[tuple[str, ...]] = set()
    for sentence in _SENTENCES.split(_LIST_MARK.sub("", answer or "")):
        words = tuple(_WORD.findall(_LOOP_CITE.sub(" ", sentence).casefold()))
        if len(words) < REPEAT_MIN_WORDS:
            continue
        if words in seen:
            return " ".join(sentence.split())
        seen.add(words)
    return None


def back_to_back(answer: str, *, max_span: int = 80) -> bool:
    """A span of words repeated back to back LOOP_TIMES times, at least
    LOOP_MIN_WORDS words in all, citations aside: the degenerate repetition
    of near-greedy decoding (Holtzman et al., ICLR 2020, "The Curious Case
    of Neural Text Degeneration"). A list whose items differ is not one."""
    words = _LOOP_CITE.sub(" ", answer or "").lower().split()
    for n in range(1, max_span + 1):
        if n * LOOP_TIMES < LOOP_MIN_WORDS:
            continue
        for i in range(len(words) - n * LOOP_TIMES + 1):
            if words[i + n] != words[i]:
                continue
            span = words[i:i + n]
            if all(words[i + k * n:i + (k + 1) * n] == span for k in range(1, LOOP_TIMES)):
                return True
    return False


#: AN ANSWER THAT THINKS ALOUD (the Q8 pilots, 2026-11-06): with thinking off,
#: Qwen3.5-9B at 0.4 and 0.7 wrote its reasoning into the answer — "titled *La
#: Dame de Monsoreau*? No, that's incorrect … *La Reine Margot*? No, again
#: incorrect" — so the reader saw invented titles, retracted. A retraction is
#: a question mark followed by a no, or the phrases that start one.
_SELF_TALK = re.compile(
    r"\?\s*(?:\*\*|_|\*)?\s*(?:No|Non|Nein|Nee|Não)\b[,.;]"
    r"|\b(?:No, (?:that|this|again|wait)|Actually, (?:it|the|that)|Wait, |Let me (?:check|re))",
    re.IGNORECASE)


def self_talk(answer: str) -> bool:
    """The answer retracts something it has just said (`_SELF_TALK`)."""
    return bool(_SELF_TALK.search(answer or ""))


#: MIXED: more than this share of the words in an answer's guessed sentences
#: is in languages other than its majority's — its Code-Mixing Index / 100
MIXED_SHARE = 0.25
#: what a sentence's language is guessed without: a span in quotes (a title,
#: a source's own words) and one in italics, stars or bold (a title, mostly);
#: an emphasis opens and closes on a letter, as CommonMark's, so a bullet's
#: "* " opens none
_QUOTE_SPAN = re.compile(r"[«\"“„][^«»\"“”„\n]{1,600}[»\"”“]")
_EMPHASIS = re.compile(r"\*\*(?=\S)[^*\n]+?(?<=\S)\*\*|__(?=\S)[^_\n]+?(?<=\S)__"
                       r"|\*(?=\S)[^*\n]+?(?<=\S)\*|(?<!\w)_(?=\S)[^_\n]+?(?<=\S)_(?!\w)")
#: a word as app/query_language counts one: letters only
_LETTER_WORD = re.compile(r"[^\W\d_]+")


def language_mix(text: str) -> dict[str, Any]:
    """The answer's language, sentence by sentence (2026-11-07: one guess
    for the whole answer took an Italian answer quoting English titles for
    English, and could not see an answer switching language halfway).

    - Citations, quoted spans and italic, starred or bold spans out first:
      a title or a source's words is not the language the answer is
      written in.
    - Each sentence guessed by the node's own guess (app/query_language:
      function words and scripts, no model); one it cannot tell (a name, a
      fragment) is no one's.
    - THE ANSWER'S LANGUAGE is the majority BY WORDS of the guessed
      sentences, as CLD2 (Google's Compact Language Detector 2) names a
      text's language by the largest share of it and reports each share.
      A tie goes to the guess over the whole (stripped) text if it is one
      of the tied, else no guess. No sentence guessed: the guess over the
      whole stripped text, which pools a short answer's evidence.
    - MIXED when the other languages hold more than MIXED_SHARE of the
      guessed words: the Code-Mixing Index (Das & Gambäck, ICON 2014;
      Gambäck & Das, LREC 2016), CMI / 100 = 1 - max_i(w_i) / (n - u), u
      the words no language claims, read per sentence.

    MIXED_SHARE = 0.25, one word in four: the guess was wrong on 4 of 490
    labelled queries (0.8 %), texts of a sentence's length
    (one-language-per-work.md §3), so the noise is a stray sentence — an
    unquoted foreign title — and one sentence in Qwen3.5-9B's answers (6 to
    8 citing sentences) is 12-17 % of the words; a switch halfway is 50 %.
    In an answer of three sentences one stray sentence crosses it.

    THE STANDARD, NOT ADOPTED: a detector with per-segment output (lingua's
    `detect_multiple_languages_of`, CLD2's ranked spans) finds the switch
    inside a sentence too. A new dependency in the node's image (CLAUDE.md:
    ask first), and the node's own guess is what the language column has
    always used; it pays if answers switch within sentences.

    {"lang": the majority or None, "words": {lang: words}, "other": share
    of the guessed words in other languages, "mixed": other > MIXED_SHARE}"""
    from app import query_language
    held = query_language.guessable()
    plain = _EMPHASIS.sub(" ", _QUOTE_SPAN.sub(" ", _LOOP_CITE.sub(" ", text or "")))
    words: dict[str, int] = {}
    for sentence in _SENTENCES.split(plain):
        lang = query_language.guess(sentence, held)
        if lang is not None:
            words[lang] = words.get(lang, 0) + len(_LETTER_WORD.findall(sentence))
    words = {k: v for k, v in words.items() if v}
    if not words:
        return {"lang": query_language.guess(plain, held), "words": {}, "other": 0.0, "mixed": False}
    most = max(words.values())
    tied = sorted(k for k, v in words.items() if v == most)
    if len(tied) == 1:
        lang = tied[0]
    else:
        pooled = query_language.guess(plain, held)
        lang = pooled if pooled in tied else None
    other = round(1.0 - most / sum(words.values()), 3)
    return {"lang": lang, "words": dict(sorted(words.items())), "other": other, "mixed": other > MIXED_SHARE}


def answer_language(text: str) -> str | None:
    """The language an answer is written in: `language_mix`'s majority."""
    return language_mix(text)["lang"]


def answer_checks(answer: str) -> dict[str, Any]:
    """What CHECKER reads off the answer's text alone, as a row records it:
    the loop, the language and whether it is mixed (`language_mix`)."""
    mix = language_mix(answer)
    return {"looped": looped(answer), "answer_lang": mix["lang"], "answer_mixed": mix["mixed"],
            "answer_words": mix["words"]}


def _warm_up(url: str, model: str) -> None:
    """One throwaway answer, so the first timed question finds the model loaded."""
    import httpx
    httpx.post(f"{url}/v1/chat/completions", timeout=900,
               json={"model": model, "messages": [{"role": "user", "content": "Say OK."}],
                     "max_tokens": 1})


def failure(e: BaseException) -> str:
    """A failed answer's row, as the server said it (2026-11-06, Command R7B's
    pilot: two rows read only "Server error '500 Internal Server Error' for url
    …", LocalAI logs no reason at INFO and the backend's ring had rotated past
    both — the diagnosis was lost). An HTTP error carries the server's body;
    the notes (PEP 678) say what failed before it, as Ask's tool fallback adds."""
    said = f"{type(e).__name__}: {e}"
    response = getattr(e, "response", None)
    if response is not None:
        try:
            said += f" | body: {response.text[:600]}"
        except Exception:   # noqa: BLE001 — an unreadable body is said as unreadable
            said += " | body: (unreadable)"
    for note in getattr(e, "__notes__", None) or []:
        said += f" | {str(note)[:400]}"
    return said[:1500]


def ask_code() -> str:
    """The code a run's answers come from: app/ask/'s files (Ask's prompt,
    its tool loop, how sources are shown), one digest. 2026-11-06: four
    pilots ran at 13:38 and their whole runs at 18:19 on another image."""
    import hashlib
    h = hashlib.sha256()
    for f in sorted((Path(__file__).resolve().parents[2] / "app" / "ask").glob("*.py")):
        h.update(f.name.encode() + b"\0" + f.read_bytes() + b"\0")
    return "sha256:" + h.hexdigest()


def run(out: Path, name: str, *, model: str, queries: list[dict[str, str]],
        collections: list[str] | None = None,
        ask: Callable[..., dict[str, Any]] | None = None,
        guess: Callable[[str], str | None] = answer_language,
        pid: Callable[[], int | None] | None = None, tag: str = "",
        redo: bool = False, search_cache: SearchCache | None = None,
        sampling: dict[str, float | int] | None = None) -> int:
    """WARM ANSWERS ONLY are timed (operator, 2026-11-03): the model is pinned
    for the run and warmed up first, and an answer during which its backend
    was (re)loaded — no backend before, or another pid after — is marked
    `warm: false`, counted, and left out of the time. `tag` labels the
    answers asked now (the configuration they ran on); with `redo` the
    questions already answered under ANOTHER tag are asked again under this
    one, so a configuration is measured on the same questions as the last."""
    run_dir = out / "runs" / name
    run_dir.mkdir(parents=True, exist_ok=True)
    settings: dict[str, Any] = {"model": model, "collections": collections or [], "code": ask_code()}
    if sampling:
        # the arm: what every request of this run sends over the model file
        settings["sampling"] = dict(sampling)
    stated = run_dir / "settings.json"
    if stated.exists():
        first = json.loads(stated.read_text(encoding="utf-8"))
        if "code" not in first:
            settings.pop("code")        # a run begun before the code was stated
        if first != settings:
            what = ("its pilot ran other code (app/ask/): a whole run resumes its pilot's answers, "
                    "so it runs its pilot's code; pilot again under a new --tag or --name"
                    if first.get("code") != settings.get("code") else "settings differ")
            raise SystemExit(f"{name}: {what} ({stated})")
    else:
        stated.write_text(json.dumps(settings, indent=1, sort_keys=True), encoding="utf-8")
    answers = run_dir / "answers.jsonl"
    done = ({(r["key"], r.get("tag", "")) for r in _rows(out, name)} if answers.exists() else set())
    done = {k for k, t in done if t == tag or not redo}
    release: Callable[[], None] = lambda: None
    if ask is None:
        import os

        from app import config, model_load, model_pins
        from app.ask import chat
        config.CHAT_MODEL = model          # this process asks the candidate only
        arm = arm_model(sampling)
        ask = lambda q, collections=None: chat.ask(q, collections=collections, call=search_cache,
                                                   model=arm)
        holder = f"chat_eval pid {os.getpid()}"
        model_pins.hold(config.CHAT_URL, model, holder)
        release = lambda: model_pins.release(holder)   # its prior pin state restored
        _warm_up(config.CHAT_URL, model)
        pid = pid or (lambda: model_load.backend_pid(config.CHAT_URL, model, 0.0))
    try:
        for q in queries:
            if q["key"] in done:
                continue
            before = pid() if pid else None
            hits0, replayed0 = ((search_cache.hits, search_cache.replayed_s) if search_cache else (0, 0.0))
            t0 = time.monotonic()
            error = None
            try:
                got = ask(q["query"], collections=collections)
            except SearchWithheld:
                raise                # deferral, not failure: no row, the run stops here
            except Exception as e:   # noqa: BLE001 — a failed answer is a row, not the end of the run
                got, error = {"answer": "", "turns": [], "rounds": 0, "exhausted": False}, failure(e)
            seconds = round(time.monotonic() - t0, 2)
            after = pid() if pid else None
            turns = got.get("turns", [])
            cited, grounded, cited_uids = cited_of(got)
            returned = len(returned_uids([t.get("result") for t in turns]))
            searched = search_hits(turns)
            facts = unsupported(got.get("answer") or "", q["query"], turns, q.get("lang"))
            support = citation_support(got, turns, q["query"], q.get("lang"))
            checks = answer_checks(got.get("answer") or "")
            row = {**q, "tag": tag, "answer": got.get("answer") or "", "rounds": got.get("rounds", 0),
                   "sampling": dict(sampling or {}),
                   "looped": checks["looped"],
                   "exhausted": bool(got.get("exhausted")),
                   "tools": [t.get("tool") for t in turns],
                   "tool_errors": sum(1 for t in turns if t.get("error")),
                   "seconds": seconds, "error": error,
                   "warm": pid is None or (before is not None and before == after),
                   "model_s": round(sum(got.get("model_s") or ()), 3),
                   "tools_s": round(sum(t.get("seconds") or 0.0 for t in turns), 3),
                   "search_s": search_seconds(turns),
                   "cited": cited, "grounded": grounded, "no_tool": not any((t.get("by") or "model") == "model" for t in turns),
                   "returned": returned, "slimmed": got.get("slimmed", 0),
                   "hits": searched, "hits_by": searched_by(turns),
                   "rewrites": got.get("rewrites") or [],
                   "rewrite_s": got.get("rewrite_s"),
                   "cited_uids": cited_uids,
                   "facts": facts, "support": support, "checker": CHECKER,
                   # the engine could not parse the model's tool call: answered without tools
                   "tools_failed": got.get("tools_failed"),
                   # what the model sent Louise: each call's tool and arguments, as asked
                   "calls": [{"tool": t.get("tool"), "arguments": t.get("arguments"),
                              "by": t.get("by") or "model", "dropped": t.get("dropped") or [],
                              "error": t.get("error")} for t in turns]}
            if search_cache is not None:
                # searches served from the recording, and the seconds they took when run:
                # `seconds` is then the answer's time without them
                row["replayed"] = search_cache.hits - hits0
                row["replayed_s"] = round(search_cache.replayed_s - replayed0, 3)
            row["answer_mixed"], row["answer_words"] = checks["answer_mixed"], checks["answer_words"]
            row["answer_lang"] = checks["answer_lang"] if guess is answer_language else guess(row["answer"])
            with answers.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    finally:
        release()
    print(f"{name}: {len(queries) - len(done)} asked now, {len(queries)} in all")
    return 0


#: the search's own timings (app/retrieval.py `timings`, milliseconds) an
#: answer's time is split by; "total" is the search's whole
SEARCH_PARTS = ("languages", "catalog", "fts", "dense", "rerank", "total")


def pilot_queries(queries: list[dict[str, str]], n: int) -> list[dict[str, str]]:
    """The first `n` questions of every language, in the file's order."""
    seen: dict[str, int] = defaultdict(int)
    out = []
    for q in queries:
        if seen[q["lang"]] < n:
            seen[q["lang"]] += 1
            out.append(q)
    return out


def _pilot_ok_path(out: Path, name: str, tag: str) -> Path:
    return out / "runs" / name / f"pilot-ok-{tag or 'untagged'}.json"


def pilot_report(out: Path, name: str, tag: str, keys: list[str]) -> Path:
    """The pilot's answers, one by one, for a reader: the question, the
    answer, what each citation is, the flags, the time and what the model
    itself called. Written beside the run."""
    rows = {r["key"]: r for r in _rows(out, name) if r.get("tag", "") == tag}
    lines = [f"# Pilot: {name} [{tag or 'untagged'}], {len(keys)} questions", "",
             "Read each one; then `chat_eval pilot-ok --out ... --name ... --tag ... --by ... --note ...`.", ""]
    for i, k in enumerate(keys, 1):
        r = rows.get(k)
        if r is None:
            continue
        lines += [f"## {i}. [{r['lang']}] {r['query']}", ""]
        if r.get("error"):
            lines += [f"**FAILED:** {r['error']}", ""]
        lines += [r.get("answer") or "(no answer)", ""]
        lines.append(f"- cited: {', '.join(r.get('cited_uids') or []) or 'nothing'}"
                     f" ({r.get('cited', 0)} citations, {r.get('grounded', 0)} grounded)")
        own = [c for c in r.get("calls") or [] if (c.get("by") or "model") == "model"]
        if own:
            lines.append("- the model called: " + "; ".join(
                f"{c.get('tool')} {json.dumps(c.get('arguments'), ensure_ascii=False)}"
                + (f" REFUSED {c.get('error')}" if c.get("error") else "") for c in own))
        if r.get("tools_failed"):
            lines.append(f"- TOOLS FAILED, answered without them: {r['tools_failed']}")
        if r.get("looped"):
            again = repeated_sentence(r.get("answer") or "")
            lines.append(f"- LOOPED: a sentence said twice: \"{again[:200]}\"" if again
                         else "- LOOPED: a span repeated back to back")
        if self_talk(r.get("answer") or ""):
            lines.append("- THINKS ALOUD: the answer retracts what it has just said")
        for u in (r.get("support") or {}).get("uncited", []):
            lines.append(f"- uncited, in no source: {', '.join(u['missing'])}")
        mixed = (f" (MIXED: {', '.join(f'{k} {v} words' for k, v in (r.get('answer_words') or {}).items())})"
                 if r.get("answer_mixed") else "")
        lines.append(f"- answer language guessed: {r.get('answer_lang')}{mixed}; {r.get('seconds')} s "
                     f"(model {r.get('model_s')} s); rounds {r.get('rounds')}"
                     + ("; ROUND BOUND HIT" if r.get("exhausted") else ""))
        lines.append("")
    path = out / "runs" / name / f"pilot-{tag or 'untagged'}.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def pilot_ok(out: Path, name: str, tag: str, by: str, note: str) -> int:
    """Records that the pilot was read: who read it and what they found."""
    if not (out / "runs" / name / f"pilot-{tag or 'untagged'}.md").exists():
        raise SystemExit(f"{name} [{tag}]: no pilot to have read (run --pilot 3 first)")
    if not by.strip() or not note.strip():
        raise SystemExit("pilot-ok needs --by (who read it) and --note (what they found)")
    path = _pilot_ok_path(out, name, tag)
    path.write_text(json.dumps({"by": by, "note": note,
                                "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}, ensure_ascii=False),
                    encoding="utf-8")
    print(f"{name} [{tag}]: pilot read by {by}; a whole run may start")
    return 0


def search_seconds(turns: list[dict[str, Any]]) -> dict[str, float]:
    """The searches' own timings summed over an answer's louise_search
    calls, in seconds: where inside the tool calls the time went."""
    out = dict.fromkeys(SEARCH_PARTS, 0.0)
    for t in turns:
        timings = (t.get("result") or {}).get("timings") if t.get("tool") == "louise_search" else None
        for part in SEARCH_PARTS:
            v = (timings or {}).get(part)
            if isinstance(v, (int, float)):
                out[part] += v / 1000.0
    return {k: round(v, 3) for k, v in out.items()}


def _rows(out: Path, name: str) -> list[dict[str, Any]]:
    path = out / "runs" / name / "answers.jsonl"
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l]


def summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(rows)
    if not n:
        return {"n": 0}
    calls = sum(len(r["tools"]) for r in rows)
    guessed = [r for r in rows if r["answer_lang"] is not None]
    warm = [r["seconds"] for r in rows if r.get("warm", True)]
    return {"n": n,
            "cold": n - len(warm),
            "failed": sum(1 for r in rows if r["error"]),
            "exhausted": sum(1 for r in rows if r["exhausted"]),
            # None for rows asked before `looped` was recorded: unknown, not 0
            "looped": (sum(1 for r in rows if r["looped"]) if all("looped" in r for r in rows) else None),
            # read off the answer itself, so rows of any age count
            "self_talk": sum(1 for r in rows if self_talk(r.get("answer") or "")),
            "calls_per_answer": calls / n,
            "tool_errors": sum(r["tool_errors"] for r in rows),
            "lang_right": sum(1 for r in guessed if r["answer_lang"] == r["lang"]),
            "lang_guessed": len(guessed),
            # more than MIXED_SHARE of the answer in another language; None for
            # rows scored before checker 3 recorded it: unknown, not 0
            "lang_mixed": (sum(1 for r in rows if r["answer_mixed"])
                           if all("answer_mixed" in r for r in rows) else None),
            "median_s": statistics.median(warm) if warm else None,
            "no_tool": sum(1 for r in rows if r.get("no_tool")),
            "cited": sum(r.get("cited", 0) for r in rows),
            "grounded": sum(r.get("grounded", 0) for r in rows),
            "uncited": sum(1 for r in rows if "cited" in r and not r["cited"]),
            # answers whose tool results had to be slimmed to fit the context (#282)
            "slimmed": (sum(1 for r in rows if r["slimmed"]) if all("slimmed" in r for r in rows)
                        else None),
            # the ones that matter: results came back and none was cited
            # (None for rows asked before `returned` was recorded: unknown, not 0)
            "uncited_with_results": (sum(1 for r in rows if not r.get("cited") and r["returned"] > 0)
                                     if all("returned" in r for r in rows) else None)}


def _labelled(out: Path) -> list[tuple[str, list[dict[str, Any]]]]:
    """(label, rows) per run and tag: a run's answers under each tag are
    their own rows, `name [tag]`; untagged ones are the run's name."""
    groups: list[tuple[str, list[dict[str, Any]]]] = []
    for name in sorted(p.name for p in (out / "runs").iterdir() if (p / "answers.jsonl").exists()):
        by_tag: dict[str, list[dict[str, Any]]] = {}
        for r in _rows(out, name):
            by_tag.setdefault(r.get("tag", ""), []).append(r)
        groups += [(f"{name} [{t}]" if t else name, rows) for t, rows in by_tag.items()]
    return groups


def _median(values: list[float]) -> str:
    return "-" if not values else format(statistics.median(values), ".2f")


def score(out: Path, works: Path | None = None) -> int:
    groups = _labelled(out)
    arms = {}
    for d in sorted((out / "runs").iterdir()):
        stated = d / "settings.json"
        if stated.exists():
            sampling = json.loads(stated.read_text(encoding="utf-8")).get("sampling")
            if sampling:
                arms[d.name] = sampling
    if arms:
        print("SAMPLING ARMS: what every request of the run sent over its model file\n")
        print("| run | sampling |")
        print("|---|---|")
        for name, sampling in arms.items():
            print(f"| {name} | " + ", ".join(f"{k}={v}" for k, v in sorted(sampling.items())) + " |")
        print()
    if works is not None:
        wk = load_works(works)
        print("THE OPERATOR'S THREE: conveys the query, summarises the results, cites them\n")
        print("| model | languages | answers with a known work | the question as typed found the work "
              "| every search found the work | MRR of the work over the searches | answer cites the work "
              "| found but left out of the answer |")
        print("|---|---|---|---|---|---|---|---|")
        for name, rows in groups:
            for label, sub in (("all", rows),
                               ("the operator's five", [r for r in rows if r["lang"] in OPERATOR_LANGS])):
                c = conveyed(sub, wk)
                if not c["n"]:
                    continue
                print(f"| {name} | {label} | {c['n']} | {c['found_typed'] / c['n']:.2f} | "
                      f"**{c['found'] / c['n']:.2f}** | {c['mrr']:.3f} | "
                      f"**{c['cited_right'] / c['n']:.2f}** | {c['left_out']} |")
        print()
    print("ADDS OR DISTORTS: the names and numbers an answer states, looked up in what its "
          "tools showed the model and in the question (entity-level factual consistency)\n")
    print("| model | answers | numbers stated | of them found | answers adding a number "
          "| names stated | of them found | answers adding a name |")
    print("|---|---|---|---|---|---|---|---|")
    for name, rows in groups:
        f = faithfulness(rows)
        if f is not None:
            print(f"| {name} | {f['n']} | {f['numbers']} | **{f['numbers_found']:.2f}** | "
                  f"**{f['numbers_adding']}** | {f['names']} | {f['names_found']:.2f} | "
                  f"{f['names_adding']} |")
    print()
    print("SAYS WHAT ITS SOURCES SAY: each sentence that cites, its names, numbers and quoted "
          "spans looked up in the sources it cites, the question left out (ALCE's citation "
          "precision, entity-level)\n")
    print("| model | answers | sentences citing | supported | quoted spans | found in the cited source "
          "| answers with a sentence its source does not support "
          "| answers stating, uncited, what no source says |")
    print("|---|---|---|---|---|---|---|---|")
    for name, rows in groups:
        c = support_summary(rows)
        if c is not None:
            print(f"| {name} | {c['n']} | {c['sentences']} | "
                  f"**{c['supported'] / c['sentences'] if c['sentences'] else 1.0:.2f}** | {c['quotes']} | "
                  f"{c['quotes_found'] / c['quotes'] if c['quotes'] else 1.0:.2f} | "
                  f"**{c['answers_misattributing']}** | **{c['answers_claiming_uncited']}** |")
    print()
    print("| model | languages | answers | failed | bound hit | looped | thinks aloud | calls/answer | tool errors "
          "| answer in the query's language (of those guessed, mixed) | cold (not timed) | median s, warm "
          "| no tool call | cited per answer | grounded | answers citing nothing "
          "| of which results had come back | results slimmed to fit |")
    print("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for name, rows in groups:
        parts = {"all": rows,
                  "the operator's five": [r for r in rows if r["lang"] in OPERATOR_LANGS],
                  "the others": [r for r in rows if r["lang"] not in OPERATOR_LANGS]}
        for label, sub in parts.items():
            s = summary(sub)
            if not s["n"]:
                continue
            share = s["lang_right"] / s["lang_guessed"] if s["lang_guessed"] else 0.0
            print(f"| {name} | {label} | {s['n']} | {s['failed']} | {s['exhausted']} | "
                  f"{'-' if s['looped'] is None else s['looped']} | {s['self_talk']} | "
                  f"{s['calls_per_answer']:.2f} | {s['tool_errors']} | "
                  f"**{share:.2f}** ({s['lang_right']}/{s['lang_guessed']}, mixed "
                  f"{'-' if s['lang_mixed'] is None else s['lang_mixed']}) | {s['cold']} | "
                  f"{'-' if s['median_s'] is None else format(s['median_s'], '.1f')} | "
                  f"{s['no_tool']} | {s['cited'] / s['n']:.1f} | "
                  f"{(s['grounded'] / s['cited']) if s['cited'] else 0.0:.2f} | {s['uncited']} | "
                  f"{'-' if s['uncited_with_results'] is None else s['uncited_with_results']} | "
                  f"{'-' if s['slimmed'] is None else s['slimmed']} |")
    print("\nWhere a warm answer's time goes (medians, seconds; the search parts are the "
          "node's own timings, summed over an answer's searches):\n")
    print("| model | warm answers | answer | model rounds | tool calls | search | rerank "
          "| language plan | dense | words |")
    print("|---|---|---|---|---|---|---|---|---|---|")
    for name, rows in groups:
        warm = [r for r in rows if r.get("warm", True) and not r["error"] and "model_s" in r]
        cells = [_median([r["seconds"] for r in warm]), _median([r["model_s"] for r in warm]),
                 _median([r["tools_s"] for r in warm])]
        cells += [_median([r["search_s"][p] for r in warm])
                  for p in ("total", "rerank", "languages", "dense", "fts")]
        print(f"| {name} | {len(warm)} | " + " | ".join(cells) + " |")
    return 0


#: what a refusal over the context says: "request (21673 tokens) exceeds the
#: available context size (8192 tokens)" (LocalAI, measured 2026-11-03)
_EXCEEDS = re.compile(r"request \((\d+) tokens\)")


def prompt_sizes(model: str, queries: list[dict[str, str]], *, collections: list[str] | None = None,
                 search_cache: SearchCache | None = None, post=None, ask=None) -> list[dict[str, Any]]:
    """The tokens of Ask's first request for each question, counted by the
    MODEL'S OWN template (2026-11-06: Command R7B serves 8,192 tokens where
    the Qwens serve 16,384; does Ask's request fit?). The body Ask sends,
    tools declared, with `max_tokens` 1: `usage.prompt_tokens` is read, or
    the count a refusal over the context names. Nothing is generated."""
    from app import config, llm
    from app.ask import chat
    from tools.measure import sampling_probe as sp
    post = post or sp.http_post
    ask = ask or chat.ask
    config.CHAT_MODEL = model
    out: list[dict[str, Any]] = []
    for q in queries:
        seen: dict[str, Any] = {}

        def counted(messages, tools, seen=seen):
            url, body = llm._request(messages, stream=False, temperature=None, max_tokens=1,
                                     num_ctx=16384)
            if tools:
                body["tools"] = tools
            status, got = post(url, body)
            if status == 200:
                seen["tokens"] = ((got or {}).get("usage") or {}).get("prompt_tokens")
            else:
                m = _EXCEEDS.search(str(got))
                seen.update(tokens=int(m.group(1)) if m else None, refused=status)
            # one round is the measure: an answer with no call ends the loop
            return "(measured)", [], {"role": "assistant", "content": "(measured)"}
        ask(q["query"], collections=collections, call=search_cache, model=counted)
        out.append({"key": q["key"], "lang": q["lang"], **seen})
    return out


def sizes_report(rows: list[dict[str, Any]], *, window: int, answer: int = 1500) -> str:
    """How many requests fit `window` with room for an answer of `answer`."""
    got = sorted(r["tokens"] for r in rows if isinstance(r.get("tokens"), int))
    if not got:
        return "no request was counted"
    def at(p):
        return got[min(len(got) - 1, int(p * len(got)))]
    fit = sum(1 for t in got if t + answer <= window)
    return (f"{len(got)} requests counted of {len(rows)}: median {at(0.5)}, p90 {at(0.9)}, max {got[-1]} "
            f"tokens; {fit} of {len(got)} fit {window} with {answer} for the answer; "
            f"{sum(1 for t in got if t > window)} exceed {window} outright")


def sample(out: Path, runs: list[str], n: int = 30, seed: int = 0) -> int:
    """The same questions, each model's answer under a blind letter; the key
    apart, so the reading is done before the names are known."""
    by_run = {r: {row["key"]: row for row in _rows(out, r)} for r in runs}
    common = sorted(set.intersection(*(set(v) for v in by_run.values())))
    rng = random.Random(seed)
    keys = rng.sample(common, min(n, len(common)))
    lines, key_lines = ["# Blind sample: the same question, one answer per model\n"], []
    for i, k in enumerate(keys, 1):
        q = by_run[runs[0]][k]
        order = runs[:]
        rng.shuffle(order)
        lines.append(f"## {i}. [{q['lang']}] {q['query']}\n")
        for letter, r in zip("ABCDEFG", order):
            row = by_run[r][k]
            lines.append(f"**{letter}**\n\n{row['answer'].strip() or '(no answer)'}\n")
            facts = row.get("facts") if isinstance(row.get("facts"), dict) else {}
            flagged = facts.get("added_numbers", []) + facts.get("added_names", [])
            if flagged:
                # what the reader checks: stated, and found neither in the results nor the question
                lines.append(f"*not in the results:* {', '.join(flagged)}\n")
            for u in (row.get("support") or {}).get("unsupported", []):
                # a sentence and what it states that the source it cites does not
                lines.append(f"*not in its cited source:* {', '.join(u['missing'])} ← \"{u['sentence']}\"\n")
            for u in (row.get("support") or {}).get("uncited", []):
                lines.append(f"*uncited, in no source:* {', '.join(u['missing'])} ← \"{u['sentence']}\"\n")
        key_lines.append(f"{i}: " + ", ".join(f"{l}={r}" for l, r in zip("ABCDEFG", order)))
    (out / "sample.md").write_text("\n".join(lines), encoding="utf-8")
    (out / "sample-key.txt").write_text("\n".join(key_lines) + "\n", encoding="utf-8")
    print(f"{len(keys)} questions in {out / 'sample.md'}; the key in {out / 'sample-key.txt'}")
    return 0


def rescore(out: Path, name: str, cache: Path) -> int:
    """A run's rows scored again by the current checker (CHECKER), without
    asking the model again: each row's searches replayed from the recording
    they were served from (SearchCache's file), numbered as Ask numbered
    them (`Sources`, `shown_search`), and its answer checked anew. A row is
    rewritten only when the rebuilt sources give back what the row recorded
    that the change cannot touch — its citing sentences, quoted spans and
    the spans found; otherwise it is kept as it was and counted. The rows as
    they were are kept beside, once per checker: answers.checker-<n>.jsonl,
    n = CHECKER - 1, the rows before this checker first rewrote any (a second
    pass, its cache grown, keeps that file; the rows it rewrites are in it)."""
    from app.ask.chat import Sources, shown_search
    kept: dict[str, Any] = {}
    for line in cache.read_text(encoding="utf-8").splitlines()[1:]:
        row = json.loads(line)
        kept[row["key"]] = row["result"]
    path = out / "runs" / name / "answers.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    done = kept_as_was = 0
    new: list[dict[str, Any]] = []
    for r in rows:
        if r.get("error") or r.get("checker") == CHECKER or not isinstance(r.get("support"), dict):
            new.append(r)
            continue
        sources, turns, whole = Sources(), [], True
        for c in r.get("calls") or []:
            if c.get("tool") != "louise_search" or c.get("error"):
                continue
            args = c.get("arguments") or {}
            key = json.dumps(args, sort_keys=True, ensure_ascii=False)
            if key not in kept:
                whole = False
                break
            shown_search(str(args.get("query", "")), kept[key], sources,
                         translated=bool(args.get("translate", True)))
            turns.append({"tool": "louise_search", "result": kept[key]})
        support = citation_support({"answer": r["answer"], "sources": sources.entries}, turns,
                                   r.get("query", ""), r.get("lang")) if whole else None
        same = support is not None and all(support[k] == r["support"].get(k)
                                           for k in ("sentences", "quotes", "quotes_found"))
        if not same:
            kept_as_was += 1
            new.append(r)
            continue
        new.append({**r, "facts": unsupported(r["answer"], r.get("query", ""), turns, r.get("lang")),
                    "support": support, **answer_checks(r["answer"]), "checker": CHECKER})
        done += 1
    if done:
        before = path.with_name(f"answers.checker-{CHECKER - 1}.jsonl")
        if not before.exists():
            before.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in new), encoding="utf-8")
    print(f"{name}: {done} rows rescored (checker {CHECKER}), {kept_as_was} kept as they were "
          f"(their sources could not be rebuilt)")
    return 0 if not kept_as_was else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--queries", type=Path, default=DEFAULT_QUERIES)
    p.add_argument("--per-lang", type=int, default=None)
    p.add_argument("--langs", default="", help="only these languages, e.g. en,it,de,fr,es")
    p.add_argument("--collections", default="")
    p.add_argument("--tag", default="", help="the configuration these answers run on "
                   "(e.g. opt0, opt-default+graph); score splits by it")
    p.add_argument("--redo", action="store_true", help="ask again, under --tag, the "
                   "questions answered under another tag")
    p.add_argument("--direct", action="store_true", help="no model: the question searched "
                   "as typed (the reference row for how well a model conveys it)")
    p.add_argument("--search-cache", type=Path, default=None, help="searches recorded once and "
                   "replayed for every model (SearchCache); run --direct with it first")
    p.add_argument("--pilot", type=int, default=None, help="only the first N questions of every "
                   "language, written for a reader (measuring.md §12); a whole run needs one read")
    p.add_argument("--sampling", default="", help="the arm: values every request sends over the "
                   "model file, e.g. temperature=0.1,presence_penalty=1.5 (a penalty of 0 is refused: "
                   "LocalAI reads it as unset)")
    p = sub.add_parser("probe", help="does LocalAI apply the sampling sent? (sampling_probe.py)")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--installed", type=Path, default=None, help="the model's file as LocalAI "
                   "installed it, kept with the result as evidence")
    p = sub.add_parser("pilot-ok")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--tag", default="")
    p.add_argument("--by", required=True, help="who read the pilot")
    p.add_argument("--note", required=True, help="what they found")
    p = sub.add_parser("prompt-sizes", help="tokens of Ask's first request, by the model's template")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--queries", type=Path, default=DEFAULT_QUERIES)
    p.add_argument("--per-lang", type=int, default=None)
    p.add_argument("--langs", default="")
    p.add_argument("--collections", default="")
    p.add_argument("--search-cache", type=Path, required=True)
    p.add_argument("--window", type=int, default=8192)
    p = sub.add_parser("wire", help="is the model given its template's prompt, and Ask its raw "
                       "output? (wire_check.py)")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--gguf", type=Path, required=True, help="the model file's weights, for the "
                   "templates they carry (GGUF_PY: llama.cpp's gguf-py)")
    p.add_argument("--queries", type=Path, default=DEFAULT_QUERIES)
    p.add_argument("--keys", required=True, help="the recorded questions, e.g. WQ506865:fr,WQ6950087:fr")
    p.add_argument("--collections", default="")
    p.add_argument("--search-cache", type=Path, required=True)
    p.add_argument("--max-tokens", type=int, default=1500)
    p.add_argument("--append", default="", help="text added to the reference prompt (what the "
                   "engine may add, e.g. a closed thinking block)")
    p.add_argument("--sampling", default="", help="values BOTH paths send besides temperature 0, "
                   "e.g. top_k=1 or presence_penalty=1e-6; written as <model>.wire.<arm>.json, "
                   "which gates nothing")
    p = sub.add_parser("score")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--works", type=Path, default=None, help="one_language_works.py's works.json: "
                   "scores the operator's three (conveys, summarises, cites)")
    p = sub.add_parser("rescore", help="a run's rows scored again by the current checker, "
                       "its searches replayed from their recording")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--runs", required=True, help="run names, comma-separated")
    p.add_argument("--search-cache", type=Path, required=True)
    p = sub.add_parser("sample")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--runs", required=True)
    p.add_argument("--n", type=int, default=30)
    a = ap.parse_args(argv)
    if a.cmd == "run":
        if a.sampling and a.direct:
            raise SystemExit("--sampling with --direct: no model answers a direct run")
        if not a.direct:
            # operator, 2026-11-06: "Be absolutely sure that localai is applying
            # your settings otherwise the test is void"
            sampling_probe.require(a.out, a.model)
            # operator, 2026-11-06: "Are you sure that the model input/output
            # are correctly wired?"
            from tools.measure import wire_check
            wire_check.require(a.out, a.model)
        cols = [c for c in a.collections.split(",") if c] or None
        langs = frozenset(l for l in a.langs.split(",") if l) or None
        queries = read_queries(a.queries, a.per_lang, langs)
        if a.pilot:
            queries = pilot_queries(queries, a.pilot)
        elif not _pilot_ok_path(a.out, a.name, a.tag).exists():
            raise SystemExit(f"{a.name} [{a.tag}]: no pilot read yet (measuring.md §12): "
                             f"run --pilot 3, read runs/{a.name}/pilot-{a.tag or 'untagged'}.md, "
                             f"then `chat_eval pilot-ok`")
        cache = SearchCache(a.search_cache, data_version(cols)) if a.search_cache else None
        for c, ok in settle(cols).items():
            print(f"settle: {c}: {'the vector leg is settled' if ok else 'NOT settled (its run-check log says why)'}")
        try:
            code = run(a.out, a.name, model=a.model, queries=queries,
                       collections=cols, tag=a.tag, redo=a.redo, search_cache=cache,
                       sampling=parse_sampling(a.sampling) or None,
                       **({"ask": ask_direct(cols, call=cache), "pid": lambda: 0} if a.direct else {}))
        except SearchWithheld as e:
            print(f"{a.name}: stopped: {e}", file=sys.stderr)
            return 3
        if a.pilot:
            print(f"pilot: {pilot_report(a.out, a.name, a.tag, [q['key'] for q in queries])}")
        return code
    if a.cmd == "prompt-sizes":
        cols = [c for c in a.collections.split(",") if c] or None
        langs = frozenset(l for l in a.langs.split(",") if l) or None
        rows = prompt_sizes(a.model, read_queries(a.queries, a.per_lang, langs), collections=cols,
                            search_cache=SearchCache(a.search_cache, data_version(cols)))
        where = a.out / "prompt-sizes" / f"{a.model}.jsonl"
        where.parent.mkdir(parents=True, exist_ok=True)
        where.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        print(sizes_report(rows, window=a.window) + f"\n{where}")
        return 0
    if a.cmd == "wire":
        from app import config
        from tools.measure import wire_check
        cols = [c for c in a.collections.split(",") if c] or None
        wanted = [k for k in a.keys.split(",") if k]
        by_key = {q["key"]: q for q in read_queries(a.queries)}
        missing = [k for k in wanted if k not in by_key]
        if missing:
            raise SystemExit(f"wire: no such question: {', '.join(missing)}")
        extra = parse_sampling(a.sampling)
        templates = wire_check.read_gguf(a.gguf)
        cache = SearchCache(a.search_cache, data_version(cols))
        for c, ok in settle(cols).items():
            print(f"settle: {c}: {'the vector leg is settled' if ok else 'NOT settled (its run-check log says why)'}")
        try:
            results = [wire_check.check(a.model, by_key[k], templates, collections=cols, search_cache=cache,
                                        post=sampling_probe.http_post, chat_url=config.CHAT_URL,
                                        max_tokens=a.max_tokens, append=a.append,
                                        sampling=extra) for k in wanted]
        except SearchWithheld as e:
            print(f"wire {a.model}: stopped: {e}", file=sys.stderr)
            return 3
        arm = a.sampling.replace("=", "").replace(",", "-")
        where = wire_check.write(a.out, results, arm)
        print(wire_check.table(results) + f"\n{where}")
        return 0 if all(r["passed"] for r in results) else 1
    if a.cmd == "rescore":
        codes = [rescore(a.out, n, a.search_cache) for n in a.runs.split(",") if n]
        return max(codes, default=0)
    if a.cmd == "probe":
        result = sampling_probe.probe(a.model)
        where = sampling_probe.write(a.out, result, a.installed, sampling_probe.localai_version())
        print(sampling_probe.table(result) + f"\n\n{where}")
        return 0 if result["passed"] else 1
    if a.cmd == "pilot-ok":
        return pilot_ok(a.out, a.name, a.tag, a.by, a.note)
    if a.cmd == "score":
        return score(a.out, a.works)
    return sample(a.out, [r for r in a.runs.split(",") if r], a.n)


if __name__ == "__main__":
    sys.exit(main())
