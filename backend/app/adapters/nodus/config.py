"""Configuration for the Nodus Compute execution adapter.

Reads exclusively from environment variables.  Never reads from application
config objects or imports Flask/app modules — keeping this module importable
without a running application.

Environment variables
---------------------
NODUS_API_KEY          : Required for ``nodus_remote`` mode.
NODUS_APP_NAME         : Required. The registered Nodus App name.
NODUS_FUNCTION_NAME    : Required. The function within the App to invoke.
NODUS_EXECUTION_MODE   : Required. One of ``nodus_remote``, ``local``, ``mock``.
                         No default — must be set explicitly.
NODUS_TIMEOUT_SECONDS  : Optional. Per-task timeout (default 30).
NODUS_MAX_RETRIES      : Optional. Max retries for transient errors (default 2).
NODUS_BASE_URL         : Optional. Override Nodus platform base URL.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from .contracts import ExecutionMode
from .errors import NodusConfigurationError, NodusErrorCode


@dataclass(frozen=True)
class NodusConfig:
    """Validated, immutable Nodus adapter configuration.

    Instantiate via :meth:`from_environment`.  Direct construction is only
    appropriate for tests that supply values explicitly.

    Attributes:
        execution_mode: Explicit execution mode (required, no default).
        app_name: Registered Nodus App name.
        function_name: Registered function within the App.
        api_key: Nodus API key (required for remote mode only).
        timeout_seconds: Per-task execution timeout.
        max_retries: Maximum retries on transient errors.
        base_url: Optional platform base URL override.
    """

    execution_mode: ExecutionMode
    app_name: str
    function_name: str
    api_key: str | None
    timeout_seconds: float
    max_retries: int
    base_url: str | None = None
    project: str | None = None
    _extra: dict = field(default_factory=dict, compare=False, repr=False)

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0:
            raise NodusConfigurationError(
                NodusErrorCode.CONFIGURATION_ERROR,
                message="NODUS_TIMEOUT_SECONDS must be a positive number.",
            )
        if self.max_retries < 0:
            raise NodusConfigurationError(
                NodusErrorCode.CONFIGURATION_ERROR,
                message="NODUS_MAX_RETRIES cannot be negative.",
            )
        if self.execution_mode == ExecutionMode.REMOTE and not self.api_key:
            raise NodusConfigurationError(
                NodusErrorCode.MISSING_CREDENTIALS,
                message=(
                    "NODUS_API_KEY must be set when NODUS_EXECUTION_MODE=nodus_remote."
                ),
            )
        if not self.app_name:
            raise NodusConfigurationError(
                NodusErrorCode.CONFIGURATION_ERROR,
                message="NODUS_APP_NAME must be set.",
            )
        if not self.function_name:
            raise NodusConfigurationError(
                NodusErrorCode.CONFIGURATION_ERROR,
                message="NODUS_FUNCTION_NAME must be set.",
            )

    @classmethod
    def from_environment(cls) -> NodusConfig:
        """Load and validate Nodus configuration from environment variables.

        Raises:
            NodusConfigurationError: If ``NODUS_EXECUTION_MODE`` is absent or
                invalid, or if credentials are missing for remote mode.
        """
        raw_mode = os.environ.get("NODUS_EXECUTION_MODE", "").strip()
        if not raw_mode:
            raise NodusConfigurationError(
                NodusErrorCode.INVALID_MODE,
                message=(
                    "NODUS_EXECUTION_MODE is not set. "
                    "Set it to one of: nodus_remote, local, mock."
                ),
            )
        try:
            execution_mode = ExecutionMode.from_str(raw_mode)
        except ValueError as exc:
            raise NodusConfigurationError(
                NodusErrorCode.INVALID_MODE,
                message=str(exc),
            ) from exc

        app_name = os.environ.get("NODUS_APP_NAME", "").strip()
        function_name = os.environ.get("NODUS_FUNCTION_NAME", "").strip()
        api_key = os.environ.get("NODUS_API_KEY", "").strip() or None
        base_url = os.environ.get("NODUS_BASE_URL", "").strip() or None
        project = os.environ.get("NODUS_PROJECT", "").strip() or None

        raw_timeout = os.environ.get("NODUS_TIMEOUT_SECONDS", "30").strip()
        try:
            timeout_seconds = float(raw_timeout)
        except ValueError:
            raise NodusConfigurationError(
                NodusErrorCode.CONFIGURATION_ERROR,
                message=f"NODUS_TIMEOUT_SECONDS must be a number, got {raw_timeout!r}.",
            )

        raw_retries = os.environ.get("NODUS_MAX_RETRIES", "2").strip()
        try:
            max_retries = int(raw_retries)
        except ValueError:
            raise NodusConfigurationError(
                NodusErrorCode.CONFIGURATION_ERROR,
                message=f"NODUS_MAX_RETRIES must be an integer, got {raw_retries!r}.",
            )

        return cls(
            execution_mode=execution_mode,
            app_name=app_name,
            function_name=function_name,
            api_key=api_key,
            timeout_seconds=timeout_seconds,
            max_retries=max_retries,
            base_url=base_url,
            project=project,
        )

    def __repr__(self) -> str:
        """Exclude api_key from repr to prevent accidental logging."""
        return (
            f"NodusConfig("
            f"execution_mode={self.execution_mode.value!r}, "
            f"app_name={self.app_name!r}, "
            f"function_name={self.function_name!r}, "
            f"project={self.project!r}, "
            f"api_key=<redacted>, "
            f"timeout_seconds={self.timeout_seconds}, "
            f"max_retries={self.max_retries}"
            f")"
        )
