"""
test_writeback_and_critic.py — regression tests for INC-SLOBur-FF52 (trace 4afddcd7)

Flow that failed in production:
    diagnoser (no pattern) -> deep_investigation #1 concluded by calling
    escalate_with_reason with a fully-evidenced finding -> writeback returned
    hypotheses:null; root_cause:null; evidence_summary:null -> critic saw
    "hypotheses: []" and rejected "empty proposal on SEV2" -> deep_investigation #2
    burning the full budget again on the same evidence -> proposal with
    tool=config_change -> critic rejected twice, but route_after_critic sent the
    double-rejected state to the remediator with an empty plan -> policy
    escalated with "Finding: no diagnosis reached" despite the agent having a
    complete elimination analysis.

Grounded on the real trace payloads. no LLM, no docker, no neo4j.
"""
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.agents.deep_investigation import deep_investigation_node, _tool_evidence
from src.agents.critic_agent import critic_node
from src.agents.remediator_agent import remediator_node, repair_proposal_node
from src.graph.policy import route_after_critic, policy_validator_node
from src.graph.state import AgentState, initial_state
from src.graph.routing import classify_severity

# ---------------- fixtures: the real INC-SLOBur-FF52 payloads ----------------

RAW_FF52 = {
    "alertname": "SLOBurnUnknown",
    "alert_name": "SLOBurnUnknown",
    "severity": "warning",
    "service": "payment_gateway",
    "runbook": None,
    "team": "platform",
    "p99_latency_s": 2.8298260869565057,
    "p50_latency_s": 0.2278562361155189,
    "error_rate": 0.027702243371855877,
    "throttle_error_rate": 0.00963730155556444,
    "rps": 344.48,
    "circuit_breaker_state": 0,
    "slo_budget_remaining": 0.87,
    "db_pool_utilization": 0.7,
    "log_patterns": {"http_429_count": 0, "circuit_breaker_open": False},
    "error_log_summaries": [],
    "metrics_ok": True,
    "logs_ok": False,
    "shadow_execution": False,
}

# verbatim finding text harvested from the FF52 trace (escalate_with_reason call)
FINDING_FF52 = (
    "Root cause NOT conclusively identified — escalating with full elimination evidence. "
    "CONFIRMED: payment_gateway p99=3.16s vs p50=0.28s (pure long-tail, not systemic slowdown); "
    "RPS normal at ~345 (no load spike); SLO budget 0.87-0.89 remaining. RULED OUT: (1) account_ledger "
    "p99=0.05s and fraud_detector p99=0.069s — dependencies fast; (2) card_rails throttling — "
    "circuit_breaker_state=0.0 closed; (3) slow DB — db_query_duration p99=0.049s on postgres_primary; "
    "(4) pool exhaustion/leak — db_connection_pool_active fluctuating 79→65 of max 100."
)


def _base_state(**kw):
    s = {
        "incident_id": "INC-SLOBur-FF52",
        "raw_signals": dict(RAW_FF52),
        "alert_name": "SLOBurnUnknown",
        "severity": "SEV2",
        "affected_services": ["payment_gateway"],
        "hypotheses": [],
        "root_cause": None,
        "diagnosis_summary": None,
        "evidence_summary": None,
        "diagnosis_mode": None,
        "diagnosis_loops": 1,
        "requires_retry": False,
        "critic_reject_count": 0,
        "critique": None,
        "escalate_reason": None,
    }
    s.update(kw)
    return s


class Msg(SimpleNamespace):
    pass


def _msg(type, content, tool_calls=None, tool_call_id=None):
    return Msg(
        type=type,
        content=content,
        tool_calls=tool_calls,
        tool_call_id=tool_call_id,
        response_metadata={},
        usage_metadata={"input_tokens": 10, "output_tokens": 5},
    )


# ---------------- tests 1 & 2: deep_investigation structured writeback ----------------

