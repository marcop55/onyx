"""add Mattermost slash command config

Revision ID: e13a5d78b9c2
Revises: d26e0f4ab1c7
Create Date: 2026-08-15
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "e13a5d78b9c2"
down_revision: str | None = "d26e0f4ab1c7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "mattermost_slash_command_config",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("instance_id", sa.String(), nullable=False),
        sa.Column("bot_user_id", sa.String(), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("token", sa.LargeBinary(), nullable=False),
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
        sa.UniqueConstraint(
            "instance_id",
            "bot_user_id",
            name="uq_mattermost_slash_command_config_instance_bot",
        ),
    )
    op.create_index(
        "ix_mattermost_slash_command_config_lookup",
        "mattermost_slash_command_config",
        ["instance_id", "bot_user_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_mattermost_slash_command_config_lookup",
        table_name="mattermost_slash_command_config",
    )
    op.drop_table("mattermost_slash_command_config")
