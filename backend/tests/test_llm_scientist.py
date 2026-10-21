"""Offline tests for the optional LLM Scientist (Issue #41).

Every test runs without credentials, network access, or a live model. The
provider is a deterministic in-process fake injected through the same
provider-independent interface Groq uses, so a passing test proves CLARITY's
validation/fallback behavior and never claims live provider execution.

Coverage:
A. Contracts, JSON parsing, schema and domain validation, canonical dedupe.
B. Provider outcomes: disabled, missing credentials, auth, rate limit, timeout,
   server error, malformed/oversized output, bounded retries, secret hygiene.
C. Discovery integration: deterministic mode unchanged, accepted proposals enter
   fitting/evaluation/ranking, evidence feedback rounds, explicit fallback,
   suggestions through the generator/planner/budget, state immutability.
D. Evaluator isolation and serialization.
E. Reproducibility with fixed fake responses.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Sequence

import pytest

from app.adapters.llm import build_llm_scientist_from_environment
from app.adapters.llm.groq_provider import GroqProposalProvider
from app.adapters.oracle.local_simulator import PolynomialLocalOracle
from app.domain.candidate_generator import HypothesisGenerationConfig
from app.domain.contracts import (
    EvaluationStatus,
    HypothesisOrigin,
    HypothesisProvenance,
    HypothesisRepresentation,
    Observation,
    StructuredError,
)
from app.domain.discovery_contracts import (
    WARNING_SCIENTIST_FALLBACK,
    WARNING_SCIENTIST_ROUND_FAILED,
    DiscoveryLifecycleStatus,
    DiscoveryRunConfig,
    ExperimentExecutionOutcome,
)
from app.domain.discovery_engine import DiscoveryEngine
from app.domain.experiment_generator import (
    CandidateExperimentGenerator,
    ExperimentGenerationConfig,
    ExperimentGenerationStrategy,
    NumericalDomainBounds,
)
from app.domain.llm_scientist import LLMScientist
from app.domain.llm_scientist_contracts import (
    SCIENTIST_PROMPT_VERSION,
    SCIENTIST_SCHEMA_VERSION,
    LLMScientistConfig,
    ProposalRejectionCode,
    ProviderCallResult,
    ProviderOutcomeStatus,
    ScientistFallbackPolicy,
    ScientistRoundStatus,
)
from app.domain.ranking_contracts import RankingPolicyConfig

FAKE_PROVIDER_SECRET = "fake-provider-secret-should-never-surface"
CANARY = "evaluator-canary-41a7-should-never-surface"

BOUNDS = NumericalDomainBounds(
    variable_name="x", min_val=-2.0, max_val=2.0, domain_name="test_polynomial"
)


# ==============================================================================
# Fixtures and helpers
# ==============================================================================


class FakeProvider:
    """Deterministic provider double implementing the real provider contract."""

    provider_id = "fake_provider"
    model_id = "fake-model-v1"
    is_live = False

    def __init__(self, responses: Sequence[Any]) -> None:
        self.responses: List[Any] = list(responses)
        self.requests: List[Any] = []

    def propose(self, request):
        self.requests.append(request)
        if not self.responses:
            raise AssertionError("FakeProvider received an unexpected extra call.")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        if callable(item):
            return item(request)
        return item


def success(
    payload: Any,
    *,
    usage: Optional[Dict[str, int]] = None,
    credential_slot: Optional[int] = None,
) -> ProviderCallResult:
    content = payload if isinstance(payload, str) else json.dumps(payload)
    return ProviderCallResult(
        status=ProviderOutcomeStatus.SUCCESS,
        content=content,
        attempts=1,
        model="fake-model-v1",
        usage=usage,
        duration_ms=1.0,
        is_live=False,
        credential_slot=credential_slot,
    )


def provider_error(status: ProviderOutcomeStatus, message: str = "provider failed") -> ProviderCallResult:
    return ProviderCallResult(
        status=status,
        error=StructuredError(code=f"LLM_{status.value.upper()}", message=message),
        attempts=1,
        is_live=False,
    )


def proposal(**overrides: Any) -> Dict[str, Any]:
    data: Dict[str, Any] = {
        "model_family": "polynomial",
        "degree": 2,
        "coefficients": [1.5, -0.25, 0.5],
        "rationale": "Measured points suggest curvature beyond a linear fit.",
        "assumptions": ["Measurement noise is small"],
        "competing_explanations": ["A linear rule with noise"],
        "distinguishing_evidence": ["Curvature at larger |x|"],
        "suggested_experiment_inputs": [],
        "self_reported_confidence": 0.4,
    }
    data.update(overrides)
    return data


def make_config(**overrides: Any) -> LLMScientistConfig:
    defaults: Dict[str, Any] = dict(
        enabled=True,
        model="fake-model-v1",
        max_proposals_per_round=3,
        max_rounds=2,
        max_retries=0,
        timeout_seconds=5.0,
    )
    defaults.update(overrides)
    return LLMScientistConfig(**defaults)


def make_scientist(responses: Sequence[Any], **overrides: Any):
    provider = FakeProvider(responses)
    return LLMScientist(make_config(**overrides), provider), provider


def make_observation(x: float, y: float, sequence: int) -> Observation:
    return Observation(
        observation_id=f"obs-{sequence}",
        input_id=f"inp-{sequence}",
        input_values={"x": float(x)},
        output_values={"y": float(y)},
        sequence_number=sequence,
    )


def observations(count: int = 3) -> List[Observation]:
    return [make_observation(x, 2.0 * x, index + 1) for index, x in enumerate([-1.5, 0.0, 1.5][:count])]


def run_round(
    scientist: LLMScientist,
    *,
    candidates: Sequence[HypothesisRepresentation] = (),
    evaluations: Optional[Dict[str, Any]] = None,
    ranking: Any = None,
    observation_sequence: Optional[Sequence[Observation]] = None,
):
    return scientist.propose(
        run_id="run-test",
        observations=observation_sequence if observation_sequence is not None else observations(),
        candidates=tuple(candidates),
        evaluations=evaluations or {},
        ranking=ranking,
        domain_bounds=BOUNDS,
        output_variable="y",
        max_polynomial_degree=2,
    )


def canonical_candidate(coefficients: Sequence[float]) -> HypothesisRepresentation:
    degree = len(coefficients) - 1
    return HypothesisRepresentation(
        model_family="polynomial",
        formula="y = candidate",
        parameters={
            "family": "polynomial",
            "degree": degree,
            "coefficient_order": "ascending",
            "coefficients": {f"c{index}": float(value) for index, value in enumerate(coefficients)},
        },
        complexity=degree + 1,
        provenance=HypothesisProvenance(
            origin=HypothesisOrigin.SYSTEM_GENERATED,
            generator="polynomial_candidate_generator",
            generator_version="1.0.0",
        ),
    )


def make_run_config(**overrides: Any) -> DiscoveryRunConfig:
    defaults: Dict[str, Any] = dict(
        max_query_budget=2,
        max_iterations=4,
        domain_bounds=BOUNDS,
        hypothesis_config=HypothesisGenerationConfig(
            min_degree=0, max_degree=2, candidate_count=4, seed=7
        ),
        ranking_policy=RankingPolicyConfig(
            min_samples_for_plausibility=2,
            performance_acceptance_threshold=1e9,
        ),
        experiment_pool_size=5,
    )
    defaults.update(overrides)
    return DiscoveryRunConfig(**defaults)


def run_discovery(scientist: Optional[LLMScientist], *, initials: int = 3, **config_overrides: Any):
    oracle = PolynomialLocalOracle(min_x=-2.0, max_x=2.0)
    engine = DiscoveryEngine(
        oracle,
        make_run_config(**config_overrides),
        scientist=scientist,
        run_id="run-llm-test",
    )
    initial = [make_observation(x, x**2, index + 1) for index, x in enumerate([-1.0, 0.0, 1.0][:initials])]
    return engine, engine.run(initial)


# ==============================================================================
# A. Contracts, parsing, validation
# ==============================================================================


def test_valid_proposal_becomes_supported_hypothesis_contract() -> None:
    scientist, provider = make_scientist([success({"hypotheses": [proposal()]})])
    outcome = run_round(scientist)

    assert outcome.record.status == ScientistRoundStatus.SUCCESS
    assert outcome.record.accepted_count == 1
    assert len(provider.requests) == 1
    hypothesis = outcome.hypotheses[0]
    assert isinstance(hypothesis, HypothesisRepresentation)
    assert hypothesis.model_family == "polynomial"
    assert hypothesis.parameters["degree"] == 2
    assert hypothesis.parameters["coefficients"] == {"c0": 1.5, "c1": -0.25, "c2": 0.5}
    assert hypothesis.provenance is not None
    assert hypothesis.provenance.origin == HypothesisOrigin.SYSTEM_GENERATED
    assert hypothesis.provenance.generator == "llm_scientist"
    metadata = hypothesis.generation_metadata
    assert metadata["proposal_round_id"] == outcome.record.round_id
    assert isinstance(metadata["accepted_at"], str) and metadata["accepted_at"]
    assert metadata["provider_id"] == "fake_provider"
    assert metadata["prompt_version"] == SCIENTIST_PROMPT_VERSION
    assert metadata["schema_version"] == SCIENTIST_SCHEMA_VERSION
    assert metadata["confidence_is_calibrated"] is False
    assert metadata["self_reported_confidence"] == 0.4
    assert metadata["is_live_provider"] is False


def test_missing_required_field_is_rejected_with_diagnostics() -> None:
    bad = proposal()
    del bad["rationale"]
    scientist, _ = make_scientist([success({"hypotheses": [bad]})])
    outcome = run_round(scientist)

    assert outcome.record.status == ScientistRoundStatus.NO_VALID_PROPOSALS
    assert outcome.record.accepted_count == 0
    assert outcome.record.rejected[0].code == ProposalRejectionCode.MISSING_FIELD
    assert outcome.record.fallback_applied is True


def test_malformed_json_is_rejected() -> None:
    scientist, provider = make_scientist([success("{not valid json")])
    outcome = run_round(scientist)

    assert outcome.record.status == ScientistRoundStatus.PROVIDER_FAILURE
    assert outcome.record.outcome_status == ProviderOutcomeStatus.MALFORMED_RESPONSE
    assert outcome.hypotheses == ()
    assert len(provider.requests) == 1


def test_non_finite_coefficients_are_rejected() -> None:
    raw = json.dumps({"hypotheses": [proposal(coefficients=[1.0, float("nan"), 0.5])]})
    scientist, _ = make_scientist([success(raw)])
    outcome = run_round(scientist)

    assert outcome.record.accepted_count == 0
    assert outcome.record.rejected[0].code == ProposalRejectionCode.NON_FINITE_VALUE


def test_invalid_coefficient_count_is_rejected() -> None:
    scientist, _ = make_scientist([success({"hypotheses": [proposal(degree=2, coefficients=[1.0])]})])
    outcome = run_round(scientist)

    assert outcome.record.rejected[0].code == ProposalRejectionCode.UNSUPPORTED_REPRESENTATION


def test_unsupported_model_family_is_rejected() -> None:
    scientist, _ = make_scientist(
        [success({"hypotheses": [proposal(model_family="neural_network")]})]
    )
    outcome = run_round(scientist)

    assert outcome.record.rejected[0].code == ProposalRejectionCode.UNSUPPORTED_MODEL_FAMILY
    assert outcome.hypotheses == ()


def test_out_of_range_degree_is_rejected() -> None:
    scientist, _ = make_scientist(
        [success({"hypotheses": [proposal(degree=3, coefficients=[1.0, 1.0, 1.0, 1.0])]})]
    )
    outcome = run_round(scientist)

    assert outcome.record.rejected[0].code == ProposalRejectionCode.UNSUPPORTED_REPRESENTATION
    assert outcome.record.rejected[0].details["max_degree"] == 2


def test_executable_fields_are_rejected_and_never_executed() -> None:
    malicious = proposal()
    malicious["code"] = "__import__('os').system('echo pwned')"
    scientist, _ = make_scientist([success({"hypotheses": [malicious]})])
    outcome = run_round(scientist)

    assert outcome.record.rejected[0].code == ProposalRejectionCode.UNSAFE_FIELD
    assert outcome.hypotheses == ()


def test_unknown_proposal_fields_are_rejected() -> None:
    scientist, _ = make_scientist(
        [success({"hypotheses": [proposal(oracle_answer="y = 2x")]})]
    )
    outcome = run_round(scientist)

    assert outcome.record.rejected[0].code == ProposalRejectionCode.SCHEMA_INVALID


def test_out_of_domain_suggested_input_is_rejected() -> None:
    scientist, _ = make_scientist(
        [success({"hypotheses": [proposal(suggested_experiment_inputs=[42.0])]})]
    )
    outcome = run_round(scientist)

    assert outcome.record.accepted_count == 0
    assert outcome.record.rejected[0].code == ProposalRejectionCode.OUT_OF_DOMAIN


def test_too_many_proposals_rejects_the_whole_round() -> None:
    scientist, provider = make_scientist(
        [success({"hypotheses": [proposal(), proposal(coefficients=[1.0, 0.0, 1.0]), proposal()]})],
        max_proposals_per_round=2,
    )
    outcome = run_round(scientist)

    assert outcome.record.status == ScientistRoundStatus.NO_VALID_PROPOSALS
    assert outcome.record.rejected[0].code == ProposalRejectionCode.TOO_MANY_PROPOSALS
    assert outcome.hypotheses == ()
    assert len(provider.requests) == 1


def test_oversized_response_is_rejected_before_parsing() -> None:
    huge = success(json.dumps({"hypotheses": [proposal()], "padding": "x" * 5000}))
    scientist, _ = make_scientist([huge], max_response_characters=1000)
    outcome = run_round(scientist)

    assert outcome.record.status == ScientistRoundStatus.PROVIDER_FAILURE
    assert outcome.record.outcome_status == ProviderOutcomeStatus.RESPONSE_TOO_LARGE


def test_duplicate_of_existing_candidate_is_rejected() -> None:
    existing = canonical_candidate([1.5, -0.25, 0.5])
    scientist, _ = make_scientist([success({"hypotheses": [proposal()]})])
    outcome = run_round(scientist, candidates=[existing])

    assert outcome.record.accepted_count == 0
    assert outcome.record.rejected[0].code == ProposalRejectionCode.DUPLICATE_PROPOSAL


def test_duplicate_within_one_round_is_rejected() -> None:
    scientist, _ = make_scientist(
        [success({"hypotheses": [proposal(), proposal(rationale="same model again")]})]
    )
    outcome = run_round(scientist)

    assert outcome.record.accepted_count == 1
    assert outcome.record.rejected_count == 1
    assert outcome.record.rejected[0].code == ProposalRejectionCode.DUPLICATE_PROPOSAL


def test_empty_hypothesis_list_is_a_structured_fallback_round() -> None:
    scientist, _ = make_scientist([success({"hypotheses": []})])
    outcome = run_round(scientist)

    assert outcome.record.status == ScientistRoundStatus.NO_VALID_PROPOSALS
    assert outcome.record.fallback_applied is True
    assert outcome.record.fallback_reason == "no_valid_hypotheses"
    assert outcome.record.accepted_count == 0


def test_round_record_preserves_credential_slot() -> None:
    scientist, _ = make_scientist(
        [success({"hypotheses": [proposal()]}, credential_slot=2)]
    )
    outcome = run_round(scientist)
    assert outcome.record.credential_slot == 2
    assert outcome.record.to_dict()["credential_slot"] == 2


def test_round_record_preserves_usage_only_when_reported() -> None:
    scientist, _ = make_scientist(
        [success({"hypotheses": [proposal()]}, usage={"prompt_tokens": 12, "completion_tokens": 34})]
    )
    outcome = run_round(scientist)
    assert outcome.record.usage == {"prompt_tokens": 12, "completion_tokens": 34}

    no_usage, _ = make_scientist([success({"hypotheses": [proposal(coefficients=[2.0, 0.5, 1.0])]})])
    outcome = run_round(no_usage)
    assert outcome.record.usage is None


# ==============================================================================
# B. Provider behavior and secret hygiene
# ==============================================================================


def test_disabled_config_never_calls_the_provider() -> None:
    provider = FakeProvider([success({"hypotheses": [proposal()]})])
    scientist = LLMScientist(make_config(enabled=False), provider)
    outcome = run_round(scientist)

    assert outcome.record.status == ScientistRoundStatus.DISABLED
    assert provider.requests == []
    assert outcome.hypotheses == ()


def test_missing_provider_is_reported_as_not_configured() -> None:
    scientist = LLMScientist(make_config(), None)
    outcome = run_round(scientist)

    assert outcome.record.status == ScientistRoundStatus.PROVIDER_FAILURE
    assert outcome.record.outcome_status == ProviderOutcomeStatus.NOT_CONFIGURED
    assert outcome.record.error.code == "LLM_PROVIDER_NOT_CONFIGURED"
    assert outcome.record.fallback_applied is True


def test_provider_failure_is_structured_and_secret_free() -> None:
    scientist, _ = make_scientist(
        [provider_error(ProviderOutcomeStatus.MISSING_CREDENTIALS, "no key present")]
    )
    outcome = run_round(scientist)

    assert outcome.record.status == ScientistRoundStatus.PROVIDER_FAILURE
    assert outcome.record.outcome_status == ProviderOutcomeStatus.MISSING_CREDENTIALS
    assert outcome.record.fallback_applied is True
    serialized = json.dumps(outcome.record.to_dict())
    assert FAKE_PROVIDER_SECRET not in serialized


def test_provider_exception_is_converted_and_secret_free() -> None:
    scientist, _ = make_scientist([RuntimeError(f"boom key={FAKE_PROVIDER_SECRET}")])
    outcome = run_round(scientist)

    assert outcome.record.status == ScientistRoundStatus.PROVIDER_FAILURE
    assert outcome.record.outcome_status == ProviderOutcomeStatus.PROVIDER_ERROR
    serialized = json.dumps(outcome.record.to_dict())
    assert FAKE_PROVIDER_SECRET not in serialized
    assert "boom" not in serialized


def test_rounds_are_bounded_by_configuration() -> None:
    provider = FakeProvider(
        [
            success({"hypotheses": [proposal(coefficients=[1.0, 0.5, 0.25])]}),
            success({"hypotheses": [proposal(coefficients=[2.0, 0.5, 1.0])]}),
        ]
    )
    scientist = LLMScientist(make_config(max_rounds=1), provider)

    first = run_round(scientist)
    second = run_round(scientist)

    assert first.record.status == ScientistRoundStatus.SUCCESS
    assert second.record.status == ScientistRoundStatus.DISABLED
    assert len(provider.requests) == 1
    assert scientist.can_propose is False


def test_disable_further_rounds_policy_stops_after_failure() -> None:
    provider = FakeProvider(
        [
            provider_error(ProviderOutcomeStatus.PROVIDER_ERROR),
            success({"hypotheses": [proposal()]}),
        ]
    )
    scientist = LLMScientist(
        make_config(fallback_policy=ScientistFallbackPolicy.DISABLE_FURTHER_ROUNDS),
        provider,
    )

    first = run_round(scientist)
    second = run_round(scientist)

    assert first.record.status == ScientistRoundStatus.PROVIDER_FAILURE
    assert second.record.status == ScientistRoundStatus.DISABLED
    assert len(provider.requests) == 1
    assert scientist.can_propose is False


def test_config_from_environment_defaults_to_disabled() -> None:
    config = LLMScientistConfig.from_environment({})
    assert config.enabled is False
    assert config.provider == "groq"
    assert config.credential_env_var == "GROQ_API_KEY"
    assert config.credential_env_vars == (
        "GROQ_API_KEY",
        "GROQ_API_KEY_2",
        "GROQ_API_KEY_3",
        "GROQ_API_KEY_4",
        "GROQ_API_KEY_5",
    )


def test_config_parses_plural_and_legacy_credential_slots() -> None:
    plural = LLMScientistConfig.from_environment(
        {"LLM_SCIENTIST_CREDENTIAL_ENV_VARS": "KEY_A, KEY_B ,KEY_C"}
    )
    assert plural.credential_env_vars == ("KEY_A", "KEY_B", "KEY_C")
    assert plural.credential_env_var == "KEY_A"

    legacy = LLMScientistConfig.from_environment(
        {"LLM_SCIENTIST_CREDENTIAL_ENV_VAR": "ONLY_KEY"}
    )
    assert legacy.credential_env_vars == ("ONLY_KEY",)

    with pytest.raises(ValueError):
        LLMScientistConfig(credential_env_vars=())


def test_config_from_environment_parses_explicit_values() -> None:
    config = LLMScientistConfig.from_environment(
        {
            "LLM_SCIENTIST_ENABLED": "true",
            "LLM_SCIENTIST_PROVIDER": "groq",
            "LLM_SCIENTIST_MODEL": "model-x",
            "LLM_SCIENTIST_MAX_PROPOSALS": "5",
            "LLM_SCIENTIST_MAX_ROUNDS": "4",
            "LLM_SCIENTIST_TIMEOUT_SECONDS": "9.5",
            "LLM_SCIENTIST_FALLBACK_POLICY": "disable_further_rounds",
        }
    )
    assert config.enabled is True
    assert config.model == "model-x"
    assert config.max_proposals_per_round == 5
    assert config.max_rounds == 4
    assert config.timeout_seconds == 9.5
    assert config.fallback_policy == ScientistFallbackPolicy.DISABLE_FURTHER_ROUNDS


@pytest.mark.parametrize(
    "environ",
    [
        {"LLM_SCIENTIST_ENABLED": "maybe"},
        {"LLM_SCIENTIST_MAX_ROUNDS": "0"},
        {"LLM_SCIENTIST_MAX_PROPOSALS": "999"},
        {"LLM_SCIENTIST_FALLBACK_POLICY": "unknown"},
    ],
)
def test_invalid_environment_configuration_fails_loudly(environ: Dict[str, str]) -> None:
    with pytest.raises(ValueError):
        LLMScientistConfig.from_environment(environ)


def test_factory_returns_none_when_disabled() -> None:
    assert build_llm_scientist_from_environment(environ={}) is None
    scientist = build_llm_scientist_from_environment(
        environ={"LLM_SCIENTIST_ENABLED": "true", "LLM_SCIENTIST_MODEL": "model-x"}
    )
    assert scientist is not None
    assert scientist.config.model == "model-x"


def test_factory_rejects_unknown_provider() -> None:
    with pytest.raises(ValueError):
        build_llm_scientist_from_environment(
            environ={"LLM_SCIENTIST_ENABLED": "true", "LLM_SCIENTIST_PROVIDER": "mystery"}
        )


def test_groq_adapter_reports_missing_credentials_without_network() -> None:
    provider = GroqProposalProvider(make_config(), environ={})
    result = provider.propose(
        run_round_request()
    )
    assert result.status == ProviderOutcomeStatus.MISSING_CREDENTIALS
    assert result.attempts == 0
    assert "GROQ_API_KEY" in (result.error.details or {}).get("credential_env_var", "")


def run_round_request():
    """Build a minimal request without invoking a model."""
    scientist = LLMScientist(make_config(enabled=False))
    return scientist.build_request(
        run_id="run-test",
        observations=observations(),
        candidates=(),
        evaluations={},
        ranking=None,
        domain_bounds=BOUNDS,
        output_variable="y",
        max_polynomial_degree=2,
    )


class _FakeCompletions:
    def __init__(self, outcomes: Sequence[Any]) -> None:
        self.outcomes = list(outcomes)
        self.calls: List[Dict[str, Any]] = []

    def create(self, **kwargs: Any):
        self.calls.append(kwargs)
        if not self.outcomes:
            raise AssertionError("unexpected provider call")
        item = self.outcomes.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class _FakeGroqClient:
    def __init__(self, outcomes: Sequence[Any]) -> None:
        self.completions = _FakeCompletions(outcomes)
        self.chat = SimpleNamespace(completions=self.completions)


class AuthenticationFailure(Exception):
    status_code = 401


class PermissionDeniedError(Exception):
    status_code = 403


class RateLimitFailure(Exception):
    status_code = 429


class TimeoutFailure(Exception):
    pass


class ServerFailure(Exception):
    status_code = 500


def _completion(content: str, *, usage: Any = None, model: str = "groq-model"):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        usage=usage,
        model=model,
    )


def _groq_provider(outcomes: Sequence[Any], **overrides: Any):
    sleeps: List[float] = []
    config = make_config(**overrides)
    client = _FakeGroqClient(outcomes)
    provider = GroqProposalProvider(config, client=client, sleep=sleeps.append)
    return provider, client, sleeps


def test_groq_adapter_success_extracts_content_and_real_usage() -> None:
    usage = SimpleNamespace(prompt_tokens=10, completion_tokens=20, total_tokens=30)
    provider, client, _ = _groq_provider([_completion('{"hypotheses": []}', usage=usage)])
    result = provider.propose(run_round_request())

    assert result.status == ProviderOutcomeStatus.SUCCESS
    assert result.content == '{"hypotheses": []}'
    assert result.usage == {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}
    assert result.model == "groq-model"
    assert result.is_live is True
    call = client.completions.calls[0]
    assert call["model"] == "fake-model-v1"
    assert call["response_format"] == {"type": "json_object"}
    assert call["messages"][0]["role"] == "system"


def test_groq_adapter_does_not_invent_usage() -> None:
    provider, _, _ = _groq_provider([_completion('{"hypotheses": []}', usage=None)])
    result = provider.propose(run_round_request())
    assert result.status == ProviderOutcomeStatus.SUCCESS
    assert result.usage is None


def test_groq_adapter_auth_failure_is_not_retried() -> None:
    provider, client, sleeps = _groq_provider(
        [AuthenticationFailure(f"bad key {FAKE_PROVIDER_SECRET}")], max_retries=2
    )
    result = provider.propose(run_round_request())

    assert result.status == ProviderOutcomeStatus.AUTHENTICATION_FAILED
    assert result.attempts == 1
    assert len(client.completions.calls) == 1
    assert sleeps == []
    serialized = json.dumps(
        {
            "error": result.error.to_dict(),
            "status": result.status.value,
            "model": result.model,
        }
    )
    assert FAKE_PROVIDER_SECRET not in serialized


def test_groq_adapter_permission_failure_is_not_retried() -> None:
    provider, client, _ = _groq_provider([PermissionDeniedError()], max_retries=2)
    result = provider.propose(run_round_request())
    assert result.status == ProviderOutcomeStatus.PERMISSION_DENIED
    assert len(client.completions.calls) == 1


def test_groq_adapter_retries_transient_failure_within_bound() -> None:
    provider, client, sleeps = _groq_provider(
        [RateLimitFailure(), _completion('{"hypotheses": []}')], max_retries=1
    )
    result = provider.propose(run_round_request())

    assert result.status == ProviderOutcomeStatus.SUCCESS
    assert result.attempts == 2
    assert len(client.completions.calls) == 2
    assert len(sleeps) == 1


def test_groq_adapter_stops_after_bounded_retries() -> None:
    provider, client, sleeps = _groq_provider(
        [RateLimitFailure(), RateLimitFailure(), RateLimitFailure()], max_retries=2
    )
    result = provider.propose(run_round_request())

    assert result.status == ProviderOutcomeStatus.RATE_LIMITED
    assert result.attempts == 3
    assert len(client.completions.calls) == 3
    assert len(sleeps) == 2


def test_groq_adapter_classifies_timeout_and_server_errors() -> None:
    provider, _, _ = _groq_provider([TimeoutFailure()], max_retries=0)
    assert provider.propose(run_round_request()).status == ProviderOutcomeStatus.TIMEOUT

    provider, _, _ = _groq_provider([ServerFailure()], max_retries=0)
    assert provider.propose(run_round_request()).status == ProviderOutcomeStatus.PROVIDER_ERROR


def test_groq_adapter_rejects_oversized_response_without_retry() -> None:
    provider, client, _ = _groq_provider(
        [_completion("x" * 5000)], max_retries=2, max_response_characters=100
    )
    result = provider.propose(run_round_request())

    assert result.status == ProviderOutcomeStatus.RESPONSE_TOO_LARGE
    assert result.attempts == 1
    assert len(client.completions.calls) == 1


def test_groq_adapter_rejects_empty_content() -> None:
    provider, _, _ = _groq_provider([_completion("   ")])
    result = provider.propose(run_round_request())
    assert result.status == ProviderOutcomeStatus.MALFORMED_RESPONSE


def _rotating_provider(scripts: Dict[str, Sequence[Any]], **overrides: Any):
    """One fake client per credential slot, keyed by environment variable name."""
    keys = tuple(scripts)
    config = make_config(credential_env_vars=keys, **overrides)
    environ = {name: f"credential-for-{name}" for name in keys}
    clients = {
        f"credential-for-{name}": _FakeGroqClient(outcomes)
        for name, outcomes in scripts.items()
    }
    sleeps: List[float] = []
    provider = GroqProposalProvider(
        config,
        client_factory=lambda credential: clients[credential],
        sleep=sleeps.append,
        environ=environ,
    )
    return provider, clients, sleeps


def test_groq_rotates_to_next_slot_on_rate_limit() -> None:
    provider, clients, _ = _rotating_provider(
        {
            "KEY_A": [RateLimitFailure()],
            "KEY_B": [_completion('{"hypotheses": []}')],
        }
    )
    result = provider.propose(run_round_request())

    assert result.status == ProviderOutcomeStatus.SUCCESS
    assert result.attempts == 2
    assert result.credential_slot == 2
    assert len(clients["credential-for-KEY_A"].completions.calls) == 1
    assert len(clients["credential-for-KEY_B"].completions.calls) == 1


def test_groq_reports_rate_limited_when_all_slots_are_exhausted() -> None:
    provider, clients, _ = _rotating_provider(
        {"KEY_A": [RateLimitFailure()], "KEY_B": [RateLimitFailure()]}
    )
    result = provider.propose(run_round_request())

    assert result.status == ProviderOutcomeStatus.RATE_LIMITED
    assert result.attempts == 2
    assert result.credential_slot == 2
    details = result.error.details or {}
    assert details["credential_env_vars"] == ["KEY_A", "KEY_B"]
    serialized = json.dumps({"details": details, "status": result.status.value})
    assert "credential-for-" not in serialized


def test_groq_does_not_rotate_on_authentication_failure() -> None:
    provider, clients, _ = _rotating_provider(
        {
            "KEY_A": [AuthenticationFailure()],
            "KEY_B": [_completion('{"hypotheses": []}')],
        }
    )
    result = provider.propose(run_round_request())

    assert result.status == ProviderOutcomeStatus.AUTHENTICATION_FAILED
    assert result.attempts == 1
    assert result.credential_slot == 1
    assert len(clients["credential-for-KEY_B"].completions.calls) == 0


def test_groq_rotation_advances_start_slot_after_success() -> None:
    provider, clients, _ = _rotating_provider(
        {
            "KEY_A": [_completion('{"hypotheses": []}')],
            "KEY_B": [_completion('{"hypotheses": []}')],
        }
    )
    first = provider.propose(run_round_request())
    second = provider.propose(run_round_request())

    assert (first.credential_slot, second.credential_slot) == (1, 2)
    assert len(clients["credential-for-KEY_A"].completions.calls) == 1
    assert len(clients["credential-for-KEY_B"].completions.calls) == 1


def test_groq_rotation_skips_absent_credential_slots() -> None:
    config = make_config(credential_env_vars=("KEY_A", "KEY_B"))
    clients = {"credential-for-KEY_B": _FakeGroqClient([_completion('{"hypotheses": []}')])}
    provider = GroqProposalProvider(
        config,
        client_factory=lambda credential: clients[credential],
        environ={"KEY_B": "credential-for-KEY_B"},
    )
    assert provider.credential_slots == 1

    result = provider.propose(run_round_request())
    assert result.status == ProviderOutcomeStatus.SUCCESS
    # KEY_A is absent, so the configured second slot is reported.
    assert result.credential_slot == 2


def test_groq_transient_retry_stays_on_the_same_slot() -> None:
    provider, clients, sleeps = _rotating_provider(
        {
            "KEY_A": [TimeoutFailure(), _completion('{"hypotheses": []}')],
            "KEY_B": [_completion('{"hypotheses": []}')],
        },
        max_retries=1,
    )
    result = provider.propose(run_round_request())

    assert result.status == ProviderOutcomeStatus.SUCCESS
    assert result.attempts == 2
    assert result.credential_slot == 1
    assert len(sleeps) == 1
    assert len(clients["credential-for-KEY_B"].completions.calls) == 0


def test_groq_missing_credentials_lists_all_slots_without_values() -> None:
    config = make_config(credential_env_vars=("KEY_A", "KEY_B"))
    provider = GroqProposalProvider(config, environ={"KEY_A": "   "})
    result = provider.propose(run_round_request())

    assert result.status == ProviderOutcomeStatus.MISSING_CREDENTIALS
    details = result.error.details or {}
    assert details["credential_env_vars"] == ["KEY_A", "KEY_B"]
    serialized = json.dumps(details)
    assert "credential-for-" not in serialized


# ==============================================================================
# C. Discovery integration
# ==============================================================================


def test_deterministic_run_has_no_scientist_history() -> None:
    engine, result = run_discovery(None)

    assert result.scientist_history == ()
    assert "scientist_history" in result.to_dict()
    assert result.to_dict()["scientist_history"] == []
    assert result.query_accounting.attempted_queries >= 1


def test_llm_assisted_proposals_enter_fitting_evaluation_and_ranking() -> None:
    scientist, provider = make_scientist(
        [success({"hypotheses": [proposal(coefficients=[3.5, -1.25, 0.75])]})],
        max_rounds=1,
    )
    engine, result = run_discovery(scientist, max_query_budget=1, max_iterations=2)

    record = result.scientist_history[0]
    assert record.status == ScientistRoundStatus.SUCCESS
    assert record.accepted_count == 1
    assert len(provider.requests) == 1

    accepted_id = record.accepted_hypothesis_ids[0]
    hypotheses = {hypothesis.hypothesis_id: hypothesis for hypothesis in result.hypotheses}
    assert accepted_id in hypotheses
    assert hypotheses[accepted_id].parameters["coefficients"]["c0"] == 3.5
    assert hypotheses[accepted_id].provenance.generator == "llm_scientist"

    evaluation = result.evaluation_by_hypothesis().get(accepted_id)
    assert evaluation is not None
    assert evaluation.status == EvaluationStatus.COMPLETED
    assert evaluation.metrics.get("mae") is not None

    assert result.ranking is not None
    assert accepted_id in {ranked.hypothesis_id for ranked in result.ranking.ranked_candidates}


def test_invalid_proposals_never_enter_the_pipeline() -> None:
    scientist, _ = make_scientist(
        [success({"hypotheses": [proposal(model_family="neural_network")]})]
    )
    engine, result = run_discovery(scientist, max_query_budget=1, max_iterations=2)

    assert result.scientist_history[0].accepted_count == 0
    assert all(
        hypothesis.provenance.generator != "llm_scientist" for hypothesis in result.hypotheses
    )
    assert any(warning.code == WARNING_SCIENTIST_FALLBACK for warning in result.warnings)
    assert result.status != DiscoveryLifecycleStatus.FAILED


def test_later_round_receives_newly_committed_observations() -> None:
    scientist, provider = make_scientist(
        [
            success({"hypotheses": [proposal(coefficients=[1.0, 0.5, 0.25])]}),
            success({"hypotheses": [proposal(coefficients=[2.0, 0.5, 1.0])]}),
        ],
        max_rounds=2,
    )
    engine, result = run_discovery(scientist, max_query_budget=1, max_iterations=2)

    assert len(provider.requests) == 2
    first, second = provider.requests
    assert len(first.observations) == 3
    assert len(second.observations) == 4
    assert second.previous_rounds and second.previous_rounds[-1]["status"] == "success"
    assert [record.round_index for record in result.scientist_history] == [1, 2]


def test_provider_failure_uses_explicit_deterministic_fallback() -> None:
    scientist, _ = make_scientist(
        [provider_error(ProviderOutcomeStatus.MISSING_CREDENTIALS)],
        max_rounds=1,
    )
    engine, result = run_discovery(scientist, max_query_budget=1, max_iterations=2)

    record = result.scientist_history[0]
    assert record.status == ScientistRoundStatus.PROVIDER_FAILURE
    assert record.fallback_applied is True
    assert result.query_accounting.successful_queries >= 1
    assert result.status != DiscoveryLifecycleStatus.FAILED
    warning = next(w for w in result.warnings if w.code == WARNING_SCIENTIST_FALLBACK)
    assert warning.details["outcome_status"] == "missing_credentials"


def test_scientist_exception_does_not_crash_discovery() -> None:
    class ExplodingScientist:
        can_propose = True

        def propose(self, **kwargs):
            raise RuntimeError("unexpected scientist failure")

    engine, result = run_discovery(ExplodingScientist(), max_query_budget=1, max_iterations=2)

    assert result.status != DiscoveryLifecycleStatus.FAILED
    assert any(warning.code == WARNING_SCIENTIST_ROUND_FAILED for warning in result.warnings)
    assert result.scientist_history == ()


def test_suggested_experiment_goes_through_generator_and_budget() -> None:
    scientist, _ = make_scientist(
        [
            success(
                {
                    "hypotheses": [
                        proposal(
                            coefficients=[1.0, 0.5, 0.25],
                            suggested_experiment_inputs=[1.25],
                        )
                    ]
                }
            )
        ],
        max_rounds=1,
    )
    engine, result = run_discovery(scientist, max_query_budget=1, max_iterations=1)

    first_experiment = result.experiment_history[0]
    assert first_experiment.input_values == {"x": 1.25}
    assert first_experiment.strategy == "suggested"
    assert first_experiment.outcome == ExperimentExecutionOutcome.INVOKED_SUCCESS
    assert result.query_accounting.attempted_queries == 1


def test_zero_budget_run_never_calls_the_provider() -> None:
    scientist, provider = make_scientist(
        [
            success(
                {
                    "hypotheses": [
                        proposal(coefficients=[1.0, 0.5, 0.25], suggested_experiment_inputs=[1.25])
                    ]
                }
            )
        ],
        max_rounds=1,
    )
    engine, result = run_discovery(scientist, max_query_budget=0, max_iterations=2)

    assert provider.requests == []
    assert result.scientist_history == ()
    assert result.query_accounting.attempted_queries == 0
    assert result.experiment_history == ()


def test_provider_output_cannot_mutate_committed_state() -> None:
    # A proposal trying to overwrite observations/rankings is rejected as an
    # unknown/unsafe field; committed evidence and counters stay untouched.
    malicious = proposal()
    malicious["observations"] = [{"input_values": {"x": 9.9}, "output_values": {"y": 0.0}}]
    malicious["ranking"] = {"ranked_candidates": []}
    scientist, _ = make_scientist([success({"hypotheses": [malicious]})], max_rounds=1)
    engine, result = run_discovery(scientist, max_query_budget=1, max_iterations=1)

    committed_x = [obs.input_values["x"] for obs in result.observations]
    assert committed_x[:3] == [-1.0, 0.0, 1.0]
    assert 9.9 not in committed_x
    assert all(
        obs.output_values["y"] == obs.input_values["x"] ** 2
        for obs in result.observations[:3]
    )
    assert result.scientist_history[0].accepted_count == 0
    assert (
        sum(
            1
            for hypothesis in result.hypotheses
            if hypothesis.provenance is not None
            and hypothesis.provenance.generator == "llm_scientist"
        )
        == 0
    )
    assert result.query_accounting.attempted_queries == 1


def test_enabled_factory_without_credentials_falls_back_visibly() -> None:
    scientist = build_llm_scientist_from_environment(
        environ={"LLM_SCIENTIST_ENABLED": "true", "LLM_SCIENTIST_MAX_ROUNDS": "1"}
    )
    assert scientist is not None
    engine, result = run_discovery(scientist, max_query_budget=1, max_iterations=2)

    record = result.scientist_history[0]
    assert record.provider_id == "groq"
    assert record.outcome_status == ProviderOutcomeStatus.MISSING_CREDENTIALS
    assert record.fallback_applied is True
    assert result.query_accounting.successful_queries >= 1
    assert result.status != DiscoveryLifecycleStatus.FAILED
    assert any(warning.code == WARNING_SCIENTIST_FALLBACK for warning in result.warnings)


def test_fixed_fake_responses_are_repeatable() -> None:
    script = {"hypotheses": [proposal(coefficients=[1.0, 0.5, 0.25])]}

    def run_once():
        scientist, provider = make_scientist([success(script)], max_rounds=1)
        _, result = run_discovery(scientist, max_query_budget=1, max_iterations=2)
        accepted = result.scientist_history[0].accepted_hypothesis_ids[0]
        hypothesis = next(h for h in result.hypotheses if h.hypothesis_id == accepted)
        return hypothesis.formula, [record.input_values for record in result.experiment_history]

    first_formula, first_inputs = run_once()
    second_formula, second_inputs = run_once()
    assert first_formula == second_formula
    assert first_inputs == second_inputs


# ==============================================================================
# D. Evaluator isolation and serialization
# ==============================================================================


def test_no_ground_truth_in_provider_context_or_serialized_result() -> None:
    secret_coefficients = {"w3": 7.0, "w2": -3.0, "w1": 11.0, "w0": -13.0}
    oracle = PolynomialLocalOracle(min_x=-2.0, max_x=2.0, coefficients=secret_coefficients)
    ground_truth = json.dumps(oracle._evaluator_get_ground_truth(), sort_keys=True)

    scientist, provider = make_scientist([success({"hypotheses": [proposal()]})], max_rounds=1)
    engine = DiscoveryEngine(
        oracle, make_run_config(max_query_budget=1, max_iterations=1), scientist=scientist, run_id="run-isolation"
    )
    result = engine.run(observations())

    request_payload = provider.requests[0].to_json()
    serialized_result = json.dumps(result.to_dict(), sort_keys=True)

    assert ground_truth not in request_payload
    assert ground_truth not in serialized_result
    assert "evaluator_only" not in request_payload
    assert "ground_truth" not in request_payload
    assert "secret" not in json.dumps(provider.requests[0].to_dict()).lower()
    # Discovery only ever receives public observations.
    assert all(
        set(observation) == {"sequence_number", "input_values", "output_values"}
        for observation in provider.requests[0].observations
    )


def test_scientist_round_record_serialization_is_discovery_safe() -> None:
    scientist, _ = make_scientist([success({"hypotheses": [proposal()]})])
    outcome = run_round(scientist)
    payload = outcome.record.to_dict()
    assert payload["run_id"] == "run-test"
    assert payload["prompt_version"] == SCIENTIST_PROMPT_VERSION
    assert payload["schema_version"] == SCIENTIST_SCHEMA_VERSION
    assert payload["is_live_provider"] is False
    # Never invent usage or claim a live call for a fake provider.
    assert "usage" not in payload


# ==============================================================================
# E. Generator integration for advisory suggestions
# ==============================================================================


def test_generator_places_valid_suggestions_first_and_rejects_duplicates() -> None:
    generator = CandidateExperimentGenerator()
    batch = generator.generate_batch(
        ExperimentGenerationConfig(
            pool_size=4,
            strategy=ExperimentGenerationStrategy.GRID,
            bounds=BOUNDS,
            suggested_inputs=[1.25, 1.25, 99.0],
        )
    )

    assert batch.candidates[0].input_data.values == {"x": 1.25}
    assert batch.candidates[0].strategy == ExperimentGenerationStrategy.SUGGESTED
    assert sum(1 for candidate in batch.candidates if candidate.input_data.values["x"] == 1.25) == 1
    assert all(candidate.input_data.values["x"] != 99.0 for candidate in batch.candidates)
    assert len(batch.candidates) == 4


def test_generator_rejects_invalid_suggestion_configuration() -> None:
    with pytest.raises(ValueError):
        ExperimentGenerationConfig(suggested_inputs=["1.0"])
    with pytest.raises(ValueError):
        ExperimentGenerationConfig(suggested_inputs=[float("inf")])
    with pytest.raises(ValueError):
        ExperimentGenerationConfig(strategy=ExperimentGenerationStrategy.SUGGESTED)
