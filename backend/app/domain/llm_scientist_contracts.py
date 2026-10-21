"""Contracts for the optional LLM Scientist (Issue #41).

The LLM Scientist is a *bounded proposal source*: it may suggest candidate
hypotheses and follow-up experiment inputs from permitted observations and
evidence, but CLARITY validates, fits, evaluates, ranks, selects experiments,
queries the oracle, and accounts for budget. The governing rule is:

    The LLM proposes. CLARITY validates and measures.

Trust boundary
--------------
Everything a provider returns is untrusted data. Nothing in this module calls an
oracle, reads evaluator-only ground truth, imports a provider SDK, executes
generated code, or mutates committed discovery state. Provider credentials never
appear in these contracts: only the *name* of the environment variable that
holds the credential is recorded.

Defaults are safe: :class:`LLMScientistConfig` is disabled, so a fresh install
runs deterministic discovery without credentials, network access, or provider
packages being required at import time.
"""

from __future__ import annotations

import math
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional, Protocol, Tuple

from .contracts import (
    StructuredError,
    freeze_payload,
    thaw_payload,
    utc_now_iso,
    verify_no_ground_truth_leakage,
)

SCIENTIST_VERSION = "1.0.0"

# Versioned prompt and response-schema identifiers. They are recorded in every
# proposal's provenance so a run can be audited against the exact instructions
# that produced it.
SCIENTIST_PROMPT_VERSION = "1.0.0"
SCIENTIST_SCHEMA_VERSION = "1.0.0"

DEFAULT_PROVIDER_ID = "groq"
DEFAULT_MODEL_ID = "llama-3.3-70b-versatile"
DEFAULT_CREDENTIAL_ENV_VAR = "GROQ_API_KEY"
# Ordered credential slots. The provider rotates to the next configured slot when
# one is rate limited, so a run can continue within the same bounded policy.
DEFAULT_CREDENTIAL_ENV_VARS: Tuple[str, ...] = (
    "GROQ_API_KEY",
    "GROQ_API_KEY_2",
    "GROQ_API_KEY_3",
    "GROQ_API_KEY_4",
    "GROQ_API_KEY_5",
)
MAX_CREDENTIAL_ENV_VARS = 10

# Hard ceilings that keep one provider call bounded regardless of configuration.
MAX_PROPOSALS_CEILING = 10
MAX_ROUNDS_CEILING = 20
MAX_TIMEOUT_SECONDS = 120.0
MAX_RETRIES_CEILING = 3
MAX_RESPONSE_CHARACTERS_CEILING = 200_000
MAX_CONTEXT_ITEMS_CEILING = 200


# ==============================================================================
# 1. Structured outcome and rejection codes
# ==============================================================================

class ProviderOutcomeStatus(str, Enum):
    """Structured result of one provider call.

    Every failure mode is distinct so logs, diagnostics, and fallback behavior
    can name what actually happened instead of collapsing to an empty list.
    """

    SUCCESS = "success"
    DISABLED = "disabled"
    NOT_CONFIGURED = "not_configured"
    MISSING_CREDENTIALS = "missing_credentials"
    SDK_UNAVAILABLE = "sdk_unavailable"
    AUTHENTICATION_FAILED = "authentication_failed"
    PERMISSION_DENIED = "permission_denied"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    TRANSIENT_FAILURE = "transient_failure"
    PROVIDER_ERROR = "provider_error"
    MALFORMED_RESPONSE = "malformed_response"
    RESPONSE_TOO_LARGE = "response_too_large"

    @property
    def is_transient(self) -> bool:
        """Whether a bounded retry is appropriate for this outcome."""
        return self in (
            ProviderOutcomeStatus.RATE_LIMITED,
            ProviderOutcomeStatus.TIMEOUT,
            ProviderOutcomeStatus.TRANSIENT_FAILURE,
        )


