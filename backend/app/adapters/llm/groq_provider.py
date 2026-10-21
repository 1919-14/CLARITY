"""Groq runtime adapter for the LLM Scientist (Issue #41).

The adapter hides the provider SDK behind :class:`LLMProposalProvider`. It reads
the API credential from an environment variable at construction time, never
stores or returns it, and converts every failure into the structured
:class:`ProviderCallResult` contract.

Security properties
-------------------
- No module-level SDK import: importing this module never requires ``groq``.
- The credential is used once to build the client and is not retained.
- Exceptions are reported by type and (when present) HTTP status only; provider
  messages, request headers, prompts, and credentials are never copied into
  errors, records, or serialized results.
- Retries are bounded and apply only to documented transient failures. Auth and
  malformed/oversized responses are never retried.
"""

from __future__ import annotations

import os
import time
from collections.abc import Mapping
from typing import Any, Callable, Dict, Optional

from ...domain.contracts import StructuredError
from ...domain.llm_scientist_contracts import (
    LLMProposalProvider,
    LLMProposalRequest,
    LLMScientistConfig,
    ProviderCallResult,
    ProviderOutcomeStatus,
)

PROVIDER_ID = "groq"

# Bounded exponential backoff for retryable failures only.
_BACKOFF_BASE_SECONDS = 0.5
_BACKOFF_CAP_SECONDS = 4.0

_TRANSIENT_EXCEPTION_NAMES = frozenset(
    {
        "APITimeoutError",
        "APIConnectionError",
        "RateLimitError",
        "InternalServerError",
        "ServiceUnavailableError",
    }
)
_AUTH_EXCEPTION_NAMES = frozenset({"AuthenticationError"})
_PERMISSION_EXCEPTION_NAMES = frozenset({"PermissionDeniedError"})


