"""`ask serve` (07 §7.3; 05 §5.4) — the three tools over the Model Context
Protocol: `louise_search`, `louise_lookup`, `louise_traverse`. Migration
step 11.

MCP is JSON-RPC 2.0 with a small vocabulary — `initialize`,
`notifications/initialized`, `ping`, `tools/list`, `tools/call` — so this
module speaks it directly, over stdio (one JSON object per line, as the
protocol's stdio transport frames it) and as a request handler the web
mounts (`POST /api/ask/mcp`). No client library and no server library:
the descriptions below are the routing (05 §5.4), the schemas are part of
the platform image, and a dependency would be one more thing to pin for
three functions.
"""
from __future__ import annotations

import json
import sys
from typing import Any, Callable

from app.hold.codex import CodexError

PROTOCOL = "2025-06-18"
SERVER = {"name": "louise", "version": "0.1"}

TOOLS: list[dict[str, Any]] = [
    {"name": "louise_search",
     "description": "Semantic and exact-term search over the collections this peer holds: one "
                    "fused list per space (05 §5.1), each result naming its collection, snapshot, "
                    "record, chunk, trust level and producer (05 §5.3); facts from reference "
                    "sources beside the lists, never in them.",
     "inputSchema": {"type": "object", "required": ["query"], "properties": {
         "query": {"type": "string"},
         "collections": {"type": "array", "items": {"type": "string"},
                         "description": "collection ids, id prefixes or local names; default: every "
                                        "collection held and usable"},
         "space": {"type": "string", "description": "a space_id: only the collections in it"},
         "annotations": {"type": "array", "items": {"type": "string"},
                         "description": "the annotations collections consulted, by local name: their work "
                                        "claims group a work's editions into one hit and their claims come "
                                        "beside the hits (05 §5.3); default: every one held, [] none"},
         "filters": {"type": "object", "description": "{facet field: value | {not: value}}"},
         "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 10},
         "rerank_depth": {"type": "integer", "minimum": 1, "maximum": 300, "default": 50},
         "as_of": {"type": "string", "description": "YYYY-MM-DD: the snapshot current at that date"},
         "freshness": {"type": "boolean", "default": False},
         "translate": {"type": "boolean", "default": False},
         "all_languages": {"type": "boolean", "default": False}}}},
    {"name": "louise_lookup",
     "description": "A fact by key from a reference source: one canonical id "
                    "(<namespace>:<id>, e.g. hgnc:1101), the row that owns it, every column, "
                    "and the source's version. Never ranked; an id with no row is unresolved.",
     "inputSchema": {"type": "object", "required": ["id"], "properties": {"id": {"type": "string"}}}},
    {"name": "louise_traverse",
     "description": "The records reached from a record's uid or a canonical id along the "
                    "declared relations (05 §5.5), in both directions, `depth` hops deep; an "
                    "edge into another collection is resolved only against the collections named in "
                    "`resolve_in`, and otherwise reported unresolved, naming what it needs.",
     "inputSchema": {"type": "object", "required": ["id"], "properties": {
         "id": {"type": "string"},
         "relation": {"type": "string", "description": "one relation type, e.g. eli:amends"},
         "depth": {"type": "integer", "minimum": 1, "maximum": 6, "default": 1},
         "collection": {"type": "string", "description": "the collection whose edges to follow"},
         "resolve_in": {"type": "array", "items": {"type": "string"},
                        "description": "collections to resolve edges into other collections against"}}}},
]


def _call_tool(name: str, args: dict[str, Any]) -> Any:
    from app.ask import lookup, results, traverse
    if name == "louise_search":
        return results.search(args["query"], collections=args.get("collections"), space=args.get("space"),
                              filters=args.get("filters"), limit=int(args.get("limit") or results.DEFAULT_LIMIT),
                              rerank_depth=int(args.get("rerank_depth") or results.DEFAULT_DEPTH),
                              as_of=args.get("as_of"), freshness=bool(args.get("freshness")),
                              translate=bool(args.get("translate")), all_languages=bool(args.get("all_languages")),
                              annotations=args.get("annotations"))
    if name == "louise_lookup":
        return lookup.lookup(str(args["id"]))
    if name == "louise_traverse":
        return traverse.traverse(str(args["id"]), relation=args.get("relation"),
                                 depth=int(args.get("depth") or 1), collection=args.get("collection"),
                                 resolve_in=args.get("resolve_in"))
    raise KeyError(name)


def handle(message: dict[str, Any], *, call: Callable[[str, dict[str, Any]], Any] = _call_tool
           ) -> dict[str, Any] | None:
    """One JSON-RPC request → its response, or None for a notification."""
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return _error(None, -32600, "a JSON-RPC 2.0 request")
    method, rid, params = message.get("method"), message.get("id"), message.get("params") or {}
    if method is None:
        return _error(rid, -32600, "a request names a method")
    if rid is None and method.startswith("notifications/"):
        return None
    if method == "initialize":
        return _result(rid, {"protocolVersion": PROTOCOL, "capabilities": {"tools": {"listChanged": False}},
                             "serverInfo": SERVER,
                             "instructions": "Three tools: search (ranked lists per space), lookup (a fact "
                                             "by key), traverse (declared relations). Facts are placed "
                                             "beside lists, never ranked (05 §5.1)."})
    if method == "ping":
        return _result(rid, {})
    if method == "tools/list":
        return _result(rid, {"tools": TOOLS})
    if method == "tools/call":
        name, args = params.get("name"), params.get("arguments") or {}
        if not isinstance(args, dict):
            return _error(rid, -32602, "arguments is an object")
        try:
            out = call(str(name), args)
        except KeyError:
            return _error(rid, -32602, f"unknown tool {name!r}; tools/list names three")
        except CodexError as e:
            return _result(rid, {"content": [{"type": "text", "text": str(e)}], "isError": True,
                                 "exit": getattr(e, "exit_code", 2)})
        except Exception as e:   # noqa: BLE001 — a failure is an answer, not a dropped connection
            return _result(rid, {"content": [{"type": "text", "text": f"{type(e).__name__}: {e}"}],
                                 "isError": True})
        return _result(rid, {"content": [{"type": "text", "text": json.dumps(out, ensure_ascii=False,
                                                                              sort_keys=True)}],
                             "structuredContent": out, "isError": False})
    return _error(rid, -32601, f"method {method!r} is not one this server speaks")


def _result(rid, result) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _error(rid, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def serve_stdio(inp=None, out=None, *, call: Callable[[str, dict[str, Any]], Any] = _call_tool) -> int:
    """`ask serve`: one JSON object per line in, one per line out, until
    the input ends. Needs nothing but what is held (07 §7.3)."""
    inp = inp or sys.stdin
    out = out or sys.stdout
    for line in inp:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            response = _error(None, -32700, "not JSON")
        else:
            response = handle(message, call=call)
        if response is not None:
            out.write(json.dumps(response, ensure_ascii=False) + "\n")
            out.flush()
    return 0
