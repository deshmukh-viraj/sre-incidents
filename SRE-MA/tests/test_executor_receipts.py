"""B6 executor typed receipts (P6): DRY_RUN preserved, no actuation added."""
from src.agents import execution_agent
from src.agents.schemas import ExecutionReceipt
from src.graph.clear_time import ClearEvidence
from tests.test_recovery_checker import _exec_state  # shared executor fixture


def _setup(monkeypatch):
    monkeypatch.setattr(execution_agent.time, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(execution_agent, "_verify_recovery",
                        lambda **kw: execution_agent.VerificationResult(verified=False))
    monkeypatch.setattr(execution_agent, "build_clear_evidence",
                        lambda **kw: ClearEvidence(t_clear_true=None, clear_source="never_cleared"))
    monkeypatch.setattr("src.tools.sre_tool.DRY_RUN", True)


def test_flag_off_no_receipts(monkeypatch):
    monkeypatch.delenv("EXECUTOR_RECEIPTS_ENABLED", raising=False)
    monkeypatch.delenv("MULTI_AGENT", raising=False)
    _setup(monkeypatch)
    out = execution_agent.execute_node(_exec_state())
    assert "receipts" not in out


def test_flag_on_emits_typed_dry_run_receipts(monkeypatch):
    monkeypatch.setenv("EXECUTOR_RECEIPTS_ENABLED", "1")
    _setup(monkeypatch)
    out = execution_agent.execute_node(_exec_state())
    assert len(out["receipts"]) == 1
    r = ExecutionReceipt(**out["receipts"][0])  # round-trips the typed contract
    assert r.dry_run is True and r.success is True and r.idem_key
    # DRY_RUN preserved: no actuation happened
    assert "[DRY-RUN]" in (r.result or "")


def test_no_new_network_calls_in_receipt_path(monkeypatch):
    """grep-proof in behavior: the receipt is built only from the existing
    execute_remediation result — block urllib to prove no new egress."""
    import urllib.request
    def _blocked(*a, **k):
        raise AssertionError("executor receipt path made a network call")
    monkeypatch.setattr(urllib.request, "urlopen", _blocked)
    monkeypatch.setenv("EXECUTOR_RECEIPTS_ENABLED", "1")
    _setup(monkeypatch)
    out = execution_agent.execute_node(_exec_state())
    assert out["receipts"][0]["success"] is True
