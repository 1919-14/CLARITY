"""Reproducible Active vs Random Benchmark Runner for CLARITY.

Strict Trust Boundary:
- The runner coordinates paired trials between CLARITY's real active experiment selection
  and a seeded random-selection baseline under equivalent query budgets and conditions.
- Ground truth is held only by the Evaluator. The discovery engine, candidate generator,
  and selection planners operate as pure black boxes and never receive ground-truth objects.
- All trials, including budget-exhausted and non-converged runs, are preserved and accounted
  for explicitly in aggregates (no silent omission).
"""

from __future__ import annotations

import math
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple, Union

from sqlalchemy.orm import Session

from ..adapters.persistence.models import BenchmarkResultModel, RunModel
from ..adapters.persistence.repository import ClarityRepository
from ..domain.candidate_generator import HypothesisGenerationConfig
from ..domain.contracts import (
    ExperimentInput,
    HypothesisRepresentation,
    Observation,
    generate_id,
    utc_now_iso,
    verify_no_ground_truth_leakage,
)
from ..domain.discovery_contracts import (
    DiscoveryLifecycleStatus,
    DiscoveryRunConfig,
    DiscoveryRunResult,
    DiscoveryTerminationReason,
)
from ..domain.discovery_engine import DiscoveryEngine
from ..domain.experiment_selection import (
    ActiveExperimentSelector,
    ExperimentSelectionPlanner,
    RandomBaselinePlanner,
)
from ..domain.ranking_contracts import RankingPolicyConfig
from ..domain.selection_contracts import (
    ExperimentSelectionConfig,
    SelectionStrategy,
)
from .benchmark_cases import (
    BenchmarkCaseDefinition,
    PolynomialGroundTruthRule,
    get_benchmark_case,
    list_benchmark_scenarios,
)
from .benchmark_evaluator import BenchmarkEvaluator


# ==============================================================================
# 1. Benchmark Data Contracts
# ==============================================================================

@dataclass(frozen=True)
class BenchmarkTrialResult:
    """Record of a single discovery execution under a specific selection strategy."""

    trial_id: str
    strategy: str  # "active_selection" or "random_baseline"
    seed: int
    scenario_id: str
    scenario_version: str
    queries_used: int
    max_budget: int
    budget_exhausted: bool
    target_reached: bool
    queries_to_target: Optional[int]
    hit_at_1: bool
    hit_at_3: bool
    hit_at_5: bool
    mrr: float
    held_out_mae: Optional[float]
    held_out_rmse: Optional[float]
    top_candidate_formula: Optional[str]
    termination_reason: str
    runtime_ms: float
    selection_history: Tuple[Dict[str, Any], ...] = ()
    observations_history: Tuple[Dict[str, Any], ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "trial_id": self.trial_id,
            "strategy": self.strategy,
            "seed": self.seed,
            "scenario_id": self.scenario_id,
            "scenario_version": self.scenario_version,
            "queries_used": self.queries_used,
            "max_budget": self.max_budget,
            "budget_exhausted": self.budget_exhausted,
            "target_reached": self.target_reached,
            "queries_to_target": self.queries_to_target,
            "hit_at_1": self.hit_at_1,
            "hit_at_3": self.hit_at_3,
            "hit_at_5": self.hit_at_5,
            "mrr": round(self.mrr, 4),
            "held_out_mae": round(self.held_out_mae, 6) if self.held_out_mae is not None else None,
            "held_out_rmse": round(self.held_out_rmse, 6) if self.held_out_rmse is not None else None,
            "top_candidate_formula": self.top_candidate_formula,
            "termination_reason": self.termination_reason,
            "runtime_ms": round(self.runtime_ms, 2),
            "selection_history": [dict(s) for s in self.selection_history],
            "observations_history": [dict(o) for o in self.observations_history],
        }


