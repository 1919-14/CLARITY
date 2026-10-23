"""Performance measurement contracts for CLARITY (Issue #27).

Defines standardized data contracts and invariants for:
1. Scientific accuracy (Hit@K, MRR, held-out MAE/RMSE, complexity, uncertainty).
2. Query efficiency (executed oracle queries, budget, queries-to-success, failure tracking).
3. Runtime & execution behavior (wall-clock duration, phase durations, task counts, queue/exec timing).
4. Compute cost (estimated, accruing, settled, and unavailable states with pricing assumptions).
5. Active vs baseline strategy comparison (compatible conditions, absolute diffs, safe relative changes, aggregation policy).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

METRIC_DEFINITIONS_VERSION = "1.0.0"
MEASUREMENT_METHOD_VERSION = "1.0.0"

# Standard units
UNIT_DIMENSIONLESS = "dimensionless"
UNIT_RATIO = "ratio"
UNIT_Y_UNITS = "y_units"
UNIT_QUERIES = "queries"
UNIT_MILLISECONDS = "milliseconds"
UNIT_SECONDS = "seconds"
UNIT_USD = "USD"
UNIT_COUNT = "count"
UNIT_PERCENTAGE = "percentage"

# Directions of improvement
DIRECTION_LOWER_IS_BETTER = "lower_is_better"
DIRECTION_HIGHER_IS_BETTER = "higher_is_better"


class MetricAvailability(str, Enum):
    """Availability status of a measurement metric."""

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    NOT_APPLICABLE = "not_applicable"
    CALCULATION_INVALID = "calculation_invalid"


class CostState(str, Enum):
    """Cost data classification (reused from Issue #15 contracts)."""

    ESTIMATED = "estimated"
    ACCRUING = "accruing"
    SETTLED = "settled"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class MetricMeasurement:
    """Standardized record of a single measured metric."""

    name: str
    value: Optional[float]
    units: str
    definition: str
    version: str = METRIC_DEFINITIONS_VERSION
    direction: str = DIRECTION_LOWER_IS_BETTER
    sample_count: int = 0
    status: MetricAvailability = MetricAvailability.AVAILABLE
    uncertainty: Optional[float] = None
    notes: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "value": round(self.value, 6) if self.value is not None else None,
            "units": self.units,
            "definition": self.definition,
            "version": self.version,
            "direction": self.direction,
            "sample_count": self.sample_count,
            "status": self.status.value,
            "uncertainty": round(self.uncertainty, 6) if self.uncertainty is not None else None,
            "notes": self.notes,
        }


@dataclass(frozen=True)
class AccuracyMeasurements:
    """Scientific accuracy measurements for a discovery run or trial."""

    hit_at_1: MetricMeasurement
    hit_at_3: MetricMeasurement
    hit_at_5: MetricMeasurement
    mrr: MetricMeasurement
    held_out_mae: MetricMeasurement
    held_out_rmse: MetricMeasurement
    model_complexity: MetricMeasurement
    residual_variance: Optional[MetricMeasurement] = None
    task_specific_metrics: Dict[str, MetricMeasurement] = field(default_factory=dict)
    benchmark_version: str = "1.0.0"
    dataset_version: int = 0
    sample_count: int = 0
    evaluation_status: str = "completed"
    evaluated_at: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "hit_at_1": self.hit_at_1.to_dict(),
            "hit_at_3": self.hit_at_3.to_dict(),
            "hit_at_5": self.hit_at_5.to_dict(),
            "mrr": self.mrr.to_dict(),
            "held_out_mae": self.held_out_mae.to_dict(),
            "held_out_rmse": self.held_out_rmse.to_dict(),
            "model_complexity": self.model_complexity.to_dict(),
            "residual_variance": self.residual_variance.to_dict() if self.residual_variance else None,
            "task_specific_metrics": {k: v.to_dict() for k, v in self.task_specific_metrics.items()},
            "benchmark_version": self.benchmark_version,
            "dataset_version": self.dataset_version,
            "sample_count": self.sample_count,
            "evaluation_status": self.evaluation_status,
            "evaluated_at": self.evaluated_at,
        }


