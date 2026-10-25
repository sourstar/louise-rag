"""MEASUREMENT, read-only (not product code): what the node's wikidata pack
(app/plugins/wikidata/pack.py) says about two questions of 2026-10-24.

`mul` — parser 2 (app/plugins/wikidata/textify.py): a referenced entity's
label falls back to `mul`, as upstream's `get_lang_val` does; parser 1 wrote
its id. On a seeded sample of entities, the English text under both chains:
how many texts change, how many referenced labels parser 1 wrote as ids;
and how many entities have no English label, and no `mul` one either —
named by names.py's order (operator, 2026-10-24) — and how many still keep
their id, having no label and no name statement at all.
With `--counts`, the label table's `en` and `mul` rows, and the entities
with a `mul` label and no English one.

`classes` — the operator: "I want that also monuments, buildings and events
are full text translated as well, as ancient and uncommon words (barbacane,
barbican) if it fits". Over the dump itself:
- the P279 (subclass of) closure of the root classes;
- the entities whose P31 (instance of) is in it, the classes themselves
  (the vocabulary: a barbican is a class of fortification), and those with
  a heritage designation (P1435);
- per language, how much of their text Wikidata's own labels would write:
  their label and description, and every label the textifier looks up
  (its own lookups, recorded);
- P279 cycles inside the closure, for the report to the Wikidata team
  (docs/notes/upstream-reports/wikidata.md), found with Tarjan's strongly
  connected components;
- on a sample, the English text's chunks and tokens through the real
  chunker: what one more language costs to embed;
- `--find`: the entities labelled with these words in any language.

On the node, in the louise container (its env finds the pack):
  nice -n 19 python -m tools.wikidata_coverage mul --dump DUMP --counts
  nice -n 19 python -m tools.wikidata_coverage classes --dump DUMP \
      --processes 6 --tokenizer sha256:f59925fc… --out FILE
It reads the pack's Parquet files and labels.sqlite, and writes FILE
(a few KB). Nothing else is written.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import multiprocessing
import random
import sqlite3
import sys
import time
from array import array
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

import pyarrow.parquet as pq

from app.plugins.wikidata import names, pack, textify

LANGS = ("en", "it", "de", "fr", "es")
#: architectural structure, building, monument, archaeological site, event, occurrence
ROOTS = ("Q811979", "Q41176", "Q4989906", "Q839954", "Q1656682", "Q1190554")
HERITAGE = "P1435"
FIND = ("barbacane", "barbican", "merlon", "machicolation")


# ---- reading the pack -----------------------------------------------------------------------

def _rows(path: Path, columns: tuple[str, ...] = ("id", "pruned", "line")) -> Iterator[tuple]:
    pf = pq.ParquetFile(path)
    for g in range(pf.num_row_groups):
        t = pf.read_row_group(g, columns=list(columns))
        yield from zip(*(t.column(c).to_pylist() for c in columns))


def _qnum(eid: Any) -> int | None:
    if isinstance(eid, str) and eid[:1] == "Q" and eid[1:].isdigit():
        return int(eid[1:])
    return None


def values(obj: dict, pid: str) -> list[str]:
    """The entity ids a property's statements name; deprecated ones left out."""
    claims = obj.get("claims")
    out: list[str] = []
    if not isinstance(claims, dict):
        return out
    for st in claims.get(pid) or ():
        if not isinstance(st, dict) or st.get("rank") == "deprecated":
            continue
        dv = ((st.get("mainsnak") or {}).get("datavalue") or {}).get("value")
        if isinstance(dv, dict) and isinstance(dv.get("id"), str):
            out.append(dv["id"])
    return out


def masks(obj: dict, langs: tuple[str, ...]) -> tuple[int, int]:
    """(labels, descriptions): bit i set for a value in `langs[i]`, bit
    `len(langs)` for one in `mul`. An empty map is `[]` in the dump."""
    out = []
    for key in ("labels", "descriptions"):
        d = obj.get(key)
        m = 0
        if isinstance(d, dict):
            for i, lang in enumerate((*langs, "mul")):
                v = d.get(lang)
                if isinstance(v, dict) and v.get("value"):
                    m |= 1 << i
        out.append(m)
    return out[0], out[1]


def _pool_map(fn: Callable, items: list, processes: int, initializer: Callable | None = None,
              initargs: tuple = ()) -> Iterator:
    if processes <= 1:
        if initializer is not None:
            initializer(*initargs)
        for it in items:
            yield fn(it)
        return
    ctx = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max_workers=processes, mp_context=ctx, initializer=initializer,
                             initargs=initargs) as ex:
        yield from ex.map(fn, items, chunksize=1)


