"""
tools/backends/jaeger.py
------------------------
jaeger adapter (TracesBackend contract). The otel-demo instance runs Jaeger,
not Tempo; the trace query syntax lives here only, same rule as tempo.py.

Jaeger query API: GET /api/traces?service=<svc>&lookback=<dur>&limit=<n>
returns data[].traceID with spans (duration in MICROseconds) and a processes
map (processID -> {serviceName, ...}). Normalized to the same shape the
tempo adapter returns so callers stay unchanged.
"""

import time
import urllib.parse
from typing import Any, Dict, List, Optional

from src.tools.backends.base import BackendConfig, TracesBackend, bounded_get, clip


class JaegerBackend(TracesBackend):

    def search(
        self,
        service: str,
        lookback_minutes: int = 10,
        limit: int = 20,
        min_duration_ms: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        now_us = int(time.time() * 1e6)
        params = {
            "service": service,
            "lookback": f"{max(1, lookback_minutes * 60)}s",
            "limit": str(min(limit, self.cfg.max_rows)),
        }
        if min_duration_ms is not None:
            # jaeger filters on duration in microseconds
            params["minDuration"] = f"{int(min_duration_ms) * 1000}us"
        qs = urllib.parse.urlencode(params)
        data = bounded_get(f"{self.cfg.url}/api/traces?{qs}", self.cfg)

        out = []
        for trace in data.get("data", []) or []:
            processes = trace.get("processes", {})
            spans = trace.get("spans", []) or []
            if not spans:
                continue
            # root span = the one no other span references as a parent
            child_ids = set()
            for s in spans:
                for ref in s.get("references", []) or []:
                    child_ids.add(ref.get("spanID"))
            root = next(
                (s for s in spans if s.get("spanID") not in child_ids),
                spans[0],
            )
            root_service = (processes.get(root.get("processID")) or {}).get("serviceName", "")
            out.append({
                "trace_id": trace.get("traceID"),
                "root_service": root_service,
                "root_trace_name": root.get("operationName", ""),
                # jaeger durations are microseconds; normalize to ms like tempo
                "duration_ms": round(int(root.get("duration", 0)) / 1000.0, 1),
                "start_unix_nano": int(root.get("startTime", 0)) * 1000,
            })
        return clip(out, self.cfg)
