"""Robustness & Edge-Case Test Suite: Scope 4 - Persistence Integrity Across Failure Modes.

Validates that:
1. Mid-operation exceptions trigger clean transaction rollbacks, preventing partial or orphaned records.
2. The database state remains consistent and resilient under concurrent read/write access.
3. Interrupted runs recovery (e.g., across system restarts or process crashes) transitions active runs
   to failed without corrupting or dropping prior observations, revisions, or scientific evidence.
4. Corrupted, schema-violating, or malformed records (e.g., non-null violations, foreign key violations,
   cross-run mismatches, sensitive metadata) are handled or sanitized gracefully.
"""

import threading
import uuid
import pytest
from sqlalchemy.exc import IntegrityError

from app.adapters.persistence.database import DatabaseManager
from app.adapters.persistence.models import (
    ComputeTaskModel,
    EvaluationResultModel,
    HypothesisModel,
    HypothesisRevisionModel,
    ObservationModel,
    RunModel,
)
from app.adapters.persistence.repository import ClarityRepository
from app.domain.run_lifecycle import RunLifecycleManager, RunLifecycleState
from app.domain.usage_contracts import TaskRunConflictError


@pytest.fixture
def file_db(tmp_path):
    """File-backed SQLite database to test cross-session and multi-threaded persistence."""
    db_file = tmp_path / "persistence_robustness.db"
    db = DatabaseManager(f"sqlite:///{db_file}")
    db.create_all_tables()
    yield db
    db.drop_all_tables()


# ==============================================================================
# 1. Transaction Rollback on Mid-Operation Exceptions
# ==============================================================================

class TestTransactionRollbacks:
    """Verifies that mid-operation failures roll back cleanly with no orphaned records."""

    def test_mid_batch_failure_rolls_back_entire_transaction(self, file_db):
        """If an exception occurs while saving a batch of observations, none are committed."""
        run_id = f"run-{uuid.uuid4().hex[:8]}"
        with file_db.session_scope() as session:
            repo = ClarityRepository(session)
            repo.create_run(RunModel(run_id=run_id, name="Batch Rollback Run", domain="1d_polynomial"))

        # Attempt atomic multi-operation with intentional failure on 3rd item
        with pytest.raises(RuntimeError, match="Simulated mid-operation failure"):
            with file_db.session_scope() as session:
                repo = ClarityRepository(session)
                repo.add_observation(ObservationModel(
                    observation_id=f"obs-{uuid.uuid4().hex[:8]}",
                    run_id=run_id,
                    input_id=f"inp-1-{uuid.uuid4().hex[:6]}",
                    input_values={"x": 1.0},
                    output_values={"y": 1.0},
                    sequence_number=1,
                ))
                repo.add_observation(ObservationModel(
                    observation_id=f"obs-{uuid.uuid4().hex[:8]}",
                    run_id=run_id,
                    input_id=f"inp-2-{uuid.uuid4().hex[:6]}",
                    input_values={"x": 2.0},
                    output_values={"y": 4.0},
                    sequence_number=2,
                ))
                # Inject failure before commit
                raise RuntimeError("Simulated mid-operation failure")

        # Verify no observations were persisted
        with file_db.session_scope() as session:
            repo = ClarityRepository(session)
            observations = repo.list_observations_for_run(run_id)
            assert len(observations) == 0, "Failed transaction must not leave orphaned observations"

    def test_cross_run_evaluation_violation_rolls_back_cleanly(self, file_db):
        """Attempting to evaluate a revision using a mismatched run_id raises ValueError and rolls back."""
        run1_id = f"run-1-{uuid.uuid4().hex[:8]}"
        run2_id = f"run-2-{uuid.uuid4().hex[:8]}"
        hyp_id = f"hyp-{uuid.uuid4().hex[:8]}"
        rev_id = f"rev-{uuid.uuid4().hex[:8]}"

        with file_db.session_scope() as session:
            repo = ClarityRepository(session)
            repo.create_run(RunModel(run_id=run1_id, name="Run 1", domain="test"))
            repo.create_run(RunModel(run_id=run2_id, name="Run 2", domain="test"))
            hyp = HypothesisModel(hypothesis_id=hyp_id, run_id=run1_id, model_family="polynomial")
            rev = HypothesisRevisionModel(
                revision_id=rev_id,
                hypothesis_id=hyp_id,
                revision_number=1,
                formula="x^2",
                parameters={"a": 1.0},
            )
            repo.create_hypothesis_with_initial_revision(hyp, rev)

        # Attempt to record evaluation belonging to run2 for revision belonging to run1
        with pytest.raises(ValueError, match="Cross-run evaluation invariant violated"):
            with file_db.session_scope() as session:
                repo = ClarityRepository(session)
                eval_model = EvaluationResultModel(
                    evaluation_id=f"eval-{uuid.uuid4().hex[:8]}",
                    run_id=run2_id,  # Mismatched!
                    revision_id=rev_id,
                    metrics={"mae": 0.05},
                )
                repo.record_evaluation(eval_model)

        # Verify no evaluation was recorded
        with file_db.session_scope() as session:
            repo = ClarityRepository(session)
            evals = repo.list_evaluations_for_hypothesis(hyp_id)
            assert len(evals) == 0

    def test_duplicate_primary_key_causes_rollback_and_preserves_clean_session(self, file_db):
        """Violating primary key uniqueness triggers IntegrityError and preserves existing records."""
        run_id = f"run-dup-{uuid.uuid4().hex[:8]}"
        with file_db.session_scope() as session:
            repo = ClarityRepository(session)
            repo.create_run(RunModel(run_id=run_id, name="Original Run", domain="test"))

        with pytest.raises(IntegrityError):
            with file_db.session_scope() as session:
                repo = ClarityRepository(session)
                repo.create_run(RunModel(run_id=run_id, name="Duplicate Run", domain="test"))

        # Verify original record is unchanged
        with file_db.session_scope() as session:
            repo = ClarityRepository(session)
            original = repo.get_run(run_id)
            assert original is not None
            assert original.name == "Original Run"