def _progress(what: str, done: int, total: int, t0: float) -> None:
    rate = done / max(time.time() - t0, 1e-9)
    print(json.dumps({"pass": what, "files": done, "of": total, "s": round(time.time() - t0),
                      "eta_s": round((total - done) / rate) if rate else None}), file=sys.stderr, flush=True)


# ---- the class tree -------------------------------------------------------------------------

def _edges_of(path: str) -> tuple[array, array, int]:
    """P279 edges (child, parent) of the chunk's items, and its largest item number."""
    child, parent, maxq = array("i"), array("i"), 0
    for eid, pruned, line in _rows(Path(path)):
        q = _qnum(eid)
        if q is not None and q > maxq:
            maxq = q
        if pruned or q is None or b'"P279"' not in line:
            continue
        for v in values(json.loads(line), "P279"):
            p = _qnum(v)
            if p is not None:
                child.append(q)
                parent.append(p)
    return child, parent, maxq


def closure(child: Iterable[int], parent: Iterable[int], roots: Iterable[int]) -> dict[int, set[int]]:
    """Each root with every class below it by P279, the root included."""
    subs: dict[int, list[int]] = {}
    for c, p in zip(child, parent):
        subs.setdefault(p, []).append(c)
    out: dict[int, set[int]] = {}
    for r in roots:
        seen, stack = {r}, [r]
        while stack:
            for c in subs.get(stack.pop(), ()):
                if c not in seen:
                    seen.add(c)
                    stack.append(c)
        out[r] = seen
    return out


def shortest_path(parents: dict[int, list[int]], start: int, goal: int) -> list[int]:
    """The fewest P279 steps from a class up to a root (breadth first), or []."""
    back: dict[int, int | None] = {start: None}
    queue = [start]
    for node in queue:
        if node == goal:
            path = [node]
            while back[path[-1]] is not None:
                path.append(back[path[-1]])
            return list(reversed(path))
        for up in parents.get(node, ()):
            if up not in back:
                back[up] = node
                queue.append(up)
    return []


def cycles(child: Iterable[int], parent: Iterable[int], within: set[int]) -> list[list[int]]:
    """P279 cycles among `within`: Tarjan's strongly connected components of
    more than one class, iteratively, and each class that is its own
    subclass. A cycle touching a class of a closure lies wholly inside it."""
    graph: dict[int, list[int]] = {}
    loops = set()
    for c, p in zip(child, parent):
        if c in within and p in within:
            if c == p:
                loops.add(c)
            else:
                graph.setdefault(c, []).append(p)
    index: dict[int, int] = {}
    low: dict[int, int] = {}
    on: set[int] = set()
    stack: list[int] = []
    out: list[list[int]] = [[c] for c in sorted(loops)]
    counter = 0
    for v0 in graph:
        if v0 in index:
            continue
        work = [(v0, 0)]
        while work:
            v, i = work[-1]
            if i == 0 and v not in index:
                index[v] = low[v] = counter
                counter += 1
                stack.append(v)
                on.add(v)
            nbrs = graph.get(v, ())
            if i < len(nbrs):
                work[-1] = (v, i + 1)
                w = nbrs[i]
                if w not in index:
                    work.append((w, 0))
                elif w in on:
                    low[v] = min(low[v], index[w])
                continue
            work.pop()
            if work:
                u = work[-1][0]
                low[u] = min(low[u], low[v])
            if low[v] == index[v]:
                comp = []
                while True:
                    w = stack.pop()
                    on.discard(w)
                    comp.append(w)
                    if w == v:
                        break
                if len(comp) > 1:
                    out.append(sorted(comp))
    return out


# ---- the main pass: per chunk file, in a worker ---------------------------------------------

_G: dict[str, Any] = {}


def _init(class_bits: dict[int, int], langs: tuple[str, ...], dump: str, every: int,
          tokenizer: str | None, max_tokens: int, find: tuple[str, ...],
          count_bits: dict[int, int] | None = None) -> None:
    _G.update(class_bits=class_bits, langs=tuple(langs), dump=Path(dump), every=every, tokenizer=tokenizer,
              max_tokens=max_tokens, find=tuple(f.casefold() for f in find), count_bits=count_bits or {})


