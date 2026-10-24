"""`louise dev wikidata-classes` — why the family map gives a class its
family, and what a candidate set of roots would change, read from a
wikidata pack's own class tree (read-only; families.py,
docs/notes/wikidata-family-slices.md §5).

For each class asked (default: the probes, then the classes that decide the
most entities in each family under the map): its family, and per family the
shortest P279 path up to that family's nearest root. With `--candidate`
(a JSON file of `roots`, and optionally `families` and `fallback`, in
families.py's shapes) the candidate map is built in memory from the same
class tree — a minute, nothing written — and its family sizes ESTIMATED from
the codes pass already done under the map (families.preview), with the
classes that would move, largest first. The codes pass under the chosen map
gives the real numbers; this exists so a root can be chosen on the dump's
own links in minutes, not on a guess and an hour's pass.

Usage:
  louise dev wikidata-classes <dump> [--map sha256:…] [--candidate roots.json]
                              [--classes Q1,Q2,…] [--top 10] [--out report.json]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from array import array
from collections import Counter
from pathlib import Path

from app.plugins.wikidata import families, pack


def _deciding(con: sqlite3.Connection, sha: str) -> tuple[Counter, Counter]:
    """The codes pass's summaries under map `sha`: (family code, class) →
    entities, and family code → entities (records and pruned)."""
    deciding: Counter = Counter()
    entities: Counter = Counter()
    for (raw,) in con.execute("SELECT summary FROM family_chunk WHERE map=? ORDER BY chunk", (sha,)):
        s = json.loads(raw)
        for part in ("records", "pruned"):
            entities.update({int(c): n for c, n in s[part].items()})
        for c, pairs in s["classes"].items():
            deciding.update({(int(c), int(q)): n for q, n in pairs})
    return deciding, entities


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="louise dev wikidata-classes", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("dump", type=Path, help="the wikidata dump the pack was built from")
    p.add_argument("--map", help="the map to explain (default: the one the code's rules give this dump)")
    p.add_argument("--candidate", type=Path, help="JSON: roots (and families, fallback) to preview")
    p.add_argument("--classes", default="", help="more classes to explain, e.g. Q6256,Q3624078")
    p.add_argument("--top", type=int, default=10, help="classes explained per family (default 10; other ×5)")
    p.add_argument("--out", type=Path, help="write the full report here as JSON")
    a = p.parse_args(argv)

    d = pack.pack_dir(a.dump)
    meta = pack.read_meta(a.dump) or {}
    if not meta.get("subclass_done"):
        print(f"the pack of {a.dump.name} has no class tree yet (version {meta.get('pack_version')})",
              file=sys.stderr)
        return 2
    raw = pack.map_path(d, a.map).read_bytes() if a.map else pack.creation_map(a.dump)
    fmap = families.load_map(raw)
    con = sqlite3.connect((d / "labels.sqlite").resolve().as_uri() + "?mode=ro", uri=True)
    labels = pack.LabelTable(a.dump)

    def label(q: int) -> str | None:
        for lang in ("en", "mul"):
            got = labels(f"Q{q}", lang)
            if got:
                return got
        return None

    def parents_of(q: int) -> list[int]:
        return [r for (r,) in con.execute("SELECT parent FROM subclass WHERE id=?", (q,))]

    try:
        deciding, entities = _deciding(con, fmap.sha256)
        new = None
        if a.candidate:
            cand = json.loads(a.candidate.read_text())
            child, parent = array("q"), array("q")
            for c, q in con.execute("SELECT id, parent FROM subclass"):
                child.append(c)
                parent.append(q)
            new = families.load_map(families.build_map(
                child, parent, max_item=fmap.max_item, dump=fmap.dump,
                roots={f: tuple(rs) for f, rs in cand["roots"].items()},
                families=tuple(cand.get("families") or fmap.families),
                fallback=tuple(cand.get("fallback", ()))))
        ask = [*families.PROBES, *(int(x.strip().lstrip("Q")) for x in a.classes.split(",") if x.strip())]
        by_family: dict[int, Counter] = {}
        for (code, q), n in deciding.items():
            by_family.setdefault(code, Counter())[q] = n
        for code, counts in sorted(by_family.items()):
            ask += [q for q, _ in counts.most_common(a.top * (5 if code == fmap.other else 1))]
        roots = new.roots if new else fmap.roots
        explained = []
        for q in dict.fromkeys(ask):
            paths = families.paths_to_roots(parents_of, q, roots)
            row = {"class": q, "label": label(q), "family": fmap.name(fmap.code_of_class(q)),
                   "paths": {f: [[x, label(x)] for x in path]
                             for f, path in sorted(paths.items(), key=lambda kv: (len(kv[1]), kv[0]))}}
            if new:
                row["candidate"] = new.name(new.code_of_class(q))
            explained.append(row)
        report: dict = {"map": fmap.sha256, "dump": a.dump.name, "explained": explained}
        if new:
            if not entities:
                report["preview"] = f"no codes pass under {fmap.sha256} to estimate from"
            else:
                sizes, moved = families.preview(deciding, entities, fmap, new)
                report["candidate"] = {"map": new.sha256, "roots": new.roots, "families": list(new.families)}
                report["preview"] = {"entities": sizes,
                                     "moved": [[f, t, q, label(q), n] for f, t, q, n in moved[:200]]}
    finally:
        con.close()

    for row in explained:
        head = f"Q{row['class']} {row['label']!r}: {row['family']}"
        if "candidate" in row and row["candidate"] != row["family"]:
            head += f" → {row['candidate']}"
        print(head)
        for f, path in row["paths"].items():
            print(f"    {f:<12} {len(path) - 1} steps: " + " → ".join(f"{lbl or '?'} (Q{x})" for x, lbl in path))
    if isinstance(report.get("preview"), dict):
        print("candidate, estimated entities per family: " + ", ".join(
            f"{f} {n:,}" for f, n in report["preview"]["entities"].items()))
        for f, t, q, lbl, n in report["preview"]["moved"][:40]:
            print(f"    {n:>12,}  {lbl or '?'} (Q{q}): {f} → {t}")
    elif "preview" in report:
        print(report["preview"])
    if a.out:
        a.out.write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
