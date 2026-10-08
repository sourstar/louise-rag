"""Rewrite collection files to 07 §7.8's model keys (operator, 2026-10-08).

    python -m tools.migrate_model_keys /config/collections/*.toml
    python -m tools.migrate_model_keys --dry-run /config/collections

`[encoder]` lost `model` and names both sides: `embed_model` (documents) and
`query_model` (questions). `[reranker]` names `rerank_model`. There is no
alias, so a file still in the old shape is refused by the node for that
collection (app/collections.py, `ModelKeys`) until this has run on it.

PER FILE, IN PLACE, LINE BY LINE — no TOML round-trip, so every comment and
the layout stay as written:

- `[encoder]` `model = X` becomes `embed_model = X` (the line's value and
  comment kept), and `query_model = <the model query_url names, or X>`
  follows it. With no `model` line, both come from the URLs: `url`'s model
  (`…/upstream/<model>`), and `query_url`'s, else `url`'s.
- `[reranker]` `model = X` becomes `rerank_model = X`; with none,
  `rerank_model = <the model its url names>` is added.

A file is REFUSED — reported, left byte for byte as it was — when a value
cannot be determined (no `model` and a URL that names none; a separate
`query_url` that names no model, where X would be the DOCUMENT model sent
to the query endpoint), or when a stated model disagrees with the model its
model-container URL names (the node would refuse the result). The rewrite is
read back with tomllib before it is written. Exit 0 when nothing was
refused, 1 otherwise; a refused file never stops the others.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tomllib
from pathlib import Path

ENCODER_TABLES = ("encoder", "embed_recipe")   # the loader reads either name
RERANKER_TABLE = "reranker"

_HEADER = re.compile(r"^\s*\[(?!\[)\s*([^\]]+?)\s*\]\s*(#.*)?$")
_ARRAY_HEADER = re.compile(r"^\s*\[\[")
_KEY = re.compile(r"^(\s*)([A-Za-z0-9_-]+)(\s*=)")


class Refused(Exception):
    """This file cannot be migrated as it stands; it is left unchanged."""


def model_named_by(url: str | None) -> str | None:
    """app.collections.model_named_by, restated so the tool runs without the
    app's dependencies: `http://<node>:<port>/upstream/<model>[/…]`."""
    if not url:
        return None
    _, sep, tail = str(url).partition("/upstream/")
    model = tail.split("/", 1)[0] if sep else ""
    return model or None


def _quote(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)   # a TOML basic string


def _sections(lines: list[str]) -> dict[str, tuple[int, int]]:
    """Each top-level `[table]`'s (header line, end line exclusive)."""
    out: dict[str, tuple[int, int]] = {}
    current, start = None, 0
    for i, line in enumerate(lines):
        header = _HEADER.match(line)
        if header or _ARRAY_HEADER.match(line):
            if current is not None:
                out.setdefault(current, (start, i))
            current = header.group(1).strip().strip('"') if header else None
            start = i
    if current is not None:
        out.setdefault(current, (start, len(lines)))
    return out


def _key_line(lines: list[str], span: tuple[int, int], key: str) -> int | None:
    for i in range(span[0] + 1, span[1]):
        m = _KEY.match(lines[i])
        if m and m.group(2) == key:
            return i
    return None


def _insert_at(lines: list[str], span: tuple[int, int]) -> int:
    """After the section's last line that is neither blank nor a comment."""
    at = span[0] + 1
    for i in range(span[0] + 1, span[1]):
        s = lines[i].strip()
        if s and not s.startswith("#"):
            at = i + 1
    return at


def _rename(line: str, new: str) -> str:
    m = _KEY.match(line)
    return f"{m.group(1)}{new}{m.group(3)}{line[m.end():]}"


def _agree(where: str, key: str, value: str, url: str | None, url_key: str) -> None:
    named = model_named_by(url)
    if named is not None and named != value:
        raise Refused(f"{where}: {key} would be {value!r} and {url_key} {url} names "
                      f"{named!r}; the node refuses a key that disagrees with its "
                      f"model-container URL — settle which model is meant by hand")


