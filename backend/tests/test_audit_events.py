"""Unit and integration tests for append-only audit trail and sensitive data redaction (Issue #15).

Verifies:
1. Run, task, oracle, hypothesis, budget, and report lifecycle events are faithfully recorded.
2. Audit trail is append-only: events cannot be overwritten or deleted.
3. Chronological event ordering and pagination.
4. Event filtering by event_type.
5. Sensitive credentials, passwords, tokens, API keys, and connection strings are scrubbed.
6. Idempotency: duplicate event delivery does not create duplicate audit events.
7. Audit history remains fully accessible after run failure or budget exhaustion.
8. REST API endpoints for audit event querying and append-only submission.
"""

from datetime import datetime, timedelta, timezone
import pytest

from app.adapters.persistence.database import get_db_manager
from app.adapters.persistence.models import AuditEventModel, RunModel
from app.adapters.persistence.repository import ClarityRepository
from app.domain.usage_contracts import (
    AUDIT_BUDGET_EXHAUSTED,
    AUDIT_HYPOTHESIS_PINNED,
    AUDIT_HYPOTHESIS_PROPOSED,
    AUDIT_ORACLE_QUERY_EXECUTED,
    AUDIT_REPORT_GENERATED,
    AUDIT_RUN_COMPLETED,
    AUDIT_RUN_CREATED,
    AUDIT_RUN_FAILED,
    AUDIT_TASK_COMPLETED,
    AUDIT_TASK_SUBMITTED,
    AuditEventConflictError,
)
from app.services.audit_service import (
    list_audit_events_service,
    record_audit_event_service,
    sanitize_metadata,
)
from app.services.run_service import cancel_run_service, create_run_service


@pytest.fixture
def db_session(app):
    """Provide an isolated database session."""
    db_uri = app.config.get("SQLALCHEMY_DATABASE_URI")
    db = get_db_manager(db_uri)
    with db.session_scope() as session:
        yield session


def _create_run(session, name="Audit Test Run") -> RunModel:
    return create_run_service(
        session=session,
        name=name,
        max_queries=5,
    )


class TestAuditEventsLogging:
    def test_record_lifecycle_audit_events(self, db_session):
        """Important run lifecycle events are recorded in append-only fashion."""
        run = _create_run(db_session)

        # 1. RUN_CREATED was recorded on creation
        repo = ClarityRepository(db_session)
        events = repo.list_audit_events_for_run(run.run_id)
        assert len(events) >= 1
        assert events[0].event_type == AUDIT_RUN_CREATED

        # 2. Record task completion
        record_audit_event_service(
            session=db_session,
            run_id=run.run_id,
            event_type=AUDIT_TASK_COMPLETED,
            actor="orchestrator",
            metadata={"task_id": "task-001", "duration_ms": 120},
        )

        # 3. Record oracle query
        record_audit_event_service(
            session=db_session,
            run_id=run.run_id,
            event_type=AUDIT_ORACLE_QUERY_EXECUTED,
            actor="orchestrator",
            metadata={"query_index": 1, "input": {"x": 2.5}},
        )

        # 4. Record hypothesis proposed
        record_audit_event_service(
            session=db_session,
            run_id=run.run_id,
            event_type=AUDIT_HYPOTHESIS_PROPOSED,
            actor="researcher",
            metadata={"hypothesis_id": "hyp-001", "formula": "y = 2*x"},
        )

        events_after = repo.list_audit_events_for_run(run.run_id)
        event_types = [e.event_type for e in events_after]
        assert AUDIT_RUN_CREATED in event_types
        assert AUDIT_TASK_COMPLETED in event_types
        assert AUDIT_ORACLE_QUERY_EXECUTED in event_types
        assert AUDIT_HYPOTHESIS_PROPOSED in event_types

    def test_chronological_ordering_and_pagination(self, db_session):
        """Audit events are returned strictly ordered by timestamp with pagination."""
        run = _create_run(db_session)
        base_time = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

        for i in range(10):
            record_audit_event_service(
                session=db_session,
                run_id=run.run_id,
                event_type=f"STEP_{i}",
                actor="system",
                timestamp=base_time + timedelta(seconds=i * 10),
            )

        events, total = list_audit_events_service(
            session=db_session,
            run_id=run.run_id,
            limit=5,
            offset=0,
        )
        assert len(events) == 5
        assert total >= 11  # including RUN_CREATED, BUDGET_CONFIGURED
        # Verify chronological ordering
        for i in range(len(events) - 1):
            assert events[i].timestamp <= events[i + 1].timestamp

        # Offset test
        page_2, _ = list_audit_events_service(
            session=db_session,
            run_id=run.run_id,
            limit=5,
            offset=5,
        )
        assert len(page_2) == 5
        assert page_2[0].event_id != events[0].event_id

    def test_filter_by_event_type(self, db_session):
        """Events can be filtered by specific event_type."""
        run = _create_run(db_session)

        record_audit_event_service(db_session, run.run_id, AUDIT_HYPOTHESIS_PINNED, metadata={"id": "h1"})
        record_audit_event_service(db_session, run.run_id, AUDIT_TASK_COMPLETED, metadata={"id": "t1"})
        record_audit_event_service(db_session, run.run_id, AUDIT_HYPOTHESIS_PINNED, metadata={"id": "h2"})

        pinned_events, count = list_audit_events_service(
            session=db_session,
            run_id=run.run_id,
            event_type=AUDIT_HYPOTHESIS_PINNED,
        )
        assert count == 2
        assert all(e.event_type == AUDIT_HYPOTHESIS_PINNED for e in pinned_events)


