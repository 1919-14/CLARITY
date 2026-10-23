"""Serializable evaluation-task contract and worker-side execution (Issue #13).

One payload/execution path is shared by both execution modes:

``local_sequential``
    The orchestrator calls :func:`execute_evaluation_task_payload` in-process.

``nodus_parallel``
    The same function runs inside the deployed Nodus worker
    (``app.nodus_worker.evaluate``) after the payload crossed the remote
    serialization boundary.

Sharing the path is what makes a sequential-versus-parallel comparison
meaningful: both modes fit and evaluate through the Issue #6
``EvaluationPipeline`` and return the same validated result envelope.

Trust boundary
--------------
Payloads contain only discovery-permitted data: the candidate specification and
committed observations. Evaluator-only/ground-truth keys are rejected before
execution (``verify_no_ground_truth_leakage``) and again before submission (the
Nodus adapter's payload validator). Nothing in this module reads the oracle,
the database, or environment credentials.

Result semantics
----------------
The envelope ``status`` describes whether the *task* produced a valid
``EvaluationResult``. A scientific failure (for example an underdetermined fit)
is a valid result: the envelope is ``succeeded`` and
``evaluation["status"] == "failed"``. Callers must never treat a failed
evaluation as a successful candidate.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from app.domain.contracts import (
    EvaluationRequest,
    EvaluationResult,
    FittingStatus,
    HypothesisRepresentation,
    Observation,
    StructuredError,
    EvaluationStatus,
    verify_no_ground_truth_leakage,
)
from app.domain.evaluation import EvaluationPipeline
from app.domain.fitting import FittingConfig

EVALUATION_TASK_SCHEMA_VERSION = "1.0"
OPERATION_EVALUATE_CANDIDATE = "evaluate_candidate"

TASK_RESULT_SUCCEEDED = "succeeded"
TASK_RESULT_FAILED = "failed"

ERROR_INVALID_TASK_PAYLOAD = "NODUS_PARALLEL_INVALID_TASK_PAYLOAD"
ERROR_INVALID_TASK_RESULT = "NODUS_PARALLEL_INVALID_TASK_RESULT"
ERROR_WORKER_EXECUTION_FAILED = "NODUS_PARALLEL_WORKER_EXECUTION_FAILED"

_TASK_PAYLOAD_MESSAGES = {
    ERROR_INVALID_TASK_PAYLOAD: (
        "The evaluation task payload failed validation; no work was performed."
    ),
    ERROR_INVALID_TASK_RESULT: (
        "The evaluation task result failed validation; no scientific score was accepted."
    ),
    ERROR_WORKER_EXECUTION_FAILED: (
        "The evaluation worker failed unexpectedly; no scientific score was produced."
    ),
}


class TaskContractError(ValueError):
    """Structured validation failure for a task payload or task result."""

    def __init__(
        self,
        code: str,
        message: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.code = code
        self.message = message or _TASK_PAYLOAD_MESSAGES.get(
            code, "Evaluation task contract error."
        )
        self.details = details or {}
        super().__init__(self.message)

    def to_safe_dict(self) -> Dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": dict(self.details)}

    def to_structured_error(self) -> StructuredError:
        return StructuredError(
            code=self.code, message=self.message, details=dict(self.details)
        )


def _invalid_payload(message: str, **details: Any) -> TaskContractError:
    return TaskContractError(ERROR_INVALID_TASK_PAYLOAD, message, details or None)


def _invalid_result(message: str, **details: Any) -> TaskContractError:
    return TaskContractError(ERROR_INVALID_TASK_RESULT, message, details or None)


def _require_mapping(value: Any, argument: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _invalid_payload(
            f"{argument} must be a mapping, got {type(value).__name__}.",
            argument=argument,
            received_type=type(value).__name__,
        )
    return value


def _require_string(value: Any, argument: str) -> str:
    if not isinstance(value, str) or not value:
        raise _invalid_payload(
            f"{argument} must be a non-empty string.",
            argument=argument,
            received_type=type(value).__name__,
        )
    return value


def _require_string_list(value: Any, argument: str, *, allow_empty: bool) -> List[str]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise _invalid_payload(
            f"{argument} must be a list of strings.",
            argument=argument,
            received_type=type(value).__name__,
        )
    items = list(value)
    if not items and not allow_empty:
        raise _invalid_payload(f"{argument} cannot be empty.", argument=argument)
    for index, item in enumerate(items):
        if not isinstance(item, str) or not item:
            raise _invalid_payload(
                f"{argument}[{index}] must be a non-empty string.",
                argument=argument,
                index=index,
            )
    if len(set(items)) != len(items):
        duplicates = sorted({item for item in items if items.count(item) > 1})
        raise _invalid_payload(
            f"{argument} contains duplicate identifiers.",
            argument=argument,
            duplicates=duplicates,
        )
    return items


def _strict_json_dump(value: Any, *, label: str, error_factory) -> None:
    """Verify a value is strict JSON (no NaN/Infinity, no arbitrary objects)."""
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError, OverflowError) as exc:
        raise error_factory(
            f"{label} is not strict JSON-serializable: {type(exc).__name__}.",
            label=label,
            exception_type=type(exc).__name__,
        ) from exc


# ---------------------------------------------------------------------------
# Payload construction (orchestrator side)
# ---------------------------------------------------------------------------


def build_evaluation_task_payload(
    request: EvaluationRequest,
    observations: Mapping[str, Observation],
    *,
    dataset_id: Optional[str] = None,
    fitting_config: Optional[FittingConfig] = None,
) -> Dict[str, Any]:
    """Serialize one evaluation request into a validated, remote-safe payload.

    The payload carries the candidate specification and the observations
    referenced by the request. Missing observation IDs are *not* filled in: the
    evaluation pipeline counts them as exclusions, exactly as it does locally.
    """
    if not isinstance(request, EvaluationRequest):
        raise _invalid_payload(
            "request must be an EvaluationRequest.",
            received_type=type(request).__name__,
        )
    if not isinstance(observations, Mapping):
        raise _invalid_payload(
            "observations must be a mapping of observation_id to Observation.",
            received_type=type(observations).__name__,
        )
    config = fitting_config if fitting_config is not None else FittingConfig()
    config_error = config.validate()
    if config_error is not None:
        raise _invalid_payload(
            "fitting_config is invalid.",
            cause=config_error.to_dict(),
        )

    training_ids = list(request.observation_ids)
    held_out_ids = list(request.held_out_observation_ids or [])
    overlap = sorted(set(training_ids).intersection(held_out_ids))
    if overlap:
        raise _invalid_payload(
            "Training and held-out observation IDs must be disjoint.",
            overlap=overlap,
        )

    serialized_observations: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for observation_id in training_ids + held_out_ids:
        if observation_id in seen:
            continue
        seen.add(observation_id)
        observation = observations.get(observation_id)
        if observation is None:
            continue
        if not isinstance(observation, Observation):
            raise _invalid_payload(
                "observations must contain Observation instances.",
                observation_id=observation_id,
                received_type=type(observation).__name__,
            )
        serialized_observations.append(observation.to_dict())

    payload: Dict[str, Any] = {
        "schema_version": EVALUATION_TASK_SCHEMA_VERSION,
        "request_id": request.request_id,
        "hypothesis_id": request.hypothesis.hypothesis_id,
        "candidate": request.hypothesis.to_dict(),
        "observations": serialized_observations,
        "observation_ids": training_ids,
        "held_out_observation_ids": held_out_ids,
        "metric_names": list(request.metric_names),
        "fitting_config": config.to_dict(),
        "dataset_id": dataset_id,
    }

    try:
        verify_no_ground_truth_leakage(payload)
    except ValueError as exc:
        raise _invalid_payload(
            "Payload contains evaluator-only or ground-truth keys; refusing to serialize.",
            rejection=type(exc).__name__,
        ) from exc
    _strict_json_dump(payload, label="task payload", error_factory=_invalid_payload)
    return payload


# ---------------------------------------------------------------------------
# Payload validation (worker side)
# ---------------------------------------------------------------------------


def validate_evaluation_task_payload(payload: Any) -> None:
    """Validate a payload received from the orchestration layer or provider."""
    data = _require_mapping(payload, "payload")

    schema_version = data.get("schema_version")
    if schema_version != EVALUATION_TASK_SCHEMA_VERSION:
        raise _invalid_payload(
            "Unsupported evaluation task schema version.",
            expected=EVALUATION_TASK_SCHEMA_VERSION,
            received=schema_version,
        )

    hypothesis_id = _require_string(data.get("hypothesis_id"), "hypothesis_id")
    candidate = _require_mapping(data.get("candidate"), "candidate")
    if candidate.get("hypothesis_id") != hypothesis_id:
        raise _invalid_payload(
            "candidate.hypothesis_id does not match hypothesis_id.",
            hypothesis_id=hypothesis_id,
            candidate_hypothesis_id=candidate.get("hypothesis_id"),
        )

    _require_string(data.get("request_id"), "request_id")
    _require_string_list(data.get("observation_ids"), "observation_ids", allow_empty=True)
    _require_string_list(
        data.get("held_out_observation_ids", []),
        "held_out_observation_ids",
        allow_empty=True,
    )
    _require_string_list(data.get("metric_names"), "metric_names", allow_empty=False)

    fitting = _require_mapping(data.get("fitting_config"), "fitting_config")
    _require_string(fitting.get("method"), "fitting_config.method")
    rank_tolerance = fitting.get("rank_tolerance")
    if (
        isinstance(rank_tolerance, bool)
        or not isinstance(rank_tolerance, (int, float))
        or not math.isfinite(float(rank_tolerance))
        or float(rank_tolerance) <= 0.0
    ):
        raise _invalid_payload(
            "fitting_config.rank_tolerance must be a finite number greater than 0.",
            received=rank_tolerance,
        )

    observations = data.get("observations", [])
    if isinstance(observations, (str, bytes)) or not isinstance(observations, Sequence):
        raise _invalid_payload(
            "observations must be a list.",
            received_type=type(observations).__name__,
        )
    seen_ids: set[str] = set()
    for index, raw in enumerate(observations):
        raw_mapping = _require_mapping(raw, f"observations[{index}]")
        observation_id = _require_string(
            raw_mapping.get("observation_id"), f"observations[{index}].observation_id"
        )
        if observation_id in seen_ids:
            raise _invalid_payload(
                "observations contains duplicate observation_id values.",
                observation_id=observation_id,
            )
        seen_ids.add(observation_id)

    dataset_id = data.get("dataset_id")
    if dataset_id is not None and (not isinstance(dataset_id, str) or not dataset_id):
        raise _invalid_payload(
            "dataset_id must be a non-empty string or null.",
            received_type=type(dataset_id).__name__,
        )

    # Defence in depth: the orchestrator checks this too, but a payload can be
    # crafted or corrupted between submission and execution.
    try:
        verify_no_ground_truth_leakage(data)
    except ValueError as exc:
        raise _invalid_payload(
            "Payload contains evaluator-only or ground-truth keys.",
            rejection=type(exc).__name__,
        ) from exc


# ---------------------------------------------------------------------------
# Worker execution
# ---------------------------------------------------------------------------


def _failed_envelope(
    hypothesis_id: str,
    request_id: str,
    error: StructuredError,
) -> Dict[str, Any]:
    return {
        "schema_version": EVALUATION_TASK_SCHEMA_VERSION,
        "hypothesis_id": hypothesis_id,
        "request_id": request_id,
        "status": TASK_RESULT_FAILED,
        "evaluation": None,
        "error": error.to_dict(),
    }


def execute_evaluation_task_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Validate and execute one evaluation task; always returns an envelope.

    The envelope is ``succeeded`` when a validated ``EvaluationResult`` was
    produced, even if that result records a failed *fit*. It is ``failed`` when
    the payload was invalid or the worker raised; in that case no evaluation
    data is present, so a failure can never be mistaken for a score.
    """
    hypothesis_id = ""
    request_id = ""
    if isinstance(payload, Mapping):
        raw_hypothesis_id = payload.get("hypothesis_id")
        if isinstance(raw_hypothesis_id, str):
            hypothesis_id = raw_hypothesis_id
        raw_request_id = payload.get("request_id")
        if isinstance(raw_request_id, str):
            request_id = raw_request_id

    try:
        validate_evaluation_task_payload(payload)
        candidate = HypothesisRepresentation.from_dict(dict(payload["candidate"]))
        observations: Dict[str, Observation] = {}
        for raw in payload.get("observations", []):
            observation = Observation.from_dict(dict(raw))
            observations[observation.observation_id] = observation

        request = EvaluationRequest(
            hypothesis=candidate,
            observation_ids=list(payload["observation_ids"]),
            held_out_observation_ids=list(payload.get("held_out_observation_ids", [])),
            request_id=payload["request_id"],
            metric_names=list(payload["metric_names"]),
        )
        fitting_config = FittingConfig(
            method=payload["fitting_config"]["method"],
            rank_tolerance=float(payload["fitting_config"]["rank_tolerance"]),
        )
        pipeline = EvaluationPipeline(fitting_config=fitting_config)
        result = pipeline.evaluate(
            request, observations, dataset_id=payload.get("dataset_id")
        )
        evaluation = result.to_dict()
    except TaskContractError as exc:
        return _failed_envelope(hypothesis_id, request_id, exc.to_structured_error())
    except Exception as exc:  # noqa: BLE001 - remote boundary must never leak details
        return _failed_envelope(
            hypothesis_id,
            request_id,
            StructuredError(
                code=ERROR_WORKER_EXECUTION_FAILED,
                message=_TASK_PAYLOAD_MESSAGES[ERROR_WORKER_EXECUTION_FAILED],
                details={"exception_type": type(exc).__name__},
            ),
        )

    envelope = {
        "schema_version": EVALUATION_TASK_SCHEMA_VERSION,
        "hypothesis_id": hypothesis_id,
        "request_id": request_id,
        "status": TASK_RESULT_SUCCEEDED,
        "evaluation": evaluation,
        "error": None,
    }
    _strict_json_dump(envelope, label="task result", error_factory=_invalid_result)
    return envelope


