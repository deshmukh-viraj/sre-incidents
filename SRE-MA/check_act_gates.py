"""assert-based self-check for the new act-rule gates (post-mortem replay)."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["SRE_ENVIRONMENT"] = "sandbox"
os.environ.pop("MULTI_AGENT", None)
os.environ.pop("VERIFIER_ENABLED", None)

from src.graph.policy import policy_validator_node, _citations_for_target
from src.tools.backends.registry import service_tier
from src.graph.routing import classify_blast_radius, requires_human_approval

# --- tier lookup ---
assert service_tier("cart") == "stateless_app", service_tier("cart")
assert service_tier("valkey-cart") == "stateful_data"
assert service_tier("prometheus") == "monitoring"
assert service_tier("unknown_service") is None
print("tier lookup: OK")

# --- blast + approval pre-gate ---
assert classify_blast_radius({"tool": "set_feature_flag", "params": {}}) == "service"
# eligible tool, service blast, reversible -> passes the pre-gate (policy tier gate decides)
assert requires_human_approval({"tool": "set_feature_flag", "reversible": True}) is False
# kubectl_scale is not an autonomy-eligible action type -> human
assert requires_human_approval({"tool": "kubectl_scale", "reversible": True}) is True
print("blast/approval pre-gate: OK")

def flag_action(target="cart"):
    return {"action": "restore cart flag to default variant", "tool": "set_feature_flag",
            "params": {"service": target, "flag": "cartFailure", "variant": "default"},
            "blast_radius": "service", "reversible": True, "requires_approval": False,
            "executed": False, "result": None}

# --- THE POST-MORTEM CASE: telemetry empty, verifier unverifiable, evidence does not implicate target ---
state = {
    "incident_id": "INC-PM", "diagnosis_mode": "deep_agents",
    "affected_services": ["cart"], "hypotheses": [{"confidence": 0.65, "evidence": [
        'http_requests_total{service="payment_gateway"} -> ERROR: no data',   # wrong-env query
        "unverified claim"]}],
    "action_plan": [flag_action()], "requires_approval": False,
    "executed_action_keys": [], "verifier_result": {"verdict": "unverifiable", "confidence": 0.0, "grounded": False},
}
out = policy_validator_node(state)
assert out["requires_approval"] is True, "post-mortem case MUST human-gate"
assert out["action_plan"][0]["requires_approval"] is True
print("post-mortem replay (no evidence + unverifiable verifier) -> human gate: OK")

# --- healthy case: grounded verifier + >=2 citations implicating cart -> autonomous ---
state_ok = {
    "incident_id": "INC-OK", "diagnosis_mode": "deep_agents",
    "affected_services": ["cart"], "hypotheses": [{"confidence": 0.75, "evidence": [
        'http_server_request_duration_seconds_bucket{service_name="cart"} -> 0.91',
        'feature_flag_evaluation_success_total{service_name="cart",feature_flag_key="cartFailure"} -> variant=failure-ish',
        "p99_latency_s=2.9 above internal warning threshold 1.5"]}],
    "action_plan": [flag_action()], "requires_approval": False,
    "executed_action_keys": [], "verifier_result": {"verdict": "upheld", "confidence": 0.8, "grounded": True},
}
assert len(_citations_for_target(state_ok, "cart")) >= 2
out2 = policy_validator_node(state_ok)
assert out2["requires_approval"] is False, f"healthy grounded case must stay autonomous: {out2}"
print("grounded + cited flag flip on stateless tier -> autonomous: OK")

# --- stateful/monitoring/unknown targets always human ---
for tgt, tier in [("valkey-cart", "stateful_data"), ("prometheus", "monitoring"), ("mystery", None)]:
    s = {**state_ok, "action_plan": [flag_action(tgt)], "verifier_result": {"verdict": "upheld", "grounded": True}}
    o = policy_validator_node(s)
    assert o["requires_approval"] is True, f"{tgt} ({tier}) must human-gate"
print("stateful / monitoring / unknown-tier targets -> human gate: OK")

# --- stateful restart that used to run autonomously (RB-002 shape) ---
s = {"incident_id": "INC-RB2", "affected_services": ["account_ledger"],
     "hypotheses": [{"confidence": 0.9, "evidence": []}],
     "action_plan": [{"action": "rolling restart", "tool": "rolling_restart",
                      "params": {"service": "account_ledger"}, "blast_radius": "service",
                      "reversible": True, "requires_approval": False, "executed": False, "result": None}],
     "executed_action_keys": []}
assert policy_validator_node(s)["requires_approval"] is True
print("rolling restart on stateful target -> human gate: OK")

print("\nALL GATE SELF-CHECKS PASSED")
