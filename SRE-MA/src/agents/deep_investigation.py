"""
agents/deep_investigation.py
----------------------------
handles deep investigations for novel incidents that don't match known runbooks.
manages the agent loop, runs diagnostic tools, avoids duplicate queries, and ensures
the investigation either reaches a proposed fix or escalates to a human engineer.
"""

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.prebuilt import create_react_agent

import re
from src.agents.utils import _get_llm, calculate_cost, _accumulate_usage
from src.tools.agent_tool import (
    ALL_TOOLS, READ_TOOLS, INVESTIGATION_TOOLS, propose_remediation, escalate_with_reason,
    reset_investigation_cache,
)
from src.tools.kg_tool import SERVICE_METRICS, get_dependencies
from src.graph.routing import (
    P99_LATENCY_WARNING, ERROR_RATE_WARNING, THROTTLE_RATE_HIGH,
    DB_POOL_WARNING, CIRCUIT_BREAKER_OPEN, DECLINE_RATE_HIGH,
    classify_blast_radius,
)

# maximum tool investigation rounds allowed
MAX_ROUNDS_RUNBOOK_HIT = 4   # runbook found
MAX_ROUNDS_RUNBOOK_MISS = 8  # no runbook found
STAGNATION_THRESHOLD = 2


def get_max_rounds(state) -> int:
    """determines how many query rounds the agent gets based on runbook availability."""
    runbook_id = state.get("runbook_id")
    if runbook_id and runbook_id.startswith("RB-"):
        return MAX_ROUNDS_RUNBOOK_HIT
    return MAX_ROUNDS_RUNBOOK_MISS


