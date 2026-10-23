"""Automated test suite for CLARITY reproducible performance-measurement layer (Issue #27).

Verifies:
1. Accuracy metrics match benchmark evaluation output (Hit@K, MRR, held-out MAE/RMSE, sample counts).
2. Query counts match recorded oracle observations and proposed experiments are NOT counted as queries.
3. A failed-to-reach-target trial is represented explicitly rather than omitted.
4. Runtime measurements distinguish total wall-clock time from overlapping parallel task durations.
5. Missing provider cost data is not represented as zero actual cost (CostState.UNAVAILABLE).
6. Estimated cost is never labelled as settled cost.
7. Active-versus-baseline differences use compatible benchmark conditions.
8. Invalid relative comparisons (e.g. division by zero when baseline is 0.0) are rejected/marked unavailable.
9. Results retain benchmark configuration, dataset version, sample count, and method versions.
10. API endpoints expose performance measurements and handle unknown runs/measurements safely.
"""

from __future__ import annotations

import math
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List
import pytest
from flask import Flask
from sqlalchemy import insert

from app import create_app
from app.adapters.persistence.database import get_db_manager, reset_db_manager
from app.adapters.persistence.models import (
    BenchmarkResultModel,
    CandidateExperimentModel,
    ComputeTaskModel,
    EvaluationResultModel,
    HypothesisModel,
    HypothesisRevisionModel,
    ObservationModel,
    RunModel,
    UsageRecordModel,
)
from app.adapters.persistence.repository import ClarityRepository
from app.domain.contracts import (
    GroundTruthRule,
    verify_no_ground_truth_leakage,
)
from app.domain.performance_contracts import (
    DIRECTION_HIGHER_IS_BETTER,
    DIRECTION_LOWER_IS_BETTER,
    MEASUREMENT_METHOD_VERSION,
    METRIC_DEFINITIONS_VERSION,
    CostMeasurements,
    CostState,
    MetricAvailability,
    MetricComparisonDetail,
    MetricMeasurement,
    QueryEfficiencyMeasurements,
    RunPerformanceMeasurement,
    RuntimeMeasurements,
    StrategyComparisonReport,
    calculate_safe_relative_change,
)
from app.evaluator.benchmark_cases import (
    CONSTANT_BENCHMARK,
    CUBIC_BENCHMARK,
    LINEAR_BENCHMARK,
    QUADRATIC_BENCHMARK,
    get_benchmark_case,
)
from app.evaluator.benchmark_runner import (
    ActiveRandomBenchmarkRunner,
    BenchmarkTrialResult,
)
from app.services.performance_measurement_service import (
    PerformanceMeasurementService,
)


@pytest.fixture
def db_session(app):
    """Provide an isolated database session bound to the test app database."""
    db_uri = app.config.get("SQLALCHEMY_DATABASE_URI")
    db_manager = get_db_manager(db_uri)
    with db_manager.session_scope() as session:
        yield session


# ==============================================================================
# 1. Scientific Accuracy Measurement Tests
# ==============================================================================

