"""MEASUREMENT (not product code): does a better encoder still pay off once
the whole search runs? The operator, 2026-11-02: "Add BM25 fusion and
translations too, make separated scores. consider top 10, 40, 50, 100 …
In a nutshell we have to verify if it doesn't pay off using a fancy embedder
if the gap is filled by the whole retrieval pipeline".

ONE SEARCH, STAGE BY STAGE, on a judged set (a gutenberg pool, a wikidata or
wikipedia known-item sample), with the product's own code at every step:

- the PLAN: `language_plan.plan(question, [collection], translate=True)`,
  the collection's own main languages, its translations (kept per question:
  one chat-model reading and one translation call, once);
- the TEXT LEG: a fresh tantivy index of the set built by `fts.index_books`,
  each document in its own language group, each group searched with the
  plan's variants (`plan.variants`, `fts.search_records`), as
  `retrieval._search_one_collection` does;
- the VECTOR LEG: each encoder's ranking of the set's chunks, cut at the
  leg's depth in records (`leg_depth.cut_at_records`);
- the FUSION: `retrieval._merge_rrf`, the legs' lists as a search fuses
  them (its `own` lists and `query_lang` tie rule);
- the RERANKER: the collection's own, over the top N of the fused list
  (N = 10, 40, 50, 100), each record judged on its matched chunks
  (JUDGED_CHUNKS) under its title, against the question in the record's
  language where the plan translated it, ordered by `_rerank_order` — the
  steps of `_rerank_list`. Each (question, chunk) score is kept, so a
  stopped run asks only what it has not.

Rows: BM25 alone; each encoder alone; each encoder fused with BM25; and
each of these after the reranker at every depth — each TWICE (the operator,
2026-11-02: "with/without query translation in the article/book
language"): [translated], the search's plan with translation, and [typed],
the API's translate false, where every language group is searched and every
pair judged with the question as typed. The vector leg embeds the question
as typed in both, as a search does; and, as a candidate the product does not
have yet (the operator, 2026-11-02: "by using the translation model harrier
is supposed to have a better score?"), "<encoder> per-language" also
embeds each of the plan's translations into a language the set holds and
fuses those vector lists as variants of the one vector leg, as the text
leg's are (`_merge_rrf`: a record at its best rank across them). And
"<encoder> translated-only" (the operator, 2026-11-02, on wikidata: "does it
make sense to translate the query always to english for bge?") embeds the
question in the set's languages only: as typed where its language is held,
the plan's translations into the others, the typed question where the plan
has none — on a set held in one language, the question in that language and
nothing else. Scored per question on the
rank of the first relevant record: MRR@5 and @10, hit@1/5/10, and what
reached 40, 50 and 100 (the reranker's ceiling at that depth). Pairs, by
Fisher's randomization test: the encoders against each other at each stage,
each encoder's whole pipeline against BM25 + the reranker alone, and each
stage translated against typed.

Not reproduced, the same for every row: the catalogue leg and its first
place (gutenberg), the by-name leg (wikidata), the grouping of a work's
editions after the reranker.
"""
from __future__ import annotations

import json
import re
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

#: the reranker's depths the operator named
DEPTHS = (10, 40, 50, 100)
#: results a search returns (05 §5.4); the candidates are max(4k, depth)
K = 10
#: candidates per reranker call: ~one search's (50 records x 2 chunks)
RERANK_BATCH = 100
#: the record prefix of the set's own ids: `pe:<doc>:<chunk>:1`
_PREFIX = "pe"


@dataclass
class Doc:
    """One record of the set: its language, its title line (what the
    reranker reads above a chunk), its chunks' texts in order."""
    lang: str
    title: str
    chunks: list[str]


@dataclass
class Question:
    text: str
    relevant: frozenset[int]          # the docs that answer it
    lang: str | None = None           # for the split; the plan's reading when None


def chunk_id(d: int, j: int) -> str:
    return f"{_PREFIX}:{d}:{j}:1"


def doc_of(chunk: str) -> int:
    return int(chunk.split(":")[1])


def _doc_of_record(rec: str) -> int:
    return int(rec.split(":")[1])


# ---------------------------------------------------------------- kept state