class ProposalRejectionCode(str, Enum):
    """Why one proposed hypothesis was rejected before entering the pipeline."""

    MALFORMED_RESPONSE = "malformed_response"
    SCHEMA_INVALID = "schema_invalid"
    MISSING_FIELD = "missing_field"
    INVALID_TYPE = "invalid_type"
    UNSUPPORTED_MODEL_FAMILY = "unsupported_model_family"
    UNSUPPORTED_REPRESENTATION = "unsupported_representation"
    UNSAFE_FIELD = "unsafe_field"
    NON_FINITE_VALUE = "non_finite_value"
    OUT_OF_DOMAIN = "out_of_domain"
    TOO_MANY_PROPOSALS = "too_many_proposals"
    DUPLICATE_PROPOSAL = "duplicate_proposal"
    INVALID_SUGGESTION = "invalid_suggestion"


class ScientistRoundStatus(str, Enum):
    """Terminal status of one proposal round."""

    SUCCESS = "success"
    NO_VALID_PROPOSALS = "no_valid_proposals"
    PROVIDER_FAILURE = "provider_failure"
    DISABLED = "disabled"


class ScientistFallbackPolicy(str, Enum):
    """Explicit fallback behavior when a round yields nothing usable.

    CONTINUE_DETERMINISTIC
        Keep running deterministic discovery with the already-committed candidate
        set. The fallback is recorded in the round record; it is never presented
        as an LLM success.

    DISABLE_FURTHER_ROUNDS
        Same deterministic continuation, but also stop calling the provider for
        the remaining run. Used when repeated provider failures should not keep
        consuming latency/budget.
    """

    CONTINUE_DETERMINISTIC = "continue_deterministic"
    DISABLE_FURTHER_ROUNDS = "disable_further_rounds"


# ==============================================================================
# 2. Configuration
# ==============================================================================

def _parse_bool(value: str, name: str) -> bool:
    normalized = value.strip().lower()
    if normalized in ("1", "true", "yes", "on"):
        return True
    if normalized in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"{name} must be a boolean (true/false), got {value!r}.")


def _parse_int(value: str, name: str) -> int:
    try:
        return int(value.strip())
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be an integer, got {value!r}.") from None


def _parse_credential_names(
    source: Mapping[str, str], defaults: "LLMScientistConfig"
) -> Tuple[str, ...]:
    """Resolve ordered credential variable names from the environment.

    Only names are read here; values stay in the environment until the provider
    constructs its clients, and are never stored in configuration.
    """
    plural = source.get("LLM_SCIENTIST_CREDENTIAL_ENV_VARS")
    if plural is not None and plural.strip():
        names = tuple(item.strip() for item in plural.split(",") if item.strip())
        return names or defaults.credential_env_vars
    legacy = source.get("LLM_SCIENTIST_CREDENTIAL_ENV_VAR")
    if legacy is not None and legacy.strip():
        return (legacy.strip(),)
    return defaults.credential_env_vars


def _parse_float(value: str, name: str) -> float:
    try:
        number = float(value.strip())
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number, got {value!r}.") from None
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite, got {value!r}.")
    return number


