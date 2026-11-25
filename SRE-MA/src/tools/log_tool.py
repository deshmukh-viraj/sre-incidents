"""
tools/log_tool.py
-----------------
Reads AWS otel-demo logs from OpenSearch over the tunnel.
"""

import os
import re
import json
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

OPENSEARCH_URL = os.getenv("LOG_OPENSEARCH_URL", "http://localhost:9200")
INDEX_PATTERN = os.getenv("LOG_OPENSEARCH_INDEX", "otel-logs-*")
ROW_LIMIT = int(os.getenv("LOG_ROW_LIMIT", "200"))
_TIMEOUT_S = float(os.getenv("LOG_TIMEOUT_S", "10"))


def _os_search(query: dict) -> Optional[dict]:
    try:
        req = urllib.request.Request(
            f"{OPENSEARCH_URL}/{INDEX_PATTERN}/_search",
            data=json.dumps(query).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        print(f"[log_tool] opensearch query failed: {e}")
        return None


def _pattern_for(body: str) -> str:
    """ponytail: regex normalization collapses IPs, UUIDs, and numbers for quick clustering."""
    p = re.sub(r"\b\d{1,3}(\.\d{1,3}){3}\b", "<ip>", body)
    p = re.sub(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", "<uuid>", p)
    return re.sub(r"\d+", "<n>", p)[:90]


def query_logs(
    service: str,
    start: Optional[str] = None,
    end: Optional[str] = None,
    text: Optional[str] = None,
    level: Optional[str] = None,
    limit: int = ROW_LIMIT,
) -> Dict[str, Any]:
    """Queries otel-demo logs for a service and groups common patterns."""
    limit = min(limit, ROW_LIMIT)
    svc = (service or "").strip()
    if not svc:
        return {"backend": "opensearch", "available": False, "reason": "service is required",
                "records": [], "top_patterns": [], "sample_lines": [],
                "window": {"mode": "none"}, "lines_scanned": 0, "limit": limit}

    now = datetime.now(timezone.utc)
    if start:
        t0 = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
        t1 = datetime.fromisoformat(str(end).replace("Z", "+00:00")) if end else now
        mode = "explicit"
    else:
        t0, t1 = now - timedelta(minutes=15), now
        mode = "rolling_15m"

    must = [
        {"match_phrase": {"resource.service.name": svc}},
        {"range": {"observedTimestamp": {"gte": t0.isoformat(), "lte": t1.isoformat()}}},
    ]
    if text:
        must.append({"match_phrase": {"body": text}})
    if level:
        must.append({"match_phrase": {"severityText": level}})

    data = _os_search({"size": limit, "sort": [{"observedTimestamp": {"order": "desc"}}],
                       "query": {"bool": {"must": must}}})
    if data is None:
        return {"backend": "opensearch", "available": False,
                "reason": f"opensearch unreachable at {OPENSEARCH_URL}",
                "records": [], "top_patterns": [], "sample_lines": [],
                "window": {"mode": mode, "from": t0.isoformat(), "to": t1.isoformat()},
                "lines_scanned": 0, "limit": limit}

    records = [
        {
            "timestamp": src.get("observedTimestamp") or src.get("@timestamp"),
            "level": src.get("severityText") or "",
            "service": (src.get("resource") or {}).get("service.name", svc),
            "message": src.get("body", ""),
        }
        for src in (h.get("_source", {}) for h in data.get("hits", {}).get("hits", []))
    ]

    # Aggregate patterns
    groups: Dict[str, Dict[str, Any]] = {}
    for r in records:
        lvl = (r.get("level") or "?").upper()[:8]
        pat = _pattern_for(str(r.get("message", "")))
        g = groups.setdefault(f"{lvl}|{pat}", {"count": 0, "level": lvl, "pattern": pat})
        g["count"] += 1
    top_patterns = sorted(groups.values(), key=lambda g: -g["count"])[:8]

    sample_lines = [
        f"{(r.get('timestamp') or '')[11:19]} [{(r.get('level') or '?')[:5]}] {str(r.get('message', ''))[:140]}"
        for r in records[:8]
    ]

    return {
        "backend": "opensearch",
        "available": bool(records),
        "reason": None if records else "no log lines matched in the window",
        "records": records,
        "top_patterns": top_patterns,
        "sample_lines": sample_lines,
        "window": {"mode": mode, "from": t0.isoformat(), "to": t1.isoformat()},
        "lines_scanned": len(records),
        "limit": limit,
        "queried_at": datetime.now(timezone.utc).isoformat(),
    }
