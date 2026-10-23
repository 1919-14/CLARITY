"""content-addressed dataset snapshots and revision request fingerprints (Issue #17)

Revision ID: b5c6d7e8f9a0
Revises: a3b4c5d6e7f8
Create Date: 2026-10-24 09:40:00.000000

Follow-up to a3b4c5d6e7f8 for two provenance gaps:

evaluation_results
- dataset_snapshot: canonical manifest of the exact observations evaluated
  (inputs, outputs, status, partition, sequence), so the dataset referenced by
  ``dataset_snapshot_id`` can be reconstructed rather than inferred from ids.

hypothesis_revisions
- request_fingerprint: canonical fingerprint of every semantically relevant
  revision-request field, so an idempotency key replayed with a different
  payload (for example a different model family) is rejected instead of
  returning the earlier revision.

Both columns are nullable, so existing rows stay valid.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "b5c6d7e8f9a0"
down_revision: Union[str, None] = "a3b4c5d6e7f8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "evaluation_results",
        sa.Column("dataset_snapshot", sa.JSON(), nullable=True),
    )
    with op.batch_alter_table("hypothesis_revisions") as batch_op:
        batch_op.add_column(
            sa.Column("request_fingerprint", sa.String(length=96), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("hypothesis_revisions") as batch_op:
        batch_op.drop_column("request_fingerprint")
    op.drop_column("evaluation_results", "dataset_snapshot")