class TestIdempotencyAndAppendOnly:
    def test_duplicate_event_id_with_identical_content_is_idempotent(self, db_session):
        """Submitting an event with the same event_id and identical content returns the existing row."""
        run = _create_run(db_session)
        fixed_id = "evt-fixed-id-12345"

        e1 = record_audit_event_service(
            session=db_session,
            run_id=run.run_id,
            event_type=AUDIT_TASK_COMPLETED,
            event_id=fixed_id,
            metadata={"attempt": 1},
        )
        e2 = record_audit_event_service(
            session=db_session,
            run_id=run.run_id,
            event_type=AUDIT_TASK_COMPLETED,
            event_id=fixed_id,
            metadata={"attempt": 1},
        )

        assert e1.event_id == e2.event_id
        repo = ClarityRepository(db_session)
        events = [e for e in repo.list_audit_events_for_run(run.run_id) if e.event_id == fixed_id]
        assert len(events) == 1
        assert events[0].metadata_json["attempt"] == 1

    def test_conflicting_event_id_payload_is_rejected(self, db_session):
        """Reusing an event_id with conflicting metadata payload raises AuditEventConflictError."""
        run = _create_run(db_session)
        fixed_id = "evt-conflict-payload"

        record_audit_event_service(
            session=db_session,
            run_id=run.run_id,
            event_type=AUDIT_TASK_COMPLETED,
            event_id=fixed_id,
            metadata={"attempt": 1},
        )

        with pytest.raises(AuditEventConflictError, match="metadata payload mismatch"):
            record_audit_event_service(
                session=db_session,
                run_id=run.run_id,
                event_type=AUDIT_TASK_COMPLETED,
                event_id=fixed_id,
                metadata={"attempt": 2},
            )

    def test_cross_run_event_id_reuse_is_rejected(self, db_session):
        """Reusing an event_id belonging to another run raises AuditEventConflictError."""
        run1 = _create_run(db_session, name="Run 1")
        run2 = _create_run(db_session, name="Run 2")
        fixed_id = "evt-cross-run-shared"

        record_audit_event_service(
            session=db_session,
            run_id=run1.run_id,
            event_type=AUDIT_TASK_COMPLETED,
            event_id=fixed_id,
            metadata={"metric": 0.95},
        )

        with pytest.raises(AuditEventConflictError, match="run_id mismatch"):
            record_audit_event_service(
                session=db_session,
                run_id=run2.run_id,
                event_type=AUDIT_TASK_COMPLETED,
                event_id=fixed_id,
                metadata={"metric": 0.95},
            )