class TestScientificAccuracyMeasurement:
    """Demonstrate that accuracy metrics match benchmark evaluation output with sample counts and units."""

    def test_accuracy_metrics_match_benchmark_evaluation(self, db_session):
        """Accuracy metrics match benchmark evaluation output with definitions, sample count, and units."""
        repo = ClarityRepository(db_session)
        now = datetime.now(timezone.utc)
        run_id = "run-acc-01"

        run = RunModel(
            run_id=run_id,
            name="Linear Polynomial Benchmark Run",
            domain="polynomial",
            status="completed",
            sampling_strategy="active_selection",
            max_queries=10,
            created_at=now,
            started_at=now,
            completed_at=now + timedelta(seconds=2),
        )
        repo.create_run(run)

        # Create benchmark result with known metrics
        bm = BenchmarkResultModel(
            benchmark_id="bm-acc-01",
            run_id=run_id,
            benchmark_name="linear_polynomial",
            target_label="target_degree_1",
            hit_at_1=True,
            hit_at_3=True,
            hit_at_5=True,
            reciprocal_rank=1.0,
            best_candidate_id="hyp-01",
            queries_to_target=4,
            held_out_mae=0.0125,
            held_out_rmse=0.0160,
            baseline_type="random_baseline",
            executed_at=now,
        )
        repo.record_benchmark_result(bm)

        # Create top hypothesis
        hyp = HypothesisModel(
            hypothesis_id="hyp-01",
            run_id=run_id,
            model_family="1d_polynomial",
            current_revision=2,
        )
        rev = HypothesisRevisionModel(
            revision_id="rev-01",
            formula="y = 2.0x + 1.0",
            parameters={"w1": 2.0, "w0": 1.0},
            complexity=2,
        )
        repo.create_hypothesis_with_initial_revision(hyp, rev)
        db_session.commit()

        service = PerformanceMeasurementService()
        measurement = service.measure_run(db_session, run_id, scenario_id="linear_polynomial")

        acc = measurement.accuracy
        # Check Hit@K
        assert acc.hit_at_1.value == 1.0
        assert acc.hit_at_1.units == "ratio"
        assert acc.hit_at_1.direction == DIRECTION_HIGHER_IS_BETTER
        assert acc.hit_at_3.value == 1.0
        assert acc.hit_at_5.value == 1.0

        # Check MRR
        assert acc.mrr.value == 1.0
        assert acc.mrr.units == "ratio"

        # Check held-out MAE and RMSE
        assert acc.held_out_mae.value == pytest.approx(0.0125, rel=1e-4)
        assert acc.held_out_mae.units == "y_units"
        assert acc.held_out_mae.direction == DIRECTION_LOWER_IS_BETTER
        assert acc.held_out_rmse.value == pytest.approx(0.0160, rel=1e-4)

        # Check sample count and versions
        assert acc.sample_count > 0
        assert acc.benchmark_version == "1.0.0"
        assert acc.hit_at_1.version == METRIC_DEFINITIONS_VERSION
        assert measurement.measurement_method_version == MEASUREMENT_METHOD_VERSION

    def test_missing_accuracy_metrics_remain_explicitly_unavailable(self, db_session):
        """When ground truth or benchmark evaluation was not performed, metrics are UNAVAILABLE, not 0.0."""
        repo = ClarityRepository(db_session)
        now = datetime.now(timezone.utc)
        run_id = "run-no-bm"

        run = RunModel(
            run_id=run_id,
            name="Unbenchmarked Run",
            domain="polynomial",
            status="completed",
            created_at=now,
        )
        repo.create_run(run)
        db_session.commit()

        service = PerformanceMeasurementService()
        measurement = service.measure_run(db_session, run_id)

        acc = measurement.accuracy
        assert acc.hit_at_1.status == MetricAvailability.UNAVAILABLE
        assert acc.hit_at_1.value is None
        assert acc.held_out_mae.status == MetricAvailability.UNAVAILABLE
        assert acc.held_out_mae.value is None
        assert acc.mrr.value is None
        assert acc.model_complexity.status == MetricAvailability.UNAVAILABLE
        assert acc.model_complexity.value is None

    def test_model_complexity_reflects_candidate_complexity_independent_of_revision_number(self, db_session):
        """[P2 Regression] Candidate complexity must reflect mathematical complexity (degree), NOT revision number."""
        repo = ClarityRepository(db_session)
        service = PerformanceMeasurementService()
        now = datetime.now(timezone.utc)

        # 1. High revision number (revision 5), but simple linear model (complexity = 1)
        run_rev5 = RunModel(
            run_id="run-rev5-simple",
            name="Run Rev 5 Simple",
            domain="polynomial",
            status="completed",
            created_at=now,
        )
        repo.create_run(run_rev5)
        hyp_rev5 = HypothesisModel(
            hypothesis_id="hyp-rev5",
            run_id="run-rev5-simple",
            model_family="1d_polynomial",
            current_revision=5,
        )
        rev5 = HypothesisRevisionModel(
            revision_id="rev-05",
            hypothesis_id="hyp-rev5",
            revision_number=5,
            formula="y = 2x + 1",
            parameters={"w1": 2.0, "w0": 1.0},
            complexity=1,
        )
        repo.create_hypothesis_with_initial_revision(hyp_rev5, rev5)

        # 2. Low revision number (revision 2), but complex quartic model (complexity = 4)
        run_rev2 = RunModel(
            run_id="run-rev2-complex",
            name="Run Rev 2 Complex",
            domain="polynomial",
            status="completed",
            created_at=now,
        )
        repo.create_run(run_rev2)
        hyp_rev2 = HypothesisModel(
            hypothesis_id="hyp-rev2",
            run_id="run-rev2-complex",
            model_family="1d_polynomial",
            current_revision=2,
        )
        rev2 = HypothesisRevisionModel(
            revision_id="rev-02",
            hypothesis_id="hyp-rev2",
            revision_number=2,
            formula="y = x^4 + 2x^2 + 1",
            parameters={"w4": 1.0, "w2": 2.0, "w0": 1.0},
            complexity=4,
        )
        repo.create_hypothesis_with_initial_revision(hyp_rev2, rev2)

        # 3. Run with no hypotheses
        run_empty = RunModel(
            run_id="run-no-hyp",
            name="Run No Hyp",
            domain="polynomial",
            status="completed",
            created_at=now,
        )
        repo.create_run(run_empty)
        db_session.commit()

        # Measure run 1: must report complexity 1.0, NOT 5.0
        m1 = service.measure_run(db_session, "run-rev5-simple")
        assert m1.accuracy.model_complexity.status == MetricAvailability.AVAILABLE
        assert m1.accuracy.model_complexity.value == 1.0
        assert m1.accuracy.model_complexity.value != 5.0

        # Measure run 2: must report complexity 4.0, NOT 2.0
        m2 = service.measure_run(db_session, "run-rev2-complex")
        assert m2.accuracy.model_complexity.status == MetricAvailability.AVAILABLE
        assert m2.accuracy.model_complexity.value == 4.0
        assert m2.accuracy.model_complexity.value != 2.0

        # Measure run 3: must be marked UNAVAILABLE and value is None
        m3 = service.measure_run(db_session, "run-no-hyp")
        assert m3.accuracy.model_complexity.status == MetricAvailability.UNAVAILABLE
        assert m3.accuracy.model_complexity.value is None


