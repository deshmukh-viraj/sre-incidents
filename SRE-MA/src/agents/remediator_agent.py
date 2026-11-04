"""
agents/remediator_agent.py
--------------------------
remediator agent: builds and validates an action plan from the diagnosed root cause.
"""

from typing import Optional
import json
import os
import re
import datetime
from src.graph.state import AgentState
from src.graph.routing import classify_blast_radius, requires_human_approval, DIAGNOSIS_CONFIDENCE, \
    P99_LATENCY_WARNING, ERROR_RATE_WARNING, THROTTLE_RATE_HIGH
from src.graph.policy import REVERSIBLE_WRITE_TOOLS
from src.tools.sre_tool import notify_slack
from src.tools import dispatcher


def _preflight_enabled() -> bool:
    """B5 flag (default 0; MULTI_AGENT=1 enables all upgrades)."""
    return os.getenv("MULTI_AGENT", "0") == "1" or \
        os.getenv("REMEDIATOR_PREFLIGHT_ENABLED", "0") == "1"

REMEDIATOR_PREFLIGHT_QUERIES = 3
_PREFLIGHT_EXPRS = [
    ("error_rate",
     'sum(rate(http_requests_total{{service="{service}",status_code=~"5.."}}[5m])) '
     '/ sum(rate(http_requests_total{{service="{service}"}}[5m]))',
     ERROR_RATE_WARNING),
    ("p99_latency_s",
     'histogram_quantile(0.99, sum(rate(http_request_duration_seconds_bucket'
     '{{service="{service}"}}[5m])) by (le))',
     P99_LATENCY_WARNING),
    ("throttle_error_rate",
     'sum(rate(http_requests_total{{service="{service}",status_code=~"429|504"}}[5m])) '
     '/ sum(rate(http_requests_total{{service="{service}"}}[5m]))',
     THROTTLE_RATE_HIGH),
]


def _preflight_validate(state: AgentState, action_plan: list) -> dict:
    """bounded pre-flight <=3 read-only metric validations of the
    plan's premise before committing it ('would restarting X drop error_rate?').

    deterministic: if every queried premise signal is ALREADY healthy, the
    action's premise is gone -> plan content unchanged but requires_approval
    is forced so a human confirms before the (possibly stale) action runs.
    """
    service = (state.get("affected_services") or ["unknown"])[0]
    calls, healthy_premise = [], True
    for key, tmpl, threshold in _PREFLIGHT_EXPRS[:REMEDIATOR_PREFLIGHT_QUERIES]:
        expr = tmpl.format(service=service)
        out = dispatcher.run_tool("query_prometheus_metric", {"expr": expr})
        ok = False
        try:
            ok = out is not None and not str(out).startswith("ERROR") \
                and float(out) < threshold
        except (TypeError, ValueError):
            ok = False
        healthy_premise = healthy_premise and ok
        calls.append({"tool": "query_prometheus_metric", "args": {"expr": expr},
                      "result": str(out)[:120], "signal": key, "within_threshold": ok,
                      "ts": datetime.datetime.utcnow().isoformat(), "worker": "remediator-preflight"})
        if len(calls) >= REMEDIATOR_PREFLIGHT_QUERIES:
            break

    ledger_note = {
        "timestamp": datetime.datetime.utcnow().isoformat(),
        "from_worker": "remediator", "kind": "note",
        "content": f"pre-flight validation: {len(calls)} query(ies), premise_still_valid="
                   f"{not healthy_premise}",
    }
    out = {"tool_invocations": calls, "message_ledger": [ledger_note],
           "budget": {"spent_iterations": 1}}
    if healthy_premise and action_plan:
        print("[remediator] pre-flight: premise already healthy — forcing human approval")
        for a in action_plan:
            a["requires_approval"] = True
        out["requires_approval"] = True
        out["action_plan"] = action_plan
    return out


