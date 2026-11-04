"""
agents/verifier_agent.py
------------------------
B1 Verifier (roster upgrade): NEW adversarial node between deep_investigation
and critic. Flag VERIFIER_ENABLED (default 0; MULTI_AGENT=1 enables all upgrades).

- read-only tools ONLY, dispatched through the Tool Librarian (dispatcher.run_tool)
- hard recursion cap: VERIFIER_RECURSION_LIMIT disproof-query executions per check
- typed VerificationChallenge {claim, disproof_queries[], verdict, confidence}
- contradicted+grounded OR unverifiable -> route back to deep_investigation ONCE
  (cap 1; unverifiable reroute lets the investigation re-ground its diagnosis,
  otherwise erroring "proof" queries silently buy zero information)
- fail-open: any error -> unverifiable verdict, routing falls back to critic
  (the pre-change target)

This node does NOT re-implement critic grounding (whitelist/target-binding
checks live in critic_agent.py) and does NOT touch kg_tool Gate-5 logic — its
single job is to try to DISPROVE the diagnosis claim with live read-only data.
"""

import os
import datetime

from langchain_core.messages import SystemMessage, HumanMessage

from src.graph.state import AgentState
from src.agents.schemas import VerificationChallenge
from src.agents.utils import _get_llm, calculate_cost
from src.tools import dispatcher
from src.tools.agent_tool import METRIC_CATALOG, validate_expr

from langfuse import observe

def observe(*args, **kwargs):
    def decorator(f):
        return f
    return decorator

VERIFIER_RECURSION_LIMIT = 8      # hard cap on disproof-query executions
VERIFIER_MAX_DISPROOF_QUERIES = 3 # disproof queries proposed per check

VERIFY_SYSTEM_PROMPT = (
    "You are an adversarial verifier. You get one diagnosis claim for a live "
    "incident. Propose up to {max_queries} read-only PromQL disproof queries "
    "that would REFUTE the claim if it is wrong. Constraints enforced by the "
    "executor:\n"
    "- use ONLY these exact metric names; invented names are rejected without "
    "execution: {catalog}\n"
    "- every rate()/increase() must carry a range selector like [5m]; brackets "
    "balanced\n"
    "- labels: request/histogram metrics key on service, db pool metrics key on "
    "db='postgres_primary'\n"
    "- building no query is better than inventing one: if nothing can be grounded "
    "in the list above return an EMPTY disproof_queries list.\n"
    "Return ONLY the structured schema."
)

VERDICT_SYSTEM_PROMPT = (
    "You are an adversarial verifier. Given the claim and the disproof query "
    "results, return a verdict: 'contradicted' if a live result refutes the "
    "claim, 'unverifiable' if results are missing/erroring, 'upheld' if the "
    "results are consistent with the claim. Be honest — missing data is never "
    "proof. Return ONLY the structured schema."
)


def verifier_enabled(state: AgentState = None) -> bool:
    if os.getenv("MULTI_AGENT", "0") == "1" or os.getenv("VERIFIER_ENABLED", "0") == "1":
        return True
    return False


def _claim_of(state: AgentState) -> str:
    return (state.get("root_cause")
            or next((h.get("hypothesis") for h in (state.get("hypotheses") or [])
                     if h.get("hypothesis")), "")
            or state.get("diagnosis_summary") or "")


def _evidence_of(state: AgentState) -> str:
    lines = []
    for h in (state.get("hypotheses") or [])[:3]:
        lines += [e for e in (h.get("evidence") or []) if e]
    ev = state.get("evidence_summary")
    if ev:
        lines.append(ev)
    return "\n".join(lines[:12])


def _invoke_structured(llm, schema, messages):
    """structured output via langchain with_structured_output; returns (parsed, raw)."""
    result = llm.with_structured_output(schema, include_raw=True).invoke(messages)
    return result["parsed"], result["raw"]


