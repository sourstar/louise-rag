"""WHICH DOCUMENTS HELD THE ENCODER SPENDS ON (07 §7.8 `embed_select`;
operator, 2026-11-02, on Wikipedia: "download and embed english only, plus
all the articles that are either missing or that are longer in another
language; in this case pick only the longer article in another language.
The rest of the material is likely to be redundant. Make it the default
option" — and, on the reading put to them, "Interpretation A is fine").

`"all"` embeds every document held (in the slices `embed_slices` names).
`"smart"`, Wikipedia's default, embeds:

- every English article;
- for each Wikidata item, ONE article in another language — the longest —
  when the item has no English article, or when that one is longer than
  the English one;
- every non-English article no Wikidata item names: it cannot be told to
  be redundant, so it is kept — nothing is declined on a guess (a page with
  no text, a redirect, costs the encoder nothing).

Every other document is DECLINED (`embed_done = -1`, app/slice_scope.py's
mark): held, word-searchable and shared as before, its records untouched
(invariant 11 — this chooses what to EMBED, never what is held); not offered
to the encoder. A document already embedded keeps its vectors. One
selected again (its group changed) goes back to 0.

THE MEASURES. "Longer" is the document's `tokens` (the manifest's, counted
by the chunker with the space's tokenizer): one unit across scripts, where
bytes would favour three-byte scripts and characters two-character words.
An article whose length is not known yet (`tokens = 0`, not chunked) is
not compared: it is selected, never declined on a guess; and an English
article of unknown length does not keep the longest other one out. Ties go to the
lower language ordinal (`ids.LANG_ORDINAL`: wikivec's frozen order), then
the lower document — the same choice from the same data, every time.

THE ITEM is the page's `wikibase_item` from the pinned run's `page_props`
(parser 5, docs/notes/wikipedia-qids.md), read from the packs app/plugins/
wikipedia/page_props.py keeps — never from the `wikidata` collection, as
the QIDs themselves. The document's language and page come from its id
(`ids.decode_native_id`), so nothing is parsed.

THE PASS is whole-collection by nature (an item's articles are anywhere in
the manifest): it reads `(rowid, native_id, tokens)` in rowid windows,
decides with numpy over the arrays (a sort, no Python per row; ~1 GB for
wikipedia's 52.9 M documents), then writes the marks in windows of short
`BEGIN IMMEDIATE` transactions that re-test the column under the lock
(slice_scope's pattern: a stage that moved a row since keeps its move),
resumable from a cursor. It is redone only when what it was computed for
changes — the policy, `embed_slices`, the manifest's last rowid, the
page_props run — so its cost follows the events (a scan that added
documents), never the rounds. Until it has caught up, `ready` says no and
the collection's pipeline waits, as for the slice scope.

The standard for the grouping is the one Wikidata itself is: an item's
sitelinks are its articles in each language. Measuring which article
carries most is a length heuristic, written here so it can be revisited —
the alternative is a content measure (a summariser, a coverage score),
which would cost a model call per article for a choice the operator made
on length.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

import numpy as np

from . import db

KEY = "embed_select"
WINDOW = 50_000
#: app/slice_scope.py's mark: out of the encoder's candidates (their index
#: prefix is embed_done = 0), never counted as embedded (`= 1`)
DECLINED = -1
#: the plugins whose default is "smart" (operator: "Make it the default")
SMART_DEFAULT = ("wikipedia",)
#: the plugins the smart choice knows how to group
SMART_PLUGINS = ("wikipedia",)


class Unavailable(RuntimeError):
    """What the smart choice reads is not on this node yet: deferral."""


def policy(collection) -> str:
    """The collection's choice: the file's `embed_select`, else its plugin's
    default."""
    got = getattr(collection, "embed_select", None)
    if got:
        return str(got)
    return "smart" if getattr(collection, "plugin", None) in SMART_DEFAULT else "all"


def owns_marks(collection) -> bool:
    """Does this pass own the collection's DECLINED marks? Then the slice
    scope leaves `embed_done` alone, and `embed_slices` is applied here."""
    return policy(collection) == "smart"


# ---------------------------------------------------------------- the choice

def smart(lang: np.ndarray, qid: np.ndarray, tokens: np.ndarray, *, english: int = 0) -> np.ndarray:
    """The smart selection over parallel arrays — `lang` the language
    ordinal (-1 unknown), `qid` the Wikidata item's number (0 none),
    `tokens` the length (0 not known). True = embedded."""
    lang = np.asarray(lang, dtype=np.int64)
    qid = np.asarray(qid, dtype=np.int64)
    tokens = np.asarray(tokens, dtype=np.int64)
    n = len(lang)
    en = lang == english
    other = ~en
    selected = en | (lang < 0)
    # no item: nothing tells it is redundant, so it is kept
    selected |= other & (qid == 0)
    # an item's length not known yet: not compared, kept
    selected |= other & (qid != 0) & (tokens == 0)
    cand = np.nonzero(other & (qid != 0) & (tokens > 0) & (lang >= 0))[0]
    if len(cand):
        idx = np.arange(n, dtype=np.int64)
        # the longest per item; ties: lower language ordinal, then lower position
        order = cand[np.lexsort((idx[cand], lang[cand], -tokens[cand], qid[cand]))]
        q = qid[order]
        first = np.ones(len(order), dtype=bool)
        first[1:] = q[1:] != q[:-1]
        best = order[first]
        # the English article's length per item (the longest, if two)
        eng = np.nonzero(en & (qid != 0))[0]
        if len(eng):
            eorder = eng[np.lexsort((-tokens[eng], qid[eng]))]
            eq = qid[eorder]
            efirst = np.ones(len(eorder), dtype=bool)
            efirst[1:] = eq[1:] != eq[:-1]
            en_q, en_t = eq[efirst], tokens[eorder][efirst]
            pos = np.searchsorted(en_q, qid[best])
            pos_c = np.minimum(pos, len(en_q) - 1)
            has_en = (pos < len(en_q)) & (en_q[pos_c] == qid[best])
            en_len = np.where(has_en, en_t[pos_c], -1)
            # an English article of unknown length (0) keeps nothing out
            pick = (~has_en) | (en_len <= 0) | (tokens[best] > en_len)
        else:
            pick = np.ones(len(best), dtype=bool)
        selected[best[pick]] = True
    return selected


# ---------------------------------------------------------------- what it reads

def _wiki_ids():
    from .plugins.wikipedia import ids
    return ids


def decode(native_ids: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """`(language ordinal, page id)` of wikipedia's native ids, vectorised
    (`ids.decode_native_id`'s arithmetic)."""
    ids = _wiki_ids()
    nid = np.asarray(native_ids, dtype=np.int64)
    rest = np.where(nid >= ids.DUMP_BASE, nid - ids.DUMP_BASE, nid)
    return rest // ids.LANG_BASE, rest % ids.LANG_BASE


def props_paths(collection) -> dict[str, Path]:
    """Each language's pinned `page_props` (source.py's layout:
    `<lang>wiki/<snapshot>/<lang>wiki-<snapshot>-page_props.sql.gz`)."""
    from .plugins.wikipedia import page_props
    pin = getattr(collection, "snapshot", None)
    root = Path(collection.corpus_dir)
    if not pin:
        raise Unavailable(f"{collection.name} pins no snapshot: its page_props run is not named")
    return {lang: root / f"{lang}wiki" / pin / f"{lang}wiki-{pin}{page_props.SUFFIX}"
            for lang in _wiki_ids().LANG_ORDINAL}


_QID = r"'wikibase_item','Q(?P<q>[0-9]+)'"


def qids_of_pack(pack: Path) -> tuple[np.ndarray, np.ndarray]:
    """`(pages, qids)` of one page_props pack, pages ascending; a page with
    no item is left out."""
    import pyarrow.compute as pc
    import pyarrow.parquet as pq
    pages_out, qids_out = [], []
    f = pq.ParquetFile(pack)
    for batch in f.iter_batches(columns=["page", "rows"], batch_size=200_000):
        got = pc.extract_regex(batch.column(1), _QID)
        # a page with no item is a NULL struct (its child reads ""): the
        # struct's validity says which pages have one
        ok = pc.is_valid(got)
        q = got.field("q")
        pages = pc.filter(batch.column(0), ok).to_numpy(zero_copy_only=False)
        qs = pc.filter(q, ok).cast("string").cast("int64").to_numpy(zero_copy_only=False)
        pages_out.append(pages.astype(np.int64))
        qids_out.append(qs.astype(np.int64))
    pages = np.concatenate(pages_out) if pages_out else np.zeros(0, dtype=np.int64)
    qids = np.concatenate(qids_out) if qids_out else np.zeros(0, dtype=np.int64)
    order = np.argsort(pages, kind="stable")
    return pages[order], qids[order]


def load_qids(collection, langs: list[int]) -> dict[int, tuple[np.ndarray, np.ndarray]]:
    """Language ordinal -> (pages, qids), for the languages the manifest
    holds; a language whose page_props is not on disk defers the pass. The
    packs are read in threads (pyarrow leaves the GIL): the multi-core rule."""
    from concurrent.futures import ThreadPoolExecutor

    from .plugins.wikipedia import page_props
    ids = _wiki_ids()
    by_ord = {v: k for k, v in ids.LANG_ORDINAL.items()}
    paths = props_paths(collection)
    want = {o: paths[by_ord[o]] for o in langs if o in by_ord}
    missing = [by_ord[o] for o, p in want.items() if not p.is_file()]
    if missing:
        raise Unavailable(f"page_props not on disk for {', '.join(sorted(missing))}")

    def one(item):
        o, props = item
        return o, qids_of_pack(page_props.ensure(props))
    with ThreadPoolExecutor(max_workers=min(len(want), 8) or 1) as pool:
        return dict(pool.map(one, sorted(want.items())))


def props_signature(collection) -> dict[str, list[int]]:
    """Each page_props file's (size, mtime): a new run redoes the pass."""
    out = {}
    try:
        for lang, p in props_paths(collection).items():
            st = p.stat()
            out[lang] = [st.st_size, int(st.st_mtime)]
    except (Unavailable, OSError):
        pass
    return out


def lookup(pages_of: np.ndarray, qids_of: np.ndarray, pages: np.ndarray) -> np.ndarray:
    """The item of each page (0 where the pack names none)."""
    if not len(pages_of):
        return np.zeros(len(pages), dtype=np.int64)
    pos = np.searchsorted(pages_of, pages)
    pos_c = np.minimum(pos, len(pages_of) - 1)
    hit = (pos < len(pages_of)) & (pages_of[pos_c] == pages)
    return np.where(hit, qids_of[pos_c], 0)


# ---------------------------------------------------------------- the pass

def _state(con) -> dict:
    try:
        return json.loads(db.get_control(con, KEY) or "{}")
    except (TypeError, ValueError):
        return {}


def _max_rowid(con) -> int:
    return int(con.execute("SELECT COALESCE(MAX(rowid), 0) FROM documents").fetchone()[0])


def signature(collection, con, props: dict | None = None) -> str:
    from . import slice_scope
    return json.dumps({"policy": policy(collection),
                       "embed": list(slice_scope.embedded_slices(collection) or []) or None,
                       "last": _max_rowid(con),
                       "props": props if props is not None else props_signature(collection)},
                      sort_keys=True)


def ready(con, collection) -> bool:
    """May the collection's stages select documents now? True for "all" with
    nothing left to unwind, or once the pass has caught up with what it is
    computed for. Cheap: the state, the last rowid, the page_props stats."""
    state = _state(con)
    if not owns_marks(collection):
        return not state.get("signature")
    return bool(state.get("done")) and state.get("signature") == signature(collection, con)


def read_manifest(con) -> dict[str, np.ndarray]:
    """`rowid, native_id, tokens` of every document, in rowid windows."""
    rowids, nids, toks = [], [], []
    cursor = 0
    while True:
        rows = con.execute("SELECT rowid, native_id, tokens FROM documents WHERE rowid > ? "
                           "ORDER BY rowid LIMIT ?", (cursor, WINDOW)).fetchall()
        if not rows:
            break
        rowids.append(np.fromiter((r[0] for r in rows), dtype=np.int64, count=len(rows)))
        nids.append(np.fromiter((int(r[1]) if str(r[1]).isdigit() else -1 for r in rows),
                                dtype=np.int64, count=len(rows)))
        toks.append(np.fromiter((r[2] or 0 for r in rows), dtype=np.int64, count=len(rows)))
        cursor = int(rows[-1][0])
    cat = (lambda xs: np.concatenate(xs) if xs else np.zeros(0, dtype=np.int64))
    return {"rowid": cat(rowids), "native_id": cat(nids), "tokens": cat(toks)}


def decide(collection, con, *, qids_for: Callable | None = None) -> tuple[np.ndarray, np.ndarray, dict]:
    """`(rowids, selected)` for every document, and the counts."""
    from . import slice_scope
    m = read_manifest(con)
    ok = m["native_id"] >= 0
    lang, page = decode(np.where(ok, m["native_id"], 0))
    lang = np.where(ok, lang, -1)
    langs = sorted(int(x) for x in np.unique(lang[lang >= 0]))
    by_lang = (qids_for or load_qids)(collection, langs)
    qid = np.zeros(len(lang), dtype=np.int64)
    for o, (pages_of, qids_of) in by_lang.items():
        at = np.nonzero(lang == o)[0]
        qid[at] = lookup(pages_of, qids_of, page[at])
    selected = smart(lang, qid, m["tokens"], english=_wiki_ids().LANG_ORDINAL["en"])
    embed = slice_scope.embedded_slices(collection)
    if embed is not None:
        by_ord = {v: k for k, v in _wiki_ids().LANG_ORDINAL.items()}
        held = np.array([slice_scope.is_held(f"lang={by_ord.get(int(o), '')}", embed)
                         for o in range(max(by_ord) + 1)], dtype=bool)
        selected &= np.where(lang >= 0, held[np.clip(lang, 0, len(held) - 1)], False)
    counts = {"documents": int(len(selected)), "selected": int(selected.sum()),
              "declined": int((~selected).sum()),
              "with_item": int((qid != 0).sum()),
              "english": int((lang == _wiki_ids().LANG_ORDINAL["en"]).sum())}
    return m["rowid"], selected, counts


def apply(con, collection, *, stop=None, log: Callable[[str], None] | None = None,
          qids_for: Callable | None = None) -> dict[str, Any]:
    """Bring `embed_done` in line with the collection's choice: resumable,
    in windows; with "all" and marks left from before, they are unwound."""
    say = log or (lambda m: None)
    state = _state(con)
    smart_on = owns_marks(collection)
    if not smart_on and not state.get("signature"):
        return {"skipped": "every document held is embedded"}
    if smart_on and getattr(collection, "plugin", None) not in SMART_PLUGINS:
        return {"skipped": f"the smart choice groups Wikipedia's articles; "
                           f"{collection.plugin} has no such grouping"}
    if ready(con, collection):
        return {"skipped": "up to date"}
    try:
        props = props_signature(collection) if smart_on else {}
        sig = signature(collection, con, props) if smart_on else None
        if smart_on:
            rowids, selected, counts = decide(collection, con, qids_for=qids_for)
        else:
            rowids, selected, counts = None, None, {}
    except Unavailable as e:
        return {"deferred": str(e)}
    if state.get("signature") != sig:
        state = {"signature": sig, "cursor": 0, "done": False}
    cursor = int(state.get("cursor", 0))
    moved = {"newly_declined": 0, "given_back": 0}
    while True:
        if stop is not None and stop.is_set():
            return {**counts, **moved, "interrupted": True, "cursor": cursor}
        rows = con.execute("SELECT rowid, embed_done FROM documents WHERE rowid > ? "
                           "ORDER BY rowid LIMIT ?", (cursor, WINDOW)).fetchall()
        if not rows:
            break
        rid = np.fromiter((r[0] for r in rows), dtype=np.int64, count=len(rows))
        done = np.fromiter((r[1] if r[1] is not None else 0 for r in rows), dtype=np.int64,
                           count=len(rows))
        if smart_on and len(rowids):
            at = np.searchsorted(rowids, rid)
            at_c = np.minimum(at, len(rowids) - 1)
            seen = (at < len(rowids)) & (rowids[at_c] == rid)
            # a document the decision has not seen (added since) is kept
            chosen = np.where(seen, selected[at_c], True)
        else:
            chosen = np.ones(len(rid), dtype=bool)
        decline = [int(x) for x in rid[~chosen & (done == 0)]]
        give_back = [int(x) for x in rid[chosen & (done == DECLINED)]]
        last = int(rows[-1][0])
        con.execute("BEGIN IMMEDIATE")
        try:
            for was, value, ids in ((0, DECLINED, decline), (DECLINED, 0, give_back)):
                for k in range(0, len(ids), 900):
                    part = ids[k:k + 900]
                    # re-tested under the lock; `+embed_done`: no index term
                    # (slice_scope's lesson: the rowids lead)
                    con.execute(f"UPDATE documents SET embed_done=? WHERE rowid IN "
                                f"({','.join('?' * len(part))}) AND +embed_done=?", (value, *part, was))
            state = {**state, "cursor": last, "done": False}
            db.set_control(con, KEY, json.dumps(state))
            con.execute("COMMIT")
        except BaseException:
            con.execute("ROLLBACK")
            raise
        moved["newly_declined"] += len(decline)
        moved["given_back"] += len(give_back)
        cursor = last
    if smart_on:
        db.set_control(con, KEY, json.dumps({**state, "cursor": cursor, "done": True}))
        say(f"embed selection (smart): {counts.get('selected', 0)} of {counts.get('documents', 0)} "
            f"documents embedded here, {counts.get('declined', 0)} declined "
            f"({moved['newly_declined']} newly, {moved['given_back']} given back)")
    else:
        con.execute("DELETE FROM control WHERE key=?", (KEY,))
        # the slice scope owns the marks again: what `embed_slices` declines,
        # given back above, is declined again by its next pass
        from . import slice_scope
        if slice_scope.embedded(collection) is not None:
            slice_scope.invalidate(con)
        say(f"embed selection: every document held is embedded again "
            f"({moved['given_back']} given back)")
    return {**counts, **moved}
