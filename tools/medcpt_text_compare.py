"""The archive rows NCBI embedded from ANOTHER text than our record cuts to —
a record revised since NCBI's 2023 run, a POPLINE abstract, anything — found
by comparing texts, not vectors (docs/notes/pubmed-other-abstracts.md; the
"residual misses" of 2026-10-19).

    python -m tools.medcpt_text_compare <collection> <workdir> [--chunks 0-36] [--keep]

NCBI ships, beside each `embeds_chunk_N.npy`, `pubmed_chunk_N.json`: PMID ->
{t: title, a: abstract, …}, the text it embedded. Our side is the vector
table's own text (a pair layer's chunk: title, a blank line, abstract).
Both are FINGERPRINTED after the normalisation MedCPT's uncased tokenizer
applies anyway — case, accents, runs of whitespace — so two texts with one
fingerprint are one input; a differing fingerprint is a row to embed again
(a text the tokenizer reads alike costs a re-embed that comes back the same).

One read of the table, then the chunks one at a time: fetched through the
acquisition transport (the transfer budget, invariant 5; `genesis._https_fetcher`),
read as a stream in bounded memory, compared, deleted unless `--keep`. Writes
<workdir>/differs.txt — the archive's rows (`chunk_verification`, genesis)
whose texts differ — for `louise dev drop-rows <collection> --ids FILE --yes`.
"""
from __future__ import annotations

import argparse
import codecs
import hashlib
import json
import re
import sqlite3
import sys
import unicodedata
from pathlib import Path
from typing import Iterator

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

#: read a chunk this many bytes at a time
BLOCK = 32 << 20
_KEY = re.compile(r'\s*[{,]?\s*"(\d+)"\s*:\s*')


def norm(text: str) -> str:
    """What an uncased BERT tokenizer reads alike: lower case, no combining
    marks, every run of whitespace one space."""
    s = unicodedata.normalize("NFD", (text or "").lower())
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return " ".join(s.split())


def fingerprint(title: str, abstract: str) -> int:
    h = hashlib.blake2b(f"{norm(title)}\x00{norm(abstract)}".encode("utf-8"), digest_size=8)
    return int.from_bytes(h.digest(), "little", signed=True)


def ours(text: str) -> tuple[str, str]:
    """Our chunk text as the pair it was cut from: the title, a blank line,
    the abstract (09 §9.3's `first-section` split)."""
    title, _, rest = (text or "").partition("\n\n")
    return title, rest


def entries(path: Path, block: int = BLOCK) -> Iterator[tuple[int, dict]]:
    """`(pmid, value)` of a flat JSON object `{"<pmid>": {...}, ...}`, read
    a block at a time: memory bounded by a block, never the whole file."""
    dec = json.JSONDecoder()
    utf8 = codecs.getincrementaldecoder("utf-8")()
    buf, pos, eof = "", 0, False
    with open(path, "rb") as f:
        while True:
            m = _KEY.match(buf, pos)
            if m is not None:
                try:
                    value, end = dec.raw_decode(buf, m.end())
                except json.JSONDecodeError:
                    value = None
                if value is not None and (end < len(buf) or eof):
                    yield int(m.group(1)), value
                    pos = end
                    continue
            if eof:
                return
            data = f.read(block)
            eof = not data
            buf = buf[pos:] + utf8.decode(data, final=eof)
            pos = 0


def main(argv: list[str] | None = None) -> int:
    import numpy as np

    from app import collections as cols, embeddings
    from app.bundles import genesis

    p = argparse.ArgumentParser(prog="tools.medcpt_text_compare")
    p.add_argument("collection")
    p.add_argument("workdir", type=Path)
    p.add_argument("--chunks", default="0-36", help="a range, or a comma list")
    p.add_argument("--keep", action="store_true", help="keep the fetched text files")
    a = p.parse_args(argv)
    chunks = ([int(x) for x in a.chunks.split(",")] if "," in a.chunks
              else list(range(int(a.chunks.split("-")[0]), int(a.chunks.split("-")[-1]) + 1)))
    a.workdir.mkdir(parents=True, exist_ok=True)
    c = cols.by_name(a.collection)

    # 1. our side, one read of the table: (pmid, fingerprint), sorted
    pm, fp = [], []
    for part in embeddings.scan_columns(c, ("book_id", "text")):
        books = part.column("book_id").to_pylist()
        texts = part.column("text").to_pylist()
        pm.extend(books)
        fp.extend(fingerprint(*ours(t)) for t in texts)
    pm_a, fp_a = np.asarray(pm, dtype=np.int64), np.asarray(fp, dtype=np.int64)
    order = np.argsort(pm_a, kind="stable")
    pm_a, fp_a = pm_a[order], fp_a[order]
    del pm, fp
    print(f"ours: {len(pm_a)} rows fingerprinted", flush=True)

    # 2. NCBI's side, a chunk at a time
    fetch = genesis._https_fetcher(genesis.MEDCPT_PUBMED)
    differ: list[int] = []
    for n in chunks:
        path = a.workdir / f"pubmed_chunk_{n}.json"
        if not path.is_file():
            fetch(path.name, path)
        seen = same = missing = 0
        for pmid, v in entries(path):
            seen += 1
            i = int(np.searchsorted(pm_a, pmid))
            if i >= len(pm_a) or pm_a[i] != pmid:
                missing += 1                       # not in our table: nothing to compare
                continue
            if fp_a[i] == fingerprint(v.get("t") or "", v.get("a") or ""):
                same += 1
            else:
                differ.append(pmid)
        print(f"chunk {n}: {seen} texts, {same} the same, {seen - same - missing} differ, "
              f"{missing} not in our table", flush=True)
        if not a.keep:
            path.unlink(missing_ok=True)

    # 3. the archive's rows among them
    con = sqlite3.connect(f"file:{c.manifest_path}?mode=ro", uri=True)
    ids = [f"pubmed:{x}:0:1" for x in sorted(set(differ))]
    archive: list[str] = []
    for i in range(0, len(ids), 500):
        part_ids = ids[i:i + 500]
        archive.extend(r[0] for r in con.execute(
            f"SELECT chunk_id FROM chunk_verification WHERE chunk_id IN "
            f"({','.join('?' * len(part_ids))}) AND producer LIKE 'genesis:%'", part_ids))
    out = a.workdir / "differs.txt"
    out.write_text("".join(f"{x}\n" for x in sorted(archive)), encoding="utf-8")
    print(f"{len(differ)} texts differ; {len(archive)} of them an archive's rows -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
