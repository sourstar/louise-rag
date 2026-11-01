"""Before tools/migrate_model_keys.py, on a node whose collection files
predate the explicit model keys (operator, 2026-10-08).

Under the model pool the URL picks the model (`…/upstream/<model>`, operator
2026-10-03); a `model = …` line beside it was a label nothing read — on the
node 2026-10-09 gutenberg's and wikipedia's reranker said
`bge-reranker-v2-m3` beside `/upstream/bge-reranker`, pubmed's encoder
`ncbi/MedCPT-Article-Encoder` beside `/upstream/medcpt-article`. The
migration refuses such a file, rightly, since the new keys must agree with
the URL. This sets each `[encoder]`/`[reranker]` `model` line to what its
block's `url` names, line by line (comments and layout kept), and prints
what it changed. A block whose URL names no model is left alone.

    python3 align_model_names.py [--dry-run] FILE...
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

# [name] and [[name]] alike: an array of tables is a block of its own
# (switch_collection.py, 2026-10-09, read `[[sources]]` as part of [encoder])
_HEADER = re.compile(r"^\s*(\[\[|\[)([^\[\]]+)(\]\]|\])\s*(#.*)?$")
_KEY = re.compile(r'^(\s*)(url|model)(\s*=\s*)"([^"]*)"(.*)$')


def model_named_by(url: str) -> str | None:
    m = re.search(r"/upstream/([^/]+)", url)
    return m.group(1) if m else None


def align(text: str) -> tuple[str, list[str]]:
    lines = text.splitlines(keepends=True)
    blocks: dict[int, str] = {}          # line index -> block name
    block = ""
    for i, line in enumerate(lines):
        h = _HEADER.match(line)
        if h:
            block = ("[]" if h.group(1) == "[[" else "") + h.group(2).strip()
        blocks[i] = block
    urls: dict[str, str] = {}
    for i, line in enumerate(lines):
        k = _KEY.match(line.rstrip("\r\n"))
        if k and k.group(2) == "url" and blocks[i] in ("encoder", "reranker"):
            urls[blocks[i]] = k.group(4)
    changed: list[str] = []
    for i, line in enumerate(lines):
        if blocks[i] not in ("encoder", "reranker"):
            continue
        end = line[len(line.rstrip("\r\n")):]
        k = _KEY.match(line.rstrip("\r\n"))
        if not (k and k.group(2) == "model"):
            continue
        named = model_named_by(urls.get(blocks[i], ""))
        if named and named != k.group(4):
            lines[i] = f'{k.group(1)}model{k.group(3)}"{named}"{k.group(5)}{end}'
            changed.append(f"[{blocks[i]}] model {k.group(4)!r} -> {named!r}")
    return "".join(lines), changed


def main(argv: list[str]) -> int:
    dry = "--dry-run" in argv
    for name in (a for a in argv if a != "--dry-run"):
        p = Path(name)
        text = p.read_bytes().decode("utf-8")
        new, changed = align(text)
        for c in changed:
            print(f"{p}: {c}")
        if changed and not dry:
            p.write_bytes(new.encode("utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
