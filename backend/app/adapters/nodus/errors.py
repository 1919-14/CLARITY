"""Structured error codes and sanitization for the Nodus Compute adapter.

All errors crossing the SDK boundary are normalized into safe, structured
dictionaries.  No raw SDK exception messages, stack traces, or provider
diagnostic strings are forwarded to callers — only a stable error code and
a sanitized summary.

Sanitization rules
------------------
* Messages are replaced by a stable, code-specific template.
* SDK exception details, provider diagnostic strings, and credential fragments
  are never propagated.
* The original exception is preserved only in ``__cause__`` for local logging;
  it must never be serialized or returned to clients.
"""

from __future__ import annotations

from typing import Any


# ---------------------------------------------------------------------------
# Error codes — stable, documented identifiers for each failure category
# ---------------------------------------------------------------------------

class NodusErrorCode:
    """Enumeration of stable Nodus adapter error codes."""

    # Configuration / setup
    MISSING_CREDENTIALS = "NODUS_MISSING_CREDENTIALS"
    INVALID_MODE = "NODUS_INVALID_MODE"
    CONFIGURATION_ERROR = "NODUS_CONFIGURATION_ERROR"

    # Execution
    REMOTE_EXECUTION_FAILED = "NODUS_REMOTE_EXECUTION_FAILED"
    TIMEOUT = "NODUS_TIMEOUT"
    CANCELLED = "NODUS_CANCELLED"
    RATE_LIMITED = "NODUS_RATE_LIMITED"
    NOT_FOUND = "NODUS_NOT_FOUND"
    PERMISSION_DENIED = "NODUS_PERMISSION_DENIED"
    INVALID_PAYLOAD = "NODUS_INVALID_PAYLOAD"
    SDK_ERROR = "NODUS_SDK_ERROR"

    # Result validation
    INVALID_RESULT = "NODUS_INVALID_RESULT"
    UNEXPECTED_RESULT_TYPE = "NODUS_UNEXPECTED_RESULT_TYPE"

    # Smoke test
    SMOKE_TEST_VALIDATION_FAILED = "NODUS_SMOKE_TEST_VALIDATION_FAILED"

    # Retry exhausted
    RETRIES_EXHAUSTED = "NODUS_RETRIES_EXHAUSTED"


# Human-readable summaries keyed by error code.  These are the *only* strings
# forwarded to callers; they must never embed SDK or provider diagnostic text.
_ERROR_SUMMARIES: dict[str, str] = {
    NodusErrorCode.MISSING_CREDENTIALS: (
        "Nodus credentials are missing or incomplete; "
        "set NODUS_API_KEY in the environment."
    ),
    NodusErrorCode.INVALID_MODE: (
        "Execution mode is invalid or missing; "
        "set NODUS_EXECUTION_MODE to one of: nodus_remote, local, mock."
    ),
    NodusErrorCode.CONFIGURATION_ERROR: (
        "Nodus adapter configuration error; check environment variables."
    ),
    NodusErrorCode.REMOTE_EXECUTION_FAILED: (
        "Remote task execution failed on the Nodus platform."
    ),
    NodusErrorCode.TIMEOUT: (
        "Task execution exceeded the configured timeout."
    ),
    NodusErrorCode.CANCELLED: "Task was cancelled before completion.",
    NodusErrorCode.RATE_LIMITED: (
        "Nodus platform returned a rate-limit response; retry after backoff."
    ),
    NodusErrorCode.NOT_FOUND: (
        "The requested Nodus resource was not found."
    ),
    NodusErrorCode.PERMISSION_DENIED: (
        "The configured credentials do not have permission for this operation."
    ),
    NodusErrorCode.INVALID_PAYLOAD: (
        "Task payload failed validation before submission."
    ),
    NodusErrorCode.SDK_ERROR: (
        "An unexpected error was returned by the Nodus SDK."
    ),
    NodusErrorCode.INVALID_RESULT: (
        "The result returned by the Nodus platform failed validation."
    ),
    NodusErrorCode.UNEXPECTED_RESULT_TYPE: (
        "The result returned by the Nodus platform has an unexpected type."
    ),
    NodusErrorCode.SMOKE_TEST_VALIDATION_FAILED: (
        "Smoke-test result did not match the expected deterministic output."
    ),
    NodusErrorCode.RETRIES_EXHAUSTED: (
        "All retry attempts were exhausted; the task did not succeed."
    ),
}


# ---------------------------------------------------------------------------
# Adapter exception hierarchy
# ---------------------------------------------------------------------------

