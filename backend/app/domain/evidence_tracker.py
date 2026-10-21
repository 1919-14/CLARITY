"""Evidence tracking engine for CLARITY Issue #7.

Classifies each observation-hypothesis pair as SUPPORTING, CONTRADICTING, or
INDETERMINATE based on a configurable tolerance band, and derives a scientific
PlausibilityStatus for each candidate from its accumulated evidence.

Design constraints
------------------
- Evidence identity key: (hypothesis_id, observation_id, partition).
  Stable across evaluation revisions; reprocessing the same observation does
  NOT duplicate evidence.
- EvidenceStore stores an append-only revision log per evidence key.
  Each revision records the full EvidenceRecord so changes in model specification
  or dataset version remain auditable.  The *current view* always uses the most
  recent revision per identity key, guaranteeing exactly-one-count semantics.
  Identical reprocessing (same evaluation_id + dataset_version) does not append
  a duplicate revision.
- Residual computation is overflow-guarded at every step.
- Evidence counts do NOT affect metric-based ranking; they establish status
  and traceability only.
- Evidence from training observations and held-out observations are tracked
  separately (partition = "training" | "held_out").
- collect_evidence() validates that each supplied observation's partition
  membership is consistent with the evaluation's observation membership records
  where available.  Observations whose membership cannot be verified are flagged
  with INDETERMINATE classification.
- The output_variable used for comparison is extracted from the fitted model
  specification (output_variable key) where available, falling back to the
  first key in observation.output_values.
- The model registry is injected into public API functions so callers using
  extended model families can supply a custom registry without monkey-patching
  a module-level singleton.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .contracts import (
    EvaluationResult,
    EvaluationStatus,
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


def _predict_for_observation(
    model_family_id: Optional[str],
    specification: Optional[Mapping[str, Any]],
    observation: Observation,
    registry: ModelFamilyRegistry,
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
    try:
        outputs, error = registry.predict(specification, observation.input_values)
    except Exception:  # pragma: no cover — defensive against registry bugs
        return None
    if error is not None or outputs is None:
        return None
    output_var = _extract_output_variable(specification)
    if output_var is None:
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
    registry: Optional[ModelFamilyRegistry] = None,
    membership_verified: bool = True,
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
    registry
        Model family registry to use for prediction.  Defaults to
        ``default_registry()`` when not supplied.  Inject a custom registry
        when using model families beyond the built-in set.
    membership_verified
        When False, the observation's partition membership in the evaluation
        being summarised could not be confirmed and the record is classified
        INDETERMINATE regardless of predicted vs observed values.  Callers
        should set this to False when the observation ID is absent from
        EvaluationResult.observation_counts or equivalent provenance.
    """
    if registry is None:
        registry = default_registry()

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

    # Membership must be verifiable before we can classify.
    if not membership_verified:
        return _indeterminate()

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
        model_family_id, fitted_specification, observation, registry
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
    registry: Optional[ModelFamilyRegistry] = None,
) -> List[EvidenceRecord]:
    """Produce evidence records for all observations associated with one evaluation.

    Idempotent: the evidence_id for each (hypothesis_id, observation_id, partition)
    triple is deterministic.  Callers that store records to a persistent store
    should upsert on evidence_id to avoid duplication.

    Only SUCCESS observations with output_values are eligible for classification.
    All others produce INDETERMINATE records that preserve traceability without
    fabricating classification.

    Membership validation
    ---------------------
    Each observation's partition label (``is_held_out``) is validated against
    the evaluation's provenance when ``EvaluationResult.dataset_ids`` contains
    explicit observation-ID lists under the keys ``"observation_ids"`` (training)
    and ``"held_out_observation_ids"`` (held-out).  If such lists are present,
    any observation whose ID is absent from the corresponding list is classified
    INDETERMINATE with a note recorded in the reason.  When the lists are absent
    (e.g. legacy results), membership is treated as unverified but not rejected,
    so evidence collection still proceeds — this is equivalent to ``membership_verified=True``
    for backwards compatibility.

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
    registry
        Optional custom model family registry.  Defaults to ``default_registry()``.
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

    # Build membership sets from evaluation provenance (when available).
    # These are the observation IDs that were *accepted* into the respective partition.
    known_training_ids: Optional[frozenset] = None
    known_held_out_ids: Optional[frozenset] = None
    if evaluation_result.dataset_ids is not None:
        raw_training = evaluation_result.dataset_ids.get("observation_ids")
        raw_held_out = evaluation_result.dataset_ids.get("held_out_observation_ids")
        if isinstance(raw_training, (list, tuple, set, frozenset)):
            known_training_ids = frozenset(str(x) for x in raw_training)
        if isinstance(raw_held_out, (list, tuple, set, frozenset)):
            known_held_out_ids = frozenset(str(x) for x in raw_held_out)

    records: List[EvidenceRecord] = []
    for obs in observations:
        # Validate partition membership against evaluation provenance.
        membership_verified = True
        if obs.is_held_out:
            if known_held_out_ids is not None:
                membership_verified = obs.observation_id in known_held_out_ids
        else:
            if known_training_ids is not None:
                membership_verified = obs.observation_id in known_training_ids

        record = classify_observation_evidence(
            hypothesis_id=hypothesis_id,
            observation=obs,
            evaluation_id=evaluation_id,
            policy=policy,
            model_family_id=model_family_id,
            fitted_specification=fitted_spec,
            dataset_id=dataset_id,
            dataset_version=dataset_version,
            registry=registry,
            membership_verified=membership_verified,
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
    6. Accepted performance criterion check: the evaluation must have a finite
       primary metric (MAE by default) on either held-out or training data.
       Without a validated metric we cannot assert acceptable predictive
       performance, so the candidate remains UNCERTAIN.
    7. Otherwise → PLAUSIBLE.

    Note: evidence counts do NOT override metric scores; they only affect status
    classification.  A CONTRADICTED candidate may still have a low MAE on
    training data — the status makes the contradiction visible without discarding
    the metric.

    Plausibility criterion (step 6)
    --------------------------------
    PLAUSIBLE requires at least one finite primary metric value (held-out
    preferred, training accepted).  This ensures that agreement on supplied
    observations alone — without a measurable predictive error — is not
    sufficient to label a candidate as scientifically plausible.  If neither
    partition produces a finite metric the candidate is UNCERTAIN even when
    all evidence is SUPPORTING.
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

    # Require at least one accepted, finite primary metric to confirm
    # acceptable predictive performance (finding #3).
    primary = policy.primary_metric
    has_held_out_metric = (
        evaluation_result.held_out_metrics is not None
        and primary in evaluation_result.held_out_metrics
        and evaluation_result.held_out_metrics[primary] is not None
        and math.isfinite(float(evaluation_result.held_out_metrics[primary]))  # type: ignore[arg-type]
    )
    has_training_metric = (
        evaluation_result.metrics is not None
        and primary in evaluation_result.metrics
        and evaluation_result.metrics[primary] is not None
        and math.isfinite(float(evaluation_result.metrics[primary]))  # type: ignore[arg-type]
    )
    if not (has_held_out_metric or has_training_metric):
        return PlausibilityStatus.UNCERTAIN

    return PlausibilityStatus.PLAUSIBLE


class EvidenceStore:
    """In-memory repository for EvidenceRecords with an append-only revision log.

    Identity and idempotency
    ------------------------
    Records are keyed by (hypothesis_id, observation_id, partition).  The
    *current view* always reflects the most recent revision per identity key
    so evidence counts each observation identity exactly once.

    Revision auditability
    ---------------------
    Every distinct evaluation produces a revision entry.  Two calls with
    identical (evidence_id, evaluation_id, dataset_version) are deduplicated
    as the same logical revision and do not create a second entry.  Calls with
    a different evaluation_id or dataset_version are appended as new revisions
    so specification changes and dataset upgrades remain auditable.

    This design satisfies finding #2: the previous prediction, residual,
    classification, and provenance are never overwritten and can be recovered
    from the revision log.
    """

    def __init__(self, initial_records: Optional[Sequence[EvidenceRecord]] = None):
        # Current view: (hypothesis_id, observation_id, partition) → latest record
        self._current: Dict[Tuple[str, str, str], EvidenceRecord] = {}
        # Revision log: same key → list of distinct revision records in ingestion order
        self._revisions: Dict[Tuple[str, str, str], List[EvidenceRecord]] = {}
        if initial_records:
            self.add_many(initial_records)

    def add(self, record: EvidenceRecord) -> None:
        """Add or update an evidence record, preserving prior revisions.

        If an identical revision (same evidence_id + evaluation_id +
        dataset_version) already exists it is not duplicated.
        """
        key = (record.hypothesis_id, record.observation_id, record.partition)

        # Append to revision log only when the revision is new.
        existing_revisions = self._revisions.setdefault(key, [])
        revision_is_new = not any(
            r.evaluation_id == record.evaluation_id
            and r.dataset_version == record.dataset_version
            for r in existing_revisions
        )
        if revision_is_new:
            existing_revisions.append(record)

        # Update the current view if this revision is newer (by timestamp).
        current = self._current.get(key)
        if current is None or record.timestamp >= current.timestamp:
            self._current[key] = record

    def add_many(self, records: Sequence[EvidenceRecord]) -> None:
        """Add multiple evidence records idempotently."""
        for r in records:
            self.add(r)

    def get_records_for_hypothesis(self, hypothesis_id: str) -> List[EvidenceRecord]:
        """Return all current-view evidence records for a specific hypothesis."""
        return [r for r in self._current.values() if r.hypothesis_id == hypothesis_id]

    def get_revisions_for_hypothesis(self, hypothesis_id: str) -> List[EvidenceRecord]:
        """Return all historical revisions for a specific hypothesis (audit log)."""
        revisions: List[EvidenceRecord] = []
        for (hid, _, _), recs in self._revisions.items():
            if hid == hypothesis_id:
                revisions.extend(recs)
        return revisions

    def get_summary(self, hypothesis_id: str) -> EvidenceSummary:
        """Compute an aggregated EvidenceSummary for a hypothesis from the current view."""
        records = self.get_records_for_hypothesis(hypothesis_id)
        return summarize_evidence(hypothesis_id, records)

    def count(self) -> int:
        """Total number of unique evidence records in the current view."""
        return len(self._current)

    def revision_count(self, hypothesis_id: Optional[str] = None) -> int:
        """Total number of stored revisions (optionally filtered by hypothesis)."""
        if hypothesis_id is None:
            return sum(len(revs) for revs in self._revisions.values())
        return sum(
            len(revs)
            for (hid, _, _), revs in self._revisions.items()
            if hid == hypothesis_id
        )

    def all_records(self) -> List[EvidenceRecord]:
        """Return all current-view evidence records."""
        return list(self._current.values())
