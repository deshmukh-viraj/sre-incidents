"""
graph/budget.py
---------------
global budget middleware: wraps every reasoning worker so an incident can never
loop unbounded through LLM workers. budget limits ride in per-incident state
(budget.max_iterations_total / max_tokens_total / spent_iterations); 0 limits =
unlimited. once exhausted, the guarded worker is skipped (returns
{"budget_exhausted": True} without running) and the graph degrades to
escalation through the routers, which all check budget_exhausted.
"""

import os


def _exhausted(state) -> bool:
    """budget exhausted for this incident? 0 limits = unlimited."""
    b = state.get("budget") or {}
    max_it = b.get("max_iterations_total", 0) or 0
    max_tok = b.get("max_tokens_total", 0) or 0
    if not max_it and not max_tok:
        return False
    if max_it and int(b.get("spent_iterations", 0) or 0) >= max_it:
        return True
    if max_tok and int(state.get("total_tokens_used", 0) or 0) >= max_tok:
        return True
    return False


def budget_guard(node_fn):
    """wrap a reasoning worker with the per-incident budget (BUDGET_ENABLED=1)."""

    def wrapped(state, *args, **kwargs):
        if os.getenv("BUDGET_ENABLED", "0") != "1":
            return node_fn(state, *args, **kwargs) or {}
        if _exhausted(state):
            print(f"[budget] exhausted — {getattr(node_fn, '__name__', 'worker')} skipped")
            return {"budget_exhausted": True}

        out = node_fn(state, *args, **kwargs) or {}
        if isinstance(out, dict):
            b = dict(out.get("budget") or {})
            prev = int((state.get("budget") or {}).get("spent_iterations", 0) or 0)
            b["spent_iterations"] = max(prev, int(b.get("spent_iterations", prev) or prev)) + 1
            out["budget"] = b
        return out

    return wrapped