class GroqProposalProvider(LLMProposalProvider):
    """Bounded Groq chat-completions provider returning structured outcomes."""

    provider_id = PROVIDER_ID
    is_live = True

    def __init__(
        self,
        config: LLMScientistConfig,
        *,
        api_key: Optional[str] = None,
        client: Any = None,
        sleep: Callable[[float], None] = time.sleep,
        environ: Optional[Mapping[str, str]] = None,
    ) -> None:
        """Create the provider.

        Parameters
        ----------
        config
            Scientist configuration (model, timeout, retries, token bound).
        api_key
            Optional explicit credential for tests. Production callers should
            omit it and set the environment variable named by
            ``config.credential_env_var`` instead.
        client
            Optional pre-built SDK-compatible client (used by tests). When
            supplied, no SDK import or credential lookup happens.
        sleep
            Injectable sleep function so retry tests do not wait.
        environ
            Injectable environment mapping for tests.
        """
        if not isinstance(config, LLMScientistConfig):
            raise ValueError("config must be an LLMScientistConfig.")
        self._config = config
        self._sleep = sleep
        self._client = client
        self._missing_credentials = False
        self._sdk_unavailable = False

        if self._client is None:
            source = os.environ if environ is None else environ
            credential = api_key if api_key is not None else source.get(config.credential_env_var)
            credential = credential.strip() if isinstance(credential, str) else ""
            if not credential:
                # Record only that the credential was absent, never its source value.
                self._missing_credentials = True
            else:
                self._client = self._build_client(credential)
                # Drop the local reference; the SDK client owns the credential.
                credential = ""

    @property
    def model_id(self) -> str:
        return self._config.model

    def _build_client(self, credential: str) -> Any:
        try:
            from groq import Groq  # type: ignore import-not-found
        except Exception:  # noqa: BLE001 - optional dependency boundary
            self._sdk_unavailable = True
            return None
        try:
            return Groq(
                api_key=credential,
                timeout=float(self._config.timeout_seconds),
                max_retries=0,  # retries are owned and bounded here
            )
        except Exception:  # noqa: BLE001 - client construction failure
            self._sdk_unavailable = True
            return None

    # ------------------------------------------------------------------
    # Provider interface
    # ------------------------------------------------------------------

    def propose(self, request: LLMProposalRequest) -> ProviderCallResult:
        """Call Groq with a bounded retry policy and return a structured result."""
        if self._missing_credentials:
            return ProviderCallResult(
                status=ProviderOutcomeStatus.MISSING_CREDENTIALS,
                error=StructuredError(
                    code="LLM_MISSING_CREDENTIALS",
                    message=(
                        "LLM-assisted mode is enabled but no credential is configured. "
                        f"Set the {self._config.credential_env_var} environment variable."
                    ),
                    details={"credential_env_var": self._config.credential_env_var},
                ),
                attempts=0,
                is_live=True,
            )
        if self._client is None:
            return ProviderCallResult(
                status=ProviderOutcomeStatus.SDK_UNAVAILABLE,
                error=StructuredError(
                    code="LLM_PROVIDER_SDK_UNAVAILABLE",
                    message=(
                        "The configured provider client is unavailable. Install the "
                        "provider package or verify the provider configuration."
                    ),
                    details={"provider": self.provider_id},
                ),
                attempts=0,
                is_live=True,
            )

        messages = [
            {"role": "system", "content": request_system_prompt()},
            {"role": "user", "content": request_user_prompt(request)},
        ]
        max_attempts = 1 + self._config.max_retries
        start = time.monotonic()
        last_status = ProviderOutcomeStatus.PROVIDER_ERROR
        last_error = StructuredError(
            code="LLM_PROVIDER_ERROR",
            message="Provider call failed without a structured explanation.",
        )

        for attempt in range(1, max_attempts + 1):
            try:
                completion = self._client.chat.completions.create(
                    messages=messages,
                    model=self._config.model,
                    temperature=float(self._config.temperature),
                    max_tokens=self._config.max_tokens,
                    response_format={"type": "json_object"},
                    timeout=float(self._config.timeout_seconds),
                )
            except Exception as exc:  # noqa: BLE001 - untrusted provider boundary
                status, retryable = _classify_exception(exc)
                last_status = status
                last_error = _error_for(status, exc)
                if retryable and attempt < max_attempts:
                    self._sleep(_backoff(attempt))
                    continue
                return ProviderCallResult(
                    status=status,
                    error=last_error,
                    attempts=attempt,
                    model=self._config.model,
                    duration_ms=_elapsed_ms(start),
                    is_live=True,
                )

            content, usage, model = _extract_completion(completion)
            if not isinstance(content, str) or not content.strip():
                return ProviderCallResult(
                    status=ProviderOutcomeStatus.MALFORMED_RESPONSE,
                    error=StructuredError(
                        code="LLM_MALFORMED_RESPONSE",
                        message="Provider returned no text content.",
                    ),
                    attempts=attempt,
                    model=model,
                    usage=usage,
                    duration_ms=_elapsed_ms(start),
                    is_live=True,
                )
            if len(content) > self._config.max_response_characters:
                return ProviderCallResult(
                    status=ProviderOutcomeStatus.RESPONSE_TOO_LARGE,
                    error=StructuredError(
                        code="LLM_RESPONSE_TOO_LARGE",
                        message=(
                            "Provider response exceeded the configured maximum of "
                            f"{self._config.max_response_characters} characters."
                        ),
                        details={"response_characters": len(content)},
                    ),
                    attempts=attempt,
                    model=model,
                    usage=usage,
                    duration_ms=_elapsed_ms(start),
                    is_live=True,
                )
            return ProviderCallResult(
                status=ProviderOutcomeStatus.SUCCESS,
                content=content,
                attempts=attempt,
                model=model,
                usage=usage,
                duration_ms=_elapsed_ms(start),
                is_live=True,
            )

        # Unreachable: the loop always returns. Kept for defensive clarity.
        return ProviderCallResult(
            status=last_status,
            error=last_error,
            attempts=max_attempts,
            model=self._config.model,
            duration_ms=_elapsed_ms(start),
            is_live=True,
        )


# Small indirections keep the prompt module importable without a circular
# dependency between the domain prompts and the adapter.
def request_system_prompt() -> str:
    from ...domain.llm_scientist import render_system_prompt

    return render_system_prompt()


def request_user_prompt(request: LLMProposalRequest) -> str:
    from ...domain.llm_scientist import render_user_prompt

    return render_user_prompt(request)


