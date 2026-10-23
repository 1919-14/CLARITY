"""Development seed data factory for local testing and demonstration."""

from .models import (
    RunModel,
    HypothesisModel,
    HypothesisRevisionModel,
    ObservationModel,
    UsageRecordModel,
    AuditEventModel,
    BenchmarkResultModel,
    ReportModel,
)
from .repository import ClarityRepository


def seed_minimal_development_data(repo: ClarityRepository) -> str:
    """Seed a representative discovery run with hypotheses, observations, and benchmarks.
    
    Safe for repeated test invocations.
    """
    run_id = "run-seed-001"
    existing = repo.get_run(run_id)
    if existing:
        return run_id

    # 1. Run record
    run = RunModel(
        run_id=run_id,
        name="Seed Polynomial Discovery Run",
        domain="1d_polynomial",
        status="completed",
        execution_mode="local",
        sampling_strategy="active_disagreement",
        stop_reason="target_accuracy_reached",
        max_queries=20,
        max_evaluations=50,
        spend_ceiling_usd=5.0,
    )
    repo.create_run(run)

    # 2. Candidate Hypothesis 1 (Linear)
    h1 = HypothesisModel(
        hypothesis_id="hyp-seed-1",
        run_id=run_id,
        model_family="1d_linear",
        current_revision=1,
        is_pinned=False,
        is_excluded=False,
    )
    r1 = HypothesisRevisionModel(
        revision_id="rev-seed-1-1",
        revision_number=1,
        formula="y = 2.0x + 1.0",
        parameters={"w1": 2.0, "w0": 1.0},
        complexity=2,
        model_family="1d_linear",
        creation_source="generator",
    )
    repo.create_hypothesis_with_initial_revision(h1, r1)

    # 3. Candidate Hypothesis 2 (Cubic target) with 2 revisions
    h2 = HypothesisModel(
        hypothesis_id="hyp-seed-2",
        run_id=run_id,
        model_family="1d_polynomial",
        current_revision=1,
        is_pinned=True,
        is_excluded=False,
    )
    r2_v1 = HypothesisRevisionModel(
        revision_id="rev-seed-2-1",
        revision_number=1,
        formula="y = 0.4x^3 - 0.8x^2 - 1.5x + 2.8",
        parameters={"w3": 0.4, "w2": -0.8, "w1": -1.5, "w0": 2.8},
        complexity=4,
        model_family="1d_polynomial",
        creation_source="generator",
    )
    repo.create_hypothesis_with_initial_revision(h2, r2_v1)

    # Second revision simulating refit after more observations
    r2_v2 = HypothesisRevisionModel(
        revision_id="rev-seed-2-2",
        revision_number=2,
        formula="y = 0.5x^3 - 1.0x^2 - 2.0x + 3.0",
        parameters={"w3": 0.5, "w2": -1.0, "w1": -2.0, "w0": 3.0},
        complexity=4,
        model_family="1d_polynomial",
        creation_source="refit",
    )
    repo.add_hypothesis_revision("hyp-seed-2", r2_v2)

    # 4. Training Observations
    obs1 = ObservationModel(
        observation_id="obs-seed-1",
        run_id=run_id,
        input_id="inp-seed-1",
        input_values={"x": -2.0},
        output_values={"y": -1.0},
        status="success",
        sequence_number=1,
        is_held_out=False,
    )
    obs2 = ObservationModel(
        observation_id="obs-seed-2",
        run_id=run_id,
        input_id="inp-seed-2",
        input_values={"x": 1.0},
        output_values={"y": 0.5},
        status="success",
        sequence_number=2,
        is_held_out=False,
    )
    repo.add_observation(obs1)
    repo.add_observation(obs2)

    # 5. Usage record
    usage = UsageRecordModel(
        usage_id="use-seed-1",
        run_id=run_id,
        queries_executed=2,
        evaluations_scheduled=4,
        evaluations_completed=4,
        estimated_cost_usd=0.015,
        settled_cost_usd=None,
        is_budget_exhausted=False,
    )
    repo.record_usage(usage)

    # 6. Audit Event
    event = AuditEventModel(
        event_id="evt-seed-1",
        run_id=run_id,
        event_type="HYPOTHESIS_PINNED",
        actor="researcher",
        metadata_json={"hypothesis_id": "hyp-seed-2"},
    )
    repo.record_audit_event(event)

    # 7. Benchmark Result
    bench = BenchmarkResultModel(
        benchmark_id="bench-seed-1",
        run_id=run_id,
        benchmark_name="1d_polynomial_cubic",
        target_label="target_degree_3_polynomial",
        hit_at_1=True,
        hit_at_3=True,
        hit_at_5=True,
        reciprocal_rank=1.0,
        best_candidate_id="hyp-seed-2",
        queries_to_target=2,
        held_out_mae=0.005,
        held_out_rmse=0.008,
        baseline_type="random_sampling",
        efficiency_gain="3.5x",
    )
    repo.record_benchmark_result(bench)

    return run_id
