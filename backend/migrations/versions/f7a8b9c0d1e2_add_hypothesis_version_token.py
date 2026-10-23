"""add version and version_token to hypotheses for atomic optimistic locking (Issue #16)

Revision ID: f7a8b9c0d1e2
Revises: e4b7c1a29d63
Create Date: 2026-10-23 22:30:00.000000

Adds:
- hypotheses.version: monotonically increasing integer counter for flag updates
- hypotheses.version_token: opaque unique token rotated on every update
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "f7a8b9c0d1e2"
down_revision: Union[str, None] = "e4b7c1a29d63"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "hypotheses",
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column(
        "hypotheses",
        sa.Column("version_token", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("hypotheses", "version_token")
    op.drop_column("hypotheses", "version")
