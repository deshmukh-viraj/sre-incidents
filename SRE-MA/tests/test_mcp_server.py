"""self-checks for the MCP capability server (Phase 4 / Bet 4)."""

from src import mcp_server


def test_initialize_handshake():
    out = mcp_server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert out["result"]["serverInfo"]["name"] == "sre-ma-capabilities"
    assert out["result"]["protocolVersion"] == mcp_server.PROTOCOL_VERSION


def test_tools_list_exposes_only_read_capabilities():
    from src.tools.agent_tool import READ_TOOLS
    out = mcp_server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    names = {t["name"] for t in out["result"]["tools"]}
    assert {"query_prometheus_metric", "query_topology", "query_traces"} <= names
    write_names = {"propose_remediation", "escalate_with_reason"}
    assert names.isdisjoint(write_names), "write tools must never be MCP-exposed"
    assert names == {t.name for t in READ_TOOLS}


def test_tool_call_happy_path_and_unknown():
    out = mcp_server.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                             "params": {"name": "nope", "arguments": {}}})
    assert out["result"]["isError"] is True
    out2 = mcp_server.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                              "params": {"name": "get_recent_changes", "arguments": {"service": "payment_gateway"}}})
    assert out2["result"]["isError"] is False
    assert "signal unavailable" in out2["result"]["content"][0]["text"]  # honest degradation offline


def test_notifications_get_no_response():
    assert mcp_server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) == {}