def _maybe_preflight(state: AgentState, action_plan: list, needs_approval: bool) -> dict:
    """run the bounded pre-flight mini-loop on NOVEL paths only
    (diagnosis_mode == deep_agents); runbook/deterministic plans pass through
    byteidentical."""
    if not (_preflight_enabled() and state.get("diagnosis_mode") == "deep_agents"
            and action_plan):
        return {"action_plan": action_plan, "requires_approval": needs_approval}
    out = _preflight_validate(state, action_plan)
    if out.pop("requires_approval", None):
        needs_approval = True
    if "action_plan" in out:
        action_plan = out.pop("action_plan")
    return {**out, "action_plan": action_plan, "requires_approval": needs_approval}


#remediator node

def _plan_citations(state: AgentState, cap: int = 3) -> list:
    """tool-result lines from the investigation scratchpad — the evidence a plan
    must carry so a human (or the attribution report) can see what grounded it."""
    return [
        str(e)[6:160]
        for e in state.get("investigation_scratchpad", [])
        if str(e).startswith("tool: ")
    ][-cap:]

def remediator_node(state: AgentState) -> dict:
    if state.get("action_plan"):
        if _preflight_enabled() and state.get("diagnosis_mode") == "deep_agents":
            out = _preflight_validate(state, state["action_plan"])
            forced = out.pop("requires_approval", False)
            out.pop("action_plan", None)  # plan dict mutated in place; state holds it
            return out if (out or forced) else {}
        return {}

    # critic retry: rebuild the plan from the proposal the harness actually made.
    # freshest-first: state fields beat a stale scratchpad replay that would
    # resurrect an earlier (possibly rejected) proposal.
    if state.get("requires_retry"):
        plan = _item_from_state_fields(state)
        if plan is None:
            plan = _extract_from_scratchpad(state)
        if plan is None:
            plan = _llm_extract_proposal(state)
        if plan:
            print(f"\n[remediator] Retry: rebuilt plan from investigation proposal ({plan[0]['tool']})")
            return {**_maybe_preflight(state, plan, any(a.get("requires_approval") for a in plan)),
                    "requires_retry": False}
        
    print(f"\n[remediator] Building action plan for {state['incident_id']}")

    consensus_rb = _check_correlated_rb_consensus(state)
    if state.get("correlated_alerts"):
        if consensus_rb:
            print(f"[remediator] Correlated runbook consensus achieved: {consensus_rb}")
        else:
            print(f"[remediator] Correlated alerts disagree on runbook")

    hypotheses = state.get("hypotheses", [])
    if not hypotheses:
        return {"action_plan": [], "requires_approval": False}

    best = max(hypotheses, key=lambda h:h.get("confidence", 0))
    runbook = consensus_rb or state.get("runbook_id") or best.get("supporting_runbook")
    llm_action = state.get("llm_suggested_action")

    #if confidence is below threshold and no LLM action was proposed, do not execute Path A
    if best.get("confidence", 0) < DIAGNOSIS_CONFIDENCE and not llm_action:
        print(f"[remediator] Low confidence ({best.get('confidence', 0):.2f} < {DIAGNOSIS_CONFIDENCE}) and no LLM proposal -> no auto runbook")
        return {"action_plan": [], "requires_approval": True}

    #path A: known resolution
    action_plan = _build_action_plan(runbook, state)
    is_fallback = (not action_plan or (len(action_plan) == 1 and action_plan[0].get("tool")=="notify"))

    if not is_fallback:
        print(f"[remediator] Path A runbook {runbook}")

    #path B: novel alert -> safe mitifation
    elif llm_action:
        print(f"[remediator] Path B - KG action: {llm_action[:60]}")
        _destructive = ["delete", "drop", 'purge', 'revoke', 'terminate', 'wipe']
        is_reversible = not any(w in llm_action.lower() for w in _destructive)

        action_plan = [{
            "action": f"KG suggestion: {llm_action}",
            "tool": "execute_kg_suggestion",
            "params": {"command": llm_action, 
            "service": state.get("affected_services", ["unknown"])[0]}, 
            "blast_radius": "service",
            "reversible": is_reversible,
            "requires_approval": True,
            "executed": False,
            "result": None,
        }]

    # path C: safe fallback based on severity + signal pattern
    else:
        print("[remediator] Path C -> safe mitigation")
        action_plan = _build_safe_mitigation(state)

    #safety layer: fill blast_radius / approval / reversibility / citations
    needs_approval = False
    citations = _plan_citations(state)
    for action in action_plan:
        if not action.get("blast_radius"):
            action["blast_radius"] = classify_blast_radius(action)
        action.setdefault("reversible", action.get("tool") in REVERSIBLE_WRITE_TOOLS)
        if citations and not action.get("citations"):
            action["citations"] = citations
        if action.get("requires_approval") is None:
            action["requires_approval"] = requires_human_approval(action)
        if action.get("requires_approval"):
            needs_approval = True

    for a in action_plan:
        print(f"  -> {a['action'][:60]}  blast={a['blast_radius']} approval={a['requires_approval']}")

    return _maybe_preflight(state, action_plan, needs_approval)