INVESTIGATOR_PROMPT = (
    "You are an SRE investigating an incident with no matching runbook.\n"
    "Your job is to check live data, find the root cause, and either propose a fix or escalate with clear evidence.\n\n"

    "1. Discovering metrics and topology:\n"
    "- Before writing PromQL for the primary service, call discover_prometheus_telemetry(primary_service).\n"
    "- Use the returned metric and label names as your starting point. Do not guess or assume HTTP, RPC, or DB metrics exist.\n"
    "- If you get 'service_label_unmatched', look at the observed label names and values, find the right service label, and retry discovery with service_label and service_value.\n"
    "- If you get 'service_matched_no_metrics', do not assume the service is healthy. Check other telemetry sources and upstream/downstream services.\n"
    "- query_topology helps show service dependencies and past incidents, though it is not a live list of Prometheus metrics.\n\n"

    "2. Profiling metrics:\n"
    "- Before building PromQL for an unfamiliar service, call profile_prometheus_telemetry(service).\n"
    "- Use its discovery section for real metric names and labels, and its profile section for likely metric roles.\n"
    "- Treat these roles as hints, not proof of health or root cause. Prefer metrics with metadata and live series, and do not guess unknown roles.\n"
    "- Use list_metrics or discover_prometheus_telemetry if you need more discovery or explicit label matching.\n\n"

    "3. Testing hypotheses and running queries:\n"
    "- Every tool call should test a specific idea. Never make up metric values.\n"
    "- Batch independent queries in the same round (for example, p99 latency, error rate, and DB pool checks).\n"
    "- Keep PromQL valid: balance brackets and include a range selector like [5m] inside rate() or increase().\n"
    "- Do not repeat a query the cache already returned; move on to a different check right away.\n\n"

    "4. Planned PromQL queries:\n"
    "- Use profile_prometheus_telemetry to inspect candidate metric roles, then use execute_prometheus_plan to test a hypothesis against live data.\n"
    "- Only use metrics and labels seen in discovery, with an operation that fits the metric type.\n"
    "- Do not invent metric names, labels, or values, and prefer execute_prometheus_plan over hand-written PromQL.\n\n"

    "How to read query results:\n"
    "- DATA: samples came back; inspect their values and labels.\n"
    "- EMPTY: the query ran but matched no samples.\n"
    "- QUERY_ERROR: Prometheus rejected the query.\n"
    "- BACKEND_UNAVAILABLE: telemetry could not be reached.\n"
    "- PLAN_REJECTED: the query plan failed validation.\n"
    "Never treat EMPTY or an error as proof that the service is healthy; try another angle when evidence is thin.\n\n"

    "5. Feature flags and logs:\n"
    "- Feature flags: if the alert or signals involve feature flags, get the per-flag and per-variant breakdown first via query_prometheus_series:\n"
    "  sum by (feature_flag_key, feature_flag_result_variant) (increase(feature_flag_evaluation_success_total{{service_name=\"<svc>\"}}[10m]))\n"
    "  A spike on a non-default variant is the fault, not healthy traffic. Pin it with explicit matchers (feature_flag_key=..., feature_flag_result_variant=...) and propose set_feature_flag back to the healthy variant. Do not judge flags from aggregate success rates alone.\n"
    "- Logs (logs_ok is {logs_ok}):\n"
    "  If logs_ok is False, do not call query_service_logs; note that logs are unavailable.\n"
    "  If logs_ok is True, call query_service_logs early to catch error signatures.\n\n"

    "6. Missing data and escalation:\n"
    "- Missing data is not proof of health: an empty query or unreachable backend only means that signal is unavailable.\n"
    "- When a query returns empty or fails, check whether it is a missing signal or a label/window mismatch, and check related dependencies.\n"
    "- Never escalate just because data is missing; escalate based on the concrete evidence you found.\n\n"

    "Investigation flow:\n"
    "1. Understand the alert and affected service.\n"
    "2. Discover available telemetry instead of assuming metric names.\n"
    "3. Profile relevant metrics to see candidate signals.\n"
    "4. Form a specific, testable hypothesis and run a validated query.\n"
    "5. Check the returned values, labels, and timestamps.\n"
    "6. If evidence is thin, test another hypothesis on a different signal.\n"
    "7. Cross-check metrics with logs, traces, and recent changes when available.\n"
    "8. Pin down the most likely root cause with supporting and contradicting evidence.\n"
    "9. If you still cannot confirm a root cause, escalate instead of guessing a fix.\n\n"

    "INVESTIGATION RULES:\n"
    "- Never repeat an identical query without a reason.\n"
    "- Never interpret an empty result as proof of normal health.\n"
    "- Never confuse correlation with causation.\n"
    "- A metric name alone does not establish its meaning.\n"
    "- Do not propose remediation until the root cause has sufficient "
    "supporting evidence.\n"
    "- Respect all existing critic, verifier, attribution and policy gates.\n\n"


    "## 6. BUDGET & TERMINATION RULES\n"
    "- Budget: You have a total budget of {max_rounds} tool rounds. Remaining rounds are reported each round.\n"
    "- Termination: You MUST conclude your investigation by either:\n"
    "  1. Calling propose_remediation with a fully evidence-backed action plan, OR\n"
    "  2. Calling escalate_with_reason (or responding with 'escalate') detailing what was checked, what was ruled out, and why human intervention is required.\n"
    "- Never end your investigation with only a conversational summary."
)

#key signals to check against warning thresholds
_BREACH_CHECKS = [
    ("circuit_breaker_state", CIRCUIT_BREAKER_OPEN),
    ("db_pool_utilization", DB_POOL_WARNING),
    ("p99_latency_s", P99_LATENCY_WARNING),
    ("error_rate", ERROR_RATE_WARNING),
    ("throttle_error_rate", THROTTLE_RATE_HIGH),
    ("payment_decline_rate", DECLINE_RATE_HIGH),
]


def _breaches(state) -> list:
    """finds any active metrics that exceed warning thresholds."""
    s = state.get("raw_signals", {})
    out = []
    for key, thr in _BREACH_CHECKS:
        v = s.get(key)
        if v is not None and v >= thr:
            out.append(f"{key}={v:.3f} above internal warning threshold {thr}")
    lp = s.get("log_patterns", {}) or {}
    if (lp.get("auth_failures") or 0) >= 50:
        out.append(f"auth_failures={lp.get('auth_failures')} (>=50)")
    if lp.get("bulk_data_export"):
        out.append("bulk_data_export=True")
    return out

_harness = None


def get_harness():
    """initializes the react agent with both investigation and conclusion tools."""
    global _harness
    if _harness is None:
        llm = _get_llm(temperature=0.2)
        llm = llm.bind_tools(ALL_TOOLS, parallel_tool_calls=True)
        _harness = create_react_agent(
            model=llm,
            tools=INVESTIGATION_TOOLS,
        )
    return _harness