@dataclass(frozen=True)
class QueryEfficiencyMeasurements:
    """Oracle query resources and target achievement metrics."""

    queries_executed: int
    query_budget: int
    remaining_budget: int
    target_reached: bool
    success_threshold: float
    queries_to_target: Optional[int]
    budget_exhausted: bool
    status: str  # "target_reached", "budget_exhausted", "in_progress", "failed"
    proposed_experiments_count: int = 0  # Tracked separately from actual executed queries

    def to_dict(self) -> Dict[str, Any]:
        return {
            "queries_executed": self.queries_executed,
            "query_budget": self.query_budget,
            "remaining_budget": self.remaining_budget,
            "target_reached": self.target_reached,
            "success_threshold": self.success_threshold,
            "queries_to_target": self.queries_to_target,
            "budget_exhausted": self.budget_exhausted,
            "status": self.status,
            "proposed_experiments_count": self.proposed_experiments_count,
        }


@dataclass(frozen=True)
class RuntimeMeasurements:
    """Timing and execution behavior measurements with strict boundaries."""

    total_wall_clock_ms: float
    local_processing_ms: float
    remote_execution_ms: Optional[float]
    phase_durations_ms: Dict[str, float]
    task_queue_duration_ms: Optional[float]
    task_execution_duration_ms: Optional[float]
    evaluation_duration_ms: Optional[float]
    tasks_total: int
    tasks_submitted: int
    tasks_completed: int
    tasks_failed: int
    tasks_timed_out: int
    tasks_cancelled: int
    parallel_overlap_factor: Optional[float] = None
    timing_method: str = "monotonic_clock"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total_wall_clock_ms": round(self.total_wall_clock_ms, 2),
            "local_processing_ms": round(self.local_processing_ms, 2),
            "remote_execution_ms": round(self.remote_execution_ms, 2) if self.remote_execution_ms is not None else None,
            "phase_durations_ms": {k: round(v, 2) for k, v in self.phase_durations_ms.items()},
            "task_queue_duration_ms": round(self.task_queue_duration_ms, 2) if self.task_queue_duration_ms is not None else None,
            "task_execution_duration_ms": round(self.task_execution_duration_ms, 2) if self.task_execution_duration_ms is not None else None,
            "evaluation_duration_ms": round(self.evaluation_duration_ms, 2) if self.evaluation_duration_ms is not None else None,
            "tasks_total": self.tasks_total,
            "tasks_submitted": self.tasks_submitted,
            "tasks_completed": self.tasks_completed,
            "tasks_failed": self.tasks_failed,
            "tasks_timed_out": self.tasks_timed_out,
            "tasks_cancelled": self.tasks_cancelled,
            "parallel_overlap_factor": round(self.parallel_overlap_factor, 2) if self.parallel_overlap_factor is not None else None,
            "timing_method": self.timing_method,
        }


@dataclass(frozen=True)
class CostMeasurements:
    """Accurate compute cost measurements and classifications."""

    cost_state: CostState
    estimated_cost_usd: Optional[float]
    accruing_cost_usd: Optional[float]
    settled_cost_usd: Optional[float]
    currency: str = "USD"
    calculation_method: str = "application_documented_rates"
    pricing_assumptions: Dict[str, Any] = field(default_factory=dict)
    source: str = "application_estimate"
    is_provider_reported: bool = False
    measurement_timestamp: Optional[str] = None
    notes: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "cost_state": self.cost_state.value,
            "estimated_cost_usd": round(self.estimated_cost_usd, 4) if self.estimated_cost_usd is not None else None,
            "accruing_cost_usd": round(self.accruing_cost_usd, 4) if self.accruing_cost_usd is not None else None,
            "settled_cost_usd": round(self.settled_cost_usd, 4) if self.settled_cost_usd is not None else None,
            "currency": self.currency,
            "calculation_method": self.calculation_method,
            "pricing_assumptions": dict(self.pricing_assumptions),
            "source": self.source,
            "is_provider_reported": self.is_provider_reported,
            "measurement_timestamp": self.measurement_timestamp,
            "notes": self.notes,
        }


