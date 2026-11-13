"""
tools/backends: adapter interfaces for the observability backends.
registry.get_backend(kind) returns the instances for the active environment.
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
