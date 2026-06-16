"""
tools/loki_tool.py
------------------
ask loki for logs.
keep all the logql stuff here so agents don't have to deal with it.
"""

import os
import httpx
import json
import time
from typing import List, Dict, Any, Optional
from dotenv import load_dotenv

load_dotenv()

LOKI_URL = os.getenv("LOKI_URL", "http://localhost:3100")


#low-level Loki query

def _query_loki(logql: str, limit: int = 50, lookback_minutes: int = 10) -> List[Dict]:
    """
    Execute a LogQL query against Loki.
    Returns list of parsed log record dicts.
    """
    end_ns   = int(time.time() * 1e9)
    start_ns = int((time.time() - (lookback_minutes * 60)) * 1e9)

    try:
        resp = httpx.get(
            f"{LOKI_URL}/loki/api/v1/query_range",
            params={
                "query": logql,
                "start": start_ns,
                "end":   end_ns,
                "limit": limit,
                "direction": "backward",
            },
            timeout=15.0,
        )
        resp.raise_for_status()
        data   = resp.json()
        result = data.get("data", {}).get("result", [])

        records = []
        for stream in result:
            for _ts, line in stream.get("values", []):
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    records.append({"raw": line})
        return records

    except Exception as e:
        print(f"[loki_tool] query failed: {e}")
        return []


#named log queries

def get_error_logs(service: str, limit: int = 20, lookback_minutes: int = 10) -> List[Dict]:
    """
    grab recent error logs for a service so we can see what blew up.
    """
    return _query_loki(
        f'{{service="{service}"}} | json | level="ERROR"',
        limit=limit,
        lookback_minutes=lookback_minutes,
    )


def get_slow_query_logs(lookback_minutes: int = 10) -> List[Dict]:
    """
    Fetch slow query log records.
    Presence of these is evidence for RB-004 (DB connection exhaustion).
    """
    return _query_loki(
        '{service="db_proxy"} | json | alert="SLOW_QUERY"',
        limit=30,
        lookback_minutes=lookback_minutes,
    )


def get_security_logs(lookback_minutes: int = 10) -> List[Dict]:
    """
    Fetch security event logs (auth failures, bulk exports, PII access).
    Used by RB-003 (data exfiltration) diagnosis.
    """
    return _query_loki(
        '{service="api_gateway"} | json | audit_required="true"',
        limit=50,
        lookback_minutes=lookback_minutes,
    )


def get_circuit_breaker_logs(service: str, lookback_minutes: int = 10) -> List[Dict]:
    """
    Fetch logs mentioning circuit breaker state changes.
    Used by RB-002 diagnosis.
    """
    return _query_loki(
        f'{{service="{service}"}} | json | message=~"circuit breaker.*"',
        limit=20,
        lookback_minutes=lookback_minutes,
    )


def get_logs_by_trace_id(trace_id: str) -> List[Dict]:
    """
    Fetch all log records for a specific trace_id.
    Used for trace-to-log correlation in the diagnoser.
    """
    return _query_loki(
        f'{{service=~".+"}} | json | trace_id="{trace_id}"',
        limit=100,
        lookback_minutes=60,
    )


def get_compliance_logs(regulation: str = "PCI-DSS", lookback_minutes: int = 60) -> List[Dict]:
    """
    Fetch compliance event logs for a given regulation.
    Used by RB-005 (compliance audit).
    """
    return _query_loki(
        f'{{service="compliance_engine"}} | json | line_format "{{{{.compliance}}}}"',
        limit=100,
        lookback_minutes=lookback_minutes,
    )


#log analysis helpers

def extract_log_summary(logs: List[Dict], max_records: int = 10) -> List[str]:
    """
    turn ugly logs into short strings so we don't blow up the llm token limit.
    """
    summaries = []
    for log in logs[:max_records]:
        parts = []

        if "timestamp" in log:
            parts.append(log["timestamp"][:19])  # trim to seconds
        if "level" in log:
            parts.append(f"[{log['level']}]")
        if "service" in log:
            parts.append(f"service={log['service']}")
        if "message" in log:
            parts.append(log["message"][:120])

        # include error detail if present
        if "error" in log and isinstance(log["error"], dict):
            err = log["error"]
            if "message" in err:
                parts.append(f"error={err['message'][:100]}")

        # include security event type if present
        if "security_event" in log and isinstance(log["security_event"], dict):
            evt = log["security_event"]
            parts.append(f"security_event={evt.get('event_type', '')} ip={evt.get('source_ip', '')}")

        # include DB info if present
        if "db" in log and isinstance(log["db"], dict):
            db = log["db"]
            parts.append(f"db={db.get('name', '')} op={db.get('operation', '')} duration={db.get('duration_ms', '')}ms")

        summaries.append(" | ".join(parts))

    return summaries


def detect_patterns(logs: List[Dict]) -> Dict[str, Any]:
    """
    look for specific keywords in logs to guess what went wrong.
    """
    patterns = {
        "hikaripoolerror": False,
        "circuit_breaker_open": False,
        "card_rails_timeout": False,
        "bulk_data_export": False,
        "auth_failures": 0,
        "slow_queries": 0,
        "http_500_count": 0,
        "http_429_count": 0,
    }

    for log in logs:
        msg = (log.get("message") or "").lower()

        if "hikaripool" in msg or "connection pool" in msg:
            patterns["hikaripoolerror"] = True
        if "circuit breaker" in msg and "open" in msg:
            patterns["circuit_breaker_open"] = True
        if "card_rails" in msg and ("timeout" in msg or "did not respond" in msg):
            patterns["card_rails_timeout"] = True

        if "security_event" in log and isinstance(log["security_event"], dict):
            evt_type = log["security_event"].get("event_type", "")
            if evt_type == "bulk_data_export":
                patterns["bulk_data_export"] = True
            if evt_type == "authentication_failure":
                patterns["auth_failures"] += 1

        if log.get("alert") == "SLOW_QUERY":
            patterns["slow_queries"] += 1

        status = log.get("http_status") or log.get("status_code")
        if status:
            if str(status).startswith("5"):
                patterns["http_500_count"] += 1
            if str(status) == "429":
                patterns["http_429_count"] += 1

    return patterns