class RerankCache:
    """Each (question, chunk, the question's text it was judged against)
    reranker score, as JSON lines under `where`, pinned to `settings`
    (another reranker or form is another directory). A pair the typed and
    the translated searches both ask — the same text — is scored once."""

    def __init__(self, where: Path, settings: dict) -> None:
        where.mkdir(parents=True, exist_ok=True)
        pinned = where / "settings.json"
        if pinned.exists():
            before = json.loads(pinned.read_text(encoding="utf-8"))
            if before != settings:
                changed = sorted(k for k in set(before) | set(settings)
                                 if before.get(k) != settings.get(k))
                raise SystemExit(f"{where}: scored before with different settings "
                                 f"({', '.join(changed)}); use another directory")
        else:
            pinned.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
        self.path = where / "scores.jsonl"
        self.scores: dict[tuple[int, str, str], float] = {}
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    x = json.loads(line)
                    self.scores[(x["q"], x["c"], x["v"])] = float(x["s"])

    def add(self, q: int, got: dict[tuple[str, str], float]) -> None:
        with self.path.open("a", encoding="utf-8") as fh:
            for (c, v), sc in got.items():
                fh.write(json.dumps({"q": q, "c": c, "v": v, "s": sc}, ensure_ascii=False) + "\n")
                self.scores[(q, c, v)] = sc


class PlanStore:
    """Each question's language plan, as `make(text)` gives it (a dict:
    core, translations, query_lang, only, main), kept as JSON lines."""

    def __init__(self, path: Path, make: Callable[[str], dict]) -> None:
        self.path, self.make = path, make
        self.rows: dict[int, dict] = {}
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    x = json.loads(line)
                    self.rows[x["q"]] = x

    def get(self, q: int, text: str) -> dict:
        row = self.rows.get(q)
        if row is None or row["query"] != text:
            row = {"q": q, "query": text, **self.make(text)}
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            self.rows[q] = row
        return row


def search_plans(collection, translate: bool = True) -> Callable[[str], dict]:
    """`PlanStore`'s `make` for `collection`: the search's own plan."""
    def make(text: str) -> dict:
        from app import language_plan
        p = language_plan.plan(text, [collection], translate)
        main = p.main_for(collection) if translate else None
        return {"core": p.core, "translations": {k.lower(): v for k, v in (p.translations or {}).items() if v},
                "query_lang": p.query_lang, "only": list(p.only),
                "main": list(main) if main is not None else None, "status": p.status}
    return make


def _plan_of(row: dict):
    from app.language_plan import LanguagePlan
    return LanguagePlan(core=row["core"], only=tuple(row.get("only") or ()),
                        translations=dict(row.get("translations") or {}),
                        query_lang=row.get("query_lang"))


def call_reranker(recipe, query: str, texts: list[str], timeout: float) -> list[float]:
    """One request as `_rerank_list` sends it; the scores in text order."""
    from app import retrieval
    if recipe.api == "jina":
        body = {"query": query, "documents": texts, "top_n": len(texts)}
    else:
        body = {"query": query, "texts": texts, "truncate": True}
    if recipe.model:
        body["model"] = recipe.model
    data = retrieval._rerank_call(recipe, body, timeout, time.monotonic() + timeout)
    rows = data["results"] if isinstance(data, dict) else data
    got = {int(o["index"]): float(o["relevance_score"] if "relevance_score" in o else o["score"])
           for o in rows}
    if sorted(got) != list(range(len(texts))):
        raise SystemExit(f"the reranker answered for {len(got)} of {len(texts)} candidates")
    return [got[i] for i in range(len(texts))]


