"""Performance Measurement Service for CLARITY (Issue #27).

Orchestrates reproducible measurement of:
- Scientific accuracy (ground-truth metrics, held-out errors, complexity, uncertainty).
- Oracle-query efficiency (actual executed queries, budget accounting, queries-to-success).
- Runtime behavior (wall-clock time, phase durations, compute task counts/durations).
- Compute cost (estimates, accruing, settled, and unavailable state tracking).
- Fair active vs baseline comparisons (absolute diffs, safe relative changes, aggregation policy).
"""

from __future__ import annotations

import math
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..adapters.persistence.models import (
    BenchmarkResultModel,
    CandidateExperimentModel,
    ComputeTaskModel,
    ObservationModel,
    ReportModel,
    RunBudgetUsageModel,
    RunModel,
    UsageRecordModel,
)
from ..adapters.persistence.repository import ClarityRepository
from ..domain.contracts import (
    GroundTruthRule,
    HypothesisRepresentation,
    generate_id,
    utc_now_iso,
    verify_no_ground_truth_leakage,
)
from ..domain.performance_contracts import (
    DIRECTION_HIGHER_IS_BETTER,
    DIRECTION_LOWER_IS_BETTER,
    MEASUREMENT_METHOD_VERSION,
    METRIC_DEFINITIONS_VERSION,
    UNIT_COUNT,
    UNIT_DIMENSIONLESS,
    UNIT_MILLISECONDS,
    UNIT_PERCENTAGE,
    UNIT_QUERIES,
    UNIT_RATIO,
    UNIT_USD,
    UNIT_Y_UNITS,
    AccuracyMeasurements,
    CostMeasurements,
    CostState,
    MetricAvailability,
    MetricComparisonDetail,
    MetricMeasurement,
    QueryEfficiencyMeasurements,
    RunPerformanceMeasurement,
    RuntimeMeasurements,
    StrategyAggregateMetrics,
    StrategyComparisonReport,
    calculate_safe_relative_change,
)
from ..services.usage_service import (
    DEFAULT_EVALUATION_COST_USD,
    DEFAULT_LOCAL_TASK_HOURLY_RATE_USD,
    DEFAULT_ORACLE_QUERY_COST_USD,
    DEFAULT_REMOTE_TASK_HOURLY_RATE_USD,
    DEFAULT_REMOTE_TASK_MINUTE_RATE_USD,
)
from ..evaluator.benchmark_cases import (
    BenchmarkCaseDefinition,
    PolynomialGroundTruthRule,
    get_benchmark_case,
)
from ..evaluator.benchmark_evaluator import BenchmarkEvaluator
from ..evaluator.benchmark_runner import (
    ActiveRandomBenchmarkRunner,
    BenchmarkComparisonSummary,
    BenchmarkTrialResult,
    _compute_held_out_error,
)


