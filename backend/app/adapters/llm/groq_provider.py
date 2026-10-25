"""Groq runtime adapter for the LLM Scientist (Issue #41).

The adapter hides the provider SDK behind :class:`LLMProposalProvider`. It reads
API credentials from ordered environment-variable slots at construction time,
never stores or returns the values, and converts every failure into the
structured :class:`ProviderCallResult` contract.

Credential rotation
-------------------
When several credential slots are configured, calls start at the next slot in
round-robin order. A slot that is rate limited hands the call to the next
configured slot within the same bounded attempt sequence; authentication,
permission, timeout, provider, malformed, and oversized failures return
immediately (no credential is silently skipped for them). If every configured
slot is rate limited, the call reports ``RATE_LIMITED``. Only the 1-based slot
index is recorded in diagnostics, never the key.

Security properties
-------------------
- No module-level SDK import: importing this module never requires ``groq``.
- Credentials are used once to build clients and are not retained by this object.
- Exceptions are reported by type and (when present) HTTP status only; provider
  messages, request headers, prompts, and credentials are never copied into
  errors, records, or serialized results.
- Retries are bounded and apply only to documented transient failures. Auth and
  malformed/oversized responses are never retried.
"""

from __future__ import annotations

import os
import time
from collections.abc import Mapping, Sequence
from typing import Any, Callable, Dict, List, Optional, Tuple

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
    """Bounded, rotating Groq chat-completions provider."""

    provider_id = PROVIDER_ID
    is_live = True

    def __init__(
        self,
        config: LLMScientistConfig,
        *,
        api_key: Optional[str] = None,
        api_keys: Optional[Sequence[str]] = None,
        client: Any = None,
        client_factory: Optional[Callable[[str], Any]] = None,
        sleep: Callable[[float], None] = time.sleep,
        environ: Optional[Mapping[str, str]] = None,
    ) -> None:
        """Create the provider.

        Parameters
        ----------
        config
            Scientist configuration (model, timeout, retries, token bound, and
            ordered credential variable names).
        api_key
            Optional single explicit credential for tests. Production callers
            should omit it and set the environment variables named by
            ``config.credential_env_vars`` instead.
        api_keys
            Optional explicit ordered credentials for tests (rotation).
        client
            Optional pre-built SDK-compatible client. When supplied, exactly one
            credential slot is used and no SDK import or environment lookup
            happens.
        client_factory
            Optional callable mapping one credential to a client; used by tests
            that need one client per configured slot.
        sleep
            Injectable sleep function so retry tests do not wait.
        environ
            Injectable environment mapping for tests.
        """
        if not isinstance(config, LLMScientistConfig):
            raise ValueError("config must be an LLMScientistConfig.")
        self._config = config
        self._sleep = sleep
        # Each entry is (1-based configured slot position, variable name, client).
        self._slots: List[Tuple[int, str, Any]] = []
        self._next_start = 0
        self._missing_credentials = False
        self._sdk_unavailable = False

        if client is not None:
            self._slots = [(1, config.credential_env_vars[0], client)]
            return

        source = os.environ if environ is None else environ
        pairs = self._resolve_pairs(source, api_key, api_keys)
        if not pairs:
            # Record only that no credential was present, never any value.
            self._missing_credentials = True
            return

        if client_factory is not None:
            self._slots = [
                (position, name, client_factory(credential))
                for position, name, credential in pairs
            ]
            return

        built: List[Tuple[int, str, Any]] = []
        for position, name, credential in pairs:
            candidate = self._build_client(credential)
            if candidate is not None:
                built.append((position, name, candidate))
        self._slots = built
        if not built:
            self._sdk_unavailable = True
        # Drop local credential references; clients own their configuration.
        credential = ""
        pairs = []

    @property
    def model_id(self) -> str:
        return self._config.model

    @property
    def credential_slots(self) -> int:
        """Number of usable credential slots (values are never exposed)."""
        return len(self._slots)

    def _resolve_pairs(
        self,
        source: Mapping[str, str],
        api_key: Optional[str],
        api_keys: Optional[Sequence[str]],
    ) -> List[Tuple[int, str, str]]:
        """Resolve ordered (position, variable name, credential) pairs."""
        names = list(self._config.credential_env_vars)
        pairs: List[Tuple[int, str, str]] = []
        if api_keys is not None:
            for index, value in enumerate(api_keys):
                if isinstance(value, str) and value.strip():
                    name = names[index] if index < len(names) else f"INJECTED_API_KEY_{index + 1}"
                    pairs.append((index + 1, name, value.strip()))
            return pairs

        if api_key is not None and api_key.strip():
            pairs.append((1, names[0], api_key.strip()))
        for index, name in enumerate(names, start=1):
            raw = source.get(name)
            if isinstance(raw, str) and raw.strip():
                if not any(existing_name == name for _, existing_name, _ in pairs):
                    pairs.append((index, name, raw.strip()))
        return pairs

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
        except TypeError:
            try:
                import httpx
                return Groq(
                    api_key=credential,
                    timeout=float(self._config.timeout_seconds),
                    max_retries=0,
                    http_client=httpx.Client(),
                )
            except Exception:
                self._sdk_unavailable = True
                return None
        except Exception:  # noqa: BLE001 - client construction failure
            self._sdk_unavailable = True
            return None

    # ------------------------------------------------------------------
    # Provider interface
    # ------------------------------------------------------------------

    def propose(self, request: LLMProposalRequest) -> ProviderCallResult:
        """Call Groq with rotation, bounded retries, and a structured result."""
        if not self._slots:
            if self._missing_credentials:
                return ProviderCallResult(
                    status=ProviderOutcomeStatus.MISSING_CREDENTIALS,
                    error=StructuredError(
                        code="LLM_MISSING_CREDENTIALS",
                        message=(
                            "LLM-assisted mode is enabled but no credential is configured. "
                            "Set at least one of the configured credential environment "
                            "variables."
                        ),
                        details={
                            "credential_env_var": self._config.credential_env_var,
                            "credential_env_vars": list(self._config.credential_env_vars),
                        },
                    ),
                    attempts=0,
                    is_live=True,
                )
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
        max_attempts_per_slot = 1 + self._config.max_retries
        start = time.monotonic()
        start_index = self._next_start % len(self._slots)
        order = list(range(start_index, len(self._slots))) + list(
            range(0, start_index)
        )

        total_attempts = 0
        last_status = ProviderOutcomeStatus.PROVIDER_ERROR
        last_error = StructuredError(
            code="LLM_PROVIDER_ERROR",
            message="Provider call failed without a structured explanation.",
        )
        last_slot: Optional[int] = None
        last_name = self._slots[0][1]

        for slot_index in order:
            slot_position, name, client = self._slots[slot_index]
            last_name = name
            for attempt_in_slot in range(1, max_attempts_per_slot + 1):
                total_attempts += 1
                try:
                    completion = client.chat.completions.create(
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
                    last_slot = slot_position
                    last_error = _error_for(status, exc, name)
                    if retryable and attempt_in_slot < max_attempts_per_slot:
                        self._sleep(_backoff(attempt_in_slot))
                        continue
                    break

                content, usage, model = _extract_completion(completion)
                if not isinstance(content, str) or not content.strip():
                    return ProviderCallResult(
                        status=ProviderOutcomeStatus.MALFORMED_RESPONSE,
                        error=StructuredError(
                            code="LLM_MALFORMED_RESPONSE",
                            message="Provider returned no text content.",
                            details={"credential_env_var": name},
                        ),
                        attempts=total_attempts,
                        model=model,
                        usage=usage,
                        duration_ms=_elapsed_ms(start),
                        is_live=True,
                        credential_slot=slot_position,
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
                            details={
                                "response_characters": len(content),
                                "credential_env_var": name,
                            },
                        ),
                        attempts=total_attempts,
                        model=model,
                        usage=usage,
                        duration_ms=_elapsed_ms(start),
                        is_live=True,
                        credential_slot=slot_position,
                    )
                self._next_start = (slot_index + 1) % len(self._slots)
                return ProviderCallResult(
                    status=ProviderOutcomeStatus.SUCCESS,
                    content=content,
                    attempts=total_attempts,
                    model=model,
                    usage=usage,
                    duration_ms=_elapsed_ms(start),
                    is_live=True,
                    credential_slot=slot_position,
                )

            if last_status != ProviderOutcomeStatus.RATE_LIMITED:
                # Only a rate limit rotates credentials. Auth/permission/provider
                # failures are returned immediately so a bad key is not hidden by
                # another slot.
                return ProviderCallResult(
                    status=last_status,
                    error=last_error,
                    attempts=total_attempts,
                    model=self._config.model,
                    duration_ms=_elapsed_ms(start),
                    is_live=True,
                    credential_slot=last_slot,
                )

        # Every configured slot was rate limited.
        self._next_start = (order[-1] + 1) % len(self._slots)
        return ProviderCallResult(
            status=ProviderOutcomeStatus.RATE_LIMITED,
            error=StructuredError(
                code="LLM_RATE_LIMITED",
                message=(
                    f"All {len(self._slots)} configured credential slot(s) were rate "
                    "limited; no further retry was permitted."
                ),
                details={
                    "credential_slot_count": len(self._slots),
                    "credential_env_vars": [slot_name for _, slot_name, _ in self._slots],
                    "last_credential_env_var": last_name,
                },
            ),
            attempts=total_attempts,
            model=self._config.model,
            duration_ms=_elapsed_ms(start),
            is_live=True,
            credential_slot=last_slot,
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


def _error_for(
    status: ProviderOutcomeStatus,
    exc: BaseException,
    credential_env_var: Optional[str] = None,
) -> StructuredError:
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
    if credential_env_var:
        # The slot NAME only; the credential value is never included.
        details["credential_env_var"] = credential_env_var
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