class QueryRanker:
    """Texts embedded as one encoder's query side (`encode`), kept by text
    under `path`, each ranking the set's chunks: (doc, chunk) best first.
    `check` = (typed questions, the vectors the row was scored with): before
    the first embedding the first few are embedded again and must agree
    (cosine ≥ CHECK_MIN), or the query side was not reproduced and nothing is
    measured with it."""

    CHECK_MIN = 0.999

    def __init__(self, path: Path, encode: Callable[[list[str]], list[list[float]]], doc_unit,
                 pairs: Sequence[tuple[int, int]], *, depth: int = 2_000, check=None) -> None:
        import numpy as np
        self.path, self.encode, self.doc, self.pairs = path, encode, doc_unit, list(pairs)
        self.depth = min(depth, len(self.pairs))
        self.check = check
        self.vectors: dict[str, np.ndarray] = {}
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    x = json.loads(line)
                    self.vectors[x["t"]] = np.asarray(x["v"], dtype=np.float32)

    def _verify(self) -> None:
        import numpy as np
        texts, stored = self.check
        self.check = None
        n = min(4, len(texts))
        got = np.asarray(self.encode(list(texts[:n])), dtype=np.float64)
        ref = np.asarray(stored[:n], dtype=np.float64)
        cos = (got * ref).sum(1) / (np.linalg.norm(got, axis=1) * np.linalg.norm(ref, axis=1))
        if n and float(cos.min()) < self.CHECK_MIN:
            raise SystemExit(f"{self.path.name}: the typed questions embed to cosine "
                             f"{float(cos.min()):.6f} of the row's own vectors: the query side "
                             f"is not the one the row was scored with")

    def __call__(self, texts: list[str]) -> list[list[tuple[int, int]]]:
        import numpy as np
        missing = sorted({t for t in texts if t not in self.vectors})
        if missing:
            if self.check is not None:
                self._verify()
            got = self.encode(missing)
            with self.path.open("a", encoding="utf-8") as fh:
                for t, v in zip(missing, got):
                    fh.write(json.dumps({"t": t, "v": [float(x) for x in v]}, ensure_ascii=False) + "\n")
                    self.vectors[t] = np.asarray(v, dtype=np.float32)
        out = []
        for t in texts:
            v = self.vectors[t].astype(np.float64)
            sims = self.doc @ (v / np.linalg.norm(v))
            best = np.argpartition(-sims, self.depth - 1)[:self.depth]
            best = best[np.lexsort((best, -sims[best]))]
            out.append([self.pairs[int(i)] for i in best])
        return out


# ---------------------------------------------------------------- the legs

def build_index(where: Path, docs: Sequence[Doc], batch: int = 2000):
    """A fresh text index of the set, each document in its language's group
    (`fts.index_books`, the analyzer and stemmer a record gets); reused when
    the same documents built it. Returns the ad-hoc collection to search."""
    import hashlib
    import shutil

    from app import collections as collections_mod
    from app import fts
    sig = hashlib.sha256(json.dumps([[d.lang, d.chunks] for d in docs],
                                    ensure_ascii=False).encode("utf-8")).hexdigest()
    c = collections_mod.Collection(
        name="pipeline-eval", plugin="gutenberg", profile=None, embed_mode_default=None,
        corpus_dir=where, manifest_path=where / "unused.sqlite", fts_dir=where / "index",
        lance_dir=where / "unused-lance")
    marker = where / "index.sig"
    if marker.exists() and marker.read_text().strip() == sig:
        return c
    shutil.rmtree(where / "index", ignore_errors=True)
    where.mkdir(parents=True, exist_ok=True)
    entries = [(d.lang or "und", i, [{"id": chunk_id(i, j), "chapter": "", "text": t}
                                     for j, t in enumerate(d.chunks)])
               for i, d in enumerate(docs)]
    for k in range(0, len(entries), batch):
        fts.index_books(entries[k:k + batch], c)
    fts.close_all()
    marker.write_text(sig + "\n")
    return c


def text_lists(index, plan, limit: int, main: Sequence[str] | None = None
               ) -> tuple[list[list[dict]], set[int]]:
    """The text leg's lists, as `_search_one_collection` runs it: each
    group the plan searches, with each of its variants; `main` the
    collection's main languages (`plan.main_for`), None for every group."""
    from app import fts
    lists: list[list[dict]] = []
    own: set[int] = set()
    main = tuple(main) if main is not None else None
    for group in fts.existing_groups(index):
        if not plan.searches(group):
            continue
        held = fts.group_languages(group, index)
        for q in (plan.variants(group, held, main) if main is not None else plan.variants(group, held)):
            hits = fts.search_records(group, q, limit, collection=index, langs=plan.lang_filter(group))
            if hits:
                if q == plan.core:
                    own.add(len(lists))
                lists.append(hits)
    return lists, own


def vector_list(ranking: Sequence[tuple[int, int]], docs: Sequence[Doc], limit: int) -> list[dict]:
    """An encoder's chunk ranking as the vector leg's list: chunk hits, cut
    where they hold `limit` records."""
    from app import leg_depth
    hits = [{"chunk_id": chunk_id(d, j), "book_id": d, "lang": docs[d].lang,
             "text": docs[d].chunks[j], "chapter": "", "source": "dense"}
            for d, j in ranking]
    return leg_depth.cut_at_records(hits, limit)


# ---------------------------------------------------------------- scoring

def _first_relevant(ranked: list[dict], relevant: frozenset[int]) -> int:
    from app import retrieval
    for r, h in enumerate(ranked, 1):
        if _doc_of_record(retrieval.record_of(h)) in relevant:
            return r
    return 10 ** 9


