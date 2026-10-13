"""tools/legal_pilot.py — the pilot's measurement over a BUILT legal
collection (gazzetta-plugin.md §7.2; normattiva-multivigenza-review.md §10;
docs/notes/legal-collection-template.md): what the platform answers on
JuriFindIT's expert questions and on dated questions, scored
DETERMINISTICALLY — a gold article is held or not, a version is the one in
force at the date or not — never by an LLM judge (R2's method).

    python -m tools.legal_pilot fetch         --out DIR
    python -m tools.legal_pilot coverage      --out DIR --collection normattiva
    python -m tools.legal_pilot run           --out DIR --collection normattiva [--k 20]
    python -m tools.legal_pilot temporal-make --collection normattiva --out FILE.jsonl [--limit 200]
    python -m tools.legal_pilot temporal-run  --collection normattiva --questions FILE.jsonl --out DIR

fetch          the dataset, through encoder_bakeoff.fetch — CC-BY-NC-SA-4.0, a
               gated class: record the acknowledgement before any use
               (invariant 9).
coverage       which gold articles the collection holds, BY KEY. The dataset's
               corpus node carries the act as `file_name`
               (`§akn§it§act§rd§STATO_ITALIANO§1942-03-16§0262§ITA`: type,
               date, number) and the article as `path` (`…__article_1167`);
               the act resolves through the collection's own `validity_works`
               keys — the URN:NIR every version carries — and the article
               through the shown version's chunks, whose `chapter` is the
               article's label. No text matching anywhere. A question none of
               whose gold articles is held is EXCLUDED from `run` and listed:
               EU regulations are, Normattiva does not hold them.
run            every covered question through `app.ask.results.search` as the
               node is configured (its reranker included), recall@k, mrr@k and
               ndcg@k over the gold node ids (encoder_bakeoff.metrics_for, the
               hand-pinned definitions), and the REACH analysis: of the gold
               articles the top-k missed, how many sit in an act one declared
               relation away from a top-k hit's act (`app.ask.traverse`, either
               direction). That number is the measured upper bound of what
               widening the candidate set along the edges could add — the
               question the review left open, answered before anything is
               built for it.
temporal-make  dated question STUBS mined from the version table: an article
               whose text differs between two consecutive versions of one act,
               a date inside the earlier version's validity, and a numeric
               nugget present in the earlier text and absent from the later.
               They are stubs: a person reads them, keeps the ones that ask a
               real question, and edits the wording (R2 screened its 209 by
               experts; the screening is the measurement's honesty).
temporal-run   each dated question through search(as_of=date): did the version
               in force at the date come back for that article (BY UID — the
               version is a key lookup, invariant 4), does its text carry the
               nugget; and the control, the same question undated: does the
               current version come back and the nugget vanish. The second is
               R2's 0 % provenance of a current-only index, measured here.

Everything reads the node's own stores read-only; nothing writes to a
collection. Runs land under --out/runs as JSON with the collection id and
snapshot named, so two runs are comparable.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import encoder_bakeoff as bakeoff  # noqa: E402 — fetch, metrics_for, LICENCE

KS = (1, 3, 5, 10, 20)
DEFAULT_K = 20

#: an article's number, in the dataset's node path (`article_1167`), in a
#: chunk's label ("Art. 1167.", "CODICE CIVILE-art. 906", "Articolo 11") or in
#: a question; the Latin suffix kept, normalised: `1167`, `2043-bis`
_ARTICLE = re.compile(r"art(?:icle|icolo)?\.?\s*_?\s*(\d+)\s*[-_ ]?\s*"
                      r"(bis|ter|quater|quinquies|sexies|septies|octies|novies|decies)?\b", re.I)
#: any authority, not `stato` alone: D.M. 7 novembre 2001 n. 458 is held as
#: `urn:nir:ministero.giustizia:decreto:2001-11-07;458` and the coverage run
#: of 2026-10-13 counted it "act not held" (review §10.1)
_URN = re.compile(r"^urn:nir:([^:]+):([^:]+):(\d{4}-\d{2}-\d{2});(\d+)")
_NUMBER = re.compile(r"\d[\d.,]*\d|\d")
#: JuriFindIT's act-type abbreviations → the URN:NIR type token, consulted
#: only where one (date, number) names several acts
TYPE_OF: dict[str, tuple[str, ...]] = {
    "rd": ("regio.decreto",), "rdl": ("regio.decreto.legge",), "l": ("legge",),
    "lcost": ("legge.costituzionale",), "dlgs": ("decreto.legislativo",), "dl": ("decreto.legge",),
    "dpr": ("decreto.del.presidente.della.repubblica",),
    "dpcm": ("decreto.del.presidente.del.consiglio.dei.ministri",),
    # a ministerial decree is `decreto` under its ministry's authority
    "dm": ("decreto.ministeriale", "decreto"),
    "dlt": ("decreto.legislativo.luogotenenziale",)}


def article_token(text: str) -> str | None:
    """`1167`, `5-bis` — from a node path, a chunk label or a sentence; None
    where no article number is named."""
    m = _ARTICLE.search(text or "")
    if not m:
        return None
    num, suffix = m.group(1), m.group(2)
    return f"{int(num)}-{suffix.lower()}" if suffix else str(int(num))


def act_of(file_name: str) -> tuple[str, str, int] | None:
    """`(type abbreviation, date, number)` of an Italian act from the
    dataset's `file_name`; None for anything else (EU acts, placeholders)."""
    parts = [p for p in (file_name or "").split("§") if p]
    if len(parts) < 7 or parts[0] != "akn" or parts[1] != "it":
        return None
    digits = re.sub(r"\D", "", parts[6])
    if not digits:
        return None
    return parts[3].lower(), parts[5], int(digits)