def _cache_key(tool_name: str, args: dict) -> str:
    """creates a unique cache key for a tool call and its arguments."""
    return f"{tool_name}: {hash(frozenset(args.items()))}"


def _terminal_call_ids(msgs) -> set:
    """returns ids of exit tools (propose/escalate) so they aren't confused with diagnostic evidence."""
    return {tc.get("id") for m in msgs
            for tc in (getattr(m, "tool_calls", None) or [])
            if (tc.get("name") or "") in ("propose_remediation", "escalate_with_reason")}


def _tool_evidence(msgs, cap: int = 6) -> list:
    """pairs each tool query with its output to build clear evidence for the diagnosis."""
    terminal = {"propose_remediation", "escalate_with_reason"}
    queries, out = {}, []
    for m in msgs:
        for tc in (getattr(m, "tool_calls", None) or []):
            a = tc.get("args") or {}
            queries[tc.get("id")] = (tc.get("name") or "", str(a.get("expr") or a.get("query") or ""))
        if getattr(m, "type", "") == "tool" and getattr(m, "tool_call_id", None) in queries:
            name, expr = queries[m.tool_call_id]
            if name in terminal:
                continue
            #extract metric and labels (e.g. http_requests_total{service="..."}) for readability
            ref = re.search(r"[a-zA-Z_][a-zA-Z0-9_]*(?=\{)", expr)
            if ref:
                end = expr.find("}", ref.end())
                short = expr[ref.start(): end + 1][:120] if end != -1 else expr[ref.start():][:120]
            else:
                short = expr.split("(", 1)[0][:60] or "tool"
            out.append(f"{short} -> {str(m.content)[:160]}")
    return out[-cap:]


def _structured_writeback(state, base_return, proposal, terminal, text_escalated, msgs) -> dict:
    """formats the agent's findings, observed metrics, and proposed fix back into incident state."""
    finding, conf, agent_escalated = None, 0.0, False
    if proposal:
        finding, conf = str(proposal.get("action") or ""), 0.65  #requires critic review before executing
    elif text_escalated:
        finding = str((terminal["args"] or {}).get("reason", "") or "")
        conf, agent_escalated = 0.60, True

    if not finding:
        return base_return

    s = state.get("raw_signals", {})
    breaches = _breaches(state) or [
        f"{key}={s.get(key)} within internal threshold {thr}"
        for key, thr in _BREACH_CHECKS if s.get(key) is not None
    ][:4]
    #collect real observations from threshold breaches and tool queries
    ev_core = breaches + _tool_evidence(msgs)
    evidence = ev_core

    return {**base_return,
        "hypotheses": [{"hypothesis": finding, "evidence": evidence,
                        "confidence": conf, "supporting_runbook": None}],
        "root_cause": finding,
        "diagnosis_summary": finding,
        "evidence_summary": "\n".join(ev_core),
        "llm_suggested_action": proposal.get("action") if proposal else None,
        "proposed_tool": proposal.get("tool") if proposal else None,
        "proposed_flag": (proposal.get("flag") or None) if proposal else None,
        "proposed_variant": (proposal.get("variant") or None) if proposal else None,
        "proposed_target": proposal.get("target_service") if proposal else None,
        "proposed_blast_radius": proposal.get("blast_radius") if proposal else None,
        "escalate_reason": "agent_escalated" if agent_escalated else None,
        "investigation_complete": True,
    }