# ==============================================================================
# 2. Concurrent Read/Write Consistency
# ==============================================================================

class TestConcurrentReadWriteConsistency:
    """Verifies persistence stability under concurrent multi-threaded execution."""

    def test_concurrent_observations_and_status_reads(self, file_db):
        """Simultaneous writers inserting observations while concurrent readers query run status and count."""
        run_id = f"run-concurrent-{uuid.uuid4().hex[:8]}"
        with file_db.session_scope() as session:
            repo = ClarityRepository(session)
            repo.create_run(RunModel(run_id=run_id, name="Concurrent Run", domain="test", status="running"))

        errors = []
        num_writers = 4
        items_per_writer = 15

        def writer_worker(writer_idx: int):
            try:
                for i in range(items_per_writer):
                    with file_db.session_scope() as session:
                        repo = ClarityRepository(session)
                        obs_id = f"obs-w{writer_idx}-{i}-{uuid.uuid4().hex[:6]}"
                        repo.add_observation(ObservationModel(
                            observation_id=obs_id,
                            run_id=run_id,
                            input_id=f"inp-w{writer_idx}-{i}",
                            input_values={"x": float(i)},
                            output_values={"y": float(i * 2)},
                            sequence_number=writer_idx * 100 + i,
                        ))
            except Exception as e:
                errors.append(e)

        def reader_worker():
            try:
                for _ in range(30):
                    with file_db.session_scope() as session:
                        repo = ClarityRepository(session)
                        run = repo.get_run(run_id)
                        assert run is not None
                        _ = repo.count_observations_for_run(run_id)
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=writer_worker, args=(w,))
            for w in range(num_writers)
        ] + [
            threading.Thread(target=reader_worker)
            for _ in range(2)
        ]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Concurrent read/write encountered errors: {errors}"

        with file_db.session_scope() as session:
            repo = ClarityRepository(session)
            total_obs = repo.count_observations_for_run(run_id)
            assert total_obs == num_writers * items_per_writer

    def test_concurrent_hypothesis_flag_updates(self, file_db):
        """Concurrent updates to pinned/excluded flags properly update versioning without corruption."""
        run_id = f"run-hyp-flag-{uuid.uuid4().hex[:8]}"
        hyp_id = f"hyp-flag-{uuid.uuid4().hex[:8]}"
        rev_id = f"rev-flag-{uuid.uuid4().hex[:8]}"

        with file_db.session_scope() as session:
            repo = ClarityRepository(session)
            repo.create_run(RunModel(run_id=run_id, name="Hyp Flag Run", domain="test"))
            repo.create_hypothesis_with_initial_revision(
                HypothesisModel(hypothesis_id=hyp_id, run_id=run_id, model_family="polynomial"),
                HypothesisRevisionModel(
                    revision_id=rev_id,
                    hypothesis_id=hyp_id,
                    revision_number=1,
                    formula="x+1",
                    parameters={"a": 1.0, "b": 1.0},
                ),
            )

        errors = []

        def toggle_pin(pinned: bool):
            try:
                for _ in range(10):
                    with file_db.session_scope() as session:
                        repo = ClarityRepository(session)
                        repo.update_hypothesis_flags(hyp_id, is_pinned=pinned)
            except Exception as e:
                errors.append(e)

        t1 = threading.Thread(target=toggle_pin, args=(True,))
        t2 = threading.Thread(target=toggle_pin, args=(False,))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert not errors, f"Concurrent flag updates failed: {errors}"

        with file_db.session_scope() as session:
            repo = ClarityRepository(session)
            final_hyp = repo.get_hypothesis(hyp_id)
            assert final_hyp is not None
            assert final_hyp.version >= 11  # Initial was 1, plus updates