def _build_action_plan(runbook_id: str, state: AgentState) -> list:
    """load action plan from json."""
    raw = state.get("raw_signals", {})
    # P10: the fintech pack can point at a different runbooks file
    from src.config.loader import config_pack
    pack_path = (config_pack().get("runbooks_path")
                 or os.path.join("runbooks", "runbooks.json"))
    plans_path = os.path.join(os.path.dirname(__file__), "..", "..", pack_path)
    
    try:
        with open(plans_path, "r") as f:
            plans = json.load(f)
    except FileNotFoundError:
        print(f"[remediator] WARNING: runbooks.json not found at {plans_path} falling back to safe actions")
        plans = {}

    plan = plans.get(runbook_id)
    if plan:

        plan_str = json.dumps(plan)
        if "{attacking_ip}" in plan_str:
            plan_str = plan_str.replace("{attacking_ip}", raw.get("attacking_ip", "unknown"))
        if "{compromised_key}" in plan_str:
            plan_str = plan_str.replace("{compromised_key}", raw.get("compromised_key", "unknown"))
        return json.loads(plan_str)

    return [{
        "action": "Escalate to on-call engineer",
        "tool": "notify",
        "params": {"channel": "incidents", "severity": "critical"},
        "blast_radius": "pod",
        "reversible": True,
        "requires_approval": False,
        "executed": False,
        "result": None,
    }]


def _build_safe_mitigation(state: AgentState) -> list:
    severity = state.get("severity", "SEV3")
    signals = state.get("raw_signals", {})
    service = state.get("affected_services", ["unknown"])[0]

    # SEV1 + high latency -> rate-limit to stop cascade
    if severity == "SEV1" and (signals.get("p99_latency_s") or 0) > 3.0:
        return [{
            "action": "Enable rate-limiting to prevent cascading failure",
            "tool": "set_feature_flag",
            "params": {"flag": "rate_limiting_enabled", "value": True, "service": service},
            "blast_radius": "pod",
            "reversible": True,
            "requires_approval": False,
            "executed": False,
            "result": None,
        }]

    # SEV1 unknown -> rollback to last good deployment
    if severity == "SEV1":
        return [{
            "action": "Rollback to last good deployment",
            "tool": "rollback_deployment",
            "params": {"revision": "previous", "service": service},
            "blast_radius": "service",
            "reversible": True,
            "requires_approval": True,
            "executed": False,
            "result": None,
        }]

    # SEV2 -> capture diagnostics + notify
    if severity == "SEV2":
        return [{
            "action": "Notify on-call and capture diagnostics",
            "tool": "notify",
            "params": {"channel": "incidents", "severity": "warning", "message": f"SEV2 incident on {service} requires investigation"},
            "blast_radius": "pod",
            "reversible": True,
            "requires_approval": False,
            "executed": False,
            "result": None,
        }, {
            "action": "Capture thread dump and heap diagnostics",
            "tool": "capture_diagnostics",
            "params": {"service": service},
            "blast_radius": "pod",
            "reversible": True,
            "requires_approval": False,
            "executed": False,
            "result": None,
        }]

    # default -> capture diagnostics for human investigation
    return [{
        "action": "Capture thread dump and heap diagnostics",
        "tool": "capture_diagnostics",
        "params": {"service": service},
        "blast_radius": "pod",
        "reversible": True,
        "requires_approval": False,
        "executed": False,
        "result": None,
    }]


