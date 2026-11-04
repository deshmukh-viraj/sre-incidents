"""
tools/dispatcher.py
-------------------
Tool Librarian (TASK A) dispatch surface: catalog_search / explain_tool / run_tool.
Pure python, zero LLM.

run_tool is THE one place tool timeouts and result row caps are enforced
(single enforcement site, see _cap_rows and the timeout handling below).
Read-only enforcement: non-read tools are rejected unless the caller passes
allow_write=True (reserved for governed paths).
"""

import concurrent.futures
from typing import Any, Dict, List, Optional

from src.tools.registry import REGISTRY, ToolSpec


def catalog_search(query: str, limit: int = 5) -> List[ToolSpec]:
    """keyword search over the registry (name + description)."""
    q = (query or "").lower()
    words = [w for w in q.replace(",", " ").split() if len(w) > 2]
    scored = []
    for spec in REGISTRY.values():
        hay = f"{spec.name} {spec.description}".lower()
        score = sum(1 for w in words if w in hay)
        if score:
            scored.append((score, spec.name, spec))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [s for _, _, s in scored[:limit]]


def explain_tool(name: str) -> Optional[Dict[str, Any]]:
    """full metadata for one registered tool (for prompts / humans)."""
    spec = REGISTRY.get(name)
    if spec is None:
        return None
    return {
        "name": spec.name,
        "description": spec.description,
        "read_only": spec.read_only,
        "timeout_s": spec.timeout_s,
        "row_cap": spec.row_cap,
    }


def _cap_rows(result: Any, cap: int) -> Any:
    """THE row-cap enforcement site: truncates oversized tool output."""
    if isinstance(result, str):
        lines = result.splitlines()
        if len(lines) > cap:
            return "\n".join(lines[:cap]) + \
                f"\n... [truncated {len(lines) - cap} lines (row_cap={cap})]"
        return result
    if isinstance(result, (list, tuple)):
        if len(result) > cap:
            return list(result[:cap]) + \
                [f"... [truncated {len(result) - cap} rows (row_cap={cap})]"]
        return list(result)
    return result


def run_tool(
    name: str,
    args: Optional[Dict[str, Any]] = None,
    *,
    allow_write: bool = False,
    timeout_s: Optional[float] = None,
    row_cap: Optional[int] = None,
) -> Any:
    """dispatch one registered tool.

    - read-only enforcement: write tools rejected unless allow_write=True
    - per-tool timeout + row cap enforced HERE (single site)
    - never raises: failures return 'ERROR: ...' strings (tool-protocol safe)
    """
    args = dict(args or {})
    spec = REGISTRY.get(name)
    if spec is None:
        return f"ERROR: unknown tool '{name}'. Known: {sorted(REGISTRY)}"
    if not spec.read_only and not allow_write:
        return (f"ERROR: tool '{name}' is not read-only. run_tool only dispatches "
                f"read tools (allow_write=True required, governed paths only).")
    timeout = spec.timeout_s if timeout_s is None else timeout_s
    cap = spec.row_cap if row_cap is None else row_cap
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        future = pool.submit(spec.callable, **args)
        result = future.result(timeout=timeout)
    except concurrent.futures.TimeoutError:
        # ponytail: the runaway thread keeps running (threads are unkillable);
        # its own socket timeouts bound the leak. Upgrade path: killable
        # subprocess dispatch per tool.
        return f"ERROR: tool '{name}' timed out after {timeout}s"
    except TypeError as e:
        return f"ERROR: tool '{name}' bad args: {e}"
    except Exception as e:
        return f"ERROR: tool '{name}' failed: {e}"
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return _cap_rows(result, cap)