@dataclass(frozen=True)
class LLMScientistConfig:
    """Validated configuration for the optional LLM Scientist.

    Fields
    ------
    enabled
        Master switch. Default ``False``: deterministic-only discovery never
        constructs a provider or touches the network.
    provider
        Provider identifier. ``groq`` is the only runtime provider implemented.
    model
        Provider model identifier, recorded as provenance.
    credential_env_vars
        Ordered *names* of the environment variables holding provider API keys.
        The provider tries them in rotation and moves to the next slot when a key
        is rate limited. Values are never stored in configuration, serialized,
        or logged.
    max_proposals_per_round
        Upper bound on accepted proposals requested/used from one round.
    timeout_seconds
        Per-request timeout handed to the provider client.
    max_retries
        Bounded retries for transient provider failures only (not for
        authentication or malformed output).
    max_tokens
        Response token bound requested from the provider.
    max_response_characters
        Local response-size bound. Oversized responses are rejected before JSON
        parsing.
    max_context_observations / max_context_hypotheses
        Bounds on the permitted evidence context (most recent/ranked items kept).
    max_rounds
        Maximum proposal rounds per run, so the LLM can never drive unbounded
        candidate or token growth.
    temperature
        Sampling temperature recorded as provenance; it does not make output
        reproducible.
    fallback_policy
        Explicit behavior when a round yields no usable proposal.
    """

    enabled: bool = False
    provider: str = DEFAULT_PROVIDER_ID
    model: str = DEFAULT_MODEL_ID
    credential_env_vars: Tuple[str, ...] = DEFAULT_CREDENTIAL_ENV_VARS
    max_proposals_per_round: int = 3
    timeout_seconds: float = 20.0
    max_retries: int = 1
    max_tokens: int = 1024
    max_response_characters: int = 20_000
    max_context_observations: int = 20
    max_context_hypotheses: int = 8
    max_rounds: int = 3
    temperature: float = 0.2
    fallback_policy: ScientistFallbackPolicy = ScientistFallbackPolicy.CONTINUE_DETERMINISTIC

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise ValueError("enabled must be a bool.")
        if not isinstance(self.provider, str) or not self.provider:
            raise ValueError("provider must be a non-empty string.")
        if not isinstance(self.model, str) or not self.model:
            raise ValueError("model must be a non-empty string.")
        if isinstance(self.credential_env_vars, str) or not isinstance(
            self.credential_env_vars, (tuple, list)
        ):
            raise ValueError("credential_env_vars must be a sequence of variable names.")
        names = tuple(self.credential_env_vars)
        if not names:
            raise ValueError("credential_env_vars must contain at least one name.")
        if len(names) > MAX_CREDENTIAL_ENV_VARS:
            raise ValueError(
                f"credential_env_vars cannot exceed {MAX_CREDENTIAL_ENV_VARS} entries."
            )
        if len(set(names)) != len(names):
            raise ValueError("credential_env_vars must not contain duplicate names.")
        for name in names:
            if not isinstance(name, str) or not name.strip():
                raise ValueError("credential_env_vars entries must be non-empty strings.")
        object.__setattr__(self, "credential_env_vars", names)
        self._check_int("max_proposals_per_round", self.max_proposals_per_round, 1, MAX_PROPOSALS_CEILING)
        self._check_int("max_rounds", self.max_rounds, 1, MAX_ROUNDS_CEILING)
        self._check_int("max_retries", self.max_retries, 0, MAX_RETRIES_CEILING)
        self._check_int("max_tokens", self.max_tokens, 1, 32_768)
        self._check_int(
            "max_response_characters",
            self.max_response_characters,
            1,
            MAX_RESPONSE_CHARACTERS_CEILING,
        )
        self._check_int("max_context_observations", self.max_context_observations, 1, MAX_CONTEXT_ITEMS_CEILING)
        self._check_int("max_context_hypotheses", self.max_context_hypotheses, 1, MAX_CONTEXT_ITEMS_CEILING)
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not math.isfinite(float(self.timeout_seconds))
            or not 0.0 < float(self.timeout_seconds) <= MAX_TIMEOUT_SECONDS
        ):
            raise ValueError(
                f"timeout_seconds must be a finite number in (0, {MAX_TIMEOUT_SECONDS}]."
            )
        if (
            isinstance(self.temperature, bool)
            or not isinstance(self.temperature, (int, float))
            or not math.isfinite(float(self.temperature))
            or not 0.0 <= float(self.temperature) <= 2.0
        ):
            raise ValueError("temperature must be a finite number in [0.0, 2.0].")
        if not isinstance(self.fallback_policy, ScientistFallbackPolicy):
            raise ValueError(
                "fallback_policy must be a ScientistFallbackPolicy, got "
                f"{type(self.fallback_policy).__name__}."
            )

    @property
    def credential_env_var(self) -> str:
        """First credential slot name (backwards-compatible singular accessor)."""
        return self.credential_env_vars[0]

    @staticmethod
    def _check_int(name: str, value: Any, minimum: int, maximum: int) -> None:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{name} must be an integer.")
        if not minimum <= value <= maximum:
            raise ValueError(f"{name} must be between {minimum} and {maximum}, got {value}.")

    @classmethod
    def from_environment(cls, environ: Optional[Mapping[str, str]] = None) -> "LLMScientistConfig":
        """Build a configuration from environment variables.

        Unknown/invalid values raise ``ValueError`` so a misconfigured deployment
        fails loudly at startup instead of silently running with an unintended
        provider, model, or budget.

        Variables:
          LLM_SCIENTIST_ENABLED, LLM_SCIENTIST_PROVIDER, LLM_SCIENTIST_MODEL,
          LLM_SCIENTIST_CREDENTIAL_ENV_VARS (comma-separated names; legacy
          LLM_SCIENTIST_CREDENTIAL_ENV_VAR still works), LLM_SCIENTIST_MAX_PROPOSALS,
          LLM_SCIENTIST_TIMEOUT_SECONDS, LLM_SCIENTIST_MAX_RETRIES,
          LLM_SCIENTIST_MAX_TOKENS, LLM_SCIENTIST_MAX_RESPONSE_CHARS,
          LLM_SCIENTIST_MAX_CONTEXT_OBSERVATIONS,
          LLM_SCIENTIST_MAX_CONTEXT_HYPOTHESES, LLM_SCIENTIST_MAX_ROUNDS,
          LLM_SCIENTIST_TEMPERATURE, LLM_SCIENTIST_FALLBACK_POLICY
        """
        source = os.environ if environ is None else environ
        defaults = cls()

        def get(name: str) -> Optional[str]:
            raw = source.get(name)
            if raw is None:
                return None
            stripped = raw.strip()
            return stripped if stripped else None

        policy_raw = get("LLM_SCIENTIST_FALLBACK_POLICY")
        fallback = defaults.fallback_policy
        if policy_raw is not None:
            try:
                fallback = ScientistFallbackPolicy(policy_raw.lower())
            except ValueError:
                allowed = ", ".join(item.value for item in ScientistFallbackPolicy)
                raise ValueError(
                    f"LLM_SCIENTIST_FALLBACK_POLICY must be one of: {allowed}."
                ) from None

        enabled_raw = get("LLM_SCIENTIST_ENABLED")
        return cls(
            enabled=(
                _parse_bool(enabled_raw, "LLM_SCIENTIST_ENABLED")
                if enabled_raw is not None
                else defaults.enabled
            ),
            provider=get("LLM_SCIENTIST_PROVIDER") or defaults.provider,
            model=get("LLM_SCIENTIST_MODEL") or defaults.model,
            credential_env_vars=_parse_credential_names(source, defaults),
            max_proposals_per_round=(
                _parse_int(get("LLM_SCIENTIST_MAX_PROPOSALS"), "LLM_SCIENTIST_MAX_PROPOSALS")
                if get("LLM_SCIENTIST_MAX_PROPOSALS") is not None
                else defaults.max_proposals_per_round
            ),
            timeout_seconds=(
                _parse_float(get("LLM_SCIENTIST_TIMEOUT_SECONDS"), "LLM_SCIENTIST_TIMEOUT_SECONDS")
                if get("LLM_SCIENTIST_TIMEOUT_SECONDS") is not None
                else defaults.timeout_seconds
            ),
            max_retries=(
                _parse_int(get("LLM_SCIENTIST_MAX_RETRIES"), "LLM_SCIENTIST_MAX_RETRIES")
                if get("LLM_SCIENTIST_MAX_RETRIES") is not None
                else defaults.max_retries
            ),
            max_tokens=(
                _parse_int(get("LLM_SCIENTIST_MAX_TOKENS"), "LLM_SCIENTIST_MAX_TOKENS")
                if get("LLM_SCIENTIST_MAX_TOKENS") is not None
                else defaults.max_tokens
            ),
            max_response_characters=(
                _parse_int(
                    get("LLM_SCIENTIST_MAX_RESPONSE_CHARS"), "LLM_SCIENTIST_MAX_RESPONSE_CHARS"
                )
                if get("LLM_SCIENTIST_MAX_RESPONSE_CHARS") is not None
                else defaults.max_response_characters
            ),
            max_context_observations=(
                _parse_int(
                    get("LLM_SCIENTIST_MAX_CONTEXT_OBSERVATIONS"),
                    "LLM_SCIENTIST_MAX_CONTEXT_OBSERVATIONS",
                )
                if get("LLM_SCIENTIST_MAX_CONTEXT_OBSERVATIONS") is not None
                else defaults.max_context_observations
            ),
            max_context_hypotheses=(
                _parse_int(
                    get("LLM_SCIENTIST_MAX_CONTEXT_HYPOTHESES"),
                    "LLM_SCIENTIST_MAX_CONTEXT_HYPOTHESES",
                )
                if get("LLM_SCIENTIST_MAX_CONTEXT_HYPOTHESES") is not None
                else defaults.max_context_hypotheses
            ),
            max_rounds=(
                _parse_int(get("LLM_SCIENTIST_MAX_ROUNDS"), "LLM_SCIENTIST_MAX_ROUNDS")
                if get("LLM_SCIENTIST_MAX_ROUNDS") is not None
                else defaults.max_rounds
            ),
            temperature=(
                _parse_float(get("LLM_SCIENTIST_TEMPERATURE"), "LLM_SCIENTIST_TEMPERATURE")
                if get("LLM_SCIENTIST_TEMPERATURE") is not None
                else defaults.temperature
            ),
            fallback_policy=fallback,
        )

    def to_dict(self) -> Dict[str, Any]:
        """Discovery-safe configuration snapshot (never contains credentials)."""
        return {
            "scientist_version": SCIENTIST_VERSION,
            "enabled": self.enabled,
            "provider": self.provider,
            "model": self.model,
            "credential_env_var": self.credential_env_var,
            "credential_env_vars": list(self.credential_env_vars),
            "max_proposals_per_round": self.max_proposals_per_round,
            "timeout_seconds": float(self.timeout_seconds),
            "max_retries": self.max_retries,
            "max_tokens": self.max_tokens,
            "max_response_characters": self.max_response_characters,
            "max_context_observations": self.max_context_observations,
            "max_context_hypotheses": self.max_context_hypotheses,
            "max_rounds": self.max_rounds,
            "temperature": float(self.temperature),
            "fallback_policy": self.fallback_policy.value,
            "prompt_version": SCIENTIST_PROMPT_VERSION,
            "schema_version": SCIENTIST_SCHEMA_VERSION,
        }