class TestSensitiveDataRedaction:
    def test_credentials_and_secrets_scrubbed_from_metadata(self, db_session):
        """Sensitive fields like passwords, tokens, API keys are completely redacted."""
        run = _create_run(db_session)
        sensitive_payload = {
            "api_key": "sk-live-secret-nodus-12345",
            "password": "super-secret-password",
            "auth_token": "bearer xyz789",
            "db_connection": "mysql+pymysql://root:mypassword123@localhost:3306/clarity",
            "safe_field": "public_data",
            "nested": {
                "secret_key": "private_value",
                "normal": 42,
            },
        }

        event = record_audit_event_service(
            session=db_session,
            run_id=run.run_id,
            event_type="TEST_REDACTION",
            metadata=sensitive_payload,
        )

        meta = event.metadata_json
        assert meta["api_key"] == "***REDACTED***"
        assert meta["password"] == "***REDACTED***"
        assert meta["auth_token"] == "***REDACTED***"
        assert "mypassword123" not in meta["db_connection"]
        assert meta["safe_field"] == "public_data"
        assert meta["nested"]["secret_key"] == "***REDACTED***"
        assert meta["nested"]["normal"] == 42

    def test_bearer_token_and_auth_headers_redacted_in_free_form_strings(self, db_session):
        """Raw Authorization headers and Bearer tokens are scrubbed in ordinary metadata keys like error_message."""
        run = _create_run(db_session)
        payload = {
            "error_message": "Remote call failed: Authorization: Bearer abc123def456 from client",
            "details": "Invalid credentials: Bearer my-secret-jwt-token-789",
            "log_line": "Request headers sent: {'Authorization': 'Bearer raw-header-token-000'}",
            "http_status": "401 Unauthorized",
            "nested": {
                "raw_exception": "Exception: Bearer inline-nested-secret failed to authenticate",
                "normal_field": "ordinary_value",
            },
        }

        event = record_audit_event_service(
            session=db_session,
            run_id=run.run_id,
            event_type="AUTH_FAILURE_LOG",
            metadata=payload,
        )

        meta = event.metadata_json
        # Raw bearer tokens must be scrubbed, never leaked
        assert "abc123def456" not in meta["error_message"]
        assert "Bearer ***" in meta["error_message"]

        assert "my-secret-jwt-token-789" not in meta["details"]
        assert "Bearer ***" in meta["details"]

        assert "raw-header-token-000" not in meta["log_line"]
        assert "Bearer ***" in meta["log_line"]

        assert meta["http_status"] == "401 Unauthorized"

        assert "inline-nested-secret" not in meta["nested"]["raw_exception"]
        assert "Bearer ***" in meta["nested"]["raw_exception"]
        assert meta["nested"]["normal_field"] == "ordinary_value"

    def test_sanitize_exception_text_redacts_embedded_bearer_headers(self):
        """sanitize_exception_text scrubs Authorization headers and Bearer tokens directly."""
        from app.domain.sanitization import sanitize_exception_text

        # Header style with Bearer
        assert sanitize_exception_text("Authorization: Bearer secret123") == "Authorization: Bearer ***"
        # Standalone Bearer token
        assert sanitize_exception_text("Failed due to Bearer sk-secret-abc") == "Failed due to Bearer ***"
        # Basic auth in header
        assert sanitize_exception_text("Authorization: Basic dXNlcjpwYXNz") == "Authorization: Basic ***"
        # Quoted Bearer token
        assert "secret123" not in sanitize_exception_text("Authorization: Bearer 'secret123'")



class TestAuditPersistenceAcrossFailureAndBudget:
    def test_audit_history_retained_after_run_cancelled(self, db_session):
        """Cancelled runs preserve their full historical audit events."""
        run = _create_run(db_session)
        record_audit_event_service(db_session, run.run_id, "EARLY_WORK")

        cancel_run_service(db_session, run.run_id, stop_reason="Researcher stopped early")

        repo = ClarityRepository(db_session)
        events = repo.list_audit_events_for_run(run.run_id)
        event_types = [e.event_type for e in events]
        assert "RUN_CANCELLED" in event_types
        assert "EARLY_WORK" in event_types

    def test_audit_history_retained_after_budget_exhaustion(self, db_session):
        """Budget exhausted runs retain all prior audit events."""
        run = _create_run(db_session)
        record_audit_event_service(db_session, run.run_id, "PRE_BUDGET_WORK")
        record_audit_event_service(
            db_session,
            run.run_id,
            AUDIT_BUDGET_EXHAUSTED,
            actor="orchestrator",
            metadata={"limit": 5, "used": 5, "resource": "oracle_query"},
        )

        events, total = list_audit_events_service(db_session, run.run_id)
        assert total >= 3
        types = [e.event_type for e in events]
        assert AUDIT_BUDGET_EXHAUSTED in types
        assert "PRE_BUDGET_WORK" in types


