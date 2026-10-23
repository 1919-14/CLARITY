"""Acceptance tests for auditable researcher history (Issue #17).

Covers the acceptance gate:

A. Revision preservation      — initial revision, edits append, invalid edits write nothing,
                               revision numbers stay unique under conflicting updates.
B. Evaluation provenance      — each evaluation names its exact revision and dataset snapshot,
                               re-evaluation appends, failures stay failed with no fake scores.
C. Lifecycle integrity        — pin/exclude/restore are audited without touching science.
D. Event integrity            — stable IDs, UTC timestamps, actor, before/after, sanitization,
                               append-only enforcement.
E. History retrieval          — deterministic order, cursor + offset pagination, filters,
                               empty vs missing resources.
F. Transaction failure        — revision + audit event commit or roll back together.
G. Idempotency                — replay returns the committed revision, key reuse with a
                               different payload is rejected.
H. Run failure / cancellation — prior history survives.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.adapters.persistence.database import get_db_manager
from app.adapters.persistence.models import (
    AuditEventModel,
    ObservationModel,
    RunModel,
)
from app.adapters.persistence.repository import ClarityRepository
from app.domain.history_contracts import (
    AUDIT_HYPOTHESIS_REVISED,
    AppendOnlyViolationError,
    InvalidHistoryCursorError,
    compute_dataset_snapshot_id,
    decode_history_cursor,
    encode_history_cursor,
)
from app.domain.evaluation import EvaluationPipeline
from app.services.audit_service import (
    AuditCorrectionTargetNotFound,
    correct_audit_event_service,
    record_audit_event_service,
)
from app.services.history_service import (
    HistoryNotFoundError,
    list_hypothesis_revisions,
    list_revision_evaluations,
    list_run_history,
)
from app.services.hypothesis_service import (
    HypothesisConflictError,
    HypothesisNotFoundError,
    HypothesisValidationError,
    exclude_hypothesis_service,
    pin_hypothesis_service,
    propose_hypothesis_service,
    reevaluate_hypothesis_service,
    restore_hypothesis_service,
    revise_hypothesis_service,
    unpin_hypothesis_service,
)
from app.services.run_service import cancel_run_service, create_run_service


# =============================================================================
# Fixtures and helpers
# =============================================================================


@pytest.fixture
def db(app):
    """Provide the DatabaseManager bound to the isolated test database."""
    return get_db_manager(app.config["SQLALCHEMY_DATABASE_URI"])


@pytest.fixture
def session(db):
    """Provide a transactional session for service-level tests."""
    with db.session_scope() as scoped:
        yield scoped


def _new_run(session, name="History Test Run") -> RunModel:
    return create_run_service(session=session, name=name, max_queries=10)


def _add_observations(session, run_id: str, count: int = 3, start: int = 1) -> None:
    """Add deterministic training observations for ``y = 3x^2 + 2``."""
    for i in range(start, start + count):
        x_val = float(i)
        session.add(
            ObservationModel(
                observation_id=f"obs-{uuid.uuid4().hex[:10]}",
                run_id=run_id,
                input_id=f"inp-{i}",
                input_values={"x": x_val},
                output_values={"y": 3.0 * x_val * x_val + 2.0},
                status="success",
                sequence_number=i,
                is_held_out=False,
            )
        )
    session.flush()


def _event_types(session, run_id: str):
    repo = ClarityRepository(session)
    return [e.event_type for e in repo.list_audit_events_for_run(run_id)]


# =============================================================================
# A. Revision preservation
# =============================================================================


class TestRevisionPreservation:
    def test_creation_records_initial_revision_and_audit_event(self, session):
        run = _new_run(session)
        hyp = propose_hypothesis_service(session, run.run_id, "linear", "y = 2*x + 1")

        repo = ClarityRepository(session)
        revisions = repo.list_hypothesis_revisions(hyp.hypothesis_id)
        assert len(revisions) == 1
        rev = revisions[0]
        assert rev.revision_number == 1
        assert rev.previous_revision_id is None
        assert rev.formula == "y = 2*x + 1"
        assert rev.actor == "researcher"
        assert hyp.current_revision == 1

        propose_events = [
            e for e in repo.list_audit_events_for_run(run.run_id)
            if e.event_type == "HYPOTHESIS_PROPOSED"
        ]
        assert len(propose_events) == 1
        event = propose_events[0]
        assert event.hypothesis_id == hyp.hypothesis_id
        assert event.revision_id == rev.revision_id
        assert event.before_json is None
        assert event.after_json["revision_number"] == 1
        assert event.after_json["formula"] == "y = 2*x + 1"

    def test_edit_appends_revision_and_preserves_previous_definition(self, session):
        run = _new_run(session)
        hyp = propose_hypothesis_service(session, run.run_id, "linear", "y = 2*x + 1")
        repo = ClarityRepository(session)
        original = repo.list_hypothesis_revisions(hyp.hypothesis_id)[0]
        original_parameters = dict(original.parameters)

        revised = revise_hypothesis_service(
            session,
            hyp.hypothesis_id,
            formula="y = 3*x^2 + 2",
            reason="quadratic fit explains the residual",
        )

        assert revised.revision_number == 2
        assert revised.previous_revision_id == original.revision_id
        assert revised.change_reason == "quadratic fit explains the residual"

        revisions = repo.list_hypothesis_revisions(hyp.hypothesis_id)
        assert [r.revision_number for r in revisions] == [1, 2]

        preserved = revisions[0]
        assert preserved.revision_id == original.revision_id
        assert preserved.formula == "y = 2*x + 1"
        assert dict(preserved.parameters) == original_parameters
        assert preserved.complexity == original.complexity
        assert preserved.change_reason is None

        assert repo.get_hypothesis(hyp.hypothesis_id).current_revision == 2

        revise_events = [
            e for e in repo.list_audit_events_for_run(run.run_id)
            if e.event_type == AUDIT_HYPOTHESIS_REVISED
        ]
        assert len(revise_events) == 1
        event = revise_events[0]
        assert event.before_json["formula"] == "y = 2*x + 1"
        assert event.after_json["formula"] == "y = 3*x^2 + 2"
        assert event.reason == "quadratic fit explains the residual"
        assert event.revision_id == revised.revision_id

    def test_revision_records_the_validated_model_family(self, session):
        """The family a revision is created under is part of its scientific record."""
        run = _new_run(session)
        hyp = propose_hypothesis_service(session, run.run_id, "linear", "y = 2*x + 1")
        repo = ClarityRepository(session)

        initial = repo.list_hypothesis_revisions(hyp.hypothesis_id)[0]
        # The revision records the family an evaluation can actually replay ...
        assert initial.model_family == "polynomial"
        # ... while the hypothesis row keeps the researcher-facing projection.
        assert hyp.model_family == "linear"

        revised = revise_hypothesis_service(
            session,
            hyp.hypothesis_id,
            formula="y = 3*x^2 + 2",
            model_family="1d_polynomial",
        )
        assert revised.model_family == "polynomial"
        # The parent row follows the newest revision instead of leading it.
        assert repo.get_hypothesis(hyp.hypothesis_id).model_family == "polynomial"
        assert repo.list_hypothesis_revisions(hyp.hypothesis_id)[0].model_family == "polynomial"

        revise_events = [
            e for e in repo.list_audit_events_for_run(run.run_id)
            if e.event_type == AUDIT_HYPOTHESIS_REVISED
        ]
        assert revise_events[0].metadata_json["model_family"] == "polynomial"
        assert revise_events[0].after_json["model_family"] == "polynomial"

    def test_invalid_edit_creates_no_revision_and_no_event(self, session):
        run = _new_run(session)
        hyp = propose_hypothesis_service(session, run.run_id, "linear", "y = 2*x + 1")
        repo = ClarityRepository(session)
        events_before = _event_types(session, run.run_id)

        with pytest.raises(HypothesisValidationError):
            revise_hypothesis_service(
                session, hyp.hypothesis_id, formula="__import__('os').system('echo hi')"
            )

        assert len(repo.list_hypothesis_revisions(hyp.hypothesis_id)) == 1
        assert repo.get_hypothesis(hyp.hypothesis_id).current_revision == 1
        assert _event_types(session, run.run_id) == events_before

    def test_noop_edit_is_rejected(self, session):
        run = _new_run(session)
        hyp = propose_hypothesis_service(session, run.run_id, "linear", "y = 2*x + 1")
        with pytest.raises(HypothesisValidationError):
            revise_hypothesis_service(session, hyp.hypothesis_id, formula="y = 2*x + 1")
        assert len(ClarityRepository(session).list_hypothesis_revisions(hyp.hypothesis_id)) == 1

    def test_stale_expected_revision_is_rejected(self, session):
        run = _new_run(session)
        hyp = propose_hypothesis_service(session, run.run_id, "linear", "y = 2*x + 1")
        revise_hypothesis_service(
            session, hyp.hypothesis_id, formula="y = 2*x + 2", expected_revision=1
        )
        with pytest.raises(HypothesisConflictError):
            revise_hypothesis_service(
                session, hyp.hypothesis_id, formula="y = 2*x + 3", expected_revision=1
            )
        assert len(ClarityRepository(session).list_hypothesis_revisions(hyp.hypothesis_id)) == 2

    def test_duplicate_revision_number_is_rejected_at_the_database_boundary(self, db):
        """A racing writer that produces a used revision number cannot commit it."""
        with db.session_scope() as setup:
            run = _new_run(setup)
            hyp = propose_hypothesis_service(setup, run.run_id, "linear", "y = 2*x + 1")
            revise_hypothesis_service(setup, hyp.hypothesis_id, formula="y = 2*x + 2")
            run_id, hypothesis_id = run.run_id, hyp.hypothesis_id

        # Simulate the interleaving: a second writer still believes revision 1 is current.
        with db.session_scope() as stale:
            ClarityRepository(stale).get_hypothesis(hypothesis_id).current_revision = 1

        with pytest.raises(HypothesisConflictError):
            with db.session_scope() as racing:
                revise_hypothesis_service(racing, hypothesis_id, formula="y = 2*x + 3")

        with db.session_scope() as verify:
            repo = ClarityRepository(verify)
            revisions = repo.list_hypothesis_revisions(hypothesis_id)
            assert [r.revision_number for r in revisions] == [1, 2]
            assert repo.count_audit_events_for_run(
                run_id, event_type=AUDIT_HYPOTHESIS_REVISED
            ) == 1
            # The rejected write left no partial pointer behind either.
            repo.get_hypothesis(hypothesis_id).current_revision = 2


# =============================================================================
# B. Evaluation provenance
# =============================================================================


class TestEvaluationProvenance:
    def test_evaluation_names_revision_and_dataset_snapshot(self, session):
        run = _new_run(session)
        _add_observations(session, run.run_id, count=4)
        hyp = propose_hypothesis_service(session, run.run_id, "linear", "y = 3*x^2 + 2")
        repo = ClarityRepository(session)

        evaluation, _ = reevaluate_hypothesis_service(session, hyp.hypothesis_id)
        revisions = repo.list_hypothesis_revisions(hyp.hypothesis_id)

        assert evaluation.status == "completed"
        assert evaluation.revision_id == revisions[0].revision_id
        assert evaluation.sample_count == 4
        assert evaluation.dataset_version == 4
        assert evaluation.dataset_snapshot_id.startswith("dset-")

        observations = repo.list_observations_for_run(run.run_id)
        assert evaluation.dataset_snapshot_id == compute_dataset_snapshot_id(
            run.run_id, observations
        )
        assert evaluation.dataset_snapshot["snapshot_id"] == evaluation.dataset_snapshot_id
        assert evaluation.dataset_snapshot["observation_count"] == 4

    def test_snapshot_requires_observation_content_not_bare_ids(self):
        """A hash of identifiers alone cannot detect changed evaluated inputs."""
        with pytest.raises(ValueError):
            compute_dataset_snapshot_id("run-1", ["obs-1", "obs-2"])

    def test_changed_observation_content_changes_the_snapshot_without_changing_ids(self, session):
        """Regression: same observation IDs, different scientific content."""
        run = _new_run(session)
        _add_observations(session, run.run_id, count=3)
        hyp = propose_hypothesis_service(session, run.run_id, "linear", "y = 3*x^2 + 2")
        repo = ClarityRepository(session)
        observations = repo.list_observations_for_run(run.run_id)
        observation_ids = [o.observation_id for o in observations]

        first, _ = reevaluate_hypothesis_service(session, hyp.hypothesis_id)
        first_snapshot = first.dataset_snapshot_id
        first_manifest = dict(first.dataset_snapshot)
        changed = observations[0]
        original_inputs = dict(changed.input_values)
        original_outputs = dict(changed.output_values)

        # Keep the identifier, change the evaluated content.
        changed.input_values = {"x": 42.0}
        changed.output_values = {"y": 999.0}
        session.flush()

        second, _ = reevaluate_hypothesis_service(session, hyp.hypothesis_id)

        # Identifiers are unchanged, so an id-only fingerprint would be stable.
        assert [o.observation_id for o in repo.list_observations_for_run(run.run_id)] == observation_ids
        assert second.dataset_snapshot_id != first_snapshot
        assert second.dataset_snapshot_id == second.dataset_snapshot["snapshot_id"]
        second_record = next(
            record
            for record in second.dataset_snapshot["observations"]
            if record["observation_id"] == changed.observation_id
        )
        assert second_record["input_values"] == {"x": 42.0}
        assert second_record["output_values"] == {"y": 999.0}

        # The earlier evaluation keeps the manifest of what it actually used.
        original = repo.list_evaluations_for_revision(first.revision_id, order="asc")[0]
        assert original.dataset_snapshot_id == first_snapshot
        assert original.dataset_snapshot == first_manifest
        original_record = next(
            record
            for record in original.dataset_snapshot["observations"]
            if record["observation_id"] == changed.observation_id
        )
        assert original_record["input_values"] == original_inputs
        assert original_record["output_values"] == original_outputs

    def test_new_observations_change_the_snapshot_without_rewriting_history(self, session):
        run = _new_run(session)
        _add_observations(session, run.run_id, count=3)
        hyp = propose_hypothesis_service(session, run.run_id, "linear", "y = 3*x^2 + 2")
        repo = ClarityRepository(session)

        first, _ = reevaluate_hypothesis_service(session, hyp.hypothesis_id)
        first_snapshot = first.dataset_snapshot_id
        first_metrics = dict(first.metrics)

        _add_observations(session, run.run_id, count=2, start=10)
        second, _ = reevaluate_hypothesis_service(session, hyp.hypothesis_id)

        assert second.evaluation_id != first.evaluation_id
        assert second.dataset_snapshot_id != first_snapshot
        assert second.dataset_version == 5

        # The earlier evaluation still points at its own inputs and metrics.
        original = repo.list_evaluations_for_revision(first.revision_id, order="asc")[0]
        assert original.evaluation_id == first.evaluation_id
        assert original.dataset_snapshot_id == first_snapshot
        assert dict(original.metrics) == first_metrics
        assert len(repo.list_evaluations_for_revision(first.revision_id)) == 2

    def test_same_dataset_produces_same_snapshot(self, session):
        run = _new_run(session)
        _add_observations(session, run.run_id, count=3)
        hyp = propose_hypothesis_service(session, run.run_id, "linear", "y = 3*x^2 + 2")
        first, _ = reevaluate_hypothesis_service(session, hyp.hypothesis_id)
        second, _ = reevaluate_hypothesis_service(session, hyp.hypothesis_id)
        assert first.dataset_snapshot_id == second.dataset_snapshot_id
        assert first.evaluation_id != second.evaluation_id

    def test_failed_evaluation_preserves_error_and_fabricates_no_scores(self, session):
        run = _new_run(session)
        hyp = propose_hypothesis_service(session, run.run_id, "linear", "y = 3*x^2 + 2")
        evaluation, _ = reevaluate_hypothesis_service(session, hyp.hypothesis_id)

        assert evaluation.status == "failed"
        assert evaluation.error_message
        assert evaluation.metrics == {}
        assert evaluation.held_out_metrics is None
        assert evaluation.sample_count == 0

        # Distinguishable from a legitimate successful zero-error score.
        _add_observations(session, run.run_id, count=3)
        completed, _ = reevaluate_hypothesis_service(session, hyp.hypothesis_id)
        assert completed.status == "completed"
        assert completed.metrics  # real, non-empty metrics
        assert completed.evaluation_id != evaluation.evaluation_id

    def test_evaluation_uses_the_family_recorded_on_the_revision(self, session, monkeypatch):
        """Regression: a revision's family, not the mutable parent field, drives evaluation."""
        run = _new_run(session)
        _add_observations(session, run.run_id, count=3)
        hyp = propose_hypothesis_service(session, run.run_id, "linear", "y = 3*x^2 + 2")
        repo = ClarityRepository(session)

        revised = revise_hypothesis_service(
            session,
            hyp.hypothesis_id,
            formula="y = 3*x^2 + 2.5",
            model_family="1d_polynomial",
        )
        assert revised.model_family == "polynomial"

        # Simulate the current-state projection lagging behind the revision it
        # points at; the evaluation must not pick this value up.
        repo.get_hypothesis(hyp.hypothesis_id).model_family = "legacy_unregistered"
        session.flush()

        seen = {}
        original_evaluate = EvaluationPipeline.evaluate

        def _capture(self, request, observations, dataset_id=None):
            seen["model_family"] = request.hypothesis.model_family
            return original_evaluate(self, request, observations, dataset_id=dataset_id)

        monkeypatch.setattr(EvaluationPipeline, "evaluate", _capture)

        evaluation, _ = reevaluate_hypothesis_service(session, hyp.hypothesis_id)

        assert evaluation.status == "completed"
        assert evaluation.revision_id == revised.revision_id
        assert seen["model_family"] == revised.model_family == "polynomial"

    def test_editing_does_not_rewrite_earlier_evaluations(self, session):
        run = _new_run(session)
        _add_observations(session, run.run_id, count=3)
        hyp = propose_hypothesis_service(session, run.run_id, "linear", "y = 3*x^2 + 2")
        repo = ClarityRepository(session)

        first_eval, _ = reevaluate_hypothesis_service(session, hyp.hypothesis_id)
        first_revision_id = first_eval.revision_id
        first_metrics = dict(first_eval.metrics)
        first_snapshot = first_eval.dataset_snapshot_id

        revised = revise_hypothesis_service(session, hyp.hypothesis_id, formula="y = 3*x^2 + 2.5")

        stored = repo.list_evaluations_for_revision(first_revision_id)[0]
        assert stored.metrics == first_metrics
        assert stored.dataset_snapshot_id == first_snapshot
        assert stored.revision_id != revised.revision_id