# ==============================================================================
# 3. Interrupted Runs & Crash Recovery
# ==============================================================================

class TestCrashAndInterruptedRunsRecovery:
    """Verifies recovery of interrupted runs across process crashes without losing scientific results."""

    def test_recover_interrupted_runs_preserves_all_scientific_records(self, file_db):
        """Active runs (running, cancelling) transition to FAILED with audit, preserving observations and evals."""
        run_running = f"run-running-{uuid.uuid4().hex[:8]}"
        run_cancelling = f"run-cancelling-{uuid.uuid4().hex[:8]}"
        run_completed = f"run-completed-{uuid.uuid4().hex[:8]}"

        hyp_id = f"hyp-{uuid.uuid4().hex[:8]}"
        rev_id = f"rev-{uuid.uuid4().hex[:8]}"
        eval_id = f"eval-{uuid.uuid4().hex[:8]}"
        obs_id = f"obs-{uuid.uuid4().hex[:8]}"

        with file_db.session_scope() as session:
            repo = ClarityRepository(session)
            repo.create_run(RunModel(run_id=run_running, name="Interrupted Active", domain="test", status="running"))
            repo.create_run(RunModel(run_id=run_cancelling, name="Interrupted Cancelling", domain="test", status="cancelling"))
            repo.create_run(RunModel(run_id=run_completed, name="Already Done", domain="test", status="completed"))

            # Populate running run with scientific artifacts
            repo.create_hypothesis_with_initial_revision(
                HypothesisModel(hypothesis_id=hyp_id, run_id=run_running, model_family="polynomial"),
                HypothesisRevisionModel(
                    revision_id=rev_id,
                    hypothesis_id=hyp_id,
                    revision_number=1,
                    formula="2*x",
                    parameters={"a": 2.0},
                ),
            )
            repo.add_observation(ObservationModel(
                observation_id=obs_id,
                run_id=run_running,
                input_id="inp-init-1",
                input_values={"x": 5.0},
                output_values={"y": 10.0},
                sequence_number=1,
            ))
            repo.record_evaluation(EvaluationResultModel(
                evaluation_id=eval_id,
                run_id=run_running,
                revision_id=rev_id,
                metrics={"mae": 0.001, "rmse": 0.002},
            ))

        # Simulate system restart / crash recovery
        manager = RunLifecycleManager()
        with file_db.session_scope() as session:
            recovered_ids = manager.recover_interrupted_runs(session)
            assert sorted(recovered_ids) == sorted([run_running, run_cancelling])

        # Verify state and artifacts in database
        with file_db.session_scope() as session:
            repo = ClarityRepository(session)

            # 1. Status updated
            r_active = repo.get_run(run_running)
            r_cancelling = repo.get_run(run_cancelling)
            r_done = repo.get_run(run_completed)

            assert r_active.status == RunLifecycleState.FAILED.value
            assert "restart" in r_active.stop_reason.lower()
            assert r_cancelling.status == RunLifecycleState.FAILED.value
            assert r_done.status == RunLifecycleState.COMPLETED.value  # Untouched!

            # 2. Observations and evaluations are completely preserved
            obs_list = repo.list_observations_for_run(run_running)
            assert len(obs_list) == 1
            assert obs_list[0].observation_id == obs_id
            assert obs_list[0].output_values == {"y": 10.0}

            eval_list = repo.list_evaluations_for_hypothesis(hyp_id)
            assert len(eval_list) == 1
            assert eval_list[0].evaluation_id == eval_id
            assert eval_list[0].metrics["mae"] == 0.001

            # 3. Audit trail records system recovery event
            audit_events = repo.list_audit_events_for_run(run_running)
            fail_event = next((e for e in audit_events if e.event_type == "RUN_FAILED"), None)
            assert fail_event is not None
            assert fail_event.actor == "system_recovery"
            assert fail_event.metadata_json["from_state"] == "running"
            assert fail_event.metadata_json["failure_details"]["error_code"] == "SYSTEM_RESTART_INTERRUPTED"