# ---- the collection, by key ------------------------------------------------------

class Held:
    """A built collection's acts and articles, read by key from its own
    stores: `validity_works` (every version's canonical ids), `validity` (the
    chain, the shown version), the records themselves for the article labels
    — cut through app.layers exactly as the stages cut them."""

    def __init__(self, collection, con) -> None:
        from app import collections as collections_mod
        self.collection, self.con = collection, con
        self.plugin = collections_mod.PLUGIN_REGISTRY[collection.plugin]
        self._acts: dict[tuple[str, int], list[tuple[str, str]]] | None = None
        self._records: dict[str, Any] = {}
        self._articles: dict[str, tuple[dict[str, list[int]], dict[int, str]]] = {}

    def acts(self) -> dict[tuple[str, int], list[tuple[str, str]]]:
        """`(date, number) → [(URN type token, work)]` from the URN keys."""
        if self._acts is None:
            out: dict[tuple[str, int], list[tuple[str, str]]] = {}
            for r in self.con.execute("SELECT key, work FROM validity_works WHERE key LIKE 'urn:nir:%'"):
                m = _URN.match(r["key"])
                if not m:
                    continue
                entry = (m.group(2), r["work"])
                lst = out.setdefault((m.group(3), int(m.group(4))), [])
                if entry not in lst:
                    lst.append(entry)
            self._acts = out
        return self._acts

    def work_for(self, act: tuple[str, str, int]) -> str | None:
        abbrev, day, number = act
        found = self.acts().get((day, number)) or []
        if len(found) == 1:
            return found[0][1]
        wanted = TYPE_OF.get(abbrev) or ()
        exact = [w for t, w in found if t in wanted]
        return exact[0] if len(exact) == 1 else None

    def keys_of(self, work: str) -> set[str]:
        return {r[0] for r in self.con.execute("SELECT key FROM validity_works WHERE work=?", (work,))}

    def shown(self, work: str) -> tuple[str, int] | None:
        r = self.con.execute("SELECT uid, native_id FROM validity WHERE work=? AND shown=1", (work,)).fetchone()
        return (r["uid"], int(r["native_id"])) if r else None

    def work_of(self, uid: str) -> str | None:
        r = self.con.execute("SELECT work FROM validity WHERE uid=?", (uid,)).fetchone()
        return r["work"] if r else None

    def version(self, uid: str) -> dict[str, Any] | None:
        r = self.con.execute("SELECT uid, work, valid_from, valid_to, shown FROM validity WHERE uid=?",
                             (uid,)).fetchone()
        return dict(r) if r else None

    def _row_and_doc(self, uid: str):
        if uid not in self._records:
            from app import plugin_io
            row = self.con.execute("SELECT * FROM documents WHERE uid=?", (uid,)).fetchone()
            self._records[uid] = (row, plugin_io.canonical_doc(self.plugin, row)) if row else (None, None)
        return self._records[uid]

    def record(self, uid: str):
        return self._row_and_doc(uid)[1]

    def articles(self, uid: str) -> tuple[dict[str, list[int]], dict[int, str]]:
        """`({article token: [chunk seq]}, {chunk seq: label})` of one version,
        cut as the stages cut it (app.layers, never a chunker directly)."""
        if uid not in self._articles:
            from app import layers
            by_token: dict[str, list[int]] = {}
            by_seq: dict[int, str] = {}
            row, doc = self._row_and_doc(uid)
            if doc is not None:
                for ch in layers.chunks_for_document(self.collection, row, doc.sections):
                    seq, label = int(ch["seq"]), str(ch.get("chapter") or "")
                    by_seq[seq] = label
                    tok = article_token(label)
                    if tok:
                        by_token.setdefault(tok, []).append(seq)
            self._articles[uid] = (by_token, by_seq)
        return self._articles[uid]

    def text_of(self, uid: str, token: str) -> str:
        doc = self.record(uid)
        if doc is None:
            return ""
        return "\n".join(body for label, body in (doc.sections or []) if article_token(label) == token)


