"""Tool Librarian (P0/TASK A) unit tests. Pure python, no network, no LLM."""
import concurrent.futures
import time
from types import SimpleNamespace

from src.tools import dispatcher
from src.tools.registry import REGISTRY, ToolSpec, build_registry


def _fake_tool(name, description, read_only):
    return SimpleNamespace(name=name, description=description, func=lambda **kw: None)


def _seed_fake_registry(monkeypatch):
    """swap in a fake registry so tests never touch the network."""
    slow = ToolSpec(name="slow_tool", description="sleeps", callable=lambda: time.sleep(0.5),
                    read_only=True, timeout_s=0.1, row_cap=40)
    big = ToolSpec(name="big_tool", description="big output",
                   callable=lambda: list(range(100)), read_only=True, timeout_s=5, row_cap=40)
    long_str = ToolSpec(name="str_tool", description="long string",
                        callable=lambda: "\n".join(f"line{i}" for i in range(100)),
                        read_only=True, timeout_s=5, row_cap=40)
    writer = ToolSpec(name="write_tool", description="mutates state",
                      callable=lambda: "wrote", read_only=False, timeout_s=5, row_cap=40)
    fake = {s.name: s for s in (slow, big, long_str, writer)}
    monkeypatch.setattr(dispatcher, "REGISTRY", fake)
    return fake


def test_registry_seeded_from_agent_tool_lists():
    names = set(REGISTRY)
    assert "query_prometheus_metric" in names
    assert "propose_remediation" in names
    assert REGISTRY["query_prometheus_metric"].read_only is True
    assert REGISTRY["propose_remediation"].read_only is False


def test_registry_read_flags_match_read_tools():
    for name, spec in REGISTRY.items():
        assert spec.read_only == any(t.name == name for t in
                                     __import__("src.tools.agent_tool", fromlist=["READ_TOOLS"]).READ_TOOLS)


def test_catalog_search_finds_prometheus_tool():
    hits = [s.name for s in dispatcher.catalog_search("prometheus latency query")]
    assert "query_prometheus_metric" in hits
    assert dispatcher.catalog_search("zzzz-no-match") == []


def test_explain_tool():
    meta = dispatcher.explain_tool("query_prometheus_metric")
    assert meta["read_only"] is True and meta["row_cap"] > 0
    assert dispatcher.explain_tool("no_such_tool") is None


def test_run_tool_unknown_and_write_enforcement(monkeypatch):
    _seed_fake_registry(monkeypatch)
    out = dispatcher.run_tool("nope")
    assert out.startswith("ERROR: unknown tool")
    out = dispatcher.run_tool("write_tool")
    assert out.startswith("ERROR: tool 'write_tool' is not read-only")


def test_run_tool_timeout_enforced(monkeypatch):
    _seed_fake_registry(monkeypatch)
    t0 = time.time()
    out = dispatcher.run_tool("slow_tool")
    assert "timed out after 0.1s" in out
    assert time.time() - t0 < 0.5


def test_run_tool_row_caps_enforced(monkeypatch):
    _seed_fake_registry(monkeypatch)
    out = dispatcher.run_tool("big_tool")
    assert len(out) == 41 and "truncated 60 rows" in out[-1]
    out = dispatcher.run_tool("str_tool")
    assert out.count("\n") == 40 and "truncated 60 lines" in out


def test_run_tool_passthrough_and_bad_args(monkeypatch):
    small = ToolSpec(name="small_tool", description="small", callable=lambda n: list(range(n)),
                     read_only=True, timeout_s=5, row_cap=40)
    monkeypatch.setitem(dispatcher.REGISTRY, "small_tool", small)
    assert dispatcher.run_tool("small_tool", {"n": 3}) == [0, 1, 2]  # within cap, untouched
    assert dispatcher.run_tool("small_tool", {"bad": 1}).startswith("ERROR: tool 'small_tool' bad args")