# ==============================================================================
# 2. Query Efficiency & Accounting Tests
# ==============================================================================

class TestQueryEfficiencyMeasurement:
    """Demonstrate that query counts match recorded oracle observations and distinguish proposed experiments."""

    def test_query_counts_match_actual_executed_observations_not_proposed_experiments(self, db_session):
        """Proposed candidate experiments must NOT be counted as executed oracle queries."""
        repo = ClarityRepository(db_session)
        now = datetime.now(timezone.utc)
        run_id = "run-query-acc-01"

        run = RunModel(
            run_id=run_id,
            name="Query Accounting Run",
            domain="polynomial",
            status="completed",
            max_queries=10,
            created_at=now,
        )
        repo.create_run(run)

        # Add 3 actual executed observations
        for i in range(3):
            obs = ObservationModel(
                observation_id=f"obs-{i}",
                run_id=run_id,
                input_id=f"inp-{i}",
                input_values={"x": float(i)},
                output_values={"y": float(i * 2)},
                status="success",
                sequence_number=i + 1,
            )
            repo.add_observation(obs)

        # Add 7 proposed candidate experiments that were NEVER executed
        for j in range(7):
            exp = CandidateExperimentModel(
                experiment_id=f"exp-cand-{j}",
                run_id=run_id,
                proposed_input={"x": float(j + 10)},
                execution_status="proposed",
            )
            db_session.add(exp)

        db_session.commit()

        service = PerformanceMeasurementService()
        measurement = service.measure_run(db_session, run_id)
        qe = measurement.query_efficiency

        # Invariant: queries_executed == 3, NOT 10 (3 + 7 proposed)
        assert qe.queries_executed == 3
        assert qe.proposed_experiments_count == 7
        assert qe.query_budget == 10
        assert qe.remaining_budget == 7

    def test_query_accounting_does_not_truncate_at_10000_observations(self, db_session):
        """[P1 Regression] Query counting uses exact database count and does NOT truncate runs with >10,000 observations."""
        repo = ClarityRepository(db_session)
        service = PerformanceMeasurementService()
        now = datetime.now(timezone.utc)
        run_id = "run-large-obs-10005"

        run = RunModel(
            run_id=run_id,
            name="Large Observation Run",
            domain="polynomial",
            status="completed",
            max_queries=15000,
            created_at=now,
        )
        repo.create_run(run)

        # Bulk insert 10,005 successful observations
        total_obs = 10005
        observations_data = [
            {
                "observation_id": f"obs-large-{i}",
                "run_id": run_id,
                "input_id": f"inp-large-{i}",
                "input_values": {"x": float(i)},
                "output_values": {"y": float(2 * i)},
                "status": "success",
                "sequence_number": i + 1,
                "is_held_out": False,
            }
            for i in range(total_obs)
        ]
        db_session.execute(insert(ObservationModel), observations_data)
        db_session.commit()

        measurement = service.measure_run(db_session, run_id)
        qe = measurement.query_efficiency

        # Invariant: executed queries must be EXACTLY 10,005 (not truncated to 10,000)
        assert qe.queries_executed == 10005
        assert qe.query_budget == 15000
        # Invariant: remaining budget must be 15000 - 10005 = 4995 (not overstated to 5000)
        assert qe.remaining_budget == 4995

    def test_failed_to_reach_target_trial_is_represented_explicitly(self):
        """A trial that fails to reach the target within its budget is explicitly recorded."""
        scenario = LINEAR_BENCHMARK
        failed_trial = BenchmarkTrialResult(
            trial_id="trial-fail-01",
            strategy="random_baseline",
            seed=999,
            scenario_id=scenario.id,
            scenario_version=scenario.version,
            queries_used=10,
            max_budget=10,
            budget_exhausted=True,
            target_reached=False,
            queries_to_target=None,
            hit_at_1=False,
            hit_at_3=False,
            hit_at_5=False,
            mrr=0.0,
            held_out_mae=1.45,
            held_out_rmse=1.89,
            top_candidate_formula="y = 0.1x",
            termination_reason="budget_exhausted",
            runtime_ms=150.0,
        )

        service = PerformanceMeasurementService()
        measurement = service.measure_benchmark_trial(failed_trial, scenario)
        qe = measurement.query_efficiency

        assert qe.target_reached is False
        assert qe.queries_to_target is None
        assert qe.budget_exhausted is True
        assert qe.status == "budget_exhausted"
        assert qe.queries_executed == 10
        assert qe.remaining_budget == 0


