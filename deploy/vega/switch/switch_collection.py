"""Point one collection file at LocalAI through VeGa (docs/notes/vega.md §13).

    python3 switch_collection.py FILE EMBED_MODEL QUERY_MODEL [RERANK_MODEL] [--dry-run]

In `[encoder]`: `url` becomes LocalAI's /v1/embeddings, `api` "vega", the two
model keys the vega- models given, and a `query_url` line goes (one URL serves
both sides; the body's model picks the side). In `[reranker]`, when the file
has one and RERANK_MODEL is given: LocalAI's /v1/rerank, `api` "jina",
`rerank_model`. Line by line, comments and everything else kept; prints what
it changed. The caller keeps the previous file (collections.pre-vega/).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

LOCALAI = "http://192.168.178.10:30286"
# `[name]` and `[[name]]` alike: an array of tables (`[[sources]]`) is a block
# of its own. 2026-10-09: the first version matched `[name]` only, so a
# `[[sources]]` after `[encoder]` was read as part of it and louise-stop's
# source url was rewritten to LocalAI's.
_HEADER = re.compile(r"^\s*(\[\[|\[)([^\[\]]+)(\]\]|\])\s*(#.*)?$")
_KEY = re.compile(r'^(\s*)([A-Za-z_]+)(\s*=\s*)("[^"]*")(.*)$')


def switch(text: str, embed: str, query: str, rerank: str | None) -> tuple[str, list[str]]:
    want = {
        # the encoder's url is the SERVER: the client appends /v1/embeddings
        # (2026-10-09: written with the path, every request 404'd and 4,295
        # gutenberg documents took an error). The reranker's url is the endpoint.
        "encoder": {"url": LOCALAI, "api": "vega",
                    "embed_model": embed, "query_model": query},
        "reranker": ({"url": f"{LOCALAI}/v1/rerank", "api": "jina", "rerank_model": rerank}
                     if rerank else {}),
    }
    out, changed, block = [], [], ""
    seen: dict[str, set[str]] = {"encoder": set(), "reranker": set()}
    url_at: dict[str, int] = {}        # where a missing key goes: after the block's url
    eol = "\r\n" if "\r\n" in text else "\n"
    for line in text.splitlines(keepends=True):
        h = _HEADER.match(line)
        if h:
            block = ("[]" if h.group(1) == "[[" else "") + h.group(2).strip()
        body = line.rstrip("\r\n")
        end = line[len(body):]
        k = _KEY.match(body)
        if k and block in want and want[block]:
            key = k.group(2)
            if block == "encoder" and key == "query_url":
                changed.append(f"[encoder] query_url {k.group(4)} removed")
                continue
            if key in want[block]:
                seen[block].add(key)
                new = f'"{want[block][key]}"'
                if new != k.group(4):
                    changed.append(f"[{block}] {key} {k.group(4)} -> {new}")
                line = f"{k.group(1)}{key}{k.group(3)}{new}{k.group(5)}{end}"
                if key == "url":
                    url_at[block] = len(out)
        out.append(line)
    # a key the file leaves to its default (`api` is "openai" when absent) is
    # written after the block's url; a block with no url is not switched
    for b in ("reranker", "encoder"):                  # later block first: indices hold
        missing = [k for k in want[b] if k not in seen[b]]
        if not missing:
            continue
        if b not in url_at:
            raise SystemExit(f"[{b}] has no url line: not switched")
        at = url_at[b] + 1
        for key in missing:
            out.insert(at, f'{key} = "{want[b][key]}"{eol}')
            changed.append(f"[{b}] {key} added: {want[b][key]!r}")
            at += 1
    return "".join(out), changed


def main(argv: list[str]) -> int:
    dry = "--dry-run" in argv
    args = [a for a in argv if a != "--dry-run"]
    path, embed, query = Path(args[0]), args[1], args[2]
    rerank = args[3] if len(args) > 3 else None
    text = path.read_bytes().decode("utf-8")
    new, changed = switch(text, embed, query, rerank)
    for c in changed:
        print(f"{path.name}: {c}")
    if not dry:
        path.write_bytes(new.encode("utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
