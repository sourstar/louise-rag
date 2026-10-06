"""tests/ask/test_mcp.py — the MCP server (05 §5.4; 07 §7.3 `ask serve`):
initialize, the three tools listed with their schemas, a call answered
with the tool's object, a tool's refusal an error result with the exit
code, a notification unanswered, stdio framing."""
import io
import json

from app.ask import mcp, results


def rpc(method, params=None, rid=1):
    return {"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}


def test_initialize_lists_three_tools_and_pings():
    init = mcp.handle(rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}}))
    assert init["result"]["protocolVersion"] == mcp.PROTOCOL and init["result"]["serverInfo"]["name"] == "louise"
    assert mcp.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    tools = mcp.handle(rpc("tools/list"))["result"]["tools"]
    assert [t["name"] for t in tools] == ["louise_search", "louise_lookup", "louise_traverse"]
    assert all({"name", "description", "inputSchema"} <= set(t) for t in tools)
    assert tools[0]["inputSchema"]["required"] == ["query"] and tools[1]["inputSchema"]["required"] == ["id"]
    assert mcp.handle(rpc("ping"))["result"] == {}
    assert mcp.handle(rpc("nothing/here"))["error"]["code"] == -32601
    assert mcp.handle({"id": 1, "method": "ping"})["error"]["code"] == -32600


def test_a_call_answers_the_tool_s_object_and_a_refusal_is_an_error_result():
    calls = []

    def call(name, args):
        calls.append((name, args))
        if name == "louise_lookup":
            return {"facts": [], "unresolved": [args["id"]]}
        if name == "louise_search":
            raise results.NotYet("as_of: not built yet")
        raise KeyError(name)
    out = mcp.handle(rpc("tools/call", {"name": "louise_lookup", "arguments": {"id": "hgnc:1"}}), call=call)
    assert out["result"]["isError"] is False
    assert out["result"]["structuredContent"] == {"facts": [], "unresolved": ["hgnc:1"]}
    assert json.loads(out["result"]["content"][0]["text"]) == out["result"]["structuredContent"]
    refused = mcp.handle(rpc("tools/call", {"name": "louise_search", "arguments": {"query": "q", "as_of": "x"}}), call=call)
    assert refused["result"]["isError"] is True and refused["result"]["exit"] == 2
    assert "not built yet" in refused["result"]["content"][0]["text"]
    assert mcp.handle(rpc("tools/call", {"name": "louise_nothing", "arguments": {}}), call=call)["error"]["code"] == -32602
    assert mcp.handle(rpc("tools/call", {"name": "louise_lookup", "arguments": "x"}), call=call)["error"]["code"] == -32602
    assert calls == [("louise_lookup", {"id": "hgnc:1"}), ("louise_search", {"query": "q", "as_of": "x"}),
                     ("louise_nothing", {})], "the server asks the tools, and an unknown one is their refusal"


def test_stdio_frames_one_object_per_line_and_skips_notifications():
    inp = io.StringIO("\n".join([
        json.dumps(rpc("initialize", rid=1)), "", json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
        "not json", json.dumps(rpc("tools/list", rid=2))]) + "\n")
    out = io.StringIO()
    assert mcp.serve_stdio(inp, out, call=lambda n, a: {}) == 0
    lines = [json.loads(l) for l in out.getvalue().splitlines()]
    assert [l.get("id") for l in lines] == [1, None, 2]
    assert lines[1]["error"]["code"] == -32700 and len(lines[2]["result"]["tools"]) == 3