# ---------------------------------------------------------------------------
# Result parsing (orchestrator side)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParsedTaskResult:
    """Validated interpretation of a worker envelope."""

    status: str
    evaluation: Optional[EvaluationResult]
    error: Optional[StructuredError]


def _parse_metric_mapping(value: Any, argument: str) -> Dict[str, Optional[float]]:
    mapping = _require_mapping(value, argument)
    parsed: Dict[str, Optional[float]] = {}
    for name, metric in mapping.items():
        if not isinstance(name, str) or not name:
            raise _invalid_result(
                f"{argument} keys must be non-empty strings.",
                argument=argument,
            )
        if metric is None:
            parsed[name] = None
            continue
        if isinstance(metric, bool) or not isinstance(metric, (int, float)):
            raise _invalid_result(
                f"{argument}[{name!r}] must be a finite number or null.",
                argument=argument,
                metric=name,
            )
        numeric = float(metric)
        if not math.isfinite(numeric):
            raise _invalid_result(
                f"{argument}[{name!r}] must be finite; non-finite metrics are not accepted.",
                argument=argument,
                metric=name,
            )
        parsed[name] = numeric
    return parsed


def _optional_mapping(value: Any, argument: str) -> Optional[Dict[str, Any]]:
    if value is None:
        return None
    return dict(_require_mapping(value, argument))


