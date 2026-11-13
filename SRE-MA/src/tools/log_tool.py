"""
tools/log_tool.py
-----------------
reads the AWS otel-demo logs from OpenSearch over the ssh tunnel
(indices otel-logs-*). query_logs only reports what the index actually
holds; it never invents entries.
"""

import os
import re
import json
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

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
    """collapse numbers/uuids/ips so 'x47 timeouts on 10.0.0.3' groups together"""
    p = re.sub(r"\b\d{1,3}(\.\d{1,3}){3}\b", "<ip>", body)
    p = re.sub(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", "<uuid>", p)
    p = re.sub(r"\d+", "<n>", p)
    return p[:90]


def query_logs(
    service: str,
    start: Optional[str] = None,
    end: Optional[str] = None,
    text: Optional[str] = None,
    level: Optional[str] = None,
    limit: int = ROW_LIMIT,
) -> Dict[str, Any]:
    """query the otel-demo logs for one service. returns the aggregated contract
    consumed by sre_tool.collect_all_signals and agent_tool.query_service_logs."""
    limit = min(limit, ROW_LIMIT)
    svc = (service or "").strip()
    if not svc:
        return {"backend": "opensearch", "available": False, "reason": "service is required",
                "records": [], "top_patterns": [], "sample_lines": [],
                "window": {"mode": "none"}, "lines_scanned": 0, "limit": limit}

    # window: explicit start/end win; else a 15-minute rolling window
    now = datetime.now(timezone.utc)
    if start:
        t0 = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
        t1 = datetime.fromisoformat(str(end).replace("Z", "+00:00")) if end else now
        mode = "explicit"
    else:
        t0, t1 = now - timedelta(minutes=15), now
        mode = "rolling_15m"
    ts_field = "observedTimestamp"

    must = [
        {"match_phrase": {"resource.service.name": svc}},
        {"range": {ts_field: {"gte": t0.isoformat(), "lte": t1.isoformat()}}},
    ]
    if text:
        must.append({"match_phrase": {"body": text}})
    if level:
        must.append({"match_phrase": {"severityText": level}})

    query = {"size": limit, "sort": [{ts_field: {"order": "desc"}}],
             "query": {"bool": {"must": must}}}
    data = _os_search(query)
    if data is None:
        return {"backend": "opensearch", "available": False,
                "reason": f"opensearch unreachable at {OPENSEARCH_URL}",
                "records": [], "top_patterns": [], "sample_lines": [],
                "window": {"mode": mode, "from": t0.isoformat(), "to": t1.isoformat()},
                "lines_scanned": 0, "limit": limit}

    hits = data.get("hits", {}).get("hits", [])
    records = []
    for h in hits:
        src = h.get("_source", {})
        records.append({
            "timestamp": src.get(ts_field) or src.get("@timestamp"),
            "level": src.get("severityText") or "",
            "service": (src.get("resource", {}) or {}).get("service.name", svc),
            "message": src.get("body", ""),
        })

    # top patterns: normalized message groups (most frequent first)
    groups: Dict[str, Dict[str, Any]] = {}
    for r in records:
        lvl = (r.get("level") or "?").upper()[:8]
        pat = _pattern_for(str(r.get("message", "")))
        key = f"{lvl}|{pat}"
        g = groups.setdefault(key, {"count": 0, "level": lvl, "pattern": pat})
        g["count"] += 1
    top_patterns = sorted(groups.values(), key=lambda g: -g["count"])[:8]

    sample_lines = [
        f"{(r.get('timestamp') or '')[11:19]} [{(r.get('level') or '?')[:5]}] {str(r.get('message', ''))[:140]}"
        for r in records[:8]
    ]
    available = len(records) > 0
    return {
        "backend": "opensearch",
        "available": available,
        "reason": None if available else "no log lines matched in the window",
        "records": records,
        "top_patterns": top_patterns,
        "sample_lines": sample_lines,
        "window": {"mode": mode, "from": t0.isoformat(), "to": t1.isoformat()},
        "lines_scanned": len(records),
        "limit": limit,
        "queried_at": datetime.now(timezone.utc).isoformat(),
    }