def test_deep_escalation_writeback_populates_findings():
    """deep#1 of FF52: agent called escalate_with_reason; the finding must reach
    hypotheses/root_cause/evidence_summary so critic and the escalator can use it."""
    msgs = [
        _msg("system", "YOU ARE AN EXPERT SRE INVESTIGATING AN INCIDENT..."),
        _msg("ai", "Investigating p99 tail latency..."),
        _msg("ai", "Tool round complete...", tool_calls=[
            {"name": "query_prometheus_metric",
             "args": {"expr": "db_connection_pool_active{db=\"postgres_primary\"}"},
             "id": "call_pool"},
        ]),
        _msg("tool", "79.0", tool_call_id="call_pool"),
        _msg("ai", "", tool_calls=[{"name": "escalate_with_reason", "args": {"reason": FINDING_FF52}, "id": "call_esc"}]),
    ]
    with patch("src.agents.deep_investigation.get_harness") as gh:
        gh.return_value = SimpleNamespace(invoke=lambda *a, **k: {"messages": msgs})
        out = deep_investigation_node(_base_state())

    assert out.get("hypotheses"), f"critic saw hypotheses:[] — writeback missing: {out.get('hypotheses')}"
    hyp = out["hypotheses"][0]
    assert FINDING_FF52[:60] in hyp["hypothesis"]
    assert hyp["confidence"] > 0
    assert hyp["evidence"], "hypothesis carries zero evidence"
    flattened = " | ".join(str(e) for e in hyp["evidence"]).lower()
    # grounding: evidence must cite the live tool observations and/or live signal keys
    assert "db_connection_pool_active" in flattened
    assert "p99" in flattened
    assert out.get("root_cause") == out["hypotheses"][0]["hypothesis"]
    assert out.get("evidence_summary"), "critic cannot check grounding with an empty evidence_summary"
    assert out.get("escalate_reason") == "agent_escalated", "critic must not demand a re-run of a deliberate specialist escalation"


def test_deep_proposal_writeback_grounded_evidence():
    """deep#2 of FF52: proposal path must write evidence from observed tool calls,
    not just echo the action string back into evidence."""
    action = "MITIGATE card_rails-throttle-induced connection-hold on payment_gateway"
    msgs = [
        _msg("system", "YOU ARE AN EXPERT SRE..."),
        _msg("ai", "", tool_calls=[
            {"name": "query_prometheus_metric",
             "args": {"expr": "db_connection_pool_active{db=\"postgres_primary\"}"},
             "id": "call_pool"},
        ]),
        _msg("tool", "79.0", tool_call_id="call_pool"),
        _msg("ai", "", tool_calls=[
            {"name": "propose_remediation",
             "args": {"action": action, "tool": "config_change", "target_service": "payment_gateway"},
             "id": "call_prop"},
        ]),
    ]
    with patch("src.agents.deep_investigation.get_harness") as gh:
        gh.return_value = SimpleNamespace(invoke=lambda *a, **k: {"messages": msgs})
        out = deep_investigation_node(_base_state())

    assert out.get("hypotheses"), "proposal must produce a hypothesis"
    hyp = out["hypotheses"][0]
    assert hyp["hypothesis"] == action
    flattened = " | ".join(str(e) for e in hyp["evidence"]).lower()
    assert "db_connection_pool_active" in flattened, f"evidence must include live tool observations, got: {hyp['evidence']}"
    assert "p99" in flattened, "evidence must include the live signal bullets"
    assert out.get("root_cause") == action
    assert out.get("evidence_summary"), "evidence_summary must be populated for the critic"


# ---------------- tests 3 & 4: critic specificity (crosscheck removed) ----------------

def test_critic_rejection_names_the_failing_check():
    """critic#2 of FF52: 'ungrounded proposal: tool=config_change, target=payment_gateway'
    must say WHY config_change is rejected."""
    state = _base_state(
        proposed_tool="config_change",
        proposed_target="payment_gateway",
        llm_suggested_action="MITIGATE card_rails-throttle-induced connection-hold on payment_gateway",
        hypotheses=[{"hypothesis": "pool connection holds", "evidence": ["p99_latency_s=2.830 exceeds threshold"], "confidence": 0.65}],
    )
    out = critic_node(state)
    critique = out.get("critique", "")
    assert "config_change" in critique
    assert "not in allowed" in critique.lower(), f"rejection must name the failing check (allowed-list), got: {critique}"
    assert out.get("requires_retry"), f"disallowed tool must actually be rejected, got: {out.get('requires_retry')}"


