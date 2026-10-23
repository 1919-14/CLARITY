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

import os
import pytest
from sqlalchemy.exc import IntegrityError
from app.adapters.persistence.database import DatabaseManager, get_db_manager, reset_db_manager
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


# ==============================================================================
# 9. Relational Integrity, Retention & Environment Enforcement (PR #35 Review Findings)
# ==============================================================================

def test_run_deletion_cannot_silently_erase_historical_records(repo):
    """Verify deleting a run cannot erase child scientific/audit records (Finding 1).
    
    Relationships configure passive_deletes=True and database-level RESTRICT
    constraints to ensure historical records are preserved from accidental deletion.
    """
    run = RunModel(run_id="run-protect-01", name="Protected Run")
    repo.create_run(run)

    obs = ObservationModel(
        observation_id="obs-protect-01",
        run_id="run-protect-01",
        input_id="inp-01",
        input_values={"x": 5.0},
        sequence_number=1,
    )
    repo.add_observation(obs)
    repo.session.commit()

    # Attempting to delete the parent run directly must be rejected by foreign key RESTRICT
    run_to_delete = repo.get_run("run-protect-01")
    repo.session.delete(run_to_delete)
    with pytest.raises(IntegrityError):
        repo.session.commit()
    repo.session.rollback()

    # Verify both parent run and historical observation remain fully intact
    assert repo.get_run("run-protect-01") is not None
    obs_list = repo.list_observations_for_run("run-protect-01")
    assert len(obs_list) == 1
    assert obs_list[0].observation_id == "obs-protect-01"


def test_cross_run_evaluation_rejected(repo):
    """Verify that an evaluation cannot reference a hypothesis revision from a different run (Finding 2)."""
    run1 = RunModel(run_id="run-iso-1", name="Run 1")
    run2 = RunModel(run_id="run-iso-2", name="Run 2")
    repo.create_run(run1)
    repo.create_run(run2)

    hyp1 = HypothesisModel(hypothesis_id="hyp-iso-1", run_id="run-iso-1", model_family="poly")
    rev1 = HypothesisRevisionModel(revision_id="rev-iso-1", formula="y = x", parameters={})
    repo.create_hypothesis_with_initial_revision(hyp1, rev1)
    repo.session.commit()

    # Attempt to record an evaluation under run-iso-2 for rev-iso-1 (which belongs to run-iso-1)
    cross_run_eval = EvaluationResultModel(
        evaluation_id="eval-cross-01",
        revision_id="rev-iso-1",
        run_id="run-iso-2",
        metrics={"mae": 0.05},
    )
    with pytest.raises(ValueError, match="Cross-run evaluation invariant violated"):
        repo.record_evaluation(cross_run_eval)
    repo.session.rollback()


def test_foreign_key_enforcement_rejects_orphan_child(repo):
    """Verify SQLite foreign key enforcement is active and rejects orphan child records (Finding 3)."""
    orphan_obs = ObservationModel(
        observation_id="orphan-obs-001",
        run_id="non-existent-run-id",
        input_id="inp-orphan",
        input_values={"x": 1.0},
        sequence_number=1,
    )
    repo.session.add(orphan_obs)
    with pytest.raises(IntegrityError):
        repo.session.flush()
    repo.session.rollback()


def test_production_environment_rejects_missing_database_url(monkeypatch):
    """Verify production environment fails fast if DATABASE_URL is missing or SQLite (Finding 4)."""
    import os
    from app.adapters.persistence.database import get_db_manager, reset_db_manager
    from app import create_app

    # 1. get_db_manager fails fast in production when DATABASE_URL is missing
    reset_db_manager()
    monkeypatch.setenv("FLASK_ENV", "production")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(ValueError, match="DATABASE_URL must be explicitly configured"):
        get_db_manager()

    # 2. get_db_manager fails fast in production when DATABASE_URL is SQLite
    reset_db_manager()
    monkeypatch.setenv("DATABASE_URL", "sqlite:///clarity_prod.db")
    with pytest.raises(ValueError, match="SQLite database URI cannot be used in production"):
        get_db_manager()

    # 3. create_app in production mode fails fast when DATABASE_URL is missing
    monkeypatch.setenv("SECRET_KEY", "real-production-secret-key-1234567890")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(ValueError, match="DATABASE_URL.*explicitly configured"):
        create_app("production")

    reset_db_manager()