def _optional_string(value: Any, argument: str) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise _invalid_result(
            f"{argument} must be a non-empty string or null.",
            argument=argument,
            received_type=type(value).__name__,
        )
    return value


def _count_mapping(value: Any, argument: str) -> Optional[Dict[str, int]]:
    """Validate a count mapping (non-negative integer values)."""
    mapping = _optional_mapping(value, argument)
    if mapping is None:
        return None
    for key, count in mapping.items():
        if (
            isinstance(count, bool)
            or not isinstance(count, int)
            or count < 0
        ):
            raise _invalid_result(
                f"{argument}[{key!r}] must be a non-negative integer.",
                argument=argument,
                key=key,
            )
    return mapping


def _parse_warnings(value: Any) -> Optional[tuple]:
    if value is None:
        return None
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise _invalid_result("warnings must be a list of structured errors.")
    warnings = []
    for index, raw in enumerate(value):
        mapping = _require_mapping(raw, f"warnings[{index}]")
        code = mapping.get("code")
        message = mapping.get("message")
        if not isinstance(code, str) or not code or not isinstance(message, str):
            raise _invalid_result(
                f"warnings[{index}] must contain string code and message fields."
            )
        warnings.append(
            StructuredError(
                code=code,
                message=message,
                details=_optional_mapping(mapping.get("details"), f"warnings[{index}].details"),
            )
        )
    return tuple(warnings) or None


