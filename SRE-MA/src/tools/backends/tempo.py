"""
tools/backends/tempo.py
-----------------------
tempo adapter (TraceQL search). the trace query syntax lives here only.
"""

import time
import urllib.parse
from typing import Any, Dict, List, Optional

from src.tools.backends.base import BackendConfig, TracesBackend, bounded_get, clip


class TempoBackend(TracesBackend):

    def search(
        self,
        service: str,
        lookback_minutes: int = 10,
        limit: int = 20,
        min_duration_ms: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        # TraceQL search query, authored here and nowhere else
        q = f'{{service="{service}"'
        if min_duration_ms is not None:
            q += f' && duration>={min_duration_ms}ms'
        q += "}"
        end_ns = int(time.time() * 1e9)
        start_ns = int((time.time() - (lookback_minutes * 60)) * 1e9)
        params = urllib.parse.urlencode({
            "q": q,
            "start": start_ns,
            "end": end_ns,
            "limit": min(limit, self.cfg.max_rows),
        })
        data = bounded_get(f"{self.cfg.url}/api/search?{params}", self.cfg)
        traces = data.get("traces", [])
        return clip([
            {
                "trace_id": t.get("traceID"),
                "root_service": t.get("rootServiceName"),
                "root_trace_name": t.get("rootTraceName"),
                "duration_ms": t.get("durationMs"),
                "start_unix_nano": t.get("startTimeUnixNano"),
            }
            for t in traces
        ], self.cfg)