def _check_correlated_rb_consensus(state: AgentState) -> Optional[str]:
    """
    if this incident has correlated alerts,,and they all share the same
    runbook label, that a strong deterministric signal, one root cause, 
    one fix, no need for extra reasoning
    """
    incident_id = state["incident_id"]
    corr = state.get("correlated_alerts", [])
    if not corr:
        return state.get("runbook_id")
    
    rb_seen = {state.get("runbook_id")}
    for entry in corr:
        rb = entry.get("runbook_id")
        if rb:
            rb_seen.add(rb)
    
    if len(rb_seen) == 1:
        return rb_seen.pop()
    return None


#task 3.4: extraction helpers for the critic retry path

def _item_from_state_fields(state: AgentState) -> Optional[list]:
    """build the action item from the CURRENT proposed_* / llm_suggested_action
    fields (written by deep_investigation's writeback or the repair hop)."""
    tool, act = state.get("proposed_tool"), state.get("llm_suggested_action")
    if not tool or not act or tool in ("escalate", "escalate_with_reason"):
        return None
    service = state.get("proposed_target") or (state.get("affected_services") or ["unknown"])[0]
    blast = state.get("proposed_blast_radius") or classify_blast_radius({"tool": tool, "params": {"service": service}})
    item = {
        "action": act,
        "tool": tool,
        "params": {"service": service},
        "blast_radius": blast,
        "reversible": tool in REVERSIBLE_WRITE_TOOLS,
        "requires_approval": False,
        "executed": False,
        "result": None,
    }
    item["requires_approval"] = requires_human_approval(item)
    return [item]


def repair_proposal_node(state: AgentState) -> dict:
    """critic rejected the proposal (empty/ungrounded): ONE structured LLM call
    (temperature 0, zero tool rounds, no re-investigation) rewrites the proposal
    fields against the critique. a full re-investigation stays reserved for when
    this also fails (route_after_critic). SEV1 pages the human at the first hop
    so retries never delay the page (communicator already generated
    escalation_message in parallel with the detector; escalate dedupes)."""
    print(f"\n[repair] Critic rejected #{state.get('critic_reject_count', 0)} "
          f"— cheap one-call repair (no tool rounds)")
    out: dict = {"repair_attempts": (state.get("repair_attempts") or 0) + 1}
    if _early_page(state):
        out["early_paged"] = True

    scratchpad = "\n".join(str(e) for e in state.get("investigation_scratchpad", [])[-12:])
    try:
        from langchain_core.messages import SystemMessage, HumanMessage
        from src.agents.utils import _get_llm
        from src.agents.schemas import RemediationProposal
        resp = _get_llm(temperature=0.0, simple=True).with_structured_output(
            RemediationProposal, include_raw=False).invoke([
            SystemMessage(
                content="You are repairing a remediation proposal the critic rejected. "
                "Use only facts from the investigation scratchpad; satisfy the critic's "
                "rejection exactly. tool must be a whitelisted write tool (set_feature_flag, "
                "rollback_deployment, rolling_restart, capture_diagnostics, notify, "
                "execute_kg_suggestion, kubectl_patch_deployment, kubectl_restart_deployment, "
                "kubectl_scale). If no valid proposal is possible, use tool='capture_diagnostics' "
                "and put 'escalate: <reason>' in the action."
            ),
            HumanMessage(
                content=f"CRITIC REJECTION: {state.get('critique') or 'unspecified'}\n"
                        f"PROPOSED SO FAR: tool={state.get('proposed_tool')}, "
                        f"target={state.get('proposed_target')}\n\nSCRATCHPAD:\n{scratchpad}"
            ),
        ])
    except Exception as e:
        print(f"[repair] repair LLM failed: {e}")
        return out

    tool = (getattr(resp, "tool", None) or "").strip()
    if not tool or tool in ("escalate", "escalate_with_reason"):
        print("[repair] repair produced no valid tool — leaving fields for the router ladder")
        return out
    print(f"[repair] repaired proposal: tool={tool} target={getattr(resp, 'target_service', None)}")
    out.update({
        "proposed_tool": tool,
        "proposed_target": getattr(resp, "target_service", None),
        "llm_suggested_action": getattr(resp, "action", None) or tool,
        "proposed_blast_radius": getattr(resp, "blast_radius", None),
    })
    return out


