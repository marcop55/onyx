"""add Mattermost bot config

Revision ID: b8a1c2d3e4f5
Revises: d26e0f4ab1c7
Create Date: 2026-08-15
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "b8a1c2d3e4f5"
down_revision: str | None = "d26e0f4ab1c7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "mattermost_bot",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("url", sa.String(), nullable=False),
        sa.Column(
            "enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False
        ),
        sa.Column("token", sa.LargeBinary(), nullable=False),
        sa.Column("bot_user_id", sa.String(), nullable=False),
        sa.Column("bot_username", sa.String(), nullable=False),
        sa.Column(
            "health_status", sa.String(), server_default="unknown", nullable=False
        ),
        sa.Column("health_error", sa.Text(), nullable=True),
        sa.Column(
            "time_created",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "time_updated",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("token", name="uq_mattermost_bot_token"),
    )


def downgrade() -> None:
    op.drop_table("mattermost_bot")
