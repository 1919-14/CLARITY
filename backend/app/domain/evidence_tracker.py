"""Evidence tracking engine for CLARITY Issue #7.

Classifies each observation-hypothesis pair as SUPPORTING, CONTRADICTING, or
INDETERMINATE based on a configurable tolerance band, and derives a scientific
PlausibilityStatus for each candidate from its accumulated evidence.

Design constraints
------------------
- Evidence identity key: (hypothesis_id, observation_id, partition).
  Stable across evaluation revisions; reprocessing the same observation does
  NOT duplicate evidence.
- Evaluation provenance (evaluation_id, dataset_version) is recorded but does
  not affect idempotency.
- Residual computation is overflow-guarded at every step.
- Evidence counts do NOT affect metric-based ranking; they establish status
  and traceability only.
- Evidence from training observations and held-out observations are tracked
  separately (partition = "training" | "held_out").
- The output_variable used for comparison is extracted from the fitted model
  specification (output_variable key) where available, falling back to the
  first key in observation.output_values.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .contracts import (
    EvaluationResult,
    EvaluationStatus,
    HypothesisRepresentation,
    Observation,
    ObservationStatus,
    generate_id,
    utc_now_iso,
)
from .hypothesis_models import ModelFamilyRegistry, default_registry
from .ranking_contracts import (
    EvidenceClassification,
    EvidenceRecord,
    EvidenceSummary,
    PlausibilityStatus,
    RankingPolicyConfig,
)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _safe_float(value: Any) -> Optional[float]:
    """Convert value to float, returning None on any failure."""
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(result):
        return None
    return result


def _safe_abs_residual(a: float, b: float) -> Optional[float]:
    """Compute |a - b|, returning None on overflow or non-finite result."""
    try:
        diff = a - b
    except OverflowError:
        return None
    if not math.isfinite(diff):
        return None
    try:
        result = abs(diff)
    except OverflowError:
        return None
    return result


def _compute_tolerance(observed: float, policy: RankingPolicyConfig) -> Optional[float]:
    """Compute T = tau_abs + tau_rel * |y_obs|, guarding against overflow."""
    try:
        rel_term = policy.tolerance_relative * abs(observed)
    except OverflowError:
        return None
    try:
        tolerance = policy.tolerance_absolute + rel_term
    except OverflowError:
        return None
    if not math.isfinite(tolerance):
        return None
    return tolerance


def _extract_output_variable(specification: Mapping[str, Any]) -> Optional[str]:
    """Extract output_variable from a fitted specification, if present."""
    output_var = specification.get("output_variable")
    if isinstance(output_var, str) and output_var:
        return output_var
    return None


def _resolve_output_variable(
    specification: Optional[Mapping[str, Any]],
    output_values: Dict[str, Any],
) -> Optional[str]:
    """Determine the output variable name to use for comparison.

    Preference order:
    1. specification["output_variable"] when the fitted specification exists.
    2. The first key in output_values when the dict has exactly one key.
    3. None — cannot resolve; evidence will be INDETERMINATE.
    """
    if specification is not None:
        var = _extract_output_variable(specification)
        if var is not None:
            return var
    # Fall back to the single-key case to support bare observations without specs.
    keys = list(output_values.keys())
    if len(keys) == 1:
        return keys[0]
    return None


# Module-level registry singleton used by the evidence tracker.
# Callers that need a custom registry (e.g. extended max degree) should call
# collect_evidence() or classify_observation_evidence() via a wrapper that
# injects their registry into _predict_for_observation.
_DEFAULT_REGISTRY: ModelFamilyRegistry = default_registry()


def _predict_for_observation(
    model_family_id: Optional[str],
    specification: Optional[Mapping[str, Any]],
    observation: Observation,
    registry: ModelFamilyRegistry = _DEFAULT_REGISTRY,
) -> Optional[float]:
    """Attempt to produce a scalar numeric prediction for one observation.

    Returns None if:
    - model_family_id is absent or not in the registry.
    - specification is absent.
    - The prediction raises or returns non-finite.
    - The output variable cannot be resolved.
    """
    if model_family_id is None or specification is None:
        return None
    # Use the registry's unified predict path, which dispatches by family.
    try:
        outputs, error = registry.predict(specification, observation.input_values)
    except Exception:  # pragma: no cover — defensive against registry bugs
        return None
    if error is not None or outputs is None:
        return None
    output_var = _extract_output_variable(specification)
    if output_var is None:
        # Try to get it from outputs directly.
        keys = list(outputs.keys())
        if len(keys) == 1:
            output_var = keys[0]
        else:
            return None
    raw = outputs.get(output_var)
    return _safe_float(raw)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def classify_observation_evidence(
    hypothesis_id: str,
    observation: Observation,
    evaluation_id: str,
    policy: RankingPolicyConfig,
    model_family_id: Optional[str] = None,
    fitted_specification: Optional[Mapping[str, Any]] = None,
    dataset_id: Optional[str] = None,
    dataset_version: Optional[int] = None,
) -> EvidenceRecord:
    """Produce one EvidenceRecord for a single (hypothesis, observation) pair.

    The result is always an EvidenceRecord — never raises.  All computation
    failures produce INDETERMINATE classification with None scores.

    Parameters
    ----------
    hypothesis_id
        Stable ID of the candidate being evaluated.
    observation
        Source observation from the run's observation history.
    evaluation_id
        EvaluationResult.evaluation_id that triggered this classification.
    policy
        RankingPolicyConfig supplying tolerance parameters.
    model_family_id
        Registered family identifier for prediction.  None → INDETERMINATE.
    fitted_specification
        Fitted model specification from EvaluationResult.  None → INDETERMINATE.
    dataset_id
        Dataset identifier for provenance.
    dataset_version
        Numeric dataset version, when available.
    """
    partition = "held_out" if observation.is_held_out else "training"
    evidence_id = EvidenceRecord.make_evidence_id(
        hypothesis_id, observation.observation_id, partition
    )
    now = utc_now_iso()

    def _indeterminate(obs_val: Optional[float] = None, pred_val: Optional[float] = None) -> EvidenceRecord:
        return EvidenceRecord(
            evidence_id=evidence_id,
            hypothesis_id=hypothesis_id,
            observation_id=observation.observation_id,
            experiment_id=observation.input_id,
            partition=partition,
            classification=EvidenceClassification.INDETERMINATE,
            observed_value=obs_val,
            predicted_value=pred_val,
            residual=None,
            tolerance=None,
            is_held_out=observation.is_held_out,
            dataset_id=dataset_id,
            evaluation_id=evaluation_id,
            dataset_version=dataset_version,
            timestamp=now,
        )

    # Only SUCCESS observations with numeric outputs can yield finite evidence.
    if observation.status != ObservationStatus.SUCCESS or observation.output_values is None:
        return _indeterminate()

    # Resolve output variable.
    output_var = _resolve_output_variable(fitted_specification, observation.output_values)
    if output_var is None:
        return _indeterminate()

    # Extract observed value.
    observed_raw = observation.output_values.get(output_var)
    observed = _safe_float(observed_raw)
    if observed is None:
        return _indeterminate()

    # Produce model prediction.
    predicted = _predict_for_observation(
        model_family_id, fitted_specification, observation
    )
    if predicted is None:
        return _indeterminate(obs_val=observed)

    # Compute residual.
    residual = _safe_abs_residual(observed, predicted)
    if residual is None:
        return _indeterminate(obs_val=observed, pred_val=predicted)

    # Compute tolerance.
    tolerance = _compute_tolerance(observed, policy)
    if tolerance is None:
        return _indeterminate(obs_val=observed, pred_val=predicted)

    # Classify.
    classification = (
        EvidenceClassification.SUPPORTING
        if residual <= tolerance
        else EvidenceClassification.CONTRADICTING
    )

    return EvidenceRecord(
        evidence_id=evidence_id,
        hypothesis_id=hypothesis_id,
        observation_id=observation.observation_id,
        experiment_id=observation.input_id,
        partition=partition,
        classification=classification,
        observed_value=observed,
        predicted_value=predicted,
        residual=residual,
        tolerance=tolerance,
        is_held_out=observation.is_held_out,
        dataset_id=dataset_id,
        evaluation_id=evaluation_id,
        dataset_version=dataset_version,
        timestamp=now,
    )


def collect_evidence(
    evaluation_result: EvaluationResult,
    observations: Sequence[Observation],
    policy: RankingPolicyConfig,
    dataset_version: Optional[int] = None,
) -> List[EvidenceRecord]:
    """Produce evidence records for all observations associated with one evaluation.

    Idempotent: the evidence_id for each (hypothesis_id, observation_id, partition)
    triple is deterministic.  Callers that store records to a persistent store
    should upsert on evidence_id to avoid duplication.

    Only SUCCESS observations with output_values are eligible for classification.
    All others produce INDETERMINATE records that preserve traceability without
    fabricating classification.

    Parameters
    ----------
    evaluation_result
        Completed or failed evaluation result.  When FAILED, all records are
        INDETERMINATE (no fitted specification is available for prediction).
    observations
        All observations passed to the evaluator for this candidate, both
        training and held-out.
    policy
        Tolerance policy for residual classification.
    dataset_version
        Optional dataset version number for provenance.
    """
    hypothesis_id = evaluation_result.hypothesis_id
    evaluation_id = evaluation_result.evaluation_id
    model_family_id = evaluation_result.model_family
    fitted_spec = (
        dict(evaluation_result.fitted_specification)
        if evaluation_result.fitted_specification is not None
        else None
    )
    dataset_id: Optional[str] = None
    if evaluation_result.dataset_ids is not None:
        ds = evaluation_result.dataset_ids.get("dataset_id")
        if isinstance(ds, str) and ds:
            dataset_id = ds

    records: List[EvidenceRecord] = []
    for obs in observations:
        record = classify_observation_evidence(
            hypothesis_id=hypothesis_id,
            observation=obs,
            evaluation_id=evaluation_id,
            policy=policy,
            model_family_id=model_family_id,
            fitted_specification=fitted_spec,
            dataset_id=dataset_id,
            dataset_version=dataset_version,
        )
        records.append(record)
    return records


def summarize_evidence(
    hypothesis_id: str,
    records: Sequence[EvidenceRecord],
) -> EvidenceSummary:
    """Aggregate a flat list of EvidenceRecords into a structured summary.

    Deduplication: when multiple records share the same (observation_id,
    partition) pair, only the most recent one (by timestamp, then stable sort)
    is used.  This handles idempotent re-processing without re-counting.

    Observation IDs are preserved in sorted order for deterministic output.
    """
    # Deduplicate: keep last record per (observation_id, partition).
    seen: Dict[Tuple[str, str], EvidenceRecord] = {}
    for rec in records:
        key = (rec.observation_id, rec.partition)
        existing = seen.get(key)
        if existing is None or rec.timestamp >= existing.timestamp:
            seen[key] = rec

    supporting_ids: List[str] = []
    contradicting_ids: List[str] = []
    indeterminate_ids: List[str] = []

    for rec in seen.values():
        if rec.classification == EvidenceClassification.SUPPORTING:
            supporting_ids.append(rec.observation_id)
        elif rec.classification == EvidenceClassification.CONTRADICTING:
            contradicting_ids.append(rec.observation_id)
        else:
            indeterminate_ids.append(rec.observation_id)

    supporting_ids.sort()
    contradicting_ids.sort()
    indeterminate_ids.sort()

    s = len(supporting_ids)
    c = len(contradicting_ids)
    i = len(indeterminate_ids)

    return EvidenceSummary(
        hypothesis_id=hypothesis_id,
        supporting_count=s,
        contradicting_count=c,
        indeterminate_count=i,
        total_evaluated=s + c + i,
        supporting_observation_ids=tuple(supporting_ids),
        contradicting_observation_ids=tuple(contradicting_ids),
        indeterminate_observation_ids=tuple(indeterminate_ids),
    )


def derive_plausibility_status(
    evaluation_result: Optional[EvaluationResult],
    evidence_summary: EvidenceSummary,
    policy: RankingPolicyConfig,
) -> PlausibilityStatus:
    """Derive the scientific PlausibilityStatus for a candidate.

    Decision tree (applied in order):

    1. evaluation_result is None → UNEVALUATED.
    2. evaluation_result.status is FAILED → UNEVALUABLE.
    3. evaluation_result.sample_count < policy.min_samples_for_plausibility → UNCERTAIN.
    4. evidence_summary.contradicting_count > policy.max_tolerated_contradictions → CONTRADICTED.
    5. evidence_summary.indeterminate_count > 0 and no contradictions → UNCERTAIN
       (because we cannot confirm full consistency without finite predictions).
    6. Otherwise → PLAUSIBLE.

    Note: evidence counts do NOT override metric scores; they only affect status
    classification.  A CONTRADICTED candidate may still have a low MAE on
    training data — the status makes the contradiction visible without discarding
    the metric.
    """
    if evaluation_result is None:
        return PlausibilityStatus.UNEVALUATED

    if evaluation_result.status == EvaluationStatus.FAILED:
        return PlausibilityStatus.UNEVALUABLE

    # Require sufficient training observations for plausibility determination.
    if evaluation_result.sample_count < policy.min_samples_for_plausibility:
        return PlausibilityStatus.UNCERTAIN

    # Any validated contradiction exceeding the tolerance → CONTRADICTED.
    if evidence_summary.contradicting_count > policy.max_tolerated_contradictions:
        return PlausibilityStatus.CONTRADICTED

    # Indeterminate evidence means we cannot confirm full consistency.
    if evidence_summary.indeterminate_count > 0:
        return PlausibilityStatus.UNCERTAIN

    return PlausibilityStatus.PLAUSIBLE


class EvidenceStore:
    """In-memory or persistence-backed repository for EvidenceRecords.

    Guarantees idempotency: keyed by (hypothesis_id, observation_id, partition).
    Upserting the same observation updates evaluation provenance without
    duplicating records or inflating evidence counts.
    """

    def __init__(self, initial_records: Optional[Sequence[EvidenceRecord]] = None):
        self._records: Dict[Tuple[str, str, str], EvidenceRecord] = {}
        if initial_records:
            self.add_many(initial_records)

    def add(self, record: EvidenceRecord) -> None:
        """Add or update an evidence record idempotently."""
        key = (record.hypothesis_id, record.observation_id, record.partition)
        self._records[key] = record

    def add_many(self, records: Sequence[EvidenceRecord]) -> None:
        """Add multiple evidence records idempotently."""
        for r in records:
            self.add(r)

    def get_records_for_hypothesis(self, hypothesis_id: str) -> List[EvidenceRecord]:
        """Return all evidence records for a specific hypothesis."""
        return [r for r in self._records.values() if r.hypothesis_id == hypothesis_id]

    def get_summary(self, hypothesis_id: str) -> EvidenceSummary:
        """Compute an aggregated EvidenceSummary for a hypothesis."""
        records = self.get_records_for_hypothesis(hypothesis_id)
        return summarize_evidence(hypothesis_id, records)

    def count(self) -> int:
        """Total number of unique evidence records stored."""
        return len(self._records)

    def all_records(self) -> List[EvidenceRecord]:
        """Return all stored evidence records."""
        return list(self._records.values())