class NodusAdapterError(Exception):
    """Base class for all Nodus adapter errors.

    Attributes:
        code: Stable ``NodusErrorCode`` string for this failure.
        message: Sanitized, human-readable summary (never contains SDK text).
        context: Optional structured metadata (must be JSON-serializable,
            must not contain secrets or provider diagnostic text).
    """

    def __init__(
        self,
        code: str,
        message: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.message = message or _ERROR_SUMMARIES.get(code, "Nodus adapter error.")
        self.context: dict[str, Any] = context or {}
        super().__init__(self.message)

    def to_safe_dict(self) -> dict[str, Any]:
        """Return a sanitized, JSON-serializable error representation.

        Never includes raw SDK text, stack traces, or secret fragments.
        """
        return {
            "code": self.code,
            "message": self.message,
            "context": self.context,
        }


class NodusConfigurationError(NodusAdapterError):
    """Raised when credentials or mode configuration is invalid."""

    def __init__(self, code: str = NodusErrorCode.CONFIGURATION_ERROR, **kwargs: Any) -> None:
        super().__init__(code, **kwargs)


class NodusExecutionError(NodusAdapterError):
    """Raised when remote task execution fails."""

    def __init__(self, code: str = NodusErrorCode.REMOTE_EXECUTION_FAILED, **kwargs: Any) -> None:
        super().__init__(code, **kwargs)


class NodusTimeoutError(NodusAdapterError):
    """Raised when task execution times out."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(NodusErrorCode.TIMEOUT, **kwargs)


class NodusResultError(NodusAdapterError):
    """Raised when the result from the platform fails validation."""

    def __init__(self, code: str = NodusErrorCode.INVALID_RESULT, **kwargs: Any) -> None:
        super().__init__(code, **kwargs)


class NodusSmokeTestError(NodusAdapterError):
    """Raised when the smoke-test result does not match expectations."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(NodusErrorCode.SMOKE_TEST_VALIDATION_FAILED, **kwargs)


# ---------------------------------------------------------------------------
# Sanitization helpers
# ---------------------------------------------------------------------------

# Fragments that indicate a string may contain secret or provider diagnostics
_SENSITIVE_PATTERNS = (
    "api_key",
    "apikey",
    "api key",
    "token",
    "secret",
    "password",
    "credential",
    "bearer",
    "authorization",
)


def _looks_sensitive(text: str) -> bool:
    """Heuristically detect strings that may contain secret fragments."""
    lower = text.lower()
    return any(pat in lower for pat in _SENSITIVE_PATTERNS)


def sanitize_sdk_error(exc: Exception) -> dict[str, Any]:
    """Return a safe error dict from a raw SDK exception.

    Rules:
    * The error code is derived from the SDK exception class name only.
    * The message is replaced by the stable summary for ``SDK_ERROR``.
    * The original ``str(exc)`` is never forwarded.

    Returns a ``to_safe_dict``-compatible dictionary.
    """
    exc_type = type(exc).__name__
    code = _classify_sdk_exception(exc_type)
    return {
        "code": code,
        "message": _ERROR_SUMMARIES.get(code, _ERROR_SUMMARIES[NodusErrorCode.SDK_ERROR]),
        "context": {"sdk_exception_type": exc_type},
    }


def _classify_sdk_exception(exc_class_name: str) -> str:
    """Map SDK exception class name to a ``NodusErrorCode``."""
    name = exc_class_name.lower()
    if "timeout" in name:
        return NodusErrorCode.TIMEOUT
    if "ratelimit" in name or "rate_limit" in name or "toomanyrequests" in name:
        return NodusErrorCode.RATE_LIMITED
    if "notfound" in name or "not_found" in name:
        return NodusErrorCode.NOT_FOUND
    if "permission" in name or "forbidden" in name or "unauthorized" in name:
        return NodusErrorCode.PERMISSION_DENIED
    if "cancelled" in name or "canceled" in name:
        return NodusErrorCode.CANCELLED
    return NodusErrorCode.SDK_ERROR


def sanitize_result_error(observation_error: Any) -> dict[str, Any]:
    """Return a safe error dict from a non-success observation error.

    The raw observation error value is type-checked; only a stable code and
    classification are forwarded.  If the value is a string it is checked for
    sensitive fragments and replaced if detected.
    """
    if isinstance(observation_error, str):
        if _looks_sensitive(observation_error):
            # Do not forward; replace with stable message
            return {
                "code": NodusErrorCode.INVALID_RESULT,
                "message": _ERROR_SUMMARIES[NodusErrorCode.INVALID_RESULT],
                "context": {"sanitized": True},
            }
        # Truncate to a reasonable length and exclude newlines
        safe_text = observation_error.replace("\n", " ").replace("\r", "")[:200]
        return {
            "code": NodusErrorCode.INVALID_RESULT,
            "message": _ERROR_SUMMARIES[NodusErrorCode.INVALID_RESULT],
            "context": {"provider_summary": safe_text},
        }
    # Non-string error value: record only its type
    return {
        "code": NodusErrorCode.INVALID_RESULT,
        "message": _ERROR_SUMMARIES[NodusErrorCode.INVALID_RESULT],
        "context": {"error_type": type(observation_error).__name__},
    }