# ==============================================================================
# 3. Request contract
# ==============================================================================

@dataclass(frozen=True)
class LLMProposalRequest:
    """Bounded, permitted evidence context sent to a proposal provider.

    The request is assembled by :class:`~app.domain.llm_scientist.LLMScientist`
    from explicit public discovery state only. It never carries oracle objects,
    hidden rules, evaluator labels, benchmark answers, or credentials.
    """

    request_id: str
    round_index: int
    domain: str
    variable_name: str
    output_variable: str
    domain_min: float
    domain_max: float
    model_family: str
    max_polynomial_degree: int
    max_proposals: int
    observations: Tuple[Mapping[str, Any], ...] = ()
    hypotheses: Tuple[Mapping[str, Any], ...] = ()
    ranking_summary: Mapping[str, Any] = field(default_factory=dict)
    existing_canonical_keys: Tuple[str, ...] = ()
    previous_rounds: Tuple[Mapping[str, Any], ...] = ()
    failure_summaries: Tuple[str, ...] = ()
    prompt_version: str = SCIENTIST_PROMPT_VERSION
    schema_version: str = SCIENTIST_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.request_id, str) or not self.request_id:
            raise ValueError("request_id must be a non-empty string.")
        if self.round_index < 1:
            raise ValueError("round_index must be >= 1.")
        if not self.model_family:
            raise ValueError("model_family must be a non-empty string.")
        if self.max_proposals < 1:
            raise ValueError("max_proposals must be >= 1.")
        if self.max_polynomial_degree < 0:
            raise ValueError("max_polynomial_degree must be >= 0.")
        for name, value in (("domain_min", self.domain_min), ("domain_max", self.domain_max)):
            if not math.isfinite(float(value)):
                raise ValueError(f"{name} must be finite.")
        if float(self.domain_min) >= float(self.domain_max):
            raise ValueError("domain_min must be smaller than domain_max.")
        for name in ("observations", "hypotheses", "previous_rounds"):
            payload = getattr(self, name)
            if not isinstance(payload, tuple):
                raise ValueError(f"{name} must be a tuple.")
        object.__setattr__(
            self, "observations", tuple(freeze_payload(dict(item)) for item in self.observations)
        )
        object.__setattr__(
            self, "hypotheses", tuple(freeze_payload(dict(item)) for item in self.hypotheses)
        )
        object.__setattr__(
            self,
            "previous_rounds",
            tuple(freeze_payload(dict(item)) for item in self.previous_rounds),
        )
        object.__setattr__(self, "existing_canonical_keys", tuple(self.existing_canonical_keys))
        object.__setattr__(self, "failure_summaries", tuple(self.failure_summaries))
        object.__setattr__(self, "ranking_summary", freeze_payload(dict(self.ranking_summary)))
        # Fail closed if a caller ever wires evaluator state into the request.
        verify_no_ground_truth_leakage(self.to_dict())

    def to_dict(self) -> Dict[str, Any]:
        """Plain JSON-compatible request payload sent to the provider."""
        return {
            "request_id": self.request_id,
            "round_index": self.round_index,
            "prompt_version": self.prompt_version,
            "schema_version": self.schema_version,
            "domain": self.domain,
            "variable_name": self.variable_name,
            "output_variable": self.output_variable,
            "domain_bounds": {
                "min_val": float(self.domain_min),
                "max_val": float(self.domain_max),
            },
            "model_family": self.model_family,
            "max_polynomial_degree": self.max_polynomial_degree,
            "max_proposals": self.max_proposals,
            "observations": thaw_payload(self.observations),
            "hypotheses": thaw_payload(self.hypotheses),
            "ranking_summary": thaw_payload(self.ranking_summary),
            "existing_canonical_keys": list(self.existing_canonical_keys),
            "previous_rounds": thaw_payload(self.previous_rounds),
            "failure_summaries": list(self.failure_summaries),
        }

    def to_json(self) -> str:
        """Deterministic JSON rendering used as the user message."""
        import json

        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))