@dataclass(frozen=True)
class StrategyAggregateStats:
    """Summary statistics for repeated trials of a selection strategy."""

    strategy: str
    total_trials: int
    successful_trials: int
    failed_trials: int
    success_rate: float
    mean_queries_used: float
    std_queries_used: float
    mean_queries_to_target: Optional[float]
    mean_held_out_mae: Optional[float]
    std_held_out_mae: Optional[float]
    mean_held_out_rmse: Optional[float]
    hit_at_1_rate: float
    hit_at_3_rate: float
    mean_mrr: float
    mean_runtime_ms: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "strategy": self.strategy,
            "total_trials": self.total_trials,
            "successful_trials": self.successful_trials,
            "failed_trials": self.failed_trials,
            "success_rate": round(self.success_rate, 4),
            "mean_queries_used": round(self.mean_queries_used, 2),
            "std_queries_used": round(self.std_queries_used, 2),
            "mean_queries_to_target": round(self.mean_queries_to_target, 2) if self.mean_queries_to_target is not None else None,
            "mean_held_out_mae": round(self.mean_held_out_mae, 6) if self.mean_held_out_mae is not None else None,
            "std_held_out_mae": round(self.std_held_out_mae, 6) if self.std_held_out_mae is not None else None,
            "mean_held_out_rmse": round(self.mean_held_out_rmse, 6) if self.mean_held_out_rmse is not None else None,
            "hit_at_1_rate": round(self.hit_at_1_rate, 4),
            "hit_at_3_rate": round(self.hit_at_3_rate, 4),
            "mean_mrr": round(self.mean_mrr, 4),
            "mean_runtime_ms": round(self.mean_runtime_ms, 2),
        }


@dataclass(frozen=True)
class BenchmarkComparisonSummary:
    """Rigorous comparison between active selection and the random baseline."""

    comparison_id: str
    scenario_id: str
    scenario_version: str
    timestamp: str
    paired_trials_count: int
    active_stats: StrategyAggregateStats
    random_stats: StrategyAggregateStats
    efficiency_gain: Optional[str]
    mae_reduction_ratio: Optional[float]
    claims_advantage: bool
    advantage_summary: str
    preliminary: bool
    aggregation_policy: str
    metric_directions: Dict[str, str]
    active_trials: Tuple[BenchmarkTrialResult, ...] = ()
    random_trials: Tuple[BenchmarkTrialResult, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "comparison_id": self.comparison_id,
            "scenario_id": self.scenario_id,
            "scenario_version": self.scenario_version,
            "timestamp": self.timestamp,
            "paired_trials_count": self.paired_trials_count,
            "active_stats": self.active_stats.to_dict(),
            "random_stats": self.random_stats.to_dict(),
            "efficiency_gain": self.efficiency_gain,
            "mae_reduction_ratio": round(self.mae_reduction_ratio, 4) if self.mae_reduction_ratio is not None else None,
            "claims_advantage": self.claims_advantage,
            "advantage_summary": self.advantage_summary,
            "preliminary": self.preliminary,
            "aggregation_policy": self.aggregation_policy,
            "metric_directions": dict(self.metric_directions),
            "active_trials": [t.to_dict() for t in self.active_trials],
            "random_trials": [t.to_dict() for t in self.random_trials],
        }


# ==============================================================================
# 2. Benchmark Computation Helpers
# ==============================================================================

def _compute_held_out_error(
    candidate: HypothesisRepresentation,
    held_out_dataset: Sequence[Dict[str, Any]],
) -> Tuple[Optional[float], Optional[float]]:
    """Compute exact held-out MAE and RMSE for a candidate model against held-out ground truth."""
    if not held_out_dataset:
        return None, None

    # Extract polynomial coefficients
    params = candidate.parameters or {}
    coeffs = PolynomialGroundTruthRule._extract_coefficients(params)
    if not coeffs:
        return None, None

    errors = []
    for sample in held_out_dataset:
        x_val = float(sample["input"]["x"])
        expected_y = float(sample["expected_output"]["y"])
        pred_y = 0.0
        for power, coef in coeffs.items():
            pred_y += coef * (x_val ** power)
        errors.append(abs(pred_y - expected_y))

    mae = sum(errors) / float(len(errors))
    rmse = math.sqrt(sum(e ** 2 for e in errors) / float(len(errors)))
    return mae, rmse


