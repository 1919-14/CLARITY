"""merge researcher history and scientific report heads

Revision ID: c7d8e9f0a1b2
Revises: b5c6d7e8f9a0, b3c4d5e6f7a8
Create Date: 2026-10-24 10:15:00.000000

Issue #17 (``a3b4c5d6e7f8`` -> ``b5c6d7e8f9a0``) and the scientific reports
feature (``b3c4d5e6f7a8``) were developed from the same parent revision and
merged independently, which leaves two Alembic heads. This empty revision joins
them so ``upgrade head`` has a single target. It changes no schema.
"""
from typing import Sequence, Union


# revision identifiers, used by Alembic.
revision: str = "c7d8e9f0a1b2"
down_revision: Union[str, Sequence[str], None] = ("b5c6d7e8f9a0", "b3c4d5e6f7a8")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