def evaluation_result_from_dict(data: Any) -> EvaluationResult:
    """Reconstruct an ``EvaluationResult`` from its JSON representation.

    Strict: unknown statuses, non-finite metrics, and malformed nested values
    are rejected instead of being coerced into a score.
    """
    payload = _require_mapping(data, "evaluation")
    hypothesis_id = _require_string(payload.get("hypothesis_id"), "evaluation.hypothesis_id")
    raw_status = payload.get("status")
    try:
        status = EvaluationStatus(raw_status)
    except (TypeError, ValueError) as exc:
        raise _invalid_result(
            "evaluation.status is not a recognised evaluation status.",
            received=raw_status,
        ) from exc
    if status == EvaluationStatus.PENDING:
        raise _invalid_result(
            "A pending evaluation is not a completed task result.",
            received=raw_status,
        )

    metrics = _parse_metric_mapping(payload.get("metrics", {}), "evaluation.metrics")
    sample_count = payload.get("sample_count", 0)
    if isinstance(sample_count, bool) or not isinstance(sample_count, int) or sample_count < 0:
        raise _invalid_result("evaluation.sample_count must be a non-negative integer.")

    error_data = payload.get("error")
    error = None
    if error_data is not None:
        try:
            error = StructuredError.from_dict(dict(_require_mapping(error_data, "evaluation.error")))
        except (KeyError, TypeError) as exc:
            raise _invalid_result(
                "evaluation.error must contain string code and message fields."
            ) from exc
    if status == EvaluationStatus.FAILED and error is None:
        raise _invalid_result("A failed evaluation must include a structured error.")

    held_out_metrics = None
    if payload.get("held_out_metrics") is not None:
        held_out_metrics = _parse_metric_mapping(
            payload["held_out_metrics"], "evaluation.held_out_metrics"
        )

    fitting_status = None
    raw_fitting_status = payload.get("fitting_status")
    if raw_fitting_status is not None:
        try:
            fitting_status = FittingStatus(raw_fitting_status)
        except (TypeError, ValueError) as exc:
            raise _invalid_result(
                "evaluation.fitting_status is not a recognised fitting status.",
                received=raw_fitting_status,
            ) from exc

    held_out_sample_count = payload.get("held_out_sample_count")
    if held_out_sample_count is not None and (
        isinstance(held_out_sample_count, bool)
        or not isinstance(held_out_sample_count, int)
        or held_out_sample_count < 0
    ):
        raise _invalid_result(
            "evaluation.held_out_sample_count must be a non-negative integer or null."
        )

    complexity = payload.get("complexity")
    if complexity is not None and (
        isinstance(complexity, bool) or not isinstance(complexity, int)
    ):
        raise _invalid_result("evaluation.complexity must be an integer or null.")

    metric_definitions = _optional_mapping(
        payload.get("metric_definitions"), "evaluation.metric_definitions"
    )
    if metric_definitions is not None and any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in metric_definitions.items()
    ):
        raise _invalid_result(
            "evaluation.metric_definitions must map strings to strings."
        )

    try:
        return EvaluationResult(
            hypothesis_id=hypothesis_id,
            status=status,
            evaluation_id=_require_string(
                payload.get("evaluation_id"), "evaluation.evaluation_id"
            ),
            metrics=metrics,
            sample_count=sample_count,
            held_out_metrics=held_out_metrics,
            error=error,
            evaluated_at=_require_string(
                payload.get("evaluated_at"), "evaluation.evaluated_at"
            ),
            model_family=_optional_string(
                payload.get("model_family"), "evaluation.model_family"
            ),
            fitting_status=fitting_status,
            fitting_method=_optional_string(
                payload.get("fitting_method"), "evaluation.fitting_method"
            ),
            fitting_config=_optional_mapping(
                payload.get("fitting_config"), "evaluation.fitting_config"
            ),
            fitted_specification=_optional_mapping(
                payload.get("fitted_specification"), "evaluation.fitted_specification"
            ),
            complexity=complexity,
            held_out_sample_count=held_out_sample_count,
            observation_counts=_count_mapping(
                payload.get("observation_counts"), "evaluation.observation_counts"
            ),
            exclusion_codes=_count_mapping(
                payload.get("exclusion_codes"), "evaluation.exclusion_codes"
            ),
            metric_definitions=metric_definitions,
            metric_version=_optional_string(
                payload.get("metric_version"), "evaluation.metric_version"
            ),
            dataset_ids=_optional_mapping(payload.get("dataset_ids"), "evaluation.dataset_ids"),
            request_id=_optional_string(
                payload.get("request_id"), "evaluation.request_id"
            ),
            warnings=_parse_warnings(payload.get("warnings")),
        )
    except TaskContractError:
        # A specific validation error from argument construction must keep its
        # own message instead of being reported as a generic inconsistency.
        raise
    except (TypeError, ValueError) as exc:
        raise _invalid_result(
            "evaluation is internally inconsistent.",
            exception_type=type(exc).__name__,
        ) from exc