def statement_facts(obj: dict) -> Counter:
    """What the text renders specially or leaves out (operator, 2026-10-24:
    "check if there is any other information from wikidata that we silently
    ignore"): `novalue` / `somevalue` statements, lexeme values, quantities
    with bounds, and the statements the best-rank filter drops (a normal one
    of a property that has a preferred one) — each counted, and the entity
    once per kind (`<kind>_entities`)."""
    out: Counter = Counter()
    claims = obj.get("claims") if isinstance(obj.get("claims"), dict) else {}
    for statements in claims.values():
        if not isinstance(statements, list):
            continue
        preferred = any(isinstance(st, dict) and st.get("rank") == "preferred" for st in statements)
        for st in statements:
            if not isinstance(st, dict):
                continue
            snak = st.get("mainsnak") if isinstance(st.get("mainsnak"), dict) else {}
            kind = snak.get("snaktype")
            if kind in ("novalue", "somevalue"):
                out[kind] += 1
            dv = snak.get("datavalue") if isinstance(snak.get("datavalue"), dict) else {}
            v = dv.get("value")
            if dv.get("type") == "wikibase-entityid" and isinstance(v, dict) and \
                    v.get("entity-type") in ("lexeme", "form", "sense"):
                out["lexeme_values"] += 1
            if dv.get("type") == "quantity" and isinstance(v, dict) and "upperBound" in v:
                out["quantities_with_bounds"] += 1
            if preferred and st.get("rank") == "normal":
                out["dropped_by_rank"] += 1
    for kind in list(out):
        out[f"{kind}_entities"] = 1
    return out


class _Lookups(textify.Normalizer):
    """The textifier's own label lookups, recorded: every id whose label its
    text would write, in the order it asks."""

    def __init__(self) -> None:
        super().__init__(lang="en", fallback=None, labels=lambda eid, lang: None)
        self.asked: list[str] = []

    def _label(self, entity_id: str):
        self.asked.append(entity_id)
        return None


def sampled(eid: str, every: int) -> bool:
    """A fixed sample, the same on every run: one entity in `every` by its id."""
    return every > 0 and int.from_bytes(hashlib.blake2b(eid.encode(), digest_size=8).digest(), "big") % every == 0


def _labels_for_cost(dump: Path) -> Callable:
    """The pack's labels; on a version-1 pack (no names yet) a name lookup
    answers nothing rather than deferring, so the cost is still measured."""
    table = pack.labels(dump)
    ready = (pack.read_meta(dump) or {}).get("pack_version") == pack.PACK_VERSION
    return table if ready else (lambda eid, lang: None if lang == names.NAME else table(eid, lang))


def _cost(eid: str, obj: dict) -> tuple[int, int | None, int | None]:
    """The English text's characters, and its chunks and tokens through the
    real chunker when the tokenizer is given."""
    text = textify.entity_text(obj, "en", _labels_for_cost(_G["dump"]), fallback="en")
    if not _G["tokenizer"]:
        return len(text), None, None
    from app.plugins.wikidata import chunker
    tok = chunker.tokenizer_for(_G["tokenizer"])
    pieces = [c["text"] for c in chunker.chunk_wikidata_text(
        eid, 0, [("en", text)], params={"max_tokens": _G["max_tokens"], "tokenizer": _G["tokenizer"]})]
    return len(text), len(pieces), sum(chunker._encode(tok, p)[0] for p in pieces)