class TestAuditApiEndpoints:
    def test_list_audit_events_endpoint(self, client):
        """GET /api/v1/runs/<run_id>/audit-events returns chronological trail."""
        res_run = client.post("/api/v1/runs", json={"name": "API Audit Run"})
        run_id = res_run.get_json()["run"]["run_id"]

        res = client.get(f"/api/v1/runs/{run_id}/audit-events")
        assert res.status_code == 200
        data = res.get_json()
        assert "audit_events" in data
        assert data["count"] >= 1
        assert data["audit_events"][0]["event_type"] == "RUN_CREATED"

    def test_post_audit_event_endpoint(self, client):
        """POST /api/v1/runs/<run_id>/audit-events appends sanitized event."""
        res_run = client.post("/api/v1/runs", json={"name": "Post Audit Run"})
        run_id = res_run.get_json()["run"]["run_id"]

        res = client.post(
            f"/api/v1/runs/{run_id}/audit-events",
            json={
                "event_type": "RESEARCHER_NOTE",
                "actor": "dr_smith",
                "metadata": {"note": "Promising linear candidate", "password": "dont_leak_me"},
            },
        )
        assert res.status_code == 201
        event = res.get_json()["audit_event"]
        assert event["event_type"] == "RESEARCHER_NOTE"
        assert event["actor"] == "dr_smith"
        assert event["metadata"]["note"] == "Promising linear candidate"
        assert event["metadata"]["password"] == "***REDACTED***"

    def test_post_audit_event_conflict_returns_409(self, client):
        """POST /api/v1/runs/<run_id>/audit-events returns 409 on cross-run or conflicting reuse."""
        r1 = client.post("/api/v1/runs", json={"name": "Run Conflict 1"}).get_json()["run"]["run_id"]
        r2 = client.post("/api/v1/runs", json={"name": "Run Conflict 2"}).get_json()["run"]["run_id"]

        # Create event in run 1
        res1 = client.post(
            f"/api/v1/runs/{r1}/audit-events",
            json={"event_id": "evt-api-fixed-1", "event_type": "NOTE", "metadata": {"x": 1}},
        )
        assert res1.status_code == 201

        # Attempt to reuse the same event_id in run 2 (cross-run conflict)
        res_cross = client.post(
            f"/api/v1/runs/{r2}/audit-events",
            json={"event_id": "evt-api-fixed-1", "event_type": "NOTE", "metadata": {"x": 1}},
        )
        assert res_cross.status_code == 409
        assert "run_id mismatch" in res_cross.get_json()["message"]

        # Attempt to reuse with conflicting payload in run 1
        res_conflict_payload = client.post(
            f"/api/v1/runs/{r1}/audit-events",
            json={"event_id": "evt-api-fixed-1", "event_type": "NOTE", "metadata": {"x": 999}},
        )
        assert res_conflict_payload.status_code == 409
        assert "metadata payload mismatch" in res_conflict_payload.get_json()["message"]

    def test_post_audit_event_scrubs_embedded_bearer_headers(self, client):
        """POST /api/v1/runs/<run_id>/audit-events scrubs embedded raw bearer tokens under non-sensitive keys."""
        res_run = client.post("/api/v1/runs", json={"name": "API Bearer Scrub Run"})
        run_id = res_run.get_json()["run"]["run_id"]

        res = client.post(
            f"/api/v1/runs/{run_id}/audit-events",
            json={
                "event_type": "TASK_ERROR_LOG",
                "actor": "worker-1",
                "metadata": {
                    "error_message": "Upstream service error: Authorization: Bearer secret-raw-token-4433",
                    "log_context": "Authenticated via Bearer internal-secret-token",
                },
            },
        )
        assert res.status_code == 201
        event = res.get_json()["audit_event"]
        meta = event["metadata"]
        assert "secret-raw-token-4433" not in meta["error_message"]
        assert "Bearer ***" in meta["error_message"]
        assert "internal-secret-token" not in meta["log_context"]
        assert "Bearer ***" in meta["log_context"]


