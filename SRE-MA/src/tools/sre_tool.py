"""
tools/sre_tool.py
-----------------
the write side and shared helpers: collect_all_signals, lookup_runbook,
execute_remediation, notify_slack, check_alert_status and create_jira.
"""

import os
import uuid
import json
import urllib.request
import urllib.parse
from datetime import datetime, timezone
from typing import Optional, Dict, Any

from dotenv import load_dotenv

from src.tools.prometheus_tool import collect_incident_signals
from src.tools.loki_tool import (
    query_loki,
    detect_patterns,
    extract_log_summary,
)
from src.tools.backends.registry import get_backend

load_dotenv()

try:
    from langfuse import observe
except ImportError:
    def observe(*args, **kwargs):
        def decorator(f):
            return f
        return decorator

DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"
JIRA_URL = os.getenv("JIRA_URL")
JIRA_TOKEN = os.getenv("JIRA_TOKEN")
JIRA_PROJECT = os.getenv("JIRA_PROJECT", "INC")


def collect_all_signals(service: str) -> Dict[str, Any]:
    """
    collects prometheus metrics + loki log patterns in one call.
    called by detector_node, returns everything it needs at once.
    """
    print(f"[sre_tools] Collecting signals for {service}")

    #prometheus metrics
    signals = collect_incident_signals(service)

    if os.getenv("LOG_BACKEND", "loki") == "opensearch":
        from src.tools.log_tool import query_logs
        log_result = query_logs(service)

        signals["log_patterns"] = detect_patterns(log_result.get("records", []))
        signals["log_top_patterns"] = log_result.get("top_patterns", [])
        signals["error_log_summaries"] = log_result.get("sample_lines", [])
        signals["security_log_summaries"] = []
        signals["log_window"] = log_result.get("window")
        signals["logs_signal"] = log_result.get("signal")
        signals["metrics_ok"] = signals.get("p99_latency_s") is not None
        signals["logs_ok"] = bool(log_result.get("available"))
        print(f"[sre_tools] logs({log_result.get('backend')}): available={signals['logs_ok']} "
              f"lines={log_result.get('lines_scanned')}/{log_result.get('limit')} "
              f"patterns={len(signals['log_top_patterns'])}")
        return signals

    #loki logs
    logs = get_backend("logs")
    if logs is not None:
        error_logs = logs.log_capability("service_errors", service=service, limit=20)
        sec_logs = logs.log_capability("audit_events", lookback_minutes=10)
        slow_logs = logs.log_capability("slow_queries", limit=30, lookback_minutes=10)
    else:
        error_logs, sec_logs, slow_logs = [], [], []
    
    all_logs = error_logs + sec_logs + slow_logs

    signals["log_patterns"] = detect_patterns(all_logs)
    signals["error_log_summaries"] = extract_log_summary(error_logs, max_records=8)
    signals["security_log_summaries"] = extract_log_summary(sec_logs, max_records=5)
    signals["metrics_ok"] = signals.get("p99_latency_s") is not None
    signals["logs_ok"] = len(all_logs) > 0

    return signals


def lookup_runbook(query: str, runbook_id: Optional[str] = None, k: int = 3) -> Dict[str, Any]:
    """
    look up runbooks in faiss.
    returns chunks of text we can feed to the llm.
    """
    print(f"[sre_tools] FAISS lookup: '{query[:50]}' runbook_id={runbook_id}")

    try:
        from src.rag.retriever import retrieve, format_chunks_for_llm
        docs = retrieve(query=query, k=k, runbook_id=runbook_id)
        chunks = [
            {
                "source": doc.metadata.get("source", "unknown"),
                "section": doc.metadata.get("section", ""),
                "content": doc.page_content.strip()[:500],
            }
            for doc in docs
        ]
        return {
            "success": True,
            "chunks": chunks,
            "context": format_chunks_for_llm(docs),
        }
    except FileNotFoundError:
        return {
            "success": False,
            "chunks": [],
            "context": "FAISS index not found. Run: python -m rag.ingest_runbooks",
        }
    except Exception as e:
        return {
            "success": False,
            "chunks": [],
            "context": f"Runbook lookup failed: {e}",
        }