# ---- gold, by key ------------------------------------------------------------------

@dataclass
class Gold:
    node_id: int
    file_name: str
    path: str
    act: tuple[str, str, int] | None
    token: str | None
    work: str | None = None
    uid: str | None = None
    seqs: list[int] = field(default_factory=list)

    @property
    def held(self) -> bool:
        return bool(self.uid and self.seqs)

    @property
    def why(self) -> str:
        if self.act is None:
            return "not an Italian act"
        if self.token is None:
            return "no article in the node path"
        if self.work is None:
            return "act not held"
        if not self.seqs:
            return "article not among the shown version's chunks"
        return "held"


def gold_dict(g: Gold) -> dict[str, Any]:
    """The gold as a run file carries it: the fields, and the two verdicts."""
    return {**asdict(g), "held": g.held, "why": g.why}


def gold_from(d: dict[str, Any]) -> Gold:
    fields_ = {k: v for k, v in d.items() if k not in ("held", "why")}
    if fields_.get("act"):
        fields_["act"] = tuple(fields_["act"])
    return Gold(**fields_)


def resolve_gold(held: Held, corpus_rows: Iterable[dict[str, Any]], wanted: set[int]) -> dict[int, Gold]:
    out: dict[int, Gold] = {}
    for r in corpus_rows:
        nid = int(r["id"])
        if nid not in wanted:
            continue
        path = str(r.get("path") or "")
        last = [p for p in path.split("__") if p.startswith("article")]
        g = Gold(node_id=nid, file_name=str(r.get("file_name") or ""), path=path,
                 act=act_of(str(r.get("file_name") or "")), token=article_token(last[-1]) if last else None)
        if g.act and g.token:
            g.work = held.work_for(g.act)
            if g.work:
                shown = held.shown(g.work)
                if shown:
                    g.uid = shown[0]
                    g.seqs = list(held.articles(g.uid)[0].get(g.token, []))
        out[nid] = g
    return out


def coverage(held: Held, corpus_rows: Iterable[dict[str, Any]],
             questions: list[dict[str, Any]]) -> dict[str, Any]:
    wanted = {int(i) for q in questions for i in q["relevant_doc_ids"]}
    gold = resolve_gold(held, corpus_rows, wanted)
    covered, excluded = [], []
    for i, q in enumerate(questions):
        ids = [int(x) for x in q["relevant_doc_ids"]]
        held_ids = [x for x in ids if x in gold and gold[x].held]
        if held_ids:
            covered.append({"index": i, "gold": ids, "held": held_ids})
        else:
            excluded.append({"index": i, "question": q["question"],
                             "why": sorted({gold[x].why if x in gold else "node not in corpus" for x in ids})})
    why: dict[str, int] = {}
    for g in gold.values():
        why[g.why] = why.get(g.why, 0) + 1
    return {"questions": len(questions), "covered": len(covered), "excluded": len(excluded),
            "articles": len(gold), "articles_held": sum(1 for g in gold.values() if g.held),
            "articles_by_outcome": why, "covered_questions": covered, "excluded_questions": excluded,
            "gold": {str(k): gold_dict(v) for k, v in gold.items()}}


# ---- the run -----------------------------------------------------------------------