def test_alembic_config_escapes_url_encoded_credentials(monkeypatch):
    """Verify Alembic config safely escapes % in URL-encoded credentials (Finding 5)."""
    import os
    import pathlib
    from alembic.config import Config

    repo_root = pathlib.Path(__file__).resolve().parent.parent.parent
    ini_path = repo_root / "alembic.ini"

    # URL-encoded credential containing %40 (@) and %25 (%)
    encoded_url = "mysql+pymysql://clarity_user:p%40ss%25word%21@db.host.internal:3306/clarity_db"
    monkeypatch.setenv("DATABASE_URL", encoded_url)

    cfg = Config(str(ini_path))
    env_db_url = os.environ.get("DATABASE_URL")
    assert env_db_url is not None

    # Unescaped setting would raise ValueError("invalid interpolation syntax") in ConfigParser
    with pytest.raises(ValueError, match="invalid interpolation syntax"):
        cfg.set_main_option("sqlalchemy.url", env_db_url)

    # With proper escaping as done in migrations/env.py, it succeeds and round-trips correctly
    cfg.set_main_option("sqlalchemy.url", env_db_url.replace("%", "%%"))
    retrieved_url = cfg.get_main_option("sqlalchemy.url")
    assert retrieved_url == encoded_url


def test_production_environment_rejects_explicit_sqlite_uri(monkeypatch):
    """Verify production mode rejects an explicitly passed SQLite URI across all entry points (Follow-up Finding 1)."""
    from app.adapters.persistence.database import (
        DatabaseManager,
        get_db_manager,
        init_db,
        reset_db_manager,
    )

    reset_db_manager()
    monkeypatch.setenv("FLASK_ENV", "production")

    # 1. get_db_manager with explicit SQLite argument
    with pytest.raises(ValueError, match="SQLite database URI cannot be used in production"):
        get_db_manager("sqlite:///clarity_prod.db")

    # 2. init_db with explicit SQLite argument
    reset_db_manager()
    with pytest.raises(ValueError, match="SQLite database URI cannot be used in production"):
        init_db("sqlite:///clarity_prod.db")

    # 3. DatabaseManager direct constructor with SQLite argument
    with pytest.raises(ValueError, match="SQLite database URI cannot be used in production"):
        DatabaseManager("sqlite:///clarity_prod.db")

    reset_db_manager()


def test_alembic_migrations_upgrade_and_downgrade_smoke(tmp_path):
    """Verify that Alembic migrations successfully apply, roll back, and re-apply cleanly (Follow-up Finding 2)."""
    import pathlib
    import sqlite3
    from alembic import command
    from alembic.config import Config

    repo_root = pathlib.Path(__file__).resolve().parent.parent.parent
    ini_path = repo_root / "alembic.ini"
    migrations_dir = repo_root / "backend" / "migrations"

    db_file = tmp_path / "smoke_migration.db"
    db_url = f"sqlite:///{db_file}"

    cfg = Config(str(ini_path))
    cfg.set_main_option("script_location", str(migrations_dir))
    cfg.set_main_option("sqlalchemy.url", db_url)

    # 1. Upgrade to head
    command.upgrade(cfg, "head")

    # Verify tables were created in the SQLite database
    conn = sqlite3.connect(str(db_file))
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
    tables = {row[0] for row in cursor.fetchall()}
    conn.close()

    expected_tables = {
        "runs",
        "hypotheses",
        "hypothesis_revisions",
        "observations",
        "candidate_experiments",
        "evaluation_results",
        "compute_tasks",
        "usage_records",
        "audit_events",
        "benchmark_results",
        "reports",
        "alembic_version",
    }
    assert expected_tables.issubset(tables)

    # 2. Downgrade to base
    command.downgrade(cfg, "base")

    conn = sqlite3.connect(str(db_file))
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name != 'alembic_version';")
    downgraded_tables = [row[0] for row in cursor.fetchall()]
    conn.close()
    assert len(downgraded_tables) == 0

    # 3. Re-upgrade to head
    command.upgrade(cfg, "head")
    conn = sqlite3.connect(str(db_file))
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
    recreated_tables = {row[0] for row in cursor.fetchall()}
    conn.close()
    assert expected_tables.issubset(recreated_tables)


