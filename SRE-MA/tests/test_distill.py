"""self-checks for the distillation loop (Bet 5 / D-06)."""

from src.learning import distill


def _q(tmp_path, monkeypatch):
    q = tmp_path / "learning_queue.jsonl"
    monkeypatch.setattr(distill, "_QUEUE_PATH", q)
    return q


def _state(**over):
    st = {
        "alert_name": "PaymentGatewayP99LatencyHigh",
        "affected_services": ["payment_gateway"],
        "root_cause": "card_rails throttling held connections",
        "action_taken": "set_feature_flag checkout to healthy variant",
        "resolution_cause": "agent_remediated",
        "verified": True,
        "severity": "SEV2",
        "raw_signals": {"p99_latency_s": 3.5, "error_rate": 0.12},
        "hypotheses": [{"evidence": ["a", "b"]}],
        "policy_rejections": [],
    }
    st.update(over)
    return st


def test_fingerprint_is_stable_and_shape_sensitive(tmp_path, monkeypatch):
    _q(tmp_path, monkeypatch)
    a = distill.fingerprint("AlertX", "svc", {"p99": 3.5, "error_rate": 0.1})
    b = distill.fingerprint("AlertX", "svc", {"error_rate": 0.9, "p99": 9.9})
    c = distill.fingerprint("AlertY", "svc", {"p99": 3.5, "error_rate": 0.1})
    assert a == b          # shape repeats even when values differ
    assert a != c          # different alert class -> different shape


def test_enqueue_is_human_gated_and_dedupes(tmp_path, monkeypatch):
    _q(tmp_path, monkeypatch)
    distill.enqueue(_state())
    distill.enqueue(_state())  # same shape -> deduped
    drafts = distill.pending_drafts()
    assert len(drafts) == 1
    assert drafts[0]["status"] == "pending"
    assert drafts[0]["provenance"]["evidence_lines"] == 2


def test_empty_terminal_state_is_not_teachable(tmp_path, monkeypatch):
    _q(tmp_path, monkeypatch)
    distill.enqueue(_state(root_cause="", action_taken=""))
    assert distill.pending_drafts() == []


def test_approval_applies_and_correction_wins(tmp_path, monkeypatch):
    _q(tmp_path, monkeypatch)
    distill.enqueue(_state())
    d = distill.pending_drafts()[0]
    out = distill.approve_draft(d["draft_id"], "oncall-engineer",
                                edited_root_cause="human-corrected cause")
    assert out["ok"] is True
    assert distill.pending_drafts() == []
    mem = distill.retrieve_similar("PaymentGatewayP99LatencyHigh", "payment_gateway",
                                   {"p99_latency_s": 9.9})
    assert mem and mem[0]["root_cause"] == "human-corrected cause"
    assert mem[0]["approved_by"] == "oncall-engineer"


def test_rejected_memory_never_retrieves(tmp_path, monkeypatch):
    _q(tmp_path, monkeypatch)
    distill.enqueue(_state())
    d = distill.pending_drafts()[0]
    with open(distill._QUEUE_PATH) as f:
        import json as _json
        recs = [_json.loads(l) for l in f if l.strip()]
    recs[0]["status"] = "rejected"
    with open(distill._QUEUE_PATH, "w") as f:
        for r in recs:
            f.write(_json.dumps(r) + "\n")
    assert distill.retrieve_similar("PaymentGatewayP99LatencyHigh", "payment_gateway", {}) == []
