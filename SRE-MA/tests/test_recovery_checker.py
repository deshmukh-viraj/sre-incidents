"""B2 RecoveryChecker unit tests (P3): deterministic, mocked poll results."""
import datetime

from src.agents.schemas import VerificationResult
from src.agents import execution_agent
from src.graph.clear_time import ClearEvidence
from src.graph.recovery_checker import check_recovery

RECOVERED = VerificationResult(
    verified=True, p99_latency_s=0.31, error_rate=0.002,
    t_clear="2026-01-01T00:05:00", signal="p99", healthy_run_len=4)

NOT_RECOVERED = VerificationResult(
    verified=False, p99_latency_s=2.9, error_rate=0.12,
    t_clear=None, signal=None, healthy_run_len=0)

T_ACTION_END = "2026-01-01T00:04:30"
PRE = {"p99_latency_s": 2.9, "error_rate": 0.12}


def test_correct_recovery():
    out = check_recovery(RECOVERED, PRE, t_action_end=T_ACTION_END)
    assert out["verdict"] == "recovered"
    assert out["lead_from_action_s"] == 30.0
    assert out["checker_version"] == "recovery-checker-v1"


def test_no_recovery():
    out = check_recovery(NOT_RECOVERED, PRE, t_action_end=T_ACTION_END)
    assert out["verdict"] == "not_recovered"
    assert out["lead_from_action_s"] is None


def test_flap_still_breaching():
    out = check_recovery(
        VerificationResult(verified=True, p99_latency_s=1.8, error_rate=0.002,
                           t_clear="2026-01-01T00:05:00", signal="alertmanager",
                           healthy_run_len=4),
        PRE, t_action_end=T_ACTION_END)
    assert out["verdict"] == "flap_risk"
    assert "still_breaching=True" in out["reason"]


def test_flap_thin_run():
    out = check_recovery(
        VerificationResult(verified=True, p99_latency_s=0.31, error_rate=0.002,
                           t_clear="2026-01-01T00:05:00", signal="p99",
                           healthy_run_len=1),
        PRE, t_action_end=T_ACTION_END)
    assert out["verdict"] == "flap_risk"


def _exec_state():
    return {
        "incident_id": "INC-CHK", "alert_name": "PaymentGatewayP99LatencyHigh",
        "alert_started_at": "2026-01-01T00:00:00",
        "agent_invoked_at": "2026-01-01T00:00:00",
        "action_plan": [{
            "action": "test action", "tool": "set_feature_flag",
            "params": {"flag": "rate_limiting_enabled", "value": True,
                       "service": "payment_gateway"},
            "blast_radius": "pod", "reversible": True,
            "requires_approval": False, "executed": False, "result": None,
        }],
        "affected_services": ["payment_gateway"],
        "raw_signals": {"p99_latency_s": 2.9, "error_rate": 0.12},
        "executed_action_keys": [], "severity": "SEV2", "root_cause": "rc",
    }


def test_execute_emits_checker_result_without_changing_verdicts(monkeypatch):
    async_sleep = None
    monkeypatch.setattr(execution_agent.time, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(execution_agent, "_verify_recovery",
                        lambda **kw: RECOVERED)
    monkeypatch.setattr(execution_agent, "build_clear_evidence",
                        lambda **kw: ClearEvidence(t_clear_true=None,
                                                   clear_source="never_cleared"))
    # .env sets DRY_RUN=false at import; force the dry-run path (never touch the sim)
    monkeypatch.setattr("src.tools.sre_tool.DRY_RUN", True)

    base = execution_agent.execute_node(_exec_state())
    monkeypatch.setenv("RECOVERY_CHECKER_ENABLED", "1")
    flagged = execution_agent.execute_node(_exec_state())

    # verdicts identical with and without the checker
    for k in ("resolution_status", "verified", "resolution_cause",
              "gate4_verified", "gate5_stable"):
        assert base[k] == flagged[k], k
    assert abs((base["business_mttr_seconds"] or 0) -
               (flagged["business_mttr_seconds"] or 0)) <= 5  # wall clock between runs
    assert base["checker_result"] is None                    # flag off: no signal
    assert flagged["checker_result"]["verdict"] == "recovered"  # flag on: extra signal
    assert async_sleep is None