# ==============================================================================
# 3. Runtime & Execution Behavior Measurement Tests
# ==============================================================================

class TestRuntimeMeasurement:
    """Demonstrate that runtime measurements distinguish total wall-clock time from task durations."""

    def test_distinguish_wall_clock_time_from_overlapping_parallel_task_durations(self, db_session):
        """Total wall-clock duration must NOT simply sum overlapping parallel task durations."""
        repo = ClarityRepository(db_session)
        now = datetime.now(timezone.utc)
        run_id = "run-runtime-parallel"

        # Wall clock run: 1.5 seconds total (1500 ms)
        run = RunModel(
            run_id=run_id,
            name="Parallel Execution Run",
            domain="polynomial",
            status="completed",
            created_at=now,
            started_at=now,
            completed_at=now + timedelta(milliseconds=1500),
        )
        repo.create_run(run)

        # 4 parallel tasks running concurrently: each takes 1000ms.
        # Sum of task execution durations = 4000ms, while wall-clock is 1500ms!
        for i in range(4):
            task = ComputeTaskModel(
                task_id=f"task-p-{i}",
                run_id=run_id,
                execution_mode="nodus_remote",
                status="completed",
                started_at=now + timedelta(milliseconds=100),
                completed_at=now + timedelta(milliseconds=1100),
                duration_ms=1000,
            )
            repo.record_compute_task(task)

        db_session.commit()

        service = PerformanceMeasurementService()
        measurement = service.measure_run(db_session, run_id)
        rt = measurement.runtime

        # Wall-clock duration must reflect true elapsed wall-clock span (1500 ms)
        assert rt.total_wall_clock_ms == pytest.approx(1500.0, rel=1e-2)

        # Sum of task durations is 4000 ms
        assert rt.task_execution_duration_ms == 4000.0

        # Parallel overlap factor = 4000 / 1500 = 2.67x
        assert rt.parallel_overlap_factor == pytest.approx(4000.0 / 1500.0, rel=1e-2)

        # Remote execution duration is recorded separately
        assert rt.remote_execution_ms == 4000.0
        assert rt.tasks_total == 4
        assert rt.tasks_completed == 4

    def test_queue_duration_and_failure_counts_tracked(self, db_session):
        """Task queue durations and terminal outcome counts are accurately captured."""
        repo = ClarityRepository(db_session)
        now = datetime.now(timezone.utc)
        run_id = "run-task-states"

        run = RunModel(run_id=run_id, name="Task States Run", status="completed", created_at=now)
        repo.create_run(run)

        # Task 1: completed, queued for 200ms, executed for 500ms
        t1 = ComputeTaskModel(
            task_id="t-01",
            run_id=run_id,
            status="completed",
            created_at=now,
            started_at=now + timedelta(milliseconds=200),
            completed_at=now + timedelta(milliseconds=700),
            duration_ms=500,
        )
        # Task 2: failed
        t2 = ComputeTaskModel(
            task_id="t-02",
            run_id=run_id,
            status="failed",
            created_at=now,
            started_at=now + timedelta(milliseconds=100),
            completed_at=now + timedelta(milliseconds=300),
            duration_ms=200,
        )
        # Task 3: timed_out
        t3 = ComputeTaskModel(
            task_id="t-03",
            run_id=run_id,
            status="timed_out",
            created_at=now,
        )
        repo.record_compute_task(t1)
        repo.record_compute_task(t2)
        repo.record_compute_task(t3)
        db_session.commit()

        service = PerformanceMeasurementService()
        measurement = service.measure_run(db_session, run_id)
        rt = measurement.runtime

        assert rt.tasks_total == 3
        assert rt.tasks_completed == 1
        assert rt.tasks_failed == 1
        assert rt.tasks_timed_out == 1
        # Average queue duration = (200 + 100) / 2 = 150ms
        assert rt.task_queue_duration_ms == pytest.approx(150.0, rel=1e-2)


# ==============================================================================
# 4. Compute Cost Classification & Accuracy Tests
# ==============================================================================