# ==============================================================================
# 4. Schema-Violating, Malformed, and Sanitized Records
# ==============================================================================

class TestMalformedAndCorruptedRecords:
    """Verifies that the persistence layer enforces constraints and sanitizes sensitive/malformed data."""

    def test_not_null_constraints_prevent_corrupted_records(self, file_db):
        """Attempting to persist an observation without required fields raises an IntegrityError."""
        with pytest.raises(IntegrityError):
            with file_db.session_scope() as session:
                repo = ClarityRepository(session)
                # observation_id and run_id cannot be null
                corrupt_obs = ObservationModel(observation_id=None, run_id=None)
                repo.add_observation(corrupt_obs)

    def test_compute_task_reassignment_across_runs_rejected(self, file_db):
        """A compute task cannot be reassigned to a different run ID (TaskRunConflictError)."""
        run1 = f"run-t1-{uuid.uuid4().hex[:8]}"
        run2 = f"run-t2-{uuid.uuid4().hex[:8]}"
        task_id = f"task-{uuid.uuid4().hex[:8]}"

        with file_db.session_scope() as session:
            repo = ClarityRepository(session)
            repo.create_run(RunModel(run_id=run1, name="Run 1", domain="test"))
            repo.create_run(RunModel(run_id=run2, name="Run 2", domain="test"))
            repo.record_compute_task(ComputeTaskModel(
                task_id=task_id,
                run_id=run1,
                execution_mode="nodus_remote",
                status="completed",
            ))

        # Attempt to overwrite existing task with different run_id
        with pytest.raises(TaskRunConflictError, match="cannot be reassigned"):
            with file_db.session_scope() as session:
                repo = ClarityRepository(session)
                repo.record_compute_task(ComputeTaskModel(
                    task_id=task_id,
                    run_id=run2,  # Conflict!
                    execution_mode="nodus_remote",
                    status="running",
                ))

    def test_metadata_sanitization_scrubs_secrets_from_persisted_error_metadata(self, file_db):
        """Compute tasks with secret tokens, passwords, or deep recursive data have them sanitized."""
        run_id = f"run-sec-{uuid.uuid4().hex[:8]}"
        task_id = f"task-sec-{uuid.uuid4().hex[:8]}"

        sensitive_metadata = {
            "api_key": "secret-nodus-token-12345",
            "password": "super-secret-password",
            "safe_detail": "Execution failed due to matrix singularity",
            "nested": {
                "auth_token": "token-9999",
                "code": 400,
            },
        }

        with file_db.session_scope() as session:
            repo = ClarityRepository(session)
            repo.create_run(RunModel(run_id=run_id, name="Security Test", domain="test"))
            repo.record_compute_task(ComputeTaskModel(
                task_id=task_id,
                run_id=run_id,
                execution_mode="nodus_remote",
                status="failed",
                error_metadata=sensitive_metadata,
            ))

        with file_db.session_scope() as session:
            repo = ClarityRepository(session)
            saved_task = repo.get_compute_task(task_id)
            assert saved_task is not None
            meta = saved_task.error_metadata
            # Secrets must be scrubbed using the canonical redaction string
            assert meta["api_key"] == "***REDACTED***"
            assert meta["password"] == "***REDACTED***"
            assert meta["nested"]["auth_token"] == "***REDACTED***"
            # Safe data remains
            assert meta["safe_detail"] == "Execution failed due to matrix singularity"
            assert meta["nested"]["code"] == 400

    def test_repository_handles_none_for_optional_json_fields_gracefully(self, file_db):
        """Entities with None for optional JSON columns retrieve safely without AttributeError."""
        run_id = f"run-none-json-{uuid.uuid4().hex[:8]}"
        obs_id = f"obs-none-json-{uuid.uuid4().hex[:8]}"

        with file_db.session_scope() as session:
            repo = ClarityRepository(session)
            repo.create_run(RunModel(run_id=run_id, name="None JSON Run", domain="test"))
            # Observation with None output_values and None noise_metadata (optional fields)
            obs = ObservationModel(
                observation_id=obs_id,
                run_id=run_id,
                input_id="inp-opt-1",
                input_values={"x": 1.0},
                output_values=None,
                noise_metadata=None,
                sequence_number=1,
            )
            repo.add_observation(obs)

        with file_db.session_scope() as session:
            repo = ClarityRepository(session)
            fetched = repo.get_observation(obs_id)
            assert fetched is not None
            assert fetched.input_values == {"x": 1.0}
            assert fetched.output_values is None
            assert fetched.noise_metadata is None