@dataclass(frozen=True)
class RunPerformanceMeasurement:
    """Comprehensive performance measurement record for a single run or trial."""

    measurement_id: str
    run_id: str
    benchmark_id: Optional[str]
    scenario_id: Optional[str]
    scenario_version: str
    strategy: str
    seed: Optional[int]
    timestamp: str
    accuracy: AccuracyMeasurements
    query_efficiency: QueryEfficiencyMeasurements
    runtime: RuntimeMeasurements
    cost: CostMeasurements
    configuration: Dict[str, Any] = field(default_factory=dict)
    measurement_method_version: str = MEASUREMENT_METHOD_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "measurement_id": self.measurement_id,
            "run_id": self.run_id,
            "benchmark_id": self.benchmark_id,
            "scenario_id": self.scenario_id,
            "scenario_version": self.scenario_version,
            "strategy": self.strategy,
            "seed": self.seed,
            "timestamp": self.timestamp,
            "accuracy": self.accuracy.to_dict(),
            "query_efficiency": self.query_efficiency.to_dict(),
            "runtime": self.runtime.to_dict(),
            "cost": self.cost.to_dict(),
            "configuration": dict(self.configuration),
            "measurement_method_version": self.measurement_method_version,
        }


# ==============================================================================
# Strategy Comparison Contracts
# ==============================================================================

def calculate_safe_relative_change(
    active_val: Optional[float],
    baseline_val: Optional[float],
    direction: str = DIRECTION_LOWER_IS_BETTER,
) -> Tuple[Optional[float], bool, Optional[str]]:
    """Calculate relative percentage change safely without dividing by zero or invalid data.

    Returns:
        (relative_change_pct, is_valid, reason_if_invalid)

    Rules:
    - If either value is None: invalid.
    - If baseline_val == 0.0: relative percentage is mathematically undefined (division by zero).
    - For lower_is_better (e.g. MAE, queries, runtime):
      positive percentage means reduction/improvement: (baseline - active) / baseline * 100%
    - For higher_is_better (e.g. Hit@1, MRR, success_rate):
      positive percentage means increase/improvement: (active - baseline) / baseline * 100%
    """
    if active_val is None or baseline_val is None:
        return None, False, "Metric value unavailable for one or both comparison arms."

    if abs(baseline_val) < 1e-12:
        return (
            None,
            False,
            f"Baseline value is {baseline_val:.4f}; relative percentage change is undefined to avoid division by zero.",
        )

    if direction == DIRECTION_LOWER_IS_BETTER:
        # Reduction relative to baseline
        pct = ((baseline_val - active_val) / abs(baseline_val)) * 100.0
    else:
        # Gain relative to baseline
        pct = ((active_val - baseline_val) / abs(baseline_val)) * 100.0

    return round(pct, 2), True, None


@dataclass(frozen=True)
class MetricComparisonDetail:
    """Pairwise comparison for a single performance metric between active and baseline."""

    metric_name: str
    active_value: Optional[float]
    baseline_value: Optional[float]
    units: str
    direction: str  # "lower_is_better" or "higher_is_better"
    absolute_difference: Optional[float]
    relative_change_pct: Optional[float]
    relative_change_valid: bool
    relative_change_reason: Optional[str] = None
    better_strategy: Optional[str] = None  # "active", "baseline", "tie", "inconclusive"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "metric_name": self.metric_name,
            "active_value": round(self.active_value, 6) if self.active_value is not None else None,
            "baseline_value": round(self.baseline_value, 6) if self.baseline_value is not None else None,
            "units": self.units,
            "direction": self.direction,
            "absolute_difference": round(self.absolute_difference, 6) if self.absolute_difference is not None else None,
            "relative_change_pct": self.relative_change_pct,
            "relative_change_valid": self.relative_change_valid,
            "relative_change_reason": self.relative_change_reason,
            "better_strategy": self.better_strategy,
        }