class TestComputeCostMeasurement:
    """Demonstrate that cost states distinguish estimates, settled charges, and missing data."""

    def test_missing_provider_cost_data_is_not_represented_as_zero_actual_cost(self, db_session):
        """When provider cost is unavailable, settled_cost_usd is None and state is UNAVAILABLE, not $0.00."""
        repo = ClarityRepository(db_session)
        now = datetime.now(timezone.utc)
        run_id = "run-missing-cost"

        run = RunModel(run_id=run_id, name="Missing Cost Run", status="completed", created_at=now)
        repo.create_run(run)
        db_session.commit()

        service = PerformanceMeasurementService()
        measurement = service.measure_run(db_session, run_id)
        cost = measurement.cost

        # Must NOT invent $0.00 settled cost
        assert cost.cost_state == CostState.UNAVAILABLE
        assert cost.settled_cost_usd is None
        assert cost.is_provider_reported is False
        assert cost.notes is not None
        assert "not assumed to be $0.00" in cost.notes

    def test_estimated_cost_is_never_labelled_as_settled_cost(self, db_session):
        """Application estimates are labelled ESTIMATED with source='application_estimate', not SETTLED."""
        repo = ClarityRepository(db_session)
        now = datetime.now(timezone.utc)
        run_id = "run-estimated-cost"

        run = RunModel(run_id=run_id, name="Estimated Cost Run", status="completed", created_at=now)
        repo.create_run(run)

        # Usage record with application estimate
        usage = UsageRecordModel(
            usage_id="use-est-01",
            run_id=run_id,
            queries_executed=5,
            evaluations_scheduled=4,
            evaluations_completed=4,
            estimated_cost_usd=0.07,
            live_accruing_cost_usd=None,
            settled_cost_usd=None,
        )
        repo.record_usage(usage)
        db_session.commit()

        service = PerformanceMeasurementService()
        measurement = service.measure_run(db_session, run_id)
        cost = measurement.cost

        assert cost.cost_state == CostState.ESTIMATED
        assert cost.estimated_cost_usd == 0.07
        assert cost.settled_cost_usd is None
        assert cost.is_provider_reported is False
        assert cost.source == "application_estimate"

    def test_settled_provider_cost_is_correctly_classified(self, db_session):
        """When provider reports finalized billing, it is classified as SETTLED with is_provider_reported=True."""
        repo = ClarityRepository(db_session)
        now = datetime.now(timezone.utc)
        run_id = "run-settled-cost"

        run = RunModel(run_id=run_id, name="Settled Cost Run", status="completed", created_at=now)
        repo.create_run(run)

        usage = UsageRecordModel(
            usage_id="use-settled-01",
            run_id=run_id,
            settled_cost_usd=0.145,
            estimated_cost_usd=0.150,
        )
        repo.record_usage(usage)
        db_session.commit()

        service = PerformanceMeasurementService()
        measurement = service.measure_run(db_session, run_id)
        cost = measurement.cost

        assert cost.cost_state == CostState.SETTLED
        assert cost.settled_cost_usd == 0.145
        assert cost.is_provider_reported is True
        assert cost.source == "provider_reported"


# ==============================================================================
# 5. Active vs Baseline Comparison & Safety Tests
# ==============================================================================

