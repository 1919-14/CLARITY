"""Nodus Compute execution adapter package.

Public API
----------
NodusExecutionAdapter   Core adapter class.
NodusConfig             Immutable configuration (load via ``from_environment``).
NodusTask               Task specification contract.
NodusResult             Normalized result contract.
ExecutionMode           Supported execution modes.
NodusAdapterError       Base adapter exception.
NodusConfigurationError Configuration or credential error.
NodusExecutionError     Remote execution failure.
NodusTimeoutError       Execution timeout.
NodusResultError        Result validation failure.
NodusSmokeTestError     Smoke-test expectation mismatch.
build_adapter_from_environment
                        Convenience factory.

SDK imports remain lazy; this package is importable without ``nodus-compute``
installed or credentials present.
"""

from .adapter import NodusExecutionAdapter, build_adapter_from_environment
from .config import NodusConfig
from .contracts import ExecutionMode, NodusResult, NodusTask
from .errors import (
    NodusAdapterError,
    NodusConfigurationError,
    NodusErrorCode,
    NodusExecutionError,
    NodusResultError,
    NodusSmokeTestError,
    NodusTimeoutError,
)

__all__ = [
    "NodusExecutionAdapter",
    "NodusConfig",
    "NodusTask",
    "NodusResult",
    "ExecutionMode",
    "NodusAdapterError",
    "NodusConfigurationError",
    "NodusErrorCode",
    "NodusExecutionError",
    "NodusResultError",
    "NodusSmokeTestError",
    "NodusTimeoutError",
    "build_adapter_from_environment",
]