def _compute_aggregate_stats(
    strategy: str,
    trials: Sequence[BenchmarkTrialResult],
) -> StrategyAggregateStats:
    """Compute aggregate statistical indicators over repeated trials."""
    n = len(trials)
    if n == 0:
        return StrategyAggregateStats(
            strategy=strategy,
            total_trials=0,
            successful_trials=0,
            failed_trials=0,
            success_rate=0.0,
            mean_queries_used=0.0,
            std_queries_used=0.0,
            mean_queries_to_target=None,
            mean_held_out_mae=None,
            std_held_out_mae=None,
            mean_held_out_rmse=None,
            hit_at_1_rate=0.0,
            hit_at_3_rate=0.0,
            mean_mrr=0.0,
            mean_runtime_ms=0.0,
        )

    successful = [t for t in trials if t.target_reached]
    success_count = len(successful)
    fail_count = n - success_count
    success_rate = float(success_count) / float(n)

    queries_used_vals = [float(t.queries_used) for t in trials]
    mean_queries_used = sum(queries_used_vals) / float(n)
    var_queries = sum((q - mean_queries_used) ** 2 for q in queries_used_vals) / float(n if n > 1 else 1)
    std_queries_used = math.sqrt(var_queries)

    # Queries to target for successful trials
    success_queries = [float(t.queries_to_target) for t in successful if t.queries_to_target is not None]
    mean_queries_to_target = (sum(success_queries) / float(len(success_queries))) if success_queries else None

    # Held out MAE & RMSE
    mae_vals = [t.held_out_mae for t in trials if t.held_out_mae is not None]
    if mae_vals:
        mean_mae = sum(mae_vals) / float(len(mae_vals))
        var_mae = sum((m - mean_mae) ** 2 for m in mae_vals) / float(len(mae_vals) if len(mae_vals) > 1 else 1)
        std_mae = math.sqrt(var_mae)
    else:
        mean_mae = None
        std_mae = None

    rmse_vals = [t.held_out_rmse for t in trials if t.held_out_rmse is not None]
    mean_rmse = (sum(rmse_vals) / float(len(rmse_vals))) if rmse_vals else None

    hit_1_count = sum(1 for t in trials if t.hit_at_1)
    hit_3_count = sum(1 for t in trials if t.hit_at_3)
    mean_mrr = sum(t.mrr for t in trials) / float(n)

    runtimes = [t.runtime_ms for t in trials]
    mean_runtime = sum(runtimes) / float(n)

    return StrategyAggregateStats(
        strategy=strategy,
        total_trials=n,
        successful_trials=success_count,
        failed_trials=fail_count,
        success_rate=success_rate,
        mean_queries_used=mean_queries_used,
        std_queries_used=std_queries_used,
        mean_queries_to_target=mean_queries_to_target,
        mean_held_out_mae=mean_mae,
        std_held_out_mae=std_mae,
        mean_held_out_rmse=mean_rmse,
        hit_at_1_rate=float(hit_1_count) / float(n),
        hit_at_3_rate=float(hit_3_count) / float(n),
        mean_mrr=mean_mrr,
        mean_runtime_ms=mean_runtime,
    )


# ==============================================================================
# 3. Active vs Random Benchmark Runner
# ==============================================================================

