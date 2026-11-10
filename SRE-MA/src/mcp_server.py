"""
mcp_server.py — SRE-MA capability layer as an MCP server (Phase 4 / Bet 4).

Exposes the READ-ONLY investigation capabilities as MCP tools so any MCP-capable
agent (Hermes, Claude Code, DeepSeek Harness, IDEs) inherits the SRE brain.
WRITE capabilities are deliberately NOT exposed: the write path stays inside
SRE-MA's policy validator + human gate (dynamic evidence, static action).

Minimal Model Context Protocol implementation over stdio, newline-delimited
JSON-RPC 2.0 — zero dependencies beyond the repo itself. Run:

    .venv/bin/python -m src.mcp_server

Test with any MCP client, or by piping newline-delimited JSON-RPC on stdin.
"""

import json
import sys
from typing import Any, Dict

PROTOCOL_VERSION = "2024-11-05"


def _tool_defs() -> list:
    """the read-only capability surface, straight from the live tool module."""
    from src.tools.agent_tool import READ_TOOLS
    return [
        {
            "name": t.name,
            "description": (t.description or "")[:500],
            "inputSchema": _schema_of(t),
        }
        for t in READ_TOOLS
    ]


def _schema_of(t) -> dict:
    """derive a JSON schema from the langchain tool's args (pydantic)."""
    try:
        schema = t.args_schema.model_json_schema() if getattr(t, "args_schema", None) else {}
    except Exception:
        schema = {}
    schema.setdefault("type", "object")
    return schema


def _call_tool(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    from src.tools.agent_tool import READ_TOOLS, reset_investigation_cache
    tool = next((t for t in READ_TOOLS if t.name == name), None)
    if tool is None:
        return {"content": [{"type": "text", "text": f"unknown tool: {name}"}], "isError": True}
    try:
        reset_investigation_cache()  # each MCP call is its own investigation scope
        result = tool.invoke(args or {})
        return {"content": [{"type": "text", "text": str(result)}], "isError": False}
    except Exception as e:
        return {"content": [{"type": "text", "text": f"tool error: {e}"}], "isError": True}


def handle(request: Dict[str, Any]) -> Dict[str, Any]:
    """pure JSON-RPC handler (no stdio) — unit-testable without a subprocess."""
    method = request.get("method", "")
    rid = request.get("id")
    if method == "initialize":
        return {
            "jsonrpc": "2.0", "id": rid,
            "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "sre-ma-capabilities", "version": "1.0.0"},
            },
        }
    if method == "notifications/initialized":
        return {}  # notification: no response
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": _tool_defs()}}
    if method == "tools/call":
        params = request.get("params") or {}
        return {"jsonrpc": "2.0", "id": rid,
                "result": _call_tool(params.get("name", ""), params.get("arguments") or {})}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": rid, "result": {}}
    if rid is None:
        return {}
    return {"jsonrpc": "2.0", "id": rid,
            "error": {"code": -32601, "message": f"method not found: {method}"}}


def serve() -> None:
    """newline-delimited JSON-RPC over stdio. Ctrl-C / EOF exits."""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except Exception:
            print(json.dumps({"jsonrpc": "2.0", "id": None,
                              "error": {"code": -32700, "message": "parse error"}}), flush=True)
            continue
        response = handle(request)
        if response:
            print(json.dumps(response), flush=True)


if __name__ == "__main__":
    serve()
