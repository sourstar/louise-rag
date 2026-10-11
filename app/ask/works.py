"""One hit per work (05 §5.3, amended 2026-10-09;
docs/notes/one-language-per-work.md).

Gutenberg holds a work and its translations as separate books, and a search
used to show each edition that matched as its own hit. Keeping ONE language
per work costs recall whichever language is kept — measured over 451
queries, every such filter lost the work the reader was after on 57–83 of
them, because the edition that matches a query cannot be predicted from its
language. What wins (50 queries better, 0 worse) is to rank as before and
then SHOW EACH WORK ONCE, at the position of its best-ranked edition, as the
edition in the reader's language when one of the work's editions in it was
found, else English, else the best-ranked one; the other editions are listed
under it, so nothing is lost.

WHAT A WORK IS comes from claims, never from a guess at search time: the
`same-work` and `translation-of` claims of the annotations collections this
peer holds and may use about the result's collection (02 §2.10, 05 §5.5 —
a link the origin does not state is an annotation), read from the claim
index `app/hold/annotations.py` keeps per snapshot. For gutenberg those are
the books' own "Wikipedia page about this book" lines joined by Wikidata
sitelinks, and Project Gutenberg's "several editions of this eBook" notes
(`tools/annotations_from_works.py`). No claim, no grouping: an edition no
claim names is shown as it ranked.

The ranked list is unchanged up to the grouping — its scores, its order,
the reranker's work. This is presentation, after the reranker and before
the cut to `limit`, so a page of ten shows ten works.

COST per search: the annotations collections held are listed once; for one
that applies, one walk of its in-memory claim index from the candidates'
uids (bounded by their works' sizes) and one indexed manifest query for the
titles and languages of the editions found. A collection no annotations
collection is about pays the listing alone.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

#: the claim types that say two records are one work
WORK_TYPES = ("same-work", "translation-of")
ENGLISH = "en"


@dataclass(frozen=True)
class Edition:
    """A record of a work, as the target collection holds it."""
    uid: str
    title: str | None
    lang: str | None
    held: bool


def preference(show_in: str | None, query_lang: str | None,
               english: str = ENGLISH) -> tuple[str, ...]:
    """The languages a work is shown in, first choice first: the Ask page's
    "show results in", then the language the query is written in, then
    English. Duplicates and unknowns dropped."""
    out: list[str] = []
    for code in (show_in, query_lang, english):
        code = (code or "").strip().lower()
        if code and code not in ("original", "und") and code not in out:
            out.append(code)
    return tuple(out)


def _uid(hit: dict[str, Any]) -> str:
    from app.retrieval import record_of
    return record_of(hit)


def collapse(hits: Sequence[dict[str, Any]], work_of: dict[str, str],
             members: dict[str, Sequence[Edition]], prefer: Sequence[str]) -> list[dict[str, Any]]:
    """`hits` in ranked order, one per record; `work_of` maps a record's uid
    to its work's key, `members` a work's key to every edition of it known.
    Each work keeps ONE hit, at the position of its best-ranked edition: the
    best-ranked edition in the first language of `prefer` among the work's
    hits, else the best-ranked hit. That hit carries `editions`, every
    other edition of the work — `{uid, title, lang, held, rank, chunk_id}`,
    `rank` and `chunk_id` where the edition was among the hits — those in
    the preferred languages first. A hit of no work is untouched."""
    by_work: dict[str, list[tuple[int, dict[str, Any]]]] = {}
    for i, h in enumerate(hits):
        w = work_of.get(_uid(h))
        if w is not None:
            by_work.setdefault(w, []).append((i, h))
    if not by_work:
        return list(hits)
    lang_of = {e.uid: e.lang for es in members.values() for e in es}

    def lang(h: dict[str, Any]) -> str:
        return (h.get("lang") or lang_of.get(_uid(h)) or "").lower()

    def pref_index(code: str | None) -> int:
        code = (code or "").lower()
        return prefer.index(code) if code in prefer else len(prefer)

    out: list[dict[str, Any]] = []
    for i, h in enumerate(hits):
        w = work_of.get(_uid(h))
        if w is None:
            out.append(h)
            continue
        found = by_work[w]
        if found[0][0] != i:
            continue                       # a later edition of a work already shown
        head_i, head = min(found, key=lambda ih: (pref_index(lang(ih[1])), ih[0]))
        head = dict(head)
        rank_of = {_uid(x): (j + 1, x.get("chunk_id")) for j, x in found}
        editions = []
        for e in members.get(w, ()):
            if e.uid == _uid(head):
                continue
            rank, chunk = rank_of.get(e.uid, (None, None))
            editions.append({"uid": e.uid, "title": e.title, "lang": e.lang, "held": e.held,
                             "rank": rank, "chunk_id": chunk})
        editions.sort(key=lambda e: (pref_index(e["lang"]),
                                     e["rank"] if e["rank"] is not None else float("inf"), e["uid"]))
        head["editions"] = editions
        out.append(head)
    return out


# --------------------------------------------------------------------------
# the work groups, from the claims held here

def _usable(collection, annotations: Iterable | None) -> list:
    """The annotations collections held here, usable, about `collection`."""
    codex = getattr(collection, "codex", None)
    if codex is None:
        return []
    from app.ask import annotations as facts
    held = facts._held(annotations)
    return [a for a in held if a.codex.annotates.collection_id == codex.collection_id
            and facts._withheld(a) is None]


def work_groups(collection, uids: Sequence[str], *, root: Path | None = None,
                annotations: Iterable | None = None,
                ) -> tuple[dict[str, str], dict[str, list[Edition]]]:
    """(work_of, members) for the records `uids` of `collection`, from the
    work claims (`WORK_TYPES`) of the usable annotations collections about
    it whose `from_snapshot` the answer's snapshot has reached. A work is a
    connected set of records under those claims, keyed by its smallest uid;
    records no claim names are in no work."""
    usable = _usable(collection, annotations)
    if not usable or not uids:
        return {}, {}
    from app.hold import annotations as ann
    from app.hold import collection as col
    from app.share import seeder
    from app.ask.results import _root
    root = Path(root) if root is not None else _root()
    try:
        snap = seeder.current_snapshot(col.shard_dir_of(root, collection.name))
    except Exception:   # noqa: BLE001 — no snapshot named: no claim applies (05 §5.3)
        snap = None
    read = snap.snapshot_id if snap is not None else None
    indexes = [ann.index_of(col.shard_dir_of(root, a.name)) for a in usable
               if ann.applies_from(a.codex.annotates.from_snapshot, read)]
    if not indexes:
        return {}, {}
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    seen: set[str] = set()
    todo = list(dict.fromkeys(uids))
    while todo:                     # the candidates' works, whole: a star of claims
        u = todo.pop()              # reaches every edition through its centre
        if u in seen:
            continue
        seen.add(u)
        for idx in indexes:
            for cl in idx.by_target.get(u, ()):
                c = cl.claim
                if c["type"] not in WORK_TYPES:
                    continue
                a, b = find(c["subject"]), find(c["object"])
                if a != b:
                    parent[max(a, b)] = min(a, b)
                for other in (c["subject"], c["object"]):
                    if other not in seen:
                        todo.append(other)
    groups: dict[str, list[str]] = {}
    for u in parent:
        groups.setdefault(find(u), []).append(u)
    groups = {k: v for k, v in groups.items() if len(v) >= 2}
    if not groups:
        return {}, {}
    from app.ask.annotations import _target_rows
    rows = _target_rows(collection, [u for v in groups.values() for u in v])
    work_of = {u: k for k, v in groups.items() for u in v}
    members = {k: [Edition(u, (rows.get(u) or {}).get("title"), (rows.get(u) or {}).get("lang"),
                           u in rows) for u in sorted(v)]
               for k, v in groups.items()}
    return work_of, members


def one_hit_per_work(hits: list[dict[str, Any]], collection, prefer: Sequence[str], *,
                     root: Path | None = None, annotations: Iterable | None = None,
                     ) -> list[dict[str, Any]]:
    """`collapse` with the work groups held here. A grouping that cannot be
    read returns the list as it ranked: a guess about what a book IS never
    costs a reader a hit (and never the search)."""
    if not hits or getattr(collection, "codex", None) is None:
        return hits
    try:
        work_of, members = work_groups(collection, [_uid(h) for h in hits], root=root,
                                       annotations=annotations)
    except Exception:   # noqa: BLE001
        return hits
    return collapse(hits, work_of, members, prefer) if work_of else hits