def test_get_db_manager_rebinds_when_resolved_uri_differs(monkeypatch):
    """Regression test: get_db_manager() must unconditionally update singleton when resolved URI changes."""
    reset_db_manager()
    try:
        # 1. Initialize with an explicit URI
        explicit_uri = "sqlite:///:memory:"
        mgr1 = get_db_manager(explicit_uri)
        assert mgr1.database_uri == explicit_uri

        # 2. Set environment variable DATABASE_URL and call get_db_manager() with database_uri=None
        env_uri = "sqlite:///test_rebind_env.db"
        monkeypatch.setenv("DATABASE_URL", env_uri)
        mgr2 = get_db_manager()
        assert mgr2.database_uri == env_uri
        assert mgr2 is not mgr1

        # 3. Clear environment and verify fallback to default development URI
        monkeypatch.delenv("DATABASE_URL", raising=False)
        mgr3 = get_db_manager()
        assert mgr3.database_uri == "sqlite:///clarity_dev.db"
        assert mgr3 is not mgr2
    finally:
        reset_db_manager()


def test_list_observations_for_run_pagination_and_count(repo):
    """Verify repo.list_observations_for_run applies SQL LIMIT and OFFSET, and count_observations_for_run counts correctly."""
    run = RunModel(run_id="run-paged-obs", name="Paged Observations Test")
    repo.create_run(run)
    for i in range(10):
        repo.add_observation(
            ObservationModel(
                observation_id=f"obs-paged-{i}",
                run_id="run-paged-obs",
                input_id=f"inp-{i}",
                input_values={"x": float(i)},
                output_values={"y": float(i * 2)},
                sequence_number=i + 1,
                is_held_out=(i >= 8),  # 8 training, 2 held-out
            )
        )
    repo.session.commit()

    # Total counts
    assert repo.count_observations_for_run("run-paged-obs") == 10
    assert repo.count_observations_for_run("run-paged-obs", only_training=True) == 8

    # Paginated queries
    page1 = repo.list_observations_for_run("run-paged-obs", limit=3, offset=0)
    assert len(page1) == 3
    assert [o.sequence_number for o in page1] == [1, 2, 3]

    page2 = repo.list_observations_for_run("run-paged-obs", limit=3, offset=3)
    assert len(page2) == 3
    assert [o.sequence_number for o in page2] == [4, 5, 6]

    # Training only with limit
    training_page = repo.list_observations_for_run("run-paged-obs", only_training=True, limit=5, offset=5)
    assert len(training_page) == 3  # only 8 total training (items 6, 7, 8)
    assert all(not o.is_held_out for o in training_page)


