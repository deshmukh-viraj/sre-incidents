"""
tools/sre_tool.py
-----------------
Remediation, telemetry aggregation, and alerting tools for SRE workflows.
"""

import json
import os
import urllib.request
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from dotenv import load_dotenv

from src.tools.backends.registry import get_backend
from src.tools.loki_tool import detect_patterns, extract_log_summary
from src.tools.prometheus_tool import collect_incident_signals

load_dotenv()

try:
    from langfuse import observe
except ImportError:
    def observe(*args, **kwargs):
        return lambda f: f

DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"
JIRA_URL = os.getenv("JIRA_URL")
JIRA_TOKEN = os.getenv("JIRA_TOKEN")
JIRA_PROJECT = os.getenv("JIRA_PROJECT", "INC")
LOOKBACK_MIN = int(os.getenv("LOOKBACK_MIN", "15"))


def _post_json(url: str, data: dict, timeout: float = 30.0, headers: Optional[dict] = None) -> dict:
    req_headers = {"Content-Type": "application/json"}
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(
        url,
        data=json.dumps(data).encode("utf-8"),
        headers=req_headers,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        content = resp.read().decode("utf-8")
        return json.loads(content) if content else {}


def collect_all_signals(service: str) -> Dict[str, Any]:
    """Collects metrics and log patterns for a service across configured backends."""
    signals = collect_incident_signals(service)
    signals["metrics_ok"] = signals.get("p99_latency_s") is not None

    def _empty_logs(err: Optional[str] = None) -> Dict[str, Any]:
        return {
            "log_patterns": [],
            "log_top_patterns": [],
            "error_log_summaries": [],
            "security_log_summaries": [],
            "logs_ok": False,
            "logs_error": err,
        }

    if not service or not service.strip():
        signals.update(_empty_logs("service is required"))
        return signals

    logs = get_backend("logs")
    if logs is None:
        signals.update(_empty_logs("logs backend is disabled"))
        return signals

    try:
        errs = logs.log_capability("service_errors", service=service, limit=20, lookback_minutes=LOOKBACK_MIN)
        sec = logs.log_capability("audit_events", service=service, lookback_minutes=LOOKBACK_MIN)
        slow = logs.log_capability("slow_queries", service=service, limit=30, lookback_minutes=LOOKBACK_MIN)
        all_logs = errs + sec + slow

        signals.update({
            "log_patterns": detect_patterns(all_logs),
            "log_top_patterns": detect_patterns(all_logs),
            "error_log_summaries": extract_log_summary(errs, max_records=8),
            "security_log_summaries": extract_log_summary(sec, max_records=5),
            "logs_ok": bool(all_logs),
            "logs_error": None if all_logs else "no matching log records",
        })
    except Exception as exc:
        signals.update(_empty_logs(str(exc)))

    return signals


def lookup_runbook(query: str, runbook_id: Optional[str] = None, k: int = 3) -> Dict[str, Any]:
    """Retrieves relevant runbook sections from FAISS vector store."""
    try:
        from src.rag.retriever import format_chunks_for_llm, retrieve
        docs = retrieve(query=query, k=k, runbook_id=runbook_id)
        chunks = [
            {"source": d.metadata.get("source", "unknown"),
             "section": d.metadata.get("section", ""),
             "content": d.page_content.strip()[:500]}
            for d in docs
        ]
        return {"success": True, "chunks": chunks, "context": format_chunks_for_llm(docs)}
    except FileNotFoundError:
        return {"success": False, "chunks": [], "context": "FAISS index not found. Run: python -m rag.ingest_runbooks"}
    except Exception as e:
        return {"success": False, "chunks": [], "context": f"Runbook lookup failed: {e}"}


@observe(name="execute_remediation")
def execute_remediation(action: str, tool: str, params: Dict[str, Any], environment: str = "") -> Dict[str, Any]:
    """Executes a remediation action against live environments or simulation."""
    ts = datetime.now(timezone.utc).isoformat()
    if DRY_RUN:
        return {"success": True, "result": f"[DRY-RUN] Execute {tool} with {params}", "dry_run": True, "executed_at": ts}

    admin_url = os.getenv("AWS_ADMIN_URL", "http://aws-tunnel:8085")
    try:
        if tool == "set_feature_flag" and params.get("remote"):
            body = _post_json(f"{admin_url}/set-flag", {"flag": params.get("flag"), "variant": params.get("variant")}, timeout=45.0)
            if body.get("success"):
                return {"success": True, "result": f"Remote flag {body.get('flag')} set {body.get('before')} -> {body.get('after')}. Infra reacting.", "dry_run": False, "executed_at": ts}
            return {"success": False, "error": f"remote set-flag rejected: {json.dumps(body)[:200]}", "dry_run": False, "executed_at": ts}

        if environment == "aws-oteldemo":
            if tool in ("restart_service", "rolling_restart", "kubectl_restart_deployment"):
                service = params.get("service") or params.get("deployment") or ""
                body = _post_json(f"{admin_url}/restart-service", {"service": service}, timeout=150.0)
                if body.get("success"):
                    return {"success": True, "result": f"Remote restart of {service} done. Infra reacting.", "dry_run": False, "executed_at": ts}
                return {"success": False, "error": f"remote restart rejected: {json.dumps(body)[:200]}", "dry_run": False, "executed_at": ts}
            if tool not in ("notify", "capture_diagnostics"):
                return {
                    "success": False,
                    "error": f"no remote executor for tool '{tool}' on environment 'aws-oteldemo' (only set_feature_flag and service restarts execute remotely)",
                    "dry_run": False,
                    "executed_at": ts,
                }

        req = urllib.request.Request("http://simulator:8001/control/resolve", method="POST")
        with urllib.request.urlopen(req, timeout=5.0) as response:
            if response.status == 200:
                return {"success": True, "result": f"Executed {tool} with {params}. Infra reacting.", "dry_run": False, "executed_at": ts}
            return {"success": False, "error": f"simulator failed to resolve (status {response.status})", "dry_run": False, "executed_at": ts}
    except Exception as e:
        return {"success": False, "error": str(e), "dry_run": False, "executed_at": ts}


def notify_slack(message: str, channel: str = "incidents", severity: str = "info", incident_id: Optional[str] = None) -> Dict[str, Any]:
    """Sends incident notifications to Slack."""
    colors = {"info": "#36a64f", "warning": "#ff9900", "critical": "#ff0000"}
    emojis = {"info": ":white_check_mark:", "warning": ":warning:", "critical": ":rotating_light:"}
    title = f"{emojis.get(severity, ':bell:')} SRE Agent - {severity.upper()}" + (f" [{incident_id}]" if incident_id else "")

    if DRY_RUN:
        return {"success": True, "dry_run": True}

    try:
        webhook_url = os.getenv("SLACK_WEBHOOK_URL", "http://localhost:8002/slack")
        payload = {"attachments": [{
            "color": colors.get(severity, "#36a64f"),
            "title": title,
            "text": message,
            "footer": f"#{channel} | {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        }]}
        _post_json(webhook_url, payload, timeout=10.0)
        return {"success": True, "dry_run": False}
    except Exception as e:
        return {"success": False, "error": str(e)}


def check_alert_status(alert_name: str) -> str:
    """Checks whether an alert is firing or resolved in Alertmanager."""
    am_url = os.getenv("ALERTMANAGER_URL", "http://alertmanager:9093")
    try:
        req = urllib.request.Request(f"{am_url}/api/v2/alerts?active=true")
        with urllib.request.urlopen(req, timeout=10.0) as resp:
            alerts = json.loads(resp.read().decode())
        for a in alerts:
            if a.get("labels", {}).get("alertname") == alert_name:
                return "firing"
        return "resolved"
    except Exception as e:
        print(f"[sre_tool] Alertmanager API error: {e}")
        return "unknown"


def create_jira(
    incident_id: str, summary: str, description: str, severity: str = "SEV3",
    alert_name: Optional[str] = None, root_cause: Optional[str] = None, runbook_id: Optional[str] = None
) -> Dict[str, Any]:
    """Creates an incident ticket in Jira or mocks in dry-run mode."""
    priorities = {"SEV1": "Highest", "SEV2": "High", "SEV3": "Medium", "SEV4": "Low", "SEV5": "Lowest"}
    priority = priorities.get(severity.upper(), "Medium")

    if DRY_RUN or not JIRA_URL:
        ticket_id = f"{JIRA_PROJECT}-{str(uuid.uuid4().int)[:4]}"
        return {"success": True, "ticket_id": ticket_id, "ticket_url": f"https://jira.example.com/browse/{ticket_id}", "dry_run": True}

    try:
        full_desc = f"Incident: {incident_id} | Alert: {alert_name} | Severity: {severity} | Root cause: {root_cause} | Runbook: {runbook_id}\n\n{description}"
        payload = {
            "fields": {
                "project": {"key": JIRA_PROJECT},
                "summary": summary,
                "description": {"type": "doc", "version": 1, "content": [{"type": "paragraph", "content": [{"type": "text", "text": full_desc}]}]},
                "issuetype": {"name": "Incident"},
                "priority": {"name": priority},
                "labels": ["sre-agent", severity.lower()],
            }
        }
        resp = _post_json(f"{JIRA_URL}/rest/api/3/issue", payload, timeout=15.0, headers={"Authorization": f"Bearer {JIRA_TOKEN}"})
        ticket_id = resp.get("key", "UNKNOWN")
        return {"success": True, "ticket_id": ticket_id, "ticket_url": f"{JIRA_URL}/browse/{ticket_id}", "dry_run": False}
    except Exception as e:
        return {"success": False, "error": str(e)}
