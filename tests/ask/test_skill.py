"""tests/ask/test_skill.py — Louise as an agent skill (app/ask/skill.py;
operator, 2026-11-03: "Make a skill or whatever works, add it to the
'connect to' section"): a valid Agent Skills SKILL.md, naming only tools
and parameters that exist, served from Connect with the node's address
in it, and the same text as the MCP server's instructions."""
import io
import json
import re
import zipfile

import pytest
from fastapi.testclient import TestClient

from app import config
from app.api import app
from app.ask import mcp, skill


def test_the_skill_is_a_valid_agent_skill():
    """agentskills.io: `name` lowercase letters, digits, hyphens, at most 64
    characters; `description` at most 1024, saying when to use it."""
    fm = skill.front_matter(skill.text())
    assert fm["name"] == skill.NAME and re.fullmatch(r"[a-z0-9-]{1,64}", fm["name"])
    assert 0 < len(fm["description"]) <= 1024 and "Use when" in fm["description"]
    assert set(fm) == {"name", "description"}


def test_the_skill_names_only_tools_and_parameters_that_exist():
    """A skill that names a parameter the server does not take teaches the
    model a refused call."""
    text = skill.text()
    tools = {t["name"]: t["inputSchema"]["properties"] for t in mcp.TOOLS}
    named = set(re.findall(r"\blouise_\w+", text))
    assert named == set(tools), "every tool, and nothing else"
    for tool, args in re.findall(r"`(louise_\w+) (\{.*?\})`", text):
        assert set(json.loads(args)) <= set(tools[tool]), f"{tool}: {args}"
    for param in re.findall(r"`(\w+)` for ", text) + re.findall(r"with `(\w+)` \(", text):
        assert param in tools["louise_search"], param


def test_the_skill_says_what_the_benchmark_showed_the_models_did_not_do():
    """München's five3: the question sent verbatim, once, never again when
    the work was missing; and the model's own knowledge kept out of the
    answer (the operator's fourth measure)."""
    text = skill.text()
    assert '"translate": true' in text and "search again" in text
    assert "title and author" in text and "never into the" in text
    assert "Never cite a record no tool returned" in text


def test_the_mcp_server_s_instructions_are_the_skill():
    init = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    instructions = init["result"]["instructions"]
    assert instructions == skill.body() and not instructions.startswith("---")
    assert instructions.startswith("# Louise") and "louise_search" in instructions


@pytest.fixture
def client(isolated_config, monkeypatch):
    from tests.test_web_pages import _patch_hash, _patch_health_pings
    _patch_hash(monkeypatch)
    _patch_health_pings(monkeypatch)
    monkeypatch.setattr(config, "ADMIN_TOKEN", "sekrit")
    with TestClient(app) as c:
        yield c


def test_connect_offers_the_skill_with_this_nodes_address(client):
    page = client.get("/connect", headers={"host": "louise.lan:30042"})
    assert 'href="/connect/louise-skill.md"' in page.text and 'href="/connect/louise-skill.zip"' in page.text
    assert "~/.claude/skills/louise/SKILL.md" in page.text
    md = client.get("/connect/louise-skill.md", headers={"host": "louise.lan:30042"})
    assert md.status_code == 200 and md.headers["content-type"].startswith("text/markdown")
    assert "http://louise.lan:30042/api/ask/mcp" in md.text and "{{base}}" not in md.text
    assert "sekrit" not in md.text, "no key in a file that travels"
    z = client.get("/connect/louise-skill.zip", headers={"host": "louise.lan:30042"})
    with zipfile.ZipFile(io.BytesIO(z.content)) as f:
        assert f.namelist() == ["louise/SKILL.md"]
        assert f.read("louise/SKILL.md").decode() == md.text