# =============================================================================
# C. Lifecycle actions are audited but never scientific
# =============================================================================


class TestLifecycleIntegrity:
    def test_pin_and_unpin_are_audited_without_touching_science(self, session):
        run = _new_run(session)
        _add_observations(session, run.run_id, count=3)
        hyp = propose_hypothesis_service(session, run.run_id, "linear", "y = 3*x^2 + 2")
        repo = ClarityRepository(session)
        evaluation, _ = reevaluate_hypothesis_service(session, hyp.hypothesis_id)
        metrics_before = dict(
            repo.list_evaluations_for_revision(evaluation.revision_id)[0].metrics
        )
        revisions_before = len(repo.list_hypothesis_revisions(hyp.hypothesis_id))

        pin_hypothesis_service(session, hyp.hypothesis_id, reason="promising")
        unpin_hypothesis_service(session, hyp.hypothesis_id)

        events = [
            e for e in repo.list_audit_events_for_run(run.run_id)
            if e.hypothesis_id == hyp.hypothesis_id
            and e.event_type in {"HYPOTHESIS_PINNED", "HYPOTHESIS_UNPINNED"}
        ]
        assert [e.event_type for e in events] == ["HYPOTHESIS_PINNED", "HYPOTHESIS_UNPINNED"]
        assert events[0].before_json == {"is_pinned": False}
        assert events[0].after_json == {"is_pinned": True}
        assert events[0].reason == "promising"
        assert events[1].before_json == {"is_pinned": True}
        assert events[1].after_json == {"is_pinned": False}

        # A lifecycle flag is not a scientific revision and never alters metrics.
        assert len(repo.list_hypothesis_revisions(hyp.hypothesis_id)) == revisions_before
        stored = repo.list_evaluations_for_revision(evaluation.revision_id)[0]
        assert dict(stored.metrics) == metrics_before

    def test_exclude_and_restore_are_audited(self, session):
        run = _new_run(session)
        hyp = propose_hypothesis_service(session, run.run_id, "linear", "y = 2*x + 1")
        repo = ClarityRepository(session)

        exclude_hypothesis_service(session, hyp.hypothesis_id, reason="unlikely mechanism")
        restore_hypothesis_service(session, hyp.hypothesis_id)

        events = [
            e for e in repo.list_audit_events_for_run(run.run_id)
            if e.event_type in {"HYPOTHESIS_EXCLUDED", "HYPOTHESIS_RESTORED"}
        ]
        assert [e.event_type for e in events] == ["HYPOTHESIS_EXCLUDED", "HYPOTHESIS_RESTORED"]
        assert events[0].after_json == {"is_excluded": True}
        assert events[0].reason == "unlikely mechanism"
        assert events[1].after_json == {"is_excluded": False}
        assert repo.get_hypothesis(hyp.hypothesis_id).is_excluded is False


