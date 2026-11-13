"""
tools/registry.py
-----------------
one ToolSpec per agent tool, seeded from the READ_TOOLS / ALL_TOOLS lists
in agent_tool.py. No LLM involved.

The registry holds the tool metadata every agent sees (name, description,
read_only, timeout, row_cap). dispatcher.py is the one place that enforces
timeouts and row caps.
"""

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from src.tools.agent_tool import ALL_TOOLS, READ_TOOLS


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    callable: Callable
    read_only: bool
    timeout_s: float = 10.0
    row_cap: int = 40


def _callable_of(tool: Any) -> Optional[Callable]:
    fn = getattr(tool, "func", None)
    if fn is None:
        fn = getattr(tool, "coroutine", None)
    return fn


def build_registry(tools: List[Any] = None) -> Dict[str, ToolSpec]:
    """build ToolSpecs from langchain @tool objects (or anything with
    .name/.description/.func). read_only is derived from the READ_TOOLS list,
    so the registry can never disagree with the live tool file."""
    tools = tools if tools is not None else ALL_TOOLS
    read_only_names = {t.name for t in READ_TOOLS}
    specs: Dict[str, ToolSpec] = {}
    for t in tools:
        specs[t.name] = ToolSpec(
            name=t.name,
            description=getattr(t, "description", "") or "",
            callable=_callable_of(t),
            read_only=t.name in read_only_names,
        )
    return specs


REGISTRY: Dict[str, ToolSpec] = build_registry()