def _classes_of(path: str) -> dict[str, Any]:
    class_bits: dict[int, int] = _G["class_bits"]
    langs: tuple[str, ...] = _G["langs"]
    q_all, lm_all, dm_all, pruned_q = array("i"), bytearray(), bytearray(), array("i")
    props: dict[str, int] = {}
    sel: Counter = Counter()
    inst: Counter = Counter()
    refs: Counter = Counter()
    sample: list[tuple] = []
    errors: Counter = Counter()
    examples: list[tuple[str, str]] = []
    found: list[dict] = []
    facts: Counter = Counter()
    facts_selected: Counter = Counter()
    counted: Counter = Counter()
    count_bits: dict[int, int] = _G.get("count_bits") or {}
    entities = 0
    for eid, pruned, line in _rows(Path(path)):
        q = _qnum(eid)
        if pruned:
            if q is not None:
                pruned_q.append(q)
            continue
        entities += 1
        obj = json.loads(line)
        lm, dm = masks(obj, langs)
        these = statement_facts(obj)
        facts.update(these)
        if count_bits:
            bits = 0
            for c in values(obj, "P31"):
                cq = _qnum(c)
                if cq is not None:
                    bits |= count_bits.get(cq, 0)
            for i in range(bits.bit_length()):
                if bits >> i & 1:
                    counted[i] += 1
        if q is None:
            props[eid] = lm
            continue
        q_all.append(q)
        lm_all.append(lm)
        dm_all.append(dm)
        p31 = values(obj, "P31")
        inst_bits = 0
        for c in p31:
            cq = _qnum(c)
            if cq is not None:
                inst_bits |= class_bits.get(cq, 0)
        own_bits = class_bits.get(q, 0)
        heritage = bool(values(obj, HERITAGE))
        labels = obj.get("labels")
        if _G["find"] and isinstance(labels, dict):
            hits = [(lang, v["value"]) for lang, v in labels.items()
                    if isinstance(v, dict) and isinstance(v.get("value"), str) and v["value"].casefold() in _G["find"]]
            if hits:
                found.append({"id": eid, "labels": hits, "p31": p31, "p279": values(obj, "P279"),
                              "instance_bits": inst_bits, "class_bits": own_bits, "heritage": heritage})
        if not (inst_bits or own_bits or heritage):
            continue
        sl = obj.get("sitelinks")
        n_sl = len(sl) if isinstance(sl, dict) else 0
        # bit i: a Wikipedia article in langs[i] (`itwiki`): where it is documented
        hm = sum(1 << i for i, lang in enumerate(langs) if isinstance(sl, dict) and f"{lang}wiki" in sl)
        sel[(inst_bits, own_bits, heritage, 0 if n_sl == 0 else 1 if n_sl < 5 else 2, lm, dm, hm)] += 1
        facts_selected.update(these)
        for c in p31:
            cq = _qnum(c)
            if cq is not None and cq in class_bits:
                inst[cq] += 1
        try:
            look = _Lookups()
            look.normalize(obj)
            refs.update(look.asked)
            if sampled(eid, _G["every"]):
                sample.append((eid, inst_bits, own_bits, heritage, *_cost(eid, obj), lm, dm, hm))
        except Exception as e:   # noqa: BLE001 — counted: the parse would fail on it too
            errors[type(e).__name__] += 1
            if len(examples) < 5:
                examples.append((eid, f"{type(e).__name__}: {e}"[:200]))
    ref_q, ref_n, ref_p = array("i"), array("L"), {}
    for rid, n in refs.items():
        rq = _qnum(rid)
        if rq is not None:
            ref_q.append(rq)
            ref_n.append(n)
        else:
            ref_p[rid] = n
    return {"q": q_all, "lm": bytes(lm_all), "dm": bytes(dm_all), "pruned_q": pruned_q, "props": props,
            "sel": sel, "inst": inst, "ref_q": ref_q, "ref_n": ref_n, "ref_p": ref_p, "sample": sample,
            "errors": errors, "examples": examples, "found": found, "entities": entities,
            "facts": facts, "facts_selected": facts_selected, "counted": counted}


# ---- classes --------------------------------------------------------------------------------

def _label_of(dump: Path, eid: str) -> str | None:
    table = pack.labels(dump)
    return table(eid, "en") or table(eid, "mul")