def metrics(ranks: Sequence[int], depths: Sequence[int] = DEPTHS) -> dict:
    n = max(1, len(ranks))
    out = {"mrr@5": sum(1 / r for r in ranks if r <= 5) / n,
           "mrr@10": sum(1 / r for r in ranks if r <= 10) / n}
    for k in sorted({1, 5, 10, *depths}):
        out[f"hit@{k}"] = sum(1 for r in ranks if r <= k) / n
    return {k: round(v, 4) for k, v in out.items()}


#: the two forms of a question (the operator, 2026-11-02: "with/without
#: query translation in the article/book language")
TRANSLATED, TYPED = "translated", "typed"
#: the vector leg run with each translation too (translated rows only)
PER_LANGUAGE = "per-language"
TRANSLATED_ONLY = "translated-only"


def row_name(leg: str, depth: int | None, mode: str) -> str:
    return f"{leg}" + (f" + rerank@{depth}" if depth else "") + f" [{mode}]"


def evaluate(out: Path, collection, docs: Sequence[Doc], questions: Sequence[Question],
             rankings: dict[str, Sequence[Sequence[tuple[int, int]]]], *,
             recipe, plans: PlanStore, typed: PlanStore | None = None,
             query_rankers: dict[str, Callable[[list[str]], list[list[tuple[int, int]]]]] | None = None,
             depths: Sequence[int] = DEPTHS, k: int = K,
             call=None, timeout: float = 600.0, index=None, say=print) -> dict:
    """The module docstring's measurement. `rankings[encoder][q]` is that
    encoder's chunks for question q, best first, as (doc, chunk) pairs.
    `plans` the search's plans with translation, `typed` without (the API's
    translate false: every language group searched, and every pair judged,
    with the question as typed); each gives its own rows.
    `query_rankers[encoder](texts)` embeds texts as that encoder's query
    side and ranks the set's chunks, (doc, chunk) best first: given, the
    translated rows gain "<encoder> per-language". Writes
    `out`/pipeline.json; returns it."""
    from app import retrieval
    call = call or call_reranker
    depths = sorted(set(depths))
    limit = max(k, depths[-1], 20)
    index = index or build_index(out / "bm25", docs)
    cache = RerankCache(out / "rerank" / re.sub(r"[^A-Za-z0-9_.-]+", "_", recipe.model or "rerank"),
                        {"url": recipe.url, "api": recipe.api, "model": recipe.model,
                         "window": getattr(recipe, "window", None), "format": 2,
                         "head_chars": retrieval.HEAD_CHARS, "judged": retrieval.JUDGED_CHUNKS})
    retrieval._warm_reranker(recipe)
    modes = {TRANSLATED: plans} | ({TYPED: typed} if typed is not None else {})

    def text_of(c: str) -> str:
        d = docs[doc_of(c)]
        body = d.chunks[int(c.split(":")[2])]
        head = (d.title or "")[:retrieval.HEAD_CHARS]
        return f"{head}\n\n{body}" if head else body

    def variant(plan, c: str) -> str:
        """The question as `_rerank_list` judges this chunk: in its record's
        language where the plan translated it, else as typed."""
        return plan.translations.get((docs[doc_of(c)].lang or "").lower()) or plan.core

    def matched(h: dict) -> list[dict]:
        return (h.get(retrieval.MATCHED) or [h])[:retrieval.JUDGED_CHUNKS]

    held_langs = {(d.lang or "").lower() for d in docs}
    rankers = query_rankers or {}
    legs = ["BM25"] + [f"{e}{s}" for e in rankings for s in ("", " + BM25")]
    per_lang = [f"{e} {s}" for e in rankings if e in rankers
                for s in (PER_LANGUAGE, TRANSLATED_ONLY)]
    legs_tr = legs + [f"{b}{s}" for b in per_lang for s in ("", " + BM25")]
    ranks: dict[tuple[str, str, int | None], list[int]] = {}
    langs: list[str | None] = []
    untranslated = 0
    asked = 0
    top = max(4 * k, depths[-1])
    t0 = time.monotonic()
    for q, question in enumerate(questions):
        lists: dict[str, tuple] = {}
        for mode, store in modes.items():
            row = store.get(q, question.text)
            plan = _plan_of(row)
            if mode == TRANSLATED:
                untranslated += row.get("status") not in (None, "ok", "off")
                langs.append(question.lang or row.get("query_lang"))
            texts, own = text_lists(index, plan, limit, row.get("main"))
            merged = {"BM25": retrieval._merge_rrf(texts, limit, own=own, query_lang=plan.query_lang)}
            for e, per_q in rankings.items():
                vec = vector_list(per_q[q], docs, limit)
                merged[e] = retrieval._merge_rrf([vec], limit, query_lang=plan.query_lang)
                merged[f"{e} + BM25"] = retrieval._merge_rrf(texts + [vec], limit, own=own,
                                                             query_lang=plan.query_lang)
                if mode == TRANSLATED and e in rankers:
                    variants = [t for lang, t in sorted(plan.translations.items())
                                if lang in held_langs and t and t != plan.core]
                    tvecs = [vector_list(r, docs, limit) for r in rankers[e](variants)]
                    vecs = [vec] + tvecs
                    merged[f"{e} {PER_LANGUAGE}"] = retrieval._merge_rrf(
                        vecs, limit, query_lang=plan.query_lang)
                    merged[f"{e} {PER_LANGUAGE} + BM25"] = retrieval._merge_rrf(
                        texts + vecs, limit, own=own, query_lang=plan.query_lang)
                    held = (plan.query_lang or "").lower() in held_langs
                    only = ([vec] if held else []) + tvecs or [vec]
                    merged[f"{e} {TRANSLATED_ONLY}"] = retrieval._merge_rrf(
                        only, limit, query_lang=plan.query_lang)
                    merged[f"{e} {TRANSLATED_ONLY} + BM25"] = retrieval._merge_rrf(
                        texts + only, limit, own=own, query_lang=plan.query_lang)
            lists[mode] = (plan, merged)
        # the reranker's pairs: every list's top records, their matched
        # chunks, each against its form of the question; the deepest list
        # covers every shallower one
        want = {(m["chunk_id"], variant(plan, m["chunk_id"]))
                for plan, merged in lists.values() for lst in merged.values()
                for h in lst[:depths[-1]] for m in matched(h)}
        todo = sorted(w for w in want if (q, *w) not in cache.scores)
        if todo:
            groups: dict[str, list[str]] = {}
            for c, v in todo:
                groups.setdefault(v, []).append(c)
            got: dict[tuple[str, str], float] = {}
            for v, cs in groups.items():
                sent = retrieval.fit_to_window([text_of(c) for c in cs], recipe)
                for b in range(0, len(cs), RERANK_BATCH):
                    got.update({(c, v): sc for c, sc in zip(
                        cs[b:b + RERANK_BATCH], call(recipe, v, sent[b:b + RERANK_BATCH], timeout))})
            cache.add(q, got)
            asked += len(todo)
        for mode, (plan, merged) in lists.items():
            for leg in (legs_tr if mode == TRANSLATED else legs):
                lst = merged[leg]
                ranks.setdefault((mode, leg, None), []).append(_first_relevant(lst, question.relevant))
                cand = lst[:top]
                for n in depths:
                    head, tail = cand[:n], cand[n:]
                    best = [max(cache.scores[(q, m["chunk_id"], variant(plan, m["chunk_id"]))]
                                for m in matched(h)) for h in head]
                    order = retrieval._rerank_order(best, fuse=retrieval.RERANK_FUSE)
                    ranks.setdefault((mode, leg, n), []).append(
                        _first_relevant([head[i] for i in order] + tail, question.relevant))
        if q % 20 == 0:
            say(f"  question {q + 1}/{len(questions)}: {asked} pairs scored, "
                f"{time.monotonic() - t0:.0f} s")
    res = _report(ranks, langs, depths, list(rankings), list(modes), per_lang)
    res |= {"collection": getattr(collection, "name", str(collection)), "documents": len(docs),
            "chunks": sum(len(d.chunks) for d in docs), "questions": len(questions),
            "untranslated_plans": untranslated, "pairs_scored": len(cache.scores),
            "depths": depths, "k": k, "modes": list(modes)}
    out.mkdir(parents=True, exist_ok=True)
    (out / "pipeline.json").write_text(json.dumps(res, indent=2, ensure_ascii=False) + "\n",
                                       encoding="utf-8")
    for line in table(res):
        say(line)
    return res