def migrate_text(text: str, where: str = "file") -> str:
    """The file's new text (unchanged when already migrated); Refused when a
    value cannot be determined."""
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise Refused(f"{where}: not a TOML file: {e}") from None
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith(("\n", "\r")):
        lines[-1] += newline
        added_final_newline = True
    else:
        added_final_newline = False
    want: dict[str, dict[str, str]] = {}

    # edits are collected as (line index, replacement lines) and applied
    # bottom-up so earlier indices stay valid
    edits: list[tuple[int, int, list[str]]] = []

    for table in ENCODER_TABLES:
        enc = raw.get(table)
        if not isinstance(enc, dict):
            continue
        spans = _sections(lines)
        if table not in spans:
            raise Refused(f"{where}: [{table}] is not written as a `[{table}]` header; "
                          f"edit it by hand")
        span = spans[table]
        url, query_url = enc.get("url"), enc.get("query_url")
        has_embed, has_query = "embed_model" in enc, "query_model" in enc
        if has_embed and has_query and "model" not in enc:
            continue                                    # already migrated
        if "model" in enc and (has_embed or has_query):
            raise Refused(f"{where}: [{table}] has `model` beside "
                          f"`embed_model`/`query_model`; settle it by hand")
        if "model" in enc:
            embed = str(enc["model"])
        elif has_embed:
            embed = str(enc["embed_model"])
        else:
            embed = model_named_by(url)
            if not embed:
                raise Refused(f"{where}: [{table}] names no `model` and its url {url} "
                              f"names none (…/upstream/<model>): embed_model cannot be "
                              f"determined")
        if has_query:
            query = str(enc["query_model"])
        elif query_url:
            query = model_named_by(query_url)
            if not query:
                raise Refused(f"{where}: [{table}] query_url {query_url} names no model: "
                              f"query_model cannot be determined, and the document "
                              f"model {embed!r} is not the query side's")
        else:
            query = embed
        _agree(where, "embed_model", embed, url, "url")
        _agree(where, "query_model", query, query_url or url,
               "query_url" if query_url else "url")
        want[table] = {"embed_model": embed, "query_model": query}
        model_at = _key_line(lines, span, "model")
        if "model" in enc:
            if model_at is None:
                raise Refused(f"{where}: [{table}] `model` is not on a line of its own "
                              f"in the table; edit it by hand")
            indent = _KEY.match(lines[model_at]).group(1)
            edits.append((model_at, model_at + 1, [
                _rename(lines[model_at], "embed_model"),
                f"{indent}query_model = {_quote(query)}{newline}"]))
        else:
            at = _insert_at(lines, span)
            add = []
            if not has_embed:
                add.append(f"embed_model = {_quote(embed)}{newline}")
            if not has_query:
                add.append(f"query_model = {_quote(query)}{newline}")
            edits.append((at, at, add))

    rr = raw.get(RERANKER_TABLE)
    if isinstance(rr, dict) and "rerank_model" not in rr:
        spans = _sections(lines)
        if RERANKER_TABLE not in spans:
            raise Refused(f"{where}: [reranker] is not written as a `[reranker]` header; "
                          f"edit it by hand")
        span = spans[RERANKER_TABLE]
        url = rr.get("url")
        if "model" in rr:
            model = str(rr["model"])
            _agree(where, "rerank_model", model, url, "[reranker] url")
            at = _key_line(lines, span, "model")
            if at is None:
                raise Refused(f"{where}: [reranker] `model` is not on a line of its own; "
                              f"edit it by hand")
            edits.append((at, at + 1, [_rename(lines[at], "rerank_model")]))
        else:
            model = model_named_by(url)
            if not model:
                raise Refused(f"{where}: [reranker] url {url} names no model "
                              f"(…/upstream/<model>): rerank_model cannot be determined")
            at = _insert_at(lines, span)
            edits.append((at, at, [f"rerank_model = {_quote(model)}{newline}"]))
        want[RERANKER_TABLE] = {"rerank_model": model}

    if not edits:
        return text
    for start, end, new in sorted(edits, key=lambda e: e[0], reverse=True):
        lines[start:end] = new
    out = "".join(lines)
    if added_final_newline:
        out = out[:-len(newline)]
    # read back: the rewrite says exactly what was meant, and nothing else moved
    after = tomllib.loads(out)
    for table, keys in want.items():
        got = after.get(table) or {}
        if "model" in got or any(got.get(k) != v for k, v in keys.items()):
            raise Refused(f"{where}: the rewrite of [{table}] did not read back as "
                          f"{keys}; nothing written")
    for key in raw:
        if key not in want and after.get(key) != raw[key]:
            raise Refused(f"{where}: the rewrite moved `{key}`; nothing written")
    return out


def migrate_file(path: Path, *, dry_run: bool = False) -> bool:
    """True when the file was (or, dry, would be) rewritten."""
    data = path.read_bytes()
    text = data.decode("utf-8")
    new = migrate_text(text, str(path))
    if new == text:
        return False
    if not dry_run:
        tmp = path.with_name(path.name + ".migrating")
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            f.write(new)
        os.replace(tmp, path)
    return True


def _files(args: list[str]) -> list[Path]:
    out: list[Path] = []
    for a in args:
        p = Path(a)
        out.extend(sorted(p.glob("*.toml")) if p.is_dir() else [p])
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="migrate_model_keys", description=__doc__.split("\n")[0])
    p.add_argument("paths", nargs="+", help="collection files, or directories of them")
    p.add_argument("--dry-run", action="store_true", help="say what would change; write nothing")
    a = p.parse_args(argv)
    refused = 0
    for path in _files(a.paths):
        try:
            changed = migrate_file(path, dry_run=a.dry_run)
        except Refused as e:
            refused += 1
            print(f"refused: {e}", file=sys.stderr)
            continue
        except (OSError, UnicodeDecodeError) as e:
            refused += 1
            print(f"refused: {path}: {e}", file=sys.stderr)
            continue
        verb = ("would migrate" if a.dry_run else "migrated") if changed else "unchanged"
        print(f"{verb}: {path}")
    return 1 if refused else 0


if __name__ == "__main__":
    sys.exit(main())