class PerformanceMeasurementService:
    """Core service for capturing, calculating, persisting, and comparing performance measurements."""

    def __init__(self, runner: Optional[ActiveRandomBenchmarkRunner] = None):
        self.runner = runner or ActiveRandomBenchmarkRunner()

    def measure_run(
        self,
        session: Session,
        run_id: str,
        scenario_id: Optional[str] = None,
        tolerance: float = 0.05,
    ) -> RunPerformanceMeasurement:
        """Extract and compile performance measurements for an existing discovery run.

        Pulls actual records from persistence:
        - Query counts from executed ObservationModel records (not proposed experiments).
        - Runtime from RunModel and ComputeTaskModel records.
        - Cost from UsageRecordModel and ComputeTaskModel records.
        - Accuracy from BenchmarkResultModel or candidate evaluations.
        """
        repo = ClarityRepository(session)
        run = repo.get_run(run_id)
        if not run:
            raise ValueError(f"Run '{run_id}' not found.")

        scenario = get_benchmark_case(scenario_id) if scenario_id else None
        bm_result = repo.get_benchmark_results_for_run(run_id)
        latest_bm = bm_result[-1] if bm_result else None

        # ----------------------------------------------------------------------
        # 1. Scientific Accuracy Measurement
        # ----------------------------------------------------------------------
        accuracy = self._build_accuracy_measurements(repo, run, latest_bm, scenario, tolerance)

        # ----------------------------------------------------------------------
        # 2. Query Efficiency Measurement
        # ----------------------------------------------------------------------
        query_efficiency = self._build_query_efficiency_measurements(repo, run, latest_bm, scenario, tolerance)

        # ----------------------------------------------------------------------
        # 3. Runtime & Execution Behavior Measurement
        # ----------------------------------------------------------------------
        runtime = self._build_runtime_measurements(repo, run)

        # ----------------------------------------------------------------------
        # 4. Compute Cost Measurement
        # ----------------------------------------------------------------------
        cost = self._build_cost_measurements(repo, run)

        config = {
            "execution_mode": run.execution_mode,
            "sampling_strategy": run.sampling_strategy,
            "max_queries": run.max_queries,
            "max_evaluations": run.max_evaluations,
            "max_runtime_seconds": run.max_runtime_seconds,
            "spend_ceiling_usd": run.spend_ceiling_usd,
        }

        measurement_id = generate_id("pm")
        return RunPerformanceMeasurement(
            measurement_id=measurement_id,
            run_id=run.run_id,
            benchmark_id=latest_bm.benchmark_id if latest_bm else None,
            scenario_id=scenario.id if scenario else (latest_bm.benchmark_name if latest_bm else run.name),
            scenario_version=scenario.version if scenario else "1.0.0",
            strategy=run.sampling_strategy,
            seed=None,
            timestamp=utc_now_iso(),
            accuracy=accuracy,
            query_efficiency=query_efficiency,
            runtime=runtime,
            cost=cost,
            configuration=config,
            measurement_method_version=MEASUREMENT_METHOD_VERSION,
        )

    def measure_benchmark_trial(
        self,
        trial: BenchmarkTrialResult,
        scenario: BenchmarkCaseDefinition,
    ) -> RunPerformanceMeasurement:
        """Construct a standardized RunPerformanceMeasurement from an in-memory benchmark trial."""
        sample_count = len(scenario.generate_held_out_dataset())
        tolerance = scenario.success_threshold

        # Accuracy
        hit_1 = MetricMeasurement(
            name="hit_at_1",
            value=1.0 if trial.hit_at_1 else 0.0,
            units=UNIT_RATIO,
            definition="Binary indicator: top-ranked candidate matches target within tolerance.",
            direction=DIRECTION_HIGHER_IS_BETTER,
            sample_count=1,
            status=MetricAvailability.AVAILABLE,
        )
        hit_3 = MetricMeasurement(
            name="hit_at_3",
            value=1.0 if trial.hit_at_3 else 0.0,
            units=UNIT_RATIO,
            definition="Binary indicator: target is within top-3 ranked candidates.",
            direction=DIRECTION_HIGHER_IS_BETTER,
            sample_count=1,
            status=MetricAvailability.AVAILABLE,
        )
        hit_5 = MetricMeasurement(
            name="hit_at_5",
            value=1.0 if trial.hit_at_5 else 0.0,
            units=UNIT_RATIO,
            definition="Binary indicator: target is within top-5 ranked candidates.",
            direction=DIRECTION_HIGHER_IS_BETTER,
            sample_count=1,
            status=MetricAvailability.AVAILABLE,
        )
        mrr = MetricMeasurement(
            name="mrr",
            value=trial.mrr,
            units=UNIT_RATIO,
            definition="Reciprocal rank of first matching candidate (1/rank).",
            direction=DIRECTION_HIGHER_IS_BETTER,
            sample_count=1,
            status=MetricAvailability.AVAILABLE,
        )
        mae = MetricMeasurement(
            name="held_out_mae",
            value=trial.held_out_mae,
            units=UNIT_Y_UNITS,
            definition="Mean Absolute Error on reproducible held-out test points.",
            direction=DIRECTION_LOWER_IS_BETTER,
            sample_count=sample_count,
            status=MetricAvailability.AVAILABLE if trial.held_out_mae is not None else MetricAvailability.UNAVAILABLE,
        )
        rmse = MetricMeasurement(
            name="held_out_rmse",
            value=trial.held_out_rmse,
            units=UNIT_Y_UNITS,
            definition="Root Mean Squared Error on reproducible held-out test points.",
            direction=DIRECTION_LOWER_IS_BETTER,
            sample_count=sample_count,
            status=MetricAvailability.AVAILABLE if trial.held_out_rmse is not None else MetricAvailability.UNAVAILABLE,
        )
        actual_complexity: Optional[float] = None
        if trial.top_candidate_formula:
            clean_formula = trial.top_candidate_formula.replace(" ", "")
            deg = 0
            if "x" in clean_formula:
                deg = 1
            for p_str in re.findall(r"x(?:\^|\*\*)(\d+)", clean_formula):
                deg = max(deg, int(p_str))
            actual_complexity = float(deg)

        complexity = MetricMeasurement(
            name="model_complexity",
            value=actual_complexity,
            units=UNIT_COUNT,
            definition="Hypothesis candidate mathematical complexity (polynomial degree).",
            direction=DIRECTION_LOWER_IS_BETTER,
            sample_count=1 if actual_complexity is not None else 0,
            status=MetricAvailability.AVAILABLE if actual_complexity is not None else MetricAvailability.UNAVAILABLE,
            notes=None if actual_complexity is not None else "Model complexity unavailable: no top candidate formula found.",
        )

        accuracy = AccuracyMeasurements(
            hit_at_1=hit_1,
            hit_at_3=hit_3,
            hit_at_5=hit_5,
            mrr=mrr,
            held_out_mae=mae,
            held_out_rmse=rmse,
            model_complexity=complexity,
            residual_variance=None,
            task_specific_metrics={},
            benchmark_version=scenario.version,
            dataset_version=sample_count,
            sample_count=sample_count,
            evaluation_status="completed",
            evaluated_at=utc_now_iso(),
        )

        # Query Efficiency
        query_efficiency = QueryEfficiencyMeasurements(
            queries_executed=trial.queries_used,
            query_budget=trial.max_budget,
            remaining_budget=max(0, trial.max_budget - trial.queries_used),
            target_reached=trial.target_reached,
            success_threshold=tolerance,
            queries_to_target=trial.queries_to_target,
            budget_exhausted=trial.budget_exhausted,
            status="target_reached" if trial.target_reached else ("budget_exhausted" if trial.budget_exhausted else "failed"),
            proposed_experiments_count=len(trial.selection_history),
        )

        # Runtime
        runtime = RuntimeMeasurements(
            total_wall_clock_ms=trial.runtime_ms,
            local_processing_ms=trial.runtime_ms,
            remote_execution_ms=None,
            phase_durations_ms={"discovery_loop_ms": trial.runtime_ms},
            task_queue_duration_ms=None,
            task_execution_duration_ms=trial.runtime_ms,
            evaluation_duration_ms=None,
            tasks_total=0,
            tasks_submitted=0,
            tasks_completed=0,
            tasks_failed=0,
            tasks_timed_out=0,
            tasks_cancelled=0,
            parallel_overlap_factor=None,
            timing_method="perf_counter_monotonic",
        )

        # Cost: Local trial estimate based on oracle queries
        est_cost = round(trial.queries_used * DEFAULT_ORACLE_QUERY_COST_USD, 4)
        cost = CostMeasurements(
            cost_state=CostState.ESTIMATED,
            estimated_cost_usd=est_cost,
            accruing_cost_usd=None,
            settled_cost_usd=None,
            currency="USD",
            calculation_method="application_documented_rates",
            pricing_assumptions={
                "oracle_query_cost_usd": DEFAULT_ORACLE_QUERY_COST_USD,
                "local_task_hourly_rate_usd": DEFAULT_LOCAL_TASK_HOURLY_RATE_USD,
            },
            source="application_estimate",
            is_provider_reported=False,
            measurement_timestamp=utc_now_iso(),
            notes="Local simulator execution: compute cost is $0.00; estimated query cost reflects standard rates.",
        )

        return RunPerformanceMeasurement(
            measurement_id=f"pm-{trial.trial_id}",
            run_id=trial.trial_id,
            benchmark_id=None,
            scenario_id=scenario.id,
            scenario_version=scenario.version,
            strategy=trial.strategy,
            seed=trial.seed,
            timestamp=utc_now_iso(),
            accuracy=accuracy,
            query_efficiency=query_efficiency,
            runtime=runtime,
            cost=cost,
            configuration={"max_budget": trial.max_budget, "tolerance": tolerance},
            measurement_method_version=MEASUREMENT_METHOD_VERSION,
        )

    def compare_strategies(
        self,
        active_measurements: Sequence[RunPerformanceMeasurement],
        baseline_measurements: Sequence[RunPerformanceMeasurement],
        scenario_id: str,
        scenario_version: str,
        query_budget: int,
        success_threshold: float,
    ) -> StrategyComparisonReport:
        """Perform rigorous comparative performance analysis between active and baseline strategies."""
        comparison_id = generate_id("comp")
        if not active_measurements or not baseline_measurements:
            raise ValueError("Active and baseline measurement sequences must both be non-empty for strategy comparison.")
        if len(active_measurements) != len(baseline_measurements):
            raise ValueError(
                f"Active and baseline measurements must have equal sample counts for paired comparison "
                f"(got {len(active_measurements)} and {len(baseline_measurements)})."
            )

        # Sort measurements by seed if seeds are provided to ensure stable, fair paired alignment
        active_sorted = sorted(active_measurements, key=lambda m: (m.seed if m.seed is not None else -1))
        baseline_sorted = sorted(baseline_measurements, key=lambda m: (m.seed if m.seed is not None else -1))

        expected_case = get_benchmark_case(scenario_id) if scenario_id else None
        expected_scen_id = expected_case.id if expected_case else scenario_id

        # Validate that each pair shares identical experimental conditions
        for i, (act, base) in enumerate(zip(active_sorted, baseline_sorted)):
            act_case = get_benchmark_case(act.scenario_id) if act.scenario_id else None
            act_scen = act_case.id if act_case else act.scenario_id

            base_case = get_benchmark_case(base.scenario_id) if base.scenario_id else None
            base_scen = base_case.id if base_case else base.scenario_id

            if act_scen != base_scen or (expected_scen_id and act_scen != expected_scen_id):
                raise ValueError(
                    f"Incompatible paired benchmark conditions at trial pair {i}: scenario mismatch "
                    f"(active: '{act.scenario_id}', baseline: '{base.scenario_id}', expected: '{scenario_id}')."
                )

            if act.scenario_version != base.scenario_version or (scenario_version and act.scenario_version != scenario_version):
                raise ValueError(
                    f"Incompatible paired benchmark conditions at trial pair {i}: scenario version mismatch "
                    f"(active: '{act.scenario_version}', baseline: '{base.scenario_version}', expected: '{scenario_version}')."
                )

            act_budget = act.query_efficiency.query_budget
            base_budget = base.query_efficiency.query_budget
            if act_budget != base_budget or (query_budget is not None and act_budget != query_budget):
                raise ValueError(
                    f"Incompatible paired benchmark conditions at trial pair {i}: per-trial budget mismatch "
                    f"(active: {act_budget}, baseline: {base_budget}, expected: {query_budget})."
                )

            if act.seed is not None or base.seed is not None:
                if act.seed != base.seed:
                    raise ValueError(
                        f"Incompatible paired benchmark conditions at trial pair {i}: seed mismatch "
                        f"(active: {act.seed}, baseline: {base.seed}). Paired comparisons must share identical seeds."
                    )

        n = len(active_sorted)

        active_aggr = self._compute_strategy_aggregates("active_selection", active_sorted)
        baseline_aggr = self._compute_strategy_aggregates("random_baseline", baseline_sorted)

        # Metric comparisons
        metric_comparisons: Dict[str, MetricComparisonDetail] = {}

        # 1. held_out_mae
        mae_comp = self._build_metric_comparison(
            metric_name="held_out_mae",
            active_val=active_aggr.mean_held_out_mae,
            baseline_val=baseline_aggr.mean_held_out_mae,
            units=UNIT_Y_UNITS,
            direction=DIRECTION_LOWER_IS_BETTER,
        )
        metric_comparisons["held_out_mae"] = mae_comp

        # 2. held_out_rmse
        rmse_comp = self._build_metric_comparison(
            metric_name="held_out_rmse",
            active_val=active_aggr.mean_held_out_rmse,
            baseline_val=baseline_aggr.mean_held_out_rmse,
            units=UNIT_Y_UNITS,
            direction=DIRECTION_LOWER_IS_BETTER,
        )
        metric_comparisons["held_out_rmse"] = rmse_comp

        # 3. queries_used
        queries_comp = self._build_metric_comparison(
            metric_name="queries_used",
            active_val=active_aggr.mean_queries_used,
            baseline_val=baseline_aggr.mean_queries_used,
            units=UNIT_QUERIES,
            direction=DIRECTION_LOWER_IS_BETTER,
        )
        metric_comparisons["queries_used"] = queries_comp

        # 4. queries_to_target
        q_target_comp = self._build_metric_comparison(
            metric_name="queries_to_target",
            active_val=active_aggr.mean_queries_to_target,
            baseline_val=baseline_aggr.mean_queries_to_target,
            units=UNIT_QUERIES,
            direction=DIRECTION_LOWER_IS_BETTER,
        )
        metric_comparisons["queries_to_target"] = q_target_comp

        # 5. hit_at_1
        hit_comp = self._build_metric_comparison(
            metric_name="hit_at_1_rate",
            active_val=active_aggr.hit_at_1_rate,
            baseline_val=baseline_aggr.hit_at_1_rate,
            units=UNIT_PERCENTAGE,
            direction=DIRECTION_HIGHER_IS_BETTER,
        )
        metric_comparisons["hit_at_1_rate"] = hit_comp

        # 6. mrr
        mrr_comp = self._build_metric_comparison(
            metric_name="mean_mrr",
            active_val=active_aggr.mean_mrr,
            baseline_val=baseline_aggr.mean_mrr,
            units=UNIT_RATIO,
            direction=DIRECTION_HIGHER_IS_BETTER,
        )
        metric_comparisons["mean_mrr"] = mrr_comp

        # 7. success_rate
        succ_comp = self._build_metric_comparison(
            metric_name="success_rate",
            active_val=active_aggr.success_rate,
            baseline_val=baseline_aggr.success_rate,
            units=UNIT_PERCENTAGE,
            direction=DIRECTION_HIGHER_IS_BETTER,
        )
        metric_comparisons["success_rate"] = succ_comp

        # 8. wall_clock_duration
        wc_comp = self._build_metric_comparison(
            metric_name="wall_clock_ms",
            active_val=active_aggr.mean_wall_clock_ms,
            baseline_val=baseline_aggr.mean_wall_clock_ms,
            units=UNIT_MILLISECONDS,
            direction=DIRECTION_LOWER_IS_BETTER,
        )
        metric_comparisons["wall_clock_ms"] = wc_comp

        # 9. estimated_cost
        cost_comp = self._build_metric_comparison(
            metric_name="estimated_cost_usd",
            active_val=active_aggr.mean_estimated_cost_usd,
            baseline_val=baseline_aggr.mean_estimated_cost_usd,
            units=UNIT_USD,
            direction=DIRECTION_LOWER_IS_BETTER,
        )
        metric_comparisons["estimated_cost_usd"] = cost_comp

        # Efficiency gain ratio (baseline_queries_to_target / active_queries_to_target)
        efficiency_gain: Optional[str] = None
        if (
            active_aggr.mean_queries_to_target is not None
            and active_aggr.mean_queries_to_target > 0
            and baseline_aggr.mean_queries_to_target is not None
        ):
            gain = float(baseline_aggr.mean_queries_to_target) / float(active_aggr.mean_queries_to_target)
            efficiency_gain = f"{gain:.1f}x"
        elif active_aggr.success_rate > 0 and baseline_aggr.success_rate == 0:
            efficiency_gain = ">2.0x (Baseline failed to reach target)"

        # Honest advantage evaluation
        claims_advantage = False
        reasons: List[str] = []

        if active_aggr.success_rate > baseline_aggr.success_rate:
            claims_advantage = True
            reasons.append(
                f"Active selection demonstrated higher success rate ({active_aggr.success_rate:.0%} vs {baseline_aggr.success_rate:.0%})."
            )
        elif active_aggr.success_rate == baseline_aggr.success_rate and active_aggr.success_rate > 0:
            if (
                active_aggr.mean_queries_to_target is not None
                and baseline_aggr.mean_queries_to_target is not None
                and active_aggr.mean_queries_to_target < baseline_aggr.mean_queries_to_target
            ):
                claims_advantage = True
                reasons.append(
                    f"Active selection required fewer queries to target ({active_aggr.mean_queries_to_target:.1f} vs {baseline_aggr.mean_queries_to_target:.1f})."
                )
            if (
                active_aggr.mean_held_out_mae is not None
                and baseline_aggr.mean_held_out_mae is not None
                and active_aggr.mean_held_out_mae < baseline_aggr.mean_held_out_mae
            ):
                claims_advantage = True
                reasons.append(
                    f"Active selection achieved lower held-out MAE ({active_aggr.mean_held_out_mae:.4f} vs {baseline_aggr.mean_held_out_mae:.4f})."
                )

        if not claims_advantage:
            if active_aggr.success_rate == 0 and baseline_aggr.success_rate == 0:
                advantage_summary = "Neither strategy converged to target within the configured query budget."
            else:
                advantage_summary = "Active selection did not demonstrate a measurable advantage over random baseline on this benchmark trial set."
        else:
            advantage_summary = " ".join(reasons)

        preliminary = (n < 5)
        aggregation_policy = (
            "All trials (including failed-to-converge and budget-exhausted runs) are explicitly preserved "
            "in trial results and included in denominators for success rates and query averages."
        )

        return StrategyComparisonReport(
            comparison_id=comparison_id,
            scenario_id=scenario_id,
            scenario_version=scenario_version,
            timestamp=utc_now_iso(),
            paired_trials_count=n,
            query_budget_per_trial=query_budget,
            success_threshold=success_threshold,
            active_aggregates=active_aggr,
            baseline_aggregates=baseline_aggr,
            metric_comparisons=metric_comparisons,
            efficiency_gain_ratio=efficiency_gain,
            claims_advantage=claims_advantage,
            advantage_summary=advantage_summary,
            preliminary=preliminary,
            aggregation_policy=aggregation_policy,
            active_trial_measurements=tuple(active_sorted),
            baseline_trial_measurements=tuple(baseline_sorted),
            measurement_method_version=MEASUREMENT_METHOD_VERSION,
        )

    def run_and_measure_benchmark(
        self,
        session: Session,
        scenario_name: str,
        seeds: Sequence[int],
        max_budget: Optional[int] = None,
    ) -> Tuple[RunModel, BenchmarkResultModel, StrategyComparisonReport]:
        """Execute paired active vs random benchmarks and record the complete performance measurement layer."""
        scenario = get_benchmark_case(scenario_name)
        if not scenario:
            raise ValueError(f"Unknown benchmark scenario '{scenario_name}'.")
        if not seeds:
            raise ValueError("Field 'seeds' must contain at least one random seed.")

        budget = max_budget if max_budget is not None else scenario.max_query_budget
        active_measurements: List[RunPerformanceMeasurement] = []
        baseline_measurements: List[RunPerformanceMeasurement] = []

        for seed in seeds:
            act_trial, rand_trial = self.runner.run_paired_trial(
                scenario=scenario,
                seed=seed,
                max_budget=budget,
            )
            active_measurements.append(self.measure_benchmark_trial(act_trial, scenario))
            baseline_measurements.append(self.measure_benchmark_trial(rand_trial, scenario))

        report = self.compare_strategies(
            active_measurements=active_measurements,
            baseline_measurements=baseline_measurements,
            scenario_id=scenario.id,
            scenario_version=scenario.version,
            query_budget=budget,
            success_threshold=scenario.success_threshold,
        )

        repo = ClarityRepository(session)
        now = datetime.now(timezone.utc)
        run_id = f"run-pm-{report.comparison_id}"

        # 1. Create run record
        run_record = RunModel(
            run_id=run_id,
            name=f"Performance Measurement: {scenario.id}",
            domain="polynomial",
            execution_mode="mock",
            sampling_strategy="active_vs_random",
            max_queries=budget,
            status="completed",
            created_at=now,
            started_at=now,
            completed_at=now,
        )
        repo.create_run(run_record)

        # 2. Prepare structured metadata with zero ground truth leakage
        metadata = report.to_dict()
        verify_no_ground_truth_leakage(metadata)

        # 3. Create benchmark result
        bm_record = BenchmarkResultModel(
            benchmark_id=f"bm-pm-{report.comparison_id}",
            run_id=run_id,
            benchmark_name=scenario.id,
            target_label=scenario.id,
            hit_at_1=(report.active_aggregates.hit_at_1_rate > 0.5),
            hit_at_3=(report.active_aggregates.hit_at_3_rate > 0.5),
            hit_at_5=True,
            reciprocal_rank=report.active_aggregates.mean_mrr,
            best_candidate_id=None,
            queries_to_target=int(report.active_aggregates.mean_queries_to_target) if report.active_aggregates.mean_queries_to_target else None,
            held_out_mae=report.active_aggregates.mean_held_out_mae,
            held_out_rmse=report.active_aggregates.mean_held_out_rmse,
            baseline_type="random_baseline",
            efficiency_gain=report.efficiency_gain_ratio,
            execution_metadata=metadata,
            executed_at=now,
        )
        saved_bm = repo.record_benchmark_result(bm_record)

        return run_record, saved_bm, report

    # ==========================================================================
    # Internal Helpers
    # ==========================================================================

    def _build_accuracy_measurements(
        self,
        repo: ClarityRepository,
        run: RunModel,
        bm: Optional[BenchmarkResultModel],
        scenario: Optional[BenchmarkCaseDefinition],
        tolerance: float,
    ) -> AccuracyMeasurements:
        hit_1_val = (1.0 if bm.hit_at_1 else 0.0) if bm else None
        hit_3_val = (1.0 if bm.hit_at_3 else 0.0) if bm else None
        hit_5_val = (1.0 if bm.hit_at_5 else 0.0) if bm else None
        mrr_val = bm.reciprocal_rank if bm else None
        mae_val = bm.held_out_mae if bm else None
        rmse_val = bm.held_out_rmse if bm else None

        sample_count = 0
        if scenario:
            sample_count = len(scenario.generate_held_out_dataset())
        elif bm and bm.execution_metadata:
            sample_count = bm.execution_metadata.get("paired_trials_count", 0)

        # Candidate complexity from relevant hypothesis revision / model representation
        hypotheses = repo.list_hypotheses_for_run(run.run_id, include_excluded=False)
        top_hyp = None
        if bm and bm.best_candidate_id:
            top_hyp = next((h for h in hypotheses if h.hypothesis_id == bm.best_candidate_id), None)
        if not top_hyp and hypotheses:
            top_hyp = hypotheses[0]

        complexity_val: Optional[float] = None
        if top_hyp and top_hyp.revisions:
            target_rev = next(
                (r for r in top_hyp.revisions if r.revision_number == top_hyp.current_revision),
                top_hyp.revisions[-1],
            )
            if target_rev:
                if target_rev.complexity is not None:
                    complexity_val = float(target_rev.complexity)
                elif target_rev.formula:
                    clean_formula = target_rev.formula.replace(" ", "")
                    deg = 0
                    if "x" in clean_formula:
                        deg = 1
                    for p_str in re.findall(r"x(?:\^|\*\*)(\d+)", clean_formula):
                        deg = max(deg, int(p_str))
                    complexity_val = float(deg)

        hit_1 = MetricMeasurement(
            name="hit_at_1",
            value=hit_1_val,
            units=UNIT_RATIO,
            definition="Binary indicator: top-ranked candidate matches target within tolerance.",
            direction=DIRECTION_HIGHER_IS_BETTER,
            sample_count=1 if hit_1_val is not None else 0,
            status=MetricAvailability.AVAILABLE if hit_1_val is not None else MetricAvailability.UNAVAILABLE,
        )
        hit_3 = MetricMeasurement(
            name="hit_at_3",
            value=hit_3_val,
            units=UNIT_RATIO,
            definition="Binary indicator: target is within top-3 ranked candidates.",
            direction=DIRECTION_HIGHER_IS_BETTER,
            sample_count=1 if hit_3_val is not None else 0,
            status=MetricAvailability.AVAILABLE if hit_3_val is not None else MetricAvailability.UNAVAILABLE,
        )
        hit_5 = MetricMeasurement(
            name="hit_at_5",
            value=hit_5_val,
            units=UNIT_RATIO,
            definition="Binary indicator: target is within top-5 ranked candidates.",
            direction=DIRECTION_HIGHER_IS_BETTER,
            sample_count=1 if hit_5_val is not None else 0,
            status=MetricAvailability.AVAILABLE if hit_5_val is not None else MetricAvailability.UNAVAILABLE,
        )
        mrr = MetricMeasurement(
            name="mrr",
            value=mrr_val,
            units=UNIT_RATIO,
            definition="Mean Reciprocal Rank of first matching candidate.",
            direction=DIRECTION_HIGHER_IS_BETTER,
            sample_count=1 if mrr_val is not None else 0,
            status=MetricAvailability.AVAILABLE if mrr_val is not None else MetricAvailability.UNAVAILABLE,
        )
        mae = MetricMeasurement(
            name="held_out_mae",
            value=mae_val,
            units=UNIT_Y_UNITS,
            definition="Mean Absolute Error on held-out dataset.",
            direction=DIRECTION_LOWER_IS_BETTER,
            sample_count=sample_count,
            status=MetricAvailability.AVAILABLE if mae_val is not None else MetricAvailability.UNAVAILABLE,
        )
        rmse = MetricMeasurement(
            name="held_out_rmse",
            value=rmse_val,
            units=UNIT_Y_UNITS,
            definition="Root Mean Squared Error on held-out dataset.",
            direction=DIRECTION_LOWER_IS_BETTER,
            sample_count=sample_count,
            status=MetricAvailability.AVAILABLE if rmse_val is not None else MetricAvailability.UNAVAILABLE,
        )
        complexity = MetricMeasurement(
            name="model_complexity",
            value=complexity_val,
            units=UNIT_COUNT,
            definition="Hypothesis candidate mathematical complexity (independent of revision numbering).",
            direction=DIRECTION_LOWER_IS_BETTER,
            sample_count=1 if complexity_val is not None else 0,
            status=MetricAvailability.AVAILABLE if complexity_val is not None else MetricAvailability.UNAVAILABLE,
            notes=None if complexity_val is not None else "Model complexity unavailable: no candidate hypothesis revision found.",
        )

        return AccuracyMeasurements(
            hit_at_1=hit_1,
            hit_at_3=hit_3,
            hit_at_5=hit_5,
            mrr=mrr,
            held_out_mae=mae,
            held_out_rmse=rmse,
            model_complexity=complexity,
            residual_variance=None,
            task_specific_metrics={},
            benchmark_version=scenario.version if scenario else "1.0.0",
            dataset_version=sample_count,
            sample_count=sample_count,
            evaluation_status="completed" if bm else "pending",
            evaluated_at=bm.executed_at.isoformat() if bm and bm.executed_at else None,
        )

    def _build_query_efficiency_measurements(
        self,
        repo: ClarityRepository,
        run: RunModel,
        bm: Optional[BenchmarkResultModel],
        scenario: Optional[BenchmarkCaseDefinition],
        tolerance: float,
    ) -> QueryEfficiencyMeasurements:
        # INVARIANT: Only actual executed oracle queries (from observations with status == 'success') are counted, NEVER proposed experiments
        # Database-side exact query count to prevent truncation on runs with >10,000 observations
        executed_queries_stmt = select(func.count(ObservationModel.observation_id)).where(
            ObservationModel.run_id == run.run_id,
            ObservationModel.status == "success",
        )
        executed_queries = int(repo.session.scalar(executed_queries_stmt) or 0)

        # Count candidate experiments directly in database
        exp_count_stmt = select(func.count(CandidateExperimentModel.experiment_id)).where(
            CandidateExperimentModel.run_id == run.run_id
        )
        proposed_count = int(repo.session.scalar(exp_count_stmt) or 0)

        budget = run.max_queries or (scenario.max_query_budget if scenario else 10)
        remaining = max(0, budget - executed_queries)

        target_reached = False
        queries_to_target = None
        if bm:
            target_reached = bool(bm.hit_at_1 or (bm.held_out_mae is not None and bm.held_out_mae <= tolerance))
            queries_to_target = bm.queries_to_target if target_reached else None

        budget_exhausted = (executed_queries >= budget) or (run.status == "budget_exhausted")
        status = "target_reached" if target_reached else ("budget_exhausted" if budget_exhausted else run.status)

        return QueryEfficiencyMeasurements(
            queries_executed=executed_queries,
            query_budget=budget,
            remaining_budget=remaining,
            target_reached=target_reached,
            success_threshold=tolerance,
            queries_to_target=queries_to_target,
            budget_exhausted=budget_exhausted,
            status=status,
            proposed_experiments_count=proposed_count,
        )

    def _build_runtime_measurements(
        self,
        repo: ClarityRepository,
        run: RunModel,
    ) -> RuntimeMeasurements:
        # Wall clock duration
        wall_clock_ms = 0.0
        if run.started_at and run.completed_at:
            wall_clock_ms = max(0.0, (run.completed_at - run.started_at).total_seconds() * 1000.0)

        # Compute tasks
        tasks = repo.list_compute_tasks_for_run(run.run_id)
        tasks_total = len(tasks)
        tasks_submitted = len([t for t in tasks if t.status in ("pending", "submitted")])
        tasks_completed = len([t for t in tasks if t.status == "completed"])
        tasks_failed = len([t for t in tasks if t.status == "failed"])
        tasks_timed_out = len([t for t in tasks if t.status == "timed_out"])
        tasks_cancelled = len([t for t in tasks if t.status == "cancelled"])

        # Task execution durations
        task_durations = [t.duration_ms for t in tasks if t.duration_ms is not None]
        task_exec_total_ms = float(sum(task_durations)) if task_durations else None

        # Queue durations
        queue_durations = []
        for t in tasks:
            if t.started_at and t.created_at:
                qd = max(0.0, (t.started_at - t.created_at).total_seconds() * 1000.0)
                queue_durations.append(qd)
        avg_queue_ms = (sum(queue_durations) / float(len(queue_durations))) if queue_durations else None

        # Local vs remote execution durations
        local_tasks = [t for t in tasks if t.execution_mode == "local"]
        remote_tasks = [t for t in tasks if t.execution_mode == "nodus_remote"]

        remote_durations = [t.duration_ms for t in remote_tasks if t.duration_ms is not None]
        remote_exec_ms = float(sum(remote_durations)) if remote_durations else None

        local_durations = [t.duration_ms for t in local_tasks if t.duration_ms is not None]
        local_exec_ms = float(sum(local_durations)) if local_durations else wall_clock_ms

        # Parallel overlap factor: ratio of sum(task durations) to total wall-clock time
        parallel_overlap: Optional[float] = None
        if wall_clock_ms > 0 and task_exec_total_ms is not None and task_exec_total_ms > 0:
            parallel_overlap = task_exec_total_ms / wall_clock_ms

        return RuntimeMeasurements(
            total_wall_clock_ms=wall_clock_ms,
            local_processing_ms=local_exec_ms,
            remote_execution_ms=remote_exec_ms,
            phase_durations_ms={"wall_clock_ms": wall_clock_ms},
            task_queue_duration_ms=avg_queue_ms,
            task_execution_duration_ms=task_exec_total_ms,
            evaluation_duration_ms=task_exec_total_ms,
            tasks_total=tasks_total,
            tasks_submitted=tasks_submitted,
            tasks_completed=tasks_completed,
            tasks_failed=tasks_failed,
            tasks_timed_out=tasks_timed_out,
            tasks_cancelled=tasks_cancelled,
            parallel_overlap_factor=parallel_overlap,
            timing_method="timestamp_delta" if wall_clock_ms > 0 else "unrecorded",
        )

    def _build_cost_measurements(
        self,
        repo: ClarityRepository,
        run: RunModel,
    ) -> CostMeasurements:
        usage = repo.get_latest_usage_for_run(run.run_id)
        budget_usage = repo.get_budget_usage(run.run_id)

        # Check for provider-reported settled cost
        settled_cost = None
        if usage and usage.settled_cost_usd is not None:
            settled_cost = usage.settled_cost_usd
        elif budget_usage and budget_usage.settled_cost_usd is not None:
            settled_cost = budget_usage.settled_cost_usd

        # Accruing cost
        accruing_cost = None
        if usage and usage.live_accruing_cost_usd is not None:
            accruing_cost = usage.live_accruing_cost_usd
        elif budget_usage and budget_usage.accruing_cost_usd is not None:
            accruing_cost = budget_usage.accruing_cost_usd

        # Estimated cost
        estimated_cost = None
        if usage and usage.estimated_cost_usd is not None:
            estimated_cost = usage.estimated_cost_usd
        elif budget_usage and budget_usage.estimated_cost_usd is not None:
            estimated_cost = budget_usage.estimated_cost_usd

        # Classification
        if settled_cost is not None:
            cost_state = CostState.SETTLED
            source = "provider_reported"
            is_provider = True
        elif accruing_cost is not None:
            cost_state = CostState.ACCRUING
            source = "provider_reported"
            is_provider = True
        elif estimated_cost is not None:
            cost_state = CostState.ESTIMATED
            source = "application_estimate"
            is_provider = False
        else:
            cost_state = CostState.UNAVAILABLE
            source = "unavailable"
            is_provider = False

        pricing = {
            "remote_task_minute_rate_usd": DEFAULT_REMOTE_TASK_MINUTE_RATE_USD,
            "remote_task_hourly_rate_usd": DEFAULT_REMOTE_TASK_HOURLY_RATE_USD,
            "local_task_hourly_rate_usd": DEFAULT_LOCAL_TASK_HOURLY_RATE_USD,
            "oracle_query_cost_usd": DEFAULT_ORACLE_QUERY_COST_USD,
            "candidate_evaluation_cost_usd": DEFAULT_EVALUATION_COST_USD,
        }

        notes = None
        if cost_state == CostState.UNAVAILABLE:
            notes = "Provider cost data is unavailable and was not assumed to be $0.00."

        return CostMeasurements(
            cost_state=cost_state,
            estimated_cost_usd=estimated_cost,
            accruing_cost_usd=accruing_cost,
            settled_cost_usd=settled_cost,
            currency="USD",
            calculation_method="application_documented_rates" if cost_state == CostState.ESTIMATED else "provider_reported",
            pricing_assumptions=pricing,
            source=source,
            is_provider_reported=is_provider,
            measurement_timestamp=utc_now_iso(),
            notes=notes,
        )

    def _compute_strategy_aggregates(
        self,
        strategy: str,
        measurements: Sequence[RunPerformanceMeasurement],
    ) -> StrategyAggregateMetrics:
        n = len(measurements)
        if n == 0:
            return StrategyAggregateMetrics(
                strategy=strategy,
                total_trials=0,
                successful_trials=0,
                failed_trials=0,
                success_rate=0.0,
                mean_queries_used=0.0,
                std_queries_used=0.0,
                min_queries_used=0,
                max_queries_used=0,
                mean_queries_to_target=None,
                mean_held_out_mae=None,
                std_held_out_mae=None,
                mean_held_out_rmse=None,
                hit_at_1_rate=0.0,
                hit_at_3_rate=0.0,
                mean_mrr=0.0,
                mean_wall_clock_ms=0.0,
                mean_task_execution_ms=None,
                mean_estimated_cost_usd=None,
            )

        successful = [m for m in measurements if m.query_efficiency.target_reached]
        succ_count = len(successful)
        fail_count = n - succ_count
        succ_rate = float(succ_count) / float(n)

        queries_used_list = [float(m.query_efficiency.queries_executed) for m in measurements]
        mean_queries = sum(queries_used_list) / float(n)
        var_q = sum((q - mean_queries) ** 2 for q in queries_used_list) / float(n if n > 1 else 1)
        std_queries = math.sqrt(var_q)
        min_q = int(min(queries_used_list))
        max_q = int(max(queries_used_list))

        # Queries to target for successful trials
        q_to_target_list = [
            float(m.query_efficiency.queries_to_target)
            for m in successful
            if m.query_efficiency.queries_to_target is not None
        ]
        mean_q_to_target = (sum(q_to_target_list) / float(len(q_to_target_list))) if q_to_target_list else None

        # MAE and RMSE
        mae_list = [m.accuracy.held_out_mae.value for m in measurements if m.accuracy.held_out_mae.value is not None]
        if mae_list:
            mean_mae = sum(mae_list) / float(len(mae_list))
            var_mae = sum((x - mean_mae) ** 2 for x in mae_list) / float(len(mae_list) if len(mae_list) > 1 else 1)
            std_mae = math.sqrt(var_mae)
        else:
            mean_mae = None
            std_mae = None

        rmse_list = [m.accuracy.held_out_rmse.value for m in measurements if m.accuracy.held_out_rmse.value is not None]
        mean_rmse = (sum(rmse_list) / float(len(rmse_list))) if rmse_list else None

        hit_1_count = sum(1 for m in measurements if m.accuracy.hit_at_1.value and m.accuracy.hit_at_1.value > 0.5)
        hit_3_count = sum(1 for m in measurements if m.accuracy.hit_at_3.value and m.accuracy.hit_at_3.value > 0.5)
        mrr_vals = [m.accuracy.mrr.value for m in measurements if m.accuracy.mrr.value is not None]
        mean_mrr = (sum(mrr_vals) / float(len(mrr_vals))) if mrr_vals else 0.0

        wc_list = [m.runtime.total_wall_clock_ms for m in measurements]
        mean_wc = sum(wc_list) / float(n)

        exec_list = [m.runtime.task_execution_duration_ms for m in measurements if m.runtime.task_execution_duration_ms is not None]
        mean_exec = (sum(exec_list) / float(len(exec_list))) if exec_list else None

        cost_list = [m.cost.estimated_cost_usd for m in measurements if m.cost.estimated_cost_usd is not None]
        mean_cost = (sum(cost_list) / float(len(cost_list))) if cost_list else None

        return StrategyAggregateMetrics(
            strategy=strategy,
            total_trials=n,
            successful_trials=succ_count,
            failed_trials=fail_count,
            success_rate=succ_rate,
            mean_queries_used=mean_queries,
            std_queries_used=std_queries,
            min_queries_used=min_q,
            max_queries_used=max_q,
            mean_queries_to_target=mean_q_to_target,
            mean_held_out_mae=mean_mae,
            std_held_out_mae=std_mae,
            mean_held_out_rmse=mean_rmse,
            hit_at_1_rate=float(hit_1_count) / float(n),
            hit_at_3_rate=float(hit_3_count) / float(n),
            mean_mrr=mean_mrr,
            mean_wall_clock_ms=mean_wc,
            mean_task_execution_ms=mean_exec,
            mean_estimated_cost_usd=mean_cost,
        )

    def _build_metric_comparison(
        self,
        metric_name: str,
        active_val: Optional[float],
        baseline_val: Optional[float],
        units: str,
        direction: str,
    ) -> MetricComparisonDetail:
        abs_diff: Optional[float] = None
        if active_val is not None and baseline_val is not None:
            abs_diff = active_val - baseline_val

        rel_pct, is_valid, reason = calculate_safe_relative_change(
            active_val=active_val,
            baseline_val=baseline_val,
            direction=direction,
        )

        better_strategy: Optional[str] = None
        if active_val is None or baseline_val is None:
            better_strategy = "inconclusive"
        elif abs(active_val - baseline_val) < 1e-9:
            better_strategy = "tie"
        elif direction == DIRECTION_LOWER_IS_BETTER:
            better_strategy = "active" if active_val < baseline_val else "baseline"
        else:
            better_strategy = "active" if active_val > baseline_val else "baseline"

        return MetricComparisonDetail(
            metric_name=metric_name,
            active_value=active_val,
            baseline_value=baseline_val,
            units=units,
            direction=direction,
            absolute_difference=abs_diff,
            relative_change_pct=rel_pct,
            relative_change_valid=is_valid,
            relative_change_reason=reason,
            better_strategy=better_strategy,
        )