def _hits(answer: dict[str, Any]) -> list[dict[str, Any]]:
    hits: list[dict[str, Any]] = []
    for lst in answer.get("lists") or []:
        hits.extend(lst.get("hits") or [])
    hits.sort(key=lambda h: (h.get("rank") is None, h.get("rank", 0)))
    return hits


def _seq_of(chunk_id: str | None) -> int | None:
    try:
        return int(str(chunk_id).rsplit(":", 2)[1])
    except (IndexError, ValueError):
        return None


def default_search(question: str, *, collections: list[str], limit: int, as_of: str | None):
    from app.ask import results
    return results.search(question, collections=collections, limit=limit, as_of=as_of)


def default_traverse(uid: str, *, collection: str):
    from app.ask import traverse
    return traverse.traverse(uid, depth=1, collection=collection)


_DEFAULT = object()


def run(held: Held, questions: list[dict[str, Any]], gold: dict[int, Gold], *, k: int = DEFAULT_K,
        search_fn: Callable | None = None, traverse_fn: Callable | None | object = _DEFAULT,
        as_of: str | None = None) -> dict[str, Any]:
    """Every covered question: the ranking as (work, article) pairs, the gold
    node ids among them, the metrics, and the reach of the misses. The two
    callables default to the node's own search and traverse, looked up at
    call time (a test replaces them on the module); `traverse_fn=None` skips
    the reach."""
    search_fn = search_fn or default_search
    traverse_fn = default_traverse if traverse_fn is _DEFAULT else traverse_fn
    name = held.collection.name
    by_pair: dict[tuple[str, str], int] = {(g.work, g.token): g.node_id for g in gold.values() if g.held}
    synthetic = -1
    per_question: list[dict[str, Any]] = []
    totals: dict[str, float] = {}
    reach = {"misses": 0, "reachable": 0}
    edges_cache: dict[str, set[str]] = {}

    def edge_targets(uid: str) -> set[str]:
        if traverse_fn is None:
            return set()
        if uid not in edges_cache:
            try:
                got = traverse_fn(uid, collection=name)
                edges_cache[uid] = {str(e.get("to")) for e in got.get("edges") or []}
            except Exception as e:   # noqa: BLE001 — the reach is a measurement, never the run's failure
                edges_cache[uid] = set()
                per_question.append({"traverse_error": f"{uid}: {type(e).__name__}: {e}"})
        return edges_cache[uid]

    n = 0
    for i, q in enumerate(questions):
        relevant = {int(x) for x in q["relevant_doc_ids"] if int(x) in gold and gold[int(x)].held}
        if not relevant:
            continue
        n += 1
        answer = search_fn(q["question"], collections=[name], limit=k, as_of=as_of)
        ranking: list[int] = []
        seen_pairs: list[tuple[str, str]] = []
        for h in _hits(answer)[:k]:
            uid, seq = str(h.get("uid")), _seq_of(h.get("chunk_id"))
            work = held.work_of(uid)
            token = held.articles(uid)[1].get(seq) if (work and seq is not None) else None
            token = article_token(token) if token else None
            pair = (work or uid, token or f"chunk{seq}")
            if pair in seen_pairs:
                continue
            seen_pairs.append(pair)
            if pair in by_pair:
                ranking.append(by_pair[pair])
            else:
                ranking.append(synthetic)
                synthetic -= 1
        row = bakeoff.metrics_for(ranking, relevant, KS)
        for key, value in row.items():
            totals[key] = totals.get(key, 0.0) + value
        missed = sorted(relevant - set(ranking))
        reachable: list[int] = []
        if missed and traverse_fn is not None:
            hit_uids = [str(h.get("uid")) for h in _hits(answer)[:k]]
            hit_works = {held.work_of(u) for u in hit_uids} - {None}
            hit_keys = set().union(*(held.keys_of(w) for w in hit_works)) if hit_works else set()
            for nid in missed:
                g = gold[nid]
                gold_keys = held.keys_of(g.work) if g.work else set()
                forward = any(edge_targets(u) & gold_keys for u in hit_uids)
                backward = bool(g.uid) and bool(edge_targets(g.uid) & hit_keys)
                if forward or backward:
                    reachable.append(nid)
        reach["misses"] += len(missed)
        reach["reachable"] += len(reachable)
        per_question.append({"index": i, "question": q["question"], "relevant": sorted(relevant),
                             "ranking": ranking, "missed": missed, "reachable_by_edge": reachable,
                             **{m: row[m] for m in (f"recall@{k if k in KS else KS[-1]}", "mrr@10", "ndcg@10")}})
    summary = {key: (value / n if n else 0.0) for key, value in totals.items()}
    summary["questions_run"] = n
    summary["reach_misses"] = reach["misses"]
    summary["reach_reachable_by_one_edge"] = reach["reachable"]
    summary["reach_share"] = (reach["reachable"] / reach["misses"]) if reach["misses"] else None
    return {"summary": summary, "per_question": per_question}


