"""MEASUREMENT (not product code): read gutenberg's SHARD for docs/notes/classical-translation.md,
read-only, in the throwaway container of classical_translation.py.

  catalog OUT_FILE      one metadata line per record (all languages, no text): uid, lang,
                        title, creators and creators_detail (name, role, PG agent id,
                        aliases), LCSH, the description's Wikipedia links, chars, est. tokens
  dump UID [UID...]     the records' sections to /out/texts/<native_id>.json
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from app.pack import records as R
from app.plugins import shard as D

SHARD_DIR = Path("/data/collections/gutenberg/shard")


def catalog(path: str) -> None:
    n = 0
    with open(path, "w") as out:
        for p in D.shard_files(SHARD_DIR):
            lang = p.parent.name.split("=", 1)[1]
            for rec in R.iter_records(p):
                ex = rec.get("extra") or {}
                chars = sum(len(b or "") for _, b in rec.get("sections") or [])
                out.write(json.dumps({
                    "uid": rec["uid"], "lang": lang, "title": rec.get("title") or "",
                    "creators": rec.get("creators") or [],
                    "detail": [{k: c.get(k) for k in ("name", "role", "agent_id", "aliases")}
                               for c in ex.get("creators_detail") or [] if c.get("role") != "producer"],
                    "subjects": (ex.get("subjects_by_scheme") or {}).get("LCSH") or [],
                    "wiki": re.findall(r"https?://[a-z\-]+\.wikipedia\.org/wiki/\S+", ex.get("description") or ""),
                    "languages": ex.get("languages"), "header": ex.get("text_header") or {},
                    "shelves": ex.get("bookshelves") or "", "chars": chars, "est_tokens": chars // 4,
                }, ensure_ascii=False) + "\n")
                n += 1
    print(n, "records", file=sys.stderr)


def dump(uids: list[str]) -> None:
    want = set(uids)
    out = Path("/out/texts")
    out.mkdir(exist_ok=True)
    for p in D.shard_files(SHARD_DIR):
        for u in R.read_index(p).uids:
            if u in want:
                rec = R.read_record(p, u)
                (out / f"{rec['native_id']}.json").write_text(json.dumps(
                    {"uid": u, "lang": rec["lang"], "title": rec.get("title"),
                     "sections": rec.get("sections")}, ensure_ascii=False))
                want.discard(u)
    if want:
        print("missing", sorted(want), file=sys.stderr)


if __name__ == "__main__":
    if sys.argv[1] == "catalog":
        catalog(sys.argv[2])
    else:
        dump(sys.argv[2:])