def _report(ranks: dict[tuple[str, str, int | None], list[int]], langs: list[str | None],
            depths: Sequence[int], encoders: list[str], modes: list[str],
            per_lang: Sequence[str] = ()) -> dict:
    from tools.measure.fusion_calibration import randomization
    rows = {row_name(leg, d, mode): metrics(r, depths) for (mode, leg, d), r in ranks.items()}
    by_lang: dict[str, dict[str, float]] = {}
    for lang in sorted({x for x in langs if x}):
        idx = [i for i, x in enumerate(langs) if x == lang]
        by_lang[lang] = {"questions": len(idx)} | {
            row_name(leg, d, mode): metrics([r[i] for i in idx], depths)["mrr@10"]
            for (mode, leg, d), r in ranks.items()}

    def pair(a: tuple, b: tuple) -> dict:
        out: dict = {}
        for cut in (10, 5):
            xa = [1 / r if r <= cut else 0.0 for r in ranks[a]]
            xb = [1 / r if r <= cut else 0.0 for r in ranks[b]]
            d = [x - y for x, y in zip(xa, xb)]
            out |= {f"mrr@{cut}": round(sum(d) / len(d), 4), f"p@{cut}": round(randomization(d), 4),
                    f"better@{cut}": sum(x > 0 for x in d), f"worse@{cut}": sum(x < 0 for x in d)}
        return out

    pairs: dict[str, dict] = {}
    stages = [None, *depths]

    def add(ka: tuple, kb: tuple) -> None:
        if ka in ranks and kb in ranks:
            pairs[f"{row_name(*ka[1:], ka[0])}  vs  {row_name(*kb[1:], kb[0])}"] = pair(ka, kb)
    for mode in modes:
        # the encoders against each other, alone and fused, at every stage;
        # the per-language legs among themselves and against their own
        for fused in ("", " + BM25"):
            for d in stages:
                for group in (encoders, list(per_lang)):
                    for i, a in enumerate(group):
                        for b in group[i + 1:]:
                            add((mode, a + fused, d), (mode, b + fused, d))
                for b in per_lang:
                    add((mode, b + fused, d), (mode, b.rsplit(" ", 1)[0] + fused, d))
        # the operator's question: does the encoder pay off once the rest runs?
        for e in [*encoders, *per_lang]:
            for d in depths:
                add((mode, f"{e} + BM25", d), (mode, "BM25", d))
    # what the translation itself buys, stage by stage
    if TRANSLATED in modes and TYPED in modes:
        for leg in ["BM25"] + [f"{e}{s}" for e in encoders for s in ("", " + BM25")]:
            for d in stages:
                ka, kb = (TRANSLATED, leg, d), (TYPED, leg, d)
                if ka in ranks and kb in ranks and (leg.endswith("BM25") or d):
                    pairs[f"{row_name(leg, d, TRANSLATED)}  vs  {row_name(leg, d, TYPED)}"] = pair(ka, kb)
    return {"rows": rows, "by_language": by_lang, "pairs": pairs}