def run_classes(dump: Path, *, roots: tuple[str, ...] = ROOTS, langs: tuple[str, ...] = LANGS,
                processes: int = 1, every: int = 1000, tokenizer: str | None = None, max_tokens: int = 1024,
                find: tuple[str, ...] = FIND, top: int = 30, top_missing: int = 200,
                count_roots: tuple[str, ...] = ()) -> dict[str, Any]:
    import numpy as np

    if len(langs) > 7:
        raise SystemExit("at most 7 languages (one byte of bits with mul)")
    meta = pack.read_meta(dump) or {}
    if not meta.get("complete"):
        raise SystemExit(f"{dump}: its pack is not complete")
    files = [str(p) for p in pack.chunk_files(dump)]
    t0 = time.time()
    child, parent, maxq = array("i"), array("i"), 0
    for i, (c, p, m) in enumerate(_pool_map(_edges_of, files, processes), 1):
        child.extend(c)
        parent.extend(p)
        maxq = max(maxq, m)
        if i % 50 == 0 or i == len(files):
            _progress("P279", i, len(files), t0)
    root_q = [q for q in (_qnum(r) for r in roots) if q is not None]
    trees = closure(child, parent, root_q)
    class_bits: dict[int, int] = {}
    for i, r in enumerate(root_q):
        for c in trees[r]:
            class_bits[c] = class_bits.get(c, 0) | (1 << i)
    within = set(class_bits)
    # roots only COUNTED (Wikimedia's internal items): their trees, outside the selection
    count_q = [q for q in (_qnum(r) for r in count_roots) if q is not None]
    count_trees = closure(child, parent, count_q) if count_q else {}
    count_bits: dict[int, int] = {}
    for i, r in enumerate(count_q):
        for c in count_trees[r]:
            count_bits[c] = count_bits.get(c, 0) | (1 << i)
    loops = cycles(child, parent, within)
    parents: dict[int, list[int]] = {}
    for c, p in zip(child, parent):
        if c in within and p in within:
            parents.setdefault(c, []).append(p)
    t_tree = time.time() - t0

    lm = np.zeros(maxq + 1, np.uint8)
    dm = np.zeros(maxq + 1, np.uint8)
    pruned = np.zeros(maxq + 1, np.bool_)
    present = np.zeros(maxq + 1, np.bool_)
    ref = np.zeros(maxq + 1, np.uint32)          # references per item: ~0.6 GB at 1.5e8 items
    props: dict[str, int] = {}
    ref_p: Counter = Counter()
    sel: Counter = Counter()
    inst: Counter = Counter()
    sample: list[tuple] = []
    errors: Counter = Counter()
    examples: list = []
    found: list = []
    entities = beyond = 0
    t1 = time.time()
    args = (class_bits, langs, str(dump), every, tokenizer, max_tokens, find, count_bits)
    facts: Counter = Counter()
    facts_selected: Counter = Counter()
    counted: Counter = Counter()
    for i, got in enumerate(_pool_map(_classes_of, files, processes, _init, args), 1):
        q = np.frombuffer(got["q"], dtype=np.int32)
        present[q] = True
        lm[q] = np.frombuffer(got["lm"], dtype=np.uint8)
        dm[q] = np.frombuffer(got["dm"], dtype=np.uint8)
        pruned[np.frombuffer(got["pruned_q"], dtype=np.int32)] = True
        rq = np.frombuffer(got["ref_q"], dtype=np.int32)
        rn = np.frombuffer(got["ref_n"], dtype=np.uint64 if got["ref_n"].itemsize == 8 else np.uint32)
        keep = rq <= maxq
        beyond += int(rn[~keep].sum())
        np.add.at(ref, rq[keep], rn[keep].astype(np.uint32))
        props.update(got["props"])
        ref_p.update(got["ref_p"])
        sel.update(got["sel"])
        inst.update(got["inst"])
        sample.extend(got["sample"])
        errors.update(got["errors"])
        facts.update(got["facts"])
        facts_selected.update(got["facts_selected"])
        counted.update(got["counted"])
        examples.extend(got["examples"][: max(0, 10 - len(examples))])
        found.extend(got["found"])
        entities += got["entities"]
        if i % 50 == 0 or i == len(files):
            _progress("entities", i, len(files), t1)

    mul_bit = 1 << len(langs)
    root_names = {r: _label_of(dump, r) for r in roots}

    def share(pred: Callable[[tuple], bool]) -> dict[str, Any]:
        total = sum(n for k, n in sel.items() if pred(k))
        out: dict[str, Any] = {"entities": total}
        for i, lang in enumerate(langs):
            bit = 1 << i
            out[lang] = {
                "label": round(sum(n for k, n in sel.items() if pred(k) and k[4] & bit) / total, 4) if total else None,
                "label_or_mul": round(sum(n for k, n in sel.items() if pred(k) and k[4] & (bit | mul_bit)) / total, 4)
                if total else None,
                "description": round(sum(n for k, n in sel.items() if pred(k) and k[5] & bit) / total, 4)
                if total else None,
            }
        out["with_sitelinks"] = round(sum(n for k, n in sel.items() if pred(k) and k[3]) / total, 4) if total else None
        return out

    per_root = {}
    for i, r in enumerate(roots):
        bit = 1 << i
        per_root[r] = {"label": root_names[r], "classes_in_tree": len(trees.get(_qnum(r), ())),
                       "instances": share(lambda k, b=bit: bool(k[0] & b)),
                       "classes": share(lambda k, b=bit: bool(k[1] & b))}
    union = share(lambda k: True)
    heritage_only = sum(n for k, n in sel.items() if k[2] and not k[0] and not k[1])

    # WHERE A LANGUAGE ADDS MOST (operator, 2026-10-24: "focus on the niche
    # cases"): Wikidata says more about the entity in L than in English — a
    # label in L and no English or mul one, a description in L and no English
    # one, or an L Wikipedia article and no English one
    en = langs.index("en") if "en" in langs else None

    def is_niche(i: int, lm_: int, dm_: int, hm_: int) -> dict[str, bool]:
        b, e = 1 << i, (1 << en) if en is not None else 0
        got = {"label_not_english": bool(lm_ & b) and not lm_ & (e | mul_bit),
               "description_not_english": bool(dm_ & b) and not dm_ & e,
               "wiki_not_english": bool(hm_ & b) and not hm_ & e}
        got["any"] = any(got.values())
        return got

    niche: dict[str, Any] = {}
    for i, lang in enumerate(langs):
        if i == en:
            continue
        counts = Counter()
        for k, n in sel.items():
            for name, yes in is_niche(i, k[4], k[5], k[6]).items():
                if yes:
                    counts[name] += n
        tok = [s_[6] for s_ in sample if s_[6] is not None and is_niche(i, s_[7], s_[8], s_[9])["any"]]
        per_entity = sum(tok) / len(tok) if tok else None
        niche[lang] = {**{k: counts.get(k, 0) for k in ("label_not_english", "description_not_english",
                                                        "wiki_not_english", "any")},
                       "named": sum(n for k, n in sel.items() if k[4] & ((1 << i) | mul_bit)),
                       "sampled_any": len(tok), "tokens_per_entity_any": round(per_entity, 1) if per_entity else None,
                       "tokens_any": round(per_entity * counts.get("any", 0)) if per_entity else None}
    niche["all"] = {"sections": sum(v["any"] for v in niche.values()),
                    "tokens": sum(v["tokens_any"] or 0 for v in niche.values())}
    for i, r in enumerate(roots):
        bit = 1 << i
        per_root[r]["niche_any"] = {lang: sum(n for k, n in sel.items() if (k[0] & bit or k[1] & bit)
                                              and is_niche(j, k[4], k[5], k[6])["any"])
                                    for j, lang in enumerate(langs) if j != en}

    total_refs = int(ref.sum()) + sum(ref_p.values()) + beyond
    ref_cov = {}
    for i, lang in enumerate(langs):
        bit = 1 << i
        items_own = int(ref[(lm & bit) != 0].sum())
        items_any = int(ref[(lm & (bit | mul_bit)) != 0].sum())
        p_own = sum(n for pid, n in ref_p.items() if props.get(pid, 0) & bit)
        p_any = sum(n for pid, n in ref_p.items() if props.get(pid, 0) & (bit | mul_bit))
        ref_cov[lang] = {"own_language": round((items_own + p_own) / total_refs, 4) if total_refs else None,
                         "with_mul": round((items_any + p_any) / total_refs, 4) if total_refs else None}
    referenced = {"label_lookups": total_refs, "to_properties": sum(ref_p.values()),
                  "to_pruned_articles": int(ref[pruned].sum()), "to_ids_not_in_dump": beyond,
                  # an id below the largest the dump holds but with no line: merged into another
                  # entity (a redirect, which the JSON dump does not carry) or deleted
                  "to_absent_entities": int(ref[~present & ~pruned].sum()),
                  "absent_entities": int(((ref > 0) & ~present & ~pruned).sum()),
                  "coverage": ref_cov}

    # THE WORKLIST for the report: per language, the entities the selection's
    # texts name most often that have no label in it (nor in mul) — where the
    # text in that language falls back, most-referenced first
    missing = {}
    for i, lang in enumerate(langs):
        bit = 1 << i
        cand = np.nonzero((ref > 0) & present & ((lm & (bit | mul_bit)) == 0))[0]
        order = cand[np.argsort(ref[cand], kind="stable")[::-1][:top_missing]]
        missing[lang] = {"entities": int(cand.size), "references": int(ref[cand].sum()),
                         "most_referenced": [{"id": f"Q{int(x)}", "label": _label_of(dump, f"Q{int(x)}"),
                                              "references": int(ref[x])} for x in order]}

    def mean(xs: list) -> float | None:
        xs = [x for x in xs if x is not None]
        return round(sum(xs) / len(xs), 2) if xs else None
    cost = {"sampled": len(sample), "every": every,
            "chars_per_entity": mean([s[4] for s in sample]),
            "chunks_per_entity": mean([s[5] for s in sample]),
            "tokens_per_entity": mean([s[6] for s in sample])}
    if cost["chunks_per_entity"] is not None:
        cost["chunks_per_language"] = round(cost["chunks_per_entity"] * union["entities"])
        cost["tokens_per_language"] = round((cost["tokens_per_entity"] or 0) * union["entities"])
    tops = [{"id": f"Q{c}", "label": _label_of(dump, f"Q{c}"), "instances": n,
             "roots": [r for i, r in enumerate(roots) if class_bits.get(c, 0) & (1 << i)]}
            for c, n in inst.most_common(top)]
    # per root, what its tree holds: the classes with the most instances
    # per root, what its tree holds: the classes with the most instances, and
    # the shortest P279 chain that brings each in (what an editor would question)
    def chain(c: int, r: str) -> list[str]:
        return [f"Q{x} {_label_of(dump, f'Q{x}') or '?'}" for x in shortest_path(parents, c, _qnum(r))]
    per_root_tops = {r: [{"id": f"Q{c}", "label": _label_of(dump, f"Q{c}"), "instances": n, "path": chain(c, r)}
                         for c, n in sorted(((c, n) for c, n in inst.items() if class_bits.get(c, 0) & (1 << i)),
                                            key=lambda cn: (-cn[1], cn[0]))[:top]]
                     for i, r in enumerate(roots)}
    return {
        "dump": str(dump), "languages": list(langs), "roots": per_root,
        "selected": union, "heritage_only": heritage_only, "entities_read": entities, "niche": niche,
        "referenced_labels": referenced, "missing_labels": missing, "cost_per_added_language": cost,
        "top_classes": tops, "top_classes_per_root": per_root_tops,
        "p279_cycles": {"count": len(loops), "classes": sum(len(c) for c in loops),
                        "largest": [{"classes": [{"id": f"Q{x}", "label": _label_of(dump, f"Q{x}")} for x in c[:8]],
                                     "size": len(c)} for c in sorted(loops, key=len, reverse=True)[:20]]},
        "found": found, "textifier_errors": dict(errors), "error_examples": examples,
        "statements": {"all": dict(sorted(facts.items())), "selected": dict(sorted(facts_selected.items()))},
        "counted": {r: {"label": _label_of(dump, r), "classes_in_tree": len(count_trees.get(_qnum(r), ())),
                        "entities": counted.get(i, 0)} for i, r in enumerate(count_roots)},
        "seconds": {"tree": round(t_tree), "entities": round(time.time() - t1)},
    }


