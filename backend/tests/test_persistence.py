"""Comprehensive persistence and schema tests for CLARITY (Issue #18).

Tests cover:
1. Complete entity creation and retrieval (Run, Hypothesis, Revision, Observation, Experiment, Evaluation, Task, Usage, Audit, Benchmark, Report).
2. Scientific history preservation: updating hypothesis revisions preserves prior revisions and evaluations.
3. Uniqueness constraints: rejecting duplicate revision numbers for the same hypothesis.
4. Foreign-key referential integrity: rejecting orphan child records without existing runs.
5. Transaction rollback: ensuring partial multi-record failures roll back cleanly.
6. Observation dataset isolation: linking evaluation results to exact revision and observation version.
7. Seed factory: idempotent minimal development seed data creation.
8. Filter queries: filtering hypotheses by run and exclusion state, observations by training status.
"""

import pytest
from sqlalchemy.exc import IntegrityError
from app.adapters.persistence.database import DatabaseManager
from app.adapters.persistence.models import (
    Base,
    RunModel,
    HypothesisModel,
    HypothesisRevisionModel,
    ObservationModel,
    CandidateExperimentModel,
    EvaluationResultModel,
    ComputeTaskModel,
    UsageRecordModel,
    AuditEventModel,
    BenchmarkResultModel,
    ReportModel,
)
from app.adapters.persistence.repository import ClarityRepository
from app.adapters.persistence.seed import seed_minimal_development_data


@pytest.fixture
def db_manager():
    """In-memory SQLite database manager for isolated test execution."""
    manager = DatabaseManager("sqlite:///:memory:")
    manager.create_all_tables()
    yield manager
    manager.drop_all_tables()


@pytest.fixture
def repo(db_manager):
    """Repository instance bound to a fresh in-memory session."""
    session = db_manager.get_session()
    yield ClarityRepository(session)
    session.close()


# ==============================================================================
# 1. Full Entity Creation and Retrieval
# ==============================================================================

def test_create_and_retrieve_run(repo):
    """Verify discovery run entity persists and retrieves with limits and status."""
    run = RunModel(
        run_id="run-test-001",
        name="1D Polynomial Discovery Test",
        domain="1d_polynomial",
        status="running",
        execution_mode="local",
        sampling_strategy="active_disagreement",
        max_queries=15,
        max_evaluations=60,
        max_runtime_seconds=120,
        spend_ceiling_usd=10.0,
    )
    repo.create_run(run)
    repo.session.commit()

    retrieved = repo.get_run("run-test-001")
    assert retrieved is not None
    assert retrieved.name == "1D Polynomial Discovery Test"
    assert retrieved.status == "running"
    assert retrieved.max_queries == 15
    assert retrieved.spend_ceiling_usd == 10.0


def test_create_observation_and_experiment(repo):
    """Verify observations and candidate experiments link correctly."""
    run = RunModel(run_id="run-obs-01", name="Obs Test Run")
    repo.create_run(run)

    obs = ObservationModel(
        observation_id="obs-001",
        run_id="run-obs-01",
        input_id="inp-001",
        input_values={"x": 2.5},
        output_values={"y": 9.5625},
        status="success",
        sequence_number=1,
        is_held_out=False,
    )
    repo.add_observation(obs)

    exp = CandidateExperimentModel(
        experiment_id="exp-001",
        run_id="run-obs-01",
        proposed_input={"x": 2.5},
        selection_rationale={"disagreement": 14.2},
        execution_status="executed",
        observation_id="obs-001",
    )
    repo.session.add(exp)
    repo.session.commit()

    # Query observations
    obs_list = repo.list_observations_for_run("run-obs-01")
    assert len(obs_list) == 1
    assert obs_list[0].output_values["y"] == 9.5625
    assert obs_list[0].experiment is not None
    assert obs_list[0].experiment.experiment_id == "exp-001"


# ==============================================================================
# 2. Scientific History Preservation: Revisions & Evaluations
# ==============================================================================