def test_critic_does_not_reject_negation_claims():
    """INC-SLOBur-633F (trace b3c0f072): the deterministic crosscheck matched '429'
    inside the negation 'card_rails throttling (no live 429 traffic)' and rejected a
    legitimate plan twice. String-matching natural language is the mechanism that
    failed; mechanism correctness now lives in simulator-side action verification.
    Guards against re-adding a brittle text crosscheck."""
    state = _base_state(
        proposed_tool="set_feature_flag",
        proposed_target="payment_gateway",
        llm_suggested_action="Disable the aggressive card_rails throttling (no live 429 traffic observed)",
        hypotheses=[{"hypothesis": "no 429 throttling", "evidence": ["throttle_error_rate=0.010 within threshold"], "confidence": 0.65}],
    )
    out = critic_node(state)
    assert not out.get("requires_retry"), f"negated 429 claim must not be string-matched into a rejection, got: {out.get('critique')}"


def test_critic_specific_grounding_issue_on_hypotheses():
    """diagnosis_issues must name the check that failed, not a bare label."""
    state = _base_state(
        hypotheses=[{"hypothesis": "meteor strike", "evidence": ["the stars aligned"], "confidence": 0.9}],
    )
    out = critic_node(state)
    issues = out.get("diagnosis_issues") or []
    assert issues, "ungrounded hypothesis evidence must be flagged"
    assert "the stars aligned" in issues[0] or "ungrounded" in issues[0].lower(), f"detail expected, got: {issues}"


# ---------------- tests 6 & 7: critic rejection ladder (routes) ----------------

def test_route_after_critic_single_reject_goes_to_repair():
    """ladder rung 2: a first rejection becomes ONE cheap repair call, not a
    full re-investigation."""
    state = {
        "critic_reject_count": 1,
        "requires_retry": True,
        "escalate_reason": None,
        "critique": "empty proposal on SEV2: missing proposed_tool and action text",
        "action_plan": [],
    }
    assert route_after_critic(state) == "repair_proposal"


def test_route_after_critic_after_repair_conf_high_goes_to_remediator():
    """ladder rung 3: repair failed but a confident hypothesis already exists —
    let the deterministic remediator build the runbook plan."""
    state = {
        "critic_reject_count": 2,
        "requires_retry": True,
        "escalate_reason": None,
        "repair_attempts": 1,
        "critique": "ungrounded proposal: tool 'config_change' not in allowed write tools",
        "hypotheses": [{"hypothesis": "pool exhaustion", "confidence": 0.8}],
        "action_plan": [],
    }
    assert route_after_critic(state) == "remediator"


def test_route_after_critic_after_repair_conf_low_goes_to_deep():
    """ladder rung 4: no confident hypothesis after repair -> full (budget-capped)
    re-investigation, not a repetitive repair."""
    state = {
        "critic_reject_count": 2,
        "requires_retry": True,
        "escalate_reason": None,
        "repair_attempts": 1,
        "critique": "ungrounded proposal: ...",
        "hypotheses": [{"hypothesis": "guess", "confidence": 0.4}],
        "action_plan": [],
    }
    assert route_after_critic(state) == "deep_investigation"


def test_route_after_critic_escalates_triple_reject():
    """hard ceiling: three rejections -> escalate; a bounded, known-cost ladder."""
    state = {
        "critic_reject_count": 3,
        "requires_retry": False,
        "escalate_reason": "critic_rejected_after_retry",
        "critique": "ungrounded proposal: ...",
        "action_plan": [],
    }
    assert route_after_critic(state) == "escalate"


