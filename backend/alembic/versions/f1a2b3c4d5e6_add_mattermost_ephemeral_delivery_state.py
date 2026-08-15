"""add Mattermost ephemeral delivery state

Revision ID: f1a2b3c4d5e6
Revises: e13a5d78b9c2
Create Date: 2026-08-15
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "f1a2b3c4d5e6"
down_revision: str | None = "e13a5d78b9c2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "mattermost_event_state",
        sa.Column("delivery_mode", sa.String(), nullable=True),
    )
    op.add_column(
        "mattermost_event_state",
        sa.Column("terminal_outcome", sa.String(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("mattermost_event_state", "terminal_outcome")
    op.drop_column("mattermost_event_state", "delivery_mode")