def test_alembic_migration_reports_report_json_incremental(tmp_path):
    """Verify incremental migration adding report_json to reports table against an existing schema.

    Tests that:
    1. A database at prior head 'f7a8b9c0d1e2' has the reports table without report_json.
    2. Existing records inserted prior to migration persist safely.
    3. Upgrading to 'head' (b3c4d5e6f7a8) adds report_json without data loss.
    4. ORM queries with ReportModel succeed on pre-existing rows (report_json is None).
    5. New updates and inserts can store and retrieve JSON data in report_json.
    6. Downgrading to 'f7a8b9c0d1e2' cleanly drops report_json while retaining rows.
    """
    import pathlib
    import sqlite3
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from app.adapters.persistence.models import ReportModel

    repo_root = pathlib.Path(__file__).resolve().parent.parent.parent
    ini_path = repo_root / "alembic.ini"
    migrations_dir = repo_root / "backend" / "migrations"

    db_file = tmp_path / "incremental_migration.db"
    db_url = f"sqlite:///{db_file}"

    cfg = Config(str(ini_path))
    cfg.set_main_option("script_location", str(migrations_dir))
    cfg.set_main_option("sqlalchemy.url", db_url)

    # 1. Upgrade up to prior revision f7a8b9c0d1e2 (before report_json)
    command.upgrade(cfg, "f7a8b9c0d1e2")

    # Verify report_json does NOT exist at f7a8b9c0d1e2
    conn = sqlite3.connect(str(db_file))
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(reports);")
    columns_before = {row[1] for row in cursor.fetchall()}
    assert "report_id" in columns_before
    assert "run_id" in columns_before
    assert "report_json" not in columns_before

    # Insert a run and a report record directly into the existing schema
    cursor.execute(
        "INSERT INTO runs (run_id, name, domain, status, execution_mode, sampling_strategy, created_at) "
        "VALUES ('run-mig-01', 'Migration Test Run', 'test_domain', 'active', 'autonomous', 'active_learning', '2026-10-24 00:00:00');"
    )
    cursor.execute(
        "INSERT INTO reports (report_id, run_id, status, generated_at, ranked_candidates, stop_reason, limitations) "
        "VALUES ('rep-mig-01', 'run-mig-01', 'generated', '2026-10-24 00:00:00', '[]', 'max_queries', 'None');"
    )
    conn.commit()
    conn.close()

    # 2. Upgrade to head (b3c4d5e6f7a8) which introduces report_json
    command.upgrade(cfg, "head")

    # Verify column now exists in reports
    conn = sqlite3.connect(str(db_file))
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(reports);")
    columns_after = {row[1]: row for row in cursor.fetchall()}
    assert "report_json" in columns_after
    conn.close()

    # 3. Test ORM access against the migrated database
    engine = create_engine(db_url)
    with Session(engine) as session:
        # Pre-existing report must load successfully via ORM without column-missing errors
        existing_report = session.get(ReportModel, "rep-mig-01")
        assert existing_report is not None
        assert existing_report.run_id == "run-mig-01"
        assert existing_report.status == "generated"
        assert existing_report.report_json is None

        # Update existing report with report_json
        canonical_payload = {
            "summary": "Updated canonical report after migration",
            "findings": ["Hypothesis A fits data"],
        }
        existing_report.report_json = canonical_payload
        session.commit()

        # Re-fetch and verify persistence
        session.expire_all()
        refetched = session.get(ReportModel, "rep-mig-01")
        assert refetched.report_json == canonical_payload

        # Insert a new report with report_json populated
        new_report = ReportModel(
            report_id="rep-mig-02",
            run_id="run-mig-01",
            status="generated",
            ranked_candidates=[],
            stop_reason="target_accuracy_reached",
            report_json={"summary": "New report", "target_degree": 2},
        )
        session.add(new_report)
        session.commit()

        session.expire_all()
        fetched_new = session.get(ReportModel, "rep-mig-02")
        assert fetched_new is not None
        assert fetched_new.report_json == {"summary": "New report", "target_degree": 2}

    engine.dispose()

    # 4. Downgrade back to f7a8b9c0d1e2
    command.downgrade(cfg, "f7a8b9c0d1e2")

    # Verify report_json is removed and existing row still preserved
    conn = sqlite3.connect(str(db_file))
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(reports);")
    columns_downgraded = {row[1] for row in cursor.fetchall()}
    assert "report_json" not in columns_downgraded
    assert "report_id" in columns_downgraded

    cursor.execute("SELECT report_id, status FROM reports WHERE report_id = 'rep-mig-01';")
    row = cursor.fetchone()
    assert row is not None
    assert row[0] == "rep-mig-01"
    conn.close()