def parse_task_result_envelope(
    envelope: Any, *, expected_hypothesis_id: str
) -> ParsedTaskResult:
    """Validate a worker envelope and correlate it with its originating task."""
    data = _require_mapping(envelope, "envelope")
    schema_version = data.get("schema_version")
    if schema_version != EVALUATION_TASK_SCHEMA_VERSION:
        raise _invalid_result(
            "Unsupported task result schema version.",
            expected=EVALUATION_TASK_SCHEMA_VERSION,
            received=schema_version,
        )

    hypothesis_id = _require_string(data.get("hypothesis_id"), "hypothesis_id")
    if hypothesis_id != expected_hypothesis_id:
        raise _invalid_result(
            "Task result hypothesis_id does not match the submitted task.",
            expected_hypothesis_id=expected_hypothesis_id,
            received_hypothesis_id=hypothesis_id,
        )

    status = data.get("status")
    if status == TASK_RESULT_FAILED:
        error_data = _require_mapping(data.get("error"), "error")
        code = error_data.get("code")
        message = error_data.get("message")
        if not isinstance(code, str) or not code or not isinstance(message, str):
            raise _invalid_result("A failed task result requires a structured error.")
        return ParsedTaskResult(
            status=TASK_RESULT_FAILED,
            evaluation=None,
            error=StructuredError(
                code=code,
                message=message,
                details=_optional_mapping(error_data.get("details"), "error.details"),
            ),
        )
    if status != TASK_RESULT_SUCCEEDED:
        raise _invalid_result(
            "Unrecognised task result status.",
            received=status,
        )

    evaluation = evaluation_result_from_dict(data.get("evaluation"))
    if evaluation.hypothesis_id != expected_hypothesis_id:
        raise _invalid_result(
            "Evaluation hypothesis_id does not match the submitted task.",
            expected_hypothesis_id=expected_hypothesis_id,
            received_hypothesis_id=evaluation.hypothesis_id,
        )
    return ParsedTaskResult(status=TASK_RESULT_SUCCEEDED, evaluation=evaluation, error=None)


__all__ = [
    "EVALUATION_TASK_SCHEMA_VERSION",
    "OPERATION_EVALUATE_CANDIDATE",
    "TASK_RESULT_FAILED",
    "TASK_RESULT_SUCCEEDED",
    "ERROR_INVALID_TASK_PAYLOAD",
    "ERROR_INVALID_TASK_RESULT",
    "ERROR_WORKER_EXECUTION_FAILED",
    "ParsedTaskResult",
    "TaskContractError",
    "build_evaluation_task_payload",
    "evaluation_result_from_dict",
    "execute_evaluation_task_payload",
    "parse_task_result_envelope",
    "validate_evaluation_task_payload",
]
