"""persist the effective model family on each hypothesis revision (Issue #17)

Revision ID: d8e9f0a1b2c3
Revises: c7d8e9f0a1b2
Create Date: 2026-10-24 12:10:00.000000

A revision is the scientific record of what was evaluated, so it must carry the
model family that was actually used. Previously evaluation read
``hypotheses.model_family`` (a mutable current-state field), which meant a
revision request that changed the family was accepted without being recorded on
the revision.

hypothesis_revisions
- model_family: effective, validated family for this revision. Existing rows are
  backfilled from their parent hypothesis, which is what earlier evaluations
  implicitly used. The column stays nullable so rows written before this
  migration remain valid if the backfill cannot run (for example a revision
  whose parent hypothesis no longer exists).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "d8e9f0a1b2c3"
down_revision: Union[str, None] = "c7d8e9f0a1b2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("hypothesis_revisions") as batch_op:
        batch_op.add_column(sa.Column("model_family", sa.String(length=64), nullable=True))

    # Backfill from the parent hypothesis: that is the family earlier revisions
    # were evaluated under before the column existed. Alias spellings are mapped
    # to the canonical family the expression validator would resolve them to, so
    # new rows and backfilled rows agree.
    op.execute(
        """
        UPDATE hypothesis_revisions
        SET model_family = (
            SELECT CASE
                WHEN LOWER(h.model_family) IN (
                    'linear', 'quadratic', 'cubic', 'constant',
                    '1d_linear', '1d_polynomial'
                ) THEN 'polynomial'
                ELSE h.model_family
            END
            FROM hypotheses h
            WHERE h.hypothesis_id = hypothesis_revisions.hypothesis_id
        )
        WHERE model_family IS NULL
        """
    )


def downgrade() -> None:
    with op.batch_alter_table("hypothesis_revisions") as batch_op:
        batch_op.drop_column("model_family")