def test_route_after_critic_accept_still_remediates():
    state = {
        "critic_reject_count": 0,
        "requires_retry": False,
        "escalate_reason": None,
        "critique": "no concerns",
    }
    assert route_after_critic(state) == "remediator"


def test_route_after_critic_escalates_agent_requested_escalation():
    """when the specialist concluded and asked for human escalation (including a
    budget-abort conclusion), the critic must not send it back to repair."""
    state = {
        "critic_reject_count": 1,
        "requires_retry": False,
        "escalate_reason": "agent_escalated",
        "critique": "empty proposal on SEV2: investigation concluded with an escalation request",
        "action_plan": [],
    }
    assert route_after_critic(state) == "escalate"


def test_critic_sets_escalation_reason_on_third_rejection():
    state = _base_state(
        proposed_tool="config_change",
        proposed_target="payment_gateway",
        llm_suggested_action="some fix",
        hypotheses=[{"hypothesis": "pool", "evidence": ["p99_latency_s=2.9"], "confidence": 0.65}],
        critic_reject_count=2,
        requires_retry=True,
    )
    out = critic_node(state)
    assert out["critic_reject_count"] == 3
    assert out["escalate_reason"] == "critic_rejected_after_retry"
    assert out["requires_retry"] is False


# ---------------- P0–P4 plan (trace b3c0f072 audit) ----------------

def test_p0_escalate_reason_is_declared_on_agentstate():
    """LangGraph silently drops node-return keys not declared on the state schema.
    deep_investigation writes escalate_reason and critic/route_after_critic read it:
    unless the key is annotated, agent_escalated died at the channel boundary
    (INC-SLOBur-633F: critic#1 saw null and re-ran a full budget into the same wall)."""
    assert "escalate_reason" in AgentState.__annotations__, \
        "escalate_reason must be declared on AgentState or LangGraph drops it"
    state = initial_state("INC-X", {"alert_name": "a"})
    assert "escalate_reason" in state, "channel never registered -> KeyError risk on reads"

    # the dict-merge semantics LangGraph uses on plain channels: last-write-wins, nothing dropped
    merged: dict = dict(initial_state("INC-X", {"alert_name": "a"}))
    merged.update({"escalate_reason": "agent_escalated"})
    assert merged["escalate_reason"] == "agent_escalated"


def test_p1_tool_evidence_keeps_metric_and_labels():
    """'sum -> 0.012' told the critic nothing: split at the first paren stripped the
    function's argument (metric + labels). Evidence must cite metric{labels}."""
    msgs = [
        _msg("ai", "", tool_calls=[
            {"name": "query_prometheus_metric",
             "args": {"expr": "sum(rate(http_request_duration_seconds_bucket{service=\"payment_gateway\", job=\"api\"}[1m])) by (le)"},
             "id": "c1"},
        ]),
        _msg("tool", "2.829", tool_call_id="c1"),
        _msg("ai", "", tool_calls=[
            {"name": "query_prometheus_metric",
             "args": {"expr": "db_connection_pool_active{db=\"postgres_primary\"}"},
             "id": "c2"},
        ]),
        _msg("tool", "79.0", tool_call_id="c2"),
    ]
    ev = _tool_evidence(msgs)
    assert ev[0].startswith("http_request_duration_seconds_bucket{service=\"payment_gateway\", job=\"api\"} ->"), \
        f"metric+labels must survive, got: {ev[0]}"
    assert not ev[0].startswith("sum"), f"wrapping function must not replace the metric: {ev[0]}"
    assert ev[1].startswith("db_connection_pool_active{db=\"postgres_primary\"} -> 79.0"), f"got: {ev[1]}"