# =============================================================================
# D. Event integrity
# =============================================================================


class TestEventIntegrity:
    def test_events_have_stable_ids_utc_timestamps_and_actor(self, session):
        run = _new_run(session)
        event = record_audit_event_service(
            session=session,
            run_id=run.run_id,
            event_type="REPORT_GENERATED",
            actor="researcher",
            actor_id="researcher-7",
            correlation_id="req-123",
            metadata={"format": "json"},
            before={"reports": 0},
            after={"reports": 1},
            reason="export after review",
        )
        assert event.event_id.startswith("evt-")
        assert event.timestamp.tzinfo is not None
        assert event.timestamp.utcoffset() == timedelta(0)
        assert event.actor == "researcher"
        assert event.actor_id == "researcher-7"
        assert event.correlation_id == "req-123"
        assert event.before_json == {"reports": 0}
        assert event.after_json == {"reports": 1}
        assert event.reason == "export after review"

    def test_metadata_is_sanitized_before_storage(self, session):
        run = _new_run(session)
        event = record_audit_event_service(
            session=session,
            run_id=run.run_id,
            event_type="RUN_CONFIGURED",
            metadata={
                "password": "hunter2",
                "api_key": "sk-live-secret",
                "authorization": "Bearer abc123",
                "nested": {"token": "tok-123", "limit": 5},
            },
        )
        raw = str(event.metadata_json)
        for secret in ("hunter2", "sk-live-secret", "Bearer abc123", "tok-123"):
            assert secret not in raw
        assert event.metadata_json["nested"]["limit"] == 5

    def test_audit_events_are_append_only(self, db):
        with db.session_scope() as setup:
            run = _new_run(setup)
            event = record_audit_event_service(
                session=setup, run_id=run.run_id, event_type="RUN_STARTED"
            )
            event_id = event.event_id

        with db.session_scope() as tamper:
            stored = ClarityRepository(tamper).get_audit_event(event_id)
            stored.event_type = "RUN_FAILED"
            with pytest.raises(AppendOnlyViolationError):
                tamper.flush()
            tamper.rollback()

        with db.session_scope() as verify:
            assert ClarityRepository(verify).get_audit_event(event_id).event_type == "RUN_STARTED"

    def test_audit_events_cannot_be_deleted(self, db):
        with db.session_scope() as setup:
            run = _new_run(setup)
            event = record_audit_event_service(
                session=setup, run_id=run.run_id, event_type="RUN_STARTED"
            )
            event_id = event.event_id

        with db.session_scope() as tamper:
            stored = ClarityRepository(tamper).get_audit_event(event_id)
            tamper.delete(stored)
            with pytest.raises(AppendOnlyViolationError):
                tamper.flush()
            tamper.rollback()

        with db.session_scope() as verify:
            assert ClarityRepository(verify).get_audit_event(event_id) is not None

    def test_reason_text_is_sanitized(self, session):
        run = _new_run(session)
        hyp = propose_hypothesis_service(session, run.run_id, "linear", "y = 2*x + 1")
        revision = revise_hypothesis_service(
            session,
            hyp.hypothesis_id,
            formula="y = 2*x + 12",
            reason="see token=abc123secret for the trace",
        )
        assert "abc123secret" not in (revision.change_reason or "")
        assert "see token=***" in revision.change_reason

        repo = ClarityRepository(session)
        event = [
            e for e in repo.list_audit_events_for_run(run.run_id)
            if e.event_type == AUDIT_HYPOTHESIS_REVISED
        ][0]
        assert "abc123secret" not in (event.reason or "")

    def test_corrections_append_and_reference_the_original(self, session):
        run = _new_run(session)
        original = record_audit_event_service(
            session=session, run_id=run.run_id, event_type="RUN_CONFIGURED",
            metadata={"max_queries": 10},
        )
        correction = correct_audit_event_service(
            session=session,
            run_id=run.run_id,
            corrects_event_id=original.event_id,
            reason="budget was recorded against the wrong run",
            actor="researcher",
        )
        assert correction.event_type == "AUDIT_EVENT_CORRECTED"
        assert correction.corrects_event_id == original.event_id
        assert correction.before_json["event_id"] == original.event_id
        assert correction.reason == "budget was recorded against the wrong run"

        repo = ClarityRepository(session)
        stored_original = repo.get_audit_event(original.event_id)
        assert stored_original.event_type == "RUN_CONFIGURED"
        assert stored_original.metadata_json == {"max_queries": 10}

    def test_correction_requires_an_existing_target(self, session):
        run = _new_run(session)
        with pytest.raises(AuditCorrectionTargetNotFound):
            correct_audit_event_service(
                session=session,
                run_id=run.run_id,
                corrects_event_id="evt-does-not-exist",
                reason="typo",
            )

    def test_duplicate_event_id_with_conflicting_payload_is_rejected(self, session):
        run = _new_run(session)
        record_audit_event_service(
            session=session,
            run_id=run.run_id,
            event_type="RUN_STARTED",
            event_id="evt-fixed-001",
            metadata={"phase": "initializing"},
        )
        from app.domain.usage_contracts import AuditEventConflictError

        with pytest.raises(AuditEventConflictError):
            record_audit_event_service(
                session=session,
                run_id=run.run_id,
                event_type="RUN_STARTED",
                event_id="evt-fixed-001",
                metadata={"phase": "running"},
            )


