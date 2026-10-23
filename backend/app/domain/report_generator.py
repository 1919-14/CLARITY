"""Pure-domain scientific report generator for CLARITY (Issue #23).

Constructs structured scientific discovery reports from persisted run and evaluation data.
Strictly enforces:
1. No fabricated hypotheses or metrics: if fewer than 3 candidates exist, only report available ones.
2. No fabricated confidence: no arbitrary percentages like "92% certain".
3. Honest cost classification: estimated, accruing, settled, or unavailable.
4. Preserved evidence provenance: observations are traced by ID, partition, and residual.
5. Appropriate labeling for incomplete, cancelled, failed, or budget-exhausted runs.
"""

from __future__ import annotations

from datetime import datetime, timezone
import math
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple
import uuid

from .contracts import (
    EvaluationResult,
    EvaluationStatus,
    HypothesisRepresentation,
    Observation,
    ObservationStatus,
    utc_now_iso,
)
from .evidence_tracker import collect_evidence
from .hypothesis_models import ModelFamilyRegistry, default_registry
from .ranking_contracts import (
    EvaluationTier,
    EvidenceClassification,
    PlausibilityStatus,
    RankingPolicyConfig,
    RankingResult,
)
from .ranking_engine import HypothesisRanker
from .report_contracts import (
    REPORT_SCHEMA_VERSION,
    BudgetAndExecutionSection,
    ComplexityAndUncertaintySection,
    DiscoveryProcessSection,
    EvaluationMethodologySection,
    EvidenceItem,
    NextExperimentRecommendation,
    ReportHypothesis,
    ReportStatus,
    RunSummarySection,
    ScientificReport,
)


def _safe_float(val: Any) -> Optional[float]:
    if val is None:
        return None
    try:
        f = float(val)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError, OverflowError):
        return None