def test_p1_evidence_summary_not_duplicated_with_finding():
    """the finding already lands in root_cause/diagnosis_summary/hypothesis; the
    evidence_summary must carry observations only."""
    msgs = [
        _msg("ai", "", tool_calls=[
            {"name": "query_prometheus_metric",
             "args": {"expr": "db_connection_pool_active{db=\"postgres_primary\"}"},
             "id": "call_pool"},
        ]),
        _msg("tool", "79.0", tool_call_id="call_pool"),
        _msg("ai", "", tool_calls=[{"name": "escalate_with_reason", "args": {"reason": FINDING_FF52}, "id": "call_esc"}]),
    ]
    with patch("src.agents.deep_investigation.get_harness") as gh:
        gh.return_value = SimpleNamespace(invoke=lambda *a, **k: {"messages": msgs})
        out = deep_investigation_node(_base_state())

    assert out.get("evidence_summary")
    assert FINDING_FF52[:60] not in out["evidence_summary"], \
        "finding text must not be duplicated into evidence_summary (it is root_cause already)"


def test_p4_alert_label_wins_over_metrics_classification():
    """INC-SLOBur-633F: alert label 'warning' but detector-enriched live p99=3.156
    >= 3.0 pushed metrics-only classification to SEV1. The alert label is the
    paging contract → label wins; metrics only classify when no label exists."""
    b3c0_signals = dict(RAW_FF52)  # severity='warning', p99 mocked live below
    b3c0_signals["p99_latency_s"] = 3.156
    assert classify_severity({"raw_signals": b3c0_signals}) == "SEV2", \
        "warning-label alert must not be pushed to SEV1 by a live metric sample"

    assert classify_severity({"raw_signals": {"severity": "critical", "p99_latency_s": 0.2}}) == "SEV1"
    assert classify_severity({"raw_signals": {"severity": "info", "p99_latency_s": 3.2}}) == "SEV3"
    # no label -> existing metrics-only behaviour preserved
    no_label = {"p99_latency_s": 3.156}
    assert classify_severity({"raw_signals": no_label}) == "SEV1"
    assert classify_severity({"raw_signals": {"p99_latency_s": 0.4, "error_rate": 0.01}}) == "SEV3"


# ---------------- retry ladder (5 recommendations) ----------------

def test_propose_remediation_rejects_unknown_tool_in_loop():
    """the model must see REJECTED + the allowed list inside the same react loop
    instead of discovering the policy gate after a full re-investigation."""
    from src.tools.agent_tool import propose_remediation
    out = propose_remediation.invoke({"action": "fix it", "tool": "restart_service",
                                      "target_service": "payment_gateway", "blast_radius": "service"})
    assert out.startswith("REJECTED: tool 'restart_service'"), out
    assert "rolling_restart" in out, "must hand the allowed list back to the model"

    out_ok = propose_remediation.invoke({"action": "fix it", "tool": "rolling_restart",
                                         "target_service": "payment_gateway", "blast_radius": "service"})
    assert out_ok == "proposal recorded, investigation ended."


def test_repair_node_records_fields_with_no_side_effects_on_sev2():
    """#2: one structured call rewrites the rejected proposal fields; the recorded
    tool is passed through unchanged (aliasing/snapping is deliberately kept out —
    the policy gate rejects unknown tools instead of guessing)."""
    repaired = SimpleNamespace(action="restart payment_gateway deploy to clear stuck pool",
                               tool="restart_service", target_service=None, blast_radius="service")
    fake_llm = SimpleNamespace(with_structured_output=lambda schema, include_raw=True: SimpleNamespace(invoke=lambda msgs: repaired))
    with patch("src.agents.utils._get_llm", return_value=fake_llm), \
         patch("src.agents.remediator_agent.notify_slack") as ns:
        out = repair_proposal_node({
            "incident_id": "INC-R", "severity": "SEV2", "early_paged": False,
            "critic_reject_count": 1, "repair_attempts": 0,
            "critique": "ungrounded proposal: tool 'restart_service' not in allowed write tools",
            "investigation_scratchpad": ["tool: propose_remediation(...restart payment_gateway...)"],
            "affected_services": ["payment_gateway"], "proposed_tool": "restart_service",
            "proposed_target": None,
        })
    assert out["proposed_tool"] == "restart_service", "repair passes the tool through unchanged"
    assert out["llm_suggested_action"] == "restart payment_gateway deploy to clear stuck pool"
    assert out["repair_attempts"] == 1
    assert "early_paged" not in out, "SEV2 must not page"
    ns.assert_not_called()