class ActiveRandomBenchmarkRunner:
    """Executes fair, reproducible benchmark comparisons between active and random selection."""

    def __init__(self, tolerance_override: Optional[float] = None):
        self._tolerance_override = tolerance_override

    def run_single_trial(
        self,
        scenario: BenchmarkCaseDefinition,
        strategy: str,  # "active_selection" or "random_baseline"
        seed: int,
        max_budget: Optional[int] = None,
    ) -> BenchmarkTrialResult:
        """Run an isolated discovery trial with the given selection strategy.

        Budget accounting
        -----------------
        ``max_budget`` (or ``scenario.max_query_budget`` when None) is the *total*
        oracle-call budget for the entire trial, including initial seed queries.

        Initial inputs are capped to ``budget`` items *before* any oracle calls are
        made, so the oracle can never be called more than ``budget`` times regardless
        of how many initial inputs the scenario declares.  Any initial inputs beyond
        the cap are silently skipped (their count is available as the local variable
        ``skipped_initial_inputs`` during execution).  The engine then receives
        ``budget - initial_count`` remaining calls.  ``queries_used`` is always
        reported as ``initial_count + engine_queries_used``.
        """
        trial_id = generate_id("trial")
        budget = max_budget if max_budget is not None else scenario.max_query_budget
        tolerance = self._tolerance_override or scenario.success_threshold

        # 1. Instantiate the isolated oracle (ground-truth kept strictly inside)
        oracle = scenario.create_oracle(noise_seed=seed)

        # 2. Gather equivalent initial observations, counting them against the budget.
        #
        # BUDGET SAFETY: cap the initial-input batch to at most `budget` items BEFORE
        # calling oracle.query(). This ensures the oracle is never called more than
        # `budget` times in total, even if the scenario declares more initial inputs
        # than the configured budget. Any inputs beyond the cap are skipped; their
        # count is tracked in `skipped_initial_inputs` for transparency.
        inputs_within_budget = scenario.initial_inputs[:budget]
        skipped_initial_inputs: int = len(scenario.initial_inputs) - len(inputs_within_budget)

        initial_obs: List[Observation] = []
        for inp in inputs_within_budget:
            exp_inp = ExperimentInput(input_id=generate_id("inp-init"), values=inp)
            obs = oracle.query(exp_inp)
            initial_obs.append(obs)

        initial_query_count: int = oracle.query_count  # == len(inputs_within_budget) for valid inputs

        # Remaining budget for the discovery engine after initial queries are consumed.
        engine_budget = max(0, budget - initial_query_count)

        # 3. Configure the selection planner

        if strategy == "active_selection":
            planner = ExperimentSelectionPlanner(
                config=ExperimentSelectionConfig(
                    strategy=SelectionStrategy.ACTIVE_DISAGREEMENT,
                    min_eligible_hypotheses=2,
                    exclude_queried_inputs=True,
                    allow_candidate_specifications=True,
                )
            )
        elif strategy == "random_baseline":
            planner = RandomBaselinePlanner(seed=seed)
        else:
            raise ValueError(f"Unknown strategy '{strategy}'. Must be 'active_selection' or 'random_baseline'.")

        # 4. Build discovery configuration using the scenario's declared search-space capability.
        # IMPORTANT: max_degree comes from scenario.max_hypothesis_degree — a public field
        # set independently of the hidden ground truth — NOT from scenario.ground_truth.target_parameters.
        engine_config = DiscoveryRunConfig(
            max_query_budget=engine_budget,
            max_iterations=engine_budget * 2 if engine_budget > 0 else 1,
            domain_bounds=scenario.domain_bounds,
            hypothesis_config=HypothesisGenerationConfig(
                min_degree=0,
                max_degree=scenario.max_hypothesis_degree,
                candidate_count=5,
                seed=seed,
            ),
            ranking_policy=RankingPolicyConfig(
                min_samples_for_plausibility=2,
                performance_acceptance_threshold=1e6,
                max_tolerated_contradictions=99,
            ),
            experiment_pool_size=10,
            experiment_seed=seed,
        )

        # 5. Execute discovery loop
        start_time = time.perf_counter()
        engine = DiscoveryEngine(oracle=oracle, config=engine_config, planner=planner)
        run_result: DiscoveryRunResult = engine.run(initial_observations=initial_obs)
        runtime_ms = (time.perf_counter() - start_time) * 1000.0

        # Total oracle calls = initial queries (capped) + engine-issued queries.
        # Overrun is structurally impossible: inputs_within_budget = initial_inputs[:budget]
        # and engine_budget = budget - initial_query_count, so total <= budget by construction.
        total_queries_used: int = initial_query_count + run_result.query_accounting.attempted_queries

        # 6. Extract candidates and evaluate against ground truth (EVALUATOR PATH)
        ranked_candidates: List[HypothesisRepresentation] = []
        if run_result.ranking and run_result.ranking.ranked_candidates:
            for rc in run_result.ranking.ranked_candidates:
                hyp = next((h for h in run_result.hypotheses if h.hypothesis_id == rc.hypothesis_id), None)
                if hyp:
                    ranked_candidates.append(hyp)
        elif run_result.hypotheses:
            ranked_candidates = list(run_result.hypotheses)

        evaluator = BenchmarkEvaluator(ground_truth=scenario.ground_truth, tolerance=tolerance)
        eval_report = evaluator.evaluate_candidates(
            run_id=run_result.run_id,
            ranked_candidates=ranked_candidates,
            queries_executed=total_queries_used,
        )

        # 7. Held-out predictive error on reproducible held-out points
        held_out_dataset = scenario.generate_held_out_dataset()
        held_out_mae: Optional[float] = None
        held_out_rmse: Optional[float] = None
        top_formula: Optional[str] = None
        if ranked_candidates:
            top_cand = ranked_candidates[0]
            top_formula = top_cand.formula
            mae, rmse = _compute_held_out_error(top_cand, held_out_dataset)
            held_out_mae = mae
            held_out_rmse = rmse

        # 8. Check target achievement
        target_reached = eval_report.hit_at_1 or (held_out_mae is not None and held_out_mae <= tolerance)
        queries_to_target = total_queries_used if target_reached else None

        # 9. Format selection and observation history
        selection_history = []
        for exp in run_result.experiment_history:
            diag = exp.selection_diagnostics or {}
            selection_history.append({
                "sequence": exp.sequence,
                "input_values": dict(exp.input_values) if exp.input_values is not None else {},
                "outcome": exp.outcome.value,
                "strategy": exp.strategy,
                "reason_code": diag.get("reason_code"),
            })

        obs_history = []
        for obs in run_result.observations:
            obs_history.append({
                "observation_id": obs.observation_id,
                "input_values": dict(obs.input_values) if obs.input_values is not None else {},
                "output_values": dict(obs.output_values) if obs.output_values is not None else {},
                "status": obs.status.value,
            })

        term_reason = run_result.termination_reason.value if run_result.termination_reason else "unknown"
        budget_exhausted = (total_queries_used >= budget) or (
            run_result.status == DiscoveryLifecycleStatus.TERMINATED_BUDGET_EXHAUSTED
        )

        return BenchmarkTrialResult(
            trial_id=trial_id,
            strategy=strategy,
            seed=seed,
            scenario_id=scenario.id,
            scenario_version=scenario.version,
            queries_used=total_queries_used,
            max_budget=budget,
            budget_exhausted=budget_exhausted,
            target_reached=target_reached,
            queries_to_target=queries_to_target,
            hit_at_1=eval_report.hit_at_1,
            hit_at_3=eval_report.hit_at_3,
            hit_at_5=eval_report.hit_at_5,
            mrr=eval_report.reciprocal_rank,
            held_out_mae=held_out_mae,
            held_out_rmse=held_out_rmse,
            top_candidate_formula=top_formula,
            termination_reason=term_reason,
            runtime_ms=runtime_ms,
            selection_history=tuple(selection_history),
            observations_history=tuple(obs_history),
        )

    def run_paired_trial(
        self,
        scenario: BenchmarkCaseDefinition,
        seed: int,
        max_budget: Optional[int] = None,
    ) -> Tuple[BenchmarkTrialResult, BenchmarkTrialResult]:
        """Run a paired comparison under equivalent conditions and seed."""
        active_trial = self.run_single_trial(
            scenario=scenario,
            strategy="active_selection",
            seed=seed,
            max_budget=max_budget,
        )
        random_trial = self.run_single_trial(
            scenario=scenario,
            strategy="random_baseline",
            seed=seed,
            max_budget=max_budget,
        )
        return active_trial, random_trial

    def run_benchmark(
        self,
        scenario_name: str,
        seeds: Sequence[int],
        max_budget: Optional[int] = None,
    ) -> BenchmarkComparisonSummary:
        """Execute paired active vs random benchmark across multiple random seeds."""
        scenario = get_benchmark_case(scenario_name)
        if not scenario:
            raise ValueError(f"Unknown benchmark scenario '{scenario_name}'.")
        if not seeds:
            raise ValueError("Parameter 'seeds' must contain at least one random seed.")

        comparison_id = generate_id("comp")
        active_trials: List[BenchmarkTrialResult] = []
        random_trials: List[BenchmarkTrialResult] = []

        for seed in seeds:
            act_trial, rand_trial = self.run_paired_trial(
                scenario=scenario,
                seed=seed,
                max_budget=max_budget,
            )
            active_trials.append(act_trial)
            random_trials.append(rand_trial)

        active_stats = _compute_aggregate_stats("active_selection", active_trials)
        random_stats = _compute_aggregate_stats("random_baseline", random_trials)

        # Efficiency gain ratio (baseline / active for successful trials)
        efficiency_gain: Optional[str] = None
        if (
            active_stats.mean_queries_to_target is not None
            and active_stats.mean_queries_to_target > 0
            and random_stats.mean_queries_to_target is not None
        ):
            gain = float(random_stats.mean_queries_to_target) / float(active_stats.mean_queries_to_target)
            efficiency_gain = f"{gain:.1f}x"
        elif active_stats.success_rate > 0 and random_stats.success_rate == 0:
            efficiency_gain = ">2.0x (Baseline failed to converge)"

        # MAE reduction ratio
        mae_reduction: Optional[float] = None
        if (
            active_stats.mean_held_out_mae is not None
            and random_stats.mean_held_out_mae is not None
            and random_stats.mean_held_out_mae > 0
        ):
            mae_reduction = active_stats.mean_held_out_mae / random_stats.mean_held_out_mae

        # Honest advantage determination
        claims_advantage = False
        summary_reasons = []

        if active_stats.success_rate > random_stats.success_rate:
            claims_advantage = True
            summary_reasons.append(
                f"Active selection achieved higher success rate ({active_stats.success_rate:.0%} vs {random_stats.success_rate:.0%})."
            )
        elif active_stats.success_rate == random_stats.success_rate and active_stats.success_rate > 0:
            if (
                active_stats.mean_queries_to_target is not None
                and random_stats.mean_queries_to_target is not None
                and active_stats.mean_queries_to_target < random_stats.mean_queries_to_target
            ):
                claims_advantage = True
                summary_reasons.append(
                    f"Active selection required fewer queries to target ({active_stats.mean_queries_to_target:.1f} vs {random_stats.mean_queries_to_target:.1f})."
                )
            if (
                active_stats.mean_held_out_mae is not None
                and random_stats.mean_held_out_mae is not None
                and active_stats.mean_held_out_mae < random_stats.mean_held_out_mae
            ):
                claims_advantage = True
                summary_reasons.append(
                    f"Active selection achieved lower held-out MAE ({active_stats.mean_held_out_mae:.4f} vs {random_stats.mean_held_out_mae:.4f})."
                )

        if not claims_advantage:
            if active_stats.success_rate == 0 and random_stats.success_rate == 0:
                advantage_summary = "Neither strategy converged within the configured query budget."
            else:
                advantage_summary = "Active selection did not demonstrate a measurable advantage over random selection on this benchmark trial set."
        else:
            advantage_summary = " ".join(summary_reasons)

        preliminary = (len(seeds) < 5)

        aggregation_policy = (
            "All trials (including non-converged and budget-exhausted runs) are preserved "
            "in individual results and included in denominators for success rates and query averages."
        )

        metric_directions = {
            "held_out_mae": "lower_is_better",
            "held_out_rmse": "lower_is_better",
            "queries_used": "lower_is_better",
            "queries_to_target": "lower_is_better",
            "hit_at_1": "higher_is_better",
            "hit_at_3": "higher_is_better",
            "mrr": "higher_is_better",
            "success_rate": "higher_is_better",
        }

        return BenchmarkComparisonSummary(
            comparison_id=comparison_id,
            scenario_id=scenario.id,
            scenario_version=scenario.version,
            timestamp=utc_now_iso(),
            paired_trials_count=len(seeds),
            active_stats=active_stats,
            random_stats=random_stats,
            efficiency_gain=efficiency_gain,
            mae_reduction_ratio=mae_reduction,
            claims_advantage=claims_advantage,
            advantage_summary=advantage_summary,
            preliminary=preliminary,
            aggregation_policy=aggregation_policy,
            metric_directions=metric_directions,
            active_trials=tuple(active_trials),
            random_trials=tuple(random_trials),
        )

    def run_and_persist(
        self,
        session: Session,
        scenario_name: str,
        seeds: Sequence[int],
        max_budget: Optional[int] = None,
        run_name: Optional[str] = None,
    ) -> Tuple[RunModel, BenchmarkResultModel, BenchmarkComparisonSummary]:
        """Execute benchmark comparison and persist results in the database."""
        summary = self.run_benchmark(
            scenario_name=scenario_name,
            seeds=seeds,
            max_budget=max_budget,
        )

        repo = ClarityRepository(session)
        now = datetime.now(timezone.utc)
        run_id = f"run-bm-{summary.comparison_id}"

        # Create RunModel for tracking the benchmark suite execution
        run_record = RunModel(
            run_id=run_id,
            name=run_name or f"Benchmark: Active vs Random ({summary.scenario_id})",
            domain="polynomial",
            execution_mode="mock",
            sampling_strategy="active_vs_random",
            max_queries=max_budget or 10,
            status="completed",
            created_at=now,
        )
        repo.create_run(run_record)

        # Prepare execution metadata
        metadata = {
            "comparison_id": summary.comparison_id,
            "scenario_id": summary.scenario_id,
            "scenario_version": summary.scenario_version,
            "paired_trials_count": summary.paired_trials_count,
            "active_stats": summary.active_stats.to_dict(),
            "random_stats": summary.random_stats.to_dict(),
            "efficiency_gain": summary.efficiency_gain,
            "mae_reduction_ratio": summary.mae_reduction_ratio,
            "claims_advantage": summary.claims_advantage,
            "advantage_summary": summary.advantage_summary,
            "preliminary": summary.preliminary,
            "aggregation_policy": summary.aggregation_policy,
            "metric_directions": summary.metric_directions,
            "active_trials": [t.to_dict() for t in summary.active_trials],
            "random_trials": [t.to_dict() for t in summary.random_trials],
        }

        # Verify boundary integrity
        verify_no_ground_truth_leakage(metadata)

        # Persist BenchmarkResultModel
        bm_record = BenchmarkResultModel(
            benchmark_id=f"bm-{summary.comparison_id}",
            run_id=run_id,
            benchmark_name=summary.scenario_id,
            target_label=summary.scenario_id,
            hit_at_1=(summary.active_stats.hit_at_1_rate > 0.5),
            hit_at_3=(summary.active_stats.hit_at_3_rate > 0.5),
            hit_at_5=True,
            reciprocal_rank=summary.active_stats.mean_mrr,
            best_candidate_id=summary.active_trials[0].top_candidate_formula if summary.active_trials else None,
            queries_to_target=int(summary.active_stats.mean_queries_to_target) if summary.active_stats.mean_queries_to_target else None,
            held_out_mae=summary.active_stats.mean_held_out_mae,
            held_out_rmse=summary.active_stats.mean_held_out_rmse,
            baseline_type="random_baseline",
            efficiency_gain=summary.efficiency_gain,
            execution_metadata=metadata,
            executed_at=now,
        )
        saved_bm = repo.record_benchmark_result(bm_record)

        return run_record, saved_bm, summary
