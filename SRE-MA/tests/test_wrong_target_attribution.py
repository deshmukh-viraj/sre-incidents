"""
fires a sysnthetic PaymentGatewayP99LatencyCritical alert labeled with a test only runbook 
whose action targets the wrong service (account ledger instead of payment gateway).
meanwhile natural_calm_transient_spikes interruptible-false governs the real recovery independently.

expected result: resolution_cause should be action_failed or ambiguos, NEVER the agent_remediated
"""

import json 
import time
import urllib.request

AGENT_WEBHOOK = "http://localhost:8080/webhook/alert"
CONTROL_API = "http://localhost:8001"

def trigger_natural_calm():
    req = urllib.request.Request(
        f"{CONTROL_API}/control/trigger/natural_calm_transient_spike",
        method="POST", data=b""
    )
    urllib.request.urlopen(req, timeout=5)
    print("[test] natural_calm_transient_spike triggered")

def fire_synthetic_alert():
    payload = {
        "status": "firing",
        "alerts": [{
            "status": "firing",
            "labels": {
                "alertname": "PaymentGatewayP99LatencyCritical",
                "severity": "critical",
                "service": "payment_gateway",
                "team": "payments",
                "runbook": "TEST-WRONGTARGET",
            },
            "annotations": {
                "summary": "TEST: forcing wrong-target action for attribution validation"
            },
            "startsAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        }]
    }

    req = urllib.request.Request(AGENT_WEBHOOK,  method="POST", data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
    resp = urllib.request.urlopen(req, timeout=5)
    body = json.loads(resp.read().decode())
    print(f"[test] synthetic alert fired -> {body}")
    return body.get("incident_id")


def main():
    print("="*60)
    print("test-2: wrong-target action vs independent natural recovery")
    print("="*60)

    trigger_natural_calm()
    time.sleep(2)
    incident_ids = fire_synthetic_alert()
    
    if not incident_ids:
        print("[test] Alert was debounced or correlated to an active incident.")
        return

    incident_id = incident_ids[0] if isinstance(incident_ids, list) else incident_ids
    print(f"[test] Created incident: {incident_id}")
    
    #poll for incident completion
    for _ in range(20):
        time.sleep(5)
        try:
            req = urllib.request.Request(f"http://localhost:8080/incidents/{incident_id}")
            resp = urllib.request.urlopen(req, timeout=5)
            data = json.loads(resp.read().decode())
            status = data.get("status")
            res_cause = data.get("resolution_cause")
            if status in ("resolved", "executed_unverified", "failed", "escalated"):
                print(f"\n[test] Incident finished with status='{status}', resolution_cause='{res_cause}'")
                assert res_cause != "agent_remediated", f"FAILURE: expected resolution_cause != agent_remediated, got '{res_cause}'"
                print("[test] SUCCESS: wrong-target attribution test passed successfully!")
                break
        except Exception as e:
            print(f"[test] waiting for incident completion: {e}")

if __name__=="__main__":
    main()



'''
run the wring targt test, feed that logs to llm, and after getting correct logs, ask the question, are we doing offline evaluation?? we have these test, mean we are evaluating the system, so why we do not have a separate evaluation file, why these test??? also if we want to evaluate our system, then langfuse has evaluators, can we evaluate there without writing such files???

clear all the fogs i have, and explain all the things correctly and technically,
'''

"""
do you remember i said you, agent should think, reason, find the root cause, then implement action and solve the all the firing incidents or alerts,??? so how we are going to implement that, as i see we are using the python scripts all the way, agent be like when receives an alerts, it should say... i got these alerts on firing, let me find the root cause, then agent should check all the metrics, thinking smartly, reason it, and then the action plan or solve the root cause, and by solving the root cause, after grace period all those firing should stop gradually, not like getting one by one alerting and solve for each.....

so how and when we are going to provide its brain to agent, such that it act independently to think, reason, and find the root cause, and to take action by accessing tools, 

this is my question, now answer this very profound way without lying and be on point, based on the conversation so far and the system architechute...also should i fix the previous bug first?? then you answer, or this question and that bug fixing is independent...
"""