def test_repair_node_pages_early_on_sev1_and_deduplicates():
    """#5: SEV1 must not wait for retries — the early page goes out at the first
    ladder hop using the communicator's already-generated escalation_message."""
    with patch("src.agents.utils._get_llm", side_effect=RuntimeError("llm down")), \
         patch("src.agents.remediator_agent.notify_slack") as ns:
        out = repair_proposal_node({
            "incident_id": "INC-S", "severity": "SEV1", "early_paged": False,
            "critic_reject_count": 1, "repair_attempts": 0,
            "critique": "empty proposal", "escalation_message": "pd-123: SEV1 payment outage",
            "investigation_scratchpad": [], "affected_services": ["payment_gateway"],
        })
    ns.assert_called_once()
    assert "pd-123: SEV1 payment outage" in ns.call_args.kwargs["message"], \
        "early page must reuse the communicator's parallel escalation_message"
    assert ns.call_args.kwargs["severity"] == "critical"
    assert out["early_paged"] is True, "escalate_node dedupes the page via early_paged"
    assert out["repair_attempts"] == 1, "LLM failure still counts the hop"


def test_remediator_prefers_current_proposal_fields_over_stale_scratchpad():
    """#2: after repair, remediator must rebuild from the fixed fields, not replay
    the old scratchpad alias."""
    state = {
        "requires_retry": True,
        "proposed_tool": "rolling_restart",
        "proposed_target": "payment_gateway",
        "llm_suggested_action": "rolling restart payment_gateway to clear stuck pool",
        "proposed_blast_radius": "service",
        "affected_services": ["payment_gateway"],
        "hypotheses": [{"hypothesis": "stuck pool", "confidence": 0.75}],
        "investigation_scratchpad": ["tool: propose_remediation(tool=restart_service...)"],
        "incident_id": "INC-R2",
    }
    plan = remediator_node(state)["action_plan"]
    assert plan[0]["tool"] == "rolling_restart"
    assert plan[0]["params"]["service"] == "payment_gateway"
    assert state.get("investigation_scratchpad")  # scratchpad untouched, not replayed


def test_policy_validator_rejects_unwhitelisted_tool():
    """the policy gate is the only tool-name check on non-loop paths: an unknown
    tool is rejected outright (no aliasing, no snapping)."""
    out = policy_validator_node({
        "incident_id": "INC-S",
        "affected_services": ["payment_gateway"],
        "action_plan": [{"action": "restart", "tool": "restart_service",
                         "params": {}, "blast_radius": "pod", "reversible": True,
                         "requires_approval": False, "executed": False, "result": None}],
        "hypotheses": [{"confidence": 0.9}],
        "executed_action_keys": [],
        "requires_approval": False,
    })
    assert out["action_plan"] == []
    assert out["plan_viable"] is False
    assert out["policy_rejections"], "rejection must carry a reason"


def test_escalate_does_not_repager_when_early_page_fired():
    """#5: the SEV1 early page found the human; the terminal escalation must not
    send a second page, only record the final status for cancel/take-over."""
    from src.agents.escalation_agent import escalate_node
    with patch("src.agents.escalation_agent.notify_slack") as ns:
        out = escalate_node({
            "incident_id": "INC-E", "severity": "SEV1", "alert_name": "SLOBurnUnknown",
            "early_paged": True, "hypotheses": [],
            "diagnosis_mode": "deep_agents", "diagnosis_loops": 1,
        })
    ns.assert_not_called()
    assert "early page" in out["escalation_message"].lower()
    assert out["resolution_status"] == "escalated"
    with patch("src.agents.escalation_agent.notify_slack") as ns2:
        escalate_node({
            "incident_id": "INC-E2", "severity": "SEV1", "alert_name": "SLOBurnUnknown",
            "early_paged": False, "hypotheses": [], "diagnosis_mode": "deep_agents",
            "diagnosis_loops": 1,
        })
    ns2.assert_called_once()
