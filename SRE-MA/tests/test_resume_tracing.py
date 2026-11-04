"""
test_resume_tracing.py - Unit test verifying Langfuse trace continuation across human approval & resume.
"""
from unittest.mock import patch, MagicMock
from src.observability.langfuse_logger import get_langfuse_handler, get_trace_id, log_human_feedback
from src.api.app import _incidents, _resume_agent


def test_get_langfuse_handler_with_trace_context():
    handler = get_langfuse_handler(
        session_id="INC-123",
        alert_name="PaymentLatencySpike",
        trace_id="trace-abc-123",
    )
    assert handler._trace_context == {"trace_id": "trace-abc-123"}


def test_get_trace_id_extracts_last_trace_id():
    mock_handler = MagicMock()
    mock_handler.last_trace_id = "trace-xyz-789"
    assert get_trace_id(mock_handler) == "trace-xyz-789"


def test_log_human_feedback_scores_existing_trace():
    mock_client = MagicMock()
    with patch("src.observability.langfuse_logger._get_client", return_value=mock_client):
        log_human_feedback(
            trace_id="trace-abc-123",
            approved=True,
            approver="sre_oncall",
            notes="LGTM to restart",
        )
        mock_client.create_score.assert_called_once_with(
            trace_id="trace-abc-123",
            name="human_approval",
            value=1.0,
            comment="LGTM to restart",
            data_type="BOOLEAN",
            metadata={"approver": "sre_oncall"},
        )


def test_resume_agent_passes_callbacks_with_trace_context():
    incident_id = "INC-TEST-RESUME"
    _incidents[incident_id] = {
        "incident_id": incident_id,
        "trace_id": "trace-existing-456",
        "raw_signals": {"alert_name": "HighMemory", "scenario_name": "mem_leak"},
        "severity": "SEV1",
    }

    mock_graph = MagicMock()
    mock_graph.invoke.return_value = {
        "resolution_status": "RESOLVED",
        "action_plan": [{"action": "restart"}],
    }

    with patch("src.graph.orchestrator.app", mock_graph), \
         patch("src.api.app.log_incident_run") as mock_log_incident:
        
        _resume_agent(incident_id, config={"configurable": {"thread_id": incident_id}})

        # Verify sre_graph.invoke was called
        assert mock_graph.invoke.called
        call_args, call_kwargs = mock_graph.invoke.call_args
        config_passed = call_kwargs.get("config", {})
        assert "callbacks" in config_passed
        callbacks = config_passed["callbacks"]
        assert len(callbacks) == 1
        assert callbacks[0]._trace_context == {"trace_id": "trace-existing-456"}

        # Verify final incident logging was invoked with the same trace ID
        mock_log_incident.assert_called_once()
        assert mock_log_incident.call_args[1]["trace_id"] == "trace-existing-456"


def test_log_incident_run_scores_mttr_and_resolved():
    from src.observability.langfuse_logger import log_incident_run

    mock_client = MagicMock()
    with patch("src.observability.langfuse_logger.Langfuse", return_value=mock_client):
        result = {
            "incident_id": "INC-TEST-001",
            "resolution_status": "RESOLVED",
            "business_mttr_seconds": 315,
            "agent_mttr_seconds": 250,
            "token_cost_usd": 0.0278,
            "llm_suggested_action": "DIAGNOSIS: pool contention. ACTION: scale",
        }
        log_incident_run(result, trace_id="trace-abc-123")

        # Verify create_score called with mttr_seconds = 315
        scored_names = {call.kwargs.get("name"): call.kwargs for call in mock_client.create_score.call_args_list}
        assert "mttr_seconds" in scored_names
        assert scored_names["mttr_seconds"]["value"] == 315.0
        assert scored_names["mttr_seconds"]["trace_id"] == "trace-abc-123"

        # Verify escalation_class is scored as 'resolved'
        assert "escalation_class" in scored_names
        assert scored_names["escalation_class"]["value"] == "resolved"
        assert scored_names["escalation_class"]["trace_id"] == "trace-abc-123"


def test_approve_resolved_incident_rejected():
    import pytest
    from fastapi import HTTPException, BackgroundTasks
    from src.api.app import approve_incident, _incidents
    from src.api.schema import ApprovalRequest

    incident_id = "INC-TEST-RESOLVED-1"
    _incidents[incident_id] = {
        "incident_id": incident_id,
        "status": "resolved",
    }

    bg = BackgroundTasks()
    req = ApprovalRequest(approved=True, approver="admin")

    with pytest.raises(HTTPException) as exc_info:
        import asyncio
        asyncio.run(approve_incident(incident_id, bg, req))
    assert exc_info.value.status_code == 409
    assert "already 'resolved'" in exc_info.value.detail


def test_resume_agent_skips_resolved():
    from src.api.app import _resume_agent, _incidents

    incident_id = "INC-TEST-RESOLVED-2"
    _incidents[incident_id] = {
        "incident_id": incident_id,
        "status": "resolved",
    }

    mock_graph = MagicMock()
    with patch("src.graph.orchestrator.app", mock_graph):
        _resume_agent(incident_id, config={})
        assert not mock_graph.invoke.called


