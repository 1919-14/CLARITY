"""add researcher history provenance to revisions, evaluations and audit events (Issue #17)

Revision ID: a3b4c5d6e7f8
Revises: f7a8b9c0d1e2
Create Date: 2026-10-23 23:10:00.000000

Adds (all nullable, so existing rows stay valid):

hypothesis_revisions
- previous_revision_id: chain to the preceding revision of the same hypothesis
- change_reason: optional researcher-supplied explanation
- actor / actor_id: who produced this revision
- request_key: client idempotency key, unique per hypothesis

evaluation_results
- dataset_snapshot_id: deterministic fingerprint of the exact observation set used

audit_events
- hypothesis_id, revision_id: structured resource references
- actor_id, correlation_id: identity and request tracing
- before_json / after_json: state snapshots for the action
- reason: optional researcher explanation
- corrects_event_id: earlier event superseded by this correction
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "a3b4c5d6e7f8"
down_revision: Union[str, None] = "f7a8b9c0d1e2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("hypothesis_revisions") as batch_op:
        batch_op.add_column(sa.Column("previous_revision_id", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("change_reason", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("actor", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("actor_id", sa.String(length=128), nullable=True))
        batch_op.add_column(sa.Column("request_key", sa.String(length=128), nullable=True))
        # previous_revision_id is an application-maintained chain reference, not a
        # self-referential foreign key (see models.HypothesisRevisionModel).
        batch_op.create_unique_constraint(
            "uq_hypothesis_revision_request_key",
            ["hypothesis_id", "request_key"],
        )

    op.add_column(
        "evaluation_results",
        sa.Column("dataset_snapshot_id", sa.String(length=96), nullable=True),
    )
    op.create_index(
        "idx_evaluations_dataset_snapshot",
        "evaluation_results",
        ["dataset_snapshot_id"],
    )

    op.add_column("audit_events", sa.Column("hypothesis_id", sa.String(length=64), nullable=True))
    op.add_column("audit_events", sa.Column("revision_id", sa.String(length=64), nullable=True))
    op.add_column("audit_events", sa.Column("actor_id", sa.String(length=128), nullable=True))
    op.add_column("audit_events", sa.Column("correlation_id", sa.String(length=128), nullable=True))
    op.add_column("audit_events", sa.Column("before_json", sa.JSON(), nullable=True))
    op.add_column("audit_events", sa.Column("after_json", sa.JSON(), nullable=True))
    op.add_column("audit_events", sa.Column("reason", sa.Text(), nullable=True))
    op.add_column("audit_events", sa.Column("corrects_event_id", sa.String(length=64), nullable=True))
    op.create_index(
        "idx_audit_hypothesis_timestamp",
        "audit_events",
        ["hypothesis_id", "timestamp"],
    )
    op.create_index("idx_audit_correlation", "audit_events", ["correlation_id"])


def downgrade() -> None:
    op.drop_index("idx_audit_correlation", table_name="audit_events")
    op.drop_index("idx_audit_hypothesis_timestamp", table_name="audit_events")
    op.drop_column("audit_events", "corrects_event_id")
    op.drop_column("audit_events", "reason")
    op.drop_column("audit_events", "after_json")
    op.drop_column("audit_events", "before_json")
    op.drop_column("audit_events", "correlation_id")
    op.drop_column("audit_events", "actor_id")
    op.drop_column("audit_events", "revision_id")
    op.drop_column("audit_events", "hypothesis_id")

    op.drop_index("idx_evaluations_dataset_snapshot", table_name="evaluation_results")
    op.drop_column("evaluation_results", "dataset_snapshot_id")

    with op.batch_alter_table("hypothesis_revisions") as batch_op:
        batch_op.drop_constraint("uq_hypothesis_revision_request_key", type_="unique")
        batch_op.drop_column("request_key")
        batch_op.drop_column("actor_id")
        batch_op.drop_column("actor")
        batch_op.drop_column("change_reason")
        batch_op.drop_column("previous_revision_id")