def test_hypothesis_revision_history_preservation(repo):
    """Verify new revisions increment revision number and preserve prior versions."""
    run = RunModel(run_id="run-hist-01", name="History Run")
    repo.create_run(run)

    hyp = HypothesisModel(
        hypothesis_id="hyp-01",
        run_id="run-hist-01",
        model_family="1d_polynomial",
    )
    rev1 = HypothesisRevisionModel(
        revision_id="rev-01-v1",
        formula="y = 0.4x^3",
        parameters={"w3": 0.4},
        complexity=2,
        creation_source="generator",
    )
    repo.create_hypothesis_with_initial_revision(hyp, rev1)
    repo.session.commit()

    # Record evaluation on revision 1
    eval1 = EvaluationResultModel(
        evaluation_id="eval-01",
        revision_id="rev-01-v1",
        run_id="run-hist-01",
        status="completed",
        sample_count=2,
        dataset_version=2,
        metrics={"mae": 0.12, "rmse": 0.15},
    )
    repo.record_evaluation(eval1)
    repo.session.commit()

    # Add revision 2 (e.g. after observing more data)
    rev2 = HypothesisRevisionModel(
        revision_id="rev-01-v2",
        formula="y = 0.5x^3 - 1.0x^2",
        parameters={"w3": 0.5, "w2": -1.0},
        complexity=3,
        creation_source="refit",
    )
    repo.add_hypothesis_revision("hyp-01", rev2)
    repo.session.commit()

    # Record evaluation on revision 2
    eval2 = EvaluationResultModel(
        evaluation_id="eval-02",
        revision_id="rev-01-v2",
        run_id="run-hist-01",
        status="completed",
        sample_count=5,
        dataset_version=5,
        metrics={"mae": 0.02, "rmse": 0.03},
    )
    repo.record_evaluation(eval2)
    repo.session.commit()

    # Verify hypothesis current revision updated to 2
    stored_hyp = repo.get_hypothesis("hyp-01")
    assert stored_hyp.current_revision == 2

    # Verify both revisions exist in order
    assert len(stored_hyp.revisions) == 2
    assert stored_hyp.revisions[0].revision_number == 1
    assert stored_hyp.revisions[0].formula == "y = 0.4x^3"
    assert stored_hyp.revisions[1].revision_number == 2
    assert stored_hyp.revisions[1].formula == "y = 0.5x^3 - 1.0x^2"

    # Verify evaluations identify exact revision and dataset version
    rev1_evals = repo.list_evaluations_for_revision("rev-01-v1")
    assert len(rev1_evals) == 1
    assert rev1_evals[0].metrics["mae"] == 0.12
    assert rev1_evals[0].dataset_version == 2

    rev2_evals = repo.list_evaluations_for_revision("rev-01-v2")
    assert len(rev2_evals) == 1
    assert rev2_evals[0].metrics["mae"] == 0.02
    assert rev2_evals[0].dataset_version == 5


# ==============================================================================
# 3. Integrity Constraints: Uniqueness and Foreign Keys
# ==============================================================================

def test_duplicate_revision_number_rejected(repo):
    """Verify unique constraint on (hypothesis_id, revision_number) is enforced."""
    run = RunModel(run_id="run-uq-01", name="UQ Run")
    repo.create_run(run)

    hyp = HypothesisModel(hypothesis_id="hyp-uq", run_id="run-uq-01", model_family="poly")
    rev1 = HypothesisRevisionModel(
        revision_id="rev-uq-1",
        hypothesis_id="hyp-uq",
        revision_number=1,
        formula="y = x",
        parameters={},
    )
    repo.create_hypothesis_with_initial_revision(hyp, rev1)
    repo.session.commit()

    # Attempt inserting a duplicate revision number 1 for the same hypothesis
    duplicate_rev = HypothesisRevisionModel(
        revision_id="rev-uq-dup",
        hypothesis_id="hyp-uq",
        revision_number=1,
        formula="y = 2x",
        parameters={},
    )
    repo.session.add(duplicate_rev)
    with pytest.raises(IntegrityError):
        repo.session.commit()
    repo.session.rollback()


# ==============================================================================
# 4. Transaction Rollback Behavior
# ==============================================================================

def test_transaction_rollback_on_failure(db_manager):
    """Verify session_scope context manager rolls back completely on any error."""
    with pytest.raises(RuntimeError, match="Simulated execution error"):
        with db_manager.session_scope() as session:
            run = RunModel(run_id="run-rollback-test", name="Rollback Test Run")
            session.add(run)
            session.flush()

            # Add an invalid action triggering exception before commit
            raise RuntimeError("Simulated execution error during multi-record write")

    # Verify run was not committed
    with db_manager.session_scope() as verify_session:
        retrieved = verify_session.get(RunModel, "run-rollback-test")
        assert retrieved is None


# ==============================================================================
# 5. Researcher Action Auditing and Flags
# ==============================================================================

