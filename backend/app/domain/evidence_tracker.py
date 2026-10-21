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
  Revision identity = (evaluation_id, dataset_version, content_fingerprint)
  where content_fingerprint encodes (classification, residual, predicted_value).
  Same fingerprint → no-op (idempotent).  Same (eval_id, dataset_version) but
  different content → ValueError (caller violated revision-identity contract).
  Different (eval_id, dataset_version) → new revision appended.
- Membership verification (Finding #4): collect_evidence() checks whether each
  supplied observation's ID appears in EvaluationResult.dataset_ids membership
  lists.  When no lists are present, ALL observations are unverified
  (membership_verified=False).  Classification is still computed from residuals
  for display, but EvidenceSummary.unverified_membership_count and status
  derivation treat such records as uncertain.
- PLAUSIBLE requires an explicit performance_acceptance_threshold in policy
  (Finding #1).  Without a configured threshold, even a finite metric with
  enough samples remains UNCERTAIN, because no agreed acceptance criterion has
  been established.
- Evidence counts do NOT participate in metric-based ranking.  Evidence
  establishes PlausibilityStatus and traces why a candidate holds its position;
  it does not double-count observations already reflected in MAE/RMSE.
- No confidence percentages, probability-of-correctness claims, or hidden-rule
  knowledge are produced here.
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
    """Determine the output variable name to use for comparison."""
    if specification is not None:
        var = _extract_output_variable(specification)
        if var is not None:
            return var
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
    """Attempt to produce a scalar numeric prediction for one observation."""
    if model_family_id is None or specification is None:
        return None
    try:
        outputs, error = registry.predict(specification, observation.input_values)
    except Exception:  # pragma: no cover
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


def _content_fingerprint(
    classification: EvidenceClassification,
    residual: Optional[float],
    predicted_value: Optional[float],
) -> Tuple:
    """Stable content fingerprint for revision identity (Finding #6).

    Two records with the same (classification, residual, predicted_value) at
    the same (evaluation_id, dataset_version) are considered the same revision.
    If any of these differ under the same evaluation identity, the payload has
    mutated and the caller has violated the revision-identity contract.
    """
    # Round to 15 significant digits to avoid float representation drift.
    r = round(residual, 15) if residual is not None else None
    p = round(predicted_value, 15) if predicted_value is not None else None
    return (classification.value, r, p)


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
    membership_verified
        When False, the observation's partition membership in the evaluation
        being summarised could not be confirmed.  Classification is still
        computed from residuals (for display), but EvidenceSummary will count
        this record in unverified_membership_count, and derive_plausibility_status
        will keep the candidate at UNCERTAIN regardless of other criteria.

        Callers should set this to False when the observation ID is absent
        from EvaluationResult.dataset_ids.observation_ids / held_out_observation_ids,
        or when those lists are entirely absent from the evaluation result.
    """
    if registry is None:
        registry = default_registry()

    partition = "held_out" if observation.is_held_out else "training"
    evidence_id = EvidenceRecord.make_evidence_id(
        hypothesis_id, observation.observation_id, partition
    )
    now = utc_now_iso()

    def _make_record(
        cls: EvidenceClassification,
        obs_val: Optional[float] = None,
        pred_val: Optional[float] = None,
        res: Optional[float] = None,
        tol: Optional[float] = None,
    ) -> EvidenceRecord:
        return EvidenceRecord(
            evidence_id=evidence_id,
            hypothesis_id=hypothesis_id,
            observation_id=observation.observation_id,
            experiment_id=observation.input_id,
            partition=partition,
            classification=cls,
            observed_value=obs_val,
            predicted_value=pred_val,
            residual=res,
            tolerance=tol,
            is_held_out=observation.is_held_out,
            dataset_id=dataset_id,
            evaluation_id=evaluation_id,
            dataset_version=dataset_version,
            timestamp=now,
            membership_verified=membership_verified,
        )

    def _indeterminate(obs_val: Optional[float] = None, pred_val: Optional[float] = None) -> EvidenceRecord:
        return _make_record(EvidenceClassification.INDETERMINATE, obs_val, pred_val)

    # Membership must be verifiable before we can compute supporting/contradicting evidence.
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

    return _make_record(classification, observed, predicted, residual, tolerance)


def collect_evidence(
    evaluation_result: EvaluationResult,
    observations: Sequence[Observation],
    policy: RankingPolicyConfig,
    dataset_version: Optional[int] = None,
    registry: Optional[ModelFamilyRegistry] = None,
) -> List[EvidenceRecord]:
    """Produce evidence records for all observations associated with one evaluation.

    Membership verification (Finding #4)
    ------------------------------------
    Each observation's partition membership is validated against
    ``EvaluationResult.dataset_ids.observation_ids`` (training) and
    ``held_out_observation_ids`` (held-out).

    * When BOTH lists are absent → no observations can be verified →
      ALL records carry ``membership_verified=False``.
    * When a list IS present for a partition, observations in that partition
      are verified individually (present in list → True, absent → False).
    * When only one partition's list is present, only that partition benefits
      from verified membership; the other partition's observations are unverified.

    Records with ``membership_verified=False`` are classified normally for
    display, but ``EvidenceSummary.unverified_membership_count`` counts them,
    and ``derive_plausibility_status`` cannot grant PLAUSIBLE when this count
    is > 0.
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

    # Build membership sets from evaluation provenance.
    known_training_ids: Optional[frozenset] = None
    known_held_out_ids: Optional[frozenset] = None
    if evaluation_result.dataset_ids is not None:
        raw_training = (
            evaluation_result.dataset_ids.get("training_observation_ids")
            or evaluation_result.dataset_ids.get("observation_ids")
        )
        raw_held_out = evaluation_result.dataset_ids.get("held_out_observation_ids")
        if isinstance(raw_training, (list, tuple, set, frozenset)):
            known_training_ids = frozenset(str(x) for x in raw_training)
        if isinstance(raw_held_out, (list, tuple, set, frozenset)):
            known_held_out_ids = frozenset(str(x) for x in raw_held_out)

    # When NEITHER list is present, no observation can be verified.
    has_any_membership_lists = (known_training_ids is not None) or (known_held_out_ids is not None)

    records: List[EvidenceRecord] = []
    for obs in observations:
        if not has_any_membership_lists:
            # No membership information at all — all observations unverified.
            membership_verified = False
        elif obs.is_held_out:
            if known_held_out_ids is None:
                # Held-out list absent; cannot verify held-out observation.
                membership_verified = False
            else:
                membership_verified = obs.observation_id in known_held_out_ids
        else:
            if known_training_ids is None:
                # Training list absent; cannot verify training observation.
                membership_verified = False
            else:
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
    is used.

    Records with ``membership_verified=False`` are counted in
    ``unverified_membership_count`` in addition to their classification bucket.
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
    unverified_ids: List[str] = []

    for rec in seen.values():
        if not rec.membership_verified:
            unverified_ids.append(rec.observation_id)
        if rec.classification == EvidenceClassification.SUPPORTING:
            supporting_ids.append(rec.observation_id)
        elif rec.classification == EvidenceClassification.CONTRADICTING:
            contradicting_ids.append(rec.observation_id)
        else:
            indeterminate_ids.append(rec.observation_id)

    supporting_ids.sort()
    contradicting_ids.sort()
    indeterminate_ids.sort()
    unverified_ids.sort()

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
        unverified_membership_count=len(unverified_ids),
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
    5. evidence_summary.unverified_membership_count > 0 → UNCERTAIN.
       The observation population is unknown; we cannot confirm the evidence base
       is correct for this evaluation, so PLAUSIBLE is not granted.
    6. evidence_summary.indeterminate_count > 0 → UNCERTAIN.
    7. Accepted performance criterion (Finding #1):
       A finite primary metric must exist AND must satisfy
       policy.performance_acceptance_threshold.  When the threshold is None,
       PLAUSIBLE is NOT granted — no agreed acceptance criterion exists, so
       we cannot assert acceptable performance.  When set, the best available
       metric (held-out preferred) must meet it.
    8. Otherwise → PLAUSIBLE.

    PLAUSIBLE therefore requires ALL of:
    - Enough samples (>= min_samples_for_plausibility).
    - No contradictions exceeding the tolerance.
    - All evidence with verified membership.
    - No INDETERMINATE residuals.
    - An explicit performance threshold configured in policy AND the metric
      value meets that threshold.
    """
    if evaluation_result is None:
        return PlausibilityStatus.UNEVALUATED

    if evaluation_result.status == EvaluationStatus.FAILED:
        return PlausibilityStatus.UNEVALUABLE

    if evaluation_result.sample_count < policy.min_samples_for_plausibility:
        return PlausibilityStatus.UNCERTAIN

    if evidence_summary.contradicting_count > policy.max_tolerated_contradictions:
        return PlausibilityStatus.CONTRADICTED

    # Unverified membership means we cannot confirm the evidence population
    # matches the evaluation, so PLAUSIBLE cannot be granted.
    if evidence_summary.unverified_membership_count > 0:
        return PlausibilityStatus.UNCERTAIN

    if evidence_summary.indeterminate_count > 0:
        return PlausibilityStatus.UNCERTAIN

    # Finding #1: require explicit, task-appropriate performance criterion.
    # Pick best available metric (held-out preferred over training).
    primary = policy.primary_metric
    best_metric: Optional[float] = None
    if (
        evaluation_result.held_out_metrics is not None
        and primary in evaluation_result.held_out_metrics
        and evaluation_result.held_out_metrics[primary] is not None
    ):
        try:
            v = float(evaluation_result.held_out_metrics[primary])  # type: ignore[arg-type]
            if math.isfinite(v):
                best_metric = v
        except (TypeError, ValueError):
            pass
    if best_metric is None and primary in evaluation_result.metrics:
        try:
            v = float(evaluation_result.metrics[primary])  # type: ignore[arg-type]
            if math.isfinite(v):
                best_metric = v
        except (TypeError, ValueError):
            pass

    if best_metric is None:
        # No finite primary metric available at all.
        return PlausibilityStatus.UNCERTAIN

    # Apply the explicit acceptance threshold (if configured).
    if not policy.metric_meets_acceptance(best_metric):
        # threshold is None → "not configured" → UNCERTAIN
        # threshold set but metric fails → UNCERTAIN
        return PlausibilityStatus.UNCERTAIN

    return PlausibilityStatus.PLAUSIBLE


class EvidenceStore:
    """In-memory repository for EvidenceRecords with an append-only revision log.

    Identity and idempotency
    ------------------------
    Records are keyed by (hypothesis_id, observation_id, partition).  The
    *current view* always reflects the most recent revision per identity key
    so evidence counts each observation identity exactly once.

    Revision identity (Finding #6)
    --------------------------------
    Revision identity = (evaluation_id, dataset_version, content_fingerprint)
    where content_fingerprint encodes (classification, residual, predicted_value).

    * Same revision identity → no-op (idempotent re-ingest).
    * Same (evaluation_id, dataset_version) but DIFFERENT content fingerprint
      → ValueError: the caller has violated the revision-identity contract by
      mutating payload under a stable identity key.  This prevents silent
      corruption of the audit log.
    * Different (evaluation_id, dataset_version) → new revision appended;
      prior revisions are preserved for auditability.
    """

    def __init__(self, initial_records: Optional[Sequence[EvidenceRecord]] = None):
        # Current view: (hypothesis_id, observation_id, partition) → latest record
        self._current: Dict[Tuple[str, str, str], EvidenceRecord] = {}
        # Revision log: same key → list of (fingerprint_tuple, record)
        self._revisions: Dict[Tuple[str, str, str], List[Tuple[Tuple, EvidenceRecord]]] = {}
        if initial_records:
            self.add_many(initial_records)

    def add(self, record: EvidenceRecord) -> None:
        """Add or update an evidence record, preserving prior revisions.

        Revision identity = (evaluation_id, dataset_version, content_fingerprint).
        Identical revision → no-op.
        Same (eval_id, dataset_version) with different fingerprint → ValueError.
        """
        key = (record.hypothesis_id, record.observation_id, record.partition)
        fingerprint = _content_fingerprint(
            record.classification, record.residual, record.predicted_value
        )
        revision_tuple = (record.evaluation_id, record.dataset_version, fingerprint)

        existing_revisions = self._revisions.setdefault(key, [])

        for stored_fp, _ in existing_revisions:
            stored_eval_id, stored_ds_ver, stored_content_fp = stored_fp
            if (stored_eval_id == record.evaluation_id
                    and stored_ds_ver == record.dataset_version):
                if stored_content_fp == fingerprint:
                    # Identical revision — idempotent no-op.
                    return
                else:
                    # Same (eval_id, dataset_version), different content.
                    raise ValueError(
                        f"EvidenceStore: conflicting payload for revision identity "
                        f"(evaluation_id={record.evaluation_id!r}, "
                        f"dataset_version={record.dataset_version!r}) "
                        f"on key (hypothesis={record.hypothesis_id!r}, "
                        f"observation={record.observation_id!r}, "
                        f"partition={record.partition!r}).  "
                        f"Stored fingerprint={stored_content_fp!r}, "
                        f"incoming fingerprint={fingerprint!r}.  "
                        f"A revision identity must not produce different evidence payloads."
                    )

        existing_revisions.append((revision_tuple, record))

        # Update the current view if this revision is newer (by timestamp).
        current = self._current.get(key)
        if current is None or record.timestamp >= current.timestamp:
            self._current[key] = record

    def add_many(self, records: Sequence[EvidenceRecord]) -> None:
        """Add multiple evidence records, honouring the revision-identity contract."""
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
                revisions.extend(r for _, r in recs)
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