def deep_investigation_node(state) -> dict:
    """runs a multi-round diagnostic investigation for unfamiliar alerts and generates a verified resolution."""
    print(f"\n[deep_investigation] Starting investigation for {state.get('incident_id')}")
    print(f"[deep_investigation] Alert: {state.get('alert_name')} | Severity: {state.get('severity')}")
    print(f"[deep_investigation] Services: {state.get('affected_services')}")

    # reset tool query cache for fresh incidents or post-fix re-evaluations; keep cache on verifier reroutes
    if not state.get("verifier_reroutes") or state.get("reattack_hint"):
        reset_investigation_cache()
    tool_call_cache = {}

    raw = state.get("raw_signals", {})
    sig = "\n".join(
        f" {k}: {v}" for k, v in raw.items()
        if v is not None and not k.endswith("summaries") and k != "log_patterns"
        and not str(k).startswith("__")) or "none"

    error_logs = raw.get("error_log_summaries", [])[:6]
    error_text = "\n".join(error_logs)

    affected_services = state.get('affected_services') or ['unknown']
    primary_service = affected_services[0]
    #find downstream dependencies mentioned in error logs to check alongside the primary service
    frontier = get_dependencies(primary_service, hops=2)
    downstream_services = {dep for log in error_logs for dep in frontier if dep in log.lower()}
    all_services = [primary_service] + list(downstream_services)

    hypothesis_hint = ""
    if downstream_services:
        hypothesis_hint = (
            f"\n\nHYPOTHESIS SEEDS: Error logs mention these downstream services: {', '.join(sorted(downstream_services))}. "
            f"You MUST query these services specifically. Do not limit investigation to {primary_service} only."
        )

    seeds = "\n".join(SERVICE_METRICS.get(primary_service, [])[:6])
    seed_text = f"\nSEED METRICS (run these first):\n{seeds}" if seeds else ""

    # determine maximum tool rounds for this investigation
    max_rounds = get_max_rounds(state)
    logs_ok = raw.get("logs_ok", False)
    
    #format investigator prompt with budget and available signals
    formatted_prompt = INVESTIGATOR_PROMPT.format(
        max_rounds=max_rounds,
        logs_ok=logs_ok
    )

    #if the critic rejected a previous proposal, pass the critique so the agent fixes it
    critique_hint = ""
    if state.get("requires_retry") and state.get("critique"):
        critique_hint = f"\nCRITIC REJECTION (fix this): {state['critique']}\n"

    #if the verifier challenged the previous diagnosis, prompt for new evidence
    verifier_hint = ""
    vr = state.get("verifier_result") or {}
    if state.get("verifier_reroutes") and vr.get("verdict") != "upheld":
        verifier_hint = (
            f"\nVERIFIER CHALLENGE: your diagnosis did not survive adversarial checking "
            f"(verdict={vr.get('verdict')}, reason={vr.get('reason')}). Disproof queries "
            f"already run: {vr.get('disproof_queries') or 'none executed'}. Re-ground the "
            f"diagnosis with DIFFERENT evidence; do not repeat those queries.\n"
        )

    #if a previous fix didn't resolve the alert, pass post-action readings for the retry
    reattack_hint = ""
    if state.get("reattack_hint"):
        reattack_hint = f"\nPOST-ACTION RETRY: {state['reattack_hint']}\n"

    # include detected metric jump/drop times so the agent's timeline stays accurate
    cs = raw.get("changepoints") or []
    change_hint = ""
    if cps:
        lines = "\n".join(
            f"  {c['signal']}: level shift {c['direction']} {c['sigma']}sigma at "
            f"{c['onset_iso']} ({c['minutes_ago']}m ago, {c['before']} -> {c['after']})"
            for c in cps
        )
        change_hint = (
            "\nCHANGE-POINT EVIDENCE (deterministic onset detection; your root-cause "
            f"story must be consistent with these times):\n{lines}\n"
        )

    # include similar past incidents as reference hints (must be verified against live data)
    mem_hint = ""
    try:
        from src.learning.distill import retrieve_similar
        mem = retrieve_similar(state.get("alert_name", ""), primary_service, raw)
        for m in mem:
            mem_hint += (
                f"\nAPPROVED MEMORY (low-trust hint from a human-approved past incident on "
                f"{m.get('service')}): root_cause='{m.get('root_cause')}' fixed_by='{m.get('action_taken')}'. "
                f"VERIFY against live signals before relying on it.\n"
            )
    except Exception:
        pass

    ctx = (
        f"ALERT: {state.get('alert_name')} | SEVERITY: {state.get('severity')}\n"
        f"SERVICES: {', '.join(all_services)} (primary: {primary_service})\n"
        f"ROUNDS REMAINING: {max_rounds}\n"
        f"LOGS AVAILABLE: {logs_ok}\n"
        f"METRICS: \n{sig}\nERROR LOGS:\n{error_text}{critique_hint}{verifier_hint}{reattack_hint}{change_hint}{mem_hint}{hypothesis_hint}{seed_text}\nInvestigate now"
    )

    try:
        result = get_harness().invoke(
            {"messages": [SystemMessage(content=formatted_prompt), HumanMessage(content=ctx)]},
            config={"recursion_limit": 2 * max_rounds + 2, "run_name": "deep-investigation"}
        )
    except Exception as e:
        print(f"[deep_investigation] ERROR: {e}")
        return {
            "errors": state.get("errors", []) + [f"deep_investigation failed: {e}"],
            "diagnosis_mode": "pending_llm",
            "diagnosis_loops": state.get('diagnosis_loops', 0) + 1
        }

    msgs = result["messages"]

    #track unique vs. duplicate tool queries to detect loops
    tool_call_in_response = [tc for m in msgs for tc in (getattr(m, "tool_calls", None) or [])]
    fresh_calls = []
    for tc in tool_call_in_response:
        key = _cache_key(tc['name'], tc.get('args', {}))
        if key in tool_call_cache:
            print(f"[deep_investigation] DUPLICATE TOOL CALL HAPPENED: {tc['name']}")
        else:
            tool_call_cache[key] = True
            fresh_calls.append(tc)

    stagnant = len(tool_call_in_response) > 0 and len(fresh_calls) == 0
    if stagnant:
        print(f"[deep_investigation] STAGNATION: all {len(tool_call_in_response)} tool calls were duplicates")

    #if the agent didn't call propose or escalate, prompt it once more to conclude
    terminal = next((tc for m in msgs for tc in (getattr(m, "tool_calls", None) or []) if tc['name'] in ("propose_remediation", "escalate_with_reason")), None)
    if terminal is None:
        try:
            fin_llm = _get_llm(temperature=0.0).bind_tools([propose_remediation, escalate_with_reason], tool_choice="required")
            # final nudge prompting for an immediate conclusion
            fin_ctx = (
                f"ROUNDS REMAINING: 0 (limit reached)\n"
                f"Investigation limit reached. You MUST call propose_remediation or escalate_with_reason now.\n"
                f"{ctx}"
            )
            assert msgs and msgs[0].type == "system", "First message must be labeled system"
            for attempt, nudge in enumerate([fin_ctx, "Output your proposal now."]):
                fin_msg = fin_llm.invoke(msgs + [HumanMessage(content=nudge)])
                msgs.append(fin_msg)
                terminal = next((tc for tc in (getattr(fin_msg, "tool_calls", None) or []) if tc['name'] in ("propose_remediation", "escalate_with_reason")), None)
                if terminal:
                    break
        except Exception as e:
            print(f"[deep_investigation] Forced finalization failed: {e}")

    if terminal is None:
        top_text = next((str(m.content) for m in msgs if "[Known Fixes" in str(m.content)), "") or "\n".join(state.get("investigation_scratchpad", []))
        fixes = re.findall(r'-\s*(.*?)\s*\(success_rate=(\d+)%', top_text)
        keywords = [w for w in re.findall(r'\w+', f"{state.get('alert_name', '')} {' '.join(_breaches(state))}".lower()) if len(w) > 3]
        relevant = [(fix, int(rate)/100.0) for fix, rate in fixes if any(k in fix.lower() for k in keywords)]
        #check if known fixes match observed symptoms; otherwise escalate
        if relevant and len(fresh_calls) >= STAGNATION_THRESHOLD:
            best_fix, rate = max(relevant, key=lambda x: x[1])
            tool = "rollback_deployment" if "rollback" in best_fix.lower() else ("set_feature_flag" if "flag" in best_fix.lower() else "rolling_restart")
            blast = classify_blast_radius({"tool": tool, "params": {"service": primary_service}})
            terminal = {"name": "propose_remediation", "args": {"action": best_fix, "tool": tool, "target_service": primary_service, "blast_radius": blast, "confidence": 0.70 if rate >= 0.90 else 0.55}}
        else:
            #escalate with the clearest available finding rather than an empty reason
            hyps = state.get("hypotheses") or []
            finding = (state.get("root_cause") or (hyps[0].get("hypothesis") if hyps else "") or "no diagnosis reached")
            terminal = {"name": "escalate_with_reason", "args": {"reason": f"no_relevant_known_fix; finding: {finding}"}}

    proposal = terminal["args"] if terminal and terminal["name"] == "propose_remediation" else None
    text_escalated = terminal is not None and terminal["name"] == "escalate_with_reason"
    if text_escalated:
        print(f"[deep_investigation] Agent concluded: escalate ({terminal['args'].get('reason')})")

    #calculate token usage and estimated cost
    model_name = next((getattr(m, "response_metadata", {}).get("model_name") or getattr(m, "response_metadata", {}).get("model") for m in reversed(msgs) if getattr(m, "response_metadata", None)), None)
    input_tokens, output_tokens = _accumulate_usage(msgs)
    cost = calculate_cost(input_tokens, output_tokens, model_name)

    base_return = {        # clear one-time retry hints so they don't leak into subsequent runs
        "reattack_hint": None,
        "investigation_scratchpad": [
            f"{m.type}: {str(m.content)[:200]}" for m in msgs
            if not (getattr(m, "type", "") == "tool"
                    and getattr(m, "tool_call_id", None) in _terminal_call_ids(msgs))
        ],
        "investigation_steps": sum(1 for m in msgs if m.type == "ai"),
        "diagnosis_mode": "deep_agents",
        "total_tokens_used": state.get("total_tokens_used", 0) + input_tokens + output_tokens,
        "token_cost_usd": state.get("token_cost_usd", 0.0) + cost,
        "model_used": model_name or state.get("model_used") or "deep-agent-harness",
    }

    # policy for unresolved novel alerts:
    # - if all metrics are healthy, close as a false positive (no human paged).
    # - if metrics show an anomaly but no fix was found, run safe diagnostics first for sev2/sev3
    # - for sev1, escalate directly to the on-call engineer.
    # false-positive closes are handled in-memory; upgrade path is an explicit alertmanager silence
    if proposal is None and not text_escalated:
        service = primary_service
        found = _breaches(state)

        if not found:
            healthy_ev = [
                f"{key}={state.get('raw_signals', {}).get(key)} within threshold {thr}"
                for key, thr in _BREACH_CHECKS
                if state.get("raw_signals", {}).get(key) is not None
            ][:4]
            print(f"[deep_investigation] ALL CLEAR: no live breach, calming alert (false-positive close, no human paged)")
            return {**base_return,
                "investigation_complete": True,
                "hypotheses": [{
                    "hypothesis": f"No active anomaly on {service}; all signals within thresholds, so {state.get('alert_name')} is likely transient or a false positive",
                    "evidence": healthy_ev or ["no breaching signal found during live investigation"],
                    "confidence": 0.85,
                    "alternative": "slow-burn issue below thresholds",
                    "supporting_runbook": None,
                }],
                "action_plan": [{
                    "action": f"Alert calmed: {state.get('alert_name')} investigated, no real anomaly, closed as false positive",
                    "tool": "notify",
                    "params": {"channel": "incidents", "severity": "info", "message": f"{state.get('alert_name')} on {service}: investigation found no breach; no human action needed"},
                    "blast_radius": "pod",
                    "reversible": True,
                    "requires_approval": False,
                    "executed": False,
                    "result": None,
                }],
            }

        if state.get("severity") in ("SEV2", "SEV3"):
            print(f"[deep_investigation] Anomaly confirmed, no proposal; running safe mitigation before escalation: {found[0]}")
            return {**base_return,
                "investigation_complete": True,
                "hypotheses": [{
                    "hypothesis": f"Investigation confirmed anomaly on {service}: {found[0]}; root cause not identified, capturing diagnostics and monitoring",
                    "evidence": found,
                    "confidence": 0.60,
                    "alternative": "transient spike that will self-recover",
                    "supporting_runbook": None,
                }],
                "action_plan": [{
                    "action": f"Capture diagnostics on {service} and monitor (safe mitigation to calm {state.get('alert_name')})",
                    "tool": "capture_diagnostics",
                    "params": {"service": service},
                    "blast_radius": "pod",
                    "reversible": True,
                    "requires_approval": False,
                    "executed": False,
                    "result": None,
                }],
            }

    print(f"[deep_investigation] Completed. Proposal: {proposal.get('action') if proposal else ('escalate' if text_escalated else 'None')}")

    out = _structured_writeback(state, base_return, proposal, terminal, text_escalated, msgs)
    if "investigation_complete" not in out:
        out = {**out, "investigation_complete": proposal is not None or text_escalated or stagnant}
    if "hypotheses" in out:
        print(f"[deep_investigation] Writeback: finding={str(out.get('root_cause'))[:80]!r} "
              f"evidence={len(out['hypotheses'][0]['evidence'])} entries, agent_escalated={out.get('escalate_reason')}")
    return out