@observe(name="verifier")
def verifier_node(state: AgentState) -> dict:
    if not verifier_enabled(state):
        return {}

    claim = _claim_of(state)
    if not claim:
        return {"verifier_result": {"verdict": "unverifiable", "claim": "",
                                    "disproof_queries": [], "confidence": 0.0,
                                    "reason": "no claim to verify (fail-open)"}}

    llm = _get_llm(temperature=0.0)
    evidence = _evidence_of(state)

    invocations, ledger = [], []
    spent_in, spent_out = 0, 0

    try:
        challenge, raw = _invoke_structured(
            llm, VerificationChallenge,
            [SystemMessage(content=VERIFY_SYSTEM_PROMPT.format(
                 max_queries=VERIFIER_MAX_DISPROOF_QUERIES,
                 catalog=", ".join(METRIC_CATALOG))),
             HumanMessage(content=f"CLAIM: {claim}\n\nEVIDENCE SO FAR:\n{evidence or 'none'}")])
        usage = raw.usage_metadata or raw.response_metadata.get("token_usage", {})
        spent_in += usage.get("input_tokens", 0)
        spent_out += usage.get("output_tokens", 0)

        results, executed = [], 0
        for expr in (challenge.disproof_queries or [])[:VERIFIER_MAX_DISPROOF_QUERIES]:
            # static pre-check: reject broken PromQL before the round trip; the
            # catalog check stays off here — unknown metrics are honest no-data
            # results, and the read-only dispatcher is the enforcement point
            valid, why = validate_expr(expr, check_catalog=False)
            if not valid:
                invocations.append({"tool": "query_prometheus_metric", "args": {"expr": expr},
                                    "result": f"SKIP: {why}",
                                    "ts": datetime.datetime.utcnow().isoformat(),
                                    "worker": "verifier"})
                results.append(f"{expr} -> SKIP: {why}")
                continue
            if executed >= VERIFIER_RECURSION_LIMIT:
                break
            out = dispatcher.run_tool("query_prometheus_metric", {"expr": expr})
            executed += 1
            invocations.append({"tool": "query_prometheus_metric", "args": {"expr": expr},
                                "result": str(out)[:120], "ts": datetime.datetime.utcnow().isoformat(),
                                "worker": "verifier"})
            results.append(f"{expr} -> {out}")

        if executed:
            verdict_challenge, raw = _invoke_structured(
                llm, VerificationChallenge,
                [SystemMessage(content=VERDICT_SYSTEM_PROMPT),
                 HumanMessage(content=f"CLAIM: {claim}\n\nDISPROOF RESULTS:\n" +
                              "\n".join(results))])
            usage = raw.usage_metadata or raw.response_metadata.get("token_usage", {})
            spent_in += usage.get("input_tokens", 0)
            spent_out += usage.get("output_tokens", 0)
            challenge = verdict_challenge
        else:
            #a repair loop that re-prompts the challenge step once
            challenge = challenge.model_copy(update={
                "verdict": "unverifiable", "confidence": 0.0,
                "reason": "no executable disproof query (PromQL failed catalog validation)"})
    except Exception as e:
        print(f"[verifier] ERROR (fail-open): {e}")
        return {
            "verifier_result": {"claim": claim, "disproof_queries": [],
                                "verdict": "unverifiable", "confidence": 0.0,
                                "reason": f"verifier failed: {e} (fail-open)"},
            "message_ledger": [{"timestamp": datetime.datetime.utcnow().isoformat(),
                                "from_worker": "verifier", "kind": "note",
                                "content": f"verifier fail-open: {e}"}],
            "budget": {"spent_iterations": 1},
        }

    # grounded = at least one executed query returned a real (non-ERROR/non-SKIP) value
    grounded = any(not str(r["result"]).startswith(("ERROR", "SKIP")) for r in invocations)
    res = challenge.model_dump()
    res["grounded"] = grounded
    res["reason"] = getattr(challenge, "reason", None)

    reroutes_now = int(state.get("verifier_reroutes", 0) or 0)
    route_back = reroutes_now < 1 and (
        (res["verdict"] == "contradicted" and grounded) or res["verdict"] == "unverifiable")
    # the router reads the marker (not the counter): the counter already includes
    # this reroute, so a bare counter comparison would dead-lock the second pass
    res["reroute"] = route_back

    ledger.append({"timestamp": datetime.datetime.utcnow().isoformat(),
                   "from_worker": "verifier", "kind": "note",
                   "content": f"claim={str(claim)[:80]!r} verdict={res['verdict']} "
                              f"grounded={grounded} reroute={route_back}"})

    return {
        "verifier_result": res,
        "verifier_reroutes": reroutes_now + (1 if route_back else 0),
        "tool_invocations": invocations,
        "message_ledger": ledger,
        "total_tokens_used": state.get("total_tokens_used", 0) + spent_in + spent_out,
        "token_cost_usd": state.get("token_cost_usd", 0.0) +
                          calculate_cost(spent_in, spent_out),
        "budget": {"spent_iterations": 1, "spent_tokens": spent_in + spent_out},
    }


def route_after_verifier(state: AgentState):
    """verdict routing: contradicted+grounded or unverified returns to deep_investigation
    exactly once (cap 1); contradicted but ungraounded, upheld and every second pass fall back to the
    pre-change target, critic."""
    res = state.get("verifier_result") or {}
    if res.get("reroute") is True:
        print("[router] verifier -> deep_investigation (contradicted, cap 1)")
        return "deep_investigation"
    print(f"[router] verifier -> critic (verdict={res.get('verdict')}, cap 1)")
    return "critic"