# ---- the temporal arm ----------------------------------------------------------------

def _nugget(before: str, after: str) -> str | None:
    """A number the earlier text carries and the later does not."""
    later = set(_NUMBER.findall(after))
    for n in _NUMBER.findall(before):
        if n not in later and len(n) >= 1:
            return n
    return None


def temporal_make(held: Held, *, limit: int = 200) -> list[dict[str, Any]]:
    """Dated question stubs: for each act with two consecutive text
    versions, the articles whose text differs and whose earlier text carries
    a number the later lost — the date is the earlier version's last day."""
    rows = held.con.execute(
        "SELECT uid, work, valid_from, valid_to FROM validity WHERE has_text=1 "
        "ORDER BY work, valid_from, native_id").fetchall()
    out: list[dict[str, Any]] = []
    by_work: dict[str, list] = {}
    for r in rows:
        by_work.setdefault(r["work"], []).append(r)
    for work, versions in by_work.items():
        for a, b in zip(versions, versions[1:]):
            if a["valid_to"] is None or int(a["valid_to"]) != int(b["valid_from"]):
                continue
            tokens_a = held.articles(a["uid"])[0]
            tokens_b = held.articles(b["uid"])[0]
            for token in sorted(set(tokens_a) & set(tokens_b), key=lambda t: (len(t), t)):
                before, after = held.text_of(a["uid"], token), held.text_of(b["uid"], token)
                if not before or before == after:
                    continue
                nugget = _nugget(before, after)
                if not nugget:
                    continue
                last_day = _day(int(a["valid_to"])) - timedelta(days=1)
                doc = held.record(a["uid"])
                name = (doc.extra or {}).get("denomination") or work
                out.append({"work": work, "denomination": name, "article": token,
                            "as_of": last_day.isoformat(), "gold_uid": a["uid"], "control_uid": b["uid"],
                            "nugget": nugget,
                            "question": f"Che cosa stabiliva l'art. {token} della {name} al {last_day.isoformat()}?",
                            "review": "stub — keep only if this asks a real question; edit the wording"})
                if len(out) >= limit:
                    return out
    return out


def _day(yyyymmdd: int) -> date:
    s = f"{int(yyyymmdd):08d}"
    return date(int(s[:4]), int(s[4:6]), int(s[6:]))


def temporal_run(held: Held, items: list[dict[str, Any]], *, k: int = DEFAULT_K,
                 search_fn: Callable | None = None) -> dict[str, Any]:
    search_fn = search_fn or default_search
    name = held.collection.name
    per: list[dict[str, Any]] = []
    counts = {"n": 0, "version_right": 0, "nugget_found": 0, "control_current": 0, "control_nugget_gone": 0}
    for it in items:
        counts["n"] += 1
        token = article_token(f"art {it['article']}") or str(it["article"])

        def first_match(answer):
            for h in _hits(answer)[:k]:
                uid, seq = str(h.get("uid")), _seq_of(h.get("chunk_id"))
                if held.work_of(uid) == it["work"] and seq is not None:
                    label = held.articles(uid)[1].get(seq, "")
                    if article_token(label) == token:
                        return uid, str(h.get("text") or "")
            return None, ""
        dated_uid, dated_text = first_match(search_fn(it["question"], collections=[name], limit=k,
                                                      as_of=it["as_of"]))
        control_uid, control_text = first_match(search_fn(it["question"], collections=[name], limit=k,
                                                          as_of=None))
        version_right = dated_uid == it["gold_uid"]
        nugget_found = bool(dated_text) and str(it["nugget"]) in dated_text
        control_current = control_uid == it["control_uid"]
        control_gone = bool(control_text) and str(it["nugget"]) not in control_text
        counts["version_right"] += version_right
        counts["nugget_found"] += nugget_found
        counts["control_current"] += control_current
        counts["control_nugget_gone"] += control_gone
        per.append({**it, "dated_uid": dated_uid, "version_right": version_right, "nugget_found": nugget_found,
                    "control_uid_returned": control_uid, "control_current": control_current,
                    "control_nugget_gone": control_gone})
    return {"summary": counts, "per_question": per}