# =============================================================================
# E. History retrieval
# =============================================================================


class TestHistoryRetrieval:
    def test_events_are_returned_in_deterministic_chronological_order(self, session):
        run = _new_run(session)
        base = datetime(2026, 10, 23, 12, 0, 0, tzinfo=timezone.utc)
        # Same timestamp for two events: the event id must break the tie.
        record_audit_event_service(
            session, run.run_id, "TASK_STARTED", event_id="evt-b", timestamp=base
        )
        record_audit_event_service(
            session, run.run_id, "TASK_STARTED", event_id="evt-a", timestamp=base
        )
        record_audit_event_service(
            session, run.run_id, "TASK_COMPLETED", event_id="evt-c",
            timestamp=base + timedelta(seconds=1),
        )

        page = list_run_history(session, run.run_id, limit=10)
        mine = [
            e.event_id for e in page.events
            if e.event_id in {"evt-a", "evt-b", "evt-c"}
        ]
        assert mine == ["evt-a", "evt-b", "evt-c"]

    def test_cursor_pagination_returns_every_event_exactly_once(self, session):
        run = _new_run(session)
        base = datetime(2026, 10, 23, 9, 0, 0, tzinfo=timezone.utc)
        for i in range(5):
            record_audit_event_service(
                session=session,
                run_id=run.run_id,
                event_type="TASK_STARTED",
                event_id=f"evt-page-{i}",
                timestamp=base + timedelta(milliseconds=i),
            )

        seen = []
        cursor = None
        pages = 0
        while True:
            page = list_run_history(
                session, run.run_id, event_type="TASK_STARTED", limit=2, cursor=cursor
            )
            seen.extend(e.event_id for e in page.events)
            pages += 1
            if not page.has_more:
                assert page.next_cursor is None
                break
            cursor = page.next_cursor
            assert cursor

        assert pages == 3
        assert seen == [f"evt-page-{i}" for i in range(5)]

    def test_offset_pagination_reports_totals(self, session):
        run = _new_run(session)
        for i in range(3):
            record_audit_event_service(
                session, run.run_id, "TASK_STARTED", event_id=f"evt-off-{i}"
            )
        page = list_run_history(session, run.run_id, limit=2, offset=1)
        assert page.total >= 3
        assert page.offset == 1
        assert page.has_more is True

    def test_event_type_filter(self, session):
        run = _new_run(session)
        record_audit_event_service(session, run.run_id, "REPORT_GENERATED", event_id="evt-r1")
        record_audit_event_service(session, run.run_id, "TASK_STARTED", event_id="evt-t1")

        page = list_run_history(session, run.run_id, event_type="REPORT_GENERATED")
        assert [e.event_id for e in page.events] == ["evt-r1"]
        assert page.total == 1

    def test_hypothesis_filter_scopes_history_to_one_candidate(self, session):
        run = _new_run(session)
        first = propose_hypothesis_service(session, run.run_id, "linear", "y = 2*x + 1")
        second = propose_hypothesis_service(session, run.run_id, "linear", "y = 5*x + 1")

        page = list_run_history(session, run.run_id, hypothesis_id=first.hypothesis_id)
        assert page.events
        assert {e.hypothesis_id for e in page.events} == {first.hypothesis_id}

    def test_empty_history_is_a_valid_response(self, session):
        run = _new_run(session)
        page = list_run_history(session, run.run_id, event_type="NEVER_RECORDED")
        assert page.events == []
        assert page.total == 0
        assert page.next_cursor is None
        assert page.has_more is False

    def test_missing_run_is_distinguished_from_empty_history(self, session):
        with pytest.raises(HistoryNotFoundError):
            list_run_history(session, "run-does-not-exist")

    def test_missing_hypothesis_is_an_error_not_empty_history(self, session):
        with pytest.raises(HistoryNotFoundError):
            list_hypothesis_revisions(session, "hyp-does-not-exist")

    def test_missing_revision_is_an_error(self, session):
        run = _new_run(session)
        hyp = propose_hypothesis_service(session, run.run_id, "linear", "y = 2*x + 1")
        with pytest.raises(HistoryNotFoundError):
            list_revision_evaluations(session, "rev-does-not-exist", hypothesis_id=hyp.hypothesis_id)

    def test_revision_evaluation_history_rejects_foreign_revision(self, session):
        run = _new_run(session)
        first = propose_hypothesis_service(session, run.run_id, "linear", "y = 2*x + 1")
        second = propose_hypothesis_service(session, run.run_id, "linear", "y = 4*x + 1")
        revision = ClarityRepository(session).list_hypothesis_revisions(first.hypothesis_id)[0]
        with pytest.raises(HistoryNotFoundError):
            list_revision_evaluations(
                session, revision.revision_id, hypothesis_id=second.hypothesis_id
            )

    def test_invalid_cursor_is_rejected(self):
        with pytest.raises(InvalidHistoryCursorError):
            decode_history_cursor("not-a-valid-cursor!!")

    def test_cursor_round_trip(self):
        ts = datetime(2026, 10, 23, 8, 30, 15, tzinfo=timezone.utc)
        cursor = encode_history_cursor(ts, "evt-abc")
        decoded = decode_history_cursor(cursor)
        assert decoded is not None
        assert decoded[1] == "evt-abc"
        assert decoded[0] == ts


