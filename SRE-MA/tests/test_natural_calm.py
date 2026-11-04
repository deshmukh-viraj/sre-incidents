import time
import sys
import urllib.request
import urllib.error
import json


CONTROL_API = "http://localhost:8001"


def trigger_scenario(scenario_name: str):
    
    url = f"{CONTROL_API}/control/trigger/{scenario_name}"
    req = urllib.request.Request(url, method="POST", data=b"", headers={"Content-Type": "application/json"})

    try:
        resp = urllib.request.urlopen(req, timeout=5)
        body = json.loads(resp.read().decode())
        print(f"[trigger] {body}")
        return True
    except urllib.error.URLError as e:
        print(f"[error] cannot reach control API at {CONTROL_API}")
        print(f"  port 8001 expose nhi hai be, docker compose up -d kiya kya??")
        print(f" detail: {e}")
        return False


def list_scenarios():
    url = f"{CONTROL_API}/control/scenarios"
    try:
        resp = urllib.request.urlopen(url, timeout=5)
        body = json.loads(resp.read().decode())
        return body.get("available_scenarios", [])
    except urllib.error.URLError:
        return []


def main():
    scenario_name = "natural_calm_transient_spike"
    print("=" * 60)
    print("Natural Calm Scenario Test")
    print("=" * 60)

    
    # check connectivity
    scenarios = list_scenarios()
    if not scenarios:
        print("\n[error] Cannot reach sim-generator control API.")
        print("        Make sure 'docker compose up -d' is running.")
        sys.exit(1)
    print(f"[info] Available scenarios: {scenarios}")
    print(f"[info] Triggering: {scenario_name}")
    print()
    if not trigger_scenario(scenario_name):
        sys.exit(1)
    print()
    print("Scenario is now running on the sim-generator container.")
    print("Prometheus is scraping the SAME container's metrics.")
    print()
    print("Watch the alert lifecycle in real time:")
    print("  -> http://localhost:9090/alerts")
    print()
    print("Expected timeline:")
    print("  T+0s  - T+60s  : ramp_up         (metrics degrade)")
    print("  T+60s - T+240s : peak_degradation (alert fires after 2m pending)")
    print("  T+240s- T+300s : natural_calming  (metrics drop, alert resolves)")
    print("  T+300s- T+330s : resolved         (baseline restored)")
    print()
    print("No agent action is taken. Resolution is purely timer-driven.")
if __name__ == "__main__":
    main()