@observe(name="execute_remediation")
def execute_remediation(action: str, tool: str, params: Dict[str, Any],
                        environment: str = "") -> Dict[str, Any]:
    """
    run the fix.
    if dry_run is on, just print what we would do.
    otherwise, actually hit the endpoints.
    """
    ts = datetime.now(timezone.utc).isoformat()
    print(f"[sre_tools] {'[DRY-RUN] ' if DRY_RUN else ''}execute: {tool} - {action[:50]}")

    if DRY_RUN:
        return {
            "success": True,
            "result": f"[DRY-RUN] Execute {tool} with {params}",
            "dry_run": True,
            "executed_at": ts,
        }

    try:
       
        if tool == "set_feature_flag" and params.get("remote"):
            admin_url = os.getenv("AWS_ADMIN_URL", "http://aws-tunnel:8085")
            req = urllib.request.Request(
                f"{admin_url}/set-flag",
                data=json.dumps({"flag": params.get("flag"), "variant": params.get("variant")}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=45.0) as response:
                body = json.loads(response.read().decode())
            if body.get("success"):
                return {
                    "success": True,
                    "result": f"Remote flag {body.get('flag')} set {body.get('before')} -> {body.get('after')}. Infra reacting.",
                    "dry_run": False,
                    "executed_at": ts,
                }
            return {
                "success": False,
                "error": f"remote set-flag rejected: {json.dumps(body)[:200]}",
                "dry_run": False,
                "executed_at": ts,
            }

        if environment == "aws-oteldemo" and tool not in ("notify", "capture_diagnostics"):
            return {
                "success": False,
                "error": (f"no remote executor for tool '{tool}' on environment 'aws-oteldemo' "
                          f"(only set_feature_flag with flag+variant executes remotely)"),
                "dry_run": False,
                "executed_at": ts,
            }

        req = urllib.request.Request("http://simulator:8001/control/resolve", method="POST")
        with urllib.request.urlopen(req, timeout=5.0) as response:
            if response.status == 200:
                return {
                    "success": True,
                    "result": f"Executed {tool} with {params}. Infra reacting.",
                    "dry_run": False,
                    "executed_at": ts,
                }
            else:
                return {
                    "success": False,
                    "error": f"simulator failed to resolve (status {response.status})",
                    "dry_run": False,
                    "executed_at": ts,
                }
    except Exception as e:
        return {"success": False, "error": str(e), "dry_run": False, "executed_at": ts}


def notify_slack(
    message: str, channel: str = "incidents", severity: str = "info", incident_id: Optional[str] = None
) -> Dict[str, Any]:
    """sends a colour-coded Slack message via webhook."""
    colors = {"info": "#36a64f", "warning": "#ff9900", "critical": "#ff0000"}
    emojis = {"info": ":white_check_mark:", "warning": ":warning:", "critical": ":rotating_light:"}

    title = f"{emojis.get(severity, ':bell:')} SRE Agent - {severity.upper()}"
    if incident_id:
        title += f" [{incident_id}]"

    if DRY_RUN:
        print(f"\n[notify_slack] [DRY-RUN] #{channel} | {title}")
        print(f" {message[:200]}")
        return {"success": True, "dry_run": True}

    try:
        webhook_url = os.getenv("SLACK_WEBHOOK_URL", "http://localhost:8002/slack")
        payload = {"attachments": [{
            "color": colors.get(severity, "#36a64f"),
            "title": title,
            "text": message,
            "footer": f"#{channel} | {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        }]}
        req = urllib.request.Request(
            webhook_url,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=10.0):
            return {"success": True, "dry_run": False}
    except Exception as e:
        return {"success": False, "error": str(e)}


def check_alert_status(alert_name: str) -> str:
    """queries alertmanager api to check if a specific alert is still firing."""
    am_url = os.getenv("ALERTMANAGER_URL", "http://alertmanager:9093")
    try:
        print(f"[sre_tool] Querying Alertmanager for alert: {alert_name}")
        req = urllib.request.Request(f"{am_url}/api/v2/alerts?active=true")
        with urllib.request.urlopen(req, timeout=10.0) as resp:
            alerts = json.loads(resp.read().decode())

        for a in alerts:
            labels = a.get("labels", {})
            if labels.get("alertname") == alert_name:
                print(f"[sre_tool] Alert {alert_name} is still firing/pending")
                return "firing"
        
        #if the alert is not found in alertmanager at all, it has resolved (the
        # query succeeded and named alerts are absent).
        # 'unknown' is reserved for AM query failures, so the verify loop can
        # fall back to prometheus metrics instead of trusting a dead query.
        print(f"[sre_tool] Alert {alert_name} not found in alertmanager -> resolved")
        return "resolved"
    except Exception as e:
        print(f"[sre_tool] Alertmanager api call failed: {e}")
        return "unknown"


#create_jira
def create_jira(
    incident_id: str, summary: str, description: str, severity: str = "SEV3", 
    alert_name: Optional[str] = None, root_cause: Optional[str] = None, runbook_id: Optional[str] = None
) -> Dict[str, Any]:
    """make a jira ticket."""
    priority_map = {"SEV1": "Highest", "SEV2": "High", "SEV3": "Medium", "SEV4": "Low", "SEV5": "Lowest"}
    priority = priority_map.get(severity.upper(), "Medium")

    if DRY_RUN or not JIRA_URL:
        ticket_id = f"{JIRA_PROJECT}-{str(uuid.uuid4().int)[:4]}"
        print(f"\n[create_jira] [DRY-RUN] {ticket_id} | {summary[:60]} | {priority}")
        return {
            "success": True,
            "ticket_id": ticket_id,
            "ticket_url": f"https://jira.example.com/browse/{ticket_id}",
            "dry_run": True,
        }

    try:
        full_desc = (
            f"Incident: {incident_id} | Alert: {alert_name} | "
            f"Severity: {severity} | Root cause: {root_cause} | "
            f"Runbook: {runbook_id}\n\n{description}"
        )
        payload = {"fields": {
            "project": {"key": JIRA_PROJECT},
            "summary": summary,
            "description": {"type": "doc", "version": 1, "content": [
                {"type": "paragraph", "content": [{"type": "text", "text": full_desc}]}
            ]},
            "issuetype": {"name": "Incident"},
            "priority": {"name": priority},
            "labels": ["sre-agent", severity.lower()],
        }}
        req = urllib.request.Request(
            f"{JIRA_URL}/rest/api/3/issue",
            data=json.dumps(payload).encode(),
            headers={"Authorization": f"Bearer {JIRA_TOKEN}", "Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=15.0) as resp:
            ticket_id = json.loads(resp.read().decode()).get("key", "UNKNOWN")
            return {
                "success": True,
                "ticket_id": ticket_id,
                "ticket_url": f"{JIRA_URL}/browse/{ticket_id}",
                "dry_run": False,
            }
    except Exception as e:
        return {"success": False, "error": str(e)}