def _elapsed_ms(start: float) -> float:
    return max(0.0, (time.monotonic() - start) * 1000.0)


def _backoff(attempt: int) -> float:
    return min(_BACKOFF_CAP_SECONDS, _BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))


def _classify_exception(exc: BaseException) -> tuple[ProviderOutcomeStatus, bool]:
    """Map a provider exception to a status plus whether a bounded retry applies.

    Classification uses the SDK exception type name so the adapter never needs
    the SDK at import time and so tests can supply equivalent fakes. The SDK's
    own retry loop is disabled; this function owns the bound.
    """
    name = type(exc).__name__
    status_code = getattr(exc, "status_code", None)
    if name in _AUTH_EXCEPTION_NAMES or "Authentication" in name:
        return ProviderOutcomeStatus.AUTHENTICATION_FAILED, False
    if name in _PERMISSION_EXCEPTION_NAMES or "PermissionDenied" in name:
        return ProviderOutcomeStatus.PERMISSION_DENIED, False
    if name == "RateLimitError" or "RateLimit" in name:
        return ProviderOutcomeStatus.RATE_LIMITED, True
    if name == "APITimeoutError" or "Timeout" in name:
        return ProviderOutcomeStatus.TIMEOUT, True
    if name in _TRANSIENT_EXCEPTION_NAMES or "Connection" in name:
        return ProviderOutcomeStatus.TRANSIENT_FAILURE, True
    if name == "APIStatusError" or "InternalServer" in name:
        if isinstance(status_code, int) and status_code >= 500:
            return ProviderOutcomeStatus.PROVIDER_ERROR, True
        return ProviderOutcomeStatus.PROVIDER_ERROR, False
    return ProviderOutcomeStatus.PROVIDER_ERROR, False


def _error_for(status: ProviderOutcomeStatus, exc: BaseException) -> StructuredError:
    messages: Dict[ProviderOutcomeStatus, str] = {
        ProviderOutcomeStatus.AUTHENTICATION_FAILED: (
            "Provider rejected the configured credential (authentication failed)."
        ),
        ProviderOutcomeStatus.PERMISSION_DENIED: (
            "Provider credential is valid but not permitted to use this model."
        ),
        ProviderOutcomeStatus.RATE_LIMITED: (
            "Provider rate limit was reached; no further retry was permitted."
        ),
        ProviderOutcomeStatus.TIMEOUT: "Provider request timed out.",
        ProviderOutcomeStatus.TRANSIENT_FAILURE: (
            "Provider connection failed transiently; retries were exhausted."
        ),
        ProviderOutcomeStatus.PROVIDER_ERROR: "Provider returned a server-side error.",
    }
    details: Dict[str, Any] = {"exception_type": type(exc).__name__}
    status_code = getattr(exc, "status_code", None)
    if isinstance(status_code, int):
        details["status_code"] = status_code
    return StructuredError(
        code=f"LLM_{status.value.upper()}",
        message=messages.get(status, "Provider call failed."),
        details=details,
    )


def _extract_completion(completion: Any) -> tuple[Optional[str], Optional[Mapping[str, Any]], Optional[str]]:
    """Extract content, real usage metadata, and reported model from a completion."""
    content: Optional[str] = None
    try:
        choices = getattr(completion, "choices", None) or ()
        if choices:
            message = getattr(choices[0], "message", None)
            raw_content = getattr(message, "content", None)
            if isinstance(raw_content, str):
                content = raw_content
    except Exception:  # noqa: BLE001 - provider object is untrusted
        content = None

    usage: Optional[Dict[str, Any]] = None
    try:
        raw_usage = getattr(completion, "usage", None)
        if raw_usage is not None:
            collected: Dict[str, Any] = {}
            for name in ("prompt_tokens", "completion_tokens", "total_tokens"):
                value = getattr(raw_usage, name, None)
                if isinstance(value, int) and not isinstance(value, bool):
                    collected[name] = value
            if collected:
                usage = collected
    except Exception:  # noqa: BLE001 - usage is optional metadata
        usage = None

    model = getattr(completion, "model", None)
    return content, usage, model if isinstance(model, str) else None


__all__ = ["GroqProposalProvider", "PROVIDER_ID"]