class TestActiveVsBaselineComparison:
    """Demonstrate fair comparison, safe relative changes, and no unsupported claims."""

    def test_active_vs_baseline_comparison_under_compatible_conditions(self):
        """Paired comparison calculates absolute differences, sample counts, and honest advantage."""
        scenario = LINEAR_BENCHMARK
        service = PerformanceMeasurementService()

        # Create active trials
        act_trial = BenchmarkTrialResult(
            trial_id="act-01",
            strategy="active_selection",
            seed=42,
            scenario_id=scenario.id,
            scenario_version=scenario.version,
            queries_used=4,
            max_budget=10,
            budget_exhausted=False,
            target_reached=True,
            queries_to_target=4,
            hit_at_1=True,
            hit_at_3=True,
            hit_at_5=True,
            mrr=1.0,
            held_out_mae=0.015,
            held_out_rmse=0.020,
            top_candidate_formula="y = 2.0x + 1.0",
            termination_reason="target_accuracy_reached",
            runtime_ms=120.0,
        )
        # Create baseline trial
        rand_trial = BenchmarkTrialResult(
            trial_id="rand-01",
            strategy="random_baseline",
            seed=42,
            scenario_id=scenario.id,
            scenario_version=scenario.version,
            queries_used=8,
            max_budget=10,
            budget_exhausted=False,
            target_reached=True,
            queries_to_target=8,
            hit_at_1=True,
            hit_at_3=True,
            hit_at_5=True,
            mrr=1.0,
            held_out_mae=0.045,
            held_out_rmse=0.060,
            top_candidate_formula="y = 2.0x + 1.0",
            termination_reason="target_accuracy_reached",
            runtime_ms=180.0,
        )

        act_m = service.measure_benchmark_trial(act_trial, scenario)
        rand_m = service.measure_benchmark_trial(rand_trial, scenario)

        report = service.compare_strategies(
            active_measurements=[act_m],
            baseline_measurements=[rand_m],
            scenario_id=scenario.id,
            scenario_version=scenario.version,
            query_budget=10,
            success_threshold=0.05,
        )

        assert report.paired_trials_count == 1
        assert report.active_aggregates.mean_queries_to_target == 4.0
        assert report.baseline_aggregates.mean_queries_to_target == 8.0

        # Efficiency gain ratio = 8.0 / 4.0 = 2.0x
        assert report.efficiency_gain_ratio == "2.0x"

        # Queries used comparison
        q_comp = report.metric_comparisons["queries_used"]
        assert q_comp.absolute_difference == -4.0  # 4 - 8
        assert q_comp.relative_change_valid is True
        assert q_comp.relative_change_pct == 50.0  # 50% query reduction
        assert q_comp.better_strategy == "active"

        # MAE comparison
        mae_comp = report.metric_comparisons["held_out_mae"]
        assert mae_comp.relative_change_valid is True
        assert mae_comp.better_strategy == "active"

        # Supported advantage claimed
        assert report.claims_advantage is True
        assert "Active selection required fewer queries" in report.advantage_summary

    def test_invalid_relative_comparison_when_baseline_is_zero(self):
        """Relative percentage comparison is marked invalid/unavailable when baseline is 0.0 (division by zero)."""
        rel_pct, is_valid, reason = calculate_safe_relative_change(
            active_val=1.0,
            baseline_val=0.0,
            direction=DIRECTION_HIGHER_IS_BETTER,
        )
        assert is_valid is False
        assert rel_pct is None
        assert "Baseline value is 0.0000" in reason
        assert "division by zero" in reason

    def test_invalid_relative_comparison_when_value_is_none(self):
        """Relative percentage comparison is marked invalid when either value is None."""
        rel_pct, is_valid, reason = calculate_safe_relative_change(
            active_val=None,
            baseline_val=0.05,
        )
        assert is_valid is False
        assert rel_pct is None
        assert "unavailable" in reason

    def test_no_unsupported_advantage_claimed_when_both_fail(self):
        """When neither strategy converges, claims_advantage is False and honest summary is recorded."""
        scenario = CUBIC_BENCHMARK
        service = PerformanceMeasurementService()

        # Both fail to reach target
        act_fail = BenchmarkTrialResult(
            trial_id="act-f",
            strategy="active_selection",
            seed=1,
            scenario_id=scenario.id,
            scenario_version=scenario.version,
            queries_used=10,
            max_budget=10,
            budget_exhausted=True,
            target_reached=False,
            queries_to_target=None,
            hit_at_1=False,
            hit_at_3=False,
            hit_at_5=False,
            mrr=0.0,
            held_out_mae=2.5,
            held_out_rmse=3.0,
            top_candidate_formula="y = 0.5x",
            termination_reason="budget_exhausted",
            runtime_ms=200.0,
        )
        rand_fail = BenchmarkTrialResult(
            trial_id="rand-f",
            strategy="random_baseline",
            seed=1,
            scenario_id=scenario.id,
            scenario_version=scenario.version,
            queries_used=10,
            max_budget=10,
            budget_exhausted=True,
            target_reached=False,
            queries_to_target=None,
            hit_at_1=False,
            hit_at_3=False,
            hit_at_5=False,
            mrr=0.0,
            held_out_mae=2.8,
            held_out_rmse=3.2,
            top_candidate_formula="y = 0.4x",
            termination_reason="budget_exhausted",
            runtime_ms=190.0,
        )

        act_m = service.measure_benchmark_trial(act_fail, scenario)
        rand_m = service.measure_benchmark_trial(rand_fail, scenario)

        report = service.compare_strategies(
            active_measurements=[act_m],
            baseline_measurements=[rand_m],
            scenario_id=scenario.id,
            scenario_version=scenario.version,
            query_budget=10,
            success_threshold=0.05,
        )

        assert report.claims_advantage is False
        assert "Neither strategy converged" in report.advantage_summary

    def test_strategy_comparison_rejects_incompatible_pairing_conditions(self):
        """[Pairing Integrity] Comparison requires strictly compatible paired experimental conditions."""
        service = PerformanceMeasurementService()
        scenario = LINEAR_BENCHMARK

        trial_base = BenchmarkTrialResult(
            trial_id="t-active",
            strategy="active_selection",
            seed=42,
            scenario_id=scenario.id,
            scenario_version=scenario.version,
            queries_used=4,
            max_budget=10,
            budget_exhausted=False,
            target_reached=True,
            queries_to_target=4,
            hit_at_1=True,
            hit_at_3=True,
            hit_at_5=True,
            mrr=1.0,
            held_out_mae=0.01,
            held_out_rmse=0.02,
            top_candidate_formula="y = 2x + 1",
            termination_reason="target_accuracy_reached",
            runtime_ms=100.0,
        )
        trial_baseline = BenchmarkTrialResult(
            trial_id="t-base",
            strategy="random_baseline",
            seed=42,
            scenario_id=scenario.id,
            scenario_version=scenario.version,
            queries_used=8,
            max_budget=10,
            budget_exhausted=False,
            target_reached=True,
            queries_to_target=8,
            hit_at_1=True,
            hit_at_3=True,
            hit_at_5=True,
            mrr=1.0,
            held_out_mae=0.04,
            held_out_rmse=0.05,
            top_candidate_formula="y = 2x + 1",
            termination_reason="target_accuracy_reached",
            runtime_ms=120.0,
        )

        m_active = service.measure_benchmark_trial(trial_base, scenario)
        m_base = service.measure_benchmark_trial(trial_baseline, scenario)

        # 1. Reject unequal list lengths
        with pytest.raises(ValueError, match="equal sample counts"):
            service.compare_strategies(
                active_measurements=[m_active, m_active],
                baseline_measurements=[m_base],
                scenario_id=scenario.id,
                scenario_version=scenario.version,
                query_budget=10,
                success_threshold=0.05,
            )

        # 2. Reject empty measurements
        with pytest.raises(ValueError, match="must both be non-empty"):
            service.compare_strategies(
                active_measurements=[],
                baseline_measurements=[],
                scenario_id=scenario.id,
                scenario_version=scenario.version,
                query_budget=10,
                success_threshold=0.05,
            )

        # 3. Reject mismatched seed
        trial_diff_seed = BenchmarkTrialResult(
            trial_id="t-base-99",
            strategy="random_baseline",
            seed=99,  # Mismatched seed!
            scenario_id=scenario.id,
            scenario_version=scenario.version,
            queries_used=8,
            max_budget=10,
            budget_exhausted=False,
            target_reached=True,
            queries_to_target=8,
            hit_at_1=True,
            hit_at_3=True,
            hit_at_5=True,
            mrr=1.0,
            held_out_mae=0.04,
            held_out_rmse=0.05,
            top_candidate_formula="y = 2x + 1",
            termination_reason="target_accuracy_reached",
            runtime_ms=120.0,
        )
        m_diff_seed = service.measure_benchmark_trial(trial_diff_seed, scenario)
        with pytest.raises(ValueError, match="seed mismatch"):
            service.compare_strategies(
                active_measurements=[m_active],
                baseline_measurements=[m_diff_seed],
                scenario_id=scenario.id,
                scenario_version=scenario.version,
                query_budget=10,
                success_threshold=0.05,
            )

        # 4. Reject mismatched per-trial budget
        trial_diff_budget = BenchmarkTrialResult(
            trial_id="t-base-b5",
            strategy="random_baseline",
            seed=42,
            scenario_id=scenario.id,
            scenario_version=scenario.version,
            queries_used=5,
            max_budget=5,  # Mismatched budget!
            budget_exhausted=True,
            target_reached=False,
            queries_to_target=None,
            hit_at_1=False,
            hit_at_3=False,
            hit_at_5=False,
            mrr=0.0,
            held_out_mae=1.5,
            held_out_rmse=2.0,
            top_candidate_formula="y = x",
            termination_reason="budget_exhausted",
            runtime_ms=90.0,
        )
        m_diff_budget = service.measure_benchmark_trial(trial_diff_budget, scenario)
        with pytest.raises(ValueError, match="per-trial budget mismatch"):
            service.compare_strategies(
                active_measurements=[m_active],
                baseline_measurements=[m_diff_budget],
                scenario_id=scenario.id,
                scenario_version=scenario.version,
                query_budget=10,
                success_threshold=0.05,
            )

        # 5. Reject mismatched scenario
        trial_diff_scen = BenchmarkTrialResult(
            trial_id="t-base-quad",
            strategy="random_baseline",
            seed=42,
            scenario_id=QUADRATIC_BENCHMARK.id,  # Mismatched scenario!
            scenario_version=QUADRATIC_BENCHMARK.version,
            queries_used=8,
            max_budget=10,
            budget_exhausted=False,
            target_reached=True,
            queries_to_target=8,
            hit_at_1=True,
            hit_at_3=True,
            hit_at_5=True,
            mrr=1.0,
            held_out_mae=0.04,
            held_out_rmse=0.05,
            top_candidate_formula="y = 2x^2 + 1",
            termination_reason="target_accuracy_reached",
            runtime_ms=120.0,
        )
        m_diff_scen = service.measure_benchmark_trial(trial_diff_scen, QUADRATIC_BENCHMARK)
        with pytest.raises(ValueError, match="scenario mismatch"):
            service.compare_strategies(
                active_measurements=[m_active],
                baseline_measurements=[m_diff_scen],
                scenario_id=scenario.id,
                scenario_version=scenario.version,
                query_budget=10,
                success_threshold=0.05,
            )

        # 6. Reject mismatched scenario version
        trial_diff_ver = BenchmarkTrialResult(
            trial_id="t-base-v2",
            strategy="random_baseline",
            seed=42,
            scenario_id=scenario.id,
            scenario_version="2.0.0",  # Mismatched version!
            queries_used=8,
            max_budget=10,
            budget_exhausted=False,
            target_reached=True,
            queries_to_target=8,
            hit_at_1=True,
            hit_at_3=True,
            hit_at_5=True,
            mrr=1.0,
            held_out_mae=0.04,
            held_out_rmse=0.05,
            top_candidate_formula="y = 2x + 1",
            termination_reason="target_accuracy_reached",
            runtime_ms=120.0,
        )
        m_diff_ver = service.measure_benchmark_trial(trial_diff_ver, scenario)
        # Update version to 2.0.0
        m_diff_ver = RunPerformanceMeasurement(
            measurement_id=m_diff_ver.measurement_id,
            run_id=m_diff_ver.run_id,
            benchmark_id=m_diff_ver.benchmark_id,
            scenario_id=m_diff_ver.scenario_id,
            scenario_version="2.0.0",
            strategy=m_diff_ver.strategy,
            seed=m_diff_ver.seed,
            timestamp=m_diff_ver.timestamp,
            accuracy=m_diff_ver.accuracy,
            query_efficiency=m_diff_ver.query_efficiency,
            runtime=m_diff_ver.runtime,
            cost=m_diff_ver.cost,
            configuration=m_diff_ver.configuration,
            measurement_method_version=m_diff_ver.measurement_method_version,
        )
        with pytest.raises(ValueError, match="scenario version mismatch"):
            service.compare_strategies(
                active_measurements=[m_active],
                baseline_measurements=[m_diff_ver],
                scenario_id=scenario.id,
                scenario_version=scenario.version,
                query_budget=10,
                success_threshold=0.05,
            )


