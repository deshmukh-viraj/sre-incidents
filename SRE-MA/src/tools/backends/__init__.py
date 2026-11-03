"""
tools/backends — adapter interfaces for observability backends.
see registry.get_backend(kind) for the active environment's instances.
"""

from src.tools.backends.base import (
    BackendConfig,
    BackendError,
    LogsBackend,
    MetricsBackend,
    TracesBackend,
)

__all__ = [
    "BackendConfig",
    "BackendError",
    "LogsBackend",
    "MetricsBackend",
    "TracesBackend",
]