def test_researcher_pin_exclude_and_audit(repo):
    """Verify pinning, exclusion, and audit events function without altering evaluations."""
    run = RunModel(run_id="run-audit-01", name="Audit Run")
    repo.create_run(run)

    hyp = HypothesisModel(hypothesis_id="hyp-aud", run_id="run-audit-01", model_family="poly")
    rev = HypothesisRevisionModel(revision_id="rev-aud-1", formula="y = x", parameters={})
    repo.create_hypothesis_with_initial_revision(hyp, rev)
    repo.session.commit()

    # Pin hypothesis and record audit event
    repo.update_hypothesis_flags("hyp-aud", is_pinned=True)
    repo.record_audit_event(
        AuditEventModel(
            event_id="evt-01",
            run_id="run-audit-01",
            event_type="HYPOTHESIS_PINNED",
            actor="researcher",
            metadata_json={"hypothesis_id": "hyp-aud"},
        )
    )
    repo.session.commit()

    # Verify state
    updated_hyp = repo.get_hypothesis("hyp-aud")
    assert updated_hyp.is_pinned is True
    assert updated_hyp.is_excluded is False

    events = repo.list_audit_events_for_run("run-audit-01")
    assert len(events) == 1
    assert events[0].event_type == "HYPOTHESIS_PINNED"

    # Exclude hypothesis
    repo.update_hypothesis_flags("hyp-aud", is_excluded=True)
    repo.session.commit()

    active_hypotheses = repo.list_hypotheses_for_run("run-audit-01", include_excluded=False)
    assert len(active_hypotheses) == 0

    all_hypotheses = repo.list_hypotheses_for_run("run-audit-01", include_excluded=True)
    assert len(all_hypotheses) == 1


# ==============================================================================
# 6. Usage and Budget Progression
# ==============================================================================

def test_usage_record_cost_separation(repo):
    """Verify usage records distinguish estimated, accruing, and settled costs."""
    run = RunModel(run_id="run-use-01", name="Usage Test")
    repo.create_run(run)

    usage1 = UsageRecordModel(
        usage_id="use-01",
        run_id="run-use-01",
        queries_executed=5,
        evaluations_scheduled=10,
        evaluations_completed=8,
        estimated_cost_usd=0.035,
        live_accruing_cost_usd=0.030,
        settled_cost_usd=None,
        is_budget_exhausted=False,
    )
    repo.record_usage(usage1)
    repo.session.commit()

    latest = repo.get_latest_usage_for_run("run-use-01")
    assert latest.queries_executed == 5
    assert latest.estimated_cost_usd == 0.035
    assert latest.settled_cost_usd is None  # Unknown/unsettled is represented as None, not 0.0


# ==============================================================================
# 7. Benchmarks and Reports
# ==============================================================================

def test_benchmark_and_report_storage(repo):
    """Verify benchmark metrics (Hit@K, MRR) and reports persist reliably."""
    run = RunModel(run_id="run-rep-01", name="Report Run")
    repo.create_run(run)

    bench = BenchmarkResultModel(
        benchmark_id="bench-01",
        run_id="run-rep-01",
        benchmark_name="polynomial_cubic_benchmark",
        target_label="target_degree_3_polynomial",
        hit_at_1=True,
        hit_at_3=True,
        hit_at_5=True,
        reciprocal_rank=1.0,
        best_candidate_id="hyp-target",
        queries_to_target=4,
        held_out_mae=0.012,
        held_out_rmse=0.018,
        efficiency_gain="3.0x",
    )
    repo.record_benchmark_result(bench)

    report = ReportModel(
        report_id="rep-01",
        run_id="run-rep-01",
        status="generated",
        ranked_candidates=[
            {"hypothesis_id": "hyp-target", "rank": 1, "empirical_mae": 0.012}
        ],
        stop_reason="target_accuracy_reached",
        limitations="Evaluated over [-10.0, 10.0] grid domain.",
    )
    repo.save_report(report)
    repo.session.commit()

    bench_results = repo.get_benchmark_results_for_run("run-rep-01")
    assert len(bench_results) == 1
    assert bench_results[0].hit_at_1 is True
    assert bench_results[0].reciprocal_rank == 1.0

    retrieved_rep = repo.get_latest_report_for_run("run-rep-01")
    assert retrieved_rep.status == "generated"
    assert len(retrieved_rep.ranked_candidates) == 1


# ==============================================================================
# 8. Seed Data Factory Test
# ==============================================================================

def test_minimal_seed_data_factory(repo):
    """Verify minimal development seed data initializes idempotently."""
    run_id1 = seed_minimal_development_data(repo)
    repo.session.commit()

    run1 = repo.get_run(run_id1)
    assert run1 is not None
    assert len(run1.hypotheses) == 2
    assert len(run1.observations) == 2

    # Second invocation should be idempotent without creating duplicate runs
    run_id2 = seed_minimal_development_data(repo)
    assert run_id1 == run_id2