def _early_page(state: AgentState) -> bool:
    """deliver the human page NOW instead of after every retry hop: the
    communicator runs in parallel with the detector and already wrote
    escalation_message — the relay costs one notify. Not terminal; the agent
    keeps working. escalate_node skips the duplicate page (early_paged)."""
    if state.get("severity") != "SEV1" or state.get("early_paged"):
        return False
    finding = state.get("root_cause") or state.get("diagnosis_summary") or "root cause under investigation"
    detail = state.get("escalation_message") or f"Alert: {state.get('alert_name')} | Finding: {finding}"
    message = (
        f" *Early page — agent still working*\n"
        f"Incident: {state.get('incident_id')} | Severity: SEV1\n"
        f"{detail}\n"
        f"Action Required: acknowledge now; cancel when the automated fix verifies. "
        f"The agent continues investigating in the background."
    )
    notify_slack(message=message, channel="incidents", severity="critical",
                 incident_id=state.get("incident_id"))
    print("[repair] EARLY PAGE sent (human can cancel before fixes execute)")
    return True


def _extract_from_scratchpad(state: AgentState) -> Optional[list]:
    """find the last propose_remediation call in the scratchpad and copy its args into an action plan."""
    entries = [str(e) for e in state.get("investigation_scratchpad", []) if "propose_remediation" in str(e)]
    if not entries:
        return None
    blob = entries[-1]
    args = dict(re.findall(r"(\w+)\s*=\s*['\"]([^'\"]+)['\"]", blob))
    tool = args.get("tool")
    if not tool:
        return None
    service = args.get("target_service") or (state.get("affected_services") or ["unknown"])[0]
    action = {
        "action": args.get("action", tool),
        "tool": tool,
        "params": {"service": service},
        "blast_radius": args.get("blast_radius") or classify_blast_radius({"tool": tool, "params": {"service": service}}),
        "reversible": tool in REVERSIBLE_WRITE_TOOLS,
        "requires_approval": False,
        "executed": False,
        "result": None,
    }
    action["requires_approval"] = requires_human_approval(action)
    return [action]


def _llm_extract_proposal(state: AgentState) -> Optional[list]:
    """no proposal in scratchpad -> one LLM call with scratchpad + critique to synthesize it."""
    try:
        from langchain_core.messages import SystemMessage, HumanMessage
        from src.agents.utils import _get_llm
        from src.agents.schemas import RemediationProposal
    except ImportError:
        return None

    scratchpad = "\n".join(str(e) for e in state.get("investigation_scratchpad", [])[-20:])
    critique = state.get("critique") or "no critique"
    try:
        from src.agents.utils import _get_llm
        from src.agents.schemas import RemediationProposal
        resp = _get_llm(temperature=0.0, simple=True).with_structured_output(
            RemediationProposal, include_raw=False).invoke([
            # every LLM call must start with a system message (prompt-channel fix from trace INC-SLOBur-0F86)
            SystemMessage(content="Extract the remediation proposal from the investigation scratchpad. Return the tool call the investigator intended."),
            HumanMessage(content=f"CRITIC: {critique}\n\nSCRATCHPAD:\n{scratchpad}"),
        ])
    except Exception as e:
        print(f"[remediator] LLM proposal extraction failed: {e}")
        return None

    tool = getattr(resp, "tool", None)
    if not tool:
        return None
    service = getattr(resp, "target_service", None) or (state.get("affected_services") or ["unknown"])[0]
    action = {
        "action": getattr(resp, "action", None) or tool,
        "tool": tool,
        "params": {"service": service},
        "blast_radius": getattr(resp, "blast_radius", None) or classify_blast_radius({"tool": tool, "params": {"service": service}}),
        "reversible": tool in REVERSIBLE_WRITE_TOOLS,
        "requires_approval": False,
        "executed": False,
        "result": None,
    }
    action["requires_approval"] = requires_human_approval(action)
    return [action]