import os
import uuid
import httpx
from datetime import datetime, timezone
from typing import Optional, Dict, Any

from dotenv import load_dotenv

from src.tools.prometheus_tool import collect_incident_signals
from src.tools.loki_tool import (
    get_error_logs,
    get_security_logs,
    get_slow_query_logs,
    detect_patterns,
    extract_log_summary,
)

load_dotenv()

DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"


#collect_all_signals

def collect_all_signals(service: str) -> Dict[str, Any]:
    """
    collects prometheus metrics + loki log patterns in one call.
    called by detector_node, returns everything it needs at once.
    """
    print(f"[sre_tools] Collecting signals for {service}")

    #prometheus metrics
    signals = collect_incident_signals(service)

    #loki logs
    error_logs = get_error_logs(service, limit=20)
    sec_logs = get_security_logs(lookback_minutes=10)
    slow_logs = get_slow_query_logs(lookback_minutes=10)
    all_logs = error_logs + sec_logs + slow_logs

    signals["log_patterns"] = detect_patterns(all_logs)
    signals["error_log_summaries"] = extract_log_summary(error_logs, max_records=8)
    signals["security_log_summaries"] = extract_log_summary(sec_logs,   max_records=5)
    signals["metrics_ok"] = signals.get("p99_latency_s") is not None
    signals["logs_ok"] = len(all_logs) > 0

    return signals


#lookup_runbook 
def lookup_runbook(
    query: str,
    runbook_id: Optional[str] = None,
    k: int = 3,
) -> Dict[str, Any]:
    """
    look up runbooks in faiss.
    returns chunks of text we can feed to the llm.
    """
    print(f"[sre_tools] FAISS lookup: '{query[:50]}' runbook_id={runbook_id}")

    try:
        from rag.retriever import retrieve, format_chunks_for_llm

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


#execute_remediation 
def execute_remediation(
    action: str,
    tool: str,
    params: Dict[str, Any],
) -> Dict[str, Any]:
    """
    run the fix.
    if dry_run is on, just print what we would do.
    otherwise, actually hit the endpoints.
    """
    ts = datetime.now(timezone.utc).isoformat()
    print(f"[sre_tools] {'[DRY-RUN] ' if DRY_RUN else ''}execute: {tool} — {action[:50]}")

    if DRY_RUN:
        return {
            "success": True,
            "result": f"[DRY-RUN] {_describe_action(tool, params)}",
            "dry_run": True,
            "executed_at": ts,
            
        }

    try:
        result = _run_action(tool, params)
        response = httpx.post("http://localhost:8001/control/resolve", timeout=5.0)
        if response.status_code == 200:
            return {
                "success": True,
                "result": f"Executed {tool}. Infra reacting. ({result})",
                "dry_run": False,
                "executed_at": ts,
            }
        else:
            return {
                "success": False,
                "error": f"simulator failed to resolve (status {response.status_code})",
                "dry_run": False,
                "executed_at": ts,
            }

    except Exception as e:
        return {"success": False, "error": str(e), "dry_run": False, "executed_at": ts}
        


def _describe_action(tool: str, params: Dict) -> str:
    """human-readable description of what the action would do."""
    descriptions = {
        "set_feature_flag": lambda p: f"Feature flag '{p.get('flag')}' → {p.get('value')} on '{p.get('service', 'all')}'",
        "restart_service": lambda p: f"Restart service '{p.get('service')}'",
        "rolling_restart": lambda p: f"Rolling restart of '{p.get('service')}'",
        "rollback_deployment": lambda p: f"Rollback '{p.get('service')}' to {p.get('revision', 'previous')}",
        "block_ip": lambda p: f"Block IP {p.get('ip')} at '{p.get('service')}'",
        "revoke_api_key": lambda p: f"Revoke API key '{p.get('key_id')}'",
        "export_logs": lambda p: f"Export logs for '{p.get('service')}' ({p.get('duration', '24h')})",
        "capture_diagnostics": lambda p: f"Capture thread dump for '{p.get('service')}'",
        "execute_kg_suggestion":lambda p: f"Execute KG action: {p.get('command', '')[:80]}",
        "notify": lambda p: f"Notify channel '{p.get('channel')}' severity={p.get('severity')}",
    }
    fn = descriptions.get(tool)
    return fn(params) if fn else f"Execute {tool}({params})"