# ---- the command --------------------------------------------------------------------

def _open(name: str, root: Path | None):
    from app import collections as collections_mod, db
    for c in collections_mod.load_all():
        if c.name == name:
            return c, db.connect(c)
    raise SystemExit(f"no collection named {name!r} is held here")


def _load_dataset(out: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    import pyarrow.parquet as pq
    corpus = pq.read_table(out / "corpus.parquet", columns=["id", "file_name", "path"]).to_pylist()
    questions = pq.read_table(out / "questions-validation.parquet").to_pylist()
    return corpus, questions


def _stamp(held: Held) -> dict[str, Any]:
    codex = getattr(held.collection, "codex", None)
    return {"collection": held.collection.name,
            "collection_id": getattr(codex, "collection_id", None),
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}


def _write(out: Path, name: str, payload: dict[str, Any]) -> Path:
    runs = out / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    path = runs / f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{name}.json"
    path.write_text(json.dumps(payload, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("fetch"); p.add_argument("--out", type=Path, required=True)
    for cmd in ("coverage", "run"):
        p = sub.add_parser(cmd)
        p.add_argument("--out", type=Path, required=True)
        p.add_argument("--collection", required=True)
        if cmd == "run":
            p.add_argument("--k", type=int, default=DEFAULT_K)
            p.add_argument("--as-of", default=None, dest="as_of")
            p.add_argument("--no-reach", action="store_true", dest="no_reach")
    p = sub.add_parser("temporal-make")
    p.add_argument("--collection", required=True); p.add_argument("--out", type=Path, required=True)
    p.add_argument("--limit", type=int, default=200)
    p = sub.add_parser("temporal-run")
    p.add_argument("--collection", required=True); p.add_argument("--questions", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True); p.add_argument("--k", type=int, default=DEFAULT_K)
    a = ap.parse_args(argv)

    if a.cmd == "fetch":
        return bakeoff.fetch(a.out)
    c, con = _open(a.collection, None)
    held = Held(c, con)
    if a.cmd in ("coverage", "run"):
        corpus, questions = _load_dataset(a.out)
        cov = coverage(held, corpus, questions)
        gold = {int(k): gold_from(v) for k, v in cov["gold"].items()}
        if a.cmd == "coverage":
            path = _write(a.out, "coverage", {**_stamp(held), **cov})
            print(f"questions {cov['questions']}  covered {cov['covered']}  excluded {cov['excluded']}  "
                  f"articles {cov['articles']} held {cov['articles_held']}  {cov['articles_by_outcome']}")
            print(f"written {path}")
            return 0
        got = run(held, questions, gold, k=a.k, as_of=a.as_of,
                  traverse_fn=None if a.no_reach else _DEFAULT)
        path = _write(a.out, "run", {**_stamp(held), "k": a.k, "as_of": a.as_of,
                                     "coverage": {k: v for k, v in cov.items() if k != "gold"}, **got})
        s = got["summary"]
        print("  ".join(f"{m} {s.get(m, 0.0):.4f}" for m in ("recall@10", "mrr@10", "ndcg@10", "recall@20")))
        print(f"questions {s['questions_run']}  misses {s['reach_misses']}  reachable by one edge "
              f"{s['reach_reachable_by_one_edge']}  share {s['reach_share']}")
        print(f"written {path}")
        return 0
    if a.cmd == "temporal-make":
        items = temporal_make(held, limit=a.limit)
        a.out.parent.mkdir(parents=True, exist_ok=True)
        a.out.write_text("".join(json.dumps(it, ensure_ascii=False) + "\n" for it in items), encoding="utf-8")
        print(f"{len(items)} stubs written to {a.out} — review them before they count")
        return 0
    items = [json.loads(line) for line in a.questions.read_text(encoding="utf-8").splitlines() if line.strip()]
    got = temporal_run(held, items, k=a.k)
    path = _write(a.out, "temporal", {**_stamp(held), "k": a.k, **got})
    print(json.dumps(got["summary"]))
    print(f"written {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