# ==============================================================================
# 6. REST API & Persistence Tests
# ==============================================================================

class TestPerformanceMeasurementApi:
    """Demonstrate REST API endpoints expose measurements, list summaries, and reject ground-truth injection."""

    def test_post_measurements_for_existing_run(self, app, client):
        """POST /api/v1/benchmarks/measurements measures an existing run and returns structured metrics."""
        # 1. Create a run via direct repository write
        from app.adapters.persistence.database import get_db_manager
        db_uri = app.config.get("SQLALCHEMY_DATABASE_URI")
        db = get_db_manager(db_uri)
        now = datetime.now(timezone.utc)
        with db.session_scope() as session:
            repo = ClarityRepository(session)
            run = RunModel(
                run_id="run-api-test-01",
                name="API Run Test",
                status="completed",
                max_queries=10,
                created_at=now,
                started_at=now,
                completed_at=now + timedelta(seconds=1),
            )
            repo.create_run(run)
            obs = ObservationModel(
                observation_id="obs-api-01",
                run_id="run-api-test-01",
                input_id="inp-01",
                input_values={"x": 1.0},
                output_values={"y": 2.0},
                status="success",
                sequence_number=1,
            )
            repo.add_observation(obs)

        # 2. Call POST /api/v1/benchmarks/measurements
        resp = client.post(
            "/api/v1/benchmarks/measurements",
            json={"run_id": "run-api-test-01"},
        )
        assert resp.status_code == 201
        data = resp.get_json()
        assert data["run_id"] == "run-api-test-01"
        assert "measurement" in data
        assert data["measurement"]["query_efficiency"]["queries_executed"] == 1
        assert data["measurement"]["measurement_method_version"] == MEASUREMENT_METHOD_VERSION

    def test_get_run_measurements_returns_404_for_unknown_run(self, client):
        """GET /api/v1/benchmarks/runs/<run_id>/measurements returns 404 for non-existent run."""
        resp = client.get("/api/v1/benchmarks/runs/non-existent-run-xyz/measurements")
        assert resp.status_code == 404

    def test_post_measurements_rejects_injected_ground_truth(self, client):
        """POST /api/v1/benchmarks/measurements rejects client-supplied ground-truth secrets."""
        resp = client.post(
            "/api/v1/benchmarks/measurements",
            json={
                "scenario_id": "linear_polynomial",
                "target_formula": "y = 2x + 1",
            },
        )
        assert resp.status_code == 400
        data = resp.get_json()
        assert "Client-supplied ground truth" in data["message"]

    def test_trigger_measured_benchmark_comparison_via_api(self, client):
        """POST /api/v1/benchmarks/measurements executes benchmark, records measurements, and returns 201."""
        resp = client.post(
            "/api/v1/benchmarks/measurements",
            json={
                "scenario_id": "linear_polynomial",
                "seeds": [42],
                "max_query_budget": 4,
            },
        )
        assert resp.status_code == 201
        data = resp.get_json()
        assert "report" in data
        assert data["report"]["scenario_id"] in ("linear_polynomial", "polynomial_linear_benchmark")
        assert data["report"]["paired_trials_count"] == 1
        assert "metric_comparisons" in data["report"]
        assert "held_out_mae" in data["report"]["metric_comparisons"]
        measurement_id = data["measurement_id"]

        # Retrieve by measurement_id
        get_resp = client.get(f"/api/v1/benchmarks/measurements/{measurement_id}")
        assert get_resp.status_code == 200
        get_data = get_resp.get_json()
        assert get_data["measurements"]["scenario_id"] in ("linear_polynomial", "polynomial_linear_benchmark")

        # List measurements
        list_resp = client.get("/api/v1/benchmarks/measurements")
        assert list_resp.status_code == 200
        list_data = list_resp.get_json()
        assert list_data["count"] >= 1
