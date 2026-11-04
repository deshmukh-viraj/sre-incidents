"""
tools/backends/registry.py
--------------------------
environment pack loader. SRE_ENVIRONMENT selects the pack in
config/environments.yaml; each pack names its metrics/logs/traces backend,
endpoint and bounds. swapping a backend or environment is a config change —
no node, prompt or adapter edit.
"""

import os
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

from src.tools.backends.base import BackendConfig, LogsBackend, MetricsBackend, TracesBackend

_ADAPTERS = {}  # populated lazily so importing this module never imports heavy deps

PACK_FILE = Path(__file__).resolve().parents[3] / "config" / "environments.yaml"


def _load_pack(pack_name: str) -> Dict:
    with open(PACK_FILE) as fh:
        packs = yaml.safe_load(fh) or {}
    if pack_name not in packs:
        raise ValueError(f"unknown environment pack '{pack_name}' in {PACK_FILE}: {sorted(packs)}")
    return packs[pack_name]


def active_environment() -> str:
    return os.getenv("SRE_ENVIRONMENT", "fintech_sim")


def _adapter_class(kind: str, adapter_name: str):
    if not _ADAPTERS:
        from src.tools.backends.prometheus import PrometheusBackend
        from src.tools.backends.loki import LokiBackend
        from src.tools.backends.tempo import TempoBackend
        _ADAPTERS.update({
            ("metrics", "prometheus"): PrometheusBackend,
            ("logs", "loki"): LokiBackend,
            ("traces", "tempo"): TempoBackend,
        })
    cls = _ADAPTERS.get((kind, adapter_name))
    if cls is None:
        raise ValueError(f"no {kind} adapter named '{adapter_name}'")
    return cls


_BACKEND_CACHE: dict = {}


def get_backend(kind: str) -> Optional[Any]:
    """configured backend instance (MetricsBackend | LogsBackend | TracesBackend)
    for the active environment, or None if disabled. concrete type depends on kind."""
    if kind in _BACKEND_CACHE:
        return _BACKEND_CACHE[kind]
    pack = _load_pack(active_environment())
    section = pack.get(kind) or {}
    if not section.get("enabled", True):
        _BACKEND_CACHE[kind] = None
        return None
    cfg = BackendConfig(
        kind=kind,
        adapter=section.get("adapter", kind),
        url=section["url"],
        timeout_s=float(section.get("timeout_s", 10.0)),
        max_rows=int(section.get("max_rows", 100)),
        enabled=True,
    )
    backend = _adapter_class(kind, cfg.adapter)(cfg)
    _BACKEND_CACHE[kind] = backend
    return backend


def reset_cache():
    """drop cached instances (used by tests and after config changes)."""
    _BACKEND_CACHE.clear()