@dataclass(frozen=True)
class StrategyAggregateMetrics:
    """Statistical indicators over repeated trials for one strategy."""

    strategy: str
    total_trials: int
    successful_trials: int
    failed_trials: int
    success_rate: float
    mean_queries_used: float
    std_queries_used: float
    min_queries_used: int
    max_queries_used: int
    mean_queries_to_target: Optional[float]
    mean_held_out_mae: Optional[float]
    std_held_out_mae: Optional[float]
    mean_held_out_rmse: Optional[float]
    hit_at_1_rate: float
    hit_at_3_rate: float
    mean_mrr: float
    mean_wall_clock_ms: float
    mean_task_execution_ms: Optional[float]
    mean_estimated_cost_usd: Optional[float]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "strategy": self.strategy,
            "total_trials": self.total_trials,
            "successful_trials": self.successful_trials,
            "failed_trials": self.failed_trials,
            "success_rate": round(self.success_rate, 4),
            "mean_queries_used": round(self.mean_queries_used, 2),
            "std_queries_used": round(self.std_queries_used, 2),
            "min_queries_used": self.min_queries_used,
            "max_queries_used": self.max_queries_used,
            "mean_queries_to_target": round(self.mean_queries_to_target, 2) if self.mean_queries_to_target is not None else None,
            "mean_held_out_mae": round(self.mean_held_out_mae, 6) if self.mean_held_out_mae is not None else None,
            "std_held_out_mae": round(self.std_held_out_mae, 6) if self.std_held_out_mae is not None else None,
            "mean_held_out_rmse": round(self.mean_held_out_rmse, 6) if self.mean_held_out_rmse is not None else None,
            "hit_at_1_rate": round(self.hit_at_1_rate, 4),
            "hit_at_3_rate": round(self.hit_at_3_rate, 4),
            "mean_mrr": round(self.mean_mrr, 4),
            "mean_wall_clock_ms": round(self.mean_wall_clock_ms, 2),
            "mean_task_execution_ms": round(self.mean_task_execution_ms, 2) if self.mean_task_execution_ms is not None else None,
            "mean_estimated_cost_usd": round(self.mean_estimated_cost_usd, 4) if self.mean_estimated_cost_usd is not None else None,
        }


@dataclass(frozen=True)
class StrategyComparisonReport:
    """Rigorous active vs baseline performance comparison across all four measurement dimensions."""

    comparison_id: str
    scenario_id: str
    scenario_version: str
    timestamp: str
    paired_trials_count: int
    query_budget_per_trial: int
    success_threshold: float
    active_aggregates: StrategyAggregateMetrics
    baseline_aggregates: StrategyAggregateMetrics
    metric_comparisons: Dict[str, MetricComparisonDetail]
    efficiency_gain_ratio: Optional[str]
    claims_advantage: bool
    advantage_summary: str
    preliminary: bool
    aggregation_policy: str
    active_trial_measurements: Tuple[RunPerformanceMeasurement, ...] = ()
    baseline_trial_measurements: Tuple[RunPerformanceMeasurement, ...] = ()
    measurement_method_version: str = MEASUREMENT_METHOD_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "comparison_id": self.comparison_id,
            "scenario_id": self.scenario_id,
            "scenario_version": self.scenario_version,
            "timestamp": self.timestamp,
            "paired_trials_count": self.paired_trials_count,
            "query_budget_per_trial": self.query_budget_per_trial,
            "success_threshold": self.success_threshold,
            "active_aggregates": self.active_aggregates.to_dict(),
            "baseline_aggregates": self.baseline_aggregates.to_dict(),
            "metric_comparisons": {k: v.to_dict() for k, v in self.metric_comparisons.items()},
            "efficiency_gain_ratio": self.efficiency_gain_ratio,
            "claims_advantage": self.claims_advantage,
            "advantage_summary": self.advantage_summary,
            "preliminary": self.preliminary,
            "aggregation_policy": self.aggregation_policy,
            "active_trial_measurements": [m.to_dict() for m in self.active_trial_measurements],
            "baseline_trial_measurements": [m.to_dict() for m in self.baseline_trial_measurements],
            "measurement_method_version": self.measurement_method_version,
        }
