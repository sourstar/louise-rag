"""Every command and every long flag of `louise` is in docs/07-commands.md
(handover, 2026-10-25: 14 commands and flags had drifted out of it). A
command or flag added to the tree without a line in 07 fails here."""
from __future__ import annotations

import argparse
import re
from pathlib import Path

from app import louise

SPEC = (Path(__file__).resolve().parents[1] / "docs" / "07-commands.md").read_text(encoding="utf-8")

#: Flags 07 §7.7 covers for every command, or that are not for users.
COMMON = {"--help", "--root", "--json", "--dry-run", "--yes"}
NOT_FOR_USERS = {"--shard-records"}         # records build: tests override slicing.shard_records


def _leaves(parser: argparse.ArgumentParser, path: tuple[str, ...] = ()):
    subs = next((a for a in parser._actions if isinstance(a, argparse._SubParsersAction)), None)
    if subs is None:
        yield path, parser
        return
    seen: set[int] = set()
    for name, sp in subs.choices.items():
        if id(sp) not in seen:
            seen.add(id(sp))
            yield from _leaves(sp, path + (name,))


def test_every_command_is_in_07():
    missing = [" ".join(p) for p, _ in _leaves(louise.build_parser())
               if not re.search(r"(?<![\w-])" + re.escape(p[-1]) + r"(?![\w-])", SPEC)]
    assert not missing, f"commands with no line in docs/07-commands.md: {missing}"


def test_every_long_flag_is_in_07():
    missing = sorted({f"{' '.join(p)} {o}" for p, sp in _leaves(louise.build_parser())
                      for a in sp._actions for o in a.option_strings
                      if o.startswith("--") and o not in COMMON | NOT_FOR_USERS
                      and not re.search(re.escape(o) + r"(?![\w-])", SPEC)})
    assert not missing, f"flags with no mention in docs/07-commands.md: {missing}"
