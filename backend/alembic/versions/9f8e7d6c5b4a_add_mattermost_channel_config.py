"""add Mattermost channel config

Revision ID: 9f8e7d6c5b4a
Revises: f1a2b3c4d5e6
Create Date: 2026-08-15
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "9f8e7d6c5b4a"
down_revision: str | None = "f1a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "mattermost_channel_config",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("mattermost_bot_id", sa.Integer(), nullable=False),
        sa.Column("channel_id", sa.String(), nullable=False),
        sa.Column(
            "is_ephemeral",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "time_created", sa.DateTime(timezone=True), server_default=sa.func.now()
        ),
        sa.Column(
            "time_updated",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["mattermost_bot_id"], ["mattermost_bot.id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint(
            "mattermost_bot_id",
            "channel_id",
            name="uq_mattermost_channel_config_bot_channel",
        ),
    )
    op.create_index(
        "ix_mattermost_channel_config_bot_enabled",
        "mattermost_channel_config",
        ["mattermost_bot_id", "enabled"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_mattermost_channel_config_bot_enabled",
        table_name="mattermost_channel_config",
    )
    op.drop_table("mattermost_channel_config")