def table(res: dict) -> list[str]:
    """The rows as a fixed-width table, then the pairs."""
    cols = ["mrr@10", "mrr@5", "hit@1", "hit@5", "hit@10"] + [f"hit@{n}" for n in res["depths"]
                                                             if n not in (1, 5, 10)]
    width = max(len(n) for n in res["rows"])
    out = ["| " + "stage".ljust(width) + " | " + " | ".join(c.rjust(7) for c in cols) + " |",
           "|" + "-" * (width + 2) + "|" + "|".join("-" * 9 for _ in cols) + "|"]
    for name, m in res["rows"].items():
        out.append("| " + name.ljust(width) + " | "
                   + " | ".join(f"{m.get(c, float('nan')):.4f}".rjust(7) for c in cols) + " |")
    out.append("")
    out.append(f"Pairs ({res['questions']} questions; Fisher's randomization test, uncorrected):")
    for name, p in res["pairs"].items():
        out.append(f"  {name}: MRR@10 {p['mrr@10']:+.4f} (p = {p['p@10']:.4f}, "
                   f"{p['better@10']} better, {p['worse@10']} worse); "
                   f"MRR@5 {p['mrr@5']:+.4f} (p = {p['p@5']:.4f})")
    if res.get("untranslated_plans"):
        out.append(f"\n{res['untranslated_plans']} questions had no translation (the model "
                   f"unavailable): their variants are the question as typed")
    return out


if __name__ == "__main__":       # the adapters run it: encoder_bakeoff, wikidata_encoder_ab
    print("run `python -m tools.encoder_bakeoff rerank …` or "
          "`python -m tools.measure.wikidata_encoder_ab rerank …`", file=sys.stderr)
    raise SystemExit(2)