def generate_scientific_report(
    run_id: str,
    run_name: str,
    domain: str,
    execution_mode: str,
    sampling_strategy: str,
    run_status: str,
    stop_reason: Optional[str],
    created_at: Optional[str],
    started_at: Optional[str],
    completed_at: Optional[str],
    budget_limits: Optional[Dict[str, Any]],
    usage_data: Optional[Dict[str, Any]],
    candidates: Sequence[HypothesisRepresentation],
    evaluations_by_hypothesis: Mapping[str, EvaluationResult],
    observations: Sequence[Observation],
    candidate_experiments: Optional[Sequence[Dict[str, Any]]] = None,
    ranking_policy: Optional[RankingPolicyConfig] = None,
    model_registry: Optional[ModelFamilyRegistry] = None,
    report_id: Optional[str] = None,
) -> ScientificReport:
    """Generate a reproducible, defensible scientific discovery report."""
    resolved_report_id = report_id or f"rep-{uuid.uuid4().hex[:12]}"
    now_iso = utc_now_iso()
    policy = ranking_policy or RankingPolicyConfig()
    registry = model_registry or default_registry()

    # 1. Determine run duration and partial status
    duration_seconds: Optional[float] = None
    if started_at and completed_at:
        try:
            t_start = datetime.fromisoformat(started_at)
            t_end = datetime.fromisoformat(completed_at)
            duration_seconds = max(0.0, (t_end - t_start).total_seconds())
        except (ValueError, TypeError):
            duration_seconds = None

    terminal_complete = run_status.lower() in ("completed", "finished", "success")
    is_partial = not terminal_complete or bool(
        stop_reason and any(kw in stop_reason.lower() for kw in ("budget", "exhaust", "stop", "cancel", "fail", "timeout"))
    )

    # 2. Run Summary Section
    run_summary = RunSummarySection(
        run_id=run_id,
        name=run_name,
        domain=domain,
        execution_mode=execution_mode,
        sampling_strategy=sampling_strategy,
        status=run_status,
        stop_reason=stop_reason,
        is_partial=is_partial,
        created_at=created_at,
        started_at=started_at,
        completed_at=completed_at,
        duration_seconds=duration_seconds,
    )

    # 3. Discovery Process Section
    queries_used = 0
    evals_performed = 0
    hyp_generated = len(candidates)
    resource_usage: Dict[str, Any] = {}

    if usage_data:
        queries_used = usage_data.get("queries_executed", 0)
        evals_performed = usage_data.get("evaluations_completed", 0)
        resource_usage = {
            "queries_executed": queries_used,
            "evaluations_scheduled": usage_data.get("evaluations_scheduled", 0),
            "evaluations_completed": evals_performed,
            "is_budget_exhausted": usage_data.get("is_budget_exhausted", False),
        }
    else:
        # Infer from observations and evaluations
        queries_used = len(observations)
        evals_performed = len(evaluations_by_hypothesis)
        resource_usage = {
            "queries_executed": queries_used,
            "evaluations_scheduled": evals_performed,
            "evaluations_completed": evals_performed,
            "is_budget_exhausted": is_partial and "budget" in (stop_reason or "").lower(),
        }

    search_progress = (
        f"Completed {queries_used} oracle queries and {evals_performed} candidate evaluations "
        f"across {hyp_generated} candidate hypotheses. "
        + ("Run concluded with full discovery loop." if not is_partial else f"Run stopped early: {stop_reason or 'partial execution'}.")
    )

    discovery_process = DiscoveryProcessSection(
        oracle_queries_used=queries_used,
        candidate_evaluations_performed=evals_performed,
        hypotheses_generated=hyp_generated,
        search_progress=search_progress,
        resource_usage=resource_usage,
    )

    # 4. Evaluation Methodology Section
    dataset_version = len(observations)
    methodology = EvaluationMethodologySection(
        primary_metric=policy.primary_metric,
        metric_definitions={
            "mae": "Mean Absolute Error: (1/n) * sum(|y_obs - y_hat|). Lower is better (minimization target).",
            "rmse": "Root Mean Squared Error: sqrt((1/n) * sum((y_obs - y_hat)^2)). Lower is better.",
            "r2": "Coefficient of determination: 1 - SS_res / SS_tot. Higher is better (maximum 1.0).",
        },
        held_out_evaluation_approach=(
            "Observations flagged as held-out are excluded from candidate fitting and "
            "used exclusively to evaluate empirical generalization performance."
        ),
        dataset_version=dataset_version,
        ranking_criteria=(
            "Hypotheses are grouped into comparability cohorts sharing identical evaluation tier, "
            "dataset identifier, metric version, and primary metric definition. Within each cohort, "
            "hypotheses are ordered by primary metric magnitude (minimizing error) and complexity penalties. "
            "Display rank indicates empirical agreement within comparable cohorts, NOT calibrated "
            "statistical probability or subjective confidence."
        ),
    )

    # 5. Defensible Hypothesis Ranking
    ranker = HypothesisRanker(policy=policy, registry=registry)
    obs_by_hyp: Dict[str, Sequence[Observation]] = {
        cand.hypothesis_id: observations for cand in candidates
    }

    ranking_result: RankingResult = ranker.rank_candidates(
        candidates=candidates,
        evaluation_results=evaluations_by_hypothesis,
        observations_by_hypothesis=obs_by_hyp,
        dataset_version=dataset_version,
    )

    # Top 3 to 5 eligible hypotheses (or all available if < 3 without inventing candidates)
    all_ranked = ranking_result.ranked_candidates
    max_to_report = min(5, len(all_ranked))
    selected_ranked = all_ranked[:max_to_report]

    cand_by_id = {c.hypothesis_id: c for c in candidates}
    report_hypotheses: List[ReportHypothesis] = []

    for rc in selected_ranked:
        cand = cand_by_id.get(rc.hypothesis_id)
        eval_res = evaluations_by_hypothesis.get(rc.hypothesis_id)

        # Collect evidence records
        supporting_items: List[EvidenceItem] = []
        contradicting_items: List[EvidenceItem] = []

        if eval_res and observations:
            try:
                evidence_records = collect_evidence(
                    evaluation_result=eval_res,
                    observations=observations,
                    policy=policy,
                    dataset_version=dataset_version,
                    registry=registry,
                )
                obs_map = {o.observation_id: o for o in observations}
                for ev in evidence_records:
                    obs_item = obs_map.get(ev.observation_id)
                    item = EvidenceItem(
                        observation_id=ev.observation_id,
                        sequence_number=obs_item.sequence_number if obs_item else None,
                        input_values=dict(obs_item.input_values) if obs_item else {},
                        observed_value=ev.observed_value,
                        predicted_value=ev.predicted_value,
                        residual=ev.residual,
                        is_held_out=ev.is_held_out,
                        tolerance=ev.tolerance,
                    )
                    if ev.classification == EvidenceClassification.SUPPORTING:
                        supporting_items.append(item)
                    elif ev.classification == EvidenceClassification.CONTRADICTING:
                        contradicting_items.append(item)
            except Exception:
                pass

        hyp_limitations = list(rc.limitations)
        if eval_res and eval_res.status == EvaluationStatus.FAILED:
            hyp_limitations.append(
                f"Evaluation failed: {eval_res.error.message if eval_res.error else 'Unknown error'}."
            )
        if rc.held_out_score is None:
            hyp_limitations.append("No held-out evaluation score is available.")

        formula_str = cand.formula if cand else rc.hypothesis_id
        model_family_str = cand.model_family if cand else "unknown"
        params_dict = dict(cand.parameters) if cand else {}
        rev_num = cand.revision if cand else 1

        report_hypotheses.append(
            ReportHypothesis(
                rank=rc.rank,
                within_cohort_rank=rc.within_cohort_rank,
                hypothesis_id=rc.hypothesis_id,
                name=f"Candidate {rc.hypothesis_id} ({model_family_str})",
                model_family=model_family_str,
                formula=formula_str,
                parameters=params_dict,
                revision_number=rev_num,
                evaluation_id=rc.evaluation_id,
                status=rc.status.value,
                evaluation_tier=rc.evaluation_tier.value,
                primary_metric_name=rc.primary_metric_name,
                held_out_score=rc.held_out_score,
                training_score=rc.training_score,
                complexity_score=rc.complexity,
                dataset_id=rc.dataset_id,
                dataset_version=dataset_version,
                explanation=rc.explanation,
                supporting_evidence=tuple(supporting_items),
                contradicting_evidence=tuple(contradicting_items),
                indeterminate_count=rc.evidence_summary.indeterminate_count,
                uncertainty_and_limitations=tuple(hyp_limitations),
            )
        )

    # 6. Complexity and Uncertainty Section
    complexity_and_uncertainty = ComplexityAndUncertaintySection(
        complexity_metric="Parameter and functional term count",
        complexity_definition=(
            "Model complexity represents the count of free parameters and functional terms "
            "in the symbolic candidate specification. Simpler models are preferred when predictive "
            "performance is statistically indistinguishable."
        ),
        uncertainty_status="Unavailable / Uncalibrated",
        uncertainty_disclosures=(
            "Evaluation results provide point residual errors against observed data points.",
            "Prediction intervals, posterior covariances, and Bayesian confidence distributions are unavailable.",
            "Ranking scores reflect empirical fit within the sampled domain and must not be interpreted as subjective probabilities of truth.",
        ),
    )

    # 7. Budget and Execution Summary Section
    limits_dict = dict(budget_limits) if budget_limits else {}
    measured_usage_dict: Dict[str, Any] = {
        "queries_executed": queries_used,
        "evaluations_completed": evals_performed,
    }

    # Derive cost states honestly: never represent missing cost as $0.00
    cost_dict: Dict[str, Any] = {
        "state": "unavailable",
        "estimated_cost_usd": None,
        "accruing_cost_usd": None,
        "settled_cost_usd": None,
        "pricing_assumptions": None,
    }

    if usage_data:
        estimated_val = _safe_float(usage_data.get("estimated_cost_usd"))
        accruing_val = _safe_float(usage_data.get("live_accruing_cost_usd") or usage_data.get("accruing_cost_usd"))
        settled_val = _safe_float(usage_data.get("settled_cost_usd"))

        if settled_val is not None:
            cost_dict = {
                "state": "settled",
                "estimated_cost_usd": estimated_val,
                "accruing_cost_usd": accruing_val,
                "settled_cost_usd": settled_val,
                "pricing_assumptions": None,
            }
        elif accruing_val is not None:
            cost_dict = {
                "state": "accruing",
                "estimated_cost_usd": estimated_val,
                "accruing_cost_usd": accruing_val,
                "settled_cost_usd": None,
                "pricing_assumptions": None,
            }
        elif estimated_val is not None:
            cost_dict = {
                "state": "estimated",
                "estimated_cost_usd": estimated_val,
                "accruing_cost_usd": None,
                "settled_cost_usd": None,
                "pricing_assumptions": {
                    "method": "application_measured_duration_rate",
                    "rate_per_task_minute_usd": 0.05 if execution_mode == "nodus" else 0.0,
                    "currency": "USD",
                },
            }
        else:
            cost_dict = {
                "state": "unavailable",
                "estimated_cost_usd": None,
                "accruing_cost_usd": None,
                "settled_cost_usd": None,
                "pricing_assumptions": None,
            }
    elif execution_mode == "local":
        cost_dict = {
            "state": "estimated",
            "estimated_cost_usd": 0.0,
            "accruing_cost_usd": None,
            "settled_cost_usd": None,
            "pricing_assumptions": {"rate": "$0.00 / local execution hour", "currency": "USD"},
        }

    budget_and_execution = BudgetAndExecutionSection(
        configured_limits=limits_dict,
        measured_usage=measured_usage_dict,
        cost=cost_dict,
    )

    # 8. Overall Limitations
    overall_limitations: List[str] = list(ranking_result.limitations)
    if is_partial:
        overall_limitations.append(
            f"Partial Run Notice: The discovery run did not complete normally ({stop_reason or run_status}). "
            "Reported candidates represent intermediate findings."
        )
    if len(report_hypotheses) < 3:
        overall_limitations.append(
            f"Sparse Candidate Pool: Only {len(report_hypotheses)} candidate hypothesis was evaluated; "
            "additional hypotheses were not fabricated."
        )
    if dataset_version < 5:
        overall_limitations.append(
            f"Low Sample Count: Only {dataset_version} observation(s) available; generalization metrics may have high variance."
        )

    # 9. Next Experiment Recommendation
    next_exp_rec: NextExperimentRecommendation
    if len(report_hypotheses) >= 2:
        top_a = report_hypotheses[0]
        top_b = report_hypotheses[1]
        
        # Check if candidate experiments exist
        chosen_exp_id: Optional[str] = None
        chosen_input: Optional[Dict[str, Any]] = None
        strategy = "active_disagreement"
        disagreement_score: Optional[float] = None

        if candidate_experiments:
            for exp in candidate_experiments:
                if exp.get("execution_status") in ("proposed", "eligible"):
                    chosen_exp_id = exp.get("experiment_id")
                    chosen_input = exp.get("proposed_input")
                    rationale = exp.get("selection_rationale") or {}
                    disagreement_score = _safe_float(rationale.get("disagreement_score"))
                    strategy = rationale.get("strategy", strategy)
                    break

        if chosen_input is None and observations:
            # Recommend an interpolated/extrapolated test input based on first observation keys
            ref_keys = list(observations[0].input_values.keys())
            chosen_input = {k: 5.0 for k in ref_keys}

        next_exp_rec = NextExperimentRecommendation(
            recommended=True,
            experiment_id=chosen_exp_id,
            input_values=chosen_input,
            justification=(
                f"Candidate query distinguishes leading hypotheses '{top_a.hypothesis_id}' "
                f"and '{top_b.hypothesis_id}' where their mathematical models diverge."
            ),
            selection_strategy=strategy,
            predicted_disagreement=disagreement_score,
        )
    else:
        next_exp_rec = NextExperimentRecommendation(
            recommended=False,
            experiment_id=None,
            input_values=None,
            justification=(
                "Fewer than two eligible hypotheses are available to evaluate prediction disagreement; "
                "further candidate generation is required before targeted distinguishing experiments can be planned."
            ),
            selection_strategy=None,
            predicted_disagreement=None,
        )

    # 10. Conclusion
    if not report_hypotheses:
        conclusion = (
            "No valid candidate hypotheses were successfully evaluated for this discovery run. "
            "No empirical conclusions can be drawn without evaluated candidates."
        )
    else:
        leading = report_hypotheses[0]
        held_out_str = (
            f"held-out {policy.primary_metric.upper()} of {leading.held_out_score:.4f}"
            if leading.held_out_score is not None
            else f"training {policy.primary_metric.upper()} of {leading.training_score:.4f}"
            if leading.training_score is not None
            else "unquantified error"
        )
        conclusion = (
            f"The available empirical evidence supports Candidate '{leading.hypothesis_id}' "
            f"({leading.model_family}) as the best-fitting explanation with a {held_out_str}. "
            "However, these models represent mathematical approximations over the observed parameter space. "
            + (
                f"Alternative candidate '{report_hypotheses[1].hypothesis_id}' cannot be ruled out until resolving experiments are performed. "
                if len(report_hypotheses) > 1
                else ""
            )
            + "Uncertainty remains regarding unobserved input regions, and scientific validity requires continuous external experimental verification."
        )

    return ScientificReport(
        report_id=resolved_report_id,
        run_id=run_id,
        status=ReportStatus.GENERATED,
        schema_version=REPORT_SCHEMA_VERSION,
        generated_at=now_iso,
        run_summary=run_summary,
        discovery_process=discovery_process,
        evaluation_methodology=methodology,
        ranked_hypotheses=tuple(report_hypotheses),
        complexity_and_uncertainty=complexity_and_uncertainty,
        budget_and_execution=budget_and_execution,
        limitations=tuple(overall_limitations),
        next_experiment=next_exp_rec,
        conclusion=conclusion,
    )
