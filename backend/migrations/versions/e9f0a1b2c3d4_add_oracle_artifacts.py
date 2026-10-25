"""uploaded black-box oracles (researcher-supplied simulators and models)

Revision ID: e9f0a1b2c3d4
Revises: d8e9f0a1b2c3
Create Date: 2026-10-25 14:05:00.000000

Adds the reusable oracle library and the optional run binding:

oracle_artifacts
- provenance for one uploaded artifact: its declared kind, original and stored
  file names, SHA-256, size, entrypoint, input/output names, search bounds and
  the registration probe. ``probe_json`` is a record of what was observed at
  registration, not a claimed ground truth.

runs.oracle_id
- nullable; ``NULL`` keeps the built-in local simulator. ``ON DELETE RESTRICT``
  so a bound artifact cannot be deleted out from under a historical run.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "e9f0a1b2c3d4"
down_revision: Union[str, None] = "d8e9f0a1b2c3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "oracle_artifacts",
        sa.Column("oracle_id", sa.String(length=64), primary_key=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("original_filename", sa.String(length=255), nullable=False),
        sa.Column("stored_filename", sa.String(length=255), nullable=False),
        sa.Column("storage_path", sa.String(length=512), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("entrypoint", sa.String(length=128), nullable=True),
        sa.Column("input_name", sa.String(length=64), nullable=False, server_default="x"),
        sa.Column("output_name", sa.String(length=64), nullable=False, server_default="y"),
        sa.Column("min_value", sa.Float(), nullable=False),
        sa.Column("max_value", sa.Float(), nullable=False),
        sa.Column("probe_json", sa.JSON(), nullable=True),
        sa.Column("created_by", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "idx_oracle_artifacts_created_at", "oracle_artifacts", ["created_at"]
    )

    with op.batch_alter_table("runs") as batch_op:
        batch_op.add_column(sa.Column("oracle_id", sa.String(length=64), nullable=True))
        batch_op.create_foreign_key(
            "fk_runs_oracle_id",
            "oracle_artifacts",
            ["oracle_id"],
            ["oracle_id"],
            ondelete="RESTRICT",
        )


def downgrade() -> None:
    with op.batch_alter_table("runs") as batch_op:
        batch_op.drop_constraint("fk_runs_oracle_id", type_="foreignkey")
        batch_op.drop_column("oracle_id")
    op.drop_index("idx_oracle_artifacts_created_at", table_name="oracle_artifacts")
    op.drop_table("oracle_artifacts")