# =============================================================================
# F. Transactional consistency
# =============================================================================


class TestTransactionConsistency:
    def test_audit_write_failure_rolls_back_the_revision(self, db, monkeypatch):
        with db.session_scope() as setup:
            run = _new_run(setup)
            hyp = propose_hypothesis_service(setup, run.run_id, "linear", "y = 2*x + 1")
            run_id, hypothesis_id = run.run_id, hyp.hypothesis_id

        original = ClarityRepository.record_audit_event

        def exploding_record(self, event):  # noqa: ANN001, ANN202
            raise RuntimeError("audit store unavailable")

        monkeypatch.setattr(ClarityRepository, "record_audit_event", exploding_record)
        try:
            with pytest.raises(RuntimeError):
                with db.session_scope() as failing:
                    revise_hypothesis_service(
                        failing, hypothesis_id, formula="y = 2*x + 5"
                    )
        finally:
            monkeypatch.setattr(ClarityRepository, "record_audit_event", original)

        with db.session_scope() as verify:
            repo = ClarityRepository(verify)
            revisions = repo.list_hypothesis_revisions(hypothesis_id)
            assert [r.revision_number for r in revisions] == [1]
            assert repo.get_hypothesis(hypothesis_id).current_revision == 1
            assert repo.count_audit_events_for_run(
                run_id, event_type=AUDIT_HYPOTHESIS_REVISED
            ) == 0

    def test_failed_request_can_be_retried_safely(self, db, monkeypatch):
        with db.session_scope() as setup:
            run = _new_run(setup)
            hyp = propose_hypothesis_service(setup, run.run_id, "linear", "y = 2*x + 1")
            hypothesis_id = hyp.hypothesis_id

        original = ClarityRepository.record_audit_event

        def exploding_record(self, event):  # noqa: ANN001, ANN202
            raise RuntimeError("audit store unavailable")

        monkeypatch.setattr(ClarityRepository, "record_audit_event", exploding_record)
        try:
            with pytest.raises(RuntimeError):
                with db.session_scope() as failing:
                    revise_hypothesis_service(
                        failing,
                        hypothesis_id,
                        formula="y = 2*x + 7",
                        request_key="edit-key-1",
                    )
        finally:
            monkeypatch.setattr(ClarityRepository, "record_audit_event", original)

        with db.session_scope() as retry:
            revision = revise_hypothesis_service(
                retry, hypothesis_id, formula="y = 2*x + 7", request_key="edit-key-1"
            )
            assert revision.revision_number == 2
            assert revision.request_key == "edit-key-1"

    def test_revision_and_event_commit_together(self, session):
        run = _new_run(session)
        hyp = propose_hypothesis_service(session, run.run_id, "linear", "y = 2*x + 1")
        revise_hypothesis_service(session, hyp.hypothesis_id, formula="y = 2*x + 4")
        repo = ClarityRepository(session)
        revisions = repo.list_hypothesis_revisions(hyp.hypothesis_id)
        events = [
            e for e in repo.list_audit_events_for_run(run.run_id)
            if e.event_type == AUDIT_HYPOTHESIS_REVISED
        ]
        assert len(revisions) == 2
        assert len(events) == 1
        assert events[0].revision_id == revisions[-1].revision_id


