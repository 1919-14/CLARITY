"""Scientific Benchmark Evaluation Service.

Orchestrates ground-truth benchmark evaluation for discovery runs.
Operates on the evaluator side of the trust boundary:
- Uses BenchmarkEvaluator to measure Hit@K, MRR, held-out accuracy, and query efficiency.
- Persists BenchmarkResultModel records in the database.
- Guarantees zero leakage of evaluator secrets into discovery-facing objects.
"""

from typing import Any, Dict, List, Mapping, Optional, Tuple
from sqlalchemy.orm import Session

from ..adapters.persistence.models import BenchmarkResultModel, HypothesisModel
from ..adapters.persistence.repository import ClarityRepository
from ..domain.contracts import (
    GroundTruthRule,
    HypothesisRepresentation,
    BenchmarkReport,
    verify_no_ground_truth_leakage,
)
from ..evaluator.benchmark_evaluator import BenchmarkEvaluator
from ..evaluator.benchmark_cases import get_benchmark_case

FORBIDDEN_METADATA_KEY_SUBSTRINGS = (
    "ground_truth",
    "secret",
    "oracle_internal",
    "evaluator_only",
    "true_rule",
    "target_formula",
    "target_parameters",
    "target_coefficients",
)


def _inspect_metadata_node(
    node: Any,
    ground_truth: GroundTruthRule,
    path: str = "",
) -> None:
    """Recursively validate metadata for forbidden secret keys or leaked values."""
    if isinstance(node, Mapping):
        for k, v in node.items():
            if not isinstance(k, str):
                raise ValueError(
                    f"Metadata keys must be strings, got {type(k).__name__} at '{path}'."
                )
            k_lower = k.lower()
            for pattern in FORBIDDEN_METADATA_KEY_SUBSTRINGS:
                if pattern in k_lower:
                    raise ValueError(
                        f"Unsafe metadata key '{k}' detected at '{path}': "
                        f"contains forbidden pattern '{pattern}'."
                    )
            _inspect_metadata_node(v, ground_truth, f"{path}.{k}" if path else k)
    elif isinstance(node, (list, tuple)):
        for idx, item in enumerate(node):
            _inspect_metadata_node(item, ground_truth, f"{path}[{idx}]")
    elif isinstance(node, str):
        # Check if string contains secret target formula
        if ground_truth.target_formula:
            clean_tgt = ground_truth.target_formula.replace(" ", "").lower()
            clean_val = node.replace(" ", "").lower()
            if len(clean_tgt) > 3 and clean_tgt in clean_val:
                raise ValueError(
                    f"Unsafe metadata value at '{path}': contains secret target formula."
                )
        # Check if string contains secret_seed
        if ground_truth.secret_seed is not None and len(str(ground_truth.secret_seed)) >= 2:
            seed_str = str(ground_truth.secret_seed)
            if seed_str in node:
                raise ValueError(
                    f"Unsafe metadata value at '{path}': contains evaluator secret seed."
                )
    elif isinstance(node, (int, float)) and not isinstance(node, bool):
        if ground_truth.secret_seed is not None and int(node) == int(ground_truth.secret_seed):
            raise ValueError(
                f"Unsafe metadata value at '{path}': matches evaluator secret seed."
            )


