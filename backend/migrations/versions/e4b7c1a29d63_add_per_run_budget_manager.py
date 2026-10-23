"""add per-run budget manager (Issue #14)

Revision ID: e4b7c1a29d63
Revises: c7d60426c008
Create Date: 2026-10-23 00:00:00.000000

Adds:
- runs.max_concurrent_remote_tasks, runs.max_generated_hypotheses,
  runs.budget_config_json, runs.deadline_at
- run_budget_usage: one authoritative counter row per run
- budget_reservations: idempotent admission ledger

Existing rows remain valid: new columns are nullable and usage rows are
created lazily with documented defaults by the budget service.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "e4b7c1a29d63"
down_revision: Union[str, None] = "c7d60426c008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "runs",
        sa.Column("max_concurrent_remote_tasks", sa.Integer(), nullable=True),
    )
    op.add_column(
        "runs",
        sa.Column("max_generated_hypotheses", sa.Integer(), nullable=True),
    )
    op.add_column("runs", sa.Column("budget_config_json", sa.JSON(), nullable=True))
    op.add_column(
        "runs",
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "run_budget_usage",
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("queries_reserved", sa.Integer(), nullable=False),
        sa.Column("queries_completed", sa.Integer(), nullable=False),
        sa.Column("queries_failed", sa.Integer(), nullable=False),
        sa.Column("queries_released", sa.Integer(), nullable=False),
        sa.Column("evaluations_reserved", sa.Integer(), nullable=False),
        sa.Column("evaluations_completed", sa.Integer(), nullable=False),
        sa.Column("evaluations_failed", sa.Integer(), nullable=False),
        sa.Column("evaluations_released", sa.Integer(), nullable=False),
        sa.Column("hypotheses_generated", sa.Integer(), nullable=False),
        sa.Column("hypotheses_released", sa.Integer(), nullable=False),
        sa.Column("active_remote_tasks", sa.Integer(), nullable=False),
        sa.Column("estimated_cost_usd", sa.Float(), nullable=True),
        sa.Column("accruing_cost_usd", sa.Float(), nullable=True),
        sa.Column("settled_cost_usd", sa.Float(), nullable=True),
        sa.Column("cost_data_state", sa.String(length=32), nullable=False),
        sa.Column("is_exhausted", sa.Boolean(), nullable=False),
        sa.Column("stop_reason", sa.String(length=64), nullable=True),
        sa.Column("exhausted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["runs.run_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("run_id"),
    )
    op.create_index(
        "idx_budget_usage_exhausted",
        "run_budget_usage",
        ["is_exhausted"],
        unique=False,
    )

    op.create_table(
        "budget_reservations",
        sa.Column("reservation_id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("resource", sa.String(length=32), nullable=False),
        sa.Column("operation_id", sa.String(length=128), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("details_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["run_id"], ["runs.run_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("reservation_id"),
        sa.UniqueConstraint(
            "run_id",
            "resource",
            "operation_id",
            name="uq_budget_reservation_operation",
        ),
    )
    op.create_index(
        "idx_budget_reservation_run_state",
        "budget_reservations",
        ["run_id", "resource", "state"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "idx_budget_reservation_run_state",
        table_name="budget_reservations",
    )
    op.drop_table("budget_reservations")
    op.drop_index("idx_budget_usage_exhausted", table_name="run_budget_usage")
    op.drop_table("run_budget_usage")

    op.drop_column("runs", "deadline_at")
    op.drop_column("runs", "budget_config_json")
    op.drop_column("runs", "max_generated_hypotheses")
    op.drop_column("runs", "max_concurrent_remote_tasks")