def _run_action(tool: str, params: Dict) -> str:
    """
    the actual logic to run commands in production.
    add real kubectl stuff here later.
    """
    #feature flags
    if tool == "set_feature_flag":
        return f"Flag '{params.get('flag')}' set to {params.get('value')}"

    # Service restarts
    if tool in ("restart_service", "rolling_restart"):
        # TODO: subprocess.run(["kubectl", "rollout", "restart", ...])
        return f"Rolling restart initiated for '{params.get('service')}'"

    # Rollback
    if tool == "rollback_deployment":
        # TODO: kubectl rollout undo deployment/...
        return f"Rollback initiated for '{params.get('service')}'"

    # Security
    if tool == "block_ip":
        return f"IP {params.get('ip')} blocked at {params.get('service')}"

    if tool == "revoke_api_key":
        return f"API key {params.get('key_id')} revoked"

    # Diagnostics
    if tool == "capture_diagnostics":
        return f"Diagnostics captured for '{params.get('service')}'"

    if tool == "export_logs":
        return f"Logs exported for '{params.get('service')}'"

    if tool == "execute_kg_suggestion":
        return f"KG suggestion queued for human review: {params.get('command', '')[:100]}"

    if tool == "notify":
        return f"Notification sent to {params.get('channel')}"

    raise NotImplementedError(
        f"Tool '{tool}' has no production implementation. "
        f"Add it to _run_action() in sre_tools.py"
    )


#notify_slack

def notify_slack(
    message: str,
    channel: str = "incidents",
    severity: str = "info",
    incident_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    sends a colour-coded Slack message via webhook.
    DRY_RUN or missing webhook -> prints instead.
    """
    colors = {"info": "#36a64f", "warning": "#ff9900", "critical": "#ff0000"}
    emojis = {"info": ":white_check_mark:", "warning": ":warning:", "critical": ":rotating_light:"}

    title = f"{emojis.get(severity, ':bell:')} SRE Agent — {severity.upper()}"
    if incident_id:
        title += f" [{incident_id}]"

    if DRY_RUN :
        print(f"\n[notify_slack] [DRY-RUN] #{channel} | {title}")
        print(f" {message[:200]}")
        return {"success": True, "dry_run": True}

    try:
        resp = httpx.post(
            json={"attachments": [{
                "color": colors.get(severity, "#36a64f"),
                "title": title,
                "text":  message,
                "footer": f"#{channel} | {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
            }]},
            timeout=10.0,
        )
        resp.raise_for_status()
        return {"success": True, "dry_run": False}
    except Exception as e:
        return {"success": False, "error": str(e)}


#create_jira

def create_jira(
    incident_id: str,
    summary: str,
    description: str,
    severity: str = "SEV3",
    alert_name: Optional[str] = None,
    root_cause: Optional[str] = None,
    runbook_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    make a jira ticket.
    if dry run, just pretend.
    """
    priority_map = {"SEV1": "Highest", "SEV2": "High", "SEV3": "Medium",
                    "SEV4": "Low", "SEV5": "Lowest"}
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
        resp = httpx.post(
            f"{JIRA_URL}/rest/api/3/issue",
            headers={"Authorization": f"Bearer {JIRA_TOKEN}",
                     "Content-Type": "application/json"},
            json={"fields": {
                "project": {"key": JIRA_PROJECT},
                "summary": summary,
                "description": {"type": "doc", "version": 1, "content": [
                    {"type": "paragraph", "content": [{"type": "text", "text": full_desc}]}
                ]},
                "issuetype": {"name": "Incident"},
                "priority": {"name": priority},
                "labels": ["sre-agent", severity.lower()],
            }},
            timeout=15.0,
        )
        resp.raise_for_status()
        ticket_id = resp.json().get("key", "UNKNOWN")
        return {
            "success": True,
            "ticket_id": ticket_id,
            "ticket_url": f"{JIRA_URL}/browse/{ticket_id}",
            "dry_run": False,
        }
    except Exception as e:
        return {"success": False, "error": str(e)}