def run_benchmark_evaluation(
    session: Session,
    run_id: str,
    ground_truth: GroundTruthRule,
    benchmark_name: Optional[str] = None,
    baseline_type: Optional[str] = "random_sampling",
    expected_baseline_queries: Optional[int] = None,
    custom_metadata: Optional[Dict[str, Any]] = None,
    tolerance: float = 0.05,
) -> BenchmarkResultModel:
    """Run an isolated scientific benchmark evaluation on a discovery run.

    Args:
        session: Active SQLAlchemy database session.
        run_id: ID of the discovery run being benchmarked.
        ground_truth: Independently held GroundTruthRule with reference targets.
        benchmark_name: Human-readable name of the benchmark suite/case.
        baseline_type: Baseline method comparison ('random_sampling', 'passive_uniform').
        expected_baseline_queries: Query count expected for the baseline method.
        custom_metadata: Additional non-sensitive audit metadata.
        tolerance: Parameter and prediction matching tolerance.

    Returns:
        Persisted BenchmarkResultModel containing standard scientific metrics.
    """
    repo = ClarityRepository(session)
    run = repo.get_run(run_id)
    if not run:
        raise ValueError(f"Run '{run_id}' not found.")

    # 1. Fetch hypotheses and order them by scientific merit
    hypotheses = repo.list_hypotheses_for_run(run_id, include_excluded=False)
    ranked_candidates, candidate_metrics = _build_and_rank_candidates(repo, run_id, hypotheses)

    # 2. Extract query efficiency
    queries_executed = _get_queries_executed(repo, run_id)

    # 3. Compute baseline efficiency gain
    # Requirement 1: Zero queries must NOT report a 1.0x gain (ratio undefined -> unavailable/None).
    # Requirement 2: Do NOT invent a default 50 queries. Require explicit or registered baseline.
    efficiency_gain = None
    if baseline_type and queries_executed is not None and queries_executed > 0:
        baseline_queries = expected_baseline_queries
        if baseline_queries is None:
            # Check if this benchmark case has a registered baseline query expectation
            case_def = get_benchmark_case(benchmark_name or ground_truth.evaluator_label)
            if (
                case_def
                and case_def.expected_baseline_queries
                and baseline_type in case_def.expected_baseline_queries
            ):
                baseline_queries = case_def.expected_baseline_queries[baseline_type]

        if baseline_queries is not None and baseline_queries > 0:
            gain_val = float(baseline_queries) / float(queries_executed)
            efficiency_gain = f"{gain_val:.1f}x"

    # 4. Instantiate evaluator and evaluate candidates
    evaluator = BenchmarkEvaluator(ground_truth=ground_truth, tolerance=tolerance)
    
    # 5. Extract held-out MAE and RMSE from best candidate or matching candidate
    # Requirement 3: Strictly preserve None for missing held-out metrics; DO NOT fall back to training error.
    held_out_mae = None
    held_out_rmse = None
    
    # Check if any candidate matches ground truth
    matching_candidate_id = None
    for cand in ranked_candidates:
        if evaluator.is_match(cand):
            matching_candidate_id = cand.hypothesis_id
            break

    target_id_for_metrics = matching_candidate_id or (ranked_candidates[0].hypothesis_id if ranked_candidates else None)
    if target_id_for_metrics and target_id_for_metrics in candidate_metrics:
        m = candidate_metrics[target_id_for_metrics]
        held_out_mae = m.get("held_out_mae")
        held_out_rmse = m.get("held_out_rmse")

    report: BenchmarkReport = evaluator.evaluate_candidates(
        run_id=run_id,
        ranked_candidates=ranked_candidates,
        queries_executed=queries_executed,
        held_out_mae=held_out_mae,
        held_out_rmse=held_out_rmse,
        benchmark_name=benchmark_name or f"{ground_truth.domain}_benchmark",
        baseline_type=baseline_type,
        efficiency_gain=efficiency_gain,
        metadata=custom_metadata,
    )

    # 6. Audit & build execution metadata (must not contain ground truth secrets)
    # Requirement 5: Validate and reject unsafe/secret metadata before persistence.
    safe_custom_metadata: Dict[str, Any] = {}
    if custom_metadata:
        _inspect_metadata_node(custom_metadata, ground_truth)
        safe_custom_metadata = dict(custom_metadata)

    execution_metadata: Dict[str, Any] = {
        "candidate_count": len(ranked_candidates),
        "matching_candidate_found": matching_candidate_id is not None,
        "tolerance": tolerance,
        "baseline_type": baseline_type,
    }
    execution_metadata.update(safe_custom_metadata)

    # Verify boundary integrity
    verify_no_ground_truth_leakage(execution_metadata)

    # 7. Persist to database
    result = BenchmarkResultModel(
        benchmark_id=report.benchmark_id,
        run_id=run_id,
        benchmark_name=report.benchmark_name or benchmark_name or f"{ground_truth.domain}_benchmark",
        target_label=report.target_label,
        hit_at_1=report.hit_at_1,
        hit_at_3=report.hit_at_3,
        hit_at_5=report.hit_at_5,
        reciprocal_rank=report.reciprocal_rank,
        best_candidate_id=report.best_candidate_id,
        queries_to_target=report.queries_to_target,
        held_out_mae=report.held_out_mae,
        held_out_rmse=report.held_out_rmse,
        baseline_type=report.baseline_type,
        efficiency_gain=report.efficiency_gain,
        execution_metadata=execution_metadata,
    )

    return repo.record_benchmark_result(result)


def _get_queries_executed(repo: ClarityRepository, run_id: str) -> Optional[int]:
    """Retrieve total queries executed for a run from usage or budget records."""
    usage = repo.get_latest_usage_for_run(run_id)
    if usage is not None and usage.queries_executed is not None:
        return usage.queries_executed

    budget = repo.get_budget_usage(run_id)
    if budget is not None and budget.queries_completed is not None:
        return budget.queries_completed

    # Fallback to observation count
    count = repo.count_observations_for_run(run_id, only_training=True)
    return count if count > 0 else None