# ==============================================================================
# 4. Provider contract
# ==============================================================================

@dataclass(frozen=True)
class ProviderCallResult:
    """Structured result of exactly one provider call (never a raw exception).

    Fields
    ------
    status
        Structured outcome. ``SUCCESS`` is the only status with content.
    content
        Untrusted response text; parsed and validated by the scientist.
    error
        Structured failure explanation when status is not ``SUCCESS``. Never
        contains credentials, authorization headers, or prompt content.
    attempts
        Number of provider attempts actually made (bounded retries included).
    model
        Model identifier reported by the provider, when available.
    usage
        Token metadata actually returned by the provider. Absent means
        unavailable; it is never invented.
    duration_ms
        Measured wall-clock duration of the provider call sequence.
    is_live
        True only for a real provider client. Fixed fake providers report False
        so a test can never be described as live execution.
    credential_slot
        1-based credential slot that produced the final outcome, when multiple
        configured keys are used. The key itself is never recorded.
    """

    status: ProviderOutcomeStatus
    content: Optional[str] = None
    error: Optional[StructuredError] = None
    attempts: int = 1
    model: Optional[str] = None
    usage: Optional[Mapping[str, Any]] = None
    duration_ms: Optional[float] = None
    is_live: bool = False
    credential_slot: Optional[int] = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, ProviderOutcomeStatus):
            raise ValueError("status must be a ProviderOutcomeStatus.")
        if self.status == ProviderOutcomeStatus.SUCCESS:
            if not isinstance(self.content, str):
                raise ValueError("SUCCESS requires string content.")
        elif self.error is None:
            raise ValueError(f"Provider status {self.status.value} requires a structured error.")
        # Zero is valid: a disabled/unconfigured provider can fail before any
        # network attempt is made, and that must be reported truthfully.
        if self.attempts < 0:
            raise ValueError("attempts must be >= 0.")
        if self.usage is not None:
            if not isinstance(self.usage, Mapping):
                raise ValueError("usage must be a mapping or None.")
            object.__setattr__(self, "usage", freeze_payload(dict(self.usage)))
        if self.credential_slot is not None:
            if (
                isinstance(self.credential_slot, bool)
                or not isinstance(self.credential_slot, int)
                or self.credential_slot < 1
            ):
                raise ValueError("credential_slot must be a positive integer or None.")