# =============================================================================
# G. Idempotency
# =============================================================================


class TestIdempotency:
    def test_replay_returns_the_committed_revision(self, session):
        run = _new_run(session)
        hyp = propose_hypothesis_service(session, run.run_id, "linear", "y = 2*x + 1")
        first = revise_hypothesis_service(
            session, hyp.hypothesis_id, formula="y = 2*x + 6", request_key="key-abc"
        )
        replay = revise_hypothesis_service(
            session, hyp.hypothesis_id, formula="y = 2*x + 6", request_key="key-abc"
        )

        assert replay.revision_id == first.revision_id
        repo = ClarityRepository(session)
        assert len(repo.list_hypothesis_revisions(hyp.hypothesis_id)) == 2
        assert repo.count_audit_events_for_run(
            run.run_id, event_type=AUDIT_HYPOTHESIS_REVISED
        ) == 1

    def test_reused_key_with_different_model_family_is_rejected(self, session):
        """Formula/parameters/complexity match, only the family differs."""
        run = _new_run(session)
        hyp = propose_hypothesis_service(session, run.run_id, "linear", "y = 2*x + 1")
        first = revise_hypothesis_service(
            session,
            hyp.hypothesis_id,
            formula="y = 2*x + 6",
            model_family="linear",
            request_key="key-family",
        )
        assert first.request_fingerprint.startswith("req-")

        with pytest.raises(HypothesisConflictError):
            revise_hypothesis_service(
                session,
                hyp.hypothesis_id,
                formula="y = 2*x + 6",
                model_family="polynomial",
                request_key="key-family",
            )
        assert len(ClarityRepository(session).list_hypothesis_revisions(hyp.hypothesis_id)) == 2

    def test_replay_matches_the_full_canonical_request(self, session):
        run = _new_run(session)
        hyp = propose_hypothesis_service(session, run.run_id, "linear", "y = 2*x + 1")
        first = revise_hypothesis_service(
            session,
            hyp.hypothesis_id,
            formula="y = 2*x + 6",
            model_family="linear",
            parameters={"coefficients": {"c0": 6.0, "c1": 2.0}, "family": "polynomial", "degree": 1, "coefficient_order": "ascending"},
            complexity=2,
            request_key="key-canonical",
        )
        replay = revise_hypothesis_service(
            session,
            hyp.hypothesis_id,
            formula="y = 2*x + 6",
            model_family="linear",
            parameters={"coefficient_order": "ascending", "degree": 1, "family": "polynomial", "coefficients": {"c1": 2.0, "c0": 6.0}},
            complexity=2,
            request_key="key-canonical",
        )
        assert replay.revision_id == first.revision_id

    def test_reused_key_with_different_payload_is_rejected(self, session):
        run = _new_run(session)
        hyp = propose_hypothesis_service(session, run.run_id, "linear", "y = 2*x + 1")
        revise_hypothesis_service(
            session, hyp.hypothesis_id, formula="y = 2*x + 6", request_key="key-xyz"
        )
        with pytest.raises(HypothesisConflictError):
            revise_hypothesis_service(
                session, hyp.hypothesis_id, formula="y = 2*x + 9", request_key="key-xyz"
            )
        assert len(ClarityRepository(session).list_hypothesis_revisions(hyp.hypothesis_id)) == 2

    def test_request_key_is_unique_per_hypothesis_in_the_database(self, session):
        """The constraint, not an in-memory check, protects concurrent duplicates."""
        from sqlalchemy.exc import IntegrityError
        from app.adapters.persistence.models import HypothesisRevisionModel

        run = _new_run(session)
        hyp = propose_hypothesis_service(session, run.run_id, "linear", "y = 2*x + 1")
        revise_hypothesis_service(
            session, hyp.hypothesis_id, formula="y = 2*x + 6", request_key="unique-key"
        )

        session.add(
            HypothesisRevisionModel(
                revision_id="rev-race-1",
                hypothesis_id=hyp.hypothesis_id,
                revision_number=99,
                formula="y = 2*x + 7",
                parameters={},
                complexity=2,
                creation_source="researcher",
                request_key="unique-key",
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()

    def test_validation_failure_does_not_consume_the_key(self, session):
        run = _new_run(session)
        hyp = propose_hypothesis_service(session, run.run_id, "linear", "y = 2*x + 1")
        with pytest.raises(HypothesisValidationError):
            revise_hypothesis_service(
                session, hyp.hypothesis_id, formula="os.system('x')", request_key="retry-key"
            )
        revision = revise_hypothesis_service(
            session, hyp.hypothesis_id, formula="y = 2*x + 8", request_key="retry-key"
        )
        assert revision.revision_number == 2


# =============================================================================
# H. Run failure and cancellation preserve history
# =============================================================================


class TestRunFailurePreservesHistory:
    def test_cancellation_preserves_revisions_and_evaluations(self, session):
        run = _new_run(session)
        _add_observations(session, run.run_id, count=3)
        hyp = propose_hypothesis_service(session, run.run_id, "linear", "y = 3*x^2 + 2")
        evaluation, _ = reevaluate_hypothesis_service(session, hyp.hypothesis_id)
        revise_hypothesis_service(session, hyp.hypothesis_id, formula="y = 3*x^2 + 2.25")

        cancel_run_service(session, run.run_id, stop_reason="Cancelled by researcher")

        repo = ClarityRepository(session)
        assert repo.get_run(run.run_id).status == "cancelled"
        assert [r.revision_number for r in repo.list_hypothesis_revisions(hyp.hypothesis_id)] == [1, 2]
        stored = repo.list_evaluations_for_revision(evaluation.revision_id)
        assert len(stored) == 1
        assert stored[0].status == "completed"
        types = _event_types(session, run.run_id)
        assert "HYPOTHESIS_PROPOSED" in types
        assert AUDIT_HYPOTHESIS_REVISED in types
        assert "RUN_CANCELLED" in types

    def test_run_failure_preserves_prior_events(self, session):
        run = _new_run(session)
        hyp = propose_hypothesis_service(session, run.run_id, "linear", "y = 2*x + 1")
        revise_hypothesis_service(session, hyp.hypothesis_id, formula="y = 2*x + 3")
        repo = ClarityRepository(session)
        before = _event_types(session, run.run_id)

        repo.update_run_status(run.run_id, "failed", "EXECUTION_FAILURE")
        record_audit_event_service(
            session=session,
            run_id=run.run_id,
            event_type="RUN_FAILED",
            actor="system",
            metadata={"error_code": "EXECUTION_FAILURE"},
        )

        after = _event_types(session, run.run_id)
        assert after[: len(before)] == before
        assert after[-1] == "RUN_FAILED"
        assert len(repo.list_hypothesis_revisions(hyp.hypothesis_id)) == 2


# =============================================================================
# API contract
# =============================================================================


class TestHistoryAPI:
    def _create_hypothesis(self, client, run_id):
        response = client.post(
            f"/api/v1/runs/{run_id}/hypotheses",
            json={"model_family": "linear", "formula": "y = 2*x + 1"},
        )
        assert response.status_code == 201, response.get_json()
        return response.get_json()["hypothesis"]["hypothesis_id"]

    def _create_run(self, client):
        response = client.post("/api/v1/runs", json={"name": "API History Run"})
        assert response.status_code == 201, response.get_json()
        return response.get_json()["run"]["run_id"]

    def test_create_revision_returns_201_and_revision_history(self, client):
        run_id = self._create_run(client)
        hypothesis_id = self._create_hypothesis(client, run_id)

        response = client.post(
            f"/api/v1/hypotheses/{hypothesis_id}/revisions",
            json={"formula": "y = 3*x^2 + 2", "reason": "quadratic mechanism"},
            headers={"X-Request-ID": "req-api-1"},
        )
        assert response.status_code == 201, response.get_json()
        body = response.get_json()
        assert body["revision"]["revision_number"] == 2
        assert body["revision"]["model_family"] == "polynomial"
        assert body["revision"]["change_reason"] == "quadratic mechanism"
        assert body["current_revision"] == 2

        history = client.get(f"/api/v1/hypotheses/{hypothesis_id}/revisions")
        assert history.status_code == 200
        revisions = history.get_json()["revisions"]
        assert [r["revision_number"] for r in revisions] == [1, 2]
        assert revisions[1]["previous_revision_id"] == revisions[0]["revision_id"]
        assert revisions[0]["formula"] == "y = 2*x + 1"
        assert revisions[1]["model_family"] == "polynomial"
        assert revisions[1]["created_at"].endswith("+00:00")

    def test_edit_api_rejects_stale_revision_with_409(self, client):
        run_id = self._create_run(client)
        hypothesis_id = self._create_hypothesis(client, run_id)
        client.post(
            f"/api/v1/hypotheses/{hypothesis_id}/revisions",
            json={"formula": "y = 2*x + 2", "expected_revision": 1},
        )
        stale = client.post(
            f"/api/v1/hypotheses/{hypothesis_id}/revisions",
            json={"formula": "y = 2*x + 3", "expected_revision": 1},
        )
        assert stale.status_code == 409

    def test_edit_api_rejects_invalid_formula_with_422(self, client):
        run_id = self._create_run(client)
        hypothesis_id = self._create_hypothesis(client, run_id)
        response = client.post(
            f"/api/v1/hypotheses/{hypothesis_id}/revisions",
            json={"formula": "__import__('os')"},
        )
        assert response.status_code == 422

    def test_edit_api_requires_a_change(self, client):
        run_id = self._create_run(client)
        hypothesis_id = self._create_hypothesis(client, run_id)
        response = client.post(
            f"/api/v1/hypotheses/{hypothesis_id}/revisions", json={"reason": "no change"}
        )
        assert response.status_code == 400

    def test_idempotency_key_replay_returns_200(self, client):
        run_id = self._create_run(client)
        hypothesis_id = self._create_hypothesis(client, run_id)
        payload = {"formula": "y = 2*x + 11"}
        first = client.post(
            f"/api/v1/hypotheses/{hypothesis_id}/revisions",
            json=payload,
            headers={"Idempotency-Key": "api-key-1"},
        )
        assert first.status_code == 201
        second = client.post(
            f"/api/v1/hypotheses/{hypothesis_id}/revisions",
            json=payload,
            headers={"Idempotency-Key": "api-key-1"},
        )
        assert second.status_code == 200
        assert second.get_json()["revision"]["revision_id"] == first.get_json()["revision"]["revision_id"]
        assert second.get_json()["idempotent_replay"] is True

    def test_run_history_endpoint_returns_cursor_pagination(self, client):
        run_id = self._create_run(client)
        hypothesis_id = self._create_hypothesis(client, run_id)
        for i in range(3):
            client.post(
                f"/api/v1/hypotheses/{hypothesis_id}/revisions",
                json={"formula": f"y = {i + 3}*x + 1"},
            )

        first_page = client.get(f"/api/v1/runs/{run_id}/history?limit=2")
        assert first_page.status_code == 200
        body = first_page.get_json()
        assert body["pagination"]["count"] == 2
        assert body["pagination"]["next_cursor"]
        assert body["events"][0]["timestamp"].endswith("+00:00")

        cursor = body["pagination"]["next_cursor"]
        seen = [e["event_id"] for e in body["events"]]
        while cursor:
            page = client.get(f"/api/v1/runs/{run_id}/history?limit=2&cursor={cursor}")
            assert page.status_code == 200
            page_body = page.get_json()
            seen.extend(e["event_id"] for e in page_body["events"])
            cursor = page_body["pagination"]["next_cursor"]

        assert len(seen) == len(set(seen))
        assert "HYPOTHESIS_REVISED" in {
            e["event_type"] for e in client.get(f"/api/v1/runs/{run_id}/history?limit=200").get_json()["events"]
        }

    def test_run_history_endpoint_filters(self, client):
        run_id = self._create_run(client)
        hypothesis_id = self._create_hypothesis(client, run_id)
        client.post(
            f"/api/v1/hypotheses/{hypothesis_id}/revisions", json={"formula": "y = 2*x + 21"}
        )

        response = client.get(
            f"/api/v1/runs/{run_id}/history?event_type=HYPOTHESIS_REVISED"
        )
        assert response.status_code == 200
        events = response.get_json()["events"]
        assert len(events) == 1
        assert events[0]["hypothesis_id"] == hypothesis_id
        assert events[0]["before"]["formula"] == "y = 2*x + 1"
        assert events[0]["after"]["formula"] == "y = 2*x + 21"

    def test_run_history_endpoint_returns_404_for_unknown_run(self, client):
        assert client.get("/api/v1/runs/run-missing/history").status_code == 404

    def test_run_history_endpoint_rejects_malformed_cursor(self, client):
        run_id = self._create_run(client)
        response = client.get(f"/api/v1/runs/{run_id}/history?cursor=%%%not-a-cursor")
        assert response.status_code == 400

    def test_revision_evaluation_history_endpoint(self, client):
        run_id = self._create_run(client)
        hypothesis_id = self._create_hypothesis(client, run_id)
        reevaluate = client.post(f"/api/v1/hypotheses/{hypothesis_id}/reevaluate")
        assert reevaluate.status_code in {200, 202}
        revision_id = reevaluate.get_json()["evaluation"]["revision_id"]

        response = client.get(
            f"/api/v1/hypotheses/{hypothesis_id}/revisions/{revision_id}/evaluations"
        )
        assert response.status_code == 200
        body = response.get_json()
        assert body["revision_id"] == revision_id
        assert body["evaluations"]
        assert body["evaluations"][0]["revision_id"] == revision_id

    def test_revision_evaluation_history_endpoint_404s_for_foreign_revision(self, client):
        run_id = self._create_run(client)
        hypothesis_id = self._create_hypothesis(client, run_id)
        response = client.get(
            f"/api/v1/hypotheses/{hypothesis_id}/revisions/rev-missing/evaluations"
        )
        assert response.status_code == 404

    def test_revisions_endpoint_404s_for_unknown_hypothesis(self, client):
        assert client.get("/api/v1/hypotheses/hyp-missing/revisions").status_code == 404
