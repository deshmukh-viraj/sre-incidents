"""
tools/loki_tool.py
------------------
log capability facade. LogQL lives in the backend adapter
(src/tools/backends/loki.py); this module keeps the historical function
signatures used across the repo.
"""

import os
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

load_dotenv()

from src.tools.backends.registry import get_backend  # noqa: E402

LOKI_URL = os.getenv("LOKI_URL", "http://localhost:3100")  # deprecated; pack url wins


def query_loki(logql: str, limit: int = 50, lookback_minutes: int = 10) -> List[Dict]:
    """
    Execute a log query against the active logs backend.
    Returns list of parsed log record dicts.
    """
    backend = get_backend("logs")
    if backend is None:
        return []
    return backend.query(logql, limit=limit, lookback_minutes=lookback_minutes)


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

        if "error" in log and isinstance(log["error"], dict):
            err = log["error"]
            if "message" in err:
                parts.append(f"error={err['message'][:100]}")

        if "security_event" in log and isinstance(log["security_event"], dict):
            evt = log["security_event"]
            parts.append(f"security_event={evt.get('event_type', '')} ip={evt.get('source_ip', '')}")

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