class LLMProposalProvider(Protocol):
    """Narrow provider-independent interface consumed by the scientist.

    Implementations hide provider SDK details. They receive only the permitted
    request contract and must return a structured :class:`ProviderCallResult`;
    they may never raise provider-specific exceptions across this boundary.
    """

    provider_id: str
    model_id: str
    is_live: bool

    def propose(self, request: LLMProposalRequest) -> ProviderCallResult:
        """Return one bounded structured response or a structured failure."""
        ...


# ==============================================================================
# 5. Proposal and round records
# ==============================================================================

@dataclass(frozen=True)
class ProposalRejection:
    """One rejected proposal with an actionable, non-sensitive diagnostic."""

    proposal_index: int
    code: ProposalRejectionCode
    message: str
    details: Optional[Mapping[str, Any]] = None

    def __post_init__(self) -> None:
        if self.proposal_index < 0:
            raise ValueError("proposal_index must be >= 0.")
        if not isinstance(self.code, ProposalRejectionCode):
            raise ValueError("code must be a ProposalRejectionCode.")
        if not isinstance(self.message, str) or not self.message:
            raise ValueError("message must be a non-empty string.")
        if self.details is not None:
            object.__setattr__(self, "details", freeze_payload(dict(self.details)))

    def to_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "proposal_index": self.proposal_index,
            "code": self.code.value,
            "message": self.message,
        }
        if self.details is not None:
            data["details"] = thaw_payload(self.details)
        return data


