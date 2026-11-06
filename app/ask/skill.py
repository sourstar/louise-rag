"""Louise as an AGENT SKILL (operator, 2026-11-03: "Make a skill or
whatever works, add it to the 'connect to' section"): how a model drives
the three tools of 05 §5.4, in one file, SKILL.md beside this module.

The format is the Agent Skills standard (agentskills.io; Anthropic,
2025): a folder with a SKILL.md, whose YAML front matter names the skill
(`name`, lowercase letters, digits and hyphens, at most 64 characters)
and says when to use it (`description`, at most 1024 characters). Claude
Code reads it from `~/.claude/skills/louise/SKILL.md`; the Claude apps
take the folder as a zip.

"Whatever works": the same instructions reach a client that installs no
skill, as the MCP server's `instructions` (MCP 2025-06-18, `initialize`),
which a client may add to its model's prompt (app/ask/mcp.py). One text,
two ways in. Ask's own loop (app/ask/chat.py) searches before its model
answers and needs no skill.

What the skill says was measured, not guessed: on the node's chat
benchmark (München, five3, 2026-11-03) the model sent the question
verbatim, once, and never searched again when the work was missing.
"""
from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path

PATH = Path(__file__).with_name("SKILL.md")
NAME = "louise"
#: where the node's own address goes in the text
BASE = "{{base}}"


def text(base: str | None = None) -> str:
    """SKILL.md, the node's address filled in (CLAUDE.md RULE 2: nobody
    types an address); without one, a placeholder the reader can see."""
    return PATH.read_text(encoding="utf-8").replace(BASE, base or "<this node's address>")


def front_matter(skill: str) -> dict[str, str]:
    """The `key: value` lines between the opening `---` and the next one."""
    m = re.match(r"---\n(.*?)\n---\n", skill, re.DOTALL)
    if not m:
        return {}
    out = {}
    for line in m.group(1).splitlines():
        key, sep, value = line.partition(":")
        if sep:
            out[key.strip()] = value.strip()
    return out


def body(base: str | None = None) -> str:
    """The instructions without the front matter: MCP's `instructions`."""
    return re.sub(r"\A---\n.*?\n---\n+", "", text(base), flags=re.DOTALL).strip()


def zip_bytes(base: str | None = None) -> bytes:
    """The skill as the Claude apps take it: `louise/SKILL.md` in a zip."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(f"{NAME}/SKILL.md", text(base))
    return buf.getvalue()