def _build_and_rank_candidates(
    repo: ClarityRepository,
    run_id: str,
    hypotheses: List[HypothesisModel],
) -> Tuple[List[HypothesisRepresentation], Dict[str, Dict[str, Optional[float]]]]:
    """Transform hypothesis models and evaluations into an ordered candidate list."""
    candidate_metrics: Dict[str, Dict[str, Optional[float]]] = {}
    candidate_scores: List[Tuple[float, float, int, HypothesisRepresentation]] = []
    unscored_candidates: List[HypothesisRepresentation] = []

    # Check if a synthesized report already exists for preferred ordering
    latest_report = repo.get_latest_report_for_run(run_id)
    report_rank_order: Dict[str, int] = {}
    if latest_report and latest_report.ranked_candidates:
        for idx, entry in enumerate(latest_report.ranked_candidates):
            if isinstance(entry, dict) and "hypothesis_id" in entry:
                report_rank_order[entry["hypothesis_id"]] = idx

    for hyp in hypotheses:
        # Get latest revision
        latest_rev = hyp.revisions[-1] if hyp.revisions else None
        formula = latest_rev.formula if latest_rev else "y = 0.0"
        parameters = latest_rev.parameters if latest_rev else {}
        complexity = latest_rev.complexity if latest_rev else 1

        cand_repr = HypothesisRepresentation(
            hypothesis_id=hyp.hypothesis_id,
            model_family=hyp.model_family,
            formula=formula,
            parameters=parameters,
            complexity=complexity,
            revision=hyp.current_revision,
        )

        # Collect evaluation metrics separately: held-out error is NEVER conflated with training error
        held_out_mae = None
        held_out_rmse = None
        training_mae = None
        training_rmse = None

        if latest_rev and latest_rev.evaluations:
            for ev in reversed(latest_rev.evaluations):
                if ev.status == "completed":
                    if ev.held_out_metrics:
                        held_out_mae = ev.held_out_metrics.get("mae")
                        held_out_rmse = ev.held_out_metrics.get("rmse")
                    if ev.metrics:
                        training_mae = ev.metrics.get("mae")
                        training_rmse = ev.metrics.get("rmse")
                    break

        candidate_metrics[hyp.hypothesis_id] = {
            "training_mae": training_mae,
            "training_rmse": training_rmse,
            "held_out_mae": held_out_mae,
            "held_out_rmse": held_out_rmse,
        }

        # Ranking uses held-out error if available, else training error for relative sorting
        rank_metric = held_out_mae if held_out_mae is not None else training_mae
        rank_secondary = held_out_rmse if held_out_rmse is not None else (training_rmse or 0.0)

        # Determine score
        if hyp.hypothesis_id in report_rank_order:
            rank_idx = report_rank_order[hyp.hypothesis_id]
            candidate_scores.append((rank_idx, 0.0, complexity, cand_repr))
        elif rank_metric is not None:
            candidate_scores.append((rank_metric, rank_secondary, complexity, cand_repr))
        else:
            unscored_candidates.append(cand_repr)

    # Sort scored candidates: lowest primary score, lowest secondary, lowest complexity
    candidate_scores.sort(key=lambda t: (t[0], t[1], t[2]))
    ordered_candidates = [t[3] for t in candidate_scores] + unscored_candidates

    return ordered_candidates, candidate_metrics


def trigger_benchmark_service(
    session: Session,
    run_id: str,
    benchmark_case: str,
    baseline_type: Optional[str] = "random_sampling",
    expected_baseline_queries: Optional[int] = None,
    custom_metadata: Optional[Dict[str, Any]] = None,
    tolerance: float = 0.05,
) -> BenchmarkResultModel:
    """Route-facing entry point. Requires an independently registered benchmark case.

    Callers cannot supply or inject custom ground-truth rules, target formulas,
    or parameters (Requirement 4).
    """
    if not benchmark_case or not isinstance(benchmark_case, str) or not benchmark_case.strip():
        raise ValueError("Field 'benchmark_case' is required and must reference a registered benchmark case.")

    case_def = get_benchmark_case(benchmark_case.strip())
    if not case_def:
        raise ValueError(f"Unknown benchmark case '{benchmark_case}'.")

    return run_benchmark_evaluation(
        session=session,
        run_id=run_id,
        ground_truth=case_def.ground_truth,
        benchmark_name=case_def.benchmark_name,
        baseline_type=baseline_type,
        expected_baseline_queries=expected_baseline_queries,
        custom_metadata=custom_metadata,
        tolerance=tolerance,
    )