# ---- mul ------------------------------------------------------------------------------------

class _Chain(textify.Normalizer):
    """The textifier with a given label chain, its lookups counted."""

    def __init__(self, chain: tuple[str, ...], labels: Callable) -> None:
        super().__init__(lang="en", fallback="en", labels=labels)
        self._chain = chain
        self.slots = self.unlabelled = 0
        self.via_mul: list[tuple[str, str]] = []

    def _label(self, entity_id: str):
        self.slots += 1
        got = next((x for x in (self.labels(entity_id, lang) for lang in self._chain) if x), None)
        if not got:
            self.unlabelled += 1
        elif "mul" in self._chain and not self.labels(entity_id, "en"):
            self.via_mul.append((entity_id, got))
        return got


def run_mul(dump: Path, *, groups: int = 100, per_group: int = 100, seed: int = 20261024,
            counts: bool = False) -> dict[str, Any]:
    files = pack.chunk_files(dump)
    all_groups = [(f, g) for f in files for g in range(pq.ParquetFile(f).num_row_groups)]
    rng = random.Random(seed)
    picks = sorted(rng.sample(all_groups, min(groups, len(all_groups))))
    table = pack.labels(dump)
    n = changed = slots = p1_ids = p2_ids = no_en = no_name = titled_by_id = 0
    examples: list[dict] = []
    untitled: list[dict] = []
    for f, g in picks:
        t = pq.ParquetFile(f).read_row_group(g, columns=["id", "pruned", "line"])
        rows = [(i, line) for i, p, line in zip(t.column("id").to_pylist(), t.column("pruned").to_pylist(),
                                                 t.column("line").to_pylist()) if not p]
        for eid, line in rng.sample(rows, min(per_group, len(rows))):
            obj = json.loads(line)
            one, two = _Chain(("en",), table), _Chain(("en", "mul"), table)
            t1, t2 = one.normalize(obj).to_text("en"), two.normalize(obj).to_text("en")
            n += 1
            # the record's title and its text's first words: the English label,
            # else mul, else the id (source.py, textify._lang_val)
            own = obj.get("labels") if isinstance(obj.get("labels"), dict) else {}
            if not (own.get("en") or {}).get("value"):
                no_en += 1
                if not (own.get("mul") or {}).get("value"):
                    no_name += 1
                    titled_by_id += names.display_name(obj) is None
                    if len(untitled) < 10:
                        sl = obj.get("sitelinks")
                        untitled.append({
                            "entity": eid, "label_count": len(own),
                            "labels": {k: (v or {}).get("value") for k, v in list(own.items())[:12]},
                            "instance_of": [f"{c} ({table(c, 'en') or table(c, 'mul') or '?'})"
                                            for c in values(obj, "P31")],
                            "sitelinks": sorted(sl)[:12] if isinstance(sl, dict) else [],
                            "named": names.display_name(obj)})
            slots += one.slots
            p1_ids += one.unlabelled
            p2_ids += two.unlabelled
            if t1 != t2:
                changed += 1
                if len(examples) < 10 and two.via_mul:
                    examples.append({"entity": eid, "referenced": two.via_mul[0][0], "mul_label": two.via_mul[0][1]})
    out: dict[str, Any] = {
        "dump": str(dump), "entities": n, "texts_changed": changed,
        "texts_changed_share": round(changed / n, 4) if n else None,
        "label_lookups": slots, "written_as_id_parser1": p1_ids, "written_as_id_parser2": p2_ids,
        "examples": examples,
        "no_english_label": no_en, "no_english_nor_mul": no_name, "no_english_nor_mul_examples": untitled,
        "no_english_nor_mul_share": round(no_name / n, 4) if n else None,
        "titled_by_id": titled_by_id}
    if counts:
        con = sqlite3.connect((pack.pack_dir(dump) / "labels.sqlite").resolve().as_uri() + "?mode=ro", uri=True)
        out["label_rows"] = {lang: con.execute("SELECT COUNT(*) FROM label WHERE lang=?", (lang,)).fetchone()[0]
                             for lang in ("en", "mul")}
        out["mul_without_en"] = con.execute(
            "SELECT COUNT(*) FROM label m WHERE m.lang='mul' AND NOT EXISTS "
            "(SELECT 1 FROM label e WHERE e.id=m.id AND e.lang='en')").fetchone()[0]
        con.close()
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("mul", help="parser 1 against parser 2 on a sample")
    m.add_argument("--dump", required=True)
    m.add_argument("--groups", type=int, default=100)
    m.add_argument("--per-group", type=int, default=100)
    m.add_argument("--seed", type=int, default=20261024)
    m.add_argument("--counts", action="store_true")
    m.add_argument("--out")
    c = sub.add_parser("classes", help="monuments, buildings, events: counts and languages")
    c.add_argument("--dump", required=True)
    c.add_argument("--roots", default=",".join(ROOTS))
    c.add_argument("--langs", default=",".join(LANGS))
    c.add_argument("--processes", type=int, default=1)
    c.add_argument("--every", type=int, default=1000, help="one selected entity in EVERY rendered for the cost")
    c.add_argument("--tokenizer", help="the layer's tokenizer sha256, for chunks and tokens")
    c.add_argument("--max-tokens", type=int, default=1024)
    c.add_argument("--find", default=",".join(FIND))
    c.add_argument("--top-missing", type=int, default=200)
    c.add_argument("--top", type=int, default=30, help="classes listed, overall and per root")
    c.add_argument("--count-roots", default="", help="roots whose instances are only counted (e.g. Q17442446)")
    c.add_argument("--out")
    a = ap.parse_args(argv)
    if a.cmd == "mul":
        got = run_mul(Path(a.dump), groups=a.groups, per_group=a.per_group, seed=a.seed, counts=a.counts)
    else:
        got = run_classes(Path(a.dump), roots=tuple(x for x in a.roots.split(",") if x),
                          langs=tuple(x for x in a.langs.split(",") if x), processes=a.processes, every=a.every,
                          tokenizer=a.tokenizer, max_tokens=a.max_tokens,
                          find=tuple(x for x in a.find.split(",") if x), top_missing=a.top_missing, top=a.top,
                          count_roots=tuple(x for x in a.count_roots.split(",") if x))
    text = json.dumps(got, ensure_ascii=False, indent=1)
    if a.out:
        Path(a.out).write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