@dataclass(frozen=True)
class ScientistRoundRecord:
    """Auditable provenance for one proposal round.

    Recorded for every round, including failures and rounds that produced no
    valid hypothesis, so the reason is never lost. Contains no credentials, no
    raw provider payloads, and no evaluator-only data.
    """

    round_id: str
    round_index: int
    run_id: str
    provider_id: str
    model_id: str
    prompt_version: str
    schema_version: str
    status: ScientistRoundStatus
    started_at: str
    finished_at: str
    duration_ms: float
    proposals_returned: int = 0
    accepted_count: int = 0
    rejected_count: int = 0
    accepted_hypothesis_ids: Tuple[str, ...] = ()
    rejected: Tuple[ProposalRejection, ...] = ()
    outcome_status: Optional[ProviderOutcomeStatus] = None
    error: Optional[StructuredError] = None
    attempts: int = 0
    usage: Optional[Mapping[str, Any]] = None
    is_live_provider: bool = False
    credential_slot: Optional[int] = None
    fallback_applied: bool = False
    fallback_reason: Optional[str] = None
    evidence_summary: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.round_index < 1:
            raise ValueError("round_index must be >= 1.")
        if not isinstance(self.status, ScientistRoundStatus):
            raise ValueError("status must be a ScientistRoundStatus.")
        if self.status == ScientistRoundStatus.SUCCESS and self.accepted_count < 1:
            raise ValueError("SUCCESS requires at least one accepted proposal.")
        if self.status == ScientistRoundStatus.SUCCESS and self.error is not None:
            raise ValueError("SUCCESS must not carry a provider error.")
        if self.status != ScientistRoundStatus.SUCCESS and not self.fallback_applied:
            raise ValueError(
                f"Non-success status {self.status.value} must record the fallback that was applied."
            )
        if self.proposals_returned < 0 or self.accepted_count < 0 or self.rejected_count < 0:
            raise ValueError("proposal counts must be non-negative.")
        if self.accepted_count > self.proposals_returned:
            raise ValueError("accepted_count cannot exceed proposals_returned.")
        if self.accepted_hypothesis_ids and len(tuple(self.accepted_hypothesis_ids)) != self.accepted_count:
            raise ValueError("accepted_hypothesis_ids must match accepted_count.")
        if self.credential_slot is not None:
            if (
                isinstance(self.credential_slot, bool)
                or not isinstance(self.credential_slot, int)
                or self.credential_slot < 1
            ):
                raise ValueError("credential_slot must be a positive integer or None.")
        object.__setattr__(self, "accepted_hypothesis_ids", tuple(self.accepted_hypothesis_ids))
        object.__setattr__(self, "rejected", tuple(self.rejected))
        object.__setattr__(self, "evidence_summary", freeze_payload(dict(self.evidence_summary)))
        if self.usage is not None:
            object.__setattr__(self, "usage", freeze_payload(dict(self.usage)))
        verify_no_ground_truth_leakage(self.to_dict())

    def to_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "round_id": self.round_id,
            "round_index": self.round_index,
            "run_id": self.run_id,
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "prompt_version": self.prompt_version,
            "schema_version": self.schema_version,
            "status": self.status.value,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_ms": self.duration_ms,
            "proposals_returned": self.proposals_returned,
            "accepted_count": self.accepted_count,
            "rejected_count": self.rejected_count,
            "accepted_hypothesis_ids": list(self.accepted_hypothesis_ids),
            "rejected": [item.to_dict() for item in self.rejected],
            "attempts": self.attempts,
            "is_live_provider": self.is_live_provider,
            "fallback_applied": self.fallback_applied,
            "evidence_summary": thaw_payload(self.evidence_summary),
        }
        if self.outcome_status is not None:
            data["outcome_status"] = self.outcome_status.value
        if self.error is not None:
            data["error"] = self.error.to_dict()
        if self.usage is not None:
            data["usage"] = thaw_payload(self.usage)
        if self.fallback_reason is not None:
            data["fallback_reason"] = self.fallback_reason
        if self.credential_slot is not None:
            data["credential_slot"] = self.credential_slot
        return data


__all__ = [
    "SCIENTIST_VERSION",
    "SCIENTIST_PROMPT_VERSION",
    "SCIENTIST_SCHEMA_VERSION",
    "DEFAULT_PROVIDER_ID",
    "DEFAULT_MODEL_ID",
    "DEFAULT_CREDENTIAL_ENV_VAR",
    "DEFAULT_CREDENTIAL_ENV_VARS",
    "MAX_CREDENTIAL_ENV_VARS",
    "MAX_PROPOSALS_CEILING",
    "MAX_ROUNDS_CEILING",
    "MAX_TIMEOUT_SECONDS",
    "MAX_RETRIES_CEILING",
    "MAX_RESPONSE_CHARACTERS_CEILING",
    "MAX_CONTEXT_ITEMS_CEILING",
    "ProviderOutcomeStatus",
    "ProposalRejectionCode",
    "ScientistRoundStatus",
    "ScientistFallbackPolicy",
    "LLMScientistConfig",
    "LLMProposalRequest",
    "ProviderCallResult",
    "LLMProposalProvider",
    "ProposalRejection",
    "ScientistRoundRecord",
]
