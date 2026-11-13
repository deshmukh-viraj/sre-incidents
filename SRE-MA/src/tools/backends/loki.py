"""
tools/backends/loki.py
----------------------
loki adapter. logql selectors and the pre-built log queries live here only.
"""

import json
import time
import urllib.parse
from typing import Any, Dict, List, Optional

from src.tools.backends.base import BackendConfig, LogsBackend, bounded_get, clip

# named log queries owned by this adapter (moved verbatim from sre_tool.collect_all_signals)
LOG_CAPABILITIES = {
    "service_errors": {
        "selector": '{{service="{service}"}} | json | level="ERROR"',
        "limit": 20,
    },
    "audit_events": {
        "selector": '{{service="api_gateway"}} | json | audit_required="true"',
        "limit": 20,
    },
    "slow_queries": {
        "selector": '{{service="db_proxy"}} | json | alert="SLOW_QUERY"',
        "limit": 30,
    },
}


class LokiBackend(LogsBackend):

    def _selector(self, **pairs) -> str:
        service = pairs.get("service", "")
        extra = "".join(f', {k}="{v}"' for k, v in pairs.items() if k != "service" and v is not None)
        return f'{{service="{service}"{extra}}}'

    def query(self, selector: str, limit: int = 50, lookback_minutes: int = 10,
              start_ns: Optional[int] = None, end_ns: Optional[int] = None) -> List[Dict[str, Any]]:
        end_ns = end_ns if end_ns is not None else int(time.time() * 1e9)
        start_ns = start_ns if start_ns is not None else int((time.time() - (lookback_minutes * 60)) * 1e9)
        params = urllib.parse.urlencode({
            "query": selector,
            "start": start_ns,
            "end": end_ns,
            "limit": min(limit, self.cfg.max_rows),
            "direction": "backward",
        })
        try:
            data = bounded_get(f"{self.cfg.url}/loki/api/v1/query_range?{params}", self.cfg)
        except Exception:
            return []
        result = data.get("data", {}).get("result", [])

        records = []
        for stream in result:
            for _ts, line in stream.get("values", []):
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    records.append({"raw": line})
        return clip(records, self.cfg)

    def log_capability(self, capability: str, **params) -> List[Dict[str, Any]]:
        try:
            cap = LOG_CAPABILITIES[capability]
        except KeyError:
            raise KeyError(f"unknown log capability '{capability}'. known: {sorted(LOG_CAPABILITIES)}")
        selector = cap["selector"].format(**{"service": params.get("service", "")})
        return self.query(
            selector,
            limit=params.get("limit", cap["limit"]),
            lookback_minutes=params.get("lookback_minutes", 10),
        )
