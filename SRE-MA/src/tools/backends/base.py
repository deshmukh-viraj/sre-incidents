"""
tools/backends/base.py
----------------------
uniform contracts for observability backends + the bounds every backend call runs under.

spec contract:
- graph nodes and prompt templates never author backend query dialects; they call
  capability methods here and adapters translate to the native dialect (PromQL,
  LogQL, TraceQL, ...).
- adapters return normalized structures (scalar values, series, log rows, spans).
- supporting a new corporate environment = add an adapter module + a pack in
  config/environments.yaml. nodes, prompts and other adapters stay untouched.
- every adapter is read-only by construction: only GET requests leave this layer.
"""

import json
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple


class BackendError(Exception):
    """a backend call failed after the dispatcher applied its bounds."""


@dataclass
class BackendConfig:
    """one backend's slice of the environment pack (config/environments.yaml)."""
    kind: str                 # metrics | logs | traces
    adapter: str              # prometheus | loki | tempo | ...
    url: str
    timeout_s: float = 10.0
    max_rows: int = 100       # hard ceiling on rows/series any query may return
    enabled: bool = True


def bounded_get(url: str, cfg: BackendConfig) -> dict:
    """single GET with the pack's timeout; the only egress path adapters may use."""
    req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=cfg.timeout_s) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        raise BackendError(f"{cfg.kind} query failed ({cfg.adapter}): {e}") from e


def clip(rows: Sequence[Any], cfg: BackendConfig) -> List[Any]:
    """enforce the pack's row ceiling on any result set leaving a backend."""
    return list(rows[: cfg.max_rows])


class MetricsBackend(ABC):
    """scalar/series metrics backend (prometheus, datadog-metrics, ...)."""

    kind = "metrics"

    def __init__(self, cfg: BackendConfig):
        self.cfg = cfg

    @property
    def url(self) -> str:
        return self.cfg.url

    @abstractmethod
    def instant(self, expr: str) -> Optional[float]:
        """evaluate a native instant query, return the scalar value or None."""

    @abstractmethod
    def range(
        self, expr: str, start_s: float, end_s: float, step_s: int
    ) -> List[Tuple[float, float]]:
        """evaluate a native range query, return [(unix_ts, value), ...]."""

    @abstractmethod
    def metric_names(self, prefixes: Optional[Tuple[str, ...]] = None) -> Optional[List[str]]:
        """live metric family names (metadata API); None when the backend is unreachable."""

    @abstractmethod
    def template(self, capability: str, **params) -> str:
        """capability request -> native query. the ONLY place dialect is authored."""

    def capability(self, capability: str, **params) -> Optional[float]:
        """translate + evaluate one named capability; nodes call this, never template()."""
        return self.instant(self.template(capability, **params))


class LogsBackend(ABC):
    """log row backend (loki, elastic, ...)."""

    kind = "logs"

    def __init__(self, cfg: BackendConfig):
        self.cfg = cfg

    @property
    def url(self) -> str:
        return self.cfg.url

    @abstractmethod
    def query(self, selector: str, limit: int = 50, lookback_minutes: int = 10) -> List[Dict[str, Any]]:
        """run a selector-based log query, return normalized row dicts (clipped to max_rows)."""

    @abstractmethod
    def log_capability(self, capability: str, **params) -> List[Dict[str, Any]]:
        """named, pre-built log query owned by the adapter (service_errors, audit_events, ...)."""

    def build_selector(self, service: str, **pairs) -> str:
        """translate {service: x, level: error} -> native selector; dialect stays here."""
        return self._selector(service=service, **pairs)

    @abstractmethod
    def _selector(self, **pairs) -> str:
        """native selector syntax lives here only."""


class TracesBackend(ABC):
    """distributed trace backend (tempo, jaeger, ...)."""

    kind = "traces"

    def __init__(self, cfg: BackendConfig):
        self.cfg = cfg

    @property
    def url(self) -> str:
        return self.cfg.url

    @abstractmethod
    def search(
        self,
        service: str,
        lookback_minutes: int = 10,
        limit: int = 20,
        min_duration_ms: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """search spans by service + window; return normalized span summaries."""
