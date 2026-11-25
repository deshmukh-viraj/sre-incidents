"""
OpenSearch logs backend adapter.
Read-only queries against structured OpenTelemetry logs in OpenSearch.
"""

import json
import time
from typing import Any, Dict, List, Optional

from src.tools.backends.base import (
    BackendConfig,
    LogsBackend,
    bounded_post_json,
    clip,
)

class OpenSearchBackend(LogsBackend):
    """Read-only OpenSearch adapter for structured OTel logs."""

    DEFAULT_INDEX = "otel-logs-*"
    TIMESTAMP_FIELD = "@timestamp"
    SERVICE_FIELD = "resource.service.name.keyword"
    LEVEL_FIELD = "severity.text"
    MESSAGE_FIELD = "body"

    def __init__(self, cfg: BackendConfig):
        super().__init__(cfg)
        self.index_pattern = getattr(cfg, "index_pattern", None) or self.DEFAULT_INDEX

    def _search(self, query: dict) -> dict:
        url = f"{self.cfg.url.rstrip('/')}/{self.index_pattern}/_search"
        return bounded_post_json(url, query, self.cfg)

    def _selector(self, **pairs) -> str:
        return json.dumps({k: v for k, v in pairs.items() if v not in (None, "")})

    def query(
        self,
        selector: str,
        limit: int = 50,
        lookback_minutes: int = 10,
        start_ns: Optional[int] = None,
        end_ns: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        try:
            filters = json.loads(selector) if selector else {}
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("OpenSearch selector must be valid JSON") from exc

        if not isinstance(filters, dict):
            raise ValueError("OpenSearch selector must be a JSON object")

        service = filters.get("service")
        if not service:
            raise ValueError("OpenSearch queries require a service filter")

        now_ns = time.time_ns()
        end = end_ns if end_ns is not None else now_ns
        start = start_ns if start_ns is not None else end - int(lookback_minutes * 60 * 1e9)

        must = [
            {"term": {self.SERVICE_FIELD: service}},
            {"range": {self.TIMESTAMP_FIELD: {
                "gte": int(start // 1_000_000_000),
                "lte": int(end // 1_000_000_000),
                "format": "epoch_second",
            }}},
        ]
        if filters.get("text"):
            must.append({"match_phrase": {self.MESSAGE_FIELD: filters["text"]}})
        if filters.get("level"):
            must.append({"match_phrase": {self.LEVEL_FIELD: filters["level"]}})

        payload = {
            "size": min(max(1, int(limit)), self.cfg.max_rows),
            "sort": [{self.TIMESTAMP_FIELD: {"order": "desc"}}],
            "query": {"bool": {"must": must}},
        }

        data = self._search(payload)
        records = []
        for hit in data.get("hits", {}).get("hits", []):
            src = hit.get("_source") or {}
            sev = src.get("severity") or {}
            records.append({
                "timestamp": src.get(self.TIMESTAMP_FIELD) or src.get("@timestamp"),
                "level": sev.get("text", "") if isinstance(sev, dict) else (src.get(self.LEVEL_FIELD) or ""),
                "service": (src.get("resource") or {}).get("service.name", service),
                "message": src.get(self.MESSAGE_FIELD, ""),
            })

        return clip(records, self.cfg)

    def log_capability(self, capability: str, **params) -> List[Dict[str, Any]]:
        service = params.get("service")
        if not service:
            raise ValueError(f"service is required for capability '{capability}'")

        cap_map = {
            "service_errors": {"service": service, "level": "ERROR"},
            "audit_events": {"service": service, "text": params.get("text", "audit")},
            "slow_queries": {"service": service, "text": params.get("text", "slow")},
        }
        if capability not in cap_map:
            raise KeyError(f"Unknown OpenSearch log capability '{capability}'")

        return self.query(
            self._selector(**cap_map[capability]),
            limit=params.get("limit", 50),
            lookback_minutes=params.get("lookback_minutes", 10),
        )
